@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Agent OS - Web Channel

cd /d "%~dp0"
set "ROOT=%cd%"
set "LOG=%ROOT%\start_web.log"
set "PORTS_FILE=%ROOT%\.ports"

echo %date% %time% 启动脚本开始 > "%LOG%"

echo ========================================
echo   Agent OS Web Channel 启动脚本
echo ========================================
echo.
echo 项目目录: %ROOT%
echo %date% %time% 脚本启动 >> "%LOG%"

:: 检查 Python
where python >nul 2>&1 || (
    echo [ERROR] 未找到 Python，请先安装 Python
    pause
    exit /b 1
)

:: 检查 Node
where node >nul 2>&1 || (
    echo [ERROR] 未找到 Node.js，请先安装 Node.js
    pause
    exit /b 1
)

:: ========== 确保 Docker 和 Redis 就绪 ==========
call :ensure_docker_and_redis

:: ========== 关闭当前项目的旧实例 ==========
call :stop_project_instance

:: ========== 查找可用端口 ==========
echo [INFO] 正在查找可用端口...

set "BACKEND_PORT=8888"
call :find_available_port BACKEND_PORT
if !errorlevel! neq 0 (
    echo [ERROR] 无法找到可用的后端端口
    pause
    exit /b 1
)

set "FRONTEND_PORT=5188"
call :find_available_port FRONTEND_PORT
if !errorlevel! neq 0 (
    echo [ERROR] 无法找到可用的前端端口
    pause
    exit /b 1
)

echo [OK] 后端端口: !BACKEND_PORT!
echo [OK] 前端端口: !FRONTEND_PORT!

:: 保存端口到项目目录的 .ports 文件
echo BACKEND_PORT=!BACKEND_PORT!> "%PORTS_FILE%"
echo FRONTEND_PORT=!FRONTEND_PORT!>> "%PORTS_FILE%"
echo [INFO] 端口信息已保存到 %PORTS_FILE%

:: ========== 安装前端依赖 ==========
if not exist "frontend\node_modules" (
    echo [INFO] 前端依赖未安装，正在安装...
    pushd frontend && npm install && popd
    echo.
)

:: ========== 启动后端 ==========
echo [1/2] 启动后端服务器 (FastAPI + WebSocket :!BACKEND_PORT!)...
set "VITE_API_BASE_URL=http://localhost:!BACKEND_PORT!"
start "Agent OS Backend" /D "%ROOT%" cmd /c "set PYTHONPATH=src&& set BACKEND_PORT=!BACKEND_PORT!&& python start_server.py"

:: ========== 等待后端就绪 ==========
echo [INFO] 等待后端服务就绪...
set "BACKEND_READY=0"
for /L %%i in (1,1,30) do (
    if "!BACKEND_READY!"=="0" (
        curl -s -o nul http://localhost:!BACKEND_PORT!/health >nul 2>&1
        if !errorlevel! == 0 (
            set "BACKEND_READY=1"
            echo [OK] 后端已就绪
        ) else (
            timeout /t 1 /nobreak >nul
        )
    )
)
if "!BACKEND_READY!"=="0" (
    echo [WARN] 后端未在 30 秒内就绪，继续启动前端...
)

:: ========== 启动前端 ==========
echo [2/2] 启动前端开发服务器 (Vite :!FRONTEND_PORT!)...
start "Agent OS Frontend" /D "%ROOT%\frontend" cmd /c "set VITE_API_BASE_URL=http://localhost:!BACKEND_PORT!&& npm run dev -- --port !FRONTEND_PORT!"

:: ========== 等待前端就绪并打开浏览器 ==========
echo [INFO] 等待前端服务就绪...
set "FRONTEND_READY=0"
for /L %%i in (1,1,30) do (
    if "!FRONTEND_READY!"=="0" (
        curl -s -o nul http://localhost:!FRONTEND_PORT! >nul 2>&1
        if !errorlevel! == 0 (
            set "FRONTEND_READY=1"
            echo [OK] 前端已就绪
        ) else (
            timeout /t 1 /nobreak >nul
        )
    )
)
if "!FRONTEND_READY!"=="0" (
    echo [WARN] 前端未在 30 秒内就绪，尝试打开浏览器...
)

:: 打开浏览器
echo [INFO] 打开浏览器...
start "" "http://localhost:!FRONTEND_PORT!"

echo.
echo ========================================
echo   服务已启动:
echo %date% %time% 脚本完成 >> "%LOG%"
echo   后端: http://localhost:!BACKEND_PORT!
echo   前端: http://localhost:!FRONTEND_PORT!
echo   API 文档: http://localhost:!BACKEND_PORT!/docs
echo.
echo   项目目录: %ROOT%
echo   端口文件: %PORTS_FILE%
echo   关闭此窗口不会停止服务
echo   使用 stop_web.bat 停止本项目的服务
echo ========================================
echo.
pause
goto :eof


:: ========== 子程序：确保 Docker 和 Redis 就绪 ==========
:ensure_docker_and_redis
where docker >nul 2>&1 || (
    echo [WARN] 未找到 Docker，跳过 Docker/Redis 检查
    exit /b 0
)

docker info >nul 2>&1
if !errorlevel! equ 0 goto :docker_already_running

echo [INFO] Docker 未启动，正在尝试启动 Docker Desktop...
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
    echo [WARN] 未找到 Docker Desktop，跳过 Docker/Redis 检查
    exit /b 0
)

start "" "!DOCKER_DESKTOP!"
echo [INFO] 正在等待 Docker Desktop 启动...
set "DOCKER_READY=0"
for /L %%i in (1,1,60) do (
    if "!DOCKER_READY!"=="0" (
        docker info >nul 2>&1
        if !errorlevel! equ 0 (
            set "DOCKER_READY=1"
            echo [OK] Docker Desktop 已启动
        ) else (
            timeout /t 3 /nobreak >nul
        )
    )
)
if "!DOCKER_READY!"=="0" (
    echo [WARN] Docker Desktop 未能在 3 分钟内启动，继续启动
    exit /b 0
)

:docker_already_running
set "REDIS_RUNNING=0"
for /f "usebackq" %%c in (`docker ps -q -f "name=agent-os-redis" 2^>nul`) do set "REDIS_RUNNING=1"
if "!REDIS_RUNNING!"=="1" (
    echo [OK] Redis 容器已运行
    exit /b 0
)

set "REDIS_EXISTS=0"
for /f "usebackq" %%c in (`docker ps -a -q -f "name=agent-os-redis" 2^>nul`) do set "REDIS_EXISTS=1"
if "!REDIS_EXISTS!"=="1" (
    echo [INFO] Redis 容器已存在但未运行，正在启动...
    docker start agent-os-redis >nul 2>&1
    if !errorlevel! equ 0 (
        echo [OK] Redis 容器已启动
        exit /b 0
    )
)

if not exist "%ROOT%\docker-compose.yml" (
    echo [WARN] 未找到 docker-compose.yml，跳过 Redis 启动
    exit /b 0
)

echo [INFO] 正在通过 docker compose 启动 Redis...
docker compose -f "%ROOT%\docker-compose.yml" up -d redis 2>nul
if !errorlevel! neq 0 (
    echo [WARN] docker compose 启动 Redis 失败，继续启动
    exit /b 0
)

echo [INFO] 等待 Redis 就绪...
set "REDIS_READY=0"
for /L %%i in (1,1,20) do (
    if "!REDIS_READY!"=="0" (
        docker exec agent-os-redis redis-cli ping >nul 2>&1
        if !errorlevel! equ 0 (
            set "REDIS_READY=1"
            echo [OK] Redis 已就绪
        ) else (
            timeout /t 1 /nobreak >nul
        )
    )
)
if "!REDIS_READY!"=="0" (
    echo [WARN] Redis 未能在 20 秒内就绪，继续启动
)
exit /b 0


:: ========== 子程序：查找可用端口 ==========
:find_available_port
set "PORT_VAR=%~1"
set "TEST_PORT=!%PORT_VAR%!"
set /a "MAX_PORT=TEST_PORT+100"
:port_check_loop
if !TEST_PORT! gtr !MAX_PORT! (
    echo [ERROR] 在端口 !%PORT_VAR%!-!MAX_PORT! 范围内没有可用端口
    exit /b 1
)
netstat -aon 2>nul | findstr ":!TEST_PORT! " | findstr "LISTENING" >nul 2>&1
if !errorlevel! equ 0 (
    set /a "TEST_PORT+=1"
    goto port_check_loop
)
set "%PORT_VAR%=!TEST_PORT!"
exit /b 0


:: ========== 子程序：停止当前项目的旧实例 ==========
:stop_project_instance
if not exist "%PORTS_FILE%" exit /b 0

echo [INFO] 检测到本项目的旧实例，正在关闭...

set "OLD_BACKEND_PORT="
set "OLD_FRONTEND_PORT="
for /f "usebackq tokens=1,2 delims==" %%a in ("%PORTS_FILE%") do (
    if "%%a"=="BACKEND_PORT" set "OLD_BACKEND_PORT=%%b"
    if "%%a"=="FRONTEND_PORT" set "OLD_FRONTEND_PORT=%%b"
)

if not defined OLD_BACKEND_PORT goto :skip_stop_backend
for /f "tokens=5" %%p in ('netstat -aon ^| findstr ":!OLD_BACKEND_PORT! " ^| findstr "LISTENING" 2^>nul') do (
    echo [INFO] 关闭旧后端进程 PID=%%p 端口 !OLD_BACKEND_PORT!
    taskkill /F /PID %%p >nul 2>&1
)
:skip_stop_backend

if not defined OLD_FRONTEND_PORT goto :skip_stop_frontend
for /f "tokens=5" %%p in ('netstat -aon ^| findstr ":!OLD_FRONTEND_PORT! " ^| findstr "LISTENING" 2^>nul') do (
    echo [INFO] 关闭旧前端进程 PID=%%p 端口 !OLD_FRONTEND_PORT!
    taskkill /F /PID %%p >nul 2>&1
)
:skip_stop_frontend

timeout /t 2 /nobreak >nul
del "%PORTS_FILE%" 2>nul
echo [OK] 旧实例已关闭
exit /b 0
