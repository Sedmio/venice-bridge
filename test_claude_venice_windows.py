#!/usr/bin/env python3
"""Windows tests for claude-venice.cmd / claude-venice.ps1 (same cases as test_claude_venice.py)."""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.join(HERE, "claude-venice.cmd")
PY = sys.executable


def make_fake_bin(tmpdir: str) -> str:
    bindir = os.path.join(tmpdir, "bin")
    os.makedirs(bindir, exist_ok=True)
    # stub curl: logs argv and the contents of any -H @file, answers a healthy bridge/adapter
    with open(os.path.join(bindir, "curl_stub.py"), "w", encoding="utf-8") as f:
        f.write(
            "import os, sys\n"
            "d = os.path.dirname(os.path.abspath(__file__))\n"
            "open(os.path.join(d, 'curl_argv.log'), 'a', encoding='utf-8').write(' '.join(sys.argv[1:]) + '\\n')\n"
            "for a in sys.argv[1:]:\n"
            "    if a.startswith('@'):\n"
            "        open(os.path.join(d, 'curl_hdrfiles.log'), 'a', encoding='utf-8').write(open(a[1:], encoding='utf-8').read())\n"
            "print('{\"ok\": true, \"venice_session\": \"active\", \"chat_quota\": {\"remaining\": 5000}, \"data\": [{\"id\": \"m1\"}]}')\n"
        )
    with open(os.path.join(bindir, "curl.cmd"), "w") as f:
        f.write(f'@"{PY}" "%~dp0curl_stub.py" %*\n')
    # stub claude: dumps the env it got + argv as JSON
    with open(os.path.join(bindir, "claude_stub.py"), "w", encoding="utf-8") as f:
        f.write(
            "import json, os, sys\n"
            "print(json.dumps({'model': os.environ.get('ANTHROPIC_MODEL'),"
            " 'base_url': os.environ.get('ANTHROPIC_BASE_URL'),"
            " 'has_token': bool(os.environ.get('ANTHROPIC_AUTH_TOKEN')),"
            " 'has_api_key': bool(os.environ.get('ANTHROPIC_API_KEY')),"
            " 'config_dir': os.environ.get('CLAUDE_CONFIG_DIR'),"
            " 'argv': sys.argv[1:]}))\n"
        )
    with open(os.path.join(bindir, "claude.cmd"), "w") as f:
        f.write(f'@"{PY}" "%~dp0claude_stub.py" %*\n')
    return bindir


def base_env(bindir: str, home: str, extra: dict = None) -> dict:
    env = os.environ.copy()
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_EFFORT",
              "VENICE_API_KEY", "UNQUANT_API_KEY", "ZAI_API_KEY", "OPENCODE_GO_API_KEY", "VENICE_CLAUDE_BACKEND"):
        env.pop(k, None)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env["VENICE_CURL"] = os.path.join(bindir, "curl.cmd")
    env["USERPROFILE"] = home  # $HOME in PowerShell: keeps the test profile and key files in the temp dir
    env.update(extra or {})
    return env


def run_launcher(bindir: str, home: str, args: list, extra_env: dict = None) -> dict:
    out = subprocess.check_output([LAUNCHER] + args, env=base_env(bindir, home, extra_env), text=True)
    return json.loads(out.strip().splitlines()[-1])


def main():
    with tempfile.TemporaryDirectory(prefix="claude-venice-test-") as tmp:
        bindir = make_fake_bin(tmp)
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        run = lambda args, env=None: run_launcher(bindir, home, args, env)

        # 1. default
        r = run(["-p", "hi"])
        assert "--strict-mcp-config" not in r["argv"] and "--mcp-config" not in r["argv"], r
        assert "--dangerously-skip-permissions" in r["argv"], r
        assert r["model"] == "deepseek-v4-1-flash-uncensored", r
        assert r["base_url"] == "http://127.0.0.1:18892" and r["has_token"] and not r["has_api_key"], r
        assert r["config_dir"] == os.path.join(home, ".claude-venice"), r

        # 2. --lean before -p: empty MCP config passed as a file (inline JSON does not survive Windows PowerShell)
        r = run(["--lean", "-p", "hi"])
        assert "--lean" not in r["argv"] and "--strict-mcp-config" in r["argv"], r
        cfg = r["argv"][r["argv"].index("--mcp-config") + 1]
        assert json.load(open(cfg)) == {"mcpServers": {}}, cfg
        assert r["argv"][-2:] == ["-p", "hi"], r

        # 3-5. -m and --lean in any order / position
        r = run(["-m", "z-ai-glm-5-3-flash", "--lean", "-p", "hi"])
        assert r["model"] == "z-ai-glm-5-3-flash" and "--lean" not in r["argv"] and "--strict-mcp-config" in r["argv"], r
        r = run(["--lean", "-m", "qwen-3-8-27b", "-p", "hi"])
        assert r["model"] == "qwen-3-8-27b" and "--strict-mcp-config" in r["argv"], r
        r = run(["-p", "hi", "--lean"])
        assert "--lean" not in r["argv"] and "--strict-mcp-config" in r["argv"], r

        # 6. caller's own --mcp-config is kept
        r = run(["--lean", "--mcp-config", "C:\\custom.json", "-p", "hi"])
        assert r["argv"].count("--mcp-config") == 1 and "C:\\custom.json" in r["argv"], r
        assert "--strict-mcp-config" in r["argv"], r

        # 7. zero args / bare --lean
        r = run([])
        assert r["argv"] == ["--dangerously-skip-permissions", "--permission-mode", "bypassPermissions"], r
        r = run(["--lean"])
        assert "--strict-mcp-config" in r["argv"] and "--lean" not in r["argv"], r

        # 8. permission flags
        r = run(["--permission-mode", "bypassPermissions", "-p", "hi"])
        assert "--dangerously-skip-permissions" in r["argv"], r
        r = run(["--permission-mode", "plan", "-p", "hi"])
        assert "--dangerously-skip-permissions" not in r["argv"], r
        r = run(["-p", "hi"], {"VENICE_CLAUDE_SKIP_PERMS": "0"})
        assert "--dangerously-skip-permissions" not in r["argv"], r

        # 9-10. --unquant
        uq = {"UNQUANT_API_KEY": "test-dummy-key", "UNQUANT_BASE_URL": "https://api.unquant.io"}
        r = run(["--unquant", "-p", "hi"], uq)
        assert "--unquant" not in r["argv"] and r["model"] == "deepseek-v4.1-flash-1017", r
        assert r["base_url"] == "https://api.unquant.io" and r["has_token"] is True, r
        r = run(["--unquant", "-m", "glm-5.3-flash", "--lean", "-p", "hi"], uq)
        assert r["model"] == "glm-5.3-flash" and "--strict-mcp-config" in r["argv"], r

        # 11-12. --zai
        r = run(["--zai", "-p", "hi"], {"ZAI_API_KEY": "test-dummy-zai-key"})
        assert "--zai" not in r["argv"] and r["model"] == "glm-5.3", r
        assert r["base_url"] == "https://api.z.ai/api/anthropic" and r["has_token"] is True, r
        r = run(["--zai", "-m", "glm-5.3-flash", "--lean", "-p", "hi"], {"ZAI_API_KEY": "k"})
        assert r["model"] == "glm-5.3-flash" and "--strict-mcp-config" in r["argv"], r

        # 13-14. --go / --opencode-go
        r = run(["--go", "-p", "hi"], {"OPENCODE_GO_API_KEY": "k"})
        assert "--go" not in r["argv"] and r["model"] == "mimo-v2.6-flash", r
        assert r["base_url"] == "https://opencode.ai/inference/go/anthropic", r
        r = run(["--opencode-go", "-m", "qwen4-max", "--lean", "-p", "hi"], {"OPENCODE_GO_API_KEY": "k"})
        assert "--opencode-go" not in r["argv"] and r["model"] == "qwen4-max", r

        # 15-16. --claude / --anthropic
        r = run(["--claude", "-p", "hi"], {"CLAUDE_API_KEY": "k"})
        assert "--claude" not in r["argv"] and r["model"] == "claude-haiku-5-5", r
        assert r["base_url"] is None and r["has_api_key"] is True and r["has_token"] is False, r
        assert "--effort" not in r["argv"], r
        r = run(["--claude", "-p", "hi"], {"CLAUDE_API_KEY": "k", "CLAUDE_API_EFFORT": "high"})
        assert r["argv"][r["argv"].index("--effort") + 1] == "high", r
        r = run(["--anthropic", "-m", "claude-sonnet-5-5", "--effort", "high", "--lean", "-p", "hi"], {"CLAUDE_API_KEY": "k"})
        assert r["model"] == "claude-sonnet-5-5" and r["argv"].count("--effort") == 1, r

        # 16b. --models: keys go through a header file, never on curl's command line; the file is removed
        for flag, env in (("--venice-api", {"VENICE_API_KEY": "SECRET-V"}), ("--claude", {"CLAUDE_API_KEY": "SECRET-C"}),
                          ("--unquant", {"UNQUANT_API_KEY": "SECRET-U"})):
            out = subprocess.check_output([LAUNCHER, flag, "--models"], env=base_env(bindir, home, env), text=True)
            assert out.strip() == "m1", out
            key = list(env.values())[0]
            argv_log = open(os.path.join(bindir, "curl_argv.log"), encoding="utf-8").read()
            hdr_log = open(os.path.join(bindir, "curl_hdrfiles.log"), encoding="utf-8").read()
            assert key not in argv_log, (flag, argv_log)
            assert key in hdr_log, (flag, hdr_log)
            for line in argv_log.splitlines():
                for a in line.split():
                    if a.startswith("@"):
                        assert not os.path.exists(a[1:]), "key file left behind"

        # 16c. key read from the key file under %USERPROFILE%\.config\venice-bridge
        kdir = os.path.join(home, ".config", "venice-bridge")
        os.makedirs(kdir, exist_ok=True)
        open(os.path.join(kdir, "zai-api-key.txt"), "w").write("FILE-KEY\r\n")
        r = run(["--zai", "-p", "hi"])
        assert r["has_token"] is True, r

        # 17-18. --venice-api / --api
        r = run(["--venice-api", "-p", "hi"], {"VENICE_API_KEY": "k"})
        assert "--venice-api" not in r["argv"] and r["model"] == "deepseek-v4-1-flash-uncensored", r
        assert r["base_url"] == "http://127.0.0.1:18892/venice-api" and r["has_token"] and not r["has_api_key"], r
        r = run(["--api", "-m", "venice-uncensored-1-2", "--lean", "-p", "hi"], {"VENICE_API_KEY": "k"})
        assert "--api" not in r["argv"] and r["model"] == "venice-uncensored-1-2", r

        # 19. arguments with spaces survive the .cmd -> .ps1 -> claude hops
        r = run(["-p", "hello world"])
        assert r["argv"][-2:] == ["-p", "hello world"], r

        # 20. --quota
        out = subprocess.check_output([LAUNCHER, "--quota"], env=base_env(bindir, home), text=True)
        assert "remaining=5000" in out and "Bridge cache" in out, out

    print("claude-venice windows tests ok")


if __name__ == "__main__":
    main()
