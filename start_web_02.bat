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
REM    AGENTOS_USER_ROOT     user space root; pinned to <project>\user_root below
REM                          (dev isolation from the installed app's OS-default
REM                          space, ADR 2026-09-18-dev-local-user-root)
REM
REM  [Supervision note] The kernel is supervised by run_kernel_supervised.bat
REM  (G8 lifecycle supervisor, wired in step 3 below): exit code 75
REM  (restart-as-unload) respawns after 1s with no counting; any other exit
REM  code auto-respawns with exponential backoff (5s/15s/45s cap) and stops
REM  after AGENTOS_SUPERVISOR_MAX_CONSECUTIVE_FAILS consecutive failures
REM  (default 5, circuit break); a run of at least
REM  AGENTOS_SUPERVISOR_STABLE_SECS (default 60s) resets the failure streak.
REM  The supervisor and the kernel write no log file of their own: kernel
REM  diagnostics live in the kernel's rotating file layer under logs\
REM  (kernel.log.YYYY-MM-DD). The former .kernel_02.log stdout redirect and
REM  .kernel_supervisor.log event log are retired as unbounded append-only
REM  surfaces. The former external session supervisor
REM  (.zcode_tmp_kernel_supervisor.sh, a prior ZCode session's background
REM  task) is retired and gone: unexpected-death recovery is owned by the
REM  supervisor loop itself.
REM  ============================================================
setlocal EnableDelayedExpansion

cd /d "%~dp0"
set "PROJECT_ROOT=%cd%"

REM 2026-09-07 fix: when run from Git Bash, GNU coreutils in Git's PATH shadow
REM Windows commands -- `timeout /t 1` hits GNU syntax and fails with "invalid
REM time interval '/t'", so the health-poll loop spins instantly and aborts
REM before the kernel is up. Prepend System32 so native commands win; behavior
REM is identical for double-click and Git Bash entry points.
REM NOTE: keep comments in this file ASCII-only -- cmd parses the batch in a
REM legacy codepage and non-ASCII text can be misread into executable fragments.
set "PATH=%SystemRoot%\System32;%SystemRoot%;%PATH%"
set "KERNEL_DIR=%PROJECT_ROOT%\kernel"
set "FRONTEND_DIR=%PROJECT_ROOT%\frontend"
set "KERNEL_BIN=%KERNEL_DIR%\target\release\agentos-kernel.exe"

REM 2026-09-18 (ADR 2026-09-18-dev-local-user-root): pin the dev user space
REM root INSIDE the project (gitignored user_root\) so all dev-owned assets
REM (plugins/config/data/.env) live together and never touch the installed
REM app's OS-default space (%APPDATA%\agentos). The packaged app does not
REM inherit this variable and keeps the OS default. Set before every step
REM that resolves user assets (env guard, kernel spawn). Delete this line
REM to fall back to the OS default.
set "AGENTOS_USER_ROOT=%PROJECT_ROOT%\user_root"

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
REM  Port-targeted kill: find PIDs LISTENING on OUR ports via
REM  netstat -ano, then taskkill /F /T /PID (tree kill).
REM  The old version carpet-bombed ALL node.exe / agentos-kernel.exe
REM  on the machine - killing unrelated projects' processes.
REM ============================================================
echo [CLEAN] Stopping old instances (port-targeted)...

REM Kill leftover G8 supervisor cmd trees from previous launches FIRST.
REM The supervisor respawns the kernel on any exit (5s/15s/45s backoff),
REM so port/image kills alone lose the race: it relaunches the very exe
REM cargo is about to replace minutes later at link time, and the build
REM dies with os error 5 (2026-09-11, 2026-09-14). The tree kill also
REM takes the currently supervised kernel and its sidecars in one shot.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*run_kernel_supervised.bat*' } | ForEach-Object { Write-Host ('       [CLEAN] killing leftover supervisor tree PID ' + $_.ProcessId); taskkill /F /T /PID $_.ProcessId 2>&1 | Out-Null }"

call :KillPort "%AGENTOS_KERNEL_PORT%" "kernel"
call :KillPort "%AGENTOS_FRONTEND_PORT%" "frontend"

REM Image-name fallback: agentos-kernel.exe is a product-unique image, so
REM killing by name cannot hit unrelated projects (the old carpet-bomb problem
REM was node.exe). Port scan alone misses instances bound to other ports
REM (manual/debug runs with AGENTOS_KERNEL_PORT override).
tasklist /FI "IMAGENAME eq agentos-kernel.exe" 2>nul | findstr /I "agentos-kernel" >nul 2>&1
if not errorlevel 1 (
    echo        [CLEAN] killing lingering agentos-kernel.exe by image name
    taskkill /F /IM agentos-kernel.exe >nul 2>&1
)

REM Wait until the exe is actually replaceable, not a blind 3s sleep:
REM after taskkill the image handle can linger a few seconds (AV scan / WER),
REM and a still-running G8 supervisor from a previous launch may even
REM re-launch it via its exit-75 path (see note above).
REM A rename round-trip proves the lock is really gone before cargo touches it.
call :WaitExeUnlock "%KERNEL_BIN%"
echo [OK] Old instances stopped.
echo.

REM ============================================================
REM  Step 1: build kernel (release)
REM ============================================================
if "%NO_BUILD%"=="1" (
    echo [1/4] Skipping kernel build (--no-build)
) else (
    echo [1/4] Building Rust kernel (release)...
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

REM Async artifact sweep: cargo never deletes stale fingerprint copies, so
REM every release build leaves the previous set behind (~19GB/month observed).
REM Just-built artifacts are the newest hash cluster and are never touched;
REM older clusters older than 3 days get reclaimed in the background. Fire
REM and forget: the sweep always exits 0 and skips in-use files.
start "artifact-sweep" /b python "%PROJECT_ROOT%\scripts\clean_rust_debug.py" --older-than 3 --with-release

REM Same-origin guard: native cdylibs built from sources other than the
REM kernel make tool dispatch sites SIGSEGV (proven twice, 2026-09-01/08-31).
REM --build re-auto-builds missing/stale cdylibs first (a fresh clone gets
REM its artifacts from this step on first run); abort only if it still fails.
echo [1.5/4] Syncing native cdylibs with kernel (--build)...
python "%PROJECT_ROOT%\scripts\check_native_artifacts_sync.py" --build
if errorlevel 1 (
    echo [ERROR] native cdylib 自动重编失败或内核 exe 过期，按上方指引处理后重试。
    pause
    exit /b 1
)
echo.

REM D4 env integrity guard (2026-09-14): a gutted .env made every LLM call
REM return empty (all tasks fake-green in minutes). Snapshot healthy keys to
REM .env.backup and self-heal missing keys from it; abort only if .env exists
REM but is unparsable with no usable backup.
echo [1.6/4] Env health check (.env integrity)...
python "%PROJECT_ROOT%\scripts\check_env_health.py" --fix
if errorlevel 1 (
    echo [ERROR] .env 损坏且无法自愈（所有 LLM 调用将空返回），按上方指引处理后重试。
    pause
    exit /b 1
)
echo.

REM ============================================================
REM  Step 2: prepare plugin venvs (first run only; skip dirs with .venv)
REM  Plugins run in per-directory uv venvs - the kernel refuses to fall
REM  back to a bare PATH python, so a fresh clone without this step gets
REM  every Python sidecar down (502 on plugin endpoints).
REM ============================================================
echo [2/4] Preparing Python plugin venvs...
where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] uv not found in PATH.
    echo         Plugins need per-directory venvs created by uv.
    echo         Install: https://docs.astral.sh/uv/  then re-run.
    pause
    exit /b 1
)
set "VENV_CREATED=0"
REM Three plugin dir layouts: system|tools/<name> (two levels),
REM pipeline/<phase>/<name> (three levels), shared/<name> top level
REM (db_admin etc.); uv sync only when .venv missing (idempotent skip).
for %%A in (system tools) do (
    for /d %%B in ("%PROJECT_ROOT%\plugins\shared\%%A\*") do (
        if exist "%%B\plugin.json" if exist "%%B\pyproject.toml" if not exist "%%B\.venv" (
            echo        uv sync: %%~nB
            uv sync --project "%%B" >nul 2>&1
            if errorlevel 1 (
                echo [WARN] uv sync failed: %%B ^(see plugin uv.lock/pyproject^)
            ) else (
                set /a VENV_CREATED+=1
            )
        )
    )
)
for /d %%A in ("%PROJECT_ROOT%\plugins\shared\pipeline\*") do (
    for /d %%B in ("%%A\*") do (
        if exist "%%B\plugin.json" if exist "%%B\pyproject.toml" if not exist "%%B\.venv" (
            echo        uv sync: %%~nB
            uv sync --project "%%B" >nul 2>&1
            if errorlevel 1 (
                echo [WARN] uv sync failed: %%B ^(see plugin uv.lock/pyproject^)
            ) else (
                set /a VENV_CREATED+=1
            )
        )
    )
)
for /d %%B in ("%PROJECT_ROOT%\plugins\shared\*") do (
    if exist "%%B\plugin.json" if exist "%%B\pyproject.toml" if not exist "%%B\.venv" (
        echo        uv sync: %%~nB
        uv sync --project "%%B" >nul 2>&1
        if errorlevel 1 (
            echo [WARN] uv sync failed: %%B ^(see plugin uv.lock/pyproject^)
        ) else (
            set /a VENV_CREATED+=1
        )
    )
)
echo [OK] Plugin venvs ready ^(created !VENV_CREATED! this run; existing ones skipped^).
echo.

REM ============================================================
REM  Step 3: start kernel
REM ============================================================
echo [3/4] Starting kernel on port :%AGENTOS_KERNEL_PORT%...

set "AGENTOS_KERNEL_HOST=0.0.0.0"
set "AGENTOS_PLUGINS_DIR=%PROJECT_ROOT%\plugins\shared"
set "AGENTOS_CONFIG_ROOT=%PROJECT_ROOT%\config"
REM 2026-09-07 P12 note: idle-unload no longer needs a raised threshold --
REM the kernel now guards reclamation with an in-flight call counter
REM (79cb1dc72), so a busy plugin is never unloaded mid-call. Default 300s
REM reclaims truly idle sidecars promptly; override via env if needed.
if not defined AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS set "AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS=300"

REM G8 supervisor: exit 75 (POST /api/v1/system/restart, watcher cdylib
REM set change - A3) respawns after 1s; other exit codes auto-respawn with
REM exponential backoff and circuit-break after N consecutive failures
REM (default 5). Kernel output is discarded; the kernel's own log lives
REM under logs\ (kernel.log.YYYY-MM-DD).
start "AgentOS Kernel" /B cmd /c ""%PROJECT_ROOT%\run_kernel_supervised.bat" "%KERNEL_BIN%""

echo        Waiting for kernel (poll /health up to 60s)...
set "KERNEL_READY=0"
for /l %%i in (1,1,60) do (
    if "!KERNEL_READY!"=="0" (
        curl -s -o nul -w "%%{http_code}" "http://localhost:%AGENTOS_KERNEL_PORT%/health" 2>nul | findstr "200" >nul
        if not errorlevel 1 (
            set "KERNEL_READY=1"
            echo [OK] Kernel ready.
        ) else (
            ping -n 2 127.0.0.1 >nul
        )
    )
)
if "!KERNEL_READY!"=="0" (
    echo [ERROR] Kernel not ready within 60s, aborting.
    echo [HINT] Kernel did not answer /health. Check log: logs\kernel.log.*
    call :KillPort "%AGENTOS_KERNEL_PORT%" "kernel"
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
REM  Step 4: start frontend (proxy to 0.2 kernel :9100)
REM ============================================================
echo [4/4] Starting frontend on port :%AGENTOS_FRONTEND_PORT%...

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
REM 127.0.0.1 instead of localhost: on Windows localhost resolves to IPv6
REM (::1) first while the kernel listens on IPv4 only, so each request pays
REM an extra ::1 failure fallback (with silent-drop firewalls this shows up
REM as seconds of slow first paint / WS connect).
start "AgentOS Frontend" /B cmd /c "set VITE_PROXY_TARGET=http://127.0.0.1:%AGENTOS_KERNEL_PORT%&& npx --yes vite --host 0.0.0.0 --port %AGENTOS_FRONTEND_PORT%"
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
            ping -n 2 127.0.0.1 >nul
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
echo   Stop: run stop_web_02.bat (Git Bash 下也可用 stop_web_02.sh), or kill by port:
echo     netstat -aon ^| findstr /C:":%AGENTOS_KERNEL_PORT% " ^| findstr LISTENING  -^> taskkill /F /T /PID ^<pid^>
echo ========================================
echo.
pause
endlocal
exit /b 0

REM ------------------------------------------------------------
REM  KillPort <port> <label>
REM  Kill every PID LISTENING on the given TCP port (tree kill).
REM  netstat -ano columns: Proto Local Foreign State PID -> tokens=5
REM  (findstr /C:":port " with trailing space avoids :91001-style
REM   mismatches; LISTENING filter avoids killing outbound clients)
REM ------------------------------------------------------------
:KillPort
set "KILLPORT_FOUND=0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":%~1 " ^| findstr /C:"LISTENING"') do (
    echo        [CLEAN] %~2: killing PID %%p on port %~1
    taskkill /F /T /PID %%p >nul 2>&1
    set "KILLPORT_FOUND=1"
)
if "!KILLPORT_FOUND!"=="0" echo        [CLEAN] %~2: no listener on port %~1
set "KILLPORT_FOUND="
goto :eof

REM ------------------------------------------------------------
REM  WaitExeUnlock <exe-path>
REM  Poll up to ~15s until the exe can be opened for exclusive
REM  read-write - the same access cargo's linker needs to replace
REM  it. NOTE: rename/move of a running exe SUCCEEDS on Windows
REM  (only delete/write are blocked), so a rename round-trip is NOT
REM  a valid lock probe; an exclusive File.Open is. Each failed
REM  probe is followed by a name-targeted kill, so instances the
REM  port scan missed also get cleared here.
REM ------------------------------------------------------------
:WaitExeUnlock
if not exist "%~1" goto :eof
set "WEU_N=0"
:WaitExeUnlockLoop
powershell -NoProfile -Command "try { $f=[System.IO.File]::Open('%~1','Open','ReadWrite','None'); $f.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto :eof
taskkill /F /IM agentos-kernel.exe >nul 2>&1
set /a WEU_N+=1
if !WEU_N! GEQ 15 (
    echo        [WARN] exe still locked after 15s - cargo build may fail with os error 5
    goto :eof
)
ping -n 2 127.0.0.1 >nul
goto :WaitExeUnlockLoop
