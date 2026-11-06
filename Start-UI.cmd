@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m arbitrage.ui --open-browser %*
if errorlevel 1 pause
