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
echo Press any key to start (needs WSL sudo password)...
pause >nul

echo.

REM === 1. Ensure Ubuntu WSL exists ===
echo [1/5] Check WSL2 Ubuntu...
REM wsl -l -q 输出是 UTF-16LE(每字符后跟\0空字节),findstr/直接-match 都匹配不到。
REM 修法:用 PowerShell 检测,匹配前先 Replace 掉 \0 空字节。
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

REM === 2. Configure mirrored networking (so localhost works, no IP lookup) ===
echo.
echo [2/5] Configure WSL mirrored networking (localhost direct)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$cfg = \"$env:USERPROFILE\.wslconfig\";" ^
  "$content = if (Test-Path $cfg) { Get-Content $cfg -Raw } else { '' };" ^
  "if ($content -notmatch 'networkingMode') {" ^
  "  if ($content -notmatch '\[wsl2\]') { $content += \"`n[wsl2]`n\" };" ^
  "  $content = $content -replace '(\[wsl2\][^\[]*)', '$1networkingMode=mirrored`n';" ^
  "  Set-Content -Path $cfg -Value $content -Encoding UTF8;" ^
  "  Write-Host 'NEED_WSL_RESTART'" ^
  "} else { Write-Host '[OK] networkingMode already set' }" 2>&1 | findstr /C:"NEED_WSL_RESTART" >nul && (
    echo [INFO] Network mode updated. Restarting WSL to apply...
    wsl --shutdown
    timeout /t 5 /nobreak >nul
)

REM === 3. Run install script inside WSL ===
echo.
echo [3/5] Install docker-ce inside WSL2 Ubuntu...
echo [INFO] If prompted for password, enter your WSL Ubuntu user password
echo.

:run_wsl_install
wsl -d Ubuntu -- bash -c "cd /mnt/d/myproject/container_224042d3b925 && bash install_wsl_docker.sh"
set "WSL_RC=!errorlevel!"

REM exit 100 = systemd just enabled, need wsl --shutdown then rerun
if "!WSL_RC!"=="100" (
    echo.
    echo [INFO] systemd enabled. Restarting WSL to apply...
    wsl --shutdown
    timeout /t 5 /nobreak >nul
    echo [INFO] Re-running install script...
    goto run_wsl_install
)

if "!WSL_RC!"=="0" (
    echo [OK] WSL2 docker installed
) else (
    echo [ERROR] Install failed (exit code !WSL_RC!)
    echo [ERROR] Check errors above, or run manually in Ubuntu:
    echo          bash /mnt/d/myproject/container_224042d3b925/install_wsl_docker.sh
    pause
    exit /b 1
)

REM === 4. Set DOCKER_HOST (mirrored mode: localhost works) ===
echo.
echo [4/5] Configure Windows DOCKER_HOST...
setx DOCKER_HOST "tcp://localhost:2375" >nul
set "DOCKER_HOST=tcp://localhost:2375"
echo [OK] DOCKER_HOST=tcp://localhost:2375 set (takes effect in new terminals)

REM === 5. Verify Windows can reach WSL docker ===
echo.
echo [5/5] Verify Windows can connect to WSL docker (waiting for daemon)...
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
    echo [OK] Windows connected to WSL2 docker successfully!
    for /f "delims=" %%v in ('docker version --format "{{.Server.Version}}" 2^>nul') do set "DOCKER_VER=%%v"
    echo      docker Server version: !DOCKER_VER!
) else (
    echo [WARN] Cannot connect yet. May need to reopen terminal for DOCKER_HOST.
    echo [WARN] After reopening cmd, run: docker version
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
echo   4. Start project with start_web_cn.bat (code already supports WSL path)
echo.
echo NOTE: Before uninstalling Docker Desktop, make sure "docker version" works.
echo.
pause
exit /b 0
