"""Python port of opencode-venice-web's tolerant envelope parser (lenient-json.mjs + emulate.mjs:573-1020).
Native DSML invoke parsing (emulate.mjs:661-680) and cleanLeakedEnvelope (788-809) are left out."""
import json, re
from typing import Any, Dict, List, Optional, Tuple

# ---- lenient-json.mjs:30-207 ----------------------------------------------------------
_WS = " \t\n\r"
_SIMPLE = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_NUM = re.compile(r"-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_LIT = re.compile(r"(?:true|false|null)(?!\w)")
_HEX4 = re.compile(r"[0-9a-fA-F]{4}")

class _Reader:
    def __init__(self, text: str, pos: int) -> None:
        self.t, self.p = text, pos

    def _peek(self) -> Optional[str]:
        return self.t[self.p] if self.p < len(self.t) else None

    def _skip(self) -> None:
        while self.p < len(self.t) and self.t[self.p] in _WS:
            self.p += 1

    def _solid(self, i: int) -> Tuple[Optional[str], int]:
        while i < len(self.t) and self.t[i] in _WS:
            i += 1
        return (self.t[i] if i < len(self.t) else None), i

    def _starts_value(self, i: int) -> bool:
        c = self.t[i] if i < len(self.t) else None
        if c is None or c not in '"{[-0123456789tfn':
            return False
        return c not in "tfn" or bool(_LIT.match(self.t, i))

    def _closes_string(self, q: int) -> bool:  # a quote ends a string only if JSON can follow it
        c, i = self._solid(q + 1)
        if c is None or c in "}]:":
            return True
        if c != ",":
            return False
        c, i = self._solid(i + 1)
        while c == ",":
            c, i = self._solid(i + 1)
        return c is None or c in "}]" or self._starts_value(i)

    def value(self) -> Any:
        self._skip()
        c = self._peek()
        if c == "{":
            return self._object({})
        if c == "[":
            return self._array()
        if c == '"':
            return self._string()
        if c is not None and (c == "-" or c in "0123456789"):
            m = _NUM.match(self.t, self.p)
            if not m:
                raise ValueError(f"bad number at {self.p}")
            self.p = m.end()
            return json.loads(m.group(0))
        for word, val in (("true", True), ("false", False), ("null", None)):
            if self.t.startswith(word, self.p):
                self.p += len(word)
                return val
        raise ValueError(f"unexpected {c!r} at {self.p}")

    def _string(self) -> str:
        self.p += 1
        out: List[str] = []
        t = self.t
        while self.p < len(t):
            c = t[self.p]
            if c == "\\":
                n = t[self.p + 1] if self.p + 1 < len(t) else ""
                if n in _SIMPLE and n:
                    out.append(_SIMPLE[n]); self.p += 2; continue
                if n == "u" and _HEX4.fullmatch(t, self.p + 2, self.p + 6):
                    code = int(t[self.p + 2:self.p + 6], 16)
                    self.p += 6
                    # JS joins surrogate pairs by itself; Python must, or utf-8 encoding fails later
                    if 0xD800 <= code < 0xDC00 and t.startswith("\\u", self.p) and _HEX4.fullmatch(t, self.p + 2, self.p + 6):
                        low = int(t[self.p + 2:self.p + 6], 16)
                        if 0xDC00 <= low < 0xE000:
                            code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                            self.p += 6
                    out.append(chr(code) if not 0xD800 <= code < 0xE000 else "�")
                    continue
                out.append(c); self.p += 1; continue  # invalid escape kept as written
            if c == '"':
                if self._closes_string(self.p):
                    self.p += 1
                    return "".join(out)
            out.append(c)
            self.p += 1
        return "".join(out)  # end of input closes it

    def _object(self, target: Dict[str, Any]) -> Dict[str, Any]:
        if self._peek() == "{":
            self.p += 1
        while True:
            self._skip()
            c = self._peek()
            if c is None:
                return target
            if c == "}":
                self.p += 1
                return target
            if c == "]":
                return target  # the parent array closes; this object ends with it
            if c == ",":
                self.p += 1
                continue
            if c == "{" and target:
                return target  # a sibling starts: we were never closed
            if c != '"':
                raise ValueError(f"expected a key at {self.p}")
            key = self._string()
            self._skip()
            if self._peek() != ":":
                raise ValueError(f"expected ':' at {self.p}")
            self.p += 1
            target[key] = self.value()

    def _array(self) -> List[Any]:
        self.p += 1
        out: List[Any] = []
        while True:
            self._skip()
            c = self._peek()
            if c is None:
                return out
            if c == "]":
                self.p += 1
                return out
            if c == "}":
                return out  # the parent object closes; this array ends with it
            if c == ",":
                self.p += 1
                continue
            if c == '"' and out and isinstance(out[-1], dict):
                mark = self.p
                self._string()
                is_key = self._solid(self.p)[0] == ":"
                self.p = mark
                if is_key:  # `{...}, "key": value` inside an array: the object closed too early
                    self._object(out[-1])
                    continue
            out.append(self.value())

def read_lenient(text: str, start: int = 0) -> Tuple[Any, int]:
    r = _Reader(text, start)
    try:
        return r.value(), r.p
    except RecursionError as exc:
        raise ValueError("too deep") from exc

# ---- emulate.mjs:573-619 scanners -------------------------------------------------------
def _scan(text: str, start: int, open_ch: str = "{", close_ch: str = "}") -> int:
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': in_str = False
            continue
        if c == '"': in_str = True
        elif c == open_ch: depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1

# ---- emulate.mjs:627-655 markup stripping ------------------------------------------------
_BAR = r"[｜|]\s*DSML\s*[｜|]"
_PARAM_BLOCK = re.compile(rf'<\s*(?:{_BAR}\s*)?parameter\s+name="([^"]+)"[^>]*>([\s\S]*?)</\s*(?:{_BAR}\s*)?parameter\s*>', re.I)
_MARKUP = re.compile(rf"</?\s*(?:{_BAR}\s*)?(?:function_calls|calls|invoke|parameter|tool_calls?|tool_response|tool_result)\b[^>]*>", re.I)

def strip_tool_markup(text: str) -> str:
    text = _PARAM_BLOCK.sub(lambda m: m.group(2) if m.group(1) == "content" else "", text or "")
    return re.sub(r"\n{3,}", "\n\n", _MARKUP.sub("", text)).strip()

# ---- emulate.mjs:697-785 text repairs ----------------------------------------------------
_BOUNDARY = re.compile(r',[\s\r\n]*"[\w$]+"\s*:')
_TRAILING = re.compile(r",\s*([}\]])")

def _repair_arguments(t: str) -> str:  # quote a raw (non-JSON) run after "arguments":
    out, last = [], 0
    for m in re.finditer(r'"arguments"\s*:\s*', t):
        start = m.end()
        if m.start() < last:
            continue
        out.append(t[last:start]); last = start
        if start < len(t) and t[start] in '{["-0123456789tfn':
            continue
        b = _BOUNDARY.search(t, start)
        raw = t[start:(b.start() if b else len(t))].rstrip()
        out.append(json.dumps(raw, ensure_ascii=False)); last = start + len(raw)
    out.append(t[last:])
    return "".join(out)

def _parse_loose(s: str) -> Optional[Dict[str, Any]]:
    for step in (lambda x: x, lambda x: _TRAILING.sub(r"\1", x), _repair_arguments,
                 lambda x: _TRAILING.sub(r"\1", _repair_arguments(x))):
        try:
            v = json.loads(step(s), strict=False)  # strict=False = repairJsonControlChars
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    return None

def _salvage_calls(s: str) -> List[Dict[str, Any]]:
    calls = []
    for m in re.finditer(r'"name"\s*:\s*"([^"]+)"', s):
        am = re.compile(r'"arguments"\s*:\s*').search(s, m.end())
        if not am or am.start() - m.end() > 40:
            calls.append({"name": m.group(1), "arguments": {}}); continue
        start = am.end()
        trimmed = s[start:].lstrip()
        off = len(s) - start - len(trimmed)
        if trimmed[:1] in ("{", "["):
            bal = _scan(trimmed, 0, trimmed[0], "}" if trimmed[0] == "{" else "]")
            end = start + (off + bal if bal > 0 else len(s) - start)
        else:
            b = _BOUNDARY.search(s, start)
            end = b.start() if b else len(s)
        raw = re.sub(r"[\s,]+$", "", s[start:end])
        try:
            args = json.loads(raw, strict=False)
        except ValueError:
            args = None
        calls.append({"name": m.group(1), "arguments": args if isinstance(args, dict) else {"content": raw}})
    return calls

# ---- emulate.mjs:811-883 envelope helpers ------------------------------------------------
_MISSING = object()
_CALL_KEYS = {"name", "id", "type", "arguments", "function"}
_OBJ_START = re.compile(r'\{\s*"')
STATUSES = {"continue", "done", "ask"}

def _is_envelope(v: Any) -> bool:
    return isinstance(v, dict) and (isinstance(v.get("tool_calls"), list) or isinstance(v.get("content"), str))

def _unwrap_nested(env: Dict[str, Any], depth: int = 0) -> Dict[str, Any]:
    calls = env.get("tool_calls")
    inner = env["content"].strip() if isinstance(env.get("content"), str) else ""
    if depth > 2 or (isinstance(calls, list) and calls) or not inner.startswith("{") or '"tool_calls"' not in inner:
        return env
    try:
        v = json.loads(inner, strict=False)
    except ValueError:
        try:
            v = read_lenient(inner, 0)[0]
        except ValueError:
            v = None
    if not _is_envelope(v):
        return env
    v = dict(v)
    if v.get("status") is None:
        v["status"] = env.get("status")
    return _unwrap_nested(v, depth + 1)

def _build_call(call: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(call, dict):
        return None
    name, args = call.get("name"), call.get("arguments", _MISSING)
    fn = call.get("function")
    if not isinstance(name, str) and isinstance(fn, dict) and isinstance(fn.get("name"), str):
        name, args = fn["name"], fn.get("arguments", _MISSING)  # OpenAI shape
    if not isinstance(name, str):
        return None
    stray = {k: v for k, v in call.items() if k not in _CALL_KEYS}
    if args is _MISSING:
        args = stray  # arguments written at the call's top level
    else:
        if isinstance(args, str):
            try:
                parsed = json.loads(args, strict=False)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                args = parsed
        if isinstance(args, dict):
            for k, v in stray.items():  # `arguments` closed one key too early
                args.setdefault(k, v)
    if isinstance(args, str):
        args = {"content": args}
    return {"name": name, "arguments": {} if args is None else args}

# ---- emulate.mjs:899-1020 the scan -------------------------------------------------------
def parse_tool_reply(text: str) -> Dict[str, Any]:
    """{'content', 'calls', 'saw_envelope', 'broken', 'status'}; calls are [{'name','arguments'}] or None."""
    parts: List[str] = []
    calls: List[Dict[str, Any]] = []
    saw = broken = False
    status = None
    pending, i = "", 0
    while i < len(text):
        m_start = _OBJ_START.search(text, i)
        start = m_start.start() if m_start else -1
        if start < 0:
            pending += text[i:]
            break
        pending += text[i:start]
        end = _scan(text, start)
        sl = text[start:end] if end > 0 else ""
        parsed = _parse_loose(sl) if sl else None
        if not _is_envelope(parsed):
            try:
                val, lend = read_lenient(text, start)
                if _is_envelope(val):
                    parsed, end, sl = val, lend, text[start:lend]
            except ValueError:
                pass
        if _is_envelope(parsed):
            parsed = _unwrap_nested(parsed)
        piece = parsed["content"].strip() if isinstance(parsed, dict) and isinstance(parsed.get("content"), str) else ""
        region = sl or text[start:]
        looks = parsed is not None or '"tool_calls"' in region
        salvaged = None
        has_list = isinstance(parsed, dict) and isinstance(parsed.get("tool_calls"), list)
        if looks and not has_list and not piece:
            salvaged = _salvage_calls(region) or None
        here = looks and (_is_envelope(parsed) or bool(salvaged))
        if not here:
            if re.match(r'\{\s*"(?:content|tool_calls|status)"\s*:', region.lstrip()):
                broken = True  # an envelope we could not read: never show it raw
                if pending.strip():
                    parts.append(pending.strip())
                pending = ""
                m = re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"', region)
                if m:
                    try:
                        parts.append(json.loads('"%s"' % m.group(1), strict=False).strip())
                    except ValueError:
                        pass
                break
            pending += "{"
            i = start + 1
            continue
        saw = True
        st = parsed.get("status") if isinstance(parsed, dict) else None
        if isinstance(st, str) and st.strip().lower() in STATUSES:
            status = st.strip().lower()
        if pending.strip():
            parts.append(pending.strip())
        pending = ""
        if piece:
            parts.append(piece)
        for call in (salvaged or (parsed.get("tool_calls") if isinstance(parsed, dict) else None) or []):
            built = _build_call(call)
            if built:
                calls.append(built)
        i = end if end > 0 else len(text)
    if pending.strip():
        parts.append(pending.strip())
    content = strip_tool_markup("\n".join(p for p in parts if p))
    if not saw:
        return {"content": content if broken else strip_tool_markup(text.strip()), "calls": None,
                "saw_envelope": False, "broken": broken, "status": None}
    return {"content": content, "calls": calls or None, "saw_envelope": True,
            "broken": broken and not calls, "status": status}
