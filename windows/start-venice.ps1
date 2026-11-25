# start-venice.ps1 - start venice-bridge (:18891) and venice-adapters (:18892) in the background on Windows.
# Replaces the macOS launchd plists.
#
#   start-venice.ps1               start both (skips one whose /health already answers)
#   start-venice.ps1 -BridgeOnly   start only the bridge (OpenCode and other OpenAI clients need nothing else)
#   start-venice.ps1 -Install      also start both at every logon (Task Scheduler, current user, no admin)
#   start-venice.ps1 -Uninstall    remove that logon task
#
# Logs: %LOCALAPPDATA%\venice-bridge\logs\{bridge,adapters}.log   Stop: stop-venice.ps1
param(
    [switch]$BridgeOnly,
    [switch]$Install,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)   # repo root: this script lives in windows\
$TaskName = 'venice-bridge'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        "Logon task '$TaskName' removed."
    } else { "No logon task '$TaskName'." }
    return
}

if ($Install) {
    $argLine = "-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$($MyInvocation.MyCommand.Path)`""
    if ($BridgeOnly) { $argLine += ' -BridgeOnly' }
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argLine -WorkingDirectory $Here
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description 'Start venice-bridge and venice-adapters at logon' -Force | Out-Null
    "Logon task '$TaskName' installed (remove with: start-venice -Uninstall)."
}

$py = Get-Command python -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike '*\WindowsApps\*' } | Select-Object -First 1
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue | Select-Object -First 1 }
if (-not $py) { throw 'Python not found on PATH (install: winget install Python.Python.3.13).' }

$LogDir = Join-Path $env:LOCALAPPDATA 'venice-bridge\logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Test-Health([string]$url) {
    & curl.exe -s --noproxy '*' --max-time 2 -o NUL $url 2>$null
    return $LASTEXITCODE -eq 0
}

function Start-Part([string]$name, [string]$script, [string]$healthUrl) {
    if (Test-Health $healthUrl) { "$name already running ($healthUrl)"; return }
    $log = Join-Path $LogDir "$name.log"
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'
    # cmd /c does the redirect so stdout and stderr share one log file
    $cmdLine = "/c `"`"$($py.Source)`" `"$(Join-Path $Here $script)`" >> `"$log`" 2>&1`""
    Start-Process -FilePath 'cmd.exe' -ArgumentList $cmdLine -WorkingDirectory $Here -WindowStyle Hidden | Out-Null
    for ($n = 0; $n -lt 40; $n++) {
        Start-Sleep -Milliseconds 250
        if (Test-Health $healthUrl) { "$name started ($healthUrl), log: $log"; return }
    }
    "[!] $name did not answer $healthUrl yet - see $log"
}

Start-Part 'bridge' 'venice_bridge.py' 'http://127.0.0.1:18891/health'
if (-not $BridgeOnly) { Start-Part 'adapters' 'venice_adapters.py' 'http://127.0.0.1:18892/health' }
