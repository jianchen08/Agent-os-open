@echo off
REM ============================================================
REM  Lingxi AgentOS 0.2 新架构启动脚本 (Windows)
REM
REM  流程：编译内核 (cargo build --release) -> 启动内核二进制 -> 启动前端(Vite, 连接新内核)
REM
REM  用法:
REM    start_web_02.bat              REM 完整启动 (编译 + 内核 + 前端)
REM    start_web_02.bat --no-build   REM 跳过编译, 直接启动
REM    start_web_02.bat --kernel-only REM 仅启动内核, 不启动前端
REM
REM  环境变量:
REM    LINGXI_KERNEL_PORT   内核端口  (默认 9100)
REM    LINGXI_FRONTEND_PORT 前端端口  (默认 5290)
REM
REM  注意: 此脚本须在项目根目录执行, 且依赖:
REM    - Rust 工具链 (cargo)
REM    - Node.js (node / npx)
REM    - curl (用于健康检查)
REM ============================================================

setlocal EnableExtensions EnableDelayedExpansion

REM ---------- 切换到脚本所在目录作为项目根 ----------
cd /d "%~dp0"

REM ---------- 解析参数 ----------
set "NO_BUILD=0"
set "KERNEL_ONLY=0"
:PARSE_ARGS
if "%~1"=="" goto AFTER_ARGS
if /I "%~1"=="--no-build"   set "NO_BUILD=1"
if /I "%~1"=="--kernel-only" set "KERNEL_ONLY=1"
shift
goto PARSE_ARGS
:AFTER_ARGS

REM ---------- 端口配置 ----------
if not defined LINGXI_KERNEL_PORT   set "LINGXI_KERNEL_PORT=9100"
if not defined LINGXI_FRONTEND_PORT set "LINGXI_FRONTEND_PORT=5290"
set "KERNEL_PORT=%LINGXI_KERNEL_PORT%"
set "FRONTEND_PORT=%LINGXI_FRONTEND_PORT%"

REM ---------- 路径配置 ----------
set "PROJECT_ROOT=%cd%"
set "KERNEL_DIR=%PROJECT_ROOT%\kernel"
set "FRONTEND_DIR=%PROJECT_ROOT%\frontend"
set "KERNEL_BIN=%KERNEL_DIR%\target\release\lingxi-kernel.exe"
set "PORTS_FILE=%PROJECT_ROOT%\.ports_02"

echo ========================================
echo   Lingxi AgentOS 0.2 new-arch launcher
echo ========================================
echo.

REM ============================================================
REM  检查工具链
REM ============================================================
echo [CHECK] Verifying toolchain...

where cargo >nul 2>&1
if errorlevel 1 (
    echo [ERROR] cargo not found. Install Rust toolchain first.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('cargo --version') do echo   cargo: %%v

if "%KERNEL_ONLY%"=="0" (
    where node >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Node.js not found. Install Node.js first.
        pause
        exit /b 1
    )
    for /f "delims=" %%v in ('node --version') do echo   node: %%v
)
echo.

REM ============================================================
REM  清理可能存在的旧实例 (.ports_02)
REM ============================================================
if exist "%PORTS_FILE%" (
    for /f "usebackq tokens=1,2 delims==" %%a in ("%PORTS_FILE%") do (
        set "%%a=%%b"
    )
    if defined OLD_KERNEL_PID (
        taskkill /F /PID !OLD_KERNEL_PID! >nul 2>&1
        if not errorlevel 1 echo [CLEAN] Killed old kernel PID !OLD_KERNEL_PID!
    )
    if defined OLD_FRONTEND_PID (
        taskkill /F /PID !OLD_FRONTEND_PID! >nul 2>&1
        if not errorlevel 1 echo [CLEAN] Killed old frontend PID !OLD_FRONTEND_PID!
    )
    del /f /q "%PORTS_FILE%" >nul 2>&1
)

set "KERNEL_PID="
set "FRONTEND_PID="

REM ============================================================
REM  步骤 1: 编译内核
REM ============================================================
if "%NO_BUILD%"=="1" (
    echo [SKIP] Skipping kernel build (--no-build).
) else (
    echo [1/3] Building Rust kernel (cargo build --release)...
    echo        First build may take several minutes; incremental builds ~30s.
    pushd "%KERNEL_DIR%"
    cargo build --release --bin lingxi-kernel
    set "BUILD_ERR=!errorlevel!"
    popd
    if not "!BUILD_ERR!"=="0" (
        echo [ERROR] Kernel build failed.
        pause
        exit /b 1
    )
    echo [OK] Kernel build succeeded.
)

REM 验证二进制存在
if not exist "%KERNEL_BIN%" (
    echo [ERROR] Kernel binary not found: %KERNEL_BIN%
    echo         Re-run without --no-build.
    pause
    exit /b 1
)
echo   kernel binary: %KERNEL_BIN%
echo.

REM ============================================================
REM  步骤 2: 启动内核
REM ============================================================
echo [2/3] Starting Rust kernel on port :%KERNEL_PORT%...

set "LINGXI_KERNEL_PORT=%KERNEL_PORT%"
set "LINGXI_KERNEL_HOST=0.0.0.0"

REM 后台启动内核, 单独窗口便于查看日志, 同时保留 PID 用于清理
set "KERNEL_LOG=%PROJECT_ROOT%\.kernel_02.log"
start "Lingxi Kernel" /B cmd /c ""%KERNEL_BIN%" > "%KERNEL_LOG%" 2>&1"
REM 通过 wmic 获取最近启动的 lingxi-kernel.exe PID (Windows 10/11 可用)
set "KERNEL_PID="
for /f "skip=1 tokens=1" %%p in ('wmic process where "name='lingxi-kernel.exe'" get ProcessId 2^>nul ^| findstr /R "[0-9]"') do (
    if not defined KERNEL_PID set "KERNEL_PID=%%p"
)

if not defined KERNEL_PID (
    echo [WARN] Could not determine kernel PID via wmic; relying on process kill by image name on exit.
)

REM 等待内核就绪 (15 秒)
echo        Waiting for kernel...
set "KERNEL_READY=0"
for /l %%i in (1,1,15) do (
    curl -s -o nul -w "%%{http_code}" "http://localhost:%KERNEL_PORT%/health" 2>nul | findstr /R "200" >nul
    if not errorlevel 1 (
        set "KERNEL_READY=1"
        for /f "delims=" %%h in ('curl -s "http://localhost:%KERNEL_PORT%/health" 2^>nul') do (
            echo [OK] Kernel ready (http://localhost:%KERNEL_PORT%)  Health: %%h
        )
        goto :KERNEL_READY_OK
    )
    timeout /t 1 /nobreak >nul
)

:KERNEL_READY_OK
if "%KERNEL_READY%"=="0" (
    echo [ERROR] Kernel did not become ready within 15s.
    if defined KERNEL_PID taskkill /F /PID %KERNEL_PID% >nul 2>&1
    pause
    exit /b 1
)

REM 写端口/PID 文件, 供后续清理使用
>  "%PORTS_FILE%" echo OLD_KERNEL_PID=%KERNEL_PID%
>> "%PORTS_FILE%" echo OLD_KERNEL_PORT=%KERNEL_PORT%
>> "%PORTS_FILE%" echo OLD_FRONTEND_PORT=%FRONTEND_PORT%

REM ============================================================
REM  步骤 3: 启动前端
REM ============================================================
if "%KERNEL_ONLY%"=="1" (
    echo [SKIP] Kernel-only mode (--kernel-only); frontend not started.
) else (
    echo.
    echo [3/3] Starting frontend (Vite :%FRONTEND_PORT%, talking to kernel :%KERNEL_PORT%)...

    REM 安装前端依赖 (如未安装)
    if not exist "%FRONTEND_DIR%\node_modules" (
        echo        Installing frontend dependencies...
        pushd "%FRONTEND_DIR%"
        call npm install
        set "NPM_ERR=!errorlevel!"
        popd
        if not "!NPM_ERR!"=="0" (
            echo [ERROR] npm install failed.
            pause
            exit /b 1
        )
    )

    REM 启动 Vite, 通过 VITE_API_BASE_URL 指向内核
    pushd "%FRONTEND_DIR%"
    set "VITE_API_BASE_URL=http://localhost:%KERNEL_PORT%"
    start "Lingxi Frontend" /B cmd /c "set VITE_API_BASE_URL=http://localhost:%KERNEL_PORT%&& npx vite --host 0.0.0.0 --port %FRONTEND_PORT%"
    popd

    REM 获取前端 PID
    timeout /t 1 /nobreak >nul
    set "FRONTEND_PID="
    for /f "skip=1 tokens=1" %%p in ('wmic process where "name='node.exe' and CommandLine like '%%vite%%'" get ProcessId 2^>nul ^| findstr /R "[0-9]"') do (
        if not defined FRONTEND_PID set "FRONTEND_PID=%%p"
    )

    REM 等待前端就绪 (30 秒)
    echo        Waiting for frontend...
    set "FRONTEND_READY=0"
    for /l %%i in (1,1,30) do (
        curl -s -o nul -w "%%{http_code}" "http://localhost:%FRONTEND_PORT%" 2>nul | findstr /R "200" >nul
        if not errorlevel 1 (
            set "FRONTEND_READY=1"
            echo [OK] Frontend ready (http://localhost:%FRONTEND_PORT%)
            goto :FRONTEND_READY_OK
        )
        timeout /t 1 /nobreak >nul
    )

:FRONTEND_READY_OK
    if "%FRONTEND_READY%"=="0" (
        echo [WARN] Frontend did not become ready within 30s; service may still be starting.
    )

    >> "%PORTS_FILE%" echo OLD_FRONTEND_PID=%FRONTEND_PID%
)

REM ============================================================
REM  输出信息
REM ============================================================
echo.
echo ========================================
echo   Services started:
echo   Kernel (Rust):  http://localhost:%KERNEL_PORT%
echo   Health:         http://localhost:%KERNEL_PORT%/health
echo   Schema API:     http://localhost:%KERNEL_PORT%/api/v1/schema
echo   WebSocket:      ws://localhost:%KERNEL_PORT%/ws
if "%KERNEL_ONLY%"=="0" (
    echo   Frontend:      http://localhost:%FRONTEND_PORT%
)
echo.
if defined KERNEL_PID   echo   Kernel PID:    %KERNEL_PID%
if defined FRONTEND_PID echo   Frontend PID:  %FRONTEND_PID%
echo   Ports file:     %PORTS_FILE%
echo   Stop:           taskkill /F /IM lingxi-kernel.exe ^& taskkill /F /IM node.exe
echo                   (or close the Lingxi Kernel / Lingxi Frontend windows)
echo ========================================

REM 端点验证 (内核)
echo.
echo [VERIFY] Endpoint check:
for %%e in (/health /api/v1/schema /api/v1/agents /api/v1/pipelines /api/v1/tools) do (
    for /f "delims=" %%c in ('curl -s -o nul -w "%%{http_code}" "http://localhost:%KERNEL_PORT%%%e" 2^>nul') do (
        echo %%c | findstr /R "200" >nul
        if not errorlevel 1 (
            echo   ^ OK  GET %%e -^> %%c
        ) else (
            echo   X FAIL GET %%e -^> %%c
        )
    )
)

for /f "delims=" %%c in ('curl -s -o nul -w "%%{http_code}" -X POST "http://localhost:%KERNEL_PORT%/api/v1/chat" -H "Content-Type: application/json" -d "{\"message\":\"test\",\"session_id\":\"verify\"}" 2^>nul') do (
    echo %%c | findstr /R "200" >nul
    if not errorlevel 1 (
        echo   ^ OK  POST /api/v1/chat -^> %%c
    ) else (
        echo   X FAIL POST /api/v1/chat -^> %%c
    )
)

echo.
echo [OK] All checks complete. Closing launcher in 10s (or press a key).
timeout /t 10 /nobreak >nul
endlocal
exit /b 0