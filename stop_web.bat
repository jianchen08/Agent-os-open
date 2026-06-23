@echo off
setlocal
title Agent OS - Stop Docker

cd /d "%~dp0"

echo ========================================
echo   Stop Agent OS Docker containers
echo ========================================
echo.

docker compose down
if errorlevel 1 (
    echo [ERROR] Stop failed
    pause
    exit /b 1
)

echo.
echo [OK] Containers stopped
echo.
pause
