# aalegate-egress: the egress proxy (plane 2)

A forward + CONNECT proxy that lets the agent reach **only allowlisted hosts** and refuses (and
logs) everything else. It is the agent's single controlled route to the internet. Go standard
library only: a `FROM scratch` container on docker/podman/kata, `bin/aalegate-egress` as a host
process on apptainer. It exists only when a run asks for web access.

## Turning it on

| flag | effect |
|---|---|
| `--egress GROUPS` | allow named groups, comma-separated, or `all` |
| `--allow HOST[:PORT]` | allow one host (and its subdomains); repeatable; a custom port is added to the allowed ports |
| `--subscription` | adds the `anthropic` group (Claude Code's OAuth login and side calls) |

Groups (`EGRESS_DOMAINS` in `recorder.py`):

| group | hosts |
|---|---|
| `gh` | github.com, api.github.com, objects/raw.githubusercontent.com |
| `hf` | huggingface.co, hf.co |
| `pypi` | pypi.org, files.pythonhosted.org |
| `go` | proxy.golang.org, sum.golang.org, storage.googleapis.com, golang.org |
| `anthropic` | api.anthropic.com, claude.ai, claude.com, platform/downloads/code.claude.com, mcp-proxy.anthropic.com |

The agent gets `HTTPS_PROXY` / `NO_PROXY` pointing at the proxy (the recorder stays direct), plus
`NODE_USE_ENV_PROXY=1`, without which Node's `fetch`/`https` ignore the proxy.
`probe-node-proxy.py` (repo root) checks all of this from inside an agent.

The proxy variables are advisory: a tool can ignore them. What enforces the allowlist is the
agent's network isolation (internal networks on docker/podman, the netns airgap on apptainer),
which leaves the proxy as the only way out.

## Configuration (set by `aalegate-run`)

| variable | meaning | default |
|---|---|---|
| `EGRESS_LISTEN` | bind address | `:3128` |
| `EGRESS_ALLOW` | allowed domains/IPs (exact, or any subdomain) | none |
| `EGRESS_PORTS` | allowed ports | `443,80` |
| `EGRESS_LOG` | access log path (appended) | stderr |
| `EGRESS_MAX_BODY` | max body of a forwarded plain-HTTP request | 64 MiB |

## Access log

One line per attempt, plus byte volume when a tunnel closes (volume only, never content):

```
<ts> <client> ALLOW|DENY|FAIL <host:port>
<ts> <client> CLOSE <host:port> sent=<bytes> recv=<bytes> dur=<ms>
```

Read it with `aalegate-access` (`show-access.py`): per run, a project picker, or `--summary`.
Logs from the retired squid proxy are still readable.

## Tests

`test_parse.py` checks the log parser against Go-proxy and legacy squid lines; `test_server.go`
is a streaming test server.
