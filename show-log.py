#!/usr/bin/env python3
"""show-log.py---dump the raw plane-1 provenance records of a aalegate run.

Usage:
  ./show-log.py [RUN_ID]               # a run; on a TTY with no args, an interactive project picker
  ./show-log.py --pick                 # picker: up to 10 recent projects, type to fuzzy-filter
  ./show-log.py --project DIR|NAME     # latest run of a project (a path, or a fuzzy handle/name)
  ./show-log.py -y [RUN_ID]            # ...and skips the "use this tokenizer?" confirmation
  ./show-log.py --root DIR RUN_ID
  ./show-log.py --tokenizer PATH RUN_ID        # force a specific local tokenizer.json (file or dir)
  ./show-log.py --tokenize[-url URL] RUN_ID    # exact counts via a running backend's /tokenize

Prints index.json + each plane-1 record (endpoint, status, message structure, token
usage, content hashes, and the prev/record hash chain), plus a per-run token totals footer.
Token usage is normalized to input (fresh, non-cached) / output, with output split into
reasoning (the model's chain-of-thought) + writing (answer + tool-call args). llama.cpp DOES
stream the reasoning back---as a separate `reasoning_content` field, which the recorder captures
---but it reports no reasoning-token COUNT (completion_tokens lumps thinking + writing). To split
it EXACTLY we tokenize the captured thinking with the model's OWN tokenizer: by default show-log
auto-matches a tokenizer.json under ./tokenizers/ to the run's model name and, once confirmed
(or with -y), counts offline. Force one with --tokenizer PATH, or use a running backend's
/tokenize via --tokenize / --tokenize-url URL. If nothing matches, you decline, or the backend
is unreachable, the split falls back to a char-ratio estimate of the captured text, marked '~'.
Prefill/decode tk/s come from the backend `timings` (exact) or the gateway phase timestamps
(`ttft_ms`/`decode_ms`), and read N/A for runs recorded before timing capture. Stdlib only.
"""
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))  # so `import bpe_tokenizer` works anywhere


def _projects(root):
    """(handle_dir, run_count, last_mtime) per project handle with >=1 run, newest first.
    A handle dir holds run children (<handle>/<run_id>/index.json); legacy flat runs
    (index.json directly under root) have no such children and are skipped here."""
    out = []
    if not root.is_dir():
        return out
    for h in root.iterdir():
        if not h.is_dir():
            continue
        runs = list(h.glob("*/index.json"))
        if runs:
            out.append((h, len(runs), max(p.stat().st_mtime for p in runs)))
    out.sort(key=lambda t: t[2], reverse=True)
    return out


def _fuzzy(query, name):
    """Match score of `name` against `query` (case-insensitive); higher is better,
    None = no match. Substring beats subsequence; an earlier hit scores higher."""
    q, n = query.lower().strip(), name.lower()
    if not q:
        return 0
    i = n.find(q)
    if i >= 0:
        return 1000 - i
    it = iter(n)                                   # subsequence: chars appear in order
    return 100 if all(c in it for c in q) else None


def _latest_run(scope):
    runs = sorted(scope.glob("*/index.json"), key=lambda p: p.stat().st_mtime)
    if not runs:
        sys.exit(f"no runs under {scope}")
    return runs[-1].parent


def _resolve_project(root, value):
    """Resolve a --project value to a single handle dir, or None (0 or >1 matches, so
    the caller can prompt). A value that is an existing dir maps through its realpath
    handle; otherwise it's a fuzzy handle/name query."""
    p = Path(value).expanduser()
    if p.is_dir():
        from recorder import project_handle          # after sys.path insert (see top)
        h = root / project_handle(str(p))
        return h if h.is_dir() else None
    hits = [h for (h, _c, _m) in _projects(root) if _fuzzy(value, h.name) is not None]
    return hits[0] if len(hits) == 1 else None


def pick_project(root, initial=""):
    """Interactive picker: lists up to 10 recent projects; a number selects, text
    (fuzzy) re-filters, Enter takes the top (most recent), q quits. Prompts go to
    stderr so stdout stays a clean log dump. Returns the chosen handle dir."""
    projs = _projects(root)
    if not projs:
        sys.exit(f"no projects under {root}")
    query = initial
    while True:
        ranked = [(s, h, c, m) for (h, c, m) in projs
                  for s in (_fuzzy(query, h.name),) if s is not None]
        ranked.sort(key=lambda t: (t[0], t[3]), reverse=True)
        shown = ranked[:10]
        print(f"\nprojects under {root}" + (f"  filter={query!r}" if query else ""),
              file=sys.stderr)
        for i, (_s, h, c, m) in enumerate(shown, 1):
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m))
            print(f"  {i:>2}  {h.name:<34} {c:>3} run{' ' if c == 1 else 's'}  {ts}",
                  file=sys.stderr)
        if not shown:
            print("  (no match)", file=sys.stderr)
        print("select #, type to filter, Enter=1, q=quit: ", end="", file=sys.stderr, flush=True)
        try:
            ans = input().strip()
        except EOFError:
            sys.exit(0)
        if ans in ("q", "Q"):
            sys.exit(0)
        if ans == "":
            if shown:
                return shown[0][1]
            continue
        if ans.isdigit():
            k = int(ans)
            if 1 <= k <= len(shown):
                return shown[k - 1][1]
            print(f"  # out of range (1-{len(shown)})", file=sys.stderr)
            continue
        query = ans


def resolve(argv):
    from recorder import DEFAULT_AUDIT_ROOT       # single source (honors $AALGT_AUDIT_ROOT)
    root = DEFAULT_AUDIT_ROOT
    run = project = None
    pick = False
    it = iter(argv)
    for a in it:
        if a == "--root":
            root = Path(next(it))
        elif a == "--project":
            project = next(it, "")
        elif a in ("--pick", "-i"):
            pick = True
        else:
            run = a
    # an explicit run id/path wins over any project selection.
    if run:
        # accept an absolute/relative path, a '<handle>/<run_id>', or a bare run_id
        # nested one level under its project handle.
        for cand in (root / run, *sorted(root.glob(f"*/{run}"))):
            if (cand / "index.json").exists() or cand.is_dir():
                return cand
        sys.exit(f"no such run: {run}")
    if pick:
        return _latest_run(pick_project(root, project or ""))
    if project is not None:
        scope = _resolve_project(root, project)
        return _latest_run(scope) if scope else _latest_run(pick_project(root, project))
    # no run, no project: prompt on an interactive terminal, else pick global-latest
    # (keeps `show-log.py | less` and scripts working without blocking on input).
    if sys.stdin.isatty() and sys.stderr.isatty():
        return _latest_run(pick_project(root, ""))
    runs = list(root.glob("*/*/index.json")) + list(root.glob("*/index.json"))
    runs = sorted(runs, key=lambda p: p.stat().st_mtime)
    if not runs:
        sys.exit(f"no runs under {root}")
    return runs[-1].parent


# NOTE: usage-field semantics are BACKEND-SPECIFIC---this handles llama.cpp only (for now).
# llama.cpp: prompt_tokens is cache-INCLUSIVE, cached at prompt_tokens_details.cached_tokens. It
# streams the thinking as `reasoning_content` (captured into the record's `reasoning` field) but
# reports no reasoning-token COUNT---completion_tokens is the undifferentiated total. Splitting it
# needs a tokenizer, and the right one is the model's own: we reuse the backend's /tokenize
# endpoint (see make_tokenizer) when --tokenize is passed, else fall back to a char-ratio estimate
# of the captured text below. Other backends need their own mapping: OpenAI adds
# completion_tokens_details.reasoning_tokens (use it directly, no estimate/tokenizer); Anthropic
# reports input_tokens (already cache-EXCLUDED) + output_tokens + cache_read/creation_input_tokens.
# Branch here on the recorded api/backend when adding them.
def token_stats(r, tok=None):
    """Normalize a record's usage to input/output (Anthropic sense) and split output into
    reasoning (CoT) + writing (answer + tool-call args). Returns None for non-LLM records.

    input   = prompt_tokens - cached_tokens   (fresh tokens actually prefilled this turn)
    output  = completion_tokens               (everything the model generated)
    reasoning = completion_tokens_details.reasoning_tokens  if the server reports a count; else
                tok(reasoning_text)                          EXACT, when a tokenizer `tok` is given;
                else output * reasoning_chars / (r+w)_chars  a char-ratio ESTIMATE (est=True).
    writing = output - reasoning              (so the two always sum to output; tool-call
                                               arguments and any thinking-delimiter tokens land
                                               on the writing side)."""
    u = r.get("usage") or {}
    # Normalize backend-specific usage to (pt=cache-inclusive prompt, cache, out). llama.cpp/OpenAI
    # report prompt_tokens (cache-INCLUSIVE) + completion_tokens; Anthropic reports input_tokens
    # (cache-EXCLUDED) + cache_read/creation_input_tokens + output_tokens. For Anthropic we fold the
    # cached counts back in so pt is cache-inclusive and inp = pt - cache stays the fresh-prefill count.
    reasoning = None
    if u.get("prompt_tokens") is not None:                # llama.cpp / OpenAI-compatible
        pt = u.get("prompt_tokens")
        cache = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        out = u.get("completion_tokens") or 0
        reasoning = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    elif u.get("input_tokens") is not None:               # Anthropic Messages API (Claude Code)
        cache = (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        pt = (u.get("input_tokens") or 0) + cache
        out = u.get("output_tokens") or 0                 # Anthropic reports no reasoning-token count
    else:
        return None
    est = reasoning is None
    if reasoning is None and tok is not None:            # exact: tokenize the captured thinking
        exact = tok(r.get("reasoning") or "")
        if exact is not None:
            reasoning, est = min(exact, out), False
    if reasoning is None:                                 # fall back to a char-ratio estimate
        rchars = len(r.get("reasoning") or "")
        wchars = len(r.get("completion") or "")
        for tc in (r.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if fn:                                        # OpenAI shape: function.name + arguments
                wchars += len(fn.get("name") or "") + len(fn.get("arguments") or "")
            else:                                         # Anthropic shape: name + JSON(input)
                wchars += len(tc.get("name") or "") + len(json.dumps(tc.get("input") or {}))
        reasoning = round(out * rchars / (rchars + wchars)) if (rchars + wchars) else 0
    inp = max(pt - cache, 0)
    # throughput: prefer the backend `timings` (exact); else gateway phase timestamps
    # (ttft_ms / decode_ms); None when the run predates timing capture.
    tim = r.get("timings") or {}
    pm, dm = tim.get("prompt_ms"), tim.get("predicted_ms")
    ttft, dms = r.get("ttft_ms"), r.get("decode_ms")
    prefill_s = (pm / 1000.0) if isinstance(pm, (int, float)) else (
        (ttft / 1000.0) if isinstance(ttft, (int, float)) and ttft > 0 else None)
    decode_s = (dm / 1000.0) if isinstance(dm, (int, float)) else (
        (dms / 1000.0) if isinstance(dms, (int, float)) and dms > 0 else None)
    prefill_tks = tim.get("prompt_per_second")
    if not isinstance(prefill_tks, (int, float)):
        prefill_tks = (inp / prefill_s) if prefill_s else None
    decode_tks = tim.get("predicted_per_second")
    if not isinstance(decode_tks, (int, float)):
        decode_tks = (out / decode_s) if decode_s else None
    return {"ctx": pt, "cache": cache, "input": inp, "output": out,
            "reasoning": reasoning, "writing": max(out - reasoning, 0), "est": est,
            "prefill_s": prefill_s, "decode_s": decode_s,
            "prefill_tks": prefill_tks, "decode_tks": decode_tks}


def _local_tokenizer(path):
    """(fn(text)->int, label) from a local HF tokenizer.json---offline & exact. (None, None) if
    unusable. `path` may point at the file or its parent dir."""
    p = Path(path)
    if p.is_dir():
        p = p / "tokenizer.json"
    try:
        from bpe_tokenizer import Tokenizer
        t = Tokenizer.from_file(str(p))
    except Exception as e:
        print(f"# --tokenizer {p}: {e}", file=sys.stderr)
        return None, None
    return t.count, f"local tokenizer {p}"


def _backend_tokenizer(url):
    """(fn(text)->int|None, label) via a llama.cpp /tokenize endpoint. (None, None) if no URL or
    the backend is unreachable. Per-call None (mid-run drop) makes that record fall back to est."""
    if not url:
        print("# --tokenize: no backend URL (none recorded in index.json)", file=sys.stderr)
        return None, None
    import ssl
    import urllib.request
    base = url.rstrip("/")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    cache = {}

    def count(text):
        if not text:
            return 0
        if text in cache:
            return cache[text]
        try:
            body = json.dumps({"content": text}).encode()
            req = urllib.request.Request(base + "/tokenize", data=body,
                                         headers={"Content-Type": "application/json"})
            o = json.load(urllib.request.urlopen(req, context=ctx, timeout=30))
            n = len(o.get("tokens") if isinstance(o, dict) else o)
        except Exception:
            return None
        cache[text] = n
        return n

    if count("probe") is None:
        print(f"# --tokenize: backend {base} unreachable", file=sys.stderr)
        return None, None
    return count, f"backend /tokenize {base}"


def _catalog(script_dir):
    """Local tokenizer catalog: [(dirname, tokenizer.json path)] under ./tokenizers/."""
    base = Path(script_dir) / "tokenizers"
    out = []
    if base.is_dir():
        for d in sorted(base.iterdir()):
            tj = d / "tokenizer.json"
            if tj.is_file():
                out.append((d.name, tj))
    return out


def _norm(s):
    return "".join(c for c in s.lower() if c.isalnum())


def _match_tokenizer(model, catalog):
    """Best (name, path) whose dir name aligns with the model name, or None. Scored by shared
    normalized prefix length; requires the whole tokenizer name to match or >=5 shared chars, so a
    wrong model (e.g. llama vs the only-Qwen catalog) matches nothing rather than mis-picking."""
    if not model or not catalog:
        return None
    m = _norm(Path(model).name)
    best = None
    for name, path in catalog:
        t = _norm(name)
        c = 0
        for x, y in zip(m, t):
            if x != y:
                break
            c += 1
        if c and (c == len(t) or c >= 5) and (best is None or c > best[0]):
            best = (c, name, path)
    return (best[1], best[2]) if best else None


def _first_model(plane1):
    """The model name from the first record that carries one (early records may omit it)."""
    try:
        with open(plane1) as f:
            for line in f:
                if line.strip():
                    m = json.loads(line).get("model")
                    if m:
                        return m
    except OSError:
        pass
    return None


def resolve_tokenizer(tok_path, tok_url, default_url, model, script_dir, assume_yes):
    """Pick an exact-count tokenizer. Precedence: explicit --tokenizer path → explicit backend →
    auto-match a local tokenizer to the model name (confirmed interactively, or with -y). Returns
    (fn, label); (None, None) → caller keeps the char-ratio estimate. tok_url '' means default_url."""
    if tok_path:                                    # 1) explicit local path
        fn, label = _local_tokenizer(tok_path)
        if fn:
            return fn, label
        if tok_url is None:
            return None, None
        print("# falling back to backend /tokenize", file=sys.stderr)
    if tok_url is not None:                          # 2) explicit backend
        return _backend_tokenizer(tok_url or default_url)
    hit = _match_tokenizer(model, _catalog(script_dir))   # 3) auto-match local to model
    if not hit:
        return None, None
    name, path = hit
    mname = Path(model).name
    if not assume_yes:
        if not sys.stdin.isatty():
            print(f"# matched local tokenizer '{name}' for model '{mname}'---pass -y to use it "
                  f"(or --tokenizer PATH); using char-ratio estimate for now", file=sys.stderr)
            return None, None
        print(f"Model '{mname}' → matched local tokenizer '{name}'. Use it for EXACT token "
              f"counts? [Y/n] ", end="", file=sys.stderr, flush=True)
        try:
            ans = input().strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("", "y", "yes"):
            print("# using char-ratio estimate", file=sys.stderr)
            return None, None
    return _local_tokenizer(str(path))


def main():
    tok_path = None
    tok_url = None                     # None = backend not asked for; "" = asked, use index.json llm
    assume_yes = False
    rest = []
    it = iter(sys.argv[1:])
    for a in it:
        if a == "--tokenizer":
            tok_path = next(it, None)
        elif a == "--tokenize-url":
            tok_url = next(it, None) or ""
        elif a == "--tokenize":
            tok_url = ""
        elif a in ("-y", "--yes"):
            assume_yes = True
        else:
            rest.append(a)
    d = resolve(rest)
    if not (d / "plane1.jsonl").exists():
        sys.exit(f"no plane1.jsonl in {d}")
    idx, idx_txt = {}, ""
    if (d / "index.json").exists():
        idx_txt = (d / "index.json").read_text()
        try:
            idx = json.loads(idx_txt)
        except ValueError:
            pass
    # resolve the tokenizer BEFORE any stdout, so its confirmation prompt (stderr) comes first
    model = _first_model(d / "plane1.jsonl")
    tok, tok_label = resolve_tokenizer(tok_path, tok_url, idx.get("llm"), model, SCRIPT_DIR, assume_yes)
    print(f"===== {d} =====")
    if idx_txt:
        print(idx_txt)
    if tok_label:
        print(f"# exact reasoning/writing tokens via {tok_label}", file=sys.stderr)
    print("===== plane1.jsonl =====")
    totals = {"input": 0, "cache": 0, "output": 0, "reasoning": 0, "writing": 0, "turns": 0,
              "est": False, "pf_tok": 0, "pf_s": 0.0, "dec_tok": 0, "dec_s": 0.0}
    rate = lambda x: f"{x:,.0f}" if isinstance(x, (int, float)) else "N/A"  # noqa: E731
    for line in open(d / "plane1.jsonl"):
        r = json.loads(line)
        print("\n-- seq %s | %s | status %s | %s | %sms --" % (
            r["seq"], r["endpoint"], r.get("status"), r.get("ts_request", "")[:19], r.get("latency_ms")))
        print("   model=%s stream=%s req=%sB resp=%sB" % (
            r.get("model"), r.get("stream"), r.get("request_bytes"), r.get("response_bytes")))
        req = r.get("request")
        if isinstance(req, dict) and req.get("messages"):
            offset = r.get("messages_offset", 0)
            if offset:
                print("     [... %d prior messages elided ...]" % offset)
            for m in req["messages"]:
                c = m.get("content")
                c = c if isinstance(c, str) else json.dumps(c)
                print("     [%-9s] %6dc  %s" % (str(m.get("role")), len(c or ""), repr((c or "")[:80])))
        if r.get("completion"):
            print("   completion: " + repr(r["completion"][:200]))
        if r.get("reasoning"):
            print("   reasoning:  " + repr(r["reasoning"][:200]))
        if r.get("tool_calls"):
            print("   tool_calls: " + json.dumps(r["tool_calls"])[:200])
        st = token_stats(r, tok)
        if st:
            m = "~" if st["est"] else ""
            print("   tokens: input=%d (+cache %d, ctx %d) | output=%d = reasoning %s%d + writing %s%d" % (
                st["input"], st["cache"], st["ctx"], st["output"], m, st["reasoning"], m, st["writing"]))
            if st["prefill_tks"] is not None or st["decode_tks"] is not None:
                print("   speed:  prefill %s tk/s | decode %s tk/s" % (
                    rate(st["prefill_tks"]), rate(st["decode_tks"])))
            for k in ("input", "cache", "output", "reasoning", "writing"):
                totals[k] += st[k]
            totals["turns"] += 1
            totals["est"] = totals["est"] or st["est"]
            if st["prefill_s"]:
                totals["pf_tok"] += st["input"]
                totals["pf_s"] += st["prefill_s"]
            if st["decode_s"]:
                totals["dec_tok"] += st["output"]
                totals["dec_s"] += st["decode_s"]
        print("   prompt_sha=%s completion_sha=%s tool_calls_sha=%s" % (
            (r.get("prompt_sha256") or "")[:16], (r.get("completion_sha256") or "")[:16],
            (r.get("tool_calls_sha256") or "")[:16]))
        print("   prev=%-16s hash=%s" % (r["prev_hash"][:16] or "(genesis)", r["record_hash"][:16]))

    if totals["turns"]:
        m = "~" if totals["est"] else ""
        print("\n===== totals: %d LLM turns =====" % totals["turns"])
        print("  input  (fresh, non-cached):  %s" % f"{totals['input']:,}")
        print("  cache-read (context reused): %s" % f"{totals['cache']:,}")
        print("  output: %s = reasoning %s%s + writing %s%s" % (
            f"{totals['output']:,}", m, f"{totals['reasoning']:,}", m, f"{totals['writing']:,}"))
        pf = f"{totals['pf_tok'] / totals['pf_s']:,.0f} tk/s" if totals["pf_s"] else "N/A (no timing captured)"
        dc = f"{totals['dec_tok'] / totals['dec_s']:,.0f} tk/s" if totals["dec_s"] else "N/A (no timing captured)"
        print("  avg prefill: %s   avg decode: %s" % (pf, dc))
        if totals["est"]:
            print("  (~ reasoning/writing split is char-ratio ESTIMATED from the captured thinking text;")
            print("     llama.cpp streams the reasoning content but reports no token count. Pass")
            print("     --tokenizer PATH (offline) or --tokenize-url URL for exact counts.)")
        elif tok_label:
            print("  (reasoning/writing split is EXACT---tokenized via %s.)" % tok_label)


if __name__ == "__main__":
    main()
