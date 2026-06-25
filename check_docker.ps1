# =============================================================================
# Docker daemon 健康检查（带超时）
#
# 被 start_web_cn.bat / start_web.bat 调用。
# 退出码:
#   0 = daemon 就绪
#   1 = daemon 未就绪（正在启动中，可继续等待）
#   3 = 超时（daemon 假死，docker info 在规定时间内无响应）
#
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File check_docker.ps1 -Timeout 90
# =============================================================================

param(
    [int]$Timeout = 90
)

$ErrorActionPreference = 'Stop'

# 用 Start-Job + Wait-Job 实现 docker info 的超时控制
# docker info 在 daemon 假死时会无限阻塞，不能直接调用
$job = Start-Job -ScriptBlock {
    docker info 2>&1 | Out-Null
    return $LASTEXITCODE
} | Wait-Job -Timeout $Timeout

if ($job -eq $null) {
    # 超时：Job 仍在运行，说明 docker info 卡住了 → daemon 假死
    Get-Job | Stop-Job -ErrorAction SilentlyContinue
    Get-Job | Remove-Job -ErrorAction SilentlyContinue
    exit 3
}

$result = Receive-Job $job
Remove-Job $job -ErrorAction SilentlyContinue

if ($result -eq 0) {
    # docker info 返回 0 → daemon 就绪
    exit 0
} else {
    # docker info 返回非 0 → daemon 未就绪（可能正在启动）
    exit 1
}
