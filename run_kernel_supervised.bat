@echo off
REM ============================================================
REM  G8 kernel supervisor
REM
REM  Exit code 75 = restart requested (POST /api/v1/system/restart
REM  restart-as-unload: drain running runs -> suspend, then exit;
REM  also the watcher cdylib-set auto restart). Respawn after 1s:
REM  no failure counting, no backoff, no circuit breaker.
REM
REM  Any other exit code (crash / start error) is also respawned,
REM  with exponential backoff (base 5s -> 15s -> 45s cap). The old
REM  "honest stop on non-75" contract is retired: with the external
REM  session supervisor gone it left unexpected deaths unrecovered
REM  (2026-09-10: kernel stayed down 69 min). Masking a crash loop
REM  is prevented by two guard rails:
REM    - consecutive non-75 failures are counted; after
REM      AGENTOS_SUPERVISOR_MAX_CONSECUTIVE_FAILS consecutive
REM      failures (default 5) the supervisor stops (circuit break);
REM    - a kernel run that lasted >= AGENTOS_SUPERVISOR_STABLE_SECS
REM      (default 60s) counts as a successful start and resets the
REM      failure streak and the backoff; a run shorter than the
REM      threshold keeps the streak growing, so a crash loop cannot
REM      whitewash the counter with short runs. 75 exits neither
REM      count as failure nor reset the streak.
REM
REM  Every exit / backoff / respawn / circuit-break appends one line
REM  to .kernel_supervisor.log next to this script:
REM    [ISO8601] action=<exited|respawn|backoff|circuit-break> exit_code=<N> consecutive_failures=<N>
REM  This file is the forensic record of supervisor decisions (exit
REM  codes used to go to stdout only, which WMI/background launches
REM  lose entirely).
REM
REM  Tunables (env overrides; defaults below are the production
REM  contract; tests inject small values to run fast):
REM    AGENTOS_SUPERVISOR_BACKOFF_MS_BASE=5000
REM    AGENTOS_SUPERVISOR_BACKOFF_MS_MAX=45000
REM    AGENTOS_SUPERVISOR_MAX_CONSECUTIVE_FAILS=5
REM    AGENTOS_SUPERVISOR_STABLE_SECS=60
REM
REM  Usage: run_kernel_supervised.bat <kernel_bin> <log_file>
REM  NOTE: keep comments in this file ASCII-only -- cmd parses the
REM  batch in a legacy codepage and non-ASCII text can be misread
REM  into executable fragments (precedent ced82b8cb).
REM ============================================================
set "SUPERVISOR_LOG=%~dp0.kernel_supervisor.log"
if not defined AGENTOS_SUPERVISOR_BACKOFF_MS_BASE set "AGENTOS_SUPERVISOR_BACKOFF_MS_BASE=5000"
if not defined AGENTOS_SUPERVISOR_BACKOFF_MS_MAX set "AGENTOS_SUPERVISOR_BACKOFF_MS_MAX=45000"
if not defined AGENTOS_SUPERVISOR_MAX_CONSECUTIVE_FAILS set "AGENTOS_SUPERVISOR_MAX_CONSECUTIVE_FAILS=5"
if not defined AGENTOS_SUPERVISOR_STABLE_SECS set "AGENTOS_SUPERVISOR_STABLE_SECS=60"
set /a CONSECUTIVE_FAILS=0
:LOOP
REM 2026-09-07: wait out transient AV/Defender first-run scan locks on the
REM freshly-built exe (spawn during scan fails with "process cannot access
REM the file" and errorlevel 0, which looked like an honest exit).
set "ATTEMPT=0"
:WAIT_UNLOCK
powershell -NoProfile -Command "try { $f=[System.IO.File]::Open('%~1','Open','ReadWrite','None'); $f.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    set /a ATTEMPT+=1
    if %ATTEMPT% GEQ 6 (
        echo [supervisor] exe still locked after 5 waits, attempting spawn anyway.
        goto SPAWN
    )
    ping -n 3 127.0.0.1 >nul
    goto WAIT_UNLOCK
)
:SPAWN
REM A hidden process (zombie reader etc.) can hold the log file exclusively;
REM the redirect below would then fail with errorlevel 0 and the supervisor
REM would mistake it for an honest exit (kernel never starts). Probe the log
REM with the same exclusive-open idiom as WAIT_UNLOCK; if locked, rename it
REM aside so the redirect creates a fresh file. Rename goes through only for
REM holders opened with delete-sharing (e.g. msys readers); if it fails the
REM spawn proceeds unchanged.
if not exist "%~2" goto DO_SPAWN
powershell -NoProfile -Command "try { $f=[System.IO.File]::Open('%~2','Open','ReadWrite','None'); $f.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 ren "%~2" "%~nx2.locked-%RANDOM%" >nul 2>&1
:DO_SPAWN
call :NOW_EPOCH
set "SPAWN_EPOCH=%EPOCH_SECS%"
"%~1" >> "%~2" 2>&1
set "KEXIT=%errorlevel%"
call :NOW_EPOCH
set /a RUNTIME_SECS=EPOCH_SECS - SPAWN_EPOCH
if "%KEXIT%"=="75" goto EXIT_75
REM Non-75 exit: count the failure and respawn with backoff (see header).
REM A run reaching the stable threshold proved the kernel started and
REM served, so the streak and the backoff restart from scratch; a shorter
REM run keeps accumulating toward the circuit breaker.
if %RUNTIME_SECS% GEQ %AGENTOS_SUPERVISOR_STABLE_SECS% (
    set /a CONSECUTIVE_FAILS=0
    set "BACKOFF_MS="
)
set /a CONSECUTIVE_FAILS+=1
echo [supervisor] kernel exited with code %KEXIT% ^(ran %RUNTIME_SECS%s^), consecutive failures: %CONSECUTIVE_FAILS%.
call :LOG_EVENT exited %KEXIT% %CONSECUTIVE_FAILS%
if %CONSECUTIVE_FAILS% GEQ %AGENTOS_SUPERVISOR_MAX_CONSECUTIVE_FAILS% goto CIRCUIT_BREAK
if not defined BACKOFF_MS set "BACKOFF_MS=%AGENTOS_SUPERVISOR_BACKOFF_MS_BASE%"
if %BACKOFF_MS% GTR %AGENTOS_SUPERVISOR_BACKOFF_MS_MAX% set "BACKOFF_MS=%AGENTOS_SUPERVISOR_BACKOFF_MS_MAX%"
echo [supervisor] non-75 exit, backing off %BACKOFF_MS%ms before respawn.
call :LOG_EVENT backoff %KEXIT% %CONSECUTIVE_FAILS%
powershell -NoProfile -Command "Start-Sleep -Milliseconds %BACKOFF_MS%" >nul 2>&1
set /a BACKOFF_MS*=3
if %BACKOFF_MS% GTR %AGENTOS_SUPERVISOR_BACKOFF_MS_MAX% set "BACKOFF_MS=%AGENTOS_SUPERVISOR_BACKOFF_MS_MAX%"
call :LOG_EVENT respawn %KEXIT% %CONSECUTIVE_FAILS%
goto LOOP

:EXIT_75
echo [supervisor] G8 restart requested ^(exit 75^), respawning in 1s...
call :LOG_EVENT exited 75 %CONSECUTIVE_FAILS%
ping -n 2 127.0.0.1 >nul
call :LOG_EVENT respawn 75 %CONSECUTIVE_FAILS%
goto LOOP

:CIRCUIT_BREAK
echo [supervisor] circuit break: %CONSECUTIVE_FAILS% consecutive failures, supervisor stops.
call :LOG_EVENT circuit-break %KEXIT% %CONSECUTIVE_FAILS%
exit /b %KEXIT%

REM ------------------------------------------------------------
REM  NOW_EPOCH
REM  Set EPOCH_SECS to current UTC seconds; used to measure a
REM  kernel run's duration against the stable threshold. PowerShell
REM  per call is acceptable here (loop is low frequency).
REM ------------------------------------------------------------
:NOW_EPOCH
for /f %%t in ('powershell -NoProfile -Command "[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()"') do set "EPOCH_SECS=%%t"
goto :eof

REM ------------------------------------------------------------
REM  LOG_EVENT <action> <exit_code> <consecutive_failures>
REM  Append one forensic line to the supervisor log. ISO8601
REM  timestamp via powershell; loop is low frequency so the
REM  per-event process cost is acceptable.
REM ------------------------------------------------------------
:LOG_EVENT
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format o"') do set "SUP_TS=%%d"
>>"%SUPERVISOR_LOG%" echo [%SUP_TS%] action=%~1 exit_code=%~2 consecutive_failures=%~3
goto :eof
