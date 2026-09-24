"""Apptainer (HPC) runtime wrapper for aalegate."""
import os
import subprocess
import sys
from pathlib import Path

from recorder import (
    DEFAULT_PORT, SCRIPT_DIR, EGRESS_DOMAINS,
    die, resolve_llm_key, recorder_env, egress_for, sif_provenance, _have, wait_port,
    create_run, parse_work_data, run_with_provenance, host_dotfiles,
)

HOME = Path.home()


def _parse_mount(spec):
    parts = spec.split(":")
    if len(parts) == 2:
        host, ctr, mode = parts[0], parts[1], "rw"
    elif len(parts) == 3:
        host, ctr, mode = parts
        if mode not in ("ro", "rw"):
            die(f"--mount '{spec}': mode must be 'ro' or 'rw', got '{mode}'")
    else:
        die(f"--mount '{spec}': expected HOST:CONTAINER[:ro|rw]")
    if not ctr.startswith("/"):
        die(f"--mount '{spec}': container path must be absolute")
    from recorder import resolve_dir
    return (resolve_dir(host, "--mount"), Path(ctr), mode)


# what netns-run needs on PATH, and the package that provides it
NETNS_TOOLS = {"unshare": "util-linux", "ip": "iproute2", "slirp4netns": "slirp4netns"}


def _netns_preflight():
    """Check what the netns airgap needs BEFORE anything starts; die with the fix if it can't work."""
    problems = []
    netns_run = SCRIPT_DIR / "netns-run"
    if not os.access(netns_run, os.X_OK):
        problems.append(f"{netns_run} is missing or not executable (re-run ./install.sh)")
    missing = [t for t in NETNS_TOOLS if not _have(t)]
    if missing:
        problems.append(f"not on PATH: {', '.join(missing)}---install "
                        f"{' '.join(NETNS_TOOLS[t] for t in missing)}")
    if _have("unshare"):
        r = subprocess.run(["unshare", "--user", "--map-root", "--net", "true"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            problems.append("unprivileged user namespaces are unavailable here ("
                            f"{r.stderr.strip() or f'unshare: exit {r.returncode}'}). Check "
                            "kernel.unprivileged_userns_clone, user.max_user_namespaces, or the "
                            "AppArmor userns restriction (Ubuntu 24.04+), or ask your admins")
    if problems:
        die("the network-namespace airgap (the apptainer default) can't be set up:\n  - "
            + "\n  - ".join(problems)
            + "\nFix the above, or pass --no-netns to run WITHOUT enforcement (the agent shares the "
              "host network and can bypass the egress proxy, unlogged).")


def _policy_note(args):
    if args.netns:
        print("note: netns airgap via netns-run: rootless netns, no default route, and apptainer\n"
              "      --drop-caps NET_ADMIN,NET_RAW (the agent can't re-add the route). Still reachable:\n"
              "      every host-loopback port at 10.0.2.2 (I12), until the Unix-socket design lands.",
              file=sys.stderr)
    else:
        print("WARNING: --no-netns---Apptainer SHARES THE HOST NETWORK. The airgap is POLICY only:\n"
              "         any tool that ignores HTTPS_PROXY (e.g. Node fetch without NODE_USE_ENV_PROXY,\n"
              "         custom clients, raw sockets) reaches the internet DIRECTLY, and that traffic is\n"
              "         NOT in access.log (verified with probe-node-proxy.py). Only traffic that uses\n"
              "         the proxy or the recorder is audited. Drop --no-netns for the enforced airgap.",
              file=sys.stderr)


def run(args):
    agent_cmd = list(args.cmd)
    if agent_cmd and agent_cmd[0] == "--":
        agent_cmd = agent_cmd[1:]
    if args.prompt:
        agent_cmd += ["-p", args.prompt]
    args.netns = args.netns is not False   # on unless --no-netns (the effective value is recorded)
    if args.netns and not args.dry_run:
        _netns_preflight()

    run_id, audit_dir, run_home = create_run(args.audit_root, args.run_id, getattr(args, "project", None))
    run_scratch = audit_dir / "scratch"
    run_scratch.mkdir(parents=True, exist_ok=True)
    work, data, write_dirs = parse_work_data(args)
    mounts = [_parse_mount(m) for m in (args.mount or [])]
    write_dirs += [str(h) for h, _, m in mounts if m == "rw"]
    upstream = args.llm
    verify = args.llm_verify
    egress = egress_for(args.egress)
    subscription = getattr(args, "subscription", False)
    local = getattr(args, "local", False)
    if local and args.api == "openai":
        args.api = "anthropic"
    have_key = bool(args.llm_key or getattr(args, "llm_key_file", None) or getattr(args, "llm_key_env", None))
    if local and subscription:
        die("--local and --subscription are mutually exclusive")
    if subscription:
        if args.api != "anthropic":
            die("--subscription is Anthropic-only; pass --api anthropic")
        if have_key:
            die("--subscription (OAuth) and --llm-key* (key custody) are mutually exclusive")
        egress = sorted(set(egress) | set(EGRESS_DOMAINS["anthropic"]))
    # --allow HOST[:PORT] (implies egress): merge hosts into the allow-list, extract custom ports
    allow_hosts = getattr(args, "allow", None) or []
    allow_ports = {"443", "80"}
    for spec in allow_hosts:
        if ":" in spec and not spec.startswith("["):
            host, p = spec.rsplit(":", 1)
            allow_ports.add(p)
        else:
            host = spec
        egress = list(egress) + [host]
    egress_ports = ",".join(sorted(allow_ports))
    if subscription:
        auth = "subscription"
    elif local:
        auth = "local+custody" if have_key else "local"
    else:
        auth = "custody" if have_key else "passthrough"
    port = getattr(args, "port", None) or DEFAULT_PORT   # --port defaults to None, not absent

    egress_label = args.egress or ("anthropic" if subscription else "none")
    if allow_hosts:
        egress_label = f"{egress_label}+allow:{','.join(allow_hosts)}"
    print(f"aalegate-run (apptainer/HPC): agent={args.agent} | llm={upstream} | api={args.api} | auth={auth} | "
          f"egress={egress_label}")
    print(f"  run_id: {run_id}\n  audit:  {audit_dir}")

    listen_host = "127.0.0.1"
    reach_host = getattr(args, "recorder_host", None) or ("10.0.2.2" if args.netns else "127.0.0.1")
    gateway_bin = str(SCRIPT_DIR / "bin" / "aalegate-gateway")
    egress_bin = str(SCRIPT_DIR / "bin" / "aalegate-egress")

    rec_env = {
        "GATEWAY_LISTEN": f"{listen_host}:{port}", "GATEWAY_UPSTREAM": upstream,
        "GATEWAY_TLS_VERIFY": "1" if verify else "0",
        "GATEWAY_LOG": str(audit_dir / "plane1.jsonl"), "GATEWAY_RUN_ID": run_id,
    }
    kind, keyval = resolve_llm_key(args.llm_key, getattr(args, "llm_key_file", None),
                                    getattr(args, "llm_key_env", None))
    if kind:
        if local:
            hdr, pfx = ("Authorization", "Bearer ")
        elif args.api == "anthropic":
            hdr, pfx = ("x-api-key", "")
        else:
            hdr, pfx = ("Authorization", "Bearer ")
        rec_env["GATEWAY_UPSTREAM_KEY_HEADER"] = hdr
        rec_env["GATEWAY_UPSTREAM_KEY_PREFIX"] = pfx
        if kind == "file":
            rec_env["GATEWAY_UPSTREAM_KEY_FILE"] = keyval
        else:
            rec_env["GATEWAY_UPSTREAM_KEY"] = keyval
    aenv = recorder_env(reach_host, port, anthropic_subscription=subscription)
    for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        aenv[v] = ""
    aenv["NO_PROXY"] = aenv["no_proxy"] = f"{reach_host},127.0.0.1,localhost"

    egr_port = port + 1
    if egress:
        purl = f"http://{reach_host}:{egr_port}"
        for v in ("HTTPS_PROXY", "https_proxy"):
            aenv[v] = purl
        noproxy = f"{reach_host},127.0.0.1,localhost"
        for v in ("NO_PROXY", "no_proxy"):
            aenv[v] = noproxy
        # Node's fetch/https ignore the proxy vars without this (Node 24; probe-node-proxy.py)
        aenv["NODE_USE_ENV_PROXY"] = "1"

    if local:
        aenv["AALE_ANTHROPIC_LOCAL"] = "1"
    if args.shell:
        aenv["AALE_SHELL"] = "1"
    app = ["apptainer", "run", "--contain", "--cleanenv",
           "--workdir", str(run_scratch), "--home", f"{run_home}:{HOME}"]
    if args.netns:
        # Drop NET_ADMIN/NET_RAW for the process INSIDE the container so it can't re-add the
        # default route and undo the airgap (I13). apptainer does its own setup (SIF extraction
        # needs the full bounding set) BEFORE applying this, which is why it must be an apptainer
        # flag, not a setpriv wrapper around apptainer. Removed once the Unix-socket design (no
        # slirp uplink to route to) lands.
        app += ["--drop-caps", "CAP_NET_ADMIN,CAP_NET_RAW"]
    for host, dot in host_dotfiles(getattr(args, "dotfile", None)):
        app += ["--bind", f"{host}:{HOME}/{dot}"]
    if args.gpu == "nvidia":
        app += ["--nv"]
    if work:
        app += ["--pwd", str(work[0][0])]
    for k, v in aenv.items():
        app += ["--env", f"{k}={v}"]
    for p, m in work:
        app += ["--bind", f"{p}:{p}" + (":ro" if m == "ro" else "")]
    for p, m in data:
        app += ["--bind", f"{p}:{p}" + (":ro" if m == "ro" else "")]
    for host, ctr, m in mounts:
        app += ["--bind", f"{host}:{ctr}" + (":ro" if m == "ro" else "")]
    app += [args.agent] + agent_cmd
    if args.netns:
        app = [str(SCRIPT_DIR / "netns-run"), "--"] + app

    if args.dry_run:
        envs = " ".join(f"{k}={v}" for k, v in rec_env.items())
        print(f"# recorder (host process):\n  {envs} {gateway_bin}")
        if egress:
            print(f"# egress proxy:\n  EGRESS_LISTEN=127.0.0.1:{egr_port} "
                  f"EGRESS_ALLOW={','.join(egress)} EGRESS_PORTS={egress_ports} {egress_bin}")
        print("# agent:\n  " + " ".join(app))
        _policy_note(args)
        return 0

    # --- preflight ---
    if not Path(gateway_bin).exists():
        die(f"recorder binary not found: {gateway_bin}")
    if not _have("apptainer"):
        die("apptainer not found on PATH")
    if args.agent.endswith(".sif") and not Path(args.agent).exists():
        die(f"SIF not found: {args.agent}")
    if egress and not Path(egress_bin).exists():
        die(f"egress proxy binary not found: {egress_bin}")

    sif_sha, sig = sif_provenance(args.agent) if Path(args.agent).is_file() else (None, None)
    if sif_sha:
        print(f"agent: {args.agent}  sif_sha256={sif_sha}")
        if sig and sig["status"] == "verified":
            print(f"  SIF verified (signer: {sig['signer']})")
        else:
            st = sig["status"] if sig else "unknown"
            print(f"warning: SIF signature {st}---supply chain not verified", file=sys.stderr)
    _policy_note(args)

    # --- start recorder (host process) ---
    reclog = open(audit_dir / "recorder.log", "w")
    rec = subprocess.Popen([gateway_bin], env={**os.environ, **rec_env}, stdout=reclog, stderr=subprocess.STDOUT)
    egr_proc, egr_log = None, None
    if egress:
        egr_log = open(audit_dir / "egress.log", "w")
        egr_proc = subprocess.Popen(
            [egress_bin], stdout=egr_log, stderr=subprocess.STDOUT,
            env={**os.environ, "EGRESS_LISTEN": f"127.0.0.1:{egr_port}", "EGRESS_ALLOW": ",".join(egress),
                 "EGRESS_PORTS": egress_ports, "EGRESS_LOG": str(audit_dir / "access.log")})
        print(f"egress: {len(egress)} domains allow-listed (127.0.0.1:{egr_port})")

    wait_port(listen_host, port, timeout=10)
    if rec.poll() is not None:
        die(f"recorder exited on start---is 127.0.0.1:{port} already taken?")
    if egr_proc is not None:
        wait_port("127.0.0.1", egr_port, timeout=10)
        if egr_proc.poll() is not None:
            die(f"egress proxy exited on start")

    def agent_fn():
        return subprocess.run(app).returncode

    try:
        rc = run_with_provenance(
            agent_fn, audit_dir=audit_dir, run_id=run_id, runtime="apptainer",
            args=args, upstream=upstream, work=work, write_dirs=write_dirs,
            no_hash=args.no_hash, sif_sha256=sif_sha, signature=sig, netns=args.netns)
    finally:
        rec.terminate()
        try:
            rec.wait(timeout=5)
        except subprocess.TimeoutExpired:
            rec.kill()
        reclog.close()
        if egr_proc is not None:
            egr_proc.terminate()
            try:
                egr_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                egr_proc.kill()
            egr_log.close()
    return rc
