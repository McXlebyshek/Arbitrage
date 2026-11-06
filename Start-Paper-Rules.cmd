@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m arbitrage.paper_rules --watch %*
if errorlevel 1 pause
