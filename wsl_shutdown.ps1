# Run `wsl --shutdown` with a hard timeout.
# Under a WSL2 kernel D-state deadlock, `wsl --shutdown` itself can hang; without
# a timeout the retry loop in start_web_cn.bat would freeze on a blank window too.
#
# Exit codes:
#   0   shutdown completed within the timeout
#   124 shutdown hung past the timeout (killed) -> caller proceeds to wait+retry anyway
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File wsl_shutdown.ps1 [-Timeout 15]
param(
    [int]$Timeout = 15
)

$p = Start-Process -FilePath 'wsl.exe' -ArgumentList '--shutdown' `
    -WindowStyle Hidden -PassThru

if (-not $p.WaitForExit($Timeout * 1000)) {
    try { $p.Kill() } catch {}
    exit 124
}
exit $p.ExitCode
