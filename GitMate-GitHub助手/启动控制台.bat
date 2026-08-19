@echo off
chcp 936 >nul
title GitMate GitHub 图形助手
cd /d "%~dp0"
echo ============================================
echo    GitMate GitHub 图形助手 - 启动中...
echo ============================================
where pythonw >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 pythonw，请先安装 Python 3。
    pause
    exit /b 1
)
rem [single-instance] stop previous instance and free ports first
powershell -NoProfile -Command "$pidFile='data\server.pid'; $p=Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue; if($p){Stop-Process -Id ([int]$p) -Force -ErrorAction SilentlyContinue}; Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue; 9650..9659 | ForEach-Object { Get-NetTCPConnection -State Listen -LocalPort $_ -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess } | Sort-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 300"
start "" pythonw "%~dp0git_mate.py" --no-browser %*
timeout /t 3 /nobreak >nul
set OK=0
set OKPORT=
for /L %%p in (9650,1,9659) do (
    curl.exe -s --max-time 2 "http://127.0.0.1:%%p/api/health" >nul 2>nul
    if not errorlevel 1 (
        set OK=1
        set OKPORT=%%p
        goto :found
    )
)
:found
if "%OK%"=="1" (
    start "" "http://127.0.0.1:%OKPORT%/"
    echo 控制台已启动并自动打开，端口 %OKPORT%。
) else (
    echo [提示] 未检测到服务响应，请查看 data\logs\console.log
)
timeout /t 3 /nobreak >nul
