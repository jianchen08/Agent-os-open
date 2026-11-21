; 装机安装器自定义宏集（electron-builder build.nsis.include 唯一入口）。
; 内容移植自 sync/open-main-20261008（62646ab4c 播种 + 253590fe0 makensis 6001 惰性引用修），时点=253590fe0（不含 Defender/升级路径特性线）。
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
; 检测语义由「INSTDIR 路径前缀」收窄为「镜像名精确匹配（per-user 安装追加
; USERNAME 过滤）」：Electron 全部进程共用同一 exe 名，行为等价（2026-11-12
; 起恢复 per-user 免管理员安装，manifest RequestExecutionLevel user——用户
; 裁定「安装应用不得使用管理员账户」；卸装语义见文件尾注）。
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
!include "nsDialogs.nsh"
Var /GLOBAL pid
Var /GLOBAL IsPowerShellAvailable
; AgentOsAdminPassword 仅安装上下文使用（customInstall 播种；2026-11-08 起卸载
; 侧零引用——凭据保留契约见文件尾注）。卸载器编译（BUILD_UNINSTALLER）不声明：
; makensis 6001「声明无使用点」被 builder 按 error 处理，此坑原由卸载宏的惰性
; ReadRegStr 顶住，卸载宏删除后以条件声明收口。
!ifndef BUILD_UNINSTALLER
Var /GLOBAL AgentOsAdminPassword
!endif

; ── R10（2026-10-20）：安装段前孤儿进程预清障 + 进度可见 ──
;
; 事故（2026-10-20 装机 0.2.2 实测）：旧版残留孤儿进程（hindsight venv 的
; python / rclone，父链已断、镜像在旧安装目录 resources 子树下）锁住包内
; venv 的 python.exe，模板 extractUsing7za 的 CopyFiles 段逐文件失败并整树
; 重试（5 轮 × 全量重拷，分钟级），叠加安装段开头 SetDetailsPrint none 压掉
; 全部细节输出 → 安装器假死 20+ 分钟零提示。清障哲学与 ADR
; 2026-10-19-memory-governor-and-process-pooling §4 D4（spawn 前端口清障）
; 同源：锁因在写目录之前拆除。
;
; 预清障（customInit；.onInit 时 $INSTDIR 已被 initMultiUser 置为上一版安装
; 目录，覆盖安装=主场景）：Toolhelp32 快照枚举全部进程（getProcessInfo.nsh
; 同款纯 System::Call 路线，零新插件依赖；不经 WMI/PowerShell——BUG-12 既定
; 纪律），名字命中 python* 前缀或 rclone.exe 的候选逐个取镜像全路径，按
; 「$INSTDIR\resources\ 前缀」过滤——只碰安装目录 resources 子树，系统
; python 永不触碰；取不到路径或前缀不匹配一律跳过（宁不杀，不碰身份不明
; 进程）。命中即 taskkill /F /T 并逐个 DetailPrint。
; 镜像路径用 QueryFullProcessImageNameW（kernel32，跨进程位数可靠）而非
; getProcessInfo.nsh 的 psapi::GetModuleFileNameExW——后者从 32 位安装器读
; 64 位进程必失败（空路径→整个清障静默失效），恰是本事故进程形态。
; 已知边界：allowToChangeInstallationDirectory 下用户改装到其他目录时，本
; 清障扫的是上一版目录（.onInit 早于目录页）；该场景由 CopyFiles 失败重试
; 与重试对话框兜底（步骤 2/3 文案已提示占用可能）。
;
; 进度可见：customInit 里 SetDetailsView show 常开详情视图；customCheckAppRunning
; 里恢复 SetDetailsPrint both——模板的 none 从安装段开头一直压到结束，连模板
; 自身的失败 DetailPrint（CopyFiles 失败「Can't modify ...」）也一并被压，
; 本次一并解锁；随后以中文步骤名打点 1/3、2/3，customInstall 打点 3/3。
!ifndef BUILD_UNINSTALLER
Var /GLOBAL StaleProcName
Var /GLOBAL StaleProcImage
Var /GLOBAL StaleProcPrefix
Var /GLOBAL StaleProcPrefixLen
Var /GLOBAL StaleProcCount
; WSL 环境准备页（MXC linux-bash 隔离后端前置）：checkbox 句柄 + 用户选择。
Var /GLOBAL AgentOsWslCheckbox
Var /GLOBAL AgentOsWslChecked
!endif

!macro customInit
  SetDetailsView show
  ; 免管理员安装（用户裁定 2026-11-12）：initMultiUser 在检测到既有
  ; per-machine 安装（旧包 HKLM 注册项）时默认 setInstallModePerAllUsers
  ; ——普通用户上下文将尝试写 Program Files（失败）或触发 UAC（模板
  ; install 段对 hasPerMachineInstallation=1 的静默安装会主动 RunElevated）。
  ; 故在既有安装检测之后清零该标记并强制 per-user 缺省：/D 静默目录与
  ; HKCU 既有安装路径照常生效；安装模式页由下方 customInstallmode 直接
  ; 跳过（不再提供 all-users 选项）。
  !ifndef INSTALL_MODE_PER_ALL_USERS
  !ifndef ONE_CLICK
    StrCpy $hasPerMachineInstallation "0"
    StrCpy $hasPerUserInstallation "1"
    ; 重设安装模式/目录：initMultiUser 可能已按旧 HKLM 记录置为 Program Files
    !insertmacro setInstallModePerUser
  !endif
  !endif
  ; WSL 环境准备页缺省（2026-11-17 用户裁定「安装期可选提权」）：
  ; 交互安装默认勾选（推荐）；静默安装不弹 UAC，需显式 AGENTOS_INSTALL_WSL=1
  ; 才启用——维护链无人值守不会被 UAC 卡死。
  StrCpy $AgentOsWslChecked "0"
  ${IfNot} ${Silent}
    StrCpy $AgentOsWslChecked "1"
  ${EndIf}
  ReadEnvStr $R9 "AGENTOS_INSTALL_WSL"
  ${If} $R9 == "1"
    StrCpy $AgentOsWslChecked "1"
  ${EndIf}
  Call AgentOsSweepStaleResourceProcesses
!macroend

; 安装模式页强制 current-user（模板 multiUserUi.nsh 官方钩子
; customInstallmode）：设 isForceCurrentInstall=1 后页面 PRE 直接
; setInstallModePerUser + Abort，交互安装也不再出现「所有用户」单选项与
; UAC 盾牌（用户裁定：安装不得使用管理员账户）。
!ifndef INSTALL_MODE_PER_ALL_USERS
!macro customInstallmode
  StrCpy $isForceCurrentInstall "1"
!macroend
!endif

; ── WSL 环境准备页（2026-11-17 用户裁定：安装期可选提权）──
;
; MXC linux-bash 隔离后端依赖 WSL ≥3.0.5（wslc）。安装器保持 per-user 零提权，
; 但提供这一页让用户显式选择是否在安装期一并装/升级 WSL；勾选后由
; resources/prereqs/installer_env_bootstrap.py 自行发起单次 UAC（不是安装器
; 整体提权，2026-11-12 裁定不变）。静默安装不显示本页，默认不勾选。
;
; 页序：electron-builder assistedInstaller.nsh 在目录页之后插入本宏
; （!ifmacrodef customPageAfterChangeDir 官方钩子）。
!ifndef BUILD_UNINSTALLER
; 页声明与页面体分离（2026-11-18 出包轮修复）：electron-builder 26.15.3 的
; assistedInstaller.nsh 在**顶层**（页序列处，非 Section/Function 内）展开
; customPageAfterChangeDir —— 该展开点只能放 Page 声明：nsDialogs/MUI 宏在
; 此处会报「SendMessage not valid outside Section or Function」（MUI_HEADER_TEXT）
; 或「Can't add entry, no section or function is open!」（NSD_CreateLabel），
; 两者都使安装器构建整体失败（win-unpacked 已出、NSIS 阶段红）。故本页改为：
; 顶层 `Page custom` 声明 + 页面创建体放 Function（对话框与标题宏在其中合法）。
!macro customPageAfterChangeDir
  Page custom AgentOsWslPreparePage
!macroend

Function AgentOsWslPreparePage
  ; 标题不用模板 MUI_HEADER_TEXT：该宏在本 include 的解析位尚未定义（electron-builder
  ; 自身只在页函数内用，且定义晚于本 include 解析）→ 插入报「macro named
  ; MUI_HEADER_TEXT not found」。改由本页标签自带标题+副标题（函数内 NSD 合法）。
  nsDialogs::Create 1018
  Pop $0
  ${If} $0 == error
    Abort
  ${EndIf}
  ${NSD_CreateLabel} 0 0 100% 14u "环境准备（可选）——隔离执行环境依赖 WSL，安装或升级需一次管理员授权"
  Pop $0
  ${NSD_CreateLabel} 0 18u 100% 30u "灵汐的隔离执行环境（MXC linux-bash）依赖 WSL 3.0.5 或更高版本。$\r$\n勾选后安装程序会自动检测：已满足则跳过；缺失或过旧则安装/升级（可能需要重启 Windows）。"
  Pop $0
  ${NSD_CreateCheckbox} 0 52u 100% 12u "安装 / 升级 WSL（推荐，需要管理员权限）"
  Pop $AgentOsWslCheckbox
  ${If} $AgentOsWslChecked == "1"
    ${NSD_SetState} $AgentOsWslCheckbox ${BST_CHECKED}
  ${Else}
    ${NSD_SetState} $AgentOsWslCheckbox ${BST_UNCHECKED}
  ${EndIf}
  ${NSD_OnClick} $AgentOsWslCheckbox AgentOsWslCheckboxClicked
  nsDialogs::Show
FunctionEnd

Function AgentOsWslCheckboxClicked
  ${NSD_GetState} $AgentOsWslCheckbox $0
  StrCpy $AgentOsWslChecked $0
FunctionEnd
!endif

!ifndef BUILD_UNINSTALLER
; 安装目录 resources 子树残留进程清障（口径见上方 R10 注释块）。
Function AgentOsSweepStaleResourceProcesses
  Push $R0
  Push $R1
  Push $R2
  Push $R3
  Push $R5
  Push $R6
  Push $R7
  Push $R8
  Push $R9

  StrCpy $StaleProcCount 0
  StrCpy $StaleProcPrefix "$INSTDIR\resources\"
  StrLen $StaleProcPrefixLen $StaleProcPrefix
  DetailPrint "预清障：扫描安装目录内旧版残留进程（python/rclone）..."

  System::Call 'Kernel32::CreateToolhelp32Snapshot(i 2, i 0) i.R0'
  IntCmp $R0 -1 sweep_done
  System::Alloc 1024
  Pop $R9
  !if ${NSIS_PTR_SIZE} == 8
    ; 64 位 NSIS 编译：th32DefaultHeapID 为指针宽（8B），结构随之加长
    System::Call "*$R9(i 560)"
  !else
    ; 32 位布局（getProcessInfo.nsh 同款：9 个 4 字节标量 + 256 宽字符名）
    System::Call "*$R9(i 556)"
  !endif
  System::Call 'Kernel32::Process32FirstW(i R0, i $R9) i.R1'

sweep_loop:
  IntCmp $R1 0 sweep_cleanup
  !if ${NSIS_PTR_SIZE} == 8
    System::Call "*$R9(i,i,i.R2,p,i,i,i,i,i,&w256.R3)"
  !else
    System::Call "*$R9(i,i,i.R2,i,i,i,i,i,i,&w256.R3)"
  !endif
  StrCpy $StaleProcName $R3
  ; 名字预筛（StrCmp 不区分大小写）：python 前缀覆盖 python/pythonw/python3.x
  StrCpy $R5 $StaleProcName 6
  StrCmp $R5 "python" sweep_take
  StrCmp $StaleProcName "rclone.exe" sweep_take
  Goto sweep_next

sweep_take:
  ; 取镜像全路径：PROCESS_QUERY_LIMITED_INFORMATION，受拒面最小的查询权
  System::Call 'Kernel32::OpenProcess(i 0x1000, i 0, i R2) i.R5'
  IntCmp $R5 0 sweep_next
  ; 尺寸与 System 插件 t 输出的自动缓冲量(NSIS_MAX_STRLEN)对齐：不溢出不截断
  StrCpy $R6 ${NSIS_MAX_STRLEN}
  System::Call 'Kernel32::QueryFullProcessImageNameW(i R5, i 0, t .R8, *i .R6) i.R7'
  System::Call 'Kernel32::CloseHandle(i R5)'
  IntCmp $R7 0 sweep_next
  StrCpy $StaleProcImage $R8
  ; 路径门：只认「$INSTDIR\resources\」前缀（StrCmp 不区分大小写；8.3 短路径
  ; 等异形不匹配即跳过，绝不误伤系统 python）
  StrCpy $R8 $StaleProcImage $StaleProcPrefixLen
  StrCmp $R8 $StaleProcPrefix sweep_kill
  Goto sweep_next

sweep_kill:
  DetailPrint "清理旧版残留进程: $StaleProcName (PID $R2)..."
  nsExec::Exec '"$SYSDIR\taskkill.exe" /F /T /PID $R2'
  Pop $R5
  IntCmp $R5 0 sweep_kill_ok
  IntCmp $R5 128 sweep_next
  DetailPrint "清理旧版残留进程: $StaleProcName (PID $R2) taskkill 未成功(exit=$R5)，继续安装"
  Goto sweep_next
sweep_kill_ok:
  IntOp $StaleProcCount $StaleProcCount + 1

sweep_next:
  System::Call 'Kernel32::Process32NextW(i R0, i $R9) i.R1'
  Goto sweep_loop

sweep_cleanup:
  System::Free $R9
  System::Call 'Kernel32::CloseToolhelp32Snapshot(i R0)'

sweep_done:
  IntCmp $StaleProcCount 0 0 sweep_reported sweep_reported
  DetailPrint "预清障完成：安装目录内未发现残留进程"
  Goto sweep_fin
sweep_reported:
  DetailPrint "预清障完成：已清理 $StaleProcCount 个旧版残留进程"
sweep_fin:
  Pop $R9
  Pop $R8
  Pop $R7
  Pop $R6
  Pop $R5
  Pop $R3
  Pop $R2
  Pop $R1
  Pop $R0
FunctionEnd
!endif

; ── WinFsp 预带安装（多端挂载依赖，ADR 2026-10-18-unified-namespace-fs-mount-and-mcp-transfer）──
;
; 装机契约：WinFsp = rclone mount 的硬依赖（内核级文件系统驱动），MSI 随包
; 预带（resources/prereqs/，文件名单源钉在 scripts/check_prereqs.mjs，链首
; 断言与本文件引用逐字一致）。它是可选「多端挂载」能力的依赖，主程序安装
; 不依赖它。
;
; 提权语义（用户裁定 2026-11-12「安装应用不得使用管理员账户」；ADR
; 2026-11-12-installer-per-user-no-admin）：
;   - nsis.perMachine=false + allowElevation=false → 模板 RequestExecutionLevel
;     user，安装器全程普通用户权限，默认装到 %LOCALAPPDATA%\Programs\<产品名>；
;     双击与 /S 无人值守都零 UAC；
;   - WinFsp 是系统级驱动，msiexec 安装必须提权（1018 pack14 实证：非提权
;     上下文静默装失败 exit 103）。故 customInstall 先探当前进程是否提权：
;     已提权（右键「以管理员身份运行」/维护轮显式提权）才静默装 MSI；普通
;     用户安装跳过并打印手动安装路径，不弹 UAC、不阻断主程序安装；
;   - 需要开箱即用多端挂载的环境：右键安装包「以管理员身份运行」，或装后
;     按 deploy/multi_device/README.md §1.1 路径②手动装（需提权）。
; 知情同意口径：预带的知情同意 = 用户安装灵汐助手这个动作本身；与 multi_device
; README §1.1 的"永不自动装"纪律并存不冲突——后者指 agent 代装/后台场景需
; 显式审批。
;
; 失败口径（不阻断）：msiexec 退出码仅记日志，任何失败都不阻断主程序安装；
;   0=成功；3010=成功待重启；1638=本机已有同版/更新版（重复安装/维护轮重装
;   常态，视为跳过成功）；其余=失败（仅 DetailPrint）。
; WinFsp 2.1 布局（1018 装机实证）：dll/sys 在 SxS\sxs.<ts>\bin\，服务名
;   WinFsp.Launcher（非 winfsp），检测口径见 desktop_setup.sh check_winfsp。
; rclone.exe 为包内载荷（resources/bin/），不进系统、不需提权、随卸载随 app 移除。

; 卸载口径（记录于 README §1.1）：WinFsp 是系统级驱动，卸 app 不卸驱动
; ——customUnInstall 仅清环境变量播种，不动 WinFsp（同版重复安装由 MSI 1638
; 语义自然幂等）；rclone.exe 与其余 resources 随 $INSTDIR 删除自动清场。

!macro customCheckAppRunning
  ; R10：模板在安装段开头 SetDetailsPrint none 压掉全部细节输出，此处已在
  ; none 生效之后——恢复 both，模板自身与本文件的后续 DetailPrint 才可见
  SetDetailsPrint both
  DetailPrint "步骤 1/3：检查并关闭正在运行的旧版应用..."
  StrCpy $IsPowerShellAvailable 1
  !insertmacro _CHECK_APP_RUNNING
  ; 2/3 覆盖「移除旧版 + 解压写入新资源」两段（其间无挂钩点），写入段最慢
  DetailPrint "步骤 2/3：移除旧版本并解压写入新版资源（最耗时，请耐心等待）..."
  DetailPrint "若本步骤长时间停留：可能是残留进程仍占用安装目录，请检查并结束相关进程"
!macroend

; 播种语义：已存在则原样保留（升级不换口令，既有会话不因升级全灭，内核重启
; 自对齐）；CryptGenRandom 失败则不播种（宁缺毋弱——弱口令比未播种更危险，
; 后者走「未播种=部署 bug」的显式回落路径）。
!macro customInstall
  DetailPrint "步骤 3/3：收尾（WinFsp 依赖、WSL 环境准备与管理口令播种）..."
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
  ; WinFsp 预带静默安装（口径见上方预带注释块；msi 路径=包内 resources/prereqs/）。
  ; 免管理员安装（用户裁定 2026-11-12）：普通用户上下文不弹 UAC、跳过驱动
  ; 安装并提示手动路径；仅当安装器本身已提权（右键管理员运行）才静默装。
  DetailPrint "检查多端挂载依赖 WinFsp（系统级驱动，需管理员权限）..."
  System::Call 'shell32::IsUserAnAdmin() i .R8'
  ${If} $R8 == 0
    DetailPrint "当前为普通用户安装：已跳过 WinFsp；需要多端挂载时请右键以管理员身份运行本安装包，或手动安装 $INSTDIR\resources\prereqs\winfsp-2.1.25156.msi"
  ${Else}
    DetailPrint "安装多端挂载依赖 WinFsp..."
    ExecWait '"$SYSDIR\msiexec.exe" /i "$INSTDIR\resources\prereqs\winfsp-2.1.25156.msi" /qn /norestart' $R0
    ${If} $R0 == 0
      DetailPrint "WinFsp 安装完成"
    ${ElseIf} $R0 == 3010
      DetailPrint "WinFsp 安装完成（重启后生效）"
    ${ElseIf} $R0 == 1638
      DetailPrint "WinFsp 已有安装（msiexec 1638），跳过"
    ${Else}
      DetailPrint "WinFsp 静默安装未成功（msiexec exit=$R0），不阻断主程序安装"
    ${EndIf}
  ${EndIf}
  ; ── WSL 环境准备（MXC linux-bash 隔离后端前置）──
  ; 勾选则调用随包 Python 引导脚本；脚本自行判断版本并在需要时发起一次 UAC
  ; （wsl --install --no-distribution / wsl --update --web-download）。
  ; 不随包分发 WSL 负载（用户裁定：Windows 上一条命令即可在线安装）。
  ${If} $AgentOsWslChecked == "1"
    DetailPrint "环境准备：检测 WSL（MXC linux-bash 隔离依赖）..."
    StrCpy $R2 "$INSTDIR\resources\prereqs\python\python.exe"
    StrCpy $R3 "$INSTDIR\resources\prereqs\installer_env_bootstrap.py"
    ${If} ${FileExists} "$R2"
    ${AndIf} ${FileExists} "$R3"
      nsExec::ExecToStack '"$R2" "$R3" --install-wsl --json'
      Pop $R4
      Pop $R5
      ${If} $R4 == 0
        DetailPrint "WSL 环境准备完成（已满足或安装/升级成功）"
      ${ElseIf} $R4 == 3010
        DetailPrint "WSL 已安装/升级，需重启 Windows 后生效"
        MessageBox MB_ICONINFORMATION|MB_OK "WSL 已安装/升级，需要重启 Windows 后生效。$\r$\n重启后隔离执行环境即可使用。"
      ${ElseIf} $R4 == 4
        DetailPrint "WSL 环境准备已取消（用户未授权管理员权限）"
      ${Else}
        DetailPrint "WSL 环境准备未完成（exit=$R4）：$R5"
        MessageBox MB_ICONEXCLAMATION|MB_OK "WSL 环境准备未完成（exit=$R4）。$\r$\n可稍后以管理员身份运行：$R3 --install-wsl$\r$\n或手动执行：wsl --install --no-distribution，然后 wsl --update"
      ${EndIf}
    ${Else}
      DetailPrint "WSL 引导脚本缺失（包体不完整），跳过环境准备"
    ${EndIf}
  ${EndIf}
!macroend

; ── 卸载零清除（2026-11-08，ADR 2026-11-08-installer-credential-retention）──
;
; 本宏已删除（模板 uninstaller.nsh !ifmacrodef 条件挂载，缺席=合法）。
;
; 旧实现卸载即删 AGENTOS_ADMIN_PASSWORD 播种——NSIS 升级流程=旧卸载器先跑，
; 等效于每次升级都轮换口令：内核重启按 env 校验存量哈希不匹配 → 重置口令并
; 令旧会话全部失效（boot_env seed_admin_user + token password_binding 校验）
; → 已发令牌全灭 → 用户每次升级都被打回登录框（2026-10-22 / 2026-11-06 /
; 2026-11-08 三轮用户实证）。customInstall 的「已存在则原样保留（升级不换
; 口令）」守卫被本删除自拆，契约从未真正生效。
;
; 现契约（用户裁定 2026-11-08：安装/更新不得清理凭据与用户数据）：卸载不清
; 播种、不清 %APPDATA%（模板默认即不清）；真卸载残留 = HKCU 一枚 32 hex 随机
; 串 env + 用户数据目录（与内核 DB 同为机器级事实源，重装复用=跨卸载重装也
; 会话不断）。机械执法 = CI 闸 installer-userdata-guard
; （scripts/check_installer_userdata_guard.py，删除面回潮即红）。
; WinFsp 不在此卸载（系统级驱动，卸 app 不卸驱动，口径见上方预带注释块与
; multi_device README §1.1）；rclone.exe 等包内载荷随 $INSTDIR 删除自动清场。
