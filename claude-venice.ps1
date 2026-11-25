# claude-venice.ps1 - Windows port of the bash `claude-venice` launcher.
# Claude Code CLI on a local venice-bridge (your venice.ai web plan, no API credits).
#
#   claude  --/v1/messages-->  venice-adapters :18892  -->  venice-bridge :18891  -->  venice.ai web
#
# Isolated profile %USERPROFILE%\.claude-venice: the official `claude` install and its auth stay untouched.
# Same flags as the bash launcher:
#   claude-venice [-m <model>] [--lean] [--venice-api|--api|--unquant|--zai|--go|--opencode-go|--claude|--anthropic] [claude args...]
#   claude-venice [backend flag] --models
#   claude-venice --quota
# Usually started through claude-venice.cmd (works from cmd, PowerShell and Windows Terminal).

$ErrorActionPreference = 'Stop'

$ADAPTER = 'http://127.0.0.1:18892'
$BRIDGE = 'http://127.0.0.1:18891'
$CfgDir = Join-Path $HOME '.config\venice-bridge'
$VENICE_API_DIRECT = 'https://api.venice.ai/api/v1'
$UNQUANT_GATE = 'http://127.0.0.1:18893'
$UNQUANT_DIRECT = 'https://api.unquant.io'
$ZAI_ANTHROPIC_URL = 'https://api.z.ai/api/anthropic'
$ZAI_MODELS_URL = 'https://api.z.ai/api/coding/paas/v4/models'
$OPENCODE_GO_ANTHROPIC_URL = 'https://opencode.ai/inference/go/anthropic'
$OPENCODE_GO_MODELS_URL = 'https://opencode.ai/inference/go/openai/v1/models'
$CLAUDE_API_URL = 'https://api.anthropic.com'
$PROFILE_DIR = Join-Path $HOME '.claude-venice'
# test hook: a stub curl; normal runs use Windows' own curl.exe (System32, Windows 10 1803+)
$Curl = if ($env:VENICE_CURL) { $env:VENICE_CURL } else { 'curl.exe' }

function Get-EnvOr([string]$name, [string]$default) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrEmpty($v)) { return $default } else { return $v }
}

function Stop-Launcher([string]$msg) {
    [Console]::Error.WriteLine("[x] $msg")
    exit 1
}

# --- launcher-owned flags anywhere before `--` ---
$MODEL = ''
$MODEL_EXPLICIT = $false
$LEAN = $false
$BACKEND = Get-EnvOr 'VENICE_CLAUDE_BACKEND' 'venice'
$pass = New-Object System.Collections.Generic.List[string]
$argv = @($args | ForEach-Object { [string]$_ })
$i = 0
while ($i -lt $argv.Count) {
    $a = $argv[$i]
    switch -CaseSensitive ($a) {
        '-m' {
            if ($i + 1 -lt $argv.Count) { $MODEL = $argv[$i + 1]; $MODEL_EXPLICIT = $true; $i += 2 }
            else { $pass.Add($a); $i++ }
            continue
        }
        '--lean' { $LEAN = $true; $i++; continue }
        { $_ -in '--venice-api', '--api' } { $BACKEND = 'venice-api'; $i++; continue }
        '--unquant' { $BACKEND = 'unquant'; $i++; continue }
        '--zai' { $BACKEND = 'zai'; $i++; continue }
        { $_ -in '--go', '--opencode-go' } { $BACKEND = 'opencode-go'; $i++; continue }
        { $_ -in '--claude', '--anthropic' } { $BACKEND = 'claude'; $i++; continue }
        '--' { for ($j = $i; $j -lt $argv.Count; $j++) { $pass.Add($argv[$j]) }; $i = $argv.Count; continue }
        default { $pass.Add($a); $i++ }
    }
}

if (-not $MODEL_EXPLICIT) {
    $MODEL = switch ($BACKEND) {
        'venice-api' { Get-EnvOr 'VENICE_API_MODEL' 'deepseek-v4-1-flash-uncensored' }
        'unquant' { Get-EnvOr 'UNQUANT_CLAUDE_MODEL' 'deepseek-v4.1-flash-1017' }
        'zai' { Get-EnvOr 'ZAI_CLAUDE_MODEL' 'glm-5.3' }
        'opencode-go' { Get-EnvOr 'OPENCODE_GO_CLAUDE_MODEL' 'mimo-v2.6-flash' }
        'claude' { Get-EnvOr 'CLAUDE_API_MODEL' 'claude-haiku-5-5' }
        default { Get-EnvOr 'VENICE_CLAUDE_MODEL' 'deepseek-v4-1-flash-uncensored' }
    }
}

# backend -> key env vars (in order), key file, display name
$KeySpec = @{
    'venice-api'  = @{ Env = @('VENICE_API_KEY'); File = (Get-EnvOr 'VENICE_API_KEY_FILE' (Join-Path $CfgDir 'venice-api-key.txt')); Name = 'Venice API' }
    'unquant'     = @{ Env = @('UNQUANT_API_KEY'); File = (Get-EnvOr 'UNQUANT_API_KEY_FILE' (Join-Path $CfgDir 'unquant-api-key.txt')); Name = 'Unquant API' }
    'zai'         = @{ Env = @('ZAI_API_KEY'); File = (Get-EnvOr 'ZAI_API_KEY_FILE' (Join-Path $CfgDir 'zai-api-key.txt')); Name = 'Z.AI API' }
    'opencode-go' = @{ Env = @('OPENCODE_GO_API_KEY'); File = (Get-EnvOr 'OPENCODE_GO_API_KEY_FILE' (Join-Path $CfgDir 'opencode-go-api-key.txt')); Name = 'OpenCode Go API' }
    'claude'      = @{ Env = @('CLAUDE_API_KEY', 'ANTHROPIC_API_KEY'); File = (Get-EnvOr 'CLAUDE_API_KEY_FILE' (Join-Path $CfgDir 'anthropic-api-key.txt')); Name = 'Anthropic API' }
}

function Resolve-Key([string]$backend) {
    $s = $KeySpec[$backend]
    foreach ($e in $s.Env) {
        $v = [Environment]::GetEnvironmentVariable($e)
        if (-not [string]::IsNullOrEmpty($v)) { return $v }
    }
    if (Test-Path -LiteralPath $s.File -PathType Leaf) {
        $v = ([IO.File]::ReadAllText($s.File)) -replace "[`r`n]", ''
        if ($v) { return $v }
    }
    $envs = $s.Env -join ' / '
    Stop-Launcher "$($s.Name) key not found. Set $envs or save it to $($s.File)."
}

# curl with one secret header read from a temp file (`-H @file`), so the key never shows in the process list.
function Invoke-CurlWithKey([string]$header, [string[]]$curlArgs) {
    $hf = [IO.Path]::GetTempFileName()
    try {
        [IO.File]::WriteAllText($hf, "$header`n")
        $out = & $Curl -H "@$hf" @curlArgs
        return ($out -join "`n")
    } finally {
        Remove-Item -LiteralPath $hf -Force -ErrorAction SilentlyContinue
    }
}

function Get-Local([string]$url, [int]$timeout) {
    # returns the body ('' when nothing answered); --noproxy so a system proxy never sees loopback traffic
    $out = & $Curl -s --noproxy '*' --max-time $timeout $url 2>$null
    if ($LASTEXITCODE -ne 0) { return '' }
    return ($out -join "`n")
}

function Write-ModelIds([string]$json) {
    try { (ConvertFrom-Json $json).data | ForEach-Object { $_.id } }
    catch { Stop-Launcher 'could not parse the model list' }
}

$first = if ($pass.Count -gt 0) { $pass[0] } else { '' }

if ($first -eq '--models') {
    switch ($BACKEND) {
        'venice-api' { $k = Resolve-Key $BACKEND; $j = Invoke-CurlWithKey "Authorization: Bearer $k" @('-s', '--max-time', '10', '-H', 'User-Agent: claude-venice/1.0', "$VENICE_API_DIRECT/models") }
        'unquant' { $k = Resolve-Key $BACKEND; $j = Invoke-CurlWithKey "Authorization: Bearer $k" @('-s', '--max-time', '10', "$UNQUANT_DIRECT/v1/models") }
        'zai' { $k = Resolve-Key $BACKEND; $j = Invoke-CurlWithKey "Authorization: Bearer $k" @('-s', '--max-time', '10', $ZAI_MODELS_URL) }
        'opencode-go' { $k = Resolve-Key $BACKEND; $j = Invoke-CurlWithKey "Authorization: Bearer $k" @('-s', '--max-time', '10', $OPENCODE_GO_MODELS_URL) }
        'claude' { $k = Resolve-Key $BACKEND; $j = Invoke-CurlWithKey "x-api-key: $k" @('-s', '--max-time', '10', '-H', 'anthropic-version: 2023-06-01', "$CLAUDE_API_URL/v1/models") }
        default { $j = Get-Local "$BRIDGE/v1/models" 10 }
    }
    Write-ModelIds $j
    exit 0
}

if ($first -eq '--quota') {
    switch ($BACKEND) {
        'venice-api' { "Official Venice API - check credits/spend limits at https://venice.ai/settings/api (endpoint: $VENICE_API_DIRECT)."; exit 0 }
        'unquant' { 'Unquant.io - flat-rate unlimited tokens (concurrency-metered per plan).'; exit 0 }
        'zai' { "Z.AI Coding Plan - dedicated GLM coding plan (endpoint: $ZAI_ANTHROPIC_URL)."; exit 0 }
        'opencode-go' { "OpenCode Go - check weekly usage at https://opencode.ai/console (endpoint: $OPENCODE_GO_ANTHROPIC_URL)."; exit 0 }
        'claude' { 'Official Anthropic API - check credits/billing at https://console.anthropic.com/settings/billing'; exit 0 }
    }
    try {
        $d = ConvertFrom-Json (Get-Local "$BRIDGE/health" 5)
        $q = $d.chat_quota; $c = $d.cache
        function Show-Value($v, $dflt) { if ($null -eq $v) { $dflt } else { $v } }
        'Venice web quota  remaining={0}  resets_in_min={1}' -f (Show-Value $q.remaining '?'), (Show-Value $q.resets_in_min '?')
        'Bridge cache      hits={0} misses={1} entries={2} enabled={3}' -f (Show-Value $c.hits 0), (Show-Value $c.misses 0), (Show-Value $c.entries 0), $c.enabled
    } catch {
        "could not read $BRIDGE/health"
    }
    exit 0
}

# --- preflight + routing ---
$Q_REM = ''
$TARGET_URL = ''
$token = $null
$apiKey = $null

switch ($BACKEND) {
    'venice-api' {
        $token = Resolve-Key $BACKEND
        if ($env:VENICE_API_BASE_URL) { $TARGET_URL = $env:VENICE_API_BASE_URL.TrimEnd('/') }
        else {
            if (-not (Get-Local "$ADAPTER/health" 3)) { Stop-Launcher "venice-adapters ($ADAPTER) is down.  Start it: start-venice.cmd" }
            $TARGET_URL = "$ADAPTER/venice-api"
        }
    }
    'unquant' {
        $token = Resolve-Key $BACKEND
        if ($env:UNQUANT_BASE_URL) { $TARGET_URL = $env:UNQUANT_BASE_URL.TrimEnd('/') }
        elseif (Get-Local "$UNQUANT_GATE/health" 2) { $TARGET_URL = $UNQUANT_GATE }
        else { $TARGET_URL = $UNQUANT_DIRECT }
    }
    'zai' { $token = Resolve-Key $BACKEND; $TARGET_URL = (Get-EnvOr 'ZAI_BASE_URL' $ZAI_ANTHROPIC_URL).TrimEnd('/') }
    'opencode-go' { $token = Resolve-Key $BACKEND; $TARGET_URL = (Get-EnvOr 'OPENCODE_GO_BASE_URL' $OPENCODE_GO_ANTHROPIC_URL).TrimEnd('/') }
    'claude' { $apiKey = Resolve-Key $BACKEND; $TARGET_URL = (Get-EnvOr 'ANTHROPIC_API_BASE_URL' $CLAUDE_API_URL).TrimEnd('/') }
    default {
        # bridge + adapter must be up and the Venice session active
        $bh = Get-Local "$BRIDGE/health" 3
        if (-not $bh) { Stop-Launcher "venice-bridge ($BRIDGE) is down.  Start it: start-venice.cmd" }
        if ($bh -notmatch '"venice_session": *"active"') {
            Stop-Launcher 'Venice web session is not active (login expired). Refresh the cookie file (README, Quick start step 1).'
        }
        if (-not (Get-Local "$ADAPTER/health" 3)) { Stop-Launcher "venice-adapters ($ADAPTER) is down.  Start it: start-venice.cmd" }
        # the web plan is request-metered: warn when the daily quota is low
        try { $Q_REM = [string](ConvertFrom-Json $bh).chat_quota.remaining } catch { $Q_REM = '' }
        if ($Q_REM -match '^\d+$' -and [int]$Q_REM -lt 500) {
            [Console]::Error.WriteLine("[!] Venice web quota low: $Q_REM requests left today (run 'claude-venice --quota').")
        }
        $TARGET_URL = $ADAPTER
        $token = 'venice-local'   # adapter ignores auth; a filler keeps Claude Code happy
    }
}

# --- isolated profile + routing (overrides any inherited provider/auth env) ---
New-Item -ItemType Directory -Force -Path $PROFILE_DIR | Out-Null
$env:CLAUDE_CONFIG_DIR = $PROFILE_DIR
$env:CLAUDE_HOME = $PROFILE_DIR
if ($BACKEND -eq 'claude') {
    Remove-Item Env:ANTHROPIC_AUTH_TOKEN -ErrorAction SilentlyContinue
    if ($env:ANTHROPIC_API_BASE_URL) { $env:ANTHROPIC_BASE_URL = $TARGET_URL } else { Remove-Item Env:ANTHROPIC_BASE_URL -ErrorAction SilentlyContinue }
    $env:ANTHROPIC_API_KEY = $apiKey
} else {
    Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue   # no real Anthropic credential reaches Venice
    $env:ANTHROPIC_BASE_URL = $TARGET_URL
    $env:ANTHROPIC_AUTH_TOKEN = $token
}
$env:ANTHROPIC_MODEL = $MODEL
$env:ANTHROPIC_DEFAULT_OPUS_MODEL = $MODEL
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = $MODEL
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = $MODEL
$env:CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT = '1'
$env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = '1'
$env:API_TIMEOUT_MS = '3000000'
$env:NO_PROXY = '127.0.0.1,localhost'

$claude = Get-Command claude -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $claude) { Stop-Launcher "'claude' command not found on PATH (install: irm https://claude.ai/install.ps1 | iex)" }

$out = New-Object System.Collections.Generic.List[string]
$out.AddRange($pass)

# Metered Anthropic API: keep Claude Code's default effort; CLAUDE_API_EFFORT opts in (an explicit --effort wins).
if ($BACKEND -eq 'claude' -and $env:CLAUDE_API_EFFORT) {
    if (-not ($out | Where-Object { $_ -eq '--effort' -or $_ -like '--effort=*' })) {
        $out.InsertRange(0, [string[]]@('--effort', $env:CLAUDE_API_EFFORT))
    }
}

# --- no permission prompts by default (same reasons as the bash launcher) ---
$addSkip = (Get-EnvOr 'VENICE_CLAUDE_SKIP_PERMS' '1') -eq '1'
$hasDsp = $false; $wantBypass = $false; $prev = ''
foreach ($a in $out) {
    if ($a -eq '--dangerously-skip-permissions') { $addSkip = $false; $hasDsp = $true }
    elseif ($a -eq '--permission-mode=bypassPermissions') { $addSkip = $false; $wantBypass = $true }
    elseif ($a -eq '--permission-mode' -or $a -like '--permission-mode=*') { $addSkip = $false }
    if ($prev -eq '--permission-mode' -and $a -eq 'bypassPermissions') { $wantBypass = $true }
    $prev = $a
}
if ($addSkip) { $out.InsertRange(0, [string[]]@('--dangerously-skip-permissions', '--permission-mode', 'bypassPermissions')) }
elseif ($wantBypass -and -not $hasDsp) { $out.Insert(0, '--dangerously-skip-permissions') }

if ($LEAN) {
    if ($out | Where-Object { $_ -eq '--mcp-config' -or $_ -like '--mcp-config=*' }) {
        $out.Insert(0, '--strict-mcp-config')
    } else {
        # Windows PowerShell mangles inline JSON passed to native programs, so pass an empty config file instead
        $emptyMcp = Join-Path $PROFILE_DIR 'empty-mcp.json'
        if (-not (Test-Path -LiteralPath $emptyMcp)) { [IO.File]::WriteAllText($emptyMcp, '{"mcpServers":{}}') }
        $out.InsertRange(0, [string[]]@('--strict-mcp-config', '--mcp-config', $emptyMcp))
    }
}

if (-not [Console]::IsOutputRedirected) {
    switch ($BACKEND) {
        'venice-api' { "Claude Code on Official Venice API (metered) - model: $MODEL"; "   endpoint: $TARGET_URL -> $VENICE_API_DIRECT   profile: $PROFILE_DIR" }
        'unquant' { "Claude Code on Unquant - model: $MODEL"; "   endpoint: $TARGET_URL   profile: $PROFILE_DIR" }
        'zai' { "Claude Code on Z.AI Coding Plan - model: $MODEL"; "   endpoint: $TARGET_URL   profile: $PROFILE_DIR" }
        'opencode-go' { "Claude Code on OpenCode Go - model: $MODEL"; "   endpoint: $TARGET_URL   profile: $PROFILE_DIR" }
        'claude' { "Claude Code on Official Anthropic API (metered) - model: $MODEL (effort: $(Get-EnvOr 'CLAUDE_API_EFFORT' 'default'))"; "   endpoint: $TARGET_URL   profile: $PROFILE_DIR" }
        default {
            "Claude Code on Venice - model: $MODEL"
            "   adapter: $ADAPTER   profile: $PROFILE_DIR   (official claude profile untouched)"
            if ($Q_REM) { "   web quota remaining: $Q_REM   ('claude-venice --quota' for details)" }
        }
    }
    if ($addSkip) { '   permissions: skipped - no prompts (set VENICE_CLAUDE_SKIP_PERMS=0 to restore)' }
    if ($LEAN) { '   mode: --lean (MCP servers disabled via --strict-mcp-config)' }
    ''
}

& $claude.Source @out
exit $LASTEXITCODE
