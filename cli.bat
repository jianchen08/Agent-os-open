@echo off
cd /d "%~dp0"

:: Clear all __pycache__
for /d /r src %%d in (__pycache__) do (
    if exist "%%d" rd /s /q "%%d" 2>nul
)

set PYTHONPATH=src
echo Starting Agent OS CLI...
python -m channels.cli.cli_main %*
