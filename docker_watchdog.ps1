#Requires -Version 5.1
# Docker Daemon Watchdog - auto-capture full state on hang/slowdown.
#
# Problem: Docker Desktop (WSL2) hangs periodically; the hang is intermittent
#          and you cannot manually grab logs at that moment, so root cause is lost.
# Solution: this script runs persistently, probes daemon responsiveness; the
#           moment it slows down beyond threshold (hang precursor), it dumps ALL
#           state (docker ps/logs/info, container status, WSL status, key procs)
#           to a timestamped file for post-mortem analysis. Multiple hangs
#           accumulate multiple dump files.
#
# Usage:
#   Foreground: powershell -ExecutionPolicy Bypass -File docker_watchdog.ps1
#   Background: register as scheduled task / launch from start_web_cn.bat
#
# Params:
#   -ProbeInterval  : seconds between probes (default 15; do not go too low)
#   -SlowThreshold  : seconds considered slow (default 8; > this = hang precursor)
#   -DumpDir        : dir for dump files (default .\docker_watchdog_dumps)
#   -MaxDumps       : max dump files kept (default 20; oldest deleted)

param(
    [int]$ProbeInterval = 15,
    [int]$SlowThreshold = 8,
    [string]$DumpDir = ".\docker_watchdog_dumps",
    [int]$MaxDumps = 20
)

$ErrorActionPreference = 'Continue'

if (-not (Test-Path $DumpDir)) { New-Item -ItemType Directory -Path $DumpDir | Out-Null }

$script:LastDumpTime = [DateTime]::MinValue
$script:DumpCount = 0

function Write-WLog([string]$msg) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Write-Host "[$ts] $msg"
}

# Lightweight daemon probe: docker version with 12s hard timeout to avoid self-block.
function Test-DaemonResponsive {
    # Returns: response ms (ok) / -1 (slow/timeout) / -2 (failed)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'docker'
    $psi.Arguments = 'version --format {{.Server.Version}}'
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
        if ($proc.WaitForExit(12000)) {
            $sw.Stop()
            if ($proc.ExitCode -eq 0) {
                $out = $proc.StandardOutput.ReadToEnd()
                if ($out -and $out.Trim() -match '^\d') { return [int]$sw.ElapsedMilliseconds }
            }
            return -2
        } else {
            try { $proc.Kill() } catch {}
            $sw.Stop()
            return -1
        }
    } catch {
        $sw.Stop()
        return -2
    }
}

# Dump all diagnostic info to a single timestamped file.
function Invoke-FullDump([string]$reason) {
    # cooldown 60s to avoid duplicate dumps for the same hang
    $now = Get-Date
    if (($now - $script:LastDumpTime).TotalSeconds -lt 60) { return }
    $script:LastDumpTime = $now

    $ts = $now.ToString('yyyyMMdd_HHmmss')
    $dumpFile = Join-Path $DumpDir "dump_$ts.txt"
    $script:DumpCount++

    Write-WLog "!!! HANG DETECTED: $reason -> dumping to $dumpFile"

    $sb = New-Object System.Text.StringBuilder
    function Add-Sec([string]$title) {
        [void]$sb.AppendLine("")
        [void]$sb.AppendLine("============================================================")
        [void]$sb.AppendLine(" $title")
        [void]$sb.AppendLine("============================================================")
    }
    function Add-Ln([string]$s) { [void]$sb.AppendLine($s) }

    Add-Sec "WATCHDOG DUMP - $ts"
    Add-Ln "Reason: $reason"
    Add-Ln "Host: $env:COMPUTERNAME"

    Add-Sec "1. docker ps -a"
    try {
        $ps = & docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}" 2>&1
        $ps | ForEach-Object { Add-Ln $_ }
    } catch { Add-Ln "(failed: $_)" }

    Add-Sec "2. docker info"
    try {
        $info = & docker info 2>&1 | Select-Object -First 30
        $info | ForEach-Object { Add-Ln $_ }
    } catch { Add-Ln "(failed: $_)" }

    Add-Sec "3. container logs (last 20 lines each)"
    try {
        $containers = & docker ps -a --format "{{.Names}}" 2>&1
        foreach ($c in $containers) {
            if ($c -and $c -notmatch '^(NAMES|Error|error)') {
                Add-Ln "--- $c ---"
                $logs = & docker logs --tail 20 $c 2>&1
                $logs | ForEach-Object { Add-Ln $_ }
            }
        }
    } catch { Add-Ln "(failed: $_)" }

    Add-Sec "4. key processes (Docker/WSL)"
    try {
        $procs = @('Docker Desktop','com.docker.backend','com.docker.build','vmmem','wslservice','wslhost')
        foreach ($p in $procs) {
            Get-Process -Name $p -ErrorAction SilentlyContinue | ForEach-Object {
                Add-Ln ("{0,-30} PID={1,-8} CPU={2,-10} MEM={3}MB" -f $_.Name, $_.Id, $_.CPU, [int]($_.WorkingSet64/1MB))
            }
        }
    } catch { Add-Ln "(failed: $_)" }

    Add-Sec "5. wsl --status"
    try {
        $wsl = & wsl --status 2>&1
        $wsl | ForEach-Object { Add-Ln $_ }
    } catch { Add-Ln "(failed: $_)" }

    Add-Sec "6. system resources"
    try {
        $os = Get-CimInstance Win32_OperatingSystem
        Add-Ln ("TotalMem: {0}MB, Free: {1}MB" -f [int]($os.TotalVisibleMemorySize/1024), [int]($os.FreePhysicalMemory/1024))
        $cpu = (Get-CimInstance Win32_Processor).LoadPercentage
        Add-Ln "CPU load: $cpu%"
    } catch { Add-Ln "(failed: $_)" }

    Add-Sec "7. Docker Desktop host log (recent errors)"
    try {
        $logDir = "$env:LOCALAPPDATA\Docker\log\host"
        $latest = Get-ChildItem -Path $logDir -Filter *.log -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($latest) {
            Add-Ln "Log file: $($latest.FullName)"
            Get-Content $latest.FullName -Tail 40 -ErrorAction SilentlyContinue |
                Where-Object { $_ -match 'error|not running|500|panic|backend|kill|exit' } |
                ForEach-Object { Add-Ln $_ }
        }
    } catch { Add-Ln "(failed: $_)" }

    $sb.ToString() | Out-File -FilePath $dumpFile -Encoding UTF8
    Write-WLog "Dump complete: $dumpFile"

    Get-ChildItem $DumpDir -Filter "dump_*.txt" | Sort-Object LastWriteTime |
        Select-Object -First ($script:DumpCount - $MaxDumps) |
        Where-Object { $_ -ne $null } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

# ===========================================================================
# Main loop
# ===========================================================================
Write-WLog "Docker Watchdog started | probe=${ProbeInterval}s | slow=${SlowThreshold}s | dir=$DumpDir"
Write-WLog "Watching... (Ctrl+C to stop)"

$healthyCount = 0
while ($true) {
    $ms = Test-DaemonResponsive

    if ($ms -ge 0) {
        $sec = [math]::Round($ms / 1000.0, 2)
        if ($ms -gt ($SlowThreshold * 1000)) {
            Write-WLog "WARN daemon slow: ${sec}s (>${SlowThreshold}s) - dumping"
            Invoke-FullDump -reason "daemon slow response ${sec}s"
        } else {
            $healthyCount++
            if ($healthyCount % 20 -eq 0) {
                Write-WLog "OK daemon healthy (${sec}s), $healthyCount consecutive ok"
            }
        }
    } elseif ($ms -eq -1) {
        Write-WLog "CRITICAL daemon no response in 12s (hung) - dumping"
        Invoke-FullDump -reason "daemon timeout >12s (hung)"
    } else {
        Write-WLog "ERROR docker command failed - dumping"
        Invoke-FullDump -reason "docker command failed (daemon down?)"
    }

    Start-Sleep -Seconds $ProbeInterval
}
