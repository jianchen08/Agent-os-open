# 插件协议开发者文档（Plugin Protocol）

> 面向**想给灵汐 AgentOS 0.2 开发一个新插件**的开发者。读完本文，你应能在 1 小时内发布第一个可被内核加载的插件。
>
> 本文是 0.2 架构（Rust 内核 + Python sidecar + YAML 配置）的统一插件协议说明。历史方案记录见 [working/_archive_0.2_migration/0.2_rust_plugin_solution.md](../working/_archive_0.2_migration/0.2_rust_plugin_solution.md)，整体架构见 [ARCHITECTURE.md](../ARCHITECTURE.md)，分篇上手教程见 [开发指南索引](README.md)。

---

## 目录

- [1. 总览](#1-总览)
- [2. plugin.json manifest schema](#2-pluginjson-manifest-schema)
- [3. 插件类型分类](#3-插件类型分类)
- [4. host_type：sidecar vs in-process](#4-hosttypesidecar-vs-in-process)
- [5. 双插件根约定](#5-双插件根约定)
- [6. config_files：配置显式映射注入](#6-config_files配置显式映射注入)
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
  → 按需 load(id): 首次调用某工具时启动 sidecar 进程（python server.py）
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
| `entry` | string | ✅* | 启动命令。Python 插件一般是 `python server.py`。`composite` 类型可空。 |
| `language` | string | ✅ | 实现语言，如 `python` / `rust`。 |
| `capabilities` | object | ✅ | 能力声明，见 [§2.2](#22-capabilities-子字段)。 |
| `requires_services` | array | — | 插件间耦合唯一轴：声明需要的能力角色（`ns` / `ns.method`），见 [§2.3](#23-requires_services插件间依赖)。 |
| `permissions` | object | — | 权限声明（文件系统 / 网络 / 环境变量 / 系统调用）。默认全空。 |
| `error_policy` | enum | — | 已收敛为唯一值 `retry` 并整体移除（ADR 2026-08-18），不要再声明。见 [§2.4](#24-error_policy已收敛不再声明)。 |
| `priority` | int | — | 优先级，数字越小越靠前。默认 `100`。管道插件执行顺序按此排序。 |
| `pipeline_role` | enum | —* | 仅 `plugin_type=pipeline` 时必填：`input` / `core` / `output`。 |
| `description` | string | — | 一句话描述（展示用）。 |
| `requires_content` | int | — | 内容懒加载声明：需要预加载的最近消息条数。见 [§2.5](#25-requirescontent)。 |
| `config_files` | array | — | 配置文件显式映射（`{id, path, label}` 三要素）。见 [§6](#6-config_files配置显式映射注入)。 |
| `ui_schema` | object | — | 前端 UI schema 声明（P0-3）。见 [§7](#7-uischema前端-schema-驱动p0-3)。 |
| `mcp` | object | — | MCP 传输配置（`transport` / `endpoint` / `idle_timeout_secs` / `protocol_version` / `request_timeout_secs`）；接入**外部第三方 MCP 服务**也经它声明，见 [§2.6](#26-接入外部-mcp-服务)。 |
| `invoke_entry` | string | ✅** | 管道/系统等非 tool 插件的 MCP 入口方法名（如 `llm_core.execute`）；`plugin_type=pipeline` 必填，缺失启动期校验失败。tool 插件不用此字段。 |
| `http_endpoints` | array | — | HTTP 端点贡献：每项 `{route_id, method, path, auth, handler_capability, timeout_ms}`，path 必须落在 `/ext/{plugin_id}/**`，经 capability RPC 调插件 `http.handle`。 |
| `native` | object | —* | 原生插件产物（cdylib 路径与符号）；`host_type=in_process` 必填。见 [plugin-native-rust.md](plugin-native-rust.md)。 |
| `lifecycle` | object | — | 生命周期策略覆盖（如 `idle_timeout_secs`，交互类插件设 0 = 永不卸载）。缺省用内核默认。 |
| `host_group` | string | — | 合宿分组声明（`"light"` = 准入轻量合宿组，多插件共享宿主进程）；缺省 = 独占宿主。白名单制：声明即作者担保无阻塞调用/无 C 扩展/无重依赖。 |
| `granted_capabilities` | array | — | 反向 capability 调用白名单（如 `["config-reader"]`）；空 = 默认全授予（存量兼容），一旦非空即白名单制，越权单点拒绝。 |
| `contributes` | object | — | 前端贡献点声明（viewsContainers/widgets/menus/commands/settingsPanels 等），内核仅在 `/api/v1/schema` 透传，前端 ContributionRegistry 消费。 |
| `enabled` | bool | — | 启用开关（`false` = 已安装但不进注册表出口）；缺省由 `config/plugins/default_profile.yaml` 决定。 |
| `activation` | enum | — | 激活策略：`eager`（启动即 load）/ `lazy`（首次调用再 load，默认）/ `manual`（仅用户显式启动）。 |
| `persistent_fields` | array | — | 声明需持久化的 state 累计型标量键（如 `track.total_tokens`），引擎 merge 时投影落库。 |
| `export_fields` | array | — | state 出口白名单（支持 `前缀.*` 通配）；未声明且不在内核基线 = 不出口（默认拒绝）。 |
| `tool_prefix` | string | — | 工具名前缀，避免多插件工具名冲突时 McpBridge 拼错工具名。 |

> \* `entry` 对 `composite` 类型可空；`pipeline_role` 仅 pipeline 类型有意义；`native` 仅 `in_process` 必填。
> \*\* `invoke_entry` 对 `plugin_type=pipeline` 必填（启动期聚合校验）。

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
- `route_signals`：遗留声明位（`next_llm` / `next_tool` / `end` / `wait`），加载时仍会校验并注册进能力注册表，但**执行面零消费**——0.2 路由由管道 YAML 的 G10 DSL（`when`/`then`/`set`）驱动，见 [pipeline-configuration.md](pipeline-configuration.md)。新插件无需声明。
- `lifecycle_hooks` 取值（`LifecycleHook`）：`on_load` / `on_unload` / `on_pipeline_start` / `on_pipeline_end` / `on_error` / `domain_event`。
- 发流式事件的插件必须声明 `capabilities.streaming`（`{events, part_types, persist}`），未声明网关拒绝（fail-closed），见 [streaming-protocol.md](streaming-protocol.md)。

### 2.3 requires_services（插件间依赖）

```json
{
  "requires_services": ["human-interaction", "pipeline-executor", "event-bus"]
}
```

- 条目是**能力角色名**（`ns` 或 `ns.method`），注册表映射到提供该角色的插件，**不点名插件 id**；boot 期依赖闸校验，无人提供该角色则内核启动被拒（ADR 2026-08-18 插件依赖包）。
- 声明了依赖的插件握手后经 `CapabilityHandle` 反向调用提供方（详见 SDK `capability.py` 与 `plugins/shared/system/approval/` 实现）。
- 历史文档中的 `dependencies` / `capabilities_required` **不是 manifest 字段**（`deny_unknown_fields` 下声明即加载失败）：Python 依赖写在插件 `pyproject.toml`（uv venv 单轨，见 [plugin-sidecar-python.md](plugin-sidecar-python.md)），插件间耦合只走本字段。

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

复用**已存在的第三方 MCP 服务**（Playwright、smithery、MCP Registry 等）而不写 `server.py`：约定 `language: "external"` + `entry: "mcp:external"` + `host_type: "sidecar"`，用 `mcp` 声明连接——`transport: "streamable_http"` 用 `endpoint.url` / `headers` / `auth`（HTTP 直连，不 spawn）；`transport: "stdio"` 用 `endpoint.command` / `args` / `env`（spawn 第三方命令）。

- `auth.value` 与 `env` 值支持 `${VAR}` 占位（构造时解析，缺失早暴露）。
- `request_timeout_secs`：长等待业务（如等审批）必须显式声明，否则内核 MCP client 默认 300s 先行掐断。
- external MCP 工具**缺 `input_schema` 拒注册**（fail-closed）。

完整 manifest 示例与上手步骤见 [plugin-external-mcp.md](plugin-external-mcp.md)；真实插件在 `plugins/shared/tools/external_mcp/`。

---

## 3. 插件类型分类

`plugin_type`（`pipeline` / `tool` / `system` / `composite`）的职责、三角色分工与目录位置见 [plugin-development.md](plugin-development.md) §1。契约语义补充：插件间通过管道 `state` 通信、**不直接互调**；返回 `PluginResult`（`state_updates` Patch；`route_signal` 为遗留字段，执行链不消费——路由由管道 G10 DSL 驱动）；`priority` 决定同一阶段内的执行顺序。

---

## 4. host_type：sidecar vs in-process

所有插件类型双轨自选（ADR ⑧，不因插件类型受限）：`sidecar`（独立进程，MCP over stdio，默认——进程隔离，崩溃不影响内核）/ `in_process`（Rust cdylib 进程内零 IPC——高频热路径晋升轨）。选型依据与晋升路径见 [plugin-development.md](plugin-development.md) §2 与 [plugin-native-rust.md](plugin-native-rust.md)。

---

## 5. 双插件根约定

内置根 `plugins/shared/`（只读）+ 用户根（环境变量 `AGENTOS_USER_PLUGINS_DIR` 或 OS 标准目录，可写）；**同 ID 时用户根覆盖内置根**（不修改仓库即可替换/魔改内置插件）。发现算法与"哪些目录不需要 manifest"见 [plugin-development.md](plugin-development.md) §3 与[附录 B](#附录-b哪些目录不需要-manifest)。

---

## 6. config_files：配置显式映射注入

> 对应 ROADMAP P0-2：配置按需注入（早期设计名 `config_refs`，已被本字段取代并从契约删除——残留 `config_refs` 会被 `deny_unknown_fields` 拒载）。

**问题**：插件需要读 `config/` 下的运行配置。内核不做全量投递——未声明 `config_files` 的插件收空配置。

**解法**：manifest 用 `config_files` 逐项声明"我要哪个配置文件"。每项三要素：

- `id`：配置子项标识（插件内唯一，作为注入命名空间 key 与 API file_id）；
- `path`：相对 `config/` 根的文件路径（如 `config/models/llm.yaml`）；
- `label`：前端展示用的名称。

```json
{
  "config_files": [
    { "id": "llm", "path": "config/models/llm.yaml", "label": "LLM 模型配置" }
  ]
}
```

**规则**：

- 未声明（或空数组）→ 插件收空配置：需要哪个文件就显式映射哪条（fail-closed）。
- `path` 经内核安全校验：归一化后必须落在 `config/` 子树内。
- 声明的文件参与 sidecar 配置注入，并经 `/api/v1/plugins/{id}/config/{file_id}` 读写，配置热更新走 mtime 缓存。
- 追加 `"settings": false` = 注入专用：不出口到 `/api/v1/schema` 的 plugin_configs（该文件的 UI 由插件自声明的 settings 页/widget 承载，避免双入口）。

**现状参考**：`llm_service`（`config/models/llm.yaml` + `config/models/embedding.yaml` 两条映射）。

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
| `widgets[].type` | widget 类型（对应前端已注册的 14+ Widget，如 `review_document` / `decision` / `task_card` 等） |
| `widgets[].space` | 渲染空间：`chat` / `workspace` / `floating` / `dock` / `fullscreen` / `scene`（共 6 个；`scene` 已标废弃，新插件勿用） |
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
  "config_files": [
    { "id": "echo", "path": "config/tools/echo.yaml", "label": "Echo 配置" }
  ]
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

排障对照表见 [troubleshooting.md](troubleshooting.md)。协议侧两条高频问题：

- **工具注册了但 LLM 不调用**：`capabilities.tools[].name` 与 server.py 注册名完全一致（含大小写）；`description` 写清用途（LLM 据此选择）；`input_schema` 越准成功率越高。另见三层可见性过滤（troubleshooting 首条）。
- **`config_files` 配置注入没生效**：`path` 须相对 `config/` 根且落在 `config/` 子树内；未声明（或空）= 收空配置，需要哪个文件就显式映射哪条。

---

## 附录 A：manifest 覆盖现状统计

> 数据基于 `plugins/shared/` 全量扫描（97 个 `plugin.json`），核对日期 2026-09。
> 数量随插件增删会漂移，以本表口径（git 跟踪 manifest 全量解析）自行复测为准。

**按目录类别：**

| 类别 | 目录位置 | 数量 |
|------|----------|------|
| pipeline / input | `plugins/shared/pipeline/input/` | 22 |
| pipeline / core | `plugins/shared/pipeline/core/` | 2 |
| pipeline / output | `plugins/shared/pipeline/output/` | 14 |
| pipeline（根下直挂） | `plugins/shared/pipeline/<name>/` | 4 |
| system（含连接器/通道/系统服务） | `plugins/shared/system/` | 29 |
| tools | `plugins/shared/tools/`（18 个顶层插件 + `external_mcp/` 下 8 个预置接入清单） | 26 |
| **合计** | | **97** |

**按 `plugin_type`：**

| plugin_type | 数量 |
|-------------|------|
| `pipeline` | 39 |
| `system` | 28 |
| `tool` | 30 |

**按 `host_type`：**

| host_type | 数量 |
|-----------|------|
| `sidecar` | 93 |
| `in_process`（Rust cdylib） | 4（`pipeline_tool_core` / `pipeline_sensitive_checker` / `pipeline_spill_guard` / `native_test`） |

**关键内部模块覆盖确认：**

| 模块类别 | 期望 | 已有 manifest | 状态 |
|----------|------|--------------|------|
| 连接器（connectors） | `connectors_service`（聚合） | `plugins/shared/system/connectors/plugin.json` | ✅ |
| 通道（channel_*） | 5 个（cli/dingtalk/feishu/qq/wecom） | 5 个 | ✅ 全覆盖（`channel_api` 已整体退役，ADR 2026-08-21 插件自持 http_endpoints；`channel_gateway` 不再是独立插件） |
| Agent（scene） | `scene_service` | `plugins/shared/system/scene/plugin.json` | ✅ |
| 工具（tools） | 18 个顶层 + 8 个 external_mcp 预置接入 | 26 个 | ✅ 全覆盖（`external_mcp/` 本身是聚合目录，manifest 在其 8 个子目录里） |
| 系统服务 | memory/llm/approval/evaluation/... | 29 个 | ✅ |
| 内置工具聚合 sidecar（builtin_tools） | 1 个（8 个工具的 MCP 聚合） | 1 | ✅ |

**结论**：全部应独立加载的内部模块均已收敛到 `plugin.json` 协议（含原 `builtin_tools`、`artifacts` 两处历史缺口已补齐 manifest）。

---

## 附录 B：哪些目录不需要 manifest

发现算法只把"直接含 `plugin.json` 的目录"当插件。下列目录**故意没有** manifest，它们是父插件的**子模块**（被父 `server.py` import），不独立加载：

| 目录 | 父插件 | 说明 |
|------|--------|------|
| `plugins/shared/system/connectors/creative/` | `connectors_service` | 创意类连接器实现（comfyui / game_engine / generic） |
| `plugins/shared/system/connectors/vscode/` | `connectors_service` | VS Code 连接器适配器 |
| `plugins/shared/pipeline/_base/` | — | 管道插件公共基类 |
| `plugins/shared/tools/external_mcp/`（目录本身） | — | 预置外部 MCP 接入的聚合目录：manifest 在其 8 个子目录里，目录自身不需要 |
| 各插件目录下的 `__pycache__/` | — | Python 缓存，非代码 |

**判断原则**：一个目录是否需要 manifest，取决于它是否要**被内核作为独立插件加载**。如果只是被某个 `server.py` 通过 `import` 引用的实现细节，就不需要——加了反而会被错误地当成新插件发现。

> 子模块约定也适用于你自己的插件：把大逻辑拆成多个 `.py` 文件放在插件目录内即可，无需为每个文件建子目录或 manifest。

---

*分篇上手教程见 [开发指南索引](README.md)（sidecar / native / 外部 MCP / 主题 / Agent 配置 / 管道配置 / 排障）——0.2 统一以本文 `plugin.json` 协议为准。*
