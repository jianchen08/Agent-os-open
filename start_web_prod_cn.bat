@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Agent OS - Web Channel (Production)

cd /d "%~dp0"
set "ROOT=%cd%"
set "LOG=%ROOT%\start_web_prod.log"
set "PORTS_FILE=%ROOT%\.ports"

set "PROJECT_ID="
for /f "delims=" %%h in ('powershell -NoProfile -Command "[System.BitConverter]::ToString([System.Security.Cryptography.MD5]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes('%ROOT%'))).Replace('-','').Substring(0,8).ToLower()"') do set "PROJECT_ID=%%h"

echo %date% %time% 生产构建启动脚本开始 >> "%LOG%"

echo ========================================
echo   Agent OS Web Channel (Production)
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

:: ========== 检查 pip ==========
python -m pip --version >nul 2>&1 || (
    echo [ERROR] pip 未找到，请重新安装 Python 并确保勾选 pip
    pause
    exit /b 1
)

:: ========== 安装 Python 依赖 ==========
call :install_python_deps

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
echo [OK] 项目标识: !PROJECT_ID!

:: 保存端口到项目目录的 .ports 文件
echo BACKEND_PORT=!BACKEND_PORT!> "%PORTS_FILE%"
echo FRONTEND_PORT=!FRONTEND_PORT!>> "%PORTS_FILE%"
echo PROJECT_ROOT=!ROOT!>> "%PORTS_FILE%"
echo PROJECT_ID=!PROJECT_ID!>> "%PORTS_FILE%"
echo REDIS_HOST_PORT=!REDIS_HOST_PORT!>> "%PORTS_FILE%"
echo [INFO] 端口信息已保存到 %PORTS_FILE%

:: ========== 安装前端依赖 ==========
if not exist "frontend\node_modules" (
    echo [INFO] 前端依赖未安装，正在安装...
    pushd frontend && npm install && popd
    echo.
)

:: ========== 构建前端 ==========
echo [1/3] 构建前端生产包 (vite build)...
pushd frontend
call npx vite build
if !errorlevel! neq 0 (
    echo [ERROR] 前端构建失败，请检查代码
    popd
    pause
    exit /b 1
)
popd
echo [OK] 前端构建完成
echo.

:: ========== 启动后端 ==========
echo [2/3] 启动后端服务器 (FastAPI + WebSocket :!BACKEND_PORT!)...
start "Agent OS Backend - !PROJECT_ID!" /D "%ROOT%" cmd /c "set PYTHONPATH=src&& set BACKEND_PORT=!BACKEND_PORT!&& set REDIS_PORT=!REDIS_HOST_PORT!&& set _AO_PROJECT_ID=!PROJECT_ID!&& python app_factory.py"

:: ========== 启动前端预览服务器 ==========
echo [3/3] 启动前端生产服务器 (Vite Preview :!FRONTEND_PORT!)...
start "Agent OS Frontend (Prod) - !PROJECT_ID!" /D "%ROOT%\frontend" cmd /c "set VITE_API_BASE_URL=&& set _AO_PROJECT_ID=!PROJECT_ID!&& npx vite preview --host 0.0.0.0 --port !FRONTEND_PORT!"

:: ========== 等待前端就绪 ==========
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

:: 获取前端进程 PID
set "FRONTEND_PID="
for /f "tokens=5" %%p in ('netstat -aon 2^>nul ^| findstr ":!FRONTEND_PORT! " ^| findstr "LISTENING"') do set "FRONTEND_PID=%%p"

:: 更新 .ports 文件，追加 PID 信息
echo BACKEND_PID=!BACKEND_PID!>> "%PORTS_FILE%"
echo FRONTEND_PID=!FRONTEND_PID!>> "%PORTS_FILE%"

echo.
echo ========================================
echo   服务已启动 (Production Mode):
echo %date% %time% 脚本完成 >> "%LOG%"
echo   后端: http://localhost:!BACKEND_PORT!
echo   前端: http://localhost:!FRONTEND_PORT!
echo   API 文档: http://localhost:!BACKEND_PORT!/docs
echo.
echo   模式: 生产构建 (vite build + preview)
echo   项目目录: %ROOT%
echo   端口文件: %PORTS_FILE%
echo   关闭此窗口不会停止服务
echo   使用 stop_web.bat 停止本项目的服务
echo ========================================
echo.
pause
goto :eof


:: ========== 子程序：安装 Python 依赖 ==========
:install_python_deps
if not exist "%ROOT%\requirements.txt" (
    echo [WARN] requirements.txt 未找到，跳过依赖安装
    exit /b 0
)

if exist "%ROOT%\.py_deps_installed" (
    echo [OK] Python 依赖已安装（如需重装请删除 .py_deps_installed）
    exit /b 0
)

echo.
echo [INFO] ========================================
echo [INFO] 首次运行，正在安装 Python 依赖...
echo [INFO] 这可能需要几分钟，请耐心等待
echo [INFO] ========================================
echo.

python -m pip install --upgrade pip --quiet >nul 2>&1

echo [INFO] 执行: pip install -r requirements.txt
python -m pip install -r "%ROOT%\requirements.txt" --disable-pip-version-check 2>"%ROOT%\pip_err.tmp"
set "PIP_RC=!errorlevel!"
if !PIP_RC! equ 0 (
    del "%ROOT%\pip_err.tmp" 2>nul
    echo. > "%ROOT%\.py_deps_installed"
    echo [OK] Python 依赖安装完成
    exit /b 0
)

echo [WARN] 安装失败（错误码: !PIP_RC!），正在尝试修复...

if not exist "%ROOT%\pip_err.tmp" (
    echo [WARN] Python 依赖安装失败
    echo [INFO] 请手动执行: python -m pip install -r requirements.txt
    echo [INFO] 将尝试继续启动...
    exit /b 0
)

:: 策略1: 权限不足 → --user 模式（非虚拟环境下）
findstr /r /i "PermissionError Access.is.denied" "%ROOT%\pip_err.tmp" >nul 2>&1
if !errorlevel! equ 0 if not defined VIRTUAL_ENV (
    echo [INFO] 检测到权限问题，使用 --user 模式重试...
    python -m pip install -r "%ROOT%\requirements.txt" --user --disable-pip-version-check 2>nul
    if !errorlevel! equ 0 (
        del "%ROOT%\pip_err.tmp" 2>nul
        echo. > "%ROOT%\.py_deps_installed"
        echo [OK] Python 依赖安装完成（--user 模式）
        exit /b 0
    )
)

:: 策略2: SSL/网络问题 → 跳过证书验证
findstr /r /i "SSL CERTIFICATE Could.not.fetch" "%ROOT%\pip_err.tmp" >nul 2>&1
if !errorlevel! equ 0 (
    echo [INFO] 检测到网络或 SSL 问题，跳过证书验证重试...
    python -m pip install -r "%ROOT%\requirements.txt" --trusted-host pypi.org --trusted-host files.pythonhosted.org --disable-pip-version-check 2>nul
    if !errorlevel! equ 0 (
        del "%ROOT%\pip_err.tmp" 2>nul
        echo. > "%ROOT%\.py_deps_installed"
        echo [OK] Python 依赖安装完成（跳过 SSL 验证）
        exit /b 0
    )
)

del "%ROOT%\pip_err.tmp" 2>nul
echo [WARN] Python 依赖自动安装失败
echo [INFO] 请手动执行: python -m pip install -r requirements.txt
echo [INFO] 将尝试继续启动...
exit /b 0


:: ========== 子程序：确保 Docker 和 Redis 就绪 ==========
:ensure_docker_and_redis
where docker >nul 2>&1 || (
    echo [WARN] 未找到 Docker，跳过 Docker/Redis 检查
    exit /b 0
)

:: 用带超时的 check_docker.ps1 替代裸 docker info，避免 daemon 假死时无限阻塞
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_docker.ps1" -Timeout 30 >nul 2>&1
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
        powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_docker.ps1" -Timeout 10 >nul 2>&1
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
for /f "usebackq" %%c in (`docker ps -q -f "name=agent-os-redis-!PROJECT_ID!" 2^>nul`) do set "REDIS_RUNNING=1"
if "!REDIS_RUNNING!"=="1" (
    echo [OK] Redis 容器已运行
    exit /b 0
)

set "REDIS_EXISTS=0"
for /f "usebackq" %%c in (`docker ps -a -q -f "name=agent-os-redis-!PROJECT_ID!" 2^>nul`) do set "REDIS_EXISTS=1"
if "!REDIS_EXISTS!"=="1" (
    echo [INFO] Redis 容器已存在但未运行，正在启动...
    docker start agent-os-redis-!PROJECT_ID! >nul 2>&1
    if !errorlevel! equ 0 (
        echo [OK] Redis 容器已启动
        exit /b 0
    )
)

echo [INFO] 正在启动 Redis 容器 (agent-os-redis-!PROJECT_ID!)...
set "REDIS_HOST_PORT=6379"
call :find_available_port REDIS_HOST_PORT
docker run -d --name "agent-os-redis-!PROJECT_ID!" --restart unless-stopped -p !REDIS_HOST_PORT!:6379 redis:7-alpine redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru --appendonly yes >nul 2>&1
if !errorlevel! neq 0 (
    echo [WARN] Redis 容器启动失败，继续启动
    exit /b 0
)

echo [INFO] 等待 Redis 就绪...
set "REDIS_READY=0"
for /L %%i in (1,1,20) do (
    if "!REDIS_READY!"=="0" (
        docker exec agent-os-redis-!PROJECT_ID! redis-cli ping >nul 2>&1
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

echo [INFO] 检测到本项目的旧实例，正在检查...

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
        echo [INFO] 端口文件属于其他项目目录 [!OLD_PROJECT_ROOT!]，跳过关闭
        del "%PORTS_FILE%" 2>nul
        exit /b 0
    )
)

if not defined OLD_BACKEND_PORT goto :skip_stop_backend
set "KILLED_BACKEND=0"
for /f "tokens=5" %%p in ('netstat -aon 2^>nul ^| findstr ":!OLD_BACKEND_PORT! " ^| findstr "LISTENING"') do (
    echo [INFO] 关闭旧后端进程 PID=%%p 端口 !OLD_BACKEND_PORT!
    taskkill /F /PID %%p >nul 2>&1
    set "KILLED_BACKEND=1"
)
:skip_stop_backend

if not defined OLD_FRONTEND_PORT goto :skip_stop_frontend
set "KILLED_FRONTEND=0"
for /f "tokens=5" %%p in ('netstat -aon 2^>nul ^| findstr ":!OLD_FRONTEND_PORT! " ^| findstr "LISTENING"') do (
    echo [INFO] 关闭旧前端进程 PID=%%p 端口 !OLD_FRONTEND_PORT!
    taskkill /F /PID %%p >nul 2>&1
    set "KILLED_FRONTEND=1"
)
:skip_stop_frontend

timeout /t 2 /nobreak >nul
del "%PORTS_FILE%" 2>nul
echo [OK] 旧实例检查完成
exit /b 0
