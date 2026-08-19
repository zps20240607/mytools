@echo off
rem Create desktop shortcut for PortWatch
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0install.ps1"
pause
