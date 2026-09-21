#!/usr/bin/env bash
# build-pi.sh---build the pi coding-agent image (aalegate-pi / aalegate-pi-cuda).
#
# Three build paths; each Dockerfile's COPY paths dictate a specific context (encoded below
# so you don't have to remember them):
#
#   (default) from-npm     harnesses/pi/Dockerfile            context = aalegate root
#             installs pi from its official npm package (@earendil-works/pi-coding-agent). No
#             source tree, no mount; builds with docker OR podman.
#
#   --from-base            harnesses/pi/Dockerfile.from-base  context = harnesses/
#             reuses a prebuilt `harness-pi` image (fast; no recompile). Needs `harness-pi` locally.
#
#   --cuda                 harnesses/pi-cuda/Dockerfile...    context = pi-agent repo root
#             CUDA 12.8 / Ubuntu 22.04 variant. Stage 1 does `COPY pi-build /build`, so the repo
#             root must contain pi-build/. NOTE: that Dockerfile also COPYs `aalegate-docker/...`
#             --- if this checkout's platform dir is `aalegate/`, fix that prefix or symlink first.
#
# Usage: ./build-pi.sh [--cuda | --from-base] [--engine docker|podman] [--pi-src DIR] [--tag NAME]
#   Engine defaults to $AALE_ENGINE, else docker; --engine overrides. Build with the SAME
#   engine you run with --- podman's rootless image store is separate from docker's daemon store.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"      # aalegate root
REPO_ROOT="$(cd "$HERE/.." && pwd)"        # pi-agent repo root (holds pi-build/)
MODE="from-source"
ENGINE=""                                  # default chosen per-mode below
PI_SRC="$REPO_ROOT/pi-build"
TAG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --cuda)      MODE="cuda"; shift ;;
    --from-base) MODE="from-base"; shift ;;
    --engine)    ENGINE="$2"; shift 2 ;;
    --pi-src)    PI_SRC="$2"; shift 2 ;;
    --tag)       TAG="$2"; shift 2 ;;
    *) echo "build-pi.sh: unknown arg '$1'" >&2; exit 2 ;;
  esac
done

case "$MODE" in
  from-source)
    : "${ENGINE:=${AALE_ENGINE:-docker}}"   # npm-install Dockerfile: no source mount; docker or podman
    TAG="${TAG:-aalegate-pi}"
    echo "== $TAG (npm: @earendil-works/pi-coding-agent) =="
    exec "$ENGINE" build -t "$TAG" -f "$HERE/harnesses/pi/Dockerfile" "$HERE"
    ;;
  from-base)
    : "${ENGINE:=${AALE_ENGINE:-docker}}"
    TAG="${TAG:-aalegate-pi}"
    echo "== $TAG (from prebuilt harness-pi base) =="
    exec "$ENGINE" build -t "$TAG" -f "$HERE/harnesses/pi/Dockerfile.from-base" "$HERE/harnesses"
    ;;
  cuda)
    : "${ENGINE:=${AALE_ENGINE:-docker}}"
    TAG="${TAG:-aalegate-pi-cuda}"
    echo "== $TAG (npm; CUDA 12.8 / Ubuntu 24.04; context=$HERE) =="
    exec "$ENGINE" build -t "$TAG" -f "$HERE/harnesses/pi-cuda/Dockerfile.pi-cuda-ubuntu24.04" "$HERE"
    ;;
esac
