@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Agent OS - 停止服务

cd /d "%~dp0"
set "ROOT=%cd%"
set "PORTS_FILE=%ROOT%\.ports"

set "PROJECT_ID="
for /f "delims=" %%h in ('powershell -NoProfile -Command "[System.BitConverter]::ToString([System.Security.Cryptography.MD5]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes('%ROOT%'))).Replace('-','').Substring(0,8).ToLower()"') do set "PROJECT_ID=%%h"

echo ========================================
echo   Agent OS Web Channel 停止脚本
echo ========================================
echo.
echo 项目目录: %ROOT%
echo 项目标识: !PROJECT_ID!
echo.

set "FOUND=0"
set "BACKEND_PORT="
set "FRONTEND_PORT="
set "STORED_BACKEND_PID="
set "STORED_FRONTEND_PID="
set "STORED_PROJECT_ROOT="

:: ========== 读取 .ports 文件 ==========
if not exist "%PORTS_FILE%" (
    echo [INFO] 未找到 .ports 文件，本项目没有运行中的实例
    echo.
    pause
    exit /b 0
)

echo [INFO] 从 .ports 文件读取端口信息...
for /f "tokens=1,2 delims==" %%a in (%PORTS_FILE%) do (
    if "%%a"=="BACKEND_PORT" set "BACKEND_PORT=%%b"
    if "%%a"=="FRONTEND_PORT" set "FRONTEND_PORT=%%b"
    if "%%a"=="PROJECT_ROOT" set "STORED_PROJECT_ROOT=%%b"
    if "%%a"=="PROJECT_ID" (
        if not "%%b"=="!PROJECT_ID!" (
            echo [WARN] .ports 文件中的项目标识不匹配，可能已被其他项目覆盖
        )
    )
    if "%%a"=="BACKEND_PID" set "STORED_BACKEND_PID=%%b"
    if "%%a"=="FRONTEND_PID" set "STORED_FRONTEND_PID=%%b"
)

if defined STORED_PROJECT_ROOT (
    if /i not "!ROOT!"=="!STORED_PROJECT_ROOT!" (
        echo [WARN] .ports 文件属于其他项目目录 [!STORED_PROJECT_ROOT!]，拒绝操作
        echo [INFO] 如需强制停止，请手动删除 %PORTS_FILE%
        echo.
        pause
        exit /b 1
    )
)

if defined BACKEND_PORT echo [INFO] 后端端口: !BACKEND_PORT!
if defined FRONTEND_PORT echo [INFO] 前端端口: !FRONTEND_PORT!
if defined STORED_BACKEND_PID echo [INFO] 后端 PID: !STORED_BACKEND_PID!
if defined STORED_FRONTEND_PID echo [INFO] 前端 PID: !STORED_FRONTEND_PID!

:: ========== 关闭后端进程（带 PID 验证） ==========
if defined BACKEND_PORT (
    for /f "tokens=5" %%p in ('netstat -aon 2^>nul ^| findstr ":!BACKEND_PORT! " ^| findstr "LISTENING"') do (
        if defined STORED_BACKEND_PID (
            if "%%p"=="!STORED_BACKEND_PID!" (
                echo [INFO] 关闭后端进程 PID=%%p ^(端口 !BACKEND_PORT!^)
                taskkill /F /PID %%p >nul 2>&1
                set "FOUND=1"
            ) else (
                echo [WARN] 端口 !BACKEND_PORT! 上的进程已变更（存储PID=!STORED_BACKEND_PID!，当前PID=%%p），跳过关闭以防误杀
            )
        ) else (
            echo [INFO] 关闭后端进程 PID=%%p ^(端口 !BACKEND_PORT!^)
            taskkill /F /PID %%p >nul 2>&1
            set "FOUND=1"
        )
    )
)

:: ========== 关闭前端进程（带 PID 验证） ==========
if defined FRONTEND_PORT (
    for /f "tokens=5" %%p in ('netstat -aon 2^>nul ^| findstr ":!FRONTEND_PORT! " ^| findstr "LISTENING"') do (
        if defined STORED_FRONTEND_PID (
            if "%%p"=="!STORED_FRONTEND_PID!" (
                echo [INFO] 关闭前端进程 PID=%%p ^(端口 !FRONTEND_PORT!^)
                taskkill /F /PID %%p >nul 2>&1
                set "FOUND=1"
            ) else (
                echo [WARN] 端口 !FRONTEND_PORT! 上的进程已变更（存储PID=!STORED_FRONTEND_PID!，当前PID=%%p），跳过关闭以防误杀
            )
        ) else (
            echo [INFO] 关闭前端进程 PID=%%p ^(端口 !FRONTEND_PORT!^)
            taskkill /F /PID %%p >nul 2>&1
            set "FOUND=1"
        )
    )
)

:: ========== 等待端口释放 ==========
timeout /t 2 /nobreak >nul

set "STILL_RUNNING=0"
if defined BACKEND_PORT (
    for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":!BACKEND_PORT! " ^| findstr "LISTENING"') do set "STILL_RUNNING=1"
)
if defined FRONTEND_PORT (
    for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":!FRONTEND_PORT! " ^| findstr "LISTENING"') do set "STILL_RUNNING=1"
)

if "!STILL_RUNNING!"=="1" (
    echo [WARN] 部分端口仍被占用，强制重试...
    if defined BACKEND_PORT (
        for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":!BACKEND_PORT! " ^| findstr "LISTENING"') do (
            taskkill /F /T /PID %%a >nul 2>&1
        )
    )
    if defined FRONTEND_PORT (
        for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":!FRONTEND_PORT! " ^| findstr "LISTENING"') do (
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
