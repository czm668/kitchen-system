@echo off
chcp 65001 >nul
title Kitchen API - KEEP THIS WINDOW OPEN
cd /d "%~dp0.."

rem Portable Python lives 4 levels up from tools\ : <root>\.local\python311\python.exe
rem Using %~dp0 + relative ".." keeps this file pure ASCII even if the project
rem path contains Chinese characters (encoding would otherwise garble it).
set "PY=%~dp0..\..\..\..\.local\python311\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo   Kitchen customer API   --^>   http://0.0.0.0:8848
echo   KEEP THIS WINDOW OPEN while testing the mini program.
echo   Press Ctrl+C to stop the server.
echo.

"%PY%" tools\miniapp_check.py --serve --lan
if errorlevel 1 echo [ERROR] Server exited with code %errorlevel%.
pause
