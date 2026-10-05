# ADR 2026-10-05-nsis-upgrade-flow-and-defender-exclusion: NSIS 升级流程反馈、Defender 排除与契约收敛

## 背景

2026-10-04 22:55 实测：双击新版 `灵汐助手 Setup 0.1.0.exe` 触发升级流程，**25 分钟无 UI、且未完成**。同期老的两个 `old-uninstaller.exe` 实例在 `nsm9BFD.tmp` 下被先后启停（PID 109892 → 105812），构成重试风暴。`MsMpEng` 进程 CPU 长期 163%、67 线程、WorkingSet 437 MB——Defender 实时扫盘被反复触发。`agentos-kernel.exe`（PID 48480，前一天 dev 跑遗留）持续在系统中持有 `%APPDATA%\agentos\agentos_kernel.db-wal`（4 MB）。

三类问题被实测定位：

1. **零 UI 反馈**：`--updated` 触发的静默卸载器（`/S /KEEP_APP_DATA /allusers --updated`）走 `SilentInstall silent`，没有任何 MessageBox/toast；前 8 分钟用户在 Setup.exe 双击后无任何提示，「被感知」。
2. **Defender 扫盘**：递归删除 win-unpacked 700MB 时每文件经过 Defender 实时扫描（`MsMpEng` 163% CPU、78 万次读累计），而其它装机版（Chrome/Edge/VSCode 等）在安装路径或二进制上有 Defender 排除路径——我们空集。
3. **`%APPDATA%\agentos` 未真正保留**：NSIS 默认卸载器在 `/KEEP_APP_DATA` 之外的 `RMDir /r` 链上仍会触碰用户数据（`agentos_kernel.db` 锁一旦打开，删除被重试，与 ADR `2026-10-03-packaged-user-data-first` 的「用户数据为准」契约不严——前者只在「安装期」播种补缺保护，并未覆盖「升级期」卸载器对用户数据的二次触碰）。

附加三个工程债：

4. **`release/latest.yml` path 与磁盘 exe 名错配**：`latest.yml:4` 写死 `agent-os-setup-0.1.0.exe`（electron-builder 默认），磁盘实为 `灵汐助手 Setup 0.1.0.exe`（`productName` 中文格式）；`build.nsis.artifactName` 未配置——auto-update manifest 找不到匹配文件即报 fallback 错。
5. **`frontend/dist` 双重入库**：`build.files` 与 `build.extraResources[0]` 同时收入同一份 19 MB Vite 产物，重复 19 MB。
6. **三个 gate 未接入 `electron:build`**：`scripts/check_packaged_layout.py`（322 行结构校验）、`scripts/packtest_smoke.py`（CDP UI 烟测）、`scripts/post_install_smoke.py`（装机后内核握手+聊天）现仅手跑，没有 fail-fast 兜底。

## 决策

四面收敛：

1. **`customInstall` 加 MessageBox-toast（升级路径可见性）**：在 Setup 检测到 `--updated` 后、spawn 老卸载器前，弹 `MessageBox MB_ICONINFORMATION|MB_TOPMOST "灵汐助手 $APP_VERSION 即将开始升级，将自动清理旧版本并保留您的聊天记录和配置。最长约 1 分钟，请稍候。"`——silent 模式下用 `MessageBox /SD IDOK` 仍弹（NSIS `/S` 在 MessageBox 行为上仍展示），用户可见。
2. **`customInstall` 末尾自动写 Defender 排除路径**：perMachine 安装路径走 `nsExec::ExecToLog powershell -Command "Add-MpPreference -ExclusionPath '$INSTDIR','$APPDATA\agentos' -Force"`——`Add-MpPreference` 已存在幂等（同路径累加不报错），失败只 warn 不中断（Defender 已是 win 系统的最佳实践，且非阻断路径）。**perUser 安装不写**（写入 HKLM 需 admin，perUser 进程无权；ADR 段明示，docs/guides/deployment.md 加一段手动命令指引）。
3. **`customUnInstall` 严守「不动 user data」**：仅在 `--delete-app-data` 显式传时才 `RMDir /r $APPDATA\agentos`（NSIS 标准 `$isDeleteAppData` 宏已判），其它路径只清 `$INSTDIR`；`AGENTOS_ADMIN_PASSWORD` HKCU 仍按既有合约删除（一次升级会重新播种）。
4. **`package.json` 工程治理三件套**：
   - `build.nsis.artifactName: '${productName}-Setup-${version}.${ext}'` —— 修复 `latest.yml` 路径错配
   - `build.extraResources` 删首项 `frontend/dist`（`build.files` 已含，删后 win-unpacked 减少 ~19 MB）
   - `scripts.electron:build` 链尾追加 `&& python scripts/check_packaged_layout.py release/win-unpacked && python scripts/packtest_smoke.py --exe release/win-unpacked/灵汐助手.exe --cdp-port 9223 --timeout 420`——结构门+升级门 fail-fast

## Alternatives Considered

- **不加 toast 维持现状**：拒绝——用户双击 Setup.exe 后 8 分钟零反馈，且没有进度条；4+ 分钟里可以弹一个 5 秒读完的 MessageBox，体验改善巨大、风险近零。
- **靠运维手工加 Defender 排除**：拒绝——装机版 CI 出包不可控交付对象是终端用户，靠运维加排除不可行；Chrome/Edge/VSCode 都自带了 `add exclusion` 路径是行业基线。
- **直接给 kernel.exe + 灵汐助手.exe 加 EV 代码签名让 Defender 不扫**：拒绝——本次不做（EV 证书+signtool+CI HSM 配置独立成线），ADR 标为后续路标。Defender 排除路径是过渡方案，等代码签名到位即可下调。
- **perUser 也自动加排除**：拒绝——perUser 安装进程跑在 user 上下文，`Add-MpPreference -ExclusionPath HKLM` 写注册表被拒；半自动加 admin 提权破坏 silent install 性质。文档指引 + 装好后弹一次"是否加排除"对话框是更好的折中，本期先做文档指引。
- **`build.extraResources[0]` 改 `remove`：直接解法**：拒绝——`build.files` 与 `build.extraResources` 两者职责不同（前者入 asar，后者入 resources/）；删 extraResource 后 asar 内一份即足，不需要保留 extraResource。已考虑。
- **`customUnInstall` 干脆全部不删 `$APPDATA`**：拒绝——`--delete-app-data` 是用户显式指令（清理路径），NSIS `$isDeleteAppData` 宏已处理；我们要补的是默认不删的契约，不是禁掉内建。
- **三个 gate 都接到 `electron:build` 末尾**：`post_install_smoke.py` 不接——它要求「已装好的应用启动后跑」，需先 install 再跑，不能在 build 链里；改为 `electron:build:post-install-smoke` 独立 script 由 CI 的 `install` 步骤触发；packtest 接到 build 链内（产物即 win-unpacked，直接 CDP 烟测）。已考虑。

## 影响

- **用户体验**：升级时长 25 分钟 → 实测预期 3 分钟内（Defender 排除后 NSIS 删盘从分钟级降到秒级；toast 给到第一道反馈）；perMachine 自动 Write、perUser 文档指引。
- **契约**：NSIS 卸载器对 `%APPDATA%\agentos` 的触碰面收到 `--delete-app-data` 显式分支，与 ADR `2026-10-03-packaged-user-data-first` 的「用户数据为准」共识对齐到卸载路径。
- **构建链**：`electron:build` 末尾新增两个 fail-fast gate（`check_packaged_layout`、`packtest_smoke`）；产物减少 ~19 MB。
- **Auto-update 路径**：`latest.yml` 与磁盘 exe 名一致——auto-update manifest 找得到文件。auto-update 协议层不在本期范围。

## 风险与缓解

- **perUser 用户未自动加排除**：docs/guides/deployment.md 增加手动命令段落（`Add-MpPreference -ExclusionPath "$env:LOCALAPPDATA\agent-os"`）；桌面应用首次启动后右下角气泡 / 通知中心弹一次「可选优化」按钮，点击后调 admin 提权写排除——本期先做文档。
- **`MessageBox` 在 `/S` 模式被吞**：实测 NSIS 在 `SilentInstall silent` 下 MessageBox 仍展示（与安装向导不同，silent installer 的 MessageBox 由 nsExec 显式唤起，路径不静音）；若用户用 `--silent` 自定义标志导致 toast 失效，本期为可控退化。
- **`packtest_smoke.py` 加 build gate 后慢**：CDP 启动+4 断言约 30-40 秒，build 链总时长 +30-40 秒可接受；提供 `--skip-packtest` 兜底（但默认开启，违反 fail-fast 一致性故不推荐常态用）。
- **`nsExec::Exec powershell Add-MpPreference` 失败静默**：失败只写 `DetailPrint`，不中断安装；产品体验等价于「Defender 还是慢（但应用照常装上）」，符合「不阻断、只损失写侧」的契约。

## 不覆盖保证（用户追加 2026-10-05）

- **user_root `AGENTOS_ADMIN_PASSWORD` 升级后保留**：`customUnInstall` 不删 HKCU `Environment\AGENTOS_ADMIN_PASSWORD` 的写入契约——若本期会触发删除，则会与 ADR `2026-10-02-packaged-auto-login-env-only` 的「升级不换口令」契约冲突。决议：**仍按旧合约删除**，因为 `customInstall` 检测到口令已存在时**原样保留**（`ReadRegStr $AgentOsAdminPassword HKCU ... ${If} $AgentOsAdminPassword == ""` 守卫），卸载器虽然删，写入时不覆盖——净效应等价「升级不换口令」。本 ADR 不改动该语义。
- **`seedTreeMissingOnly` add-only**：与本期正交，不动；引用既有 ADR `2026-10-03-packaged-user-data-first`。
- **perUser 装机版的排除路径**：不自动加，文档指引；该承诺未达成（已知边界）。

## 落地修正（2026-10-06 实机装机测试）

首版实现经实机升级测试（旧版 Oct-2 → 新版 `/S /allusers`）暴露两处缺陷，
当日修正：

1. **排除写入是死代码**：首版把 Defender 排除写在 `customInstall` 内并以
   **编译期宏** `!ifdef INSTALL_MODE_PER_ALL_USERS` 门控；本仓构建
   `nsis.perMachine` 未设（build 日志实锤 `perMachine=false`），该编译期宏
   永不定义 → 排除写入从未编译进安装器。实测装机后 `ExclusionPath` 为空。
   修正：改**运行期** `${UAC_IsAdmin}` 门控（交互选 all-users 经 UAC 提权后、
   静默 per-machine 升级的 UAC 内层实例均满足；per-user 非提权路径本就无权
   写 HKLM，走文档指引不变）。
2. **排除写入时机太晚**：`customInstall` 挂在 `installApplicationFiles`
   （解包 copy）之后——即使生效，copy 阶段已被 Defender 逐文件扫描完毕
   （实测 Setup 9 分钟 CPU 仅 3.2s = 纯阻塞等扫描返回；old-uninstaller 删盘
   烧 151s CPU 同因）。修正：新增 `customInit` 钩点（.onInit 内
   initMultiUser 之后、先于卸载旧版与 copy）前置写排除，`customInstall`
   降级为幂等兜底（覆盖交互首装提权后才 admin 的路径）。nsExec 结果补
   Pop（首版漏 Pop 属 NSIS 栈失衡隐患）。

实机四契约复测口径：kernel hash 更新 / user data 保留 / `ExclusionPath`
写入 / `AGENTOS_ADMIN_PASSWORD` 播种，全过即验收。

## 归档

- 实测事故：2026-10-04 22:55 升级流程卡 25 分钟零 UI（PID 77616 / 109892 / 105812 三进程链）
- 实现：本期 commit（待落地）
- 已知后续路标：代码签名（EV 证书）、auto-update 协议层、perUser 装机版自动加排除、macOS DMG / Linux AppImage 等价 NSIS 处理