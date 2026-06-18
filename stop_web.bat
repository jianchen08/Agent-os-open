@echo off
setlocal
chcp 65001 >nul 2>&1
title Agent OS - Stop Docker

cd /d "%~dp0"

echo ========================================
echo   停止 Agent OS Docker 容器
echo ========================================
echo.

docker compose down
if errorlevel 1 (
    echo [ERROR] 停止失败
    pause
    exit /b 1
)

echo.
echo [OK] 容器已停止
echo.
pause
