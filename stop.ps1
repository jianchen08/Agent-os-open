# AI Agent 系统停止脚本
# 停止所有相关服务

Write-Host "Stopping AI Agent System..." -ForegroundColor Yellow

# 停止 Python 后端进程
$pythonProcesses = Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -match "uvicorn" -or $_.CommandLine -match "src.api.main"
}

if ($pythonProcesses) {
    Write-Host "   Stopping backend service (Python/Uvicorn)..." -ForegroundColor Gray
    $pythonProcesses | Stop-Process -Force
    Write-Host "   Backend service stopped" -ForegroundColor Green
} else {
    Write-Host "   No running backend service found" -ForegroundColor Gray
}

# 停止 Node 前端进程
$nodeProcesses = Get-Process -Name "node" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -match "vite" -or $_.CommandLine -match "frontend"
}

if ($nodeProcesses) {
    Write-Host "   Stopping frontend service (Node/Vite)..." -ForegroundColor Gray
    $nodeProcesses | Stop-Process -Force
    Write-Host "   Frontend service stopped" -ForegroundColor Green
} else {
    Write-Host "   No running frontend service found" -ForegroundColor Gray
}

# 停止 PowerShell 作业
$jobs = Get-Job -Name "*backend*", "*frontend*" -ErrorAction SilentlyContinue
if ($jobs) {
    Write-Host "   Cleaning up PowerShell jobs..." -ForegroundColor Gray
    $jobs | Remove-Job -Force
    Write-Host "   Jobs cleaned up" -ForegroundColor Green
}

Write-Host "`nAll services stopped" -ForegroundColor Green
