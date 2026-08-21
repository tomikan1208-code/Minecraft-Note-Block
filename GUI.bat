@echo off
setlocal
rem mcnb - GUI launcher. Double-click to start.
rem All Japanese messages are printed by Python (see mcnb/launch.py),
rem so this file stays ASCII-only and is immune to code page problems.

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

uv sync --extra gui --extra audio --extra measure
if errorlevel 1 (
  echo [ERROR] dependency setup failed.
  echo         Close Minecraft / other mcnb windows and try again.
  pause
  exit /b 1
)

uv run mcnb gui
if not "%errorlevel%"=="0" pause
endlocal
