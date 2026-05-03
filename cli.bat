@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

:: 清除所有 pycache
for /d /r src %%d in (__pycache__) do (
    if exist "%%d" rd /s /q "%%d" 2>nul
)

set PYTHONPATH=src
echo Starting Agent OS CLI...
python -m channels.cli.cli_main %*
