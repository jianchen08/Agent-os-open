@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Agent OS - 停止服务

cd /d "%~dp0"

echo ========================================
echo   Agent OS Web Channel 停止脚本
echo ========================================
echo.

:: ========== 关闭窗口标题匹配的进程 ==========
echo [INFO] 查找 Agent OS 服务进程...

set "FOUND=0"

:: 关闭 Agent OS Backend 窗口
tasklist /FI "WINDOWTITLE eq Agent OS Backend*" /NH 2>nul | findstr /I "cmd python" >nul 2>&1
if !errorlevel! == 0 (
    echo [INFO] 关闭后端窗口...
    taskkill /F /FI "WINDOWTITLE eq Agent OS Backend*" >nul 2>&1
    set "FOUND=1"
)

:: 关闭 Agent OS Frontend 窗口
tasklist /FI "WINDOWTITLE eq Agent OS Frontend*" /NH 2>nul | findstr /I "cmd node" >nul 2>&1
if !errorlevel! == 0 (
    echo [INFO] 关闭前端窗口...
    taskkill /F /FI "WINDOWTITLE eq Agent OS Frontend*" >nul 2>&1
    set "FOUND=1"
)

:: ========== 按端口关闭 ==========
set "BACKEND_PORT=8888"
set "FRONTEND_PORT=5188"

:: 杀 8888 端口进程
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%BACKEND_PORT% " ^| findstr "LISTENING" 2^>nul') do (
    echo [INFO] 关闭后端进程 PID=%%a ^(端口 %BACKEND_PORT%^)
    taskkill /F /PID %%a >nul 2>&1
    set "FOUND=1"
)

:: 杀 5188 端口进程
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%FRONTEND_PORT% " ^| findstr "LISTENING" 2^>nul') do (
    echo [INFO] 关闭前端进程 PID=%%a ^(端口 %FRONTEND_PORT%^)
    taskkill /F /PID %%a >nul 2>&1
    set "FOUND=1"
)

:: ========== 等待端口释放 ==========
timeout /t 2 /nobreak >nul

set "STILL_RUNNING=0"
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%BACKEND_PORT% " ^| findstr "LISTENING" 2^>nul') do set "STILL_RUNNING=1"
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%FRONTEND_PORT% " ^| findstr "LISTENING" 2^>nul') do set "STILL_RUNNING=1"

if "!STILL_RUNNING!"=="1" (
    echo [WARN] 部分端口仍被占用，强制重试...
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%BACKEND_PORT% " ^| findstr "LISTENING" 2^>nul') do (
        taskkill /F /T /PID %%a >nul 2>&1
    )
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%FRONTEND_PORT% " ^| findstr "LISTENING" 2^>nul') do (
        taskkill /F /T /PID %%a >nul 2>&1
    )
)

:: ========== 结果 ==========
echo.
if "!FOUND!"=="0" (
    echo [INFO] 没有发现运行中的 Agent OS 服务
) else (
    echo [OK] Agent OS 服务已停止
)
echo.
pause
