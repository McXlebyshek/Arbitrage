@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m arbitrage.delivery --watch %*
if errorlevel 1 pause
