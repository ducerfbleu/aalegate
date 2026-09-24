"""recorder.py---shared recorder lifecycle for all aalegate runtimes.

Manages the aalegate-gateway (start/stop), audit directory, key custody, provenance
index, egress domain lists, and plane-3 filesystem manifests. Runtime-agnostic: the
gateway can run as a Docker container, Podman container, or host process.

Imported by the runtime wrappers (runtimes/*.py) and by the unified aalegate-run CLI.
Also executable standalone:
    aalegate-recorder start --llm URL [--llm-key-file PATH] [--api openai|anthropic]
    aalegate-recorder stop [--run-id ID]
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent

EGRESS_DOMAINS = {
    "gh": ["github.com", "api.github.com", "objects.githubusercontent.com", "raw.githubusercontent.com"],
    "hf": ["huggingface.co", "hf.co"],
    "pypi": ["pypi.org", "files.pythonhosted.org"],
    # Claude Code subscription auth + its direct-to-Anthropic side calls. Inference (/v1/messages)
    # still routes through the recorder via ANTHROPIC_BASE_URL (plane 1) --- but Claude Code ALSO
    # calls api.anthropic.com DIRECTLY (not via the base URL) for the first-run connectivity check,
    # the fast-mode + WebFetch-safety checks, and feature flags, and platform.claude.com for OAuth
    # token exchange/refresh. If those are blocked, startup fails with 403 / "Unable to connect to
    # Anthropic services". Trade-off: api.anthropic.com being
    # reachable means a direct call would show in plane 2 only as a CONNECT, not payload --- inherent
    # to subscription auth (the agent holds the token). Inference itself is still captured in plane 1.
    "anthropic": ["api.anthropic.com", "claude.ai", "claude.com", "platform.claude.com",
                  "downloads.claude.ai", "code.claude.com", "mcp-proxy.anthropic.com"],
    "go": ["proxy.golang.org", "sum.golang.org", "storage.googleapis.com", "golang.org"],
}

AGENTS = {
    "pi": {"image": "aalegate-pi"},
    "pi-cuda": {"image": "aalegate-pi-cuda"},
    "claude-code": {"image": "aalegate-claude-code",
                    "defaults": {"api": "anthropic",
                                 "llm": "https://api.anthropic.com",
                                 "llm_verify": True}},
}

DEFAULT_PORT = 8080
# override with $AALE_AUDIT_ROOT; the --audit-root flag still wins over both.
DEFAULT_AUDIT_ROOT = Path(os.environ.get("AALE_AUDIT_ROOT") or
                          HOME / ".local" / "state" / "aalegate").expanduser()
GATEWAY_IMAGE = "aalegate-gateway"
EGRESS_IMAGE = "aalegate-egress"              # Go CONNECT allow-list proxy (every runtime)


def die(m):
    print(f"error: {m}", file=sys.stderr)
    sys.exit(1)


def resolve_dir(p, label):
    d = Path(p).expanduser().resolve()
    if not d.is_dir():
        die(f"{label} '{p}' is not a directory")
    return d


def host_dotfiles(names):
    """--dotfile NAME...: the host ~/.NAME entries to bind (rw) into the agent's home, as
    (host_path, ".NAME"). 'claude' and '.claude' are the same; duplicates are dropped. A missing
    entry is created as a directory (agent state dirs like ~/.claude, ~/.pi); an existing file
    is bound as a file."""
    out = []
    for n in names or []:
        dot = n if n.startswith(".") else f".{n}"
        if (not all(c.isalnum() or c in "._-" for c in dot[1:]) or not dot[1:2].isalnum()):
            die(f"--dotfile '{n}': expected a name like 'claude' or '.pi' (an entry directly under ~)")
        if any(d == dot for _, d in out):
            continue
        host = Path.home() / dot
        if not host.exists():
            host.mkdir(parents=True)
        out.append((host, dot))
    return out


def resolve_agent(name):
    entry = AGENTS.get(name)
    return entry["image"] if entry else name


def agent_defaults(name):
    entry = AGENTS.get(name)
    return dict(entry.get("defaults", {})) if entry else {}


# ---- key custody -------------------------------------------------------------

def resolve_llm_key(llm_key=None, llm_key_file=None, llm_key_env=None):
    """Where the REAL upstream key comes from. Returns (kind, value):
    ('file', path) | ('value', secret) | (None, None). Fed to the recorder only."""
    if llm_key_file:
        p = Path(llm_key_file).expanduser()
        if not p.is_file():
            die(f"--llm-key-file: not a file: {p}")
        return "file", str(p.resolve())
    if llm_key_env:
        v = os.environ.get(llm_key_env)
        if not v:
            die(f"--llm-key-env {llm_key_env}: not set in the environment")
        return "value", v
    if llm_key:
        print("warning: --llm-key puts the secret on argv (visible in `ps`); prefer --llm-key-file",
              file=sys.stderr)
        return "value", llm_key
    return None, None


# ---- recorder env (agent-facing) ---------------------------------------------

def recorder_env(host, port, key=None, anthropic_subscription=False):
    """Env vars the agent needs to reach the recorder. In key-custody mode the agent
    gets a dummy key and the real key lives in the recorder.

    anthropic_subscription: subscription/OAuth mode for Claude Code. Leave ANTHROPIC_API_KEY
    UNSET so the CLI performs its own claude.ai login instead of API-key auth; the recorder
    then forwards the OAuth bearer upstream unchanged (no custody). ANTHROPIC_BASE_URL still
    points at the recorder so /v1/messages is captured (plane 1)."""
    base = f"http://{host}:{port}"
    k = key or "aalegate"
    env = {
        "PI_OFFLINE": "1", "LLAMA_HOST": host, "LLAMA_PORT": str(port),
        "OPENAI_BASE_URL": f"{base}/v1", "OPENAI_API_BASE": f"{base}/v1", "OPENAI_API_KEY": k,
        "ANTHROPIC_BASE_URL": base,
    }
    if not anthropic_subscription:
        # a set key keeps Claude Code out of its browser OAuth flow and is the dummy the
        # gateway swaps for the real upstream key (key custody).
        env["ANTHROPIC_API_KEY"] = k
    if key:
        env["LLAMA_API_KEY"] = key
    return env


# ---- egress domain list -------------------------------------------------------

def egress_for(groups):
    if not groups:
        return []
    req = [g.strip() for g in groups.split(",") if g.strip()]
    if "all" in req:
        req = list(EGRESS_DOMAINS)
    bad = [g for g in req if g not in EGRESS_DOMAINS]
    if bad:
        die(f"unknown egress groups: {', '.join(bad)}; have: {', '.join(EGRESS_DOMAINS)}")
    out = []
    for g in req:
        out += EGRESS_DOMAINS[g]
    return out


# ---- plane-3 filesystem manifest ----------------------------------------------

def manifest(dirs, do_hash=True, cap=50 * 1024 * 1024):
    out = {}
    for d in dirs:
        base = Path(d)
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if not f.is_file() or f.is_symlink():
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            rec = {"size": st.st_size, "mtime": int(st.st_mtime)}
            if do_hash and st.st_size <= cap:
                try:
                    h = hashlib.sha256()
                    with open(f, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            h.update(chunk)
                    rec["sha256"] = h.hexdigest()
                except OSError:
                    pass
            out[str(f)] = rec
    return out


def manifest_diff(before, after):
    ch = []
    for p, a in after.items():
        b = before.get(p)
        if b is None:
            ch.append({"path": p, "status": "created", "sha256": a.get("sha256"), "size": a["size"]})
        elif b.get("sha256") != a.get("sha256") or b["size"] != a["size"]:
            ch.append({"path": p, "status": "modified",
                       "sha256_before": b.get("sha256"), "sha256_after": a.get("sha256")})
    for p, b in before.items():
        if p not in after:
            ch.append({"path": p, "status": "deleted", "sha256_before": b.get("sha256")})
    return ch


# ---- provenance index ---------------------------------------------------------

def write_index(audit_dir, run_id, *, runtime, status, agent, api, llm,
                gpu=None, egress=None, work=None, rc=None, started=None, **extra):
    """Write or update the run's index.json. `extra` passes through runtime-specific
    provenance fields (agent_image_digest, sif_sha256, signature, etc.)."""
    plane1 = audit_dir / "plane1.jsonl"
    plane2 = audit_dir / "access.log"
    chain_head, chain_len, _last = "", 0, ""
    if plane1.exists():
        with open(plane1) as _f:
            for _line in _f:
                if _line.strip():
                    chain_len += 1
                    _last = _line
        if _last:
            try:
                chain_head = json.loads(_last).get("record_hash", "")
            except (ValueError, TypeError):
                pass
    idx = {
        "run_id": run_id, "runtime": runtime, "status": status, "agent": agent,
        "api": api, "llm": llm, "gpu": gpu or "none", "egress": egress or "none",
        "started": int(started) if started else None,
        "ended": None if status == "running" else int(time.time()), "exit_code": rc,
        "work": [[str(p), m] for p, m in (work or [])],
        "planes": {"llm_log": str(plane1), "egress_log": str(plane2),
                   "fs_manifest": str(audit_dir / "plane3.json")},
        "counts": {
            "llm_records": chain_len,
            "egress_lines": sum(1 for _ in open(plane2)) if plane2.exists() else 0,
        },
        "chain": {"head": chain_head, "len": chain_len},
        **extra,
    }
    (audit_dir / "index.json").write_text(json.dumps(idx, indent=2))
    return idx


# ---- image helpers (shared by docker/podman/kata) -----------------------------

def image_exists(name, cmd="docker"):
    if cmd == "podman":
        return subprocess.run(["podman", "image", "exists", name], capture_output=True).returncode == 0
    return subprocess.run(["docker", "image", "inspect", name], capture_output=True).returncode == 0


def image_digest(name, cmd="docker"):
    r = subprocess.run([cmd, "inspect", "--format", "{{.Id}}", name], capture_output=True, text=True)
    img_id = r.stdout.strip() if r.returncode == 0 else None
    r2 = subprocess.run([cmd, "inspect", "--format", "{{range .RepoDigests}}{{.}} {{end}}", name],
                        capture_output=True, text=True)
    toks = r2.stdout.split() if r2.returncode == 0 else []
    return img_id, (toks[0] if toks else None)


# ---- network helpers (shared by docker/podman/kata) ---------------------------

def ensure_net(name, internal=True, cmd="docker"):
    if subprocess.run([cmd, "network", "inspect", name], capture_output=True).returncode != 0:
        args = [cmd, "network", "create"] + (["--internal"] if internal else []) + [name]
        subprocess.run(args, check=True)
        print(f"created network: {name}")


def net_connect(net, ctr, cmd="docker"):
    subprocess.run([cmd, "network", "connect", net, ctr], capture_output=True)


def rm_container(name, cmd="docker"):
    subprocess.run([cmd, "rm", "-f", name], capture_output=True)


# ---- GPU helpers --------------------------------------------------------------

def _cdi_nvidia_device():
    """A CDI device string ('nvidia.com/gpu=all') if an NVIDIA CDI spec is present, else None.
    CDI is the correct, host-agnostic path: `nvidia-ctk cdi generate` writes nvidia.yaml/json into
    /etc/cdi or /var/run/cdi, enumerating the right device nodes + version-matched driver libs +
    nvidia-smi for THIS host --- no hardwired lib paths, no single-version pin. Preferred over the
    SONAME bind-mount fallback. Escape hatches: AALE_CDI=0 forces the manual fallback;
    AALE_CDI_DIR adds a spec dir (for no-root / HPC, where CDI lives under $HOME)."""
    if os.environ.get("AALE_CDI") == "0":
        return None
    dirs = ["/etc/cdi", "/var/run/cdi"]
    extra = os.environ.get("AALE_CDI_DIR")
    if extra:
        dirs.insert(0, extra)
    for d in dirs:
        try:
            for f in Path(d).iterdir():
                if f.name.startswith("nvidia") and f.suffix in (".yaml", ".yml", ".json"):
                    return "nvidia.com/gpu=all"
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
    return None


def gpu_args_nvidia_docker():
    """NVIDIA GPU for Docker/Kata. Preference: (1) CDI --- nvidia.com/gpu=all when a CDI spec exists
    (correct + complete + version-matched; run `nvidia-ctk cdi generate`); (2) WSL2 --- /dev/dxg +
    /usr/lib/wsl; (3) fallback --- explicit device nodes + host driver libs by SONAME (brittle:
    hardwires /usr/lib/x86_64-linux-gnu and only 3 libs). CDI on docker needs docker 25+."""
    args, env = [], []
    cdi = _cdi_nvidia_device()
    if cdi:
        return ["--device", cdi], []
    if Path("/dev/dxg").exists():
        args += ["--device", "/dev/dxg"]
        if Path("/usr/lib/wsl").is_dir():
            args += ["-v", "/usr/lib/wsl:/usr/lib/wsl:ro"]
        env += ["-e", "LD_LIBRARY_PATH=/usr/lib/wsl/lib:/usr/local/cuda/lib64"]
        return args, env
    for dev in ("/dev/nvidia0", "/dev/nvidiactl", "/dev/nvidia-uvm"):
        if Path(dev).exists():
            args += ["--device", dev]
    libdir = "/usr/lib/x86_64-linux-gnu"
    for so in ("libcuda.so.1", "libnvidia-ml.so.1", "libnvidia-ptxjitcompiler.so.1"):
        src = os.path.realpath(os.path.join(libdir, so))
        if os.path.isfile(src):
            args += ["-v", f"{src}:/opt/nvidia-driver/{so}:ro"]
    env += ["-e", "LD_LIBRARY_PATH=/opt/nvidia-driver:/usr/local/cuda/lib64"]
    return args, env


def gpu_args_podman(kind):
    """GPU passthrough for Podman: NVIDIA and/or Intel. NVIDIA preference mirrors
    gpu_args_nvidia_docker(): CDI (nvidia.com/gpu=all) if a spec exists, else WSL2 /dev/dxg,
    else SONAME driver-lib mounts."""
    args, env = [], []
    if kind in ("nvidia", "both"):
        cdi = _cdi_nvidia_device()
        if cdi:
            args += ["--device", cdi]
        elif Path("/dev/dxg").exists():
            # WSL2: the GPU is reached through /dev/dxg + the Windows driver's userspace shipped in
            # /usr/lib/wsl/lib (libcuda.so.1, libnvidia-ml.so.1, nvidia-smi, ...). There is NO
            # /dev/nvidia* and nothing in /usr/lib/x86_64-linux-gnu --- pass dxg + mount that tree.
            args += ["--device", "/dev/dxg"]
            if Path("/usr/lib/wsl").is_dir():
                args += ["-v", "/usr/lib/wsl:/usr/lib/wsl:ro"]
            env += ["-e", "LD_LIBRARY_PATH=/usr/lib/wsl/lib:/usr/local/cuda/lib64"]
        else:
            for dev in ("/dev/nvidia0", "/dev/nvidiactl", "/dev/nvidia-uvm"):
                if Path(dev).exists():
                    args += ["--device", dev]
            libdir = "/usr/lib/x86_64-linux-gnu"
            for so in ("libcuda.so.1", "libnvidia-ml.so.1", "libnvidia-ptxjitcompiler.so.1"):
                src = os.path.realpath(os.path.join(libdir, so))
                if os.path.isfile(src):
                    args += ["-v", f"{src}:/opt/nvidia-driver/{so}:ro"]
            env += ["-e", "LD_LIBRARY_PATH=/opt/nvidia-driver:/usr/local/cuda/lib64"]
    if kind in ("intel", "both"):
        for dev in ("/dev/dri/renderD128", "/dev/dri/card0"):
            if Path(dev).exists():
                args += ["--device", dev]
        env += ["-e", "UR_LOADER_USE_LEVEL_ZERO_V2=0"]
    return args, env


# ---- SIF provenance (apptainer) -----------------------------------------------

def sif_provenance(sif_path):
    sha = None
    try:
        h = hashlib.sha256()
        with open(sif_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        sha = h.hexdigest()
    except OSError:
        pass
    return sha, _sif_verify(sif_path)


def _sif_verify(sif_path):
    if not _have("apptainer"):
        return {"status": "unknown", "signer": None, "detail": "apptainer not on PATH"}
    r = subprocess.run(["apptainer", "verify", str(sif_path)], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0:
        signer = None
        for line in out.splitlines():
            s = line.strip()
            low = s.lower()
            if low.startswith("signing entity"):
                signer = s.split(":", 1)[1].strip()
            elif low.startswith("fingerprint") and not signer:
                signer = s.split(":", 1)[1].strip()
        return {"status": "verified", "signer": signer, "detail": out[:2000]}
    low = out.lower()
    unsigned = any(t in low for t in ("signature not found", "no signatures", "no signature",
                                      "not signed", "no objects"))
    return {"status": "unsigned" if unsigned else "unverified", "signer": None, "detail": out[:2000]}


def _have(prog):
    return subprocess.run(["sh", "-c", f"command -v {prog}"], capture_output=True).returncode == 0


def wait_port(host, port, timeout=10):
    import socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


# ---- run lifecycle (used by all runtimes) -------------------------------------

def project_handle(path):
    """Directory handle for a project path: '<stem>__<hash8>'.

    The 8-hex sha256 of the canonical realpath owns uniqueness --- two projects
    that share a basename (e.g. two 'pi-agent' checkouts) get different hashes,
    so they never collide. The stem is cosmetic (a filesystem-safe basename) and
    the '__' seam marks name-vs-hash even when the name itself contains dashes.
    Same realpath -> same handle, always; a move/rename yields a new handle."""
    real = Path(path).expanduser().resolve()
    stem = "".join(c if c.isalnum() or c in "._-" else "_" for c in real.name) or "root"
    h = hashlib.sha256(str(real).encode()).hexdigest()[:8]
    return f"{stem}__{h}"


def session_candidates(work_dirs):
    """Rank candidate session-root dirs from a run's --work dirs.

    Returns [(Path, coverage), ...] where coverage is how many work dirs sit at or under
    that dir. Candidates are each work dir plus every ancestor shared by >=2 work dirs,
    bounded at their deepest common ancestor and never $HOME or the filesystem root. Sorted
    by coverage desc, then --work order: the top entry is the broadest shared parent (the
    default / -y pick), the tail is the individual work dirs; project_handle() then names
    whichever the caller selects. Empty -> []; a single work dir -> just that dir."""
    reals = []
    for w in work_dirs:
        r = Path(w).expanduser().resolve()
        if r not in reals:
            reals.append(r)
    n = len(reals)
    if n == 0:
        return []
    if n == 1:
        return [(reals[0], 1)]

    dca = Path(os.path.commonpath([str(r) for r in reals]))
    home = Path.home().resolve()
    root = Path(reals[0].anchor)

    def under(r, c):
        return r == c or c in r.parents

    seen = []
    for r in reals:
        if r not in seen:
            seen.append(r)
        for anc in r.parents:          # climb only up to the common ancestor (inclusive)
            if anc == dca:
                if anc not in seen:
                    seen.append(anc)
                break
            if dca in anc.parents:     # anc sits between r and dca -> keep climbing
                if anc not in seen:
                    seen.append(anc)
                continue
            break                      # anc is at/above dca's level but != dca -> stop

    def first_mention(c):
        for i, r in enumerate(reals):
            if under(r, c):
                return i
        return n

    ranked = []
    for c in seen:
        cov = sum(1 for r in reals if under(r, c))
        if c not in reals and (cov < 2 or c == home or c == root):
            continue                   # parents: only if shared by >=2, never $HOME or /
        ranked.append((c, cov, first_mention(c)))
    ranked.sort(key=lambda t: (-t[1], t[2], str(t[0])))
    return [(c, cov) for c, cov, _ in ranked]


def new_run_id():
    """Sortable, human-readable per-session id: UTC timestamp + 4 hex (so two
    runs in the same second don't collide). Filesystem-safe (no colons)."""
    return time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:4]


def create_run(audit_root=None, run_id=None, project=None):
    """Runs live under <audit_root>/<project-handle>/<run_id>/, grouping every
    session of a project in one browsable dir. `project` defaults to the CWD
    (the invocation dir is the project root); pass an explicit path to override."""
    run_id = run_id or new_run_id()
    handle = project_handle(project or Path.cwd())
    audit_dir = Path(audit_root or DEFAULT_AUDIT_ROOT).expanduser() / handle / run_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    run_home = audit_dir / "home"
    run_home.mkdir(parents=True, exist_ok=True)
    return run_id, audit_dir, run_home


def parse_work_data(args):
    """Parse --work/--work-ro/--data/--data-rw into (work, data, write_dirs)."""
    work = [(resolve_dir(w, "--work"), "rw") for w in (args.work or [])]
    work += [(resolve_dir(w, "--work-ro"), "ro") for w in (args.work_ro or [])]
    data = [(resolve_dir(d, "--data"), "ro") for d in (args.data or [])]
    data += [(resolve_dir(d, "--data-rw"), "rw") for d in (args.data_rw or [])]
    write_dirs = [str(p) for p, m in work if m == "rw"]
    write_dirs += [str(p) for p, m in data if m == "rw"]
    return work, data, write_dirs


def run_with_provenance(run_fn, *, audit_dir, run_id, runtime, args, upstream,
                        work, write_dirs, no_hash=False, **index_extra):
    """Wrap an agent run with before/after manifest and index writes."""
    before = manifest(write_dirs, do_hash=not no_hash)
    started = time.time()
    write_index(audit_dir, run_id, runtime=runtime, status="running", agent=args.agent,
                api=args.api, llm=upstream, gpu=getattr(args, "gpu", None),
                egress=getattr(args, "egress", None), work=work, started=started, **index_extra)
    rc, status = None, "interrupted"
    try:
        rc = run_fn()
        status = "completed"
    finally:
        after = manifest(write_dirs, do_hash=not no_hash)
        (audit_dir / "plane3.json").write_text(json.dumps(
            {"run_id": run_id, "write_dirs": write_dirs,
             "changes": manifest_diff(before, after)}, indent=2))
        idx = write_index(audit_dir, run_id, runtime=runtime, status=status, agent=args.agent,
                          api=args.api, llm=upstream, gpu=getattr(args, "gpu", None),
                          egress=getattr(args, "egress", None), work=work,
                          rc=rc, started=started, **index_extra)
    print(f"\nprovenance: {audit_dir}/index.json  ({idx['counts']['llm_records']} llm records, {status})")
    return rc
