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
; 模板语义：`$IsPowerShellAvailable` 在 app-builder-lib 模板中 `0 = PowerShell 可用`、
; `1 = PowerShell 不可用（强制走 tasklist 分支）`，故赋值 1 即意图路径。
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
; ── 升级路径反馈（ADR 2026-10-05-nsis-upgrade-flow-and-defender-exclusion）──
;
; 实测事故（2026-10-04 22:55）：双击新 Setup.exe 后 25 分钟零反馈——新检测器
; 经 assistedInstaller 的 .onInit（line 175）→ ALLOW_ONLY_ONE_INSTALLER_INSTANCE
; → CHECK_APP_RUNNING → customCheckAppRunning 是用户最早可见的 NSIS 钩点，
; 在此读既有安装位注册表（initMultiUser 之前），命中即弹 MessageBox 告知
; 「升级中，最长约 1 分钟，聊天记录与配置保留」。该路径默认 interactive 模式
; 下走，silent 模式（`/S`）直接跳过（避免装机 toast 阻塞 CI）。
;
; ── Defender 排除路径（ADR 2026-10-05 / 2026-10-06 实机修正）──
;
; 装机递归解包 750MB 时每文件经 Defender 实时扫描是实测挂死真因（Setup 9 分钟
; CPU 仅 3.2s = 纯阻塞等扫描返回；old-uninstaller 删盘烧 151s CPU 同因）。
;
; 时机契约：排除必须写在文件解包（installApplicationFiles）**之前**——
; customInstall 在 copy 之后才跑，太晚；customInit 是 .onInit 内
; initMultiUser 之后的钩点，先于卸载旧版与 copy，静默升级的 UAC 内层
; 实例在此已是 admin，正是写排除的位置。
;
; 门控契约：以**运行期** ${UAC_IsAdmin} 为准，不用编译期
; INSTALL_MODE_PER_ALL_USERS——本仓 nsis.perMachine 未设（=false），该编译期
; 宏永不定义，2026-10-05 首版把它当门控 = 排除写入从未生效（死代码）。
; per-user 非提权安装写不了 HKLM，属文档指引面（docs/guides/deployment.md）。
;
; 幂等性 Add-MpPreference -Force 同路径累加不报错；失败仅留痕不阻断安装
; （与 env 播种同一静默语义，失败为软而非硬）。
;
; 注意：声明必须置于顶层（本文件在模板之前被 include，!include/Var 不能出现在
; Function 上下文内——即宏的展开点）。
!include "getProcessInfo.nsh"
!include "LogicLib.nsh"
Var /GLOBAL pid
Var /GLOBAL IsPowerShellAvailable
Var /GLOBAL AgentOsAdminPassword
; DefenderExecResult 仅安装器侧宏（customInit/customInstall →
; _writeDefenderExclusions）引用；卸载器构建不含这两个宏，若无条件声明即触发
; makensis 6001（builder 按 error 处理，同 AgentOsAdminPassword 惰性引用教训）。
!ifndef BUILD_UNINSTALLER
Var /GLOBAL DefenderExecResult
!endif

; 写 Defender 排除（$INSTDIR + per-user 默认装位 + 用户数据根）。
; 仅 admin 可写（HKLM）；nsExec 结果必须 Pop，防 NSIS 栈失衡。
!macro _writeDefenderExclusions
  ${If} ${UAC_IsAdmin}
    nsExec::ExecToLog `powershell.exe -NoProfile -NonInteractive -Command "try { Add-MpPreference -ExclusionPath '$INSTDIR','$LOCALAPPDATA\Programs\agent-os','$APPDATA\agentos' -Force } catch { Write-Host ('Add-MpPreference failed: ' + $_) }"`
    Pop $DefenderExecResult
    ${If} $DefenderExecResult != 0
      DetailPrint `Defender exclusion write failed (exit $DefenderExecResult) — install continues`
    ${EndIf}
  ${EndIf}
!macroend

!macro customInit
  ; 解包前写排除：copy 阶段即免扫（成熟软件安装分钟级的关键一手）
  !insertmacro _writeDefenderExclusions
!macroend

!macro customCheckAppRunning
  StrCpy $IsPowerShellAvailable 1
  !insertmacro _CHECK_APP_RUNNING

  ; 升级路径反馈：CHECK_APP_RUNNING 挂在 install Section 内、先于
  ; uninstallOldVersion——此处读 INSTALL_REGISTRY_KEY 探测既有装机位，
  ReadRegStr $R6 HKLM "${INSTALL_REGISTRY_KEY}" "InstallLocation"
  ${If} $R6 == ""
    ReadRegStr $R6 HKCU "${INSTALL_REGISTRY_KEY}" "InstallLocation"
  ${EndIf}
  ${If} $R6 != ""
  ${AndIfNot} ${Silent}
    MessageBox MB_ICONINFORMATION|MB_TOPMOST "灵汐助手 即将开始升级$\r$\n$\r$\n将自动清理旧版本并保留您的聊天记录与配置，最长约 1 分钟，请稍候。"
  ${EndIf}
!macroend

; 播种语义：已存在则原样保留（升级不换口令，既有会话不因升级全灭，内核重启
; 自对齐）；CryptGenRandom 失败则不播种（宁缺毋弱——弱口令比未播种更危险，
; 后者走「未播种=部署 bug」的显式回落路径）。
;
; 末尾经 _writeDefenderExclusions 兜底再写一次排除（幂等，见该宏注释）。
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
      WriteRegStr HKCU "Environment" "AGENTOS_ADMIN_PASSWORD" "$AgentOsAdminPassword"
      ; 广播 WM_SETTINGCHANGE（SMTO_ABORTIFHUNG），资源管理器等进程即时感知新变量
      System::Call 'user32::SendMessageTimeout(p 0xFFFF, i 0x001A, p 0, t "Environment", i 2, i 10000, *p .r7)'
    ${EndIf}
  ${EndIf}

  ; 收尾兜底再写一次排除（幂等）：覆盖交互首装经 UI 选 all-users 提权后才
  ; admin 的路径（该路径 customInit 时还不是 admin）；已写过则同路径累加无副作用。
  !insertmacro _writeDefenderExclusions
!macroend

; 卸载清场：移除播种的环境变量（重装会重新播种，内核重置语义自对齐）。
; ReadRegStr 为惰性引用：卸载器脚本不含 customInstall（Var 声明在此上下文
; 无使用点），不引用即触发 makensis 6001 警告；builder 新版按 error 处理。
;
; 与用户数据契约按 ADR 2026-10-03-packaged-user-data-first 的「用户数据为准」
; 共识：customUnInstall 仅清 HKCU 环境变量，不主动触碰 $APPDATA\agentos；
; NSIS 模板 uninstaller.nsh 的 $isDeleteAppData 宏仅在 --delete-app-data
; 显式传入时 RMDir 用户数据——已是默认 add-only 语义，本宏不重复删除。
!macro customUnInstall
  ReadRegStr $AgentOsAdminPassword HKCU "Environment" "AGENTOS_ADMIN_PASSWORD"
  DeleteRegValue HKCU "Environment" "AGENTOS_ADMIN_PASSWORD"
  System::Call 'user32::SendMessageTimeout(p 0xFFFF, i 0x001A, p 0, t "Environment", i 2, i 10000, *p .r7)'
!macroend