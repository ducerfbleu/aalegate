"""Podman runtime wrapper for aalegate."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from recorder import (
    GATEWAY_IMAGE, EGRESS_IMAGE, EGRESS_DOMAINS, DEFAULT_PORT, SCRIPT_DIR,
    die, ensure_net, net_connect, rm_container, image_exists, image_digest,
    resolve_llm_key, recorder_env, egress_for, gpu_args_podman,
    create_run, parse_work_data, run_with_provenance, host_dotfiles,
)

AIRGAP_NET = "aalegate-airgap"
EGRESS_NET = "aalegate-net"
WAN_NET = "aalegate-wan"
EGRESS_PREFIX = "aalegate-egress"
GATEWAY_PREFIX = "aalegate-gw"

WIRE = {
    "pi": {"model_config": "pi_models_json", "command": ["pi", "--provider", "llamacpp"]},
}


def _podman(*args, check=True, capture=False):
    cmd = ["podman"] + list(args)
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True)
    return subprocess.run(cmd, check=check)


def _host_dns():
    """Extract a usable DNS server for multi-homed containers. Rootless podman with
    aardvark-dns picks the internal network's DNS when multiple networks are given,
    breaking external resolution. Check systemd-resolved's upstream first (skips the
    127.0.0.53 stub), then /etc/resolv.conf, fall back to public DNS."""
    for path in ("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf"):
        try:
            with open(path) as f:
                for line in f:
                    if line.strip().startswith("nameserver"):
                        ip = line.split()[1]
                        if not ip.startswith("127."):
                            return ip
        except OSError:
            continue
    return "1.1.1.1"


def _container_ip(name, network):
    r = _podman("inspect", name, "--format", "{{json .NetworkSettings.Networks}}", capture=True)
    if r.returncode != 0:
        return None
    nets = json.loads(r.stdout)
    return nets.get(network, {}).get("IPAddress")


def _check_wire_deps(image):
    probe = ('m=; for t in jq curl bash; do command -v "$t" >/dev/null 2>&1 || m="$m $t"; done; printf %s "$m"')
    r = subprocess.run(["podman", "run", "--rm", "--entrypoint", "/bin/sh", image, "-c", probe],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return ["<no /bin/sh---need bash, jq, curl>"]
    return r.stdout.split()


def _gateway_upstream(url):
    """Rewrite loopback to host.containers.internal for the containerized recorder
    (mirrors docker.py; podman auto-maps host.containers.internal -> the host). Keeps the
    scheme, PORT (from the URL, not hardwired), and path --- only a loopback host is swapped."""
    parts = urlsplit(url)
    if parts.hostname in ("127.0.0.1", "localhost", "0.0.0.0"):
        netloc = "host.containers.internal" + (f":{parts.port}" if parts.port else "")
        return urlunsplit(parts._replace(netloc=netloc))
    return url


def run(args):
    agent_cmd = list(args.cmd)
    if agent_cmd and agent_cmd[0] == "--":
        agent_cmd = agent_cmd[1:]
    if args.prompt:
        agent_cmd += ["-p", args.prompt]

    run_id, audit_dir, run_home = create_run(args.audit_root, args.run_id, getattr(args, "project", None))
    gw_container = f"{GATEWAY_PREFIX}-{run_id}"
    egress_container = f"{EGRESS_PREFIX}-{run_id}"
    work, data, write_dirs = parse_work_data(args)
    upstream = _gateway_upstream(args.llm)
    verify = args.llm_verify
    egress = egress_for(args.egress)
    allow_hosts = getattr(args, "allow", None) or []
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
    use_proxy = bool(egress or allow_hosts)
    if subscription:
        auth = "subscription"
    elif local:
        auth = "local+custody" if have_key else "local"
    else:
        auth = "custody" if have_key else "passthrough"
    gw = args.recorder_name
    port = DEFAULT_PORT
    llm_leg = getattr(args, "llm_net", None)

    egress_label = args.egress or ("anthropic" if subscription else "none")
    if allow_hosts:
        egress_label = f"{egress_label}+allow:{','.join(allow_hosts)}"
    print(f"aalegate-run (podman): agent={args.agent} | llm={upstream} | api={args.api} | auth={auth} | "
          f"egress={egress_label}")
    print(f"  run_id: {run_id}\n  audit:  {audit_dir}")

    # --- gateway ---
    gw_env = {
        "GATEWAY_LISTEN": f":{port}", "GATEWAY_UPSTREAM": upstream,
        "GATEWAY_TLS_VERIFY": "1" if verify else "0",
        "GATEWAY_LOG": "/logs/plane1.jsonl", "GATEWAY_RUN_ID": run_id,
    }
    dns = _host_dns()
    gw_cmd = ["podman", "run", "-d", "--name", gw_container,
              "--network", AIRGAP_NET,
              "-v", f"{audit_dir}:/logs"]
    kind, keyval = resolve_llm_key(args.llm_key, getattr(args, "llm_key_file", None),
                                    getattr(args, "llm_key_env", None))
    if kind:
        if local:
            hdr, pfx = ("Authorization", "Bearer ")
        elif args.api == "anthropic":
            hdr, pfx = ("x-api-key", "")
        else:
            hdr, pfx = ("Authorization", "Bearer ")
        gw_env["GATEWAY_UPSTREAM_KEY_HEADER"] = hdr
        gw_env["GATEWAY_UPSTREAM_KEY_PREFIX"] = pfx
        if kind == "file":
            gw_cmd += ["-v", f"{keyval}:/run/secrets/llm_key:ro"]
            gw_env["GATEWAY_UPSTREAM_KEY_FILE"] = "/run/secrets/llm_key"
        else:
            gw_env["GATEWAY_UPSTREAM_KEY"] = keyval
    for k, v in gw_env.items():
        gw_cmd += ["-e", f"{k}={v}"]
    gw_cmd.append(GATEWAY_IMAGE)

    # --- agent ---
    aenv = recorder_env(gw, port, anthropic_subscription=subscription)
    agent_name = f"aalegate-agent-{run_id}"
    create = ["podman", "create", "--rm", "-i", "--name", agent_name, "--network", AIRGAP_NET,
              "--userns=keep-id"]
    if sys.stdin.isatty():
        create.append("-t")
    if args.as_root:
        create += ["--user", "0:0"]
    create += ["-v", f"{run_home}:/home/user", "-e", "HOME=/home/user"]
    for host, dot in host_dotfiles(getattr(args, "dotfile", None)):
        create += ["-v", f"{host}:/home/user/{dot}"]
    for k, v in aenv.items():
        create += ["-e", f"{k}={v}"]
    if use_proxy:
        ensure_net(EGRESS_NET, internal=True, cmd="podman")

    # runtime-inject wiring
    ep_src = manifest_src = None
    if args.wire or args.manifest:
        ep_src = SCRIPT_DIR / "harnesses" / "entrypoint.sh"
        if not ep_src.is_file():
            die(f"runtime wiring: shared entrypoint not found at {ep_src}")
        if args.manifest:
            manifest_src = Path(args.manifest).expanduser().resolve()
            if not manifest_src.is_file():
                die(f"--manifest not found: {manifest_src}")
        else:
            if args.wire not in WIRE:
                die(f"unknown --wire profile '{args.wire}' (have: {', '.join(WIRE)})")
            manifest_src = audit_dir / "harness.json"
        create += ["-v", f"{ep_src}:/aalegate/entrypoint:ro",
                   "-v", f"{manifest_src}:/aalegate/harness.json:ro",
                   "-e", "AALE_MANIFEST=/aalegate/harness.json"]

    if local:
        create += ["-e", "AALE_ANTHROPIC_LOCAL=1"]
    if args.shell:
        create += ["-e", "AALE_SHELL=1"]
    for p, m in work:
        create += ["-v", f"{p}:{p}" + (":ro" if m == "ro" else "")]
    for p, m in data:
        create += ["-v", f"{p}:{p}" + (":ro" if m == "ro" else "")]
    for spec in (args.mount or []):
        parts = spec.split(":")
        if len(parts) < 2:
            die(f"--mount must be HOST:CONTAINER[:ro], got '{spec}'")
        parts[0] = str(Path(parts[0]).expanduser().resolve())
        create += ["-v", ":".join(parts)]
    if args.gpu:
        ga, ge = gpu_args_podman(args.gpu)
        create += ga + ge
    create += ["-w", str(work[0][0]) if work else "/workspace"]

    # image + command are appended AFTER proxy env vars (below), since podman
    # treats everything after the image name as the container command.
    create_tail = [args.agent]
    uses_our_ep = getattr(args, "catalogued", False) or bool(args.wire or args.manifest)
    if args.shell and not uses_our_ep:
        import shlex
        create_tail += ["bash"]
        if agent_cmd:
            print("  --shell: in the shell, launch the agent with:  "
                  + " ".join(shlex.quote(c) for c in agent_cmd))
    else:
        if ep_src:
            create_tail += ["/aalegate/entrypoint"]
        if agent_cmd:
            create_tail += agent_cmd

    if args.dry_run:
        import shlex
        if use_proxy:
            create += ["-e", "HTTP_PROXY=http://<egress-ip>:<port>",
                       "-e", "HTTPS_PROXY=http://<egress-ip>:<port>",
                       "-e", "http_proxy=http://<egress-ip>:<port>",
                       "-e", "https_proxy=http://<egress-ip>:<port>",
                       "-e", "NO_PROXY=<gw-ip>,127.0.0.1,localhost",
                       "-e", "no_proxy=<gw-ip>,127.0.0.1,localhost"]
        create += ["--add-host", f"{gw}:<gw-ip>"]
        create += create_tail
        print("# recorder:\n  %s" % " ".join(shlex.quote(c) for c in gw_cmd))
        if use_proxy:
            total = len(list(egress or []) + allow_hosts)
            print(f"# egress: podman run -d --name {egress_container} --network {EGRESS_NET} ({total} entries)")
        print("# agent:\n  " + " ".join(shlex.quote(c) for c in create))
        return 0

    # --- preflight ---
    if args.wire and not args.manifest:
        prof = WIRE[args.wire]
        (audit_dir / "harness.json").write_text(json.dumps(
            {"model_config": prof["model_config"], "command": prof["command"]}, indent=2))
    if not image_exists(args.agent, cmd="podman"):
        die(f"agent image '{args.agent}' not found")
    if args.wire or args.manifest:
        missing = _check_wire_deps(args.agent)
        if missing:
            die(f"runtime wiring needs jq+curl+bash---'{args.agent}' is missing:{' '.join(missing)}")
    if not image_exists(GATEWAY_IMAGE, cmd="podman"):
        die(f"recorder image '{GATEWAY_IMAGE}' not found---run ./build-aalegate.sh")

    img_id, repo = image_digest(args.agent, cmd="podman")
    print(f"agent: {args.agent}  digest={img_id}")

    # --- start recorder ---
    ensure_net(AIRGAP_NET, internal=True, cmd="podman")
    ensure_net(WAN_NET, internal=False, cmd="podman")
    rm_container(gw_container, cmd="podman")
    print(f"debug: recorder cmd: {' '.join(gw_cmd)}", file=sys.stderr)
    cp = subprocess.run(gw_cmd, capture_output=True, text=True)
    if cp.returncode != 0:
        die(f"recorder failed (exit {cp.returncode}): {cp.stderr.strip()}")
    print(f"debug: recorder created on {AIRGAP_NET}", file=sys.stderr)
    if llm_leg:
        net_connect(llm_leg, gw_container, cmd="podman")
        print(f"debug: recorder connected to llm_leg={llm_leg}", file=sys.stderr)
    net_connect(WAN_NET, gw_container, cmd="podman")
    print(f"debug: recorder connected to {WAN_NET}", file=sys.stderr)

    gw_ip = None
    for _ in range(20):
        gw_ip = _container_ip(gw_container, AIRGAP_NET)
        if gw_ip:
            break
        time.sleep(0.3)
    if not gw_ip:
        die(f"recorder has no IP on {AIRGAP_NET}")

    # dump recorder's full network state
    r = _podman("inspect", gw_container, "--format", "{{json .NetworkSettings.Networks}}", capture=True)
    print(f"debug: recorder networks: {r.stdout.strip()}", file=sys.stderr)

    time.sleep(0.5)
    r = _podman("inspect", gw_container, "--format", "{{.State.Running}}", capture=True)
    if r.stdout.strip() != "true":
        r = _podman("logs", gw_container, capture=True)
        die(f"recorder exited---logs:\n{r.stdout}\n{r.stderr}")
    print(f"recorder: {gw_container} at {gw_ip}")

    # --- egress proxy ---
    if use_proxy:
        if not image_exists(EGRESS_IMAGE, cmd="podman"):
            die(f"egress image '{EGRESS_IMAGE}' not found---run ./build-aalegate.sh")
        rm_container(egress_container, cmd="podman")
        egr_port = port + 1
        # merge --allow hosts into the domain/IP list; extract custom ports
        all_domains = list(egress or [])
        allow_ports = {"443", "80"}
        for spec in allow_hosts:
            if ":" in spec and not spec.startswith("["):
                host, p = spec.rsplit(":", 1)
                allow_ports.add(p)
            else:
                host = spec
            all_domains.append(host)
        ensure_net(EGRESS_NET, internal=True, cmd="podman")
        egress_cmd = [
            "podman", "run", "-d", "--name", egress_container,
            "--network", f"{WAN_NET},{EGRESS_NET}", "--dns", dns,
            "-e", f"EGRESS_LISTEN=:{egr_port}", "-e", f"EGRESS_ALLOW={','.join(all_domains)}",
            "-e", f"EGRESS_PORTS={','.join(sorted(allow_ports))}",
            "-e", "EGRESS_LOG=/logs/access.log",
            "-v", f"{audit_dir}:/logs", EGRESS_IMAGE,
        ]
        print(f"debug: egress cmd: {' '.join(egress_cmd)}", file=sys.stderr)
        subprocess.run(egress_cmd, check=True, capture_output=True)
        print(f"debug: egress created on {WAN_NET},{EGRESS_NET}", file=sys.stderr)

        # dump egress proxy's full network state + resolv.conf
        r = _podman("inspect", egress_container, "--format", "{{json .NetworkSettings.Networks}}", capture=True)
        print(f"debug: egress networks: {r.stdout.strip()}", file=sys.stderr)
        r = _podman("inspect", egress_container, "--format",
                     "{{.ResolvConfPath}}", capture=True)
        print(f"debug: egress resolv.conf path: {r.stdout.strip()}", file=sys.stderr)

        egr_ip = None
        for _ in range(20):
            egr_ip = _container_ip(egress_container, EGRESS_NET)
            if egr_ip:
                break
            time.sleep(0.3)
        if not egr_ip:
            # diagnostic: dump the container's network state
            r = _podman("inspect", egress_container, "--format",
                        "{{json .NetworkSettings.Networks}}", capture=True)
            die(f"egress proxy has no IP on {EGRESS_NET} after 6s. "
                f"Networks: {r.stdout.strip() if r.returncode == 0 else r.stderr.strip()}")
        purl = f"http://{egr_ip}:{egr_port}"
        for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            create += ["-e", f"{v}={purl}"]
        noproxy = f"{gw_ip},{gw},127.0.0.1,localhost"
        for v in ("NO_PROXY", "no_proxy"):
            create += ["-e", f"{v}={noproxy}"]
        # Node's fetch/https ignore the proxy vars without this (Node 24; probe-node-proxy.py)
        create += ["-e", "NODE_USE_ENV_PROXY=1"]
        total = len(all_domains)
        print(f"egress: {egress_container} at {egr_ip}:{egr_port} ({total} entries allowlisted)")

    # inject recorder IP via --add-host, then append image + command
    create += ["--add-host", f"{gw}:{gw_ip}"]
    create += create_tail

    # --- run agent ---
    print(f"  shell in: podman exec -it {agent_name} bash")
    cp = subprocess.run(create, capture_output=True, text=True)
    if cp.returncode != 0:
        rm_container(gw_container, cmd="podman")   # don't leave the recorder orphaned
        if use_proxy:
            rm_container(egress_container, cmd="podman")
        die(f"podman create failed (exit {cp.returncode}): {cp.stderr.strip()}\n"
            f"  (a common cause: a --mount HOST path that doesn't exist --- rootless podman "
            f"can't create it under a root-owned dir like /opt)\n"
            f"  cmd: {' '.join(create)}")
    cid = cp.stdout.strip()
    if use_proxy:
        net_connect(EGRESS_NET, cid, cmd="podman")
        print(f"debug: agent connected to {EGRESS_NET}", file=sys.stderr)

    def agent_fn():
        return subprocess.run(["podman", "start", "-a", "-i", cid]).returncode

    try:
        rc = run_with_provenance(
            agent_fn, audit_dir=audit_dir, run_id=run_id, runtime="podman",
            args=args, upstream=upstream, work=work, write_dirs=write_dirs,
            no_hash=args.no_hash, agent_image_digest=img_id, agent_repo_digest=repo)
    finally:
        rm_container(gw_container, cmd="podman")
        if use_proxy:
            rm_container(egress_container, cmd="podman")
    return rc
