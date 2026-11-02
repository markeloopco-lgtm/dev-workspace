@echo off
rem ============================================================
rem  Auto video editor. Drag and drop a video file onto this file.
rem  (Double-click without a file opens a file picker.)
rem  First run sets up Python packages and ffmpeg automatically.
rem ============================================================
setlocal
cd /d "%~dp0"

set "PY="
py -3.12 --version >nul 2>nul && set "PY=py -3.12"
if not defined PY (py -3 --version >nul 2>nul && set "PY=py -3")
if not defined PY (python --version >nul 2>nul && set "PY=python")
if not defined PY (
  echo [setup] Python was not found. Installing Python 3.12 with winget...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  echo.
  echo [setup] Python was installed. Close this window and drag the video onto this file again.
  pause
  exit /b 1
)

%PY% video_edit\auto_edit.py %*
echo.
pause
