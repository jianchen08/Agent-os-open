# Docker daemon 健康检查（带超时）
# 用法: powershell -File check_docker.ps1 [-Timeout <秒>]
# 退出码:
#   0 = daemon 就绪
#   1 = daemon 未就绪（启动中或返回错误）
#   3 = 命令超时（daemon 无响应，可能假死）
#
# 设计：docker info 在 daemon 假死时会无限期阻塞，必须用进程级超时强制终止。
# 用 .NET Process API 实现（比 Start-Process 文件重定向更可靠）。

param(
    [int]$Timeout = 60
)

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "docker"
$psi.Arguments = "info -f {{.ServerVersion}}"
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true

$proc = [System.Diagnostics.Process]::Start($psi)
$stdout = $null

$finished = $proc.WaitForExit($Timeout * 1000)
if (-not $finished) {
    try { $proc.Kill() } catch {}
    exit 3
}

$stdout = $proc.StandardOutput.ReadToEnd()
if ($proc.ExitCode -ne 0) {
    exit 1
}

if ($stdout -and $stdout.Trim() -match '^\d') {
    exit 0
}
exit 1
