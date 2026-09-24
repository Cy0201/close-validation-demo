@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" launch.py --stop
if errorlevel 1 pause
