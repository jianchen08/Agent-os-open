# Agent OS 停止脚本 (PowerShell)
# 按项目目录隔离：只关闭当前项目 .ports 文件中记录的端口进程
# 支持 PID 验证，防止误杀其他项目的进程

Write-Host "========================================"
Write-Host "  Agent OS Web Channel 停止脚本"
Write-Host "========================================"
Write-Host ""

$ProjectRoot = $PSScriptRoot
if (-not $ProjectRoot) { $ProjectRoot = (Get-Location).Path }
$PortsFile = Join-Path $ProjectRoot ".ports"

$md5 = [System.Security.Cryptography.MD5]::Create()
$hashBytes = $md5.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($ProjectRoot))
$ProjectId = [BitConverter]::ToString($hashBytes).Replace("-","").Substring(0,8).ToLower()

Write-Host "项目目录: $ProjectRoot"
Write-Host "项目标识: $ProjectId"
Write-Host ""

$Found = $false
$BackendPort = $null
$FrontendPort = $null
$StoredBackendPid = $null
$StoredFrontendPid = $null
$StoredProjectRoot = $null

# ========== 读取 .ports 文件 ==========
if (-not (Test-Path $PortsFile)) {
    Write-Host "[INFO] 未找到 .ports 文件，本项目没有运行中的实例" -ForegroundColor Gray
    exit 0
}

Write-Host "[INFO] 从 .ports 文件读取端口信息..." -ForegroundColor Gray
$lines = Get-Content $PortsFile
foreach ($line in $lines) {
    if ($line -match "^BACKEND_PORT=(\d+)") {
        $BackendPort = $Matches[1]
    }
    if ($line -match "^FRONTEND_PORT=(\d+)") {
        $FrontendPort = $Matches[1]
    }
    if ($line -match "^PROJECT_ROOT=(.+)") {
        $StoredProjectRoot = $Matches[1]
    }
    if ($line -match "^BACKEND_PID=(\d+)") {
        $StoredBackendPid = $Matches[1]
    }
    if ($line -match "^FRONTEND_PID=(\d+)") {
        $StoredFrontendPid = $Matches[1]
    }
    if ($line -match "^PROJECT_ID=(.+)") {
        if ($Matches[1] -ne $ProjectId) {
            Write-Host "[WARN] .ports 文件中的项目标识不匹配，可能已被其他项目覆盖" -ForegroundColor Yellow
        }
    }
}

if ($StoredProjectRoot -and $StoredProjectRoot -ne $ProjectRoot) {
    Write-Host "[WARN] .ports 文件属于其他项目目录 [$StoredProjectRoot]，拒绝操作" -ForegroundColor Red
    Write-Host "[INFO] 如需强制停止，请手动删除 $PortsFile" -ForegroundColor Gray
    exit 1
}

if ($BackendPort) { Write-Host "[INFO] 后端端口: $BackendPort" -ForegroundColor Gray }
if ($FrontendPort) { Write-Host "[INFO] 前端端口: $FrontendPort" -ForegroundColor Gray }
if ($StoredBackendPid) { Write-Host "[INFO] 后端 PID: $StoredBackendPid" -ForegroundColor Gray }
if ($StoredFrontendPid) { Write-Host "[INFO] 前端 PID: $StoredFrontendPid" -ForegroundColor Gray }

# ========== 关闭后端进程（带 PID 验证） ==========
if ($BackendPort) {
    $connections = Get-NetTCPConnection -LocalPort $BackendPort -State Listen -ErrorAction SilentlyContinue
    if ($connections) {
        foreach ($conn in $connections) {
            $currentPid = $conn.OwningProcess
            if ($StoredBackendPid) {
                if ($currentPid -eq [int]$StoredBackendPid) {
                    Write-Host "[INFO] 关闭后端进程 PID=$currentPid (端口 $BackendPort)" -ForegroundColor Gray
                    Stop-Process -Id $currentPid -Force -ErrorAction SilentlyContinue
                    $Found = $true
                } else {
                    Write-Host "[WARN] 端口 $BackendPort 上的进程已变更（存储PID=$StoredBackendPid，当前PID=$currentPid），跳过关闭以防误杀" -ForegroundColor Yellow
                }
            } else {
                Write-Host "[INFO] 关闭后端进程 PID=$currentPid (端口 $BackendPort)" -ForegroundColor Gray
                Stop-Process -Id $currentPid -Force -ErrorAction SilentlyContinue
                $Found = $true
            }
        }
    }
}

# ========== 关闭前端进程（带 PID 验证） ==========
if ($FrontendPort) {
    $connections = Get-NetTCPConnection -LocalPort $FrontendPort -State Listen -ErrorAction SilentlyContinue
    if ($connections) {
        foreach ($conn in $connections) {
            $currentPid = $conn.OwningProcess
            if ($StoredFrontendPid) {
                if ($currentPid -eq [int]$StoredFrontendPid) {
                    Write-Host "[INFO] 关闭前端进程 PID=$currentPid (端口 $FrontendPort)" -ForegroundColor Gray
                    Stop-Process -Id $currentPid -Force -ErrorAction SilentlyContinue
                    $Found = $true
                } else {
                    Write-Host "[WARN] 端口 $FrontendPort 上的进程已变更（存储PID=$StoredFrontendPid，当前PID=$currentPid），跳过关闭以防误杀" -ForegroundColor Yellow
                }
            } else {
                Write-Host "[INFO] 关闭前端进程 PID=$currentPid (端口 $FrontendPort)" -ForegroundColor Gray
                Stop-Process -Id $currentPid -Force -ErrorAction SilentlyContinue
                $Found = $true
            }
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
