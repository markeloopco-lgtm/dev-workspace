@echo off
rem GPU/CPU temperature monitor - double-click to start.
rem Runs monitor_temps.ps1 under Windows PowerShell 5.1 with administrator
rem rights, which is what the CPU temperature sensors require.
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

fltmc >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0monitor_temps.ps1" -Setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0monitor_temps.ps1" %*

echo.
pause
