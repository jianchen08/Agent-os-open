# 灵汐 AgentOS 路线图

> 本文档描述灵汐 AgentOS 的**未来演进方向**。路线图是规划性的，会根据社区反馈和优先级动态调整。
> 想影响路线图？欢迎在 [Discussions](https://github.com/jianchen08/Agent-os-open/discussions) 发起讨论。

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
- 🔌 **可嵌入** —— 0.2.0 核心地基层一并做透：管道引擎执行模型与 AdrEngine 重设计（统一插件模型 + 串行循环 + state 契约/隔离 + 引擎回归调度器/账本 + SQLite 四表 + 多分支回滚）+ 第三方插件协议（含内部模块完全插件化）+ 宿主接入 + 前端 Schema 驱动 + 记忆检索/注入补全 + 路由方式收敛 + 多租户契约预留；再跨平台打包分发（0.3.0）
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
> | **管道插件** | ✅ 已上线 | 管道内部的 Input / LLM / Output 插件链，负责上下文注入、推理、后处理与路由信号仲裁 |
> | **系统级插件化** | ✅ 已上线 | 整个系统由可替换部件组成；其中 **Agent / 工具支持热插拔**，通道 / 记忆 / 触发器为可替换模块（非热插拔） |
> | **第三方插件协议** | 🚧 0.2.0 | manifest 驱动的统一插件协议，覆盖两个方向：① 把内部模块（工具/连接器/Agent/通道）完全插件化（按协议封装、可独立分发/热插拔）；② 让灵汐作为插件嵌入外部宿主（游戏引擎、VS Code、视频剪辑器等），也让第三方开发者为灵汐贡献扩展 |

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

### 0.2.0 🎯 核心地基层

**主题**：率先把核心能力做透——都是后续版本的地基。本版本以**架构设计与调试**为主，少加新功能。

#### 内核边界（本版本确立）

灵汐采用「**最小内核 + 全插件**」架构。内核只保留为插件服务的基础底座：

| 层 | 内容 | 说明 |
|---|---|---|
| **内核（最小底座）** | 管道引擎 + 多租户 + 配置系统 + 日志系统 + 插件加载器 | 为插件提供执行环境、租户隔离、配置与生命周期管理 |
| **插件（全部，含内置）** | 通道层 / 记忆 / 评估 / 工具 / Agent / 审批 / 连接器 / 触发器 / Skill / 数字人 / 游戏接入 …… | 一切能力都是插件；内置插件随发行提供，第三方插件可装卸 |

> 多租户是**内核**,不是插件——它和管道引擎同级,为插件提供「为谁执行、能用什么」的租户上下文。插件感知租户(用租户的配置/凭据),但不负责隔离(隔离由内核完成)。

#### 〇、管道引擎与插件执行模型（内核地基决策）

> 本版本在内核地基层固化一组贯穿性架构决策，是「第三方插件协议」「路由收敛」「多租户」等其它功能小节共同依赖的**最底层约定**——后面几项都建立在它之上。决策详情见 `docs/working/0.2插件体系核心决策.md`（决策 1–10）与 `docs/working/adr_engine_design.md`（ADR 引擎重设计）；本节为路线图层面的固化与交叉引用，**不含实现细节**，排在「一」之前。

**① 插件统一执行模型**（核心决策 1–4）

| 决策点 | 0.2 决策 | 否决项（防止回退） |
|--------|----------|--------------------|
| 插件本质 | 所有插件（管道 / 工具 / 触发器 / 钩子 / 记忆服务）共用同一模型：`输入 → 输出 + 副作用`，没有第二种 | ❌ 按类型区分多套执行模型 |
| 执行特征归属 | 串行 / 并行 / 异步 / 同步是**引擎的属性**，不是插件的属性；插件不声明自己被怎么调度 | ❌ 在 trait 上标注 `serial`/`parallel`/`async` |
| 统一契约 | 所有插件满足同一签名 `execute(Value) -> Value`；类型安全由**消费者**负责，不在 trait 层强制 | ❌ 多个特化 trait；❌ 单 trait + 枚举信封 |
| 消费者 / 执行者分离 | 执行者只管产出输出；消费者决定调谁 / 传什么 / 怎么用输出；**消费者用错就报错，不兜底、不兼容、不猜测** | ❌ 执行者反向理解调用方；❌ 系统层兜底消费者错误 |

**② 引擎核心循环与状态契约**（核心决策 5–8）

| 决策点 | 0.2 决策 | 否决项 |
|--------|----------|--------|
| 管道引擎执行语义 | **严格串行循环**：`Input链(串行) → Core → Output链(串行)`，一步执行 + state 合并完才进下一步，无异步、无并发、无「等所有插件返回」 | ❌ 管道内并行执行插件链（破坏 state 依赖语义） |
| 工具执行器执行语义 | **并行调度**：LLM 返回多个 tool_calls 时并行发起、收集结果交回管道 | ❌ 工具串行执行（无谓损失并行收益） |
| state 归属 | state 是**管道引擎的固定 Value 契约**，只存在于管道引擎内部；工具 / 记忆 / 评估插件不接触 state，其状态在各自存储里 | ❌ 全局共享 state；❌ 工具读写 state |
| state 隔离 | 每个管道（含子管道）独立 state；「共享」是消费者**显式编排**的效果（父管道显式传输入、子管道显式交输出），不是隐式机制 | ❌ 子管道继承父 state；❌ 全局 state 树 |
| 字段 schema | `FieldSpec` 只对管道插件存在（声明 state 字段）；工具插件声明 args/result JSON Schema，不碰 state | ❌ 所有插件统一进字段 schema；❌ 管道走 trait、工具走 manifest 的分裂设计 |

**③ 插件一等公民与子管道归属**（核心决策 9–10）

| 决策点 | 0.2 决策 | 否决项 |
|--------|----------|--------|
| 插件反向调用 | 插件是一等公民，可作为消费者调用其他插件，但**必须走统一 invoker**（`ctx.call_plugin`），让日志 / 错误 / metrics / 热插拔统一生效 | ❌ 插件直接持有其他插件引用；❌ 插件持有引擎引用（循环依赖） |
| 运行时句柄能力 | 运行时句柄只提供 `call_plugin`，**不提供 `spawn_pipeline`** | ❌ 插件直接 spawn 子管道 |
| 子管道触发权 | 子管道由**专门业务服务**（任务系统 / 复盘系统等）触发调度，不由管道引擎 fork/delegate；引擎对此无感知 | ❌ 引擎层 fork；❌ 引擎层 delegate |

> 子管道触发权 + 路由信号精简（0.1 的 6 种 → 0.2 的 4 种：删 `delegate`/`fork`，保留 `next_llm`/`next_tool`/`end`/`wait`）的完整论证见下方「**七、路由方式收敛**」。

**④ 引擎极简主义重设计（ADR）**——把 0.1 的 `PipelineEngine` 重写为 `AdrEngine`

| ADR 要点 | 0.2 决策 | 替代 / 变更 |
|----------|----------|-------------|
| 引擎极简主义（①） | 引擎仅为**调度器 + 状态账本**，不含业务逻辑；路由表仲裁**下沉到 YAML 配置**（`route_check` 条件分支 step，`condition`/`then_steps`/`else_steps`） | 移除引擎内路由表仲裁逻辑 |
| 单一真相源 + Append-Only（③） | 所有状态变更以**追加 Patch** 记录，历史永不修改、永不删除 | redb event log → SQLite `traces` 表 |
| SQLite 四表（④） | `runs` / `messages` / `traces` / `blobs` 四表为唯一核心存储（向量库不再承担核心存储） | redb + YAML state 文件 → SQLite 四表 |
| 多分支回滚（⑤） | 引入 `branch_id + seq_in_branch`；回滚 = **创建新分支 + 正向重放 Patch** 恢复状态，不删除、不逆操作、可审计 | checkpoint 标记「已回退」 → 新分支正向重放 |
| 原子 + 组合插件（⑥） | 新增组合插件：YAML 编排 step 序列由引擎解释执行，每个 step 仍是原子插件（走统一 `execute` 接口，不加新 trait） | 仅原子插件 → 原子 + 组合 |
| 内容懒加载（⑦） | 插件声明 `requires_content: N`，按需从 `blobs` 表加载消息内容；state 只存摘要 | state 全量加载 → 按需懒加载 |
| HookContext 标签化（⑨） | 固定 6 字段 struct → `HashMap<String, Value>` 动态标签（新增字段只调 `set`，不改 struct + 所有消费方） | 改 struct + 消费方 → 只调 `set`/`get` |
| 向量库独立可选（⑩） | 向量库降为**附属索引**，从 SQLite 异步同步、可独立启停，不参与核心一致性 | pgvector 核心存储 → 可选附属 |

> 这组重设计在 0.2 已落地（`kernel/crates/engine`，实现见 `docs/working/task06_engine_review_report.md`）；插件系统（trait / invoker / manifest）基本不动（ADR ⑫ 验证通过）。

---

本版本汇集多项核心能力，按工作量组合分多个小版本逐步上线（做完一批发一个小版本，不追求一次性全上）：

- **管道引擎与插件执行模型固化**（内核地基：统一执行模型 + 串行循环 + state 契约/隔离 + 插件一等公民走 invoker；`PipelineEngine` 重写为 `AdrEngine`——调度器+状态账本、路由仲裁下沉 YAML、SQLite 四表、多分支回滚、组合插件、内容懒加载、HookContext 标签化）
- 插件协议（含内部模块完全插件化）+ 宿主接入
- 前端 Schema 驱动调试齐全
- 审批闭环优化
- 记忆检索/注入补全（VECTOR / TAGWAVE / SUMMARY 从「有代码未上线」到「正式可用」）
- 路由方式收敛（路由信号精简为 next_llm / next_tool / end / wait 四种；跨管道路由统一走工具触发专门服务，不再走引擎 delegate/fork）
- **多租户核心系统**（TenantContext 穿透 + 数据访问咽喉点 + tenant_id 隔离过滤，单租户无感；多租户管理/RBAC/凭据保险库放到后续版本）

#### 一、第三方插件协议（基础，先做）

| 类别 | 条目 | 说明 |
|------|------|------|
| 插件清单协议 | **manifest schema**（能力、依赖、版本约束、宿主类型） | 声明式；JSON Schema 可校验；支持声明「我是给 Unity 的插件」/「我是给灵汐核心的插件」 |
| 双插件根 | **只读内置根 + 可写用户根** | 内置插件随发行只读；第三方插件落到用户目录（如 `%APPDATA%/agentos/plugins`） |
| 生命周期 | 加载 / 启动 / 热更新 / 卸载 | 进程内 + 进程外双形态；复用已上线的 `hot_swap`/`hot_reload` 能力 |
| 事件总线与扩展点 | 稳定的可被插件监听/干预的接口 | 版本化扩展点表 |
| 第三方插件注册中心 | HTTP / 本地双模式 | 公网托管源 + 内网私有源 |
| 准入安全 | 插件签名 + 白名单 + 崩溃隔离 | 防恶意插件；加载失败不影响宿主核心 |

#### 二、内部模块统一 manifest 化

> 各内部模块**已具备约定式自动发现 + 热插拔**（工具 `auto_loader` + `hot_swap`、连接器 `registry`、Agent YAML + `hot_reload`、通道适配器），本版本只把它们**收敛到第三方插件 manifest 协议之下**（统一加载入口、可独立分发），不是从零做插件化。

| 类别 | 现状 | 本版本收敛 |
|------|------|-----------|
| 工具 | 约定式自动发现 + 热替换**已上线**（41 个 builtin） | 按 manifest 封装，统一加载入口，与第三方同一协议 |
| 连接器 | 注册表式发现**已上线**（vscode/game_engine/comfyui/generic） | 按 manifest 封装，可独立分发 |
| Agent / 通道 | YAML + 热重载**已上线** | 纳入 manifest，支持独立分发 |

#### 三、宿主接入

灵汐与外部宿主**统一**通过三种形态接入，同一套接口，不同外壳：

| 接入形态 | 机制 | 适用宿主 |
|----------|------|----------|
| **悬浮窗** | 独立浮层窗口，叠加在宿主 UI 之上 | 游戏引擎、VS Code、视频剪辑器、任意桌面应用 |
| **内置插件** | 作为宿主原生插件加载（Editor 扩展 / VS Code Extension 等） | 有插件体系宿主首选 |
| **进程内调用** | 宿主直接 RPC 调用灵汐后端 | 无 UI 需求、需高性能的场景 |

**本版本只接 1–2 个宿主作为协议验证**（插件化后，新增宿主只是"多加一个插件"，无需再做大版本）：

| 宿主 | 接入形态 | 交付内容 |
|------|----------|----------|
| **游戏引擎（选 1–2 个）** | 内置插件 + 悬浮窗 | 补悬浮窗外壳与一个可运行的"聊天 Agent 嵌入 Demo"；**不追求三大引擎全接入** |
| **编辑器/工具（选 1 个，如 VS Code）** | 内置插件 | 验证"悬浮窗 + 内置插件"模式在非游戏宿主同样成立 |

- 引擎沙箱与权限边界：宿主化执行，防止越权与误操作
- 后续 Unity/Unreal/Godot 全家、视频剪辑器、设计工具等，均可作为社区插件增量补充

#### 四、前端 Schema 驱动调优

> 超级终端愿景的基础能力：后端能力定义 → 前端自动生成对应界面。**现有框架已相当成熟**（5 渲染空间 Chat/Workspace/Floating/Dock/Scene + 14 个 Widget + SchemaParser/RenderingEngine/CompositionEngine 引擎层 + 后端 ui_schema 校验），本版本做的是**全覆盖调优**，不是从零搭框架。

| 类别 | 现状 | 本版本调优 |
|------|------|-----------|
| 后端配置 → 前端控件全映射 | YAML-to-form 基础已有 | 覆盖无遗漏，全部可配置项可渲染 |
| 能力 → 界面自动生成 | 引擎层已就绪 | 打磨「新增能力/插件 → 前端自动长出界面」链路稳定性 |
| Widget 覆盖 | 14 个 Widget 已实现 | 补全审批/任务/制品等场景的 Widget |
| 工作区 Tab 与 Widget 联动 | 渲染空间 + Widget 注册已就绪 | Schema 路由到正确渲染空间，打通自动联动 |

#### 五、审批闭环补全（全能闭环核心）

> 文本审批闭环**已上线**：choice/conversation 审批 + 管道暂停/恢复 + 反馈注入 + 打回重做都通了。diff 渲染能力也已具备（`ReviewDiff` 组件 LCS 算法 + 后端 `get_version_diff` API + 批注 CRUD 全套）。本版本只补「最后一公里」：review-request 协议增强 + 工作区自动联动。

| 类别 | 现状 | 本版本补全 |
|------|------|-----------|
| 审批协议增强 | 现有 `create_choice_request` / `create_conversation_request` | 新增 `create_review_request`：携带制品（artifacts）+ 结构化批注（annotations）反馈 |
| diff 渲染 | `ReviewDiff`（LCS 算法 side-by-side/unified）+ `TextDiffView` + 后端 `get_version_diff` **已具备** | 接进审批 Workspace 渲染回路（目前组件已实现但未在审批场景接线） |
| 批注服务 | `annotation_service.py` 全套 CRUD **已上线** | 接进文档审阅交互（选中文字→批注框→提交） |
| 工作区自动联动 | 5 渲染空间 + Widget 注册 + Schema 路由**已就绪** | 审批请求到达 → 自动打开 Workspace 审阅 Tab → Schema 路由到 `review_document` Widget |
| 对话+制品互补 | conversation 模式已上线 | 扩展携带 artifacts：Chat 快速讨论 + Workspace 精确批注互补 |

> 图片/视频/3D 等多模态审批（`review_image`/`review_video`/`review_3d`）为 P1-P3，放到后续版本随对应场景（数字人/游戏）一起做。

#### 六、记忆检索/注入补全

把设计中的 3×3 检索×注入矩阵从「部分上线」补全为「全量可用」。现状：KEYWORD 检索 + FULL/RETRIEVAL 注入已上线；VECTOR 装配链路已通（仅运行环境缺 PG 配置）；TAGWAVE 有代码未注册；SUMMARY 是 stub。

**检索方式**：

| 方式 | 现状 | 目标 |
|------|------|------|
| KEYWORD | ✅ 已上线 | 保持 |
| **VECTOR（向量语义）** | ⚠️ 装配链路已通（`PgVectorRetriever` 已实例化+注册+API 已暴露 hybrid），仅运行环境缺 PG+pgvector+embedding 配置（缺则降级 keyword） | 补全运行环境配置 + `.env` 模板，使其默认可用 |
| **TAGWAVE（标签联想）** | ⚠️ 有代码未上线（`tag_network.py` 三阶段算法完整，但未注册进 `_retrievers`，引擎不消费） | 注册 tagwave 检索器；澄清「标签检索」语义 |

**注入方式**：

| 方式 | 现状 | 目标 |
|------|------|------|
| FULL | ✅ 已上线 | 保持 |
| RETRIEVAL | ✅ 已上线（仅走 keyword） | 扩展：vector/tagwave 方法也走通 |
| **SUMMARY（摘要注入）** | ⚠️ stub（`_retrieve_summary` 直接返回检索结果，无摘要生成） | 实现：接 embedding_service 做真正摘要生成 |

#### 七、路由方式收敛

把路由信号与管道编排能力从「半成品」收敛为「干净可用」。现状：next_llm / next_tool / end / wait 已上线并闭环；delegate、decision 有插件引用但引擎不消费；管道/message 级 fork 完全缺失。

**决策方向**：核心判断——子管道触发权不在引擎，而在专门服务（任务系统、复盘系统等）。路由信号只负责"本管道内下一步走向"，跨管道的派生由专门服务通过工具调用显式发起。

**路由信号精简**：0.1 的 6 种 → 0.2 的 4 种

| 信号 | 0.2 处置 | 说明 |
|------|----------|------|
| next_llm / next_tool / end / wait | ✅ 保留 | 核心循环必需 |
| ~~delegate~~ | ❌ 移除 | 语义已被"工具同步调用专门服务"覆盖；引擎不再消费 delegate 信号 |
| ~~fork（管道级 / message 级）~~ | ❌ 移除 | 与 state 隔离、消费者/执行者分离原则冲突；用工具/触发器异步调用专门服务替代 |
| decision | ⬇️ 下沉为路由表条件分支 | 引擎不作为独立信号消费，由 output_routes 的 condition 表达式承担决策逻辑 |

**跨管道路由统一走工具触发**：

| 场景 | 0.1 做法 | 0.2 做法 |
|------|----------|----------|
| 同步委派子管道 | delegate 路由信号 | 工具同步调用任务系统/复盘系统等服务 |
| 异步分叉子管道 | fork 路由信号（缺失） | 触发器/工具异步调用专门服务 |
| 子任务结果回传父管道 | ✅ 已上线（走 task 工具链） | 沿用——这正是 0.2 的统一模式 |

> 跨管道路由在 0.1 已通过 task 工具链跑通，0.2 把这条路径确立为唯一模式，并移除引擎层的 delegate/fork 特殊路径。引擎回归纯粹——只跑 next_llm / next_tool / end / wait 四种信号的串行循环。

#### 八、多租户核心系统

> 多租户的完整运营（多租户创建/切换、RBAC、凭据保险库、配额）放到后续版本，但**隔离地基现在做实**——避免后续给已定型内核补 tenant 维度时大面积返工。本版本只支持默认租户运行，单租户场景下无感，但隔离机制是完整的、可验证的。

| 条目 | 说明 |
|------|------|
| **TenantContext** | 定义租户上下文数据结构（tenant_id / user_id / 角色 / 权限 / 启用插件清单 / 凭据句柄），作为内核对象贯穿管道执行全程 |
| **管道注入点 + 异步路径穿透** | 管道执行第一件事装配 TenantContext，后续子系统（记忆/工具/Agent）均从此取租户信息；触发器、跨管道通知等异步路径同样穿透租户上下文 |
| **数据访问统一咽喉点** | 所有数据读写必经内核统一数据访问层，按 tenant_id 强制过滤；插件拿不到原始连接，只能通过带租户上下文的接口访问数据 |
| **数据模型 tenant_id** | 记忆（EPISODE/SEMANTIC）、会话、制品等数据模型内置 `tenant_id` 字段，默认值=默认租户（单租户无感） |
| **插件归属契约** | 约定全局插件（`plugins/`，所有租户可用）+ 租户插件（`tenants/{id}/plugins/`，某用户独有）的目录与发现契约 |

#### 通过标准

- **管道引擎与插件执行模型固化**：统一执行模型（`输入→输出+副作用` + 统一 `execute` 签名 + 类型安全归消费者）、引擎串行循环 / 工具并行调度、state 为管道引擎固定契约且按管道隔离、插件走 invoker 反向调用（无 `spawn_pipeline`）—— 全部落地且无可绕过路径
- **AdrEngine 重设计落地**：引擎回归「调度器 + 状态账本」，路由仲裁下沉 YAML（`route_check` 条件分支）；SQLite 四表（runs/messages/traces/blobs）为唯一核心存储，Append-Only Patch + 多分支正向重放回滚；组合插件 / 内容懒加载 / HookContext 标签化 / 向量库降为附属索引均已就绪（插件系统 trait/invoker 不返工）
- 第三方插件协议固化，文档齐全，可在 1 小时内发布第一个内部插件
- 内部模块（工具/连接器/Agent/通道）收敛到统一插件协议，与第三方插件同一加载入口
- 双插件根落地：内置插件只读、第三方插件可写入用户目录，二者均能被正确发现加载
- 至少 1 个游戏引擎 + 1 个非游戏宿主跑通"悬浮窗/内置插件 + 灵汐对话"的端到端 Demo
- 插件加载失败不影响宿主核心（沙箱边界已落地）
- 前端可由后端 schema 完整长出界面，新增能力/插件无需手写前端
- 文本审批闭环的 diff 渲染联动补全：审批请求 → 自动打开工作区审阅 Tab → ReviewDiff 渲染版本对比（现有组件已具备，本版本接线）
- VECTOR / TAGWAVE 检索正式注册可用；SUMMARY 注入产出真正摘要；3×3 矩阵全部可达
- delegate / fork 路由信号从引擎移除，跨管道路由统一走工具触发专门服务；decision 下沉为路由表条件分支
- 多租户核心系统落地：TenantContext 穿透管道执行上下文（含异步路径）+ 数据访问统一咽喉点按 tenant_id 强制过滤 + 记忆/会话/制品内置 tenant_id（单租户场景无感，隔离机制完整可验证）

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

- ✅ 公共 API 冻结；1.x 内只做向后兼容修复
- ✅ 语义化版本承诺（SemVer）；弃用至少提前 1 个 minor 版本预告
- ✅ 变更日志规约（Keep a Changelog）；强制化

#### 质量门

- ✅ 全量端到端测试覆盖核心路径；用户旅程表 + 影响矩阵对应
- ✅ 性能基线 & 回归基线；CI 卡口固化
- ✅ 安全审计与渗透测试；第三方或内部红队报告

#### 生态

- ✅ 长期支持计划（LTS）：1.x 维护 ≥ 18 个月
- ✅ 插件市场上架审核流程：第三方插件可用、可信、可回滚
- ✅ 国际化（i18n）支持 10+ 语言

#### 核心里程碑（沿用原版）

- ✅ 至少 3 个企业级生产案例
- ✅ 完整的中英文档、视频教程、案例库
- ✅ 社区生态规模：100+ 第三方插件、50+ 任务模板

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

**mypy 类型注解**：当前约 470 个类型检查错误（`call-arg`/`union-attr`/`arg-type` 为主，多为 Optional 链路与字符串注解引用，非崩溃级 Bug）。CI 的 typecheck job 已设 `continue-on-error`，报告可见但不阻塞。完善类型注解是长期工作，欢迎认领：补全函数签名注解 → 收窄 Optional → 移除 `continue-on-error` 恢复硬门禁。

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

**参考**：本地 DSH 源码已 clone 至 `D:\reference_repos\deepseek-harness\`（如不存在可重新 `git clone --depth 1 https://github.com/deepseek-ai/deepseek-harness.git`）。关键文件：`packages/sdk/server/src/server.ts`（SDK server，改这里）、`packages/core/tools/src/index.ts`（Events 接口，钩子点权威清单）、`packages/shell/shell/src/types.ts`（service 契约样例）。

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

**触发条件**：0.2 迁移收尾后，作为"用户可信度"的硬基础。当前 mypy 仍有约 470 个类型检查错误（ROADMAP 已知技术债），测试覆盖率无硬门禁。

**已落地（2026-08-15，机械门禁部分）**：统一机械门禁入口 `scripts/run_gates.py`（21 个门禁单一事实源，CI 跑穷尽集 + 本地 fast 廉价检查，每个承诺都有非零退出命令）+ 覆盖率豁免重型套件 `scripts/coverage_exempt.py`（94 插件子进程冒烟矩阵免插桩、与插桩 gate 并行，实测对父进程覆盖率零贡献；覆盖率地板 44% 只升不降 + 失败数基线锁只减不增自动守护名单与车道）+ electron 桌面壳编译门禁（新增 CI job）+ 修复一批门禁接入后机械暴露的既有破损（python-lint mypy 路径 bug、SDK 5 处类型错误、10 处非法追溯标记、47 个新测试文件未标记、kernel fmt 漂移、root 死 test 脚本）。详见 `docs/working/机械门禁统一入口与覆盖率豁免.md`。

**现状对比**：DSH 有完整工程基础设施——oxlint + knip（未用依赖检测）+ jscpd（重复码检测）+ publint + lefthook + Vitest e2e/snapshot test，且 `test:coverage` 是 CI 硬门禁（per-file 100%）。本项目在测试/CI 门禁上明显弱于 DSH，这直接影响用户/开发者对项目的信任度。

**落地方向**：
1. mypy 类型错误清零（470 → 0），CI typecheck job 取消 `continue-on-error` 恢复硬门禁
2. 测试覆盖率门禁收紧（✅ 底线已设：Python fail-under=50、Rust line% 基线锁只升不降、前端 vitest thresholds；逐步收紧 Python 50→80%，对标 DSH 的 per-file 100%）
3. 引入 knip（未用依赖/导出检测）、jscpd（重复码）等质量工具
4. e2e/snapshot test 基础设施（对标 DSH 的 vitest e2e + keyless snapshot replay）

**优先级**：🟡 P1——不是"可选"，是"必须"，只是排在 0.2 迁移收尾之后。

### 插件契约完善（0.2 定型后）

**触发条件**：0.2 核心地基层定型（路由收敛 / capabilities 交互面 / contributes 定型）之后。当前契约已具备基础：`plugin.json` capabilities（tools/resources/route_signals/lifecycle_hooks/services）、工具契约（含 output_schema/render，消费端见 `task_dsh_plugin_adapter.md` 任务 1）、管道插件接口（Input/Core/Output + RouteSignal 四信号）、contributes 全景——**这些 0.2 定型后尽量不再动**。

**核心原则（契约冻结）**：0.2 定型后，接口契约**能不动就不动**——版本更新必须考虑旧插件兼容，频繁改契约会让系统往更复杂方向走。动契约 = breaking change，必须走 ADR 记录 + 兼容机制（旧插件继续可用）。

**0.2 后完善的索引**（到时机再展开，不做前置详细任务文档）：
1. **插件契约设计指南**：契约三问（读什么 / 吐什么 / 挂哪）+ 各插件类型立契模板 + 表单级 / 条件循环级 / 任意语言级三个入口（同一契约，三种填写方式）
2. **契约校验器**：JSON Schema 合集，新插件注册机器裁决（缺失声明拒绝 + 报缺什么）；与 `task_dsh_plugin_adapter` 任务 1 的 output_schema 消费端共用契约事实
3. **写插件 skill 模板化**：衔接 `.zcode/skills`（resource-tool-create 等），按"契约三问 + 领域惯例"重组织——skill 教"先立契"的思考流程，不教语言细节
4. **自进化衔接**：agent 按契约三问立契 → 实现 → 校验器放行 → 复盘闭环

**优先级**：🟢 P2——0.2 定型后完善；当前只做不破坏契约的事（如 output_schema 消费端、spill_guard）。

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

详见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

---

## 📊 决策原则

路线图不是承诺。优先级判断遵循以下原则：

1. **用户价值 > 技术先进性** —— 优先解决真实用户的痛点，而非追逐技术热点
2. **核心深度 > 边缘广度** —— 把管道引擎、Agent 协作做透，比铺更多通道更重要
3. **可演进性 > 一步到位** —— 优先选可扩展的方案，承认"完美设计"不存在
4. **社区共识 > 个人偏好** —— 重大方向变更需在 Discussions 充分讨论
5. **契约冻结 > 灵活演进** —— 0.2 定型后接口契约能不动就不动：版本更新必须兼容旧插件，频繁改契约的短期便利会被长期兼容性负担淹没；动契约 = breaking change，需 ADR + 兼容机制

---

## 🕒 时间线

```
▓▓▓ 0.1.0 ✅ (已发布)
░░░ 0.2.0 核心地基层（管道引擎执行模型 + AdrEngine 重设计 + 插件化 + Schema + 审批 + 记忆补全 + 路由收敛 + 多租户契约预留）
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
