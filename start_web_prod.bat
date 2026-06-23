@echo off
setlocal enabledelayedexpansion
title Agent OS - Web Channel (Production)

cd /d "%~dp0"
set "ROOT=%cd%"
set "LOG=%ROOT%\start_web_prod.log"
set "PORTS_FILE=%ROOT%\.ports"

set "PROJECT_ID="
for /f "delims=" %%h in ('powershell -NoProfile -Command "[System.BitConverter]::ToString([System.Security.Cryptography.MD5]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes('%ROOT%'))).Replace('-','').Substring(0,8).ToLower()"') do set "PROJECT_ID=%%h"

echo %date% %time% Production build startup script started >> "%LOG%"

echo ========================================
echo   Agent OS Web Channel (Production)
echo ========================================
echo.
echo Project dir: %ROOT%
echo %date% %time% Script started >> "%LOG%"

:: Check Python
where python >nul 2>&1 || (
    echo [ERROR] Python not found. Please install Python first.
    pause
    exit /b 1
)

:: Check Node
where node >nul 2>&1 || (
    echo [ERROR] Node.js not found. Please install Node.js first.
    pause
    exit /b 1
)

:: ========== Check pip ==========
python -m pip --version >nul 2>&1 || (
    echo [ERROR] pip not found. Please reinstall Python and make sure pip is checked.
    pause
    exit /b 1
)

:: ========== Install Python dependencies ==========
call :install_python_deps

:: ========== Ensure Docker and Redis are ready ==========
call :ensure_docker_and_redis

:: ========== Stop old instances of the current project ==========
call :stop_project_instance

:: ========== Find available ports ==========
echo [INFO] Finding available ports...

set "BACKEND_PORT=8988"
call :find_available_port BACKEND_PORT
if !errorlevel! neq 0 (
    echo [ERROR] Cannot find an available backend port
    pause
    exit /b 1
)

set "FRONTEND_PORT=5188"
call :find_available_port FRONTEND_PORT
if !errorlevel! neq 0 (
    echo [ERROR] Cannot find an available frontend port
    pause
    exit /b 1
)

echo [OK] Backend port: !BACKEND_PORT!
echo [OK] Frontend port: !FRONTEND_PORT!
echo [OK] Project id: !PROJECT_ID!

:: Save ports to the project's .ports file
echo BACKEND_PORT=!BACKEND_PORT!> "%PORTS_FILE%"
echo FRONTEND_PORT=!FRONTEND_PORT!>> "%PORTS_FILE%"
echo PROJECT_ROOT=!ROOT!>> "%PORTS_FILE%"
echo PROJECT_ID=!PROJECT_ID!>> "%PORTS_FILE%"
echo REDIS_HOST_PORT=!REDIS_HOST_PORT!>> "%PORTS_FILE%"
echo [INFO] Port info saved to %PORTS_FILE%

:: ========== Install frontend dependencies ==========
if not exist "frontend\node_modules" (
    echo [INFO] Frontend deps not installed, installing...
    pushd frontend && npm install && popd
    echo.
)

:: ========== Build frontend ==========
echo [1/3] Building frontend production bundle (vite build)...
pushd frontend
call npx vite build
if !errorlevel! neq 0 (
    echo [ERROR] Frontend build failed, please check the code
    popd
    pause
    exit /b 1
)
popd
echo [OK] Frontend build complete
echo.

:: ========== Start backend ==========
echo [2/3] Starting backend server (FastAPI + WebSocket :!BACKEND_PORT!)...
start "Agent OS Backend - !PROJECT_ID!" /D "%ROOT%" cmd /c "set PYTHONPATH=src&& set BACKEND_PORT=!BACKEND_PORT!&& set REDIS_PORT=!REDIS_HOST_PORT!&& set _AO_PROJECT_ID=!PROJECT_ID!&& python app_factory.py"

:: ========== Start frontend preview server ==========
echo [3/3] Starting frontend production server (Vite Preview :!FRONTEND_PORT!)...
start "Agent OS Frontend (Prod) - !PROJECT_ID!" /D "%ROOT%\frontend" cmd /c "set VITE_API_BASE_URL=&& set _AO_PROJECT_ID=!PROJECT_ID!&& npx vite preview --host 0.0.0.0 --port !FRONTEND_PORT!"

:: ========== Wait for frontend ready ==========
echo [INFO] Waiting for frontend to be ready...
set "FRONTEND_READY=0"
for /L %%i in (1,1,30) do (
    if "!FRONTEND_READY!"=="0" (
        curl -s -o nul http://localhost:!FRONTEND_PORT! >nul 2>&1
        if !errorlevel! == 0 (
            set "FRONTEND_READY=1"
            echo [OK] Frontend ready
        ) else (
            timeout /t 1 /nobreak >nul
        )
    )
)
if "!FRONTEND_READY!"=="0" (
    echo [WARN] Frontend not ready within 30s, attempting to open the browser...
)

:: Get the frontend process PID
set "FRONTEND_PID="
for /f "tokens=5" %%p in ('netstat -aon 2^>nul ^| findstr ":!FRONTEND_PORT! " ^| findstr "LISTENING"') do set "FRONTEND_PID=%%p"

:: Append PID info to the .ports file
echo BACKEND_PID=!BACKEND_PID!>> "%PORTS_FILE%"
echo FRONTEND_PID=!FRONTEND_PID!>> "%PORTS_FILE%"

echo.
echo ========================================
echo   Services started (Production Mode):
echo %date% %time% Script finished >> "%LOG%"
echo   Backend:  http://localhost:!BACKEND_PORT!
echo   Frontend: http://localhost:!FRONTEND_PORT!
echo   API docs: http://localhost:!BACKEND_PORT!/docs
echo.
echo   Mode: production build (vite build + preview)
echo   Project dir: %ROOT%
echo   Ports file:  %PORTS_FILE%
echo   Closing this window will NOT stop the services
echo   Use stop_web.bat to stop this project's services
echo ========================================
echo.
pause
goto :eof


:: ========== Subroutine: install Python dependencies ==========
:install_python_deps
if not exist "%ROOT%\requirements.txt" (
    echo [WARN] requirements.txt not found, skipping dependency install
    exit /b 0
)

if exist "%ROOT%\.py_deps_installed" (
    echo [OK] Python deps already installed (delete .py_deps_installed to reinstall)
    exit /b 0
)

echo.
echo [INFO] ========================================
echo [INFO] First run, installing Python dependencies...
echo [INFO] This may take a few minutes, please wait
echo [INFO] ========================================
echo.

python -m pip install --upgrade pip --quiet >nul 2>&1

echo [INFO] Running: pip install -r requirements.txt
python -m pip install -r "%ROOT%\requirements.txt" --disable-pip-version-check 2>"%ROOT%\pip_err.tmp"
set "PIP_RC=!errorlevel!"
if !PIP_RC! equ 0 (
    del "%ROOT%\pip_err.tmp" 2>nul
    echo. > "%ROOT%\.py_deps_installed"
    echo [OK] Python dependencies installed
    exit /b 0
)

echo [WARN] Install failed (code: !PIP_RC!), attempting to fix...

if not exist "%ROOT%\pip_err.tmp" (
    echo [WARN] Python dependency install failed
    echo [INFO] Please run manually: python -m pip install -r requirements.txt
    echo [INFO] Will try to continue starting...
    exit /b 0
)

:: Strategy 1: permission denied -> --user mode (non-venv only)
findstr /r /i "PermissionError Access.is.denied" "%ROOT%\pip_err.tmp" >nul 2>&1
if !errorlevel! equ 0 if not defined VIRTUAL_ENV (
    echo [INFO] Detected a permission issue, retrying with --user mode...
    python -m pip install -r "%ROOT%\requirements.txt" --user --disable-pip-version-check 2>nul
    if !errorlevel! equ 0 (
        del "%ROOT%\pip_err.tmp" 2>nul
        echo. > "%ROOT%\.py_deps_installed"
        echo [OK] Python dependencies installed (--user mode)
        exit /b 0
    )
)

:: Strategy 2: SSL/network issue -> skip cert verification
findstr /r /i "SSL CERTIFICATE Could.not.fetch" "%ROOT%\pip_err.tmp" >nul 2>&1
if !errorlevel! equ 0 (
    echo [INFO] Detected a network or SSL issue, retrying with cert verification skipped...
    python -m pip install -r "%ROOT%\requirements.txt" --trusted-host pypi.org --trusted-host files.pythonhosted.org --disable-pip-version-check 2>nul
    if !errorlevel! equ 0 (
        del "%ROOT%\pip_err.tmp" 2>nul
        echo. > "%ROOT%\.py_deps_installed"
        echo [OK] Python dependencies installed (SSL verification skipped)
        exit /b 0
    )
)

del "%ROOT%\pip_err.tmp" 2>nul
echo [WARN] Python dependency auto-install failed
echo [INFO] Please run manually: python -m pip install -r requirements.txt
echo [INFO] Will try to continue starting...
exit /b 0


:: ========== Subroutine: ensure Docker and Redis are ready ==========
:ensure_docker_and_redis
where docker >nul 2>&1 || (
    echo [WARN] Docker not found, skipping Docker/Redis check
    exit /b 0
)

docker info >nul 2>&1
if !errorlevel! equ 0 goto :docker_already_running

echo [INFO] Docker not started, trying to start Docker Desktop...
set "DOCKER_DESKTOP="
if exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" (
    set "DOCKER_DESKTOP=C:\Program Files\Docker\Docker\Docker Desktop.exe"
)
if not defined DOCKER_DESKTOP if exist "C:\Program Files (x86)\Docker\Docker\Docker Desktop.exe" (
    set "DOCKER_DESKTOP=C:\Program Files (x86)\Docker\Docker\Docker Desktop.exe"
)
if not defined DOCKER_DESKTOP if exist "%LOCALAPPDATA%\Docker\wsl\Docker Desktop.exe" (
    set "DOCKER_DESKTOP=%LOCALAPPDATA%\Docker\wsl\Docker Desktop.exe"
)

if not defined DOCKER_DESKTOP (
    echo [WARN] Docker Desktop not found, skipping Docker/Redis check
    exit /b 0
)

start "" "!DOCKER_DESKTOP!"
echo [INFO] Waiting for Docker Desktop to start...
set "DOCKER_READY=0"
for /L %%i in (1,1,60) do (
    if "!DOCKER_READY!"=="0" (
        docker info >nul 2>&1
        if !errorlevel! equ 0 (
            set "DOCKER_READY=1"
            echo [OK] Docker Desktop started
        ) else (
            timeout /t 3 /nobreak >nul
        )
    )
)
if "!DOCKER_READY!"=="0" (
    echo [WARN] Docker Desktop did not start within 3 minutes, continuing anyway
    exit /b 0
)

:docker_already_running
set "REDIS_RUNNING=0"
for /f "usebackq" %%c in (`docker ps -q -f "name=agent-os-redis-!PROJECT_ID!" 2^>nul`) do set "REDIS_RUNNING=1"
if "!REDIS_RUNNING!"=="1" (
    echo [OK] Redis container already running
    exit /b 0
)

set "REDIS_EXISTS=0"
for /f "usebackq" %%c in (`docker ps -a -q -f "name=agent-os-redis-!PROJECT_ID!" 2^>nul`) do set "REDIS_EXISTS=1"
if "!REDIS_EXISTS!"=="1" (
    echo [INFO] Redis container exists but not running, starting...
    docker start agent-os-redis-!PROJECT_ID! >nul 2>&1
    if !errorlevel! equ 0 (
        echo [OK] Redis container started
        exit /b 0
    )
)

echo [INFO] Starting Redis container (agent-os-redis-!PROJECT_ID!)...
set "REDIS_HOST_PORT=6379"
call :find_available_port REDIS_HOST_PORT
docker run -d --name "agent-os-redis-!PROJECT_ID!" --restart unless-stopped -p !REDIS_HOST_PORT!:6379 redis:7-alpine redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru --appendonly yes >nul 2>&1
if !errorlevel! neq 0 (
    echo [WARN] Redis container failed to start, continuing anyway
    exit /b 0
)

echo [INFO] Waiting for Redis to be ready...
set "REDIS_READY=0"
for /L %%i in (1,1,20) do (
    if "!REDIS_READY!"=="0" (
        docker exec agent-os-redis-!PROJECT_ID! redis-cli ping >nul 2>&1
        if !errorlevel! equ 0 (
            set "REDIS_READY=1"
            echo [OK] Redis ready
        ) else (
            timeout /t 1 /nobreak >nul
        )
    )
)
if "!REDIS_READY!"=="0" (
    echo [WARN] Redis not ready within 20s, continuing anyway
)
exit /b 0


:: ========== Subroutine: find an available port ==========
:find_available_port
set "PORT_VAR=%~1"
set "TEST_PORT=!%PORT_VAR%!"
set /a "MAX_PORT=TEST_PORT+100"
:port_check_loop
if !TEST_PORT! gtr !MAX_PORT! (
    echo [ERROR] No available port in range !%PORT_VAR%!-!MAX_PORT!
    exit /b 1
)
netstat -aon 2>nul | findstr ":!TEST_PORT! " | findstr "LISTENING" >nul 2>&1
if !errorlevel! equ 0 (
    set /a "TEST_PORT+=1"
    goto port_check_loop
)
set "%PORT_VAR%=!TEST_PORT!"
exit /b 0


:: ========== Subroutine: stop old instances of the current project ==========
:stop_project_instance
if not exist "%PORTS_FILE%" exit /b 0

echo [INFO] Old instance of this project detected, checking...

set "OLD_BACKEND_PORT="
set "OLD_FRONTEND_PORT="
set "OLD_PROJECT_ROOT="
set "OLD_PROJECT_ID="
set "OLD_BACKEND_PID="
set "OLD_FRONTEND_PID="
for /f "usebackq tokens=1,2 delims==" %%a in ("%PORTS_FILE%") do (
    if "%%a"=="BACKEND_PORT" set "OLD_BACKEND_PORT=%%b"
    if "%%a"=="FRONTEND_PORT" set "OLD_FRONTEND_PORT=%%b"
    if "%%a"=="PROJECT_ROOT" set "OLD_PROJECT_ROOT=%%b"
    if "%%a"=="PROJECT_ID" set "OLD_PROJECT_ID=%%b"
    if "%%a"=="BACKEND_PID" set "OLD_BACKEND_PID=%%b"
    if "%%a"=="FRONTEND_PID" set "OLD_FRONTEND_PID=%%b"
)

if defined OLD_PROJECT_ROOT (
    if /i not "!ROOT!"=="!OLD_PROJECT_ROOT!" (
        echo [INFO] Ports file belongs to another project dir [!OLD_PROJECT_ROOT!], skipping stop
        del "%PORTS_FILE%" 2>nul
        exit /b 0
    )
)

if not defined OLD_BACKEND_PORT goto :skip_stop_backend
set "KILLED_BACKEND=0"
for /f "tokens=5" %%p in ('netstat -aon 2^>nul ^| findstr ":!OLD_BACKEND_PORT! " ^| findstr "LISTENING"') do (
    echo [INFO] Stopping old backend process PID=%%p port !OLD_BACKEND_PORT!
    taskkill /F /PID %%p >nul 2>&1
    set "KILLED_BACKEND=1"
)
:skip_stop_backend

if not defined OLD_FRONTEND_PORT goto :skip_stop_frontend
set "KILLED_FRONTEND=0"
for /f "tokens=5" %%p in ('netstat -aon 2^>nul ^| findstr ":!OLD_FRONTEND_PORT! " ^| findstr "LISTENING"') do (
    echo [INFO] Stopping old frontend process PID=%%p port !OLD_FRONTEND_PORT!
    taskkill /F /PID %%p >nul 2>&1
    set "KILLED_FRONTEND=1"
)
:skip_stop_frontend

timeout /t 2 /nobreak >nul
del "%PORTS_FILE%" 2>nul
echo [OK] Old instance check complete
exit /b 0
