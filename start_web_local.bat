@echo off
setlocal enabledelayedexpansion
title Agent OS - Local (no Docker)

cd /d "%~dp0"

echo ========================================
echo   Agent OS Local Startup (no Docker needed)
echo ========================================
echo.
echo Project dir: %cd%
echo.

:: ===========================================================================
:: 1. Detect Python (prefer 3.11-3.13, avoid the 3.14 asyncio subprocess bug)
:: ===========================================================================
set "PYEXE="
for %%v in (311 312 313) do (
    for /f "delims=" %%p in ('where python%%v 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%p"
    )
)
:: Fall back to default python if no specific minor version (may be 3.14)
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
echo.

:: ===========================================================================
:: 2. Detect Node.js (required by the frontend dev server)
:: ===========================================================================
where node >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js not found. Local startup requires Node.js to run the frontend.
    echo [INFO]  Download: https://nodejs.org/
    pause
    exit /b 1
)
echo [OK] Node:
node --version
echo.

:: ===========================================================================
:: 3. Python dependencies
:: ===========================================================================
if not exist ".py_deps_installed" (
    echo [INFO] Installing Python dependencies...
    "%PYEXE%" -m pip install -r requirements.txt 2>nul
    if errorlevel 1 "%PYEXE%" -m pip install -r requirements.txt --user 2>nul
    echo. > ".py_deps_installed"
    echo [OK] Python dependencies installed
) else (
    echo [OK] Python dependencies already installed
)
echo.

:: ===========================================================================
:: 4. Frontend dependencies
:: ===========================================================================
if not exist "frontend\node_modules" (
    echo [INFO] Installing frontend dependencies (slow on first run)...
    pushd frontend
    call npm install
    popd
    echo [OK] Frontend dependencies installed
) else (
    echo [OK] Frontend dependencies already installed
)
echo.

:: ===========================================================================
:: 5. Start backend (port 8989, in-memory fallback mode, no Redis needed)
:: ===========================================================================
echo [INFO] Starting backend (port 8989, no Redis dependency, auto in-memory mode)...
start "Agent OS Backend (local)" /D "%cd%" cmd /k "set PYTHONPATH=src&& set BACKEND_PORT=8989&& "%PYEXE%" -m channels.websocket.app_factory"

:: ===========================================================================
:: 6. Start frontend (vite dev server, port 5290, proxy to 8989)
:: ===========================================================================
echo [INFO] Starting frontend (vite dev server, port 5290)...
start "Agent OS Frontend (local)" /D "%cd%\frontend" cmd /k "set VITE_API_BASE_URL=http://localhost:8989&& set VITE_WS_BASE_URL=ws://localhost:8989&& npm run dev"

echo.
echo ========================================
echo   Startup complete
echo ========================================
echo   Backend:  http://localhost:8989
echo   Frontend: http://localhost:5290
echo   Mode:    local (no Docker / Redis in-memory fallback)
echo   Stop:    close the two popped-up windows
echo ========================================
echo.
echo [NOTE] Wait for "Application startup complete" in the backend window before opening the frontend
echo [NOTE] First visit may be slow (the backend needs to init the pipeline engine)
echo.
pause
