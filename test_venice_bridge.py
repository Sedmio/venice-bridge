"""Offline self-check for the upstream-failure handling. Run: python3 test_venice_bridge.py"""
import base64
import datetime
import io
import json
import os
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace

import venice_bridge as vb

# never touch the real persisted Clerk client: every test in this run writes under a temp dir
STATE_DIR = tempfile.mkdtemp(prefix="vbridge-test-")
vb.CLIENT_STATE_FILE = os.path.join(STATE_DIR, "venice-bridge-client.json")
vb.BACKENDS = []  # never the real backends file or the real Ollama / LM Studio on this machine
vb._CACHE_ENABLED = False  # tests replay byte-identical bodies to exercise the upstream; the response cache would collapse them


def events(body: bytes):
    unread = []
    return list(vb._iter_chat_events(SimpleNamespace(stdout=io.BytesIO(body)), unread)), unread


def main() -> None:
    # normal reply: status line, headers, then the body is left unread
    s = io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\nBODY")
    assert vb._read_head(s) == (200, {"content-type": "text/event-stream"})
    assert s.read() == b"BODY"

    # interim 100 Continue is skipped; the over-quota reply is seen as 429
    s = io.BytesIO(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 429 Too Many Requests\r\nx-ratelimit-remaining: 0\r\n\r\n{}")
    status, headers = vb._read_head(s)
    assert status == 429 and headers["x-ratelimit-remaining"] == "0"

    # curl died before any response
    assert vb._read_head(io.BytesIO(b"")) == (0, {})

    # the body Venice sends when over quota carries no event: nothing to answer with
    got, unread = events(b"{}\n")
    assert got == [] and unread == []

    # reasoning and answer arrive in the same event; the item type tells them apart
    sse = (
        b'event: response.output_item.added\ndata: {"output_index":0,"item":{"type":"reasoning"}}\n\n'
        b'event: response.output_text.delta\ndata: {"output_index":0,"delta":"hm"}\n\n: keepalive\n\n'
        b'event: response.output_item.added\ndata: {"output_index":1,"item":{"type":"message"}}\n\n'
        b'event: response.output_text.delta\ndata: {"output_index":1,"delta":"PONG"}\n\n'
        b'event: response.completed\ndata: {}\n\ndata: [DONE]\n\n'
        b'event: response.output_text.delta\ndata: {"output_index":1,"delta":"late"}\n\n'
    )
    assert events(sse) == ([("reasoning", "hm"), ("text", "PONG"), ("done", "")], [])
    assert events(b'event: response.failed\ndata: {"response":{"error":{"message":"x"}}}\n\n')[0][0][0] == "error"

    # tool calls are found bare, fenced and after prose; plain text is not a call
    for reply in ('{"tool_calls":[{"name":"read","arguments":{"filePath":"a"}}]}',
                  '```json\n{"tool_calls":[{"name":"read","arguments":{"filePath":"a"}}]}\n```',
                  'I will read it.\n{ "tool_calls": [{"name":"read","arguments":{"filePath":"a"}}] } trailing'):
        calls = vb._try_parse_tool_calls(reply)
        assert calls and calls[0]["function"] == {"name": "read", "arguments": '{"filePath": "a"}'}, reply
    assert vb._try_parse_tool_calls("The port is 18891.") is None
    # an envelope no call can be read from is flagged; an explicit empty list and quoting prose are not
    assert vb._read_reply('{"tool_calls": [') == (None, "", True)
    assert vb._read_reply('I will edit it.\n{"tool_calls": [') == (None, "I will edit it.", True)
    assert vb._read_reply('{"content":"Done.","tool_calls":[]}') == (None, "Done.", False)
    assert vb._read_reply("The port is 18891.") == (None, "The port is 18891.", False)
    assert vb._read_reply("A long answer that explains the format. " * 6 + '{"tool_calls":[...]}')[2] is False

    def names(reply, tools=None):
        return [c["function"]["name"] for c in vb._read_reply(reply, tools)[0] or []]

    def args(reply):
        return json.loads(vb._read_reply(reply)[0][0]["function"]["arguments"])

    # shapes the first parser lost: content first, two envelopes, raw newline, invalid escape
    assert names('{"content":"","tool_calls":[{"name":"read","arguments":{"filePath":"a"}}]}') == ["read"]
    assert names('{"tool_calls":[{"name":"read","arguments":{}}]}{"tool_calls":[{"name":"bash","arguments":{}}]}') == ["read", "bash"]
    assert args('{"tool_calls":[{"name":"write","arguments":{"content":"a\nb"}}]}') == {"content": "a\nb"}
    assert "a\\|b" in args('{"tool_calls":[{"name":"bash","arguments":{"command":"grep a\\|b f"}}]}')["command"]

    # the real malformed replies collected by opencode-venice-web (test/fixtures/real-replies.mjs, MIT)
    here = os.path.dirname(os.path.abspath(__file__))
    want = {
        "DSML_CONTENT_WRAPPER": ["read"], "DSML_TAIL": ["shell"], "EARLY_ARGUMENTS_CLOSE": ["edit", "edit"],
        "EARLY_ARGUMENTS_CLOSE_LAST": ["edit"], "INVENTED_TOOL_RESPONSE": ["glob"], "MISSING_CALL_BRACE": ["shell"],
        "NESTED_ENVELOPE": ["shell"], "OPEN_CALL_BETWEEN_CALLS": ["edit", "edit"],
    }
    for key, reply in json.load(open(os.path.join(here, "test_fixtures_real_replies.json"), encoding="utf-8")).items():
        calls, content, _ = vb._read_reply(reply)
        assert [c["function"]["name"] for c in calls] == want[key], key
        assert "tool_calls" not in content and "DSML" not in content, key
        if "EARLY_ARGUMENTS_CLOSE" in key:  # the argument that fell outside `arguments` is put back
            assert all("newString" in json.loads(c["function"]["arguments"]) for c in calls), key

    # the reply that made DeepSeek V4.1 Flash (1017) write no file: a sentence, a marker, then the call
    # inside <tool_calls> tags, with the quotes inside the file content left unescaped
    tagged = json.load(open(os.path.join(here, "test_fixture_tagged_call.json"), encoding="utf-8"))["DEEPSEEK_1017_TAGGED"]
    wtools = [{"type": "function", "function": {"name": "write"}}, {"type": "function", "function": {"name": "read"}}]
    calls, content, unreadable = vb._read_reply(tagged, wtools)
    wargs = json.loads(calls[0]["function"]["arguments"])
    assert [c["function"]["name"] for c in calls] == ["write"] and not unreadable
    assert content == "I'll build the app. Starting with the three files."  # no tag, no @@WRITE@@
    compile(wargs["content"], "todo.py", "exec")  # the whole file, not one cut at an inner quote
    assert wargs["content"].rstrip().endswith("sys.exit(main(sys.argv[1:]))") and 'os.path.join(' in wargs["content"]
    two = 'ok <tool_calls>[{"name":"read","arguments":{"filePath":"a"}}, {"name":"write","arguments":{"filePath":"b","content":"x"}}]</tool_calls>'
    assert [c["function"]["name"] for c in vb._read_reply(two, wtools)[0]] == ["read", "write"]
    one = '<tool_call>\n{"name": "read", "arguments": {"filePath": "a"}}\n</tool_call>'
    assert json.loads(vb._read_reply(one, wtools)[0][0]["function"]["arguments"]) == {"filePath": "a"}
    cut = 'I will write it.<tool_calls>{"name":"write","arguments":{"filePath":"a","content":"half'
    assert vb._read_reply(cut, wtools) == (None, "I will write it.", True)  # unreadable: retried, never half a file
    junk = '<tool_calls>{"name":"write","arguments":{"filePath":"a","content":"x"}} and then some words</tool_calls>'
    jcalls = vb._read_reply(junk, wtools)[0]  # a complete call followed by stray words is still that call
    assert json.loads(jcalls[0]["function"]["arguments"]) == {"filePath": "a", "content": "x"}
    out = []
    g = vb._LiveGate(lambda: out.append("<start>"), out.append)
    for ch in tagged:
        g.feed(ch)
    assert out == [] and not g.live  # never shown as text
    out = []
    g = vb._LiveGate(lambda: out.append("<start>"), out.append)
    for ch in "y" * 250 + " now the file.@@WRITE@@<tool_calls>{\"name\":\"write\"":
        g.feed(ch)
    assert "".join(out) == "<start>" + "y" * 250 + " now the file." and g.rest().startswith("@@WRITE@@<tool_calls>")

    # --- gap-fix routing (tool-audit 2026-11-20): OpenCode 2.3.2 tools take `path`, not `filePath` ---
    oc = [{"type": "function", "function": {"name": n, "parameters": {"type": "object", "properties": pr}}} for n, pr in [
        ("read", {"path": {"type": "string"}, "offset": {"type": "number"}}),
        ("write", {"path": {"type": "string"}, "content": {"type": "string"}}),
        ("edit", {"path": {"type": "string"}, "oldString": {"type": "string"}, "newString": {"type": "string"}, "replaceAll": {"type": "boolean"}}),
        ("grep", {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}}),
        ("shell", {"command": {"type": "string"}, "workdir": {"type": "string"}}),
        ("subagent", {"agent": {"type": "string"}, "description": {"type": "string"}, "prompt": {"type": "string"}}),
        ("skill", {"id": {"type": "string"}}),
        ("websearch", {"query": {"type": "string"}}),
        ("webfetch", {"url": {"type": "string"}}),
        ("execute", {"code": {"type": "string"}}),
    ]]

    def g(reply):
        calls = vb._read_reply(reply, oc)[0] or []
        return [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls]

    # file-name aliases reach `path`
    assert g('{"tool_calls":[{"name":"cat","arguments":{"file":"a.txt"}}]}') == [("read", {"path": "a.txt"})]
    assert g('{"tool_calls":[{"name":"read_file","arguments":{"absolute_path":"/x/a"}}]}') == [("read", {"path": "/x/a"})]
    assert g('{"tool_calls":[{"name":"edit_file","arguments":{"target_file":"a","old_str":"x","new_str":"y"}}]}') == [
        ("edit", {"path": "a", "oldString": "x", "newString": "y"})]
    # new shell/grep names + argument renames (workdir, include, replaceAll)
    assert g('{"tool_calls":[{"name":"exec_command","arguments":{"command":"ls","cwd":"src"}}]}') == [("shell", {"command": "ls", "workdir": "src"})]
    assert g('{"tool_calls":[{"name":"search_file_content","arguments":{"pattern":"TODO","file_pattern":"*.py"}}]}') == [
        ("grep", {"pattern": "TODO", "include": "*.py"})]
    assert g('{"tool_calls":[{"name":"search_replace","arguments":{"path":"a","oldString":"x","newString":"y","replace_all":true}}]}') == [
        ("edit", {"path": "a", "oldString": "x", "newString": "y", "replaceAll": True})]
    # execute-with-command goes to shell even though `execute` is a declared Code Mode tool
    assert g('{"tool_calls":[{"name":"execute","arguments":{"command":"ls"}}]}') == [("shell", {"command": "ls"})]
    # a real execute (code) is left alone (declared) -> passes through as execute
    assert g('{"tool_calls":[{"name":"execute","arguments":{"code":"print(1)"}}]}') == [("execute", {"code": "print(1)"})]
    # wrapper prefix stripped
    assert g('{"tool_calls":[{"name":"functions.shell","arguments":{"command":"ls"}}]}') == [("shell", {"command": "ls"})]
    # command as a list -> one string
    assert g('{"tool_calls":[{"name":"shell","arguments":{"command":["bash","-lc","ls -la"]}}]}') == [("shell", {"command": "bash -lc ls -la"})]
    # sub-agent: Agent alias -> subagent, agent filled, description from prompt
    out = g('{"tool_calls":[{"name":"Agent","arguments":{"subagent_type":"reviewer","prompt":"Review the diff carefully"}}]}')
    assert out == [("subagent", {"agent": "reviewer", "prompt": "Review the diff carefully", "description": "Review the diff carefully"})], out
    out = g('{"tool_calls":[{"name":"subagent","arguments":{"prompt":"do x"}}]}')
    assert out[0][1]["agent"] == "general" and out[0][1]["description"] == "do x"
    # skill: foreign name + arg
    assert g('{"tool_calls":[{"name":"use_skill","arguments":{"skill":"deploy"}}]}') == [("skill", {"id": "deploy"})]
    # webfetch: url pulled out of a prompt
    assert g('{"tool_calls":[{"name":"web_fetch","arguments":{"prompt":"read https://example.com/x please"}}]}') == [
        ("webfetch", {"url": "https://example.com/x"})]
    # --- second gap batch (tool-mapping §6/§7, 2026-11-20) ---
    q_oc = [{"type": "function", "function": {"name": "question", "parameters": {"type": "object", "properties": {"questions": {"type": "array"}}}}},
            {"type": "function", "function": {"name": "subagent", "parameters": {"type": "object", "properties": {"agent": {"type": "string"}, "description": {"type": "string"}, "prompt": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "grep", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}, "limit": {"type": "number"}}}}},
            {"type": "function", "function": {"name": "shell", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "background": {"type": "boolean"}}}}},
            {"type": "function", "function": {"name": "write", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "edit", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "oldString": {"type": "string"}, "newString": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]

    def q(reply):
        calls = vb._read_reply(reply, q_oc)[0] or []
        return [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls]

    # AskUserQuestion -> question, a single string becomes questions[], multiSelect -> multiple
    out = q('{"tool_calls":[{"name":"AskUserQuestion","arguments":{"question":"Pick a branch name"}}]}')
    assert out[0][0] == "question" and out[0][1]["questions"][0]["question"] == "Pick a branch name"
    assert out[0][1]["questions"][0]["header"] == "Pick a branch name"[:30] and out[0][1]["questions"][0]["options"] == []
    out = q('{"tool_calls":[{"name":"question","arguments":{"questions":[{"question":"x","header":"h","options":[1],"multiSelect":true,"junk":9}]}}]}')
    item = out[0][1]["questions"][0]
    assert item == {"question": "x", "header": "h", "options": [1], "multiple": True}  # junk/multiSelect gone
    # Claude sub-agent value map: general-purpose -> general; Explore -> explore
    out = q('{"tool_calls":[{"name":"Agent","arguments":{"subagent_type":"general-purpose","prompt":"do x"}}]}')
    assert out[0] == ("subagent", {"agent": "general", "prompt": "do x", "description": "do x"}), out
    assert q('{"tool_calls":[{"name":"subagent","arguments":{"agent":"Explore","prompt":"y"}}]}')[0][1]["agent"] == "explore"
    # grep: glob -> include (pattern present), head_limit -> limit; shell: run_in_background -> background
    assert q('{"tool_calls":[{"name":"grep","arguments":{"pattern":"TODO","glob":"*.py","head_limit":10}}]}') == [
        ("grep", {"pattern": "TODO", "include": "*.py", "limit": 10})]
    assert q('{"tool_calls":[{"name":"shell","arguments":{"command":"sleep 1","run_in_background":true}}]}') == [
        ("shell", {"command": "sleep 1", "background": True})]
    # edit_file with whole-file content (no oldString) -> write
    assert q('{"tool_calls":[{"name":"edit_file","arguments":{"path":"a.py","content":"print(1)"}}]}') == [
        ("write", {"path": "a.py", "content": "print(1)"})]
    # a real edit (oldString) stays an edit
    assert q('{"tool_calls":[{"name":"edit","arguments":{"path":"a","oldString":"x","newString":"y"}}]}') == [
        ("edit", {"path": "a", "oldString": "x", "newString": "y"})]
    # extra shell names
    assert q('{"tool_calls":[{"name":"execute_shell","arguments":{"command":"ls"}}]}') == [("shell", {"command": "ls"})]
    # multi-file / multi-command / unmappable edit payloads are NOT routed -> clean "no such tool"
    assert q('{"tool_calls":[{"name":"edit_file","arguments":{"path":"a","code_edit":"..."}}]}') == [("edit_file", {"path": "a", "code_edit": "..."})]
    assert q('{"tool_calls":[{"name":"str_replace_based_edit_tool","arguments":{"command":"view","path":"a"}}]}')[0][0] == "str_replace_based_edit_tool"
    # new sub-agent names from the survey route to subagent
    assert q('{"tool_calls":[{"name":"start_sub_agent","arguments":{"prompt":"p"}}]}')[0][0] == "subagent"
    # one call -> several (MultiEdit / diff / read_many_files)
    me = q('{"tool_calls":[{"name":"MultiEdit","arguments":{"file_path":"a.py","edits":[{"old_string":"x","new_string":"y"},{"old_string":"a","new_string":"b"}]}}]}')
    assert me == [("edit", {"path": "a.py", "oldString": "x", "newString": "y"}), ("edit", {"path": "a.py", "oldString": "a", "newString": "b"})], me
    dq = q('{"tool_calls":[{"name":"replace_in_file","arguments":{"path":"a.py","diff":"<<<<<<< SEARCH\nold1\n=======\nnew1\n>>>>>>> REPLACE\n<<<<<<< SEARCH\nold2\n=======\nnew2\n>>>>>>> REPLACE"}}]}')
    assert dq == [("edit", {"path": "a.py", "oldString": "old1", "newString": "new1"}), ("edit", {"path": "a.py", "oldString": "old2", "newString": "new2"})], dq
    rm = q('{"tool_calls":[{"name":"read_many_files","arguments":{"paths":["/x/a","/x/b"]}}]}')
    assert rm == [("read", {"path": "/x/a"}), ("read", {"path": "/x/b"})], rm
    # replace_file -> write
    assert q('{"tool_calls":[{"name":"replace_file","arguments":{"path":"a","content":"x"}}]}') == [("write", {"path": "a", "content": "x"})]
    # a normal single edit is unaffected by the expander
    assert q('{"tool_calls":[{"name":"edit","arguments":{"path":"a","oldString":"x","newString":"y"}}]}') == [("edit", {"path": "a", "oldString": "x", "newString": "y"})]

    # unknown keys are dropped (tools are additionalProperties:false): replace_all leftover goes, replaceAll stays
    # unknown keys are dropped (tools are additionalProperties:false): replace_all leftover goes, replaceAll stays
    out = g('{"tool_calls":[{"name":"edit","arguments":{"path":"a","oldString":"x","newString":"y","replace_all":true,"junk":1}}]}')
    assert out == [("edit", {"path": "a", "oldString": "x", "newString": "y", "replaceAll": True})], out
    # a clean, correct call is untouched
    assert g('{"tool_calls":[{"name":"read","arguments":{"path":"a","offset":5}}]}') == [("read", {"path": "a", "offset": 5})]
    # patch/replace_in_file are NOT routed to edit (they carry diffs) -> left as the unknown name
    # patch/replace_in_file are NOT routed to edit (they carry diffs) -> left as the unknown name
    assert g('{"tool_calls":[{"name":"patch","arguments":{"patchText":"@@"}}]}') == [("patch", {"patchText": "@@"})]
    # a listing name routes to a shell ls
    assert g('{"tool_calls":[{"name":"list","arguments":{"path":"src"}}]}') == [("shell", {"command": "ls -la src"})]

    # the model's own markup, as DeepSeek V4.1 Flash writes it on the plain chat route (real reply, 2026-11-19)
    dsml = ('<｜DSML｜ invoke name="shell">\n<｜DSML｜ parameter name="command" string="true">pwd && ls -la</｜DSML｜ parameter>\n'
            '</｜DSML｜ invoke>\n</｜DSML｜ calls>')
    stools = [{"type": "function", "function": {"name": "shell", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}}}}},
              {"type": "function", "function": {"name": "write", "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}, "content": {"type": "string"}}}}}]
    calls, content, unreadable = vb._read_reply(dsml, stools)
    assert [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls] == [("shell", {"command": "pwd && ls -la"})]
    assert content == "" and not unreadable
    two_native = ('I will write it.\n<｜DSML｜ function_calls>\n<｜DSML｜ invoke name="write">\n<｜DSML｜ parameter name="path" string="true">a.py</｜DSML｜ parameter>\n'
                  '<｜DSML｜ parameter name="content" string="true">x = "1"\nprint(x)\n</｜DSML｜ parameter>\n</｜DSML｜ invoke>\n'
                  '<invoke name="bash"><parameter name="command">ls</parameter><parameter name="timeout" string="false">5</parameter></invoke>')
    calls, content, unreadable = vb._read_reply(two_native, stools)
    assert [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls] == [
        ("write", {"filePath": "a.py", "content": 'x = "1"\nprint(x)\n'}), ("shell", {"command": "ls", "timeout": 5})]
    assert content == "I will write it."  # the markup is never shown
    assert vb._read_reply('<｜DSML｜ invoke name="', stools) == (None, "", False) or vb._read_reply('<｜DSML｜ invoke name="', stools)[0] is None
    out = []
    g = vb._LiveGate(lambda: out.append("<start>"), out.append)
    for ch in "y" * 250 + ' now.\n<｜DSML｜ invoke name="shell">':
        g.feed(ch)
    assert "".join(out) == "<start>" + "y" * 250 + " now.\n" and g.rest().startswith("<｜DSML｜")

    # only tools the request declared; the case is corrected; a lone invented name is passed on
    tools = [{"type": "function", "function": {"name": "read"}}, {"type": "function", "function": {"name": "bash"}}]
    assert names('{"tool_calls":[{"name":"Read","arguments":{}},{"name":"view_media","arguments":{}}]}', tools) == ["read"]
    assert names('{"tool_calls":[{"name":"frobnicate","arguments":{"path":"a"}}]}', tools) == ["frobnicate"]

    # routing: a foreign tool name goes to the client's tool for that job, with its argument names
    rt = [{"type": "function", "function": {"name": n, "parameters": {"type": "object", "properties": {k: {"type": "string"} for k in keys}}}}
          for n, keys in (("read", ["filePath"]), ("bash", ["command", "description"]), ("edit", ["filePath", "oldString", "newString"]),
                          ("write", ["filePath", "content"]), ("grep", ["pattern", "path"]), ("webfetch", ["url"]))]

    def routed(reply):
        calls = vb._read_reply(reply, rt)[0] or []
        return [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls]

    assert routed('{"tool_calls":[{"name":"cat","arguments":{"path":"a.txt"}}]}') == [("read", {"filePath": "a.txt"})]
    assert routed('{"tool_calls":[{"name":"shell","arguments":{"cmd":"ls"}}]}') == [("bash", {"command": "ls"})]
    assert routed('{"tool_calls":[{"name":"execute","arguments":{"command":"ls"}}]}') == [("bash", {"command": "ls"})]
    assert routed('{"tool_calls":[{"name":"str_replace","arguments":{"file_path":"a","old_str":"x","new_str":"y"}}]}') == [
        ("edit", {"filePath": "a", "oldString": "x", "newString": "y"})]
    assert routed('{"tool_calls":[{"name":"create_file","arguments":{"filename":"a","contents":"x"}}]}') == [
        ("write", {"filePath": "a", "content": "x"})]
    assert routed('{"tool_calls":[{"name":"web_scrape","arguments":{"link":"https://example.com"}}]}') == [
        ("webfetch", {"url": "https://example.com"})]
    assert routed('{"tool_calls":[{"name":"list_dir","arguments":{"path":"src dir"}}]}') == [("bash", {"command": "ls -la 'src dir'"})]
    # the desktop app calls its command tool `shell`, the CLI `bash`: each name reaches whichever exists
    desk = [{"type": "function", "function": {"name": "shell", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}]
    got = vb._read_reply('{"tool_calls":[{"name":"bash","arguments":{"cmd":"ls"}}]}', desk)[0]
    assert got[0]["function"]["name"] == "shell" and json.loads(got[0]["function"]["arguments"]) == {"command": "ls"}
    assert "run a command with `shell`" in vb._route_hint({"shell": "shell"})
    # the sub-agent tool is `subagent` there and `task` in the CLI; models call it Agent / Task / agent
    sub = [{"type": "function", "function": {"name": "subagent", "parameters": {"type": "object", "properties": {
        "description": {"type": "string"}, "prompt": {"type": "string"}}}}}]
    for said in ("Agent", "Task", "task", "agent"):
        got = vb._read_reply('{"tool_calls":[{"name":"%s","arguments":{"description":"d","task":"go"}}]}' % said, sub)[0]
        assert got[0]["function"]["name"] == "subagent", said
        assert json.loads(got[0]["function"]["arguments"]) == {"description": "d", "prompt": "go"}, said
    assert "start a sub-agent with `subagent`" in vb._route_hint({"subagent": "subagent"})
    # Venice's media tool is a file read only when its id is a path; a made-up id is not
    assert routed('{"tool_calls":[{"name":"get_attachment","arguments":{"media_ids":["src/app.py"]}}]}') == [("read", {"filePath": "src/app.py"})]
    assert routed('{"tool_calls":[{"name":"view_media","arguments":{"media_ids":["nope"]}}]}') == [("view_media", {"media_ids": ["nope"]})]
    # the right tool with a wrong argument name is fixed too; a name the tool knows is left alone
    assert routed('{"tool_calls":[{"name":"read","arguments":{"path":"a.txt"}}]}') == [("read", {"filePath": "a.txt"})]
    assert routed('{"tool_calls":[{"name":"grep","arguments":{"query":"x","path":"src"}}]}') == [("grep", {"pattern": "x", "path": "src"})]
    # `path` is not in this read's schema (it declares filePath) -> dropped (additionalProperties:false)
    assert routed('{"tool_calls":[{"name":"read","arguments":{"filePath":"a","path":"b"}}]}') == [("read", {"filePath": "a"})]

    # the retry after a swallowed call names the tools and where they go
    known = {"read": "read", "bash": "bash"}
    steer = vb._steer_reminder("server_tool", {"recognized": ["get_attachment"]}, known)
    assert "`get_attachment`" in steer and "use `read`" in steer and '{"tool_calls"' in steer
    steer = vb._steer_reminder("server_loop", {"unrecognized": ["Bash", "glob"]}, known)
    assert steer.startswith(vb.NATIVE_CALL_REMINDER) and "`bash`, `glob`" in steer
    assert vb._steer_reminder("server_loop", {}, known) == vb.NATIVE_CALL_REMINDER
    assert "read a file with `read`" in vb._route_hint(known) and "never use them" in vb._route_hint({})
    quoting = "The bridge asks the model for this shape. " * 8 + '{"tool_calls":[{"name":"<exact tool name>","arguments":{}}]}'
    assert names(quoting, tools) == []
    assert vb._read_reply(quoting, tools)[2] is False
    # an answer that merely contains JSON is left alone
    plain = 'Here is the message:\n{"content": "hi", "role": "user"}'
    assert vb._read_reply(plain, tools) == (None, plain, False)

    # the reminder's one example is a real call when nothing is declared, and prose when real tools are
    example = vb.TOOL_REMINDER[vb.TOOL_REMINDER.index('{"tool_calls":[{"name":"example_tool"'):].split("} ", 1)[0] + "}"
    assert len(vb.TOOL_REMINDER) < 700 and names(example) == ["example_tool"]
    assert names(vb.TOOL_REMINDER, tools) == []

    # request translation: system joined, tool result and assistant call kept, reminder last
    payload, has_tools = vb.openai_to_venice_chat({
        "model": "venice-bridge/m", "tools": [{"type": "function", "function": {"name": "read", "parameters": {}}}],
        "messages": [
            {"role": "system", "content": "S\x00"}, {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok", "tool_calls": [{"id": "c1", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "data"},
        ]})
    texts = [(m["role"], m["content"][0]["text"]) for m in payload["messages"]]
    assert has_tools and payload["modelId"] == "m" and "\x00" not in payload["systemPrompt"]["instructions"]
    assert "- read()" in payload["systemPrompt"]["instructions"]
    assert texts[1] == ("assistant", 'ok\n{"tool_calls": [{"name": "read", "arguments": {}}]}')
    assert texts[2] == ("user", "[Tool Result c1]\ndata") and texts[3][1] == vb.TOOL_REMINDER

    # tool list tiers: built-ins and tools used so far get their full argument structure as JSON
    nested = {"$schema": "x", "type": "object", "additionalProperties": False, "required": ["todos"], "properties": {
        "todos": {"type": "array", "description": "d" * 400, "items": {"type": "object", "required": ["status"],
                  "properties": {"status": {"type": "string", "enum": ["pending", "done"]},
                                 "title": {"type": "string"}, "description": {"type": "string"}}}}}}
    mcp = lambda name, desc="does things " * 20: {"type": "function", "function": {
        "name": name, "description": desc, "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}
    def schema_of(block, name):
        line = block.split("- %s(" % name, 1)[1].split("\n")[1]
        assert line.startswith("  arguments schema: "), line
        return json.loads(line.split(": ", 1)[1])
    block = vb._build_tool_instructions([{"type": "function", "function": {"name": "todowrite", "parameters": nested}}])
    got = schema_of(block, "todowrite")
    assert "$schema" not in got and "additionalProperties" not in got and got["required"] == ["todos"]
    item = got["properties"]["todos"]["items"]
    assert item["required"] == ["status"] and item["properties"]["status"]["enum"] == ["pending", "done"]
    assert set(item["properties"]) == {"status", "title", "description"}  # arguments named like keywords stay
    assert len(got["properties"]["todos"]["description"]) == vb._ARG_DESC_CAP
    # an MCP tool is a one-liner until the conversation has called it
    convo = {"tools": [mcp("srv_find"), mcp("srv_other")], "messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "function": {"name": "srv_find", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c", "content": "r"}]}
    instr = vb.openai_to_venice_chat(convo)[0]["systemPrompt"]["instructions"]
    assert schema_of(instr, "srv_find") == {"type": "object", "properties": {"q": {"type": "string"}}}
    assert "- srv_other(q:string): does" in instr and "arguments schema" not in instr.split("- srv_other(")[1]
    # tiny budget: within budget, every tool still named, the last line says how many lost their arguments
    many = [{"type": "function", "function": {"name": "bash", "parameters": nested}}] + [mcp("srv_t%02d" % i) for i in range(30)]
    floor = len(vb._build_tool_instructions(many, budget=0))
    small = vb._build_tool_instructions(many, budget=floor + 600)
    assert len(small) <= floor + 600 and all("srv_t%02d" % i in small for i in range(30))
    assert small.endswith("tools are listed by name only.)") and "- srv_t00(q:string): does" in small
    assert schema_of(small, "bash")["properties"]["todos"]["items"]["properties"]["status"]["enum"] == ["pending", "done"]
    assert "listed by name only" not in vb._build_tool_instructions(many)  # the default budget fits this
    # no description, no parameters, odd parameters: no crash
    odd = vb._build_tool_instructions([{"function": {"name": "x_y"}}, {"function": {"name": "z", "description": None,
                                       "parameters": None}}, {"function": {"name": "w_v", "parameters": "?"}}, {"x": 1}])
    assert "- x_y(): " in odd and "- z(): " in odd and "- w_v(): " in odd

    # a real captured stream, when the capture is still around
    har = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venice.ai.har")
    if os.path.exists(har):
        for entry in json.load(open(har, encoding="utf-8", errors="ignore"))["log"]["entries"]:
            if "/inference/chat/agents/" in entry["request"]["url"] and entry["request"]["method"] == "POST":
                got, unread = events(entry["response"]["content"]["text"].encode("utf-8"))
                kinds = {k for k, _ in got}
                assert kinds == {"reasoning", "text", "done"} and unread == [], (kinds, unread)
                print("har replay: %d events, %d answer chars" % (len(got), sum(len(t) for k, t in got if k == "text")))

    assert vb._upstream_error(403, "x")[0] == 403 and vb._upstream_error(500, "")[0] == 502 and vb._upstream_error(0, "")[0] == 502
    # readable detail from the JSON body; 413 is a context-length error for the client, 402 a plan problem
    code, message, kind = vb._upstream_error(413, '{"error":"userPromptTooLong"}')
    assert code == 400 and "userPromptTooLong" in message and kind == ("invalid_request_error", "context_length_exceeded")
    assert "boom" in vb._upstream_error(500, '{"error":{"message":"boom"}}')[1]
    assert vb._upstream_error(500, "not json")[1].endswith("not json")
    assert vb._upstream_error(402, "")[0] == 402 and "credits" in vb._upstream_error(402, "")[1]
    # a 429 with quota left is a passing overload: no lock, stays retryable
    vb.VENICE.chat_quota = lambda: {"remaining": 9000, "reset_at": vb.time.time() + 86400}
    vb.VENICE.quota_until = 0.0
    assert vb._upstream_error(429, "")[0] == 429 and vb.VENICE.quota_until == 0.0
    vb.VENICE.chat_quota = lambda: {"remaining": 0, "reset_at": vb.time.time() + 600}
    assert vb._upstream_error(429, "")[0] == 403 and vb.VENICE.quota_until > vb.time.time()
    # over quota: answered as a non-retryable 403 that names the wait
    vb.VENICE.quota_until = vb.time.time() + 600
    code, message = vb._quota_error()
    assert code == 403 and "10 min" in message
    # judge verdicts and the reply ranking
    assert vb._parse_verdict('Sure! {"verdict": "Finished"}') == "finished"
    assert vb._parse_verdict('{"verdict":"stopped_early","reason":"x"}') == "stopped_early"
    for bad in ('{"verdict":"maybe"}', "no json", "", "{", '["finished"]'):
        assert vb._parse_verdict(bad) is None, bad
    r = lambda calls=None, content="", trigger=None: {"calls": calls, "content": content, "trigger": trigger}
    call, prose, broken_prose = r(calls=[1]), r(content="answer"), r(content="I will", trigger="unreadable")
    nothing, empty = r(trigger="server_loop"), r(trigger="empty")
    assert vb._better(call, prose) and vb._better(prose, broken_prose) and vb._better(broken_prose, nothing)
    assert not vb._better(nothing, prose) and not vb._better(empty, broken_prose) and not vb._better(prose, call)
    assert not vb._better(r(calls=[2]), call)  # between two call sets the first stays
    assert not vb._better(r(content="other"), prose) and vb._better(r(content="other"), prose, replace=True)
    assert vb._better(empty, nothing)  # between two failures the newer one picks the next reminder
    # the judge request: newest user message, last two tool results, the reply; tails only, small
    long = {"messages": [{"role": "user", "content": "OLDMSG"}, {"role": "user", "content": "Q" * 9000}] + [
        {"role": "tool", "tool_call_id": str(i), "content": str(i) * 9000} for i in range(4)]}
    jp = vb._judge_payload(long, "R" * 9000)
    jtext = jp["messages"][0]["content"][0]["text"]
    assert jp["reasoningMode"] == "disabled" and jp["tools"] == [] and jp["modelId"] == vb.JUDGE_MODEL
    assert "OLDMSG" not in jtext and "[1] …2" in jtext and "[2] …3" in jtext and "0" * 10 not in jtext
    assert len(jtext) + len(jp["systemPrompt"]["instructions"]) <= 11_000
    assert "(none)" in vb._judge_payload({"messages": []}, "")["messages"][0]["content"][0]["text"]
    assert vb._ends_after_tool({"messages": [{"role": "user"}, {"role": "tool"}, {"role": "system"}]})
    assert not vb._ends_after_tool({"messages": [{"role": "tool"}, {"role": "user"}]})
    print("ok")


def end_to_end() -> None:
    """The bridge against a fake Venice on localhost: a cut stream, a stale token and a failed
    response must reach the client as errors or a retry, never as a normal answer; tools-mode
    retries carry the right reminder and stay within their budgets; the judge can only add a retry."""
    import threading
    import urllib.error
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def frame(event, data):
        return ("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode()

    LONG = "The change is finished and every check passes. " * 7  # 329 chars: past the stream gate
    LONG_PARTS = [LONG[i:i + 40] for i in range(0, len(LONG), 40)]
    text = lambda t: (frame("response.output_item.added", {"output_index": 0, "item": {"type": "message"}})
                      + frame("response.output_text.delta", {"output_index": 0, "delta": t}))
    done = frame("response.completed", {})
    call = text('{"tool_calls":[{"name":"read","arguments":{"filePath":"a"}}]}') + done
    swallowed = (text("I will read it.") + frame("veniceai:tool_call_generation.in_progress",
                 {"detected_tools": [{"type": "Unrecognized", "name": "read"}]}) * 4
                 + text(" Tool not found, my tools are broken.") + done)
    broken = text('I will edit it.\n{"tool_calls": [') + done
    prose = text("I will now run the tests.") + done
    static = {
        "ok": text("PONG") + done,
        "agentonly": text("PONG") + done,
        "stale": text("PONG") + done,
        "cut": text('{"tool_calls":[{"name":"write","arguments":{"filePath":"a","content":"half'),  # no response.completed
        "failed": text("so") + frame("response.failed", {"response": {"error": {"message": "boom"}}}),
        "empty": done,
        "swallowed": swallowed,
        "broken": broken,
        "broken_bare": text('{"tool_calls": [') + done,
        "prose": prose,
        # one refused native call, then the model uses our format: the call must come through
        "recovered": text("I will read it.") + frame("veniceai:tool_call_generation.in_progress",
            {"detected_tools": [{"type": "Unrecognized", "name": "read"}]})
            + text('{"tool_calls":[{"name":"read","arguments":{"filePath":"a"}}]}') + done,
        "call": call,
        # long prose arrives in pieces and is shown while it comes; a call after it is held back
        "long_prose": b"".join(text(p) for p in LONG_PARTS) + done,
        "long_then_call": b"".join(text(p) for p in LONG_PARTS)
            + text('\n```json\n{"tool_ca') + text('lls":[{"name":"read","arguments":{"filePath":"a"}}]}\n```') + done,
        "long_then_cut": b"".join(text(p) for p in LONG_PARTS),  # no response.completed
        "long_with_braces": b"".join(text(p) for p in LONG_PARTS) + text(' Use {"name": "x"} or { y } here.') + done,
        # a first-turn reply that writes the command as a code block instead of calling the shell (phase-0 T1)
        "code": text("```python\nimport sqlite3\nprint(1)\n```") + done,
    }
    own_step = frame("veniceai:tool_call_generation.in_progress", {"detected_tools": [{"type": "Recognized", "tool": "get_attachment"}]})
    static["own_tool_recovered"] = own_step + call  # one step with Venice's tool, then our format: left alone
    own_tool = (own_step * 4
                + text("I need file tools, but only media viewing is available here.") + done)
    static["own_tool"] = own_tool
    sequences = {  # the n-th request of a case gets the n-th reply (the last one repeats)
        "own_tool_then_call": [own_tool, call],
        "loop_then_call": [swallowed, call], "broken_then_call": [broken, call], "empty_then_text": [done, prose],
        "prose_then_call": [prose, call], "cut_then_loop_then_call": [static["cut"], swallowed, swallowed, call],
        "loop_then_cut_then_call": [swallowed, static["cut"], call], "prose_then_500": [prose, None],  # None = HTTP 500
        "code_then_call": [static["code"], call],
    }
    verdicts = {"judge-stopped": "stopped_early", "judge-finished": "finished", "judge-missing": "missing_result",
                "judge-quota": "stopped_early", "judge-slow": "stopped_early"}
    seen = {}  # case -> last user message text of every request
    chat_seen = []  # request bodies that reached the plain chat route
    judged = []  # judge request bodies

    class FakeVenice(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            case = body["modelId"]
            if self.path == "/api/inference/chat":  # the plain chat route: JSON lines, no closing event
                chat_seen.append(body)
                if case == "agentonly":
                    page = b'{"error":"modelNotFound"}'
                    self.send_response(400); self.send_header("Content-Length", str(len(page))); self.end_headers()
                    self.wfile.write(page)
                    return
                line = lambda kind, content: (json.dumps({"kind": kind, "content": content}) + "\n").encode()
                out = line("meta", "") + {
                    "ok": line("content", "PO") + line("content", "NG"),
                    "call": line("content", '{"tool_calls":[{"name":"read","arguments":{"filePath":"a"}}]}'),
                }.get(case, line("content", "half an ans"))
                self.send_response(200)
                # a body shorter than announced = a cut stream: curl exits non-zero
                self.send_header("Content-Length", str(len(out) + (40 if case == "cut" else 0)))
                self.end_headers()
                self.wfile.write(out)
                return
            if case.startswith("judge-"):
                judged.append(body)
                if case == "judge-quota":  # another request used the quota up meanwhile
                    vb.VENICE.quota_until = time.time() + 600
                if case == "judge-slow":  # answers after the judge time limit
                    time.sleep(0.6)
                if case == "judge-500":
                    self.send_response(500); self.send_header("Content-Length", "0"); self.end_headers(); return
                out = text('Sure! {"verdict":"%s","reason":"r"}' % verdicts[case]) + done
                try:
                    self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                    self.wfile.write(out)
                except OSError:  # curl gave up already
                    pass
                return
            seen.setdefault(case, []).append(body["messages"][-1]["content"][0]["text"])
            n = len(seen[case])
            if case == "stale" and n == 1:
                self.send_response(401); self.end_headers(); return
            if case.startswith("quota_"):  # the quota runs out while this request is answered
                vb.VENICE.quota_until = time.time() + 600
            seq = sequences.get(case)
            reply = seq[min(n, len(seq)) - 1] if seq else static.get(case.replace("quota_", ""))
            if seq and reply is None:
                self.send_response(500); self.send_header("Content-Length", "0"); self.end_headers(); return
            if case in ("challenge", "toolong"):
                page = b"<html>Just a moment...</html>" if case == "challenge" else b'{"error":"userPromptTooLong"}'
                self.send_response(403 if case == "challenge" else 413)
                if case == "challenge":
                    self.send_header("cf-mitigated", "challenge")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if case == "slow_long":  # the text is there long before the reply ends
                self.wfile.write(b"".join(text(p) for p in LONG_PARTS)); self.wfile.flush()
                time.sleep(0.6)
                self.wfile.write(text(" Done.") + done)
                return
            self.wfile.write(reply)

    fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeVenice)
    bridge = ThreadingHTTPServer(("127.0.0.1", 0), vb.OpenAIBridgeHandler)
    for srv in (fake, bridge):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    vb.OUTERFACE_BASE = "http://127.0.0.1:%d" % fake.server_address[1]
    vb.ROUTE = "agent"
    vb.VENICE.auth_headers = lambda force_refresh=False: {"Content-Type": "application/json"}
    vb.VENICE.quota_until = 0.0
    tools = [{"type": "function", "function": {"name": "read"}}, {"type": "function", "function": {"name": "write"}}]
    after_tool = [{"role": "user", "content": "run the tests"},
                  {"role": "assistant", "content": "", "tool_calls": [
                      {"id": "c1", "function": {"name": "read", "arguments": "{}"}}]},
                  {"role": "tool", "tool_call_id": "c1", "content": "file text"}]

    def ask(case, stream=False, with_tools=False, messages=None, tool_list=None):
        seen.pop(case, None)
        del judged[:]
        vb.VENICE.quota_until = 0.0
        body = {"model": case, "stream": stream, "messages": messages or [{"role": "user", "content": "x"}]}
        if with_tools:
            body["tools"] = tool_list or tools
        req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % bridge.server_address[1],
                                     json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def calls_of(out, stream):
        if stream:
            return ["read"] if '"tool_calls": [{' in out and '"name": "read"' in out else []
        msg = json.loads(out)["choices"][0]["message"]
        return [c["function"]["name"] for c in msg.get("tool_calls") or []]

    def content_of(out, stream):
        if stream:
            parts = []
            for line in out.splitlines():
                if not line.startswith("data: {"):
                    continue
                ch = json.loads(line[6:]).get("choices") or []  # the usage chunk has choices: []
                if ch:
                    parts.append(ch[0]["delta"].get("content") or "")
            return "".join(parts)
        return json.loads(out)["choices"][0]["message"]["content"]

    act = lambda reminder: "(act reminder) " + reminder

    # the fake swallows a native `read`: the retry names that tool

    NATIVE_READ = vb._steer_reminder("server_loop", {"unrecognized": ["read"]}, {"read": "read"})
    vb.JUDGE_ENABLED, vb.JUDGE_MODEL = True, "judge-finished"
    code, out = ask("ok")
    assert code == 200 and json.loads(out)["choices"][0]["message"]["content"] == "PONG"
    code, out = ask("stale")  # 401 once, then the retry with a fresh token answers
    assert code == 200 and len(seen["stale"]) == 2 and "PONG" in out
    # failing on every attempt: a 502 once the budget is used up, so OpenCode retries the turn by itself
    budget = {"cut": 2, "failed": 2, "empty": 3, "swallowed": 3}  # a broken stream is resent once
    for case, want in budget.items():
        for stream in (False, True):
            assert ask(case, stream=stream, with_tools=True)[0] == 502, (case, stream)
            assert len(seen[case]) == want, (case, stream, len(seen[case]))
            if case in ("cut", "failed"):  # resent unchanged, no reminder
                assert set(seen[case]) == {vb.TOOL_REMINDER}, case
    assert seen["swallowed"] == [vb.TOOL_REMINDER] + [act(NATIVE_READ)] * 2
    # server loop, then the call on the retry, which carries the native-call reminder (stream and not)
    for stream in (False, True):
        code, out = ask("loop_then_call", stream=stream, with_tools=True)
        assert code == 200 and calls_of(out, stream) == ["read"], (stream, out)
        assert seen["loop_then_call"] == [vb.TOOL_REMINDER, act(NATIVE_READ)], seen["loop_then_call"]
    # a broken stream after a reminder retry is resent with the same reminder
    code, out = ask("cut_then_loop_then_call", with_tools=True)
    assert code == 502 and seen["cut_then_loop_then_call"] == [vb.TOOL_REMINDER, vb.TOOL_REMINDER,
                                                                act(NATIVE_READ)], seen
    # a broken stream after a reminder retry is resent with that reminder, and the call comes through
    code, out = ask("loop_then_cut_then_call", with_tools=True)
    assert code == 200 and seen["loop_then_cut_then_call"] == [vb.TOOL_REMINDER] + [act(NATIVE_READ)] * 2
    # unreadable call, then a valid one
    code, out = ask("broken_then_call", with_tools=True)
    assert calls_of(out, False) == ["read"] and seen["broken_then_call"][1] == act(vb.BROKEN_ENVELOPE_REMINDER)
    # unreadable on every attempt: the readable text, never the raw envelope
    for stream in (False, True):
        code, out = ask("broken", stream=stream, with_tools=True)
        assert code == 200 and content_of(out, stream) == "I will edit it." and len(seen["broken"]) == 3, out
    code, out = ask("broken_bare", with_tools=True)  # nothing readable at all: the marker, never the envelope
    assert code == 200 and "could not be read" in content_of(out, False) and len(seen["broken_bare"]) == 3, out
    # empty, then text
    code, out = ask("empty_then_text", with_tools=True)
    assert code == 200 and content_of(out, False) == "I will now run the tests."
    assert seen["empty_then_text"] == [vb.TOOL_REMINDER, act(vb.EMPTY_RETRY_REMINDER)]

    # judge: the reply ends the turn right after a tool result
    vb.JUDGE_MODEL = "judge-stopped"
    for stream in (False, True):
        code, out = ask("prose_then_call", stream=stream, with_tools=True, messages=after_tool)
        assert code == 200 and calls_of(out, stream) == ["read"], (stream, out)
        assert seen["prose_then_call"] == [vb.TOOL_REMINDER, act(vb.CONTINUE_REMINDER)] and len(judged) == 1
        jtext = judged[0]["messages"][0]["content"][0]["text"]
        assert "run the tests" in jtext and "file text" in jtext and "I will now run the tests." in jtext
        assert judged[0]["reasoningMode"] == "disabled" and judged[0]["tools"] == []
    # stopped_early, but the retry is prose again: the first reply stays, no second judge call
    code, out = ask("prose", with_tools=True, messages=after_tool)
    assert code == 200 and len(seen["prose"]) == 2 and len(judged) == 1
    # the judge's retry fails upstream: the first reply stays
    code, out = ask("prose_then_500", with_tools=True, messages=after_tool)
    assert code == 200 and content_of(out, False) == "I will now run the tests." and len(seen["prose_then_500"]) == 2
    # a retry that is no better ends the judging even with judge budget left
    vb.MAX_JUDGE_RETRIES = 2
    code, out = ask("prose", with_tools=True, messages=after_tool)
    assert code == 200 and len(seen["prose"]) == 2 and len(judged) == 1
    vb.MAX_JUDGE_RETRIES = 1
    # tool results earlier in the conversation, but the user spoke last: not judged
    code, out = ask("prose", with_tools=True, messages=after_tool + [
        {"role": "assistant", "content": "done"}, {"role": "user", "content": "thanks, what next?"}])
    assert code == 200 and len(seen["prose"]) == 1 and judged == []
    # first turn, a code block instead of a call while a shell tool is declared: the judge is asked
    with_bash = tools + [{"type": "function", "function": {"name": "bash"}}]
    for stream in (False, True):
        code, out = ask("code_then_call", stream=stream, with_tools=True, tool_list=with_bash)
        assert code == 200 and calls_of(out, stream) == ["read"], (stream, out)
        assert seen["code_then_call"] == [vb.TOOL_REMINDER, act(vb.CONTINUE_REMINDER)] and len(judged) == 1
    vb.JUDGE_MODEL = "judge-finished"  # the judge says the code block is the answer: it is kept, no retry
    code, out = ask("code", with_tools=True, tool_list=with_bash)
    assert code == 200 and "import sqlite3" in content_of(out, False) and len(seen["code"]) == 1 and len(judged) == 1
    vb.JUDGE_MODEL = "judge-stopped"
    code, out = ask("code", with_tools=True)  # no shell tool declared: a code block is just an answer, not judged
    assert code == 200 and len(seen["code"]) == 1 and judged == []
    # missing_result: a second text answer may replace the first
    vb.JUDGE_MODEL = "judge-missing"
    code, out = ask("prose", with_tools=True, messages=after_tool)
    assert code == 200 and seen["prose"][1] == act(vb.RESULT_REMINDER) and len(judged) == 1
    for model in ("judge-finished", "judge-500"):  # finished, or the judge itself fails: the reply is kept
        vb.JUDGE_MODEL = model
        for stream in (False, True):
            code, out = ask("prose", stream=stream, with_tools=True, messages=after_tool)
            assert code == 200 and content_of(out, stream) == "I will now run the tests.", (model, out)
            assert len(seen["prose"]) == 1 and len(judged) == 1, (model, len(seen["prose"]), len(judged))
    vb.JUDGE_MODEL, vb.JUDGE_TIMEOUT = "judge-slow", 0.2  # the judge times out: the reply is kept
    code, out = ask("prose", with_tools=True, messages=after_tool)
    assert code == 200 and len(seen["prose"]) == 1 and len(judged) == 1
    vb.JUDGE_TIMEOUT = 30
    vb.JUDGE_MODEL = "judge-quota"  # a verdict, but the quota ran out meanwhile: no retry
    code, out = ask("prose", with_tools=True, messages=after_tool)
    assert code == 200 and len(seen["prose"]) == 1 and len(judged) == 1
    vb.JUDGE_MODEL = "judge-stopped"
    vb.JUDGE_ENABLED = False  # VENICE_BRIDGE_JUDGE=0
    code, out = ask("prose", with_tools=True, messages=after_tool)
    assert code == 200 and len(seen["prose"]) == 1 and judged == []
    vb.JUDGE_ENABLED = True
    code, out = ask("prose", with_tools=True)  # a first turn is never judged
    assert code == 200 and len(seen["prose"]) == 1 and judged == []
    # the quota runs out during the first attempt: no retry, no judge
    code, out = ask("quota_swallowed", with_tools=True)
    assert code == 502 and len(seen["quota_swallowed"]) == 1
    code, out = ask("quota_prose", with_tools=True, messages=after_tool)
    assert code == 200 and len(seen["quota_prose"]) == 1 and judged == []
    vb.VENICE.quota_until = 0.0

    code, out = ask("call", with_tools=True)
    assert json.loads(out)["choices"][0]["finish_reason"] == "tool_calls" and len(seen["call"]) == 1
    code, out = ask("recovered", with_tools=True)
    assert json.loads(out)["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "read"
    assert len(seen["recovered"]) == 1
    code, out = ask("challenge", with_tools=True)  # Cloudflare page: a 502, and never a quota lock
    assert code == 502 and "Cloudflare" in out and vb.VENICE.quota_until == 0.0, (code, out)
    code, out = ask("toolong", with_tools=True)
    assert code == 400 and "context_length_exceeded" in out and "userPromptTooLong" in out, (code, out)

    code, out = ask("call", stream=True, with_tools=True)
    assert '"finish_reason": "tool_calls"' in out and out.rstrip().endswith("data: [DONE]")
    code, out = ask("ok", stream=True)
    assert '"content": "PONG"' in out and '"finish_reason": "stop"' in out
    for case in ("cut", "failed", "swallowed"):  # streamed without tools: an error event, no normal ending, no retry
        code, out = ask(case, stream=True)
        assert code == 200 and '"error"' in out and "finish_reason\": \"" not in out and "[DONE]" not in out, (case, out)
        assert len(seen[case]) == 1
    # ---- Venice runs one of its own tools: cut at once, asked again with the tool it should use ----
    for stream in (False, True):
        code, out = ask("own_tool_then_call", stream=stream, with_tools=True)
        assert code == 200 and calls_of(out, stream) == ["read"], out[:200]
        first_last, second_last = seen["own_tool_then_call"]
        assert "get_attachment" not in first_last and "`get_attachment`" in second_last and "use `read`" in second_last
    assert ask("own_tool", with_tools=True)[0] == 502 and len(seen["own_tool"]) == 3  # every attempt: budget used up
    code, out = ask("own_tool_recovered", with_tools=True)  # the model recovered inside the response
    assert calls_of(out, False) == ["read"] and len(seen["own_tool_recovered"]) == 1
    code, out = ask("own_tool")  # without tools nothing is cut: the model's text is the answer
    assert code == 200 and "media viewing" in out

    # ---- the plain chat route: instructions folded into the first user message, no agent loop ----
    vb.ROUTE = "chat"
    try:
        code, out = ask("ok")
        assert code == 200 and json.loads(out)["choices"][0]["message"]["content"] == "PONG"
        body = chat_seen[-1]
        assert body["systemPrompt"] == vb.CHAT_SYSTEM_PROMPT and [m["role"] for m in body["prompt"]] == ["user"]
        assert body["prompt"][0]["content"].startswith("[SESSION INSTRUCTIONS]\n") and "[USER REQUEST]\nx" in body["prompt"][0]["content"]
        for stream in (False, True):
            code, out = ask("call", stream=stream, with_tools=True, messages=after_tool)
            assert code == 200 and calls_of(out, stream) == ["read"], out[:200]
        body = chat_seen[-1]  # user(instructions+request) / assistant(call) / user(tool result + reminder)
        assert [m["role"] for m in body["prompt"]] == ["user", "assistant", "user"]
        assert "[Tool Result c1]" in body["prompt"][2]["content"] and body["prompt"][2]["content"].endswith(vb.TOOL_REMINDER)
        assert "- read(" in body["prompt"][0]["content"]  # the tool list travels with the instructions
        del chat_seen[:]
        assert ask("cut", with_tools=True)[0] == 502 and len(chat_seen) == 2  # a cut reply is no answer: retried once
        # auto: a model the chat route does not know is sent to the agent route, and remembered
        vb.ROUTE = "auto"
        vb.AGENT_ONLY_MODELS.clear()
        del chat_seen[:]
        code, out = ask("agentonly")
        assert code == 200 and "PONG" in out and len(chat_seen) == 1 and vb.AGENT_ONLY_MODELS == {"agentonly"}
        code, out = ask("agentonly")
        assert code == 200 and len(chat_seen) == 1  # the second request goes straight to the agent route
    finally:
        vb.ROUTE = "agent"
        vb.AGENT_ONLY_MODELS.clear()

    # ---- the stream gate: long prose is shown while it is written, a call never is ----
    def said(out):
        parts = []
        for l in out.splitlines():
            if not l.startswith('data: {"id"'):
                continue
            ch = json.loads(l[6:]).get("choices") or []  # usage chunk has choices: []
            if ch:
                parts.append(ch[0]["delta"].get("content") or "")
        return "".join(parts)

    code, out = ask("long_prose", stream=True, with_tools=True, messages=after_tool)
    assert code == 200 and said(out) == LONG and '"finish_reason": "stop"' in out and out.count('"content"') > 2
    assert judged == [] and len(seen["long_prose"]) == 1  # shown already: not judged, not retried
    code, out = ask("long_then_call", stream=True, with_tools=True)
    assert said(out).strip() == LONG.strip() and "tool_ca" not in said(out) and "```" not in said(out), said(out)[-80:]
    assert calls_of(out, True) == ["read"] and '"finish_reason": "tool_calls"' in out
    code, out = ask("long_with_braces", stream=True, with_tools=True)
    assert said(out) == LONG + ' Use {"name": "x"} or { y } here.'
    code, out = ask("long_then_cut", stream=True, with_tools=True)  # shown, then cut: an error, no normal ending
    assert code == 200 and said(out) and '"error"' in out and "finish_reason\": \"" not in out and "[DONE]" not in out
    assert ask("long_prose", with_tools=True)[0] == 200  # not streaming: unchanged
    code, out = ask("prose", stream=True, with_tools=True)  # a short reply stays held (and judgeable)
    assert said(out) == "I will now run the tests." and out.count('"content"') == 1

    seen.pop("slow_long", None)
    body = {"model": "slow_long", "stream": True, "tools": tools, "messages": [{"role": "user", "content": "x"}]}
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % bridge.server_address[1],
                                 json.dumps(body).encode(), {"Content-Type": "application/json"})
    t0, first = time.time(), None
    with urllib.request.urlopen(req, timeout=30) as r:
        for line in r:
            if first is None and b'"content"' in line:
                first = time.time() - t0
    assert first is not None and first < 0.45 and time.time() - t0 > 0.55, (first, time.time() - t0)

    gate_out = []
    gate = vb._LiveGate(lambda: gate_out.append("<start>"), gate_out.append)
    for ch in "x" * 250 + ' see {"a": 1} then\n```json\n{ "tool_calls": [1]}':
        gate.feed(ch)
    shown = "".join(gate_out)
    assert shown.startswith("<start>") and shown.endswith('see {"a": 1} then\n') and gate.rest().startswith("```json")
    gate.reset()
    for ch in '{"tool_calls":[{"name":"read","arguments":{}}]}' + "y" * 300:
        gate.feed(ch)
    assert not gate.live  # a reply that starts as a call is never shown
    # usage is estimated (Venice gives none) so Claude Code / Codex counters are not 0
    code, out = ask("ok")
    u = json.loads(out)["usage"]
    assert u["prompt_tokens"] > 0 and u["completion_tokens"] >= 1 and u["total_tokens"] == u["prompt_tokens"] + u["completion_tokens"]
    code, out = ask("ok", stream=True)
    usage_chunks = [json.loads(l[6:]) for l in out.splitlines() if l.startswith("data: {") and '"usage"' in l]
    assert usage_chunks and usage_chunks[-1]["usage"]["completion_tokens"] >= 1 and usage_chunks[-1]["choices"] == []
    code, out = ask("call", stream=True, with_tools=True, messages=after_tool)
    assert any('"usage"' in l for l in out.splitlines())  # tool-call stream also carries usage
    print("end to end ok")


def session_checks() -> None:
    """Attestation expiry and single flight, Clerk session choice, rotated __client persistence.
    Fakes only: no network, obviously fake cookie strings, state file under STATE_DIR."""
    # expiresAt in seconds, milliseconds, ISO or garbage: never cached past now+300
    now = 1_800_000_000.0
    iso = datetime.datetime.fromtimestamp(now + 120, datetime.timezone.utc).isoformat()
    for raw, want in ((int(now) + 120, now + 120), (int((now + 120) * 1000), now + 120), (str(int(now) + 120), now + 120),
                      (iso, now + 120), (iso.replace("+00:00", "Z"), now + 120), (iso.replace("+00:00", ""), now + 120),
                      (now + 99999, now + 300), (float("inf"), now + 240), (float("nan"), now + 240), ("garbage", now + 240),
                      ("", now + 240), (None, now + 240), (True, now + 240), ({}, now + 240), (now - 5, now + 240)):
        got = vb._attest_expiry(raw, now)
        assert abs(got - want) < 1e-6 and got <= now + 300, repr(raw)

    real_http, real_get_session = vb.cffi_requests, vb.get_session
    calls = []
    jwt = "x.%s.y" % base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 60}).encode()).decode().rstrip("=")

    class Resp:
        def __init__(self, status, body, cookies=()):
            self.status_code, self._body = status, body
            self.headers = SimpleNamespace(get_list=lambda name: list(cookies))

        def json(self):
            return self._body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("http %d" % self.status_code)

    def clerk(refuse=(), rotate_to=None, sessions=None):
        sessions = sessions or [{"id": "s1", "status": "active", "user": {"id": "u1"}}]

        def get(url, headers=None, **kw):
            cookie = headers["Cookie"].split("=", 1)[1]
            calls.append(("client", cookie))
            if cookie in refuse:
                return Resp(401, {})
            rot = ["__client=%s; Path=/; HttpOnly" % rotate_to] if rotate_to else []
            return Resp(200, {"response": {"last_active_session_id": sessions[0]["id"], "sessions": sessions}}, rot)

        def post(url, headers=None, **kw):
            cookie = headers["Cookie"].split("=", 1)[1]
            calls.append(("tokens", cookie))
            return Resp(401, {}) if cookie in refuse else Resp(200, {"jwt": jwt})
        return SimpleNamespace(get=get, post=post)

    def sb(ts, value):
        vb.get_session = lambda domain: {"timestamp": ts, "cookies": [{"key": "__client", "value": value}]}

    try:
        # session choice: an ended last-active session falls to an active one; none active is an auth error
        c = vb.VeniceClient()
        vb.cffi_requests = clerk(sessions=[{"id": "s1", "status": "ended"}, {"id": "s2", "status": "active", "user": {"id": "u1"}}])
        assert c._ensure_session_id("fake-client-0") == "s2" and c._user_id == "u1"
        c = vb.VeniceClient()
        vb.cffi_requests = clerk(sessions=[{"id": "s1", "status": "ended"}, {"id": "s2", "status": "revoked"}])
        try:
            c._ensure_session_id("fake-client-0")
            raise AssertionError("no active session must raise")
        except vb.VeniceAuthError:
            pass
        # within one sync the account is pinned: another user's last-active session is not taken
        c = vb.VeniceClient()
        c._user_id = "u1"
        vb.cffi_requests = clerk(sessions=[{"id": "s3", "status": "active", "user": {"id": "u2"}},
                                           {"id": "s2", "status": "active", "user": {"id": "u1"}}])
        assert c._ensure_session_id("fake-client-0") == "s2"

        # rotation is saved 0600, atomically, keyed by the sync timestamp; the mint uses the rotated value
        ts = 1763490000123
        sb(ts, "fake-client-1")
        vb.cffi_requests = clerk(rotate_to="fake-client-2")
        c = vb.VeniceClient()
        assert c.get_jwt() == jwt and calls[-1] == ("tokens", "fake-client-2")
        st = os.stat(vb.CLIENT_STATE_FILE)
        assert st.st_mode & 0o777 == 0o600 and os.listdir(STATE_DIR) == ["venice-bridge-client.json"]
        saved = json.load(open(vb.CLIENT_STATE_FILE, encoding="utf-8"))
        assert saved["session_ts"] == ts and saved["client"] == "fake-client-2" and saved["saved_at"] > 0
        # after a restart the same sync generation prefers the saved value
        assert vb.VeniceClient()._load_clerk_client() == "fake-client-2"
        # a newer sync wins over the file
        sb(ts + 1000, "fake-client-3")
        assert vb.VeniceClient()._load_clerk_client() == "fake-client-3"
        # a sync without a timestamp never uses or writes the file
        sb(0, "fake-client-4")
        assert vb.VeniceClient()._load_clerk_client() == "fake-client-4"
        # deletion cookies are not stored as the cookie
        sb(ts, "fake-client-1")
        c = vb.VeniceClient()
        assert c._load_clerk_client() == "fake-client-2"
        before = open(vb.CLIENT_STATE_FILE, encoding="utf-8").read()
        c._update_rotated_client(SimpleNamespace(get_list=lambda name: [
            "__client=; Path=/; Max-Age=0", "__client=fake-gone; Max-Age=0", "__client=fake-gone2; max-age=-1",
            "__client=fake-old; Expires=Thu, 01 Jan 1970 00:00:00 GMT", "__client_uat=0; Path=/"]))
        assert c._clerk_client == "fake-client-2" and open(vb.CLIENT_STATE_FILE, encoding="utf-8").read() == before
        # the saved value is refused: the file is dropped, one retry with the session source value works
        vb.cffi_requests = clerk(refuse={"fake-client-2"})
        del calls[:]
        assert c.get_jwt() == jwt and c._clerk_client == "fake-client-1" and not os.path.exists(vb.CLIENT_STATE_FILE)
        assert [k for k, _ in calls] == ["client", "client", "tokens"]
        # the same fallback when the token mint is the call that refuses (session id already known)
        vb._write_saved_client(ts, "fake-client-2")
        c = vb.VeniceClient()
        c._load_clerk_client()
        c._session_id = "s1"
        del calls[:]
        assert c.get_jwt() == jwt and c._clerk_client == "fake-client-1" and not os.path.exists(vb.CLIENT_STATE_FILE)
        assert [k for k, _ in calls] == ["tokens", "client", "tokens"]
        # both values refused: exactly one retry, then the auth error
        vb._write_saved_client(ts, "fake-client-2")
        vb.cffi_requests = clerk(refuse={"fake-client-1", "fake-client-2"})
        del calls[:]
        try:
            vb.VeniceClient().get_jwt()
            raise AssertionError("refused twice must raise")
        except vb.VeniceAuthError:
            pass
        assert [k for k, _ in calls] == ["client", "client"] and not os.path.exists(vb.CLIENT_STATE_FILE)

        # attestation single flight: two concurrent (even forced) callers cause one mint
        mints = []
        gate = threading.Barrier(2)

        def attest_post(url, **kw):
            mints.append(1)
            time.sleep(0.3)
            return Resp(200, {"token": "fake-attest", "expiresAt": int(time.time()) + 200})
        vb.cffi_requests = SimpleNamespace(post=attest_post)
        c, got = vb.VeniceClient(), []

        def worker():
            gate.wait()
            got.append(c.get_attestation("fake-jwt", force=True))
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(mints) == 1 and got == ["fake-attest", "fake-attest"], (len(mints), len(got))
        assert c._attest_exp <= time.time() + 300
        assert c.get_attestation("fake-jwt") == "fake-attest" and len(mints) == 1  # cached
    finally:
        vb.cffi_requests, vb.get_session = real_http, real_get_session
    print("session ok")


def cookie_file_checks() -> None:
    """The login can come from a text file holding the `__client` cookie; no session daemon needed."""
    import tempfile
    old_file, old_daemon = vb.COOKIE_FILE, vb._daemon_session
    tmp = tempfile.mkdtemp(prefix="vb-cookie-")
    try:
        vb.COOKIE_FILE = os.path.join(tmp, "client-cookie.txt")
        vb._daemon_session = None
        assert vb.get_session("venice.ai") is None  # no file, no daemon
        vb._daemon_session = lambda domain: {"timestamp": 5, "tokens": {"clerk_client": "fake-daemon"}}
        assert vb.get_session("venice.ai")["tokens"]["clerk_client"] == "fake-daemon"
        for text in ("fake-client-1\n", "__client=fake-client-1; Path=/; Secure\n"):  # bare value or a pasted cookie
            open(vb.COOKIE_FILE, "w").write(text)
            got = vb.get_session("venice.ai")
            assert got["tokens"]["clerk_client"] == "fake-client-1" and got["timestamp"] > 1e12  # the file wins
        open(vb.COOKIE_FILE, "w").write("  \n")
        assert vb.get_session("venice.ai")["tokens"]["clerk_client"] == "fake-daemon"  # an empty file is no login
    finally:
        vb.COOKIE_FILE, vb._daemon_session = old_file, old_daemon
        for f in os.listdir(tmp):
            os.unlink(os.path.join(tmp, f))
        os.rmdir(tmp)
    print("cookie file ok")


def local_backend_checks() -> None:
    """Local OpenAI backends: chosen by the model id's prefix, passed through unchanged and live,
    listed next to Venice's models; a backend that is down breaks neither the list nor Venice,
    and a prompt for a local model never goes to Venice."""
    import socket
    import urllib.error
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    got = []  # (path, authorization, body) of every chat request the fake backend saw
    chunk = lambda delta: ("data: %s\n\n" % json.dumps({"choices": [{"index": 0, "delta": delta}]})).encode()
    CALL = [{"index": 0, "id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]

    class FakeLocal(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            page = json.dumps({"object": "list", "data": [{"id": "qwen3:8b"}, {"id": "org/model-x"}]}).encode()
            self.send_response(200 if self.path == "/v1/models" else 404)
            self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            got.append((self.path, self.headers.get("Authorization"), body))
            if body["model"] == "missing":
                page = b'{"error":{"message":"model not found"}}'
                self.send_response(404); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page)
                return
            if not body.get("stream"):
                page = json.dumps({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": CALL}}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page)
                return
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            self.wfile.write(chunk({"content": "PO"})); self.wfile.flush()
            time.sleep(0.6)  # the first piece must reach the client before the reply ends
            self.wfile.write(chunk({"tool_calls": CALL}) + b"data: [DONE]\n\n")

    fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeLocal)
    bridge = ThreadingHTTPServer(("127.0.0.1", 0), vb.OpenAIBridgeHandler)
    for srv in (fake, bridge):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); dead = sock.getsockname()[1]; sock.close()
    base = "http://127.0.0.1:%d" % bridge.server_address[1]
    old = vb.BACKENDS, vb.BACKENDS_FILE, vb.VENICE.list_models, vb._open_upstream, vb.VENICE.quota_until
    venice_calls = []

    def no_venice(payload, max_time=None):
        venice_calls.append(payload["modelId"])
        return None, (502, {"error": {"message": "fake venice", "type": "test"}})

    def post(model, stream=False):
        req = urllib.request.Request(base + "/v1/chat/completions", json.dumps({
            "model": model, "stream": stream, "tools": [{"type": "function", "function": {"name": "read"}}],
            "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "x"}]}).encode(),
            {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    try:
        vb.BACKENDS = [{"name": "ollama", "base_url": "http://127.0.0.1:%d/v1" % fake.server_address[1], "api_key": "k"},
                       {"name": "lmstudio", "base_url": "http://127.0.0.1:%d/v1" % dead}]
        vb._open_upstream = no_venice
        # the prefix decides, with or without the provider name in front; anything else is Venice's
        assert vb._resolve_backend("venice-bridge/ollama/qwen3:8b") == (vb.BACKENDS[0], "qwen3:8b")
        assert vb._resolve_backend("ollama/org/model-x")[1] == "org/model-x"
        assert vb._resolve_backend("deepseek-v4-1-flash") == (None, "deepseek-v4-1-flash")
        assert vb._resolve_backend("venice-bridge/ollamax/m") == (None, "ollamax/m")

        # the list: Venice's models and the reachable backend's, prefixed; the dead backend is skipped
        vb.VENICE.list_models = lambda: [{"id": "venice-m", "object": "model"}]
        ids = lambda: [m["id"] for m in json.load(urllib.request.urlopen(base + "/v1/models", timeout=10))["data"]]
        assert ids() == ["venice-auto", "venice-m", "ollama/qwen3:8b", "ollama/org/model-x"]
        def venice_down():
            raise RuntimeError("logged out")
        vb.VENICE.list_models = venice_down  # Venice down: the virtual + local models are still listed
        assert ids() == ["venice-auto", "ollama/qwen3:8b", "ollama/org/model-x"]

        # passthrough: the request as the client sent it (tools and system message included), only the model id changed
        vb.VENICE.quota_until = time.time() + 600  # Venice's quota lock does not apply to a local model
        code, out = post("venice-bridge/ollama/qwen3:8b")
        path, auth, body = got[-1]
        assert code == 200 and json.loads(out)["choices"][0]["message"]["tool_calls"] == CALL
        assert path == "/v1/chat/completions" and auth == "Bearer k" and body["model"] == "qwen3:8b"
        assert body["tools"][0]["function"]["name"] == "read" and body["messages"][0] == {"role": "system", "content": "S"}
        assert "[TOOLS]" not in json.dumps(body)  # no tool emulation
        vb.VENICE.quota_until = 0.0
        # streamed: relayed while it is written, native tool_calls untouched
        req = urllib.request.Request(base + "/v1/chat/completions", json.dumps({
            "model": "ollama/qwen3:8b", "stream": True, "messages": [{"role": "user", "content": "x"}]}).encode(),
            {"Content-Type": "application/json"})
        t0, first, lines = time.time(), None, []
        with urllib.request.urlopen(req, timeout=30) as r:
            assert r.headers["Content-Type"] == "text/event-stream"
            for line in r:
                first = first if first is not None else time.time() - t0
                lines.append(line)
        assert first < 0.45 and time.time() - t0 > 0.55, (first, time.time() - t0)
        assert b"".join(lines) == chunk({"content": "PO"}) + chunk({"tool_calls": CALL}) + b"data: [DONE]\n\n"
        # the backend's own error reaches the client as it is
        code, out = post("ollama/missing")
        assert code == 404 and "model not found" in out
        # a backend that is down: a 502 that names it, and the prompt never goes to Venice
        code, out = post("lmstudio/some-model")
        assert code == 502 and "lmstudio" in out and "local_backend_error" in out
        assert venice_calls == []
        # every other model still takes the Venice path
        assert post("deepseek-v4-1-flash")[0] == 502 and venice_calls == ["deepseek-v4-1-flash"]

        # the backends file: a list or {"backends": [...]}, an empty list = none, no file = the defaults
        vb.BACKENDS, vb.BACKENDS_FILE = None, os.path.join(STATE_DIR, "backends.json")
        assert vb._backends() == vb.DEFAULT_BACKENDS
        open(vb.BACKENDS_FILE, "w").write('{"backends":[{"name":"mlx","base_url":"http://127.0.0.1:8080/v1/"},{"name":"bad"}]}')
        assert vb._backends() == [{"name": "mlx", "base_url": "http://127.0.0.1:8080/v1", "api_key": None}]
        open(vb.BACKENDS_FILE, "w").write("[]")
        assert vb._backends() == [] and vb._resolve_backend("ollama/x") == (None, "ollama/x")
        open(vb.BACKENDS_FILE, "w").write("{not json")
        assert vb._backends() == vb.DEFAULT_BACKENDS
    finally:
        vb.BACKENDS, vb.BACKENDS_FILE, vb.VENICE.list_models, vb._open_upstream, vb.VENICE.quota_until = old
        vb.BACKENDS = []
    print("local backends ok")


def reasoning_auto_checks() -> None:
    # Fix 2: reasoning must stay OFF on a tool turn, even with reasoning_effort high (400 trap otherwise).
    tool = [{"type": "function", "function": {"name": "read"}}]
    p, has = vb.openai_to_venice_chat({"model": "m", "reasoning_effort": "high",
                                       "messages": [{"role": "user", "content": "hi"}], "tools": tool})
    assert has is True and "_think" not in p and p["reasoningMode"] == "disabled"
    chat = vb._to_chat_payload(p)
    assert chat["reasoning"] is False and not any(t["content"].startswith("/think") for t in chat["prompt"])
    # no tools + effort high still reasons
    p2, _ = vb.openai_to_venice_chat({"model": "m", "reasoning_effort": "high", "messages": [{"role": "user", "content": "hi"}]})
    assert p2.get("_think") is True and vb._to_chat_payload(p2)["reasoning"] is True

    # Fix 3: venice-auto resolves even behind the OpenCode `venice-bridge/` provider prefix.
    old_q = vb.VENICE.quota_until
    vb.VENICE.quota_until = 0.0
    try:
        assert vb._resolve_auto("venice-bridge/venice-auto") == "deepseek-v4-1-flash-uncensored"
        assert vb._resolve_auto("venice-bridge/venice-auto:think") == "deepseek-v4-1-flash-uncensored:think"
    finally:
        vb.VENICE.quota_until = old_q
    print("reasoning + venice-auto prefix ok")


def nested_arguments_unwrap_checks():
    props = {"command": {"type": "string"}, "timeout": {"type": "integer"}}
    # 1. Double-nested {"arguments": {"command": "..."}}
    out = vb._coerce_arguments(vb._fit_arguments({"arguments": {"command": "ls -la"}}, props), props)
    assert out == {"command": "ls -la"}, out
    # 2. Nested with redundant "name" {"name": "Bash", "arguments": {"command": "pwd"}}
    out = vb._coerce_arguments(vb._fit_arguments({"name": "Bash", "arguments": {"command": "pwd"}}, props), props)
    assert out == {"command": "pwd"}, out
    # 3. JSON-encoded string inside nested "arguments"
    out = vb._coerce_arguments(vb._fit_arguments({"arguments": '{"cmd": "echo hi"}'}, props), props)
    assert out == {"command": "echo hi"}, out
    # 4. Anthropic-style {"input": {"command": "..."}} when "input" is not in schema
    out = vb._coerce_arguments(vb._fit_arguments({"input": {"command": "id"}}, props), props)
    assert out == {"command": "id"}, out
    # 5. Tool that genuinely has an "input" property keeps it intact
    props_with_input = {"input": {"type": "object"}}
    out = vb._coerce_arguments(vb._fit_arguments({"input": {"a": 1}}, props_with_input), props_with_input)
    assert out == {"input": {"a": 1}}, out
    print("nested arguments unwrap ok")


def bare_string_content_checks():
    # P0-3: a bare-string call arrives as {"content": raw}; with one required string param, assign it.
    props = {"command": {"type": "string"}}
    out = vb._coerce_arguments(vb._fit_arguments({"content": "ls -la"}, props, ["command"]), props)
    assert out == {"command": "ls -la"}, out
    # untyped required param also counts as a string slot
    out = vb._fit_arguments({"content": "x"}, {"q": {}}, ["q"])
    assert out == {"q": "x"}, out
    # NEGATIVE: two required params -> ambiguous, leave untouched
    out = vb._fit_arguments({"content": "x"}, {"a": {"type": "string"}, "b": {"type": "string"}}, ["a", "b"])
    assert out == {"content": "x"}, out
    # NEGATIVE: the tool really has a `content` property -> keep it
    out = vb._fit_arguments({"content": "x"}, {"content": {"type": "string"}}, ["content"])
    assert out == {"content": "x"}, out
    # NEGATIVE: sole required param is not a string -> leave untouched
    out = vb._fit_arguments({"content": "5"}, {"n": {"type": "integer"}}, ["n"])
    assert out == {"content": "5"}, out
    # NEGATIVE: no required list -> leave untouched
    out = vb._fit_arguments({"content": "x"}, {"command": {"type": "string"}}, [])
    assert out == {"content": "x"}, out
    # a normal, already-correct call is unchanged
    out = vb._fit_arguments({"command": "ls"}, props, ["command"])
    assert out == {"command": "ls"}, out
    print("bare string content ok")
    # phase-0c: qwen-3-8-27b called the shell as `bash_tool`; it must reach the declared bash / shell tool
    assert vb._route_call("bash_tool", {"command": "ls"}, {"bash": "bash"}) == ("bash", {"command": "ls"})
    assert vb._route_call("bash_tool", {"command": "ls"}, {"shell": "shell"}) == ("shell", {"command": "ls"})
    print("bash_tool alias ok")


if __name__ == "__main__":
    cookie_file_checks()
    try:
        session_checks()
        main()
        end_to_end()
        local_backend_checks()
        reasoning_auto_checks()
        nested_arguments_unwrap_checks()
        bare_string_content_checks()
    finally:
        shutil.rmtree(STATE_DIR, ignore_errors=True)
