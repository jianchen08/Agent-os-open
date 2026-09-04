@echo off
chcp 65001 >nul
REM ============================================================
REM  AgentOS 0.2 Stopper (Windows)
REM
REM  Port-targeted kill, same strategy as start_web_02.bat's cleanup:
REM  find PIDs LISTENING on our ports via netstat -ano, then
REM  taskkill /F /T (tree kill), plus an image-name fallback for
REM  instances bound to other ports (AGENTOS_KERNEL_PORT override).
REM
REM  Env vars (same defaults as start_web_02.bat):
REM    AGENTOS_KERNEL_PORT    default 9100
REM    AGENTOS_FRONTEND_PORT  default 6390
REM
REM  Linux/macOS: use stop_web_02.sh instead (PID bookkeeping via .ports_02).
REM  ============================================================
setlocal EnableDelayedExpansion

cd /d "%~dp0"
if not defined AGENTOS_KERNEL_PORT set "AGENTOS_KERNEL_PORT=9100"
if not defined AGENTOS_FRONTEND_PORT set "AGENTOS_FRONTEND_PORT=6390"

echo ========================================
echo   AgentOS 0.2 Stopper
echo ========================================
echo   kernel:   :%AGENTOS_KERNEL_PORT%
echo   frontend: :%AGENTOS_FRONTEND_PORT%
echo.

set "STOPPED=0"

call :KillPort "%AGENTOS_KERNEL_PORT%" "kernel" && set "STOPPED=1"
call :KillPort "%AGENTOS_FRONTEND_PORT%" "frontend" && set "STOPPED=1"

REM Image-name fallback: product-unique image, cannot hit unrelated projects.
tasklist /FI "IMAGENAME eq agentos-kernel.exe" 2>nul | findstr /I "agentos-kernel" >nul 2>&1
if not errorlevel 1 (
    echo        [STOP] killing lingering agentos-kernel.exe by image name
    taskkill /F /IM agentos-kernel.exe >nul 2>&1
    set "STOPPED=1"
)

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
