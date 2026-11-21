---
name: 插件开发
description: 插件新建/修改/调试/验证/迁移时加载。继承 code-implement 通用基座，只写插件叠加项：staging 五步流转管线、派发参数契约（工作空间/staging槽位/隔离实例/内核二进制）、scratch 实例装载验证（三根 instance.yaml + 运行时副本断言，新开内核实例是插件的完成语义）、宿主侧 plugin_verify 工具（固定脚本 pin + 路径闸 + 环境洗白）、技能按模式标签收窄、功能性骨架（主题/页面）、调试与诊断脚本全景、装机自举验证车道。设计真值 = docs/decisions/2026-11-03-plugin-dev-skill-and-staging-verify-pipeline.md + docs/decisions/2026-11-11-mode-scoped-assets-and-plugin-verify-tool.md。
---

# 插件开发

> **分层声明（ADR 2026-11-03-plugin-dev-skill-and-staging-verify-pipeline）**：本技能是
> `code-implement`（通用代码开发基座：TDD/模块边界/门禁/落账）的**子集叠加**，不重复基座
> 内容。派发规则：任务书目标含插件 → 双指针（code-implement + 本技能）；纯代码 → 单指针
> code-implement。工具声明契约单源 = `skills/resource-tool-create/SKILL.md`（不复制）。

完成语义：普通代码的验收单位=变更本身（测试绿+门禁绿即完成）；插件的验收单位=**在真实内核里被装载、被声明、被调用**。验证标配=**新开内核实例装载测试**（G2 静态校验过 ≠ 装载活；装载环节 = plugin-loader 发现 → G2 → 注册 → sidecar spawn + venv 解析）。

## 一、铁律（先读再动手）

1. **新建插件必须在新建目录空间内进行**（隔离任务工作空间；终端用户在用户空间 staging）——
   禁止在主仓 dev 目录直接长插件（沿用 skill-dev-toolkit 铁律）。
2. **staging 是唯一创作落点**：终端用户/装机场景插件先落 staging 目录（用户空间内固定
   槽位，自持 .venv——不放系统临时目录，防清理策略扫掉 venv）。
3. **现役内核与装机应用永不触碰**：验证一律走 scratch 验证实例（三隔离）。
   **验证实例毒化豁免边界**（ADR D3）：scratch 验证实例允许经脚本拉起——端口隔离
   （硬拒 9100/9101）+ 不调 LLM（断言面 /health、/api/v1/tools、/api/v1/schema，无密钥
   需求）+ 实例档案精确 stop；此豁免不适用于现役内核与装机应用，"装机侧动作先问用户"
   铁律不变。
4. **electron 壳不进验证车道**：前端本体验证走"asar 解包（只读安装目录）+ 静态伺服 +
   playwright 反代"无壳形态；装机前端本体不开第二实例（单实例锁 + 装机铁律）。
5. **声明面契约单源**：工具声明（input_schema/output_schema/render 必填、tool_core
   fail-closed）见 resource-tool-create；改了 plugin.json 的 http_endpoints / render.card
   必跑对应生成闸（见 §五）。
6. **复用优先**：仓内已有脚本一律指针引用（§五标"已有"者），禁止复制进本技能
   （防双真值漂移）；本技能 scripts/ 只放插件域特有件。

## 二、staging 五步流转管线（主流程）

```
① 创作/修改
   脚手架起步（§四），产物落 staging 目录
   （dev 用户空间：<user_root>/plugins_staging/<plugin_id>/；
    装机用户空间：%APPDATA%\agentos\plugins_staging\<plugin_id>\）
② 装载验证（新开内核实例，三隔离 + 三根显式配置）
   插件根走临时 instance.yaml（AGENTOS_INSTANCE_CONFIG，分级单副本 ADR 2026-11-07）：
   Tier2 = tier1_root/shared_root 指内置根、tier2_root 指只含被测插件的干净视图；
   Tier1 = tier1_root 指「清单内全部 Tier1 各一 junction、被测件指向 staging」视图，
   tier2_root 空目录（清单内 Tier1 缺席内核启动守卫拒启）。其余隔离：
   AGENTOS_KERNEL_PORT=<空闲端口，硬拒 9100/9101> + AGENTOS_DB_PATH=<临时快照> +
   AGENTOS_CONFIG_ROOT=<含 kernel/plugin_tiers.yaml 的配置根> + AGENTOS_USER_ROOT=
   <scratch 内临时用户根，不碰真实用户空间>；断言链带 Bearer（登录换 token，
   匿名必 401）——verify_plugin.py 已内置全套
   断言链：plugin_check 声明校验 → /health 就绪 → /api/v1/tools 工具面可见
   → /api/v1/system/plugin-load-manifest 运行时副本=staging 那份（防同 id 旧副本假绿）
   → /api/v1/schema ui_schema 声明 → 端点响应/output_schema 校验
③ 通过 → 迁移
   promote_plugin.py：staging → 用户插件根 + 版本严格大于校验（BuiltinFirst，
   持平=同步无效）+ 迁移后热发现确认
④ 装载进应用
   现役应用 watcher 热发现接管用户插件根，双源裁决用户版胜出，零重启
   装机版同步真身 = sync_installed_user_space.py（人工执行、先问用户，不进自动车道）
✗ ②不通过 → 删 staging，按 plugin_doctor.py 诊断报告修复后重来
```

前端验证在②内并行（分层 L1/L2/L3，见 §六）。

## 二·五、派发参数契约（任务书必含；编码 agent 经任务书加载本技能）

插件任务的任务书必须写死下表五项，不留给执行者现场推导。技能仍按既定口径经
**任务书技能索引指针**下发（不挂 agent 提示词）；本技能只规定插件域增量，通用
TDD/门禁见 `code-implement`。

| 参数 | 规定值 | 约束依据 |
|---|---|---|
| 工作空间 | 仓内派发=`workspace/{task_id}` 隔离工作空间；终端用户=`<user_root>/plugins_staging/<plugin_id>/` | 禁止在主仓 dev 目录直接长插件（§一铁律 1）；staging 是唯一创作落点 |
| staging 槽位 | `<workspace>/plugins_staging/<plugin_id>/`（仓内任务）或 `<user_root>/plugins_staging/<plugin_id>/`（装机） | 自持 `.venv`，不放系统临时目录（§一铁律 2） |
| 验证实例 | 后端 `verify_plugin.py`（三根 instance.yaml + 空闲端口 + 临时 DB + scratch 用户根）；后端+前端一体用 `verify_host.py [--frontend]` | 端口硬拒 9100/9101；不碰现役内核/装机应用（§一铁律 3、毒化豁免边界）；前端车道复用同一实例 |
| 转发方式 | 隔离环境是 WSL → `verify_host.sh`（interop + `wslpath` + 落盘 .bat，**不需要 broker**）；Docker → 没有 interop，需宿主 broker/白名单 | 命令内联路径会被 interop 引号双重复原，一律落盘 .bat 再执行（e2e 实测）；**interop 不传播 Windows 退出码**（本机实测 `cmd /c exit 7` 从 WSL 看恒 0）→ bat 落 `<workspace>\.agentos_verify_rc` 回传真实码，文件缺失按失败；WSL workspace-only 沙箱会切断 interop（ADR 2026-11-10），需 `--ro-bind-try` 白名单或改用 broker |
| 内核二进制 | dev=`kernel/target/release/agentos-kernel`；装机=`<安装位>/resources/kernel/<exe>` | 装机自举 `--kernel-bin` 直指包内二进制，验证环境=目标环境 |
| 清场与证据 | `verify_evidence.json` 落 staging；实例档案精确 stop | 失败证据随任务回传，进度不口头化 |

**bash 条件可执行性**：后端车道纯 CLI/REST；**WSL 场景整条车道（含前端浏览器）都能从
隔离环境驱动**——`verify_host.sh` 经 interop 把任务转发到宿主执行，浏览器/截图都在宿主
跑，结果写回 workspace。AgentOS bash 四单词工具面（bash/log/input/kill）可完整承载。
前提三条：① WSL interop 未被沙箱切断（`sandbox_cmd` 为空时默认可用；ADR 2026-11-10
的 workspace-only 沙箱会切断 `/mnt/c` 与 Windows exe 执行）；② 有可写 scratch 目录；
③ 前端车道要宿主有 node + `frontend/node_modules/playwright`（dev 仓自带；纯装机机器
无 node_modules 时浏览器车道如实报错，后端车道不受影响）。Docker 容器无 interop，
需宿主 broker/白名单转发。

**技能按模式收窄（2026-11-11 落地）**：任务/会话带 `execution_context.mode`（缺省
回退 agent 键 `mode_X/<stem>`）时，`workspace_lifecycle` 只把**当前模式包**的
`skills/` 复制进工作空间（仓根 `skills/` 与用户根 `skills/` 恒复制）；无 mode 标签
才回退全量并集。任务书不要假设其他模式包的技能存在——按技能索引指针加载本技能/
code-implement 等公共技能不受影响。

## 五·五、宿主侧工具 `plugin_verify`（无宿主 bash 的 agent 用）

`plugins/shared/plugin_verifier` 提供一个 MCP 工具 `plugin_verify`，参数面闭集：

```json
{"plugin_path": "plugins/shared/my_plugin", "scope": "backend|frontend", "timeout_s": 240}
```

- `plugin_path` 必为**当前工作空间相对路径**（绝对路径、`..`、链接逃逸直接拒）；
  `workspace` 由内核/管道注入，调用方不可指定解释器/脚本/环境变量/任意命令。
- 工具只执行安装位内的固定脚本 `skills/plugin-dev/scripts/verify_host.py`
  （`skill-dev-toolkit/scripts/spawn_test_kernel.py` 为传递依赖），四个脚本的
  EOL 归一 SHA-256 记在 `plugins/shared/plugin_verifier/script_pins.json`；脚本被
  改动而 pin 未更新 → fail-closed 拒绝执行。**改验证脚本须同步更新 pin 表**
  （`test_verifier.py` 有同文闸）。
- 内核/配置/内置插件根取**安装位**，不取 agent 工作区内的副本；Tier2 用户副本
  场景按**进程祖先内核 exe** 反推安装根（`AGENTOS_APP_ROOT` 可显式覆盖）；子进程
  环境白名单（不带 `AGENTOS_*`/凭证）；超时杀整棵进程树；证据落盘二次脱敏；
  scratch（`instance.yaml` 明文口令）与前端 `probe_config.json` 验证结束即清。
- 语义仍是**真实装载**（被测插件代码在宿主 scratch 内核里执行，R2）——工具约束的
  是"用什么脚本、在哪个环境、对哪个目录"执行，不是把插件执行变成静态检查。
- 版本纪律同 §六：改已存在插件须 bump 版本，否则旧副本胜出、`loaded_copy` 如实红。
- 挂载：**按任务模式标签注入**——模式包 `mode.yaml` 的 `inject_tool_ids` 声明，
  `agent_config_load` 在任务/会话管道按激活模式并入（`mode_evolution` 已声明
  `plugin_verify`）。工具**不写进 agent 的 `tool_ids` 白名单**：同一 agent 在无插件
  任务时不背该工具的 schema 成本；有 bash 的编码 agent 继续用脚本车道。新增消费模式
  只需在对应 `mode.yaml` 加一行 `inject_tool_ids: [plugin_verify]`。

## 三、四参数特例（同一管线，不同取值）

| 参数 | 派发开发任务 | 终端用户创作 | 进化提案（mode_evolution） |
|---|---|---|---|
| 创作目标 | 任务书（设计真值+验收判据指针） | 用户会话意图 | 题集假设/归因 |
| 工作空间 | 隔离 workspace/{task_id} | 用户空间 staging | 主仓可改面白名单 |
| 写权限面 | 权限模式档 | staging 仅限 + 安检 | 冻结面/白名单/原子性/base_hash/安检 + 熔断 |
| 验证车道 | run_gates + 实例装载测试 | 实例装载测试（自举车道） | 门禁 + 实例装载 + held-out |

进化提案 target 命中 `plugins/**` → 执行方按本技能干活，proposal 工具叠加进化权限参数
（流程在本技能、权限在工具，两者不分家也不混装）。

## 四、脚手架：通用一级 + 功能性一级

**通用工具插件**（最常用）——用既有脚手架（指针引用）：

```bash
python skills/skill-dev-toolkit/scripts/scaffold_plugin.py my_tool \
  --one-line "一句话描述" --target <staging 父目录>
```

**功能性插件**——本技能自带生成器（按交付物/前端面类型，多文件模板）：

```bash
python skills/plugin-dev/scripts/scaffold_functional.py my_theme \
  --kind theme --one-line "一句话描述" --target <staging 父目录>
python skills/plugin-dev/scripts/scaffold_functional.py my_page \
  --kind page --one-line "一句话描述" --target <staging 父目录>
```

| kind | 生成物 | 契约参照 | 状态 |
|---|---|---|---|
| `tool` | plugin.json + server.py + 实现 + 测试 | resource-tool-create（真源） | 既有脚手架 |
| `theme` | plugin.json（contributes.themes）+ 皮肤 CSS + 说明 | dsh_adapter 判例（contributes.themes + HTTP 资产递送） | ✅ 本技能 |
| `page` | plugin.json（contributes.pages + http_endpoints）+ 自包含 HTML | mode_evolution 面板判例（CSP 自包含契约） | ✅ 本技能 |
| `settings_panel` | —— | 契约待真源化（前端设置面板消费面未核实） | ⛔ 诚实边界，不写没把握的声明结构 |
| `interaction` | —— | human-interaction 契约待真源化 | ⛔ 同上 |

骨架不只是代码起点，还是**验收契约的起点**：每类模板自带"已知限制"段与专属验证判据
（§六判据表），创作时就知道完成长什么样。

## 四·五、混合插件（tool+page 合一）组装配方

snippet_box 实测沉淀（commit 2c4bd54b7 验证通过）：

1. 双脚手架各生成到临时目录（tool 骨架 + `scaffold_functional --kind page`）；
2. **page 骨架为基底**（plugin.json 含 contributes.pages + http_endpoints），把
   tool 骨架的工具声明（六要素齐）并入 `capabilities.tools[]`；
3. server.py **双注册**：snippet 工具（LLM 面）+ `http.handle`（运行面，
   handler_capability 指向；不进 plugin.json 声明面）；
4. page.html 保持自包含契约（单 script、数据供页时锚点内联，禁出网通道）；
5. 数据文件（如 `data/*.json`）工具与供页 handler 共读同源；
6. 验证：`verify_plugin.py`（工具面+schema）+ 生成物自验收（CSP 禁串）双链。

## 五、脚本全景（按阶段；标"已有"者指针引用勿复制）

| 阶段 | 脚本 | 来源 |
|---|---|---|
| 创作 | `skills/skill-dev-toolkit/scripts/scaffold_plugin.py`（通用 tool） | 已有 |
| 创作 | `skills/plugin-dev/scripts/scaffold_functional.py`（theme/page） | 本技能 |
| 调试 | `skills/plugin-dev/scripts/debug_tool.py`（无 LLM 直调工具：单元级直调实现函数 / 集成级经实例 admin 面） | 本技能 |
| 调试 | `skills/plugin-dev/scripts/plugin_doctor.py`（装载诊断五查，"插件为什么没生效"一次出报告） | 本技能 |
| 调试 | `skills/skill-dev-toolkit/scripts/kernel_db_query.py`（查任务/管道/追踪数据） | 已有 |
| 调试 | 日志拦截提取 / DB 快照对比 / 副作用 mock | 已有（test-infra 技能，指针） |
| 调试 | venv 供给 `scripts/ensure_plugin_venvs.py`（仓根真源）；dev 侧插件 502 = 复制仓内插件 `.venv` 即愈 | 已有 |
| 声明面 | `gen_frontend_endpoints.py` / `gen_frontend_plugin_pages.py` / `generate_render_card_vocabulary.py` + 三个 check 同步闸 / `check_tool_schema_contracts.py` | 已有（改声明必跑） |
| 验证(后端) | `skills/skill-dev-toolkit/scripts/spawn_test_kernel.py`（三隔离 scratch 实例，装机场景传 `--kernel-bin` 指包内二进制） | 已有 |
| 验证(后端) | `skills/plugin-dev/scripts/verify_plugin.py`（三根 instance.yaml + 断言链编排 + 运行时副本断言 + 证据 JSON；`keep_running` 供前端复用实例） | 本技能 |
| 验证(宿主/前端) | `skills/plugin-dev/scripts/verify_host.py`（后端 keep_running + 前端 L2 同实例串跑 + 合并证据 `verify_host_evidence.json`） | 本技能 |
| 验证(前端探针) | `skills/plugin-dev/scripts/frontend_probe.cjs`（静态伺服 dist + /api、/ext 反代 + /ws 隧道 + Playwright 登录/截图/iframe 断言） | 本技能 |
| 命令转发(WSL) | `skills/plugin-dev/scripts/verify_host.sh` + `verify_host.bat`（wslpath + 落盘 .bat + `cmd.exe` interop；Docker 无 interop 才需 broker） | 本技能 |
| 验证(工具面) | `plugins/shared/plugin_verifier`（`plugin_verify`：固定脚本 + SHA-256 pin + 环境洗白 + 树杀 + 证据脱敏；无 bash 的 agent 用） | 本仓插件 |
| 迁移 | `skills/plugin-dev/scripts/promote_plugin.py`（staging → 用户插件根 + 版本严格大于） | 本技能 |
| 装机运维 | `sync_installed_user_space.py`（版本严格大于内置；绝不写安装目录） | 已有（人工、先问用户） |
| 清场 | `spawn_test_kernel.py stop`（实例档案精确杀 + 端口释放验证） | 已有 |

## 六、验证分层（后端 + 前端）

**后端断言链**（verify_plugin.py 编排，全走真内核实例）：

1. plugin_check 声明校验（内核 bin 可用时）；
2. 生成三根 instance.yaml（Tier2/Tier1 两种视图形态，见 §二②）→ scratch 实例 spawn
   → `/health` 轮询就绪；
3. `GET /api/v1/tools` → 断言插件工具出现在 items；
4. `GET /api/v1/system/plugin-load-manifest` → 断言运行时副本 path 在验证视图内且
   版本等于 staging manifest（**假绿防线**：改动已存在插件时 staging 版本须严格
   大于内置副本，否则内核 version-max 归约旧副本胜出，本步如实红）；
5. `GET /api/v1/schema` → 断言 ui_schema 声明（装机生效"两面查"口径的第二面）；
6. 业务端点响应 + output_schema 校验通过；
7. 证据 JSON（逐项 pass/fail + 端点响应摘录）落 staging。

**前端分层**（装机前端本体不开第二实例——无壳车道，ADR D4）：

| 级 | 验什么 | 怎么验 | 需要什么 |
|---|---|---|---|
| L1 声明契约 | render 在词表/schema 结构/CSP 禁串 | 静态闸 | 无实例 |
| L2 真前端渲染 | 首页登录态、插件 page 渲染（iframe 内容/尺寸）、主题变量注入、截图证据 | `verify_host.py --frontend`：静态伺服 dist（dev=`frontend/dist`，装机=`resources/frontend/dist`）+ 测试侧反代 /api、/ext、/ws → Playwright 注入 refresh_token 登录 → 首页/插件页 + iframe 截图 | scratch 实例 + node + `frontend/node_modules/playwright` |
| L3 render 卡片端到端 | 工具结果卡片视觉与交互（行号 gutter/折叠/滚动） | 需一条真实工具调用 → 本地桩 LLM provider | 桩 LLM（待查证，查证前降级标注） |

视觉质量判定：机械断言（元素存在/可点击/变量计算值/对比度）自动判 + 截图证据包人工抽检
（"审查报告只写未修项"口径）。

## 七、完成判据模板（写任务书/自验收时用）

判据写成"目标文件里存在可验证内容"，不写"落盘台账即完成"：

- 工具插件：`<staging>/<id>/` 存在且 `verify_plugin.py` 证据 JSON 中 `tools_visible=true`、
  `schema_declared=true`、业务断言全 pass；
- 主题插件：证据 JSON 中 `ui_schema_theme_declared=true` 且 L2 截图证据存在；
- 页面插件：CSP 禁串扫描 0 命中 + playwright 加载渲染截图存在；
- 迁移完成：`promote_plugin.py` 输出 `promoted=true` 且版本严格大于现役副本记录在案。

## 八、装机自举验证车道（无开发者仓库，ADR D3）

装机机器验证实例四资产全部现成：内核二进制（`resources\kernel\`，与现役同款——**验证
环境=目标环境**）、配置（包内出厂 + `%APPDATA%\agentos`）、venv（resources 镜像 +
staging 自持）、待验证插件（staging）。技能脚本随包在
`<安装位>\resources\skills\plugin-dev\scripts\`。

```powershell
# 后端（必需）
python "<安装位>\resources\skills\plugin-dev\scripts\verify_plugin.py" `
  "$env:APPDATA\agentos\plugins_staging\<plugin_id>" `
  --kernel-bin "<安装位>\resources\kernel\agentos-kernel.exe" `
  --scratch-root "$env:APPDATA\agentos\plugins_staging\.verify_scratch"

# 后端 + 前端浏览器车道（同一实例；dist 自动取 <安装位>\resources\frontend\dist）
python "<安装位>\resources\skills\plugin-dev\scripts\verify_host.py" `
  "$env:APPDATA\agentos\plugins_staging\<plugin_id>" --frontend `
  --kernel-bin "<安装位>\resources\kernel\agentos-kernel.exe" `
  --scratch-root "$env:APPDATA\agentos\plugins_staging\.verify_scratch"

# 隔离环境（WSL）命令转发（interop，无需 broker；在工作空间根执行）
#   <plugin 相对路径> 相对当前工作空间；kernel 参数为宿主 Windows 路径
bash skills/plugin-dev/scripts/verify_host.sh <plugin 相对路径> frontend "D:\...\resources\kernel\agentos-kernel.exe"
```

- 配置根缺省取 `<安装位>\resources\config`（与现役出厂一致）；要与现役用户改动
  对齐时显式 `--config-root "$env:APPDATA\agentos\config"`；
- 三隔离不变（端口硬拒 9100/9101；DB 临时；用户根=scratch 内临时目录，不写
  `%APPDATA%\agentos` 真实数据面）；
- 分级单副本口径：Tier1 插件 app 侧无热更路径，验证也要全 Tier1 视图（脚本自动
  建），改完只能走打包/重装；Tier2 才经 `sync_installed_user_space.py` 热更；
- 与 dev 侧唯一差别 = `--kernel-bin` 直指包内二进制。
- 毒化豁免边界见 §一铁律 3（只豁免 scratch 实例；现役内核/装机应用永不触碰）。

## 附录 A、接口与契约索引（插件开发对外契约，自包含）

> **定位（重要）**：装机版（应用版）的内核不对外公布——终端用户与插件开发者**没有内核
> 源码可查**。本附录是插件开发的**唯一对外契约文档**，必须自包含：每个接口写全请求/
> 响应形态、认证要求、错误语义；每个声明字段写全语义、必填性、默认值。仓内开发者仓库
> 场景的深查指针以"深查"标注降为次要。契约受"契约冻结"制度保护（AGENTS.md），动契约
> 需 ADR + 兼容机制——冻结是自包含可行的前提。本附录随实测/契约变更同批更新
> （snippet_box 实测沉淀，2026-11-03）。

### A1 内核 REST 面（调试/验证直调）

**认证模型**：JWT Bearer。`POST /api/v1/auth/login` 换 `access_token`，后续请求带
`Authorization: Bearer <access_token>`。除 `/health` 与 auth 族外全部端点需 Bearer
（匿名 401）。admin 口令来自 scratch 实例的 `AGENTOS_ADMIN_PASSWORD`（实例档案
`record.admin_password`）。401 = token 缺失/失效；`/api/v1/auth/refresh` 轮换。

| 端点 | 方法 | 请求 | 响应 | 认证 |
|---|---|---|---|---|
| `/health` | GET | — | 200 即就绪 | 无 |
| `/api/v1/auth/login` | POST | `{"username":"admin","password":"..."}` | `{access_token, refresh_token?, token_type, expires_in, must_change_password}` | 无 |
| `/api/v1/auth/refresh` | POST | refresh_token 轮换 | `{access_token, refresh_token?, token_type, expires_in, must_change_password}` | refresh_token |
| `/api/v1/auth/me` | GET | — | 当前用户 | Bearer |
| `/api/v1/auth/logout` | POST | — | 注销 | Bearer |
| `/api/v1/tools` | GET | — | `{items:[{name, description, plugin_id, category, source, ...}], total}`——LLM 工具面，**tools_visible 断言端点** | Bearer |
| `/api/v1/schema` | GET | — | `{"agents":[{id,name,ui_schema,version}], "plugins":{...}}`——声明投影，**ui_schema 两面查端点** | Bearer |
| `/api/v1/system/plugin-load-manifest` | GET | — | `{"manifest":{schema,config_path,tier1_root,tier2_root,shared_root,plugins:[{plugin_id,version,tier,path,source_root}]}}`——启动装载清单，**loaded_copy 断言端点**（"我在跑哪份"） | Bearer |
| `/api/v1/plugins` / `/api/v1/plugins/hosts` | GET | — | 已装插件与宿主清单 | Bearer |
| `/api/v1/pipelines` / `runs` / `state` | GET | — | 管道定义/运行行/状态（面板数据同源） | Bearer |
| `/api/v1/actions/execute` | POST | 动作执行 | 执行结果 | Bearer |
| `/api/v1/chat` | POST | 会话入口 | 会话流 | Bearer |
| `/api/v1/system/memstats` | GET | — | 内存指标 | Bearer |

深查：`kernel/crates/api/src/server.rs`（路由表）、`kernel/crates/api/src/auth.rs`（认证中间件与 JWT 生命周期）。

### A2 内核环境变量面（实例/验证配置）

| 变量 | 语义 | 默认/坑 |
|---|---|---|
| `AGENTOS_KERNEL_PORT` | 监听端口 | 9100=dev / 9101=装机默认；scratch 实例硬拒这两个 |
| `AGENTOS_DB_PATH` | SQLite 库路径 | 验证实例指临时快照，跑完即删 |
| `AGENTOS_ADMIN_PASSWORD` | admin 口令 | scratch 实例随机生成并记入实例档案 |
| `AGENTOS_STORAGE_DRIVER` | 存储驱动 | SQLite 默认 |
| `AGENTOS_INSTANCE_CONFIG` | 实例启动配置文件路径（**三根** tier1_root/tier2_root/shared_root） | 验证实例由 `verify_plugin.py` 生成临时 instance.yaml；分级单副本后插件根只认它（内核零推导零回落） |
| `AGENTOS_CONFIG_ROOT` | 配置根（含 `kernel/plugin_tiers.yaml`） | dev=仓根 `config`；装机=`<安装位>\resources\config` 或 `%APPDATA%\agentos\config`；验证实例必须显式给（缺省回落是编译期路径） |
| `AGENTOS_USER_ROOT` | 可写用户数据根（模式播种/venv 登记） | 验证实例指 scratch 内临时目录，不碰真实用户空间 |
| `AGENTOS_PLUGINS_DIR` | 内置插件根（单值替换语义，历史兼容） | 直指 staging 缺内置依赖必拒启；**勿用于插件验证**（插件根走 instance.yaml） |
| `AGENTOS_USER_PLUGINS_DIR` | 用户数据面插件根（模式播种/用户仓） | **不再决定装载根**（双源仲裁已退役，ADR 2026-11-07）；staging 验证勿用它 |
| `AGENTOS_DSH_REPO_ROOT` | DSH 参考仓根 | 仅 dsh_adapter 用 |

### A3 声明面（plugin.json；G2 校验 fail-closed，缺声明不入面）

**顶层键全集**（15 键）：`id / name / description / version / plugin_type / language / host_type / entry / requires_services / capabilities / contributes / http_endpoints / permissions / priority / host_group`。

| 键 | 语义 | 必填 | 说明 |
|---|---|---|---|
| `id` | 插件唯一标识 | ✅ | snake_case；建卡 ID=实体 ID |
| `name` | 展示名 | ✅ | PascalCase 惯例 |
| `description` | 一句话能力描述 | ✅ | |
| `version` | 语义版本 | ✅ | 装机同步必须严格大于内置副本（BuiltinFirst） |
| `plugin_type` | `tool` / `pipeline` / `system` | ✅ | 管道步骤插件用 pipeline |
| `language` | `python` | ✅ | |
| `host_type` | `sidecar`（独立进程）/ 合宿 | ✅ | |
| `entry` | `python server.py` | ✅ | |
| `requires_services` | 依赖的内核服务 | 可省 | 如 `["packaging"]` |
| `capabilities` | 能力声明（下表） | ✅ | |
| `contributes` | 贡献声明：pages / themes（下表） | 可省 | |
| `http_endpoints` | HTTP 端点声明（下表） | 可省 | |
| `permissions` | 最小授权：`filesystem{read_paths,write_paths}` / `network{allowed_hosts}` / `env_vars` / `system_calls` | ✅ | 通配符给警告 |
| `priority` | 执行优先级 | 可省 | 默认 50 |
| `host_group` | 宿主分组 | 可省 | 如 `light` |

**capabilities 子键**：`tools[]`（LLM 工具面）/ `services[]`（内部服务元数据）/ `lifecycle_hooks[]`（`on_load` / `on_unload`）/ `route_signals[]`。

**tools[] 六要素**（每项，契约单源 `skills/resource-tool-create/SKILL.md`）：
`name`（snake_case）/ `description` / `category` / `input_schema`（JSON Schema）/ `output_schema`（JSON Schema，缺则 tool_core 拒）/ `render`（`{"card": "<词表值>", ...}` 前端按 card 路由）。

**http_endpoints[] 七字段**：`route_id`（唯一）/ `method`（GET/POST）/ `path`（`/ext/<插件id>/...`）/ `auth`（`user` 需登录 | `none` 浏览器原生加载场景）/ `handler_capability`（恒 `"http.handle"`，指向 server.py 运行面注册的同名工具）/ `timeout_ms` / `max_concurrency`。

**contributes.pages[] 九字段**：`id` / `title` / `icon` / `space`（`workspace` | `floating`）/ `slot`（`tab` | `panel`）/ `path`（`/p/<page_id>`）/ `widget`（`"webview"`）/ `props`（`{pluginId, htmlPath, widgetId}`，htmlPath 对应 http_endpoints 的 path）/ `order`；可选 `when`（可见性条件表达式，判例 `debug_center`：`"user.role == 'admin'"`）。页面 HTML **自包含契约**：单 script、无外链资源、无出网调用（CSP connect-src 'none'），数据供页时锚点内联。

**contributes.themes[] 五字段**：`id`（`<插件>-theme` 惯例）/ `name` / `description` / `base`（`light` | `dark`）/ `variables`（`--ds-*` token 键值集，消费面前端主题注入；起步集见骨架 theme.css，键集对账判例 dsh_adapter）。

### A4 运行面（server.py，插件 SDK）

```python
from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("<插件id>")                      # 模块级实例（合宿契约：装载期注册）

plugin.register_tool(
    "<工具名>",                                          # 与 capabilities.tools[].name 一致（http.handle 同通道）
    input_schema_dict,                                   # 与声明逐字对应（G2 声明↔实现比对，漂移即剔除）
    实现函数,                                             # kwargs = input_schema.properties
    "描述", output_schema=dict, render=dict,
)
plugin.run()                                             # sidecar 启动
```

- **HttpHandleResponse**（http.handle 返回值）：`{"status": int, "headers": dict, "body": str, "body_encoding": "utf-8"|"base64"}`——body base64 原样回写；404 = `{"status":404, "headers":{}, "body":"", "body_encoding":"utf-8"}`。
- **工具结果信封**（内核侧封，插件返回值进 `data`）：七键 `{call_id?, tool_name, success, error, data, metadata, duration_ms}`；`duration_ms` **整数毫秒**（内核 u64 严格 serde，float 整条丢弃）；`error` 是 string（结构化细节进 `result_data.error_info`）。
- **生命周期钩子**：`capabilities.lifecycle_hooks: ["on_load", "on_unload"]`；实现 `@plugin.on_load / @plugin.on_unload` 装饰的异步函数（判例 mode_evolution/dsh_adapter）。
- 深查：`plugins/sdk/src/agentos_plugin_sdk/`（SDK 源）、判例 `plugins/shared/dsh_adapter/server.py`。

### A5 装载生命周期与版本纪律

watcher 热发现全链路：新建插件目录/改 plugin.json/改代码 → 发现 → G2 校验（fail-closed，失败不入面且原因可查 plugin_doctor）→ 注册/重注册（零重启）；前端 schema 刷新页面才更新。用户插件根与内置根双源 **BuiltinFirst**：用户副本 version **严格大于**内置才生效（持平=同步无效）。LLM 可见面由 agent 配置 `tool_ids` 白名单控制——不登记则工具对 LLM 不可见（`python scripts/check_agent_tool_ids.py` 验证）。

### A6 数据面

工具数据存插件目录内（permissions 最小授权，`data/*.json` 惯例）；内核库只读排查走 `scripts/kernel_db_query.py`（临时快照，**禁直连活库**）；state/trace 为管道写面，插件经 PluginContext 读写；跨插件服务调用走 service-registry 能力面（判例 mode_evolution `_load_recent_runs`）。

## 九、已知边界（诚实声明）

- `settings_panel` / `interaction` 两类骨架未提供：前端消费面契约未真源化，不写没把握的
  声明结构（补真源后扩 kind）。
- Tier1 验证视图由 `config_root` 的 `plugin_tiers.yaml` 决定：新增 Tier1 插件须先登记
  分级清单并存在于内置根（否则启动守卫拒启），脚本按清单现算，不另设开关。
- 改动**已存在**插件时 staging 版本必须严格大于内置副本：内核装载对同 id 做
  version-max 归约（持平旧副本胜出），`loaded_copy` 断言会如实打红并提示。
- instance.yaml 内的相对路径由内核按**配置文件所在目录**解析：scratch 缺省值可能是
  相对路径（插件同级），必须由调用方 resolve 成绝对再落盘，否则三根被二次拼接、
  启动守卫直接拒启（2026-11-11 WSL 转发实测，verify_plugin 已统一 resolve）。
- L2 已落地（`verify_host.py --frontend`）：深链 `/p/<id>` 的 `plugin-page-root` 只有
  `min-h-screen`，webview 的 `h-full` 拿不到确定高度——探针补 100vh 等价高度后再断言
  iframe 尺寸/节点数。webview 取数走 MCP bridge 的插件页（如 acp_quota_pool 控制台）
  在 scratch 实例下会如实红（无宿主 bridge），改用 http_endpoints 供页的插件页可正常验。
- L3 卡片端到端验证待内核自定义 LLM provider 配置面查证；查证前 L3 由前端本体回归车道
  （dev 侧 vitest）覆盖，装机车道输出降级标注。
- `debug_tool.py` 集成级直调依赖实例 admin 工具调用端点（待查证；无则降级 sidecar 直调）。
- 本技能不承载 electron 壳/打包面验证（属桌面壳开发与发布 QA），不改 mode_evolution
  proposal 工具（扩围另行立项）。
