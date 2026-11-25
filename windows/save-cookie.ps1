# save-cookie.ps1 - save the venice.ai login cookie (`__client` of clerk.venice.ai) without echoing it.
# Get it in the browser where you are logged in to venice.ai:
#   DevTools (F12) -> Application -> Cookies -> https://clerk.venice.ai -> __client -> copy Value
$ErrorActionPreference = 'Stop'
$dir = Join-Path $HOME '.config\venice-bridge'
$file = Join-Path $dir 'client-cookie.txt'
$sec = Read-Host -AsSecureString 'Paste the __client cookie value (input is hidden), then Enter'
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
try { $val = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
$val = $val.Trim() -replace '^__client=', '' -replace ';.*$', ''
if ($val.Length -lt 20) { throw 'That does not look like a cookie value (too short). Nothing saved.' }
New-Item -ItemType Directory -Force -Path $dir | Out-Null
[IO.File]::WriteAllText($file, $val)
"Saved ($($val.Length) chars) to $file"
# tell a running bridge to drop its cached login
& curl.exe -s --noproxy '*' --max-time 2 -X POST http://127.0.0.1:18891/reload -o NUL 2>$null
