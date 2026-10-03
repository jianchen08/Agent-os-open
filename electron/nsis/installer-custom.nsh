; 装机安装器自定义宏集（electron-builder build.nsis.include 唯一入口）。
;
; ── BUG-12（静默卸载挂死）修复：替换默认 CHECK_APP_RUNNING 的进程探测实现 ──
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
; ── 自动登录 env 播种（ADR 2026-10-02-packaged-auto-login-env-only）──
;
; 装机契约：安装器为用户级环境变量 AGENTOS_ADMIN_PASSWORD 播种一份密码学随机
; 口令（每安装身份一份，128bit→32 hex），应用侧只认 env 自动登录；未播种即
; 部署 bug（应用 warn 回落登录框），应用自身绝不生成口令。口令不写日志、不回显。
;
; 注意：声明必须置于顶层（本文件在模板之前被 include，!include/Var 不能出现在
; Function 上下文内——即宏的展开点）。
!include "getProcessInfo.nsh"
!include "LogicLib.nsh"
Var /GLOBAL pid
Var /GLOBAL IsPowerShellAvailable
Var /GLOBAL AgentOsAdminPassword

!macro customCheckAppRunning
  StrCpy $IsPowerShellAvailable 1
  !insertmacro _CHECK_APP_RUNNING
!macroend

; 播种语义：已存在则原样保留（升级不换口令，既有会话不因升级全灭，内核重启
; 自对齐）；CryptGenRandom 失败则不播种（宁缺毋弱——弱口令比未播种更危险，
; 后者走「未播种=部署 bug」的显式回落路径）。
!macro customInstall
  ReadRegStr $AgentOsAdminPassword HKCU "Environment" "AGENTOS_ADMIN_PASSWORD"
  ${If} $AgentOsAdminPassword == ""
    System::Call 'advapi32::CryptAcquireContext(*p .r0, p 0, p 0, i 1, i 0xF0000000)i .r1'
    ${If} $1 != 0
      StrCpy $AgentOsAdminPassword ""
      ${For} $2 1 4
        System::Call 'advapi32::CryptGenRandom(p r0, i 4, *i .r3)i .r4'
        ${If} $4 != 0
          IntFmt $5 "%08X" $3
          StrCpy $AgentOsAdminPassword "$AgentOsAdminPassword$5"
        ${EndIf}
      ${Next}
      System::Call 'advapi32::CryptReleaseContext(p r0, i 0)'
    ${EndIf}
    StrLen $6 $AgentOsAdminPassword
    ${If} $6 == 32
      WriteRegStr HKCU "Environment" "AGENTOS_ADMIN_PASSWORD" $AgentOsAdminPassword
      ; 广播 WM_SETTINGCHANGE（SMTO_ABORTIFHUNG），资源管理器等进程即时感知新变量
      System::Call 'user32::SendMessageTimeout(p 0xFFFF, i 0x001A, p 0, t "Environment", i 2, i 10000, *p .r7)'
    ${EndIf}
  ${EndIf}
!macroend

; 卸载清场：移除播种的环境变量（重装会重新播种，内核重置语义自对齐）。
; ReadRegStr 为惰性引用：卸载器脚本不含 customInstall（Var 声明在此上下文
; 无使用点），不引用即触发 makensis 6001 警告；builder 新版按 error 处理。
!macro customUnInstall
  ReadRegStr $AgentOsAdminPassword HKCU "Environment" "AGENTOS_ADMIN_PASSWORD"
  DeleteRegValue HKCU "Environment" "AGENTOS_ADMIN_PASSWORD"
  System::Call 'user32::SendMessageTimeout(p 0xFFFF, i 0x001A, p 0, t "Environment", i 2, i 10000, *p .r7)'
!macroend
