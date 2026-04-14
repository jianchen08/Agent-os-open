@echo off
chcp 65001 >nul 2>&1
set PYTHONPATH=src
echo Starting Agent OS CLI...
python -m channels.cli.cli_main %*
