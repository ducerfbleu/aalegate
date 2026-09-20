#!/usr/bin/env python3
"""Minimal OpenAI + Anthropic mock LLM for gateway smoke-testing.

Usage: mock_llm.py [port]   (default 9091)

Routes:
  GET  /props, /v1/models       -> canned JSON (mimics pi's model-refresh probes)
  POST /v1/chat/completions     -> OpenAI: non-stream JSON, or SSE stream if {"stream": true}
       model == "streamtool"    -> streams a tool call in argument fragments
       else (when streaming)    -> streams text content in token fragments
  POST /v1/messages             -> Anthropic (Claude Code): non-stream JSON, or SSE stream
       model == "streamtool"    -> streams a tool_use via input_json_delta fragments
       else (when streaming)    -> streams a thinking block then a text block

Pure standard library (http.server). No third-party deps.
"""
import sys
import json
import gzip
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, ctype, body):
        """Send a full response body, gzip-compressed when the client advertises it. The gateway's
        Transport requests `Accept-Encoding: gzip` on its own, so this exercises the gateway's
        response-decode path (real APIs like api.anthropic.com return gzip SSE)."""
        if isinstance(body, str):
            body = body.encode()
        headers = [("Content-Type", ctype), ("Connection", "close")]
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body)
            headers.append(("Content-Encoding", "gzip"))
        headers.append(("Content-Length", str(len(body))))
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj))

    def _sse(self, events):
        """events: list of (event_name_or_None, data). OpenAI omits the event line; a str data is
        emitted verbatim (e.g. the `[DONE]` sentinel), a dict is JSON-encoded."""
        out = []
        for ev, data in events:
            if ev:
                out.append("event: %s\n" % ev)
            payload = data if isinstance(data, str) else json.dumps(data)
            out.append("data: %s\n\n" % payload)
        self._send(200, "text/event-stream", "".join(out))

    def do_GET(self):
        if self.path.endswith("/props"):
            self._json(200, {"default_generation_settings": {"n_ctx": 4096}, "total_slots": 1})
        elif self.path.endswith("/models"):
            self._json(200, {"data": [{"id": "mock-model"}]})
        else:
            self._json(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(n))
        except Exception:
            body = {}
        if self.path.endswith("/messages"):
            return self._anthropic(body)
        if body.get("stream"):
            events = [(None, c) for c in self._stream_chunks(body.get("model", ""))]
            events.append((None, "[DONE]"))
            self._sse(events)
        else:
            self._json(200, {
                "model": "mock-model",
                "choices": [{"message": {"role": "assistant", "content": "Hello from mock"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            })

    def _stream_chunks(self, model):
        if model == "streamtool":
            # tool-call arguments arrive in fragments across chunks (the hard case)
            return [
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "call_1", "type": "function",
                     "function": {"name": "get_weather", "arguments": ""}}]}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": "{\"city\":"}}]}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": "\"NYC\"}"}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
        # content arrives in token fragments, usage in a trailing chunk
        return [
            {"model": "mock-model", "choices": [{"delta": {"content": "Hel"}}]},
            {"model": "mock-model", "choices": [{"delta": {"content": "lo!"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
        ]

    # ---- Anthropic Messages API (Claude Code) --------------------------------

    def _anthropic(self, body):
        if body.get("stream"):
            self._sse(self._anthropic_events(body.get("model", "")))
        else:
            self._json(200, {
                "id": "msg_mock", "type": "message", "role": "assistant",
                "model": "mock-claude",
                "content": [{"type": "text", "text": "Hello from mock"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 4},
            })

    def _anthropic_events(self, model):
        start = {"id": "msg_mock", "type": "message", "role": "assistant",
                 "model": "mock-claude", "content": [], "stop_reason": None}
        if model == "streamtool":
            # tool_use input arrives as partial_json fragments (the hard case)
            return [
                ("message_start", {"type": "message_start",
                    "message": dict(start, usage={"input_tokens": 5, "output_tokens": 1})}),
                ("content_block_start", {"type": "content_block_start", "index": 0,
                    "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": "{\"city\":"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": "\"NYC\"}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta",
                    "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 6}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        # default: a thinking block (index 0) then a text block (index 1)
        return [
            ("message_start", {"type": "message_start",
                "message": dict(start, usage={"input_tokens": 3, "output_tokens": 1})}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                "content_block": {"type": "thinking", "thinking": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "Let me think"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("content_block_start", {"type": "content_block_start", "index": 1,
                "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1,
                "delta": {"type": "text_delta", "text": "Hel"}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1,
                "delta": {"type": "text_delta", "text": "lo!"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
            ("message_delta", {"type": "message_delta",
                "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
            ("message_stop", {"type": "message_stop"}),
        ]

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9091
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
