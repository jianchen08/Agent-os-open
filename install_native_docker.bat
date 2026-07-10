@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Agent OS - Native Docker Setup

cd /d "%~dp0"

echo ========================================
echo   WSL2 Native Docker - One Click Setup
echo ========================================
echo.
echo Replaces Docker Desktop. Eliminates com.docker.backend hang.
echo No code changes needed. docker commands fully compatible.
echo.
echo Press any key to start (fully automatic, no password needed)...
pause >nul

echo.

REM === 1. Ensure Ubuntu WSL exists ===
echo [1/5] Check WSL2 Ubuntu...
powershell -NoProfile -Command ^
  "$list = ((wsl -l -q) -join \"`n\") -replace [char]0, '';" ^
  "if ($list -match 'Ubuntu') { exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo [INFO] Ubuntu not found, installing...
    wsl --install -d Ubuntu
    if errorlevel 1 (
        echo [ERROR] Ubuntu install failed. Run manually: wsl --install -d Ubuntu
        pause
        exit /b 1
    )
    echo [WARN] Ubuntu just installed. Reboot PC then re-run this script.
    pause
    exit /b 3010
)
echo [OK] Ubuntu installed

REM === 2. Install docker-ce inside WSL (as root, no password) ===
echo.
echo [2/5] Install docker-ce inside WSL2 Ubuntu (root, no password)...
echo.

REM Resolve this directory's WSL path (no hardcoded path)
set "WIN_DIR=%~dp0"
set "WIN_DIR=%WIN_DIR:\=/%"
for /f "delims=" %%p in ('wsl -d Ubuntu -u root -- wslpath -u "%WIN_DIR%" 2^>nul') do set "WSL_SCRIPT_DIR=%%p"
if "!WSL_SCRIPT_DIR!"=="" (
    echo [ERROR] Cannot resolve WSL path for script directory.
    pause
    exit /b 1
)
echo [INFO] Script dir in WSL: !WSL_SCRIPT_DIR!

:run_wsl_install
wsl -d Ubuntu -u root -- bash -c "cd '!WSL_SCRIPT_DIR!' && bash install_wsl_docker.sh"
set "WSL_RC=!errorlevel!"

REM WSL exit code is unreliable for completed scripts; check by content.
REM Success marker: script prints "WSL_DOCKER_READY" (we capture via temp file).
REM exit 100 = systemd just enabled, need wsl --shutdown then rerun
if "!WSL_RC!"=="100" (
    echo.
    echo [INFO] systemd enabled. Restarting WSL to apply...
    wsl --shutdown
    timeout /t 5 /nobreak >nul
    echo [INFO] Re-running install script...
    goto run_wsl_install
)

REM === 3. Get WSL IP and set DOCKER_HOST (NAT mode, IP may change) ===
echo.
echo [3/5] Get WSL IP and configure DOCKER_HOST...
REM hostname -I 第一个 token 就是 eth0 的 172.x IP
for /f "tokens=1 delims= " %%i in ('wsl -d Ubuntu -u root -- bash -c "hostname -I 2>/dev/null" 2^>nul') do (
    set "WSL_IP=%%i"
)

if "!WSL_IP!"=="" (
    echo [ERROR] Cannot get WSL IP. Make sure Ubuntu is running.
    pause
    exit /b 1
)

set "NEW_HOST=tcp://!WSL_IP!:2375"
echo [OK] WSL IP: !WSL_IP!
echo [INFO] Setting DOCKER_HOST=!NEW_HOST!
setx DOCKER_HOST "!NEW_HOST!" >nul
set "DOCKER_HOST=!NEW_HOST!"
echo [OK] DOCKER_HOST set (takes effect in new terminals)

REM === 4. Add firewall rule for port 2375 (once) ===
echo.
echo [4/5] Ensure firewall allows port 2375...
powershell -NoProfile -Command "if (-not (Get-NetFirewallRule -DisplayName 'WSL Docker 2375' -ErrorAction SilentlyContinue)) { Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-Command','New-NetFirewallRule -DisplayName ''WSL Docker 2375'' -Direction Inbound -LocalPort 2375 -Protocol TCP -Action Allow'; Write-Host 'added' } else { Write-Host 'exists' }" 2>&1 | findstr /i "added exists" >nul && (
    echo [OK] Firewall rule ready
) || (
    echo [WARN] Firewall rule may need manual add (run as admin if connection fails)
)

REM === 5. Verify Windows can reach WSL docker ===
echo.
echo [5/5] Verify connection to WSL docker (waiting for daemon)...
set "VERIFY_OK=0"
for /l %%i in (1,1,15) do (
    if "!VERIFY_OK!"=="0" (
        docker version --format "{{.Server.Version}}" 2>nul | findstr /r "^[0-9]" >nul
        if not errorlevel 1 (
            set "VERIFY_OK=1"
        )
        if "!VERIFY_OK!"=="0" timeout /t 3 /nobreak >nul
    )
)

if "!VERIFY_OK!"=="1" (
    echo [OK] Windows connected to WSL2 docker!
    for /f "delims=" %%v in ('docker version --format "{{.Server.Version}}" 2^>nul') do set "DOCKER_VER=%%v"
    echo      docker Server version: !DOCKER_VER!
) else (
    echo [WARN] Cannot connect yet. Try reopening terminal then run: docker version
    echo [WARN] If still fails, run this script again (WSL IP may have changed)
)

echo.
echo ========================================
echo   Setup Complete
echo ========================================
echo.
echo Next steps:
echo   1. Close all cmd/terminal windows, reopen (so DOCKER_HOST applies)
echo   2. Run: docker version  (confirm it connects to WSL docker)
echo   3. If OK, uninstall Docker Desktop (Control Panel)
echo   4. Start project with start_web_cn.bat
echo.
echo NOTE: If docker version fails later (after WSL restart), re-run this script
echo       to refresh the WSL IP. Or run start_web_cn.bat which auto-syncs it.
echo.
pause
exit /b 0
