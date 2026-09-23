@echo off
chcp 65001 >nul
REM ============================================================
REM  AgentOS 0.2 Stopper (Windows)
REM
REM  Port-targeted kill, same strategy as start_web_02.bat's cleanup:
REM  find PIDs LISTENING on our ports via netstat -ano, then
REM  taskkill /F /T (tree kill), plus a path-scoped fallback (this repo's
REM  target\release only) for instances bound to other ports
REM  (AGENTOS_KERNEL_PORT override).
REM
REM  Env vars (same defaults as start_web_02.bat):
REM    AGENTOS_KERNEL_PORT    default 9100
REM    AGENTOS_FRONTEND_PORT  default 6390
REM
REM  Linux/macOS: use stop_web_02.sh instead (PID bookkeeping via .ports_02).
REM  ============================================================
setlocal EnableDelayedExpansion

cd /d "%~dp0"
set "KERNEL_DIR=%cd%\kernel"
if not defined AGENTOS_KERNEL_PORT set "AGENTOS_KERNEL_PORT=9100"
if not defined AGENTOS_FRONTEND_PORT set "AGENTOS_FRONTEND_PORT=6390"

echo ========================================
echo   AgentOS 0.2 Stopper
echo ========================================
echo   kernel:   :%AGENTOS_KERNEL_PORT%
echo   frontend: :%AGENTOS_FRONTEND_PORT%
echo.

set "STOPPED=0"

REM Kill leftover G8 supervisor cmd trees FIRST, else the supervisor
REM respawns the kernel right after the kills below (any-exit respawn
REM contract) and the stop does not stick.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*run_kernel_supervised.bat*' } | ForEach-Object { Write-Host ('       [STOP] killing leftover supervisor tree PID ' + $_.ProcessId); taskkill /F /T /PID $_.ProcessId 2>&1 | Out-Null }"

call :KillPort "%AGENTOS_KERNEL_PORT%" "kernel" && set "STOPPED=1"
call :KillPort "%AGENTOS_FRONTEND_PORT%" "frontend" && set "STOPPED=1"

REM Path-scoped fallback for instances the port scan missed (AGENTOS_KERNEL_PORT
REM override runs): kill only kernels whose exe lives under THIS repo's
REM target\release. The installed app runs its own same-name exe (bundled
REM kernel, child of the running app, port 9101) - it must survive dev stop
REM (2026-09-20 dual-stack coexistence).
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'agentos-kernel.exe' -and $_.ExecutablePath -like '%KERNEL_DIR%\target\release\*' } | ForEach-Object { Write-Host ('       [STOP] killing dev kernel PID ' + $_.ProcessId); taskkill /F /T /PID $_.ProcessId 2>&1 | Out-Null }"

echo.
if "%STOPPED%"=="1" (
    echo [OK] AgentOS 0.2 services stopped.
) else (
    echo [INFO] No running AgentOS 0.2 services found on ports %AGENTOS_KERNEL_PORT% / %AGENTOS_FRONTEND_PORT%.
)
echo.
pause
endlocal
exit /b 0

REM ------------------------------------------------------------
REM  KillPort <port> <label>; returns errorlevel 1 if nothing killed
REM  netstat -ano columns: Proto Local Foreign State PID -> tokens=5
REM  (findstr /C:":port " with trailing space avoids :91001-style
REM   mismatches; LISTENING filter avoids killing outbound clients)
REM ------------------------------------------------------------
:KillPort
set "KILLPORT_FOUND=0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":%~1 " ^| findstr /C:"LISTENING"') do (
    echo        [STOP] %~2: killing PID %%p on port %~1
    taskkill /F /T /PID %%p >nul 2>&1
    set "KILLPORT_FOUND=1"
)
if "%KILLPORT_FOUND%"=="0" (
    echo        [STOP] %~2: no listener on port %~1
    exit /b 1
)
exit /b 0
