# aalegate-pi --- the pi coding agent, installed from its OFFICIAL npm package.
#
# No vendored source tree: pi comes from @earendil-works/pi-coding-agent on npm, so provenance
# is the registry + package integrity (pin a version + `npm ci` for full reproducibility),
# not a "trust me" blob. The shared aalegate entrypoint points pi at the recorder.
#
# UID-neutral: NO baked user. Identity is mapped at RUN time, not build time ---
#   docker / podman:  --user $(id -u):$(id -g)   (aalegate-run injects this)
#   podman rootless:  run as image-root; the rootless userns maps it to your host UID
#   apptainer:        runs as the invoking user; the SIF bakes no UID
# HOME + /workspace are world-writable so ANY uid works; nothing here encodes the builder.
#
# HARDENING --- `docker build` runs on YOUR host, OUTSIDE aalegate's runtime airgap, so the build
# itself is the exposure surface. Reduce blast radius:
#   * Build ROOTLESS (no root daemon; a bad package is capped at your UID, not host root):
#       podman build -t aalegate-pi -f harnesses/pi/Dockerfile .        # not rootful docker
#   * Scope the build's egress to the registry so a compromised step can't exfiltrate --- build
#     behind a firewall/netns allowing only registry.npmjs.org (+ the base-image registry), or
#     pre-seed an npm cache and build with --network=none (fully offline).
#   * Pin the base image by digest (build-arg below) and pin the pi version:
#       docker buildx imagetools inspect node:24-bookworm-slim --format '{{.Manifest.Digest}}'
#       podman build --build-arg NODE_IMAGE=node:24-bookworm-slim@sha256:<digest> ...
# base pinned by digest 2026-09-19 (refresh: docker buildx imagetools inspect node:24-bookworm-slim)
ARG NODE_IMAGE=node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6
FROM ${NODE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates git ripgrep curl jq \
 && rm -rf /var/lib/apt/lists/*

# pi is pinned by lockfile: harnesses/pi/package.json (version) + package-lock.json (the full
# transitive tree, every dep by integrity hash). `npm ci` installs EXACTLY that tree, verifies
# each hash, and fails if the lock and package.json disagree. --ignore-scripts skips arbitrary
# pre/post-install (the main npm supply-chain vector). To bump pi: edit the version in
# package.json, then regenerate the lock:
#   (cd harnesses/pi && npm install --package-lock-only --ignore-scripts)
# We deliberately do NOT run pi during the build; verify after:
#   docker run --rm --entrypoint pi aalegate-pi --version
WORKDIR /opt/pi
COPY harnesses/pi/package.json harnesses/pi/package-lock.json ./
RUN npm ci --ignore-scripts && npm cache clean --force
ENV PATH="/opt/pi/node_modules/.bin:$PATH"

# UID-neutral runtime home + workdir (writable by any uid; no USER baked in).
ENV HOME=/home/user
RUN mkdir -m 0777 /home/user /workspace
WORKDIR /workspace

# aalegate self-wiring: entrypoint aims the agent at the recorder; harness.json declares wiring.
COPY harnesses/entrypoint.sh /usr/local/bin/aalegate-entrypoint
COPY harnesses/pi/harness.json /etc/aalegate/harness.json
RUN chmod 0755 /usr/local/bin/aalegate-entrypoint && chmod 0644 /etc/aalegate/harness.json

ENTRYPOINT ["/usr/local/bin/aalegate-entrypoint"]
