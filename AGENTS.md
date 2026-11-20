# AGENTS.md — venice-bridge

Contract for coding agents (Claude Code, Codex, Gemini CLI, OpenCode, Cursor, Antigravity) that use this tool or
change this repository. Read it before you run, configure, or edit anything here.

## What it is

venice-bridge is a free, open-source local bridge for macOS that lets OpenCode, Claude Code, Codex CLI and other OpenAI- or Anthropic-compatible coding agents use a venice.ai web subscription, with working tool calls.

- **Bridge** (`venice_bridge.py`) on `127.0.0.1:18891`, speaking OpenAI Chat Completions. Upstream is the
  venice.ai web app's own backend (`clerk.venice.ai` for login, `outerface.venice.ai` for chat), authenticated with
  the user's browser login. It is not the paid Venice API and needs no API key.
- **Adapter** (`venice_adapters.py`, optional, standard library only) on `127.0.0.1:18892`, speaking Anthropic
  Messages (Claude Code) and OpenAI Responses (Codex CLI). It only translates and forwards to the bridge; nothing
  about Venice lives in it.
- **Launcher** (`claude-venice`, bash) runs the Claude Code CLI against the adapter in an isolated profile.
- `VENICE_BRIDGE_ROUTE` picks the Venice route: `auto` (default) uses the chat route `/api/inference/chat` and
  sends a model it does not know (`400 modelNotFound`) to the agent route `/api/inference/chat/agents/ad-hoc`;
  `chat` and `agent` force one route. The chat route has no Venice agent loop and no Venice tools, so no call is
  swallowed; the client's system prompt travels in the first user message. The log line `timing open: … route=`
  shows the route.
- A model id `<backend>/<model>` (default backends `ollama`, `lmstudio`) is passed straight to that local
  OpenAI-compatible server: no Venice login, no tool emulation, no quota.
- Tested on a paid Venice web plan (Pro). The agent route needs a paid plan (a free account gets
  `403 creator_access_required`); a free account has a very small chat quota.
- Unofficial; the endpoints are undocumented and may change. Using it may be against Venice's terms of service.
- No images. Venice's fast models are weak at long agentic chains: do not trust their output on high-stakes work.
- Cost is a daily **request** quota, not tokens. `usage` numbers are estimates (about 4 characters per token).

## Start, stop, check

| Action | Command |
|---|---|
| Install the dependency | `pip install curl_cffi` (or `pip install -r requirements.txt`); the adapter needs nothing extra |
| Start the bridge | `python3 venice_bridge.py` (options: `--host`, `--port`; default `127.0.0.1:18891`) |
| Start the adapter (Claude Code, Codex) | `python3 venice_adapters.py` (environment only, no options; default `127.0.0.1:18892`) |
| Stop a foreground run | Ctrl-C |
| Check the bridge | `curl -s http://127.0.0.1:18891/health` → `200` with `"ok": true`, `"venice_session": "active"`, `chat_quota`, `cache` |
| Check the adapter | `curl -s http://127.0.0.1:18892/health` → `200` `{"ok": true, "upstream": ..., "default_model": ...}` (does not probe the bridge) |
| List models | `curl -s http://127.0.0.1:18891/v1/models` (`venice-auto` first, then the Venice models the web subscription covers, plus `<backend>/<id>` of reachable local backends) |
| Drop cached auth after a new cookie | `curl -s -X POST http://127.0.0.1:18891/reload` |
| Autostart (launchd) | Fill in `examples/com.example.venice-bridge.plist` and `examples/com.example.venice-adapters.plist`, copy to `~/Library/LaunchAgents/`, `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<file>` |
| Stop under launchd | `launchctl bootout gui/$(id -u)/com.example.venice-bridge` (and `…/com.example.venice-adapters`); the plists set `KeepAlive`, so killing the process restarts it |

Before starting a second instance, check `/health`: the port may already be in use by a running process.

The only setup step is the login cookie: the value of the `__client` cookie of `clerk.venice.ai`, saved to
`~/.config/venice-bridge/client-cookie.txt` (mode 0600). Only the user can obtain it from their browser. If it is
missing, ask the user to follow README "Quick start" step 1; never try to obtain it yourself.

## Clients

| Client | How |
|---|---|
| OpenCode | Provider block from `examples/opencode.json`, then `opencode -m venice-bridge/<model-id>`. |
| Any OpenAI Chat Completions client | Base URL `http://127.0.0.1:18891/v1`, any API key. |
| Docker container | `http://host.docker.internal:18891/v1` reaches a bridge bound to `127.0.0.1` (measured with Docker Desktop on macOS). Do not bind to `0.0.0.0`. |
| Claude Code | `claude-venice` (needs bridge and adapter). See below. |
| Codex CLI | `VB_KEY=local codex exec --skip-git-repo-check -c 'model_providers.vb={name="vb",base_url="http://127.0.0.1:18892/v1",wire_api="responses",env_key="VB_KEY"}' -c model_provider=vb -m deepseek-v4-1-flash-uncensored "<task>"` (writes nothing to `~/.codex`; measured with Codex CLI 0.163.0). Namespace tools (MCP, multi-agent) and Codex's built-in `web_search` do not pass through. |

### `claude-venice`

- Usage: `claude-venice` · `claude-venice -m <venice-model-id>` · `VENICE_CLAUDE_MODEL=<id> claude-venice` ·
  `claude-venice --models` · `claude-venice --quota`. `-m`, `--models` and `--quota` must be the first argument;
  everything else goes to `claude`.
- Sets `CLAUDE_CONFIG_DIR=~/.claude-venice`, unsets `ANTHROPIC_API_KEY`, sets `ANTHROPIC_BASE_URL=http://127.0.0.1:18892`,
  a filler `ANTHROPIC_AUTH_TOKEN`, and points `ANTHROPIC_MODEL` and the opus/sonnet/haiku default-model variables
  at the chosen id. The user's normal `claude` profile is not touched.
- Preflight: exits 1 with a message if the bridge or adapter `/health` does not answer, or the Venice session is
  not active; warns when fewer than 500 requests are left. Ports `18891` and `18892` are fixed in the script.
- **It adds `--dangerously-skip-permissions` by default, so Claude Code runs tools without asking.** Reason:
  Venice's fast models cannot drive Claude Code's auto-mode safety classifier ("Auto mode could not evaluate this
  action and is blocking it for safety"). Opt out with `VENICE_CLAUDE_SKIP_PERMS=0` or by passing any
  `--permission-mode`. Never start it on the user's behalf in a folder they have not chosen, and tell the user
  that prompts are off.
- `--quota` prints two lines and exits 0 even when the bridge is down (it then prints `could not read …/health`).

## Endpoints

### Bridge, `127.0.0.1:18891`

| Method and path | Success | Errors |
|---|---|---|
| `POST /v1/chat/completions` | `200` JSON, or `200` `text/event-stream` when `"stream": true` | `400` invalid JSON; `400` `context_length_exceeded` (Venice 413); `402` plan or credits; `403` `venice_quota_exhausted`; `401`/`403` Venice refusal (for example `creator_access_required`); `429` `venice_rate_limited`; `502` `venice_upstream_error`, `bridge_error` or `local_backend_error` |
| `GET /v1/models` | `200` `{"object":"list","data":[...]}`: `venice-auto` first, then Venice models without credits (never `e2ee-*`, minus `VENICE_EXCLUDE_MODELS` globs), then local models (local models alone when Venice is unreachable). Hidden models can still be requested by id. | `502` `bridge_error` when nothing can be listed |
| `GET /health` | `200` `{"ok":true,"venice_session":"active","chat_quota":{"remaining","resets_in_min",...},"cache":{"hits","misses","stores","entries","enabled"}}` | `503` `{"ok":false,"error":"..."}` |
| `POST /reload` | `200` `{"ok":true}` | — |
| any other path | — | `404` |

Upstream and bridge errors use the OpenAI shape `{"error":{"message","type","code"}}`; the `404` and
invalid-JSON `400` bodies carry only `message`. The HTTP status and `error.code` can differ on purpose:

- Quota used up: HTTP `403`, `type` `venice_quota_exhausted`, `code` `429`. It is a 403 because OpenCode retries a
  429 silently. While the quota is known to be used up, the bridge answers locally without calling Venice.
- Cloudflare challenge: HTTP `502`, `type` `venice_upstream_error`, `code` `403`.
- In a stream that has already started, a failure is sent as a final `data: {"error":{...}}` event without a
  normal ending; failures before the status line are a `502`, which OpenCode retries.
- A local-prefixed model whose backend is down: `502` `local_backend_error`; it never falls through to Venice.

A request without `model` uses `qwen3-coder-480b-a35b-instruct-turbo`. A `venice-bridge/` prefix on the model id
is stripped. A suffix `:web` (web search and URL scraping) or `:xsearch` (X search), combinable, turns on Venice
web search for that request on the chat route and is removed before the id reaches Venice; off by default.

Other request behaviour on the chat route: `temperature` and `top_p` are forwarded when set; `max_tokens` is
ignored. A byte-identical repeat request (stream flag aside) is answered from the in-memory response cache without
a Venice call; local-backend requests are not cached.

Reasoning is off by default. A thinking turn is made only when asked for (model suffix `:think`, aliases `:reason`
/ `:reasoning`, combinable with `:web`; `VENICE_REASONING=1`; or `reasoning_effort` `medium` / `high`) **and** the
request declares no tools. The chain of thought comes back as `reasoning_content` (message field, or
`delta.reasoning_content` chunks before `content` when streaming). Chat route only. With tools, `:think` and
`VENICE_REASONING` are ignored: reasoning models want `reasoning_content` passed back on the next tool turn, which
agents do not do (HTTP 400). So `:think` has no effect in OpenCode / Claude Code / Codex agent loops. A
`reasoning_effort` above `low` makes no thinking turn with tools but still sets Venice's reasoning switch; do not
set it there.

`venice-auto` (virtual model) resolves to the first Venice id in `VENICE_AUTO_MODELS`; while the bridge knows the
web quota is used up, to the first local `<backend>/<model>` entry, until the reset Venice reports. The request
that hits the limit still fails once with `403` `venice_quota_exhausted`; the local backend must be running
(else `502`); a suffix is kept for the Venice model and dropped for the local one; `POST /reload` clears the
over-quota state. Use: `opencode -m venice-bridge/venice-auto` (list it in the OpenCode provider's `models`),
`claude-venice -m venice-auto`.

### Adapter, `127.0.0.1:18892`

| Method and path | Success | Errors |
|---|---|---|
| `POST /v1/messages` | `200` Anthropic Messages JSON or SSE | upstream status passed through; `400` body not JSON; `502` the bridge did not answer. Body `{"type":"error","error":{"type","message"}}` |
| `POST /v1/messages/count_tokens` | `200` `{"input_tokens": <request size / 4>}` (estimate) | — |
| `POST /v1/responses` | `200` OpenAI Responses JSON or SSE (ends with `response.completed`) | as above; body `{"error":{"type","message","code":null,"param":null}}` |
| `GET /v1/models` | the upstream's list, passed through | `502` |
| `GET /health` | `200` `{"ok":true,"upstream":"...","default_model":"..."}` | — |
| any other path | — | `404` |

The adapter forwards tool declarations, tool calls and results, `max_tokens` (`max_output_tokens` for Responses)
and the stream flag. It does not forward `temperature`, `top_p` or a reasoning effort. Freeform Responses tools
(such as `apply_patch`) become a function with one `input` string. A model id the upstream does not list (checked
against a model list refreshed every 5 minutes) is replaced with `VENICE_ADAPTER_MODEL`; a `:web` / `:xsearch`
suffix is kept when the base id is listed. Consequence: a local-prefixed model whose backend was down at the last
list read goes to the Venice default model instead of failing. Images and documents are replaced by a short
"omitted" note. `usage` reports `cached_tokens: 0`.

## Environment variables

Read once at start; set them before starting the process.

### Bridge

| Variable | Default | Meaning |
|---|---|---|
| `VENICE_BRIDGE_HOST` | `127.0.0.1` | Bind address (`--host` overrides). |
| `VENICE_BRIDGE_ROUTE` | `auto` | `auto`: chat route, agent route for a model the chat route does not know; `chat`: chat route only; `agent`: agent route only (needs a paid plan). |
| `VENICE_BRIDGE_COOKIE_FILE` | `~/.config/venice-bridge/client-cookie.txt` | Cookie file (bare value or `__client=...;`). |
| `VENICE_BRIDGE_SESSION_DIR` | empty | Folder with a `client.py` exposing `get_session(domain)` → `{"timestamp": <ms>, "tokens": {"clerk_client": "<value>"}}`. Used when the cookie file is missing or empty. |
| `VENICE_BRIDGE_CLIENT_FILE` | `~/.config/venice-bridge/rotated-client.json` | Rotated cookie, written with mode 0600. |
| `VENICE_BRIDGE_BACKENDS` | `~/.config/venice-bridge/backends.json` | Local backends: `{"backends":[{"name","base_url","api_key"}]}`. A file replaces the defaults (`ollama` → `http://127.0.0.1:11434/v1`, `lmstudio` → `http://127.0.0.1:1234/v1`); an empty list turns them off. Read per request. Example: `examples/backends.json`. |
| `VENICE_WEB` | off | `1`: Venice web search and URL scraping on every chat-route request. |
| `VENICE_X_SEARCH` | off | `1`: Venice X search on every chat-route request. |
| `VENICE_REASONING` | off | `1`: reasoning on every request that declares no tools. |
| `VENICE_EXCLUDE_MODELS` | empty | Comma-separated globs hidden from `GET /v1/models` (credit models and `e2ee-*` are always hidden). |
| `VENICE_AUTO_MODELS` | `deepseek-v4-1-flash-uncensored,ollama/qwen3:8b` | `venice-auto` candidates: first Venice id normally, first local `<backend>/<model>` while over quota. |
| `VENICE_BRIDGE_CACHE` | on | `0` disables the response cache. |
| `VENICE_BRIDGE_CACHE_MAX` | `256` | Cache entries (LRU). |
| `VENICE_BRIDGE_CACHE_TTL` | `3600` | Cache entry lifetime, seconds. |
| `VENICE_BRIDGE_FORMAT_RETRIES` | `2` | In-bridge retries: server loop, unreadable call, empty reply, broken stream. |
| `VENICE_BRIDGE_JUDGE` | on | `0` disables the early-stop judge. |
| `VENICE_BRIDGE_JUDGE_MODEL` | `qwen3-5-35b-a3b` | Judge model. |
| `VENICE_BRIDGE_JUDGE_RETRIES` | `1` | Retries the judge may add per turn. |

### Adapter

| Variable | Default | Meaning |
|---|---|---|
| `VENICE_ADAPTER_HOST` | `127.0.0.1` | Bind address. |
| `VENICE_ADAPTER_PORT` | `18892` | Port. |
| `VENICE_ADAPTER_UPSTREAM` | `http://127.0.0.1:18891/v1` | Chat Completions server to forward to. |
| `VENICE_ADAPTER_MODEL` | `deepseek-v4-1-flash-uncensored` | Model used when the client asks for one the upstream does not list (`claude-...`, `gpt-...`). |
| `VENICE_ADAPTER_DUMP` | empty | An existing folder: every request body is written there as JSON. Debugging only; bodies contain prompts and file contents. |

### Launcher

| Variable | Default | Meaning |
|---|---|---|
| `VENICE_CLAUDE_MODEL` | `deepseek-v4-1-flash-uncensored` | Venice model id (`-m` overrides). |
| `VENICE_CLAUDE_SKIP_PERMS` | `1` | `0`: do not add `--dangerously-skip-permissions`. |

## Tests

```bash
python3 test_venice_bridge.py
python3 test_venice_adapters.py
```

Offline: temporary files and fake servers on localhost; no running bridge needed. Success is exit status 0 and
these lines, in order:

```text
cookie file ok
session ok
ok
end to end ok
local backends ok
```

```text
messages ok
responses ok
```

Any `AssertionError` or non-zero exit is a failure. Run `test_venice_bridge.py` after every change to
`venice_bridge.py` or `tolerant_parser.py`, and `test_venice_adapters.py` after every change to
`venice_adapters.py`. After editing `claude-venice`, run `bash -n claude-venice`.

Live check of a running setup: `claude-venice --quota` prints `Venice web quota  remaining=… resets_in_min=…` and
`Bridge cache      hits=… misses=… entries=… enabled=…`.

## Files

| File | Owns |
|---|---|
| `venice_bridge.py` | HTTP server, Clerk login and cookie rotation, attestation, route choice (chat / agent), OpenAI ↔ Venice translation, chat-route settings, opt-in web search, local-backend passthrough, response cache, tool emulation prompt, native and tagged call readers, tool routing and argument fitting, retries, judge, stream gate, error mapping. |
| `venice_adapters.py` | Anthropic Messages and OpenAI Responses ↔ Chat Completions translation, model fallback, stream events. Standard library only. |
| `claude-venice` | Bash launcher for Claude Code on the adapter (isolated profile, preflight, permission default). |
| `tolerant_parser.py` | Reading tool calls from model text (lenient JSON, envelopes, broken replies). Port of opencode-venice-web, MIT. |
| `test_venice_bridge.py`, `test_venice_adapters.py` | Offline self-checks. |
| `test_fixtures_real_replies.json` | Real malformed model replies (from opencode-venice-web, MIT). |
| `test_fixture_tagged_call.json` | Fixture for the `<tool_calls>...</tool_calls>` reader. |
| `docs/TOOL_ROUTING.md` | Tool-name and argument-name mapping reference. |
| `examples/opencode.json` | OpenCode provider block with the example models. |
| `examples/backends.json` | Local backends file (the two defaults plus llama.cpp). |
| `examples/com.example.venice-bridge.plist`, `examples/com.example.venice-adapters.plist` | launchd templates (replace `/PATH/TO`). |
| `NOTICE.md`, `THIRD_PARTY_LICENSE-opencode-venice-web.txt` | Third-party attribution. |
| `LICENSE` | MIT. |
| `pyproject.toml`, `requirements.txt` | Package metadata and the single dependency (`curl_cffi`). |

## Rules

1. **Never print, log, paste, or commit the cookie**, the rotated-client file, a session JWT, or an attestation
   token. Use placeholders such as `<paste the cookie value>` in examples. `client-cookie.txt` and
   `rotated-client.json` are in `.gitignore`; keep them there.
2. **Never point tests at the real Venice endpoints.** Tests use fake upstreams on localhost and temp files;
   keep `CLIENT_STATE_FILE` and `COOKIE_FILE` redirected to a temp dir in any new test.
3. **Tools mode never sends a partial tool call.** The status line and the first 200 characters are held until
   the reply can no longer become a tool call or a retry; only prose past that point streams live, and a tail
   that may still become a call is held back. Keep this property when changing the stream path, because it is
   what lets a failed turn reach OpenCode as a retryable 502.
4. **A reply counts only after its completion signal**: `response.completed` on the agent route, a clean end of
   the HTTP response (curl exit code 0) on the chat route. Do not emit a cut stream as a normal answer.
5. **Keep the attribution** in `tolerant_parser.py`, `NOTICE.md`, and `THIRD_PARTY_LICENSE-opencode-venice-web.txt`.
6. **Bind to loopback only.** Neither process has client authentication; never start the bridge with a
   non-loopback `--host` or `VENICE_BRIDGE_HOST`, or the adapter with a non-loopback `VENICE_ADAPTER_HOST`. Docker
   containers use `host.docker.internal` instead.
7. **Never leave `VENICE_ADAPTER_DUMP` set** outside a debugging session, and never commit a dump folder.
8. **A local-prefixed model must never fall through to Venice in the bridge**: the route is chosen by the prefix
   alone, never by asking the backend.
9. **Respect the quota.** Every model step is one Venice request; do not loop on retries after a `403`
   `venice_quota_exhausted`.

## Common failures

| Symptom | What to do |
|---|---|
| `/health` → `503` "No venice.ai login found" | Ask the user to save the cookie (README Quick start step 1). |
| "Clerk ... rejected" / "token mint failed" | The cookie expired. Ask the user to log in on venice.ai and save a fresh cookie, then `POST /reload`. |
| `403 creator_access_required` | The agent route on a free account. Nothing to fix in code; the agent route needs a paid web plan. If `VENICE_BRIDGE_ROUTE=agent` is set, try `auto`. |
| `403 venice_quota_exhausted` | Wait for the reset shown in the message; do not loop on retries. Another Venice model will not help (the quota is plan-wide); `venice-auto` with a running local backend keeps working. |
| `502` "Cloudflare challenged the request" | Ask the user to open venice.ai in the browser and refresh the cookie file. |
| `400 context_length_exceeded` | Shorten or compact the conversation. |
| Model claims its tools are unavailable / "Tool not found" | The request used the agent route (check `route=` in the log), where Venice swallows native tool calls. Use `VENICE_BRIDGE_ROUTE=auto` or `chat`, or a model that exists on the chat route; in an existing conversation tell the model its tools work now, or start a new session. |
| `502` "Venice answered HTTP 400: … modelNotFound" with `VENICE_BRIDGE_ROUTE=chat` | The model exists only on the agent route. Use `auto`. |
| `502` `local_backend_error` | The local server for that prefix is down or misconfigured in `backends.json`. Start it; do not retry against Venice. |
| Local model through the adapter answered by a Venice model | The backend was down at the adapter's last model-list read. Start the backend and restart the adapter. |
| `400` on a tool turn with a reasoning effort set | Leave `reasoning_effort` unset or `low`. |
| `claude-venice` exits "… is down" / "session is not active" | Start the bridge and adapter / ask the user for a fresh cookie. |
| Claude Code "Auto mode could not evaluate this action" | Auto mode cannot work with these models; use the launcher default or a `--permission-mode <mode>` other than `auto`. |
| `Address already in use` on start | A process is already running on that port; check `/health` instead of starting another. |
