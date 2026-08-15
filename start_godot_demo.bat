@echo off
chcp 65001 >nul
REM ============================================================
REM  Godot Demo Launcher (Windows)
REM  Opens the AgentOS Godot demo project (hosts/godot-demo)
REM  with the agentos addon enabled:
REM    - HTTP server in editor at 127.0.0.1:9600
REM    - selection push to http://127.0.0.1:9100/ext/pipeline_godot_context/selection
REM      (AgentOS kernel on 9100; silent if not running)
REM
REM  Godot installed via winget (GodotEngine.GodotEngine 4.7.1),
REM  no start-menu shortcut: prefer "godot" on PATH, else locate
REM  the exe under winget Packages.
REM ============================================================
setlocal

set "GODOT_CMD="
where godot >nul 2>nul && set "GODOT_CMD=godot"

if not defined GODOT_CMD (
    for /f "delims=" %%i in ('dir /b /s "%LOCALAPPDATA%\Microsoft\WinGet\Packages\Godot_v*-stable_win64.exe" 2^>nul') do (
        if not defined GODOT_CMD set "GODOT_CMD=%%i"
    )
)

if not defined GODOT_CMD (
    echo [error] Godot exe not found. Install: winget install GodotEngine.GodotEngine
    pause
    exit /b 1
)

echo [godot]   %GODOT_CMD%
echo [project] %~dp0hosts\godot-demo
start "" "%GODOT_CMD%" --editor --path "%~dp0hosts\godot-demo"

endlocal
