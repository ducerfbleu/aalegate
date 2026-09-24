# AALeGate: Agentic AI Lean Gateway

## What it is

Aalegate is an unprivileged, capture-first AI gateway designed natively for batch compute,
scientific reproducibility, and airgapped benchmarking. It was written with individual users in
mind, for worry-free delegation to AI agents on a trusted system.

It is a capture-first **L7 recorder** for AI coding agents. Operating at the application layer
(HTTP: full request/response bodies, not packets), it **records every LLM exchange
(hash-chained), egress attempt, and filesystem change**: three audit planes. The agent runs in a
structural airgap and reaches only the recorder; the recorder reaches the LLM.

> **Status:** this README describes what works today (Claude Code and pi, on Docker, Podman,
> Apptainer and Kata). More harnesses, runtimes and hardening are coming; see
> [Status and roadmap](#status-and-roadmap).

## Architecture

```
┌─ airgap: no route out except the recorder and the proxy ─┐
│                                                          │
│  ┌──────────────┐   HTTP    ┌───────────────────┐        │
│  │ agent        │ ────────▶ │ recorder          │──────────────▶  LLM endpoint
│  │ Claude Code, │  (:8080)  │ aalegate-gateway  │        │
│  │ pi           │           │ plane1.jsonl      │        │
│  └──────┬───────┘           └───────────────────┘        │
│         │ HTTPS_PROXY (only with --egress/--allow)       │
│         │                   ┌───────────────────┐        │
│         └─────────────────▶ │ egress proxy      │──────────────▶  allowlisted hosts
│                             │ aalegate-egress   │        │         (github, pypi, …)
│                             │ access.log        │        │
│                             └───────────────────┘        │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

**Three audit planes**, written per run to `~/.local/state/aalegate/<project>__<hash>/<run_id>/`:

1. **LLM** (`plane1.jsonl`): every prompt, completion, tool call, token count and timing,
   SHA-256 hash-chained ([gateway/](gateway/README.md))
2. **Internet** (`access.log`): egress allow/deny per host ([egress/](egress/README.md))
3. **Filesystem** (`plane3.json`): content-addressed before/after manifest of the work directories

## Install

```bash
cd aalegate
./install.sh          # -> ~/.local/share/aalegate; builds the Go binaries; puts aalegate-run,
                      #    aalegate-tui, aalegate-log, aalegate-access on PATH (--no-go: use bin/*)
./build-aalegate.sh   # recorder + egress images for docker/podman (--engine podman)
./build-claude.sh     # agent image: Claude Code
./build-pi.sh         # agent image: pi (--cuda for the NVIDIA variant)
```

Build images with the **same engine you run with** (podman's image store is separate from
docker's). For **Apptainer/HPC**, convert the agent images to `.sif` on a networked box and copy
them to the cluster, where only `./install.sh` is needed (`--no-go` if the cluster has no Go; the
recorder and proxy run there as host binaries):

```bash
./build-apptainer.sh --engine podman aalegate-claude-code aalegate-pi   # -> *.sif + *.sif.json
```

Image details, hardening the build, and GPU setup: [harnesses/](harnesses/README.md).

## Usage

The same flags work on every runtime; change `--runtime` (`docker`, `podman`, `apptainer`,
`kata`). On Apptainer, `--agent` is the path to the `.sif`. Add `-p "task"` for a one-shot run;
without it you get the agent's interactive session.

### Claude Code with a subscription (Pro/Max)

Claude Code logs in itself; the recorder forwards its OAuth token and still records every
`/v1/messages` exchange. `--subscription` also allows the `anthropic` egress group that the
login needs.

**With `--dotfile claude`** (log in once, reuse it): host `~/.claude` is mounted into the agent.

```bash
# first run: log in inside the agent shell
aalegate-run --runtime docker --agent claude-code --subscription --dotfile claude --work "$PWD" --shell
#   in the shell:  claude  ->  /login  ->  paste the code from the browser  ->  exit

# every later run reuses the login
aalegate-run --runtime docker --agent claude-code --subscription --dotfile claude --work "$PWD"

# podman: same flags.  apptainer:
aalegate-run --runtime apptainer --agent ./aalegate-claude-code.sif --subscription --dotfile claude --work "$PWD"
```

The trade-off: the agent can read and write your real `~/.claude` (settings, history,
credentials).

**Without `--dotfile`** (most isolated): every run starts logged out, so use `--shell` and
`/login` each time. The login is written to that run's own home (`<run dir>/home`), stays there
on disk, and is not reused by later runs.

### Claude Code with an API key

The recorder holds the real key; the agent only ever sees a dummy
([key custody](gateway/README.md#key-custody)).

```bash
mkdir -p ~/.aalegate && printf %s "$ANTHROPIC_API_KEY" > ~/.aalegate/anthropic.key && chmod 600 ~/.aalegate/anthropic.key
aalegate-run --runtime docker --agent claude-code --llm-key-file ~/.aalegate/anthropic.key --work "$PWD"
# apptainer: add --api anthropic --llm https://api.anthropic.com --llm-verify (a .sif path has no catalog defaults)
```

### Claude Code with a local model

`--local` points Claude Code at a local Anthropic-compatible server (llama.cpp serves
`/v1/messages`). It maps Claude's model tiers onto the served model and reads the context size
from the server. Fully airgapped apart from the recorder-to-LLM leg; no key, no cloud.

```bash
llama-server -m model.gguf --host 0.0.0.0 --port 8014     # docker/podman need 0.0.0.0 (see below)

aalegate-run --runtime docker --agent claude-code --local --llm http://127.0.0.1:8014 --work "$PWD"
aalegate-run --runtime apptainer --agent ./aalegate-claude-code.sif --local --llm http://127.0.0.1:8014 --work "$PWD"
# server started with --api-key? add --llm-key-file (the recorder holds it; the agent gets a dummy)
```

### pi with a local model

pi runs against local OpenAI-compatible servers. It reads the model id and context size from the
server's `/v1/models` and `/props` (llama.cpp); on servers without `/props` the context falls
back to 4096 tokens.

```bash
aalegate-run --runtime podman --agent pi --llm http://127.0.0.1:8014 --work "$PWD"
aalegate-run --runtime apptainer --agent ./aalegate-pi.sif --llm http://127.0.0.1:8014 --work "$PWD"
# GPU variant: --agent pi-cuda --gpu nvidia (see harnesses/)
```

### Where the model runs

| model | `--llm` | notes |
|---|---|---|
| on this host, or a container with a published port | `http://127.0.0.1:PORT` | docker/podman: the recorder is a container and reaches the host through `host.docker.internal` / `host.containers.internal`, so the server must listen on `0.0.0.0`. apptainer: `127.0.0.1` is fine |
| a container with no published port, same engine | `http://NAME:PORT` + `--llm-net NET` | the recorder joins that network |
| another machine | `http(s)://HOST:PORT` | self-signed https is accepted; `--llm-verify` enforces a real certificate |

### Web access

The agent has no internet by default. `--egress gh,pypi` (groups: `gh`, `hf`, `pypi`, `go`,
`anthropic`, or `all`) or `--allow HOST[:PORT]` opens specific hosts through the egress proxy;
everything else is refused and logged. Details: [egress/](egress/README.md).

### Other flags

| flag | effect |
|---|---|
| `--work DIR...` / `--work-ro DIR...` | project dirs, read-write / read-only (several → the run's log folder is named after their common root; `-y` skips the prompt, `--project DIR` overrides) |
| `--data DIR...` / `--data-rw DIR...` | extra data dirs, read-only / read-write |
| `--mount HOST:CTR[:ro]` | any other bind mount |
| `--dotfile NAME...` | mount host `~/.NAME` read-write, e.g. `--dotfile claude pi`; nothing from your home is mounted otherwise |
| `--shell` | wire everything up, then drop to a shell instead of starting the agent |
| `--dry-run` | print the planned commands without running them |
| `--no-netns` | apptainer only: share the host network (policy-only airgap; tools that ignore the proxy get out unlogged) |

## Runtimes

One launcher, four runtimes. The recorder is the same everywhere; only the agent's isolation
differs.

| | **Apptainer (HPC)** | **Podman** | **Docker** | **Kata + Docker** |
|---|---|---|---|---|
| isolation | user namespace + netns airgap (default) | rootless, internal network | internal network | microVM (own kernel) |
| daemon / root | none | none | Docker daemon | Docker daemon + KVM |
| recorder, proxy | host processes | containers | containers | containers |
| GPU | `--nv` | CDI / passthrough | CDI / passthrough, WSL2 | VFIO |
| images | `.sif` (signable) | OCI | OCI | OCI (in microVM) |
| best for | HPC clusters | workstations, CI | workstations, CI | untrusted agents |

The apptainer airgap needs `unshare`, `ip`, `slirp4netns` and unprivileged user namespaces;
`aalegate-run` checks them before starting. Docker/Kata containers run as your UID with
`--cap-drop ALL`, `no-new-privileges` and a PID limit.

## Reading the logs

```bash
aalegate-tui                                   # interactive reader: pick a run, browse turns
aalegate-access                                # egress log of a run (--summary for totals)
aalegate-gateway -verify "$RUN/plane1.jsonl"   # check the hash chain
```

More in [tui/](tui/README.md).

## Why Aalegate

**Agentic AI** is a powerful tool. Realizing its full potential, however, requires complete
delegation: letting agents write code, install packages, and execute commands autonomously. That
puts the bare PC systems most individuals run at real risk.

The risks can be **catastrophic**, and an agentic harness alone cannot prevent them. Agents are
prone to **hallucinations** that can lead to fatal deletions on the system. Full access to the
internet risks **prompt injection** or a **supply chain attack** that exploits the system.

**Containerization** isolates the agentic workflow, but isolation alone is not sufficient. A
fully air-gapped container may not reach critical services, such as provider authentication. A
container with internet access is a real security risk, unless the user manages traffic
system-wide. Worse, logs inside the container are invisible to the user and tamper-prone. In
short, there is little middle ground between autonomous agentic AI and secure deployment.

**Aalegate**, the "**Agentic AI Lean Gateway**", proposes to be that middle ground.

Aalegate is a **lean gateway with only the essential functions** individual users need to run AI
agents safely on their own machines. It provides *airgap by default* with *controlled egress* to
essential domains (e.g. for LLM provider authentication). It sits between the agentic harness and
the LLM provider and *records all communication* between the two: system messages, prompts,
responses, tool calls. Log management is *centralized*: provenance from multiple harnesses lands
in one auditable place, serving as an *independent history for user-side audit* that agents
cannot tamper with (encryption at rest is planned).

Aalegate is a *per-user tool* that treats end users as **first-class citizens**: HPC users, home
lab operators, and individuals who want to delegate to autonomous agents without risking their
system.

It has a minimal third-party dependency philosophy by design, for the smallest possible supply
chain attack surface; this matters for individual users who don't have a security team auditing
their dependency trees (see [Dependency](#dependency)).

## Dependency

aalegate itself is three Go binaries and a Python launcher. Everything else below is either a
build tool, the container runtime you already use, or the agent you choose to run inside it.

### aalegate (what you install)

```
aalegate
├── aalegate-gateway   recorder          Go standard library only
├── aalegate-egress    egress proxy      Go standard library only
├── aalegate-tui       log reader        Go standard library, plus:
│   ├── golang.org/x/term  v0.38.0
│   ├── golang.org/x/text  v0.32.0
│   └── golang.org/x/sys   v0.39.0      (indirect, via x/term)
└── aalegate-run       launcher          Python >= 3.9 standard library only (no pip packages)
    ├── recorder.py, runtimes/*.py
    └── show-log.py, show-access.py, bpe_tokenizer.py, probe-node-proxy.py
```

Third-party code in total: **three modules, all maintained by the Go team** (`golang.org/x/*`),
with checksums pinned in `tui/go.sum`. The recorder and the egress proxy, which handle all agent
traffic, have none.

### Build time

| tool | needed for | notes |
|---|---|---|
| Go >= 1.24 | `install.sh` (host binaries into `bin/`) | the TUI needs 1.24; gateway and egress build with 1.18+. `install.sh --no-go` uses the committed `bin/*` |
| docker or podman | `build-aalegate.sh`, agent images | platform images build in `golang:1.23-alpine` / `golang:1.22-alpine` and ship `FROM scratch` (the static binary only) |
| apptainer | `build-apptainer.sh` (OCI image → `.sif`) | only for HPC |
| rsync | `install.sh` | optional; falls back to `cp` |

### Run time (host)

| runtime | host needs |
|---|---|
| docker | Docker |
| kata | Docker + `kata-runtime` + KVM |
| podman | podman (rootless) |
| apptainer | apptainer. The default netns airgap also needs `unshare` (util-linux), `ip` (iproute2), `slirp4netns`, and unprivileged user namespaces; `aalegate-run` checks these first |
| `--gpu nvidia` | NVIDIA driver; a CDI spec from the NVIDIA Container Toolkit is recommended |

### Agent images (third-party, not part of aalegate)

The agents are what aalegate isolates and records; their dependency trees are theirs.

| image | base | agent | pinning |
|---|---|---|---|
| `aalegate-claude-code` | `ubuntu:24.04` + apt tools | native `claude` binary from the official installer (`claude.ai/install.sh`) | **not pinned**: latest at build time |
| `aalegate-pi` | `node:24-bookworm-slim` (digest-pinned) + apt tools | npm `@earendil-works/pi-coding-agent@0.84.4` | 136 npm packages, locked with integrity hashes in `harnesses/pi/package-lock.json` |
| `aalegate-pi-cuda` | `nvidia/cuda:12.8.1-devel-ubuntu24.04` | same as `aalegate-pi` | same lock file |

The apt tools are `bash`, `curl`, `jq`, `git`, `ripgrep`, `python3`, and a few editors/utilities;
the shared entrypoint needs `bash`, `jq` and `curl`. The optional `AALE_TUI=rich` reader
(`aalegate-tui-rich`, bubbletea) is a separate binary and not part of this tree.

## File layout

```
aalegate-run            launcher (--runtime docker|podman|apptainer|kata)
recorder.py             shared run lifecycle: provenance, manifests, key custody, egress groups
runtimes/               one wrapper per runtime (kata.py delegates to docker.py)
gateway/                recorder (Go)                     -> gateway/README.md
egress/                 egress proxy (Go)                 -> egress/README.md
tui/                    log reader (Go)                   -> tui/README.md
harnesses/              agent image recipes + entrypoint  -> harnesses/README.md
bin/                    prebuilt gateway, egress, tui (for hosts without Go)
install.sh              install to ~/.local/share/aalegate + PATH shims (--uninstall, --purge)
build-aalegate.sh       recorder + egress images
build-claude.sh, build-pi.sh, build-apptainer.sh   agent images, .sif conversion
netns-run               apptainer's rootless netns airgap
show-log.py, show-access.py, bpe_tokenizer.py      log readers (aalegate-log, aalegate-access)
probe-node-proxy.py     checks from inside an agent that tools go through the proxy
examples/               CUDA GEMM demo, tiny LLM for testing
```

## Status and roadmap

What works today:

| harness | docker | podman | apptainer | notes |
|---|---|---|---|---|
| Claude Code | ✓ | ✓ | ✓ | subscription, API key (custody), or local model |
| pi | ✓ | ✓ | ✓ | local models (llama.cpp) |
| pi-cuda | ✓ | ✓ | — | CUDA 12.8; DL stack via a mounted conda |

LLM endpoints: OpenAI-compatible (llama.cpp, vLLM, SGLang, TGI) and Anthropic Messages
(`/v1/messages`, including streaming, thinking and tool use). Kata runs the same images as
docker.

Coming next:

- **Unix-socket isolation** on every runtime: the agent gets no network at all, only sockets to
  the recorder and proxy, closing the remaining loopback exposure on apptainer
- a recorder allowlist of API paths; closing the subscription side channel; signed log checkpoints
- harnesses: codex; endpoints: native OpenAI
- log encryption at rest, an MCP server add-on, a database add-on, a live streaming TUI
- `--runtime sbx` (Docker Sandboxes, recorder-only)

## Environment variables

| variable | purpose | default |
|---|---|---|
| `AALE_HOME` | install location | `~/.local/share/aalegate` |
| `AALE_AUDIT_ROOT` | where run logs are stored | `~/.local/state/aalegate` |
| `AALE_PREFIX` | prefix for the PATH shims (`$PREFIX/bin`) | `~/.local` |
| `AALE_ENGINE` | container engine for the build scripts | `docker` |
| `AALE_CDI` / `AALE_CDI_DIR` | GPU: `0` forces the SONAME fallback / extra CDI spec dir | — |
| `AALE_TUI` / `AALE_TUI_THEME` | `rich` for the add-on reader / TUI color theme | — / `nocturnal` |

Variables that `aalegate-run` sets inside the agent are listed in
[harnesses/](harnesses/README.md#self-wiring).
