# Agent OS 停止脚本 (PowerShell)
# 按项目目录隔离：只关闭当前项目 .ports 文件中记录的端口进程

Write-Host "========================================"
Write-Host "  Agent OS Web Channel 停止脚本"
Write-Host "========================================"
Write-Host ""

$ProjectRoot = $PSScriptRoot
if (-not $ProjectRoot) { $ProjectRoot = (Get-Location).Path }
$PortsFile = Join-Path $ProjectRoot ".ports"

Write-Host "项目目录: $ProjectRoot"
Write-Host ""

$Found = $false
$BackendPort = $null
$FrontendPort = $null

# ========== 读取 .ports 文件 ==========
if (Test-Path $PortsFile) {
    Write-Host "[INFO] 从 .ports 文件读取端口信息..." -ForegroundColor Gray
    $lines = Get-Content $PortsFile
    foreach ($line in $lines) {
        if ($line -match "^BACKEND_PORT=(\d+)") {
            $BackendPort = $Matches[1]
        }
        if ($line -match "^FRONTEND_PORT=(\d+)") {
            $FrontendPort = $Matches[1]
        }
    }
    if ($BackendPort) { Write-Host "[INFO] 后端端口: $BackendPort" -ForegroundColor Gray }
    if ($FrontendPort) { Write-Host "[INFO] 前端端口: $FrontendPort" -ForegroundColor Gray }
} else {
    Write-Host "[INFO] 未找到 .ports 文件，使用默认端口..." -ForegroundColor Gray
    $BackendPort = "8888"
    $FrontendPort = "5188"
}

# ========== 关闭后端进程 ==========
if ($BackendPort) {
    $connections = Get-NetTCPConnection -LocalPort $BackendPort -State Listen -ErrorAction SilentlyContinue
    if ($connections) {
        foreach ($conn in $connections) {
            Write-Host "[INFO] 关闭后端进程 PID=$($conn.OwningProcess) (端口 $BackendPort)" -ForegroundColor Gray
            Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
            $Found = $true
        }
    }
}

# ========== 关闭前端进程 ==========
if ($FrontendPort) {
    $connections = Get-NetTCPConnection -LocalPort $FrontendPort -State Listen -ErrorAction SilentlyContinue
    if ($connections) {
        foreach ($conn in $connections) {
            Write-Host "[INFO] 关闭前端进程 PID=$($conn.OwningProcess) (端口 $FrontendPort)" -ForegroundColor Gray
            Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
            $Found = $true
        }
    }
}

Start-Sleep -Seconds 2

# ========== 清理 .ports 文件 ==========
if (Test-Path $PortsFile) {
    Remove-Item $PortsFile -Force -ErrorAction SilentlyContinue
}

# ========== 结果 ==========
Write-Host ""
if (-not $Found) {
    Write-Host "[INFO] 没有发现运行中的 Agent OS 服务" -ForegroundColor Gray
} else {
    Write-Host "[OK] Agent OS 服务已停止" -ForegroundColor Green
}
