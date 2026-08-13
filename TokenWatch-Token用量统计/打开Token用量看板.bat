@echo off
chcp 936 >nul
title TokenWatch - 用量统计
cd /d "%~dp0"
echo ============================================
echo    TokenWatch  跨工具 Token 用量统计
echo ============================================
echo [1/3] 扫描最新用量并生成看板...
python "%~dp0token_watch.py" scan
python "%~dp0token_watch.py" dashboard --out "%~dp0dashboard.html"
echo.
echo [2/3] 启动后台自动刷新（每2分钟扫描一次）...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -like '*token_watch.py*watch*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
start "" pythonw "%~dp0token_watch.py" watch --interval 120 --dashboard "%~dp0dashboard.html"
echo.
echo [3/3] 打开统计看板...
start "" "%~dp0dashboard.html"
echo.
echo 看板页面每5分钟自动刷新；后台每2分钟扫描一次并更新数据。
echo 结束后台刷新请运行：关闭TokenWatch.bat
ping -n 4 127.0.0.1 >nul
