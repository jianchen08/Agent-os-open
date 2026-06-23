#monitor_docker.ps1
# 监控 Docker Desktop backend 日志，抓取 daemon 反复 shutdown/restart 的真相。
#
# 用法（在一个独立 cmd 窗口里跑，让它持续盯着）:
#   powershell -NoProfile -ExecutionPolicy Bypass -File monitor_docker.ps1
# 按 Ctrl+C 停止。结果同时打到屏幕和 monitor_docker.log。
#
# 抓什么:
#   - shutdown 请求（谁触发 daemon 被关）
#   - engine state 变化（starting/running/stopping/stopped）
#   - engine error / bootstrap failure
#   - wsl --terminate 超时（关不掉的真凶）
#   - 容器退出/重启（排查是不是容器把 daemon 拖崩）

param(
    [int]$Duration = 0   # 0 = 一直跑到 Ctrl+C；>0 = 跑 N 秒后自动停
)

$ErrorActionPreference = 'Continue'
$logDir = 'C:\Users\jc\AppData\Local\Docker\log\host'
$outLog = 'D:\myproject\container_036fa50daf44\monitor_docker.log'

# 关注的关键模式（每行匹配其一就记录）
$patterns = @(
    'shutdown requested',
    'state running -> stopping',
    'state stopping -> stopped',
    'state .* -> starting',
    'engine is running',
    'engine error',
    'set engine error',
    'engine linux/wsl shutdown',
    'wsl --terminate.*failed',
    'CommandTimedOut',
    'graceful shutdown',
    'timed out waiting for VM',
    'received state \{Docker:',
    'deploy',
    'exited',
    'restart',
    'Fatalf',
    'panic'
)
$regex = ($patterns | ForEach-Object { [regex]::Escape($_) }) -join '|'
$regex = "($regex)"

function Get-LatestBackendLog {
    Get-ChildItem "$logDir\com.docker.backend.exe.log*" |
        Where-Object { $_.Name -match '^com\.docker\.backend\.exe\.log(\.\d+)?$' } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}

"=== Docker monitor started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
    Tee-Object -FilePath $outLog | Out-Host
"Watching: $logDir" | Tee-Object -FilePath $outLog -Append | Out-Host
"Patterns: $regex" | Tee-Object -FilePath $outLog -Append | Out-Host
"Duration: $(if ($Duration -gt 0) { "$Duration s" } else { 'until Ctrl+C' })" |
    Tee-Object -FilePath $outLog -Append | Out-Host
"" | Tee-Object -FilePath $outLog -Append | Out-Host

$start = Get-Date
$lastFile = Get-LatestBackendLog
$pos = if ($lastFile) { $lastFile.Length } else { 0 }

while ($true) {
    if ($Duration -gt 0 -and ((Get-Date) - $start).TotalSeconds -gt $Duration) {
        "=== monitor duration reached, stopping $(Get-Date -Format 'HH:mm:ss') ===" |
            Tee-Object -FilePath $outLog -Append | Out-Host
        break
    }

    # 文件可能轮转，重新定位最新文件
    $cur = Get-LatestBackendLog
    if (-not $cur) { Start-Sleep -Milliseconds 500; continue }

    # 如果换了文件，从头读新文件
    if ($lastFile -and $cur.FullName -ne $lastFile.FullName) {
        "[$(Get-Date -Format 'HH:mm:ss')] log rotated to $($cur.Name)" |
            Tee-Object -FilePath $outLog -Append | Out-Host
        $pos = 0
    }
    $lastFile = $cur

    try {
        $stream = [System.IO.File]::Open($cur.FullName, 'Open', 'Read', 'ReadWrite')
        $stream.Seek($pos, 'Begin') | Out-Null
        $reader = New-Object System.IO.StreamReader($stream)
        while (-not $reader.EndOfStream) {
            $line = $reader.ReadLine()
            if ($line -match $regex) {
                "[$(Get-Date -Format 'HH:mm:ss')] $line" |
                    Tee-Object -FilePath $outLog -Append | Out-Host
            }
        }
        $pos = $stream.Position
        $reader.Close()
        $stream.Close()
    } catch {
        "[$(Get-Date -Format 'HH:mm:ss')] read error: $($_.Exception.Message)" |
            Tee-Object -FilePath $outLog -Append | Out-Host
    }

    Start-Sleep -Milliseconds 800
}
