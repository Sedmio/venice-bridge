#!/usr/bin/env python3
"""Self-check for venice_adapters.py: a fake Chat Completions upstream, both protocols, stream and not,
a full call -> result -> answer loop, and an upstream error. Run: python3 test_venice_adapters.py"""
import http.server
import json
import threading
import urllib.request

import venice_adapters as va

SEEN = []  # chat requests the fake upstream received


class Upstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        raw = json.dumps({"data": [{"id": "m1"}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append(req)
        last = req["messages"][-1]
        if "quota" in str(last.get("content")):
            raw = json.dumps({"error": {"message": "quota used up"}}).encode()
            self.send_response(429); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
            return
        if last["role"] == "tool":  # second turn: answer with text
            msg, finish = {"role": "assistant", "content": "done: " + last["content"]}, "stop"
        else:  # first turn: text + one call to the first declared tool
            name = req["tools"][0]["function"]["name"]
            args = {"input": "*** Begin Patch"} if name == "apply_patch" else {"path": "a.py"}
            msg, finish = {"role": "assistant", "content": "looking", "tool_calls": [
                {"index": 0, "id": "call_1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, "tool_calls"
        if not req.get("stream"):
            raw = json.dumps({"choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                              "usage": {"prompt_tokens": 7, "completion_tokens": 3}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
            return
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        chunks = [{"delta": {"role": "assistant", "content": msg["content"][:3]}}, {"delta": {"content": msg["content"][3:]}}]
        for tc in msg.get("tool_calls", []):
            a = tc["function"]["arguments"]
            chunks.append({"delta": {"tool_calls": [{"index": 0, "id": tc["id"], "type": "function", "function": {"name": tc["function"]["name"], "arguments": a[:4]}}]}})
            chunks.append({"delta": {"tool_calls": [{"index": 0, "function": {"arguments": a[4:]}}]}})
        chunks.append({"delta": {}, "finish_reason": finish})
        for c in chunks:
            self.wfile.write(("data: " + json.dumps({"choices": [dict(c, index=0)]}) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


def serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def post(port, path, body):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        r = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=20)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def sse(raw):
    return [json.loads(l[6:]) for l in raw.splitlines() if l.startswith("data: ")]


def main():
    up = serve(Upstream)
    va.UPSTREAM = "http://127.0.0.1:%d/v1" % up.server_port
    va.DEFAULT_MODEL = "m1"
    port = serve(va.Handler).server_port
    tool_a = {"name": "read", "description": "read a file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}

    # ---- Anthropic, not streamed: text + tool_use
    st, raw = post(port, "/v1/messages?beta=true", {"model": "claude-x", "max_tokens": 50, "system": [{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}],
                                                   "tools": [tool_a, {"type": "web_search_20250305", "name": "web_search"}],
                                                   "messages": [{"role": "user", "content": "hi"}]})
    m = json.loads(raw)
    assert st == 200 and m["stop_reason"] == "tool_use" and m["model"] == "claude-x", raw
    assert m["content"] == [{"type": "text", "text": "looking"}, {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "a.py"}}]
    assert m["usage"] == {"input_tokens": 7, "output_tokens": 3}
    sent = SEEN[-1]
    assert sent["model"] == "m1" and sent["messages"][0] == {"role": "system", "content": "S"} and len(sent["tools"]) == 1
    assert sent["tools"][0]["function"]["parameters"] == tool_a["input_schema"]
    # ---- Anthropic second turn: results first, then the user's text; text + call in one assistant message
    hist = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": m["content"]},
            {"role": "user", "content": [{"type": "text", "text": "and then?"}, {"type": "tool_result", "tool_use_id": "call_1", "content": [{"type": "text", "text": "FILE"}]}]}]
    st, raw = post(port, "/v1/messages", {"model": "m1", "tools": [tool_a], "messages": hist, "stream": True})
    sent = SEEN[-1]["messages"]
    assert [x["role"] for x in sent] == ["user", "assistant", "tool", "user"], sent
    assert sent[1]["content"] == "looking" and sent[1]["tool_calls"][0]["id"] == "call_1" and sent[2] == {"role": "tool", "tool_call_id": "call_1", "content": "FILE"}
    # the fake upstream answers the last message (user), so ask again with the result last
    hist[2]["content"] = hist[2]["content"][1:]
    st, raw = post(port, "/v1/messages", {"model": "m1", "tools": [tool_a], "messages": hist, "stream": True})
    ev = sse(raw)
    kinds = [e["type"] for e in ev]
    assert kinds[0] == "message_start" and kinds[-2:] == ["message_delta", "message_stop"], kinds
    assert "".join(e["delta"]["text"] for e in ev if e["type"] == "content_block_delta") == "done: FILE"
    assert ev[-2]["delta"]["stop_reason"] == "end_turn"
    # ---- Anthropic streamed tool call
    st, raw = post(port, "/v1/messages", {"model": "m1", "tools": [tool_a], "messages": [{"role": "user", "content": "hi"}], "stream": True})
    ev = sse(raw)
    starts = [e["content_block"] for e in ev if e["type"] == "content_block_start"]
    assert [b["type"] for b in starts] == ["text", "tool_use"] and starts[1]["name"] == "read"
    assert json.loads(next(e["delta"]["partial_json"] for e in ev if e.get("delta", {}).get("type") == "input_json_delta")) == {"path": "a.py"}
    assert [e["type"] for e in ev].count("content_block_stop") == 2 and ev[-2]["delta"]["stop_reason"] == "tool_use"
    # ---- an upstream error keeps its status, in each protocol's shape
    st, raw = post(port, "/v1/messages", {"model": "m1", "messages": [{"role": "user", "content": "quota"}], "stream": True})
    assert st == 429 and json.loads(raw) == {"type": "error", "error": {"type": "rate_limit_error", "message": "quota used up"}}, raw
    st, raw = post(port, "/v1/responses", {"model": "m1", "input": "quota", "stream": True})
    assert st == 429 and json.loads(raw)["error"]["message"] == "quota used up"
    st, raw = post(port, "/v1/messages/count_tokens", {"messages": [{"role": "user", "content": "x" * 400}]})
    assert st == 200 and json.loads(raw)["input_tokens"] > 90
    print("messages ok")

    # ---- Responses, not streamed: function tool
    fn = {"type": "function", "name": "read", "description": "d", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}
    st, raw = post(port, "/v1/responses", {"model": "gpt-x", "instructions": "S", "tools": [fn, {"type": "web_search"}],
                                           "input": [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "D"}]},
                                                     {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]})
    r = json.loads(raw)
    assert st == 200 and r["status"] == "completed" and [o["type"] for o in r["output"]] == ["message", "function_call"], raw
    assert r["output"][0]["content"][0]["text"] == "looking" and r["output"][1]["call_id"] == "call_1" and json.loads(r["output"][1]["arguments"]) == {"path": "a.py"}
    assert r["usage"]["total_tokens"] == 10
    sent = SEEN[-1]
    assert [x["role"] for x in sent["messages"]] == ["system", "system", "user"] and len(sent["tools"]) == 1
    # ---- Responses second turn, streamed: reasoning item ignored, call + output in order, ends with response.completed
    items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
             {"type": "reasoning", "summary": []},
             {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "looking"}]},
             {"type": "function_call", "call_id": "call_1", "name": "read", "arguments": "{\"path\": \"a.py\"}"},
             {"type": "function_call_output", "call_id": "call_1", "output": "FILE"}]
    st, raw = post(port, "/v1/responses", {"model": "m1", "tools": [fn], "input": items, "stream": True})
    sent = SEEN[-1]["messages"]
    assert [x["role"] for x in sent] == ["user", "assistant", "tool"] and sent[1]["content"] == "looking" and sent[1]["tool_calls"][0]["id"] == "call_1", sent
    ev = sse(raw)
    assert ev[0]["type"] == "response.created" and ev[-1]["type"] == "response.completed"
    assert [e["sequence_number"] for e in ev] == list(range(1, len(ev) + 1))
    assert "".join(e["delta"] for e in ev if e["type"] == "response.output_text.delta") == "done: FILE"
    assert ev[-1]["response"]["output"][0]["content"][0]["text"] == "done: FILE" and "usage" in ev[-1]["response"]
    # ---- Responses streamed call to a freeform (custom) tool: text input back, not JSON
    st, raw = post(port, "/v1/responses", {"model": "m1", "tools": [{"type": "custom", "name": "apply_patch", "description": "patch"}], "input": "patch it", "stream": True})
    assert SEEN[-1]["tools"][0]["function"]["parameters"]["required"] == ["input"]
    done = [e["item"] for e in sse(raw) if e["type"] == "response.output_item.done"]
    assert done[-1]["type"] == "custom_tool_call" and done[-1]["input"] == "*** Begin Patch" and done[-1]["call_id"] == "call_1", done
    # and its output item on the way back in
    chat, custom = va.responses_to_chat({"model": "m1", "input": [
        {"type": "custom_tool_call", "call_id": "c9", "name": "apply_patch", "input": "P"},
        {"type": "custom_tool_call_output", "call_id": "c9", "output": {"content": "ok"}}]})
    assert chat["messages"][0]["tool_calls"][0]["function"] == {"name": "apply_patch", "arguments": "{\"input\": \"P\"}"}
    assert chat["messages"][1] == {"role": "tool", "tool_call_id": "c9", "content": "ok"}
    print("responses ok")


if __name__ == "__main__":
    main()
