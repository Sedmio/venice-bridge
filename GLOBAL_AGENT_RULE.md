# venice-bridge (global agent rule)

Paste this into your agent's global instructions so it knows the bridge exists and how to use it.

---

## venice-bridge — Venice web models through local OpenAI and Anthropic endpoints

venice-bridge is a local server on this Mac that exposes the user's venice.ai web subscription as an OpenAI
Chat Completions API at `http://127.0.0.1:18891/v1`, with tool calling. An optional adapter at
`http://127.0.0.1:18892` adds Anthropic Messages (Claude Code) and OpenAI Responses (Codex CLI). It is unofficial
and uses the venice.ai web app's undocumented backend with the user's own login; it needs no Venice API key. It
was tested on a paid Venice web plan (Pro); a free account has a very small chat quota. The quota counts
**requests**, not tokens.

**Check before use**

```bash
curl -s http://127.0.0.1:18891/health      # 200 {"ok":true,"venice_session":"active","chat_quota":{...},...} = ready
curl -s http://127.0.0.1:18892/health      # adapter up (needed only for Claude Code / Codex)
curl -s http://127.0.0.1:18891/v1/models   # model ids: venice-auto, Venice models, <backend>/<id> local models
```

If `/health` does not answer, start it from the repo folder: `python3 venice_bridge.py` (and
`python3 venice_adapters.py` for the adapter). A `503` usually means the login cookie is missing or expired: ask
the user to save the `__client` cookie of `clerk.venice.ai` to `~/.config/venice-bridge/client-cookie.txt`, then
run `curl -s -X POST http://127.0.0.1:18891/reload`.

**Use**

- Any OpenAI Chat Completions client: base URL `http://127.0.0.1:18891/v1`, any placeholder API key. From a
  Docker container: `http://host.docker.internal:18891/v1`.
- OpenCode: provider `venice-bridge` (block in the repo's `examples/opencode.json`), e.g.
  `opencode -m venice-bridge/deepseek-v4-1-flash-uncensored`.
- Claude Code: `claude-venice` (repo launcher; isolated profile `~/.claude-venice`). It skips Claude Code's
  permission prompts by default; set `VENICE_CLAUDE_SKIP_PERMS=0` to keep them.
- Codex CLI: a provider with `base_url="http://127.0.0.1:18892/v1"` and `wire_api="responses"` (full command in
  the repo README).
- Local models: model id `ollama/<model>` or `lmstudio/<model>` goes to that local server, with no Venice quota.
- Venice web search is off; add `:web` to a model id to turn it on for one request.
- Reasoning is off; add `:think` to a model id for one request. It (and `reasoning_effort`) is ignored when tools
  are declared, so it helps plain chat and scripts, not agent loops; the chain of thought comes back as
  `reasoning_content`.
- `/v1/models` lists only models the web subscription covers (no credit or `e2ee-*` models); a hidden id can
  still be requested.
- Model `venice-auto`: a Venice model normally, a local model (default `ollama/qwen3:8b`) once the daily quota is
  used up. The request that hits the limit still fails once; the local server must be running.
- Venice's fast models are weak at long agentic chains: check their work.

**Errors**

| Response | Meaning | Action |
|---|---|---|
| `403` `venice_quota_exhausted` | Web chat quota used up (plan-wide) | Stop and tell the user when it resets, or offer `venice-auto` with a local model; do not retry in a loop |
| `403` `creator_access_required` | The agent route on a free Venice account | Tell the user the agent route needs a paid web plan |
| `502` Cloudflare challenge | Venice challenged the request | Ask the user to open venice.ai in the browser and refresh the cookie |
| `502` `local_backend_error` | The local model server is down | Start it; do not switch to Venice silently |
| `400` `context_length_exceeded` | Request too large | Compact or shorten the conversation |
| `429` `venice_rate_limited` | Passing rate limit | Wait briefly, then retry |
| Model says "Tool not found" / tools unavailable | The request used the agent route | Use `VENICE_BRIDGE_ROUTE=auto` or `chat`; start a new session |

**Rules**

- Never print, log, or commit the cookie file, `~/.config/venice-bridge/rotated-client.json`, or any token.
- Never expose ports 18891 or 18892 beyond `127.0.0.1`; neither has client authentication.
- The user's own account is at stake: automating the web session may be against Venice's terms of service.

Docs: https://nsozturk.github.io/venice-bridge/ · Repo: https://github.com/nsozturk/venice-bridge
