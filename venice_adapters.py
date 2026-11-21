#!/usr/bin/env python3
"""venice-adapters: Anthropic Messages and OpenAI Responses in front of an OpenAI Chat Completions server.

    Claude Code --POST /v1/messages --+
                                      +--> this file (127.0.0.1:18892) --> /v1/chat/completions (the bridge, :18891)
    Codex CLI  --POST /v1/responses --+

It only translates: messages, tool declarations, tool calls / results and the stream events. Nothing about
Venice lives here; the upstream can be any Chat Completions server.

Env: VENICE_ADAPTER_PORT (18892), VENICE_ADAPTER_HOST (127.0.0.1), VENICE_ADAPTER_UPSTREAM
(http://127.0.0.1:18891/v1), VENICE_ADAPTER_DUMP (a folder: save every request body), VENICE_ADAPTER_MODEL (used when the client asks for a model the upstream does
not list, e.g. claude-... or gpt-...; default deepseek-v4-1-flash-uncensored).

Run: python3 venice_adapters.py        Self-check: python3 test_venice_adapters.py
"""
import http.client
import http.server
import json
import os
import sys
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

HOST = os.environ.get("VENICE_ADAPTER_HOST", "127.0.0.1")
PORT = int(os.environ.get("VENICE_ADAPTER_PORT", "18892"))
UPSTREAM = os.environ.get("VENICE_ADAPTER_UPSTREAM", "http://127.0.0.1:18891/v1").rstrip("/")
DEFAULT_MODEL = os.environ.get("VENICE_ADAPTER_MODEL", "deepseek-v4-1-flash-uncensored")
_MODELS: Tuple[float, List[str]] = (0.0, [])


def log(msg: str) -> None:
    sys.stderr.write("[venice-adapters] %s\n" % msg)


def _upstream(method: str, path: str, body: Optional[bytes] = None) -> Tuple[http.client.HTTPConnection, Any]:
    # http.client, not urlopen: urlopen would go through the macOS system proxy
    u = urlsplit(UPSTREAM)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=900)
    conn.request(method, u.path + path, body, {"Content-Type": "application/json", "Authorization": "Bearer local"})
    return conn, conn.getresponse()


def _pick_model(asked: Any) -> str:
    """The asked model when the upstream lists it, else the default (claude-* and gpt-* are not Venice ids)."""
    global _MODELS
    if time.time() - _MODELS[0] > 300:
        try:
            conn, resp = _upstream("GET", "/models")
            _MODELS = (time.time(), [m.get("id") for m in json.loads(resp.read()).get("data", [])])
            conn.close()
        except Exception as exc:  # the upstream being down is reported by the real request
            log("model list failed: %r" % exc)
    # keep a per-request web suffix (deepseek-...:web / :xsearch) when the BASE model is valid, so the
    # bridge can parse it; a bare valid id passes through; anything else falls back to the default.
    if isinstance(asked, str):
        if "/" in asked.split(":")[0]:
            return asked  # a local backend id (ollama/...): let the bridge route it or 502, never silently Venice
        if asked in _MODELS[1]:
            return asked
        if asked.split(":")[0] in _MODELS[1]:
            return asked
    return DEFAULT_MODEL


def _text(content: Any) -> str:
    """Plain text of a content value: a string, or a list of blocks with a text field."""
    if isinstance(content, str):
        return content
    out = []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, str):
            out.append(b)
        elif isinstance(b, dict) and isinstance(b.get("text"), str):
            out.append(b["text"])
        elif isinstance(b, dict) and b.get("type") in ("image", "input_image", "document"):
            out.append("[%s omitted: this server takes text only]" % b["type"])
    return "\n".join(out)


def _call(call_id: str, name: str, arguments: Any) -> Dict[str, Any]:
    args = arguments if isinstance(arguments, str) else json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


# ---------------------------------------------------------------- Anthropic Messages -> chat

def messages_to_chat(body: Dict[str, Any]) -> Dict[str, Any]:
    msgs: List[Dict[str, Any]] = []
    system = _text(body.get("system"))
    if system:
        msgs.append({"role": "system", "content": system})
    for m in body.get("messages") or []:
        role, content = m.get("role"), m.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
        if role == "assistant":
            calls = [_call(b.get("id") or "call_" + uuid.uuid4().hex[:12], b.get("name", ""), b.get("input"))
                     for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
            msg: Dict[str, Any] = {"role": "assistant", "content": _text(
                [b for b in blocks if isinstance(b, dict) and b.get("type") == "text"]) or None}
            if calls:
                msg["tool_calls"] = calls
            msgs.append(msg)
        else:
            # results first: they must follow the assistant message that made the calls
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    text = _text(b.get("content"))
                    msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id", ""),
                                 "content": ("[error] " + text) if b.get("is_error") else text})
            rest = _text([b for b in blocks if not (isinstance(b, dict) and b.get("type") == "tool_result")])
            if rest:
                msgs.append({"role": "user", "content": rest})
    tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                               "parameters": t["input_schema"]}}
             for t in body.get("tools") or [] if isinstance(t, dict) and t.get("name") and isinstance(t.get("input_schema"), dict)]
    out: Dict[str, Any] = {"model": _pick_model(body.get("model")), "messages": msgs, "stream": bool(body.get("stream"))}
    if tools:
        out["tools"] = tools
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        tct = tc.get("type")
        if tct == "auto":
            out["tool_choice"] = "auto"
        elif tct == "any":
            out["tool_choice"] = "required"
        elif tct == "none":
            out["tool_choice"] = "none"
        elif tct == "tool" and tc.get("name"):
            out["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        if tc.get("disable_parallel_tool_use") is True:
            out["parallel_tool_calls"] = False
    if isinstance(body.get("max_tokens"), int):
        out["max_tokens"] = body["max_tokens"]
    if isinstance(body.get("stop_sequences"), list):
        stops = [s for s in body["stop_sequences"] if isinstance(s, str) and s]
        if stops:
            out["stop"] = stops
    return out


# ---------------------------------------------------------------- OpenAI Responses -> chat

def responses_to_chat(body: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """(chat request, {tool name: 'custom'} for freeform tools such as Codex's apply_patch)."""
    msgs: List[Dict[str, Any]] = []
    if isinstance(body.get("instructions"), str) and body["instructions"]:
        msgs.append({"role": "system", "content": body["instructions"]})
    items = body.get("input")
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]
    for it in items or []:
        if not isinstance(it, dict):
            continue
        kind = it.get("type") or ("message" if it.get("role") else "")
        if kind == "message":
            role = {"developer": "system"}.get(it.get("role"), it.get("role") or "user")
            text = _text(it.get("content"))
            if text:
                msgs.append({"role": role, "content": text})
        elif kind in ("function_call", "custom_tool_call"):
            args = it.get("arguments") if kind == "function_call" else {"input": it.get("input", "")}
            call = _call(it.get("call_id") or it.get("id") or "", it.get("name", ""), args)
            if msgs and msgs[-1]["role"] == "assistant":
                msgs[-1].setdefault("tool_calls", []).append(call)  # text + calls stay in one message
            else:
                msgs.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif kind in ("function_call_output", "custom_tool_call_output"):
            out = it.get("output")
            if isinstance(out, dict):
                out = out.get("content") if "content" in out else json.dumps(out, ensure_ascii=False)
            msgs.append({"role": "tool", "tool_call_id": it.get("call_id", ""), "content": _text(out) if not isinstance(out, str) else out})
        # reasoning items, web_search_call, local_shell_call ...: nothing a chat server can use
    custom: Dict[str, str] = {}
    tools = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        if t.get("type") == "function":
            tools.append({"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""),
                "parameters": t.get("parameters") if isinstance(t.get("parameters"), dict) else {"type": "object", "properties": {}}}})
        elif t.get("type") == "custom":  # freeform tool: one text argument
            custom[t["name"]] = "custom"
            tools.append({"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""),
                "parameters": {"type": "object", "properties": {"input": {"type": "string", "description": "The full text input of the tool."}},
                               "required": ["input"], "additionalProperties": False}}})
    out_req: Dict[str, Any] = {"model": _pick_model(body.get("model")), "messages": msgs, "stream": bool(body.get("stream"))}
    if tools:
        out_req["tools"] = tools
    rtc = body.get("tool_choice")
    if isinstance(rtc, str) and rtc in ("auto", "required", "none"):
        out_req["tool_choice"] = rtc
    elif isinstance(rtc, dict) and rtc.get("type") == "function" and rtc.get("name"):
        out_req["tool_choice"] = {"type": "function", "function": {"name": rtc["name"]}}
    if isinstance(body.get("parallel_tool_calls"), bool):
        out_req["parallel_tool_calls"] = body["parallel_tool_calls"]
    text_cfg = body.get("text")
    if isinstance(text_cfg, dict) and isinstance(text_cfg.get("format"), dict):
        fmt = text_cfg["format"]
        if fmt.get("type") == "json_object":
            out_req["response_format"] = {"type": "json_object"}
        elif fmt.get("type") == "json_schema":
            out_req["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": fmt.get("name") or "response",
                    "schema": fmt.get("schema") if isinstance(fmt.get("schema"), dict) else {},
                    "strict": bool(fmt.get("strict", False)),
                },
            }
    if isinstance(body.get("max_output_tokens"), int):
        out_req["max_tokens"] = body["max_output_tokens"]
    return out_req, custom


# ---------------------------------------------------------------- reading the upstream answer

def _chat_events(resp: Any, stream: bool) -> Iterator[Tuple[str, Any]]:
    """('reasoning', str) and ('text', str) as they arrive, then ('calls', [{id,name,arguments}]) and ('end', {finish, stop_sequence, usage})."""
    calls: Dict[int, Dict[str, str]] = {}
    finish, stop_seq, usage = "stop", None, {}

    def take(choice: Dict[str, Any], part: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        nonlocal finish, stop_seq
        for tc in part.get("tool_calls") or []:
            slot = calls.setdefault(tc.get("index", len(calls)), {"id": "", "name": "", "arguments": ""})
            fn = tc.get("function") or {}
            slot["id"] = tc.get("id") or slot["id"]
            slot["name"] = fn.get("name") or slot["name"]
            slot["arguments"] += fn.get("arguments") or ""
        finish = choice.get("finish_reason") or finish
        if choice.get("stop_sequence"):
            stop_seq = choice["stop_sequence"]
        reasoning = part.get("reasoning_content") if isinstance(part.get("reasoning_content"), str) and part["reasoning_content"] else None
        text = part.get("content") if isinstance(part.get("content"), str) and part["content"] else None
        return reasoning, text

    if not stream:
        data = json.loads(resp.read())
        usage = data.get("usage") or {}
        for choice in data.get("choices") or []:
            reasoning, text = take(choice, choice.get("message") or {})
            if reasoning:
                yield "reasoning", reasoning
            if text:
                yield "text", text
    else:
        buf = b""
        for chunk in iter(lambda: resp.read1(65536), b""):
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                    continue
                try:
                    data = json.loads(line[5:])
                except ValueError:
                    continue
                usage = data.get("usage") or usage
                for choice in data.get("choices") or []:
                    reasoning, text = take(choice, choice.get("delta") or {})
                    if reasoning:
                        yield "reasoning", reasoning
                    if text:
                        yield "text", text
    done = [dict(c, id=c["id"] or "call_" + uuid.uuid4().hex[:12]) for _, c in sorted(calls.items()) if c["name"]]
    if done:
        yield "calls", done
    yield "end", {"finish": "tool_calls" if done else finish, "stop_sequence": stop_seq, "usage": usage}


def _json_args(s: str) -> Any:
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {"value": v}
    except ValueError:
        return {"_raw": s}


# ---------------------------------------------------------------- HTTP

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log(fmt % args)

    def _json(self, status: int, obj: Any) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _sse(self, event: str, data: Dict[str, Any]) -> None:
        self.wfile.write(("event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))).encode("utf-8"))
        self.wfile.flush()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._json(200, {"ok": True, "upstream": UPSTREAM, "default_model": DEFAULT_MODEL})
        if path == "/v1/models":
            try:
                conn, resp = _upstream("GET", "/models")
                data = json.loads(resp.read())
                conn.close()
                return self._json(resp.status, data)
            except Exception as exc:
                return self._json(502, {"error": {"type": "api_error", "message": "upstream did not answer: %r" % exc}})
        self._json(404, {"error": {"type": "not_found_error", "message": "unknown path " + path}})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]  # Claude Code adds ?beta=true
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "body is not JSON"}})
        if os.environ.get("VENICE_ADAPTER_DUMP"):  # debugging: keep what the client sent (local, never commit)
            name = "%s_%s.json" % (time.strftime("%H%M%S"), path.strip("/").replace("/", "_"))
            with open(os.path.join(os.environ["VENICE_ADAPTER_DUMP"], name), "w") as f:
                json.dump(body, f, ensure_ascii=False)
        if path == "/v1/messages/count_tokens":
            return self._json(200, {"input_tokens": len(json.dumps(body, ensure_ascii=False)) // 4})
        if path == "/v1/messages":
            return self._run(body, messages_to_chat(body), {}, anthropic=True)
        if path == "/v1/responses":
            chat, custom = responses_to_chat(body)
            return self._run(body, chat, custom, anthropic=False)
        self._json(404, {"error": {"type": "not_found_error", "message": "unknown path " + path}})

    def _run(self, body: Dict[str, Any], chat: Dict[str, Any], custom: Dict[str, str], anthropic: bool) -> None:
        started = time.time()
        try:
            conn, resp = _upstream("POST", "/chat/completions", json.dumps(chat, ensure_ascii=False).encode("utf-8"))
        except Exception as exc:
            return self._error(502, "upstream did not answer: %r" % exc, anthropic)
        try:
            if resp.status != 200:  # before any header of ours: the client must see the real status
                raw = resp.read().decode("utf-8", "replace")
                try:
                    message = json.loads(raw)["error"]["message"]
                except Exception:
                    message = raw[:500]
                return self._error(resp.status, message, anthropic)
            events = _chat_events(resp, bool(chat.get("stream")))
            (self._anthropic_out if anthropic else self._responses_out)(body, chat, custom, events)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()
            log("%s model=%s stream=%s tools=%d msgs=%d in %.1fs" % (
                "messages" if anthropic else "responses", chat["model"], chat.get("stream"),
                len(chat.get("tools") or []), len(chat["messages"]), time.time() - started))

    def _error(self, status: int, message: str, anthropic: bool) -> None:
        kind = {429: "rate_limit_error", 401: "authentication_error", 400: "invalid_request_error"}.get(status, "api_error")
        if anthropic:
            self._json(status, {"type": "error", "error": {"type": kind, "message": message}})
        else:
            self._json(status, {"error": {"type": kind, "message": message, "code": None, "param": None}})

    # ---- Anthropic Messages out
    def _anthropic_out(self, body: Dict[str, Any], chat: Dict[str, Any], custom: Dict[str, str], events: Iterator[Tuple[str, Any]]) -> None:
        mid, stream = "msg_" + uuid.uuid4().hex[:24], bool(chat.get("stream"))
        blocks: List[Dict[str, Any]] = []
        end: Dict[str, Any] = {"finish": "stop", "usage": {}}
        if stream:
            self._sse_start()
            self._sse("message_start", {"type": "message_start", "message": {
                "id": mid, "type": "message", "role": "assistant", "model": body.get("model") or chat["model"], "content": [],
                "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
        text_open = False
        thinking_open = False

        def close_open() -> None:
            nonlocal text_open, thinking_open
            if (text_open or thinking_open) and stream:
                self._sse("content_block_stop", {"type": "content_block_stop", "index": len(blocks) - 1})
            text_open = False
            thinking_open = False

        for kind, value in events:
            if kind == "reasoning":
                if text_open:
                    close_open()
                if not thinking_open:
                    blocks.append({"type": "thinking", "thinking": ""})
                    thinking_open = True
                    if stream:
                        self._sse("content_block_start", {"type": "content_block_start", "index": len(blocks) - 1,
                                                          "content_block": {"type": "thinking", "thinking": ""}})
                blocks[-1]["thinking"] += value
                if stream:
                    self._sse("content_block_delta", {"type": "content_block_delta", "index": len(blocks) - 1,
                                                      "delta": {"type": "thinking_delta", "thinking": value}})
            elif kind == "text":
                if thinking_open:
                    close_open()
                if not text_open:
                    blocks.append({"type": "text", "text": ""})
                    text_open = True
                    if stream:
                        self._sse("content_block_start", {"type": "content_block_start", "index": len(blocks) - 1,
                                                          "content_block": {"type": "text", "text": ""}})
                blocks[-1]["text"] += value
                if stream:
                    self._sse("content_block_delta", {"type": "content_block_delta", "index": len(blocks) - 1,
                                                      "delta": {"type": "text_delta", "text": value}})
            elif kind == "calls":
                close_open()
                for c in value:
                    blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": _json_args(c["arguments"])})
                    if stream:
                        i = len(blocks) - 1
                        self._sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                            "type": "tool_use", "id": c["id"], "name": c["name"], "input": {}}})
                        self._sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                            "type": "input_json_delta", "partial_json": json.dumps(blocks[-1]["input"], ensure_ascii=False)}})
                        self._sse("content_block_stop", {"type": "content_block_stop", "index": i})
            else:
                end = value
        stop_seq = end.get("stop_sequence")
        stop = "stop_sequence" if stop_seq else {"tool_calls": "tool_use", "length": "max_tokens"}.get(end["finish"], "end_turn")
        usage = {"input_tokens": int(end["usage"].get("prompt_tokens") or 0), "output_tokens": int(end["usage"].get("completion_tokens") or 0)}
        if stream:
            close_open()
            self._sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": stop_seq}, "usage": usage})
            self._sse("message_stop", {"type": "message_stop"})
        else:
            self._json(200, {"id": mid, "type": "message", "role": "assistant", "model": body.get("model") or chat["model"],
                             "content": blocks or [{"type": "text", "text": ""}], "stop_reason": stop, "stop_sequence": stop_seq, "usage": usage})

    # ---- OpenAI Responses out
    def _responses_out(self, body: Dict[str, Any], chat: Dict[str, Any], custom: Dict[str, str], events: Iterator[Tuple[str, Any]]) -> None:
        rid, stream, seq = "resp_" + uuid.uuid4().hex[:24], bool(chat.get("stream")), [0]
        base = {"id": rid, "object": "response", "created_at": int(time.time()), "model": body.get("model") or chat["model"],
                "status": "in_progress", "output": [], "tools": body.get("tools") or [],
                "parallel_tool_calls": bool(body.get("parallel_tool_calls", True))}

        def emit(event: str, data: Dict[str, Any]) -> None:
            if stream:
                seq[0] += 1
                self._sse(event, dict(data, type=event, sequence_number=seq[0]))

        if stream:
            self._sse_start()
            emit("response.created", {"response": base})
            emit("response.in_progress", {"response": base})
        output: List[Dict[str, Any]] = []
        msg: Optional[Dict[str, Any]] = None
        end: Dict[str, Any] = {"finish": "stop", "usage": {}}

        def close_msg() -> None:
            nonlocal msg
            if msg:
                i = len(output) - 1
                part = msg["content"][0]
                emit("response.output_text.done", {"item_id": msg["id"], "output_index": i, "content_index": 0, "text": part["text"]})
                emit("response.content_part.done", {"item_id": msg["id"], "output_index": i, "content_index": 0, "part": part})
                msg["status"] = "completed"
                emit("response.output_item.done", {"output_index": i, "item": msg})
                msg = None

        for kind, value in events:
            if kind == "text":
                if not msg:
                    msg = {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "status": "in_progress",
                           "content": [{"type": "output_text", "text": "", "annotations": []}]}
                    output.append(msg)
                    emit("response.output_item.added", {"output_index": len(output) - 1, "item": dict(msg, content=[])})
                    emit("response.content_part.added", {"item_id": msg["id"], "output_index": len(output) - 1, "content_index": 0,
                                                         "part": {"type": "output_text", "text": "", "annotations": []}})
                msg["content"][0]["text"] += value
                emit("response.output_text.delta", {"item_id": msg["id"], "output_index": len(output) - 1, "content_index": 0, "delta": value})
            elif kind == "calls":
                close_msg()
                for c in value:
                    if c["name"] in custom:  # freeform tool: hand the text back, not a JSON object
                        args = _json_args(c["arguments"])
                        item = {"id": "ctc_" + uuid.uuid4().hex[:24], "type": "custom_tool_call", "status": "completed",
                                "call_id": c["id"], "name": c["name"],
                                "input": args.get("input") if isinstance(args.get("input"), str) else c["arguments"]}
                    else:
                        item = {"id": "fc_" + uuid.uuid4().hex[:24], "type": "function_call", "status": "completed",
                                "call_id": c["id"], "name": c["name"], "arguments": c["arguments"] or "{}"}
                    output.append(item)
                    i = len(output) - 1
                    emit("response.output_item.added", {"output_index": i, "item": dict(item, status="in_progress")})
                    if item["type"] == "function_call":
                        emit("response.function_call_arguments.delta", {"item_id": item["id"], "output_index": i, "delta": item["arguments"]})
                        emit("response.function_call_arguments.done", {"item_id": item["id"], "output_index": i, "arguments": item["arguments"]})
                    emit("response.output_item.done", {"output_index": i, "item": item})
            else:
                end = value
        close_msg()
        pt, ct = int(end["usage"].get("prompt_tokens") or 0), int(end["usage"].get("completion_tokens") or 0)
        final = dict(base, status="completed", output=output, usage={
            "input_tokens": pt, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": ct,
            "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": pt + ct})
        if stream:
            emit("response.completed", {"response": final})  # Codex treats a stream without it as broken
        else:
            self._json(200, final)


def main() -> None:
    server = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    log("listening on http://%s:%d  ->  %s  (default model %s)" % (HOST, PORT, UPSTREAM, DEFAULT_MODEL))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
