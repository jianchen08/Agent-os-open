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

REM === 1. Ensure Ubuntu WSL exists (and actually boots) ===
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

REM 名字在列表里 != 发行版可用:注册表条目可能还在,但它指向的
REM ext4.vhdx 文件可能已被删除/移动/损坏。此时 wsl --shutdown 无法修复
REM (虚拟磁盘文件已丢失),必须 unregister + 重装。真实启动验证 + 自愈。
:ubuntu_boot_probe
echo [INFO] Verifying Ubuntu actually boots...
set "BOOT_ERR=%TEMP%\wsl_alive_probe.err"
if exist "%BOOT_ERR%" del "%BOOT_ERR%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wsl_alive_probe.ps1" -Timeout 20 >nul 2>&1
set "BOOT_RC=!errorlevel!"
if "!BOOT_RC!"=="0" goto :ubuntu_boot_ok
REM rc=2 = probe 检测到 stderr 含磁盘丢失特征(wsl.exe 自身却返回 0),直接自愈。
if "!BOOT_RC!"=="2" goto :ubuntu_self_heal

REM rc=0/2 之外:读 wsl 的 stderr,区分"磁盘丢失/损坏"与"临时死锁/超时"。
findstr /i /c:"MountDisk" /c:"ERROR_FILE_NOT_FOUND" /c:"0x80070002" "%BOOT_ERR%" >nul 2>&1
if not errorlevel 1 goto :ubuntu_self_heal

REM 非磁盘丢失(可能死锁超时 rc=124 或其它):shutdown 后重探一次。
echo [WARN] Ubuntu boot abnormal (rc=!BOOT_RC!), retrying after wsl --shutdown...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wsl_shutdown.ps1" -Timeout 15 >nul 2>&1
ping -n 9 127.0.0.1 >nul
if exist "%BOOT_ERR%" del "%BOOT_ERR%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wsl_alive_probe.ps1" -Timeout 20 >nul 2>&1
set "BOOT_RC=!errorlevel!"
if "!BOOT_RC!"=="0" goto :ubuntu_boot_ok
if "!BOOT_RC!"=="2" goto :ubuntu_self_heal
findstr /i /c:"MountDisk" /c:"ERROR_FILE_NOT_FOUND" /c:"0x80070002" "%BOOT_ERR%" >nul 2>&1
if not errorlevel 1 goto :ubuntu_self_heal
echo [ERROR] Ubuntu 启动失败 (rc=!BOOT_RC!) 且非磁盘丢失。错误输出:
if exist "%BOOT_ERR%" type "%BOOT_ERR%"
echo [ERROR] 请手动排查后重新运行本脚本。
pause
exit /b 1

:ubuntu_self_heal
echo [WARN] Ubuntu 发行版的虚拟磁盘(ext4.vhdx)丢失或损坏,数据无法保留。
echo [INFO] 本脚本即环境重置,自动清理并重装发行版...
echo [INFO] wsl --unregister Ubuntu
wsl --unregister Ubuntu
set "UNREG_RC=!errorlevel!"
if not "!UNREG_RC!"=="0" (
    echo [ERROR] wsl --unregister 失败 (rc=!UNREG_RC!)。请手动执行: wsl --unregister Ubuntu
    pause
    exit /b 1
)
echo [OK] 旧发行版已清理
echo [INFO] wsl --install -d Ubuntu
wsl --install -d Ubuntu
if errorlevel 1 (
    echo [ERROR] Ubuntu 重装失败。请手动执行: wsl --install -d Ubuntu
    pause
    exit /b 1
)
REM 新装发行版确认可引导:可引导则就地继续配置 docker,否则提示重启后重跑。
if exist "%BOOT_ERR%" del "%BOOT_ERR%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wsl_alive_probe.ps1" -Timeout 30 >nul 2>&1
set "BOOT_RC=!errorlevel!"
if "!BOOT_RC!"=="0" goto :ubuntu_boot_ok
echo [WARN] Ubuntu 已重装,需重启 Windows 后 WSL 才能稳定运行。
echo [INFO] 请重启电脑,然后重新双击本脚本完成 docker 配置。
pause
exit /b 3010

:ubuntu_boot_ok
echo [OK] Ubuntu 可正常启动

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
REM Output goes directly to terminal (real-time, no tee/redirect = no buffering).
REM Markers (NEED_WSL_RESTART / WSL_DOCKER_READY) are written to /tmp files
REM inside WSL by install_wsl_docker.sh, read back here via wsl cat.
wsl -d Ubuntu -u root -- bash -c "cd '!WSL_SCRIPT_DIR!' && bash install_wsl_docker.sh"
set "WSL_RC=!errorlevel!"

REM Check restart marker (written to file, not stdout, for reliability).
wsl -d Ubuntu -u root -- test -f /tmp/wsl_docker_restart.marker 2>nul
if not errorlevel 1 (
    echo.
    echo [INFO] systemd enabled. Restarting WSL to apply...
    wsl --shutdown
    timeout /t 5 /nobreak >nul
    echo [INFO] Re-running install script...
    wsl -d Ubuntu -u root -- rm -f /tmp/wsl_docker_restart.marker 2>nul
    goto run_wsl_install
)

REM Check success marker.
wsl -d Ubuntu -u root -- test -f /tmp/wsl_docker_ready.marker 2>nul
if errorlevel 1 goto :install_failed
wsl -d Ubuntu -u root -- rm -f /tmp/wsl_docker_ready.marker 2>nul
goto :install_ok

:install_failed
echo [ERROR] docker-ce 安装失败 ^(rc=!WSL_RC!^),详见上方输出。
echo [ERROR] 若反复失败,可手动进入 WSL 运行: bash install_wsl_docker.sh
pause
exit /b 1

:install_ok

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
