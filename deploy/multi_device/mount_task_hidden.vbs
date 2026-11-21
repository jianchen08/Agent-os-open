' Hidden host for the mount watchdog. The scheduled task runs THIS file through
' wscript.exe (GUI subsystem), so Task Scheduler never creates a console window;
' the PowerShell child is started with window style 0 (hidden). Background: with
' powershell.exe as the task action, every 5-minute repetition flashed a console
' showing only the MOUNT_TASK_RESULT line - cosmetic, the mount was healthy.
' Keep this file ASCII-only on purpose (host encoding pitfalls, see mount_vps.ps1).
' The ps1 path below is the single coupling point: if mount_vps.ps1 moves, update
' it here too (wscript Run fails silently on a missing script).
Option Explicit
Dim fso, sh, ps1, diag, cmd, rc
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
ps1  = "D:\myproject\container_e17cc5927dfd\deploy\multi_device\mount_vps.ps1"
diag = "D:\myproject\container_e17cc5927dfd\.zctmp\rclone_reclean_diag.txt"

If Not fso.FileExists(ps1) Then
  AppendDiag "hidden host FAIL: ps1 missing: " & ps1
  WScript.Quit 2
End If

' window style 0 = hidden, bWaitOnReturn = True so the child exit code is the
' task result (0/1; raw code goes to the diag file when non-zero)
cmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & ps1 & """"
rc = sh.Run(cmd, 0, True)

If rc <> 0 Then
  AppendDiag "hidden host: powershell exit code " & rc
  WScript.Quit 1
End If
WScript.Quit 0

Sub AppendDiag(line)
  Dim f
  Set f = fso.OpenTextFile(diag, 8, True, 0)
  f.WriteLine Now & " " & line
  f.Close
End Sub
