@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m arbitrage.paper_funding_watch --watch %*
if errorlevel 1 pause
