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
if /i "%CMD%"=="backup" goto backup
goto usage

:ci
"%PY%" tools\ci.py %2 %3 %4
goto :eof

:test
"%PY%" -m pytest -o addopts= -q tests/ %2 %3
goto :eof

:serve
"%PY%" tools\miniapp_check.py --serve --lan --db --schedule
goto :eof

:backup
"%PY%" tools\backup.py --db var\runtime.db --keep 14
goto :eof

:usage
echo Usage: kitchen.bat ^<command^>
echo   ci      run ALL quality gates (pytest + migrate + web_check + miniapp_check)
echo   test    run pytest only
echo   serve   start API server (LAN + db + scheduler), for demo / mini program
echo   backup  backup runtime db into backups\ (keep 14)
endlocal
