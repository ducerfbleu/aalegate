#!/usr/bin/env bash
# Smoke test for the aalegate L7 gateway (aalegate/gateway).
#
# Builds the gateway image, runs it against mock_llm.py, exercises endpoint
# passthrough + non-stream + streamed-content + streamed-tool-call, then ASSERTS
# the hash-chained log contents and checks `-verify` (intact + tampered).
#
# Requires: docker, python3, curl. Linux host (uses --network host). Self-cleans.
# Usage:
#   ./smoke-test.sh              # build image, then test
#   ./smoke-test.sh --no-build   # test an already-built image
# Env overrides: IMAGE (default pi-gateway), GW_PORT (9090), MOCK_PORT (9091).

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${IMAGE:-aalegate-gateway}"
GW_PORT="${GW_PORT:-9090}"
MOCK_PORT="${MOCK_PORT:-9091}"
CONTAINER="gw-smoke-$$"
WORK="$(mktemp -d)"
MOCK_PID=""

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1
  [ -n "$MOCK_PID" ] && kill "$MOCK_PID" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() { echo "ERROR: $*" >&2; exit 1; }
command -v docker  >/dev/null || fail "docker not found"
command -v python3 >/dev/null || fail "python3 not found"
command -v curl    >/dev/null || fail "curl not found"

if [ "${1:-}" != "--no-build" ]; then
  echo "== build $IMAGE =="
  docker build -t "$IMAGE" -f "$HERE/Dockerfile.gateway" "$HERE" >/dev/null || fail "build failed"
fi

mkdir -p "$WORK/logs"
python3 "$HERE/mock_llm.py" "$MOCK_PORT" & MOCK_PID=$!
sleep 0.5

echo "== start gateway =="
docker run -d --name "$CONTAINER" --network host --user "$(id -u):$(id -g)" \
  -e GATEWAY_LISTEN=":$GW_PORT" -e GATEWAY_UPSTREAM="http://127.0.0.1:$MOCK_PORT" \
  -e GATEWAY_LOG=/logs/g.jsonl -e GATEWAY_RUN_ID=smoketest \
  -v "$WORK/logs:/logs" "$IMAGE" >/dev/null || fail "gateway failed to start"
sleep 0.8

echo "== exercise (props, non-stream, stream, stream-tool-call) =="
curl -s "localhost:$GW_PORT/props" >/dev/null
curl -s "localhost:$GW_PORT/v1/chat/completions" \
  -d '{"model":"nonstream","messages":[{"role":"user","content":"hi"}]}' >/dev/null
curl -s "localhost:$GW_PORT/v1/chat/completions" \
  -d '{"model":"stream","stream":true,"messages":[{"role":"user","content":"hi"}]}' >/dev/null
curl -s "localhost:$GW_PORT/v1/chat/completions" \
  -d '{"model":"streamtool","stream":true,"messages":[{"role":"user","content":"weather?"}]}' >/dev/null

echo "== exercise dedup (3-turn conversation) =="
curl -s "localhost:$GW_PORT/v1/chat/completions" \
  -d '{"model":"nonstream","messages":[{"role":"user","content":"turn1"}]}' >/dev/null
curl -s "localhost:$GW_PORT/v1/chat/completions" \
  -d '{"model":"nonstream","messages":[{"role":"user","content":"turn1"},{"role":"assistant","content":"reply1"},{"role":"user","content":"turn2"}]}' >/dev/null
curl -s "localhost:$GW_PORT/v1/chat/completions" \
  -d '{"model":"nonstream","messages":[{"role":"user","content":"turn1"},{"role":"assistant","content":"reply1"},{"role":"user","content":"turn2"},{"role":"assistant","content":"reply2"},{"role":"user","content":"turn3"}]}' >/dev/null

echo "== exercise Anthropic Messages API (non-stream, stream+thinking, stream-tool_use) =="
curl -s "localhost:$GW_PORT/v1/messages" \
  -d '{"model":"claude","max_tokens":64,"messages":[{"role":"user","content":"hi"}]}' >/dev/null
curl -s "localhost:$GW_PORT/v1/messages" \
  -d '{"model":"claude","max_tokens":64,"stream":true,"messages":[{"role":"user","content":"hi"}]}' >/dev/null
curl -s "localhost:$GW_PORT/v1/messages" \
  -d '{"model":"streamtool","max_tokens":64,"stream":true,"messages":[{"role":"user","content":"weather?"}]}' >/dev/null

echo "== exercise Anthropic dedup (3-turn conversation) =="
curl -s "localhost:$GW_PORT/v1/messages" \
  -d '{"model":"claude","max_tokens":64,"messages":[{"role":"user","content":"turn1"}]}' >/dev/null
curl -s "localhost:$GW_PORT/v1/messages" \
  -d '{"model":"claude","max_tokens":64,"messages":[{"role":"user","content":"turn1"},{"role":"assistant","content":"reply1"},{"role":"user","content":"turn2"}]}' >/dev/null
curl -s "localhost:$GW_PORT/v1/messages" \
  -d '{"model":"claude","max_tokens":64,"messages":[{"role":"user","content":"turn1"},{"role":"assistant","content":"reply1"},{"role":"user","content":"turn2"},{"role":"assistant","content":"reply2"},{"role":"user","content":"turn3"}]}' >/dev/null
sleep 0.5
docker rm -f "$CONTAINER" >/dev/null

echo
echo "== captured log (aalegate provenance records) =="
cat "$WORK/logs/g.jsonl"

echo
echo "== assertions =="
python3 - "$WORK/logs/g.jsonl" <<'PYEOF' || fail "log assertions failed"
import sys, json
recs = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
ok = True
def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(("  PASS" if cond else "  FAIL"), name)
def find(pred): return next((r for r in recs if pred(r)), None)

props = find(lambda r: r["endpoint"] == "props")
ns    = find(lambda r: r["endpoint"] == "chat.completions" and not r["stream"]
             and r.get("request") and '"hi"' in json.dumps(r["request"]))
st    = find(lambda r: r["stream"] and r.get("model") == "mock-model")
tl    = find(lambda r: r["stream"] and r.get("model") == "streamtool")

check("13 records", len(recs) == 13)
check("props passthrough logged", props and props["status"] == 200)
check("non-stream completion captured", ns and ns["completion"] == "Hello from mock")
check("non-stream usage captured", ns and ns.get("usage"))
check("streamed content reassembled -> 'Hello!'", st and st["completion"] == "Hello!")
check("streamed usage captured", st and st.get("usage"))
check("streamed tool-call reassembled",
      tl and tl["tool_calls"][0]["function"]["arguments"] == '{"city":"NYC"}')
check("prompt + completion hashes present", ns and ns.get("prompt_sha256") and ns.get("completion_sha256"))
check("text turn: completion hash set, no tool-calls hash", ns and ns.get("completion_sha256") and not ns.get("tool_calls_sha256"))
check("tool-call turn: tool_calls hash set, no empty completion hash", tl and tl.get("tool_calls_sha256") and not tl.get("completion_sha256"))
chain = recs[0]["prev_hash"] == "" and all(
    recs[i]["prev_hash"] == recs[i-1]["record_hash"] for i in range(1, len(recs)))
check("hash chain links head->tail (including deduped)", chain)

# dedup assertions (records 5-7 are the 3-turn conversation, 0-indexed 4,5,6)
dedup = [r for r in recs if r["endpoint"] == "chat.completions" and not r["stream"]
         and r.get("request") and '"turn' in json.dumps(r["request"])]
check("dedup: 3 conversation turns recorded", len(dedup) == 3)
d1, d2, d3 = dedup[0], dedup[1], dedup[2]
check("dedup turn 1: no offset (full body)", d1.get("messages_offset", 0) == 0)
check("dedup turn 2: messages_offset=1", d2.get("messages_offset") == 1)
check("dedup turn 3: messages_offset=3", d3.get("messages_offset") == 3)

d2_msgs = d2["request"].get("messages", []) if isinstance(d2["request"], dict) else []
d3_msgs = d3["request"].get("messages", []) if isinstance(d3["request"], dict) else []
check("dedup turn 2: request has 2 new messages", len(d2_msgs) == 2)
check("dedup turn 3: request has 2 new messages", len(d3_msgs) == 2)
check("dedup turn 2: prompt_sha256 is full-body hash", bool(d2.get("prompt_sha256")))
check("dedup turn 2: request_bytes reflects full body",
      d2["request_bytes"] > len(json.dumps(d2["request"])))

# ---- Anthropic Messages API (Claude Code) assertions ----
a_ns = find(lambda r: r["endpoint"] == "messages" and not r["stream"]
            and r.get("request") and '"hi"' in json.dumps(r["request"]))
a_st = find(lambda r: r["endpoint"] == "messages" and r["stream"] and not r.get("tool_calls"))
a_tl = find(lambda r: r["endpoint"] == "messages" and r["stream"] and r.get("tool_calls"))

check("anthropic endpoint normalized to 'messages'", a_ns is not None)
check("anthropic non-stream completion captured", a_ns and a_ns["completion"] == "Hello from mock")
check("anthropic non-stream model captured", a_ns and a_ns.get("model") == "mock-claude")
check("anthropic non-stream usage (input/output_tokens)",
      a_ns and (a_ns.get("usage") or {}).get("input_tokens") == 3 and a_ns["usage"].get("output_tokens") == 4)
check("anthropic non-stream prompt+completion hashes present",
      a_ns and a_ns.get("prompt_sha256") and a_ns.get("completion_sha256"))
check("anthropic stream text reassembled -> 'Hello!'", a_st and a_st["completion"] == "Hello!")
check("anthropic stream thinking captured -> reasoning", a_st and a_st.get("reasoning") == "Let me think")
check("anthropic stream finish_reason=end_turn", a_st and a_st.get("finish_reason") == "end_turn")
check("anthropic stream usage merged (input 3 from message_start + output 5 from message_delta)",
      a_st and (a_st.get("usage") or {}).get("input_tokens") == 3 and a_st["usage"].get("output_tokens") == 5)
check("anthropic stream tool_use name+input reassembled",
      a_tl and a_tl["tool_calls"][0]["name"] == "get_weather"
      and a_tl["tool_calls"][0]["input"] == {"city": "NYC"})
check("anthropic tool turn: tool_calls hash set, no completion hash",
      a_tl and a_tl.get("tool_calls_sha256") and not a_tl.get("completion_sha256"))

a_dedup = [r for r in recs if r["endpoint"] == "messages" and not r["stream"]
           and r.get("request") and '"turn' in json.dumps(r["request"])]
check("anthropic dedup: 3 conversation turns recorded", len(a_dedup) == 3)
ad1, ad2, ad3 = a_dedup
check("anthropic dedup turn 1: no offset (full body)", ad1.get("messages_offset", 0) == 0)
check("anthropic dedup turn 2: messages_offset=1", ad2.get("messages_offset") == 1)
check("anthropic dedup turn 3: messages_offset=3", ad3.get("messages_offset") == 3)

sys.exit(0 if ok else 1)
PYEOF

echo
echo "== verify intact log (expect OK) =="
docker run --rm --user "$(id -u):$(id -g)" -v "$WORK/logs:/logs" "$IMAGE" -verify /logs/g.jsonl \
  || fail "verification of intact log failed"

echo
echo "== verify tampered log (flip one char in a completion; expect FAIL) =="
sed 's/Hello from mock/Hxllo from mock/' "$WORK/logs/g.jsonl" > "$WORK/logs/t.jsonl"
if docker run --rm --user "$(id -u):$(id -g)" -v "$WORK/logs:/logs" "$IMAGE" -verify /logs/t.jsonl; then
  fail "tampered log passed verification (it should have been rejected)"
else
  echo "  -> correctly detected tampering"
fi

echo
echo "ALL CHECKS PASSED"
