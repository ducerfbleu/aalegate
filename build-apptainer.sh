#!/usr/bin/env bash
# build-apptainer.sh---convert aalegate agent images (OCI) into .sif files for apptainer / HPC.
#
# Apptainer doesn't build Dockerfiles; it converts an image already in a podman or docker store.
# Build the agent image first (./build-pi.sh, ./build-claude.sh), then convert it here. Runs as
# your user: no root, no --fakeroot. Build on a networked box, copy the .sif to the cluster.
# The recorder/egress run there as host binaries (./install.sh), so they need no SIF.
#
# Usage: ./build-apptainer.sh [--engine podman|docker] [--out DIR] [--tmpdir DIR] [--sign] [IMAGE...]
#   IMAGE     local image name(s); default: every aalegate agent image found
#             (aalegate-pi, aalegate-claude-code, aalegate-pi-cuda)
#   --engine  store to read from; default $AALE_ENGINE, else whichever store has the image
#             (podman preferred when both do---rootless, no daemon)
#   --out     output dir for <image>.sif + <image>.sif.json (default: current dir)
#   --tmpdir  scratch for export + unpack, needs ~2-3x the image size
#             (default $APPTAINER_TMPDIR, else $TMPDIR, else /tmp---often a small tmpfs)
#   --sign    `apptainer sign` with your PGP key (one-time setup: `apptainer key newpair`)
#
# Per image: <out>/<name>.sif plus <name>.sif.json (source image ID, engine, versions, sha256,
# aalegate commit). aalegate-run re-hashes the SIF and checks its signature at run time.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_IMAGES=(aalegate-pi aalegate-claude-code aalegate-pi-cuda)

ENGINE="${AALE_ENGINE:-}"
OUT="$PWD"
TMP_BASE="${APPTAINER_TMPDIR:-${TMPDIR:-/tmp}}"
SIGN=0
images=()
while [ $# -gt 0 ]; do
  case "$1" in
    --engine) ENGINE="$2"; shift 2 ;;
    --out)    OUT="$2"; shift 2 ;;
    --tmpdir) TMP_BASE="$2"; shift 2 ;;
    --sign)   SIGN=1; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)       echo "build-apptainer.sh: unknown arg '$1'" >&2; exit 2 ;;
    *)        images+=("$1"); shift ;;
  esac
done
command -v apptainer >/dev/null 2>&1 || {
  echo "build-apptainer.sh: apptainer not found on PATH (HPC: 'ml apptainer')" >&2; exit 1; }
if [ -n "$ENGINE" ] && ! command -v "$ENGINE" >/dev/null 2>&1; then
  echo "build-apptainer.sh: engine '$ENGINE' not found on PATH" >&2; exit 1
fi
command -v python3 >/dev/null 2>&1 || { echo "build-apptainer.sh: python3 needed for the provenance file" >&2; exit 1; }

mkdir -p "$OUT" "$TMP_BASE"
OUT="$(cd "$OUT" && pwd)"
WORK="$(mktemp -d "$TMP_BASE/aalegate-sif.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

has_image() { command -v "$1" >/dev/null 2>&1 && "$1" image inspect "$2" >/dev/null 2>&1; }

# the store holding IMAGE: --engine/$AALE_ENGINE if set, else podman first, then docker
engine_for() {
  local e
  if [ -n "$ENGINE" ]; then has_image "$ENGINE" "$1" && echo "$ENGINE"; return; fi
  for e in podman docker; do has_image "$e" "$1" && { echo "$e"; return 0; }; done
  return 1
}

# registry/path/name:tag -> name (or name_tag when the tag isn't "latest")
sif_name() {
  local base="${1##*/}" name tag
  name="${base%%:*}"; tag="${base#*:}"
  if [ "$tag" != "$base" ] && [ "$tag" != "latest" ]; then name="${name}_${tag}"; fi
  echo "$name"
}

commit="unknown"
if c="$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null)"; then
  commit="$c"; git -C "$HERE" diff --quiet HEAD -- 2>/dev/null || commit="$c-dirty"
fi

convert() {
  local img="$1" eng name sif partial tar src id sha eng_v apt_v
  eng="$(engine_for "$img")" || {
    echo "build-apptainer.sh: image '$img' not in ${ENGINE:-the podman or docker} store" >&2; return 1; }
  name="$(sif_name "$img")"
  sif="$OUT/$name.sif"; partial="$OUT/.$name.sif.partial"; tar="$WORK/$name.tar"
  id="$("$eng" image inspect --format '{{.Id}}' "$img")"
  echo "== $img [$eng ${id#sha256:}] -> $sif =="

  # export: podman writes native OCI; docker's save format is docker-archive
  if [ "$eng" = podman ]; then
    podman save --format oci-archive -o "$tar" "$img"; src="oci-archive://$tar"
  else
    docker save -o "$tar" "$img"; src="docker-archive://$tar"
  fi
  rm -f "$partial"
  # --disable-cache: don't grow ~/.apptainer/cache (HPC home quotas); scratch goes to $WORK
  APPTAINER_TMPDIR="$WORK" apptainer build --disable-cache "$partial" "$src"
  rm -f "$tar"                                   # free the space before the next image

  if [ "$SIGN" = 1 ]; then apptainer sign "$partial" && apptainer verify "$partial"; fi
  apptainer exec --contain --cleanenv "$partial" test -x /usr/local/bin/aalegate-entrypoint 2>/dev/null \
    || echo "   warning: no /usr/local/bin/aalegate-entrypoint in the SIF (not an aalegate harness image, or apptainer exec unavailable here)" >&2
  mv -f "$partial" "$sif"                        # atomic: running jobs keep the old inode

  sha="$(sha256sum "$sif" | cut -d' ' -f1)"
  eng_v="$("$eng" version --format '{{.Client.Version}}' 2>/dev/null || true)"
  apt_v="$(apptainer --version 2>/dev/null || true)"
  python3 - "$sif.json" "$sif" "$sha" "$img" "$id" "$eng" "$eng_v" "$apt_v" "$commit" "$SIGN" <<'PY'
import datetime, json, sys
out, sif, sha, img, iid, eng, engv, aptv, commit, signed = sys.argv[1:]
with open(out, "w") as fh:
    json.dump({"sif": sif, "sha256": sha, "source_image": img, "source_image_id": iid,
               "engine": eng, "engine_version": engv, "apptainer_version": aptv,
               "aalegate_commit": commit, "signed": signed == "1",
               "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
              fh, indent=2)
    fh.write("\n")
PY
  echo "   sha256 $sha$([ "$SIGN" = 1 ] && echo '  (signed)')"
}

if [ ${#images[@]} -eq 0 ]; then
  for img in "${DEFAULT_IMAGES[@]}"; do
    if engine_for "$img" >/dev/null; then images+=("$img"); fi
  done
  [ ${#images[@]} -gt 0 ] || {
    echo "build-apptainer.sh: no aalegate agent image found---build one first (./build-pi.sh or ./build-claude.sh)" >&2
    exit 1; }
fi

for img in "${images[@]}"; do convert "$img"; done
echo "Done. Copy the .sif (+ .sif.json) to the cluster, then:"
echo "  aalegate-run --runtime apptainer --agent <name>.sif ..."
