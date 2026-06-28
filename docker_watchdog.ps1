#Requires -Version 5.1
# Docker Daemon Watchdog - auto-capture full state on hang + auto-recover.
#
# Problem: Docker Desktop (WSL2) hangs periodically; the hang is intermittent
#          and you cannot manually grab logs at that moment, so root cause is lost.
# Solution: runs persistently, probes daemon responsiveness; on slowdown/timeout it
#           dumps ALL state (docker ps/logs/info, container logs, WSL status, key
#           procs with DOUBLE CPU SAMPLING, host log errors) + AUTO-RECOVERS by
#           invoking restart_docker.ps1.
#
# CPU double-sampling: on hang, sample high-CPU procs twice (1.5s apart), compute
#           real CPU delta + memory delta. This distinguishes:
#             - CPU up, mem flat = tight loop (busy spin)
#             - CPU up, mem up   = leak / goroutine storm
#             - CPU flat         = lock contention (spinning on lock, not burning CPU)
#
# Usage:
#   Foreground: powershell -ExecutionPolicy Bypass -File docker_watchdog.ps1
#   Register as scheduled task / launch from start_web_cn.bat for persistence.
#
# Params:
#   -ProbeInterval   : seconds between probes (default 15)
#   -SlowThreshold   : seconds considered slow (default 8; > this = hang precursor)
#   -DumpDir         : dir for dump files (default .\docker_watchdog_dumps)
#   -MaxDumps        : max dump files kept (default 20)
#   -NoAutoRecover   : if set, only dump without invoking restart_docker.ps1

param(
    [int]$ProbeInterval = 15,
    [int]$SlowThreshold = 8,
    [string]$DumpDir = ".\docker_watchdog_dumps",
    [int]$MaxDumps = 20,
    [switch]$NoAutoRecover
)

$ErrorActionPreference = 'Continue'

if (-not (Test-Path $DumpDir)) { New-Item -ItemType Directory -Path $DumpDir | Out-Null }

$script:LastDumpTime = [DateTime]::MinValue
$script:DumpCount = 0
# Monotonic recovery cooldown: do not restart docker more than once per 5 min
$script:LastRecoverTime = [DateTime]::MinValue

# Path to restart_docker.ps1 (same dir as this script)
$script:RestartScript = Join-Path $PSScriptRoot 'restart_docker.ps1'

function Write-WLog([string]$msg) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Write-Host "[$ts] $msg"
}

# Lightweight daemon probe: docker version with 12s hard timeout.
# Returns: response ms (ok) / -1 (slow/timeout) / -2 (failed)
function Test-DaemonResponsive {
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

# Run an external command with a hard timeout. Returns stdout+stderr lines,
# or a single "(timeout)" line if it did not finish in time.
# CRITICAL: during a daemon hang, bare `& docker ps` blocks forever. Every
# docker call inside dump MUST go through this to avoid the watchdog itself
# hanging and never reaching auto-recover.
function Invoke-WithTimeout([string]$exe, [string]$arguments, [int]$timeoutMs = 5000) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = $arguments
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
        if ($proc.WaitForExit($timeoutMs)) {
            $out = $proc.StandardOutput.ReadToEnd()
            $err = $proc.StandardError.ReadToEnd()
            if ($out) { return $out -split "`r?`n" }
            if ($err) { return $err -split "`r?`n" }
            return @()
        } else {
            try { $proc.Kill() } catch {}
            return @("(timeout ${timeoutMs}ms)")
        }
    } catch {
        return @("(failed: $_)")
    }
}

# Sample a single process: returns TotalProcessorTime (ticks) + WorkingSet64.
function Get-ProcSnapshot([System.Diagnostics.Process]$p) {
    try {
        $p.Refresh()
        return @{ Cpu = $p.TotalProcessorTime.Ticks; Mem = $p.WorkingSet64; Valid = $true }
    } catch {
        return @{ Cpu = 0; Mem = 0; Valid = $false }
    }
}

# Sample the key Docker/WSL processes TWICE (1.5s apart) to compute real CPU delta.
# This distinguishes busy-spin (CPU climbs, mem flat) from leak (both climb) from
# lock-contention (CPU flat, process appears busy but burns nothing).
function Sample-HotProcs {
    $names = @('com.docker.backend','Docker Desktop','com.docker.build','vmmem','wslservice')
    $samples = @{}
    $t1 = Get-Date
    foreach ($n in $names) {
        Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object {
            $samples[$_.Id] = @{ Name=$n; S1=(Get-ProcSnapshot $_) }
        }
    }
    Start-Sleep -Milliseconds 1500
    $t2 = Get-Date
    $dtSec = ($t2 - $t1).TotalSeconds
    foreach ($n in $names) {
        Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object {
            if ($samples.ContainsKey($_.Id)) {
                $s2 = Get-ProcSnapshot $_
                $s1 = $samples[$_.Id].S1
                if ($s1.Valid -and $s2.Valid) {
                    # CPU delta as percentage of one core over the interval
                    $cpuTicks = $s2.Cpu - $s1.Cpu
                    $cpuPct = [math]::Round(($cpuTicks / [TimeSpan]::TicksPerSecond) / $dtSec * 100, 1)
                    $memDeltaMB = [math]::Round(($s2.Mem - $s1.Mem) / 1MB, 1)
                    $memMB = [int]($s2.Mem / 1MB)
                    $samples[$_.Id].CpuPct = $cpuPct
                    $samples[$_.Id].MemMB = $memMB
                    $samples[$_.Id].MemDeltaMB = $memDeltaMB
                }
            }
        }
    }
    return $samples
}

# Classify a hot process based on its sampled deltas.
function Classify-Proc([hashtable]$s) {
    if (-not $s.ContainsKey('CpuPct')) { return "unmeasured" }
    $cpu = $s.CpuPct
    $memDelta = $s.MemDeltaMB
    if ($cpu -gt 50 -and [math]::Abs($memDelta) -lt 5) {
        return "BUSY-SPIN (tight loop: high CPU, mem flat) -> likely Docker bug"
    }
    if ($cpu -gt 50 -and $memDelta -gt 5) {
        return "LEAK/STORM (CPU+mem both climbing) -> resource leak"
    }
    if ($cpu -lt 10) {
        return "LOCK-CONTENTION (low CPU, process waits on lock)"
    }
    return "busy (CPU=$cpu%, memDelta=${memDelta}MB) -> under load"
}

function Invoke-FullDump([string]$reason) {
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
    $lines = Invoke-WithTimeout 'docker' 'ps -a --format "table {{.Names}}\t{{.Status}}"' 5000
    $lines | ForEach-Object { Add-Ln $_ }

    Add-Sec "2. docker info"
    $lines = Invoke-WithTimeout 'docker' 'info' 5000
    $lines | Select-Object -First 30 | ForEach-Object { Add-Ln $_ }

    Add-Sec "3. container logs (last 20 lines each)"
    # container names from section 1 reuse; re-fetch with timeout to be safe
    $names = Invoke-WithTimeout 'docker' 'ps -a --format "{{.Names}}"' 5000
    foreach ($c in $names) {
        if ($c -and $c -notmatch '^(NAMES|Error|error|timeout|failed)') {
            Add-Ln "--- $c ---"
            $clogs = Invoke-WithTimeout 'docker' "logs --tail 20 $c" 5000
            $clogs | ForEach-Object { Add-Ln $_ }
        }
    }

    # KEY: double CPU sampling on Docker/WSL processes
    Add-Sec "4. HOT PROCESS CPU SAMPLING (1.5s apart)"
    try {
        $hot = Sample-HotProcs
        foreach ($pidKey in $hot.Keys) {
            $s = $hot[$pidKey]
            $verdict = Classify-Proc $s
            $cpu = if ($s.ContainsKey('CpuPct')) { $s.CpuPct } else { '?' }
            $mem = if ($s.ContainsKey('MemMB')) { $s.MemMB } else { '?' }
            $memD = if ($s.ContainsKey('MemDeltaMB')) { $s.MemDeltaMB } else { '?' }
            Add-Ln ("{0,-30} PID={1,-8} CPU={2}% MEM={3}MB memDelta={4}MB -> {5}" -f $s.Name, $pidKey, $cpu, $mem, $memD, $verdict)
        }
    } catch { Add-Ln "(sampling failed: $_)" }

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
        Add-Ln "System CPU load: $cpu%"
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

    # cleanup old dumps
    $dumps = @(Get-ChildItem $DumpDir -Filter "dump_*.txt" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime)
    if ($dumps.Count -gt $MaxDumps) {
        $dumps | Select-Object -First ($dumps.Count - $MaxDumps) | Remove-Item -Force -ErrorAction SilentlyContinue
    }
}

# Auto-recover: invoke restart_docker.ps1 -Yes to revive the daemon.
function Invoke-AutoRecover([string]$reason) {
    if ($NoAutoRecover) {
        Write-WLog "Auto-recover disabled (-NoAutoRecover). Manual recovery required."
        return
    }
    $now = Get-Date
    if (($now - $script:LastRecoverTime).TotalSeconds -lt 300) {
        Write-WLog "Auto-recover skipped (cooldown 300s, last recover recent)."
        return
    }
    $script:LastRecoverTime = $now
    if (-not (Test-Path $script:RestartScript)) {
        Write-WLog "Auto-recover: restart_docker.ps1 not found at $($script:RestartScript). Skip."
        return
    }
    Write-WLog "AUTO-RECOVER: invoking restart_docker.ps1 -Yes (reason: $reason) ..."
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $script:RestartScript -Yes 2>&1 | ForEach-Object {
            if ($_ -match 'ready|Started|fail') { Write-WLog "  recover: $_" }
        }
        Write-WLog "AUTO-RECOVER: restart_docker.ps1 finished."
    } catch {
        Write-WLog "AUTO-RECOVER failed: $_"
    }
}

# ===========================================================================
# Main loop
# ===========================================================================
Write-WLog "Docker Watchdog started | probe=${ProbeInterval}s | slow=${SlowThreshold}s | autoRecover=$(-not $NoAutoRecover)"
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
        Write-WLog "CRITICAL daemon no response in 12s (hung) - RECOVER FIRST, then dump"
        # ORDER MATTERS: recover before dump. During a hang, docker commands block
        # even with timeout, and recovery is the priority. Dump after recover can
        # capture real container logs instead of 500 errors.
        Invoke-AutoRecover -reason "daemon timeout >12s (hung)"
        Invoke-FullDump -reason "daemon timeout >12s (hung) [post-recover dump]"
        $healthyCount = 0
    } else {
        Write-WLog "ERROR docker command failed - RECOVER FIRST, then dump"
        Invoke-AutoRecover -reason "docker command failed (daemon down?)"
        Invoke-FullDump -reason "docker command failed (daemon down?) [post-recover dump]"
        $healthyCount = 0
    }

    Start-Sleep -Seconds $ProbeInterval
}
