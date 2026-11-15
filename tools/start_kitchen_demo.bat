@echo off
chcp 936 >nul
title Kitchen Demo - one click
cd /d "%~dp0.."

rem Portable Python: 4 levels up from tools\. Relative path survives
rem project moves on the same drive.
set "PY=%~dp0..\..\..\..\.local\python311\python.exe"
if not exist "%PY%" set "PY=python"

rem 1) backend API on 8848 -- --fix starts it (minimized console) if not running
echo [1/2] backend API: check / auto-start on 8848 ...
"%PY%" tools\miniapp_netcheck.py --fix
if errorlevel 1 (
  echo [WARN] backend not ready, see messages above.
)

rem 2) open WeChat DevTools with the miniapp project (needs "服务端口"
rem    enabled in DevTools settings-security; failure here is not fatal)
echo [2/2] opening WeChat DevTools with miniapp project ...
call "D:\微信web开发者工具\cli.bat" open --project "%~dp0..\miniapp"
if errorlevel 1 (
  echo [INFO] DevTools CLI failed. Likely cause: service port not enabled
  echo        (DevTools - settings - security). Open miniapp folder manually.
  echo        Backend API is still running either way.
)

echo Done. Backend console runs minimized in the taskbar (Kitchen API).
echo Close THAT minimized window to stop the server.
pause
