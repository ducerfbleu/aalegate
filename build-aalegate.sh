#!/usr/bin/env bash
# build-aalegate.sh---build the shared aalegate PLATFORM images (recorder + egress + tui).
#   aalegate-gateway       L7 recorder (Go static -> scratch)
#   aalegate-egress        Go CONNECT allow-list proxy
#   aalegate-egress-proxy  squid domain-allowlist proxy (built for --engine docker only; --runtime docker)
#   aalegate-tui           read-only audit-log reader (Go stdlib + golang.org/x/term + x/text)
#
# Harness-agnostic and GPU-agnostic: built once, shared by every harness and runtime.
# Agent images build separately, one script per harness:
#   ./build-pi.sh     [--cuda]   pi coding agent (CPU, or NVIDIA CUDA variant)
#   ./build-claude.sh            Claude Code (Anthropic Messages API)
#
# Engine: docker by default; pass --engine podman (or set AALGT_ENGINE=podman) for a rootless,
# daemonless build. podman's image store is SEPARATE from docker's --- images built with docker
# are invisible to `aalegate-run --runtime podman`, so build with the SAME engine you run with.
# Rootless podman with no systemd user session needs cgroupfs; set it once, durably, in
# ~/.config/containers/containers.conf under [engine]:  cgroup_manager = "cgroupfs"
# (or `loginctl enable-linger "$USER"` then reconnect).
#
# Usage: ./build-aalegate.sh [--engine docker|podman]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

ENGINE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --engine) ENGINE="$2"; shift 2 ;;
    *) echo "build-aalegate.sh: unknown arg '$1'" >&2; exit 2 ;;
  esac
done
: "${ENGINE:=${AALGT_ENGINE:-docker}}"
command -v "$ENGINE" >/dev/null 2>&1 || { echo "build-aalegate.sh: engine '$ENGINE' not found on PATH" >&2; exit 1; }

echo "== aalegate-gateway (L7 recorder; Go static -> scratch) [$ENGINE] =="
"$ENGINE" build -t aalegate-gateway -f "$HERE/gateway/Dockerfile.gateway" "$HERE/gateway"

echo "== aalegate-egress (Go CONNECT proxy) [$ENGINE] =="
"$ENGINE" build -t aalegate-egress -f "$HERE/egress/Dockerfile.egress" "$HERE/egress"

# squid is the DOCKER runtime's egress proxy (--runtime docker --egress). podman/apptainer use the
# Go aalegate-egress above, so skip squid + its ubuntu pull unless we're actually building for docker.
proxy_note=""
if [ "$ENGINE" = docker ]; then
  echo "== aalegate-egress-proxy (squid; --runtime docker --egress) [$ENGINE] =="
  "$ENGINE" build -t aalegate-egress-proxy -f "$HERE/Dockerfile.proxy" "$HERE"
  proxy_note=" + aalegate-egress-proxy"
else
  echo "== aalegate-egress-proxy: skipped (squid is docker-only; $ENGINE uses the Go aalegate-egress) =="
fi

echo "== aalegate-tui (read-only audit-log reader) =="
(cd "$HERE/tui" && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o "$HERE/bin/aalegate-tui" .)

echo "Done (engine: $ENGINE): aalegate-gateway + aalegate-egress${proxy_note} + aalegate-tui"
