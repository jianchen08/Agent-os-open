#Requires -Version 5.1
# Windows Docker backend selection & stability config.
#
# WSL2 backend has known fragility: ext4.vhdx single-file lock, 9p file proxy
# crashes. Pro/Enterprise can switch to Hyper-V backend to eliminate these.
#
# Duty (deploy-time prevention, orthogonal to runtime recovery restart_docker.ps1):
#   1. Detect if Windows edition supports Hyper-V
#   2. Pro/Enterprise -> enable Hyper-V + configure Docker Desktop to Hyper-V
#   3. Home -> WSL2 only, warn about stability risk + give optimization tips
#
# Usage: powershell -File setup_backend.ps1 [-Force]
# Exit codes:
#   0 = backend ready (Hyper-V, or Home edition on WSL2 with tips given)
#   1 = reboot needed to enable Hyper-V
#   2 = user cancelled / unrecoverable error

param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$msg) { Write-Host "[setup_backend] $msg" }
function Write-Warn2([string]$msg) { Write-Host "[setup_backend] [WARN] $msg" }
function Write-Err2([string]$msg) { Write-Host "[setup_backend] [ERROR] $msg" }

# Check if running as administrator
function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Detect if Windows edition supports Hyper-V
# Pro=48, Enterprise=49, Education=121, ProWorkstation=122, EnterpriseLTSC=125
function Get-HyperVEligibility {
    $os = Get-CimInstance Win32_OperatingSystem
    $proSkus = @(48, 49, 121, 122, 125)
    if ($os.OperatingSystemSKU -in $proSkus) {
        return @{ Eligible = $true; Edition = $os.Caption }
    }
    return @{ Eligible = $false; Edition = $os.Caption }
}

# Check if Hyper-V feature is enabled (requires admin)
function Test-HyperVEnabled {
    try {
        $f = Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -ErrorAction Stop
        return $f.State -eq 'Enabled'
    } catch {
        return $false
    }
}

# Read Docker Desktop current backend.
# Returns a hashtable:
#   @{ Status='wsl2'|'hyperv'; File=<path> }   - config found with backend field
#   @{ Status='no-field'; File=<path> }        - config found but no backend field (new DD)
#   $null                                       - config file not found at all
function Get-DockerBackend {
    $paths = @(
        "$env:APPDATA\Docker\settings-store.json",
        "$env:APPDATA\Docker\settings.json"
    )
    foreach ($p in $paths) {
        if (Test-Path $p) {
            try {
                $cfg = Get-Content $p -Raw -ErrorAction Stop | ConvertFrom-Json
                if ($cfg.PSObject.Properties.Name -contains 'wslEngineEnabled') {
                    return @{ Status = ($(if ([bool]$cfg.wslEngineEnabled) { 'wsl2' } else { 'hyperv' })); File = $p }
                }
                # config exists but has no backend field: new Docker Desktop version
                return @{ Status = 'no-field'; File = $p }
            } catch {
                Write-Warn2 "Read Docker config failed: $p"
            }
        }
    }
    return $null
}

# Configure Docker Desktop backend to Hyper-V via settings file
function Set-DockerBackendHyperV {
    param([string]$SettingsFile)
    try {
        $cfg = Get-Content $SettingsFile -Raw -ErrorAction Stop | ConvertFrom-Json
        # PSCustomObject 不能直接给不存在的属性赋值,用 Add-Member -Force
        # (字段缺失时 Docker Desktop 用默认值 WSL2=true,需主动写入 false)
        if ($cfg.PSObject.Properties.Name -contains 'wslEngineEnabled') {
            $cfg.wslEngineEnabled = $false
        } else {
            $cfg | Add-Member -MemberType NoteProperty -Name 'wslEngineEnabled' -Value $false
        }
        $cfg | ConvertTo-Json -Depth 20 | Set-Content $SettingsFile -Encoding UTF8
        Write-Step "Configured Docker Desktop to use Hyper-V backend: $SettingsFile"
        return $true
    } catch {
        Write-Warn2 "Write Docker backend config failed: $($_.Exception.Message)"
        return $false
    }
}

$script:DD_EXE = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'

function Stop-DockerDesktop {
    "Quit Docker Desktop gracefully, force-kill if needed, then shut down WSL."
    Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue | ForEach-Object {
        try { $_.CloseMainWindow() | Out-Null } catch {}
    }
    $deadline = (Get-Date).AddSeconds(15)
    while (((Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue).Count -gt 0) -and ((Get-Date) -lt $deadline)) {
        Start-Sleep -Milliseconds 800
    }
    # force-kill stubborn processes
    $procs = @('Docker Desktop','com.docker.backend','com.docker.build','com.docker.service','docker','vpnkit','com.docker.gui')
    foreach ($n in $procs) {
        Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object {
            try { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue } catch {}
        }
    }
    # shut down WSL backend (only relevant when switching away from WSL2)
    & wsl --shutdown 2>$null
    Start-Sleep -Seconds 3
}

function Start-DockerDesktop {
    if (Test-Path $script:DD_EXE) {
        Start-Process -FilePath $script:DD_EXE | Out-Null
    } else {
        Write-Warn2 "Docker Desktop.exe not found at default path; please start manually."
    }
}

function Wait-DockerReady {
    param([int]$TimeoutSec = 120)
    "Poll docker info until daemon answers or timeout."
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = 'docker'
        $psi.Arguments = 'info -f {{.ServerVersion}}'
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        try {
            $proc = [System.Diagnostics.Process]::Start($psi)
            if ($proc.WaitForExit(8000) -and $proc.ExitCode -eq 0) {
                $out = $proc.StandardOutput.ReadToEnd()
                if ($out -and $out.Trim() -match '^\d') { return $true }
            } else {
                try { $proc.Kill() } catch {}
            }
        } catch {}
        Start-Sleep -Seconds 3
    }
    return $false
}

# ===========================================================================
# Main
# ===========================================================================

# 0. Non-Windows: exit cleanly (called by cross-platform entry)
if ($PSVersionTable.Platform -and $PSVersionTable.Platform -ne 'Win32NT') {
    Write-Step "Non-Windows system, skip backend selection."
    exit 0
}

# 1. Check Docker installed (if not, install_cn.bat winget step handles it)
$dockerExe = Get-Command docker -ErrorAction SilentlyContinue
$ddPath = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$dockerInstalled = ($dockerExe -ne $null) -or (Test-Path $ddPath)

if (-not $dockerInstalled) {
    Write-Step "Docker not installed, backend selection will run again after install."
    exit 0
}

# 2. Check Hyper-V eligibility
$elig = Get-HyperVEligibility
Write-Step "System: $($elig.Edition)"

if (-not $elig.Eligible) {
    # Home edition: WSL2 only, give optimization tips
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Yellow
    Write-Host " NOTE: This Windows edition only supports WSL2 backend" -ForegroundColor Yellow
    Write-Host "========================================" -ForegroundColor Yellow
    Write-Host "WSL2 has known stability issues - container hangs on heavy create/delete." -ForegroundColor Yellow
    Write-Host "Optimization tips:" -ForegroundColor Yellow
    Write-Host "  1. Exclude vhdx from Windows Defender scan (top hidden killer)"
    Write-Host "     Path: %LOCALAPPDATA%\Packages\*ext4.vhdx"
    Write-Host "  2. Set Docker Desktop memory to 8G+ (Settings -> Resources)"
    Write-Host "  3. Long-term: upgrade to Windows Pro to enable Hyper-V backend"
    Write-Host "========================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Step "Home edition WSL2 check done."
    exit 0
}

# 3. Pro/Enterprise: prefer Hyper-V
Write-Step "This edition supports Hyper-V backend, checking status..."

if (-not (Test-Admin)) {
    Write-Warn2 "Enable/switch Hyper-V backend requires administrator privileges."
    Write-Warn2 "Run install_cn.bat as admin, or manually in Docker Desktop:"
    Write-Warn2 "Settings -> General -> uncheck 'Use the WSL 2 based engine'."
    # Non-blocking: WSL2 still works without Hyper-V, just has hang risk
    exit 0
}

$hvEnabled = Test-HyperVEnabled
$script:NeedReboot = $false

if (-not $hvEnabled) {
    Write-Step "Enabling Hyper-V feature - requires reboot to take effect..."
    try {
        Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -NoRestart -ErrorAction Stop | Out-Null
        Write-Step "Hyper-V feature enabled - pending reboot."
    } catch {
        Write-Err2 "Enable Hyper-V failed: $($_.Exception.Message)"
        Write-Warn2 "Falling back to WSL2 backend - has hang risk."
        exit 0
    }
    $script:NeedReboot = $true
} else {
    Write-Step "Hyper-V feature already enabled."
}

# 4. Switch Docker Desktop backend to Hyper-V
$current = Get-DockerBackend
if ($null -eq $current) {
    # config file missing: Docker Desktop not started yet, or never initialized
    Write-Warn2 "Docker Desktop config not found, may not be started yet."
    Write-Warn2 "Start Docker Desktop once to init, then re-run this script."
    Write-Step "Backend selection skipped - please verify in Docker Desktop GUI."
    exit 0
}

if ($current.Status -eq 'no-field') {
    # Docker Desktop 没有把 wslEngineEnabled 显式写入配置(默认值 WSL2=true 不落盘)。
    # 查证:手动写入 wslEngineEnabled=false + 重启 Docker Desktop 即切到 Hyper-V 后端。
    # 参考: https://medium.com/code-kings/docker-how-to-switch-between-hyper-v-and-wsl-directly-in-the-settings-json-file
    Write-Step "Config has no backend field - writing wslEngineEnabled=false to switch to Hyper-V."
    $written = Set-DockerBackendHyperV -SettingsFile $current.File
    if (-not $written) {
        Write-Err2 "Failed to write backend config. Please switch manually in Docker Desktop GUI."
        exit 2
    }
    Write-Step "Stopping Docker Desktop to apply new backend..."
    Stop-DockerDesktop
    Write-Step "Starting Docker Desktop with Hyper-V backend..."
    Start-DockerDesktop
    $ready = Wait-DockerReady -TimeoutSec 120
    if (-not $ready) {
        Write-Warn2 "Docker Desktop did not become ready in 120s. Please start it manually."
        exit 2
    }
    Write-Step "Docker Desktop restarted with Hyper-V backend."
    exit 0
}

# Backend field exists (older Docker Desktop with wslEngineEnabled)
if ($current.Status -eq 'hyperv') {
    Write-Step "Docker Desktop already on Hyper-V backend, no switch needed."
    if ($script:NeedReboot) {
        Write-Host "[setup_backend] Reboot needed for Hyper-V to take effect." -ForegroundColor Yellow
        exit 1
    }
    exit 0
}

# Currently WSL2, switch to Hyper-V
if ($script:NeedReboot) {
    Write-Step "Writing Hyper-V config before reboot, will auto-apply after reboot."
} else {
    Write-Step "Switching Docker Desktop backend: WSL2 -> Hyper-V..."
}
Set-DockerBackendHyperV -SettingsFile $current.File | Out-Null

# 5. If Hyper-V feature was just enabled, reboot needed
if ($script:NeedReboot) {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host " Reboot Windows to activate Hyper-V backend" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "Hyper-V config written. After reboot, Docker Desktop will use Hyper-V."
    Write-Host "After reboot, re-run install_cn.bat to finish deployment."
    Write-Host "========================================" -ForegroundColor Cyan
    exit 1
}

Write-Step "Backend config done - Hyper-V."
exit 0
