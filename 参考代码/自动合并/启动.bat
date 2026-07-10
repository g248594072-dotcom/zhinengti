@echo off
cd /d "%~dp0"
python "%~dp0new7.2.py"
if errorlevel 1 pause
