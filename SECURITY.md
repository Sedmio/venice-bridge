# Security

## The cookie is a credential

venice-bridge signs in to venice.ai with the value of the `__client` cookie of `clerk.venice.ai`. This is a
long-lived login cookie: anyone who has it can use your Venice account. Treat it like a password. Never commit it,
paste it into an issue, or share it.

## What the bridge stores

| File | Contents | Mode |
|---|---|---|
| `~/.config/venice-bridge/client-cookie.txt` (or `VENICE_BRIDGE_COOKIE_FILE`) | The cookie you saved. | `0600` when created with the command in the README. The bridge reads this file and does not change its mode. |
| `~/.config/venice-bridge/rotated-client.json` (or `VENICE_BRIDGE_CLIENT_FILE`) | The cookie value after Clerk rotates it. | Written atomically with mode `0600`; the directory is created with mode `0700`. |

For each upstream call the bridge also writes the request headers (including the short-lived session token and
client attestation) and the request body to a private temporary directory (mode `0700`, files `0600`), and
removes them when the call ends. Both cookie file names are in `.gitignore`.

## Network exposure

The bridge listens on `127.0.0.1:18891` by default and has **no client authentication**: any program that can
reach the port can use your Venice account through it. Do not bind it to another address with `--host`, do not
forward the port, and do not expose it to a network.

## Reporting a vulnerability

Report vulnerabilities through GitHub on <https://github.com/nsozturk/venice-bridge>: use a private security
advisory (**Security** → **Report a vulnerability**) for anything that could expose credentials, or an issue for
everything else. Do not include real cookie or token values in a report.
