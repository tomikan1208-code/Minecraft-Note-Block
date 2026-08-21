@echo off
setlocal
rem mcnb - measurement rig. Needs a Minecraft client to connect.

cd /d "%~dp0"

where uv >nul 2>&1
if errorlevel 1 (
  echo [ERROR] uv not found. Install from https://docs.astral.sh/uv/
  pause
  exit /b 1
)

rem --- clean up leftovers from the previous run (venv only, no download) ---
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m mcnb.procs
)

uv sync --extra measure --extra audio
uv run mcnb measure
pause
endlocal
