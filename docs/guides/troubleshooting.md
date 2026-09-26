# 排障 FAQ / Troubleshooting 知识库

> 返回 [开发指南索引](README.md)。
> 定位：高频问题沉淀，三段式（**症状 / 根因 / 解法**），每条标注来源，不臆造。
> 维护口径：引用旧诊断时按 HEAD 复核——已修复的按「已修复（commit xxx）」标注；无法复核到依据的按 `[未验证]` 标注。
> 版本：R7 任务（2026-09-13）升级——保留既有 13 条（改三段式 + 来源标注），新增 10 条，共 23 条。

---

## A 组：改了不生效（生效链路）

### A1. 改了 Rust 内核代码，行为/接口不生效

- **症状**：改了 `kernel/` 下 Rust 代码（或内核二进制行为），重启内核进程后仍是旧行为；本地 `cargo test` 却是新代码。
- **根因**：内核自身代码**无热加载**——运行中的是旧二进制。native 插件（cdylib）有 G8 自动重启（同 id 换产物保守重启），但内核本体代码必须重新编译。
- **解法**：`cd kernel && cargo build --release --bin agentos-kernel`，用新产物重启内核；native 插件产物变更后把新 cdylib 复制到插件目录（文件名与 `entry` 一致），走 G8 自动重启。
- **来源**：[来源: docs/guides/plugin-native-rust.md:7（无热加载/G8）；docs/guides/deployment.md:43；AGENTS.md:57-60]

### A2. 改了插件 Python 代码"不生效"

- **症状**：编辑 sidecar 插件 `.py` 后调用工具，行为还是旧的；有时过一会儿又"自己好了"。
- **根因**：sidecar 代码走 **pull 模型**——invoker 在**下次调用**时检测目录 mtime 指纹变化才 kill + respawn；若编辑后**零调用**（空窗期），机制无事可做，感知为"不生效"。B15 诊断实证：conversation_mode 的编辑落在调用空窗期，重启后才"生效"。
- **解法**：编辑后**调用一次**触发 respawn；确认没在 stdout print（破坏 JSON-RPC，日志走 stderr）；确认日志出现 `Plugin code/config changed, reloading sidecar`。
- **来源**：[来源: docs/working/B15_watcher热重载失灵根因_20260906.md（根因实证）；docs/working/hot_reload_e2e_report.md:66（pull 模型生效）]

### A3. 改了 plugin.json 不生效

- **症状**：改 manifest（工具声明/能力/配置）后行为不变。
- **根因**：watcher 推送路径只认**目录创建**与 **plugin.json 增/改**（`plugin_watcher.rs:1331-1339` 附近），其余源码变更不触发；兜底是 60s 轮询（`plugin_watcher.rs:58`）。声明与实现不一致会被 G2 漂移**拒注册**（日志 warn）；`enabled_plugin_ids` 是启动期快照，被显式禁用的插件改 manifest 也不会启用。
- **解法**：查内核日志有无 G2 漂移 warn；确认插件未被 `config/kernel/default_profile.yaml` 禁用；必要时 reenable 重注册；前端 schema 变化需**刷新页面**。
- **来源**：[来源: docs/working/B15_watcher热重载失灵根因_20260906.md（watcher 机制）；AGENTS.md:57-60；.project/widget_contracts.md §九（reenable/刷新口径）]
- **注**：旧报告 `docs/working/hot_reload_e2e_report.md:59-60` 的「改插件代码/配置后必须重启 kernel 才生效」为 0.2 早期状态（当时 reload 端点未实现）；HEAD 现状以 AGENTS.md 口径为准（watcher 自动处理）。另：`server.rs:148` 注释显示 `history/reload*` 死端点已删除。

### A4. 前端插件表单/页面没更新

- **症状**：插件声明（`ui_schema` 等）改了，前端界面还是旧的。
- **根因**：前端 schema 为拉取 + 缓存，manifest 变更后需刷新页面重新拉取。
- **解法**：刷新前端页面（硬刷新）。
- **来源**：[来源: AGENTS.md:60「前端 schema 需刷新页面才更新」；既有条目（commit 930e56507 迁移）]

### A5. 管道配置改完不生效

- **症状**：改 `config/pipelines/autonomous.yaml` 或 `config/steps/*.yaml` 后执行行为不变。
- **根因**：管道配置是**执行前 Pull 热加载**（mtime 指纹 + 1s TTL）；但坏 YAML / 命名冲突 / 编译错误会**静默保留旧配置继续跑**（内核日志有 warn）。
- **解法**：确认文件已保存；查内核日志 warn；校验 YAML 与命名。
- **来源**：[来源: docs/guides/execution-semantics.md §6；既有条目（commit 930e56507 迁移）]

### A6. 工具注册了但 LLM 看不到 / 不调用

- **症状**：插件工具已注册，但 LLM 工具面里没有；或工具在但从不被调用；或 agent 换了工具白名单不生效。
- **根因**：三层过滤链（插件启用 → manifest 工具声明 → agent `tool_ids` 白名单）；或 name/description/schema 质量导致 LLM 不选。agent yaml 热生效**只对新任务**。
- **解法**：逐层查——`config/kernel/default_profile.yaml` enabled？manifest 带齐 `input_schema`/`output_schema`？工具名在目标 agent 的 `tool_ids`？name 与注册名完全一致（含大小写）、description 写清用途、schema 尽量准；确认工具本身已启用（对新任务生效）。
- **来源**：[来源: AGENTS.md:54-56（工具面过滤）；既有条目（commit 930e56507 迁移）]

### A7. 流式事件被网关拒绝

- **症状**：插件发流式事件被拒。
- **根因**：manifest 未声明 `capabilities.streaming`（fail-closed）。
- **解法**：按 `docs/guides/streaming-protocol.md` 补声明（watcher 自动重注册生效）。
- **来源**：[来源: docs/guides/streaming-protocol.md；既有条目（commit 930e56507 迁移）]

### A8. 工具结果前端渲染不对

- **症状**：工具返回后前端卡片渲染错误/空白。
- **根因**：`output_schema` / `render` 声明缺失或与实际返回不符（契约 fail-closed）。
- **解法**：按实际返回结构补齐声明。
- **来源**：[来源: .project/widget_contracts.md §五（output_schema 消费契约）；既有条目（commit 930e56507 迁移）]

### A9. service 方法别的插件调不到

- **症状**：插件声明了 `services`，其他插件调用不到。
- **根因**：`services` 不进 LLM 面；调用方必须声明 `requires_services`（角色名），boot 期闸不满足内核拒启。
- **解法**：调用方 manifest 补 `requires_services`。
- **来源**：[来源: docs/decisions/2026-08-18-plugin-dependency-package.md（requires_services 语义）；既有条目（commit 930e56507 迁移）]

### A10. config_files 配置注入没生效

- **症状**：插件声明了 `config_files` 但读不到配置。
- **根因**：`path` 须相对 `config/` 根且落在 `config/` 子树内；未声明（或空）= 收空配置。
- **解法**：需要哪个文件就显式映射哪条；检查 path 前缀（B 类配置已归位 `config/plugins/<id>/`）。
- **来源**：[来源: 既有条目（commit 930e56507 迁移）；相关背景 docs/decisions/2026-09-14-config-ownership-plugin-lifecycle.md]

---

## B 组：环境与路径（双环境坑）

### B1. Git Bash / WSL 与 Windows Python 的 /tmp 不一致

- **症状**：脚本在 CI（Ubuntu）绿、本地 Windows 挂；报文件找不到（`/tmp/xxx.json`）；bash 工具里 `pwd` 是 `/mnt/d/...` 而其他工具报 `D:\...`。
- **根因**：双环境——bash 走 WSL（`/mnt/d/...`，其 `/tmp` 是 WSL 内路径），Windows 侧 Python/工具走 `D:\...`（其 temp 是 `%TEMP%`）。硬编码 `/tmp` 在 Windows 侧不存在。实证：`config_injection_test` 原 7 处 `/tmp/xxx.json` 在本地 Windows 全挂（CI 不受影响），修复为 `std::env::temp_dir()`。
- **解法**：跨平台一律用 `std::env::temp_dir()`（Rust）/ `tempfile.gettempdir()`（Python），不硬编码 `/tmp`；跨环境传文件用仓库内相对路径或显式绝对路径。
- **来源**：[来源: kernel/crates/integration-tests/tests/config_injection_test.rs:164-171；docs/working/test_traceability.md:181（组5 修复记录）；本任务实测（bash 与文件工具双路径口径）]

### B2. WSL 下跑前端工具链报 native binding 错

- **症状**：WSL 里跑 `pnpm exec knip` / vite 报 `oxc-parser` native binding 错。
- **根因**：`node_modules` 是 Windows 侧安装的，WSL 侧 native binding 不匹配（与 vite 同口径）。
- **解法**：经 `cmd.exe /c` 转发到 Windows 侧跑，或直接在 Windows 侧跑（工具链口径：Windows 侧 pnpm 11.7.0）。
- **来源**：[来源: report.md（R6v2 段「工具链口径」与「未验证」）；docs/working/knip_jscpd_基线_20260913.md]

### B3. sidecar 起不来：PYPROJECT_MISSING / VENV_INTERPRETER_MISSING

- **症状**：插件加载报缺 `pyproject.toml` 或 `.venv`。
- **根因**：插件目录缺 `pyproject.toml` 或 `.venv`；内核不回退 PATH 裸 python（fail-closed）。
- **解法**：`uv sync --project <插件目录>` 重建。
- **来源**：[来源: 既有条目（commit 930e56507 迁移）；docs/working/插件venv去重方案_20260907.md、docs/decisions/2026-09-07-plugin-venv-dedup.md]

### B4. native 插件报产物缺失

- **症状**：native 插件加载期报产物缺失 / 契约闸门拒载。
- **根因**：`host_type: in_process` 且 `native.artifact` 声明的 cdylib 不在插件目录（产物缺失在**加载期**即报错，不会等到运行）。
- **解法**：插件目录内 `cargo build --release`，把产物从 `target/release/` 复制到插件目录根（文件名与 `entry` 一致）。
- **来源**：[来源: docs/guides/plugin-native-rust.md:45；既有条目（commit 930e56507 迁移）]

### B5. 不要用与内核不同的 rustc 版本编译 native 插件

- **症状**：native 插件加载/调用异常。
- **根因**：不用 abi_stable，靠 rustc 版本锁定保证 vtable 一致。
- **解法**：用 `rust-toolchain.toml` 锁定版本（1.85）；注意 `panic=abort`（panic 会终止进程）。
- **来源**：[来源: docs/guides/plugin-native-rust.md:7,53]

---

## C 组：性能与基线（别把现状当 bug）

### C1. enhanced_search 等全仓扫描工具分钟级卡顿

- **症状**：enhanced_search 全仓扫描（`path=仓库根`、`max_depth=8`）单次分钟级；极端案例 **15m13s**（D1 诊断：16:18:27 发起 → 16:33:40 返回，期间 run 已于 16:24:45 被标 failed）。
- **根因**：扫描范围含 `.venv`/`.git`/`target`/`node_modules` 等重目录（plugins 下 77 个 venv）；Windows 文件系统慢；无时限护栏时不可中断。
- **解法**（**已修复**）：三护栏齐装——`max_results` 早停 + 重目录剪枝（commit `d9d1f71c4`）+ **墙钟预算护栏**（`timeout_seconds` 缺省 30s + 每目录 deadline 检查，超时返回部分结果并标注 truncated，commit `b88aed617`，已复核在 HEAD）。规避建议：收窄 `path`、排除重目录、避免对仓库根做深扫。
- **来源**：[来源: docs/working/batch_20260913/D1_stuck_diag_fix.md:5,16；commit b88aed617（护栏，已复核在 HEAD）；commit d9d1f71c4（剪枝）；本任务实测（`find plugins` 因 `.venv` 卡顿被终止）]

### C2. 覆盖率/门禁"红"≠代码坏——基线口径

- **症状**：本地跑门禁红一片，以为代码坏了；或修好既有红后门禁仍按旧数。
- **根因**：门禁是**棘轮基线锁**（只减不增）：失败数/发现数超基线才红；低于基线不红但应**收紧**基线文件。且「门禁绿」≠「测试全绿」（需如实区分）。
- **解法**：对基线值核对——pytest 失败数 `.github/pytest-failure-baseline.txt`；mypy `.github/mypy-baseline.txt`（337）；eslint 74 / vitest 73；knip 118 / jscpd 558（`.github/knip-jscpd-baseline.txt`）。**整体覆盖率基线自 2026-09-01 起挂起观察（`--skip`，插桩照跑）**。修好既有红 → 收紧对应基线。
- **来源**：[来源: docs/working/机械门禁统一入口与覆盖率豁免.md:48,164-181；docs/guides/ci-cd-guide.md:87；commit d821072f8（knip/jscpd 基线收口）]

### C3. 审批/交互等待像"卡死"，其实是合法挂起

- **症状**：任务停在等待审批/交互状态很久，看着像卡死；run 状态 suspended。
- **根因**：审批等待 = **run suspended**（正规链路：create_choice 成功 → 审批创建 + `pipeline-executor.suspend` → 用户交互 → 恢复），**零轮次零 token**，不是卡死。超时沿用 approval 插件既有决策（超时 → `rejected/timeout`；`wait_for_choice` 默认 86400s；approval 宿主 `idle_timeout_secs: 0` 豁免 idle GC，宿主被回收即断审批等待链）。注意区分：**「创建失败 ≠ 等待中」**——通道死透（MCP response channel closed）场景由工具失败熔断兜底（同工具连败 ≥5 摘工具面、≥8 终止）。
- **解法**：确认 run 是 suspended 且审批请求已创建（前端审批卡片）；等待即可，或按超时机制处理；若审批通道连败（日志 human sidecar respawn 循环）按失败处理。
- **来源**：[来源: docs/decisions/2026-09-09-approval-lifecycle-invariants.md；docs/working/孤儿治理落地终验_20260909.md:52；docs/working/D7批1落地_20260906.md:45]

### C4. 任务/子任务找不到文件（隔离与工作空间）

- **症状**：任务里读/写文件报找不到；子任务路径与预期不符。
- **根因**：任务默认隔离执行——默认工作空间 `workspace/{task_id}` + isolated；路径锚定按工作区边界。
- **解法**：确认工作空间归属（子任务继承父工作空间）；用工作区内相对路径；主会话路径锚点需 `project_root`。
- **来源**：[来源: AGENTS.md:52；docs/decisions/2026-09-03-subtask-inherit-parent-workspace.md；commit 22acc115d（主会话路径锚点）]

---

## D 组：运行与运维

### D1. 重启内核后全员掉线/需重新登录

- **症状**：重启内核后所有会话失效，用户被踢回登录页。
- **根因**：`AGENTOS_TOKEN_SECRET` 未设 = 进程随机生成密钥，重启即全量会话失效。
- **解法**：生产显式注入固定长随机串；开发接受随机（或同样注入）。
- **来源**：[来源: docs/guides/deployment.md:81,153,233]

### D2. 内核起不来 / 库损坏

- **症状**：内核启动失败，日志报库损坏。
- **根因**：SQLite 库损坏（fail-closed，不静默重建）。
- **解法**：`AGENTOS_DB_AUTO_REBUILD=1` 重启（自动备份后重建空库继续启动）。
- **来源**：[来源: docs/guides/deployment.md:202；docs/decisions/2026-09-11-corrupt-db-fail-closed.md]

### D3. 内核进程意外退出

- **症状**：内核进程没了（崩溃 exit_code=1），任务在飞被腰斩。
- **根因**：内核崩溃（历史案例：巡检#9 内核 19:53:35 崩溃，监督者复活）。
- **解法**：用监督脚本 `run_kernel_supervised.bat` 启动（意外死亡复活）；查 `.kernel_02.log` 取证。
- **来源**：[来源: docs/working/内核监督与意外死亡恢复审计_20260910.md；run_kernel_supervised.bat；commit 9ba07feb4（巡检#9 记录）]

### D4. 忘记 admin 口令

- **症状**：无法登录 admin。
- **根因**：口令遗忘（含首登强制改密后遗忘）。
- **解法**：设 `AGENTOS_ADMIN_PASSWORD=<新口令>` 重启重置。
- **来源**：[来源: docs/guides/deployment.md:235]

---

## 附：来源索引（本文件引用到的仓内文件）

| 来源 | 位置 |
|------|------|
| 内核行为总纲 | `AGENTS.md` |
| 插件协议/开发 | `docs/guides/plugin-protocol.md`、`plugin-native-rust.md`、`streaming-protocol.md` |
| 部署/环境变量 | `docs/guides/deployment.md` |
| 执行语义（Agent/管道） | `docs/guides/execution-semantics.md` |
| CI/门禁 | `docs/guides/ci-cd-guide.md`、`docs/working/机械门禁统一入口与覆盖率豁免.md` |
| 关键 ADR | `docs/decisions/2026-09-09-approval-lifecycle-invariants.md`、`2026-08-18-plugin-dependency-package.md`、`2026-09-11-corrupt-db-fail-closed.md`、`2026-09-07-plugin-venv-dedup.md`、`2026-09-03-subtask-inherit-parent-workspace.md` |
| 诊断实证 | `docs/working/B15_watcher热重载失灵根因_20260906.md`、`docs/working/`（20260913 批次档案）、`docs/working/test_traceability.md` |
| 组件契约 | `.project/widget_contracts.md` |
