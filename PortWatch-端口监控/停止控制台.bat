@echo off
chcp 936 >nul
title PortWatch - 停止
cd /d "%~dp0"
powershell -NoProfile -Command "$p = Get-Content -LiteralPath 'data\server.pid' -ErrorAction SilentlyContinue; if ($p) { $proc = Get-Process -Id $p -ErrorAction SilentlyContinue; if ($proc) { Stop-Process -Id $p -Force; Write-Host ('已停止控制台 (pid ' + $p + ')') } else { Write-Host '未发现运行中的控制台进程（记录已过期）。' }; Remove-Item -LiteralPath 'data\server.pid' -Force -ErrorAction SilentlyContinue } else { Write-Host '未找到运行记录，控制台可能未在运行。' }"
timeout /t 3 /nobreak >nul