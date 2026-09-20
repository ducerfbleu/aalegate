#!/usr/bin/env bash
# build.sh --- thin alias for build-aalegate.sh (kept so older docs + muscle memory keep working).
# The shared-image build now lives in build-aalegate.sh and is engine-configurable:
#   ./build.sh --engine podman        ==   ./build-aalegate.sh --engine podman
#   AALGT_ENGINE=podman ./build.sh   ==   AALGT_ENGINE=podman ./build-aalegate.sh
exec "$(cd "$(dirname "$0")" && pwd)/build-aalegate.sh" "$@"
