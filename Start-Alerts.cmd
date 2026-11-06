@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m arbitrage.alerts --watch %*
if errorlevel 1 pause
