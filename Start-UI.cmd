@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Create the project environment first:
    echo   python -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements-lock.txt
    exit /b 1
)
".venv\Scripts\python.exe" monitor.py --open %*
if errorlevel 1 pause
