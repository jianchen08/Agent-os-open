# =============================================================================
# Docker daemon 自动重启
#
# 被 start_web_cn.bat / start_web.bat 调用（daemon 假死时）。
# 退出码:
#   0 = 重启后 daemon 已恢复
#   1 = 重启失败
#   2 = 用户取消
#
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File restart_docker.ps1
# =============================================================================

$ErrorActionPreference = 'Continue'

Write-Host '[INFO] Docker daemon 假死，准备自动重启 Docker Desktop'
Write-Host '[WARN] 此操作会停止所有运行中的容器'

# 询问用户确认（5 秒超时自动确认）
$timeoutSec = 5
$confirmed = $false
$startTime = Get-Date
Write-Host "[INFO] 5 秒后自动重启，按 Y 立即确认 / N 取消..." -NoNewline
while (((Get-Date) - $startTime).TotalSeconds -lt $timeoutSec) {
    if ([Console]::KeyAvailable) {
        $key = [Console]::ReadKey($true)
        if ($key.Key -eq 'Y') { $confirmed = $true; break }
        if ($key.Key -eq 'N') { Write-Host ""; Write-Host '[INFO] 用户取消'; exit 2 }
    }
    Start-Sleep -Milliseconds 200
}
Write-Host ""
if (-not $confirmed) { Write-Host '[INFO] 超时自动确认' }

# 1. 停掉 Docker Desktop 进程
Write-Host '[INFO] 停止 Docker Desktop 相关进程...'
Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-Process -Name 'com.docker.backend' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-Process -Name 'com.docker.service' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-Process -Name 'vpnkit' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

# 2. 关闭 WSL（仅对 WSL2 后端，CI/无 WSL 环境会静默失败）
Write-Host '[INFO] 关闭 WSL...'
wsl --shutdown 2>$null | Out-Null

Start-Sleep -Seconds 5

# 3. 重启 Docker Desktop
$dockerExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
if (-not (Test-Path $dockerExe)) {
    Write-Host "[ERROR] 未找到 Docker Desktop: $dockerExe"
    exit 1
}
Write-Host '[INFO] 启动 Docker Desktop...'
Start-Process $dockerExe -ArgumentList '' -WindowStyle Hidden

# 4. 轮询等待 daemon 恢复（最多 3 分钟）
$maxWait = 180
$elapsed = 0
Write-Host '[INFO] 等待 daemon 恢复...'
while ($elapsed -lt $maxWait) {
    Start-Sleep -Seconds 5
    $elapsed += 5
    $job = Start-Job -ScriptBlock { docker info 2>&1 | Out-Null; return $LASTEXITCODE } | Wait-Job -Timeout 10
    if ($job -ne $null) {
        $rc = Receive-Job $job
        Remove-Job $job -ErrorAction SilentlyContinue
        if ($rc -eq 0) {
            Write-Host "[OK] Daemon 已恢复 (${elapsed}s)"
            exit 0
        }
    } else {
        Get-Job | Stop-Job -ErrorAction SilentlyContinue
        Get-Job | Remove-Job -ErrorAction SilentlyContinue
    }
    if ($elapsed % 30 -eq 0) {
        Write-Host "[INFO] 仍在等待 daemon... (${elapsed}/${maxWait}s)"
    }
}

Write-Host '[ERROR] Daemon 在规定时间内未恢复'
exit 1
