@echo off
chcp 936 >nul
title CostWatch - 停止
cd /d "%~dp0"
powershell -NoProfile -Command "$pidFile='data\server.pid'; $id=$null; $p=Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue; if($p){$proc=Get-Process -Id $p -ErrorAction SilentlyContinue; if($proc){$id=[int]$p}}; if(-not $id){$c=Get-NetTCPConnection -State Listen -LocalPort 9640 -ErrorAction SilentlyContinue | Select-Object -First 1; if($c){$id=$c.OwningProcess}}; if($id){Stop-Process -Id $id -Force -ErrorAction SilentlyContinue; Write-Host ('已停止控制台 (pid ' + $id + ')')}else{Write-Host '未发现运行中的控制台进程。'}; Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue"
timeout /t 3 /nobreak >nul
