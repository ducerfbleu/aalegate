#!/usr/bin/env bash
# build-claude.sh---build the Claude Code agent image (aalegate-claude-code).
#
# Node-less: the Dockerfile installs the native standalone `claude` binary. Context is the
# aalegate root so the shared entrypoint (harnesses/entrypoint.sh) and the claude manifest
# (harnesses/claude-code/harness.json) resolve. Needs network to fetch the installer.
#
# Usage: ./build-claude.sh [--engine docker|podman] [--tag NAME] [-- <extra build args>]
#   Engine defaults to $AALE_ENGINE, else docker; build with the SAME engine you run with
#   (podman's rootless image store is separate from docker's daemon store).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TAG="aalegate-claude-code"
ENGINE=""

extra=()
while [ $# -gt 0 ]; do
  case "$1" in
    --tag)    TAG="$2"; shift 2 ;;
    --engine) ENGINE="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --)       shift; extra=("$@"); break ;;
    *)        echo "build-claude.sh: unknown arg '$1'" >&2; exit 2 ;;
  esac
done
: "${ENGINE:=${AALE_ENGINE:-docker}}"
command -v "$ENGINE" >/dev/null 2>&1 || { echo "build-claude.sh: engine '$ENGINE' not found on PATH" >&2; exit 1; }

echo "== $TAG (Claude Code; native standalone binary, no node) [$ENGINE] =="
"$ENGINE" build -t "$TAG" -f "$HERE/harnesses/claude-code/Dockerfile" "${extra[@]}" "$HERE"
echo "Done: $TAG"
