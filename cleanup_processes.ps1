# 启动前清理本项目残留的宿主机进程
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File cleanup_processes.ps1
#
# 关闭规则（满足任一即视为本项目进程）：
#   1. 可执行文件位于项目目录下（如本地 venv 的 python、项目内打包的 node）
#   2. 命令行中包含后端入口标识 channels.websocket.app_factory
# 注意：不按"命令行包含项目目录"匹配，避免误杀恰好引用该路径的外部工具。
#
# 安全保证：
#   - 保护当前进程及其全部祖先，绝不误杀正在运行的本启动脚本本身。
#   - 交互式终端（命令行不含项目路径）不会被关闭。
#   - Docker 容器内的服务由 Docker 进程托管，与本目录无关，不受影响。
#   - 始终退出 0：清理本身失败不应阻断启动流程。

$ErrorActionPreference = 'SilentlyContinue'

$ProjectRoot = $PSScriptRoot
if (-not $ProjectRoot) { $ProjectRoot = (Get-Location).Path }
$ProjPrefix = $ProjectRoot.TrimEnd('\').ToLowerInvariant() + '\'
$AppSignature = 'channels.websocket.app_factory'

Write-Host "项目目录: $ProjectRoot" -ForegroundColor Gray

# --- 保护当前进程及其全部祖先，避免误杀启动脚本自身 ---
$protect = @{}
$cur = [int]$PID
while ($cur -and -not $protect.ContainsKey($cur)) {
    $protect[$cur] = $true
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$cur"
    if (-not $p) { break }
    $cur = [int]$p.ParentProcessId
}

function Test-Contains([string]$haystack, [string]$needle) {
    if (-not $haystack -or -not $needle) { return $false }
    return $haystack.ToLowerInvariant().Contains($needle.ToLowerInvariant())
}

# --- 挑出本项目残留进程（可执行位于本目录下，或为后端入口） ---
$targets = Get-CimInstance Win32_Process | Where-Object {
    $procId = [int]$_.ProcessId
    if (-not $procId -or $protect.ContainsKey($procId)) { return $false }
    $exe = $_.ExecutablePath
    $cmd = $_.CommandLine
    (Test-Contains $exe $ProjPrefix) -or (Test-Contains $cmd $AppSignature)
}

if (-not $targets) {
    Write-Host "[OK] 无残留进程" -ForegroundColor Green
    exit 0
}

foreach ($t in $targets) {
    Write-Host ("[OK] 关闭残留进程: {0} (PID {1})" -f $t.Name, $t.ProcessId) -ForegroundColor Yellow
    Stop-Process -Id ([int]$t.ProcessId) -Force -ErrorAction SilentlyContinue
}

# 留出时间让端口/句柄释放
Start-Sleep -Milliseconds 800
Write-Host "[OK] 残留进程清理完成" -ForegroundColor Green
exit 0
