# 灵汐 AgentOS 路线图

> 本文档描述灵汐 AgentOS 的**未来演进方向**。路线图是规划性的，会根据社区反馈和优先级动态调整。
> 想影响路线图？欢迎在 [Discussions](https://github.com/jianchen08/Agent-os-open/discussions) 发起讨论。

> **现状（2026-08）**：0.2 核心地基层已落地——Rust 微内核 + Python 插件 + 一切皆插件
>（manifest 统一协议、双根发现、热插拔均在生产路径）。当前架构见
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，现状速览见 [README.md](README.md)；
> 本文正文中的「已上线 / 现状」字样如无特别说明指 **0.1 基线**，各规划条目的实际落地
> 状态以上述两份文档为准。

---

## 🎯 愿景

> 让 AI Agent 的搭建像配置一台服务器一样简单，但能力上限不设限。

我们追求三个长期目标：

1. **配置即产品** —— 用户通过 YAML 就能"组装"出满足自己业务需求的 Agent 产品，无需写代码
2. **可进化的内核** —— Agent 自身能通过反馈闭环持续优化，越用越好用
3. **开放协议** —— 全面兼容 MCP / OpenAI Function Calling / Anthropic Tool Use，让生态互通

并据此衍生出六条**能力演进主线**，贯穿后续版本：

- 🧬 **可进化** —— 复盘系统（`trigger_review` → `review_agent` → 经验报告）已上线，自进化闭环已具备；插件化越彻底，复盘产出能落到的可优化面越广，自进化能力越强。后续版本持续打磨这条回路（贯穿全版本）
- 🔁 **全能闭环** —— 创意生产核心交互：AI 生产制品 → 人类审批 → 批注反馈 → AI 修改 → 交付。文本审批闭环在 0.2.0 率先优化，多模态审批（图片/视频/3D）随对应场景后续补全
- 🔌 **可嵌入** —— 0.2.0 核心地基层一并做透：最小内核 + 全插件（统一插件协议 + 管道引擎路由 DSL + SQLite 存储）+ 第三方插件协议（含内部模块完全插件化）+ 宿主接入 + 前端 Schema 驱动 + 审批闭环 + 记忆系统 + 多租户契约预留；再跨平台打包分发（0.3.0）
- 👥 **多用户** —— 0.5.0 完整实现多租户：每用户独立的插件/配置/记忆/人设组合，RBAC 权限 + 凭据保险库 + 插件市场分享；单实例多租户与独立实例混合部署
- 🎭 **可扮演** —— 从任务型助手走向可导入角色卡、有人设、有形象、能进入游戏世界的智能体（0.6.0 地基 → 0.7.0 形象/NPC）
- 🛠️ **可即用** —— 从"框架"走向"开箱即用的办公技能与 Agent 团队"（0.4.0）

> **开放协议现状**：MCP 与 Function Calling 兼容协议均已支持（暂无 Anthropic Tool Use 协议）。

### 终极形态：配置即一切

上述版本迭代完成后，灵汐将达到这样的终态——**只要模型能力足够，任何新需求都不再需要改内核**：

- **执行任何能力** = 添加相应插件 + 修改/添加对应配置（YAML）
- **接入任何工具/外部服务** = 通过连接器（connectors）+ manifest 接入
- **前端表现力与体验** = 通过工作区（Workspace）+ 外部工具连接实现，后端能力即前端界面

内核趋于稳定后，灵汐成为一个**纯配置驱动的可进化平台**：插件协议、Schema 渲染、工作区、连接器共同构成「能力长出界面、界面驱动能力」的闭环。1.0.0 之后的演进，主要发生在插件市场、配置生态、人设/形象资源层面，而非内核改动。

这也是 vision.md 三维愿景的飞轮落地：**全能闭环**（覆盖更多场景）→ **自进化**（生成新能力）→ **超级终端**（自动长出界面）→ 用户体验提升 → 回到全能闭环。三者互相增强，越用越强。

---

---

## 📅 版本规划

> **关于「插件」的术语澄清**：灵汐里「插件」在不同语境下指代如下三类，本路线图统一使用：
>
> | 名称 | 状态 | 说明 |
> |------|------|------|
> | **管道插件** | ✅ 已上线 | 管道内部的 Input / Core / Output 插件链，负责上下文注入、推理、后处理；出口转移由管道 YAML 的路由 DSL 裁决 |
> | **系统级插件化** | ✅ 已上线 | 一切皆插件：Agent / 工具 / 通道 / 记忆 / 触发器全部插件承载，全链热插拔（watcher 自动发现注册） |
> | **第三方插件协议** | ✅ 0.2 已落地 | manifest 驱动的统一插件协议（`plugin.json` + 双根发现 + 热插拔，见 [docs/guides/plugin-protocol.md](docs/guides/plugin-protocol.md)）：① 内部模块已完全插件化、可独立分发；② 灵汐作为插件嵌入外部宿主（游戏引擎、VS Code 等）的方向仍按规划推进 |

### 0.1.0 ✅ 已发布

首次公开版本。核心架构落地：

- ✅ 插件化管道引擎
- ✅ 多层 Agent 协作（主管 / 编排 / 执行）
- ✅ **配置化** —— Agent / 管道 / 工具 / 约束 / Schema 全部 YAML 驱动，配置即产品；支持 `hot_swap` 热替换（快照-替换-健康检查-回滚）与 `hot_reload` 配置热重载
- ✅ 40+ 内置工具（实际 41 个 tool.py 实现）
- ✅ 多层记忆系统（情景 EPISODE + 语义 SEMANTIC）
- ✅ 4 种路由信号（next_llm / next_tool / end / wait）
- ✅ **审批交互闭环** —— 人工审批（choice / conversation 模式）+ 管道暂停/恢复 + 反馈注入；任务状态机含打回重做
- ✅ **强制评估系统** —— 任务提交时须同时提交评估指标（acceptance criteria），管道退出后强制门控转入评估、按指标审查；指标全过才 COMPLETED，失败重试耗尽则 FAILED（Agent 不主动评估也会被强制重跑）
- ✅ **定时器/触发器系统** —— Cron 定时触发、事件触发、间隔触发，无人值守运行
- ✅ **复盘系统** —— `trigger_review` → `review_agent` → 经验报告，自进化闭环
- ✅ **工作区隔离** —— 文件夹隔离 + Docker 容器隔离 + git worktree 多任务分叉
- ✅ **Skill 能力集成** —— 可加载可复用的技能（skill）包，按需注入 Agent，扩展领域能力
- ✅ 8 套前端主题（5 预设 + 3 动态）
- ✅ MCP 协议基础支持
- ✅ 多通道（Web / CLI）+ HTTP API
- ✅ Docker Compose 一键部署

> 0.1.0 是后续所有版本的依赖基线。

---

### 0.2.0 ✅ 已发布（2026-08-01）—— 核心地基层

**主题**：率先把核心能力做透——都是后续版本的地基。本版本以**架构设计与调试**为主，少加新功能。

#### 内核边界（本版本确立）

灵汐采用「**最小内核 + 全插件**」架构。内核只保留为插件服务的基础底座：

| 层 | 内容 | 说明 |
|---|---|---|
| **内核（最小底座）** | 管道引擎 + 多租户 + 配置系统 + 日志系统 + 插件加载器 | 为插件提供执行环境、租户隔离、配置与生命周期管理 |
| **插件（全部，含内置）** | 通道层 / 记忆 / 评估 / 工具 / Agent / 审批 / 连接器 / 触发器 / Skill / 数字人 / 游戏接入 …… | 一切能力都是插件；内置插件随发行提供，第三方插件可装卸 |

> 多租户是**内核**,不是插件——它和管道引擎同级,为插件提供「为谁执行、能用什么」的租户上下文。插件感知租户(用租户的配置/凭据),但不负责隔离(隔离由内核完成)。

#### 功能清单（0.2 已交付）

- **最小内核 + 一切皆插件**：内核只是执行基座（管道解释执行、能力注册表、插件装载、存储、会话/租户/HTTP 基础设施）；LLM 调用、记忆、评估、审批、触发器、工具、通道、主题、乃至 Agent 配置的加载都由插件承载——改任何业务行为 = 加/改插件或配置，不动内核。
- **管道引擎**：引擎只解释一份管道 YAML（`config/pipelines/autonomous.yaml`，所有 Agent 共用，差异由 Agent 配置体现），维护共享 `state` 与循环体调度（init → main[prepare/core/post] → exit）；出口转移由路由 DSL（`when`/`then`/`set`）在加载期编译、运行时零解析；管道配置热重载（mtime 指纹，改完无需重启内核）。
- **统一插件协议**：一个插件 = 一个目录 + 一份 `plugin.json`（声明即注册、双根发现、加载期校验、`deny_unknown_fields`）；watcher 全链热生效（新插件自动发现注册、manifest 变更重注册、Python 代码改动 respawn）；工具 / 连接器 / 通道 / Agent 配置加载等内部模块全部收敛到同一协议。
- **宿主三轨**：Python sidecar（独立进程，MCP over stdio，uv venv 隔离，懒启动/空闲回收/崩溃自愈）／Rust cdylib 原生（进程内零 IPC，高频管道步骤晋升轨）／external MCP（零代码直连第三方 MCP 服务）。
- **工具系统**：26 份工具 manifest（18 个自研 + 8 个预置外部 MCP 接入，58 个工具声明）；统一契约 `input_schema` + `output_schema` + `render`（执行后 fail-closed 校验、前端按 render 意图渲染）；LLM 可见面经「启用档案 → 能力注册 → Agent `tool_ids` 白名单」三层过滤。
- **流式协议**：LLM 正文/思考/工具调用增量统一为 8 事件块协议（`block_start` → `text_delta` / `reasoning_delta` / `tool_call_delta` → `block_end` → `usage` → `finish`），单一真值源 `config/kernel_capabilities/streaming.json`；插件可声明 `capabilities.streaming` 发射自定义事件。
- **任务域**：任务创建/派发统一走 `task_submit` 工具（前端任务面板 = 该工具的参数表单，同一通道）；提交即带评估指标（acceptance criteria），强制评估闸门——无评估证据不落 completed；任务状态单一真值 = pipeline state。
- **项目（Project）**：方案规划 → 阶段执行 → 人类审查 → 完成验收闭环；项目 = 用户创建的真实文件夹 + 登记，作为任务树分组锚点；子任务经 `project_id` 自动继承挂靠。
- **审批闭环**：choice / conversation 双模式（human-interaction 工具阻塞等待用户响应）+ 审批结果反馈注入 + 任务打回重做，构成"生成 → 审批 → 反馈 → 迭代"质量闸。
- **记忆系统**：`hindsight_memory` 插件承载（`hindsight.retain` 沉淀 / `recall` 检索 / `reflect` / `summarize` / `import_document` 文档导入），LLM 经 `memory` 工具读写。
- **隔离与工作区**：任务默认运行在独立工作区（默认文件夹隔离 `workspace/{task_id}`，高风险执行路径 Docker 容器隔离；多任务场景 git worktree 分叉独立工作目录）。
- **触发器**：定时（Cron）/ 事件 / 间隔触发，`trigger_setup` / `trigger_review` 工具注册管理，可绑定特定管道，无人值守运行。
- **Agent 体系**：Agent = `config/agents/` 下的 YAML（人设、提示词、工具白名单、约束全配置化）；`agent_id` 即执行上下文键，由管道插件按键展开（含工具面）；主管（L1）→ 编排（L2）→ 执行（L3）多层协作。
- **配置与热生效**：ConfigCenter 单一真相源（remote > env > yaml > manifest > hardcode）；Agent 配置 mtime 热生效、管道配置热重载、插件全链热生效，改动无需重启。
- **存储**：SQLite（默认 `agentos_kernel.db`，driver 化可切换；runs / messages / traces / blobs 四表 + pipeline_state）。
- **主题与前端定制**：7 套编译期预设主题 + 3 套动态 JSON 主题 + 插件 `contributes.themes` 主题/皮肤（skin）；配置可视化（YAML 字段自动映射表单控件）、插件设置页、ui_schema 前端贡献面。
- **多租户隔离地基**：TenantContext 贯穿管道执行（含异步路径）+ 数据访问统一咽喉点按 `tenant_id` 强制过滤，单租户场景无感；多租户运营（创建/切换/RBAC/凭据保险库）留待 0.5.0。

> 架构变化：0.1 的 Python 单体后端由 Rust 微内核 + 插件生态取代；独立 CLI 入口裁撤，仅存 `channel_cli` 插件壳。

---

### 0.3.0 🎯 跨平台 + 打包交付

**主题**：让框架"装得上、跑得动、用得顺"——打包、跨平台与首次上手体验一体化交付。

#### 打包设计要求

| 类别 | 条目 | 说明 |
|------|------|------|
| 依赖收集策略 | **目录扫描自动生成清单**，而非硬编码 hiddenimports | 复用 0.2.0 的插件发现逻辑，把 `tools/builtin/`、`plugins/input|output/`、`connectors/` 下所有 `.py` 自动转成 hiddenimports；`config/**/*.yaml` + `frontend/public/themes/*.json` 转成 datas |
| 双插件根兼容 | 内置插件（只读）+ 用户插件目录（可写）均纳入收集 | 与 0.2.0 双根约定一致，frozen 后用户目录仍可写 |
| 热重载目录重定向 | `config/` 热重载目标指向 frozen 包外的用户目录 | 复用 `hot_reload.py` 已有的 ConfigCenter/watchdog 双模式切换 |

#### 跨平台支持

| 类别 | 条目 | 说明 |
|------|------|------|
| Windows 原生支持 + CI | Win10/11 x64 + ARM64 | 路径分隔符、注册表、信号、进程模型适配；CI 矩阵自动构建 |
| Linux 多发行版 | Ubuntu / Debian / RHEL / Alpine | systemd / openrc / Docker 多形态交付 |
| macOS 支持 | Intel + Apple Silicon Universal Binary | Rosetta 兼容路径；codesign + notarize |
| 移动端支持 | Android（Android 12+） | 端侧能力适配、电池与后台模型策略；与其他桌面平台一并正式支持（iOS 后续评估） |

#### 打包成可执行程序

| 类别 | 条目 | 说明 |
|------|------|------|
| Python 侧打包 | **PyInstaller 单文件 / 单目录** 双形态 | 端到端依赖收敛；产物自洽；冷启动优化 |
| 桌面客户端打包 | **Electron 桌面客户端**（与本版本可视化安装器联动） | 一份代码同时打包 Win/macOS/Linux 安装包 |
| 系统安装包 | msi / dmg / deb / rpm / AppImage | 走标准包管理器或双击安装 |
| 分发安全 | 产物签名 + 校验码 | 避免杀软误报与中间人篡改；SBOM + 依赖锁定 |

#### 零配置启动

| 类别 | 条目 | 说明 |
|------|------|------|
| 默认参数自适应 | 90% 场景开箱即用 | 智能推断；无需配置文件 |
| 一键 `init` 命令 | 自动生成最小可用项目骨架 | 交互式 + 非交互式双模式 |

#### 可视化安装

| 类别 | 条目 | 说明 |
|------|------|------|
| GUI 安装器（复用 Electron 客户端） | 检测环境 / 下载依赖 / 写入 PATH / 注册服务 | 一次扫描所有先决条件 |
| 安装进度可视化 | 进度条 + 阶段提示 | 不再"黑盒安装" |
| 安装后冒烟自检 | 自动跑一次 happy-path 验证 | 失败给出可点击的修复链接 |

#### 错误提示友好化

| 类别 | 条目 | 说明 |
|------|------|------|
| 分级错误码 | **E0xx 用户可操作 / E1xx 开发者关注 / E9xx 内部故障** | 错误码携带可执行的修复建议 |
| 错误页直达文档锚点 | 一键跳转到对应排错章节 | 错误信息 + 文档深链联动 |

#### 用户引导

| 类别 | 条目 | 说明 |
|------|------|------|
| 交互式 Tutorial | 终端 + 桌面 GUI 双入口 | 第 1 个会话即完成"hello world → 实用示例" |
| 进度仪表盘 | 当前 Pipeline / Agent / 任务可视化 | 任务状态一目了然 |

#### CI/CD

- 多平台矩阵构建（GitHub Actions 等价 CI），Win × Linux × macOS × Android 并行构建
- 灰度发布与回滚通道：0.3.x → 0.4.x 平滑演进
- 产物下载站 + 就地升级通道

**通过标准**：

- 四个主平台（Win/Linux/macOS/Android）均有 `pip install` / `npm i` / 双击安装 三种入口均可落到完整功能的"happy path"跑通记录
- 主分支 CI 通过率 ≥ 95%；产物可下载可复现
- 单文件可执行包冷启动 < 5s（X86_64 工作站参考机）
- 打包后新增内置插件/工具无需改打包配置（依赖收集策略验证通过）
- 新用户从"零安装经验"到"跑通第一个示例" ≤ 5 分钟（含下载）；错误信息可读性评分 ≥ 4.0/5.0

---

### 0.4.0 🎯 可用性：预置能力 + 用户向导 + 配置可视化 + 性能

**主题**：让"非工程师"不仅装得上（0.3.0 已解决），更开箱有用、全程可视化、好用且快。

> 0.3.0 解决"装得上、跑得动"，0.4.0 解决"开箱有用、完全可视化、好用快"。这是对愿景「配置即产品」「超级终端」的直接承接。

#### 预置技能包与 Agent 团队

> 零门槛不只是"装得上"，更要"开箱有用"。随桌面客户端首发一批**精选预置办公技能**和**现成 Agent 团队**，非工程师用户首次启动即可直接调用，无需自行编排。

| 类别 | 条目 | 说明 |
|------|------|------|
| 办公技能包 | 文档处理（Word/Excel/PPT/Markdown 互转、摘要、翻译） | 复用 `docx`/`pdf` 技能；封装为可一键调用的预设 |
| 办公技能包 | 邮件 / 日历 / 会议纪要 | 邮件起草与会后纪要自动生成；与 IM 通道联动 |
| 办公技能包 | 数据处理（表格清洗、图表生成、SQL 辅助） | 面向运营/分析的轻量数据技能 |
| Agent 团队 | 项目管理团队（PM + 开发 + 测试 + 评审） | 现成的编排 + 执行角色组合，开箱即用 |
| Agent 团队 | 内容创作团队（策划 + 写作 + 校对 + 配图） | 网文/自媒体/营销文案的端到端流水线预设 |
| Agent 团队 | 调研分析团队（检索 + 摘要 + 报告） | 输出结构化研究报告，可直接交付 |
| 交付形态 | 技能/团队作为「模板」分发 | YAML 模板 + 一键导入；社区可贡献与分享 |

#### 用户向导

| 类别 | 条目 | 说明 |
|------|------|------|
| 首次启动向导 | 引导式问答生成首个可用 Agent | 从"我想要什么"到"可运行的 Agent"全流程引导 |
| 场景模板 | 按场景（办公 / 开发 / 创作 / 角色扮演）推荐起点配置 | 选模板 → 微调 → 即用 |
| 配置校验 | 向导内实时校验配置合理性 | 错配即时提示，不等到运行报错 |

#### 配置全面可视化（超级终端基础）

| 类别 | 条目 | 说明 |
|------|------|------|
| 后端配置全映射 | 后端全部可配置项 → 前端可视化控件 | 现有 YAML 字段自动映射表单控件（YAML-to-form），做到完全可视化可配置 |
| 前端能力由后端配置长出 | 后端能力定义 → 前端自动生成对应界面 | 超级终端愿景的基础能力：现有已有基础但不全，本版本彻底打通（调试齐全） |
| 可视化编排 | Agent / 管道 / 工具链的可视化组装与预览 | 拖拽式编排，所见即所得 |
| 无代码交付 | 普通用户全程不写代码/YAML 即可产出可用 Agent 产品 | 呼应愿景「配置即产品」 |

#### 性能优化

| 类别 | 条目 | 说明 |
|------|------|------|
| 管道执行效率 | 插件链调度优化、减少不必要的 LLM 往返 | 降低延迟与 Token 消耗 |
| 缓存命中优化 | prompt cache、检索结果缓存的命中率提升 | 复用现有「稳定头部 + 动态尾部」缓存策略 |
| 冷启动与内存 | 启动速度、内存占用优化 | 与 0.3.0 打包产物协同 |

#### 文档体系完善

| 类别 | 条目 | 说明 |
|------|------|------|
| 四象限文档 | docs/{quickstart, howto, reference, explanation} | 符合 Diátaxis 框架 |
| 全 API 自动生成参考 | 类型标注 → 站点化文档 | 与代码同源；CI 中"未文档化的 API 不允许合并" |
| 示例库 | 10+ 端到端工程模板 | 直接 clone 即可运行 |
| FAQ / Troubleshooting 知识库 | 高频问题沉淀 | 站内搜索 + 全文检索 |

**通过标准**：

- 至少 3 个办公技能包、3 套 Agent 团队随桌面版首发且文档完整
- 后端全部可配置项均有对应前端可视化控件，无遗漏
- 前端能力可由后端配置完整长出（超级终端基础打通）
- 普通用户无需手写任何 YAML/代码，通过向导 + 可视化编排即可产出可用 Agent
- 关键路径性能（首响延迟、Token 消耗）较 0.3.0 有可量化提升
- 文档覆盖率 ≥ 95%；入门流失率（首次启动后 24h 回访率）较 0.3.0 提升 ≥ 30%

---

### 0.5.0 🎯 多用户与能力隔离

**主题**：从单用户走向多用户——每用户独立的插件/配置/记忆/人设组合，组成不同能力。

> 多租户隔离地基在 0.2.0 已落地（TenantContext 穿透 + 数据访问咽喉点 + tenant_id 过滤 + 默认租户 fallback），本版本补运营层：多租户创建/切换管理 + RBAC + 凭据保险库 + 配额。隔离机制不返工，只做增量功能。

#### 租户与隔离

| 类别 | 条目 | 说明 |
|------|------|------|
| RBAC 权限模型 | 角色（管理员 / 成员 / 访客）× 组织（团队 / 部门） | 控制谁能装插件、谁能审批、能看哪些会话 |
| 数据隔离 | 记忆 / 会话 / 制品 / 人设按 tenant_id 隔离 | A 看不到 B 的数据（0.2.0 已落地隔离过滤，本版本随多租户管理开放多租户场景） |
| 凭据保险库 | 每租户独立 API Key / OAuth Token | 插件执行时按租户注入，不串 |

#### 能力组合（插件 × 多租户的交集）

| 类别 | 条目 | 说明 |
|------|------|------|
| 每用户插件组合 | 每用户启用自己独特的插件集合 + 配置 | 组合成不同能力（用户 A 有邮件技能，用户 B 有绘图技能） |
| 租户插件目录 | `tenants/{tenant_id}/plugins/` | 某用户独有的插件落到此目录（0.2.0 已定契约） |
| 插件市场分享 | A 用户装的插件可分享/分发给 B | 经插件市场（0.2.0 注册中心）流转 |

#### 部署形态（混合，同一套代码）

| 形态 | 适用 | 隔离方式 |
|------|------|----------|
| 单实例多租户 | 小团队 / 共享部署 | 同进程靠 tenant_id 软隔离（数据过滤 + 权限） |
| 独立实例 | 大客户 / 强隔离 | 每租户独立进程/容器，数据物理隔离（TenantContext 退化为单租户） |

**通过标准**：

- RBAC 权限模型落地，角色/组织可控
- 记忆/会话/制品按租户隔离，跨租户不可见
- 单实例多租户与独立实例两种形态均可部署
- 每用户可拥有独立的插件组合与配置

---

### 0.6.0 🎯 表现力 I：角色扮演 + 人设系统

**主题**：让灵汐从"任务型助手"也能当"酒馆"——可扮演、可沉浸、直接复用酒馆角色卡生态。

> 本版本是「表现力」主线第一段（地基）。现有 Agent 是任务调度型，缺对话/角色能力；前端会话没有「类型」概念、没有头像/形象展示、Agent 配置无导入入口。0.6.0 补齐这层**数据与前端地基**：角色卡片解析、沉浸式对话 UI。人设协议直接复用酒馆，不自研。形象（数字人）与引擎内 NPC 建立其上，放到 0.7.0。

#### 角色卡片导入与解析

| 类别 | 条目 | 说明 |
|------|------|------|
| 酒馆卡片解析 | 解析 SillyTavern 角色卡（PNG 内嵌 chara JSON + 纯 JSON 两种格式） | PNG 隐写提取（复用已声明但未启用的 Pillow 依赖，读 tEXt/iTXt chunk → base64 → JSON） |
| 字段映射 | 卡片字段 → Agent 配置 | `description→system_prompt`、`scenario→static_vars`、`world_info→知识库(knowledge_service)`、`personality→soft_constraints` |
| Schema 扩展 | Agent schema 增补对话型字段 | `first_mes`(开场白)、`mes_example`(对话样本)、`alternate_greetings`(多开场白)——现有 schema 无此概念，需扩展 |
| 导入入口 | 前端「导入角色卡」UI | 复用主题系统的「JSON 导入→校验→去重→存储」范式（`importTheme` 三段式）；JSON 先行，YAML 后续 |

#### 人设系统（直接复用酒馆生态）

人设协议与角色卡格式**直接复用 SillyTavern 酒馆生态，不自研格式**，可直接接入海量社区角色卡资源：

| 类别 | 条目 | 说明 |
|------|------|------|
| 人设协议 | 复用酒馆角色卡格式（character card v2/v3） | 不另起 schema；字段含 name/description/personality/scenario/first_mes/mes_example/world_info 等 |
| 人设来源 | 兼容酒馆社区角色卡资源 | 直接导入社区已流通的 PNG/JSON 角色卡，无需二次加工 |
| 一键切换 | 导入的角色卡即人设 | 角色卡 → Agent 配置后，整套人设/工具约束/主题色一并生效 |
| 扩展字段 | 在酒馆格式之上叠加灵汐专属字段 | 如绑定的工具集、模型选择、主题色——作为角色卡 `extensions` 扩展，不破坏酒馆兼容性 |

#### 沉浸式对话 UI（前端会话改造）

| 类别 | 条目 | 说明 |
|------|------|------|
| 会话类型 | `Session` 增补 `type`/`mode` 字段（普通 / 角色扮演） | 现有会话无类型概念，渲染层走同一套；需加类型 + 按类型分支渲染 |
| 角色形象展示 | 消息头像 URL、角色卡片、形象展示区 | 现有 `MessageItem` 只有 fallback 图标、`AgentIcon` 是 emoji 色块；需支持头像图片。激活半成品 `AgentThemeContext`（已有 avatar/bubble/layout 设计但未接通）作脚手架 |
| 角色级主题 | 角色专属主题色 / 背景图 / 字体 | 复用主题 CSS 变量注入机制，下沉到角色级 |
| 世界观展示 | 世界书/世界观条目面板 | `world_info` 解析后存入知识库，前端提供查看/触发面板 |
| 多角色同台 | 一个会话内多角色轮换、记忆隔离、剧情进度 | 面向互动小说 / 语 C 社区 |

**通过标准**：

- 酒馆角色卡（PNG + JSON）均可导入并正确映射为可用 Agent 配置
- 直接兼容酒馆社区角色卡资源，无需格式转换即可导入
- 角色扮演模式下可展示角色形象、世界观，并跑通多角色沉浸式对话

---

### 0.7.0 🎯 表现力 II：数字人 + 游戏 NPC

**主题**：让有形象的智能体走出浏览器——常驻桌面、进入游戏世界。

> 本版本是「表现力」主线第二段（应用），建在 0.6.0 的角色卡片/人设地基 + 0.3.0 的宿主接入协议之上。形象渲染走 0.3.0 已通通道，人设/音色/世界观复用 0.6.0 成果。数字人/语音以**接口模块当插件接入**现成引擎（Live2D/TTS），不自研渲染。

#### 数字人桌面助理

| 类别 | 条目 | 说明 |
|------|------|------|
| 2D 数字人 | Live2D / Spine 风格形象 | 轻量、跨平台、低显存；适合常驻桌面托盘 |
| 3D 数字人 | VRM/Unity 立绘 + 骨骼动画 | 唇形同步；形象渲染走 0.3.0 已接通的引擎通道 |
| 语音交互 | TTS 语音播报 + ASR 语音输入 | 说话即响应；可选端侧模型保护隐私 |
| 情绪/表情 | 文本情绪 → 表情/动作映射 | 让形象"有反应"，不只读稿 |
| 桌面集成 | 常驻悬浮窗 + 唤醒词 + 系统托盘 | 复用 0.3.0 悬浮窗形态 + 0.4.0 Electron 外壳 |
| 人设绑定 | 数字人形象 ↔ 0.6.0 人设/音色一键绑定 | 一个角色卡 = 一套形象 + 人设 + 语音 |

#### 游戏内 NPC 接入

| 类别 | 条目 | 说明 |
|------|------|------|
| NPC 预设 | 角色 = 人设 + 引擎内置插件 + 行为脚本 | 把灵汐 Agent 配成游戏内 NPC；接入走 0.3.0 已通通道，人设复用 0.6.0 |
| 对话系统 | 内嵌对话 UI / 对话树 + LLM 自由对话混合 | 既支持脚本化剧情，也支持自由问答 |
| 行为边界 | NPC 权限沙箱：禁用危险工具、限制世界观 | 防止 NPC 越权操作宿主游戏 |

**通过标准**：

- 2D 数字人 + 3D 数字人各 ≥ 1 个可在桌面常驻、能语音对话的可用形象
- 至少 1 款游戏引擎中跑通"AI NPC 与玩家自由对话"的最小 Demo
- 数字人形象与 0.6.0 人设可一键绑定

---

### 1.0.0 🎯 稳定版

**主题**：API 冻结、长期支持、生产可用。

#### API 稳定

- ☐ 公共 API 冻结；1.x 内只做向后兼容修复
- ☐ 语义化版本承诺（SemVer）；弃用至少提前 1 个 minor 版本预告
- ☐ 变更日志规约（Keep a Changelog）；强制化

#### 质量门

- ☐ 全量端到端测试覆盖核心路径；用户旅程表 + 影响矩阵对应
- ☐ 性能基线 & 回归基线；CI 卡口固化
- ☐ 安全审计与渗透测试；第三方或内部红队报告

#### 生态

- ☐ 长期支持计划（LTS）：1.x 维护 ≥ 18 个月
- ☐ 插件市场上架审核流程：第三方插件可用、可信、可回滚
- ☐ 国际化（i18n）支持 10+ 语言

#### 核心里程碑（沿用原版）

- ☐ 至少 3 个企业级生产案例
- ☐ 完整的中英文档、视频教程、案例库
- ☐ 社区生态规模：100+ 第三方插件、50+ 任务模板

**通过标准**：

- API 冻结与变更规约对外公开
- LTS 计划对外公告，支持窗口明确
- 至少 3 家外部生产用户在跑

---

## 🧹 已知技术债（欢迎认领）

以下技术债已识别并在 `pyproject.toml` 的 ruff 配置中暂时忽略（`PLR0911`/`PLR0912`/`PLR0915`）。它们是代码质量改进项而非 Bug，欢迎社区认领治理：

| 规则 | 含义 | 当前忽略原因 | 治理方式 |
|------|------|-------------|---------|
| `PLR0911` | 函数 return 语句过多（>6） | 需拆分函数 | 提取子函数/策略模式 |
| `PLR0912` | 函数分支过多（>12） | 需拆分函数 | 提取分支为独立方法 |
| `PLR0915` | 函数语句过多（>50） | 需拆分函数 | 按职责拆分 |

**mypy 类型注解**：基线锁现值 0（2026-08-23 CI 治理批清零；`.github/mypy-baseline.txt` 只减不增）。CI 经 `run_gates.py` 作硬门禁。维持基线不回增：新增代码带类型注解，修老错误时随之下调基线。

**认领方式**：搜索对应规则码（如 `# noqa: PLR0912`），逐个函数重构，移除 noqa 注释后确保 CI 通过。

---

## 🔮 未来可选方向（条件触发）

以下方向**技术路径已验证可行**，但**触发条件尚未满足**，暂不排期。条件成熟后可直接按既定方案推进，无需重新调研。

### 接入 DeepSeek Harness（DSH）插件生态

**触发条件**：DSH（`github.com/deepseek-ai/deepseek-harness`，MIT）出现规模化的**第三方插件生态**（非官方 monorepo 内部包）。当前 DSH 24 万 star，但其 54 个 package 仍是官方内部 workspace 包，npm 上第三方 `@someone/dsh-*` 插件尚未形成规模。**生态空置期投入"嵌套 runtime 转接层" = 基础设施先于需求，沉没成本高，故暂缓**；但"适配层移植"路线（见下）不受此限——前端视觉组件 MIT 直接可抄（P2），后端工具/策略类按需移植（已在进行的 spill_guard / 压缩优化即 DSH 决策的适配层移植实例）。

**两条接入路径（按"搬运行时 vs 翻译接口"分）**：

| 路径 | 做法 | 适用 | 前置 |
|------|------|------|------|
| **适配层移植（轻，推荐先行）** | 不搬 Cordis 运行时，翻译"数据/契约接口"：前端 = 视觉组件 + 数据映射层；后端 = 工具/策略逻辑翻译 + 契约映射 | 前端视觉组件（P2 可先行）、后端工具类/策略类插件 | 无，随时可做 |
| **嵌套 runtime（重，等生态）** | fork DSH SDK + RPC 暴露 service，sidecar 宿主驱动 DSH 进程 | 深度绑定 Cordis 的插件（self-modification、复杂 provider） | 等第三方生态成熟 |

**可行性结论（已调研，基于 DSH 源码）**：

DSH 是基于 Cordis（Koishi 同源 DI 框架）的 TypeScript agent harness，范式为"一切皆插件"。其插件按形态分 6 类，与本项目对照及转接可行性如下：

| DSH 插件形态 | 本项目对应物 | 转接可行性 |
|------|------|------|
| 钩子插件（`ctx.on` 事件订阅，68 种事件） | 管道 Input/Output 插件（priority 顺序） | ✅ 语义可对，核心钩子（`tools/pre-execute`/`tools/post-execute`/`agent/pre-step` 等）可双向翻译为 state_updates / RouteSignal |
| 工具插件（`ctx.tools.register`） | 工具插件 | ✅ 同构；且 DSH 工具多为标准 MCP，已可通过 `external_mcp` 直连 |
| 能力 Provider（`ctx.provide('shell'/'fs'/'llm'...)`） | 系统插件（manifest `provides` + capability_caller） | ✅ **可转接——fork DSH SDK 加 RPC 方法暴露 service**（见下方方案） |
| Bundle/Profile 补丁（`cordis.patch.yml`） | pipeline yaml + Agent preset | — 概念等价，无需转接 |
| Slot/UI 注入（`ctx.slots`） | contributes + WidgetRegistry | — 概念等价，实现不同 |
| 自修改运行时（`cordis_define/run`） | hot_swap（持久化 + 回滚） | — 本项目实现更工程化，无需转接 |

#### 路径 A：前端视觉组件适配层（P2，无需等生态）

对应 `docs/tasks/task_plugin_frontend_customization.md` 任务 5。三层可拿性：设计层（design-tokens / 主题机制）直接抄；组件层（渲染卡 / markdown 全家桶）移植改写；运行时层（Cordis / slot / 事件投影）不拿。适配层 = 纯函数数据结构映射（DSH toolCall/toolResult → TOOL_RESULTS/messages），试点组件：DiffBlock / CodeBlock / JsonTree / read-row 等 8 个 🟢 低成本组件。备选方案 B：Webview 包装器（组件零改动打包进 WebviewWidget）。

#### 路径 B：后端功能性插件适配层（同前端道理，条件成熟后）

后端功能性插件与前端同理：**能翻译接口的走适配层，深度绑定 Cordis 的才走嵌套 runtime**。

- **工具类**：DSH 工具多为标准 MCP 或纯函数 → 已可通过 `external_mcp` 直连（smithery 等已启用 streamable_http）；非 MCP 工具走 TS→Python 逻辑翻译 + ToolSchema 契约映射（灵汐工具契约对照 DSH `{name, description, parameters}`）
- **策略类**（压缩/spill/审批/超时）：决策适配移植已在推进——`task_spill_guard`、`task_compression_optimization`、`task_observability` 即 DSH 对应决策的适配层落地；插件本体移植的翻译表 = DSH 事件（`tools/pre-execute` / `post-execute` / `agent/pre-step`）↔ 灵汐 state_updates / RouteSignal
- **深度绑定 Cordis 的**（自修改运行时、复杂 capability provider）：仍走下方"嵌套 runtime"方案（fork SDK + RPC）

**核心技术障碍与解法（嵌套 runtime——路径 B 的"深度绑定"子集专用）**：

DSH 插件是 **in-process Cordis 函数**（非服务/sidecar 形态），强依赖 `@deepseek-ai/cordis` 运行时 + `ctx.on/inject/provide/get` 机制，无法"拔出来"直接装入本项目的 sidecar。**对深度绑定 Cordis 的插件，唯一可行的转接路径是"嵌套 runtime + 改 SDK 暴露 service"**：

```
本项目 sidecar 适配器
    ↓ JSON-RPC（自定义方法，如 shell/exec）
fork 改造的 DSH SDK server（packages/sdk/server/src/server.ts）
    ↓ this.ctx.get('shell').execute(...)   ← 进程内拿真 service
DSH 进程内的真实 service / 装载的第三方 cordis 插件
```

- **SDK 可改**：DSH MIT 开源，`server.ts` 的 `handleRequest` 是 switch，加 `case 'shell/exec'` 即可暴露任意 ctx service（`this.ctx` 在构造函数已注入）。
- **数据可序列化**：DSH service 方法参数多为普通数据类型（如 `ShellExecRequest` 的 command/workdir/timeoutMs/env），跨进程序列化无障碍；含不可序列化类型（Stream/AbortSignal/函数回调）的方法需包装层（stream 转 chunk 事件、signal 转 timeout+cancel）。
- **钩子可翻译**：DSH 的核心钩子点（约 6 个）语义清晰（waterfall 模式带 `next()`），可在本项目引擎对应时刻合成 DSH 事件触发，捕获其 Decision 翻译为 state_updates。

**落地步骤（条件成熟后执行）**：

1. Fork DSH，在 `packages/sdk/server/src/server.ts` 加 RPC 方法暴露目标 service（shell/fs 等），改动集中在单个 `rpc-export.ts` 便于 rebase。
2. 配最小 `cordis.yml` 挂载目标 provider 及其依赖链（如 `shell-sandbox` 依赖 `sandbox`+`subprocess`+`credentials`）。
3. 本项目新增 `plugins/shared/system/dsh_runtime/` system 插件，作为 sidecar 宿主驱动改过的 DSH 进程。
4. 最小验证：跑通一个真实 DSH 钩子插件（如 `repeat-tool-reminder`）和一个 service（如 `shell.exec`）在本项目内的转接，固化转接模式。
5. 扩展至核心钩子全集 + 可序列化 service 全集。

**维护成本**：DSH 处于 developer preview，明确会有 breaking changes，fork 需跟随 rebase。改动越集中（单文件）冲突越小。

**参考**：DSH 源码获取：`git clone --depth 1 https://github.com/deepseek-ai/deepseek-harness.git`。关键文件：`packages/sdk/server/src/server.ts`（SDK server，改这里）、`packages/core/tools/src/index.ts`（Events 接口，钩子点权威清单）、`packages/shell/shell/src/types.ts`（service 契约样例）。

### subagent 桥接外部 agent（spawn Codex / Claude Code）

**触发条件**：本项目多 agent 编排（L1/L2/L3）稳定后，且用户场景出现"需要调用外部专业 agent（Codex/Claude Code）执行子任务"的真实需求。当前本项目子 agent 只能调度自身 agent，无法 spawn 外部 agent 进程。

**可行性结论（已调研）**：DSH 的 `subagent` 是平铺的 provider 家族（`ctx.subagents`），支持多个 provider 共存，其中 `subagent-codex` / `subagent-claude-code` 能 spawn 真正的 Codex / Claude Code 进程作为子 agent。这是 DSH 唯一明显领先于本项目的多 agent 能力。

**与 Capability Provider 抽象的关系**：此事依赖 `task_architecture_borrow_from_dsh.md` 任务 1（Capability Provider）落地——subagent 本质也是一种 capability（多 provider：in-process/fork/codex/claude-code）。Provider 抽象就绪后，桥接外部 agent = 新增一个 provider 实现。

**落地方向（条件成熟后）**：
1. subagent capability 契约定义（start / startContinuable / followup / list-children）
2. 本地 provider（in-process / fork）—— 对标 DSH 的 spawn-in-process / fork-in-process
3. 外部 agent provider（codex / claude-code）—— 通过各 agent 的 SDK/CLI spawn 子进程，经 ACP（Agent Client Protocol）或各自协议通信
4. tool-subagent 工具——把委派能力暴露给模型

**参考**：DSH 源码 `packages/subagent/`（service 契约 + 各 provider 实现）、`packages/acp/`（ACP server，跨进程 agent 通信协议）。

### 兼容 ACP（Agent Client Protocol）实现跨 agent 互通

**触发条件**：用户场景出现"需要和其他 agent 框架（DSH / 任意 ACP 兼容 agent）互通或委派任务"的真实需求。当前本项目 agent 只能和自身生态内的 agent 协作。

**可行性**：ACP 是开放协议，实现一个 ACP server/client 即可让本项目 agent 与 DSH（及任何 ACP 兼容 agent）互相通信/委派。这是"借 DSH/外部 agent 生态"的最轻量路径——不 fork 它的 runtime，只说它的协议。比"fork DSH SDK 接生态"便宜得多。

**落地方向**（条件成熟后）：
1. 实现 ACP server（把本项目的 agent 能力暴露给外部 ACP client）
2. 实现 ACP client（本项目 agent 能调外部 ACP server，如 DSH/Codex/Claude Code 的 ACP 接口）
3. 作为 subagent capability 的一个 provider（`subagent-acp`）接入

**参考**：DSH 源码 `packages/acp/`（ACP server 实现）、`@agentclientprotocol/sdk`（ACP 标准 SDK）。与上方"subagent 桥接外部 agent"协同——ACP 是桥接的具体协议之一。

### 工程基础设施补全（测试 / CI / 质量门禁）

**触发条件**：0.2 迁移收尾后，作为"用户可信度"的硬基础。mypy 基线已清零（2026-08-23，`.github/mypy-baseline.txt` 现值 0、只减不增），覆盖率棘轮门禁已建成（Python 64.0 / Rust 86.0 基线；2026-09-01 用户裁定整体基线暂时挂起观察，`run_gates.py --skip`）。剩余工作是持续收紧与恢复硬闸门。

**已落地（2026-08-15，机械门禁部分）**：统一机械门禁入口 `scripts/run_gates.py`（27 个门禁单一事实源，CI 跑穷尽集 + 本地 fast 廉价检查，每个承诺都有非零退出命令）+ 覆盖率豁免重型套件 `scripts/coverage_exempt.py`（94 插件子进程冒烟矩阵免插桩、与插桩 gate 并行，实测对父进程覆盖率零贡献；覆盖率地板 44% 只升不降 + 失败数基线锁只减不增自动守护名单与车道）+ electron 桌面壳编译门禁（新增 CI job）+ 修复一批门禁接入后机械暴露的既有破损（python-lint mypy 路径 bug、SDK 5 处类型错误、10 处非法追溯标记、47 个新测试文件未标记、kernel fmt 漂移、root 死 test 脚本）。详见 `docs/working/机械门禁统一入口与覆盖率豁免.md`。

**现状对比**：DSH 有完整工程基础设施——oxlint + knip（未用依赖检测）+ jscpd（重复码检测）+ publint + lefthook + Vitest e2e/snapshot test，且 `test:coverage` 是 CI 硬门禁（per-file 100%）。本项目在测试/CI 门禁上明显弱于 DSH，这直接影响用户/开发者对项目的信任度。

**落地方向**：
1. mypy 类型错误已清零（2026-08-23，基线锁现值 0，CI 硬门禁）；维持基线只减不增不回退
2. 覆盖率门禁：整体基线（Python 64.0 / Rust 86.0）已建成，2026-09-01 起暂时挂起闸门（`--skip`，插桩度量照跑）；恢复后沿棘轮向 80% 收紧，对标 DSH 的 per-file 100%；前端 vitest thresholds 已设
3. 引入 knip（未用依赖/导出检测）、jscpd（重复码）等质量工具
4. e2e/snapshot test 基础设施（对标 DSH 的 vitest e2e + keyless snapshot replay）

**优先级**：🟡 P1——不是"可选"，是"必须"，只是排在 0.2 迁移收尾之后。

### 插件契约完善（0.2 定型后）

**触发条件**：0.2 核心地基层定型（路由 DSL / capabilities 交互面 / contributes 定型）之后。当前契约已具备基础：`plugin.json` capabilities（tools/resources/route_signals/lifecycle_hooks/services）、工具契约（含 output_schema/render，消费端见 `task_dsh_plugin_adapter.md` 任务 1）、管道插件接口（Input/Core/Output + RouteSignal 四信号）、contributes 全景——**这些 0.2 定型后尽量不再动**。

**核心原则（契约冻结）**：0.2 定型后，接口契约**能不动就不动**——版本更新必须考虑旧插件兼容，频繁改契约会让系统往更复杂方向走。动契约 = breaking change，必须走 ADR 记录 + 兼容机制（旧插件继续可用）。

**0.2 后完善的索引**（到时机再展开，不做前置详细任务文档）：
1. **插件契约设计指南**：契约三问（读什么 / 吐什么 / 挂哪）+ 各插件类型立契模板 + 表单级 / 条件循环级 / 任意语言级三个入口（同一契约，三种填写方式）
2. **契约校验器**：JSON Schema 合集，新插件注册机器裁决（缺失声明拒绝 + 报缺什么）；与 `task_dsh_plugin_adapter` 任务 1 的 output_schema 消费端共用契约事实
3. **写插件 skill 模板化**：衔接 `.zcode/skills`（resource-tool-create 等），按"契约三问 + 领域惯例"重组织——skill 教"先立契"的思考流程，不教语言细节
4. **自进化衔接**：agent 按契约三问立契 → 实现 → 校验器放行 → 复盘闭环

**优先级**：🟢 P2——0.2 定型后完善；当前只做不破坏契约的事（如 output_schema 消费端、spill_guard）。

### runs 表退役：执行上下文收敛进 pipeline_state（0.2 后）

**触发条件**：0.2 定型后，执行上下文（status / 起止时间 / 挂起恢复坐标）全部收敛进 `pipeline_state`（一管道一行），`runs` 表退役。当前 `runs` 表每轮一条 run 记录（status / created_at / ended_at / metadata），与 `pipeline_state`（跨轮累计字段）职责重叠；traces 已承载全部执行增量，run 级状态是唯一未入 state 的残留。

**现状（2026-09-02 已查证）**：所有消费面（`resume_pipeline` 恢复三分、`suspend_pipeline`、`get_run_status` 停止信号轮询、task_manage elapsed 计算、前端管道页快照）**只读当前状态，无"逐次尝试结局历史"消费面**；`current_branch`/`current_seq` 为死字段（实库恒 `('main', 0)`，零读面）。收敛后恢复语义从"查最新非终态 run"退化为"查单行状态"，更简单；代价是丢失"第 N 次尝试以什么状态结束"的历史（traces 无 status 字段，无法回放）。

**落地方向**（条件成熟后执行）：
1. `pipeline_state` 增 status / started_at / ended_at / 挂起坐标键（一管道一行，upsert）
2. 引擎收尾按 pipeline_id 定位（消除"翻活旧 run 无人收尾"的幽灵 running 模型问题）
3. 消费面迁移：`resume_pipeline` 三分 → 单行状态判断；`get_run_status` → 按管道查；task_manage elapsed → 管道级起止；llm 插件停止信号轮询改按管道
4. 审批挂起 metadata（`pending_interaction_request_id` + `suspend_branch_id` + `suspend_seq`）迁入 state 键
5. 迁移脚本：runs 行合并为管道级当前状态行；`runs`/`branches` 表退役（traces 保留）

**优先级**：🟢 P2——0.2 定型后；动存储模型 = breaking change，需 ADR + 迁移脚本。

### 插件包管理器与语言运行时插件（0.2 后）

**触发条件**：0.2 定型后 + 第三方插件出现（有"安装/依赖"真实需求）。当前插件全部本地目录（builtin/user/shared 三类），无安装概念。

**设计（已讨论定案）**：
- **插件包管理器 = 一个跨语言装配机制（非插件）**：解析 `dependencies` → 安装/准备环境 → 版本解析（对标 npm/pip 的插件版）；官方插件商店 = 管理器源（与"第三方 in_process 插件官方统一构建"衔接）
- **每语言一个 runtime 插件**（python_runtime / node_runtime / …）：`config` 钉三样——运行时版本（python 3.11）+ SDK 版本（`agentos_plugin_sdk==0.2.0`）+ 公共依赖清单（litellm/fastapi…，"有什么公共依赖往里加配置就行"）
- **SDK 进 runtime 插件配置**：SDK 版本从代码散落（pyproject/SERVER_VERSION/双端对齐测试）收敛为配置钉死，解决审计 M1 的 SDK 双真相源/漂移；SDK 升级 = 改一个 runtime 插件配置，全员跟随
- **依赖分层**：公共依赖（runtime 插件 config，**≥2 个插件用才进**，防垃圾桶化）+ 私有依赖（插件自身 config.deps，装配时合并）
- **依赖语义区分**（纯文档，现在可写不碰代码）：`dependencies` 指向 runtime 插件 = **环境装配**；指向普通插件 = **服务依赖**（保证被依赖插件先加载）
- `plugin_type` 加 `runtime` 值 = 枚举加值（additive，符合冻结规则：枚举只加值）
- 运行时插件走**声明型**（无进程，仅配置清单），不做执行型（spawn 共享宿主）——实现轻

**优先级**：🟢 P2——0.2 后与插件包管理器一起做；0.2 前不碰（新 plugin_type 值 + 装配语义 = 契约面扩展）。

### 文档国际化（i18n）基础设施

**触发条件**：希望获得国际影响力（不只是国内用户）时。当前文档中文为主，英文 README 存在但不完整。

**现状对比**：DSH 几乎每个文件都有 `README.i18n.yaml` + 中英双语，website 用 VitePress 投影双语文档。本项目英文 README 是单文件，模块级文档未双语化。

**落地方向**：
1. 核心文档（README / ARCHITECTURE / ROADMAP / CONTRIBUTING）中英双语对齐
2. 引入 i18n 文档机制（参考 DSH 的 `README.i18n.yaml` + VitePress，或用更轻的Crowdin/MDX 方案）
3. 代码注释/错误信息的英文覆盖（可选，按需）

**优先级**：🟢 P2——国际化是影响力放大器，但不是功能/架构阻塞项。

---

## 🤝 社区贡献优先级

我们尤其欢迎以下方向的贡献（按优先级排序）：

| 优先级 | 方向 | 说明 |
|--------|------|------|
| 🔴 高 | 工具生态 | 新增有用的内置工具 |
| 🔴 高 | 文档质量 | 改进现有文档、补充示例、翻译 |
| 🔴 高 | 预置技能 / Agent 团队 | 贡献办公技能包或现成 Agent 团队模板（0.4.0） |
| 🟡 中 | 多租户 / 权限策略 | 贡献 RBAC 角色模板、权限策略、隔离方案（0.5.0） |
| 🟡 中 | 酒馆角色卡适配 | 贡献角色卡资源、字段映射规则、扩展字段（0.6.0） |
| 🟡 中 | 数字人形象资源 | 贡献 Live2D/VRM 形象资源（0.7.0） |
| 🟡 中 | 游戏引擎适配 | 引擎内置插件示例与 NPC 预设（0.3.0/0.7.0） |
| 🟡 中 | 性能优化 | 管道执行效率、Token 消耗优化 |
| 🟡 中 | 测试覆盖 | 补充单元测试、集成测试、E2E 测试 |
| 🟡 中 | 多通道适配器 | 新的 IM 平台接入 |
| 🟢 低 | UI 美化 | 主题、动画、可访问性 |
| 🟢 低 | 国际化 | 翻译新增语言 |

详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 📊 决策原则

路线图不是承诺。优先级判断遵循以下原则：

1. **用户价值 > 技术先进性** —— 优先解决真实用户的痛点，而非追逐技术热点
2. **核心深度 > 边缘广度** —— 把管道引擎、Agent 协作做透，比铺更多通道更重要
3. **可演进性 > 一步到位** —— 优先选可扩展的方案，承认"完美设计"不存在
4. **社区共识 > 个人偏好** —— 重大方向变更需在 Discussions 充分讨论
5. **契约冻结 > 灵活演进** —— 0.2 定型后接口契约能不动就不动：版本更新必须兼容旧插件，频繁改契约的短期便利会被长期兼容性负担淹没；动契约 = breaking change，需 ADR + 兼容机制

---

## 🚫 被否方案索引（动手前先查）

> 任何非平凡决策都必须写 ADR（`docs/decisions/`，按日期排序），**被否掉的备选方案强制记录在各 ADR 的 Alternatives Considered 节**。下表只列最容易被人重新提出的架构级被否方向；提功能建议/PR 前请先对照，避免重复讨论已否决的路线。完整归档见 [docs/decisions/](docs/decisions/) 目录（制度说明：[docs/decisions/README.md](docs/decisions/README.md)；2026-08-15 前的 57 个决策点另有时间线索引 [docs/decisions/ROADMAP.md](docs/decisions/ROADMAP.md)）。

| 被否方向 | 现行结论 | ADR |
|---|---|---|
| 插件全 Rust 原生化（不留 Python 轨） | 双轨：Python sidecar 为默认轨，热路径按基准晋升 Rust cdylib | [2026-07-13-sidecar-process-model.md](docs/decisions/2026-07-13-sidecar-process-model.md) |
| 引擎内路由表仲裁 / `delegate`·`fork` 特殊路由信号 | 路由仲裁下沉管道 YAML 路由 DSL（`when`/`then`/`set`），路由信号面整体退役 | [2026-07-14-adr-engine-12-points.md](docs/decisions/2026-07-14-adr-engine-12-points.md) |
| 覆盖率静态地板（等 80% 再收紧） | 基线棘轮只升不降且略高于实测留压力 + 改动行 diff 100% | [2026-08-20-coverage-ratchet-diff-100.md](docs/decisions/2026-08-20-coverage-ratchet-diff-100.md) |
| `channel_api` 统一 HTTP 网关壳 / REST 面收回内核 | 插件自持 `http_endpoints`（`/ext/{plugin_id}/**`），内核 HTTP 面只留核心端点 | [2026-08-21-channel-api-retire-plugin-owned-http.md](docs/decisions/2026-08-21-channel-api-retire-plugin-owned-http.md) |
| LLM 流式旧 4 事件协议（`stream_chunk`/`thinking_*`） | 8 事件块协议（`block_start`/`text_delta`/`reasoning_delta`/…），单一真值源 `config/kernel_capabilities/streaming.json` | [2026-08-22-streaming-protocol-rewrite.md](docs/decisions/2026-08-22-streaming-protocol-rewrite.md) |
| sidecar 自动依赖指纹分桶 / 全插件塞单宿主 | manifest 声明 `host_group` 静态分组合宿 | [2026-08-26-sidecar-co-hosting-host-grouping.md](docs/decisions/2026-08-26-sidecar-co-hosting-host-grouping.md) |
| 中间态 / 飞行中消息落库 | 中间态寄存器：引擎内存两区（A 键值 LRU + B message→step 归属），落库即清 | [2026-08-27-transient-state-register.md](docs/decisions/2026-08-27-transient-state-register.md) |
| 面板/前端独立任务创建入口（`POST /tasks` 类专用口） | 面板 = 工具的参数表单，与 LLM 走同一条 `task_submit` 工具通道 | [2026-08-29-panel-create-via-task-submit-tool.md](docs/decisions/2026-08-29-panel-create-via-task-submit-tool.md) |
| thread / 会话 id 充当执行坐标 | `pipeline_id` 是执行态唯一身份，thread 仅组织集合 | [2026-08-30-pipeline-id-sole-execution-coordinate.md](docs/decisions/2026-08-30-pipeline-id-sole-execution-coordinate.md) |
| 工具失败连击熔断闸门 | 三信号收束闸门（连击门误杀可自愈任务，已退役） | [2026-08-30-retire-tool-fail-streak-gate.md](docs/decisions/2026-08-30-retire-tool-fail-streak-gate.md) |
| 保留系统分配器硬扛 Windows 段堆并发高水位滞留 | 内核全局分配器换 mimalloc（实测滞留根因为段堆线程本地缓存惰性 decommit） | [2026-08-31-mimalloc-global-allocator.md](docs/decisions/2026-08-31-mimalloc-global-allocator.md) |
| FFI 边界跨堆 `free` / 外层装箱归属模糊 | 对称借用协议：`Result<&str,_>` + 实现方自持缓冲，杜绝跨堆释放 | [2026-09-01-native-ffi-cross-heap-free-fix.md](docs/decisions/2026-09-01-native-ffi-cross-heap-free-fix.md) |

---

## 🕒 时间线

```
▓▓▓ 0.1.0 ✅ (已发布)
▓▓▓ 0.2.0 ✅ (已发布) 核心地基层（最小内核+全插件 / 统一插件协议 / 管道引擎 / 工具与流式契约 / 审批 / 记忆 / 触发器 / 多租户契约预留）
░░░ 0.3.0 (跨平台 + 打包交付)
░░░ 0.4.0 (可用性：预置能力 + 用户向导 + 配置可视化 + 性能)
░░░ 0.5.0 (多用户与能力隔离：多租户完整实现)
░░░ 0.6.0 (表现力 I：角色扮演 + 人设系统)
░░░ 0.7.0 (表现力 II：数字人 + 游戏 NPC)
░░░ 1.0.0 (稳定版：API 冻结 + LTS)
```

---

## 💬 参与方式

- 🐛 [报告 Bug](https://github.com/jianchen08/Agent-os-open/issues/new?template=bug_report.md)
- 💡 [功能请求](https://github.com/jianchen08/Agent-os-open/issues/new?template=feature_request.md)
- 💬 [参与讨论](https://github.com/jianchen08/Agent-os-open/discussions)
- 🩭 国内用户：[Gitee Issues](https://gitee.com/jc27/Agent-os-open/issues)
- 📧 邮件：`chenjian1306792950@foxmail.com`

---

> 🌊 **潮汐有信，迭代有时。** 我们相信稳定而持续的进步，比一次性的颠覆更有价值。
