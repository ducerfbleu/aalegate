# AALeGate: Agentic AI Lean Gateway

## What it is

Aalegate is an unprivileged, capture-first execution AI gateway designed natively for batch compute, scientific reproducibility, and airgapped benchmarking. It was written individual users in mind, for worry-free agentic AI delegation on trusted system.  

It has capture-first **L7 recorder** for AI coding agents. While operating at the application layer
(HTTP—full request/response bodies, not packets), it **records every LLM exchange
(hash-chained), egress attempt, and filesystem change**—three audit planes. The agent
runs in a structural airgap, and it reaches only the recorder; the recorder reaches the LLM.

## Architecture

```
  AIRGAP ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐
  │                                                    │
  │  ┌─────────────┐         ┌──────────────┐          │
  │  │   AGENT     │  HTTP   │   RECORDER   │          │
  │  │             │ ──────▶ │  aalegate-   │ ────────────────▶  LLM endpoint
  │  │  (pi,       │  :8080  │  gateway      │         │
  │  │   claude,   │         │               │         │
  │  │   codex)    │         │  plane1.jsonl │         │
  │  │             │         │  (hash-chain) │         │
  │  └──────┬──────┘         └──────────────┘          │
  │         │                                          │
  │         │ HTTPS          ┌──────────────┐          │
  │         │ (optional)     │   EGRESS     │          │
  │         └──────────────▶ │  aalegate-    │ ────────────────▶  github, arxiv, pubmed
  │                          │  egress       │         │     (allow-listed only)
  │                          │               │         │
  │                          │  access.log   │         │
  │                          └──────────────┘          │
  │                                                    │
  └ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─  ┘
         agent has NO route outside the boundary
         recorder + egress are the only exits

  Output: ~/.local/state/aalegate/<project>__<hash>/<run_id>/
    plane1.jsonl   LLM I/O (hash-chained)
    access.log     egress allow/deny
    plane3.json    filesystem diff
    index.json     run provenance
```

**Three audit planes:**
1. **Plane 1---LLM**: every prompt, completion, tool call, token usage, timing (SHA-256 hash-chained)
2. **Plane 2---Internet**: egress allow/deny per domain (optional, via `--egress`)
3. **Plane 3---Filesystem**: content-addressed before/after manifest of work directories

## Why Aalegate

**Agentic AI** is powerful tool, however, realizing its full potential requires complete delegation—to let agents write code, install packages, and execute commands autonomously—that could put a bare PC system to a real risk that most individuals are running. 

The risks could be **catastrophic** that agentic harness alone cannot prevent: Agents are prone to **hallucinations** that could often lead to fatal deletion of the system. Complete access to internet could risk **prompt injection** or **supply chain attack** that result in exploitation of the system.   

**Containerization** is a solution for isolating agentic workflow. However, such isolation alone is not sufficient. A fully air-gapped container may not reach critical services, such as provider authentication. A container with internet access is a real security risk, unless the user manages the traffic system-wide. Making things worse, logs inside the container are invisible to the users and tamper-prone. In short, there is not much of a middle ground for autonomous agentic AI and secure deployment.

**Aalegate**, or “**Agentic AI Lean Gateway**”, propose to be such a middle ground. 

Aalegate is a **lean gateway equipped with only essential functions** that individual users need to safety run AI agents on their own machines. It provides *airgap-by-default* with *controlled egress* for connecting essential domains (e.g., for LLM provider authentication). It sits in between agentic harness and LLM provider, and *records the logs of all the communication* between the two party—system messages, prompts, responses, and tool calling, etc. The log management is *centralized* where provenance from multiple harness lands in one auditable place, encrypted at rest, serving as an *independent history for user-side audit*, which agents cannot tamper with. 

Aalegate is a *per-user tool* that treats end-users as **first-class citizens**—HPC users; home lab operators; and individuals with PC who seek to delegate to autonomous agents without risking their system. 

Aalegate mainly written in **standard-library Go** (TUI with three Go-team maintained library) and **standard-library Python**. It has minimal third-party dependency philosophy by design, for smallest possible supply chain attack risk. We believe that this matters for individual users who don’t have a security team auditing their dependency trees. It has three Go lightweight binaries and a Python launcher. It consists of a recorder that hash-chains every LLM exchange, an egress proxy that allowlists only the necessary domains, and a TUI that allows the users to navigate the logs. Four container runtime builds—Docker, Podman, Apptainer, Kata—are provided with one launcher. 

## Runtimes

One unified launcher (`aalegate-run`), four pluggable runtimes. The recorder is the
same in all cases---only the agent isolation differs:

| | **Apptainer (HPC)** | **Podman** | **Docker** | **Kata + Docker** |
|---|---|---|---|---|
| isolation | namespace + `--netns` | namespace (rootless) | namespace | hypervisor (separate kernel) |
| daemon | none | none | Docker daemon | Docker daemon |
| rootless | always | default | opt-in (`--user`) | no (KVM needs privileges) |
| recorder | host process | container (dual-homed) | container (dual-homed) | container (dual-homed) |
| GPU | `--nv` (Apptainer hook) | device passthrough | device passthrough + WSL2 | VFIO PCI passthrough |
| target | HPC clusters (SLURM) | workstations, CI | workstations, CI | untrusted agents, dedicated GPU |
| key custody | key forwarded | key forwarded | recorder holds real key | recorder holds real key |
| images | `.sif` (signed) | OCI | OCI | OCI (in microVM) |

**Pick by trust level:**
- **Your own agent, iterating fast** → `--runtime podman`
- **Untrusted agent, long autonomous runs** → `--runtime kata`
- **HPC cluster** → `--runtime apptainer`
- **General workstation use** → `--runtime docker`

## File layout

```
aalegate-run              unified CLI (--runtime docker|podman|apptainer|kata)
recorder.py               shared recorder lifecycle (provenance, manifest, key custody)
runtimes/
  docker.py               Docker agent wrapper
  podman.py               Podman agent wrapper (--wire, --manifest for BYO images)
  apptainer.py            Apptainer/HPC agent wrapper (--netns for enforced airgap)
  kata.py                 Kata wrapper (thin: sets --runtime kata-runtime, delegates to docker.py)
gateway/
  gateway.go              L7 recorder source (Go, stdlib only, hash-chained JSONL)
  Dockerfile.gateway      recorder image (static binary -> scratch)
egress/
  egress.go               Go CONNECT allow-list proxy source
  Dockerfile.egress       egress proxy image
Dockerfile.proxy          squid egress proxy (alternative, used by --runtime docker)
harnesses/
  entrypoint.sh           shared self-wiring entrypoint (baked into agent images)
  pi/                     CPU agent image (Dockerfile + harness.json)
  pi-cuda/                NVIDIA GPU agent image (Ubuntu 24.04 / CUDA 12.8)
  claude-code/            Claude Code agent image (native binary; Anthropic Messages API)
tui/
  *.go                    aalegate-tui log reader (Go; stdlib + golang.org/x/term + x/text)
build-aalegate.sh         build the platform (recorder + egress proxy + tui)
build-pi.sh               build the pi agent (--cuda / --from-base)
build-claude.sh           build the Claude Code agent
show-log.py               read provenance logs + throughput (tk/s)
bpe_tokenizer.py          offline tokenizer for exact reasoning/writing token splits
netns-run                 rootless network namespace helper (apptainer --netns)
bin/                      pre-compiled gateway + egress binaries (HPC, no Docker)
examples/                 CUDA GEMM demo, tiny LLM for testing
```

## Quick start

Quick start example with claude code (subscription and local model). orders: docker (most friendly with users) -> podman -> apptainer. 

### 0. Build the platform + an agent image (once)

```bash
cd aalegate
./build-aalegate.sh   # platform: aalegate-gateway (recorder) + aalegate-egress-proxy + aalegate-tui
./build-claude.sh     # agent:    aalegate-claude-code  (native binary, node-less)---recommended
./build-pi.sh         # agent:    aalegate-pi           (npm: @earendil-works/pi-coding-agent; --cuda for GPU)
```


### 1. Docker + Claude Code (the common path)

Build the image from its Dockerfile first (`./build-claude.sh`, or
`docker build -t aalegate-claude-code -f harnesses/claude-code/Dockerfile .`), then pick an auth mode.

**Key custody** --- the recorder holds the real Anthropic key; the agent only ever sends a dummy:

```bash
mkdir -p ~/.aalegate && printf %s "$ANTHROPIC_API_KEY" > ~/.aalegate/anthropic.key && chmod 600 ~/.aalegate/anthropic.key

./aalegate-run --runtime docker --agent claude-code --api anthropic \
  --llm https://api.anthropic.com --llm-verify \
  --llm-key-file ~/.aalegate/anthropic.key --work "$PWD/work" -p "your task"
# every /v1/messages turn -> plane1.jsonl (completion, thinking, tool_use, tokens), hash-chained
```

**Subscription / OAuth** --- no key; log in with a Claude Pro/Max account. Claude Code does its own
claude.ai login and the recorder forwards the OAuth bearer upstream; `--subscription` auto-allows
the `anthropic` egress lane (OAuth exchange + Claude Code's direct api.anthropic.com side checks).
Inference (`/v1/messages`) is still captured:

```bash
# first run: --shell to complete the login (paste the code from the browser)
./aalegate-run --runtime docker --agent claude-code --subscription \
  --default-home --work "$PWD/work" --shell
#   in the shell:  claude  -> /login -> paste code ; the token lands in the mounted ~/.claude

# later runs reuse the login (--default-home); drop --shell to run a task directly:
./aalegate-run --runtime docker --agent claude-code --subscription \
  --default-home --work "$PWD/work" -p "your task"
```

**Local model** --- `--local` points Claude Code at a local Anthropic-compatible server (llama.cpp
serves `/v1/messages` natively); the entrypoint maps Claude's tiers onto the served model. Fully
airgapped except the recorder->LLM leg; no key, no cloud:

```bash
./aalegate-run --runtime docker --agent claude-code --local \
  --llm http://127.0.0.1:PORT --work "$PWD/work" -p "your task"
# if llama.cpp was started with --api-key SECRET (recorder holds it, agent gets a dummy), add:
#   --llm-key-file ~/.aalegate/llama.key
```

The LLM must listen on `0.0.0.0`; the containerized recorder reaches loopback via the host alias
(`host.docker.internal` / `host.containers.internal`) automatically. See "Pointing at a local LLM" below.

### 2. Podman (rootless)

Same commands with `--runtime podman` --- rootless by default (no daemon, no root); the user
namespace maps your identity in, so mounts stay yours. Works with Claude Code or the pi agent
(build pi first: `./build-pi.sh`):

```bash
./aalegate-run --runtime podman --agent claude-code --local \
  --llm http://127.0.0.1:PORT --work "$PWD/work" -p "your task"

# or the pi agent against a local model:
./aalegate-run --runtime podman --agent pi \
  --llm http://127.0.0.1:PORT --llm-key testkey --egress pypi --work "$PWD/work" -p "your task"
```

### 3. Apptainer / HPC (enforced airgap)

Build the image on a networked box, convert it to a `.sif`, ship that to the cluster; Apptainer
runs it as *you* (no baked UID). `--netns` enforces the airgap via a rootless network namespace:

```bash
# on the build box (podman preferred; docker works too): OCI image -> .sif + .sif.json provenance
./build-pi.sh --engine podman && ./build-apptainer.sh --engine podman aalegate-pi   # --sign to sign
scp aalegate-pi.sif aalegate-pi.sif.json cluster:

# on the cluster
ml apptainer
./aalegate-run --runtime apptainer --agent aalegate-pi.sif \
  --llm http://127.0.0.1:PORT --llm-key "$LLM_KEY" \
  --netns --work "$PWD/work" -p "your task"
```

### 4. NVIDIA GPU (pi-cuda)

```bash
# Podman (shared GPU, host keeps using it):
./aalegate-run --runtime podman --agent pi-cuda --gpu nvidia \
  --llm http://127.0.0.1:PORT --work "$PWD/work" \
  --mount /opt/miniconda3:/opt/miniconda3 -p "profile the kernel"

# Docker (identical GPU wiring --- see the three tiers below):
./aalegate-run --runtime docker --agent pi-cuda --gpu nvidia \
  --llm http://127.0.0.1:PORT --work "$PWD/work" -p "profile the kernel"
```

`--gpu nvidia` resolves the GPU in three tiers (same logic for both runtimes):

1. **CDI (recommended).** If an NVIDIA CDI spec is present, it emits `--device nvidia.com/gpu=all`
   and the toolkit injects the correct device nodes, **version-matched** driver libs, and
   `nvidia-smi` --- host-agnostic, no hardwired paths. Set it up once per host (regenerate after
   driver updates):
   ```bash
   sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
   ```
   No root / HPC: write it under `$HOME` and point both aalegate and podman at it ---
   `export AALE_CDI_DIR=$HOME/cdi` and add `cdi_spec_dirs = ["/home/<you>/cdi"]` under `[engine]`
   in `~/.config/containers/containers.conf`.
2. **WSL2.** Else, if `/dev/dxg` exists: `--device /dev/dxg` + `-v /usr/lib/wsl` (toolkit-free).
3. **SONAME fallback.** Else, explicit `/dev/nvidia*` + host driver libs mounted by SONAME. Brittle
   (hardwired `/usr/lib/x86_64-linux-gnu`, single version) --- prefer CDI. Force this tier with
   `AALE_CDI=0`.

### 5. Kata (hypervisor isolation)

```bash
./aalegate-run --runtime kata --agent claude-code --api anthropic \
  --llm https://api.anthropic.com --llm-verify \
  --llm-key-file ~/.aalegate/anthropic.key --work "$PWD/work" -p "your task"
```

### 6. Interactive shell

```bash
./aalegate-run --runtime docker --agent claude-code --shell \
  --llm https://api.anthropic.com --llm-verify --llm-key-file ~/.aalegate/anthropic.key --work "$PWD/work"
# self-wires, then drops to bash; run `claude` (or `pi ...`) by hand---still recorded
```

## Pointing at a local LLM

The recorder is a *container*, so `--llm` must be reachable **from a container**, not just your
shell. aalegate rewrites loopback for you under **both** docker and podman: a `127.0.0.1` /
`localhost` / `0.0.0.0` host becomes the runtime's host alias (`host.docker.internal` /
`host.containers.internal`), keeping the scheme, **port**, and path. So the portable form is:

```bash
--llm https://127.0.0.1:PORT        # -> host.<docker|containers>.internal:PORT
```

Two rules make this robust no matter how the model is deployed:

1. **The server must listen on `0.0.0.0`** (not `127.0.0.1`), or no container can reach it:
   `llama-server --host 0.0.0.0`, `vllm --host 0.0.0.0`, `OLLAMA_HOST=0.0.0.0`.
2. **Prefer a host-published port.** A model container run with `-p PORT:INTERNAL` is reachable by
   any runtime via the host alias --- no shared network needed. That is the general path; joining
   the model's container network (`--llm-net`) is only for *unpublished, same-engine* setups.

| where the model runs | `--llm` | `--llm-net`? |
|---|---|---|
| container with `-p PORT:…`, or native on host `0.0.0.0:PORT` | `https://127.0.0.1:PORT` | no --- works under docker & podman |
| container, no host port, **same engine** as the recorder | `http://<name>:<internal-port>` | `--llm-net <that-net>` |
| container, no host port, **cross-engine** (e.g. podman recorder -> docker net) | *unreachable* --- publish a host port instead | --- |
| another machine / LAN | `https://<ip-or-host>:PORT` | no (passed through unchanged) |

Auth + TLS: servers that enforce `--api-key` need `--llm-key` / `--llm-key-file` (the recorder
holds it; the agent gets a dummy) --- otherwise any value works. Self-signed `https` is accepted by
default; pass `--llm-verify` only to enforce a real cert.

## Agent images

One build script per harness (each pairs the Dockerfile with the context its COPY paths need):

- **Claude Code (`aalegate-claude-code`)**---native standalone `claude`, no Node/npm:
  ```bash
  ./build-claude.sh
  ```
- **pi CPU (`aalegate-pi`)**---installs pi from its official npm package (`@earendil-works/pi-coding-agent`):
  ```bash
  ./build-pi.sh        # or: docker build -t aalegate-pi -f harnesses/pi/Dockerfile .
  ```
- **pi NVIDIA GPU (`aalegate-pi-cuda`, Ubuntu 24.04 / CUDA 12.8)**:
  ```bash
  ./build-pi.sh --cuda
  ```

## Reading the logs

The logs live in `~/.local/state/aalegate/` by default. Implementation of DB for server-side development is TBD.

```bash
aalegate-tui       # interactive reader: pick a run, ↑/↓ through turns, enter to open a turn's
                   #   blocks (prompt/reasoning/completion/tool-calls/request-body), c=copy block
                   #   (OSC 52 clipboard, works over SSH/tmux), s=save block to $AALE_AUDIT_ROOT/clips/,
                   #   tab=stats, md/JSON highlighting, -theme <nocturnal|dracula|gruvbox|nord|solarized|ansi>.
                   #   Go, stdlib + golang.org/x/term + x/text.  AALE_TUI=rich → bubbletea add-on.
aalegate-log       # non-interactive dump (show-log.py): records + token/throughput totals
```

Raw paths (runs nest under `<project>__<hash>/<run_id>/`):
```bash
RUN=$(ls -td ~/.local/state/aalegate/*/*/ | head -1)
python3 show-log.py "$RUN/plane1.jsonl"              # prompts/completions + tk/s
cat "$RUN/index.json"                                # provenance
```

Verify hash-chain integrity:
```bash
aalegate-gateway -verify "$RUN/plane1.jsonl"
# or from the image:
docker run --rm -v "$RUN:/logs:ro" aalegate-gateway -verify /logs/plane1.jsonl
```

## Key custody (Docker / Kata only)

The recorder holds the real upstream API key; the agent gets a dummy (`aalegate`) and
never sees the secret. Three ways to pass the key:

| Flag | How | Visible in `docker inspect`? |
|---|---|---|
| `--llm-key-file PATH` | mounted read-only into recorder | no |
| `--llm-key-env VAR` | read by aalegate-run, passed to recorder | no |
| `--llm-key VALUE` | inline on argv (warns) | yes |

The recorder strips whatever auth the agent sent and injects the real key upstream.

**Subscription / OAuth (Claude Code):** `--subscription` turns custody OFF---there is no separable
key to hold. The agent performs its own claude.ai login and the recorder forwards the OAuth bearer
(carried in `anthropic-beta`; the recorder passes all headers through, so it isn't stripped). Add
`--default-home` to persist the login in host `~/.claude` across runs; the `anthropic` egress group
is auto-allowed for OAuth (platform.claude.com) and Claude Code's direct api.anthropic.com side
checks. Inference (`/v1/messages`) still routes through the recorder (plane 1). Mutually exclusive
with `--llm-key*`.

## Notes

- **LLM addressing (Docker):** `--llm http://127.0.0.1:PORT` is rewritten to
  `host.docker.internal` for the containerized recorder. The LLM must listen on
  `0.0.0.0`, not strictly `127.0.0.1`.
- **`--egress`** groups: `gh`, `hf`, `pypi`, or `all`. Omit for fully airgapped.
- **`--allow HOST[:PORT]`** allowlists a specific host (or host:port) via the egress
  proxy. Repeatable. Works for host-side services, LAN, or remote APIs. Implies egress.
  Examples: `--allow 192.168.1.100:8080 --allow api.openai.com --allow myhost.lan:9999`.
- **Each run is fresh**---ephemeral per-run `$HOME` under the audit dir; host
  `~/.pi` / `~/.claude` are never touched unless `--default-home`.
- **Session bucket:** the run's audit dir is named from the `--work` dir (basename + hash of its
  realpath), not the CWD. Several `--work` → choose the shared root (interactive prompt, or `-y`
  for the broadest common parent; `--project DIR` overrides; no `--work` falls back to CWD).
- **Hardening (Docker/Kata):** all containers run `--user`, `--security-opt=no-new-privileges`,
  `--cap-drop ALL`, `--pids-limit 4096`.
- **`--netns` (Apptainer):** enforced airgap via rootless `unshare` + `slirp4netns`.
  Without it, Apptainer shares the host network (policy-only airgap).
- **`--wire` / `--manifest` (Podman):** runtime-inject the self-wiring entrypoint into
  un-catalogued images (needs `jq`, `curl`, `bash` in the image).
- **Dry run:** `--dry-run` prints the planned commands without executing.

## Progress

### Harnesses

| harness | base OS | apptainer | podman/docker | notes |
|---|---|---|---|---|
| pi | Ubuntu 24.04 | [x] `aalegate-pi.sif` | [x] `aalegate-pi` | from-source build via bun |
| pi-cuda | Ubuntu 24.04 | [ ] | [x] `aalegate-pi-cuda` | CUDA 12.8; DL stack via mounted conda |
| claude-code | Ubuntu 24.04 | [ ] | [x] `aalegate-claude-code` | native binary (no node); Anthropic Messages codec; key custody or subscription OAuth |
| codex |---| [ ] | [ ] | needs Node.js; wire via `OPENAI_BASE_URL` |

### Runtimes

| runtime | status | isolation | notes |
|---|---|---|---|
| `--runtime docker` | [x] | namespace (runc) | key custody, WSL2 GPU |
| `--runtime podman` | [x] | namespace (rootless) | `--wire`/`--manifest` for BYO images |
| `--runtime apptainer` | [x] | namespace + `--netns` | HPC/SLURM, SIF signatures |
| `--runtime kata` | [x] | hypervisor (microVM) | untrusted agents, dedicated GPU (VFIO) |
| `--runtime sbx` | [ ] planned | hypervisor (Docker Sandboxes) | Windows-native; proprietary runtime, open recorder |

sbx integration: aalegate-gateway runs as a Docker container on the host; sbx agent's
LLM endpoint points at it. Recorder-only mode---sbx owns the agent lifecycle, aalegate
provides the audit trail. See `which-containers-to-use.md` for the architecture.

### LLM endpoints

| endpoint | status | notes |
|---|---|---|
| OpenAI-compatible | [x] | llama.cpp, vLLM, SGLang, TGI---what the recorder proxies today |
| Anthropic | [x] | Messages API (`/v1/messages`): text/thinking/tool_use, streaming, `input/output_tokens`, dedup |
| OpenAI native | [ ] | same schema as compatible, but auth + org headers differ |

### Others

- [] Log encryption / decryption
- [] MCP server addon
- [] Streaming capture: rich TUI addon
- [] Database addon

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `AALE_HOME` | Install location of the aalegate tree | `~/.local/share/aalegate` |
| `AALE_AUDIT_ROOT` | Where run provenance logs are stored | `~/.local/state/aalegate` |
| `AALE_ENGINE` | Container engine for build scripts | `docker` |
| `AALE_PREFIX` | Prefix for bin shims (`$PREFIX/bin`) | `~/.local` |
| `AALE_SHELL` | Set by `--shell` --- drop to interactive bash instead of running the agent | --- |
| `AALE_MANIFEST` | Override path to harness.json inside the container | `/etc/aalegate/harness.json` |
| `AALE_ANTHROPIC_LOCAL` | Set by `--local` --- entrypoint maps Claude's model tiers to a local LLM | --- |
| `AALE_CDI` | Set to `0` to force SONAME GPU fallback instead of CDI | --- |
| `AALE_CDI_DIR` | Extra CDI spec directory (for rootless/HPC where CDI is under `$HOME`) | --- |
| `AALE_TUI` | Set to `rich` to exec the bubbletea TUI add-on | --- |
| `AALE_TUI_THEME` | TUI color theme: `nocturnal`, `dracula`, `gruvbox`, `nord`, `solarized`, `ansi` | `nocturnal` |

Internal wire vars (`GATEWAY_*`, `EGRESS_*`) and externally-dictated agent vars (`ANTHROPIC_*`, `OPENAI_*`, `CLAUDE_CODE_*`, `LLAMA_*`) are not part of the `AALE_` namespace.
