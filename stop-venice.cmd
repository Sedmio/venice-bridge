@echo off
rem Windows entry point: runs windows\stop-venice.ps1 (the .ps1 lives in a subfolder so PATH lookups find this .cmd, which bypasses the default script policy).
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\stop-venice.ps1" %*
exit /b %ERRORLEVEL%
