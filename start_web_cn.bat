@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Agent OS

cd /d "%~dp0"

REM   set FRONTEND_HOST_PORT=5290 && set REDIS_HOST_PORT=6481 && set BACKEND_PORT=8989
if not defined FRONTEND_HOST_PORT set "FRONTEND_HOST_PORT=5289"
if not defined REDIS_HOST_PORT set "REDIS_HOST_PORT=6480"
if not defined BACKEND_PORT set "BACKEND_PORT=8988"

echo ========================================
echo   Agent OS Starting
echo ========================================
echo Project dir: %cd%
echo Ports: frontend=!FRONTEND_HOST_PORT! backend=!BACKEND_PORT! Redis=!REDIS_HOST_PORT!
echo.

REM WSL shutdown retry counter
if not defined SHUTDOWN_RETRY set "SHUTDOWN_RETRY=0"

REM ============================================================
REM  Quick Python detection (needed for isolation plugin CLI)
REM ============================================================
set "ISOLATION_CLI=%cd%\plugins\shared\system\isolation\isolation_cli.py"
set "PYEXE="

REM Try common Python locations
for %%v in (312 311 313) do (
    for /f "delims=" %%p in ('where python%%v 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%p"
    )
)
if not defined PYEXE (
    where python >nul 2>&1
    if not errorlevel 1 for /f "delims=" %%p in ('where python') do (
        if not defined PYEXE set "PYEXE=%%p"
    )
)

if not defined PYEXE (
    set "P312A=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    if exist "!P312A!" set "PYEXE=!P312A!"
)
if not defined PYEXE (
    set "P312B=%ProgramFiles%\Python312\python.exe"
    if exist "!P312B!" set "PYEXE=!P312B!"
)

if not defined PYEXE (
    echo [ERROR] Python not found. Install Python 3.11+ to use isolation plugin.
    echo [ERROR] WSL/Docker orchestration requires Python.
    pause
    exit /b 1
)

set "ISOLATION_PLUGIN_DIR=%cd%\plugins\shared\system\isolation"
echo [OK] Python for isolation: %PYEXE%

REM ============================================================
REM  WSL/Docker orchestration — delegated to isolation plugin
REM  (was ~150 lines of inline WSL/Docker logic, now calls plugin)
REM ============================================================

:wsl_alive_entry
echo [INFO] Probing WSL response...

"%PYEXE%" "%ISOLATION_CLI%" probe
set "WSL_ALIVE_RC=!errorlevel!"

if "!WSL_ALIVE_RC!"=="124" goto :probe_deadlocked
if "!WSL_ALIVE_RC!"=="2" goto :disk_lost
if not "!WSL_ALIVE_RC!"=="0" goto :probe_other_error
goto :probe_ok

:probe_deadlocked
set "REASON=WSL probe timeout (kernel deadlock?)"
goto :auto_shutdown

:probe_other_error
echo [ERROR] WSL unavailable rc=!WSL_ALIVE_RC!, cannot start without WSL2 + docker-ce
echo [ERROR] Docker Desktop is no longer supported. Run install_native_docker.bat to set up WSL2 docker first.
pause
exit /b 1

:probe_ok
echo [OK] WSL responding OK

REM Derive WSL path from script's own location
set "WIN_DIR=%cd%"
set "WIN_DIR=%WIN_DIR:\=/%"
for /f "delims=" %%i in ('wsl -d Ubuntu -u root -- bash -c "timeout 15 wslpath -u \"%WIN_DIR%\"" 2^>nul') do set "WSL_DIR=%%i"

REM Check WSL availability
wsl -d Ubuntu -u root -- bash -c "timeout 30 echo wsl_ok" >nul 2>&1
if errorlevel 1 goto :no_wsl_docker

echo [INFO] WSL docker mode detected

REM ============================================================
REM WSL kernel health check — via isolation plugin
REM ============================================================
echo [INFO] Checking WSL kernel health...
"%PYEXE%" "%ISOLATION_CLI%" health "%WSL_DIR%"
set "HEALTH_RC=!errorlevel!"
if "!HEALTH_RC!"=="0" goto :wsl_alive_ok
if "!HEALTH_RC!"=="8" goto :wsl_polluted
echo [WARN] health probe abnormal (rc=!HEALTH_RC!), treat as kernel pollution
goto :wsl_polluted

:wsl_alive_ok
REM Keep WSL alive (prevents WSL suspend)
powershell -NoProfile -Command "if (-not (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'sleep infinity' } | Select-Object -First 1)) { Start-Process wsl -ArgumentList '-d','Ubuntu','--exec','/bin/bash','-c','exec sleep infinity' -WindowStyle Hidden }" >nul 2>&1

REM ============================================================
REM Ensure dockerd running — via isolation plugin
REM ============================================================
echo [INFO] Ensuring dockerd running...
"%PYEXE%" "%ISOLATION_CLI%" daemon "%WSL_DIR%"
set "DAEMON_RC=!errorlevel!"
if "!DAEMON_RC!"=="0" goto :daemon_ok
if "!DAEMON_RC!"=="7" goto :wsl_polluted
echo [ERROR] dockerd start failed (rc=!DAEMON_RC!)
goto :wsl_polluted

:daemon_ok
REM Ensure docker compose plugin accessible
wsl -d Ubuntu -u root -- bash -c "mkdir -p /usr/lib/docker/cli-plugins /root/.docker/cli-plugins; ln -sf /usr/libexec/docker/cli-plugins/docker-compose /usr/lib/docker/cli-plugins/docker-compose 2>/dev/null; ln -sf /usr/libexec/docker/cli-plugins/docker-compose /root/.docker/cli-plugins/docker-compose 2>/dev/null" >nul 2>&1

REM ============================================================
REM Get WSL IP — via isolation plugin
REM ============================================================
echo [INFO] Getting WSL IP...
for /f "delims=" %%i in ('"%PYEXE%" "%ISOLATION_CLI%" ip 2^>nul') do set "WSL_IP=%%i"
if not defined WSL_IP goto :no_wsl_ip
echo [OK] WSL IP: %WSL_IP%

REM ============================================================
REM Setup port forwarding — via isolation plugin
REM ============================================================
echo [INFO] Setting up port forwarding...
"%PYEXE%" "%ISOLATION_CLI%" portproxy "%WSL_IP%" "%FRONTEND_HOST_PORT%" "%REDIS_HOST_PORT%"
set "PORTPROXY_RC=!errorlevel!"
if "!PORTPROXY_RC!"=="0" (
    echo [OK] Port forwarding configured
) else (
    echo [WARN] Port forwarding NOT set. Run as admin or set manually.
)

REM ============================================================
REM Start project containers — via isolation plugin
REM ============================================================
echo [INFO] Starting project containers...
"%PYEXE%" "%ISOLATION_CLI%" containers "%WSL_DIR%" "%FRONTEND_HOST_PORT%" "%REDIS_HOST_PORT%" "%BACKEND_PORT%"
set "CONTAINERS_RC=!errorlevel!"
if "!CONTAINERS_RC!"=="0" goto :containers_ok
if "!CONTAINERS_RC!"=="7" goto :cgroup_stuck
echo [ERROR] container start failed (rc=!CONTAINERS_RC!), see output above
echo [ERROR] try: wsl --shutdown then re-run this script
pause
exit /b 1

:cgroup_stuck
set "REASON=container cleanup/start blocked (cgroup/task residue)"
goto :auto_shutdown

:disk_lost
echo [ERROR] Ubuntu (ext4.vhdx) disk lost. Try: wsl --shutdown
if exist "%TEMP%\wsl_alive_probe.err" type "%TEMP%\wsl_alive_probe.err"
echo [ERROR] Run install_native_docker.bat to reconfigure Ubuntu.
pause
exit /b 8

:wsl_polluted
set "REASON=WSL kernel polluted by D-state deadlock"

:auto_shutdown
set /a "SHUTDOWN_RETRY+=1"
if !SHUTDOWN_RETRY! gtr 3 (
    echo [ERROR] auto wsl --shutdown retried !SHUTDOWN_RETRY! times still failed, giving up
    echo [ERROR] reason: !REASON!
    echo [ERROR] Run wsl --shutdown manually, wait 10s, re-run this script
    pause
    exit /b 7
)
echo [WARN] !REASON!, auto wsl --shutdown then retry ( !SHUTDOWN_RETRY!/3 )...
"%PYEXE%" "%ISOLATION_CLI%" shutdown
echo [INFO] Waiting for WSL kernel to exit (~10s)...
ping -n 11 127.0.0.1 >nul
echo [INFO] Disabling known D-state services (landscape-client etc)...
wsl -d Ubuntu -u root -- bash -c "systemctl disable landscape-client landscape-client.service unattended-upgrades 2>/dev/null; systemctl mask landscape-client landscape-client.service 2>/dev/null; true" >nul 2>&1
echo [INFO] Re-probing WSL response...
goto :wsl_alive_entry

:containers_ok
echo [OK] Containers started

REM Frontend code auto-update
echo [INFO] Checking frontend updates...
set "DOCKER_HOST=tcp://%WSL_IP%:2375"
where docker >nul 2>&1
if not errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_frontend.ps1"
) else (
    echo [INFO] Windows docker CLI not found, skipping frontend update.
)

echo.
echo [INFO] Using WSL native docker
goto :start_python

REM ============================================================
REM WSL/docker abort exits
REM ============================================================
:no_wsl_docker
echo [ERROR] WSL reachable but docker not working
echo [ERROR] Docker Desktop is no longer supported. Run install_native_docker.bat to set up WSL2 docker first.
pause
exit /b 1

:no_wsl_ip
echo [ERROR] Cannot get WSL IP
echo [ERROR] WSL2 networking not ready. Run install_native_docker.bat to reconfigure, then retry.
pause
exit /b 1

:start_python

:: ===========================================================================
REM Full Python version check + dependency installation
:: ===========================================================================
REM Python version check: project requires >=3.11. If older, auto-install Python 3.12.
"%PYEXE%" -c "import sys; exit(0 if sys.version_info >= (3,11) else 1)" 2>nul
if errorlevel 1 (
    echo [WARN] Python too old, need >=3.11. Auto-installing Python 3.12...
    winget install Python.Python.3.12 --accept-source-agreements --accept-package-agreements --silent 2>nul
    if errorlevel 1 (
        echo [ERROR] winget install failed. Please manually install Python 3.12 from python.org
        echo [ERROR] Make sure to check "Add Python to PATH" during install.
        pause
        exit /b 1
    )
    echo [OK] Python 3.12 installed. Re-detecting...
    py -3.12 -c "import sys; print(sys.executable)" >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%p in ('py -3.12 -c "import sys; print(sys.executable)" 2^>nul') do set "PYEXE=%%p"
        echo [OK] Using Python 3.12: !PYEXE!
        del ".py_deps_installed" 2>nul
    ) else (
        echo [ERROR] Python 3.12 installed but py launcher cannot find it.
        echo [ERROR] Reopen terminal and re-run this script.
        pause
        exit /b 1
    )
)
echo [OK] Python: %PYEXE%
"%PYEXE%" --version 2>&1

if not exist ".py_deps_installed" (
    echo [INFO] Installing Python deps...
    set "DEPS_OK=0"
    "%PYEXE%" -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com --timeout 60
    if not errorlevel 1 (
        set "DEPS_OK=1"
    ) else (
        echo [WARN] Some packages failed, retry with --no-deps...
        "%PYEXE%" -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com --timeout 60 --no-deps
        if not errorlevel 1 set "DEPS_OK=1"
    )
    if "!DEPS_OK!"=="0" (
        echo [WARN] requirements.txt failed, fallback: pip install -e .
        "%PYEXE%" -m pip install -e . -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com --timeout 30
        if not errorlevel 1 set "DEPS_OK=1"
    )
    if "!DEPS_OK!"=="1" (
        echo. > ".py_deps_installed"
        echo [OK] Python deps installed
    ) else (
        echo [ERROR] Python deps install failed, backend may not start
        echo [INFO] Run manually: pip install -r requirements.txt
    )
) else (
    echo [OK] Python deps already installed
)

:: ===========================================================================
echo [INFO] Starting Agent backend...
REM Write a temp launcher to avoid quote-nesting hell in start cmd /c.
set "_LAUNCHER=%TEMP%\agent_os_backend.bat"
> "%_LAUNCHER%" echo @echo off
>> "%_LAUNCHER%" echo set PYTHONPATH=src
>> "%_LAUNCHER%" echo set REDIS_URL=redis://localhost:%REDIS_HOST_PORT%/0
>> "%_LAUNCHER%" echo set BACKEND_PORT=%BACKEND_PORT%
>> "%_LAUNCHER%" echo cd /d "%cd%"
>> "%_LAUNCHER%" echo "%PYEXE%" -m channels.websocket.app_factory
start "Agent OS Backend" "%_LAUNCHER%"

echo.
echo ========================================
echo   Started
echo ========================================
echo   Backend: http://127.0.0.1:%BACKEND_PORT%
echo   Frontend: http://127.0.0.1:%FRONTEND_HOST_PORT%
echo   Isolation plugin: %ISOLATION_PLUGIN_DIR%
echo   Stop: close Agent window + docker compose down
echo ========================================
pause
exit /b 0
