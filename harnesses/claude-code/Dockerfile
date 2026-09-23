# aalegate-claude-code --- Claude Code in a self-wiring aalegate harness.
#
# Node-less: installs the native standalone `claude` binary via the official installer
# (https://claude.ai/install.sh), which bundles its own runtime --- no system Node/npm.
# The shared aalegate entrypoint points ANTHROPIC_BASE_URL at the recorder, so the agent
# speaks the Anthropic Messages API (POST /v1/messages) and every turn is captured as
# plane 1 (see gateway.go's Anthropic codec).
#
# Auth: two modes, both wired by aalegate-run / recorder.py --- neither bakes a key here.
#   key custody   (default)     --llm-key-file: agent sends a dummy, recorder injects the real key.
#   subscription  --subscription: ANTHROPIC_API_KEY unset -> Claude Code does its own claude.ai
#                                 OAuth login; recorder forwards the bearer upstream unchanged.
#
# Build (context = aalegate root):
#   ./build-claude.sh
#   # or: docker build -t aalegate-claude-code -f harnesses/claude-code/Dockerfile .
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl jq git ca-certificates ripgrep bash python3 \
      vim-tiny neovim less file tree diffutils \
    && curl -fsSL https://claude.ai/install.sh | bash \
    && cp -L /root/.local/bin/claude /usr/local/bin/claude \
    && claude --version \
    && rm -rf /var/lib/apt/lists/* /root/.local/state /root/.cache

# Airgap-friendly: keep Claude Code off non-LLM endpoints. The LLM plane reaches the
# recorder via ANTHROPIC_BASE_URL; only subscription OAuth login/refresh traverses the
# egress allow-list (recorder.py EGRESS_DOMAINS["anthropic"]).
ENV DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    TERM=xterm-256color

# UID-neutral runtime home + workdir (writable by any uid; no USER baked in).
ENV HOME=/home/user
RUN mkdir -m 0777 /home/user /workspace

# color-coded shell for --shell sessions; seeded into $HOME by the entrypoint.
COPY harnesses/bashrc /etc/skel/.bashrc
WORKDIR /workspace

# shared self-wiring entrypoint + claude manifest (model_config "none": the ANTHROPIC_BASE_URL
# env aalegate-run injects is all the wiring Claude Code needs --- nothing written to disk).
COPY harnesses/entrypoint.sh /usr/local/bin/aalegate-entrypoint
COPY harnesses/claude-code/harness.json /etc/aalegate/harness.json
RUN chmod 0755 /usr/local/bin/aalegate-entrypoint && chmod 0644 /etc/aalegate/harness.json

ENTRYPOINT ["/usr/local/bin/aalegate-entrypoint"]
