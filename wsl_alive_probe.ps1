# WSL liveness probe with a hard Windows-side timeout.
# Called from start_web_cn.bat BEFORE any blocking `wsl -d Ubuntu ...` call.
#
# Why this exists: bash-internal `timeout` cannot rescue a WSL2 kernel D-state
# deadlock, because wsl.exe itself hangs on the Windows side before bash starts.
# A blocking wsl call in the .bat would freeze the script with a blank window.
# We wrap wsl in Start-Process + WaitForExit so a hang is converted to a code.
#
# Exit codes:
#   0   WSL responded (Ubuntu distro ran the echo)
#   124 WSL hung past the timeout (kernel deadlock) -> caller does wsl --shutdown retry
#   other  WSL/Ubuntu unavailable (returns instantly) -> caller falls back to Docker Desktop
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
exit $p.ExitCode
