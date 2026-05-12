@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Agent OS - 停止服务

cd /d "%~dp0"
set "ROOT=%cd%"
set "PORTS_FILE=%ROOT%\.ports"

echo ========================================
echo   Agent OS Web Channel 停止脚本
echo ========================================
echo.
echo 项目目录: %ROOT%
echo.

set "FOUND=0"
set "BACKEND_PORT="
set "FRONTEND_PORT="

:: ========== 读取 .ports 文件 ==========
if exist "%PORTS_FILE%" (
    echo [INFO] 从 .ports 文件读取端口信息...
    for /f "tokens=1,2 delims==" %%a in (%PORTS_FILE%) do (
        if "%%a"=="BACKEND_PORT" set "BACKEND_PORT=%%b"
        if "%%a"=="FRONTEND_PORT" set "FRONTEND_PORT=%%b"
    )
    if defined BACKEND_PORT echo [INFO] 后端端口: !BACKEND_PORT!
    if defined FRONTEND_PORT echo [INFO] 前端端口: !FRONTEND_PORT!
) else (
    echo [INFO] 未找到 .ports 文件，使用默认端口...
    set "BACKEND_PORT=8888"
    set "FRONTEND_PORT=5188"
)

:: ========== 关闭后端进程 ==========
if defined BACKEND_PORT (
    for /f "tokens=5" %%p in ('netstat -aon ^| findstr ":!BACKEND_PORT! " ^| findstr "LISTENING" 2^>nul') do (
        echo [INFO] 关闭后端进程 PID=%%p ^(端口 !BACKEND_PORT!^)
        taskkill /F /PID %%p >nul 2>&1
        set "FOUND=1"
    )
)

:: ========== 关闭前端进程 ==========
if defined FRONTEND_PORT (
    for /f "tokens=5" %%p in ('netstat -aon ^| findstr ":!FRONTEND_PORT! " ^| findstr "LISTENING" 2^>nul') do (
        echo [INFO] 关闭前端进程 PID=%%p ^(端口 !FRONTEND_PORT!^)
        taskkill /F /PID %%p >nul 2>&1
        set "FOUND=1"
    )
)

:: ========== 等待端口释放 ==========
timeout /t 2 /nobreak >nul

set "STILL_RUNNING=0"
if defined BACKEND_PORT (
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":!BACKEND_PORT! " ^| findstr "LISTENING" 2^>nul') do set "STILL_RUNNING=1"
)
if defined FRONTEND_PORT (
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":!FRONTEND_PORT! " ^| findstr "LISTENING" 2^>nul') do set "STILL_RUNNING=1"
)

if "!STILL_RUNNING!"=="1" (
    echo [WARN] 部分端口仍被占用，强制重试...
    if defined BACKEND_PORT (
        for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":!BACKEND_PORT! " ^| findstr "LISTENING" 2^>nul') do (
            taskkill /F /T /PID %%a >nul 2>&1
        )
    )
    if defined FRONTEND_PORT (
        for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":!FRONTEND_PORT! " ^| findstr "LISTENING" 2^>nul') do (
            taskkill /F /T /PID %%a >nul 2>&1
        )
    )
)

:: ========== 清理 .ports 文件 ==========
if exist "%PORTS_FILE%" del "%PORTS_FILE%" 2>nul

:: ========== 结果 ==========
echo.
if "!FOUND!"=="0" (
    echo [INFO] 没有发现运行中的 Agent OS 服务
) else (
    echo [OK] Agent OS 服务已停止
)
echo.
pause
