; BUG-12（静默卸载挂死）修复：替换 electron-builder 默认 CHECK_APP_RUNNING 的进程探测实现。
;
; 根因（2026-09-15 实测，app-builder-lib 26.15.3 templates/nsis/include/allowOnlyOneInstallerInstance.nsh）：
; 默认实现先经 IS_POWERSHELL_AVAILABLE 两次 PowerShell 预检，再用
;   nsExec::Exec powershell Get-CimInstance Win32_Process（按 $INSTDIR 路径前缀找进程）
; nsExec 对子进程是无超时的同步等待；WMI 高负载/降级时该查询可分钟级乃至无限不返回
; （实测同一查询 3.3s→40-60s 抖动，R9 复现挂 50+ 分钟），且探测发生在卸载 Section
; 之前 —— 挂死时零删除、全部残留。本宏是模板官方替换点：定义 customCheckAppRunning
; 即跳过默认实现（见 CHECK_APP_RUNNING 的 !ifmacrondef 分支）。
;
; 本实现强制走同一模板内置的 tasklist/taskkill 分支（cmd /C 包装、不经 WMI、延迟有界）；
; 静默模式下重试 2 轮后经 MessageBox /SD IDCANCEL 退出，任何情况下不会无限等待。
; 检测语义由「INSTDIR 路径前缀」收窄为「镜像名精确匹配（per-user 追加 USERNAME 过滤）」：
; Electron 全部进程共用同一 exe 名，行为等价（0.2 per-user 安装契约不变；
; 2026-09-18 起 oneClick 关闭改 assisted 可选目录，perMachine 仍 false，
; 本检测依赖的是 per-user 而非一键 UI，不受影响）。
;
; 注意：声明必须置于顶层（本文件在模板之前被 include，!include/Var 不能出现在
; Function 上下文内——即本宏的展开点）。置 1（=「PowerShell 不可用」）使模板
; FIND_PROCESS/KILL_PROCESS 走 tasklist/taskkill 分支。
!include "getProcessInfo.nsh"
Var /GLOBAL pid
Var /GLOBAL IsPowerShellAvailable

!macro customCheckAppRunning
  StrCpy $IsPowerShellAvailable 1
  !insertmacro _CHECK_APP_RUNNING
!macroend
