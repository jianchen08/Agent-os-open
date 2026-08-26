# 灵汐 AgentOS 架构文档

> 本文档面向**希望深入了解灵汐内部机制、进行二次开发或参与核心贡献**的开发者。
> 0.2 架构：**Rust 微内核 + Python 插件 + React 前端**。插件开发协议见
> [plugin-protocol.md](plugin-protocol.md)，上手教程见 [guides/README.md](guides/README.md)。

---

## 目录

- [设计哲学](#设计哲学)
- [总体架构](#总体架构)
- [核心子系统](#核心子系统)
  - [管道引擎（Pipeline Engine）](#管道引擎pipeline-engine)
  - [插件系统（Plugin System）](#插件系统plugin-system)
  - [Agent 系统](#agent-系统)
  - [工具系统](#工具系统)
  - [记忆系统](#记忆系统)
  - [配置系统](#配置系统)
  - [任务系统与评估闸门](#任务系统与评估闸门)
  - [隔离与工作区（Isolation & Workspace）](#隔离与工作区isolation--workspace)
  - [触发器系统（Triggers）](#触发器系统triggers)
  - [审批交互闭环（Approval Loop）](#审批交互闭环approval-loop)
  - [复盘系统（Review）](#复盘系统review)
  - [主题与前端定制](#主题与前端定制)
- [数据流示例](#数据流示例)
- [扩展点](#扩展点)
- [架构设计四问](#架构设计四问)

---

## 设计哲学

灵汐的设计建立在三个核心原则之上：

### 1. 配置优于代码（Configuration over Code）
几乎所有运行时行为都通过 YAML / 配置文件定义。新增一个 Agent、调整一条管道、修改一个工具的可见面，都不应该需要改内核代码——Agent 是 YAML 数据，管道是 YAML 编排，工具面是白名单交集。

### 2. 状态可观测（Observable State）
管道的每一步决策都被显式建模（state 字段、路由信号、执行轨迹），分层可查（骨架 / L1 压缩块 / L0 原始记录，`read_execution_detail` 工具）。任意时刻可以回答："现在卡在哪一步？为什么？下一步会往哪走？"

### 3. 可回滚、可热替换（Rollback & Hot Swap）
Agent 配置 mtime 缓存热生效；插件目录热发现 + re-enable 即时重注册；Python 插件进程空闲回收、代码改动热重载、崩溃自动拉起；内核不因单个配置加载失败而启动失败（降级 warn + 空配置）。

---

## 总体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                 前端 React 19 + Vite（:6390，反代内核）            │
│     聊天 / 任务面板 / 配置可视化 / 插件设置 / 主题与皮肤运行时       │
└────────────────────────────┬─────────────────────────────────────┘
                             │ HTTP / WebSocket
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                Rust 微内核 agentos-kernel（:9100）                │
│  api（REST/WS 路由）│ engine（管道解释执行）│ session（会话域）      │
│  config（ConfigCenter）│ plugin-loader │ invoker │ mcp（客户端）    │
│  hooks │ http │ core（契约 trait）│ db-admin │ tenant │ user-admin │
│                存储：SQLite（默认 agentos_kernel.db，driver 化）   │
└────────────────────────────┬─────────────────────────────────────┘
                             │ MCP over stdio（sidecar）/ C-ABI（原生）
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                          插件（110 个 manifest）                   │
│  pipeline/{input,core,output}  管道步骤（Python 边车 + Rust cdylib）│
│  tools/                        LLM 工具（26 个，含外部 MCP 接入）   │
│  system/                       系统服务（LLM/记忆/审批/评估/通道…）  │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                  基础设施：LLM Providers │ 文件系统 │ Docker        │
└──────────────────────────────────────────────────────────────────┘
```

### 关键路径

**用户消息 → 前端 → 内核 chat 入口 → 管道引擎（init → main 循环体[prepare → core → post] → exit）→ 流式响应（block 协议八事件）→ 前端渲染**

---

## 核心子系统

### 管道引擎（Pipeline Engine）

内核引擎是配置解释执行器：读 `config/pipelines/autonomous.yaml`（唯一现役管道，所有 Agent 共用，差异由 Agent 配置体现），按 `loop_bodies` 顺序执行，据统一路由 DSL 决定循环、分支与循环体间转移。

```
init 循环体（单次）  workspace/environment 解析
main 循环体（while） prepare：Input 插件链（context_build → tool_schema → … → prompt_build → 守卫链）
                     core：  pipeline_llm_core ↔ pipeline_tool_core（动态切换）
                     post：  Output 插件链（track → task_reminder → stop_check → …）+ 出口路由
exit 循环体（单次）  workspace 收尾 + 环境释放（run_on_error，提前终止必经）
```

- **G10 路由 DSL**（冻结）：条件永远 `when`、目标永远 `then`（`end` / `loop` / step id / 循环体 id）、附带写入 `set:`。配置在**加载期编译**（when 预编译 AST、引用静态解析、五类命名冲突启动即报），运行时零解析。
- **step 三级命中**：管道 step id → 公共 step 库（`config/steps/`）→ 插件 id。
- **4 种路由信号**：`next_llm` / `next_tool` / `end` / `wait`（仅 output 阶段插件产出，挂起支持审批后恢复）。
- **并发模型**：RunChainRegistry 按 effective_pipeline_id 串行（同管道 FIFO、异管道并行、全局并发上限）。
- **task = pipeline state 单一真值**：`task.id = pipeline_id`；任务状态由任务域插件裁决（评估闸门），内核不回写任务状态。

配置方法见 [guides/pipeline-configuration.md](guides/pipeline-configuration.md)。

### 插件系统（Plugin System）

所有可扩展模块收敛到**同一个插件协议**：一个插件 = 一个目录 + 一份 `plugin.json` manifest（+ 实现代码）。内核统一发现、校验（`deny_unknown_fields`）、注册、按需加载，不区分内部还是第三方。

- **三类目录**：`plugins/shared/pipeline/{input,core,output}/`（管道步骤）、`plugins/shared/tools/`（LLM 工具）、`plugins/shared/system/`（系统服务）。
- **双根发现**：内置根 `plugins/shared/` + 用户根（`AGENTOS_USER_PLUGINS_DIR`），同 id 用户根覆盖内置根。
- **宿主三轨**：Python sidecar（默认；独立进程、MCP over stdio、uv venv 单轨、懒启动/空闲回收/崩溃自愈/热重载）、Rust cdylib 原生（`in_process`；高频管道步骤晋升轨、永不 dlclose）、外部 MCP（`entry: "mcp:external"`；零代码直连第三方 MCP 服务）。
- **能力声明**：`capabilities.tools`（进 LLM 面）/ `services`（内部服务，不进 LLM 面）/ `route_signals` / `lifecycle_hooks` / `streaming`（流式事件声明，fail-closed）。
- **插件间耦合唯一轴**：`requires_services`（能力角色名，boot 期闸校验）。
- **LLM 可见工具三层过滤**：启用快照（`config/plugins/default_profile.yaml`）→ 能力注册（缺 schema 的 external MCP 工具拒注册）→ Agent `tool_ids` 白名单（解析不出 = 空工具面，禁止静默全量）。
- **G2 复核**：re-enable 时 spawn sidecar 校验 manifest 声明与实现一致，漂移按净化后 manifest 注册。

协议全字段见 [plugin-protocol.md](plugin-protocol.md)；开发见 [guides/plugin-development.md](guides/plugin-development.md)。

### Agent 系统

每个 Agent 是一份 YAML（`config/agents/{main,orchestrator,executor,system,task}/**/*.yaml`），定位按文件名优先、`config_id` 回退。

```yaml
config_id: code_writer_agent
name: 代码编写专家
agent_type: executor          # main / orchestrator / executor / system
level: L3                     # L1 沟通调度 → L2 编排 → L3 执行，委托深度上限 3 层
model_tier: medium            # 或 model_name 直接指定
system_prompt: |              # 支持 {{path:...}} 文件注入与 {{project_root}} 占位
  ...
tool_ids: [file_read, file_write, bash_execute, enhanced_search]   # LLM 可见工具白名单
hard_constraints: [...]
soft_constraints: [...]
deliverables: [...]           # executor 特有：产出物声明
plugins:
  enabled:
    task_reminder: { max_reminders: 3, cooldown_seconds: 180 }     # per-plugin inputs
```

- **消费分权**：内核只读 `tool_ids`（窄接口，注入工具 schema）；全量配置由管道 prepare 步的 `pipeline_context_build` 插件自持加载，注入 `context.system_prompt` / `tool_ids` / `context.agent_level` 等。
- **agent_id 全链传导**：会话创建时写入 state，后续每轮按它取配置；切换会话 Agent 即整体切换人设/工具/约束。
- **多层协作**：主管（灵汐，L1）面向用户负责任务分类与派发；编排（L2）做多步骤编排与审查节点；执行（L3）是具体执行单元。

配置方法见 [guides/agent-configuration.md](guides/agent-configuration.md)。

### 工具系统

- **统一契约**：每个工具声明 `input_schema` + `output_schema` + `render`（前端渲染意图）。工具执行后按 `output_schema` fail-closed 校验；前端按 `render` 路由渲染结果卡片；MCP 对外下发同一契约。
- **执行**：工具调用由管道 core 步的 `pipeline_tool_core`（Rust 原生插件）执行，大输出经 `pipeline_spill_guard` 落盘兜底（`spill_retrieve` 为框架强制工具）。
- **分类来源**：内置工具插件（26 个）／外部 MCP 工具（零代码接入）／用户根自研插件。
- **工具面控制**：Agent 的 `tool_ids` 白名单 + 层级守卫（level_guard 拦截越级任务类工具）+ 安全守卫（security_check 判定路径/危险工具审批/隔离放行）。

### 记忆系统

- **情景记忆（EPISODE）**：对话压缩后的记忆，保留要点而非逐轮原文，会话切换不丢失。
- **语义记忆（SEMANTIC）**：沉淀用户偏好、项目决策、外部知识导入，跨会话可用。
- **0.2 落位**：记忆服务由 `hindsight_memory` 系统插件承载，LLM 侧经 `memory` 工具读写；管道侧 `pipeline_memory_read` 在 prepare 步按需注入。检索与注入策略由 hindsight 插件自持配置。

### 配置系统

- **ConfigCenter 单一真相源**（内核 `config` crate）：优先级 remote > env > yaml > manifest > hardcode；mtime 缓存热更新；具体配置加载失败不阻断内核启动（panic 降级 warn + 空配置），管道级配置失败才阻塞。
- **配置目录**（`config/`）：agents（Agent 定义）、pipelines（管道编排）、plugins（启用档案 `default_profile.yaml`）、models（LLM/向量模型）、isolation（工作空间/隔离）、evaluation（评估指标）、rules / skills / templates 等。
- **按需注入**：插件经 manifest `config_files` / `config_refs` 声明要哪些配置节，握手时注入，未声明收空配置。

### 任务系统与评估闸门

任务质量的硬约束，确保质量不被跳过：

- **提交即带指标**：`task_submit` 提交任务须带评估指标（acceptance criteria）；可从 Agent 的 `recommended_metrics` 自动补全。
- **评估裁决在插件**：`task_evaluate` 工具（`plugins/shared/system/evaluation/`）执行评估；管道 output 步 `pipeline_task_reminder` 是放行闸门——提醒耗尽仍无评估证据 → 任务标 `pending_evaluation`，不落 completed；有证据内核才补落默认 completed。
- **容器任务**：对"开发一个 App""写一部小说"这类多阶段大任务，提供方案规划 → 阶段执行 → 人类审查 → 完成验收闭环；容器只组织子任务链（`parent_task_id` 传递），不直接执行。
- **任务状态**：task = pipeline state 单一真值，状态由任务域插件经 pipeline-state 写面裁决。

### 隔离与工作区（Isolation & Workspace）

每个任务默认运行在**独立隔离工作区**（`workspace/{task_id}`）：

- **决策链**：prepare 步 `pipeline_isolation_guard` 按工具/路径决策隔离级别并写 `execution_contexts`；`pipeline_security_check` 复核（路径遍历/敏感目录/危险工具审批）；init/exit 步 `pipeline_workspace_lifecycle` / `pipeline_environment_lifecycle` 管工作区与环境生命周期。
- **隔离级别**：默认文件夹隔离，高风险路径走 Docker 容器隔离（isolation 系统插件提供 Provider 抽象）。
- **Git worktree**：多任务场景可为任务分叉独立 worktree，副作用在 worktree 边界审查与回滚；无 workspace 语义则无 worktree 模式。

### 触发器系统（Triggers）

无人值守自动运行：定时（Cron）、事件、间隔触发。LLM 侧经 `trigger_setup` / `trigger_review` 工具注册管理（工具插件承载），可绑定特定管道；触发后按任务复杂度直接执行或派发。

### 审批交互闭环（Approval Loop）

人机协同的质量闸，"生成 → 审批 → 反馈 → 迭代"闭环：

- **双审批模式**：`choice`（预设选项）/ `conversation`（多轮讨论），经 human-interaction 能力（`tools/human` + `system/approval` 插件群协作）。
- **管道挂起/恢复**：`wait` 路由信号挂起管道并保存 state，外部事件恢复执行。
- **反馈注入**：审批结果（通过/驳回/批注）注入管道 state，驱动 Agent 返工。

### 复盘系统（Review）

任务执行后的 LLM 深度复盘：`trigger_review` 工具触发，`review` 系统插件编排复盘管道，产出经验报告沉淀到知识库；配套记忆清理按「复盘状态 × 年龄 × 容量」决策，确保复盘产出沉淀后再回收原始记忆——系统越用越聪明的自进化闭环一环。

### 主题与前端定制

- **主题双轨**：前端预设（`frontend/src/config/themes/presets/`，7 套）+ 插件主题（manifest `contributes.themes` CSS 变量包，可带 skin 皮肤），另有动态 JSON 主题与用户自定义。
- **前端贡献通道**：`ui_schema`（页面/表单 schema 驱动）、`contributes`（主题/样式/页面）、`http_endpoints`（`/ext/{plugin_id}/**` 前端可达的 HTTP 面）。

见 [guides/theme-development.md](guides/theme-development.md) 与 [skin-plugin.md](skin-plugin.md)。

---

## 数据流示例

### 场景：用户问"项目里昨天那个 bug 改了吗？"

```
1. 前端（Web UI）
   └─ 用户发送消息 → WebSocket/HTTP → 内核 chat 入口

2. 管道出生（init 循环体）
   └─ workspace/environment 插件解析执行上下文 → state.workspace / state.environment_basis

3. main 循环体 · prepare（Input 插件链）
   ├─ context_build：按 state.agent_id 加载 Agent yaml → context.system_prompt / tool_ids
   ├─ tool_schema：按 tool_ids 白名单注入工具 schema
   ├─ memory_read：检索情景/语义记忆（关键词/标签）
   └─ prompt_build：分层组装提示词（system_prompt / tools / static_vars / 记忆 / 历史 / dynamic_vars）

4. main 循环体 · core（pipeline_llm_core，流式）
   ├─ LLM 推理：决定调用 enhanced_search + file_read
   └─ post：出口路由命中 raw_tool_calls != [] → set core_type=tool_execute → loop

5. 工具轮（pipeline_tool_core）
   ├─ security_check / isolation_guard 放行
   ├─ enhanced_search("昨天那个 bug") → 命中修复提交
   └─ file_read 验证 → tool_results 回写 state

6. 回到 LLM 轮 → LLM 收到工具结果生成文本回复 → 路由 end

7. exit 循环体：workspace 收尾；流式响应（block 协议）推送前端渲染
```

---

## 扩展点

所有扩展统一收敛到 `plugin.json` 插件协议与 YAML 配置：

| 扩展点 | 怎么做 | 参考 |
|--------|--------|------|
| 新增 LLM 工具 | 写 tool 插件（manifest + server.py + uv venv），启用并加进 Agent `tool_ids` | [guides/plugin-sidecar-python.md](guides/plugin-sidecar-python.md) |
| 零代码接第三方工具 | external MCP manifest（HTTP 远程 / 本地命令） | [guides/plugin-external-mcp.md](guides/plugin-external-mcp.md) |
| 高性能管道步骤 | Rust cdylib 原生插件（in_process） | [guides/plugin-native-rust.md](guides/plugin-native-rust.md) |
| 新增 Agent | `config/agents/` 对应层级写 yaml | [guides/agent-configuration.md](guides/agent-configuration.md) |
| 调整管道编排 | 改 `config/pipelines/autonomous.yaml`（重启内核） | [guides/pipeline-configuration.md](guides/pipeline-configuration.md) |
| 新增前端预设主题 | `frontend/src/config/themes/presets/` + index.ts 注册 | [guides/theme-development.md](guides/theme-development.md) |
| 随插件分发主题/皮肤 | manifest `contributes.themes` | [guides/theme-development.md](guides/theme-development.md) |
| 新增前端页面/表单 | manifest `ui_schema` / `http_endpoints` | [plugin-protocol.md](plugin-protocol.md) |
| 插件间服务依赖 | manifest `requires_services`（能力角色名） | [plugin-protocol.md](plugin-protocol.md) |

---

## 架构设计四问

灵汐在演进过程中始终按这四个问题审视架构决策：

### 1. 找"散"——同一个概念在代码里出现了几次？
> 多次出现 → 抽象缺失，需要统一封装。

**例**：工具输出契约（schema/render）从工具实现、内核校验、前端渲染、MCP 下发四处各自为政，收敛为 manifest 单点声明、四端共消费。

### 2. 找"分叉点"——调用方为了同一个操作需要判断几种情况？
> 调用方有分叉 → 抽象边界错误，应封装在模块内部。

**例**：管道引擎不感知插件是 Python 边车还是 Rust 原生——invoker 按 host_type 透明分发，引擎只面对统一的能力协议。

### 3. 找"谁该知道"——每个概念，谁需要知道它？
> 不该知道的人知道了 → 边界泄漏，需收回。

**例**：Agent 全量配置只有 context_build 插件消费，内核只留 `tool_ids` 窄接口——内核不需要知道提示词骨架与静态变量。

### 4. 找"变化方向"——什么会变，什么不会变？
> 把"会变的"封装在内部，"不变的"暴露为接口。

**例**：LLM Provider 会变（新增厂商、切换 API），但"调用 LLM 返回流式结果"这个动作不变——Provider 实现藏在 llm_service 插件内部，外部只看到 `llm.complete_stream`。

---

## 进一步阅读

- [plugin-protocol.md](plugin-protocol.md) —— 插件协议权威文档
- [guides/README.md](guides/README.md) —— 开发指南索引
- [ROADMAP.md](../ROADMAP.md) —— 版本路线图与被否方案索引
- [CONTRIBUTING.md](../CONTRIBUTING.md) —— 贡献流程
- [decisions/](decisions/) —— ADR 决策记录（任何非平凡决策的背景/决策/被否方案/影响）

---

> 🌊 **灵汐架构的演进方向：让 AI Agent 的搭建像配置一台服务器一样简单，但能力上限不设限。**
