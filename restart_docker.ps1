# Restart Docker daemon when it is hung (check_docker.ps1 returned 3).
#
# Usage: powershell -File restart_docker.ps1
# Exit codes:
#   0 = Docker restarted and became ready
#   1 = restart attempted but daemon still not ready (timed out)
#   2 = user declined the restart confirmation

param(
    [int]$ReadyTimeout = 90,
    [switch]$Yes
)

$ErrorActionPreference = 'Continue'

# --- 1. confirm with the user (restart kills running containers) ---
# Skip the prompt when -Yes is passed (non-interactive callers) or when there is
# no interactive host UI (e.g. invoked from another script with no console).
$doRestart = $false
if ($Yes) {
    $doRestart = $true
} else {
    # Use Read-Host instead of $host.UI.PromptForChoice: PromptForChoice throws a
    # ChoiceDescription->SwitchParameter binding error when this script is invoked
    # non-interactively via "powershell -File" from start_web_cn.bat, which made
    # the auto-recovery path unreachable. Read-Host reads from the parent console
    # (cmd window) reliably and needs no host-specific parameter binding.
    try {
        if ($null -ne $host.UI -and $null -ne $host.UI.RawUI) {
            Write-Host ''
            Write-Host 'Docker daemon is not responding (hung).'
            Write-Host 'Restarting Docker will STOP all currently running containers.'
            $ans = Read-Host 'Restart Docker now? [y/N]'
            if ($ans -and $ans.Trim() -match '^[yY]') { $doRestart = $true }
        }
    } catch {
        Write-Host "[restart_docker] no interactive host available: $($_.Exception.Message)"
    }
}
if (-not $doRestart) {
    Write-Host '[restart_docker] restart declined (no -Yes and no interactive host). exiting.'
    exit 2
}

Write-Host '[restart_docker] stopping Docker Desktop and WSL backend...'

# --- 2. quit Docker Desktop gracefully first (sends quit signal) ---
$dd = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue | ForEach-Object {
    try { $_.CloseMainWindow() | Out-Null } catch {}
}
# give it a few seconds to quit gracefully
$grace = Get-Date
while (((Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue).Count -gt 0) -and ((Get-Date) - $grace).TotalSeconds -lt 12) {
    Start-Sleep -Milliseconds 800
}

# --- 3. force-kill any stubborn Docker / docker zombie processes ---
$procNames = @('Docker Desktop','com.docker.backend','com.docker.build','com.docker.service','docker','docker-sandbox','vpnkit')
foreach ($n in $procNames) {
    Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object {
        try { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue } catch {}
    }
}

# --- 4. shut down WSL (the container backend on Windows) ---
Write-Host '[restart_docker] running: wsl --shutdown'
& wsl --shutdown 2>$null
Start-Sleep -Seconds 3

# --- 5. clear the named-pipe / context so a fresh daemon can start ---
Remove-Item '\\.\pipe\docker_engine' -ErrorAction SilentlyContinue
Remove-Item '\\.\pipe\dockerBackendApi' -ErrorAction SilentlyContinue

# --- 6. start Docker Desktop fresh ---
Write-Host '[restart_docker] starting Docker Desktop...'
if (Test-Path $dd) {
    Start-Process -FilePath $dd
} else {
    Write-Host '[restart_docker] Docker Desktop.exe not found at default path; please start it manually.'
    exit 1
}

# --- 7. wait until daemon answers docker info ---
Write-Host "[restart_docker] waiting up to $ReadyTimeout s for daemon to become ready..."
$deadline = (Get-Date).AddSeconds($ReadyTimeout)
$ready = $false
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
        if ($proc.WaitForExit(8000)) {
            if ($proc.ExitCode -eq 0) {
                $out = $proc.StandardOutput.ReadToEnd()
                if ($out -and $out.Trim() -match '^\d') { $ready = $true; break }
            }
        } else {
            try { $proc.Kill() } catch {}
        }
    } catch {}
    Start-Sleep -Seconds 3
}

if ($ready) {
    Write-Host '[restart_docker] Docker daemon is ready.'
    exit 0
} else {
    Write-Host '[restart_docker] daemon still not ready after restart; please check Docker Desktop manually.'
    exit 1
}
