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

echo [INFO] 正在关闭 WSL 内核（让下次启动从干净状态开始）...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wsl_shutdown.ps1" -Timeout 15 >nul 2>&1
echo [OK] WSL 已关闭
echo.
pause
