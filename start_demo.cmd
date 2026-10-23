@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Install the Python environment first. See README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\start_demo.py
pause
