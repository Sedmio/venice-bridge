# venice-bridge (global agent rule)

Paste this into your agent's global instructions so it knows the bridge exists and how to use it.

---

## venice-bridge — Venice web models through a local OpenAI endpoint

venice-bridge is a local server on this Mac that exposes the user's venice.ai web subscription as an OpenAI
Chat Completions API at `http://127.0.0.1:18891/v1`, with tool calling. It is unofficial and uses the venice.ai
web app's undocumented backend with the user's own login; it needs no Venice API key. It was tested on a paid Venice
web plan (Pro); its agent route needs a paid plan, and a free account has a very small chat quota.
`VENICE_BRIDGE_ROUTE=auto` (default) uses Venice's plain chat route and falls back to the agent route for a model
the chat route does not know.

**Check before use**

```bash
curl -s http://127.0.0.1:18891/health      # 200 {"ok":true,"venice_session":"active",...} = ready
curl -s http://127.0.0.1:18891/v1/models   # model ids the account can use
```

If `/health` does not answer, start it from the repo folder: `python3 venice_bridge.py`. A `503` usually
means the login cookie is missing or expired: ask the user to save the `__client` cookie of `clerk.venice.ai` to
`~/.config/venice-bridge/client-cookie.txt`, then run `curl -s -X POST http://127.0.0.1:18891/reload`.

**Use**

- Any OpenAI Chat Completions client: base URL `http://127.0.0.1:18891/v1`, any placeholder API key.
- OpenCode: provider `venice-bridge` (block in the repo's `examples/opencode.json`), e.g.
  `opencode -m venice-bridge/deepseek-v4-1-flash-uncensored`.
- Not usable directly from Claude Code (Anthropic Messages) or Codex CLI (OpenAI Responses).

**Errors**

| Response | Meaning | Action |
|---|---|---|
| `403` `venice_quota_exhausted` | Web chat quota used up | Stop and tell the user when it resets; do not retry in a loop |
| `403` `creator_access_required` | The agent route on a free Venice account | Tell the user the agent route needs a paid web plan |
| `502` Cloudflare challenge | Venice challenged the request | Ask the user to open venice.ai in the browser and refresh the cookie |
| `400` `context_length_exceeded` | Request too large | Compact or shorten the conversation |
| `429` `venice_rate_limited` | Passing rate limit | Wait briefly, then retry |
| Model says "Tool not found" / tools unavailable | The request used the agent route | Use `VENICE_BRIDGE_ROUTE=auto` or `chat`; start a new session |

**Rules**

- Never print, log, or commit the cookie file, `~/.config/venice-bridge/rotated-client.json`, or any token.
- Never expose the bridge beyond `127.0.0.1`; it has no client authentication.
- The user's own account is at stake: automating the web session may be against Venice's terms of service.

Docs: https://nsozturk.github.io/venice-bridge/ · Repo: https://github.com/nsozturk/venice-bridge
