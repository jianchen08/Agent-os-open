# 插件协议开发者文档（Plugin Protocol）

> 面向**想给灵汐 AgentOS 0.2 开发一个新插件**的开发者。读完本文，你应能在 1 小时内发布第一个可被内核加载的插件。
>
> 本文是 0.2 架构（Rust 内核 + Python sidecar + YAML 配置）的统一插件协议说明。历史方案记录见 [working/_archive_0.2_migration/0.2_rust_plugin_solution.md](working/_archive_0.2_migration/0.2_rust_plugin_solution.md)，整体架构见 [ARCHITECTURE.md](ARCHITECTURE.md)，分篇上手教程见 [开发指南索引](guides/README.md)。

---

## 目录

- [1. 总览](#1-总览)
- [2. plugin.json manifest schema](#2-pluginjson-manifest-schema)
- [3. 插件类型分类](#3-插件类型分类)
- [4. host_type：sidecar vs in-process](#4-hosttypesidecar-vs-in-process)
- [5. 双插件根约定](#5-双插件根约定)
- [6. config_refs：配置按需注入（P0-2）](#6-configrefs配置按需注入p0-2)
- [7. ui_schema：前端 schema 驱动（P0-3）](#7-uischema前端-schema-驱动p0-3)
- [8. 完整示例：从零开发一个新插件](#8-完整示例从零开发一个新插件)
- [9. SDK 用法速查](#9-sdk-用法速查)
- [10. 调试与常见问题](#10-调试与常见问题)
- [附录 A：manifest 覆盖现状统计](#附录-amanifest-覆盖现状统计)
- [附录 B：哪些目录不需要 manifest](#附录-b哪些目录不需要-manifest)

---

## 1. 总览

灵汐 0.2 的所有可扩展模块——管道插件、工具、系统服务、连接器、Agent、通道——都收敛到**同一个插件协议**之下。一个插件 = 一个目录 + 一个 `plugin.json` manifest（+ 实现代码）。内核（Rust）通过统一的发现、校验、加载流程处理它们，不区分内部还是第三方。

核心约定：

| 关注点 | 约定 |
|--------|------|
| 描述文件 | 每个插件目录下一个 `plugin.json`（也支持 `plugin.yaml`） |
| 唯一标识 | `id` 全局唯一；同 ID 时用户根覆盖内置根 |
| 能力声明 | `capabilities` 声明插件对外暴露的工具 / 服务 / 生命周期钩子 |
| 加载策略 | 按需加载——首次被调用时才启动 sidecar 进程；空闲超时自动卸载 |
| 进程模型 | 默认 `sidecar`（独立进程，MCP over stdio 通信）；高频热路径可选 `in-process`（Rust 原生） |
| 无状态 | 插件不持久化、不直接写存储；返回 Patch / 结果由引擎决定是否应用 |

加载链路：

```
内核启动
  → discover(): 扫描双根，递归找所有 plugin.json，校验 manifest schema
  → 注册 capabilities 到 CapabilityRegistry
  → 按需 load(id): 首次调用某工具时启动 sidecar 进程（python3 server.py）
  → MCP 握手（initialize）+ 配置注入
  → tools/call 调用 → 返回结果
  → 空闲超时 unload(id)
```

---

## 2. plugin.json manifest schema

manifest 字段对应内核 `PluginManifest`（见 `kernel/crates/core/src/traits.rs`）。**必填字段**缺一不可，否则加载校验失败。

### 2.1 字段总表

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | ✅ | 插件全局唯一标识，`snake_case`。用户根同 ID 会覆盖内置根。 |
| `name` | string | ✅ | 人类可读名称（展示用）。 |
| `version` | string | ✅ | 语义化版本号，如 `1.0.0`。 |
| `plugin_type` | enum | ✅ | `pipeline` / `tool` / `system` / `composite`。见 [§3](#3-插件类型分类)。 |
| `host_type` | enum | ✅ | `sidecar`（独立进程，默认）或 `in_process`（Rust 原生）。见 [§4](#4-hosttypesidecar-vs-in-process)。 |
| `entry` | string | ✅* | 启动命令。Python 插件一般是 `python3 server.py`。`composite` 类型可空。 |
| `language` | string | ✅ | 实现语言，如 `python` / `rust`。 |
| `capabilities` | object | ✅ | 能力声明，见 [§2.2](#22-capabilities-子字段)。 |
| `requires_services` | array | — | 插件间耦合唯一轴：声明需要的能力角色（`ns` / `ns.method`），见 [§2.3](#23-requires_services插件间依赖)。 |
| `permissions` | object | — | 权限声明（文件系统 / 网络 / 环境变量 / 系统调用）。默认全空。 |
| `error_policy` | enum | — | 已收敛为唯一值 `retry` 并整体移除（ADR 2026-08-18），不要再声明。见 [§2.4](#24-error_policy已收敛不再声明)。 |
| `priority` | int | — | 优先级，数字越小越靠前。默认 `100`。管道插件执行顺序按此排序。 |
| `pipeline_role` | enum | —* | 仅 `plugin_type=pipeline` 时必填：`input` / `core` / `output`。 |
| `description` | string | — | 一句话描述（展示用）。 |
| `requires_content` | int | — | 内容懒加载声明：需要预加载的最近消息条数。见 [§2.5](#25-requirescontent)。 |
| `config_refs` | array | — | 配置按需注入声明（P0-2）。见 [§6](#6-configrefs配置按需注入p0-2)。 |
| `ui_schema` | object | — | 前端 UI schema 声明（P0-3）。见 [§7](#7-uischema前端-schema-驱动p0-3)。 |
| `mcp` | object | — | MCP 传输配置（`transport` / `endpoint` / `idle_timeout_secs` / `protocol_version` / `request_timeout_secs`）；接入**外部第三方 MCP 服务**也经它声明，见 [§2.6](#26-接入外部-mcp-服务)。 |

> \* `entry` 对 `composite` 类型可空；`pipeline_role` 仅 pipeline 类型有意义。

### 2.2 capabilities 子字段

```json
{
  "capabilities": {
    "tools": [
      {
        "name": "tool.dotted.name",
        "description": "工具描述（LLM 据此决定是否调用）",
        "input_schema": { "type": "object" },
        "output_schema": { "type": "object" },
        "category": "system",
        "render": { "card": "form", "title": "工具结果卡片标题" }
      }
    ],
    "services": [
      { "name": "ns.method", "description": "内部服务方法（不进 LLM 面）", "input_schema": { "type": "object" } }
    ],
    "route_signals": ["next_llm"],
    "lifecycle_hooks": ["on_load", "on_unload"]
  }
}
```

- `tools[]`：必填 `name`；建议带全 `input_schema` + `output_schema` + `render`（工具契约 fail-closed：tool_core 执行后按 `output_schema` 校验结果，前端按 `render` 意图路由渲染）。
- `tools[].category` 取值见 `ToolCategory`（`file` / `file_system` / `search` / `web` / `memory` / `task` / `system` / `execution` / `analysis` / `evaluation` / `agent` / `monitoring`），省略时默认 `system`。
- `services[]`：内部服务方法元数据，经 capability 调用（调用方声明 `requires_services`），**不进 LLM 面**。
- `route_signals`：历史声明位（`next_llm` / `next_tool` / `end` / `wait`），加载时仍会校验并注册进能力注册表，但**执行面零消费**——0.2 路由由管道 YAML 的 G10 DSL（`when`/`then`/`set`）驱动，见 [guides/pipeline-configuration.md](guides/pipeline-configuration.md)。新插件无需声明。
- `lifecycle_hooks` 取值（`LifecycleHook`）：`on_load` / `on_unload` / `on_pipeline_start` / `on_pipeline_end` / `on_error` / `domain_event`。
- 发流式事件的插件必须声明 `capabilities.streaming`（`{events, part_types, persist}`），未声明网关拒绝（fail-closed），见 [streaming-protocol.md](guides/streaming-protocol.md)。

### 2.3 requires_services（插件间依赖）

```json
{
  "requires_services": ["human-interaction", "pipeline-executor", "event-bus"]
}
```

- 条目是**能力角色名**（`ns` 或 `ns.method`），注册表映射到提供该角色的插件，**不点名插件 id**；boot 期依赖闸校验，无人提供该角色则内核启动被拒（ADR 2026-08-18 插件依赖包）。
- 声明了依赖的插件握手后经 `CapabilityHandle` 反向调用提供方（详见 SDK `capability.py` 与 `plugins/shared/system/approval/` 实现）。
- 历史文档中的 `dependencies` / `capabilities_required` **不是 manifest 字段**（`deny_unknown_fields` 下声明即加载失败）：Python 依赖写在插件 `pyproject.toml`（uv venv 单轨，见 [guides/plugin-sidecar-python.md](guides/plugin-sidecar-python.md)），插件间耦合只走本字段。

### 2.4 error_policy（已收敛，不再声明）

> **ADR 2026-08-18**：0.2 引擎**不再按 `error_policy` 分发行为**，且枚举已收敛为
> 唯一值 `retry`（`abort` / `skip` / `fallback` 已删除）。运行时错误处理由
> 引擎/编排层按错误类型自动决定：
> - 瞬态错误（sidecar 进程崩溃 `PLUGIN_CRASHED`）→ `invoker.with_transparent_recovery`
>   force_unload + respawn + **重试一次**；
> - 工具失败结果 → tool_core 回喂 LLM 自我修正；
> - 非瞬态错误 → 引擎 warn + 继续，跳过/终止决策上抛编排层。
>
> 插件**不应再声明该字段**：manifest 的 `error_policy` 字段为可选（serde default），
> 已从全部冻结 manifest 移除，缺省即 `retry`。声明任何非 `retry` 值将因枚举收敛
> 在加载期校验失败（fail-closed）。

### 2.5 requires_content

声明插件需要多少条**最近消息的完整内容**。引擎据此从 `blobs` 表按需预加载（懒加载），避免每次全量拉取。例如 `"requires_content": 3` 表示插件最多回看最近 3 条消息。省略表示不需要消息内容。

### 2.6 接入外部 MCP 服务

当你想直接复用一个**已存在的第三方 MCP 服务**（如 Playwright、smithery、MCP Registry），而不是自己写 `server.py`，约定 `language: "external"` + `entry: "mcp:external"` + `host_type: "sidecar"`，用 `mcp` 描述如何连接它，并把它的工具纳入能力注册：

```jsonc
// HTTP 远程（不 spawn 任何进程，走 HTTP 客户端直连）
{
  "mcp": {
    "transport": "streamable_http",
    "endpoint": {
      "url": "https://registry.modelcontextprotocol.io",
      "headers": { "Accept": "application/json" },
      "auth": { "type": "api_key", "header_name": "Authorization", "value": "${MCP_REGISTRY_API_KEY}" }
    }
  }
}

// 本地第三方命令（spawn 该命令，不经插件 venv）
{
  "mcp": {
    "transport": "stdio",
    "endpoint": {
      "command": "npx",
      "args": ["@anthropic-ai/mcp-playwright", "--headless"],
      "env": {}
    }
  }
}
```

- `transport: "streamable_http"` 用 `endpoint.url` / `headers` / `auth`；`transport: "stdio"` 用 `endpoint.command` / `args` / `env`。
- `auth.value` 与 `env` 值支持 `${VAR}` 占位（构造时从环境变量替换，缺失早暴露）。
- `request_timeout_secs`：长等待业务（如等用户审批）必须显式声明，否则内核 MCP client 默认 300s 先行掐断。
- external MCP 工具**缺 `input_schema` 拒注册**（fail-closed）。

完整示例见 `plugins/shared/tools/external_mcp/`（mcp_registry = HTTP 远程，omnisearch = 本地命令）；上手见 [guides/plugin-external-mcp.md](guides/plugin-external-mcp.md)。

---

## 3. 插件类型分类

`plugin_type` 决定插件在系统中的角色：

| 类型 | 职责 | 典型位置 |
|------|------|----------|
| `pipeline` | 管道处理单元。配合 `pipeline_role` 指定阶段：**input**（预处理：校验/注入/权限）、**core**（核心逻辑：LLM 调用/工具执行）、**output**（后处理：格式化/出口裁决/统计） | `plugins/shared/pipeline/{input,core,output}/` |
| `tool` | 提供 MCP 工具给 LLM 调用 | `plugins/shared/tools/` |
| `system` | 内核级服务（记忆/审批/评估/连接器/通道等） | `plugins/shared/system/` |
| `composite` | 组合插件：由 YAML 配置编排步骤，引擎解释执行（`entry` 可空） | — |

### 各类型职责详解

**pipeline（管道插件）** —— AI Agent 处理流程的可插拔处理单元：

```
输入路由 → Input 插件链 → Core 插件 → Output 插件链 → 输出路由仲裁
```

插件间通过管道 `state` 字典通信，**不直接互调**。返回 `PluginResult`（`state_updates` Patch；`route_signal` 为遗留字段，执行链不消费——路由由管道 G10 DSL 驱动）。优先级 `priority` 决定同一阶段内的执行顺序。

**tool（工具插件）** —— 暴露一组 MCP 工具，LLM 在生成 tool_call 时按 `capabilities.tools[].name` + `description` 选择调用。

**system（系统插件）** —— 内核级横向服务，如 `memory_service` / `approval_service` / `connectors_service` / `tasks_service`。它们通常既提供工具，也监听生命周期事件（审批经 human-interaction 工具调用阻塞等待用户响应）。

**composite（组合插件）** —— 把多个已存在的步骤用 YAML 编排成一个新插件，无需写代码。适合"把 A 工具 + B 校验 + C 格式化"打包成原子能力。

---

## 4. host_type：sidecar vs in-process

**ADR ⑧**：所有插件类型都支持两种执行路径，开发者按性能需求自选，**不因插件类型被限制**。

| host_type | 模型 | 适用场景 |
|-----------|------|----------|
| `sidecar` | 独立进程，通过 MCP 协议（JSON-RPC over stdio）与内核通信 | 默认。低频插件、第三方插件、Python 实现。进程隔离，崩溃不影响内核。 |
| `in_process` | Rust 原生进程内调用，零 IPC 开销 | 高频热路径、性能敏感。需要用 Rust 实现并编译进内核。 |

> 内置插件以 `sidecar` 为主；`in_process`（Rust cdylib）用于从边车轨基准晋升的高频管道步骤（如 `pipeline_tool_core` / `pipeline_sensitive_checker` / `pipeline_spill_guard`）。晋升路径与开发方式见 [guides/plugin-native-rust.md](guides/plugin-native-rust.md)。

---

## 5. 双插件根约定

内核扫描**两个根目录**发现插件：

| 根 | 路径 | 性质 |
|----|------|------|
| **内置根**（只读） | 仓库内 `plugins/shared/` | 随发行版分发，只读。结构是二级嵌套：`tools/simple/plugin.json`。 |
| **用户根**（可写） | 环境变量 `AGENTOS_USER_PLUGINS_DIR`，或 OS 标准目录（如 `%APPDATA%/agentos/plugins`、`~/.local/share/agentos/plugins`） | 第三方 / 自研插件落地处，可写。 |

**同 ID 覆盖语义**：用户根与内置根出现同 `id` 时，**用户根优先**（覆盖内置）。这让你能不修改仓库就替换/魔改内置插件。

**发现算法**（见 `kernel/crates/api/src/bin/agentos-kernel.rs::discover_plugin_roots` + `plugin-loader/src/loader.rs::scan_root`）：

1. 递归遍历根目录，收集所有直接包含 `plugin.json`（或 `plugin.yaml`）的目录；
2. 取这些目录的**父目录**作为扫描根（因为 `scan_root` 只看一级子目录）；
3. 解析每个 manifest、校验 schema、注册能力；
4. 同 ID 去重：用户根覆盖内置根。

> 子目录（不含 manifest）不会被当作独立插件——它们只是父插件 import 的普通 Python 模块。详见[附录 B](#附录-b哪些目录不需要-manifest)。

---

## 6. config_refs：配置按需注入（P0-2）

> 对应 ROADMAP P0-2：配置按需注入。

**问题**：内核 `load_config` 会扫描 `config/` 下所有 YAML 合并为一个大 JSON。早期会把**全量配置**塞给每个插件——既浪费，也泄露无关配置。

**解法**：manifest 用 `config_refs` 声明"我只关心哪些配置节"。内核据此过滤，只投递声明的节。

```json
{
  "config_refs": ["models"]
}
```

效果：握手时 `initialize` params 里的 `config` 只含 `models` 节，而非全量。

**规则**：

- `config_refs` 省略或为空 → 注入全量配置（向后兼容，老插件无需改动）。
- 数组元素是 `config/` 下的 YAML 文件名（不含扩展名），如 `models` / `memory_storage` / `channels`。
- 配置内容仍可在运行时通过 `on_config_change` 钩子接收热更新。

**现状参考**：`llm_service`（`config_refs: ["models"]`）、`memory_service`、`tasks_service` 已采用。

---

## 7. ui_schema：前端 schema 驱动（P0-3）

> 对应 ROADMAP P0-3 / 前端 Schema 驱动。

**目标**：新增插件时前端自动长出对应界面，无需手写前端代码。manifest 里声明"我要呈现什么"，内核 schema 端点把 `ui_schema` 暴露给前端，前端引擎（`SchemaParser` + `RenderingEngine`）据此渲染。

**结构**：

```json
{
  "ui_schema": {
    "widgets": [
      {
        "id": "approval_panel",
        "type": "review_document",
        "space": "workspace",
        "trigger": "on_event:approval_requested",
        "props": {
          "diff_view": true,
          "annotation": true
        }
      }
    ]
  }
}
```

字段含义：

| 字段 | 含义 |
|------|------|
| `widgets[].id` | 前端 widget 实例标识 |
| `widgets[].type` | widget 类型（对应前端已注册的 14+ Widget，如 `review_document` / `choice` / `task_card` 等） |
| `widgets[].space` | 渲染空间：`chat` / `workspace` / `floating` / `dock` / `scene`（共 5 个） |
| `widgets[].trigger` | 触发时机，如 `on_event:<event>`（事件到达时） |
| `widgets[].props` | 传给 widget 的配置项（如是否启用 diff 视图、批注） |

**现状参考**：`approval_service` 已用 `ui_schema` 声明审阅面板，实现"审批请求到达 → 自动打开 Workspace 审阅 Tab"。

---

## 8. 完整示例：从零开发一个新插件

目标：开发一个 `echo_tool` 工具插件，接收文本返回原样。覆盖从 manifest 到注册到验证的全流程。预计 **30 分钟**完成。

### 第 1 步：建目录（落到用户根）

```bash
# 选用户根（示例用本地路径；生产用 $AGENTOS_USER_PLUGINS_DIR）
mkdir -p ~/.local/share/agentos/plugins/echo_tool
cd ~/.local/share/agentos/plugins/echo_tool
```

> 想随仓库分发？放到 `plugins/shared/tools/echo_tool/`（内置根，只读）。

### 第 2 步：写 plugin.json

```json
{
    "id": "echo_tool",
    "name": "Echo Tool",
    "description": "回显输入文本（开发示例）",
    "version": "0.1.0",
    "plugin_type": "tool",
    "language": "python",
    "host_type": "sidecar",
    "entry": "python server.py",
    "capabilities": {
        "tools": [
            {
                "name": "echo",
                "description": "原样返回输入的文本",
                "input_schema": { "type": "object", "properties": { "text": { "type": "string" } }, "required": ["text"] },
                "output_schema": { "type": "object", "required": ["echo"], "properties": { "echo": { "type": "string" }, "length": { "type": "integer" } } },
                "render": { "card": "form", "title": "回显" }
            }
        ],
        "route_signals": [],
        "lifecycle_hooks": ["on_load", "on_unload"]
    },
    "permissions": {},
    "priority": 50
}
```

要点：

- `id` 全局唯一，`snake_case`。
- `plugin_type: "tool"` + `host_type: "sidecar"` + `entry: "python server.py"` 是工具插件最常见组合。
- `capabilities.tools[].name` = `echo`，内核据此注册工具，LLM 会看到这个工具（还需加进目标 Agent 的 `tool_ids` 白名单）。
- 工具带全 `input_schema` + `output_schema` + `render`——工具契约 fail-closed。
- 示例不含 `error_policy`（已废弃，勿再声明）与 `dependencies`（不是 manifest 字段——Python 依赖走 `pyproject.toml`，见第 4 步）。

### 第 3 步：写 server.py

```python
#!/usr/bin/env python3
"""echo_tool MCP 服务端示例。"""

from agentos_plugin_sdk import AgentOSPlugin

ECHO_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "要回显的文本"}
    },
    "required": ["text"],
}


async def echo(text: str) -> dict:
    """原样返回输入文本。"""
    return {"echo": text, "length": len(text)}


def create_plugin() -> AgentOSPlugin:
    plugin = AgentOSPlugin("echo_tool")
    plugin.register_tool("echo", ECHO_SCHEMA, echo, "原样返回输入的文本")
    return plugin


if __name__ == "__main__":
    create_plugin().run()
```

要点：

- `create_plugin().run()` 启动 MCP JSON-RPC 服务端，阻塞读 stdin、写 stdout。
- `register_tool(name, input_schema, handler, description)` 注册工具；handler 可 sync 或 async。
- 工具 `name` 必须与 `plugin.json` 的 `capabilities.tools[].name` 一致。

### 第 4 步：本地验证（不依赖内核）

内核对裸 `python` 启动命令强制使用**插件目录内**的 venv（uv 单轨，缺 `pyproject.toml` / `.venv` 启动即报错）。在插件目录建 `pyproject.toml` 并同步 venv：

```toml
[project]
name = "agentos-plugin-echo-tool"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["agentos-plugin-sdk>=0.2.0"]

[tool.uv.sources]
agentos-plugin-sdk = { path = "<仓库>/plugins/sdk", editable = true }
```

```bash
uv sync --project <插件目录>
```

直接跑进程，手动发一个 initialize + tools/call（JSON-RPC over stdio）：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"echo","arguments":{"text":"hi"}}}' | .venv/bin/python server.py
```

（Windows 用 `.venv/Scripts/python.exe`。）

预期看到 `id=2` 的响应里 `content[0].text` 含 `{"echo": "hi", "length": 2}`。

### 第 5 步：让内核发现它

把插件目录放到用户根（或内置根），重启内核。日志应出现：

```
Manifest validated: id=echo_tool type=Tool host=Sidecar path=.../echo_tool/plugin.json
Discovered N plugin manifests
```

之后该工具自动出现在能力注册表，LLM 可在 tool_call 中调用 `echo`。

### 第 6 步（可选）：加配置注入与前端 schema

需要读配置：

```json
{
  "config_refs": ["my_echo_config"]
}
```

需要前端界面（如展示一个面板）：

```json
{
  "ui_schema": {
    "widgets": [
      {
        "id": "echo_panel",
        "type": "task_card",
        "space": "chat",
        "trigger": "on_event:echo_done"
      }
    ]
  }
}
```

### 各类型插件的最小骨架

- **工具插件**：见上文 `echo_tool`。
- **系统插件**：`plugin_type: "system"`，其余同上；通常还声明 `requires_services`（如 `["event-bus"]`）以拿到内核反向调用句柄。参考 `plugins/shared/system/approval/`。
- **管道插件**：`plugin_type: "pipeline"` + `pipeline_role: "input"|"core"|"output"`，并设置合理的 `priority`。参考 `plugins/shared/pipeline/input/prompt_build/`。
- **接入外部 MCP**：不写 `server.py`，约定 `entry: "mcp:external"` + `mcp` 传输配置（见 [§2.6](#26-接入外部-mcp-服务)）。参考 `plugins/shared/tools/external_mcp/browser_test/`。

---

## 9. SDK 用法速查

SDK 源码：`plugins/sdk/src/agentos_plugin_sdk/`。核心 API：

```python
from agentos_plugin_sdk import (
    AgentOSPlugin,   # 插件基类
    tool,            # @tool 装饰器（模块级声明）
    collect_tools,   # 收集模块级 @tool
    McpServer,       # MCP 服务端（一般不直接用，AgentOSPlugin.run() 内部用）
    STANDARD_CAPABILITIES,  # 标准能力枚举
    CapabilityHandle,       # 内核反向调用句柄
)
```

### 注册工具（两种等价写法）

```python
# 写法 A：实例方法
plugin = AgentOSPlugin("my_plugin")

@plugin.tool(name="search", schema={...}, description="搜索")
async def search(query: str) -> dict:
    return {"results": [...]}

# 写法 B：模块级装饰器 + 自动收集
@tool(name="search", schema={...}, description="搜索")
async def search(query: str) -> dict:
    return {"results": [...]}

plugin = AgentOSPlugin("my_plugin")
for _name, tdef in collect_tools(__main__).items():  # 扫描模块级 @tool
    plugin.register_tool(tdef.name, tdef.schema, tdef.handler, tdef.description)
```

### 生命周期钩子

SDK 为常用钩子提供了专用**装饰器**（`on_load` / `on_unload` / `on_config_change`）；其它钩子用 `on_lifecycle(event, handler)` 普通方法注册：

```python
@plugin.on_load
async def on_load(params: dict) -> None:
    # 初始化资源（握手时收到注入的 capabilities/config）
    ...

@plugin.on_unload
async def on_unload() -> None:
    # 释放资源
    ...

@plugin.on_config_change
async def on_config_change(config: dict) -> None:
    # 响应配置热更新
    ...

# 其它钩子（on_pipeline_start / on_pipeline_end / on_error）：
plugin.on_lifecycle("on_pipeline_start", handler_fn)
```

钩子名对应 manifest 的 `capabilities.lifecycle_hooks`，且映射到 MCP notification（`notifications/on_load` 等）。注意 `on_lifecycle(event, handler)` 是**普通注册方法**（传函数），不是装饰器；上面三个专用装饰器才是 `@` 语法。

### 启动服务端

```python
if __name__ == "__main__":
    plugin.run()
```

`run()` 启动 `McpServer`，从 stdin 读 JSON-RPC、写 stdout，处理 `initialize` / `tools/list` / `tools/call` / `resources/read` / `notifications/*`。反向调用通道（`KernelChannel`）随服务端一起启动，共享 stdin 多路复用。

### 内核反向调用（requires_services 插件）

声明了 `requires_services`（如 `["pipeline-executor"]`）的插件，握手后能拿到 `CapabilityHandle`，向内核/提供方插件发起反向 RPC（如让管道恢复执行）。详见 SDK `capability.py` 与 `approval_service` 实现。

---

## 10. 调试与常见问题

### Q: 启动后日志没出现我的插件？

检查：

1. 目录下确实有 `plugin.json`（文件名拼写、扩展名）。
2. `id` / `name` / `version` / `plugin_type` / `host_type` / `entry` / `language` 都非空（必填校验）。
3. 目录在双根之一下（内置根 `plugins/shared/` 或 `AGENTOS_USER_PLUGINS_DIR`）。
4. manifest JSON 合法（`python -c "import json;json.load(open('plugin.json'))"`）。

### Q: sidecar 启动后立即崩溃？

- 确认 `entry` 命令在插件目录的 working dir 下能跑通（内核以插件目录为 CWD 启动 sidecar）。
- 确认 SDK 已安装（`pip install -e plugins/sdk`）。
- 手动 `cd <插件目录> && python3 server.py` 跑一下，看报错。
- 单个 sidecar 崩溃会被隔离，不影响内核和其他插件（ADR 崩溃隔离）。

### Q: 工具没被 LLM 调用？

- `capabilities.tools[].name` 与 server.py 注册的 name 完全一致（含大小写）。
- `description` 写清楚用途——LLM 据此判断是否调用。
- `input_schema` 越准，调用成功率越高。

### Q: 配置注入没生效？

- `config_refs` 里的节名要和 `config/` 下 YAML 文件名一致（不含扩展名）。
- 留空或省略会注入全量配置（向后兼容），先确认全量是否含你期望的节。

### Q: 用户根怎么覆盖内置插件？

在用户根建一个**同 `id`** 的插件目录。内核发现时用户根优先，内置的被覆盖。适合魔改内置插件而不动仓库。

---

## 附录 A：manifest 覆盖现状统计

> 数据基于 `plugins/shared/` 全量扫描（92 个 `plugin.json`），核对日期 2026-07。
>
> **历史快照说明**：此后形态已演进——`builtin_tools/` 已补齐 manifest；已出现 `in_process` native 插件（`pipeline_tool_core` / `pipeline_sensitive_checker` / `pipeline_spill_guard`），host_type 不再全为 sidecar。下表数量仅供量级参考。

**按目录类别：**

| 类别 | 目录位置 | 数量 |
|------|----------|------|
| pipeline / input | `plugins/shared/pipeline/input/` | 21 |
| pipeline / core | `plugins/shared/pipeline/core/` | 3 |
| pipeline / output | `plugins/shared/pipeline/output/` | 20 |
| system（含连接器/通道/Agent/系统服务） | `plugins/shared/system/` | 22 |
| tools | `plugins/shared/tools/`（含 `external_mcp/` 子目录 8 个） | 26 |
| **合计** | | **92** |

> 注：上表按**目录位置**计数。"按 plugin_type"统计中 `system=24`、`tool=24`，与按目录的 22/26 不一致——因为 `plugins/shared/tools/` 下有 2 个目录（`channel_ws/`、`triggers/`）声明为 `plugin_type: "system"`（属于通道/触发器侧的 system 服务，只是物理放在 tools/ 下）。

**按 `plugin_type`：**

| plugin_type | 数量 |
|-------------|------|
| `pipeline` | 44 |
| `system` | 24 |
| `tool` | 24 |

**按 `host_type`：**

| host_type | 数量 |
|-----------|------|
| `sidecar` | 92（全部） |

**关键内部模块覆盖确认：**

| 模块类别 | 期望 | 已有 manifest | 状态 |
|----------|------|--------------|------|
| 连接器（connectors） | `connectors_service`（聚合） | `plugins/shared/system/connectors/plugin.json` | ✅ |
| 通道（channel_*） | 7 个（api/cli/dingtalk/feishu/gateway/qq/wecom） | 7 个 | ✅ 全覆盖 |
| Agent（scene） | `scene_service` | `plugins/shared/system/scene/plugin.json` | ✅ |
| 工具（tools） | 18 个顶层 + 8 个 external_mcp | 26 个 | ✅ 全覆盖 |
| 系统服务 | memory/llm/approval/evaluation/... | 22 个 | ✅ |
| 内置工具聚合 sidecar（builtin_tools） | 1 个（10 个工具的 MCP 聚合） | 0 | ⚠️ **缺 manifest，见下** |

**结论**：快照期发现的唯一遗漏——`plugins/shared/tools/builtin_tools/`（有标准 `server.py` 注册 10 个工具但缺 `plugin.json`）——**已补齐 manifest**，全部应独立加载的内部模块均已收敛到 `plugin.json` 协议。

---

## 附录 B：哪些目录不需要 manifest

发现算法只把"直接含 `plugin.json` 的目录"当插件。下列目录**故意没有** manifest，它们是父插件的**子模块**（被父 `server.py` import），不独立加载：

| 目录 | 父插件 | 说明 |
|------|--------|------|
| `plugins/shared/system/connectors/creative/` | `connectors_service` | 创意类连接器实现（comfyui / game_engine / generic） |
| `plugins/shared/system/connectors/vscode/` | `connectors_service` | VS Code 连接器适配器 |
| `plugins/shared/system/artifacts/` | （由 gateway/api 通道 sidecar 加载） | 制品服务模块（annotation/artifact 服务） |
| `plugins/shared/pipeline/_base/` | — | 管道插件公共基类 |
| 各插件目录下的 `__pycache__/` | — | Python 缓存，非代码 |

**判断原则**：一个目录是否需要 manifest，取决于它是否要**被内核作为独立插件加载**。如果只是被某个 `server.py` 通过 `import` 引用的实现细节，就不需要——加了反而会被错误地当成新插件发现。

> 子模块约定也适用于你自己的插件：把大逻辑拆成多个 `.py` 文件放在插件目录内即可，无需为每个文件建子目录或 manifest。

---

*分篇上手教程见 [开发指南索引](guides/README.md)（sidecar / native / 外部 MCP / 主题 / Agent 配置 / 管道配置 / 排障）——0.2 统一以本文 `plugin.json` 协议为准。*
