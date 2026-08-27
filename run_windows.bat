@echo off
cd /d "%~dp0"
python world_tracker.py
if errorlevel 1 pause
