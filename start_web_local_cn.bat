@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Agent OS - Local (no Docker)

cd /d "%~dp0"

echo ========================================
echo   Agent OS 本地启动（无需 Docker）
echo ========================================
echo.
echo 项目目录: %cd%
echo.

:: ===========================================================================
:: 1. 检测 Python（优先 3.11-3.13，避免 3.14 asyncio subprocess bug）
:: ===========================================================================
set "PYEXE="
for %%v in (311 312 313) do (
    for /f "delims=" %%p in ('where python%%v 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%p"
    )
)
:: 没有指定小版本时回退到默认 python（可能是 3.14）
if not defined PYEXE (
    where python >nul 2>&1
    if not errorlevel 1 for /f "delims=" %%p in ('where python') do (
        if not defined PYEXE set "PYEXE=%%p"
    )
)
if not defined PYEXE (
    echo [ERROR] 未找到 Python，请安装 Python 3.11+
    pause
    exit /b 1
)
echo [OK] Python: %PYEXE%
"%PYEXE%" --version 2>&1
echo.

:: ===========================================================================
:: 2. 检测 Node.js（前端 dev server 需要）
:: ===========================================================================
where node >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未找到 Node.js，本地启动需要 Node.js 运行前端
    echo [INFO]  下载: https://nodejs.org/
    pause
    exit /b 1
)
echo [OK] Node:
node --version
echo.

:: ===========================================================================
:: 3. Python 依赖
:: ===========================================================================
if not exist ".py_deps_installed" (
    echo [INFO] 安装 Python 依赖...
    "%PYEXE%" -m pip install -r requirements.txt 2>nul
    if errorlevel 1 "%PYEXE%" -m pip install -r requirements.txt --user 2>nul
    echo. > ".py_deps_installed"
    echo [OK] Python 依赖安装完成
) else (
    echo [OK] Python 依赖已安装
)
echo.

:: ===========================================================================
:: 4. 前端依赖
:: ===========================================================================
if not exist "frontend\node_modules" (
    echo [INFO] 安装前端依赖（首次较慢）...
    pushd frontend
    call npm install
    popd
    echo [OK] 前端依赖安装完成
) else (
    echo [OK] 前端依赖已安装
)
echo.

:: ===========================================================================
:: 5. 启动后端（端口 8888，内存模式降级，无需 Redis）
:: ===========================================================================
echo [INFO] 启动后端（端口 8888，不依赖 Redis，自动内存模式）...
start "Agent OS Backend (local)" /D "%cd%" cmd /k "set PYTHONPATH=src&& "%PYEXE%" -m channels.websocket.app_factory"

:: ===========================================================================
:: 6. 启动前端（vite dev server，端口 5189，proxy 到 8888）
:: ===========================================================================
echo [INFO] 启动前端（vite dev server，端口 5189）...
start "Agent OS Frontend (local)" /D "%cd%\frontend" cmd /k "set VITE_API_BASE_URL=http://localhost:8888&& set VITE_WS_BASE_URL=ws://localhost:8888&& npm run dev"

echo.
echo ========================================
echo   启动完成
echo ========================================
echo   后端: http://localhost:8888
echo   前端: http://localhost:5189
echo   模式: 本地（无 Docker / Redis 内存降级）
echo   停止: 关闭弹出的两个窗口
echo ========================================
echo.
echo [注意] 后端窗口出现 "Application startup complete" 后再访问前端
echo [注意] 首次访问较慢（后端需初始化管道引擎）
echo.
pause
