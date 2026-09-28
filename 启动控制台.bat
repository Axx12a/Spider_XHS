@echo off
rem ============================================================
rem  Xiaohongshu Console Launcher
rem  Double-click this file, or use the desktop shortcut.
rem  Keep this window open while using the console.
rem  Close it (or press Ctrl+C) to stop the service.
rem ============================================================
chcp 65001 >nul
title Xiaohongshu Console
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :novenv

".venv\Scripts\python.exe" webui.py

echo.
echo ------------------------------------------------------------
echo  Service stopped. Press any key to close this window.
echo ------------------------------------------------------------
pause >nul
exit /b 0

:novenv
echo.
echo [ERROR] Virtual environment not found:
echo    %CD%\.venv
echo.
echo Please run the setup once:
echo    python -m venv .venv
echo    .venv\Scripts\python.exe -m pip install -r requirements.txt
echo    npm ci
echo.
pause
exit /b 1
