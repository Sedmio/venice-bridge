#!/usr/bin/env python3
"""Render the three terminal screenshots in docs/assets/screenshots/ from REAL output.

Run from anywhere:  python3 scripts/make_screenshots.py [--keep-svg]

What is real: the offline self-check (test_venice_bridge.py) is run as a subprocess and its stdout is
captured; the bridge's own HTTP handler is started on localhost and answers real HTTP requests.
What is demo data: the upstream is a small fake Venice on localhost (as in the self-check), and the
session / quota are stubbed. No cookie file is read, no account, token or network request is involved.
Writes only the three PNGs (plus sibling SVGs with --keep-svg). Stdlib only; needs one SVG renderer
(rsvg-convert, magick, inkscape or Google Chrome) already installed.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "assets", "screenshots")
SCALE = 2  # PNG pixels per SVG unit: 2 gives 3200x2000
W, H = 1600, 1000
MONO = "ui-monospace, 'SF Mono', Menlo, monospace"
SANS = "ui-sans-serif, -apple-system, 'Segoe UI', sans-serif"
CHAR_W = 0.602  # Menlo / SF Mono advance width in em
BRIDGE_PORT_LABEL = "127.0.0.1:18891"  # what a user types; the demo bridge listens on a free port


# ---------------------------------------------------------------- 1. capture real output
def run_selfcheck():
    r = subprocess.run([sys.executable, "test_venice_bridge.py"], cwd=ROOT, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=300,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if r.returncode != 0:
        sys.exit("self-check failed:\n" + r.stdout + r.stderr[-2000:])
    return r.stdout.rstrip().splitlines()


def capture_bridge():
    sys.dont_write_bytecode = True
    sys.path.insert(0, ROOT)
    import venice_bridge as vb

    def frame(event, data):
        return ("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode()

    def reply(text):  # Responses-style SSE: one message item, one text delta, then completed
        return (frame("response.output_item.added", {"output_index": 0, "item": {"type": "message"}})
                + frame("response.output_text.delta", {"output_index": 0, "delta": text})
                + frame("response.completed", {}))

    answers = {"chat": reply("PONG"),
               "tools": reply(json.dumps({"tool_calls": [{"name": "read", "arguments": {"filePath": "README.md"}}]}))}

    class FakeUpstream(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            out = answers["tools" if "tool_calls" in json.dumps(body) else "chat"]  # tools mode adds a format reminder
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(out)

    fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    bridge = ThreadingHTTPServer(("127.0.0.1", 0), vb.OpenAIBridgeHandler)
    for srv in (fake, bridge):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    vb.OUTERFACE_BASE = "http://127.0.0.1:%d" % fake.server_address[1]
    vb.VENICE.auth_headers = lambda force_refresh=False: {"Content-Type": "application/json"}
    vb.VENICE.get_jwt = lambda force=False: "demo-session"
    vb.VENICE.chat_quota = lambda: {"reset_at": 1795000000.0, "remaining": 9834, "resets_in_min": 312}
    vb.JUDGE_ENABLED = False
    base = "http://127.0.0.1:%d" % bridge.server_address[1]
    real_stderr, sys.stderr = sys.stderr, open(os.devnull, "w")  # the bridge logs every request to stderr

    def call(path, body=None):
        req = urllib.request.Request(base + path, json.dumps(body).encode() if body else None,
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    model = "deepseek-v4-1-flash-uncensored"
    chat_req = {"model": model, "messages": [{"role": "user", "content": "Reply with PONG"}]}
    tools_req = {"model": model,
                 "messages": [{"role": "user", "content": "Read README.md"}],
                 "tools": [{"type": "function", "function": {
                     "name": "read", "description": "Read a file",
                     "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}},
                                    "required": ["filePath"]}}}]}
    import venice_adapters as va  # the adapter in front of the demo bridge: what Claude Code talks to
    va.UPSTREAM = base + "/v1"
    adapter = ThreadingHTTPServer(("127.0.0.1", 0), va.Handler)
    threading.Thread(target=adapter.serve_forever, daemon=True).start()
    msg_req = {"model": model, "max_tokens": 1024,
               "messages": [{"role": "user", "content": "Read README.md"}],
               "tools": [{"name": "Read", "description": "Read a file",
                          "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}},
                                           "required": ["file_path"]}}]}
    try:
        got = _collect(call, chat_req, tools_req)
        answers["tools"] = reply(json.dumps({"tool_calls": [{"name": "Read", "arguments": {"file_path": "README.md"}}]}))
        req = urllib.request.Request("http://127.0.0.1:%d/v1/messages" % adapter.server_address[1],
                                     json.dumps(msg_req).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            got["msg"] = json.loads(r.read())
        got["msg_req"] = msg_req
    finally:
        sys.stderr = real_stderr
        for srv in (fake, bridge, adapter):
            srv.shutdown()
    return got


def _collect(call, chat_req, tools_req):
    got = {"health": call("/health"), "chat_req": chat_req, "chat": call("/v1/chat/completions", chat_req),
           "tools_req": tools_req, "tools": call("/v1/chat/completions", tools_req),
           "upstream_text": json.dumps({"tool_calls": [{"name": "read", "arguments": {"filePath": "README.md"}}]},
                                       separators=(",", ":"))}
    return got


# ---------------------------------------------------------------- 2. build terminal lines
# a line is a list of (colour-kind, text) segments; kinds: p prompt, c command, o output, d dim comment
def wrap(text, width, indent="  "):
    return textwrap.wrap(text, width, subsequent_indent=indent, break_long_words=True, break_on_hyphens=False) or [""]


def pretty(obj, width):
    """json.dumps(indent=2), long lines wrapped and kept under their own indent."""
    out = []
    for line in json.dumps(obj, indent=2, ensure_ascii=False).splitlines():
        lead = len(line) - len(line.lstrip())
        parts = wrap(line.strip(), width - lead - 2, "")
        out += [" " * (lead + (2 if i else 0)) + w for i, w in enumerate(parts)]
    return out


def screens(g, cols):
    url = BRIDGE_PORT_LABEL
    hdr = "-H 'Content-Type: application/json'"
    s1 = [[("p", "$ "), ("c", "python3 test_venice_bridge.py | tail -n 3")]]
    s1 += [[("o", l)] for l in g["selfcheck"][-3:]]
    s1 += [[], [("p", "$ "), ("c", "curl -s %s/health | python3 -m json.tool" % url)]]
    health = dict(g["health"])
    cache = health.pop("cache", None)  # shown on one line so the screen stays readable
    hl = pretty(health, cols)
    if cache is not None:
        hl[-2] += ","
        hl.insert(-1, '  "cache": ' + json.dumps(cache))
    s1 += [[("o", l)] for l in hl]
    s1 += [[], [("p", "$ "), ("c", "curl -s %s/v1/chat/completions %s \\" % (url, hdr))]]
    s1 += [[("c", "    -d '%s'" % json.dumps(g["chat_req"], separators=(",", ":")))]]
    s1 += [[("o", l)] for l in pretty(g["chat"], cols)]

    s2 = [[("d", "# the client sends an OpenAI-style request that declares a tool called `read`")],
          [("p", "$ "), ("c", "curl -s %s/v1/chat/completions %s \\" % (url, hdr))]]
    body = json.dumps(g["tools_req"], separators=(",", ":"))
    ws = wrap(body, cols - 8, "")
    s2 += [[("c", "    " + ("-d '" if i == 0 else "    ") + w + ("'" if i == len(ws) - 1 else ""))]
           for i, w in enumerate(ws)]
    s2 += [[], [("d", "# Venice's model answers with plain text only (the web route hides client tools):")],
           [("d", "#   " + g["upstream_text"])],
           [("d", "# the bridge parses that text and returns a real OpenAI tool_calls message:")]]
    s2 += [[("o", l)] for l in pretty(g["tools"], cols)]

    aurl = "127.0.0.1:18892"
    s3 = [[("d", "# venice_adapters.py speaks Anthropic Messages (Claude Code) and OpenAI Responses (Codex CLI)")],
          [("p", "$ "), ("c", "curl -s %s/v1/messages %s \\" % (aurl, hdr))]]
    ws = wrap(json.dumps(g["msg_req"], separators=(",", ":")), cols - 8, "")
    s3 += [[("c", "    " + ("-d '" if i == 0 else "    ") + w + ("'" if i == len(ws) - 1 else ""))]
           for i, w in enumerate(ws)]
    s3 += [[], [("d", "# the adapter forwards to the bridge and answers in Anthropic's shape: a tool_use block")]]
    s3 += [[("o", l)] for l in pretty(g["msg"], cols)]
    s3 += [[], [("p", "$ "), ("c", "claude-venice              "), ("d", "# Claude Code CLI on this adapter, isolated profile")]]
    return s1, s2, s3


# ---------------------------------------------------------------- 3. SVG + PNG
COLORS = {"p": "#8b5cf6", "c": "#ede9fe", "o": "#cbd5e1", "d": "#6b7280"}


def svg(title, lines, size):
    lh = round(size * 1.45)
    x0, y0 = 40 + 36, 40 + 56 + 44
    rows = []
    for i, segs in enumerate(lines):
        if not segs:
            continue
        tsp = "".join('<tspan fill="%s"%s>%s</tspan>' % (COLORS[k], ' font-weight="700"' if k == "p" else "", escape(t))
                      for k, t in segs)
        rows.append('<text x="%d" y="%d" xml:space="preserve">%s</text>' % (x0, y0 + i * lh, tsp))
    assert y0 + (len(lines) - 1) * lh < H - 40 - 24, "too many lines for the canvas: %d" % len(lines)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0b0f19"/><stop offset="1" stop-color="#161329"/></linearGradient>
<radialGradient id="glow" cx="0.85" cy="0.1" r="0.7"><stop offset="0" stop-color="#8b5cf6" stop-opacity="0.22"/><stop offset="1" stop-color="#8b5cf6" stop-opacity="0"/></radialGradient></defs>
<rect width="{W}" height="{H}" fill="url(#bg)"/><rect width="{W}" height="{H}" fill="url(#glow)"/>
<rect x="40" y="40" width="{W - 80}" height="{H - 80}" rx="16" fill="#0a0d16" stroke="#8b5cf6" stroke-opacity="0.45" stroke-width="2"/>
<path d="M40 56 a16 16 0 0 1 16 -16 h{W - 112} a16 16 0 0 1 16 16 v40 h-{W - 80} z" fill="#171a2b"/>
<circle cx="76" cy="68" r="8" fill="#ff5f57"/><circle cx="102" cy="68" r="8" fill="#febc2e"/><circle cx="128" cy="68" r="8" fill="#28c840"/>
<text x="{W // 2}" y="74" text-anchor="middle" font-family="{SANS}" font-size="18" fill="#9ca3af">{escape(title)}</text>
<g font-family="{MONO}" font-size="{size}">{chr(10).join(rows)}</g>
</svg>
"""


def find_renderer():
    if shutil.which("rsvg-convert"):
        return "rsvg-convert", lambda s, p: ["rsvg-convert", "-w", str(W * SCALE), "-h", str(H * SCALE), s, "-o", p]
    if shutil.which("magick"):
        return "magick", lambda s, p: ["magick", "-background", "none", "-density", str(96 * SCALE), s, p]
    if shutil.which("inkscape"):
        return "inkscape", lambda s, p: ["inkscape", s, "-w", str(W * SCALE), "-h", str(H * SCALE), "-o", p]
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if os.path.exists(chrome):
        return "chrome", lambda s, p: [chrome, "--headless", "--screenshot=" + p, "--window-size=%d,%d" % (W, H),
                                       "--force-device-scale-factor=%d" % SCALE, "file://" + s]
    return None, None


def main():
    keep = "--keep-svg" in sys.argv
    os.makedirs(OUT, exist_ok=True)
    g = capture_bridge()
    g["selfcheck"] = run_selfcheck()
    name, cmd = find_renderer()
    with tempfile.TemporaryDirectory() as tmp:
        for fname, title, pick in [("01-health-and-chat", "venice-bridge — health and chat", 0),
                                   ("02-opencode-tool-run", "venice-bridge — a model reply becomes a real tool call", 1),
                                   ("03-claude-code-adapter", "venice-adapters — Anthropic Messages for Claude Code", 2)]:
            size = 22
            while True:  # largest font where every line fits the window width and the canvas height
                cols = int((W - 80 - 72) / (size * CHAR_W))
                lines = screens(g, cols)[pick]
                try:
                    text = svg(title, lines, size)
                    break
                except AssertionError:
                    size -= 1
                    assert size >= 13, "content too long for a readable screenshot"
            svg_path = os.path.join(OUT if keep else tmp, fname + ".svg")
            with open(svg_path, "w") as f:
                f.write(text)
            png = os.path.join(OUT, fname + ".png")
            if not name:
                print("no SVG renderer found (rsvg-convert, magick, inkscape, Chrome): kept", svg_path)
                shutil.copy(svg_path, os.path.join(OUT, fname + ".svg"))
                continue
            subprocess.run(cmd(svg_path, png), check=True, capture_output=True)
            print("%s: %s (font %dpx, %d lines, via %s)" % (fname, os.path.relpath(png, ROOT), size, len(lines), name))


if __name__ == "__main__":
    main()
