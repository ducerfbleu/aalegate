#!/usr/bin/env python3
"""probe-node-proxy.py---does Node reach the web through the aalegate egress proxy?

Answers plan §7 "tools that ignore HTTPS_PROXY": plain Node fetch/https ignore the proxy
variables unless NODE_USE_ENV_PROXY=1 (recent Node 22/24). Run it INSIDE the agent; python3
is in every harness image. No model or key needed: --shell stops before the agent starts.

  mkdir -p /tmp/probe && cp probe-node-proxy.py /tmp/probe/
  ./aalegate-run --runtime podman --agent pi --llm http://127.0.0.1:9 --llm-key dummy \\
      --allow example.com --work /tmp/probe --shell
  #   inside:  python3 probe-node-proxy.py ; exit
  ./show-access.py        # on the host, that run: example.com allowed (2, 5, 7, 8),
                          # example.org denied (3); nothing from 1 or 4

Usage:
  probe-node-proxy.py [--allowed URL] [--denied URL] [--timeout SEC]

Probes (each in a child process with its own env):
  1 fetch  allowed, no flag       expect fail     Node ignores the proxy; no way around it
  2 fetch  allowed, flag on       expect ok       fetch goes through the proxy
  3 fetch  denied,  flag on       expect denied   allowlist enforced on the Node path
  4 https  allowed, no flag       expect fail     same as 1, old https module (axios, SDKs)
  5 https  allowed, flag on       info            flag covers https? depends on Node version
  6 fetch  base URL, flag on      expect ok       NO_PROXY keeps the model path direct
  7 python allowed (control)      expect ok       the proxy itself works
  8 curl   allowed (control)      expect ok       bash tools work (skipped without curl)

"ok" means any HTTP status (the request got through; a 404 is fine). A proxy refusal of an
https URL is a failed tunnel, never a status: python/curl say "403", Node 24 only says
"Request was cancelled." So "denied" means: failed, but not locally (DNS or no route) --- the
request reached the proxy. The host's access log (DENY) is the ground truth.

Exit 0 when every expectation holds, 1 otherwise. Probes 1-6 are skipped without node.
Stdlib only.
"""
import argparse
import os
import shutil
import subprocess
import sys

FLAG = "NODE_USE_ENV_PROXY"
# failures that never left the agent: no resolver, no route
LOCAL_ERRORS = ("ENOTFOUND", "EAI_AGAIN", "ECONNREFUSED", "ENETUNREACH", "EHOSTUNREACH",
                "Name or service not known", "Temporary failure in name resolution")

NODE_FETCH = """
fetch(process.argv[1], { signal: AbortSignal.timeout(+process.argv[2] * 1000) })
  .then(r => console.log("HTTP " + r.status))
  .catch(e => console.log("FAIL " + (e.cause?.code || "") + " " + (e.cause?.message || e.message)));
"""

NODE_HTTPS = """
const req = require("https").get(process.argv[1], r => { console.log("HTTP " + r.statusCode); r.resume(); });
req.setTimeout(+process.argv[2] * 1000, () => req.destroy(new Error("timeout")));
req.on("error", e => console.log("FAIL " + (e.code || "") + " " + e.message));
"""

PY_URLLIB = """
import sys, urllib.request, urllib.error
try:
    r = urllib.request.urlopen(sys.argv[1], timeout=float(sys.argv[2]))
    print("HTTP", r.status)
except urllib.error.HTTPError as e:
    print("HTTP", e.code)
except Exception as e:
    print("FAIL", e)
"""


def child_env(flag):
    env = dict(os.environ)
    env.pop(FLAG, None)
    if flag:
        env[FLAG] = "1"
    return env


def run(kind, url, flag, timeout):
    if kind == "fetch":
        cmd = ["node", "-e", NODE_FETCH, url, str(timeout)]
    elif kind == "https":
        cmd = ["node", "-e", NODE_HTTPS, url, str(timeout)]
    elif kind == "python":
        cmd = [sys.executable, "-c", PY_URLLIB, url, str(timeout)]
    else:  # curl
        cmd = ["curl", "-s", "-o", "/dev/null", "-m", str(timeout), "-w", "HTTP %{http_code}", url]
    try:
        p = subprocess.run(cmd, env=child_env(flag), capture_output=True, text=True,
                           timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return "FAIL child timeout"
    out = (p.stdout.strip() or p.stderr.strip() or f"FAIL exit {p.returncode}").splitlines()[-1]
    if kind == "curl" and out == "HTTP 000":
        out = f"FAIL curl exit {p.returncode}"
    return out


def status(out):
    return int(out.split()[1]) if out.startswith("HTTP ") and out.split()[1].isdigit() else None


def verdict(expect, out):
    code = status(out)
    if expect == "info":
        return "INFO"
    if expect == "ok":
        ok = code is not None
    elif expect == "fail":
        ok = code is None
    else:  # denied: failed at the proxy, not locally
        ok = code == 403 or (code is None and not any(e in out for e in LOCAL_ERRORS))
    return "PASS" if ok else "FAIL"


def main():
    ap = argparse.ArgumentParser(description="Probe Node's proxy behaviour inside an aalegate agent.")
    ap.add_argument("--allowed", default="https://example.com", help="a URL on the run's allowlist")
    ap.add_argument("--denied", default="https://example.org", help="a URL NOT on the allowlist")
    ap.add_argument("--timeout", type=float, default=10.0, help="seconds per probe")
    a = ap.parse_args()

    base = os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    node = shutil.which("node")

    if not base:
        print("WARNING: no ANTHROPIC_BASE_URL / OPENAI_BASE_URL --- this doesn't look like an aalegate\n"
              "         agent. Run it INSIDE the agent (aalegate-run ... --shell); results from the\n"
              "         host or a plain shell say nothing about the airgap.\n", file=sys.stderr)

    print("== environment")
    if node:
        v = subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip()
        print(f"node {v}")
    else:
        print("node: not found --- skipping Node probes 1-6")
    for k in sorted(os.environ):
        if k.lower() in ("http_proxy", "https_proxy", "no_proxy", FLAG.lower()) or k.endswith("_BASE_URL"):
            print(f"{k}={os.environ[k]}")
    print()

    probes = [  # (n, label, kind, url, flag, expect, needs)
        (1, "fetch  allowed, no flag ", "fetch", a.allowed, False, "fail", node),
        (2, "fetch  allowed, flag on ", "fetch", a.allowed, True, "ok", node),
        (3, "fetch  denied,  flag on ", "fetch", a.denied, True, "denied", node),
        (4, "https  allowed, no flag ", "https", a.allowed, False, "fail", node),
        (5, "https  allowed, flag on ", "https", a.allowed, True, "info", node),
        (6, "fetch  base URL, flag on", "fetch", base, True, "ok", node and base),
        (7, "python allowed (control)", "python", a.allowed, False, "ok", True),
        (8, "curl   allowed (control)", "curl", a.allowed, False, "ok", shutil.which("curl")),
    ]

    print("== probes")
    failed = 0
    for n, label, kind, url, flag, expect, needs in probes:
        if not needs:
            print(f"{n} {label}  SKIP")
            continue
        out = run(kind, url, flag, a.timeout)
        v = verdict(expect, out)
        failed += v == "FAIL"
        print(f"{n} {label}  {v:4}  expect {expect:7}  got {out}")

    print()
    print("all expectations hold" if not failed else f"{failed} probe(s) did not match")
    if node and not failed:
        print(f"-> {FLAG}=1 routes Node through the proxy (aalegate-run sets it whenever egress is on)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
