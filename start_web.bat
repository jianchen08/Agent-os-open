@echo off
setlocal enabledelayedexpansion
title Agent OS

cd /d "%~dp0"

echo ========================================
echo   Agent OS Starting
echo ========================================
echo.
echo Project dir: %cd%
echo.

:: ===========================================================================
:: 1. Check Docker (required by this project)
::
:: Note: `docker info` can block forever when the daemon hangs (it does NOT
:: return a non-zero code). Calling it directly would hang the script forever.
:: We use a separate check_docker.ps1 with a timeout-based health check
:: (up to 90s per probe) to avoid blocking.
:: ===========================================================================
where docker >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker not found. This project requires Docker to run.
    echo [INFO]  Download: https://www.docker.com/products/docker-desktop/
    pause
    exit /b 1
)

:: Daemon health check (90s timeout, gives Docker Desktop cold-start time)
:check_daemon
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_docker.ps1" -Timeout 90 >nul 2>&1
set "DAEMON_STATUS=!errorlevel!"
if "!DAEMON_STATUS!"=="0" goto :docker_ready

:: Exit codes: 0=ready 1=not ready (starting) 3=timeout (hung)
if "!DAEMON_STATUS!"=="3" goto :daemon_hung

:: --- daemon not ready yet (status 1): it is starting up, just wait ---
:: Launch Docker Desktop on first wait entry
if not defined DOCKER_WAIT_COUNT (
    echo [INFO] Starting Docker Desktop...
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe" 2>nul
    set "DOCKER_WAIT_COUNT=0"
)

set /a "DOCKER_WAIT_COUNT+=1"
:: Wait at most 4 rounds (each round = 90s probe + 10s gap, ~7 minutes total)
if !DOCKER_WAIT_COUNT! gtr 4 goto :daemon_failed
echo [INFO] Waiting for Docker daemon to be ready... (!DOCKER_WAIT_COUNT!/4)
timeout /t 10 /nobreak >nul
goto :check_daemon

:: --- daemon hung (status 3): offer an automated restart instead of a dead wait ---
:daemon_hung
echo [WARN] docker daemon did not respond within 90s (hung, not just starting).
if defined DAEMON_RESTARTED (
    echo [WARN] Auto-restart was already attempted once and daemon is still hung. Giving up.
    goto :daemon_failed
)
echo [INFO] Launching auto-recovery (will ask for confirmation, since it stops running containers)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart_docker.ps1"
set "RESTART_RC=!errorlevel!"
if "!RESTART_RC!"=="0" (
    echo [OK] Docker daemon recovered after restart.
    set "DAEMON_RESTARTED=1"
    goto :check_daemon
)
if "!RESTART_RC!"=="2" (
    echo [INFO] Restart declined by user. Aborting.
    goto :daemon_failed
)
echo [WARN] Auto-restart did not bring the daemon back. Aborting.
goto :daemon_failed

:daemon_failed
echo [ERROR] Docker daemon not ready. Cannot start the project.
echo [ERROR] Please restart Docker Desktop manually and re-run this script:
echo [ERROR]   1. right-click the Docker tray icon -^> Quit Docker Desktop
echo [ERROR]   2. wait for the tray icon to disappear (~10s)
echo [ERROR]   3. reopen Docker Desktop and wait for the icon to turn green
echo [ERROR] If still failing: run `wsl --shutdown`, then restart Docker Desktop
echo [ERROR] Diag log: %%LOCALAPPDATA%%\Docker\log\host\com.docker.backend.exe.log
pause
exit /b 1

:docker_ready
echo [OK] Docker ready

:: ===========================================================================
:: 2. Docker services (Redis + Frontend)
:: ===========================================================================
:: Base images (node/python) are only needed when rebuilding the frontend.
:: Once agent-os-frontend:latest exists, compose up does NOT need them, so we
:: skip pre-warming here. `docker compose up` pulls redis via the configured
:: registry-mirrors (daemon.json) if missing, which is fast.
echo [INFO] Starting Docker services...
docker compose up -d
echo [OK] Docker services started

:: Frontend code update: when the image exists, check if src changed; if so, rebuild
:: and inject into the running container.
docker image inspect agent-os-frontend:latest >nul 2>&1
if errorlevel 1 (
    echo [INFO] Frontend image not found, first build needed (requires pulling the base image)
    echo [INFO] Attempting build...
    docker compose build frontend
    if errorlevel 1 (
        echo [ERROR] Frontend image build failed.
        echo [ERROR] Tried: local offline packages (packages/) -> mirror chain (aliyun/tuna/taobao) -> official source
        echo [ERROR] Troubleshooting:
        echo [ERROR]   1. Pre-download offline packages into packages/wheels and packages/npm-tarballs, then rebuild
        echo [ERROR]   2. Configure registry-mirrors in Docker daemon.json (CN mirror acceleration)
        pause
        exit /b 1
    )
    echo [OK] Frontend image built
    docker compose up -d frontend
    echo [INFO] Pruning old images...
    docker image prune -f 2>nul
    powershell -NoProfile -Command "Get-Date | Out-File -FilePath '.frontend_built_at' -Encoding ascii"
) else (
    echo [INFO] Checking frontend code updates...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_frontend.ps1"
)

:: ===========================================================================
:: 3. Python + dependencies (prefer 3.12, avoid the 3.14 asyncio subprocess bug)
:: ===========================================================================
set "PYEXE="

:: Method 1: look for versioned command aliases (python312/python311/python313)
for %%v in (312 311 313) do (
    for /f "delims=" %%p in ('where python%%v 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%p"
    )
)

:: Method 2: probe common install paths (where python312 is rarely found, need path fallback)
:: Use pre-stored paths + if-exist chain to avoid %ProgramFiles(x86)% bracket escaping issues inside for loops
set "P312A=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
set "P312B=%ProgramFiles%\Python312\python.exe"
set "P311A=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
set "P311B=%ProgramFiles%\Python311\python.exe"
set "P313A=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
set "P313B=%ProgramFiles%\Python313\python.exe"
if not defined PYEXE if exist "%P312A%" set "PYEXE=%P312A%"
if not defined PYEXE if exist "%P312B%" set "PYEXE=%P312B%"
if not defined PYEXE if exist "%P311A%" set "PYEXE=%P311A%"
if not defined PYEXE if exist "%P311B%" set "PYEXE=%P311B%"
if not defined PYEXE if exist "%P313A%" set "PYEXE=%P313A%"
if not defined PYEXE if exist "%P313B%" set "PYEXE=%P313B%"

:: Method 3: finally fall back to default python (may be 3.14, risks the asyncio subprocess bug)
if not defined PYEXE (
    where python >nul 2>&1
    if not errorlevel 1 for /f "delims=" %%p in ('where python') do (
        if not defined PYEXE set "PYEXE=%%p"
    )
)

if not defined PYEXE (
    echo [ERROR] Python not found. Please install Python 3.11+
    pause
    exit /b 1
)
echo [OK] Python: %PYEXE%
"%PYEXE%" --version 2>&1

if not exist ".py_deps_installed" (
    echo [INFO] Installing Python dependencies...
    "%PYEXE%" -m pip install -r requirements.txt 2>nul
    if errorlevel 1 "%PYEXE%" -m pip install -r requirements.txt --user 2>nul
    echo. > ".py_deps_installed"
    echo [OK] Dependencies installed
) else (
    echo [OK] Python dependencies already installed
)

:: ===========================================================================
:: 4. Agent (host machine)
:: ===========================================================================
echo [INFO] Starting Agent...
start "Agent OS Backend" /D "%cd%" cmd /c "set PYTHONPATH=src&& set REDIS_URL=redis://localhost:6380/0&& "%PYEXE%" -m channels.websocket.app_factory"

echo.
echo ========================================
echo   Startup complete
echo ========================================
echo   Backend:  http://localhost:8888
echo   Frontend: http://localhost:5189
echo   Stop:     close the Agent window + run `docker compose down`
echo ========================================
pause
exit /b 0


:: ===========================================================================
:: Subroutine: pull an image (local first, fall back to a mirror chain)
:: Usage: call :pull_image_with_fallback "image:tag"
:: Strategy:
::   1) already local -> skip
::   2) docker pull <image> (Docker Hub)
::   3) docker pull <daocloud mirror> -> docker tag back to the original name
::   4) all failed -> warn only, do not block (let compose/build retry)
:: ===========================================================================
:pull_image_with_fallback
set "IMG=%~1"

:: Skip if already local
docker image inspect "%IMG%" >nul 2>&1
if not errorlevel 1 (
    echo [OK] Image already local: %IMG%
    exit /b 0
)

echo [INFO] %IMG% not local, pulling...
docker pull "%IMG%" >nul 2>&1
if not errorlevel 1 (
    echo [OK] Pulled: %IMG%
    exit /b 0
)

:: Fallback: daocloud mirror acceleration + tag back to original name
echo [WARN] Docker Hub pull failed, trying daocloud mirror...
docker pull "docker.m.daocloud.io/library/%IMG%" >nul 2>&1
if errorlevel 1 (
    echo [WARN] Image %IMG% pull failed (both Docker Hub and daocloud unavailable)
    echo [WARN] compose/build will retry later; if it still fails, configure daemon.json registry-mirrors
    exit /b 0
)

docker tag "docker.m.daocloud.io/library/%IMG%" "%IMG%" >nul 2>&1
if errorlevel 1 (
    echo [WARN] tag rename failed: docker.m.daocloud.io/library/%IMG% -^> %IMG%
    exit /b 0
)
echo [OK] Pulled (daocloud fallback): %IMG%
exit /b 0
