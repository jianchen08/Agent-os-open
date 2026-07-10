@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Agent OS

cd /d "%~dp0"

echo ========================================
echo   Agent OS 启动
echo ========================================
echo 项目目录: %cd%
echo.

REM WSL shutdown retry counter (reset once at startup; bumped on each auto wsl --shutdown)
if not defined SHUTDOWN_RETRY set "SHUTDOWN_RETRY=0"

REM NOTE: we do NOT unconditionally `wsl --shutdown` at startup.
REM That would kill a running dockerd + containers and break a healthy WSL session.
REM Instead: probe first; only on deadlock does :auto_shutdown do the reset.
REM Idle-timeout suspension is prevented via .wslconfig vmIdleTimeout=-1.

:wsl_alive_entry
echo [INFO] 正在探测 WSL 是否响应...

REM Windows-side liveness probe with a hard timeout (see wsl_alive_probe.ps1).
REM rc=124 -> WSL hung -> auto wsl --shutdown retry; rc=0 -> alive, proceed;
REM other -> WSL/Ubuntu unavailable -> fall back to Docker Desktop mode.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wsl_alive_probe.ps1" -Timeout 20 >nul
set "WSL_ALIVE_RC=!errorlevel!"
REM Use goto-based branching, NOT if (...) blocks: cmd's parenthesised blocks break
REM on special chars (here the literal "(rc=" in an echo would corrupt block parsing
REM with "was unexpected at this time"). Same idiom as the rest of this script.
if "!WSL_ALIVE_RC!"=="124" goto :probe_deadlocked
if not "!WSL_ALIVE_RC!"=="0" goto :probe_other_error
goto :probe_ok

:probe_deadlocked
set "REASON=WSL 探测超时（内核可能死锁）"
goto :auto_shutdown

:probe_other_error
echo [INFO] WSL 不可用 rc=!WSL_ALIVE_RC!，回退到 Docker Desktop 模式
goto :no_wsl_docker

:probe_ok
echo [OK] WSL 响应正常

REM Portable: derive WSL path from script's own location (no hardcoded project path).
REM Note: %cd% uses backslashes; wsl/shell would eat them, so convert to slashes first.
set "WIN_DIR=%cd%"
set "WIN_DIR=%WIN_DIR:\=/%"
for /f "delims=" %%i in ('wsl -d Ubuntu -u root -- bash -c "timeout 15 wslpath -u \"%WIN_DIR%\"" 2^>nul') do set "WSL_DIR=%%i"

REM ===========================================================================
REM WSL native docker mode (replaces Docker Desktop)
REM Bypasses systemd (which has a bug that periodically stops docker.service).
REM Uses goto-based flow (cmd nested if-blocks break on special chars).
REM ===========================================================================
wsl -d Ubuntu -u root -- bash -c "timeout 30 echo wsl_ok" >nul 2>&1
if errorlevel 1 goto :no_wsl_docker

echo [INFO] WSL docker mode detected

:wsl_setup
REM 0. WSL kernel health pre-warm: detect D-state deadlock pollution so that
REM    later pgrep/docker probes are not infected and hang.
REM    Outer timeout 30s backstop (probe normally <3s; if even reading /proc
REM    hangs, the timeout will force-kill it).
echo [INFO] Checking WSL kernel health...
wsl -d Ubuntu -u root -- bash -c "timeout 30 %WSL_DIR%/wsl_health_probe.sh %WSL_DIR%" 2>&1
set "HEALTH_RC=!errorlevel!"
if "!HEALTH_RC!"=="0" goto :wsl_alive_ok
if "!HEALTH_RC!"=="8" goto :wsl_polluted
REM timeout force-kill returns 124, or other anomaly -> treat as pollution
echo [WARN] health probe abnormal (rc=!HEALTH_RC!)，treat as kernel pollution
goto :wsl_polluted

:wsl_alive_ok
REM 1. Keep WSL alive (sleep infinity in background, prevents WSL suspend)
powershell -NoProfile -Command "if (-not (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'sleep infinity' } | Select-Object -First 1)) { Start-Process wsl -ArgumentList '-d','Ubuntu','--exec','/bin/bash','-c','exec sleep infinity' -WindowStyle Hidden }" >nul 2>&1

REM 2. Ensure dockerd running (bypass systemd: start dockerd directly)
REM    Delegated to wsl_start_daemon.sh so that every pgrep/pkill/docker call
REM    (which walk /proc and can hang on D-state deadlock) is wrapped in timeout.
REM    Outer timeout 150s as backstop; rc=7 means kernel polluted -> wsl --shutdown.
wsl -d Ubuntu -u root -- bash -c "timeout 150 %WSL_DIR%/wsl_start_daemon.sh"
set "DAEMON_RC=!errorlevel!"
if "!DAEMON_RC!"=="0" goto :daemon_ok
if "!DAEMON_RC!"=="7" goto :wsl_polluted
echo [ERROR] dockerd start failed (rc=!DAEMON_RC!)，详见上方输出
goto :wsl_polluted

:daemon_ok

REM 2b. Ensure docker compose plugin accessible (symlink to cli-plugins)
wsl -d Ubuntu -u root -- bash -c "mkdir -p /usr/lib/docker/cli-plugins /root/.docker/cli-plugins; ln -sf /usr/libexec/docker/cli-plugins/docker-compose /usr/lib/docker/cli-plugins/docker-compose 2>/dev/null; ln -sf /usr/libexec/docker/cli-plugins/docker-compose /root/.docker/cli-plugins/docker-compose 2>/dev/null" >nul 2>&1

REM 3. Get WSL IP (NAT mode, may change on restart)
set "WSL_IP="
for /f "tokens=1 delims= " %%i in ('wsl -d Ubuntu -u root -- bash -c "hostname -I 2>/dev/null" 2^>nul') do set "WSL_IP=%%i"

if not defined WSL_IP (
    echo [ERROR] Cannot get WSL IP
    goto :no_wsl_docker
)

echo [OK] WSL IP: %WSL_IP%

REM 4. Setup netsh portproxy (Windows localhost -> WSL container ports)
REM    reset first to avoid duplicate rules from repeated runs
echo [INFO] Setting up port forwarding...
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-WindowStyle','Hidden','-Command','netsh interface portproxy reset; netsh interface portproxy add v4tov4 listenport=5289 listenaddress=0.0.0.0 connectport=5289 connectaddress=%WSL_IP%; netsh interface portproxy add v4tov4 listenport=6480 listenaddress=0.0.0.0 connectport=6480 connectaddress=%WSL_IP%'" 2>nul
echo [OK] Port forwarding configured

REM 5. Start project containers (delegated to wsl_ensure_containers.sh for real status check)
REM    Outer timeout 240s backstop; script internals also wrap every docker call.
echo [INFO] Starting project containers...
wsl -d Ubuntu -u root -- bash -c "timeout 240 %WSL_DIR%/wsl_ensure_containers.sh %WSL_DIR%"
set "CONTAINERS_RC=!errorlevel!"
if "!CONTAINERS_RC!"=="0" goto :containers_ok
if "!CONTAINERS_RC!"=="7" goto :cgroup_stuck
echo [ERROR] 容器启动失败 (rc=!CONTAINERS_RC!)，详见上方输出
echo [ERROR] 可尝试: wsl --shutdown 后重新运行本脚本
pause
exit /b 1

:cgroup_stuck
set "REASON=容器清理/启动受阻（cgroup 或 task 残留）"
goto :auto_shutdown

:wsl_polluted
set "REASON=WSL 内核被 D 状态死锁污染"

:auto_shutdown
set /a "SHUTDOWN_RETRY+=1"
if !SHUTDOWN_RETRY! gtr 3 (
    echo [ERROR] 已自动 wsl --shutdown 重试 !SHUTDOWN_RETRY! 次仍失败，放弃。
    echo [ERROR] 原因: !REASON!
    echo [ERROR] 请手动执行 wsl --shutdown，等待 10 秒后重新双击本脚本。
    pause
    exit /b 7
)
echo [WARN] !REASON!，自动 wsl --shutdown 后重试 ^(第 !SHUTDOWN_RETRY!/3 次^)...
REM wsl --shutdown itself may hang under kernel deadlock; wrap in timeout (wsl_shutdown.ps1).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wsl_shutdown.ps1" -Timeout 15 >nul 2>&1
echo [INFO] 等待 WSL 内核完全退出 ^(~10s^)...
REM ping-based delay avoids timeout.exe (unreliable under non-interactive shells).
ping -n 11 127.0.0.1 >nul
echo [INFO] 重新探测 WSL 是否响应...
goto :wsl_alive_entry

:containers_ok
echo [OK] Containers started

REM Frontend code auto-update (same flow as Docker Desktop mode).
REM Windows docker CLI reaches WSL daemon via DOCKER_HOST=tcp://<WSL_IP>:2375;
REM WSL IP may change after wsl --shutdown, so bind to current %WSL_IP%.
echo [INFO] 检查前端代码更新...
set "DOCKER_HOST=tcp://%WSL_IP%:2375"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_frontend.ps1"

echo.
echo [INFO] Skipping Docker Desktop checks (using WSL native docker)
goto :start_python

:no_wsl_docker
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

REM 容器名跟随 compose project（目录名），用 `docker compose ps -q <service>`
REM 动态获取容器 ID，不依赖固定容器名（避免换目录后失配）。
REM 范式与 update_frontend.ps1 一致。
set "REDIS_CID="
for /f "delims=" %%i in ('docker compose ps -q redis 2^>nul') do set "REDIS_CID=%%i"
if defined REDIS_CID (
    echo [OK] 复用已有 redis 容器 !REDIS_CID!
    docker start !REDIS_CID! >nul 2>&1
) else (
    docker compose up -d --no-recreate redis
)
set "FRONT_CID="
for /f "delims=" %%i in ('docker compose ps -q frontend 2^>nul') do set "FRONT_CID=%%i"
if defined FRONT_CID (
    echo [OK] 复用已有 frontend 容器 !FRONT_CID!
    docker start !FRONT_CID! >nul 2>&1
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
    set "DEPS_OK=0"
    "%PYEXE%" -m pip install -r requirements.txt 1>nul
    if not errorlevel 1 (
        set "DEPS_OK=1"
    ) else (
        echo [WARN] requirements.txt 安装失败，尝试 --user 模式...
        "%PYEXE%" -m pip install -r requirements.txt --user 1>nul
        if not errorlevel 1 set "DEPS_OK=1"
    )
    if "!DEPS_OK!"=="0" (
        echo [WARN] requirements.txt 不可用，回退: pip install -e .
        "%PYEXE%" -m pip install -e . 1>nul
        if not errorlevel 1 set "DEPS_OK=1"
    )
    if "!DEPS_OK!"=="1" (
        echo. > ".py_deps_installed"
        echo [OK] 依赖安装完成
    ) else (
        echo [ERROR] Python 依赖安装失败，后端可能无法启动
        echo [INFO] 请手动执行: pip install -r requirements.txt
    )
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
echo   后端: http://127.0.0.1:8988
echo   前端: http://127.0.0.1:5289
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
