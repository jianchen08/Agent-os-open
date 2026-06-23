# Frontend update script: check if src changed, rebuild and inject into container
# Called by start_web.bat to avoid bat quoting issues
# Exit code: 0 = success (no update or updated), 1 = error

$ErrorActionPreference = 'Stop'
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$markFile = Join-Path $ROOT '.frontend_built_at'
$hashFile = Join-Path $ROOT '.frontend_src_hash'
$frontendDir = Join-Path $ROOT 'frontend'

# BUG-FIX-fix_20260621_wrong_container_name:
# 问题根因: 原代码硬编码容器名 'agent-os-frontend-036fa'，但不同项目实例容器名
#          后缀不同（22404/036fa/...）。当前项目 (22404) 改前端代码后，构建产物
#          被 docker cp 注入到了错误的容器 (036fa)，导致工作区持续显示
#          "工作区为空 — 模块激活后自动出现"（旧代码请求 /api/modules/ui 返回 404）。
# 修复方案: 用 `docker compose ps -q frontend` 动态获取当前 compose 项目的
#          frontend 容器 ID，不再依赖硬编码容器名。
# 影响范围: 前端代码热更新（start_web.bat 触发的 update_frontend.ps1）
# 修复日期: 2026-06-21
$containerName = (docker compose ps -q frontend 2>$null | Where-Object { $_.Trim() } | Select-Object -First 1)
if ($containerName) { $containerName = $containerName.Trim() }

# 1. Determine if update is needed by comparing content hash of frontend/src
# BUG-FIX-20260618: switched from LastWriteTime to content-hash detection
#   Root cause: git operations (pull/checkout/stash) do not update file
#               LastWriteTime, so the previous mtime-based comparison
#               incorrectly reported "unchanged" even when content changed.
#   Fix: compute an MD5 fingerprint over all files under frontend/src and
#        compare it to the fingerprint saved at last successful build.
#   Scope: detection logic only; build / inject / restart flows are untouched.
$needUpdate = $false

# Build a content fingerprint: for each file compute (relativePath=MD5),
# sort by path for cross-run stability, then MD5 the joined blob.
$srcDir = Join-Path $frontendDir 'src'
$currentHash = $null
if (Test-Path $srcDir) {
    $files = @(Get-ChildItem -Path $srcDir -Recurse -File | Sort-Object -Property FullName)
    if ($files.Count -gt 0) {
        $parts = foreach ($f in $files) {
            $h = (Get-FileHash -Path $f.FullName -Algorithm MD5).Hash
            $rel = $f.FullName.Substring($srcDir.Length)
            "$rel=$h"
        }
        $combined = $parts -join [char]10
        $tempFile = [System.IO.Path]::GetTempFileName()
        try {
            [System.IO.File]::WriteAllText($tempFile, $combined, [System.Text.Encoding]::UTF8)
            $currentHash = (Get-FileHash -Path $tempFile -Algorithm MD5).Hash
        } finally {
            Remove-Item $tempFile -ErrorAction SilentlyContinue
        }
    }
}

if (-not (Test-Path $markFile)) {
    # First-build mark missing -> need update
    $needUpdate = $true
} elseif (-not (Test-Path $hashFile) -or -not $currentHash) {
    # No fingerprint recorded yet (upgrading from old version) -> need update
    # and persist the first fingerprint after build.
    $needUpdate = $true
} else {
    $savedHash = (Get-Content $hashFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($savedHash -ne $currentHash) {
        $needUpdate = $true
    }
}

if (-not $needUpdate) {
    Write-Host '[OK] Frontend code unchanged'
    exit 0
}

Write-Host '[INFO] Frontend code updated, rebuilding and injecting...'

# 2. Check npm
$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) {
    Write-Host '[WARN] npm not found, skip frontend update (using old code in image)'
    exit 0
}

# 3. Install deps (first time)
Push-Location $frontendDir
try {
    # Use npm.cmd (not npm.ps1) to avoid PowerShell wrapping node stderr as errors
    $npmCmd = 'npm.cmd'

    if (-not (Test-Path 'node_modules')) {
        Write-Host '[INFO] Installing frontend dependencies...'
        cmd /c "$npmCmd install 2>&1" | Out-Host
    }

    # 4. Build
    Write-Host '[INFO] Building frontend...'
    $buildOutput = cmd /c "$npmCmd run build 2>&1"
    $buildOutput | Out-Host
    # Check if dist was actually produced (authoritative success signal)
    $distDir = Join-Path $frontendDir 'dist'
    if (-not (Test-Path $distDir)) {
        Write-Host '[WARN] Frontend build failed (no dist output), using old code in image'
        exit 0
    }

    # 5. Inject into container (container must be running at this point)
    $ErrorActionPreference = 'Continue'
    & docker cp "$distDir/." "${containerName}:/app/dist/" 2>&1 | Out-Null
    $cpExit = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($cpExit -ne 0) {
        Write-Host '[WARN] docker cp failed, container may not be running yet'
        exit 0
    }

    & docker restart $containerName 2>&1 | Out-Null
    Get-Date | Out-File -FilePath $markFile -Encoding ascii
    if ($currentHash) {
        $currentHash | Out-File -FilePath $hashFile -Encoding ascii
    }
    Write-Host '[OK] Frontend code updated and injected into container'
}
finally {
    Pop-Location
}

exit 0
