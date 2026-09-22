#!/usr/bin/env python3
"""show-access.py---dump the plane-2 egress log of an aalegate run.

Usage:
  ./show-access.py [RUN_ID]               # a run; on a TTY with no args, an interactive project picker
  ./show-access.py --pick                 # picker: up to 10 recent projects, type to fuzzy-filter
  ./show-access.py --project DIR|NAME     # latest run of a project (a path, or a fuzzy handle/name)
  ./show-access.py --summary              # totals only: per-domain counts, bytes, denied
  ./show-access.py --root DIR RUN_ID

Parses aalegate-egress (Go proxy) log format. Legacy squid logs from pre-migration
runs are also supported (read-only; new runs always use the Go proxy). Stdlib only.
"""
import json
import re
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# ---- log parsers -------------------------------------------------------------

# Go proxy log lines (current format):
#   CONNECT: <ts> <client> ALLOW <host:port>
#            <ts> <client> CLOSE <host:port> sent=N recv=N dur=Nms
#   HTTP fw: <ts> <client> ALLOW <METHOD> <host:port> status=N resp=N dur=Nms
#   Deny:    <ts> <client> DENY <METHOD> <host:port>
#            <ts> <client> DENY-SIZE <METHOD> <host:port> body=N max=N
#            <ts> <client> DENY-METHOD <METHOD> <uri>
#            <ts> <client> FAIL <METHOD> <host:port> dur=Nms
GO_RE = re.compile(
    r'^(?P<ts>\S+)\s+(?P<client>\S+)\s+'
    r'(?P<verdict>ALLOW|DENY|DENY-METHOD|DENY-SIZE|FAIL|CLOSE)\s+'
    r'(?P<rest>.+)$'
)
GO_KV = re.compile(r'(?P<key>\w+)=(?P<val>\d+)(?:ms|MB)?')

# Legacy squid logformat (pre-migration docker runs):
#   <ts> <elapsed> <client> <result/code> <bytes> <method> <target> ...
SQUID_RE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(?P<elapsed>\d+)\s+(?P<client>\S+)\s+'
    r'(?P<result>\S+)\s+(?P<bytes>\d+)\s+(?P<method>\S+)\s+(?P<target>\S+)'
)


def _extract_host(target_str):
    """Extract host:port from a target string that may have a leading HTTP method."""
    parts = target_str.split()
    for p in reversed(parts):
        if ":" in p or "." in p:
            return p
    return parts[-1] if parts else target_str


def parse_line(line):
    line = line.strip()
    if not line:
        return None

    m = GO_RE.match(line)
    if m:
        ts, client, verdict = m["ts"], m["client"], m["verdict"]
        rest = m["rest"]
        kvs = {km["key"]: int(km["val"]) for km in GO_KV.finditer(rest)}
        # strip key=value pairs from rest to get the target portion
        target = GO_KV.sub("", rest).strip()
        method = None
        host = target
        # HTTP forward lines have "METHOD host:port" as target
        tparts = target.split(None, 1)
        if len(tparts) == 2 and tparts[0].isupper() and tparts[0].isalpha():
            method = tparts[0]
            host = tparts[1]
        return {
            "ts": ts, "client": client, "verdict": verdict,
            "target": host, "method": method,
            "status": kvs.get("status"),
            "sent": kvs.get("sent"), "recv": kvs.get("resp") or kvs.get("recv"),
            "dur_ms": kvs.get("dur"),
            "req_body": kvs.get("body"), "max_body": kvs.get("max"),
        }

    m = SQUID_RE.match(line)
    if m:
        d = m.groupdict()
        result = d["result"]
        code = result.split("/")[-1] if "/" in result else result
        method = d["method"]
        if code.startswith("2"):
            verdict = "ALLOW"
        elif code.startswith("4"):
            verdict = "DENY"
        else:
            verdict = "FAIL"
        return {
            "ts": d["ts"], "client": d["client"], "verdict": verdict,
            "target": d["target"], "method": method if method != "CONNECT" else None,
            "status": int(code) if code.isdigit() else None,
            "sent": None, "recv": int(d["bytes"]),
            "dur_ms": int(d["elapsed"]),
            "req_body": None, "max_body": None,
        }
    return None


# ---- session picker (mirrors show-log.py) ------------------------------------

def _projects(root):
    out = []
    if not root.is_dir():
        return out
    for h in root.iterdir():
        if not h.is_dir():
            continue
        runs = list(h.glob("*/index.json"))
        if runs:
            out.append((h, len(runs), max(p.stat().st_mtime for p in runs)))
    out.sort(key=lambda t: t[2], reverse=True)
    return out


def _fuzzy(query, name):
    q, n = query.lower().strip(), name.lower()
    if not q:
        return 0
    i = n.find(q)
    if i >= 0:
        return 1000 - i
    it = iter(n)
    return 100 if all(c in it for c in q) else None


def _latest_run(scope):
    runs = sorted(scope.glob("*/index.json"), key=lambda p: p.stat().st_mtime)
    if not runs:
        sys.exit(f"no runs under {scope}")
    return runs[-1].parent


def _resolve_project(root, value):
    p = Path(value).expanduser()
    if p.is_dir():
        from recorder import project_handle
        h = root / project_handle(str(p))
        return h if h.is_dir() else None
    hits = [h for (h, _c, _m) in _projects(root) if _fuzzy(value, h.name) is not None]
    return hits[0] if len(hits) == 1 else None


def pick_project(root, initial=""):
    projs = _projects(root)
    if not projs:
        sys.exit(f"no projects under {root}")
    query = initial
    while True:
        ranked = [(s, h, c, m) for (h, c, m) in projs
                  for s in (_fuzzy(query, h.name),) if s is not None]
        ranked.sort(key=lambda t: (t[0], t[3]), reverse=True)
        shown = ranked[:10]
        print(f"\nprojects under {root}" + (f"  filter={query!r}" if query else ""),
              file=sys.stderr)
        for i, (_s, h, c, m) in enumerate(shown, 1):
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m))
            print(f"  {i:>2}  {h.name:<34} {c:>3} run{' ' if c == 1 else 's'}  {ts}",
                  file=sys.stderr)
        if not shown:
            print("  (no match)", file=sys.stderr)
        print("select #, type to filter, Enter=1, q=quit: ", end="", file=sys.stderr, flush=True)
        try:
            ans = input().strip()
        except EOFError:
            sys.exit(0)
        if ans in ("q", "Q"):
            sys.exit(0)
        if ans == "":
            if shown:
                return shown[0][1]
            continue
        if ans.isdigit():
            k = int(ans)
            if 1 <= k <= len(shown):
                return shown[k - 1][1]
            print(f"  # out of range (1-{len(shown)})", file=sys.stderr)
            continue
        query = ans


def resolve(argv):
    from recorder import DEFAULT_AUDIT_ROOT
    root = DEFAULT_AUDIT_ROOT
    run = project = None
    pick = False
    it = iter(argv)
    for a in it:
        if a == "--root":
            root = Path(next(it))
        elif a == "--project":
            project = next(it, "")
        elif a in ("--pick", "-i"):
            pick = True
        else:
            run = a
    if run:
        for cand in (root / run, *sorted(root.glob(f"*/{run}"))):
            if (cand / "index.json").exists() or cand.is_dir():
                return cand
        sys.exit(f"no such run: {run}")
    if pick:
        return _latest_run(pick_project(root, project or ""))
    if project is not None:
        scope = _resolve_project(root, project)
        return _latest_run(scope) if scope else _latest_run(pick_project(root, project))
    if sys.stdin.isatty() and sys.stderr.isatty():
        return _latest_run(pick_project(root, ""))
    runs = list(root.glob("*/*/index.json")) + list(root.glob("*/index.json"))
    runs = sorted(runs, key=lambda p: p.stat().st_mtime)
    if not runs:
        sys.exit(f"no runs under {root}")
    return runs[-1].parent


# ---- display -----------------------------------------------------------------

VERDICT_COLOR = {
    "ALLOW": "\033[32m", "CLOSE": "\033[36m",
    "DENY": "\033[31m", "DENY-METHOD": "\033[31m", "DENY-SIZE": "\033[31m", "FAIL": "\033[33m",
}
RESET = "\033[0m"


def fmt_bytes(n):
    if n is None:
        return "-"
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}K"
    return f"{n / (1024 * 1024):.1f}M"


def print_entry(e, color=True):
    v = e["verdict"]
    vc = VERDICT_COLOR.get(v, "") if color else ""
    r = RESET if color and vc else ""
    target = e["target"]
    if e.get("method"):
        target = e["method"] + " " + target
    parts = [e["ts"], e["client"], f"{vc}{v:<12}{r}", target]
    if e.get("status") is not None:
        parts.append(f"status={e['status']}")
    if e["sent"] is not None or e["recv"] is not None:
        parts.append(f"sent={fmt_bytes(e['sent'])}")
        parts.append(f"recv={fmt_bytes(e['recv'])}")
    if e.get("dur_ms") is not None:
        parts.append(f"dur={e['dur_ms']}ms")
    if e.get("req_body") is not None:
        parts.append(f"body={fmt_bytes(e['req_body'])}")
        if e.get("max_body") is not None:
            parts.append(f"max={fmt_bytes(e['max_body'])}")
    print("  ".join(parts))


def print_summary(entries):
    domains = {}
    for e in entries:
        target = e["target"]
        host = target.rsplit(":", 1)[0] if ":" in target else target
        if host not in domains:
            domains[host] = {"allow": 0, "deny": 0, "close": 0,
                             "sent": 0, "recv": 0, "dur_ms": 0}
        d = domains[host]
        v = e["verdict"]
        if v == "ALLOW":
            d["allow"] += 1
            d["recv"] += e["recv"] or 0
            d["dur_ms"] += e["dur_ms"] or 0
        elif v == "CLOSE":
            d["close"] += 1
            d["sent"] += e["sent"] or 0
            d["recv"] += e["recv"] or 0
            d["dur_ms"] += e["dur_ms"] or 0
        elif v in ("DENY", "DENY-METHOD", "DENY-SIZE"):
            d["deny"] += 1

    total_allow = sum(d["allow"] for d in domains.values())
    total_deny = sum(d["deny"] for d in domains.values())
    total_close = sum(d["close"] for d in domains.values())
    total_sent = sum(d["sent"] for d in domains.values())
    total_recv = sum(d["recv"] for d in domains.values())

    print(f"\n===== egress summary =====")
    if not domains:
        print("  (no egress entries)")
        return
    print(f"\n  {'host':<40} {'allow':>6} {'deny':>6} {'tunnels':>8} {'sent':>10} {'recv':>10}")
    print(f"  {'---':<40} {'---':>6} {'---':>6} {'---':>8} {'---':>10} {'---':>10}")
    for host in sorted(domains, key=lambda h: domains[h]["recv"], reverse=True):
        d = domains[host]
        print(f"  {host:<40} {d['allow']:>6} {d['deny']:>6} {d['close']:>8} "
              f"{fmt_bytes(d['sent']):>10} {fmt_bytes(d['recv']):>10}")
    print(f"  {'---':<40} {'---':>6} {'---':>6} {'---':>8} {'---':>10} {'---':>10}")
    print(f"  {'TOTAL':<40} {total_allow:>6} {total_deny:>6} {total_close:>8} "
          f"{fmt_bytes(total_sent):>10} {fmt_bytes(total_recv):>10}")
    if total_deny:
        print(f"\n  {total_deny} denied request{'s' if total_deny != 1 else ''} "
              f"(agent attempted to reach non-allowlisted destinations)")


# ---- main --------------------------------------------------------------------

def main():
    summary_only = False
    rest = []
    for a in sys.argv[1:]:
        if a in ("--summary", "-s"):
            summary_only = True
        else:
            rest.append(a)

    d = resolve(rest)
    access_log = d / "access.log"

    if not access_log.exists():
        idx = {}
        if (d / "index.json").exists():
            try:
                idx = json.loads((d / "index.json").read_text())
            except ValueError:
                pass
        egress = idx.get("egress", "none")
        if egress == "none":
            print(f"run {d.name}: no egress was configured (fully airgapped)")
        else:
            print(f"run {d.name}: egress={egress} but no access.log found")
        return

    entries = []
    for line in open(access_log):
        e = parse_line(line)
        if e:
            entries.append(e)

    if not entries:
        print(f"run {d.name}: access.log is empty (no egress activity)")
        return

    color = sys.stdout.isatty()

    if not summary_only:
        print(f"===== {d} =====")
        if (d / "index.json").exists():
            try:
                idx = json.loads((d / "index.json").read_text())
                print(f"  egress: {idx.get('egress', 'unknown')}")
                print(f"  agent:  {idx.get('agent', 'unknown')}")
                print(f"  run_id: {idx.get('run_id', 'unknown')}")
            except ValueError:
                pass
        print(f"\n===== access.log ({len(entries)} entries) =====\n")
        for e in entries:
            print_entry(e, color=color)

    print_summary(entries)


if __name__ == "__main__":
    main()
