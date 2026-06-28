@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Agent OS

cd /d "%~dp0"

echo ========================================
echo   Agent OS 启动
echo ========================================
echo.
echo 项目目录: %cd%
echo.

REM ===========================================================================
REM WSL native docker mode (replaces Docker Desktop)
REM 1. Keep WSL alive (prevent suspend that kills containers)
REM 2. Ensure dockerd running (systemd managed)
REM 3. Start project containers
REM 4. Setup netsh portproxy (Windows localhost -> WSL container ports)
REM All automatic, no manual steps.
REM ===========================================================================
wsl -d Ubuntu -u root -- echo wsl_ok >nul 2>&1
if not errorlevel 1 (
    echo [INFO] WSL docker mode detected

    REM 1. Keep WSL alive (sleep infinity in background, prevents WSL suspend)
    powershell -NoProfile -Command "if (-not (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'sleep infinity' } | Select-Object -First 1)) { Start-Process wsl -ArgumentList '-d','Ubuntu','--exec','/bin/bash','-c','exec sleep infinity' -WindowStyle Hidden }" >nul 2>&1

    REM 2. Ensure dockerd running.
    REM    NOTE: systemd has a bug on this machine that periodically stops docker.service.
    REM    So we bypass systemd: check if dockerd process alive, if not start it directly.
    wsl -d Ubuntu -u root -- bash -c "if ! pgrep -x dockerd >/dev/null 2>&1; then pkill -9 dockerd 2>/dev/null; rm -f /var/run/docker.pid; nohup dockerd >/tmp/dockerd.log 2>&1 & sleep 6; fi; for i in 1 2 3 4 5; do docker version --format '{{.Server.Version}}' 2>/dev/null | grep -q '^[0-9]' && break; sleep 2; done" >nul 2>&1

    REM 3. Get WSL IP (NAT mode, may change on restart)
    for /f "tokens=1 delims= " %%i in ('wsl -d Ubuntu -u root -- bash -c "hostname -I 2>/dev/null" 2^>nul') do (
        set "WSL_IP=%%i"
    )

    if defined WSL_IP (
        echo [OK] WSL IP: !WSL_IP!

        REM 4. Setup netsh portproxy for Windows -> WSL port forwarding
        REM    Forwards Windows localhost:PORT -> WSL_IP:PORT (container port mapping)
        REM    Needs admin; if not admin, try and warn on failure.
        echo [INFO] Setting up port forwarding (needs admin)...
        powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-WindowStyle','Hidden','-Command','netsh interface portproxy add v4tov4 listenport=5289 listenaddress=127.0.0.1 connectport=5289 connectaddress=!WSL_IP!; netsh interface portproxy add v4tov4 listenport=6480 listenaddress=127.0.0.1 connectport=6480 connectaddress=!WSL_IP!; netsh interface portproxy add v4tov4 listenport=8988 listenaddress=127.0.0.1 connectport=8988 connectaddress=!WSL_IP!'" 2>nul
        echo [OK] Port forwarding configured

        REM 5. Start project containers (compose up)
        echo [INFO] Starting project containers...
        wsl -d Ubuntu -u root -- bash -c "cd /mnt/d/myproject/container_224042d3b925 && docker compose up -d 2>&1 | tail -3"
        echo [OK] Containers started

        REM Skip Docker Desktop checks below, go straight to Python/Agent
        echo.
        echo [INFO] Skipping Docker Desktop checks (using WSL native docker)
        goto :start_python
    )
)
echo [INFO] No WSL docker found, falling back to Docker Desktop mode

:: ===========================================================================




:: ===========================================================================
echo [INFO] 清理上次残留进程...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cleanup_processes.ps1"
echo.

:: ===========================================================================

::



:: ===========================================================================
where docker >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未找到 Docker，本项目需要 Docker 才能运行
    echo [INFO] 下载: https://www.docker.com/products/docker-desktop/
    pause
    exit /b 1
)


:check_daemon
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_docker.ps1" -Timeout 90 >nul 2>&1
set "DAEMON_STATUS=!errorlevel!"
if "!DAEMON_STATUS!"=="0" goto :docker_ready


if "!DAEMON_STATUS!"=="3" goto :daemon_hung



if not defined DOCKER_WAIT_COUNT (
    if defined WSL_IP (

        echo [INFO] 启动 WSL docker 服务...
        wsl -d Ubuntu -u root -- bash -c "systemctl start docker 2>/dev/null" >nul 2>&1
    ) else (

        echo [INFO] 正在启动 Docker Desktop...
        start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe" 2>nul
    )
    set "DOCKER_WAIT_COUNT=0"
)

set /a "DOCKER_WAIT_COUNT+=1"

if !DOCKER_WAIT_COUNT! gtr 4 goto :daemon_failed
echo [INFO] 等待 Docker daemon 就绪... (!DOCKER_WAIT_COUNT!/4)
timeout /t 10 /nobreak >nul
goto :check_daemon


:daemon_hung
echo [WARN] docker daemon 90 秒内无响应（假死，非启动中）。
if defined DAEMON_RESTARTED (
    echo [WARN] 自动重启已尝试过一次，daemon 仍然假死，放弃。
    goto :daemon_failed
)
if defined WSL_IP (

    echo [INFO] 重启 WSL docker 服务...
    wsl -d Ubuntu -u root -- bash -c "systemctl restart docker 2>/dev/null" >nul 2>&1
    set "DAEMON_RESTARTED=1"
    timeout /t 5 /nobreak >nul
    goto :check_daemon
)
echo [INFO] 启动自动恢复（会弹确认框，因为会停掉运行中的容器）...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart_docker.ps1"
set "RESTART_RC=!errorlevel!"
if "!RESTART_RC!"=="0" (
    echo [OK] Docker daemon 重启后已恢复。
    set "DAEMON_RESTARTED=1"
    goto :check_daemon
)
if "!RESTART_RC!"=="2" (
    echo [INFO] 用户取消了重启，终止。
    goto :daemon_failed
)
echo [WARN] 自动重启未能恢复 daemon，终止。
goto :daemon_failed

:daemon_failed
echo [ERROR] Docker daemon 未就绪，无法启动项目。
echo [ERROR] 请手动重启 Docker Desktop 后重新运行本脚本:
echo [ERROR]   1. 右键托盘 Docker 图标 -^> Quit Docker Desktop
echo [ERROR]   2. 等待托盘图标消失（约 10 秒）
echo [ERROR]   3. 重新打开 Docker Desktop，等待图标变绿
echo [ERROR] 若仍异常: wsl --shutdown 后重启 Docker Desktop
echo [ERROR] 诊断日志: %%LOCALAPPDATA%%\Docker\log\host\com.docker.backend.exe.log
pause
exit /b 1

:docker_ready
echo [OK] Docker 就绪

:: ===========================================================================

:: ===========================================================================




echo [INFO] 启动 Docker 服务...

docker ps -a --format "{{.Names}}" | findstr "agent-os-redis-22404" >nul 2>&1
if not errorlevel 1 (
    echo [OK] 复用已有容器 agent-os-redis-22404
    docker start agent-os-redis-22404 >nul 2>&1
) else (
    docker compose up -d --no-recreate redis
)
docker ps -a --format "{{.Names}}" | findstr "agent-os-frontend-22404" >nul 2>&1
if not errorlevel 1 (
    echo [OK] 复用已有容器 agent-os-frontend-22404
    docker start agent-os-frontend-22404 >nul 2>&1
) else (
    docker compose up -d --no-recreate frontend
)
echo [OK] Docker 服务已启动


docker image inspect agent-os-frontend:latest >nul 2>&1
if errorlevel 1 (
    echo [INFO] 前端镜像不存在，需要首次构建（需要网络拉取基础镜像）
    echo [INFO] 尝试构建...
    docker compose build frontend
    if errorlevel 1 (
        echo [ERROR] 前端镜像构建失败。
        echo [ERROR] 已尝试：本地离线包（packages/）→ 多镜像链（阿里云/清华/淘宝）→ 官方源
        echo [ERROR] 排查建议:
        echo [ERROR]   1. 预下载离线包到 packages/wheels 和 packages/npm-tarballs 后重新构建
        echo [ERROR]   2. 配置 Docker daemon.json 的 registry-mirrors（国内镜像加速）
        pause
        exit /b 1
    )
    echo [OK] 前端镜像构建完成
    docker compose up -d frontend
    echo [INFO] 清理旧镜像...
    docker image prune -f 2>nul
    powershell -NoProfile -Command "Get-Date | Out-File -FilePath '.frontend_built_at' -Encoding ascii"
) else (
    echo [INFO] 检查前端代码更新...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_frontend.ps1"
)

:: ===========================================================================
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
    echo [ERROR] 未找到 Python，请安装 Python 3.11+
    pause
    exit /b 1
)
echo [OK] Python: %PYEXE%
"%PYEXE%" --version 2>&1

if not exist ".py_deps_installed" (
    echo [INFO] 安装 Python 依赖...
    "%PYEXE%" -m pip install -r requirements.txt 2>nul
    if errorlevel 1 "%PYEXE%" -m pip install -r requirements.txt --user 2>nul
    echo. > ".py_deps_installed"
    echo [OK] 依赖安装完成
) else (
    echo [OK] Python 依赖已安装
)

:: ===========================================================================

:: ===========================================================================
echo [INFO] 启动 Agent...
start "Agent OS Backend" /D "%cd%" cmd /c "set PYTHONPATH=src&& set REDIS_URL=redis://localhost:6480/0&& "%PYEXE%" -m channels.websocket.app_factory"

echo.
echo ========================================
echo   启动完成
echo ========================================
echo   后端: http://localhost:8988
echo   前端: http://localhost:5289
echo   停止: 关闭 Agent 窗口 + docker compose down
echo ========================================
pause
exit /b 0


:: ===========================================================================







:: ===========================================================================
:pull_image_with_fallback
set "IMG=%~1"


docker image inspect "%IMG%" >nul 2>&1
if not errorlevel 1 (
    echo [OK] 本地已有镜像: %IMG%
    exit /b 0
)

echo [INFO] 本地无 %IMG%，尝试拉取...
docker pull "%IMG%" >nul 2>&1
if not errorlevel 1 (
    echo [OK] 拉取成功: %IMG%
    exit /b 0
)


echo [WARN] Docker Hub 拉取失败，尝试 daocloud 镜像...
docker pull "docker.m.daocloud.io/library/%IMG%" >nul 2>&1
if errorlevel 1 (
    echo [WARN] 镜像 %IMG% 拉取失败（Docker Hub 与 daocloud 均不可用）
    echo [WARN] 后续 compose/build 会再次尝试，若仍失败请配置 daemon.json registry-mirrors
    exit /b 0
)

docker tag "docker.m.daocloud.io/library/%IMG%" "%IMG%" >nul 2>&1
if errorlevel 1 (
    echo [WARN] tag 重命名失败: docker.m.daocloud.io/library/%IMG% -^> %IMG%
    exit /b 0
)
echo [OK] 拉取成功（daocloud 回退）: %IMG%
exit /b 0
