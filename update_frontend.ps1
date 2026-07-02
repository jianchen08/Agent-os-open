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

    # BUG-FIX-20260702_partial_node_modules:
    # 问题根因: node_modules 目录可能存在但不完整（如 .bin/vite 链接缺失），
    #   仅 Test-Path 'node_modules' 为真就跳过安装，导致后续 npm run build 找不到
    #   vite 而失败。原逻辑对此失败 exit 0 静默跳过，但仍误写 hash 标记，
    #   造成"指纹说已更新、dist 实际是旧产物"的死锁——每次重启都跳过重建。
    # 修复方案: 用 vite 可执行文件的存在作为依赖完整性的权威信号；缺失即重装。
    $viteBin = Join-Path $frontendDir 'node_modules\.bin\vite.cmd'
    if (-not (Test-Path 'node_modules') -or -not (Test-Path $viteBin)) {
        Write-Host '[INFO] Installing frontend dependencies (node_modules missing or incomplete)...'
        cmd /c "$npmCmd install 2>&1" | Out-Host
    }

    # 4. Build
    Write-Host '[INFO] Building frontend...'
    $distDir = Join-Path $frontendDir 'dist'

    # BUG-FIX-20260702_build_failure_silent_exit:
    # 问题根因: 原逻辑用 Test-Path $distDir(旧产物存在)当作"构建成功"信号,
    #   构建失败时旧 dist 仍在 → 跳过失败分支 → 误把新 hash 写进标记文件。
    #   下次启动指纹"一致"永不重建，前端改动永久不生效。
    # 修复方案: 以 npm run build 的真实退出码为权威成功信号；失败则不写 hash、
    #   明确报错退出（exit 1），让用户感知并手动修复，杜绝静默死锁。
    cmd /c "$npmCmd run build 2>&1" | Out-Host
    $buildExit = $LASTEXITCODE
    if ($buildExit -ne 0 -or -not (Test-Path $distDir)) {
        Write-Host "[ERROR] Frontend build failed (exit=$buildExit). NOT updating hash marker to avoid stale-dist lock."
        Write-Host '[ERROR] 容器将继续使用旧前端代码。请修复构建错误后重新运行。'
        exit 1
    }

    # 5. Inject into container (container must be running at this point)
    # BUG-FIX-fix_20260628_docker_cp_silent_failure:
    # 问题根因: docker cp 偶发"退出码 0 但未真正写入容器可写层"(疑似运行中容器
    #   文件锁/时机问题)。原逻辑仅靠 $LASTEXITCODE 判定成功,导致 cp 假成功时
    #   脚本误报 [OK] —— 宿主机已构建新代码、容器内仍是旧 dist,前端改动不生效
    #   且无任何告警。
    # 修复方案: cp 后对比"宿主机 dist 与容器内 /app/dist"的入口 JS hash,
    #   不一致即视为假成功:重试一次 cp,仍失败则明确报错退出(exit 1)。
    #   实现"状态可感知",杜绝静默失败。
    # 注意: 不在容器内跑 grep —— PowerShell->docker exec->sh 三层引号传递会让
    #       正则里的双引号被吞掉。改用 docker exec cat 读回 index.html + PowerShell
    #       regex 提取,宿主机与容器两侧共用同一正则(scriptBlock),逻辑对称可靠。
    $ErrorActionPreference = 'Continue'
    & docker cp "$distDir/." "${containerName}:/app/dist/" 2>&1 | Out-Null
    $cpExit = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($cpExit -ne 0) {
        Write-Host '[WARN] docker cp failed, container may not be running yet'
        exit 0
    }

    # 验证 cp 是否真正生效:对比 index.html 引用的入口 JS hash(退出码 0 不代表文件已写入)
    $extractEntryJs = {
        param([string]$content)
        ([regex]::Match($content, 'assets/index-[^"]+\.js')).Value
    }
    $hostIndex = Join-Path $distDir 'index.html'
    $hostHash = & $extractEntryJs (Get-Content $hostIndex -Raw)
    $containerHash = & $extractEntryJs ((docker exec $containerName cat /app/dist/index.html 2>$null) -join "`n")
    if ($containerHash -ne $hostHash) {
        Write-Host "[WARN] docker cp 假成功(退出码0但容器内未更新),重试一次... 宿主机=$hostHash 容器=$containerHash"
        & docker cp "$distDir/." "${containerName}:/app/dist/" 2>&1 | Out-Null
        $containerHash = & $extractEntryJs ((docker exec $containerName cat /app/dist/index.html 2>$null) -join "`n")
        if ($containerHash -ne $hostHash) {
            Write-Host "[WARN] docker cp 重试后容器内仍为旧文件,降级重建镜像...(宿主机=$hostHash 容器=$containerHash)"
            # BUG-FIX-fix_20260629_cp_fallback_rebuild:
            # 问题: docker cp 偶发假成功且重试无效(容器可写层文件锁/容器被外部
            #   并发会话用旧镜像重建等)。cp 失败时若只 exit 1 放弃,容器将永久
            #   跑旧代码,前端改动始终不生效(start_web_cn.bat 镜像存在时只走本 cp 路径)。
            # 修复: cp 重试仍失败 → docker compose up -d --build frontend 重建镜像,
            #   把宿主机最新 dist 烧进镜像层(Dockerfile 路径A: COPY frontend/dist)。
            #   重建后容器内 dist 必然是新的,从根上消除"容器跑旧代码"。
            & docker compose up -d --build frontend 2>&1 | Out-Host
            if ($LASTEXITCODE -ne 0) {
                Write-Host "[ERROR] 镜像重建也失败,放弃。请手动检查 docker compose build frontend"
                exit 1
            }
            # 重建后容器已用新镜像重启,直接确认并跳过后续 cp 路径的 restart
            $containerHash = & $extractEntryJs ((docker exec $containerName cat /app/dist/index.html 2>$null) -join "`n")
            if ($containerHash -ne $hostHash) {
                Write-Host "[ERROR] 镜像重建后容器内入口 JS 仍不匹配(宿主机=$hostHash 容器=$containerHash)"
                exit 1
            }
            Write-Host "[OK] 镜像重建成功,容器内入口 JS 已更新: $containerHash"
            Get-Date | Out-File -FilePath $markFile -Encoding ascii
            if ($currentHash) {
                $currentHash | Out-File -FilePath $hashFile -Encoding ascii
            }
            exit 0
        }
    }
    Write-Host "[OK] 容器内入口 JS 已更新: $containerHash"

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
