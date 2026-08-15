@echo off
REM ============================================================
REM  G8 kernel supervisor (restart-as-unload)
REM
REM  Respawn the kernel when it exits with code 75 — the exit
REM  code used by POST /api/v1/system/restart (drain running
REM  runs -> suspend, then exit). Any other exit code stops
REM  the supervisor (honest behavior: crash/start errors are
REM  not masked by auto-restart).
REM
REM  Usage: run_kernel_supervised.bat <kernel_bin> <log_file>
REM ============================================================
:LOOP
"%~1" >> "%~2" 2>&1
if %errorlevel%==75 (
    echo [supervisor] G8 restart requested ^(exit 75^), respawning in 1s...
    timeout /t 1 /nobreak >nul
    goto LOOP
)
echo [supervisor] kernel exited with code %errorlevel%, supervisor stops.
