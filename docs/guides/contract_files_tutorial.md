# 契约文件解析教程（面向新手）

> **目标读者**：刚加入灵汐 AgentOS 0.2 项目的开发者、插件作者、想理解"内核和插件到底是怎么约定合作的"的好奇者
> **读完你能懂**：项目里有哪些"契约文件"、它们各自规定了什么、为什么这么规定、在真实开发场景里怎么用

> **📍 现状勘误（相对 2026-07-13 调研时点的路径变更）**：
> - `docs/0.2_rust_plugin_solution.md` → 已归档至 `docs/working/_archive_0.2_migration/0.2_rust_plugin_solution.md`
> - `src/pipeline/plugin.py` / `src/pipeline/types.py` → 已随 0.1 `src/` 整体删除；0.2 等价契约：管道插件 Python 基类在 `plugins/shared/pipeline/_base/plugin.py`，类型定义在 `plugins/sdk/src/agentos_plugin_sdk/pipeline_types.py`
> - `.project/manifest_v2_schema.json` / `.project/mcp_extension_protocol.md` → 始终未作为独立文件产出；manifest 真值源 = `kernel/crates/core/src/traits.rs::PluginManifest`，协议文档 = `docs/plugin-protocol.md`
> - `docs/guides/plugin_development_guide.md` / `plugin_development_standard.md`（0.1）→ 已删除；现行开发指南 = `docs/guides/` 分篇（见 [README.md](README.md)）
> - `docs/ARCHITECTURE.md` → 已更新为 0.2 架构口径

---

## 〇、什么是"契约文件"？

> 这一节是给完全没接触过插件化架构的读者铺底的。如果你已经懂 "接口" / "协议" / "schema"，可以直接跳到第一节。

**生活里的契约**：
你和房东签租房合同——合同上写清楚：每月几号交租、房子几平米、能不能养猫、退租提前几天通知。这份"合同"就是契约。租客照合同执行，房东照合同办事，双方都不需要见面也能合作。

**软件里的契约**也是一回事**：
灵汐 0.2 项目里，内核（Rust 写的那个）和插件（可以是 Rust、Python、MCP 边车）运行在不同进程、不同语言里。它们怎么知道彼此要做什么？靠的就是 **契约文件**——提前写死的"合同"。

**契约文件长什么样？**

| 形式 | 例子 | 你可以把它理解为 |
|------|------|------------------|
| JSON Schema | `manifest_v2_schema.json` | 一份"填表说明"，规定插件 manifest 字段必须长啥样 |
| Markdown 协议文档 | `mcp_extension_protocol.md` | 一份"对话手册"，规定两边怎么说话 |
| Rust trait 定义 | `kernel/crates/core/src/traits.rs` | 一份"接口合同"，Rust 编译器照此校验代码 |
| Python 抽象基类 | `plugins/shared/pipeline/_base/plugin.py` | 一份"基类合同"，Python 解释器照此实例化插件 |
| 教程/规范文档 | `docs/guides/`（分篇指南） | 一份"开发者手册"，告诉你怎么写代码 |

**关键原则**：契约一旦定下来，**所有实现方都必须遵守**。这就是为什么灵汐把它们叫做"宪法"——后续所有插件、内核代码、Python SDK 都是围绕这些契约构建的。

---

## 一、契约文件总览

下面列出本次审查中找到的所有契约文件。先看一张总表，再逐个详细讲解。

> **表头说明**：`状态`列说明文件是否已实际产出。
> - ✅ 已存在：在仓库里能直接看到
> - ⏳ task_02 待产出：任务书里点名要产出，但当前仓库中尚未生成（教程引用其预期内容）
> - 🔁 双重身份：既是 0.1 旧版契约的对照参考，又是 0.2 演进基线

| # | 文件路径 | 形式 | 角色 | 状态 |
|---|---------|------|------|------|
| 1 | `docs/working/_archive_0.2_migration/0.2_rust_plugin_solution.md` | 方案总纲 | 0.2 整体方案的设计决策与 AC 总表（已归档） | ✅ 已存在 |
| 2 | `docs/working/0.2插件体系核心决策.md` | 决策文档 | 9+1 条插件体系核心决策（统一 trait / 状态隔离 / 路由信号精简等） | ✅ 已存在 |
| 3 | `docs/0.2_rust_plugin_checkpoints.md` | 检查点设计 | 13 个检查点 + 里程碑门控 | ✅ 已存在 |
| 4 | `.project/manifest_v2_schema.json` | JSON Schema | 插件 manifest V2.0 字段校验规范 | ⏳ task_02 待产出 |
| 5 | `.project/mcp_extension_protocol.md` | 协议文档 | MCP 灵汐扩展协议（`__kernel_*` 扩展消息） | ⏳ task_02 待产出 |
| 6 | `kernel/crates/core/src/lib.rs` | Rust 模块入口 | 0.2 内核 core crate 的模块组织入口 | ✅ 已存在 |
| 7 | `kernel/crates/core/src/traits.rs` | Rust Trait 定义 | 内核核心契约——PipelinePlugin / PluginInvoker / CapabilityRegistry / DependencyResolver / LlmProvider / PluginLoader | ✅ 已存在 |
| 8 | `kernel/crates/core/src/types.rs` | Rust 数据类型 | 核心共享类型——RouteType / ErrorPolicy / PluginResult / PluginContext / TenantContext / ToolCategory 等 | ✅ 已存在 |
| 9 | `plugins/shared/pipeline/_base/plugin.py` | Python 抽象基类 | 管道插件 Python 基类（IPlugin / IInputPlugin / ICorePlugin / IOutputPlugin / PluginContext / PluginResult / OutputResult），0.2 sidecar 管道插件业务层继承它 | ✅ 已存在 |
| 10 | `plugins/sdk/src/agentos_plugin_sdk/pipeline_types.py` | Python 类型定义 | StateKeys / RouteSignal / TargetType / create_initial_state 等 SDK 侧类型 | ✅ 已存在 |
| 11 | `config/templates/plugin_scaffold/core_plugin.py` | 脚手架模板 | Core 插件模板（含 `ICorePlugin` 继承） | ✅ 已存在 |
| 12 | `config/templates/plugin_scaffold/input_plugin.py` | 脚手架模板 | Input 插件模板（含 `IInputPlugin`、enabled 默认 True） | ✅ 已存在 |
| 13 | `config/templates/plugin_scaffold/output_plugin.py` | 脚手架模板 | Output 插件模板（含 `IOutputPlugin`、route_signals 声明） | ✅ 已存在 |
| 14 | `docs/guides/`（分篇指南） | 完整教程 | 插件开发总览 / sidecar / native / 外部 MCP / 主题 / Agent 配置 / 管道配置 / 排障 | ✅ 已存在 |
| 15 | `docs/plugin-protocol.md` | 协议权威 | plugin.json manifest 全字段 + echo_tool 从零走查 + SDK 速查 | ✅ 已存在 |
| 16 | `docs/ARCHITECTURE.md` | 架构总览 | 0.2 架构总览（设计哲学 / 子系统 / 数据流 / 扩展点） | ✅ 已存在 |

> **教程侧注**：表里 4 号和 5 号文件（`.project/` 下的 JSON Schema 和 MCP 扩展协议文档）未作为独立文件产出——manifest 真值源是 `kernel/crates/core/src/traits.rs::PluginManifest`（配 `docs/plugin-protocol.md` 说明）。下面讲解这两份"虚拟文件"的内容时，基于 [来源: docs/working/_archive_0.2_migration/0.2_rust_plugin_solution.md]、[来源: docs/working/0.2插件体系核心决策.md]、[来源: kernel/crates/core/src/traits.rs] 中已固化的字段名和决策反向推导，仅供理解。

---

## 二、逐个契约文件详解

### 2.1 方案总纲：`docs/working/_archive_0.2_migration/0.2_rust_plugin_solution.md`（已归档）

#### 这是什么？

0.2 项目的"路线图"。一份 286 行的方案总纲，回答"为什么从 0.1 迁到 0.2、迁到什么样子、怎么验证迁完了"。

#### 关键内容

**第一节"背景与目标"**——对比 0.1 和 0.2 的差异：

| 维度 | 0.1 现状 | 0.2 目标 |
|------|----------|----------|
| 多租户并行 | Python GIL 限制并发 | Rust 无 GIL，tokio work-stealing |
| 部署依赖 | Python 运行时 + 大量第三方库 | 单一二进制，零运行时依赖 |
| 内核/插件分离 | 同进程同语言 | MCP 协议解耦，可多语言 |
| 性能瓶颈 | asyncio 单线程 + GC | tokio 多线程 + 零成本抽象 + 无 GC |

**第三节"关键决策理由"**——这是教程最关心的部分，藏着 5 条核心决策（详细理由都列在文档里）：

1. **选 MCP 协议而非自研 RPC**（§3.1）
2. **管道插件混合方案**（§3.2）：高频用 Rust 原生（零 IPC 开销），低频用 MCP 边车
3. **路由信号从 6 种精简为 4 种**（§3.5）：删掉 `delegate` 和 `fork`
4. **多租户用 `tokio::task_local!`**（§3.4）：隐式穿透而非显式传参
5. **按需加载全局原则**（§3.7）：所有插件和系统组件都不预启动，空闲超时自动卸载

**第四节"方案级 AC 总表"**——16 条验收标准（AC-1 到 AC-16），每条都有"如何验证"的具体方法。task_02 关注的是 AC-9（Manifest 规范 V2.0 固化并可校验）。

#### 实际场景

- **新人入职第一周**：先读这份文档的 §1 和 §3，搞清楚"0.2 是什么、要解决什么问题"
- **产品经理/架构师**：拿 §3 的决策作为讨论基线，知道每条决策的"否决项"和来由
- **QA 工程师**：直接照 §4 的 verify_hint 写验收测试

> **来源**：[来源: docs/working/_archive_0.2_migration/0.2_rust_plugin_solution.md, §1-4]

---

### 2.2 核心决策文档：`docs/working/0.2插件体系核心决策.md`

#### 这是什么？

把方案总纲里"插件体系"那一支抽出来深挖的一份决策清单。362 行，9+1 条决策，每条都有"决策 / 含义 / 否决项"三段式。

#### 关键决策精解

**决策 1：插件 = 输入 → 输出 + 副作用**

> 所有插件（管道、工具、触发器、钩子）本质上都是同一个模型——给我输入，我给你输出，可能顺便产生副作用（写文件、记日志、调外部 API）。

**决策 4：统一 trait 契约 `execute(Value) -> Value`**

> 这是 0.2 的灵魂决策之一。**否决了** "PipelinePlugin / ToolPlugin / HookPlugin 多个特化 trait" 的方案——插件分类靠 manifest 的 `capabilities` 标签，不靠 trait 类型。

**决策 6：state 是管道引擎的固定契约**

> state（管道执行过程中的状态字典）**只存在于管道引擎内部**。工具插件不接触 state——工具插件的契约是 args/result，跟 state 无关。

**决策 7：state 隔离**

> 每个管道（含子管道）拥有**独立 state**，互不共享。"看起来像共享"的数据流，本质都是消费者显式编排的。

**决策 10：子管道由专门服务触发**

> 0.2 **不保留** 0.1 的 `delegate` 和 `fork` 路由信号。管道引擎不主动 spawn 子管道；子任务、复盘等需求由"任务系统""复盘系统"等专门服务自己调度。**这直接导致路由信号从 6 种精简为 4 种**：

| 0.1 路由信号 | 含义 | 0.2 保留 |
|---|---|---|
| `NextLlm` | 下一轮调 LLM | ✅ |
| `NextTool` | 执行工具 | ✅ |
| `End` | 管道结束 | ✅ |
| `Wait` | 挂起等外部事件 | ✅ |
| `Delegate` | 同步委派子管道 | ❌ 删 |
| `Fork` | 异步分叉子管道 | ❌ 删 |

#### 实际场景

- **新插件作者**：写插件前先读决策 1 / 4 / 6，搞清楚"我写的到底是什么"
- **架构师讨论**：用决策 3（消费者/执行者分离）和决策 7（state 隔离）作为讨论框架
- **代码审查**：拿决策 4 / 6 / 10 作为判断"这代码写得是否符合 0.2 心智"的尺子

> **来源**：[来源: docs/working/0.2插件体系核心决策.md, 决策 1-10]

---

### 2.3 检查点设计：`docs/0.2_rust_plugin_checkpoints.md`

#### 这是什么？

13 个检查点（CP-01 到 CP-13）的清单，对应 0.2 项目的 13 个子任务。每个检查点都有"类型 / 触发条件 / 检查内容 / 通过标准 / 不通过处理"。

#### 关键内容

**里程碑门控（最高优先级）**：

| 里程碑 | 位置 | 重要性 | 人类参与 |
|--------|------|--------|----------|
| **CP-02 协议冻结** | task_02 完成后 | 🔴 极高 | ✅ 必须人类确认 |
| CP-06 内核可用 | task_06 完成后 | 🔴 极高 | Agent 互审 |
| CP-08 SDK 可用 | task_08 完成后 | 🟡 高 | ✅ 建议人类确认 |
| **CP-13 发布门控** | task_13 完成后 | 🔴 极高 | ✅ 必须人类确认 |

**为什么 CP-02 是协议冻结？** ——"协议是后续所有开发的'宪法'，冻结后不可随意修改"。本教程解析的所有契约文件，都是 CP-02 要审查的对象。

#### 实际场景

- **项目经理**：拿这 13 个检查点作为里程碑
- **Agent 互审**：每个 task 完成后，照"通过标准"逐条检查
- **人类决策**：CP-02 / CP-13 必须人类确认，不能跳过

> **来源**：[来源: docs/0.2_rust_plugin_checkpoints.md, CP-02 章节]

---

### 2.4 Manifest V2.0 JSON Schema：`.project/manifest_v2_schema.json`

> ⚠️ **状态说明**：此文件是 task_02 的产出目标，当前仓库 `.project/` 目录尚未生成。以下内容基于 [来源: kernel/crates/core/src/traits.rs PluginManifest / ManifestCapabilities / ManifestPermissions 等结构体] 反向推导，等价于"如果它现在存在，会长什么样"。

#### 这是什么？

每个插件都要配一份 `plugin.json`（manifest），告诉内核"我是谁、我能做什么、我要什么"。这份 JSON Schema 就是 manifest 的"填表说明"——内核加载插件时，按这份 Schema 校验 manifest 是否合法。

#### 关键字段（按 PluginManifest 结构体）

| 字段 | 类型 | 必填 | 含义 | 实际场景举例 |
|------|------|------|------|--------------|
| `id` | string | ✅ | 插件唯一标识 | `"memory_read"` |
| `name` | string | ✅ | 人类可读名称 | `"记忆读取"` |
| `version` | string | ✅ | 语义化版本 | `"1.2.0"` |
| `plugin_type` | enum | ✅ | 插件类型 | `"pipeline"` / `"tool"` / `"system"` |
| `pipeline_role` | enum | ❌ | 管道角色（仅 pipeline 类型） | `"input"` / `"core"` / `"output"` |
| `language` | string | ✅ | 实现语言 | `"rust"` / `"python"` / `"typescript"` |
| `host_type` | enum | ✅ | 宿主类型 | `"in_process"`（Rust 原生）/ `"sidecar"`（MCP 边车） |
| `entry` | string | ✅ | 入口点 | `"MyPlugin"`（类名）或 `"main.py:serve"` |
| `capabilities` | object | ✅ | 能力声明（详见下表） | 见下方 |
| `dependencies` | array | ❌ | 依赖的其他插件 | `[{"plugin_id": "tool_schema"}]` |
| `permissions` | object | ❌ | 权限申请（默认空） | 见下方 |
| `error_policy` | enum | ❌ | 已收敛为唯一值 `retry` 并整体移除（ADR 2026-08-18），不要再声明 | `"retry"`（缺省值，字段非必填） |
| `priority` | int | ❌ | 优先级，默认 100 | 数值越小越先执行 |
| `mcp` | object | ❌ | MCP 配置（仅 sidecar 类型） | 见下方 |

**`capabilities` 子字段**：

| 子字段 | 含义 | 例子 |
|--------|------|------|
| `tools[]` | 提供的工具列表 | `[{"name": "web_search", "input_schema": {...}}]` |
| `resources[]` | 暴露的数据源 | `[{"uri": "config://agents", "mime_type": "application/json"}]` |
| `route_signals[]` | 可能产出的路由信号 | `["next_llm", "end"]` |
| `lifecycle_hooks[]` | 订阅的生命周期事件 | `["on_load", "on_pipeline_start"]` |

**`permissions` 子字段**：

| 子字段 | 含义 | 例子 |
|--------|------|------|
| `filesystem.read_paths` | 允许读的路径 | `["/tmp/cache", "/var/log"]` |
| `filesystem.write_paths` | 允许写的路径 | `["/tmp/output"]` |
| `network.allowed_hosts` | 允许访问的域名 | `["api.openai.com"]` |
| `env_vars[]` | 允许读的环境变量名 | `["OPENAI_API_KEY"]` |
| `system_calls[]` | 允许的系统调用 | `["bash_execute"]` |

**`mcp` 子字段（仅 sidecar 类型）**：

| 子字段 | 含义 | 默认值 |
|--------|------|--------|
| `transport` | 传输方式 | `"stdio"` |
| `idle_timeout_secs` | 空闲超时（秒） | 300 |
| `protocol_version` | MCP 协议版本 | `"2025-06-18"` |

#### 实际场景

- **插件作者**：写 `plugin.json` 时，照 Schema 一个个字段填。填错字段名/类型，内核加载时直接报错"schema validation failed"。
- **内核开发者**：`validate_manifest()` 函数按这份 Schema 校验用户传入的 manifest，校验通过才进 `discover()` 列表。

> **来源（反向推导）**：[来源: kernel/crates/core/src/traits.rs §7 PluginManifest / ManifestCapabilities / ManifestPermissions / McpConfig / HostType]

---

### 2.5 MCP 灵汐扩展协议：`.project/mcp_extension_protocol.md`

> ⚠️ **状态说明**：此文件未作为独立文件产出（协议以官方 MCP 标准为准）。以下基于 [来源: kernel/crates/core/src/traits.rs LifecycleHook 枚举]、[来源: docs/working/_archive_0.2_migration/0.2_rust_plugin_solution.md §3.1（MCP 选型）]、[来源: docs/working/0.2插件体系核心决策.md] 反向推导预期内容。

#### 这是什么？

MCP（Model Context Protocol）是 Anthropic 提出的 AI 工具通信标准。灵汐 0.2 在标准 MCP 之上加了**自己的扩展消息**，用来注入内核能力（生命周期钩子、依赖注入、状态快照等）。这份文档就是扩展协议的"对话手册"。

#### 标准 MCP 是什么？

> 把 MCP 想象成"插件和内核之间打电话的标准流程"：
>
> 1. **initialize**（插件上线）：插件说"我是 xxx，我能做 yyy"，内核说"行，给你分配个 ID"
> 2. **tools/list**（查我能做什么）：内核问"你有啥工具"，插件回"我有 web_search / file_read..."
> 3. **tools/call**（帮我跑一下）：内核说"用 web_search 查 Python 教程"，插件回"找到 10 条结果"
> 4. **resources/list + resources/read**（读数据源）：插件可以暴露配置、日志等数据源供内核读
> 5. **notifications**（异步通知）：插件主动推"我加载完了"、"管道开始了"等事件

#### 灵汐扩展：3 条 `__kernel_*` 消息

灵汐在标准 MCP 之上加了 3 条扩展消息（约定以 `__kernel_` 前缀开头，便于和标准 MCP 区分）：

| 扩展消息 | 方向 | 用途 |
|---------|------|------|
| `__kernel_inject_capabilities` | 内核 → 插件 | 初始化时由内核把"我（内核）能提供什么"注入插件（如租户上下文句柄、事件总线句柄、配置读取句柄） |
| `__kernel_lifecycle_hook` | 内核 → 插件 | 内核触发生命周期钩子事件（`on_load` / `on_unload` / `on_pipeline_start` / `on_pipeline_end` / `on_error`） |
| `__plugin_lifecycle_event` | 插件 → 内核 | 插件异步上报生命周期事件（如"我的配置已更新"、"我重启了"） |

**对应到 Rust trait 的入口**：

```rust
// PluginInvoker trait（kernel/crates/core/src/traits.rs §3）
async fn send_lifecycle_hook(
    &self,
    plugin_id: &str,
    hook: LifecycleHook,        // 对应 __kernel_lifecycle_hook 的 hook 字段
    context: &HookContext,      // 携带 session_id / task_id / tenant_id / pipeline_id / iteration
) -> Result<(), PluginError>;
```

**`__kernel_*` 系统工具命名约定**：内核自身也通过 MCP 暴露一些"系统工具"，命名以 `__kernel_` 开头（如 `__kernel_inject_capabilities`、`__kernel_emit_event`），与插件提供的"业务工具"在 tools/list 里清晰区分。

**`resources` 约定**：插件暴露 resources 时，URI 推荐用 `plugin://{plugin_id}/{path}` 形式（如 `plugin://memory_read/long_term`），避免与他人冲突。

**`notifications` 事件名**：插件发出的 notifications 推荐用 `{plugin_id}.{event_name}` 形式（如 `memory_read.index_updated`），方便订阅者过滤。

#### 实际场景

- **Python SDK 作者**：照这份协议实现 MCP 服务端，标准 MCP 部分用官方 SDK，扩展消息按本文档实现
- **插件作者**：用 SDK 注册扩展消息的处理函数（一般 SDK 会封装成装饰器，不必手写协议）
- **内核开发者**：照 `PluginInvoker` trait 的方法名调用扩展消息

> **来源（反向推导）**：[来源: kernel/crates/core/src/traits.rs §3 LifecycleHook / HookContext]、[来源: docs/working/_archive_0.2_migration/0.2_rust_plugin_solution.md §3.1 MCP 选型]

---

### 2.6 Rust 模块入口：`kernel/crates/core/src/lib.rs`

#### 这是什么？

0.2 内核 `core` crate 的模块入口文件，27 行，定义"core crate 由哪些模块组成"。

#### 关键内容

```rust
//! # Lingxi AgentOS 0.2 — Kernel Core Library
//! 本 crate 是 0.2 架构的"宪法层"，定义内核与所有插件之间的接口契约。

pub mod traits;  // 插件抽象接口
pub mod types;   // 共享数据结构
```

**就两个模块**：

| 模块 | 角色 |
|------|------|
| `traits` | 插件抽象接口（PipelinePlugin、PluginInvoker、CapabilityRegistry、DependencyResolver、LlmProvider、PluginLoader） |
| `types` | 共享数据结构（RouteSignal、ErrorPolicy、PluginContext、PluginResult、PluginError、TenantContext 等） |

#### 实际场景

- **新人**：看到这个 `lib.rs`，立刻知道 0.2 内核代码库就两大块——接口契约（traits）和共享类型（types），一清二楚
- **架构师**：`lib.rs` 的注释就是 0.2 的"组织架构图"

> **来源**：[来源: kernel/crates/core/src/lib.rs, 模块注释 + pub mod 声明]

---

### 2.7 Rust Trait 定义：`kernel/crates/core/src/traits.rs` ⭐

> 这是**最重要的契约文件之一**。755 行，定义了 0.2 内核的全部 trait 抽象。Rust 编译器会照此 trait 静态校验代码合规性。

#### 这是什么？

插件和内核之间的**Rust 语言级契约**。每个 trait 规定"实现方必须提供哪些方法、每个方法的签名长什么样"。

#### 关键 trait 一览

| Trait | 行号区间 | 角色 | 实际场景 |
|-------|----------|------|----------|
| `PluginMeta` | §1（25-47） | 插件元信息（id / name / version / plugin_type / error_policy / priority） | 内核从 manifest 提取后构造 `PluginMeta` 实例 |
| `PluginType` / `PipelineRole` 枚举 | §1（49-71） | 区分 Pipeline / Tool / System；Input / Core / Output | 决定插件被哪种执行器调 |
| `PipelinePlugin` | §2（82-112） | **核心 trait**——所有管道插件的入口 | 内核的 PluginInvoker 按 `dyn PipelinePlugin` 动态分发 |
| `InputPipelinePlugin` / `CorePipelinePlugin` / `OutputPipelinePlugin` | §2（120-158） | 三个子 trait，固定 role() 返回值 | 按角色实现不同 execute 逻辑 |
| `PluginInvoker` | §3（168-196） | **核心 trait**——插件调用器，按 host_type 透明分发 | 管道引擎唯一调用入口 |
| `LifecycleHook` / `HookContext` | §3（198-250） | 生命周期钩子类型 + 上下文 | 对应 `__kernel_lifecycle_hook` 消息 |
| `CapabilityRegistry` | §4（260-294） | 能力注册表——Tools / Resources / RouteSignals | 内核加载完插件后注册能力 |
| `DependencyResolver` | §5（331-341） | 依赖解析器——拓扑排序 + 环检测 | 内核实例化插件前调用 |
| `LlmProvider` | §6（384-412） | LLM 抽象层 + 流式 + 列表 | Core 插件的"模型调用方" |
| `PluginLoader` | §7（568-589） | 插件加载器——发现 / 校验 / 加载 / 卸载 | 内核启动时调用 |
| `PluginManifest` / `HostType` / `McpConfig` / `PluginStatus` | §7（591-755） | 运行时 manifest 表示 + 状态机 | 内核在内存中维护 |

#### 每个关键 trait 详细讲解

**`PipelinePlugin` trait**——管道插件的统一接口：

```rust
#[async_trait]
pub trait PipelinePlugin: PluginMeta + Any {
    fn role(&self) -> PipelineRole;
    async fn execute(&self, ctx: &PluginContext) -> Result<PluginResult, PluginError>;
    fn route_signals(&self) -> Vec<RouteType> { Vec::new() }  // 默认空
    async fn on_load(&self) -> Result<(), PluginError> { Ok(()) }  // 默认空
    async fn on_unload(&self) -> Result<(), PluginError> { Ok(()) }  // 默认空
}
```

| 方法 | 必须实现？ | 含义 |
|------|----------|------|
| `role()` | ✅ | 返回 Input / Core / Output |
| `execute()` | ✅ | 插件核心逻辑：拿到 ctx（PluginContext），返回 PluginResult |
| `route_signals()` | ❌ 默认空 | 声明本插件可能产出哪些路由信号（仅 Output 角色有效） |
| `on_load()` / `on_unload()` | ❌ 默认空 | 生命周期钩子，需要时再覆盖 |

**`PluginInvoker` trait**——插件调用器：

```rust
#[async_trait]
pub trait PluginInvoker: Send + Sync {
    async fn invoke_pipeline_plugin(&self, plugin_id: &str, ctx: &PluginContext)
        -> Result<PluginResult, PluginError>;
    async fn invoke_tool(&self, plugin_id: &str, tool_name: &str, inputs: &serde_json::Value)
        -> Result<ToolExecutionResult, PluginError>;
    async fn send_lifecycle_hook(&self, plugin_id: &str, hook: LifecycleHook, context: &HookContext)
        -> Result<(), PluginError>;
}
```

> **关键洞察**：内核根据插件的 `host_type` 字段选择调用路径：
> - `InProcess`（Rust 原生）：直接 `dyn PipelinePlugin::execute`，**零 IPC 开销**
> - `Sidecar`（MCP 边车）：通过 rmcp 客户端走 MCP 协议 `tools/call("execute", {state, config})`
>
> 两种路径对管道引擎透明——统一返回 `PluginResult`。这就是 0.2 "混合方案"的实现机制。

**`CapabilityRegistry` trait**——能力注册表：

```rust
#[async_trait]
pub trait CapabilityRegistry: Send + Sync {
    fn register_tool(&self, plugin_id: &str, tool: ToolDescriptor);
    fn unregister_tools(&self, plugin_id: &str);
    fn get_tool(&self, name: &str) -> Option<ToolDescriptor>;
    fn list_tools(&self) -> Vec<ToolDescriptor>;
    fn list_tools_by_category(&self, category: &ToolCategory) -> Vec<ToolDescriptor>;
    fn register_resource(&self, plugin_id: &str, resource: ResourceDescriptor);
    fn unregister_resources(&self, plugin_id: &str);
    fn list_resources(&self) -> Vec<ResourceDescriptor>;
    fn register_route_signals(&self, plugin_id: &str, signals: Vec<RouteType>);
    fn has_route_signal(&self, signal: &RouteType) -> bool;
    fn clear_plugin(&self, plugin_id: &str);
}
```

> 三类能力统一管理：Tools（供 LLM 选择调用）、Resources（数据源）、RouteSignals（路由表校验）。

**`LlmProvider` trait**——LLM 抽象层：

```rust
#[async_trait]
pub trait LlmProvider: Send + Sync {
    async fn complete(&self, model: &str, messages: &[LlmMessage], options: &LlmOptions)
        -> Result<LlmResponse, LlmError>;  // 非流式
    async fn complete_stream(&self, model: &str, messages: &[LlmMessage], options: &LlmOptions)
        -> Result<tokio::sync::mpsc::Receiver<LlmStreamChunk>, LlmError>;  // 流式
    async fn list_models(&self) -> Result<Vec<ModelInfo>, LlmError>;
}
```

> **设计原则（决策 4）**：LLM 实现会变（OpenAI / Anthropic / 本地模型），但"调用 LLM 返回文本"这个动作不变。抽象层长期保留，具体实现藏在各自模块。

#### 实际场景

- **Rust 插件作者**：实现 `InputPipelinePlugin` / `CorePipelinePlugin` / `OutputPipelinePlugin` 之一，覆盖 `execute()` 方法。编译时 Rust 编译器立刻告诉你"方法签名不对"。
- **内核开发者**：`PipelinePlugin` 是 0.2 的"运行时类型"，所有插件都是 `dyn PipelinePlugin` 的化身。
- **架构师**：所有 trait 的方法签名加在一起，就是 0.2 内核和插件的"完整对话手册"。

> **来源**：[来源: kernel/crates/core/src/traits.rs, 全文 755 行]

---

### 2.8 Rust 数据类型：`kernel/crates/core/src/types.rs`

#### 这是什么？

0.2 内核的"共享词汇表"。311 行，定义 trait 之间传递的所有数据结构的形状。

#### 关键类型

| 类型 | 角色 | 实际场景 |
|------|------|----------|
| `RouteType` 枚举 | 路由类型（4 种） | `NextLlm` / `NextTool` / `End` / `Wait` |
| `RouteSignal` struct | 路由信号包 | Output 插件返回 `PluginResult.route_signal` |
| `ErrorPolicy` 枚举 | 错误策略（已收敛为唯一值 `retry`，ADR 2026-08-18） | `Retry` |
| `PluginResult` struct | 插件执行结果 | 包含 `state_updates` + `route_signal` + `skip_remaining` + `error` |
| `PluginError` struct | 插件错误 | `message` + `code` + `source` |
| `PluginContext` struct | 插件执行上下文 | 包含 `state` + `config` + `tenant` + `pipeline_id` + `session_id` + `task_id` |
| `TenantContext` struct | 多租户上下文 | 通过 `tokio::task_local!` 穿透 |
| `TargetType` 枚举 | 执行目标类型 | `LlmCall` / `ToolExecute` |
| `ToolCategory` 枚举 | 工具分类 | File / FileSystem / Search / Web / Memory / Task / System / Execution / Analysis / Evaluation / Agent / Monitoring |
| `ToolSource` 枚举 | 工具来源 | Builtin / Mcp / Custom / Database |
| `ToolExecutionResult` struct | 工具执行结果 | `success` + `data` + `error` + `duration_ms` |

#### `RouteType` 详解（4 种精简）

```rust
pub enum RouteType {
    NextLlm,    // 下一轮调用 LLM
    NextTool,   // 执行工具
    End,        // 结束管道
    Wait,       // 挂起等待外部事件
}
```

> **关键变更**：0.1 有 6 种（`next_llm` / `next_tool` / `end` / `delegate` / `wait` / `decision`），0.2 删除 `delegate` 和 `fork`，精简为 4 种。详见 0.2插件体系核心决策 §决策 10。

#### `ErrorPolicy` 详解（已收敛，不再声明）

> **ADR 2026-08-18**：0.2 引擎**不再按 `error_policy` 分发行为**。枚举已收敛为唯一值
> `retry`（`abort` / `skip` / `fallback` 已删除）；错误处理由引擎/编排层按错误类型决定
> （瞬态 sidecar 崩溃→retry 一次；工具失败→回喂 LLM 自我修正；非瞬态→上抛编排层）。
> 插件**不要再声明** `error_policy`（manifest 字段可选，缺省即 retry）。

```rust
pub enum ErrorPolicy {
    Retry,  // 瞬态错误重试一次（invoker with_transparent_recovery）；其余上抛编排层
}
```

| `Retry` | 瞬态错误重试一次；其余错误决策上抛编排层（ADR 2026-08-18） | — |

#### `PluginContext` 详解

```rust
pub struct PluginContext {
    pub state: serde_json::Value,       // 管道当前状态
    pub config: serde_json::Value,      // 插件配置
    pub tenant: TenantContext,          // 租户上下文
    pub pipeline_id: Uuid,              // 管道 ID
    pub session_id: String,             // 会话 ID
    pub task_id: String,                // 任务 ID
}
```

> 插件拿到 ctx 就能读到所有"环境信息"。**state 用 `serde_json::Value`**——因为 state 是引擎和插件之间的固定契约，shape 由引擎定义，插件遵守（决策 6）。

#### 实际场景

- **插件作者**：实现 `execute()` 时，从 ctx 读 state / config / tenant，向 PluginResult 写 state_updates / route_signal / error
- **架构师**：这些类型加在一起，就是 0.2 的"数据字典"

> **来源**：[来源: kernel/crates/core/src/types.rs, 全文 311 行]

---

### 2.9 Python 插件基类：`plugins/shared/pipeline/_base/plugin.py`

#### 这是什么？

sidecar 管道插件业务层的 Python 抽象基类（ABC）——`server.py` MCP 适配层之下的业务实现继承它。（0.1 的 `src/pipeline/plugin.py` 已随 src/ 删除，本文件是它的 0.2 承接者，心智模型一致。）

#### 关键类

| 类 | 角色 | 与内核 Rust 侧的对应 |
|-----|------|---------------|
| `IPlugin` | 插件抽象基类，所有管道插件的统一接口 | 对应 `PipelinePlugin` trait |
| `IInputPlugin(IPlugin)` | Input 插件基类 | 对应 `InputPipelinePlugin` trait |
| `ICorePlugin(IPlugin)` | Core 插件基类，返回 dict | 对应 `CorePipelinePlugin` trait |
| `IOutputPlugin(IPlugin)` | Output 插件基类，返回 OutputResult | 对应 `OutputPipelinePlugin` trait |
| `PluginContext` | 插件上下文（state / config / `_services`） | 对应 `PluginContext` struct |
| `PluginResult` | 插件结果（state_updates / route_signal / skip_remaining / error） | 对应 `PluginResult` struct |
| `OutputResult(PluginResult)` | Output 插件专用结果 | 对应 `PluginResult` + `route_signal` |

#### Python 业务层 vs 内核 Rust 契约对比

| 维度 | 0.1 Python | 0.2 Rust |
|------|-----------|----------|
| 抽象机制 | ABC + `@abstractmethod` | Rust trait + `#[async_trait]` |
| 错误处理 | raise exception → 由 plugin chain 按 error_policy 处理 | 返回 `Result<PluginResult, PluginError>`（引擎不按 error_policy 分发，ADR 2026-08-18） |
| state 类型 | `dict[str, Any]` | `serde_json::Value` |
| execute 返回类型 | Input→PluginResult, Core→dict, Output→OutputResult | 全部→PluginResult（统一） |
| 路由信号 | `route_type: str`（无类型约束） | `route_type: RouteType`（强类型枚举） |
| 服务访问 | `ctx.get_service(name)`（运行时查表） | 通过 PluginContext 直接字段访问 |

#### 实际场景

- **sidecar 管道插件作者**：继承 `IInputPlugin` / `ICorePlugin` / `IOutputPlugin`，实现 `execute()`（`server.py` 适配层负责 MCP 协议，业务类只管 state 读写）
- **native 插件作者**：按同样的 state 读写语义实现内核侧 `PipelinePlugin` trait（见 [guides/plugin-native-rust.md](plugin-native-rust.md)）
- **对照理解者**：看 Python 业务层 vs Rust trait 对比，理解"同一契约在两种语言里的形态"

> **来源**：[来源: plugins/shared/pipeline/_base/plugin.py]

---

### 2.10 Python 类型定义：`plugins/sdk/src/agentos_plugin_sdk/pipeline_types.py`

#### 这是什么？

SDK 侧管道类型：StateKeys（state 字段名常量）、RouteSignal（路由信号数据类）、TargetType（执行目标枚举）、`create_initial_state` 工厂函数——sidecar 管道插件的 server.py 适配层用它构造 `PluginContext`。

#### 关键内容

- **StateKeys**——state 字段名常量（ITERATION / CORE_TYPE / ENDED / SESSION_ID / RAW_RESULT / RAW_TOOL_CALLS / RAW_THINKING ...）
- **RouteSignal**——`@dataclass`，字段：`route_type: str` + `target` + `reason` + `payload`（内核侧对应强类型 `RouteType` 枚举，4 种）

#### 实际场景

- **sidecar 管道插件作者**：server.py 适配层用 `create_initial_state(**state)` + `PluginContext` 组装执行上下文（见 [guides/plugin-sidecar-python.md](plugin-sidecar-python.md) 示例 C）
- **state 字段约定**：StateKeys 是项目级约定，内核与插件两侧字段名一致

> **来源**：[来源: plugins/sdk/src/agentos_plugin_sdk/pipeline_types.py]

---

### 2.11-2.13 插件脚手架模板：`config/templates/plugin_scaffold/*.py`

#### 这是什么？

3 个 Python 文件脚手架模板，分别对应 Input / Core / Output 三种插件类型。开发者复制模板后填实际逻辑。

#### 关键模板对照

| 模板 | 继承的基类 | 关键差异 |
|------|----------|---------|
| `core_plugin.py` | `ICorePlugin` | 构造函数读 `enabled` 配置 |
| `input_plugin.py` | `IInputPlugin` | 构造函数读 `enabled` 配置 |
| `output_plugin.py` | `IOutputPlugin` | 有 `route_signals` property（默认返回 `[]`） |

#### 模板的核心结构（以 output_plugin.py 为例）

```python
class {PluginClass}(IOutputPlugin):
    @property
    def route_signals(self) -> list[str]:
        """本插件可能产出的路由信号类型列表。"""
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:
        if not self._enabled:
            return OutputResult()
        try:
            state_updates: dict[str, Any] = {}
            # TODO: 实现插件核心逻辑
            return OutputResult(state_updates=state_updates)
        except Exception as e:
            return OutputResult(error=e)
```

#### 实际场景

- **新插件作者**：复制 `output_plugin.py` → 改类名 → 填 TODO。一份 30 行起步的合规插件就出来了。
- **code review**：照模板检查新插件是否符合契约（route_signals / 构造函数 / execute 签名；**不声明** error_policy）

> **来源**：[来源: config/templates/plugin_scaffold/core_plugin.py / input_plugin.py / output_plugin.py]

---

### 2.14 插件开发分篇指南：`docs/guides/`

#### 这是什么？

按任务组织的开发指南分篇（索引见 [README.md](README.md)）：[plugin-development.md](plugin-development.md)（总览/目录/注册/命名与 State 约定）、[plugin-sidecar-python.md](plugin-sidecar-python.md)（Python 边车，含工具/服务/管道三示例与测试规范）、[plugin-native-rust.md](plugin-native-rust.md)（Rust cdylib）、[plugin-external-mcp.md](plugin-external-mcp.md)、[theme-development.md](theme-development.md)、[agent-configuration.md](agent-configuration.md)、[pipeline-configuration.md](pipeline-configuration.md)、[troubleshooting.md](troubleshooting.md)。

#### 实际场景

- **新手**：总览 → sidecar 分篇 → 配置两篇，30 分钟写完第一个插件
- **排障**：troubleshooting.md 的"为什么不生效"对照表

> **来源**：[来源: docs/guides/README.md]

---

### 2.15 插件协议权威文档：`docs/plugin-protocol.md`

#### 这是什么？

plugin.json manifest 全字段规范（字段总表 / capabilities / requires_services / 外部 MCP 接入 / 双根发现 / config_refs / ui_schema）+ 从零开发 echo_tool 完整走查 + SDK 用法速查 + 调试 FAQ。命名规范与 State 命名空间约定的现行版本在 [guides/plugin-development.md §8](plugin-development.md#8-命名与-state-约定)。

> **来源**：[来源: docs/plugin-protocol.md]

---

### 2.16 架构总览：`docs/ARCHITECTURE.md`

#### 这是什么？

0.2 架构总览。面向"希望深入了解灵汐内部机制、进行二次开发或参与核心贡献"的开发者。

#### 关键章节

**设计哲学**——三个核心原则：

1. **配置优于代码**（Configuration over Code）：几乎所有运行时行为都通过 YAML / 配置文件定义
2. **状态可观测**（Observable State）：每一步决策都建模为"路由信号"，写入事件流
3. **可回滚、可热替换**（Rollback & Hot Swap）：所有运行时配置和插件都支持 hot_swap 和 rollback

**总体架构**——五层结构图：Channels（Web/CLI/HTTP API）→ Gateway（协议解析/鉴权/限流）→ Pipeline Engine（Input/Output 插件链 + 4 种路由信号仲裁）→ Tools/Memory/Agents/Triggers → Infrastructure（Redis/LLM/MCP/FS）。

**核心子系统**——管道引擎、Agent 系统、工具系统、记忆系统、配置系统、通道层、容器任务系统、隔离与工作区、复盘与记忆维护、触发器系统、审批交互闭环、强制评估系统、Skill 能力集成。

**数据流示例 + 扩展点 + 架构设计四问**——提供完整端到端示例和"扩展新工具/新 Agent/新管道"的具体步骤。

#### 实际场景

- **新人入职第一周**：先读"设计哲学"和"总体架构"，建立全局心智模型
- **二次开发者**：照"扩展点"一节，加新工具/新 Agent/新管道
- **0.2 迁移参考**：0.1 的子系统划分是 0.2 重写时的"功能清单"基线

> **来源**：[来源: docs/ARCHITECTURE.md, 全文 435 行]

---

## 三、契约文件之间的依赖关系

读懂单个文件只是第一步，下面画出契约文件的"依赖地图"，帮你理解"如果 X 改了，Y 一定要跟着改"。

```
方案层（决策依据）
└─ docs/working/_archive_0.2_migration/0.2_rust_plugin_solution.md  ◄── 总路线图（已归档）
   └─ docs/working/0.2插件体系核心决策.md    ◄── 插件体系决策
      └─ docs/0.2_rust_plugin_checkpoints.md ◄── 里程碑检查

契约层（不可变接口）
├─ kernel/crates/core/src/traits.rs          ◄── Rust trait（manifest 真值源）
├─ kernel/crates/core/src/types.rs           ◄── Rust 类型
├─ kernel/crates/core/src/lib.rs             ◄── 模块入口
├─ plugins/shared/pipeline/_base/plugin.py   ◄── Python 管道插件基类
└─ plugins/sdk/src/agentos_plugin_sdk/       ◄── Python SDK（pipeline_types 等）

模板层（脚手架）
└─ config/templates/plugin_scaffold/*.py    ◄── 3 种插件模板

规范层（开发者手册）
├─ docs/plugin-protocol.md                  ◄── 协议权威
├─ docs/guides/（分篇指南）                  ◄── 上手教程 + 排障
└─ docs/ARCHITECTURE.md                     ◄── 架构总览
```

**关键依赖**：

- `docs/plugin-protocol.md` ⇄ `traits.rs::PluginManifest`：字段必须一一对应
- `plugin_scaffold/*.py` ⇄ `plugins/shared/pipeline/_base/plugin.py`：模板必须继承该基类
- SDK `pipeline_types.py` ⇄ 内核 `types.rs`：state 字段名两侧一致

---

## 四、新手入门建议（按这份教程学习的顺序）

如果你刚加入项目，建议按以下顺序阅读契约文件，每一步都建立在前一步的基础上：

1. **第一周（建立全局心智）**：
   - `docs/ARCHITECTURE.md` —— 看 0.2 现状
   - `docs/working/_archive_0.2_migration/0.2_rust_plugin_solution.md §1-3` —— 看 0.2 要做什么、为什么（已归档）
   - `docs/working/0.2插件体系核心决策.md` —— 看 9+1 条决策

2. **第二周（理解接口）**：
   - `kernel/crates/core/src/lib.rs` + `traits.rs` + `types.rs` —— Rust 契约全貌
   - `plugins/shared/pipeline/_base/plugin.py` + SDK `pipeline_types.py` —— Python 侧契约

3. **第三周（动手写第一个插件）**：
   - `docs/guides/`（总览 + sidecar 分篇）—— 照示例走一遍
   - `docs/plugin-protocol.md` §8 —— echo_tool 从零走查
   - `config/templates/plugin_scaffold/*.py` —— 复制模板开始写

4. **第四周（理解 MCP 协议）**：
   - `docs/working/_archive_0.2_migration/0.2_rust_plugin_solution.md §3.1` —— 为什么选 MCP
   - `docs/plugin-protocol.md` —— 统一协议与加载链路

5. **里程碑门控**：
   - `docs/0.2_rust_plugin_checkpoints.md CP-02` —— 理解"协议冻结"为什么是极高优先级

---

## 五、结语

读完这份教程，你应该理解了：

- ✅ 灵汐 0.2 项目里有哪些契约文件、各自在哪
- ✅ 每份契约文件定义了哪些东西、关键部分代表什么意思
- ✅ 这些契约在项目里起什么作用、在真实开发场景里怎么用
- ✅ 契约文件之间的依赖关系，以及如何按顺序学习

**最重要的一点**：**契约 = 宪法**。一旦在 CP-02 协议冻结里程碑敲定，所有后续开发必须严格遵守。任何想"灵活变通"的代码都会在静态检查或运行时校验时被拦住。这正是 0.2 全面插件化能落地的基石。

> **最后提醒**：本教程中标记为"⏳ task_02 待产出"的契约文件（`.project/manifest_v2_schema.json`、`.project/mcp_extension_protocol.md`），始终未作为独立文件产出——manifest 真值源是 `kernel/crates/core/src/traits.rs::PluginManifest`（配 `docs/plugin-protocol.md` 说明），MCP 协议以官方 MCP 标准为准。教程中相关章节按决策文档反向推导，仅供理解。

---

**文档元信息**

- 教程产出：`docs/guides/contract_files_tutorial.md`
- 任务来源：`docs/tasks/task_02_contract_definition.md`
- 调研日期：2026-07-13
- 涵盖契约文件数：16 份（14 份已存在 + 2 份待产出）
