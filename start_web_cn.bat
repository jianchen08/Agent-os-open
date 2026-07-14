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

REM WSL shutdown retry counter (reset once at startup; bumped on each auto wsl --shutdown)
if not defined SHUTDOWN_RETRY set "SHUTDOWN_RETRY=0"

REM NOTE: we do NOT unconditionally `wsl --shutdown` at startup.
REM That would kill a running dockerd + containers and break a healthy WSL session.
REM Instead: probe first; only on deadlock does :auto_shutdown do the reset.
REM Idle-timeout suspension is prevented via .wslconfig vmIdleTimeout=-1.

:wsl_alive_entry
echo [INFO] Probing WSL response...

REM Windows-side liveness probe with a hard timeout (see wsl_alive_probe.ps1).
REM rc=124 -> WSL hung -> auto wsl --shutdown retry; rc=0 -> alive, proceed;
REM other -> WSL/Ubuntu unavailable -> abort (Docker Desktop not supported).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wsl_alive_probe.ps1" -Timeout 20 >nul
set "WSL_ALIVE_RC=!errorlevel!"
REM Use goto-based branching, NOT if (...) blocks: cmd's parenthesised blocks break
REM on special chars (here the literal "(rc=" in an echo would corrupt block parsing
REM with "was unexpected at this time"). Same idiom as the rest of this script.
if "!WSL_ALIVE_RC!"=="124" goto :probe_deadlocked
if "!WSL_ALIVE_RC!"=="2" goto :disk_lost
if not "!WSL_ALIVE_RC!"=="0" goto :probe_other_error
goto :probe_ok

:probe_deadlocked
set "REASON=WSL probe timeout (kernel deadlock?)"
goto :auto_shutdown

:probe_other_error
findstr /i /c:"MountDisk" /c:"ERROR_FILE_NOT_FOUND" /c:"0x80070002" "%TEMP%\wsl_alive_probe.err" >nul 2>&1
if not errorlevel 1 goto :disk_lost
echo [ERROR] WSL unavailable rc=!WSL_ALIVE_RC!, cannot start without WSL2 + docker-ce
echo [ERROR] Docker Desktop is no longer supported. Run install_native_docker.bat to set up WSL2 docker first.
pause
exit /b 1

:probe_ok
echo [OK] WSL responding OK

REM Portable: derive WSL path from script's own location (no hardcoded project path).
REM Note: %cd% uses backslashes; wsl/shell would eat them, so convert to slashes first.
set "WIN_DIR=%cd%"
set "WIN_DIR=%WIN_DIR:\=/%"
for /f "delims=" %%i in ('wsl -d Ubuntu -u root -- bash -c "timeout 15 wslpath -u \"%WIN_DIR%\"" 2^>nul') do set "WSL_DIR=%%i"

REM ===========================================================================
REM WSL native docker mode (replaces Docker Desktop)
REM Bypasses systemd (which has a bug that periodically stops docker.service).
REM Uses goto-based flow (cmd nested if-blocks break on special chars).
REM ===========================================================================
wsl -d Ubuntu -u root -- bash -c "timeout 30 echo wsl_ok" >nul 2>&1
if errorlevel 1 goto :no_wsl_docker

echo [INFO] WSL docker mode detected

:wsl_setup
REM Pre-disable landscape-client BEFORE the health probe.
REM landscape-client's landscape-sysinfo collector periodically enters D-state
REM on WSL2 and pollutes the kernel. Pre-disable on every entry (incl. retries)
REM prevents the deadlock at source. Idempotent: stop+disable+mask.
echo [INFO] Pre-disabling landscape-client to avoid D-state deadlock...
wsl -d Ubuntu -u root -- bash -c "timeout 25 systemctl stop landscape-client landscape-client.service unattended-upgrades 2>/dev/null; systemctl disable landscape-client landscape-client.service unattended-upgrades 2>/dev/null; systemctl mask landscape-client landscape-client.service 2>/dev/null; true" >nul 2>&1

REM 0. WSL kernel health pre-warm: detect D-state deadlock pollution so that
REM    later pgrep/docker probes are not infected and hang.
REM    Outer timeout 30s backstop (probe normally <3s; if even reading /proc
REM    hangs, the timeout will force-kill it).
echo [INFO] Checking WSL kernel health...
wsl -d Ubuntu -u root -- bash -c "timeout 30 %WSL_DIR%/wsl_health_probe.sh %WSL_DIR%" 2>&1
set "HEALTH_RC=!errorlevel!"
if "!HEALTH_RC!"=="0" goto :wsl_alive_ok
if "!HEALTH_RC!"=="8" goto :wsl_polluted
findstr /i /c:"MountDisk" /c:"ERROR_FILE_NOT_FOUND" /c:"0x80070002" "%TEMP%\wsl_alive_probe.err" >nul 2>&1
if not errorlevel 1 goto :disk_lost
REM timeout force-kill returns 124, or other anomaly -> treat as pollution
echo [WARN] health probe abnormal (rc=!HEALTH_RC!), treat as kernel pollution
goto :wsl_polluted

:wsl_alive_ok
REM 1. Keep WSL alive (sleep infinity in background, prevents WSL suspend)
powershell -NoProfile -Command "if (-not (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'sleep infinity' } | Select-Object -First 1)) { Start-Process wsl -ArgumentList '-d','Ubuntu','--exec','/bin/bash','-c','exec sleep infinity' -WindowStyle Hidden }" >nul 2>&1

REM 2. Ensure dockerd running (bypass systemd: start dockerd directly)
REM    Delegated to wsl_start_daemon.sh so that every pgrep/pkill/docker call
REM    (which walk /proc and can hang on D-state deadlock) is wrapped in timeout.
REM    Outer timeout 150s as backstop; rc=7 means kernel polluted -> wsl --shutdown.
wsl -d Ubuntu -u root -- bash -c "timeout 150 %WSL_DIR%/wsl_start_daemon.sh"
set "DAEMON_RC=!errorlevel!"
if "!DAEMON_RC!"=="0" goto :daemon_ok
if "!DAEMON_RC!"=="7" goto :wsl_polluted
echo [ERROR] dockerd start failed (rc=!DAEMON_RC!), see output above
goto :wsl_polluted

:daemon_ok

REM 2b. Ensure docker compose plugin accessible (symlink to cli-plugins)
wsl -d Ubuntu -u root -- bash -c "mkdir -p /usr/lib/docker/cli-plugins /root/.docker/cli-plugins; ln -sf /usr/libexec/docker/cli-plugins/docker-compose /usr/lib/docker/cli-plugins/docker-compose 2>/dev/null; ln -sf /usr/libexec/docker/cli-plugins/docker-compose /root/.docker/cli-plugins/docker-compose 2>/dev/null" >nul 2>&1

REM 3. Get WSL IP (NAT mode, may change on restart)
set "WSL_IP="
for /f "tokens=1 delims= " %%i in ('wsl -d Ubuntu -u root -- bash -c "hostname -I 2>/dev/null" 2^>nul') do set "WSL_IP=%%i"

if not defined WSL_IP goto :no_wsl_ip

echo [OK] WSL IP: %WSL_IP%

REM 4. Setup netsh portproxy (Windows localhost -> WSL container ports)
REM    reset first to avoid duplicate rules from repeated runs
echo [INFO] Setting up port forwarding...
REM Write portproxy commands to a temp bat, run elevated (avoids quote nesting hell).
set "_PORTPROXY_BAT=%TEMP%\agent_portproxy.bat"
> "%_PORTPROXY_BAT%" echo @echo off
>> "%_PORTPROXY_BAT%" echo netsh interface portproxy reset
>> "%_PORTPROXY_BAT%" echo netsh interface portproxy add v4tov4 listenport=%FRONTEND_HOST_PORT% listenaddress=0.0.0.0 connectport=%FRONTEND_HOST_PORT% connectaddress=%WSL_IP%
>> "%_PORTPROXY_BAT%" echo netsh interface portproxy add v4tov4 listenport=%REDIS_HOST_PORT% listenaddress=0.0.0.0 connectport=%REDIS_HOST_PORT% connectaddress=%WSL_IP%
powershell -NoProfile -Command "Start-Process cmd -Verb RunAs -Wait -ArgumentList '/c','%_PORTPROXY_BAT%'" 2>nul
REM Verify portproxy was actually set (UAC may have been denied).
netsh interface portproxy show v4tov4 2>nul | findstr "%FRONTEND_HOST_PORT%" >nul 2>&1
if errorlevel 1 goto :portproxy_failed
echo [OK] Port forwarding configured
goto :portproxy_done
:portproxy_failed
echo [WARN] Port forwarding NOT set. Run as admin or set manually.
:portproxy_done

REM 5. Start project containers (delegated to wsl_ensure_containers.sh for real status check)
REM    Outer timeout 240s backstop; script internals also wrap every docker call.
echo [INFO] Starting project containers...
wsl -d Ubuntu -u root -- bash -c "export FRONTEND_HOST_PORT=%FRONTEND_HOST_PORT% REDIS_HOST_PORT=%REDIS_HOST_PORT% BACKEND_PORT=%BACKEND_PORT%; timeout 240 %WSL_DIR%/wsl_ensure_containers.sh %WSL_DIR%"
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
echo [ERROR] Ubuntu (ext4.vhdx),wsl --shutdown 
echo [ERROR] :
if exist "%TEMP%\wsl_alive_probe.err" type "%TEMP%\wsl_alive_probe.err"
echo [ERROR] : install_native_docker.bat, Ubuntu,
echo [ERROR]       
echo [ERROR] : wsl --unregister Ubuntu  wsl --install -d Ubuntu
pause
exit /b 8

:wsl_polluted
set "REASON=WSL kernel polluted by D-state deadlock"

:auto_shutdown
set /a "SHUTDOWN_RETRY+=1"
if !SHUTDOWN_RETRY! gtr 3 (
    echo [ERROR] auto wsl --shutdown retried !SHUTDOWN_RETRY!  times still failed, giving up
    echo [ERROR] reason: !REASON!
    echo [ERROR] Run wsl --shutdown manually, wait 10s, re-run this script
    pause
    exit /b 7
)
echo [WARN] !REASON!, auto wsl --shutdown then retry ^( !SHUTDOWN_RETRY!/3 ^)...
REM wsl --shutdown itself may hang under kernel deadlock; wrap in timeout (wsl_shutdown.ps1).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wsl_shutdown.ps1" -Timeout 15 >nul 2>&1
echo [INFO] Waiting for WSL kernel to exit ^(~10s^)...
REM ping-based delay avoids timeout.exe (unreliable under non-interactive shells).
ping -n 11 127.0.0.1 >nul
echo [INFO] Re-probing WSL response...
goto :wsl_alive_entry

:containers_ok
echo [OK] Containers started

REM Frontend code auto-update.
REM Windows docker CLI reaches WSL daemon via DOCKER_HOST=tcp://<WSL_IP>:2375;
REM WSL IP may change after wsl --shutdown, so bind to current %WSL_IP%.
echo [INFO] Checking frontend updates...
set "DOCKER_HOST=tcp://%WSL_IP%:2375"
where docker >nul 2>&1
if not errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_frontend.ps1"
) else (
    echo [INFO] Windows  docker CLI, 
)

echo.
echo [INFO] Using WSL native docker
goto :start_python

REM ===========================================================================
REM WSL/docker abort exits (reached via goto; Docker Desktop no longer supported)
REM ===========================================================================
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
set "PYEXE="


for %%v in (312 311 313) do (
    for /f "delims=" %%p in ('where python%%v 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%p"
    )
)



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


if not defined PYEXE (
    where python >nul 2>&1
    if not errorlevel 1 for /f "delims=" %%p in ('where python') do (
        if not defined PYEXE set "PYEXE=%%p"
    )
)

if not defined PYEXE (
    echo [ERROR] Python not found. Install Python 3.11+
    pause
    exit /b 1
)

REM Python version check: project requires >=3.11. If older (e.g. 3.9),
REM auto-install Python 3.12 via winget, then re-detect.
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

:: ===========================================================================
echo [INFO] Starting Agent backend...
REM Write a temp launcher to avoid quote-nesting hell in start cmd /c.
REM PYEXE path may contain spaces (e.g. C:\Users\...\Python312\python.exe),
REM embedding it in cmd /c "... && PYEXE ..." breaks quote matching.
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
echo   Stop: close Agent window + docker compose down
echo ========================================
pause
exit /b 0


:: ===========================================================================







:: ===========================================================================
:pull_image_with_fallback
set "IMG=%~1"


docker image inspect "%IMG%" >nul 2>&1
if not errorlevel 1 (
    echo [OK] Image exists locally: %IMG%
    exit /b 0
)

echo [INFO] Not local %IMG%, trying pull...
docker pull "%IMG%" >nul 2>&1
if not errorlevel 1 (
    echo [OK] pull success: %IMG%
    exit /b 0
)


echo [WARN] Docker Hub pull failed,  daocloud ...
docker pull "docker.m.daocloud.io/library/%IMG%" >nul 2>&1
if errorlevel 1 (
    echo [WARN]  %IMG% pull failed(Docker Hub  daocloud )
    echo [WARN] compose/build will retry later, if still fails configure daemon.json registry-mirrors
    exit /b 0
)

docker tag "docker.m.daocloud.io/library/%IMG%" "%IMG%" >nul 2>&1
if errorlevel 1 (
    echo [WARN] tag tag rename failed: docker.m.daocloud.io/library/%IMG% -^> %IMG%
    exit /b 0
)
echo [OK] pull success(daocloud ): %IMG%
exit /b 0
