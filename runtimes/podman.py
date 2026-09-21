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
    create_run, parse_work_data, run_with_provenance,
)

HOME = Path.home()

AIRGAP_NET = "aalegate-airgap"
EGRESS_NET = "aalegate-net"
EGRESS_NAME = "aalegate-egress-proxy"
GATEWAY_NAME = "aalegate-gw"

WIRE = {
    "pi": {"model_config": "pi_models_json", "command": ["pi", "--provider", "llamacpp"]},
}


def _podman(*args, check=True, capture=False):
    cmd = ["podman"] + list(args)
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True)
    return subprocess.run(cmd, check=check)


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
    work, data, write_dirs = parse_work_data(args)
    upstream = _gateway_upstream(args.llm)
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
    if subscription:
        auth = "subscription"
    elif local:
        auth = "local+custody" if have_key else "local"
    else:
        auth = "custody" if have_key else "passthrough"
    gw = args.recorder_name
    port = DEFAULT_PORT
    llm_leg = getattr(args, "llm_net", None)

    print(f"aalegate-run (podman): agent={args.agent} | llm={upstream} | api={args.api} | auth={auth} | "
          f"egress={args.egress or ('anthropic' if subscription else 'none')}")
    print(f"  run_id: {run_id}\n  audit:  {audit_dir}")

    # --- gateway ---
    gw_env = {
        "GATEWAY_LISTEN": f":{port}", "GATEWAY_UPSTREAM": upstream,
        "GATEWAY_TLS_VERIFY": "1" if verify else "0",
        "GATEWAY_LOG": "/logs/plane1.jsonl", "GATEWAY_RUN_ID": run_id,
    }
    gw_cmd = ["podman", "run", "-d", "--name", GATEWAY_NAME, "--network", AIRGAP_NET,
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
    agent_name = f"aalegate-agent-{run_id[:12]}"
    create = ["podman", "create", "--rm", "-i", "--name", agent_name, "--network", AIRGAP_NET,
              "--userns=keep-id"]
    if sys.stdin.isatty():
        create.append("-t")
    if args.as_root:
        create += ["--user", "0:0"]
    create += ["-v", f"{run_home}:/home/user", "-e", "HOME=/home/user"]
    if getattr(args, "claude_state", False):
        host_claude = HOME / ".claude"
        host_claude.mkdir(parents=True, exist_ok=True)
        create += ["-v", f"{host_claude}:/home/user/.claude"]
    for k, v in aenv.items():
        create += ["-e", f"{k}={v}"]
    if egress:
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
    create += ["-w", str(work[0][0]) if work else "/workspace", args.agent]

    uses_our_ep = getattr(args, "catalogued", False) or bool(args.wire or args.manifest)
    if args.shell and not uses_our_ep:
        import shlex
        create += ["bash"]
        if agent_cmd:
            print("  --shell: in the shell, launch the agent with:  "
                  + " ".join(shlex.quote(c) for c in agent_cmd))
    else:
        if ep_src:
            create += ["/aalegate/entrypoint"]
        if agent_cmd:
            create += agent_cmd

    if args.dry_run:
        import shlex
        print("# recorder:\n  %s" % " ".join(shlex.quote(c) for c in gw_cmd))
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
        die(f"recorder image '{GATEWAY_IMAGE}' not found---run ./build.sh")

    img_id, repo = image_digest(args.agent, cmd="podman")
    print(f"agent: {args.agent}  digest={img_id}")

    # --- start recorder ---
    ensure_net(AIRGAP_NET, internal=True, cmd="podman")
    rm_container(GATEWAY_NAME, cmd="podman")
    subprocess.run(gw_cmd, check=True, capture_output=True)
    if llm_leg:
        net_connect(llm_leg, GATEWAY_NAME, cmd="podman")
    net_connect("podman", GATEWAY_NAME, cmd="podman")

    gw_ip = None
    for _ in range(20):
        gw_ip = _container_ip(GATEWAY_NAME, AIRGAP_NET)
        if gw_ip:
            break
        time.sleep(0.3)
    if not gw_ip:
        die(f"recorder has no IP on {AIRGAP_NET}")

    time.sleep(0.5)
    r = _podman("inspect", GATEWAY_NAME, "--format", "{{.State.Running}}", capture=True)
    if r.stdout.strip() != "true":
        r = _podman("logs", GATEWAY_NAME, capture=True)
        die(f"recorder exited---logs:\n{r.stdout}\n{r.stderr}")
    print(f"recorder: {GATEWAY_NAME} at {gw_ip}")

    # --- egress proxy ---
    if egress:
        if not image_exists(EGRESS_IMAGE, cmd="podman"):
            die(f"egress image '{EGRESS_IMAGE}' not found---run ./build.sh")
        rm_container(EGRESS_NAME, cmd="podman")
        egr_port = port + 1
        subprocess.run([
            "podman", "run", "-d", "--name", EGRESS_NAME, "--network", EGRESS_NET,
            "-e", f"EGRESS_LISTEN=:{egr_port}", "-e", f"EGRESS_ALLOW={','.join(egress)}",
            "-e", "EGRESS_PORTS=443,80", "-e", "EGRESS_LOG=/logs/access.log",
            "-v", f"{audit_dir}:/logs", EGRESS_IMAGE,
        ], check=True, capture_output=True)
        net_connect("podman", EGRESS_NAME, cmd="podman")
        egr_ip = None
        for _ in range(20):
            egr_ip = _container_ip(EGRESS_NAME, EGRESS_NET)
            if egr_ip:
                break
            time.sleep(0.3)
        if egr_ip:
            purl = f"http://{egr_ip}:{egr_port}"
            for v in ("HTTPS_PROXY", "https_proxy"):
                create += ["-e", f"{v}={purl}"]
            noproxy = f"{gw_ip},127.0.0.1,localhost"
            for v in ("NO_PROXY", "no_proxy"):
                create += ["-e", f"{v}={noproxy}"]
            print(f"egress: {EGRESS_NAME} at {egr_ip} ({len(egress)} domains)")

    # inject recorder IP via --add-host
    img_idx = create.index(args.agent)
    create.insert(img_idx, f"{gw}:{gw_ip}")
    create.insert(img_idx, "--add-host")

    # --- run agent ---
    print(f"  shell in: podman exec -it {agent_name} bash")
    cp = subprocess.run(create, capture_output=True, text=True)
    if cp.returncode != 0:
        rm_container(GATEWAY_NAME, cmd="podman")   # don't leave the recorder orphaned
        if egress:
            rm_container(EGRESS_NAME, cmd="podman")
        die(f"podman create failed (exit {cp.returncode}): {cp.stderr.strip()}\n"
            f"  (a common cause: a --mount HOST path that doesn't exist --- rootless podman "
            f"can't create it under a root-owned dir like /opt)\n"
            f"  cmd: {' '.join(create)}")
    cid = cp.stdout.strip()
    if egress:
        net_connect(EGRESS_NET, cid, cmd="podman")

    def agent_fn():
        return subprocess.run(["podman", "start", "-a", "-i", cid]).returncode

    try:
        rc = run_with_provenance(
            agent_fn, audit_dir=audit_dir, run_id=run_id, runtime="podman",
            args=args, upstream=upstream, work=work, write_dirs=write_dirs,
            no_hash=args.no_hash, agent_image_digest=img_id, agent_repo_digest=repo)
    finally:
        rm_container(GATEWAY_NAME, cmd="podman")
        if egress:
            rm_container(EGRESS_NAME, cmd="podman")
    return rc
