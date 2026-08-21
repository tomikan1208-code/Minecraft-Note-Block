@echo off
setlocal
rem mcnb - stop leftover processes (GUI port 8770, verification server 25566/25575).
rem Your own Minecraft client is not touched.

cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m mcnb.procs
) else (
  echo [ERROR] .venv not found. Run GUI.bat once first.
)
pause
endlocal
