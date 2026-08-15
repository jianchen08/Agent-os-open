@echo off
chcp 65001 >nul
REM ============================================================
REM  AgentOS 0.2 Launcher (Windows) - pure 0.2 architecture
REM
REM  Starts two services (frontend directly proxies to 0.2 Rust kernel,
REM  no 0.1 channel_api):
REM    1. kernel  (Rust, :9100) - serves /api/v1/* /ws /metrics
REM    2. frontend (Vite, :6390) - proxies to kernel:9100
REM
REM  Usage:
REM    start_web_02.bat              full start (release build)
REM    start_web_02.bat --no-build   skip kernel build
REM    start_web_02.bat --kernel-only  start kernel only
REM
REM  Env vars:
REM    AGENTOS_KERNEL_PORT   kernel port    (default 9100)
REM    AGENTOS_FRONTEND_PORT frontend port  (default 6390, avoids container_22404's 5289/5290/6290)
REM
REM  [Supervision note / 剩余项清仓批次 A2] Two supervisors may coexist:
REM    1. This script wires run_kernel_supervised.bat (G8 lifecycle supervisor):
REM       respawns kernel ONLY on exit code 75 (restart-as-unload); any other
REM       exit stops the supervisor honestly (crashes are not masked).
REM    2. An external session supervisor .zcode_tmp_kernel_supervisor.sh
REM       (repo root, held by a prior ZCode session background task; writes
REM       .kernel_supervisor.log / .vite_supervised.log) polls port 9100 every
REM       5s and restarts the kernel whenever it is down, plus vite every 10
REM       cycles. It therefore masks honest stops (e.g. the taskkill below and
REM       non-75 exits) - if the kernel "keeps coming back" after stopping it,
REM       that loop is the owner; stop it (kill its bash process / remove the
REM       script) or expect it to re-launch the kernel within ~5s.
REM  ============================================================
setlocal EnableDelayedExpansion

cd /d "%~dp0"
set "PROJECT_ROOT=%cd%"
set "KERNEL_DIR=%PROJECT_ROOT%\kernel"
set "FRONTEND_DIR=%PROJECT_ROOT%\frontend"
set "KERNEL_BIN=%KERNEL_DIR%\target\release\agentos-kernel.exe"

REM parse args
set "NO_BUILD=0"
set "KERNEL_ONLY=0"
:PARSE_ARGS
if "%~1"=="" goto AFTER_ARGS
if /I "%~1"=="--no-build" set "NO_BUILD=1"
if /I "%~1"=="--kernel-only" set "KERNEL_ONLY=1"
shift
goto PARSE_ARGS
:AFTER_ARGS

REM port config
if not defined AGENTOS_KERNEL_PORT set "AGENTOS_KERNEL_PORT=9100"
if not defined AGENTOS_FRONTEND_PORT set "AGENTOS_FRONTEND_PORT=6390"

echo ========================================
echo   AgentOS 0.2 Launcher (pure 0.2)
echo ========================================
echo   kernel:      http://localhost:%AGENTOS_KERNEL_PORT%
if not "%KERNEL_ONLY%"=="1" echo   frontend:    http://localhost:%AGENTOS_FRONTEND_PORT%
echo.

REM ============================================================
REM  Stop old instances (kernel process tree + frontend).
REM ============================================================
echo [CLEAN] Stopping old instances...

taskkill /F /T /IM agentos-kernel.exe >nul 2>&1
taskkill /F /IM node.exe >nul 2>&1

REM give Windows time to release the exe file handle (avoids cargo os error 5)
timeout /t 3 /nobreak >nul
echo [OK] Old instances stopped.
echo.

REM ============================================================
REM  Step 1: build kernel (release)
REM ============================================================
if "%NO_BUILD%"=="1" (
    echo [1/3] Skipping kernel build (--no-build)
) else (
    echo [1/3] Building Rust kernel (release)...
    pushd "%KERNEL_DIR%"
    set "CARGO_INCREMENTAL=0"
    cargo +stable build --release --bin agentos-kernel -j 1
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
REM  Step 2: start kernel
REM ============================================================
echo [2/3] Starting kernel on port :%AGENTOS_KERNEL_PORT%...

set "AGENTOS_KERNEL_HOST=0.0.0.0"
set "AGENTOS_PLUGINS_DIR=%PROJECT_ROOT%\plugins\shared"
set "AGENTOS_CONFIG_ROOT=%PROJECT_ROOT%\config"

set "KERNEL_LOG=%PROJECT_ROOT%\.kernel_02.log"
REM G8 supervisor: respawn kernel on exit code 75 (POST /api/v1/system/restart,
REM and watcher auto-restart on cdylib plugin set change - A3).
REM NOTE (A2): an external session supervisor (.zcode_tmp_kernel_supervisor.sh)
REM may ALSO relaunch the kernel within ~5s of any stop - see header note.
start "AgentOS Kernel" /B cmd /c ""%PROJECT_ROOT%\run_kernel_supervised.bat" "%KERNEL_BIN%" "%KERNEL_LOG%""

echo        Waiting for kernel (poll /health up to 60s)...
set "KERNEL_READY=0"
for /l %%i in (1,1,60) do (
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
    echo [ERROR] Kernel not ready within 60s, aborting.
    echo [HINT] Kernel did not answer /health. Check log: %KERNEL_LOG%
    taskkill /F /T /IM agentos-kernel.exe >nul 2>&1
    pause
    exit /b 1
)
echo.

if "%KERNEL_ONLY%"=="1" (
    echo [SKIP] Kernel-only mode, skipping frontend.
    echo.
    echo ========================================
    echo   Kernel: http://localhost:%AGENTOS_KERNEL_PORT%
    echo ========================================
    pause
    exit /b 0
)

REM ============================================================
REM  Step 3: start frontend (proxy to 0.2 kernel :9100)
REM ============================================================
echo [3/3] Starting frontend on port :%AGENTOS_FRONTEND_PORT%...

REM Check frontend deps really complete (node_modules/.bin/vite.cmd exists).
REM Do not only check node_modules dir existence: it may be empty/incomplete,
REM otherwise npx would fetch vite remotely and pop "Ok to proceed? (y)" prompt,
REM while this script runs non-interactively in background -> blocked till timeout.
if not exist "%FRONTEND_DIR%\node_modules\.bin\vite.cmd" (
    if exist "%FRONTEND_DIR%\node_modules" (
        echo        [INFO] node_modules incomplete, reinstalling frontend deps...
    ) else (
        echo        Installing frontend dependencies...
    )
    pushd "%FRONTEND_DIR%"
    call npm install
    if errorlevel 1 (
        echo [ERROR] npm install failed, frontend cannot start.
        popd
        pause
        exit /b 1
    )
    popd
)

pushd "%FRONTEND_DIR%"
REM --yes: if local vite still missing, npx auto-downloads without prompt.
start "AgentOS Frontend" /B cmd /c "set VITE_PROXY_TARGET=http://localhost:%AGENTOS_KERNEL_PORT%&& npx --yes vite --host 0.0.0.0 --port %AGENTOS_FRONTEND_PORT%"
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
echo   Services started (pure 0.2):
echo   kernel:      http://localhost:%AGENTOS_KERNEL_PORT%
echo   frontend:    http://localhost:%AGENTOS_FRONTEND_PORT%
echo.
echo   Open http://localhost:%AGENTOS_FRONTEND_PORT% in browser.
echo   Frontend proxies directly to 0.2 kernel (no 0.1 channel_api).
echo.
echo   Stop: taskkill /F /IM agentos-kernel.exe ^& taskkill /F /IM node.exe
echo ========================================
echo.
pause
endlocal
