@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-venice.ps1" %*
exit /b %ERRORLEVEL%
