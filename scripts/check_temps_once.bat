@echo off
rem Show the current GPU/CPU temperature once, then wait for a key press.
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

fltmc >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0monitor_temps.ps1" -Once

echo.
pause
