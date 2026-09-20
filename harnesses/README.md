# aalegate agent harnesses

We ship **Dockerfiles (recipes), not prebuilt images.** You build your own — so nothing
inherits the builder's UID, and the install provenance is the upstream package, not a vendored
blob. (Build once on a networked box and convert to a `.sif` for airgapped/HPC use.)

## Images

| harness | base | agent install | build (context = aalegate root) |
|---|---|---|---|
| `pi/` | `node:24-slim` | `npm ci` from a pinned `package-lock.json` (pi `@0.84.4`, official package) | `docker build -t aalegate-pi -f harnesses/pi/Dockerfile .` |
| `claude-code/` | `ubuntu:24.04` | native `claude` binary via `claude.ai/install.sh` | `docker build -t aalegate-claude-code -f harnesses/claude-code/Dockerfile .` |
| `pi-cuda/` | CUDA 12.8 / Ubuntu 24.04 | same `npm ci` lockfile as pi (node copied from the pinned node image) | `./build-pi.sh --cuda` |

Swap `docker` for `podman` for a rootless, daemonless build; both work.

## UID-neutral by design

None of these bake a user. Build as root (image files are uid 0 — universal); identity is
mapped at **run** time, not build time:

- **docker / podman:** `--user $(id -u):$(id -g)` (aalegate-run injects this).
- **podman rootless:** run as image-root — the rootless user namespace maps it to your host UID,
  so files on your mounts come out owned by you (no `--user` needed).
- **apptainer:** runs as the invoking user natively; the `.sif` bakes no UID.

`HOME=/home/user` and `/workspace` are world-writable, so any UID gets a writable home + workdir.
Whatever you `--work` / `--mount` (conda, datasets, volumes) is read/written as *you* — no chown
dance, no leaked builder UID.

## Hardening the build

`docker build` runs on your host, **outside** aalegate's runtime airgap — so the build is the
exposure surface for a bad npm/apt package. Reduce blast radius:

- **Build rootless.** Rootful docker runs the build *as root* via a root daemon (and the `docker`
  group is root-equivalent) — invoking `docker build` as a non-root *user* does **not** change
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

> Note: the claude image installs via `curl https://claude.ai/install.sh | bash` — remote code
> executed at build (Anthropic's official installer, over HTTPS). Same mitigations apply: build
> rootless with egress scoped to `claude.ai`.

## Self-wiring

Each image's `ENTRYPOINT` is the shared `entrypoint.sh`, which reads `/etc/aalegate/harness.json`
and points the agent at the recorder (`ANTHROPIC_BASE_URL` / provider config) before exec'ing it.
The same image therefore runs standalone *or* under `aalegate-run` with full plane-1 capture.
