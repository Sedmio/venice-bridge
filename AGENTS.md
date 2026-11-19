# AGENTS.md — venice-bridge

Contract for coding agents (Claude Code, Codex, Gemini CLI, OpenCode, Cursor, Antigravity) that use this tool or
change this repository. Read it before you run, configure, or edit anything here.

## What it is

venice-bridge is a free, open-source local bridge for macOS that lets OpenCode and other OpenAI-compatible coding agents use a venice.ai web subscription, with working tool calls.

- One HTTP server (`venice_bridge.py`) on `127.0.0.1:18891`, speaking OpenAI Chat Completions.
- Upstream is the venice.ai web app's own backend (`clerk.venice.ai` for login,
  `outerface.venice.ai/api/inference/chat/agents/ad-hoc` for chat), authenticated with the user's browser login.
  It is not the paid Venice API and needs no API key.
- Needs a paid Venice web plan (Pro). A free account gets `403 creator_access_required`.
- Unofficial; the endpoints are undocumented and may change. Using it may be against Venice's terms of service.
- Does not implement Anthropic Messages (`/v1/messages`) or OpenAI Responses (`/v1/responses`). No images.

## Start, stop, check

| Action | Command |
|---|---|
| Install the dependency | `pip install curl_cffi` (or `pip install -r requirements.txt`) |
| Start in the foreground | `python3 venice_bridge.py` (options: `--host`, `--port`; default `127.0.0.1:18891`) |
| Stop a foreground run | Ctrl-C |
| Check | `curl -s http://127.0.0.1:18891/health` → `200` with `"ok": true` and `"venice_session": "active"` |
| List models | `curl -s http://127.0.0.1:18891/v1/models` |
| Drop cached auth after a new cookie | `curl -s -X POST http://127.0.0.1:18891/reload` |
| Autostart (launchd) | Fill in `examples/com.example.venice-bridge.plist`, copy to `~/Library/LaunchAgents/`, `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.venice-bridge.plist` |
| Stop under launchd | `launchctl bootout gui/$(id -u)/com.example.venice-bridge` (the plist sets `KeepAlive`, so killing the process restarts it) |

Before starting a second instance, check `/health`: the port may already be in use by a running bridge.

The only setup step is the login cookie: the value of the `__client` cookie of `clerk.venice.ai`, saved to
`~/.config/venice-bridge/client-cookie.txt` (mode 0600). Only the user can obtain it from their browser. If it is
missing, ask the user to follow README "Quick start" step 1; never try to obtain it yourself.

## Endpoints

| Method and path | Success | Errors |
|---|---|---|
| `POST /v1/chat/completions` | `200` JSON, or `200` `text/event-stream` when `"stream": true` | `400` invalid JSON; `400` `context_length_exceeded` (Venice 413); `402` plan or credits; `403` `venice_quota_exhausted`; `401`/`403` Venice refusal (for example `creator_access_required`); `429` `venice_rate_limited`; `502` `venice_upstream_error` or `bridge_error` |
| `GET /v1/models` | `200` `{"object":"list","data":[...]}` | `502` `bridge_error` |
| `GET /health` | `200` `{"ok":true,"venice_session":"active","chat_quota":{...}}` | `503` `{"ok":false,"error":"..."}` |
| `POST /reload` | `200` `{"ok":true}` | — |
| any other path | — | `404` |

Upstream and bridge errors use the OpenAI shape `{"error":{"message","type","code"}}`; the `404` and
invalid-JSON `400` bodies carry only `message`. The HTTP status and `error.code` can differ on purpose:

- Quota used up: HTTP `403`, `type` `venice_quota_exhausted`, `code` `429`. It is a 403 because OpenCode retries a
  429 silently. While the quota is known to be used up, the bridge answers locally without calling Venice.
- Cloudflare challenge: HTTP `502`, `type` `venice_upstream_error`, `code` `403`.
- In a stream that has already started, a failure is sent as a final `data: {"error":{...}}` event without a
  normal ending; failures before the status line are a `502`, which OpenCode retries.

A request without `model` uses `qwen3-coder-480b-a35b-instruct-turbo`. A `venice-bridge/` prefix on the model id
is stripped.

## Environment variables

Read once at start; set them before starting the bridge.

| Variable | Default | Meaning |
|---|---|---|
| `VENICE_BRIDGE_COOKIE_FILE` | `~/.config/venice-bridge/client-cookie.txt` | Cookie file (bare value or `__client=...;`). |
| `VENICE_BRIDGE_SESSION_DIR` | empty | Folder with a `client.py` exposing `get_session(domain)` → `{"timestamp": <ms>, "tokens": {"clerk_client": "<value>"}}`. Used when the cookie file is missing or empty. |
| `VENICE_BRIDGE_CLIENT_FILE` | `~/.config/venice-bridge/rotated-client.json` | Rotated cookie, written with mode 0600. |
| `VENICE_BRIDGE_FORMAT_RETRIES` | `2` | In-bridge retries: server loop, unreadable call, empty reply, broken stream. |
| `VENICE_BRIDGE_JUDGE` | on | `0` disables the early-stop judge. |
| `VENICE_BRIDGE_JUDGE_MODEL` | `qwen3-5-35b-a3b` | Judge model. |
| `VENICE_BRIDGE_JUDGE_RETRIES` | `1` | Retries the judge may add per turn. |

## Tests

```bash
python3 test_venice_bridge.py
```

Offline: temporary files and a fake Venice server on localhost. Success is exit status 0 and these lines:

```text
cookie file ok
session ok
ok
end to end ok
```

Any `AssertionError` or non-zero exit is a failure. Run the tests after every change to `venice_bridge.py` or
`tolerant_parser.py`.

## Files

| File | Owns |
|---|---|
| `venice_bridge.py` | HTTP server, Clerk login and cookie rotation, attestation, OpenAI ↔ Venice translation, tool emulation prompt, retries, judge, stream gate, error mapping. |
| `tolerant_parser.py` | Reading tool calls from model text (lenient JSON, envelopes, broken replies). Port of opencode-venice-web, MIT. |
| `test_venice_bridge.py` | Offline self-check. |
| `test_fixtures_real_replies.json` | Real malformed model replies (from opencode-venice-web, MIT). |
| `test_fixture_tagged_call.json` | Fixture for the `<tool_calls>...</tool_calls>` reader. |
| `examples/opencode.json` | OpenCode provider block with the example models. |
| `examples/com.example.venice-bridge.plist` | launchd template (replace `/PATH/TO`). |
| `NOTICE.md`, `THIRD_PARTY_LICENSE-opencode-venice-web.txt` | Third-party attribution. |
| `LICENSE` | MIT. |
| `pyproject.toml`, `requirements.txt` | Package metadata and the single dependency (`curl_cffi`). |

## Rules

1. **Never print, log, paste, or commit the cookie**, the rotated-client file, a session JWT, or an attestation
   token. Use placeholders such as `<paste the cookie value>` in examples. `client-cookie.txt` and
   `rotated-client.json` are in `.gitignore`; keep them there.
2. **Never point tests at the real Venice endpoints.** Tests use a fake upstream on localhost and temp files;
   keep `CLIENT_STATE_FILE` and `COOKIE_FILE` redirected to a temp dir in any new test.
3. **Tools mode never sends a partial tool call.** The status line and the first 200 characters are held until
   the reply can no longer become a tool call or a retry; only prose past that point streams live, and a tail
   that may still become a call is held back. Keep this property when changing the stream path, because it is
   what lets a failed turn reach OpenCode as a retryable 502.
4. **A reply counts only after `response.completed`.** Do not emit a cut stream as a normal answer.
5. **Keep the attribution** in `tolerant_parser.py`, `NOTICE.md`, and `THIRD_PARTY_LICENSE-opencode-venice-web.txt`.
6. **Bind to loopback only.** The bridge has no client authentication; never start it with a non-loopback
   `--host`.

## Common failures

| Symptom | What to do |
|---|---|
| `/health` → `503` "No venice.ai login found" | Ask the user to save the cookie (README Quick start step 1). |
| "Clerk ... rejected" / "token mint failed" | The cookie expired. Ask the user to log in on venice.ai and save a fresh cookie, then `POST /reload`. |
| `403 creator_access_required` | Free account. Nothing to fix in code; a paid web plan is required. |
| `403 venice_quota_exhausted` | Wait for the reset shown in the message; do not loop on retries. |
| `502` "Cloudflare challenged the request" | Ask the user to open venice.ai in the browser and refresh the cookie file. |
| `400 context_length_exceeded` | Shorten or compact the conversation. |
| Model claims its tools are unavailable | Venice swallowed a native tool call; the bridge retries within its budget. Resend, or switch to a model from the README benchmark table. |
| `Address already in use` on start | A bridge is already running; check `/health` instead of starting another. |
