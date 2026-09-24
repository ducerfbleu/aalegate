# aalegate agent harnesses

We ship **Dockerfiles (recipes), not prebuilt images.** You build your own—so nothing
inherits the builder's UID, and the install provenance is the upstream package, not a vendored
blob. (Build once on a networked box and convert to a `.sif` for airgapped/HPC use.)

## Images

| harness | base | agent install | build (context = aalegate root) |
|---|---|---|---|
| `pi/` | `node:24-bookworm-slim` (digest-pinned) | `npm ci` from a pinned `package-lock.json` (pi `@0.84.4`, official package) | `./build-pi.sh` |
| `claude-code/` | `ubuntu:24.04` | native `claude` binary via `claude.ai/install.sh` | `./build-claude.sh` |
| `pi-cuda/` | CUDA 12.8 / Ubuntu 24.04 | same `npm ci` lockfile as pi (node copied from the pinned node image) | `./build-pi.sh --cuda` |

Swap `docker` for `podman` for a rootless, daemonless build; both work. Build with the **same
engine you run with**: podman's image store is separate from docker's (`--engine podman`, or
`AALE_ENGINE=podman`).

### Apptainer (HPC)

Apptainer doesn't build Dockerfiles; convert an image that's already in a podman or docker store:

```bash
./build-pi.sh --engine podman && ./build-apptainer.sh --engine podman aalegate-pi
./build-claude.sh --engine podman && ./build-apptainer.sh --engine podman aalegate-claude-code
# -> aalegate-pi.sif + aalegate-pi.sif.json (provenance); --sign to sign, --tmpdir for scratch space
```

`aalegate-run` re-hashes the `.sif` and checks its signature at run time.

## UID-neutral by design

None of these bake a user. Build as root (image files are uid 0—universal); identity is
mapped at **run** time, not build time:

- **docker / podman:** `--user $(id -u):$(id -g)` (aalegate-run injects this).
- **podman rootless:** run as image-root—the rootless user namespace maps it to your host UID,
  so files on your mounts come out owned by you (no `--user` needed).
- **apptainer:** runs as the invoking user natively; the `.sif` bakes no UID.

`HOME=/home/user` and `/workspace` are world-writable, so any UID gets a writable home + workdir.
Whatever you `--work` / `--mount` (conda, datasets, volumes) is read/written as *you*—no chown
dance, no leaked builder UID.

## Hardening the build

`docker build` runs on your host, **outside** aalegate's runtime airgap—so the build is the
exposure surface for a bad npm/apt package. Reduce blast radius:

- **Build rootless.** Rootful docker runs the build *as root* via a root daemon (and the `docker`
  group is root-equivalent)—invoking `docker build` as a non-root *user* does **not** change
  that. Use a rootless engine so the whole build runs in your user namespace and a compromise is
  capped at your UID: `podman build` (rootless by default), `buildah`, or rootless dockerd.
- **Scope the build's egress.** A Dockerfile can't restrict its own network; the *builder's*
  network does. Build behind a firewall/netns that allows only the package source
  (`registry.npmjs.org` for pi, `claude.ai` for the claude installer) + the base-image registry,
  so a compromised step can't exfiltrate or pull a second-stage payload. Or pre-seed a local npm
  cache and build `--network=none` (fully offline).
- **Pin + verify.** pi uses `--ignore-scripts` (closes the install-script vector); pin the base by
  digest (`--build-arg NODE_IMAGE=…@sha256:…`) and the pi version (`@X.Y.Z` + `npm ci`). We don't
  run the agent during the build; verify after: `docker run --rm --entrypoint pi aalegate-pi --version`.
- Build on a **disposable host / CI runner**, not your primary bare-metal box.

> Note: the claude image installs via `curl https://claude.ai/install.sh | bash`—remote code
> executed at build (Anthropic's official installer, over HTTPS). Same mitigations apply: build
> rootless with egress scoped to `claude.ai`.

## Self-wiring

Each image's `ENTRYPOINT` is the shared `entrypoint.sh`, which reads `/etc/aalegate/harness.json`
and points the agent at the recorder (`ANTHROPIC_BASE_URL` / provider config) before exec'ing it.
The same image therefore runs standalone *or* under `aalegate-run` with full plane-1 capture.

What `aalegate-run` sets inside the agent:

| variable | meaning |
|---|---|
| `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, `LLAMA_HOST`/`LLAMA_PORT` | the recorder |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | a dummy (`aalegate`); the recorder holds the real key. Unset for `--subscription` |
| `HTTPS_PROXY`, `NO_PROXY`, `NODE_USE_ENV_PROXY=1` | the egress proxy, only with `--egress`/`--allow`/`--subscription` |
| `AALE_SHELL` | `--shell`: drop to bash instead of starting the agent |
| `AALE_ANTHROPIC_LOCAL` | `--local`: map Claude's model tiers onto the local model |
| `AALE_MANIFEST` | override the manifest path (default `/etc/aalegate/harness.json`) |

## Bring your own image (podman)

`--wire PROFILE` or `--manifest FILE` injects the self-wiring entrypoint into an image that
wasn't built from these recipes. The image needs `bash`, `jq` and `curl`.

## NVIDIA GPU (`pi-cuda`)

```bash
./aalegate-run --runtime podman --agent pi-cuda --gpu nvidia \
  --llm http://127.0.0.1:PORT --work "$PWD/work" \
  --mount /opt/miniconda3:/opt/miniconda3 -p "profile the kernel"   # DL stack via a mounted conda
```

`--gpu nvidia` (docker and podman) picks the first tier that applies:

1. **CDI (recommended).** With an NVIDIA CDI spec present it passes `--device nvidia.com/gpu=all`,
   and the toolkit injects the device nodes, version-matched driver libraries and `nvidia-smi`.
   Create the spec once per host, and again after driver updates:
   `sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`. Without root, write it under
   `$HOME`, `export AALE_CDI_DIR=$HOME/cdi`, and add `cdi_spec_dirs = ["/home/<you>/cdi"]` under
   `[engine]` in `~/.config/containers/containers.conf`.
2. **WSL2.** If `/dev/dxg` exists: `--device /dev/dxg` plus `-v /usr/lib/wsl`.
3. **SONAME fallback.** Explicit `/dev/nvidia*` plus host driver libraries mounted by SONAME.
   Brittle (hard-wired `/usr/lib/x86_64-linux-gnu`); force it with `AALE_CDI=0`.

Apptainer uses its own `--nv` hook.
