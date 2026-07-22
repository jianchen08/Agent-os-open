# WSL liveness probe with a hard Windows-side timeout.
# Called from start_web_cn.bat BEFORE any blocking `wsl -d Ubuntu ...` call.
#
# Why this exists: bash-internal `timeout` cannot rescue a WSL2 kernel D-state
# deadlock, because wsl.exe itself hangs on the Windows side before bash starts.
# A blocking wsl call in the .bat would freeze the script with a blank window.
# We wrap wsl in Start-Process + WaitForExit so a hang is converted to a code.
#
# Exit codes:
#   0   WSL responded (Ubuntu distro ran the echo), and stderr is clean
#   2   Distro disk lost/corrupted (MountDisk/ERROR_FILE_NOT_FOUND in stderr) -> caller reinstalls
#   124 WSL hung past the timeout (kernel deadlock) -> caller does wsl --shutdown retry
#   other  WSL/Ubuntu unavailable (returns instantly) -> caller falls back to Docker Desktop
#
# stderr is also written to %TEMP%\wsl_alive_probe.err so the caller can
# distinguish a lost ext4.vhdx (MountDisk / ERROR_FILE_NOT_FOUND) from a
# transient deadlock and route to "reinstall distro" instead of a shutdown loop.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File wsl_alive_probe.ps1 [-Timeout 20]
param(
    [int]$Timeout = 20
)

$out = Join-Path $env:TEMP 'wsl_alive_probe.out'
$err = Join-Path $env:TEMP 'wsl_alive_probe.err'

$p = Start-Process -FilePath 'wsl.exe' `
    -ArgumentList '-d','Ubuntu','-u','root','--','bash','-c','echo ok' `
    -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $out `
    -RedirectStandardError $err

if (-not $p.WaitForExit($Timeout * 1000)) {
    try { $p.Kill() } catch {}
    exit 124
}

# wsl.exe may exit 0 even when the distro failed to boot (e.g. ext4.vhdx lost:
# it prints "MountDisk/HCS/ERROR_FILE_NOT_FOUND" to stderr but still returns 0).
# Trusting the exit code alone gives a false green. Inspect stderr on every exit.
if (Test-Path $err) {
    $errText = Get-Content $err -Raw -ErrorAction SilentlyContinue
    if ($errText -and $errText -match 'MountDisk|ERROR_FILE_NOT_FOUND|0x80070002|Wsl_E MountDisk') {
        # Distro disk lost/corrupted — cannot be fixed by wsl --shutdown.
        # Exit 2 so the caller routes to reinstall instead of a retry loop.
        exit 2
    }
}
exit $p.ExitCode
