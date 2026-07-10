@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Agent OS 一键部署

cd /d "%~dp0"

echo ========================================
echo   Agent OS 一键部署 (Windows)
echo ========================================
echo.
echo 项目目录: %cd%
echo.

:: ===========================================================================
:: 阶段 1: bootstrap —— 安装 Docker + 选择后端
:: ===========================================================================

:: --- 1.1 检查 Docker 是否已安装 ---
echo [INFO] [1/3] 检查 Docker...
where docker >nul 2>&1
set "DOCKER_FOUND=!errorlevel!"

if "!DOCKER_FOUND!"=="1" (
    :: 检查 Docker Desktop 是否在默认路径(可能未加入 PATH)
    if exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" (
        echo [OK] Docker Desktop 已安装(未加入 PATH,将使用默认路径)
        set "DOCKER_FOUND=0"
    )
)

if "!DOCKER_FOUND!"=="1" (
    echo [INFO] 未检测到 Docker,尝试自动安装...
    call :install_docker
    set "INSTALL_RC=!errorlevel!"
    if "!INSTALL_RC!"=="0" (
        echo [OK] Docker Desktop 安装完成
        echo [WARN] 首次安装需要重启/重新登录后 Docker 才能使用
        echo [WARN] 请重启电脑后重新运行本脚本
        echo.
        pause
        exit /b 3010
    )
    if "!INSTALL_RC!"=="2" (
        echo [INFO] 用户取消安装,终止。
        exit /b 2
    )
    echo [ERROR] Docker 自动安装失败
    echo [ERROR] 请手动安装: https://www.docker.com/products/docker-desktop/
    pause
    exit /b 1
)

echo [OK] Docker 已安装

:: --- 1.2 选择 Docker 后端(专业版优先 Hyper-V,避免 WSL2 卡死) ---
echo.
echo [INFO] [2/3] 配置 Docker 后端(优化稳定性)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_backend.ps1" 2>&1
set "BACKEND_RC=!errorlevel!"

if "!BACKEND_RC!"=="1" (
    :: 退出码 1 = 需要重启使 Hyper-V 生效
    echo.
    echo [INFO] 需要重启 Windows 使 Hyper-V 后端生效。
    echo [INFO] 重启后请重新运行本脚本完成部署。
    echo.
    set /p "REBOOT_NOW=现在重启吗? [y/N]: "
    if /i "!REBOOT_NOW!"=="y" (
        shutdown /r /t 5 /c "Agent OS 部署:启用 Hyper-V 后端"
        echo [INFO] 5 秒后重启,取消请运行 shutdown /a
        exit /b 0
    )
    exit /b 1
)
if "!BACKEND_RC!"=="2" (
    echo [WARN] 后端配置出错,将使用默认后端继续
)

:: --- 1.3 检查 winget 可用性(后续步骤可能需要) ---
:: (Docker 已就绪的情况下,winget 非必需,跳过检查)

:: ===========================================================================
:: 阶段 2: deploy —— 复用现有 start_web_cn.bat 的完整启动流程
:: ===========================================================================
echo.
echo [INFO] [3/3] 启动 Agent OS 服务...
echo [INFO] 调用 start_web_cn.bat (daemon 检查 / 镜像构建 / 服务启动)
echo ========================================
echo.

call "%~dp0start_web_cn.bat"
set "DEPLOY_RC=!errorlevel!"

if "!DEPLOY_RC!"=="0" (
    echo.
    echo ========================================
    echo   部署完成
    echo ========================================
)

exit /b !DEPLOY_RC!


:: ===========================================================================
:: 子程序: install_docker —— winget 自动安装 Docker Desktop
:: 退出码: 0=成功, 1=失败, 2=用户取消
:: ===========================================================================
:install_docker

:: 检查 winget
where winget >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未找到 winget,无法自动安装
    echo [ERROR] Windows 10 1809+ 自带 winget,请通过 Microsoft Store 更新"应用安装程序"
    exit /b 1
)

echo.
echo 将通过 winget 安装 Docker Desktop
echo 这会下载约 500MB,安装后需要重启/重新登录
echo.
set /p "CONFIRM=确认安装 Docker Desktop? [y/N]: "
if /i not "!CONFIRM!"=="y" exit /b 2

echo [INFO] 正在安装 Docker Desktop(可能需要几分钟)...
:: -e --id: 精确匹配包ID, --silent: 静默安装, --accept-*: 自动接受协议
winget install -e --id Docker.DockerDesktop --silent --accept-package-agreements --accept-source-agreements
set "WINGET_RC=!errorlevel!"

if "!WINGET_RC!"=="0" (
    :: 安装成功,但 Docker Desktop 需要重启/重登才能初始化 WSL2/Hyper-V 后端
    exit /b 0
)

:: winget 常见错误码:已安装(返回 0x8A15002B 等)、网络失败等
echo [WARN] winget 安装返回码: !WINGET_RC!
echo [WARN] 可能已安装或安装失败,请检查 Docker Desktop 是否在 PATH 中
exit /b 1
