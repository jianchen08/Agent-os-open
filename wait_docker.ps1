$ok = $false
for ($i = 1; $i -le 30; $i++) {
    Start-Sleep -Seconds 8
    try {
        $r = docker info --format '{{.ServerVersion}}' 2>&1
        if ($LASTEXITCODE -eq 0 -and $r -match '^\d') {
            Write-Host "[OK] Docker ready: $r"
            $ok = $true
            break
        }
    } catch {}
    Write-Host (Get-Date -Format 'HH:mm:ss') "[$i/30]..."
}
if (-not $ok) { Write-Host '[TIMEOUT]' }
