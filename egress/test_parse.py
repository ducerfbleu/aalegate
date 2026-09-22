#!/usr/bin/env python3
"""test_parse.py --- verify show-access.py parses all Go egress proxy log line types.

Usage:  python3 test_parse.py        (from aalegate/egress/ or aalegate/)
"""
import importlib.util
import sys
from pathlib import Path

# load show-access.py (hyphen in name prevents normal import)
for candidate in [Path(__file__).parent.parent / "show-access.py",
                  Path(__file__).parent / "../show-access.py",
                  Path("show-access.py")]:
    if candidate.is_file():
        spec = importlib.util.spec_from_file_location("sa", str(candidate.resolve()))
        sa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sa)
        break
else:
    sys.exit("show-access.py not found")

pass_n, fail_n = 0, 0

def check(name, got, **expect):
    global pass_n, fail_n
    if got is None:
        print(f"  FAIL  {name}: parsed to None")
        fail_n += 1
        return
    errors = []
    for k, v in expect.items():
        actual = got.get(k)
        if actual != v:
            errors.append(f"{k}: got {actual!r}, expected {v!r}")
    if errors:
        print(f"  FAIL  {name}: {'; '.join(errors)}")
        fail_n += 1
    else:
        print(f"  PASS  {name}")
        pass_n += 1


# ---- Go proxy log lines (current format) ------------------------------------

print("=== Go proxy: HTTP forward GET ===")
check("GET allow",
      sa.parse_line("2026-09-22T02:22:22Z 127.0.0.1 ALLOW GET 127.0.0.1:42189 status=200 resp=32 dur=0ms"),
      verdict="ALLOW", method="GET", target="127.0.0.1:42189", status=200, recv=32, dur_ms=0, sent=None)

print("=== Go proxy: HTTP forward POST ===")
check("POST allow",
      sa.parse_line("2026-09-22T02:22:22Z 10.0.0.5 ALLOW POST 10.0.0.5:8080 status=201 resp=4523 dur=150ms"),
      verdict="ALLOW", method="POST", target="10.0.0.5:8080", status=201, recv=4523, dur_ms=150)

print("=== Go proxy: SSE streaming (large response, long duration) ===")
check("SSE allow",
      sa.parse_line("2026-09-22T02:22:23Z 127.0.0.1 ALLOW GET 127.0.0.1:42189 status=200 resp=89 dur=502ms"),
      verdict="ALLOW", method="GET", recv=89, dur_ms=502)

print("=== Go proxy: CONNECT allow ===")
check("CONNECT allow",
      sa.parse_line("2026-09-22T02:22:22Z 127.0.0.1 ALLOW 192.168.1.100:443"),
      verdict="ALLOW", method=None, target="192.168.1.100:443", status=None, sent=None, recv=None)

print("=== Go proxy: CONNECT close (tunnel stats) ===")
check("CONNECT close",
      sa.parse_line("2026-09-22T02:22:22Z 127.0.0.1 CLOSE 192.168.1.100:443 sent=1234 recv=5678 dur=502ms"),
      verdict="CLOSE", method=None, target="192.168.1.100:443", sent=1234, recv=5678, dur_ms=502)

print("=== Go proxy: DENY host ===")
check("deny host",
      sa.parse_line("2026-09-22T02:22:23Z 127.0.0.1 DENY GET 10.99.99.99:80"),
      verdict="DENY", method="GET", target="10.99.99.99:80")

print("=== Go proxy: DENY port (CONNECT) ===")
check("deny port",
      sa.parse_line("2026-09-22T02:22:23Z 127.0.0.1 DENY 127.0.0.1:9999"),
      verdict="DENY", target="127.0.0.1:9999")

print("=== Go proxy: DENY-SIZE ===")
check("deny size",
      sa.parse_line("2026-09-22T02:22:23Z 127.0.0.1 DENY-SIZE POST 127.0.0.1:42189 body=2048 max=1024"),
      verdict="DENY-SIZE", method="POST", target="127.0.0.1:42189", req_body=2048, max_body=1024)

print("=== Go proxy: DENY-METHOD ===")
check("deny method",
      sa.parse_line("2026-09-22T02:22:23Z 127.0.0.1 DENY-METHOD GET /relative-uri"),
      verdict="DENY-METHOD", method="GET", target="/relative-uri")

print("=== Go proxy: FAIL ===")
check("fail",
      sa.parse_line("2026-09-22T02:22:23Z 127.0.0.1 FAIL POST 127.0.0.1:42189 dur=5ms"),
      verdict="FAIL", method="POST", target="127.0.0.1:42189", dur_ms=5)

print("=== Go proxy: domain target (not IP) ===")
check("domain target",
      sa.parse_line("2026-09-22T02:22:22Z 172.18.0.3 ALLOW api.openai.com:443"),
      verdict="ALLOW", method=None, target="api.openai.com:443")

print("=== Go proxy: empty / blank lines (expect None) ===")

# ---- Legacy squid log lines (pre-migration, read-only) ----------------------

print("\n=== Legacy squid: CONNECT allow ===")
check("squid connect",
      sa.parse_line("2026-09-20 14:30:00    150 172.18.0.3 TCP_TUNNEL/200 4523 CONNECT api.github.com:443 - -"),
      verdict="ALLOW", target="api.github.com:443", method=None, recv=4523, dur_ms=150)

print("=== Legacy squid: CONNECT deny ===")
check("squid deny",
      sa.parse_line("2026-09-20 14:30:01      5 172.18.0.3 TCP_DENIED/403 0 CONNECT evil.com:443 - -"),
      verdict="DENY", target="evil.com:443", recv=0, dur_ms=5)

print("=== Legacy squid: HTTP GET ===")
check("squid http",
      sa.parse_line("2026-09-20 14:30:02    200 172.18.0.3 TCP_MISS/200 8192 GET http://pypi.org/simple/ - -"),
      verdict="ALLOW", method="GET", target="http://pypi.org/simple/", recv=8192, dur_ms=200)

# ---- summary ----------------------------------------------------------------

print("\n=== summary output ===")
lines = [
    "2026-09-22T02:22:22Z 127.0.0.1 ALLOW GET 127.0.0.1:42189 status=200 resp=32 dur=0ms",
    "2026-09-22T02:22:23Z 127.0.0.1 ALLOW GET 127.0.0.1:42189 status=200 resp=89 dur=502ms",
    "2026-09-22T02:22:23Z 127.0.0.1 DENY-SIZE POST 127.0.0.1:42189 body=2048 max=1024",
    "2026-09-22T02:22:23Z 127.0.0.1 DENY GET 10.99.99.99:80",
    "2026-09-22T02:22:22Z 127.0.0.1 ALLOW 192.168.1.100:443",
    "2026-09-22T02:22:22Z 127.0.0.1 CLOSE 192.168.1.100:443 sent=1234 recv=5678 dur=502ms",
]
entries = [sa.parse_line(l) for l in lines if sa.parse_line(l)]
sa.print_summary(entries)

# ---- results -----------------------------------------------------------------

# handle the None checks specially
if sa.parse_line("") is None:
    pass_n += 1
    print("  PASS  empty -> None")
else:
    fail_n += 1
    print("  FAIL  empty -> should be None")
if sa.parse_line("   ") is None:
    pass_n += 1
    print("  PASS  whitespace -> None")
else:
    fail_n += 1
    print("  FAIL  whitespace -> should be None")

print(f"\n=== results: {pass_n} passed, {fail_n} failed ===")
sys.exit(1 if fail_n else 0)
