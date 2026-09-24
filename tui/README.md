# Reading the logs

Every run writes to `~/.local/state/aalegate/<project>__<hash>/<run_id>/` (override with
`$AALE_AUDIT_ROOT` or `--audit-root`):

| file | plane | contents |
|---|---|---|
| `plane1.jsonl` | LLM | every exchange, hash-chained (see `gateway/README.md`) |
| `access.log` | internet | egress allow/deny + tunnel byte volume (see `egress/README.md`) |
| `plane3.json` | filesystem | content-addressed before/after manifest of the work dirs |
| `index.json` | — | run provenance: runtime, agent + image digest / SIF hash, LLM, egress, work dirs, exit code, chain head |

## aalegate-tui

An interactive, read-only reader. Go standard library plus `golang.org/x/term` and `x/text`.

```bash
aalegate-tui                 # pick a run
aalegate-tui -run RUN_ID     # or -file path/to/plane1.jsonl, -pick, -root DIR
```

- `↑`/`↓` move through turns; `enter` opens a turn's blocks (prompt, reasoning, completion,
  tool calls, request body).
- `c` copies a block (OSC 52: works over SSH and tmux); `s` saves it to `$AALE_AUDIT_ROOT/clips/`.
- `tab` shows stats. Markdown and JSON are highlighted.
- `-theme nocturnal|dracula|gruvbox|nord|solarized|ansi` (or `$AALE_TUI_THEME`).
- `AALE_TUI=rich` hands off to an optional `aalegate-tui-rich` binary (bubbletea) if one is on
  `PATH`; it is not built from this repo.

## Other readers

```bash
aalegate-log  "$RUN/plane1.jsonl"   # show-log.py: records + token/throughput totals (tk/s)
aalegate-access [RUN_ID]            # show-access.py: egress log; --pick, --project, --summary
cat "$RUN/index.json"               # provenance
aalegate-gateway -verify "$RUN/plane1.jsonl"   # hash-chain integrity
```

`RUN=$(ls -td ~/.local/state/aalegate/*/*/ | head -1)` is the latest run.
