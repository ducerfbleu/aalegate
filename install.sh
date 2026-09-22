#!/usr/bin/env bash
# install.sh --- per-user installer for aalegate (the aalegate recorder toolkit).
#
# Installs the intact source tree to $AALE_HOME, builds the Go host binaries
# (gateway/egress/tui) into its bin/, drops PATH shims into $PREFIX/bin, and
# writes an env file you source from your shell rc. No sudo, no site-packages:
# the Python resolves its siblings + bin/ + harnesses/ relative to its own real
# path (via Path(__file__).resolve()), so a symlink on PATH Just Works.
#
#   ./install.sh                 # -> ~/.local/share/aalegate + ~/.local/bin shims
#   ./install.sh --prefix ~/.foo # bin shims under ~/.foo/bin
#   ./install.sh --home DIR      # install the tree to DIR ($AALE_HOME)
#   ./install.sh --no-go         # skip building; trust the committed bin/*
#   ./install.sh --uninstall     # remove shims + tree + env file + rc line (keeps images/keys/audit)
#   ./install.sh --purge         # --uninstall AND remove built images, ~/.aalegate keys, audit data
#
# Platform images (aalegate-gateway/egress/egress-proxy) are NOT built here --- run
# ./build-aalegate.sh separately (docker by default; AALE_ENGINE=podman ./build-aalegate.sh
# for a rootless podman build --- podman's image store is separate from docker's).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
# AALE_PREFIX (not the generic $PREFIX) --- a bare PREFIX is exported by Lmod modules, autotools,
# etc., and would silently aim the shims at, e.g., a module's root-owned bin. --prefix overrides.
PREFIX="${AALE_PREFIX:-$HOME/.local}"
AALE_HOME="${AALE_HOME:-$HOME/.local/share/aalegate}"
ENV_FILE="$HOME/.config/aalegate/env"
BUILD_GO=1
DO_UNINSTALL=0
DO_PURGE=0

say()  { printf '  %s\n' "$*"; }
step() { printf '\n== %s ==\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)    PREFIX="$2"; shift 2 ;;
        --home)      AALE_HOME="$2"; shift 2 ;;
        --no-go)     BUILD_GO=0; shift ;;
        --uninstall) DO_UNINSTALL=1; shift ;;
        --purge)     DO_UNINSTALL=1; DO_PURGE=1; shift ;;
        -h|--help)   sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)           die "unknown flag: $1 (try --help)" ;;
    esac
done

BINDIR="$PREFIX/bin"
SHIMS="aalegate-run aalegate-log aalegate-access aalegate-tui"
IMAGES="aalegate-gateway aalegate-egress aalegate-egress-proxy aalegate-claude-code aalegate-pi aalegate-pi-cuda"

# ---- uninstall ---------------------------------------------------------------
if [ "$DO_UNINSTALL" = 1 ]; then
    step "uninstalling aalegate"
    if [ "$DO_PURGE" = 1 ] && [ -t 0 ]; then
        printf '  --purge also deletes built images, ~/.aalegate keys, and audit data. proceed? [y/N] '
        read -r ans; case "$ans" in [yY]*) ;; *) say "aborted."; exit 0 ;; esac
    fi
    # shims
    for s in $SHIMS; do
        [ -L "$BINDIR/$s" ] && { rm -f "$BINDIR/$s"; say "removed shim $BINDIR/$s"; }
    done
    # tree + env file (+ its now-empty config dir)
    [ -d "$AALE_HOME" ] && { rm -rf "$AALE_HOME"; say "removed tree $AALE_HOME"; }
    [ -f "$ENV_FILE" ]    && { rm -f "$ENV_FILE"; say "removed env $ENV_FILE"; }
    rmdir "$(dirname "$ENV_FILE")" 2>/dev/null && say "removed empty $(dirname "$ENV_FILE")" || true
    # shell rc source line --- the line embeds $ENV_FILE, so it is unambiguously ours
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
        if [ -f "$rc" ] && grep -qsF "$ENV_FILE" "$rc"; then
            # `|| true`: when the source line is the file's ONLY line, grep -v selects
            # nothing and exits 1 --- without this the rewrite is skipped and the line survives.
            tmp="$(mktemp)"; { grep -vF "$ENV_FILE" "$rc" || true; } > "$tmp"; cat "$tmp" > "$rc"; rm -f "$tmp"
            say "removed source line from $rc"
        fi
    done

    AUDIT_ROOT="${AALE_AUDIT_ROOT:-$HOME/.local/state/aalegate}"
    if [ "$DO_PURGE" = 1 ]; then
        # container images --- docker and podman stores are SEPARATE; clean whichever exists
        for eng in docker podman; do
            command -v "$eng" >/dev/null 2>&1 || continue
            for img in $IMAGES; do
                "$eng" image inspect "$img" >/dev/null 2>&1 || continue
                if "$eng" rmi -f "$img" >/dev/null 2>&1; then say "removed $eng image $img"
                else say "could not remove $eng image $img (in use?)"; fi
            done
        done
        # key store (secrets) --- overwrite before unlinking when shred is available
        if [ -d "$HOME/.aalegate" ]; then
            command -v shred >/dev/null 2>&1 && find "$HOME/.aalegate" -type f -exec shred -u {} + 2>/dev/null || true
            rm -rf "$HOME/.aalegate"; say "removed key store ~/.aalegate"
        fi
        # audit data
        [ -d "$AUDIT_ROOT" ] && { rm -rf "$AUDIT_ROOT"; say "removed audit data $AUDIT_ROOT"; }
    else
        say ""
        say "left in place (safe by default --- re-run with --purge to remove all of these):"
        say "  audit data : $AUDIT_ROOT"
        say "  key store  : ~/.aalegate  (holds API keys --- delete if you are done)"
        say "  images     : $IMAGES"
        say "               docker/podman rmi <name>  (build stores are per-engine; clean each one you built with)"
    fi
    exit 0
fi

# ---- dependency checks -------------------------------------------------------
step "checking dependencies"
command -v python3 >/dev/null 2>&1 || die "python3 not found on PATH"
say "python3: $(command -v python3)"
if [ "$BUILD_GO" = 1 ]; then
    command -v go >/dev/null 2>&1 || die "go not found on PATH (needed to build; or pass --no-go)"
    say "go:      $(command -v go) ($(go version | awk '{print $3}'))"
fi
have_runtime=0
for rt in docker podman apptainer; do
    if command -v "$rt" >/dev/null 2>&1; then say "runtime: $rt"; have_runtime=1; fi
done
[ "$have_runtime" = 0 ] && say "warning: no docker/podman/apptainer on PATH --- install one to actually run agents."

# ---- copy the tree -----------------------------------------------------------
step "installing tree -> $AALE_HOME"
[ "$SRC" = "$AALE_HOME" ] && die "source and destination are the same dir; nothing to copy"
mkdir -p "$AALE_HOME"
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
        --exclude '__pycache__/' --exclude '*.pyc' --exclude '.git/' --exclude 'work/' \
        "$SRC"/ "$AALE_HOME"/
else
    cp -a "$SRC"/. "$AALE_HOME"/
    find "$AALE_HOME" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
fi
say "copied $(cd "$AALE_HOME" && ls | wc -l) top-level entries"

# ---- build Go host binaries --------------------------------------------------
if [ "$BUILD_GO" = 1 ]; then
    step "building Go host binaries -> $AALE_HOME/bin"
    mkdir -p "$AALE_HOME/bin"
    build_one() {  # <module-dir> <output-name>
        ( cd "$AALE_HOME/$1" && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" \
            -o "$AALE_HOME/bin/$2" . ) && say "built $2"
    }
    build_one gateway aalegate-gateway
    build_one egress  aalegate-egress
    build_one tui     aalegate-tui
else
    say "skipping Go build (--no-go); using committed bin/*"
fi
chmod +x "$AALE_HOME/aalegate-run" "$AALE_HOME/show-log.py" "$AALE_HOME/show-access.py" "$AALE_HOME/netns-run" 2>/dev/null || true

# ---- PATH shims --------------------------------------------------------------
step "linking shims -> $BINDIR"
mkdir -p "$BINDIR"
ln -sf "$AALE_HOME/aalegate-run"       "$BINDIR/aalegate-run"
ln -sf "$AALE_HOME/show-log.py"        "$BINDIR/aalegate-log"
ln -sf "$AALE_HOME/show-access.py"     "$BINDIR/aalegate-access"
ln -sf "$AALE_HOME/bin/aalegate-tui"   "$BINDIR/aalegate-tui"
for s in $SHIMS; do say "$s -> $(readlink "$BINDIR/$s")"; done

# ---- env file ----------------------------------------------------------------
step "writing env file -> $ENV_FILE"
mkdir -p "$(dirname "$ENV_FILE")"
cat > "$ENV_FILE" <<EOF
# aalegate environment --- source this from your shell rc.
export AALE_HOME="$AALE_HOME"
# audit/run store; override anytime. Default matches recorder.py's built-in.
export AALE_AUDIT_ROOT="\${AALE_AUDIT_ROOT:-\$HOME/.local/state/aalegate}"
# put the shims on PATH (idempotent --- safe to source repeatedly).
case ":\$PATH:" in *":$BINDIR:"*) ;; *) export PATH="$BINDIR:\$PATH" ;; esac
EOF
say "wrote $ENV_FILE"

# ---- wire into shell rc ------------------------------------------------------
case "${SHELL:-}" in *zsh) RC="$HOME/.zshrc" ;; *) RC="$HOME/.bashrc" ;; esac
SRC_LINE="[ -f \"$ENV_FILE\" ] && . \"$ENV_FILE\""
if grep -qsF "$ENV_FILE" "$RC" 2>/dev/null; then
    say "$RC already sources the env file"
else
    step "shell setup"
    say "add this line to $RC (or your preferred rc):"
    printf '\n    %s\n\n' "$SRC_LINE"
    if [ -t 0 ]; then
        printf '  append it to %s now? [y/N] ' "$RC"; read -r ans
        case "$ans" in [yY]*) printf '\n%s\n' "$SRC_LINE" >> "$RC"; say "appended." ;;
                       *)      say "skipped --- add it yourself when ready." ;; esac
    fi
fi

step "done"
say "installed to:  $AALE_HOME"
say "commands:      aalegate-run  aalegate-log  aalegate-access  aalegate-tui"
say "activate now:  . \"$ENV_FILE\"   (or open a new shell)"
say "runtime images: ./build-aalegate.sh in the source tree (AALE_ENGINE=podman for podman)"
