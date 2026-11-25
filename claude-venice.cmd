@echo off
rem claude-venice for Windows: runs claude-venice.ps1 next to this file (put this folder on PATH).
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0claude-venice.ps1" %*
exit /b %ERRORLEVEL%
