# Security

## The cookie is a credential

venice-bridge signs in to venice.ai with the value of the `__client` cookie of `clerk.venice.ai`. This is a
long-lived login cookie: anyone who has it can use your Venice account. Treat it like a password. Never commit it,
paste it into an issue, or share it.

## What is stored on disk

| File | Contents | Mode |
|---|---|---|
| `~/.config/venice-bridge/client-cookie.txt` (or `VENICE_BRIDGE_COOKIE_FILE`) | The cookie you saved. | `0600` when created with the command in the README. The bridge reads this file and does not change its mode. |
| `~/.config/venice-bridge/rotated-client.json` (or `VENICE_BRIDGE_CLIENT_FILE`) | The cookie value after Clerk rotates it. | Written atomically with mode `0600`; the directory is created with mode `0700`. |
| The folder named in `VENICE_ADAPTER_DUMP` (only when you set it) | Every request body the adapter receives, as JSON: your prompts, the system prompt, tool results and file contents. | Default file mode of your user. |

For each upstream call the bridge also writes the request headers (including the short-lived session token and
client attestation) and the request body to a private temporary directory (mode `0700`, files `0600`), and
removes them when the call ends. Both cookie file names are in `.gitignore`. The response cache is kept in memory
only.

`VENICE_ADAPTER_DUMP` is for debugging. Unset it when you are done, delete the dump folder, and never commit it or
attach it to an issue.

## Network exposure

Neither process has **client authentication**: any program that can reach a port can use your Venice account
through it.

| Process | Default address |
|---|---|
| Bridge (`venice_bridge.py`) | `127.0.0.1:18891` (`--host` or `VENICE_BRIDGE_HOST`) |
| Adapter (`venice_adapters.py`) | `127.0.0.1:18892` (`VENICE_ADAPTER_HOST`) |

Do not bind either to another address, do not forward the ports, and do not expose them to a network. A Docker
container on the same Mac reaches the bridge through `http://host.docker.internal:18891/v1` without changing the
bind address.

## `claude-venice` skips permission prompts

The `claude-venice` launcher starts Claude Code with `--dangerously-skip-permissions` by default, so Claude Code
runs shell commands and edits files **without asking you**. It does this because Venice's fast models cannot
drive Claude Code's auto-mode safety classifier. Combined with models that are weak at long agentic chains, this
means a wrong step runs unchecked. Commands run with your full user permissions and are not confined to the
working folder: use it only on a machine and with files you can afford to have changed or deleted.

To get permission prompts back, set `VENICE_CLAUDE_SKIP_PERMS=0`, or pass your own `--permission-mode <mode>`
(any permission flag you pass turns the default off). The launcher's isolated profile (`~/.claude-venice`) keeps
your normal Claude Code settings and login out of it.

## Web search

Venice web search is off by default. When you turn it on (`<model>:web`, `<model>:xsearch`, `VENICE_WEB=1` or
`VENICE_X_SEARCH=1`), Venice runs the search queries and fetches the pages on its servers, under your account.
The queries are written by the model from your conversation, so they can carry parts of it.

## Local backends

A `<backend>/<model>` request goes to the URL in `~/.config/venice-bridge/backends.json` (or the defaults on
`127.0.0.1`). An `api_key` in that file is sent to that URL as a bearer token; point it only at servers you trust.

## Reporting a vulnerability

Report vulnerabilities through GitHub on <https://github.com/nsozturk/venice-bridge>: use a private security
advisory (**Security** → **Report a vulnerability**) for anything that could expose credentials, or an issue for
everything else. Do not include real cookie or token values in a report.
