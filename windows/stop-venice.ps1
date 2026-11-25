# stop-venice.ps1 - stop the venice-bridge / venice-adapters processes started by start-venice.ps1.
$ErrorActionPreference = 'Stop'
$procs = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%.exe' OR Name = 'py.exe'" |
    Where-Object { $_.CommandLine -match 'venice_(bridge|adapters)\.py' }
if (-not $procs) { 'Nothing to stop.'; return }
foreach ($p in $procs) {
    $what = if ($p.CommandLine -match 'venice_adapters') { 'adapters' } else { 'bridge' }
    Stop-Process -Id $p.ProcessId -Force -Confirm:$false
    "stopped $what (pid $($p.ProcessId))"
}
