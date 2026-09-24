# aalegate-gateway: the recorder (plane 1)

An L7 reverse proxy between the agent and the LLM. The agent speaks plain HTTP to it; it forwards
to the real endpoint and appends a **hash-chained JSONL record** of every exchange: prompts,
completions, reasoning, tool calls, token usage and timing (TTFT, decode time, backend `timings`).
Go standard library only; built `CGO_ENABLED=0` into a static binary (`FROM scratch` image, or
`bin/aalegate-gateway` as a host process on apptainer).

Logging never blocks forwarding: bodies stream on to the agent while a bounded copy is logged,
and parsing happens after the stream ends.

## Configuration (set by `aalegate-run`)

| variable | meaning | default |
|---|---|---|
| `GATEWAY_LISTEN` | bind address | `:8080` |
| `GATEWAY_UPSTREAM` | real LLM base URL | required |
| `GATEWAY_TLS_VERIFY` | `1` verifies the upstream certificate (`--llm-verify`) | skip |
| `GATEWAY_CA` | PEM CA bundle for verification | system roots |
| `GATEWAY_UPSTREAM_PROXY` | HTTP proxy for the upstream leg | none |
| `GATEWAY_UPSTREAM_KEY` / `_KEY_FILE` | real upstream key (key custody) | none |
| `GATEWAY_UPSTREAM_KEY_HEADER` / `_KEY_PREFIX` | where the key goes (`x-api-key`, `Authorization: Bearer`) | — |
| `GATEWAY_LOG` | JSONL log path (appended) | required |
| `GATEWAY_RUN_ID` | run id written into every record | `unknown` |
| `GATEWAY_MAX_CAPTURE` | max bytes **logged** per body (forwarding is never truncated) | 8 MiB |

## Key custody

The recorder holds the real upstream key; the agent only ever holds a dummy (`aalegate`). The
recorder strips whatever auth the agent sent and injects the real key. Works on every runtime.

| flag | how the key reaches the recorder | visible in `docker inspect` / `ps`? |
|---|---|---|
| `--llm-key-file PATH` | file, mounted read-only (recommended) | no |
| `--llm-key-env VAR` | read by `aalegate-run` from your environment | no |
| `--llm-key VALUE` | on the command line (warns) | yes |

**Subscription (`--subscription`, Claude Code)** turns custody off: there is no separable key.
Claude Code logs in itself and the recorder forwards its OAuth bearer unchanged. Inference
(`/v1/messages`) is still recorded.

## Hash chain

Each record carries `prev_hash` (the previous record's hash, `""` for the first) and
`record_hash = sha256(prev_hash || canonical(record))`. Editing or deleting any record breaks the
chain. Verify with the same binary:

```bash
aalegate-gateway -verify "$RUN/plane1.jsonl"
docker run --rm -v "$RUN:/logs:ro" aalegate-gateway -verify /logs/plane1.jsonl
```

## Tests

`smoke-test.sh` runs the gateway against `mock_llm.py` (a stdlib mock LLM).
