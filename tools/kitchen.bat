@echo off
chcp 65001 >nul
rem Unified entry (A7): keep this file pure ASCII - Chinese paths would
rem garble under encoding differences. Dispatch to tools\ scripts.
setlocal
cd /d "%~dp0.."
set "PY=%~dp0..\..\..\..\.local\python311\python.exe"
if not exist "%PY%" set "PY=python"

set "CMD=%~1"
if "%CMD%"=="" goto usage

if /i "%CMD%"=="ci"     goto ci
if /i "%CMD%"=="test"   goto test
if /i "%CMD%"=="serve"  goto serve
if /i "%CMD%"=="serve-miniapp" goto serve-miniapp
if /i "%CMD%"=="backup" goto backup
goto usage

:ci
"%PY%" tools\ci.py %2 %3 %4
goto :eof

:test
"%PY%" -m pytest -o addopts= -q tests/ %2 %3
goto :eof

:serve
rem 生产入口：API + 同源静态前端（审查报告 §3：serve 必须真是服务）
"%PY%" -m kitchen.api.server --static web\dist
goto :eof

:serve-miniapp
rem 小程序验收用的临时服务器（原来错挂在 serve 名下）
"%PY%" tools\miniapp_check.py --serve --lan --db --schedule
goto :eof

:backup
"%PY%" tools\backup.py --db var\runtime.db --keep 14
goto :eof

:usage
echo Usage: kitchen.bat ^<command^>
echo   ci            run ALL quality gates (rule_lint + numbers + pytest + migrate + web + miniapp)
echo   test          run pytest only
echo   serve         start PRODUCTION API + static web (http://127.0.0.1:8848)
echo   serve-miniapp start TEMP test server for mini program on LAN (demo only)
echo   backup        backup runtime db into backups\ (keep 14)
endlocal
