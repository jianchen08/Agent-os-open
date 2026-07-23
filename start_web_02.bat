@echo off
REM ============================================================
REM  AgentOS 0.2 启动脚本 (Windows) — 方案B
REM
REM  启动三个服务：
REM    1. channel_api (Python FastAPI, :8988) — 提供全部 /api/v1/* 端点
REM    2. 内核 (Rust, :9100) — 管道引擎，被 channel_api 通过 MCP 调用
REM    3. 前端 (Vite, :5290) — 连 channel_api:8988
REM
REM  用法:
REM    start_web_02.bat              完整启动
REM    start_web_02.bat --no-build   跳过内核编译
REM    start_web_02.bat --kernel-only 仅启动内核
REM
REM  环境变量:
REM    AGENTOS_KERNEL_PORT   内核端口  (默认 9100)
REM    AGENTOS_FRONTEND_PORT 前端端口  (默认 5290)
REM    BACKEND_PORT          channel_api端口 (默认 8988)
REM ============================================================
setlocal EnableDelayedExpansion

cd /d "%~dp0"
set "PROJECT_ROOT=%cd%"
set "KERNEL_DIR=%PROJECT_ROOT%\kernel"
set "FRONTEND_DIR=%PROJECT_ROOT%\frontend"
set "CHANNEL_API_DIR=%PROJECT_ROOT%\plugins\shared\system\channel_api"
set "KERNEL_BIN=%KERNEL_DIR%\target\debug\agentos-kernel.exe"

REM 解析参数
set "NO_BUILD=0"
set "KERNEL_ONLY=0"
:PARSE_ARGS
if "%~1"=="" goto AFTER_ARGS
if /I "%~1"=="--no-build" set "NO_BUILD=1"
if /I "%~1"=="--kernel-only" set "KERNEL_ONLY=1"
shift
goto PARSE_ARGS
:AFTER_ARGS

REM 端口配置
if not defined BACKEND_PORT set "BACKEND_PORT=8988"
if not defined AGENTOS_KERNEL_PORT set "AGENTOS_KERNEL_PORT=9100"
if not defined AGENTOS_FRONTEND_PORT set "AGENTOS_FRONTEND_PORT=5290"

echo ========================================
echo   AgentOS 0.2 Launcher (Plan B)
echo ========================================
echo   channel_api: http://localhost:%BACKEND_PORT%
echo   kernel:      http://localhost:%AGENTOS_KERNEL_PORT%
if not "%KERNEL_ONLY%"=="1" echo   frontend:    http://localhost:%AGENTOS_FRONTEND_PORT%
echo.

REM ============================================================
REM  清理旧实例（杀内核进程树 + channel_api + 前端）
REM ============================================================
echo [CLEAN] Stopping old instances...

REM 杀内核及其所有子进程（/T 级联杀 sidecar 孤儿）
taskkill /F /T /IM agentos-kernel.exe >nul 2>&1

REM 杀 channel_api（python run_server.py）
taskkill /F /IM python.exe >nul 2>&1

REM 杀前端（vite）
taskkill /F /IM node.exe >nul 2>&1

timeout /t 2 /nobreak >nul
echo [OK] Old instances stopped.
echo.

REM ============================================================
REM  步骤 1: 编译内核
REM ============================================================
if "%NO_BUILD%"=="1" (
    echo [1/4] Skipping kernel build (--no-build)
) else (
    echo [1/4] Building Rust kernel...
    pushd "%KERNEL_DIR%"
    set "CARGO_INCREMENTAL=0"
    cargo +stable build --bin agentos-kernel -j 1
    if errorlevel 1 (
        echo [ERROR] Kernel build failed.
        popd
        pause
        exit /b 1
    )
    popd
    echo [OK] Kernel build succeeded.
)
echo.

REM ============================================================
REM  步骤 2: 启动内核
REM ============================================================
echo [2/4] Starting kernel on port :%AGENTOS_KERNEL_PORT%...

set "AGENTOS_KERNEL_HOST=0.0.0.0"
set "AGENTOS_PLUGINS_DIR=%PROJECT_ROOT%\plugins\shared"
set "AGENTOS_CONFIG_ROOT=%PROJECT_ROOT%\config"

set "KERNEL_LOG=%PROJECT_ROOT%\.kernel_02.log"
start "AgentOS Kernel" /B cmd /c ""%KERNEL_BIN%" > "%KERNEL_LOG%" 2>&1"

echo        Waiting for kernel...
set "KERNEL_READY=0"
for /l %%i in (1,1,15) do (
    if "!KERNEL_READY!"=="0" (
        curl -s -o nul -w "%%{http_code}" "http://localhost:%AGENTOS_KERNEL_PORT%/health" 2>nul | findstr "200" >nul
        if not errorlevel 1 (
            set "KERNEL_READY=1"
            echo [OK] Kernel ready.
        ) else (
            timeout /t 1 /nobreak >nul
        )
    )
)
if "!KERNEL_READY!"=="0" (
    echo [WARN] Kernel not ready within 15s, continuing anyway.
)
echo.

if "%KERNEL_ONLY%"=="1" (
    echo [SKIP] Kernel-only mode, skipping channel_api and frontend.
    echo.
    echo ========================================
    echo   Kernel: http://localhost:%AGENTOS_KERNEL_PORT%
    echo ========================================
    pause
    exit /b 0
)

REM ============================================================
REM  步骤 3: 启动 channel_api
REM ============================================================
echo [3/4] Starting channel_api on port :%BACKEND_PORT%...

set "BACKEND_PORT=%BACKEND_PORT%"
pushd "%CHANNEL_API_DIR%"
start "AgentOS channel_api" /B python run_server.py --port %BACKEND_PORT%
popd

echo        Waiting for channel_api...
set "API_READY=0"
for /l %%i in (1,1,15) do (
    if "!API_READY!"=="0" (
        curl -s -o nul -w "%%{http_code}" "http://localhost:%BACKEND_PORT%/health" 2>nul | findstr "200" >nul
        if not errorlevel 1 (
            set "API_READY=1"
            echo [OK] channel_api ready.
        ) else (
            timeout /t 1 /nobreak >nul
        )
    )
)
if "!API_READY!"=="0" echo [WARN] channel_api not ready within 15s.
echo.

REM ============================================================
REM  步骤 4: 启动前端
REM ============================================================
echo [4/4] Starting frontend on port :%AGENTOS_FRONTEND_PORT%...

if not exist "%FRONTEND_DIR%\node_modules" (
    echo        Installing frontend dependencies...
    pushd "%FRONTEND_DIR%"
    call npm install
    popd
)

pushd "%FRONTEND_DIR%"
start "AgentOS Frontend" /B cmd /c "set VITE_PROXY_TARGET=http://localhost:%BACKEND_PORT%&& npx vite --host 0.0.0.0 --port %AGENTOS_FRONTEND_PORT%"
popd

echo        Waiting for frontend...
set "FRONTEND_READY=0"
for /l %%i in (1,1,30) do (
    if "!FRONTEND_READY!"=="0" (
        curl -s -o nul -w "%%{http_code}" "http://localhost:%AGENTOS_FRONTEND_PORT%" 2>nul | findstr "200" >nul
        if not errorlevel 1 (
            set "FRONTEND_READY=1"
            echo [OK] Frontend ready.
        ) else (
            timeout /t 1 /nobreak >nul
        )
    )
)
if "!FRONTEND_READY!"=="0" echo [WARN] Frontend not ready within 30s.
echo.

REM ============================================================
echo ========================================
echo   Services started:
echo   channel_api: http://localhost:%BACKEND_PORT%
echo   kernel:      http://localhost:%AGENTOS_KERNEL_PORT%
echo   frontend:    http://localhost:%AGENTOS_FRONTEND_PORT%
echo.
echo   Open http://localhost:%AGENTOS_FRONTEND_PORT% in browser.
echo.
echo   Stop: taskkill /F /IM agentos-kernel.exe ^& taskkill /F /IM python.exe ^& taskkill /F /IM node.exe
echo ========================================
echo.
pause
endlocal
