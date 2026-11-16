' Silent launcher for rclone mount. Runs in the user's filtered (non-elevated)
' token context via the explorer proxy in mount_vps.ps1, so the NAS drive lands
' in the normal session drive-letter space (elevated mounts are UAC-isolated).
' The exe path is read from .zctmp\rclone_launch_path.txt (rewritten by
' mount_vps.ps1 on every remount) - a hardcoded path here is what silently
' killed the mount when the packaged app moved into the agent-os subdirectory
' (wscript Run fails silently on a missing exe, zero log).
' ASCII-only on purpose (host encoding pitfalls, see mount_vps.ps1).
Set fso = CreateObject("Scripting.FileSystemObject")
rclone = ""
pathFile = "D:\myproject\container_e17cc5927dfd\.zctmp\rclone_launch_path.txt"
If fso.FileExists(pathFile) Then
  rclone = Trim(Replace(Replace(fso.OpenTextFile(pathFile).ReadAll, vbCr, ""), vbLf, ""))
End If
If rclone = "" Or Not fso.FileExists(rclone) Then WScript.Quit 2
CreateObject("WScript.Shell").Run _
  """" & rclone & """ mount VM-0-16-ubuntu:/home/ubuntu V:" & _
  " --vfs-cache-mode writes --vfs-write-back 0s --network-mode --dir-cache-time 5s" & _
  " --links --volname ""VM-0-16-ubuntu""" & _
  " --log-file ""D:\myproject\container_e17cc5927dfd\.zctmp\rclone_mount_task.log"" --log-level INFO", 0, False
