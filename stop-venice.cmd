@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-venice.ps1" %*
exit /b %ERRORLEVEL%
