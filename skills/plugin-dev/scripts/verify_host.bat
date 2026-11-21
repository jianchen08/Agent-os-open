@echo off
rem 宿主验证固定入口（WSL interop 调用；参数少而固定，避免内联引号被双重复原）。
rem 用法: verify_host.bat <workspace_win> <plugin_rel_or_win> [frontend] [kernel_bin_win]
rem
rem 退出码经 %WS%\.agentos_verify_rc 落盘回传：WSL interop 在本机实测**不传播**
rem Windows 进程退出码（cmd /c exit 7 从 WSL 看恒 0），只靠 $? 会假绿。
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "WS=%~1"
set "PLUGIN=%~2"
set "MODE=%~3"
set "KERNEL=%~4"
set "PY=%AGENTOS_HOST_PYTHON%"
if "%PY%"=="" set "PY=python"
cd /d "%WS%"
set "FE="
if /I "%MODE%"=="frontend" set "FE=--frontend"
set "KB="
if not "%KERNEL%"=="" set KB=--kernel-bin "%KERNEL%"
"%PY%" "%~dp0verify_host.py" "%PLUGIN%" --workspace "%WS%" %FE% %KB%
set "RC=%ERRORLEVEL%"
> "%WS%\.agentos_verify_rc" echo %RC%
exit /b %RC%
