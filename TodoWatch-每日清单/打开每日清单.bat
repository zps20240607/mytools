@echo off
rem ============================================
rem  Daily Todo - Launcher
rem  Starts local server + opens Edge in app mode
rem ============================================
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0open.ps1"
