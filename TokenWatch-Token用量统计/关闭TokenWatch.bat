@echo off
chcp 936 >nul
title TokenWatch - 关闭
echo ============================================
echo    TokenWatch  正在关闭相关进程...
echo ============================================
echo.
powershell -NoProfile -Command "$ps = Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -like '*token_watch.py*' }; if ($ps) { $ps | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Write-Host ('已结束 ' + $ps.Count + ' 个 TokenWatch 进程') } else { Write-Host '没有发现正在运行的 TokenWatch 进程' }"
echo.
echo 若浏览器中仍打开着统计看板，请手动关闭该标签页。
echo.
ping -n 3 127.0.0.1 >nul
