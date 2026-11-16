# Multi-device path tree mount (device branch: VM-0-16-ubuntu).
# Scheduled task LingxiMultiDeviceMount (ONLOGON + manual schtasks /run).
# Semantics (user-approved 2026-10-19): all connected devices = one logical device
# with multiple disks; each remote device = one standalone NAS drive on this machine
# (drive-letter form, volume name = device name; remote drive-letter layout never
# leaks into local view). Write-through (vfs-write-back 0s); --network-mode for
# cross-session visibility. Unified tree branch /<device> is a symlink at the Git
# root pointing at this drive (frontend tree and agent share the same path language).
# NOTE: ASCII-only on purpose - PowerShell 5.1 misparses BOM-less UTF-8 comments.
$ErrorActionPreference = 'Continue'
$Log = 'D:\myproject\container_e17cc5927dfd\.zctmp\rclone_mount_task.log'
$Diag = 'D:\myproject\container_e17cc5927dfd\.zctmp\rclone_reclean_diag.txt'

# --- idempotent watchdog entry: live rclone = nothing to do. The task now
# repeats every 5 minutes (ONLOGON trigger kept); only a dead rclone pays the
# reclean+remount cost (single-instance rule: repeated kill+remount cycles
# cause VFS cache data loss). NOTE: the V: drive check CANNOT live here - the
# task runs elevated and an elevated token sees no network-mode drives (probed
# 2026-10-22: Get-ChildItem V:\ and Win32_LogicalDisk both empty from
# elevated), so rclone process liveness is the only elevation-proof health
# signal; SFTP flakes are rclone's own reconnect domain. ---
$rcloneAlive = @(Get-Process rclone -ErrorAction SilentlyContinue).Count -gt 0
if ($rcloneAlive) {
  Write-Output 'MOUNT_TASK_RESULT=ALREADY_OK'
  exit 0
}

# --- resolve rclone: the packaged app layout has already moved once
# (resources -> agent-os\resources), which is what silently killed the mount;
# probe known homes instead of hardcoding one, so the next layout drift
# self-heals at remount time ---
$RcloneHomes = @(
  'D:\agentos-r297-main\agent-os\resources\bin\rclone.exe',
  'D:\agentos-r297-main\resources\bin\rclone.exe'
)
$Rclone = $null
foreach ($candidate in $RcloneHomes) {
  if (Test-Path $candidate) { $Rclone = $candidate; break }
}
if (-not $Rclone) {
  $glob = Get-ChildItem 'D:\agentos-*\agent-os\resources\bin\rclone.exe',
    'D:\agentos-*\resources\bin\rclone.exe' -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if ($glob) { $Rclone = $glob.FullName }
}
if (-not $Rclone) {
  "rclone resolve FAIL: no candidate found" | Out-File $Diag -Append -Encoding utf8
  Write-Output 'MOUNT_TASK_RESULT=NO_RCLONE'
  exit 2
}

# VBS reads the exe path from this file (wscript gets no args through the
# explorer proxy; a stale hardcoded path in the VBS is the original failure)
[IO.File]::WriteAllText('D:\myproject\container_e17cc5927dfd\.zctmp\rclone_launch_path.txt', $Rclone + "`r`n")
"rclone resolved: $Rclone" | Out-File $Diag -Append -Encoding utf8

# --- stale instance cleanup (elevated orphans survive non-admin shells; this
# RL HIGHEST task owns the kill. taskkill /T tree-kill + diagnostics to disk) ---
"=== reclean $(Get-Date -Format 'HH:mm:ss') ===" | Out-File $Diag -Append -Encoding utf8
whoami /groups | findstr /i "S-1-16-12288 S-1-16-8192" | Out-File $Diag -Append -Encoding utf8
Get-Process rclone -ErrorAction SilentlyContinue | ForEach-Object {
  $kr = taskkill /F /T /PID $_.Id 2>&1 | Out-String
  "kill $($_.Id): $($kr.Trim())" | Out-File $Diag -Append -Encoding utf8
}
try { Restart-Service WinFsp.Launcher -Force -ErrorAction Stop; "launcher restarted" | Out-File $Diag -Append -Encoding utf8 } catch { "launcher FAIL: $_" | Out-File $Diag -Append -Encoding utf8 }
Start-Sleep -Seconds 3
Get-Process rclone -ErrorAction SilentlyContinue | ForEach-Object {
  $kr = taskkill /F /T /PID $_.Id 2>&1 | Out-String
  "rekill $($_.Id): $($kr.Trim())" | Out-File $Diag -Append -Encoding utf8
}
Start-Sleep -Seconds 2
"left after kill: $(@(Get-Process rclone -ErrorAction SilentlyContinue).Count)" | Out-File $Diag -Append -Encoding utf8

# --- stale directory-form anchors (historical mis-builds; drive-letter is final) ---
foreach ($stale in 'C:\VM-0-16-ubuntu', 'D:\VM-0-16-ubuntu') {
  if (Test-Path $stale) { cmd /c "rmdir /s /q $stale" 2>&1 | Out-Null }
}

# --- mount: standalone NAS drive V: named after the device ---
# Launched through the explorer proxy (mount_launch.vbs) so rclone runs in the
# user's filtered token: an elevated mount lands in the elevated session's
# drive-letter space (UAC isolation) and stays invisible to the normal session.
$vbs = 'D:\myproject\container_e17cc5927dfd\deploy\multi_device\mount_launch.vbs'
Start-Process explorer.exe -ArgumentList "`"$vbs`""
Start-Sleep -Seconds 10

# --- unified tree branch at Git root: /<device> symlink -> V:\ (fix stale target) ---
$GitLink = 'C:\Program Files\Git\VM-0-16-ubuntu'
$needsFix = $true
if (Test-Path $GitLink) {
  $item = Get-Item $GitLink -Force
  if ($item.LinkType -eq 'SymbolicLink' -and ("$($item.Target)" -match '^V:')) { $needsFix = $false }
}
if ($needsFix) {
  if (Test-Path $GitLink) { cmd /c "rmdir `"$GitLink`"" 2>&1 | Out-Null }
  New-Item -ItemType SymbolicLink -Path $GitLink -Target 'V:\' -ErrorAction Continue | Out-Null
}

# --- verify (elevated context: network-mode drives are UAC-blind here, so this
# reports drive visibility from elevated only - False does NOT mean the mount
# failed; check V: from a normal shell) ---
$ok = $false
try { $ok = (Get-ChildItem 'V:\' -ErrorAction Stop).Count -ge 0 } catch { $ok = $false }
"volVisibleFromElevated: $ok" | Out-File $Diag -Append -Encoding utf8
if (Test-Path $Log) { Get-Content $Log -Tail 3 | Out-File $Diag -Append -Encoding utf8 }
Write-Output ("MOUNT_TASK_RESULT=" + $(if ($ok) { 'OK' } else { 'CHECK_LOG' }))
