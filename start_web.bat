@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Agent OS - Web Channel

:: 切换到脚本所在目录
cd /d "%~dp0"
set "ROOT=%cd%"
set "LOG=%ROOT%\start_web.log"

echo %date% %time% 启动脚本开始 > "%LOG%"

echo ========================================
echo   Agent OS Web Channel 启动脚本
echo ========================================
echo.
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

:: ========== 关闭旧进程 ==========
echo [INFO] 检查并关闭旧进程...

:: 关闭残留的 Agent OS 窗口
taskkill /F /FI "WINDOWTITLE eq Agent OS Backend*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Agent OS Frontend*" >nul 2>&1

:: 循环杀端口进程（最多重试3次，确保杀干净）
set "KILL_RETRIES=0"
:kill_loop
set /a "KILL_RETRIES+=1"
if !KILL_RETRIES! gtr 3 goto kill_done

:: 杀 8888 端口
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8888 " ^| findstr "LISTENING" 2^>nul') do (
    echo [INFO] 关闭旧后端进程 PID=%%a
    taskkill /F /PID %%a >nul 2>&1
)

:: 杀 5188 端口
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5188 " ^| findstr "LISTENING" 2^>nul') do (
    echo [INFO] 关闭旧前端进程 PID=%%a
    taskkill /F /PID %%a >nul 2>&1

)

:: 检查端口是否已释放
timeout /t 2 /nobreak >nul
set "PORT_STILL_USED=0"
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8888 " ^| findstr "LISTENING" 2^>nul') do set "PORT_STILL_USED=1"
if "!PORT_STILL_USED!"=="1" goto kill_loop

:kill_done
echo [INFO] 端口已清理

:: ========== 安装前端依赖 ==========
if not exist "frontend\node_modules" (
    echo [INFO] 前端依赖未安装，正在安装...
    pushd frontend && npm install && popd
    echo.
)

:: ========== 启动后端 ==========
echo [1/2] 启动后端服务器 (FastAPI + WebSocket :8888)...
start "Agent OS Backend" /D "%ROOT%" cmd /c "set PYTHONPATH=src && python start_server.py"

:: ========== 等待后端就绪 ==========
echo [INFO] 等待后端服务就绪...
set "BACKEND_READY=0"
for /L %%i in (1,1,30) do (
    if "!BACKEND_READY!"=="0" (
        curl -s -o nul http://localhost:8888/health >nul 2>&1
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
echo [2/2] 启动前端开发服务器 (Vite :5188)...
start "Agent OS Frontend" /D "%ROOT%\frontend" cmd /c "npm run dev"

:: ========== 等待前端就绪并打开浏览器 ==========
echo [INFO] 等待前端服务就绪...
set "FRONTEND_READY=0"
for /L %%i in (1,1,30) do (
    if "!FRONTEND_READY!"=="0" (
        curl -s -o nul http://localhost:5188 >nul 2>&1
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
start "" "http://localhost:5188"

echo.
echo ========================================
echo   服务已启动:
echo %date% %time% 脚本完成 >> "%LOG%"
echo   后端: http://localhost:8888
echo   前端: http://localhost:5188
echo   API 文档: http://localhost:8888/docs
echo.
echo   关闭此窗口不会停止服务
echo   再次运行本脚本会自动重启
echo ========================================
echo.
pause
