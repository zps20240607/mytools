@echo off
chcp 936 >nul
title CacheWatch 磁盘瘦身
cd /d "%~dp0"
echo ============================================
echo    CacheWatch 磁盘瘦身 - 启动中...
echo ============================================
where pythonw >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 pythonw，请先安装 Python 3。
    pause
    exit /b 1
)
rem [single-instance] stop previous instance and free ports first
powershell -NoProfile -Command "$pidFile='data\server.pid'; if(Test-Path -LiteralPath $pidFile){$p=Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue; if($p -match '^\d+$'){ $proc=Get-Process -Id ([int]$p) -ErrorAction SilentlyContinue; if($proc -and $proc.ProcessName -match '^python'){ Stop-Process -Id ([int]$p) -Force -ErrorAction SilentlyContinue } }; Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 300"
start "" pythonw "%~dp0cache_watch.py" --no-browser %*
timeout /t 3 /nobreak >nul
set OK=0
set OKPORT=
for /L %%p in (9630,1,9639) do (
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
