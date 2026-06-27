# Diagnose Docker daemon hang root cause on Windows + Docker Desktop + WSL2.
# Run: powershell -NoProfile -File scripts\diag_docker_hang.ps1
$ErrorActionPreference = 'SilentlyContinue'

Write-Host "===== 1. SYSTEM MEMORY PRESSURE ====="
$os = Get-CimInstance Win32_OperatingSystem
$totalGB = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$freeGB = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
$freePct = [math]::Round(100 * $os.FreePhysicalMemory / $os.TotalVisibleMemorySize, 1)
Write-Host ("TotalRAM={0}GB  FreeRAM={1}GB  FreePct={2}%" -f $totalGB, $freeGB, $freePct)

Write-Host "`n===== 2. WSL/DOCKER/COM.DOCKER PROCESS MEMORY ====="
Get-Process vmmem*,vmmem,vmwp,vmms -ErrorAction SilentlyContinue |
    Select-Object Id, Name, @{N = 'RAM_MB'; E = { [int]($_.WorkingSet64 / 1MB) } } |
    Format-Table -AutoSize
Get-Process docker*, com.docker* -ErrorAction SilentlyContinue |
    Select-Object Id, Name, @{N = 'RAM_MB'; E = { [int]($_.WorkingSet64 / 1MB) } }, StartTime |
    Format-Table -AutoSize

Write-Host "`n===== 3. .wslconfig LIMITS ====="
$wslc = "$env:USERPROFILE\.wslconfig"
if (Test-Path $wslc) {
    Get-Content $wslc | Where-Object { $_ -match 'memory|swap|processors|autoMemory|sparseVhd' }
} else {
    Write-Host "NO .wslconfig (WSL2 uses defaults: 50% of host RAM)"
}

Write-Host "`n===== 4. DISK SPACE (C: where Docker lives) ====="
Get-PSDrive C, D -ErrorAction SilentlyContinue |
    Select-Object Name, @{N = 'UsedGB'; E = { [math]::Round($_.Used / 1GB, 1) } }, @{N = 'FreeGB'; E = { [math]::Round($_.Free / 1GB, 1) } } |
    Format-Table -AutoSize

Write-Host "`n===== 5. DOCKER VHDX SIZE (grows monotonically without sparseVhd) ====="
Get-ChildItem "$env:LOCALAPPDATA\Docker\wsl" -Recurse -Filter *.vhdx -ErrorAction SilentlyContinue |
    Select-Object FullName, @{N = 'GB'; E = { [math]::Round($_.Length / 1GB, 2) } } |
    Format-Table -AutoSize

Write-Host "`n===== 6. WSL DISTROS & STATES ====="
& wsl -l -v 2>&1

Write-Host "`n===== 7. RECENT SYSTEM EVENTS (docker/WSL/Hyper-V, last 3h) ====="
$events = Get-WinEvent -FilterHashtable @{LogName = 'System'; StartTime = (Get-Date).AddHours(-3) } -ErrorAction SilentlyContinue |
    Where-Object { $_.Message -match 'docker|WSL|vmmem|Hyper-V|Lxss' -or $_.ProviderName -match 'docker|Lxss' } |
    Select-Object -First 15 TimeCreated, Id, ProviderName, @{N = 'Msg'; E = {
        $m = ($_.Message -replace '\r?\n', ' ')
        $m.Substring(0, [math]::Min(130, $m.Length))
    } }
if ($events) { $events | Format-Table -AutoSize -Wrap } else { Write-Host "(no matching System events in last 3h)" }

Write-Host "`n===== 8. com.docker.service STATUS & RECOVERY POLICY ====="
Get-Service com.docker.service -ErrorAction SilentlyContinue |
    Select-Object Name, Status, StartType | Format-Table -AutoSize
& sc.exe qfailure com.docker.service 2>&1 | Select-Object -First 12

Write-Host "`n===== 9. HOW MANY cua- CONTAINERS EXIST (leak indicator) ====="
& docker ps -a --filter "name=cua-" --format "{{.Names}} {{.Status}}" 2>&1
$count = (& docker ps -aq --filter "name=cua-" 2>&1).Count
Write-Host "cua- container count: $count"

Write-Host "`n===== 10. SYSTEM UPTIME ====="
$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
$up = (Get-Date) - $boot
Write-Host ("Last boot: {0}  Uptime: {1} days {2}h {3}m" -f $boot, [int]$up.TotalDays, $up.Hours, $up.Minutes)
