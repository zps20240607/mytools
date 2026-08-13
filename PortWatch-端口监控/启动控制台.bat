@echo off
chcp 936 >nul
title PortWatch 端口监控台
cd /d "%~dp0"
echo ============================================
echo    PortWatch 端口监控台 - 正在启动...
echo ============================================
where pythonw >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 pythonw，请先安装 Python 3。
    pause
    exit /b 1
)
rem 后台启动服务（浏览器由本脚本负责打开，服务端不弹窗）
start "" pythonw "%~dp0server.py" --no-browser %*
timeout /t 3 /nobreak >nul
set OK=0
set OKPORT=
for /L %%p in (9600,1,9609) do (
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
    echo 控制台已启动，浏览器已打开（端口 %OKPORT%）。
) else (
    echo [提示] 未检测到服务响应，请查看 data\logs\console.log
)
timeout /t 3 /nobreak >nul
