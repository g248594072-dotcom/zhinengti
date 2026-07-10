@echo off
cd /d "%~dp0"
echo WorkDir: %CD%
where python
python --version
python "%~dp0new7.2.py"
echo ExitCode: %ERRORLEVEL%
pause
