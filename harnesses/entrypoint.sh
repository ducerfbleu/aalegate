#!/usr/bin/env bash
# aalegate self-wiring entrypoint (shared across all harness images).
#
# Reads the baked-in manifest (/etc/aalegate/harness.json), configures the agent from the
# recorder environment aalegate-run injects, then exec's the agent command + "$@".
# Per-harness differences are DATA (the manifest), not code---one entrypoint for every harness.
set -euo pipefail

MANIFEST="${AALGT_MANIFEST:-/etc/aalegate/harness.json}"
[ -r "$MANIFEST" ] || { echo "aalegate: no manifest at $MANIFEST" >&2; exit 1; }

model_config=$(jq -r '.model_config // ""' "$MANIFEST")
mapfile -t cmd < <(jq -r '.command[]?' "$MANIFEST")
[ "${#cmd[@]}" -gt 0 ] || { echo "aalegate: manifest has no command" >&2; exit 1; }

host="${LLAMA_HOST:-aalegate-gateway}"
port="${LLAMA_PORT:-8080}"
base="http://${host}:${port}"

case "$model_config" in
  pi_models_json)
    # pi's llamacpp provider needs ~/.pi/agent/models.json---probe the recorder and write it.
    auth=(); [ -n "${LLAMA_API_KEY:-}" ] && auth=(-H "Authorization: Bearer ${LLAMA_API_KEY}")
    for _ in $(seq 1 20); do curl -sf "${auth[@]}" "$base/props" >/dev/null 2>&1 && break; sleep 0.3; done
    props=$(curl -sf "${auth[@]}" "$base/props" 2>/dev/null || echo '{}')
    models=$(curl -sf "${auth[@]}" "$base/v1/models" 2>/dev/null || echo '{}')
    ctx=$(printf '%s' "$props" | jq -r '.default_generation_settings.n_ctx // 4096')
    slots=$(printf '%s' "$props" | jq -r '.total_slots // 1')
    [ "$slots" -gt 0 ] 2>/dev/null || slots=1
    mid=$(printf '%s' "$models" | jq -r '.data[0].id // "model"' | xargs basename)
    perctx=$(( ctx / slots ))
    mkdir -p "$HOME/.pi/agent"
    cfg="$HOME/.pi/agent/models.json"
    cur='{}'; [ -f "$cfg" ] && jq empty "$cfg" >/dev/null 2>&1 && cur=$(cat "$cfg")   # merge, don't clobber
    printf '%s' "$cur" | jq --arg url "$base/v1" --arg id "$mid" --argjson ctx "$perctx" --arg key "${LLAMA_API_KEY:-aalegate}" '
      .providers = (.providers // {})
      | .providers.llamacpp = {baseUrl:$url, api:"openai-completions", apiKey:$key,
          models:[{id:$id, name:$id, contextWindow:$ctx, maxTokens:($ctx/4), reasoning:true,
            input:["text","image"],
            compat:{thinkingFormat:"chat-template", supportsDeveloperRole:false,
                    supportsReasoningEffort:true}}]}' \
      > "$cfg.tmp" && mv "$cfg.tmp" "$cfg"
    cmd+=(--model "$mid")
    echo "aalegate: wired pi -> $base/v1 (model $mid, ctx $perctx)" >&2
    ;;
  ""|none)
    if [ -n "${AALGT_ANTHROPIC_LOCAL:-}" ]; then
      # Claude Code against a LOCAL Anthropic-compatible server (e.g. llama.cpp /v1/messages).
      # Probe the recorder (which forwards to the LLM) for context size + model id, map Claude's
      # tiers onto that model, cap its context, and disable the attribution header (avoids KV-cache
      # churn on llama.cpp). The recorder injects any upstream --api-key, so probe unauthenticated.
      for _ in $(seq 1 20); do curl -sf "$base/props" >/dev/null 2>&1 && break; sleep 0.3; done
      props=$(curl -sf "$base/props" 2>/dev/null || echo '{}')
      models=$(curl -sf "$base/v1/models" 2>/dev/null || echo '{}')
      mid=$(printf '%s' "$models" | jq -r '(.data[0].id // .models[0].model) // "local-model"')
      mid="${mid##*/}"                    # basename, in case the id is a gguf path
      [ -n "$mid" ] || mid="local-model"
      ctx=$(printf '%s' "$props" | jq -r '.default_generation_settings.n_ctx // empty')
      export ANTHROPIC_MODEL="$mid"
      export ANTHROPIC_DEFAULT_OPUS_MODEL="$mid"
      export ANTHROPIC_DEFAULT_SONNET_MODEL="$mid"
      export ANTHROPIC_DEFAULT_HAIKU_MODEL="$mid"
      export CLAUDE_CODE_ATTRIBUTION_HEADER=0
      export CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK=1   # fast-mode probe hits api.anthropic.com directly; meaningless for a local model
      if [ -n "$ctx" ]; then export CLAUDE_CODE_MAX_CONTEXT_TOKENS="$ctx"; fi
      # full-airgap: seed an onboarded ~/.claude.json so Claude Code skips its first-run setup
      # (which otherwise probes api.anthropic.com directly and blocks---no env flag disables it).
      # Only when the caller didn't provide one (e.g. via a mounted state dir).
      if [ ! -f "$HOME/.claude.json" ]; then
        ver=$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
        keyid="${ANTHROPIC_API_KEY:-aalegate}"                          # Claude Code keys approvals by the full key (<=20 chars) or its last 20
        if [ "${#keyid}" -gt 20 ]; then keyid="${keyid: -20}"; fi
        printf '{"hasCompletedOnboarding":true,"lastOnboardingVersion":"%s","theme":"dark","numStartups":1,"installMethod":"global","autoUpdates":false,"customApiKeyResponses":{"approved":["%s"],"rejected":[]}}\n' \
          "$ver" "$keyid" > "$HOME/.claude.json"
        echo "aalegate: seeded ~/.claude.json (onboarding + api-key approved) for airgap" >&2
      fi
      cmd+=(--model "$mid")
      echo "aalegate: wired claude-code -> $base (local model $mid, ctx ${ctx:-unknown})" >&2
    fi
    # else openai / cloud-anthropic: the base-URL env aalegate-run set is enough; nothing to write.
    ;;
  *)
    echo "aalegate: unknown model_config '$model_config' in $MANIFEST" >&2; exit 1
    ;;
esac

# --shell: the agent is already wired; drop to an interactive (job-control) shell so you can
# launch the agent as a CHILD job---then Ctrl+Z suspends it back to this shell, exactly like
# running pi locally. `fg` resumes it.
if [ -n "${AALGT_SHELL:-}" ]; then
  echo "aalegate: wired. launch the agent with:  ${cmd[*]}" >&2
  exec bash -i
fi

exec "${cmd[@]}" "$@"
