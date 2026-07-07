# 灵汐 AgentOS 架构文档

> 本文档面向**希望深入了解灵汐内部机制、进行二次开发或参与核心贡献**的开发者。

> **数据准确性说明**（2026-06-23）：本架构文档基于实际代码核对
> - Python 版本：3.11+（`pyproject.toml` `requires-python = ">=3.11"`）
> - FastAPI / Redis：在 25+ 文件中实际 import，均已声明于 `pyproject.toml` 的 24 个核心运行时依赖中
> - React 版本：19.2（`frontend/package.json` `"react": "^19.2.0"`）
> - 工具数量：41 个 tool.py 实现（实际），下文用"40+ 内置工具"表述
> - 通道数量：6 个真实通道（CLI / 钉钉 / 飞书 / QQ / 企微 / WebSocket），HTTP API 走 `src/channels/api/` 作为 REST 端点

---

## 目录

- [设计哲学](#设计哲学)
- [总体架构](#总体架构)
- [核心子系统](#核心子系统)
  - [管道引擎（Pipeline Engine）](#管道引擎pipeline-engine)
  - [Agent 系统](#agent-系统)
  - [工具系统](#工具系统)
  - [记忆系统](#记忆系统)
  - [配置系统](#配置系统)
  - [通道层（Channels）](#通道层channels)
  - [容器任务系统](#容器任务系统)
  - [隔离与工作区（Isolation & Workspace）](#隔离与工作区isolation--workspace)
  - [复盘与记忆维护（Review & Memory Maintenance）](#复盘与记忆维护review--memory-maintenance)
  - [触发器系统（Triggers）](#触发器系统triggers)
  - [审批交互闭环（Approval Loop）](#审批交互闭环approval-loop)
  - [强制评估系统（Mandatory Evaluation）](#强制评估系统mandatory-evaluation)
  - [Skill 能力集成](#skill-能力集成)
- [数据流示例](#数据流示例)
- [扩展点](#扩展点)
- [架构设计四问](#架构设计四问)

---

## 设计哲学

灵汐的设计建立在三个核心原则之上：

### 1. 配置优于代码（Configuration over Code）
几乎所有运行时行为都通过 YAML / 配置文件定义。新增一个 Agent、调整一条管道、修改一个工具的 Schema，都不应该需要改 Python 代码。

### 2. 状态可观测（Observable State）
管道的每一步决策都被显式建模为"路由信号"（Routing Signal），并写入事件流。任意时刻可以回答："现在卡在哪一步？为什么？下一步会往哪走？"

### 3. 可回滚、可热替换（Rollback & Hot Swap）
所有运行时配置和插件都支持 `hot_swap`（运行时替换）和 `rollback`（状态回滚）。调试一个新提示词不需要重启服务；回退到一个不稳定的版本只需要一行命令。

---

## 总体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                         Channels (多通道)                        │
│   Web UI │ CLI │ HTTP API │ 钉钉 │ 飞书 │ 企微 │ QQ │ ...     │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                      Gateway (网关层)                            │
│  协议解析 │ 鉴权 │ 限流 │ 消息标准化 │ 会话路由                  │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Pipeline Engine (管道引擎)                     │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  Input Plugins   →   LLM Call   →   Output Plugins        │  │
│  │  (上下文注入)        (推理)         (后处理/路由)          │  │
│  └────────────────────────────────────────────────────────────┘  │
│                              │                                   │
│                              ▼                                   │
│                  4 种路由信号仲裁                                  │
│           (next_llm / next_tool / end / wait / ...)              │
└────────┬──────────────┬──────────────┬──────────────┬───────────┘
         │              │              │              │
         ▼              ▼              ▼              ▼
   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
   │  Tools   │  │  Memory  │  │  Agents  │  │ Triggers │
   │ (工具)   │  │  (记忆)  │  │ (角色)   │  │ (触发器) │
   └──────────┘  └──────────┘  └──────────┘  └──────────┘
         │              │              │              │
         └──────────────┴──────────────┴──────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                  Infrastructure (基础设施)                       │
│      Redis │ LLM Providers │ MCP Servers │ File System          │
└──────────────────────────────────────────────────────────────────┘
```

### 关键路径

**用户消息 → 通道层 → 网关层 → 管道引擎 → [Input 插件链] → LLM 调用 → [Output 插件链] → 流式响应 → 前端渲染**

---

## 核心子系统

### 管道引擎（Pipeline Engine）

管道引擎是灵汐的"心脏"。它采用**路由表 + 插件链**的双层结构。

#### 路由表机制

- **输入路由表**（可叠加）：根据上下文决定执行哪些 Input 插件
- **输出路由表**（互斥优先级）：仲裁 Output 插件产生的路由信号

#### 4 种路由信号

| 信号 | 含义 | 典型场景 |
|------|------|----------|
| `next_llm` | 下一轮调用 LLM | 工具调用完成后 |
| `next_tool` | 执行工具 | LLM 决定调用工具 |
| `end` | 结束管道 | LLM 完成回复 |
| `wait` | 挂起等待 | 等用户审批 / 外部事件 |

#### 暂停/恢复机制

管道可通过 `wait` 信号挂起并保存 `state` 快照；外部事件触发后 `wake()` 继续。典型场景是"等用户审批"——挂起 → 用户在 UI 上点击"同意" → 恢复执行。

#### 跨管道路由

`PipelineRegistry` 维护所有活跃管道实例的注册表。子任务完成后能精确地把结果回传给父管道，支持复杂的层级任务编排。

#### 热替换

`hot_swap.py` 允许运行时替换管道配置或插件实现，无需重启服务。回退通过 `rollback.py` 实现。

### Agent 系统

每个 Agent 是一份 YAML 文件（位于 `config/agents/`），描述：

```yaml
id: code_writer_agent
name: 代码编写专家
type: executor  # supervisor / orchestrator / executor

system_prompt: "{{path:prompts/code_writer.md}}"  # 引用外部文件

tools:
  - file_read
  - file_write
  - bash_execute
  - enhanced_search

model:
  tier: medium  # large / medium / small
  # 或直接指定 model_name

constraints:
  hard:
    - must_validate_syntax_before_save
  soft:
    - prefer_functional_style

input_schema:  # JSON Schema
  type: object
  required: [goal, acceptance_criteria]
  properties: { ... }

output_schema:  # JSON Schema
  type: object
  properties: { ... }
```

#### 多层 Agent 协作

- **主管**（灵汐）：面向用户的统一入口，负责任务分类与初步规划
- **编排**（方案规划 / 编程编排）：复杂任务的多步骤编排、依赖管理、审查节点
- **执行**（写代码 / 调研 / 调试 / 验证）：具体的执行单元

#### 智能切换主 Agent

同一个会话内可动态切换主 Agent——聊到一半想换成"代码专家"风格？一行指令切换，整套人设、工具、约束一并生效。

### 工具系统

所有工具遵循统一接口契约：

```python
class Tool(Protocol):
    name: str
    description: str
    when_to_use: str
    when_not_to_use: str
    input_schema: dict  # JSON Schema
    output_schema: dict  # JSON Schema
    examples: list[Example]
    caveats: list[str]
    category: str
    level: int
    tags: list[str]
    source: str  # builtin / mcp / custom

    async def execute(self, **kwargs) -> ToolResult: ...
```

#### 4 种错误策略

| 策略 | 行为 | 适用场景 |
|------|------|----------|
| `ABORT` | 立即终止后续插件 | 数据一致性敏感 |
| `SKIP` | 记录警告继续 | 辅助性工具 |
| `FALLBACK` | 用兜底结果替代 | 有可用降级方案 |
| `RETRY` | 调用方实现重试循环 | 临时性故障 |

#### 动态 Schema 增强

`image_generate` 工具的 Schema 会在运行时**动态注入当前可用的图像 Provider 列表**到 `enum` 字段——LLM 看到的总是"现在能用的服务"，而不是过期的配置。

#### 工具分类

- **内置工具**（builtin）：`file_read` / `file_write` / `bash_execute` / `enhanced_search` / `image_generate` / ...
- **MCP 工具**：通过 Model Context Protocol 接入的外部服务
- **自定义工具**：用户/开发者编写的领域工具

### 记忆系统

#### 两种记忆类型

- **情景记忆（EPISODE）**：对话压缩后的记忆，保留对话要点而非逐轮原文，会话切换不丢失
- **语义记忆（SEMANTIC）**：沉淀用户偏好、项目关键决策、外部知识库导入等，下次开新会话也能调用

#### 检索 × 注入（设计能力与上线状态）

设计上支持**三种检索方式** × **三种注入方式**的灵活组合，当前上线状态如下（✅ 已上线 / 🚧 规划中，后续版本发布）：

| 注入\检索 | VECTOR（向量语义） | KEYWORD（关键词） | TAGWAVE（标签联想） |
|-----------|--------|---------|---------|
| FULL（全量注入） | 🚧 | ✅ 全量关键词匹配 | ✅ 全量标签联想 |
| RETRIEVAL（按需检索） | 🚧 | 🚧 | 🚧 |
| SUMMARY（摘要注入） | 🚧 | 🚧 | 🚧 |

> **当前实现**：关键词检索、标签检索 + 全量注入。向量语义检索、按需/摘要注入尚未上线。

### 配置系统

- **静态变量**（`static_vars`）：会话级不变，比如项目根路径、Agent 速查表
- **动态变量**（`dynamic_vars`）：每轮变化，比如当前时间戳、用户偏好
- **外部文件引用**：`{{path:...}}` 语法引用外部 Markdown / JSON，避免大段文本塞进 YAML
- **环境变量插值**：`${ENV_VAR}` 在加载时替换

### 通道层（Channels）

所有通道走同一个**网关层**。接一个新的 IM 平台，只需在 `src/channels/` 下加一个适配器，业务逻辑零改动。

```
src/channels/
├── websocket/    # WebSocket 后端（Web UI 后端，主入口 app_factory.py）
├── cli/          # CLI
├── api/          # HTTP API（FastAPI，21 个 routes_*.py）
├── gateway/      # 网关层（鉴权 / 消息标准化 / 会话路由，IM 通道共用）
├── dingtalk/     # 钉钉
├── feishu/       # 飞书
├── wecom/        # 企微
└── qq/           # QQ
```

### 容器任务系统

对于"开发一个 App""写一部网络小说""做一个游戏"这类**多阶段、有交付物**的大任务，容器任务提供完整闭环：

```
创建容器
  → solution_planning_agent 制定方案
  → 人类审查（human_review）确认方案
  → 按任务链执行（code_writer / test / verify ...）
  → 每个里程碑都有人类审查
  → trigger_setup 周期性巡检
  → container_verification_agent 逐条核验 AC
  → 通知用户确认
  → 关闭容器
```

### 隔离与工作区（Isolation & Workspace）

容器任务的每一步都运行在**隔离环境**中，由 `src/isolation/` 提供统一抽象：

- **隔离级别**：`IsolationLevel`（CONTAINER / HOST）。`IsolationDecider` 按工具维度决策，**默认 CONTAINER**；当配置要求的级别在当前环境不可用时**拒绝降级**（抛 `IsolationError`），避免跨容器污染。
- **Provider**：`DockerProvider`（真实 Docker CLI 集成，`docker create` 带 `--init`/`--cpus`/`--memory`/`--pids-limit`/网络/端口/-v 挂载，按任务复用容器、任务结束销毁）与 `HostProvider`（宿主直接执行，需显式配置 + 人工批准）。
- **工作区生命周期**：`WorkspaceLifecycleManager` 为每个任务准备独立工作目录，支持 `worktree` / `shared` / `plain` / `project_root` / `container` 等模式。

**Git Worktree 分叉**：多任务场景下，每个任务在 `task/{task_id}` 分支上分叉出独立 worktree（`_worktree_add_with_repair` 含 prune-and-retry），大仓走 sparse-checkout；worktree 创建前对项目根做 auto-save 提交以避免带入脏改动；任务完成前 `merge_worktree_before_complete` 合并回主工作区，清理时移除 worktree 与分支。并发任务因此互不抢占文件系统，副作用可在 worktree 边界审查与回滚。

### 复盘与记忆维护（Review & Memory Maintenance）

系统内置「**复盘 → 沉淀 → 回收**」闭环，位于 `src/memory/maintenance/`：

- **触发**：`trigger_review` 工具（`src/tools/builtin/trigger_review/tool.py`）解析父 `pipeline_id` 后调用 `MemoryMaintenanceService.trigger_llm_review()`；另有 REST 入口 `POST /api/v1/maintenance/review`。带自环保护（复盘管道内不再触发）与单运行守卫。
- **执行**：服务收集所有 `review_status=pending` 的已结束管道，按 Agent/状态分组，按 token 预算（模型上下文窗口 × `skeleton_budget_percent`，默认 15%，上限 `review_batch_limit`=10）分批，每批注册并启动子 `review_agent` 管道，注入目标清单 + 被复盘 Agent 的硬/软约束。
- **产出**：拉取报告（轮询上限 ~600s）→ 持久化到 KnowledgeService **并**写入 `docs/working/review_report_{id}.md` → 标记管道已复盘 → 回调通知父管道。
- **记忆清理**：周期性触发器 `memory_maintenance_check`（间隔 `cleanup_check_interval`，默认 86400s）驱动 `CleanupEngine.cleanup_by_age_and_capacity()`，按**复盘状态 × 年龄 × 容量**三维矩阵决策：

  | 复盘状态 | 年龄 | 容量 | 动作 |
  |---------|------|------|------|
  | 已复盘 | > 30 天（`cleanup_min_age_days`） | — | 删除 L0 + L1 |
  | 已复盘 | > 7 天（`cleanup_early_age_days`） | > 80%（`cleanup_capacity_threshold`） | 仅删除 L0 |
  | 未复盘 | > 30 天 | — | 直接删除 L0 + L1 |
  | 其他 | — | — | 保留 |

  清理按体积分层（先 L0 大 YAML，条件性 L1 压缩块，再关联 Episode；**Knowledge 永不删除**），删除后重建向量索引。容量压力按数据目录 YAML 总大小 / 1 GB 上限估算。

### 触发器系统（Triggers）

让灵汐无人值守自动运行。位于 `src/triggers/`：

- **触发器类型**：Cron 定时触发、事件触发（订阅 EventBus）、间隔触发
- **注册与调度**：`TriggerRegistry` 从 `config/triggers/` 加载，`TriggerManager` 经 ServiceProvider 拿到管道引擎执行
- **自动订阅**：触发器启动时自动订阅事件总线，事件命中即派发任务到管道

### 审批交互闭环（Approval Loop）

人机协同的质量闸，构成"生成→审批→反馈→迭代"闭环：

- **双审批模式**：`choice`（预设选项快速决策）/ `conversation`（多轮自由讨论），均支持自由文本输入
- **管道暂停/恢复**：`wait` 路由信号挂起管道并保存 state 快照，外部事件 `wake()` 恢复
- **反馈注入**：审批结果（通过/驳回/批注）注入管道 state，驱动 AI 返工
- **任务打回重做**：任务状态机支持打回，重新进入执行
- **版本对比**：`ReviewDiff` 组件（LCS 算法 side-by-side/unified）+ 后端 `get_version_diff` API + `annotation_service` 批注 CRUD 已具备

> 文本审批闭环已上线。审批请求携带制品（artifacts）的协议增强与工作区自动联动待补全（详见 [ROADMAP.md](../../ROADMAP.md)）。

### 强制评估系统（Mandatory Evaluation）

任务质量的硬约束，确保质量不被跳过。位于 `src/evaluation/` + `src/infrastructure/task_post_pipeline.py`：

- **提交即带指标**：`task_submit` 提交任务时须同时提交评估指标（acceptance criteria），校验指标 ID 合法性；支持从 Agent 的 `recommended_metrics` 自动补全
- **强制门控**：管道退出后若任务仍 RUNNING 且有产出，`task_post_pipeline.py` 强制 `move_to_evaluating` 并重跑评估——即使 Agent 不主动调 `task_evaluate` 也会被强制审查
- **按指标审查**：`EvaluationEngine` 分发 tool / agent / human 三类评估（`evaluator_agent` 执行单指标评估）；指标定义在 `config/evaluation_metrics/`（file_check / bash_check / human_review / semantic_check）
- **结果约束**：指标全过 → COMPLETED；失败未耗尽 → 反馈重试；失败耗尽 → FAILED
- **容器级验证**：`container_verification_agent` 做端到端验证-修复闭环（最多 3 轮）

### Skill 能力集成

可加载、可复用的技能（skill）包，按需注入 Agent 扩展领域能力。位于 `src/skills/`：

- **发现**：`SkillRegistry` 扫描 `skills/` 根目录（`DEFAULT_SKILL_ROOTS`）发现技能
- **按需注入**：技能可在 Agent 配置中声明引用，运行时注入对应能力
- **领域扩展**：无需改代码即可获得新领域能力（如文档处理 docx、PDF 生成 pdf 等）

---

## 数据流示例

### 场景：用户问"项目里昨天那个 bug 改了吗？"

```
1. 通道层（Web UI）
   └─ 用户发送消息 → WebSocket 推送到后端

2. 网关层
   ├─ 鉴权 / 限流
   ├─ 标准化消息格式
   └─ 路由到目标会话

3. 管道引擎
   ├─ 加载对话历史（情景记忆 EPISODE）
   └─ Input 插件链
       ├─ 动态变量注入（当前时间戳、项目路径、用户偏好）
       ├─ 提示词构建
       └─ 语义记忆检索（关键词/标签检索）
   └─ LLM 调用（流式）
       ├─ 推理：决定调用 enhanced_search + file_read
       └─ Output 插件：路由信号 = next_tool

4. 工具执行
   ├─ enhanced_search("昨天那个 bug")
   ├─ 命中：src/auth/login.py:42 的修复
   └─ file_read 验证 → 结果回传

5. 管道引擎（第二轮）
   ├─ LLM 收到工具结果
   └─ Output 插件：路由信号 = end

6. 流式响应
   └─ 推送到前端 → "已修复，在 src/auth/login.py:42。"
```

---

## 扩展点

| 扩展点 | 怎么做 | 涉及文件 |
|--------|--------|----------|
| 新增 Agent | 写 YAML | `config/agents/*.yaml` |
| 新增内置工具 | 实现 Tool 协议 + 注册 | `src/tools/builtin/` |
| 新增 MCP 服务 | 启动 MCP Server + 配置 | `mcp-servers/` |
| 新增 IM 通道 | 继承 `BaseComboAdapter` 实现 `adapter.py` | `src/channels/<platform>/` |
| 新增前端主题 | 写 TS 主题对象（预设）或 JSON（动态） | `frontend/src/config/themes/presets/` 或 `frontend/public/themes/` |
| 新增 Schema 表单 | 写 JSON Schema | 任何 `ui_schema` 字段 |
| 新增 Skill | 写技能包（含 SKILL.md + 实现）放到 skill 根目录 | `skills/` |
| 新增触发器类型 | 继承 `src/triggers/triggers/base.py` 基类 | `src/triggers/triggers/` |
| 新增审批视图 | 注册 review Widget + ui_schema 路由 | `frontend/src/components/review/`、`config/ui/modules/` |
| 自定义路由信号 | 扩展 `engine_route.py` 的路由分支 | `src/pipeline/engine_route.py` |

---

## 架构设计四问

灵汐在演进过程中始终按这四个问题审视架构决策：

### 1. 找"散"——同一个概念在代码里出现了几次？
> 多次出现 → 抽象缺失，需要统一封装。

**例**：原本工具的 `when_to_use` 在 5 个地方各自实现，统一抽象到 `Tool` 基类后由子类覆写。

### 2. 找"分叉点"——调用方为了同一个操作需要判断几种情况？
> 调用方有分叉 → 抽象边界错误，应封装在模块内部。

**例**：原本各通道自己判断消息类型，封装到 `ChannelAdapter` 后调用方只需 `await adapter.send_message(msg)`。

### 3. 找"谁该知道"——每个概念，谁需要知道它？
> 不该知道的人知道了 → 边界泄漏，需收回。

**例**：路由信号的细节原本对工具层可见，重构后工具只关心 `execute()` 的输入输出。

### 4. 找"变化方向"——什么会变，什么不会变？
> 把"会变的"封装在内部，"不变的"暴露为接口。

**例**：LLM Provider 实现会变（新增厂商、切换 API），但"调用 LLM 返回文本"这个动作不变。所以 Provider 实现藏在 `src/llm/`（及 `provider_adapters/`）内部，外部只看到 `llm.complete(messages)`。

---

## 进一步阅读

- [ROADMAP.md](../ROADMAP.md) —— 版本路线图与未来方向
- [CONTRIBUTING.md](../CONTRIBUTING.md) —— 贡献流程
- [CHANGELOG.md](../CHANGELOG.md) —— 版本变更记录

---

> 🌊 **灵汐架构的演进方向：让 AI Agent 的搭建像配置一台服务器一样简单，但能力上限不设限。**