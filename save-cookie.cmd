@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0save-cookie.ps1" %*
exit /b %ERRORLEVEL%
