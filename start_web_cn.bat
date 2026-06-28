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
REM WSL docker 模式：自动刷新 DOCKER_HOST（WSL 的 IP 每次重启可能变化）
REM 检测是否装了 WSL 原生 docker，是则获取当前 IP 更新 DOCKER_HOST。
REM 这样用户无需手动同步，双击本脚本即可。
REM ===========================================================================
wsl -d Ubuntu -u root -- echo wsl_ok >nul 2>&1
if not errorlevel 1 (
    REM 获取 WSL eth0 的 172.x IP（避免 PowerShell 的 $_ 在某些环境被转义）
    for /f "tokens=1 delims= " %%i in ('wsl -d Ubuntu -u root -- bash -c "hostname -I 2>/dev/null" 2^>nul') do (
        set "WSL_IP=%%i"
    )
    if defined WSL_IP (
        set "DOCKER_HOST=tcp://!WSL_IP!:2375"
        echo [OK] WSL docker: !DOCKER_HOST!
    )
)

:: ===========================================================================
REM 0. 清理上次残留的宿主机进程（避免端口/资源占用导致重复启动失败）
REM 只关闭与本项目相关的进程：后端入口(channels.websocket.app_factory)、
REM 以及可执行文件位于项目目录下的进程。
REM Docker 容器内的服务不受影响。详见 cleanup_processes.ps1
:: ===========================================================================
echo [INFO] 清理上次残留进程...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cleanup_processes.ps1"
echo.

:: ===========================================================================
REM 1. 检查 Docker（本项目必须有 Docker）
::
REM 注意：docker info 在 daemon 假死时会无限期阻塞（不是返回失败码），
REM 直接调用会导致脚本永久卡住。这里用独立的 check_docker.ps1 做带超时的
REM 健康检查（每次最多等 90 秒），避免阻塞。
:: ===========================================================================
where docker >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未找到 Docker，本项目需要 Docker 才能运行
    echo [INFO] 下载: https://www.docker.com/products/docker-desktop/
    pause
    exit /b 1
)

REM daemon 健康检查（带 90 秒超时，给 Docker Desktop 冷启动足够时间）
:check_daemon
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_docker.ps1" -Timeout 90 >nul 2>&1
set "DAEMON_STATUS=!errorlevel!"
if "!DAEMON_STATUS!"=="0" goto :docker_ready

REM 退出码: 0=就绪 1=未就绪(启动中) 3=超时(假死)
if "!DAEMON_STATUS!"=="3" goto :daemon_hung

REM --- daemon 未就绪（状态 1）：正在启动，继续等待 ---
REM 首次进入等待时启动 docker daemon
if not defined DOCKER_WAIT_COUNT (
    if defined WSL_IP (
        REM WSL docker 模式：启动 WSL 里的 docker 服务
        echo [INFO] 启动 WSL docker 服务...
        wsl -d Ubuntu -u root -- bash -c "systemctl start docker 2>/dev/null" >nul 2>&1
    ) else (
        REM Docker Desktop 模式：启动 Docker Desktop
        echo [INFO] 正在启动 Docker Desktop...
        start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe" 2>nul
    )
    set "DOCKER_WAIT_COUNT=0"
)

set /a "DOCKER_WAIT_COUNT+=1"
REM 最多等待 4 轮（每轮含 90 秒探测 + 10 秒间隔，约 7 分钟）
if !DOCKER_WAIT_COUNT! gtr 4 goto :daemon_failed
echo [INFO] 等待 Docker daemon 就绪... (!DOCKER_WAIT_COUNT!/4)
timeout /t 10 /nobreak >nul
goto :check_daemon

REM --- daemon 假死（状态 3）：触发自动恢复，而不是干等 ---
:daemon_hung
echo [WARN] docker daemon 90 秒内无响应（假死，非启动中）。
if defined DAEMON_RESTARTED (
    echo [WARN] 自动重启已尝试过一次，daemon 仍然假死，放弃。
    goto :daemon_failed
)
if defined WSL_IP (
    REM WSL docker 模式：直接重启 WSL docker 服务（比 restart_docker.ps1 快）
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
REM 2. Docker 服务（Redis + Frontend）
:: ===========================================================================
REM 基础镜像仅在重新构建前端时才需要。
REM 一旦 agent-os-frontend:latest 已存在，compose up 不再需要它们。
REM 这里跳过预热。docker compose up 若缺 redis 会通过 daemon.json 配置的
REM 镜像加速源拉取，很快。
echo [INFO] 启动 Docker 服务...
REM 检查容器是否已存在（包括停止的容器），存在则直接启动，避免冲突
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

REM 前端代码更新：镜像存在时检查 src 是否有更新，有则构建并注入运行中的容器
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
REM 3. Python + 依赖（优先 3.12，避免 3.14 asyncio subprocess bug）
:: ===========================================================================
set "PYEXE="

REM 方式1：查找带版本号的命令别名（python312/python311/python313）
for %%v in (312 311 313) do (
    for /f "delims=" %%p in ('where python%%v 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%p"
    )
)

REM 方式2：探测常见安装路径（where python312 在多数机器找不到，需路径兜底）
REM 用 set 预存路径 + if exist 串联，避免 for 循环里 %ProgramFiles(x86)% 括号转义问题
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

REM 方式3：最后回退到默认 python（可能是 3.14，有 asyncio subprocess bug 风险）
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
REM 4. Agent（宿主机）
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
REM 子程序：拉取镜像（本地优先，缺失走多镜像链回退）
REM 用法: call :pull_image_with_fallback "image:tag"
REM 策略:
REM 1) 本地已存在 → 跳过
REM 2) docker pull <image>（Docker Hub）
REM 3) docker pull <daocloud 镜像> → docker tag 回原名
REM 4) 全部失败 → 仅告警，不阻断（让 compose/build 自己再试）
:: ===========================================================================
:pull_image_with_fallback
set "IMG=%~1"

REM 本地已有则跳过
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

REM 回退：daocloud 镜像加速 + tag 回原名
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
