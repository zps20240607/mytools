@echo off
rem ============================================
rem  Daily Todo - Closer
rem  1. Graceful close via HTTP /close endpoint
rem  2. Force-kill fallback (Edge + server)
rem ============================================
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0close.ps1"
echo Daily Todo closed.
