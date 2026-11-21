# Changelog

All notable changes to venice-bridge. Versions follow [Semantic Versioning](https://semver.org/).
Releases: <https://github.com/nsozturk/venice-bridge/releases> · PyPI: <https://pypi.org/project/venice-bridge/>

## [Unreleased]

### Added

- **Multimodal image & document attachments** across OpenAI `/v1/chat/completions` (`image_url`, `file`), Anthropic `/v1/messages` (`image`, `document` base64), and OpenAI `/v1/responses` (`input_image`, `input_file`). Requests carrying attachments are sent as `multipart/form-data` to `/api/inference/chat/agents/ad-hoc`; text-only requests stay unchanged on the plain chat route. Enforces a 17-type MIME allowlist (png/jpg/gif/webp + txt/md/csv/rtf/json/yaml/xml/pdf/docx/xls/xlsx), rejects remote `http(s)://` URLs with HTTP 400 (`remote URLs not supported, send base64`), enforces per-file (10 MB) and total (20 MB) size limits with HTTP 413 (`VENICE_BRIDGE_MAX_ATTACHMENT_BYTES`, `VENICE_BRIDGE_MAX_TOTAL_ATTACHMENT_BYTES`), skips response cache for multimodal turns, and never logs raw bytes/base64.
  - *Known limitation:* Because attachments require `/api/inference/chat/agents/ad-hoc`, combining an image attachment with client tool calls in the same turn can collide with Venice's own server tools (measured: `2/5` `bash` tool calls survived on `deepseek-v4-1-flash-uncensored` with an attached PNG; text-only tool turns are unaffected).
- **OpenAI-compatible Speech-to-Text (`POST /v1/audio/transcriptions`)** backed by Venice `/api/inference/asr` (`nvidia/parakeet-tdt-0.6b-v3` default; `json`, `text`, and `verbose_json` formats; mp3/wav/m4a/webm/ogg/flac allowlist; 25 MB cap via `VENICE_BRIDGE_MAX_AUDIO_BYTES` → HTTP 413; 0 chat/image quota spent).
- **OpenAI-compatible Image Generation (`POST /v1/images/generations`)** backed by Venice in-chat `veniceai:image_generation.image_chunk` (`n=1`, `b64_json` default or `url` as `data:` URI, no file hosting; decrements both `chat` and `image` quota by 1). Both endpoints add count-only counters under `GET /stats` (`endpoints.audio_transcriptions`, `endpoints.image_generations`) and never log or cache raw audio/image bytes. Only attachments sent after the last assistant turn are uploaded; older ones become a placeholder, so the rest of the conversation returns to the plain chat route.

## 0.2.3 — 2026-11-21

### Added

- **`claude-venice --lean`** launcher flag: disables all MCP servers via `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` to keep the `[TOOLS]` block compact when a project carries `.mcp.json` (default behavior unchanged).

### Fixed

- **Runaway repetition loops cut early.** In tool mode, replies exceeding 40 tool-call envelopes or 150,000
  characters are cut at the 41st envelope / cap and parsed as complete instead of hanging for minutes
  (`VENICE_BRIDGE_LOOP_MAX_CALLS`, `VENICE_BRIDGE_LOOP_MAX_CHARS`).
- **Code written instead of run.** A reply with no tool call that holds a fenced code block while a shell tool
  is declared now goes to the judge, which can ask the model to run the command instead of showing it.
- `bash_tool` is routed to the client's shell tool.

## 0.2.2 — 2026-11-21

### Fixed

- **Bare-string tool calls are no longer dropped.** When a model answers a tool call with a bare
  string, it reaches the bridge as `{"content": "..."}`. If the tool has no `content` parameter and
  exactly one required string parameter, the string now goes into that parameter instead of being
  dropped as `dropped args not in schema: ['content']`.

### Docs

- This changelog, and a "What's new" section on the documentation site.

## 0.2.1 — 2026-11-21

### Fixed

- **Double-nested tool arguments are unwrapped**: `{"arguments": {...}}`, `{"name": ..., "arguments": {...}}`,
  a JSON string in `arguments`, and `{"input": {...}}` / `{"parameters": {...}}`, up to three levels and
  only when the tool's schema has no field of that name.
- The format reminder tells the model to put parameters directly in `arguments` and to use a heredoc for
  multi-line SQL or Python.

### Added

- Installable from PyPI: `pipx install venice-bridge` gives the `venice-bridge`, `venice-adapters` and
  `claude-venice` commands.

## 0.2.0 — 2026-11-21

### Added

- `venice_adapters.py` (127.0.0.1:18892): Anthropic Messages and OpenAI Responses in front of the bridge,
  so Claude Code and Codex CLI work. Standard library only.
- `claude-venice`: runs Claude Code against the adapter in an isolated profile.
- Local models by prefix (`ollama/...`, `lmstudio/...`, others via `backends.json`).
- `venice-auto`: Venice normally, a local model while the daily web quota is used up.
- Opt-in Venice web search (`:web`, `:xsearch`) and reasoning on no-tool requests (`:think`).
- `temperature` and `top_p` forwarded; credit-only and e2ee models hidden from `/v1/models`;
  in-memory cache for byte-identical repeat requests.

### Fixed

- A local model asked through the adapter never falls back to Venice when its backend is down.
- `reasoning_effort` no longer turns reasoning on for requests that declare tools.
- `venice-bridge/venice-auto` resolves like `venice-auto`.

## 0.1.0

- First open-source release: OpenAI-compatible bridge for the venice.ai web plan with emulated tool calls,
  plain chat route by default, native call reader and tool-name / argument routing.
