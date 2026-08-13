@echo off
chcp 936 >nul
title EnvWatch 环境体检台
cd /d "%~dp0"
echo ============================================
echo    EnvWatch 环境体检台 - 生成体检报告...
echo ============================================
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3。
    pause
    exit /b 1
)
python "%~dp0env_watch.py"
echo.
echo 报告已保存到 data\report.html，如需重新生成直接再运行本脚本。
pause
