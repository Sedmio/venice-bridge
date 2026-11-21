#!/usr/bin/env python3
"""
Venice AI OpenAI-Compatible Bridge (venice.ai web subscription -> /v1/chat/completions)
Listens on http://127.0.0.1:18891/v1 for OpenCode and other OpenAI-compatible clients.

Auth & inference flow:
  1. Reads the long-lived Clerk `__client` cookie for venice.ai (cookie file or a local session daemon).
  2. Discovers active Clerk session via GET https://clerk.venice.ai/v1/client
  3. Mints short-lived (~60s) session JWT via POST https://clerk.venice.ai/v1/client/sessions/<sid>/tokens
  4. Mints client attestation via POST https://outerface.venice.ai/api/app/user/client_attestation/mint
  5. Streams inference. Route `auto` (default, VENICE_BRIDGE_ROUTE): POST
     https://outerface.venice.ai/api/inference/chat, the plain chat route, with the client's system
     prompt folded into the first user message. Venice runs no agent loop and offers the model no
     tools of its own there, so nothing collides with the client's tools. Its 413 userPromptTooLong
     limit (~150k chars) applies to the systemPrompt field only (measured 2026-11-19).
     A model that route does not know (400 modelNotFound) is sent to the agent route,
     /api/inference/chat/agents/ad-hoc (Responses-style SSE; needs a paid plan: a free account
     gets 403 creator_access_required), where Venice may swallow native tool calls.
     Over quota Venice answers 429 with body `{}`.
  6. Emulates OpenAI tool calling (tools / tool_calls) via JSON envelope for OpenCode agent loops.
"""

import argparse
import base64
import datetime
import email.utils
import http.client
import fnmatch
import itertools
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from curl_cffi import requests as cffi_requests

# Where the venice.ai login comes from, first match wins:
#   1. COOKIE_FILE: a text file holding the value of the `__client` cookie of clerk.venice.ai
#      (copied once from the browser; see README).
#   2. A local session daemon whose `client.py` (with get_session(domain)) lives in
#      SESSION_BRIDGE_SERVER, e.g. a browser extension that syncs cookies to disk.
COOKIE_FILE = os.environ.get("VENICE_BRIDGE_COOKIE_FILE", "~/.config/venice-bridge/client-cookie.txt")
# Opt-in multi-account cookie pool (empty by default = single-account mode, identical to prior behavior).
# When set, must point to a 0600 file outside the repo with one Clerk __client cookie per line.
POOL_FILE = os.environ.get("VENICE_BRIDGE_COOKIE_POOL", "")
# Multimodal attachment limits (env-configurable; defaults: 10 MB per file, 20 MB total per request).
MAX_ATTACHMENT_BYTES = int(os.environ.get("VENICE_BRIDGE_MAX_ATTACHMENT_BYTES", str(10 * 1024 * 1024)))
MAX_TOTAL_ATTACHMENT_BYTES = int(os.environ.get("VENICE_BRIDGE_MAX_TOTAL_ATTACHMENT_BYTES", str(20 * 1024 * 1024)))
ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
ALLOWED_FILE_MIMES = {
    "text/plain", "text/markdown", "text/csv", "text/rtf", "application/rtf",
    "application/json", "application/x-yaml", "text/yaml", "text/xml", "application/xml",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_MIME_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
    "text/plain": "txt", "text/markdown": "md", "text/csv": "csv", "text/rtf": "rtf",
    "application/rtf": "rtf", "application/json": "json", "application/x-yaml": "yaml",
    "text/yaml": "yaml", "text/xml": "xml", "application/xml": "xml", "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}


class VeniceAttachmentError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
SESSION_BRIDGE_SERVER = os.environ.get("VENICE_BRIDGE_SESSION_DIR", "")
if SESSION_BRIDGE_SERVER and SESSION_BRIDGE_SERVER not in sys.path:
    sys.path.insert(0, SESSION_BRIDGE_SERVER)

try:
    from client import get_session as _daemon_session  # noqa: E402
except Exception:  # no session daemon on this machine: the cookie file is the source
    _daemon_session = None
from tolerant_parser import _build_call, parse_tool_reply, read_lenient  # noqa: E402

def get_session(domain: str) -> Optional[Dict[str, Any]]:
    """The login for `domain` as {"timestamp": ms, "tokens": {"clerk_client": value}}, or None."""
    path = os.path.expanduser(COOKIE_FILE)
    try:
        value = open(path, encoding="utf-8").read().strip()
    except OSError:
        value = ""
    if value.startswith("__client="):
        value = value[len("__client="):].split(";", 1)[0].strip()
    if value:  # the file's change time stands for the sync time: saving a new cookie starts a new generation
        return {"timestamp": int(os.path.getmtime(path) * 1000), "tokens": {"clerk_client": value}}
    return _daemon_session(domain) if _daemon_session else None


CLERK_BASE = "https://clerk.venice.ai"
OUTERFACE_BASE = "https://outerface.venice.ai"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18891
# Which Venice web route carries the chat (read at call time; VENICE_BRIDGE_ROUTE=agent|chat).
#   agent: /api/inference/chat/agents/ad-hoc. Venice runs its own agent loop there: the model gets
#          Venice's server tools as native tools, calls them or the client's tools natively, and
#          Venice swallows those calls (20-50 % of the time lost, measured 2026-11-18/19).
#   chat:  /api/inference/chat. No agent loop and no Venice tools, so nothing collides with the
#          client's tools. Its 413 "userPromptTooLong" limit (~150k chars) applies to the
#          systemPrompt field only: the same 460k chars are accepted inside a user message
#          (measured 2026-11-19), so the instructions travel as the first user message.
#   auto:  chat, except for a model the chat route does not know (400 modelNotFound, e.g. kimi-k2-5
#          and Venice's "default"): that model is remembered and sent to the agent route.
ROUTE = os.environ.get("VENICE_BRIDGE_ROUTE", "auto")
AGENT_ONLY_MODELS: set = set()
# Background JWT refresh when fewer than PROACTIVE_REFRESH_S seconds remain (hard refresh is at 15s).
PROACTIVE_REFRESH_S = int(os.environ.get("VENICE_BRIDGE_PROACTIVE_REFRESH_S", "30"))


def _uses_chat_route(model: Any) -> bool:
    return ROUTE == "chat" or (ROUTE == "auto" and model not in AGENT_ONLY_MODELS)
CHAT_SYSTEM_PROMPT = "Follow the session instructions given at the start of the first user message."


def _parse_web_tags(model: str) -> Tuple[str, bool, bool, bool]:
    """Opt-in per-call toggles for the plain chat route (all off by default), via env or a model-id
    suffix stripped before the id reaches Venice. `VENICE_WEB=1` / `VENICE_X_SEARCH=1` /
    `VENICE_REASONING=1` turn one on process-wide; suffixes do it for one call:
    `<model>:web` (web search + URL scraping), `:xsearch` (X/Twitter), `:think` (reasoning / chain of
    thought), combinable (`<model>:web:think`). Returns (clean_model, web_enabled, x_search, think).
    Web/reasoning can compete with or break a client's own tool loop, so they are never on unless asked;
    the caller still suppresses `/think` when the request carries tools."""
    web = os.environ.get("VENICE_WEB") == "1"
    xsearch = os.environ.get("VENICE_X_SEARCH") == "1"
    think = os.environ.get("VENICE_REASONING") == "1"
    parts = model.split(":")
    extra = {p.lower() for p in parts[1:]}
    if extra and extra <= {"web", "scrape", "xsearch", "xweb", "x", "think", "reason", "reasoning"}:
        web = web or bool({"web", "scrape"} & extra)
        xsearch = xsearch or bool({"xsearch", "xweb", "x"} & extra)
        think = think or bool({"think", "reason", "reasoning"} & extra)
        model = parts[0]
    return model, web, xsearch, think


def _to_chat_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The request in the plain chat route's shape: instructions folded into the first user message,
    neighbouring messages of one role joined (the route is a plain user/assistant conversation)."""
    turns: List[Dict[str, str]] = [{
        "role": "user",
        "content": "[SESSION INSTRUCTIONS]\n" + payload["systemPrompt"]["instructions"] + "\n[END OF SESSION INSTRUCTIONS]",
    }]
    first_user = True
    for m in payload["messages"]:
        role, text = m["role"], m["content"][0]["text"]
        if role == "user" and first_user:
            text, first_user = "[USER REQUEST]\n" + text, False
        if turns[-1]["role"] == role:
            turns[-1]["content"] += "\n\n" + text
        else:
            turns.append({"role": role, "content": text})
    # Reasoning lever for the web chat route: prefix the last user turn with `/think ` (the official web
    # app's own switch). Set by openai_to_venice_chat only when asked and no tools are in play.
    if payload.get("_think"):
        for t in reversed(turns):
            if t["role"] == "user":
                t["content"] = "/think " + t["content"]
                break
    out: Dict[str, Any] = {
        "requestId": uuid.uuid4().hex[:16],
        "conversationType": "text",
        "modelId": payload["modelId"],
        "prompt": turns,
        "systemPrompt": CHAT_SYSTEM_PROMPT,
        "includeVeniceSystemPrompt": False,
        "isCharacter": False,
        "simpleMode": False,
        # classic-chat fields the official web client sends on this same route (venice.ai-normalChat.har):
        "enableLargeContextChat": True,
        # reasoning is a plain bool here (NOT the agent route's reasoningMode, which this route drops).
        # Default off so tool calls stay reliable (reasoning models want reasoning_content passed back,
        # which most clients do not carry -> 400 on tool turns); on only when `_think` was set (opt-in,
        # no-tools) or the client asked for effort.
        "reasoning": bool(payload.get("_think")) or payload.get("reasoningMode") == "enabled",
    }
    if payload.get("_think"):
        # the official web client's own reasoning request (venice.ai-normalChat.har) sends the model
        # Venice's system prompt on this turn; without it the plain route returns no reasoning_content.
        out["includeVeniceSystemPrompt"] = True
    # sampling controls: forwarded only when the client set them, else Venice's own default applies.
    if isinstance(payload.get("temperature"), (int, float)):
        out["temperature"] = payload["temperature"]
    if isinstance(payload.get("topP"), (int, float)):
        out["topP"] = payload["topP"]
    # opt-in Venice server-side web (see _parse_web_tags); off unless asked.
    if payload.get("_webEnabled"):
        out["webEnabled"] = True
        out["webScrapeEnabled"] = True
    if payload.get("_xSearch"):
        out["xSearchEnabled"] = True
    return out


SERVER_LOOP_STEPS = 3  # refused native calls tolerated in one response (same value as opencode-venice-web)
STALL_SECONDS = 180  # no byte from Venice for this long = dead stream (calibrate from the log if it trips)
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class VeniceAuthError(RuntimeError):
    def __init__(self, message: str, rejected: bool = False) -> None:
        super().__init__(message)
        self.rejected = rejected  # Clerk refused this __client value (worth one retry with another value)


# Rotated Clerk `__client`, kept by the bridge across restarts. A live credential: never log it.
# Read at call time so tests can point it at a temp dir.
CLIENT_STATE_FILE = os.environ.get("VENICE_BRIDGE_CLIENT_FILE") or os.path.expanduser(
    "~/.config/venice-bridge/rotated-client.json"
)


def _read_saved_client() -> Optional[Dict[str, Any]]:
    try:
        with open(CLIENT_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("client"), str) and data["client"]:
        return data
    return None


def _write_saved_client(session_ts: Any, value: str) -> None:
    """Atomic write, mode 0600 from creation. A disk problem never breaks auth."""
    tmp = None
    try:
        d = os.path.dirname(CLIENT_STATE_FILE)
        os.makedirs(d, mode=0o700, exist_ok=True)
        tmp = os.path.join(d, ".venice-bridge-client.%s.tmp" % uuid.uuid4().hex)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"session_ts": session_ts, "client": value, "saved_at": time.time()}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CLIENT_STATE_FILE)
        tmp = None
    except OSError as exc:
        sys.stderr.write(f"[venice-bridge] could not save the rotated Clerk client ({type(exc).__name__})\n")
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _drop_saved_client_file() -> None:
    try:
        os.unlink(CLIENT_STATE_FILE)
    except OSError:
        pass


def _is_cookie_deletion(set_cookie: str, value: str) -> bool:
    if not value:
        return True
    m = re.search(r";\s*max-age\s*=\s*(-?\d+)", set_cookie, re.I)
    if m and int(m.group(1)) <= 0:
        return True
    m = re.search(r";\s*expires\s*=\s*([^;]+)", set_cookie, re.I)
    if m:
        try:
            return email.utils.parsedate_to_datetime(m.group(1).strip()).timestamp() <= time.time()
        except (TypeError, ValueError, IndexError):
            pass
    return False


def _attest_expiry(raw: Any, now: float) -> float:
    """Venice's `expiresAt` in an unknown unit (seconds, ms or ISO-8601) -> epoch seconds, capped at now+300."""
    try:
        if isinstance(raw, bool):
            raise ValueError
        if isinstance(raw, str) and not re.fullmatch(r"\s*\d+(\.\d+)?\s*", raw):
            s = raw.strip()
            dt = datetime.datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            exp = dt.timestamp()
        else:
            exp = float(raw)
            if exp > 1e12:
                exp /= 1000.0
        if not math.isfinite(exp) or exp <= now:
            raise ValueError
    except Exception:
        return now + 240
    return min(exp, now + 300)


def _jwt_exp(jwt_token: str) -> float:
    try:
        parts = jwt_token.split(".")
        if len(parts) < 2:
            return 0.0
        pad = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8"))
        return float(payload.get("exp") or 0.0)
    except Exception:
        return 0.0


class VeniceClient:
    def __init__(self, cookie_override: Optional[str] = None, label: str = "acct#1") -> None:
        self._lock = threading.Lock()  # Clerk state + JWT; held across the Clerk HTTP calls
        self._attest_lock = threading.Lock()  # attestation only; never taken together with _lock
        self._cookie_override: Optional[str] = cookie_override
        self.label: str = label
        self._clerk_client: Optional[str] = None
        self._sb_client: Optional[str] = None  # the session source's own value for the current sync
        self._session_ts: float = 0.0
        self._session_ts_raw: Any = None  # session source `timestamp` as stored (ms), the sync generation
        self._session_id: Optional[str] = None
        self._user_id: Optional[str] = None  # pinned per sync generation
        self._jwt: Optional[str] = None
        self._jwt_exp: float = 0.0
        self._attest_token: Optional[str] = None
        self._attest_exp: float = 0.0
        self._attest_gen: int = 0
        self._refreshing: bool = False
        self.quota_until: float = 0.0  # epoch seconds; chat is answered locally until then

    def _load_clerk_client(self) -> str:
        if self._cookie_override:
            if not self._clerk_client:
                self._clerk_client = self._cookie_override
            return self._clerk_client
        session = get_session("venice.ai")
        if not session:
            raise VeniceAuthError(
                "No venice.ai login found. Put the `__client` cookie of clerk.venice.ai into "
                f"{COOKIE_FILE} (see README), or sync venice.ai in your session daemon."
            )
        ts_raw = session.get("timestamp") or 0
        ts = float(ts_raw) / 1000.0
        if ts and ts != self._session_ts:
            self._clerk_client = None
            self._session_id = None
            self._user_id = None
            self._jwt = None
            self._jwt_exp = 0.0
            self._attest_token = None
            self._attest_exp = 0.0
            self._session_ts = ts
            self._session_ts_raw = ts_raw

        tok = (session.get("tokens") or {}).get("clerk_client")
        if not tok:
            for c in session.get("cookies") or []:
                if c.get("key") == "__client" and c.get("value"):
                    tok = c["value"]
                    break
        self._sb_client = tok
        if self._clerk_client:
            return self._clerk_client
        if not tok:
            raise VeniceAuthError(
                "venice.ai session is missing Clerk '__client' cookie. "
                "Make sure you are logged in on venice.ai, then refresh the cookie file or sync again."
            )
        # a value rotated from this same sync wins; a newer sync (different timestamp) wins over the file
        saved = _read_saved_client() if ts_raw else None
        if saved and saved.get("session_ts") == ts_raw:
            self._clerk_client = saved["client"]
        else:
            self._clerk_client = tok
        return self._clerk_client

    def _update_rotated_client(self, resp_headers: Any) -> None:
        set_cookies = []
        if hasattr(resp_headers, "get_list"):
            set_cookies = resp_headers.get_list("set-cookie")
        elif "set-cookie" in resp_headers:
            set_cookies = [resp_headers["set-cookie"]]
        for sc in set_cookies:
            m = re.match(r"__client=([^;]*)", sc.strip())
            if not m or _is_cookie_deletion(sc, m.group(1)):
                continue
            if m.group(1) != self._clerk_client:
                self._clerk_client = m.group(1)
                if self._session_ts_raw and not self._cookie_override:
                    _write_saved_client(self._session_ts_raw, self._clerk_client)

    def _fall_back_to_session_bridge(self) -> bool:
        """The rotated value is in use and was refused: forget it and use the session source's value."""
        if not self._sb_client or self._clerk_client == self._sb_client:
            return False
        _drop_saved_client_file()
        self._clerk_client = self._sb_client
        self._session_id = None
        return True

    def _ensure_session_id(self, clerk_client: str) -> str:
        if self._session_id:
            return self._session_id
        r = cffi_requests.get(
            f"{CLERK_BASE}/v1/client",
            headers={
                "Cookie": f"__client={clerk_client}",
                "Origin": "https://venice.ai",
                "Referer": "https://venice.ai/",
                "User-Agent": BROWSER_UA,
            },
            impersonate="chrome124",
            timeout=20,
        )
        self._update_rotated_client(r.headers)
        if r.status_code in (401, 403):
            raise VeniceAuthError(
                f"Clerk /v1/client rejected ({r.status_code}). Re-login on venice.ai and Sync.", rejected=True
            )
        r.raise_for_status()
        data = r.json()
        resp = data.get("response") or data
        active = [s for s in resp.get("sessions") or [] if isinstance(s, dict) and s.get("status") == "active"]
        user_of = lambda s: (s.get("user") or {}).get("id") if isinstance(s.get("user"), dict) else None
        if self._user_id:  # same sync generation: never switch accounts silently
            active = [s for s in active if user_of(s) == self._user_id]
        last = resp.get("last_active_session_id")
        chosen = next((s for s in active if s.get("id") == last), None) or (active[0] if active else None)
        if not chosen or not chosen.get("id"):
            # an unknown client may come back 200 with no sessions (unmeasured), so this counts as a refusal
            raise VeniceAuthError("No active Clerk session found on venice.ai. Re-login and Sync.", rejected=True)
        self._user_id = user_of(chosen) or self._user_id
        self._session_id = chosen["id"]
        return self._session_id

    def _mint_jwt(self, now: float) -> str:
        sid = self._ensure_session_id(self._clerk_client)
        r = cffi_requests.post(
            f"{CLERK_BASE}/v1/client/sessions/{sid}/tokens",
            headers={
                "Cookie": f"__client={self._clerk_client}",  # read again: /v1/client may have rotated it
                "Origin": "https://venice.ai",
                "Referer": "https://venice.ai/",
                "User-Agent": BROWSER_UA,
            },
            impersonate="chrome124",
            timeout=20,
        )
        self._update_rotated_client(r.headers)
        if r.status_code in (401, 403, 404):
            self._session_id = None
            raise VeniceAuthError(
                f"Clerk token mint failed ({r.status_code}). Re-login on venice.ai and Sync.",
                rejected=r.status_code in (401, 403),
            )
        r.raise_for_status()
        jwt_tok = r.json().get("jwt")
        if not jwt_tok:
            raise VeniceAuthError("Clerk token endpoint did not return a jwt.")
        self._jwt = jwt_tok
        self._jwt_exp = _jwt_exp(jwt_tok) or (now + 55)
        return jwt_tok

    def _proactive_refresh_bg(self) -> None:
        try:
            with self._lock:
                now = time.time()
                if self._jwt and now < (self._jwt_exp - PROACTIVE_REFRESH_S):
                    return
                self._load_clerk_client()
                self._mint_jwt(now)
            STATS.record_event("proactive_jwt_refreshes")
        except Exception as exc:
            sys.stderr.write(f"[venice-bridge] proactive JWT refresh skipped: {type(exc).__name__}\n")
        finally:
            self._refreshing = False

    def get_jwt(self, force: bool = False) -> str:
        with self._lock:  # not re-entrant: the fallback retry is the loop below, never a recursive call
            now = time.time()
            if not force and self._jwt and now < (self._jwt_exp - 15):
                if PROACTIVE_REFRESH_S > 15 and now >= (self._jwt_exp - PROACTIVE_REFRESH_S) and not self._refreshing:
                    self._refreshing = True
                    threading.Thread(target=self._proactive_refresh_bg, daemon=True).start()
                return self._jwt
            if force:
                self._session_id = None
            for attempt in (0, 1):
                self._load_clerk_client()
                try:
                    return self._mint_jwt(now)
                except VeniceAuthError as exc:
                    if attempt or not exc.rejected or not self._fall_back_to_session_bridge():
                        raise
                    sys.stderr.write("[venice-bridge] Clerk refused the saved client; retrying with the session source value\n")
            raise AssertionError("unreachable")

    def get_attestation(self, jwt_tok: str, force: bool = False) -> Optional[str]:
        # single flight: one mint at a time; whoever waited reuses the mint that finished meanwhile
        gen = self._attest_gen
        with self._attest_lock:
            now = time.time()
            fresh = self._attest_token and now < (self._attest_exp - 30)
            if fresh and (not force or self._attest_gen != gen):
                return self._attest_token
            return self._mint_attestation(jwt_tok)

    def _mint_attestation(self, jwt_tok: str) -> Optional[str]:
        r = cffi_requests.post(
            f"{OUTERFACE_BASE}/api/app/user/client_attestation/mint",
            headers={
                "Authorization": f"Bearer {jwt_tok}",
                "Origin": "https://venice.ai",
                "Referer": "https://venice.ai/",
                "User-Agent": BROWSER_UA,
                "Content-Type": "application/json",
            },
            json={
                "count": 1,
                "env": {
                    "nativeIntegrityOk": True,
                    "unexpectedGlobalsCount": 0,
                    "webdriver": False,
                },
            },
            impersonate="chrome124",
            timeout=20,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        tok = data.get("token")
        if not tok and isinstance(data.get("tokens"), list) and data["tokens"]:
            tok = data["tokens"][0].get("token")
        self._attest_token = tok
        self._attest_exp = _attest_expiry(data.get("expiresAt"), time.time())
        self._attest_gen += 1
        return tok

    def auth_headers(self, force_refresh: bool = False) -> Dict[str, str]:
        jwt_tok = self.get_jwt(force=force_refresh)
        attest = self.get_attestation(jwt_tok, force=force_refresh)
        hdrs = {
            "Authorization": f"Bearer {jwt_tok}",
            "Origin": "https://venice.ai",
            "Referer": "https://venice.ai/",
            "x-venice-middleface-version": "0.1.1128",
            "x-venice-request-timestamp-ms": str(int(time.time() * 1000)),
            "Content-Type": "application/json",
        }
        if attest:
            hdrs["x-venice-client-attestation"] = attest
        return hdrs

    def list_models(self) -> List[Dict[str, Any]]:
        hdrs = self.auth_headers()
        r = cffi_requests.get(
            f"{OUTERFACE_BASE}/api/app/models",
            params={"matureFilter": "false", "onlySafeVenice": "false"},
            headers={**hdrs, "User-Agent": BROWSER_UA},
            impersonate="chrome124",
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        models: List[Dict[str, Any]] = []
        text_section = data.get("text") if isinstance(data, dict) else None
        raw_list = (text_section or {}).get("models") if isinstance(text_section, dict) else None
        if not isinstance(raw_list, list):
            raw_list = data.get("data", []) if isinstance(data, dict) else []
        # Hide models a plain OpenAI client cannot actually use, so it never selects one that 403s:
        # `e2ee-*` are end-to-end-encrypted (need client-side keys the bridge has no part in), plus any
        # glob in VENICE_EXCLUDE_MODELS (comma-separated, e.g. "e2ee-*,qwen3-vl-*").
        exclude = ["e2ee-*"] + [p.strip() for p in os.environ.get("VENICE_EXCLUDE_MODELS", "").split(",") if p.strip()]
        for m in raw_list:
            if not isinstance(m, dict):
                continue
            # Only expose free web subscription models (usesCredits: false) by default
            if m.get("usesCredits") is True:
                continue
            mid = m.get("id") or m.get("modelId") or m.get("slug")
            if mid and not any(fnmatch.fnmatch(mid, pat) for pat in exclude):
                models.append({
                    "id": mid,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "venice-web",
                })
        return models

    def chat_quota(self) -> Optional[Dict[str, Any]]:
        """{'remaining': int|None, 'resets_in_min': int|None} for web chat, or None if unknown."""
        try:
            r = cffi_requests.get(
                f"{OUTERFACE_BASE}/api/inference/rate-limits",
                headers={**self.auth_headers(), "User-Agent": BROWSER_UA},
                impersonate="chrome124",
                timeout=15,
            )
            chat = r.json().get("chat") or {}
            reset = chat.get("resetAt")
            return {
                "reset_at": float(reset) / 1000.0 if reset else None,
                "remaining": chat.get("remaining"),
                "resets_in_min": max(0, round((float(reset) / 1000.0 - time.time()) / 60)) if reset else None,
            }
        except Exception:
            return None

    def open_chat_stream(self, payload: Dict[str, Any], force_refresh: bool = False, max_time: Optional[float] = None):
        """Stream the request to the route chosen for its model, via system curl --http1.1 (passes Cloudflare)."""
        hdrs = self.auth_headers(force_refresh=force_refresh)
        attachments = payload.get("_attachments") or []
        path = "/api/inference/chat/agents/ad-hoc"
        if not attachments and _uses_chat_route(payload.get("modelId")):
            payload, path = _to_chat_payload(payload), "/api/inference/chat"
        else:
            payload = {k: v for k, v in payload.items() if not k.startswith("_")}  # drop internal flags
        tmpdir = tempfile.mkdtemp(prefix="vbridge-")
        os.chmod(tmpdir, 0o700)
        hdr_file = os.path.join(tmpdir, "headers.txt")
        body_file = os.path.join(tmpdir, "body.json")
        if attachments:
            hdrs.pop("Content-Type", None)  # curl -F sets multipart/form-data boundary
        with os.fdopen(os.open(hdr_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            for k, v in hdrs.items():
                f.write(f"{k}: {v}\n")
        with os.fdopen(os.open(body_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        cmd = [
            "curl",
            "-sS",
            "-N",
            "-i",
            "--http1.1",
            # a stream that goes silent must not hang the turn: Venice sends keepalives while it works
            "--connect-timeout", "20",
            "--speed-limit", "1",
            "--speed-time", str(STALL_SECONDS),
            "-A",
            BROWSER_UA,
            "-H",
            f"@{hdr_file}",
        ]
        if attachments:
            cmd.extend(["-H", "Expect:", "-F", f"request=<{body_file}"])
            for idx, att in enumerate(attachments):
                att_path = os.path.join(tmpdir, f"att_{idx}.bin")
                with os.fdopen(os.open(att_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as af:
                    af.write(att["data"])
                safe_name = re.sub(r'["\\;\r\n]', "_", att["filename"])
                cmd.extend(["-F", f"media.{att['media_id']}=@{att_path};filename={safe_name};type={att['mime']}"])
            cmd.append(OUTERFACE_BASE + path)
        else:
            cmd.extend(["--data-binary", f"@{body_file}", OUTERFACE_BASE + path])
        if max_time:  # the judge's overall limit; chat has none (a stall still ends it)
            cmd[1:1] = ["--max-time", str(max_time)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return proc, tmpdir, hdr_file, body_file


VENICE = VeniceClient()


class _AccountPool:
    """Opt-in deterministic multi-account pool (disabled by default: POOL_FILE == '').

    Stays on the active account until its quota is exhausted (quota_until > now), then switches
    in order to the next available account (acct#1, acct#2, ...). Never logs or exposes cookies,
    JWTs, or user IDs. Refuses pool files whose permissions allow group/other access (!= 0600).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: List[VeniceClient] = []
        self._active_idx: int = 0
        if POOL_FILE:
            try:
                self.reload()
            except Exception as exc:
                sys.stderr.write(f"[venice-bridge] pool init failed: {exc}\n")

    def reload(self) -> None:
        with self._lock:
            self._clients = []
            self._active_idx = 0
            if not POOL_FILE:
                return
            path = os.path.expanduser(POOL_FILE)
            if not os.path.exists(path):
                return
            mode = os.stat(path).st_mode & 0o777
            if mode & 0o077:
                raise VeniceAuthError(
                    "Cookie pool file must have 0600 permissions (chmod 600 before enabling VENICE_BRIDGE_COOKIE_POOL)."
                )
            cookies: List[str] = []
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("__client="):
                        line = line[len("__client="):].split(";", 1)[0].strip()
                    if line:
                        cookies.append(line)
            self._clients = [VeniceClient(cookie_override=c, label=f"acct#{i}") for i, c in enumerate(cookies, 1)]

    def is_enabled(self) -> bool:
        with self._lock:
            return bool(self._clients)

    def active_client(self) -> VeniceClient:
        with self._lock:
            if not self._clients:
                return VENICE
            now = time.time()
            cur = self._clients[self._active_idx]
            if now >= cur.quota_until:
                return cur
            for idx, c in enumerate(self._clients):
                if now >= c.quota_until:
                    self._active_idx = idx
                    return c
            return cur

    def failover(self, exhausted: VeniceClient) -> Optional[VeniceClient]:
        with self._lock:
            if not self._clients:
                return None
            now = time.time()
            n = len(self._clients)
            for offset in range(1, n + 1):
                idx = (self._active_idx + offset) % n
                cand = self._clients[idx]
                if cand is not exhausted and now >= cand.quota_until:
                    self._active_idx = idx
                    sys.stderr.write(
                        f"[venice-bridge] pool: {exhausted.label} quota exhausted; switching to {cand.label}\n"
                    )
                    return cand
            return None

    def all_exhausted(self) -> bool:
        now = time.time()
        with self._lock:
            if not self._clients:
                return now < VENICE.quota_until
            return all(now < c.quota_until for c in self._clients)

    def earliest_reset(self) -> float:
        with self._lock:
            if not self._clients:
                return VENICE.quota_until
            return min(c.quota_until for c in self._clients)

    def status(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            if not self._clients:
                return {"enabled": False, "total": 1, "available": 0 if now < VENICE.quota_until else 1, "active": "acct#1"}
            avail = sum(1 for c in self._clients if now >= c.quota_until)
            return {
                "enabled": True,
                "total": len(self._clients),
                "available": avail,
                "active": self._clients[self._active_idx].label,
            }


POOL = _AccountPool()


def _flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content)


# Tool list in the system prompt (measured 2026-11-18 on a real OpenCode request: 411 tools, 10 built-in,
# 363,699 chars of other system text, 77,929 chars of one-liners; Venice took 450k, ~480k is the ceiling).
TOOL_BLOCK_BUDGET = 78_000  # chars of the whole [TOOLS] block: today's block size, so the total does not grow
# Built-ins are found by name: OpenCode names MCP tools "<server>_<tool>", built-ins have no "_",
# except these (apply_patch is not in the 2026-11-18 capture: unverified). The capture's three
# list_/read_mcp_resource* helpers have "_" and stay in tier B.
CORE_TOOL_NAMES = frozenset({"apply_patch"})
_ARG_DESC_CAP = 300  # longest per-argument description among the built-ins is 268 (todowrite keeps its enums there)
_SCHEMA_DROP = ("$schema", "$id", "$comment", "title", "examples")


def _compact_schema(s: Any) -> Any:
    """The schema without what carries no structure; nested shapes, enums and required lists stay."""
    if isinstance(s, list):
        return [_compact_schema(x) for x in s]
    if not isinstance(s, dict):
        return s
    out: Dict[str, Any] = {}
    for k, v in s.items():
        if k in _SCHEMA_DROP or (k == "additionalProperties" and isinstance(v, bool)):
            continue
        if k in ("properties", "patternProperties", "$defs", "definitions") and isinstance(v, dict):
            out[k] = {name: _compact_schema(sub) for name, sub in v.items()}  # keys are argument names
        elif k == "description" and isinstance(v, str):
            out[k] = v if len(v) <= _ARG_DESC_CAP else v[: _ARG_DESC_CAP - 1] + "…"
        elif k in ("enum", "const", "default", "required"):
            out[k] = v
        else:
            out[k] = _compact_schema(v)
    return out


def _tool_line(fn: Dict[str, Any], desc_len: int = 100) -> str:
    schema = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
    params = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    req = set(schema.get("required") or [])
    arg_list = ", ".join(
        f"{k}{'*' if k in req else ''}:{(v.get('type') if isinstance(v, dict) else 'any') or 'any'}"
        for k, v in params.items()
    )
    desc = ((fn.get("description") or "").splitlines() or [""])[0][:desc_len]
    return f"- {fn['name']}({arg_list}): {desc}"


def _build_tool_instructions(
    tools: List[Dict[str, Any]], used: Any = (), budget: Optional[int] = None
) -> str:
    """Tier A (tools called earlier in this conversation, and built-ins) with their compact full
    argument schema; tier B (the rest) as one-liners. Over budget, tier B degrades from the end:
    shorter descriptions, then names only, announced on the last line. Tier A is never cut, so the
    floor is header + tier A + every name."""
    budget = TOOL_BLOCK_BUDGET if budget is None else budget
    used = set(used)
    fns = [t["function"] for t in tools
           if isinstance(t, dict) and isinstance(t.get("function"), dict) and t["function"].get("name")]
    tier_a = [fn for fn in fns if fn["name"] in used or "_" not in fn["name"] or fn["name"] in CORE_TOOL_NAMES]
    tier_b = [fn for fn in fns if not (fn["name"] in used or "_" not in fn["name"] or fn["name"] in CORE_TOOL_NAMES)]
    header = (
        "\n\n[TOOLS]\n"
        "You are connected to an agent that runs these tools on the user's computer. They are called "
        "by name as shown below; they are NOT Venice server tools.\n"
        "To call tools, reply ONLY with JSON: "
        '{"tool_calls":[{"name":"<exact tool name>","arguments":{...}}]}\n'
        "Then stop: the tool result arrives in the next user message. Never invent a tool result. "
        "When the task is finished, reply with normal text.\n"
        + _route_hint({fn["name"].lower(): fn["name"] for fn in fns}) + "\n"
    )
    lines_a = []
    for fn in tier_a:
        line = _tool_line(fn)
        schema = fn.get("parameters")
        if isinstance(schema, dict) and schema.get("properties"):
            line += "\n  arguments schema: " + json.dumps(
                _compact_schema(schema), ensure_ascii=False, separators=(",", ":"))
        lines_a.append(line)

    names_prefix = "- also available (arguments not shown): "
    notice = lambda k: f"({k} of {len(fns)} tools are listed by name only.)"
    full = [_tool_line(fn) for fn in tier_b]
    short = [_tool_line(fn, 40) for fn in tier_b]
    level = [0] * len(tier_b)  # 0 = one-liner, 1 = shorter description, 2 = name only
    cost = [len(line) + 1 for line in full]
    size = len(header) + sum(len(line) + 1 for line in lines_a) + sum(cost)
    for i in reversed(range(len(tier_b))):
        if size <= budget:
            break
        size += len(short[i]) + 1 - cost[i]
        cost[i], level[i] = len(short[i]) + 1, 1
    for i in reversed(range(len(tier_b))):
        if size <= budget:
            break
        if 2 not in level:  # the names line and the notice: counted once, with the widest count
            size += len(names_prefix) + len(notice(len(fns))) + 2
        size += len(tier_b[i]["name"]) + 2 - cost[i]
        cost[i], level[i] = len(tier_b[i]["name"]) + 2, 2

    lines_b = [full[i] if level[i] == 0 else short[i] for i in range(len(tier_b)) if level[i] < 2]
    names_only = [tier_b[i]["name"] for i in range(len(tier_b)) if level[i] == 2]
    tail = [names_prefix + ", ".join(names_only), notice(len(names_only))] if names_only else []
    block = header + "\n".join(lines_a + lines_b + tail)
    sys.stderr.write(
        "[venice-bridge] tool list: A=%d tools/%d chars, B=%d one-line + %d short/%d chars, "
        "names only=%d, block=%d of %d, full schema for: %s\n" % (
            len(tier_a), sum(len(line) + 1 for line in lines_a), level.count(0), level.count(1),
            sum(len(line) + 1 for line in lines_b), len(names_only), len(block), budget,
            ", ".join(fn["name"] for fn in tier_a[:20])))
    return block


# The strongest single lever for tool use on the web models (opencode-venice-web, PROTOCOL.md §4):
# the format goes last, after the conversation.
TOOL_REMINDER = (
    "(format reminder) Only a JSON object makes anything happen on the user's computer: code blocks and "
    "described commands are never run. To use a tool, reply now with ONLY "
    '{"tool_calls":[{"name":"<exact tool name>","arguments":{...}}]}, for example '
    '{"tool_calls":[{"name":"example_tool","arguments":{"path":"notes.txt"}}]} '
    "(example_tool is only an illustration; use the real tool names). Put parameters directly in arguments "
    "(never nest arguments inside arguments); for multi-line SQL/Python use heredoc. After sending calls, "
    "stop and wait for the tool result. If the task is finished, reply with normal text."
)


def _message(role: str, text: str) -> Dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": "message",
        "status": "completed",
        "role": role,
        # Venice rejects NUL bytes ("String must not contain null bytes")
        "content": [{"type": "output_text", "text": text.replace("\x00", " "), "annotations": []}],
    }


def _parse_tool_choice(tc: Any) -> Tuple[str, Optional[str]]:
    """Normalize OpenAI / Anthropic tool_choice -> ('auto'|'none'|'required'|'named', forced_name)."""
    if tc is None:
        return "auto", None
    if isinstance(tc, str):
        low = tc.strip().lower()
        if low == "none":
            return "none", None
        if low in ("required", "any"):
            return "required", None
        return "auto", None
    if isinstance(tc, dict):
        kind = str(tc.get("type") or "").strip().lower()
        if kind == "none":
            return "none", None
        if kind in ("required", "any"):
            return "required", None
        if kind in ("function", "tool"):
            fn = tc.get("function")
            name = (fn.get("name") if isinstance(fn, dict) else None) or tc.get("name")
            if isinstance(name, str) and name.strip():
                return "named", name.strip()
    return "auto", None


def _response_format_instruction(rf: Any) -> str:
    """Build a strict JSON output instruction for OpenAI response_format (json_object / json_schema)."""
    if not isinstance(rf, dict):
        return ""
    kind = str(rf.get("type") or "").strip().lower()
    if kind == "json_object":
        return "OUTPUT FORMAT: Reply with ONLY one valid JSON object. No markdown fences, no prose, no commentary."
    if kind == "json_schema":
        spec = rf.get("json_schema") if isinstance(rf.get("json_schema"), dict) else rf
        schema = spec.get("schema") if isinstance(spec, dict) else None
        if isinstance(schema, dict):
            rendered = json.dumps(schema, ensure_ascii=False)
            if len(rendered) <= 40000:
                return (
                    "OUTPUT FORMAT: Reply with ONLY one valid JSON value matching this JSON Schema "
                    f"(no markdown fences, no prose, no commentary):\n{rendered}"
                )
        return "OUTPUT FORMAT: Reply with ONLY one valid JSON value. No markdown fences, no prose."
    return ""


def _clean_json_output(text: str) -> str:
    """Extract and normalize a JSON value from a response_format reply (tolerates markdown fences & trailing commas)."""
    if not isinstance(text, str) or not text.strip():
        return text
    s = text.strip()
    try:
        json.loads(s)
        return s
    except Exception:
        pass
    # Find the first '{' or '[' and parse leniently
    idx_obj, idx_arr = s.find("{"), s.find("[")
    starts = [i for i in (idx_obj, idx_arr) if i >= 0]
    if not starts:
        return text
    start = min(starts)
    try:
        val, _ = read_lenient(s, start)
        return json.dumps(val, ensure_ascii=False)
    except Exception:
        return text


def _parse_stop(val: Any) -> List[str]:
    """Normalize OpenAI `stop` (str or list of str) into a deduplicated list of non-empty strings."""
    if isinstance(val, str):
        return [val] if val else []
    if isinstance(val, list):
        out: List[str] = []
        for x in val:
            if isinstance(x, str) and x and x not in out:
                out.append(x)
        return out
    return []


class _StopMatcher:
    """Streaming stop-sequence detector that holds back at most (max_len - 1) tail chars so a split
    stop sequence is never leaked to the client."""

    def __init__(self, stops: List[str]) -> None:
        self._stops = [s for s in stops if s]
        self._hold = max((len(s) for s in self._stops), default=1) - 1
        self._buf = ""
        self.matched: Optional[str] = None

    def feed(self, delta: str) -> str:
        if self.matched or not delta:
            return ""
        self._buf += delta
        earliest_idx, earliest_stop = -1, None
        for s in self._stops:
            idx = self._buf.find(s)
            if idx >= 0 and (earliest_idx < 0 or idx < earliest_idx):
                earliest_idx, earliest_stop = idx, s
        if earliest_idx >= 0:
            self.matched = earliest_stop
            out = self._buf[:earliest_idx]
            self._buf = ""
            return out
        if self._hold <= 0:
            out, self._buf = self._buf, ""
            return out
        if len(self._buf) > self._hold:
            cut = len(self._buf) - self._hold
            out, self._buf = self._buf[:cut], self._buf[cut:]
            return out
        return ""

    def flush(self) -> str:
        if self.matched:
            return ""
        out, self._buf = self._buf, ""
        return out


def _parse_data_uri(uri: str, as_image: bool, filename: Optional[str] = None) -> Dict[str, Any]:
    """Decode and validate a base64 data: URI against the 17-type MIME allowlist and size limits.
    Remote http(s) URLs are rejected with 400 (no SSRF). Never puts base64 data into error messages."""
    if not isinstance(uri, str) or not uri.strip():
        raise VeniceAttachmentError(400, "Attachment data is empty; send a base64 data: URI.")
    s = uri.strip()
    if s.lower().startswith(("http://", "https://")):
        raise VeniceAttachmentError(400, "remote URLs not supported, send base64")
    if not s.lower().startswith("data:") or ";base64," not in s[:128].lower():
        raise VeniceAttachmentError(400, "Attachment must be a base64 data: URI (data:<mime>;base64,...).")
    head, b64 = s.split(",", 1)
    mime = head[5:].split(";", 1)[0].strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    allowed = ALLOWED_IMAGE_MIMES if as_image else (ALLOWED_IMAGE_MIMES | ALLOWED_FILE_MIMES)
    if mime not in allowed:
        raise VeniceAttachmentError(400, f"Unsupported attachment MIME type: {mime}")
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        raise VeniceAttachmentError(400, f"Invalid base64 encoding for attachment ({mime}).")
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise VeniceAttachmentError(413, f"Attachment size ({len(raw)} bytes) exceeds per-file limit ({MAX_ATTACHMENT_BYTES} bytes).")
    kind = "input_image" if mime in ALLOWED_IMAGE_MIMES else "input_file"
    ext = _MIME_EXT.get(mime, "bin")
    safe_name = os.path.basename(str(filename or f"attachment.{ext}")).strip() or f"attachment.{ext}"
    return {"kind": kind, "mime": mime, "data": raw, "filename": safe_name}


def _extract_attachments(content: Any, msg_id: str, total_bytes: List[int]) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split a multimodal message content list into (plain_text, venice_media_parts, binary_blobs)."""
    if not isinstance(content, list):
        return _flatten_content(content), [], []
    texts: List[str] = []
    vparts: List[Dict[str, Any]] = []
    blobs: List[Dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            texts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in ("text", "input_text", "output_text"):
            if isinstance(item.get("text"), str):
                texts.append(item["text"])
        elif kind in ("image_url", "input_image"):
            iu = item.get("image_url")
            url = iu.get("url") if isinstance(iu, dict) else iu
            detail = (iu.get("detail") if isinstance(iu, dict) else None) or item.get("detail") or "auto"
            parsed = _parse_data_uri(str(url or ""), as_image=True)
            total_bytes[0] += len(parsed["data"])
            if total_bytes[0] > MAX_TOTAL_ATTACHMENT_BYTES:
                raise VeniceAttachmentError(413, f"Total attachment size exceeds limit ({MAX_TOTAL_ATTACHMENT_BYTES} bytes).")
            media_id = f"{msg_id}.{len(blobs) + 1}"
            vparts.append({"type": "input_image", "media_id": media_id, "detail": detail})
            blobs.append({**parsed, "media_id": media_id})
        elif kind in ("file", "input_file"):
            fobj = item.get("file") if isinstance(item.get("file"), dict) else item
            uri = fobj.get("file_data") or fobj.get("data") or fobj.get("url") or ""
            fname = fobj.get("filename") or fobj.get("name")
            parsed = _parse_data_uri(str(uri), as_image=False, filename=fname)
            total_bytes[0] += len(parsed["data"])
            if total_bytes[0] > MAX_TOTAL_ATTACHMENT_BYTES:
                raise VeniceAttachmentError(413, f"Total attachment size exceeds limit ({MAX_TOTAL_ATTACHMENT_BYTES} bytes).")
            media_id = f"{msg_id}.{len(blobs) + 1}"
            if parsed["kind"] == "input_image":
                vparts.append({"type": "input_image", "media_id": media_id, "detail": "auto"})
            else:
                vparts.append({"type": "input_file", "media_id": media_id, "filename": parsed["filename"]})
            blobs.append({**parsed, "media_id": media_id})
    return "\n".join(texts), vparts, blobs


def openai_to_venice_chat(body: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    messages = body.get("messages") or []
    system_parts: List[str] = []
    prompt: List[Dict[str, Any]] = []
    used: set = set()  # tool names already called in this conversation: listed with full schemas
    all_attachments: List[Dict[str, Any]] = []
    total_att_bytes = [0]

    raw_tools = body.get("tools") or []
    tc_mode, tc_name = _parse_tool_choice(body.get("tool_choice"))
    if tc_mode == "none" or not isinstance(raw_tools, list) or not raw_tools:
        tools: List[Dict[str, Any]] = []
        has_tools = False
    else:
        tools = raw_tools
        if tc_mode == "named" and tc_name:
            matched = [t for t in tools if isinstance(t, dict)
                       and isinstance(t.get("function"), dict)
                       and str(t["function"].get("name") or "").lower() == tc_name.lower()]
            if matched:
                tools = matched
        has_tools = len(tools) > 0

    for msg in messages:
        role = msg.get("role", "user")
        raw_content = msg.get("content")
        if role == "system":
            text = _flatten_content(raw_content)
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            text = _flatten_content(raw_content)
            tool_id = msg.get("tool_call_id") or ""
            text = f"[Tool Result {tool_id}]\n{text}"
            role = "user"
            prompt.append(_message(role, text or ""))
            continue
        if role == "assistant":
            text = _flatten_content(raw_content)
            tcs = msg.get("tool_calls") or []
            if tcs:
                simplified = []
                for tc in tcs:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            pass
                    simplified.append({"name": fn.get("name"), "arguments": args})
                    if isinstance(fn.get("name"), str):
                        used.add(fn["name"])
                # prose and calls together: keep both, the model imitates its own history
                text = (text + "\n" if text else "") + json.dumps({"tool_calls": simplified}, ensure_ascii=False)
            prompt.append(_message(role, text or ""))
            continue
        # user / other role: check for multimodal attachments
        role = "user"
        if isinstance(raw_content, list) and any(
                isinstance(it, dict) and it.get("type") in ("image_url", "input_image", "file", "input_file")
                for it in raw_content):
            msg_id = str(uuid.uuid4())
            text, vparts, blobs = _extract_attachments(raw_content, msg_id, total_att_bytes)
            all_attachments.extend(blobs)
            m_obj = _message(role, text or "")
            m_obj["id"] = msg_id
            m_obj["content"].extend(vparts)
            prompt.append(m_obj)
        else:
            prompt.append(_message(role, _flatten_content(raw_content) or ""))

    if not any(m.get("role") == "user" for m in prompt):
        prompt.append(_message("user", "Hello."))

    rf_instr = _response_format_instruction(body.get("response_format"))
    if rf_instr:
        system_parts.append(rf_instr)

    instructions = "\n\n".join(system_parts) if system_parts else "You are a helpful coding assistant."
    if has_tools:
        instructions += _build_tool_instructions(tools, used)

    model = str(body.get("model") or "qwen3-coder-480b-a35b-instruct-turbo")
    if model.startswith("venice-bridge/"):
        model = model.split("/", 1)[1]
    model, web_enabled, x_search, think = _parse_web_tags(model)

    if has_tools:
        rem = TOOL_REMINDER
        if tc_mode == "named" and tc_name:
            rem += f' You MUST call the tool "{tc_name}" now using ONLY {{"tool_calls":[{{"name":"{tc_name}","arguments":{{...}}}}]}}. Do not reply in plain text.'
        elif tc_mode == "required":
            rem += ' You MUST call at least one declared tool now using ONLY {"tool_calls":[...]}. Do not reply in plain text.'
        if body.get("parallel_tool_calls") is False:
            rem += " Call at most ONE tool in this turn."
        prompt.append(_message("user", rem))
    elif rf_instr:
        prompt.append(_message("user", "(format reminder) Reply with ONLY valid JSON matching the requested schema. No markdown fences or extra text."))

    effort = body.get("reasoning_effort")
    payload: Dict[str, Any] = {
        "modelId": model,
        "systemPrompt": {"instructions": instructions.replace("\x00", " ")},
        "messages": prompt,
        "tools": [],  # Venice server tools; OpenCode's tools travel in the system prompt instead
        "clientTools": [],
        # reasoning only on a no-tools turn (a reasoning turn wants reasoning_content passed back -> 400 on
        # a tool call); _think already encodes this, and _to_chat_payload also reads reasoningMode, so gate both.
        "reasoningMode": "enabled" if (effort in ("medium", "high") and not has_tools) else "disabled",
    }  # max_tokens is ignored by the route
    # temperature / top_p ARE honoured by the plain chat route (the official web client sends them,
    # venice.ai-normalChat.har); _to_chat_payload forwards them. Pass only what the client set.
    if isinstance(body.get("temperature"), (int, float)):
        payload["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        payload["topP"] = body["top_p"]
    if web_enabled:
        payload["_webEnabled"] = True
    if x_search:
        payload["_xSearch"] = True
    # Reasoning / chain of thought: on when asked (`:think`, VENICE_REASONING, or reasoning_effort
    # medium|high) AND the request carries no tools — a reasoning turn emits reasoning_content Venice
    # wants passed back, which breaks tool calls (400), so tool requests never get it.
    if (think or effort in ("medium", "high")) and not has_tools:
        payload["_think"] = True
    if all_attachments:
        payload["_attachments"] = all_attachments
    return payload, has_tools


# ---- Routing: Venice's tools and other foreign tool names -> the tools the client declared ----
# Venice's web route gives the model its own server tools and hides the client's. Two things go wrong:
# the model calls a Venice tool (Venice runs it, it cannot see this computer, the turn ends in
# "tools are not available"), or it names a tool the client does not have (shell, cat, read_file, ...).
# Both are steered to the client's tool that does the same job. Measured 2026-11-19: for a call
# Venice swallows, the stream carries only the tool NAME, never its arguments, so such a call
# cannot be forwarded; it is asked for again, by name, in the right format.
VENICE_SERVER_TOOLS = {  # Venice tool -> the client tools that do that job, best first
    "view_media": ("read",), "get_attachment": ("read",), "hydrate_content": ("read",),
    "web_search": ("websearch", "webfetch"), "web_scrape": ("webfetch",), "web_fetch": ("webfetch",),
    "calculator": ("bash",), "code_interpreter": ("bash",), "python": ("bash",),
}
TOOL_ALIASES = {  # client tool -> names models use for it
    "bash": ("shell", "sh", "zsh", "terminal", "execute", "exec", "run", "run_command", "execute_command",
             "run_shell_command", "run_terminal_cmd", "shell_command", "command", "calculator",
             "code_interpreter", "python", "exec_command", "local_shell", "shell_exec", "bash_command",
             "run_bash", "run_shell", "container.exec", "execute_shell", "exec_shell", "execcommand",
             "bash_tool"),  # bash_tool: qwen-3-8-27b, phase-0c T1 r9 (2026-11-21)
    "read": ("cat", "read_file", "readfile", "view", "view_file", "open", "open_file", "get_file",
             "get_attachment", "view_media", "hydrate_content", "head", "tail"),
    "write": ("write_file", "writefile", "create_file", "save_file", "create", "write_to_file", "replace_file"),
    # patch / replace_in_file / apply_diff carry a diff or patchText we cannot turn into oldString/newString,
    # so they are intentionally NOT routed: the model gets a clear "no such tool" instead of a broken edit.
    "edit": ("edit_file", "str_replace", "str_replace_editor", "replace", "apply_edit", "update_file",
             "search_replace"),
    "grep": ("search", "rg", "ripgrep", "search_files", "grep_search", "find_in_files", "search_code",
             "search_file_content", "codebase_search", "search_in_files", "text_search", "content_search"),
    "glob": ("find", "find_files", "file_search", "search_file_names"),
    "websearch": ("web_search", "search_web", "google", "internet_search", "google_web_search",
                  "google_search", "bing_search", "brave_search", "search_internet"),
    "webfetch": ("web_scrape", "web_fetch", "fetch", "fetch_url", "browse", "open_url", "http_get", "scrape"),
    # the sub-agent tool: `task` in the CLI, `subagent` in the desktop app; models also say Agent / Task
    # (seen live 2026-11-19: "No tool named agent / Task / task")
    "task": ("agent", "sub_agent", "spawn_agent", "launch_agent", "dispatch_agent", "delegate", "delegate_task",
             "new_task", "run_agent", "subagent_call", "call_agent", "start_agent", "task_agent",
             # names models actually used in the survey (research/venice-model-habit-vs-bridge.json)
             "start_sub_agent", "start_subagent", "startsubagent", "spawn_subagent", "launch_subagent",
             "create_agent", "create_subagent", "create_sub_agent"),
    "skill": ("use_skill", "load_skill", "run_skill", "invoke_skill"),
    "question": ("askuserquestion", "ask_user", "ask_followup_question", "questionnaire", "ask",
                 "ask_question", "clarify"),
}
# The same client tool under the names different OpenCode versions give it (CLI: bash, desktop app: shell).
SAME_TOOL = {"bash": ("bash", "shell"), "glob": ("glob", "find"), "task": ("task", "subagent")}


def _have(target: str, declared: Dict[str, str]) -> Optional[str]:
    """The declared tool that plays `target`'s role, or None."""
    return next((declared[n] for n in SAME_TOOL.get(target, (target,)) if n in declared), None)


LISTING_ALIASES = ("ls", "list_files", "list_dir", "list_directory", "listdir", "dir", "list", "tree", "list_dir_tree")
ARG_ALIASES = {  # argument name of the client tool -> names models use for it
    "filePath": ("path", "file", "file_path", "filepath", "filename", "file_name", "target_file"),
    "path": ("filePath", "file_path", "file", "filepath", "filename", "file_name", "target_file",
             "absolute_path", "relative_path", "notebook_path", "dir", "directory", "folder", "cwd"),
    "workdir": ("cwd", "dir", "directory", "working_directory", "working_dir"),
    "include": ("glob", "file_pattern", "include_pattern", "globs", "file_glob"),
    "background": ("run_in_background", "run_async", "detach"),
    "limit": ("head_limit", "max_results", "max", "count"),
    "replaceAll": ("replace_all", "all", "global"),
    "caseSensitive": ("case_sensitive",),
    "agent": ("subagent_type", "agent_type", "subagent", "agent_name", "role", "mode"),
    "id": ("skill_id", "skill_name", "skill", "name"),
    "command": ("cmd", "script", "code", "shell_command", "input", "expression"),
    "oldString": ("old_string", "old_str", "old", "old_text", "search", "find"),
    "newString": ("new_string", "new_str", "new", "new_text", "replace", "replacement"),
    "content": ("contents", "text", "file_text", "data", "body"),
    "pattern": ("query", "regex", "glob", "q", "search", "name"),
    "query": ("q", "search", "pattern", "text", "prompt"),
    "url": ("link", "uri", "href", "address"),
    "prompt": ("task", "instructions", "instruction", "message", "request", "query", "task_description"),
}


def _fit_arguments(args: Any, props: Dict[str, Any], required: Any = ()) -> Any:
    """Unwrap double-nested argument envelopes (e.g. {"arguments": {"command": "..."}}) and rename
    arguments the model named differently (path -> filePath, cmd -> command, ...).
    Only a name the tool expects and did not get is filled, and only from a name it does not know.
    A bare-string call arrives wrapped as {"content": "..."} (tolerant_parser _build_call/_salvage);
    when the tool has exactly one required string parameter and no `content` of its own, the string is
    that parameter's value (measured: 48x `dropped args not in schema: ['content']`)."""
    if not isinstance(args, dict) or not props:
        return args
    args = dict(args)
    if (list(args) == ["content"] and "content" not in props and isinstance(args["content"], str)):
        req_strings = [k for k in (required or []) if k in props and props[k].get("type") in ("string", None)]
        if len(req_strings) == 1:
            return {req_strings[0]: args["content"]}
    # Models frequently double-wrap tool inputs as {"arguments": {...}} or {"name": "...", "arguments": {...}}
    # or Anthropic-style {"input": {...}}. Unwrap before schema pruning so commands are not dropped.
    for _ in range(3):
        unwrapped = False
        for wrap_key in ("arguments", "parameters", "input"):
            if wrap_key in args and wrap_key not in props:
                val = args[wrap_key]
                if isinstance(val, str):
                    s = val.strip()
                    if s.startswith("{") and s.endswith("}"):
                        try:
                            parsed = json.loads(s)
                            if isinstance(parsed, dict):
                                val = parsed
                        except Exception:
                            pass
                if isinstance(val, dict):
                    args.pop(wrap_key)
                    if "name" in args and "name" not in props:
                        args.pop("name", None)
                    for k, v in val.items():
                        args.setdefault(k, v)
                    unwrapped = True
                    break
        if not unwrapped:
            break
    for key, others in ARG_ALIASES.items():
        if key in props and key not in args:
            for other in others:
                if other in args and other not in props:
                    args[key] = args.pop(other)
                    break
    return args


_HTTP_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
_PREFIX_RE = re.compile(r"^(?:functions|default_api|tools)\.", re.I)


def _coerce_arguments(args: Any, props: Dict[str, Any]) -> Any:
    """Fix argument VALUES (not just names), keyed off the tool's own schema:
    a command given as a list -> one string; a url the model put in prompt; the sub-agent tool's
    required agent/description when the model left them out."""
    if not isinstance(args, dict) or not props:
        return args
    args = dict(args)
    if "command" in props and isinstance(args.get("command"), list):
        args["command"] = " ".join(str(x) for x in args["command"])  # Codex ["bash","-lc","…"]
    if "url" in props and not isinstance(args.get("url"), str):
        for k in ("prompt", "query", "text", "input"):
            m = _HTTP_RE.search(args.get(k) or "") if isinstance(args.get(k), str) else None
            if m:
                args["url"] = m.group(0)
                if k not in props:
                    args.pop(k, None)  # the source key is not a real argument of this tool
                break
    if "agent" in props:
        v = args.get("agent")
        if isinstance(v, str) and v.strip():
            v = v.strip().lower().replace(" ", "-")
            args["agent"] = "general" if v in ("general-purpose", "general_purpose", "default") else v
        else:
            args["agent"] = "general"  # the sub-agent tool requires it; general is the safe default
    if "description" in props and not isinstance(args.get("description"), str) and isinstance(args.get("prompt"), str):
        args["description"] = args["prompt"].strip().splitlines()[0][:80] if args["prompt"].strip() else "subtask"
    if "questions" in props:
        # OpenCode's question tool wants questions[{question, header, options, multiple}].
        qs = args.get("questions")
        if not isinstance(qs, list):
            text = next((args.get(k) for k in ("question", "prompt", "query", "text") if isinstance(args.get(k), str)), None)
            qs = [{"question": text}] if text else []
        norm = []
        for q in qs:
            if not isinstance(q, dict):
                q = {"question": str(q)}
            q = dict(q)
            if "multiSelect" in q:
                q["multiple"] = q.pop("multiSelect")
            q.setdefault("header", (q.get("question") or "")[:30])
            q.setdefault("options", q.get("options") or [])
            # the item is additionalProperties:false: keep only the four allowed keys
            norm.append({k: q[k] for k in ("question", "header", "options", "multiple") if k in q})
        args["questions"] = norm
    # OpenCode 2.3.2 tools are additionalProperties:false, so any key not in the schema is rejected.
    # Drop the leftovers from aliasing (e.g. `replace_all` after it became `replaceAll`).
    extra = [k for k in args if k not in props]
    for k in extra:
        args.pop(k, None)
    if extra:
        sys.stderr.write(f"[venice-bridge] dropped args not in schema: {extra}\n")
    return args


_SEARCH_REPLACE = re.compile(
    r"<{3,}\s*SEARCH\s*\n(.*?)\n={3,}\s*\n(.*?)\n>{3,}\s*REPLACE", re.S | re.I)
_MULTI_NAMES = ("multiedit", "multi_edit", "multiple_edit", "patch_file", "edit_block")
_READMANY_NAMES = ("read_many_files", "read_files", "read_multiple_files", "readmany")


def _one_edit(item: Dict[str, Any], path: Any) -> Optional[Dict[str, Any]]:
    old = next((item[k] for k in ("oldString", "old_string", "old_str", "old_text", "search", "old") if k in item), None)
    new = next((item[k] for k in ("newString", "new_string", "new_str", "new_text", "replace", "new") if k in item), None)
    p = item.get("path") or item.get("filePath") or item.get("file_path") or item.get("file") or path
    if isinstance(old, str) and isinstance(new, str):
        return {"path": p, "oldString": old, "newString": new}
    return None


def _expand_call(name: str, args: Any, declared: Dict[str, str]) -> Optional[List[Tuple[str, Dict[str, Any]]]]:
    """One model call that stands for SEVERAL (MultiEdit, a SEARCH/REPLACE diff, read_many_files) ->
    a list of (tool, args). None when it is an ordinary single call. The foreign call is never declared
    (a declared name wins and is left alone)."""
    low = _PREFIX_RE.sub("", name).lower()
    if not isinstance(args, dict) or low in declared:
        return None
    edit = _have("edit", declared)
    read = _have("read", declared)
    path = args.get("path") or args.get("filePath") or args.get("file_path") or args.get("file")
    # MultiEdit / patch_file: a list of edit items
    if edit and (low in _MULTI_NAMES or isinstance(args.get("edits"), list)) and isinstance(args.get("edits"), list):
        out = [(edit, e) for e in (_one_edit(i, path) for i in args["edits"] if isinstance(i, dict)) if e]
        return out or None
    # a Cline-style diff string with SEARCH/REPLACE blocks
    if edit and isinstance(args.get("diff"), str) and "SEARCH" in args["diff"].upper():
        out = [(edit, {"path": path, "oldString": o, "newString": n}) for o, n in _SEARCH_REPLACE.findall(args["diff"]) if path]
        return out or None
    # read_many_files: a list of paths
    if read and (low in _READMANY_NAMES or isinstance(args.get("paths"), list)) and isinstance(args.get("paths"), list):
        out = [(read, {"path": p}) for p in args["paths"] if isinstance(p, str)]
        return out or None
    return None


def _route_call(name: str, args: Any, declared: Dict[str, str]) -> Optional[Tuple[str, Any]]:
    """(client tool, arguments) for a call to a tool the client does not have, or None."""
    low = _PREFIX_RE.sub("", name).lower()  # functions.shell / default_api.read -> the bare name
    args = args if isinstance(args, dict) else {}
    # `execute` is a declared Code Mode tool (arg: code) AND a bash alias; a model that means "run a
    # command" and says execute{"command":...} must reach the shell, not the broken Code Mode call.
    if low == "execute" and ("command" in args or "cmd" in args) and "code" not in args and _have("bash", declared):
        return _have("bash", declared), args
    if low in LISTING_ALIASES:
        where = next((args[k] for k in ("path", "dir", "directory", "folder") if isinstance(args.get(k), str)), ".")
        if "list" in declared:
            return declared["list"], {"path": where}
        if _have("bash", declared):
            return _have("bash", declared), {"command": "ls -la " + shlex.quote(where)}
        return None
    if low in ("view_media", "get_attachment", "hydrate_content"):
        # Venice's media tools take ids; such a call is a file read only when the id is a file path
        ids = args.get("media_ids") or args.get("media_id") or args.get("id") or args.get("path") or args.get("file")
        first = ids[0] if isinstance(ids, list) and ids else ids
        if isinstance(first, str) and ("/" in first or "." in first) and _have("read", declared):
            return _have("read", declared), {"filePath": first}
        return None
    for target, aliases in TOOL_ALIASES.items():
        if (low in aliases or low in SAME_TOOL.get(target, ())) and _have(target, declared):
            return _have(target, declared), args
    return None


def _route_hint(declared: Dict[str, str]) -> str:
    """One sentence for the prompt: which client tool does what Venice's own tools would do."""
    jobs = [("read a file", "read"), ("run a command", "bash"), ("search the web", "websearch"),
            ("fetch a web page", "webfetch"), ("start a sub-agent", "task")]
    have = [f"{job} with `{_have(tool, declared)}`" for job, tool in jobs if _have(tool, declared)]
    return ("Venice's own tools (view_media, get_attachment, web_search, web_scrape) and built-in function "
            "calling do not reach this computer: never use them."
            + (" Here you " + ", ".join(have) + "." if have else ""))


def _steer_reminder(trigger: str, venice: Optional[Dict[str, List[str]]], declared: Dict[str, str]) -> str:
    """The reminder for a reply Venice swallowed, naming the tools involved and where they go."""
    venice = venice or {}
    if trigger == "server_tool":
        used = venice.get("recognized") or ["view_media"]
        routes = []
        for tool in used:
            target = next((_have(t, declared) for t in VENICE_SERVER_TOOLS.get(tool, ()) if _have(t, declared)), None)
            routes.append(f"instead of `{tool}` use `{target}`" if target else f"`{tool}` has no counterpart here")
        return ("you called " + ", ".join(f"`{t}`" for t in used) + ", which is one of Venice's own tools. It cannot "
                "see the user's computer and its result never reaches the agent. " + "; ".join(routes) + ". "
                'Call it now by replying with ONLY {"tool_calls":[{"name":"<exact tool name>","arguments":{...}}]}.')
    names = [declared.get(n.lower(), n) for n in venice.get("unrecognized") or []]
    extra = (" You tried to call " + ", ".join(f"`{n}`" for n in names[:4]) + " that way and it was not run: "
             "send the same call again as that JSON object.") if names else ""
    return NATIVE_CALL_REMINDER + extra


_TAGGED_CALLS = re.compile(r"<tool_calls?>(.*?)(?:</tool_calls?>|$)", re.S | re.I)
_CALL_MARKER = re.compile(r"@@[A-Z_]+@@")  # e.g. @@WRITE@@, written right before the tag


_NEXT_KEY = r'\s*,\s*"(?:[^"\\\n]|\\.){0,80}"\s*:'
_AFTER_VALUE = re.compile(r'(?:%s|\s*[}\]](?:\s*[}\]])*\s*(?:$|,\s*[{\["]|\]))' % _NEXT_KEY)
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _loose_string(s: str, i: int, key: bool) -> Tuple[str, int]:
    """A JSON string whose inner quotes may be unescaped (models do that with file content).
    A quote ends it only where the object can go on: before `:` for a key, before `, "key":` or a
    closing brace for a value. Raw line breaks are kept."""
    out, k = [], i + 1
    while k < len(s):
        c = s[k]
        if c == "\\" and k + 1 < len(s):
            n = s[k + 1]
            if n == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", s[k + 2:k + 6] or ""):
                out.append(chr(int(s[k + 2:k + 6], 16)))
                k += 6
                continue
            out.append(_ESCAPES.get(n, "\\" + n))
            k += 2
            continue
        if c == '"' and (re.match(r"\s*:", s[k + 1:]) if key else _AFTER_VALUE.match(s, k + 1)):
            return "".join(out), k + 1
        out.append(c)
        k += 1
    raise ValueError("unterminated string")


def _loose_value(s: str, i: int) -> Tuple[Any, int]:
    """One JSON value starting at s[i], read with _loose_string. Raises ValueError when it is not one."""
    i = len(s) - len(s[i:].lstrip())
    c = s[i:i + 1]
    if c == "{":
        obj: Dict[str, Any] = {}
        i += 1
        while True:
            i = len(s) - len(s[i:].lstrip())
            if s[i:i + 1] == "}":
                return obj, i + 1
            if s[i:i + 1] != '"':
                raise ValueError("key expected")
            key, i = _loose_string(s, i, True)
            i = s.index(":", i) + 1
            obj[key], i = _loose_value(s, i)
            i = len(s) - len(s[i:].lstrip())
            if s[i:i + 1] == ",":
                i += 1
            elif s[i:i + 1] != "}":
                raise ValueError("object not closed")
    if c == "[":
        arr: List[Any] = []
        i += 1
        while True:
            i = len(s) - len(s[i:].lstrip())
            if s[i:i + 1] == "]":
                return arr, i + 1
            val, i = _loose_value(s, i)
            arr.append(val)
            i = len(s) - len(s[i:].lstrip())
            if s[i:i + 1] == ",":
                i += 1
            elif s[i:i + 1] != "]":
                raise ValueError("list not closed")
    if c == '"':
        return _loose_string(s, i, False)
    m = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null").match(s, i)
    if not m:
        raise ValueError("value expected")
    return json.loads(m.group(0)), m.end()


def _tagged_calls(text: str) -> Tuple[List[Dict[str, Any]], str]:
    """(calls, prose) for calls written as <tool_calls>{"name":..,"arguments":{..}}</tool_calls>.

    DeepSeek V4.1 Flash wrote this after a first refused native call (2026-11-18): one or more
    bare call objects, or a list of them, inside the tags, after a sentence of prose, with the
    quotes inside the file content left unescaped. Anything between the tags that cannot be read
    as call objects voids the whole reply: a half-read write would put a cut file on disk.
    """
    calls: List[Dict[str, Any]] = []
    for m in _TAGGED_CALLS.finditer(text):
        body, i = m.group(1), 0
        while body[i:].strip(" \t\r\n,"):
            i = len(body) - len(body[i:].lstrip(" \t\r\n,"))
            try:
                try:
                    val, i = json.JSONDecoder(strict=False).raw_decode(body, i)
                except ValueError:
                    val, i = _loose_value(body, i)
            except (ValueError, IndexError):
                return [], ""
            if isinstance(val, dict) and isinstance(val.get("tool_calls"), list):
                val = val["tool_calls"]
            for item in val if isinstance(val, list) else [val]:
                built = _build_call(item)
                if not built:
                    return [], ""
                calls.append(built)
    return calls, _CALL_MARKER.sub("", _TAGGED_CALLS.sub("", text)).strip()


_BAR = r"(?:[｜|]\s*DSML\s*[｜|]\s*)?"
_INVOKE = re.compile(rf'<\s*{_BAR}invoke\s+name="([^"]+)"[^>]*>(.*?)(?:<\s*/\s*{_BAR}invoke\s*>|$)', re.S | re.I)
_PARAMETER = re.compile(rf'<\s*{_BAR}parameter\s+name="([^"]+)"([^>]*)>(.*?)<\s*/\s*{_BAR}parameter\s*>', re.S | re.I)
_NATIVE_MARKUP = re.compile(rf"<\s*/?\s*{_BAR}(?:function_calls|calls|invoke|parameter)\b[^>]*>", re.I)
# what a reply looks like, lower-cased and without blanks, where native call markup starts
NATIVE_STARTS = ("<tool_call", "<｜dsml｜", "<|dsml|", "<invoke", "<function_calls")


def _native_calls(text: str) -> Tuple[List[Dict[str, Any]], str]:
    """(calls, prose) for calls written in the model's own markup:
    <｜DSML｜ invoke name="shell"><｜DSML｜ parameter name="command" string="true">ls</｜DSML｜ parameter>...
    (DeepSeek on the plain chat route, measured 2026-11-19) and the same without the DSML bars.
    A parameter marked string="false" holds JSON; everything else is taken as text, unchanged."""
    calls: List[Dict[str, Any]] = []
    for m in _INVOKE.finditer(text):
        args: Dict[str, Any] = {}
        for pm in _PARAMETER.finditer(m.group(2)):
            value: Any = pm.group(3)
            if re.search(r'string\s*=\s*"false"', pm.group(2), re.I):
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            args[pm.group(1)] = value
        calls.append({"name": m.group(1), "arguments": args})
    first = _NATIVE_MARKUP.search(text)
    return calls, (text[:first.start()] if first else text).strip()


def _read_reply(
    text: str, tools: Optional[List[Dict[str, Any]]] = None
) -> Tuple[Optional[List[Dict[str, Any]]], str, bool]:
    """(tool calls or None, text for the user, unreadable) from a finished model reply.

    `unreadable` = the reply holds a tool_calls envelope from which no call could be read at all
    (cut short, no name, ...). An explicit empty list is not that, and neither is prose that quotes
    the format (its calls name tools that do not exist and are dropped).

    The reading is tolerant_parser's (a port of opencode-venice-web): calls are found bare, fenced,
    after prose, in several envelopes, and with the structural slips real models make.
    A call must name a tool the request declared; a call with wrong arguments is passed on,
    because OpenCode answers it with an error the model can correct.
    """
    parsed = parse_tool_reply(text)
    tagged = "<tool_call" in text.lower()
    if tagged:  # read the tagged form first: its reader knows where a file's content really ends
        found, prose = _tagged_calls(text)
        if found:
            parsed = dict(parsed, calls=found, content=prose)
    native = bool(_INVOKE.search(text))
    if native and not (tagged and parsed["calls"]):
        found, prose = _native_calls(text)
        if found:
            parsed = dict(parsed, calls=found, content=prose)
        tagged = True  # same handling below: with no readable call it is unreadable, never shown raw
    declared = {}
    for t in tools or []:
        name = (t.get("function") or {}).get("name") if isinstance(t, dict) else None
        if name:
            declared[name.lower()] = name
    props = {}
    reqs: Dict[str, List[str]] = {}
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name") and isinstance(fn.get("parameters"), dict):
            props[fn["name"]] = fn["parameters"].get("properties") if isinstance(
                fn["parameters"].get("properties"), dict) else {}
            reqs[fn["name"]] = fn["parameters"].get("required") if isinstance(
                fn["parameters"].get("required"), list) else []
    calls: List[Tuple[str, Any]] = []
    unknown: List[Tuple[str, Any]] = []
    subcalls: List[Tuple[str, Any]] = []
    for c in parsed["calls"] or []:
        expanded = _expand_call(c["name"], c["arguments"], declared) if declared else None
        if expanded:
            sys.stderr.write(f"[venice-bridge] expanded {c['name']} -> {len(expanded)} x {expanded[0][0]}\n")
            subcalls.extend(expanded)
        else:
            subcalls.append((c["name"], c["arguments"]))
    for rawname, rawargs in subcalls:
        c = {"name": rawname}
        name, args = (declared.get(rawname.lower()) if declared else rawname), rawargs
        if (name and name.lower() == "execute" and isinstance(args, dict) and "code" not in args
                and ("command" in args or "cmd" in args) and _have("bash", declared)):
            name = _have("bash", declared)  # declared Code Mode `execute`, but used as a shell
        if declared and not name:
            routed = _route_call(c["name"], args, declared)
            if routed:
                name, args = routed
                sys.stderr.write(f"[venice-bridge] routed call {c['name']} -> {name}\n")
        if (name and name == _have("edit", declared) and isinstance(args, dict)
                and ("content" in args or "contents" in args or "file_text" in args)
                and not any(k in args for k in ("oldString", "old_string", "old_str", "diff"))
                and _have("write", declared)):
            name = _have("write", declared)  # an edit call with a whole-file body is really a write
        if (name and name == _have("edit", declared) and c["name"].lower() not in declared
                and not any(k in (args or {}) for k in ("oldString", "old_string", "old_str", "newString",
                                                        "new_string", "new_str", "replacement"))):
            # routed to edit but the payload is a format we cannot fill (Cursor code_edit, text_editor view, ...):
            # let it pass through as the unknown name so the client answers "no such tool" clearly
            name = None
        if name and declared:
            tp = props.get(name) or {}
            args = _coerce_arguments(_fit_arguments(args, tp, reqs.get(name) or []), tp)
        (calls if name else unknown).append((name or c["name"], args))
    if unknown and not calls and len(parsed["content"]) < 200:
        # the reply is only a call, with an invented tool name: OpenCode tells the model it does not exist.
        # A long text around it is prose that quotes the format, not a call.
        calls, unknown = unknown, []
    if unknown:
        sys.stderr.write(f"[venice-bridge] dropped calls to undeclared tools: {[n for n, _ in unknown]}\n")
    if calls:
        return [
            {
                "index": idx,
                "id": f"call_{uuid.uuid4().hex[:16]}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
            for idx, (name, args) in enumerate(calls)
        ], parsed["content"], False
    if tagged or ('"tool_calls"' in text and (parsed["saw_envelope"] or parsed["broken"])):
        # an envelope with no usable call: never show it raw
        # ponytail: a long text that ends in a cut call is read as prose (the judge may still catch it),
        # because a long answer that quotes the format must not cost two 441k-char retries
        # with tags, the prose is what stands outside them: the parser's own content mixes in call pieces
        content = (_native_calls(text)[1] if native else
                   _CALL_MARKER.sub("", _TAGGED_CALLS.sub("", text)).strip() if tagged else parsed["content"])
        unreadable = (len(content) < 200 and not parsed["calls"]
                      and not re.search(r'"tool_calls"\s*:\s*\[\s*\]', text))
        if unreadable:
            sys.stderr.write(f"[venice-bridge] reply mentions tool_calls but no call could be read: {text[:300]!r}\n")
        return None, content, unreadable
    return None, text, False


def _try_parse_tool_calls(text: str, tools: Optional[List[Dict[str, Any]]] = None) -> Optional[List[Dict[str, Any]]]:
    return _read_reply(text, tools)[0]


def _read_head(stream) -> Tuple[int, Dict[str, str]]:
    """Read the HTTP response head `curl -i` prints before the body. Status 0 = no response."""
    while True:
        status_line = stream.readline().decode("latin-1").strip()
        if not status_line:
            return 0, {}
        parts = status_line.split(" ", 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        headers: Dict[str, str] = {}
        for raw in iter(stream.readline, b""):
            line = raw.decode("latin-1").strip()
            if not line:
                break
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        if not 100 <= status < 200:  # skip interim heads such as 100 Continue
            return status, headers


def _quota_error() -> Tuple[int, str]:
    # ponytail: answered as 403, not 429. OpenCode retries 429 silently and for hours
    # (7 upstream calls in 45 s, nothing shown); a 403 ends the turn and shows this message.
    mins = max(1, round((POOL.earliest_reset() - time.time()) / 60))
    return 403, f"Venice web chat quota is used up for this account. Resets in about {mins} min."


def _upstream_error(status: int, detail: str) -> Tuple[int, str, Tuple[str, Any]]:
    """Map a failed Venice response to (client status, message, (error type, error code))."""
    try:  # the body is usually {"error":"userPromptTooLong"} or {"error":{"message":"..."}}
        err = json.loads(detail).get("error")
        detail = (err.get("message") if isinstance(err, dict) else err) or detail
    except Exception:
        pass
    detail = str(detail)
    if status == 429:
        quota = VENICE.chat_quota() or {}
        if quota.get("remaining") == 0:
            # unknown reset time: hold off one minute, then ask Venice again
            VENICE.quota_until = quota.get("reset_at") or (time.time() + 60)
            return (*_quota_error(), ("venice_quota_exhausted", 429))
        # quota is left: a passing overload. Stay 429 so the client backs off and retries;
        # locking here would block the bridge until the daily reset.
        return 429, f"Venice is rate limiting right now (not the quota): {detail or 'no detail'}", ("venice_rate_limited", 429)
    kind = ("venice_upstream_error", status)
    if status == 413:
        return 400, f"The request is larger than Venice accepts (context length exceeded): {detail or 'no detail'}", (
            "invalid_request_error", "context_length_exceeded")
    if status == 402:
        return 402, f"Your Venice plan or credits do not cover this model: {detail or 'no detail'}", kind
    if status in (401, 403):
        return status, f"Venice rejected the request ({status}): {detail or 'no detail'}", kind
    if status == 0:
        return 502, f"No response from Venice: {detail or 'curl produced no output'}", kind
    return 502, f"Venice answered HTTP {status}: {detail or 'no detail'}", kind


def _iter_chat_events(proc: subprocess.Popen, unread: Optional[List[str]] = None,
                      venice: Optional[Dict[str, List[str]]] = None, cut_server_tools: bool = False):
    """Yield (kind, text) from the Responses-style SSE stream. Unparsable lines go to `unread` (first 5).

    Kinds: reasoning, text, done, error, and server_loop when Venice's own agent loop kept refusing
    the model's native tool calls (the tools-mode caller retries that with NATIVE_CALL_REMINDER).
    """
    assert proc.stdout is not None
    event = ""
    item_types: Dict[Any, str] = {}
    steps: List[str] = []  # Venice's own tool-detection steps in this response
    swallowed = 0
    plain = False
    for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", "ignore").strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        try:
            evt = json.loads(line)
        except Exception:
            if unread is not None and len(unread) < 5:
                unread.append(line[:200])
            continue
        if not event and isinstance(evt.get("kind"), str):
            # the plain chat route: one JSON object per line, no event names, no closing event
            plain = True
            if evt["kind"] in ("", "content"):
                # a content line can carry visible text and/or a reasoning_content field (web "/think")
                if isinstance(evt.get("content"), str) and evt["content"]:
                    yield ("text", evt["content"])
                if isinstance(evt.get("reasoning_content"), str) and evt["reasoning_content"]:
                    yield ("reasoning", evt["reasoning_content"])
            elif evt["kind"] in ("thinking", "reasoning") and isinstance(evt.get("content"), str) and evt["content"]:
                yield ("reasoning", evt["content"])
            elif evt.get("error"):
                yield ("error", json.dumps(evt, ensure_ascii=False)[:500])
            continue
        kind = evt.get("type") or event
        if kind == "response.output_item.added":
            item_types[evt.get("output_index")] = (evt.get("item") or {}).get("type") or ""
        elif kind == "veniceai:tool_call_generation.in_progress":
            detected = [d for d in evt.get("detected_tools") or [] if isinstance(d, dict)]
            steps.append(json.dumps(detected, ensure_ascii=False)[:200])
            if venice is not None:  # which tools Venice saw, for the reminder that steers the retry
                for d in detected:
                    key, tool = ("recognized", d.get("tool")) if d.get("type") == "Recognized" else ("unrecognized", d.get("name"))
                    if isinstance(tool, str) and tool not in venice.setdefault(key, []):
                        venice[key].append(tool)
            # A step in which Venice handled a call itself: either it ran one of its own tools
            # (Recognized: it cannot see this computer) or refused a native call (Unrecognized).
            # Both count as steps and are left alone at first: inside one response the model learns
            # from Venice's own "not found" answers and often falls back to our text format.
            # Cutting at the first Recognized step was tried on 2026-11-19 and made DeepSeek V4.1
            # Flash fail 7 of 8 turns, because every retry started with the same native call.
            own = cut_server_tools and any(d.get("type") == "Recognized" for d in detected)
            if own or any(d.get("type") == "Unrecognized" for d in detected):
                swallowed += 1
            if own and swallowed > SERVER_LOOP_STEPS:
                sys.stderr.write(f"[venice-bridge] venice ran its own tool: {steps[-1]}\n")
                yield ("server_tool", "the model called one of Venice's own tools instead of the agent's; retrying usually works")
                return
            if swallowed > SERVER_LOOP_STEPS:
                # Venice took the model's call for one of its own server tools. It answers the model
                # "Tool not found" up to 10 times and the reply ends as "my tools are broken"
                # (seen live 2026-11-18). The model often switches to our JSON format after the first
                # refusal or two, so the first steps are left alone: cutting at the first one made a
                # DeepSeek edit task fail 16 times in a row. Past that nothing useful follows.
                sys.stderr.write(f"[venice-bridge] venice swallowed a tool call: {steps[-1]}\n")
                yield ("server_loop", "Venice tried to run the tool call itself instead of returning it; retrying usually works")
                return
        elif kind.startswith("veniceai:") and kind.endswith(".done") and "tool_call_generation" not in kind:
            steps.append(kind)
        elif kind == "response.output_text.delta":
            delta = evt.get("delta")
            if isinstance(delta, str) and delta:
                # reasoning and answer share this event; the item type tells them apart
                yield ("reasoning" if item_types.get(evt.get("output_index")) == "reasoning" else "text", delta)
        elif kind in ("response.reasoning_text.delta", "response.reasoning_summary_text.delta"):
            if evt.get("delta"):
                yield ("reasoning", evt["delta"])
        elif kind == "response.completed":
            if steps:  # Venice tried to run tools itself; the model may have been told "Tool not found"
                sys.stderr.write(f"[venice-bridge] venice server steps ({len(steps)}): {steps[-6:]}\n")
            yield ("done", "")
        elif kind in ("error", "response.failed", "response.incomplete") or evt.get("error"):
            yield ("error", json.dumps(evt, ensure_ascii=False)[:500])
    if plain:
        # the chat route sends no closing event: a clean end of the HTTP response is the signal
        # (curl exits non-zero when the body is cut short)
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        if proc.returncode == 0:
            yield ("done", "")


def _cleanup(handle) -> None:
    proc, tmpdir, hdr_file, body_file = handle
    try:
        proc.wait(timeout=2)
    except Exception:
        proc.kill()
    shutil.rmtree(tmpdir, ignore_errors=True)


def _open_upstream(payload: Dict[str, Any], max_time: Optional[float] = None, _failovers: int = 0):
    """Start one upstream call and read its HTTP head: (handle, None), or (None, (client status, error body)).
    One retry with a fresh JWT and attestation when Venice says the token is stale.
    When the opt-in cookie pool is enabled and the active account hits quota (429 with remaining==0),
    switches deterministically to the next available account."""
    client = POOL.active_client()
    for attempt in (0, 1):
        t0 = time.time()
        try:
            handle = client.open_chat_stream(payload, force_refresh=attempt > 0, max_time=max_time)
        except Exception as exc:
            return None, (502, {"error": {"message": str(exc), "type": "bridge_error"}})
        proc = handle[0]
        t1 = time.time()
        status, head = _read_head(proc.stdout)
        # where the wait before the first byte goes: our token minting vs Venice reading the prompt
        sys.stderr.write("[venice-bridge] timing open: auth=%.1fs head=%.1fs status=%s model=%s route=%s\n" % (
            t1 - t0, time.time() - t1, status, payload.get("modelId"),
            "chat" if _uses_chat_route(payload.get("modelId")) else "agent"))
        if status == 401 and attempt == 0:
            sys.stderr.write("[venice-bridge] upstream 401, refreshing the session and retrying once\n")
            _cleanup(handle)
            continue
        break
    if status == 200:
        return handle, None
    # A failed or empty upstream reply must reach the client as an error, never as an empty 200.
    detail = proc.stdout.read(2000).decode("utf-8", "replace").strip()
    if status == 0:
        detail = proc.stderr.read(500).decode("utf-8", "replace").strip()
    _cleanup(handle)
    model = payload.get("modelId")
    if ROUTE == "auto" and status == 400 and "modelNotFound" in detail and model not in AGENT_ONLY_MODELS:
        AGENT_ONLY_MODELS.add(model)  # the chat route does not have this model: the agent route does
        sys.stderr.write(f"[venice-bridge] {model} is not on the chat route; using the agent route for it\n")
        return _open_upstream(payload, max_time, _failovers)
    if status == 429 and POOL.is_enabled():
        quota = client.chat_quota() or {}
        if quota.get("remaining") == 0:
            client.quota_until = quota.get("reset_at") or (time.time() + 60)
            nxt = POOL.failover(client)
            if nxt is not None and _failovers < len(POOL._clients):
                return _open_upstream(payload, max_time, _failovers + 1)
            code, message = _quota_error()
            sys.stderr.write(f"[venice-bridge] upstream 429: {message}\n")
            return None, (code, {"error": {"message": message, "type": "venice_quota_exhausted", "code": 429}})
        msg = f"Venice is rate limiting right now (not the quota): {detail or 'no detail'}"
        sys.stderr.write(f"[venice-bridge] upstream 429: {msg}\n")
        return None, (429, {"error": {"message": msg, "type": "venice_rate_limited", "code": 429}})
    if status == 403 and ("challenge" in head.get("cf-mitigated", "") or detail.startswith("<")):
        # a Cloudflare challenge is neither a quota nor an auth problem: never lock on it
        message = ("Cloudflare challenged the request. Open venice.ai in your browser, refresh the "
                   "cookie file (or sync your session daemon), and try again.")
        sys.stderr.write("[venice-bridge] upstream 403: Cloudflare challenge\n")
        return None, (502, {"error": {"message": message, "type": "venice_upstream_error", "code": 403}})
    code, message, (kind, err_code) = _upstream_error(status, detail)
    sys.stderr.write(f"[venice-bridge] upstream {status}: {message}\n")
    return None, (code, {"error": {"message": message, "type": kind, "code": err_code}})


# In-bridge retries, tools mode only. Every extra attempt resends ~441k chars and takes 15-70 s,
# so the budgets are small. Read at call time: tests set them on the module.
MAX_FORMAT_RETRIES = int(os.environ.get("VENICE_BRIDGE_FORMAT_RETRIES", "2"))  # transport + format problems
MAX_JUDGE_RETRIES = int(os.environ.get("VENICE_BRIDGE_JUDGE_RETRIES", "1"))
JUDGE_ENABLED = os.environ.get("VENICE_BRIDGE_JUDGE", "1") != "0"
JUDGE_MODEL = os.environ.get("VENICE_BRIDGE_JUDGE_MODEL") or "qwen3-5-35b-a3b"  # unverified on this account
JUDGE_TIMEOUT = 30  # seconds, the whole judge call (curl --max-time)

# Retry reminders: one extra last user message, after TOOL_REMINDER's. Adapted from opencode-venice-web
# (emulate.mjs:89-114) to our format, where plain text is the final answer. Untested on live models.
NATIVE_CALL_REMINDER = (
    "your tools work. Built-in function calling and native tool-call syntax are not available here: "
    'the ONLY way to call a tool is to reply with ONLY {"tool_calls":[{"name":"<exact tool name>","arguments":{...}}]}. '
    'Ignore any earlier "Tool not found" message and call the tool you need now that way.'
)
BROKEN_ENVELOPE_REMINDER = (
    "your last reply was not valid JSON, so its tool calls could not be executed. Send them again as exactly "
    'one valid JSON object: {"tool_calls":[{"name":"...","arguments":{...}}]}, closing every object and '
    "escaping quotes and line breaks inside strings."
)
EMPTY_RETRY_REMINDER = (
    "your last reply was empty. Reply now with the answer as normal text, or, if you still need a tool, with "
    'ONLY {"tool_calls":[{"name":"<exact tool name>","arguments":{...}}]}. Never send an empty reply.'
)
CONTINUE_REMINDER = (
    "your last reply did not finish the request: work is still open and you did not call a tool. ACT NOW: "
    'call the next tool now with ONLY {"tool_calls":[{"name":"<exact tool name>","arguments":{...}}]}. '
    "Answer in normal text only when the whole request is finished, or to ask the user something only they can decide."
)
FAILURE_RETRY_REMINDER = (
    "the latest tool output shows a failure (an error, a failing test, a missing file or command, or a non-zero "
    'exit) that your reply does not handle. Fix it now: call the next tool with ONLY {"tool_calls":[...]}, or, '
    "if it cannot be fixed, say so plainly in normal text."
)
RESULT_REMINDER = (
    "the work is done, but your reply does not tell the user the result they asked for. Answer again in normal "
    "text and put the concrete results in it (the numbers, names, findings or answers, and what changed); saying "
    "it is done or naming a file is not enough. Call a tool only if a value is still missing."
)
FORMAT_REMINDERS = {"server_loop": NATIVE_CALL_REMINDER, "unreadable": BROKEN_ENVELOPE_REMINDER,
                    "empty": EMPTY_RETRY_REMINDER, "transport": None}
JUDGE_REMINDERS = {"stopped_early": CONTINUE_REMINDER, "ignored_failure": FAILURE_RETRY_REMINDER,
                   "missing_result": RESULT_REMINDER}

# Verbatim from opencode-venice-web (emulate.mjs:229-236).
JUDGE_PROMPT = """You review one turn of an AI coding agent. The agent works for a user and can call tools (shell, read, edit, write, ...). It has just received tool results, and its latest reply calls no tool, so the turn is about to END and control goes back to the user. Decide whether ending here is right. The conversation can be in any language.
Answer with exactly one JSON object and nothing else: {"verdict":"...","reason":"one short sentence"}
verdict is one of:
- "finished": the request is done AND the reply gives the user what they asked to be told: the numbers, names, findings, answers or summary the request asks for (an honest report that something failed or cannot be done also counts). When the request only asks for an action (create, fix, rename, delete, compute something into a file) and does not ask to be told anything, confirming it is enough.
- "missing_result": the work is done, but the reply does not state a result the user asked to be told - it only says the work is done, that tests pass, or where the results were saved, while the request asked to be told the values, names, counts, findings or answers themselves ("tell me ...", or a question such as which, how many, what).
- "needs_user": the agent asks the user a real question: a decision, information or permission that only the user can give and that the request did not already give. A reply that asks nothing is never "needs_user".
- "stopped_early": work the user asked for is still open and the agent could do it now with its tools - for example the reply announces or lists next steps, promises to run, check or fix something, says it will continue, asks permission for something the user already asked for, or claims its tools are unavailable or failing although the tool results shown here worked.
- "ignored_failure": the latest tool output shows a failure (an error, a failing test, a missing file or command, a non-zero exit) that the reply ignores or presents as success."""
VERDICTS = {"finished", "needs_user", "stopped_early", "ignored_failure", "missing_result"}


STREAM_GATE_CHARS = 200  # a reply this long that is not a call is shown while it is written


class _LiveGate:
    """Shows a prose reply while it arrives, in tools mode, without ever showing a tool call.

    Why a gate and not plain streaming: OpenCode retries a turn that fails before the status line
    (a 502) but not one that fails inside a started stream. So the first STREAM_GATE_CHARS are held:
    a reply that turns out to be a call, a refused native call or an early stop ("I will now run
    the tests.") is still short at that point and keeps the retry and judge paths. Past that the
    text is prose and goes out as it comes; a tail that may still become a call is kept back.
    """

    def __init__(self, start, emit) -> None:
        self._start, self._emit = start, emit
        self.reset()

    def reset(self) -> None:
        self.buf, self.sent, self.live = "", 0, False

    def feed(self, delta: str) -> None:
        self.buf += delta
        if not self.live:
            head = self.buf.lstrip()
            compact = re.sub(r"\s+", "", self.buf.lower())
            if (len(head) < STREAM_GATE_CHARS or head[0] in "{`<" or '"tool_calls"' in self.buf
                    or any(start in compact for start in NATIVE_STARTS)):
                return
            self.live = True
            self._start()
        safe = self._safe_end()
        if safe > self.sent:
            self._emit(self.buf[self.sent:safe])
            self.sent = safe

    def _safe_end(self) -> int:
        """Up to where the text can be shown: from a brace that may open a call on, nothing is."""
        low = self.buf.lower()
        t = low.find("<", self.sent)
        while t >= 0:  # a <tool_call(s)> tag, whole or still being written
            ahead = re.sub(r"\s+", "", low[t:t + 24])
            if any(start.startswith(ahead[:len(start)]) for start in NATIVE_STARTS):
                marker = _CALL_MARKER.search(self.buf, self.sent, t)  # the @@WRITE@@ in front of it
                return marker.start() if marker and not self.buf[marker.end():t].strip() else t
            t = low.find("<", t + 1)
        i = self.buf.find("{", self.sent)
        while i >= 0:
            tail = re.sub(r"\s+", "", self.buf[i:i + 60])[:13]
            if '{"tool_calls"'.startswith(tail):  # is, or may still become, the start of a call
                fence = self.buf.rfind("```", self.sent, i)
                if fence >= 0 and not self.buf[fence + 3:i].replace("json", "").strip():
                    return fence  # the fence that wraps it stays back too
                return i
            i = self.buf.find("{", i + 1)
        # a fence, even a half-written one, that may wrap a call next
        m = re.search(r"(?:`{1,3}(?:j(?:s(?:o(?:n)?)?)?)?|@{1,2}[A-Z_]*@{0,2})\s*$", self.buf[self.sent:])
        return self.sent + m.start() if m else len(self.buf)  # the same for a @@WRITE@@ marker

    def rest(self) -> str:
        return self.buf[self.sent:]


# A runaway reply: deepseek-v4-1-flash-uncensored sometimes keeps emitting tool calls (75 to 2143 in one
# reply) or tags for 5-9 minutes, 274k-482k chars (15 of 7,167 streams, measured 2026-11-21). Normal replies
# in the same log: at most 38 calls and 68k chars. Past either limit the rest is not read.
LOOP_MAX_CALLS = int(os.environ.get("VENICE_BRIDGE_LOOP_MAX_CALLS", "40"))
LOOP_MAX_CHARS = int(os.environ.get("VENICE_BRIDGE_LOOP_MAX_CHARS", "150000"))
LOOP_CHECK_EVERY = 4000  # chars between two counts
# a call object ("name" then "arguments") or a native invoke; a "name" key inside file content is not one. A
# <tool_call> tag is not counted: it holds a call object already. A loop of bare tags is caught by the char limit.
_CALL_START = re.compile(r'"name"\s*:\s*"[^"\n]{1,80}"\s*,\s*"(?:arguments|input|parameters)"\s*:|invoke\s+name="', re.I)


def _runaway_cut(text: str) -> Optional[int]:
    """Where to cut a runaway reply (the start of call LOOP_MAX_CALLS + 1, or LOOP_MAX_CHARS), or None."""
    for n, m in enumerate(_CALL_START.finditer(text), 1):
        if n > LOOP_MAX_CALLS:
            start = m.start()
            env = text.rfind('"tool_calls"', max(0, start - 60), start)  # back to the call's envelope, if it has one
            brace = text.rfind("{", max(0, start - 60), env if env >= 0 else start)
            return brace if brace >= 0 else start
    return LOOP_MAX_CHARS if len(text) > LOOP_MAX_CHARS else None


def _drain(handle, gate: Optional[_LiveGate] = None, tools_mode: bool = False) -> Dict[str, Any]:
    """Read one upstream reply to its end: {text, reasoning, failure: None | (transport|server_loop, message)}.
    A reply counts only when Venice said response.completed: a stream cut in the middle of a write
    call would otherwise become a write of half the file. In tools mode a runaway reply (too many calls
    or chars, see LOOP_MAX_*) is cut at a call boundary and counts as complete."""
    proc = handle[0]
    text: List[str] = []
    reasoning: List[str] = []
    unread: List[str] = []
    done, failure = False, None
    venice: Dict[str, List[str]] = {}
    t0, first = time.time(), None
    size, next_check, cut_at = 0, LOOP_CHECK_EVERY, None
    try:
        for kind, delta in _iter_chat_events(proc, unread, venice, cut_server_tools=tools_mode):
            if first is None and kind in ("text", "reasoning"):
                first = time.time() - t0
            if kind in ("error", "server_loop", "server_tool"):
                failure = (kind if kind != "error" else "transport", delta)
                break
            if kind == "done":
                done = True
            elif kind == "reasoning":
                reasoning.append(delta)
            elif kind == "text":
                text.append(delta)
                if gate:
                    gate.feed(delta)
                size += len(delta)
                if tools_mode and size >= next_check:
                    next_check = size + LOOP_CHECK_EVERY
                    joined = "".join(text)
                    cut_at = _runaway_cut(joined)
                    if cut_at is not None:
                        text, done = [joined[:cut_at]], True
                        STATS.record_event("runaway_cuts")
                        sys.stderr.write("[venice-bridge] runaway reply cut at %d chars (%s)\n" % (
                            cut_at, "over %d calls" % LOOP_MAX_CALLS if cut_at != LOOP_MAX_CHARS else "char limit"))
                        proc.kill()
                        break
        if not failure and not done:
            failure = ("transport", "the Venice stream ended before the reply was complete" if text
                       else f"Venice sent no content. Unread lines: {unread or 'none'}")
    finally:
        if failure:
            proc.kill()  # the stream may still be open
        _cleanup(handle)
    sys.stderr.write("[venice-bridge] timing stream: first=%s total=%.1fs text=%d reasoning=%d chars%s\n" % (
        "%.1fs" % first if first is not None else "-", time.time() - t0,
        sum(map(len, text)), sum(map(len, reasoning)), " failed=" + failure[0] if failure else ""))
    return {"text": "".join(text), "reasoning": "".join(reasoning), "failure": failure,
            "streamed": bool(gate and gate.live), "venice": venice}


def _reply_of(att: Dict[str, Any], tools: Optional[List[Dict[str, Any]]], parallel: bool = True) -> Dict[str, Any]:
    """An attempt as {calls, content, reasoning, trigger, problem}; trigger None = a usable reply."""
    streamed = att.get("streamed", False)
    if att["failure"]:
        kind, problem = att["failure"]
        return {"calls": None, "content": "", "reasoning": att["reasoning"], "trigger": kind, "problem": problem,
                "streamed": streamed, "venice": att.get("venice")}
    calls, content, unreadable = _read_reply(att["text"], tools)
    if calls and not parallel and len(calls) > 1:
        calls = calls[:1]
    trigger = "unreadable" if unreadable else None if calls or content.strip() else "empty"
    problem = {"unreadable": "the model sent a tool call that could not be read",
               "empty": "Venice finished without sending any text."}.get(trigger)
    return {"calls": calls, "content": content, "reasoning": att["reasoning"], "trigger": trigger, "problem": problem,
            "streamed": streamed, "venice": att.get("venice")}


def _rank(r: Dict[str, Any]) -> int:
    # 3 calls, 2 readable text, 1 text beside an unreadable call, 0 nothing usable
    return 3 if r["calls"] else 0 if not r["content"].strip() else 1 if r["trigger"] else 2


def _better(cand: Dict[str, Any], cur: Dict[str, Any], replace: bool = False) -> bool:
    """opencode-venice-web's preferAnswer: a retry never replaces a better reply. Between two failures the
    newer one wins (its trigger picks the next reminder); `replace` lets a second text answer win."""
    a, b = _rank(cand), _rank(cur)
    return a > b or (a == b and (a == 0 or (replace and a == 2)))


def _ends_after_tool(body: Dict[str, Any]) -> bool:
    last = next((m.get("role") for m in reversed(body.get("messages") or [])
                 if m.get("role") not in ("system", "developer")), None)
    return last == "tool"


_FENCE = re.compile(r"```[^\n`]*\n[\s\S]*?```")


def _code_instead_of_call(content: str, declared: Dict[str, str]) -> bool:
    """A reply with no call that holds a fenced code block while a shell tool is declared: the model may
    have written the command instead of running it (qwen-3-8-27b, phase-0 T1, 2026-11-21). Only opens the
    judge path; the judge decides whether a code block was the right answer."""
    return bool(content) and bool(_have("bash", declared)) and bool(_FENCE.search(content))


def _tail(text: str, n: int) -> str:
    return "…" + text[-n:] if len(text) > n else text


def _judge_payload(body: Dict[str, Any], reply: str) -> Dict[str, Any]:
    """buildJudgeRequest: the newest user message, the last two tool results and the reply, tails only."""
    msgs = body.get("messages") or []
    asked = next((_flatten_content(m.get("content")) for m in reversed(msgs) if m.get("role") == "user"), "")
    results = [m for m in msgs if m.get("role") == "tool"][-2:]
    evidence = "\n\n".join("[%d] %s" % (i, _tail(_flatten_content(m.get("content")), 1500))
                           for i, m in enumerate(results, 1))
    text = "\n\n".join((
        "USER REQUEST:\n" + (_tail(asked.strip(), 2500) or "(none)"),
        "LATEST TOOL RESULTS (oldest first):\n" + (evidence or "(none)"),
        "AGENT REPLY (declared status: none):\n" + (_tail(reply.strip(), 2500) or "(empty)"),
    ))
    return {"modelId": JUDGE_MODEL, "systemPrompt": {"instructions": JUDGE_PROMPT},
            "messages": [_message("user", text)], "tools": [], "clientTools": [], "reasoningMode": "disabled"}


def _parse_verdict(text: str) -> Optional[str]:
    start = text.find("{")
    if start < 0:
        return None
    try:
        value, _ = read_lenient(text, start)
    except Exception:
        return None
    verdict = value.get("verdict") if isinstance(value, dict) else None
    verdict = verdict.strip().lower() if isinstance(verdict, str) else ""
    return verdict if verdict in VERDICTS else None


def _ask_judge(body: Dict[str, Any], reply: str) -> Optional[str]:
    """The verdict, or None on any failure: the judge can add a retry, never block an answer."""
    started, verdict, why = time.time(), None, ""
    try:
        handle, failure = _open_upstream(_judge_payload(body, reply), max_time=JUDGE_TIMEOUT)
        if failure:
            why = "client status %d" % failure[0]
        else:
            att = _drain(handle)
            verdict = None if att["failure"] else _parse_verdict(att["text"])
            why = att["failure"][0] if att["failure"] else "unreadable verdict"
    except Exception as exc:
        why = type(exc).__name__
    # the verdict's `reason` paraphrases the conversation: not logged
    sys.stderr.write("[venice-bridge] judge %s: %s in %.1fs\n" % (
        JUDGE_MODEL, verdict or f"no verdict ({why}), keeping the reply", time.time() - started))
    return verdict


def _complete_with_tools(body: Dict[str, Any], payload: Dict[str, Any], handle,
                         gate: Optional[_LiveGate] = None) -> Dict[str, Any]:
    """Attempt 1 is already open. Returns the best reply ({calls, content, reasoning, trigger, problem}).

    Retries (each a full upstream request): server loop, unreadable call, empty reply and a broken
    stream, at most MAX_FORMAT_RETRIES together (a broken stream once, resent unchanged); then, when
    the reply ends the turn right after a tool result, or holds a code block instead of a call while a
    shell tool is declared, the judge, and at most MAX_JUDGE_RETRIES."""
    tools = body.get("tools")
    parallel = body.get("parallel_tool_calls") is not False
    tc_mode, tc_name = _parse_tool_choice(body.get("tool_choice"))
    declared = {t["function"]["name"].lower(): t["function"]["name"] for t in tools or []
                if isinstance(t, dict) and isinstance(t.get("function"), dict) and t["function"].get("name")}
    best = _reply_of(_drain(handle, gate, tools_mode=True), tools, parallel=parallel)
    n = 1
    sys.stderr.write(f"[venice-bridge] attempt 1 (first): {_outcome(best)}\n")
    if best["streamed"]:
        return best  # already on the user's screen: nothing to retry or judge
    last = payload
    format_left, judge_left, transport_left = MAX_FORMAT_RETRIES, MAX_JUDGE_RETRIES, 1
    while True:
        if POOL.all_exhausted():
            break  # quota known to be used up: no upstream call, not even the judge
        trigger, replace, from_judge = best["trigger"], False, False
        if not trigger and not best["calls"] and tc_mode in ("required", "named") and format_left >= 1:
            format_left -= 1
            STATS.record_event("format_retries")
            req_msg = (f'You MUST call the tool "{tc_name}" now using ONLY {{"tool_calls":[{{"name":"{tc_name}","arguments":{{...}}}}]}}.'
                       if tc_mode == "named" and tc_name else
                       'You MUST call at least one declared tool now using ONLY {"tool_calls":[{"name":"<exact tool name>","arguments":{...}}]}.')
            nxt = dict(payload, messages=payload["messages"] + [_message("user", "(act reminder) " + req_msg)])
            why = "tool_choice_" + tc_mode
        elif trigger:
            if format_left < 1 or (trigger == "transport" and transport_left < 1):
                break
            format_left -= 1
            STATS.record_event("format_retries")
            if trigger == "transport":
                transport_left -= 1
                nxt = last  # resent as it was, reminder included
            else:
                reminder = (_steer_reminder(trigger, best.get("venice"), declared)
                            if trigger in ("server_loop", "server_tool") else FORMAT_REMINDERS[trigger])
                nxt = dict(payload, messages=payload["messages"] + [_message("user", "(act reminder) " + reminder)])
            why = trigger
        elif best["calls"] or not JUDGE_ENABLED or judge_left < 1 or not (
                _ends_after_tool(body) or _code_instead_of_call(best["content"], declared)):
            break
        else:
            verdict = _ask_judge(body, best["content"])
            if verdict not in JUDGE_REMINDERS or POOL.all_exhausted():
                break
            judge_left -= 1
            STATS.record_event("judge_retries")
            replace, from_judge = verdict == "missing_result", True
            nxt = dict(payload, messages=payload["messages"] + [
                _message("user", "(act reminder) " + JUDGE_REMINDERS[verdict])])
            why = "judge " + verdict
        n += 1
        handle, failure = _open_upstream(nxt)
        if failure:
            sys.stderr.write(f"[venice-bridge] attempt {n} ({why}): client status {failure[0]}, keeping the best reply\n")
            break
        last = nxt
        if gate:
            gate.reset()
        cand = _reply_of(_drain(handle, gate, tools_mode=True), tools, parallel=parallel)
        sys.stderr.write(f"[venice-bridge] attempt {n} ({why}): {_outcome(cand)}\n")
        if cand["streamed"]:
            return cand
        if _better(cand, best, replace):
            best = cand
        elif from_judge:
            break  # the first answer stays
    return best


def _outcome(r: Dict[str, Any]) -> str:
    out = f"calls={len(r['calls'])}" if r["calls"] else r["trigger"] or "text"
    return out + " (shown live)" if r.get("streamed") else out


# ---- Local OpenAI-compatible backends (Ollama, LM Studio, llama.cpp, MLX): plain passthrough ----
# These servers already speak OpenAI, so none of the Venice machinery applies to them: no Venice
# login, no tool emulation, no retries, no judge. A model id `<backend name>/<model>` is sent as it
# is to that backend's /chat/completions and the answer is relayed byte for byte (tool_calls too).
# The route is chosen by the PREFIX alone, never by asking the backend: a prompt meant for a local
# model must not go to Venice because the local server happens to be down.
# Config: a JSON list (or {"backends": [...]}) of {"name", "base_url", optional "api_key"} in
# BACKENDS_FILE; without the file the two defaults below; an empty list turns local backends off.
BACKENDS_FILE = os.environ.get("VENICE_BRIDGE_BACKENDS", "~/.config/venice-bridge/backends.json")
DEFAULT_BACKENDS = [
    {"name": "ollama", "base_url": "http://127.0.0.1:11434/v1"},
    {"name": "lmstudio", "base_url": "http://127.0.0.1:1234/v1"},
]
BACKENDS: Optional[List[Dict[str, Any]]] = None  # tests set a list; None = the file, read per request
LOCAL_TIMEOUT = 600  # seconds without a byte from a local backend (a cold model load takes minutes)


def _backends() -> List[Dict[str, Any]]:
    if BACKENDS is not None:
        return BACKENDS
    try:
        with open(os.path.expanduser(BACKENDS_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return DEFAULT_BACKENDS
    except ValueError as exc:
        sys.stderr.write(f"[venice-bridge] {BACKENDS_FILE} is not valid JSON ({exc}); using the default backends\n")
        return DEFAULT_BACKENDS
    items = data.get("backends") if isinstance(data, dict) else data
    return [{"name": str(b["name"]), "base_url": str(b["base_url"]).rstrip("/"), "api_key": b.get("api_key")}
            for b in (items if isinstance(items, list) else [])
            if isinstance(b, dict) and b.get("name") and b.get("base_url")]


# A virtual model that keeps working when Venice is out. `VENICE_AUTO_MODELS` (comma list) is the
# priority: Venice model ids first, a local `backend/id` as the offline fallback. Venice's web quota is
# PLAN-WIDE (every Venice model shares it), so falling back to another Venice model cannot beat quota —
# the only real fallback is a local backend. So `venice-auto` serves the first Venice candidate normally
# and switches to the first local candidate while the bridge knows the plan is over quota (quota_until).
DEFAULT_AUTO_MODELS = "deepseek-v4-1-flash-uncensored,ollama/qwen3:8b"


def _resolve_auto(model: str) -> str:
    """Resolve `venice-auto` (optionally `venice-auto:think`/`:web`) to a concrete model id."""
    if model.startswith("venice-bridge/"):  # OpenCode sends the provider prefix; strip it before matching
        model = model.split("/", 1)[1]
    base, _, suffix = model.partition(":")
    if base != "venice-auto":
        return model
    cands = [c.strip() for c in os.environ.get("VENICE_AUTO_MODELS", DEFAULT_AUTO_MODELS).split(",") if c.strip()]
    venice_c = [c for c in cands if "/" not in c]
    local_c = [c for c in cands if "/" in c]
    if POOL.all_exhausted() and local_c:
        return local_c[0]  # over quota: a Venice tag is meaningless on a local backend, so drop the suffix
    chosen = venice_c[0] if venice_c else (local_c[0] if local_c else "deepseek-v4-1-flash-uncensored")
    return chosen + (":" + suffix if suffix and "/" not in chosen else "")


def _resolve_backend(model: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """(backend, the model's id on that backend) for `ollama/qwen3:8b`, else (None, model)."""
    if model.startswith("venice-bridge/"):
        model = model.split("/", 1)[1]
    for b in _backends():
        if model.startswith(b["name"] + "/"):
            return b, model[len(b["name"]) + 1:]
    return None, model


def _local_request(backend: Dict[str, Any], method: str, suffix: str, body: Optional[bytes] = None,
                   timeout: float = LOCAL_TIMEOUT):
    """(connection, response). http.client on purpose: urllib would use the macOS system proxy."""
    u = urllib.parse.urlsplit(backend["base_url"])
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = cls(u.hostname, u.port, timeout=timeout)
    headers = {"Content-Type": "application/json"}
    if backend.get("api_key"):
        headers["Authorization"] = "Bearer " + str(backend["api_key"])
    try:
        conn.request(method, u.path.rstrip("/") + suffix, body, headers)
        return conn, conn.getresponse()
    except Exception:
        conn.close()
        raise


def list_local_models() -> List[Dict[str, Any]]:
    """Every reachable local backend's models as `<backend>/<id>`; a backend that is down is skipped."""
    out: List[Dict[str, Any]] = []
    for b in _backends():
        try:
            conn, resp = _local_request(b, "GET", "/models", timeout=2)
            try:
                data = json.loads(resp.read()) if resp.status == 200 else {}
            finally:
                conn.close()
            for m in data.get("data") or []:
                if isinstance(m, dict) and m.get("id"):
                    out.append({"id": f"{b['name']}/{m['id']}", "object": "model",
                                "created": int(time.time()), "owned_by": b["name"]})
        except Exception as exc:
            sys.stderr.write(f"[venice-bridge] local backend {b['name']} not listed: {type(exc).__name__}\n")
    return out


def _usage(prompt_chars: int, completion_text: str) -> Dict[str, int]:
    """An APPROXIMATE OpenAI usage block (~4 chars per token). Venice's web API returns no token
    counts, so coding clients (Claude Code, Codex) otherwise show 0. These are estimates."""
    p = max(0, round(prompt_chars / 4))
    c = max(0, round(len(completion_text or "") / 4))
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


# --- response cache -------------------------------------------------------------
# Serve a BYTE-IDENTICAL repeat request (same model+messages+tools+params, stream aside)
# from memory, WITHOUT spending a Venice web-quota call. The web plan is request-metered
# (not token-metered), so this protects the daily quota on exact retries / repeated prompts.
# Agentic turns rarely repeat verbatim, so the hit rate is modest by design.
# Disable with VENICE_BRIDGE_CACHE=0. Only clean successes (real answer or tool call) are stored.
import collections as _collections
import hashlib as _hashlib

_CACHE_ENABLED = os.environ.get("VENICE_BRIDGE_CACHE", "1") != "0"
_CACHE_MAX = int(os.environ.get("VENICE_BRIDGE_CACHE_MAX", "256"))
_CACHE_TTL = float(os.environ.get("VENICE_BRIDGE_CACHE_TTL", "3600"))
_CACHE_LOCK = threading.Lock()
_CACHE = _collections.OrderedDict()  # key -> (stored_at_epoch, entry)
_CACHE_STATS = {"hits": 0, "misses": 0, "stores": 0}


def _has_multimodal_parts(body: Dict[str, Any]) -> bool:
    for m in body.get("messages") or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list) and any(
                isinstance(it, dict) and it.get("type") in ("image_url", "input_image", "file", "input_file")
                for it in c):
            return True
    return False


def _cache_key(body: Dict[str, Any]) -> Optional[str]:
    if not _CACHE_ENABLED or not isinstance(body, dict) or _has_multimodal_parts(body):
        return None
    keyable = {k: v for k, v in body.items() if k != "stream"}
    try:
        blob = json.dumps(keyable, sort_keys=True, ensure_ascii=False)
    except Exception:
        return None
    return _hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_get(key: Optional[str]) -> Optional[Dict[str, Any]]:
    if not key:
        return None
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is None:
            _CACHE_STATS["misses"] += 1
            return None
        stored_at, entry = hit
        if now - stored_at > _CACHE_TTL:
            _CACHE.pop(key, None)
            _CACHE_STATS["misses"] += 1
            return None
        _CACHE.move_to_end(key)
        _CACHE_STATS["hits"] += 1
        return entry


def _cache_put(key: Optional[str], entry: Dict[str, Any]) -> None:
    if not key:
        return
    # never cache a non-answer (no text and no tool call)
    if not (entry.get("content") or entry.get("tool_calls")):
        return
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), entry)
        _CACHE.move_to_end(key)
        _CACHE_STATS["stores"] += 1
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


class _BridgeStats:
    """In-memory runtime counters for GET /stats (no prompts, replies, cookies, or tokens)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.time()
        self._requests = {"total": 0, "ok": 0, "error": 0, "cached": 0, "local": 0}
        self._events = {"format_retries": 0, "judge_retries": 0, "runaway_cuts": 0, "proactive_jwt_refreshes": 0}
        self._models: Dict[str, Dict[str, float]] = {}

    def record_event(self, name: str, delta: int = 1) -> None:
        with self._lock:
            if name in self._events:
                self._events[name] += delta

    def record_request(self, model: str, ok: bool, total_s: float = 0.0, ttft_s: Optional[float] = None,
                       cached: bool = False, local: bool = False) -> None:
        with self._lock:
            self._requests["total"] += 1
            self._requests["ok" if ok else "error"] += 1
            if cached:
                self._requests["cached"] += 1
            if local:
                self._requests["local"] += 1
            if model:
                m = self._models.setdefault(model, {"requests": 0, "errors": 0, "total_sum_s": 0.0, "ttft_sum_s": 0.0, "ttft_count": 0})
                m["requests"] += 1
                if not ok:
                    m["errors"] += 1
                m["total_sum_s"] += max(0.0, float(total_s))
                if ttft_s is not None and ttft_s >= 0:
                    m["ttft_sum_s"] += float(ttft_s)
                    m["ttft_count"] += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            models_out: Dict[str, Any] = {}
            for name, m in sorted(self._models.items()):
                req_n = int(m["requests"])
                ttft_n = int(m["ttft_count"])
                models_out[name] = {
                    "requests": req_n,
                    "errors": int(m["errors"]),
                    "avg_total_s": round(m["total_sum_s"] / req_n, 2) if req_n else None,
                    "avg_ttft_s": round(m["ttft_sum_s"] / ttft_n, 2) if ttft_n else None,
                }
            return {
                "uptime_s": round(time.time() - self._started, 1),
                "requests": dict(self._requests),
                "retries": {
                    "format": self._events["format_retries"],
                    "judge": self._events["judge_retries"],
                    "runaway_cuts": self._events["runaway_cuts"],
                },
                "auth": {"proactive_jwt_refreshes": self._events["proactive_jwt_refreshes"]},
                "pool": POOL.status(),
                "cache": {**_CACHE_STATS, "entries": len(_CACHE), "enabled": _CACHE_ENABLED},
                "chat_quota": POOL.active_client().chat_quota(),
                "models": models_out,
            }


STATS = _BridgeStats()


def _stats_html(snap: Dict[str, Any]) -> str:
    import html as _html
    rows = []
    for name, m in snap.get("models", {}).items():
        rows.append(
            f"<tr><td>{_html.escape(name)}</td><td>{m['requests']}</td><td>{m['errors']}</td>"
            f"<td>{m['avg_ttft_s'] if m['avg_ttft_s'] is not None else '-'}</td>"
            f"<td>{m['avg_total_s'] if m['avg_total_s'] is not None else '-'}</td></tr>"
        )
    table = "\n".join(rows) or "<tr><td colspan='5'>No requests yet</td></tr>"
    req = snap["requests"]
    ret = snap["retries"]
    cache = snap["cache"]
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>venice-bridge stats</title>"
        "<style>body{font-family:system-ui,sans-serif;background:#0f1115;color:#e6edf3;max-width:900px;margin:24px auto;padding:0 16px}"
        "h1{font-size:20px;margin:0 0 16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}"
        ".card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px}.k{font-size:12px;color:#8b949e}.v{font-size:20px;font-weight:600;margin-top:4px}"
        "table{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}"
        "th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #21262d;font-size:13px}th{color:#8b949e}</style></head><body>"
        f"<h1>venice-bridge stats <small style='color:#8b949e;font-weight:400'>uptime {snap['uptime_s']}s</small></h1>"
        "<div class='grid'>"
        f"<div class='card'><div class='k'>Requests (ok / err)</div><div class='v'>{req['total']} ({req['ok']} / {req['error']})</div></div>"
        f"<div class='card'><div class='k'>Cache (hits / entries)</div><div class='v'>{cache['hits']} / {cache['entries']}</div></div>"
        f"<div class='card'><div class='k'>Retries (fmt / judge / cut)</div><div class='v'>{ret['format']} / {ret['judge']} / {ret['runaway_cuts']}</div></div>"
        f"<div class='card'><div class='k'>Proactive JWT Refreshes</div><div class='v'>{snap['auth']['proactive_jwt_refreshes']}</div></div>"
        "</div><table><thead><tr><th>Model</th><th>Requests</th><th>Errors</th><th>Avg TTFT (s)</th><th>Avg Total (s)</th></tr></thead>"
        f"<tbody>{table}</tbody></table></body></html>"
    )


class OpenAIBridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[venice-bridge] {fmt % args}\n")

    def _send_json(self, code: int, obj: Any) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _serve_cached(self, entry: Dict[str, Any], stream: bool, model: str) -> None:
        """Replay a cached result as a normal /v1/chat/completions response (stream or not)."""
        content = entry.get("content") or ""
        reasoning = entry.get("reasoning")
        calls = entry.get("tool_calls")  # already in {id,type,function} shape, or None
        finish = entry.get("finish") or ("tool_calls" if calls else "stop")
        stop_seq = entry.get("stop_sequence")
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        completion = (content or "") + "".join(c["function"]["arguments"] for c in (calls or []))
        usage = _usage(int(entry.get("prompt_chars") or 0), completion)
        if not stream:
            message_obj: Dict[str, Any] = {"role": "assistant", "content": content or None}
            if calls:
                message_obj["tool_calls"] = calls
            choice: Dict[str, Any] = {"index": 0, "message": message_obj, "finish_reason": finish}
            if stop_seq:
                choice["stop_sequence"] = stop_seq
            self._send_json(200, {
                "id": chat_id, "object": "chat.completion", "created": created, "model": model,
                "choices": [choice],
                "usage": usage,
            })
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def _emit(delta: Dict[str, Any], fin: Optional[str] = None, sseq: Optional[str] = None) -> None:
            ch: Dict[str, Any] = {"index": 0, "delta": delta, "finish_reason": fin}
            if sseq:
                ch["stop_sequence"] = sseq
            chunk = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": model,
                     "choices": [ch]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            _emit({"role": "assistant"})
            if reasoning:
                _emit({"reasoning_content": reasoning})
            if content:
                _emit({"content": content})
            if calls:
                _emit({"tool_calls": calls})
            _emit({}, finish, stop_seq)
            uchunk = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": model,
                      "choices": [], "usage": usage}
            self.wfile.write(f"data: {json.dumps(uchunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        finally:
            self.close_connection = True

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            try:
                active = POOL.active_client()
                active.get_jwt()
                self._send_json(200, {"ok": True, "venice_session": "active", "chat_quota": active.chat_quota(),
                                      "cache": {**_CACHE_STATS, "entries": len(_CACHE), "enabled": _CACHE_ENABLED}})
            except Exception as exc:
                self._send_json(503, {"ok": False, "error": str(exc)})
            return

        if path == "/stats":
            snap = STATS.snapshot()
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if (qs.get("format") or [""])[0].lower() == "html":
                raw = _stats_html(snap).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self._send_json(200, snap)
            return

        if path == "/v1/models":
            models = list_local_models()
            try:
                models = POOL.active_client().list_models() + models
            except Exception as exc:
                if not models:
                    self._send_json(502, {"error": {"message": str(exc), "type": "bridge_error"}})
                    return
                # the local models stay usable while Venice is logged out or unreachable
                sys.stderr.write(f"[venice-bridge] Venice models not listed: {type(exc).__name__}\n")
            models = [{"id": "venice-auto", "object": "model", "created": int(time.time()),
                       "owned_by": "venice-bridge"}] + models  # virtual: Venice, local fallback over quota
            self._send_json(200, {"object": "list", "data": models})
            return

        self._send_json(404, {"error": {"message": f"Unknown GET path: {path}"}})

    def _passthrough(self, backend: Dict[str, Any], model: str, body: Dict[str, Any]) -> None:
        """Send the request to a local backend unchanged (only the model id loses its prefix) and
        relay the answer as it arrives, whatever its status."""
        try:
            conn, resp = _local_request(
                backend, "POST", "/chat/completions", json.dumps(dict(body, model=model), ensure_ascii=False).encode("utf-8"))
        except Exception as exc:
            sys.stderr.write(f"[venice-bridge] local {backend['name']} model={model}: {type(exc).__name__}: {exc}\n")
            self._send_json(502, {"error": {
                "message": f"Local backend '{backend['name']}' ({backend['base_url']}) did not answer: {exc}",
                "type": "local_backend_error"}})
            return
        sys.stderr.write(f"[venice-bridge] local {backend['name']} model={model} status={resp.status}\n")
        try:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type") or "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            # read1 returns what has arrived; read(n) would hold the tokens back until n bytes are there
            for chunk in iter(lambda: resp.read1(65536), b""):
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:  # a client that went away ends here too: closing the connection stops the generation
            conn.close()
            self.close_connection = True

    def do_POST(self) -> None:
        started = time.time()
        try:
            self._handle_post()
        finally:
            # one line per client request, with wall-clock time, so a session can be replayed from the log
            sys.stderr.write("[venice-bridge] request done %s in %.1fs path=%s\n" % (
                time.strftime("%H:%M:%S"), time.time() - started, self.path.split("?", 1)[0]))

    def _handle_post(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/reload":
            with VENICE._lock:
                VENICE._clerk_client = None
                VENICE._session_id = None
                VENICE._jwt = None
                VENICE._attest_token = None
                VENICE.quota_until = 0.0
            try:
                POOL.reload()
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, {"ok": True})
            return

        if path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": f"Unknown POST path: {path}"}})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw_body)
        except Exception as exc:
            self._send_json(400, {"error": {"message": f"Invalid JSON: {exc}"}})
            return

        if isinstance(body, dict):
            body["model"] = _resolve_auto(str(body.get("model") or ""))  # venice-auto -> a concrete id
        backend, local_model = _resolve_backend(str(body.get("model") or "")) if isinstance(body, dict) else (None, "")
        if backend:  # before anything Venice: its quota, login and prompt rewriting do not apply
            t_local = time.time()
            self._passthrough(backend, local_model, body)
            STATS.record_request(f"{backend['name']}/{local_model}", ok=True, total_s=time.time() - t_local, local=True)
            return

        if POOL.all_exhausted():  # known to be over quota: do not spend an upstream call
            code, message = _quota_error()
            STATS.record_request(str(body.get("model") or ""), ok=False)
            self._send_json(code, {"error": {"message": message, "type": "venice_quota_exhausted", "code": 429}})
            return

        req_started = time.time()
        stream = bool(body.get("stream", False))
        model = _parse_web_tags(str(body.get("model") or "qwen3-coder-480b-a35b-instruct-turbo"))[0]
        cache_key = _cache_key(body)
        cached = _cache_get(cache_key)
        if cached is not None:
            sys.stderr.write("[venice-bridge] cache hit (no Venice call spent)\n")
            self._serve_cached(cached, stream, model)
            STATS.record_request(model, ok=True, total_s=time.time() - req_started, ttft_s=0.0, cached=True)
            return
        try:
            venice_payload, has_tools = openai_to_venice_chat(body)
        except VeniceAttachmentError as exc:
            STATS.record_request(model, ok=False, total_s=time.time() - req_started)
            self._send_json(exc.status, {"error": {"message": exc.message, "type": "invalid_request_error"}})
            return
        msg_chars = sum(len(c.get("text", "")) for m in venice_payload["messages"] for c in m.get("content", []))
        prompt_chars = len(venice_payload["systemPrompt"]["instructions"]) + msg_chars
        sys.stderr.write(
            "[venice-bridge] request chars: system=%d messages=%d (%d) tools=%d%s\n" % (
                len(venice_payload["systemPrompt"]["instructions"]),
                msg_chars,
                len(venice_payload["messages"]),
                len(body.get("tools") or []),
                f" attachments={len(venice_payload['_attachments'])}" if venice_payload.get("_attachments") else "",
            )
        )

        handle, failure = _open_upstream(venice_payload)
        if failure:
            STATS.record_request(model, ok=False, total_s=time.time() - req_started)
            self._send_json(*failure)
            return
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        first_token_at: List[Optional[float]] = [None]

        def start_stream() -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

        def emit(delta: Dict[str, Any], finish: Optional[str] = None, stop_seq: Optional[str] = None) -> None:
            if first_token_at[0] is None and (delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls")):
                first_token_at[0] = time.time() - req_started
            ch: Dict[str, Any] = {"index": 0, "delta": delta, "finish_reason": finish}
            if stop_seq:
                ch["stop_sequence"] = stop_seq
            chunk = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [ch],
            }
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        def send_usage(completion_text: str) -> None:
            chunk = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": model,
                     "choices": [], "usage": _usage(prompt_chars, completion_text)}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        if has_tools:
            # The answer may be a call, so nothing is sent until it is complete, retries included. That
            # also keeps the status line free: a failed turn goes out as a 502, which OpenCode retries by
            # itself, while an error inside a started stream ends the turn (both measured).
            gate = _LiveGate(lambda: (start_stream(), emit({"role": "assistant"})),
                             lambda t: emit({"content": t})) if stream else None
            best = _complete_with_tools(body, venice_payload, handle, gate)
            calls, content = best["calls"], best["content"]
            if best["streamed"]:
                try:
                    if best["trigger"] in ("transport", "server_loop", "server_tool"):
                        # the text is already on its way: end with an error, never with a normal ending
                        sys.stderr.write(f"[venice-bridge] upstream failed mid-reply: {best['problem']}\n")
                        err = {"error": {"message": best["problem"], "type": "venice_upstream_error"}}
                        self.wfile.write(f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        STATS.record_request(model, ok=False, total_s=time.time() - req_started, ttft_s=first_token_at[0])
                        return
                    if calls:
                        emit({"tool_calls": calls})  # the held-back tail was the call
                    elif gate.rest() and best["trigger"] != "unreadable":
                        emit({"content": gate.rest()})
                    emit({}, "tool_calls" if calls else "stop")
                    send_usage((gate.buf if gate else "") + "".join(c["function"]["arguments"] for c in (calls or [])))
                    _cache_put(cache_key, {"content": content or "", "reasoning": best["reasoning"],
                                           "tool_calls": [{"id": c["id"], "type": "function", "function": c["function"]} for c in calls] if calls else None,
                                           "finish": "tool_calls" if calls else "stop", "prompt_chars": prompt_chars})
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    STATS.record_request(model, ok=True, total_s=time.time() - req_started, ttft_s=first_token_at[0])
                finally:
                    self.close_connection = True
                return
            if not calls and not content.strip():
                if best["trigger"] != "unreadable":
                    sys.stderr.write(f"[venice-bridge] upstream failed: {best['problem']}\n")
                    STATS.record_request(model, ok=False, total_s=time.time() - req_started)
                    self._send_json(502, {"error": {"message": best["problem"], "type": "venice_upstream_error"}})
                    return
                content = "(the model sent a tool call that could not be read)"  # never the raw envelope
            if not stream:
                message_obj: Dict[str, Any] = {"role": "assistant", "content": content or None}
                if calls:
                    message_obj["tool_calls"] = [{"id": c["id"], "type": "function", "function": c["function"]} for c in calls]
                completion = (content or "") + "".join(c["function"]["arguments"] for c in (calls or []))
                _cache_put(cache_key, {"content": content or "", "reasoning": best["reasoning"],
                                       "tool_calls": message_obj.get("tool_calls"),
                                       "finish": "tool_calls" if calls else "stop", "prompt_chars": prompt_chars})
                STATS.record_request(model, ok=True, total_s=time.time() - req_started)
                self._send_json(200, {
                    "id": chat_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "message": message_obj, "finish_reason": "tool_calls" if calls else "stop"}],
                    "usage": _usage(prompt_chars, completion),
                })
                return
            try:
                start_stream()
                emit({"role": "assistant"})
                if best["reasoning"]:
                    emit({"reasoning_content": best["reasoning"]})
                if content:
                    emit({"content": content})
                if calls:
                    emit({"tool_calls": calls})
                emit({}, "tool_calls" if calls else "stop")
                send_usage((content or "") + "".join(c["function"]["arguments"] for c in (calls or [])))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                STATS.record_request(model, ok=True, total_s=time.time() - req_started, ttft_s=first_token_at[0])
            finally:
                self.close_connection = True
            return

        proc = handle[0]
        unread: List[str] = []
        events = _iter_chat_events(proc, unread)
        first = next(events, None)
        if first is None or first[0] in ("error", "server_loop", "done"):
            _cleanup(handle)
            if first and first[0] in ("error", "server_loop"):
                message = first[1]
            elif first:
                message = "Venice finished without sending any text."
            else:
                message = f"Venice sent no content. Unread lines: {unread or 'none'}"
            sys.stderr.write(f"[venice-bridge] upstream gave no content: {message}\n")
            STATS.record_request(model, ok=False, total_s=time.time() - req_started)
            self._send_json(502, {"error": {"message": message, "type": "venice_upstream_error"}})
            return
        events = itertools.chain([first], events)

        stops = _parse_stop(body.get("stop"))
        matcher = _StopMatcher(stops) if stops else None

        def collect(on_reasoning=None, on_text=None) -> Tuple[str, Optional[str]]:
            """Read the stream to its end: (answer text, problem or None).

            A reply counts only when Venice said response.completed (or a client stop sequence matched).
            """
            parts: List[str] = []
            done = False
            for kind, delta in events:
                if kind in ("error", "server_loop"):
                    return "".join(parts), delta
                if kind == "done":
                    done = True
                elif kind == "reasoning" and on_reasoning:
                    on_reasoning(delta)
                elif kind == "text":
                    if matcher is not None:
                        piece = matcher.feed(delta)
                        if piece:
                            parts.append(piece)
                            if on_text:
                                on_text(piece)
                        if matcher.matched is not None:
                            done = True
                            proc.kill()
                            break
                    else:
                        parts.append(delta)
                        if on_text:
                            on_text(delta)
            if matcher is not None and matcher.matched is None:
                tail = matcher.flush()
                if tail:
                    parts.append(tail)
                    if on_text:
                        on_text(tail)
            return "".join(parts), None if done else "the Venice stream ended before the reply was complete"

        if not stream:
            reason_parts: List[str] = []
            try:
                full_text, problem = collect(on_reasoning=reason_parts.append)
            finally:
                _cleanup(handle)
            if problem:
                sys.stderr.write(f"[venice-bridge] upstream failed mid-reply: {problem}\n")
                STATS.record_request(model, ok=False, total_s=time.time() - req_started)
                self._send_json(502, {"error": {"message": problem, "type": "venice_upstream_error"}})
                return
            if _response_format_instruction(body.get("response_format")):
                full_text = _clean_json_output(full_text)
            reasoning = "".join(reason_parts) or None  # surfaced when the model reasoned (`/think`)
            stop_seq = matcher.matched if matcher is not None else None
            message_obj = {"role": "assistant", "content": full_text or None}
            if reasoning:
                message_obj["reasoning_content"] = reasoning
            _cache_put(cache_key, {"content": full_text or "", "reasoning": reasoning, "tool_calls": None,
                                   "finish": "stop", "stop_sequence": stop_seq, "prompt_chars": prompt_chars})
            STATS.record_request(model, ok=True, total_s=time.time() - req_started)
            choice_out: Dict[str, Any] = {"index": 0, "message": message_obj, "finish_reason": "stop"}
            if stop_seq:
                choice_out["stop_sequence"] = stop_seq
            self._send_json(200, {
                "id": chat_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [choice_out],
                "usage": _usage(prompt_chars, full_text),
            })
            return

        try:
            start_stream()
            emit({"role": "assistant"})
            reason_parts: List[str] = []
            full_text, problem = collect(
                on_reasoning=lambda d: (reason_parts.append(d), emit({"reasoning_content": d}))[1],
                on_text=lambda d: emit({"content": d}),
            )
            if problem:
                # the text is already on its way: end with an error, never with a normal ending
                sys.stderr.write(f"[venice-bridge] upstream failed mid-reply: {problem}\n")
                err = {"error": {"message": problem, "type": "venice_upstream_error"}}
                self.wfile.write(f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
                STATS.record_request(model, ok=False, total_s=time.time() - req_started, ttft_s=first_token_at[0])
                return
            stop_seq = matcher.matched if matcher is not None else None
            emit({}, "stop", stop_seq)
            _cache_put(cache_key, {"content": full_text or "", "reasoning": "".join(reason_parts) or None,
                                   "tool_calls": None, "finish": "stop", "stop_sequence": stop_seq, "prompt_chars": prompt_chars})
            send_usage(full_text)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            STATS.record_request(model, ok=True, total_s=time.time() - req_started, ttft_s=first_token_at[0])
        finally:
            _cleanup(handle)
            self.close_connection = True

def main() -> None:
    parser = argparse.ArgumentParser(description="Venice AI OpenAI-compatible bridge")
    # 127.0.0.1 unless asked otherwise: the bridge has no authentication of its own, so any other
    # address (0.0.0.0 for a Docker container reaching host.docker.internal) opens it to the network
    parser.add_argument("--host", default=os.environ.get("VENICE_BRIDGE_HOST") or DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), OpenAIBridgeHandler)
    print(f"[+] Venice AI Bridge listening on http://{args.host}:{args.port}/v1", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
