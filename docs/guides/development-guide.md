# AgentOS 开发指南：插件开发 · 主题 · Agent 配置 · 管道配置

> 面向要在本仓库开发插件、配置 Agent 与管道的贡献者。按"要做什么"组织：
> 各类插件怎么写（sidecar / native / 外部 MCP / 主题）、Agent 与管道怎么配、以及排障。
>
> **字段级权威**：`plugin.json` 全字段规范见 [docs/plugin-protocol.md](../plugin-protocol.md)
> （本文只做核心速查）。本文所有示例均指向仓库内真实插件，可直接对照源码。
> `docs/guides/plugin_development_guide.md` 与 `plugin_development_standard.md` 是 0.1
> 历史文档，不要按它们开发。

---

## 1. 插件全景与选型

### 1.1 插件类型（plugin_type）与目录

| plugin_type | 职责 | 目录 | 数量级 |
|---|---|---|---|
| `tool` | 暴露 LLM 工具（`capabilities.tools` 进 LLM 面） | `plugins/shared/tools/` | 26 |
| `system` | 内核级服务：记忆/审批/评估/LLM/页面插件等，通常 tools + services 混合，可产路由信号 | `plugins/shared/system/` | 22 |
| `pipeline` | 管道步骤（必须声明 `invoke_entry`，可选 `pipeline_role: input/core/output`） | `plugins/shared/pipeline/{input,core,output}/` | 21/3/20 |
| `composite` | 组合插件（entry 可空，聚合子插件） | 少用 | — |

三类目录职责：**input** = 预处理（校验/上下文注入/权限），**core** = LLM 调用或工具执行（返回 dict 直接合并 state），**output** = 后处理与路由信号（返回 OutputResult）。插件间经管道 `state` 字典通信，不直接互调。

### 1.2 宿主形态（host_type）

| host_type | 语言 | 机制 | 适用 |
|---|---|---|---|
| `sidecar`（默认） | Python | 独立进程，stdio 上的 MCP JSON-RPC；uv venv 单轨 | 一切常规场景、第三方贡献者、低频插件 |
| `sidecar` + `entry: "mcp:external"` | external | 不写代码，直连第三方 MCP 服务（HTTP 或本地命令） | 接入现成 MCP 工具 |
| `in_process` | Rust (cdylib) | libloading dlopen，C-ABI 取 trait 对象，进程内零 IPC | 高频插件（每轮必执行的管道步骤），从边车轨基准晋升 |

选型依据（ADR `2026-07-13-sidecar-process-model.md`、`2026-08-15-plugin-two-track-and-cordis-mechanisms.md`）：
两轨对所有插件类型开放，开发者按性能需求自选；制度化晋升管线为"边车 → 基准 → in_process"。
wasm 轨已关闭。**能力以 capability 协议唯一定义一次，两轨差异只允许存在于 transport 适配**——同一插件从 Python 迁到 Rust 时 manifest 的 capabilities 声明不变。

### 1.3 三条铁律

1. **声明即注册**：`capabilities.tools` 里声明的工具自动注册进 LLM 工具面（经 `tool_ids` 过滤，见 §3.3）；`capabilities.services` 是内部服务方法元数据，**不进 LLM 面**。
2. **工具契约 fail-closed**：工具必须带 `input_schema` + `output_schema` + `render`（前端渲染意图）；tool_core 执行后按 output_schema 校验，不通过即失败。
3. **配置即快照**：`enabled_plugin_ids` 是启动期快照；改 `plugin.json` 后必须 re-enable（§3.4）。

---

## 2. plugin.json 清单速查

真值源：`kernel/crates/core/src/traits.rs` 的 `PluginManifest`（`deny_unknown_fields`，写错字段名加载即失败）；必填校验在 `kernel/crates/plugin-loader/src/loader.rs`。全字段说明见 `docs/plugin-protocol.md` §2。

### 必填字段

| 字段 | 说明 |
|---|---|
| `id` | 全局唯一，snake_case |
| `name` / `version` | 人读名 / 语义化版本 |
| `plugin_type` | `pipeline` / `tool` / `system` / `composite` |
| `language` | `python` / `rust` / `external` |
| `host_type` | `sidecar`（默认）/ `in_process` |
| `entry` | sidecar 填启动命令（`python server.py`）；native 填 cdylib 文件名；external 固定 `mcp:external`；仅 composite 可空 |
| `capabilities` | 见下 |

### capabilities 四类

```jsonc
"capabilities": {
  "tools": [            // 进 LLM 面的工具声明
    {
      "name": "yaml_validate",
      "description": "YAML 配置文件格式校验",
      "input_schema": { "type": "object", "properties": { ... } },
      "output_schema": { "type": "object", "required": [...], "properties": { ... } },
      "category": "system",        // 可选：file/file_system/search/web/memory/task/
                                   // system/execution/analysis/evaluation/agent/monitoring
      "render": { "card": "form", "title": "YAML 校验" }  // 前端渲染意图，原样透传
    }
  ],
  "services": [          // 内部服务方法（不进 LLM 面），name 形如 "llm.complete_stream"
    { "name": "...", "description": "...", "input_schema": { ... } }
  ],
  "route_signals": [],   // 仅 output 管道插件有意义：next_llm / next_tool / end / wait
  "lifecycle_hooks": ["on_load", "on_unload"]
  // "streaming": { "events": [...], "part_types": [...], "persist": ... }
  //   发流式事件必须声明，否则网关拒绝（fail-closed），见 docs/streaming-protocol.md
}
```

### 常用可选字段

| 字段 | 说明 |
|---|---|
| `invoke_entry` | **pipeline 类型必填**（启动期校验）：sidecar MCP 入口方法名，如 `context_build.execute` |
| `pipeline_role` | 仅 pipeline 类型：`input` / `core` / `output` |
| `priority` | u32，默认 100，数字小者先执行 |
| `requires_services` | 插件间**唯一**耦合轴：条目为 `ns` 或 `ns.method`，映射到提供该能力角色的插件，不点名插件 id；boot 期闸校验，不满足拒启 |
| `http_endpoints[]` | 前端/外部 HTTP 面：`{route_id, method, path, auth, handler_capability, timeout_ms, max_concurrency}`。path 必须落 `/ext/{plugin_id}/**`；`auth` 仅 `none/user/admin`；handler 统一走 `http.handle` capability |
| `config_files[]` | 配置映射 `{id, path, label, target?, fields?}`；未声明收空配置。`target: "env"` + secret fields = 设置页可填密钥 |
| `requires_content` | 需预加载的最近消息条数（默认 0，走 blobs 懒加载） |
| `permissions` | `{filesystem: {read_paths, write_paths}, network: {allowed_hosts}, env_vars, system_calls}` |
| `activation` | `lazy`（默认，首次调用才拉起）/ `eager` / `manual` |
| `enabled` | manifest 级开关；`false` = 已安装不启用 |
| `lifecycle` | 如 `{"idle_timeout_secs": 300}`（sidecar 空闲回收阈值；native 插件用 0 表示常驻） |
| `native` | in_process 必填：`{"artifact": "<crate 名>"}`，加载期校验 cdylib 存在 |
| `mcp` / `mcp_endpoint` | 外部 MCP 接入配置（§6） |
| `ui_schema` / `contributes` | 前端 schema 驱动与前端贡献（页面/主题/CSS，§7） |
| `persistent_fields[]` | 分层持久化的累计型 state 标量字段 |
| `granted_capabilities[]` | 反向调用内核能力的白名单（严格模式下空 = 默认全授予，声明即收窄） |

### 陷阱

- **`error_policy` 已废弃**：不要声明（历史值已收敛移除，非法值加载失败）。
- **`capabilities.resources` 已删除**。
- 文档示例里的 `"dependencies": [...]` **不是内核字段**——内核不解析 Python 依赖；Python 依赖写在 `pyproject.toml`，插件间耦合走 `requires_services`。

---

## 3. 目录布局、发现与注册

### 3.1 单插件标准布局

sidecar（Python）：

```
plugins/shared/tools/<name>/
├── plugin.json          # 清单（必有）
├── server.py            # MCP 适配层：AgentOSPlugin + register_tool/@plugin.tool + run()
├── <业务>.py            # 纯函数实现（可选，管道插件常拆 plugin.py 业务类）
├── test_*.py            # 单元测试（就地放插件目录）
├── pyproject.toml       # 依赖声明 + uv 源映射
├── uv.lock              # uv sync 生成
└── .venv/               # uv sync 生成（内核要求，见 §4.1）
```

native（Rust）：

```
plugins/shared/pipeline/<role>/<name>/
├── plugin.json          # host_type: in_process
├── Cargo.toml           # crate-type = ["cdylib"]
├── src/lib.rs           # 实现 + agentos_plugin_create 导出
├── <artifact>.dll       # cargo build --release 产物，放插件目录根
└── tests/ 或 src 内 #[cfg(test)]
```

### 3.2 双根发现

内置根 `plugins/shared/` + 用户根（环境变量 `AGENTOS_USER_PLUGINS_DIR` 或 OS 标准目录）。同 id 用户根覆盖内置。发现算法只把**直接含 plugin.json 的目录**当插件；无 manifest 的子目录只是父插件的 Python 模块。新建插件目录后内核 watcher 5-8s 热发现 manifest。

### 3.3 LLM 能看到哪些工具：三层过滤链

1. **启用快照**：内核启动时按 `manifest.enabled > config/plugins/default_profile.yaml > 默认 true` 算出 `enabled_plugin_ids`；禁用的插件整个不进注册表（工具/路由信号/HTTP 路由全不暴露）。`default_profile.yaml` 形如：
   ```yaml
   plugins:
     simple_tools:
       enabled: true
     widget_demo:
       enabled: false
   ```
2. **能力注册**：`capabilities.tools[]` 转成 ToolDescriptor 进 CapabilityRegistry。external MCP 工具缺 `input_schema` 直接拒注册（内置工具缺则 `{}` 补注册 + warn）。
3. **tool_ids 白名单**：LLM 实际可见工具 = 注册表 ∩ 当前 Agent 的 `tool_ids`（`config/agents/<...>/<agent_id>.yaml`）。解析不出 tool_ids = **空工具面**（禁止静默全量），仅框架强制工具 `spill_retrieve` 兜底注入。

所以新工具要让 LLM 用到，三处都要通：插件启用 → 声明合法 → 加进 agent 的 `tool_ids`。

### 3.4 改动与生效动作对照

| 改了什么 | 需要做什么 |
|---|---|
| 新建插件目录 / 修改 plugin.json | 等 5-8s 热发现，然后 **re-enable**（前端插件设置页开关，或 `PUT /api/v1/plugins/{id}/enabled`）——会触发 G2 复核（spawn sidecar 校验声明与实现一致）并重注册能力 |
| 改插件 Python 代码 | 空闲 TTL 后热重载（kill + respawn）；不确定就重启内核 |
| 改 agent yaml | mtime 缓存热生效，下一个新任务/会话生效 |
| 改管道 yaml | **重启内核**（启动期编译 + 重名检测） |
| 前端新增 ui_schema/表单 | 前端刷新页面 |

---

## 4. Sidecar（Python）插件开发

### 4.1 运行时模型

- **uv venv 单轨**：entry 首词为裸 `python`/`python3` 的插件，内核强制使用**插件目录内**的解释器（`.venv/Scripts/python.exe` 或 `.venv/bin/python`）。插件目录必须同时有 `pyproject.toml` 和 `.venv`，缺一启动即报错（`PYPROJECT_MISSING` / `VENV_INTERPRETER_MISSING`），**不回退 PATH 裸 python**。初始化：
  ```bash
  uv sync --project plugins/shared/tools/<name>
  ```
- **依赖声明**（pyproject.toml）——SDK 不在任何 registry，必须本地源映射，相对路径按目录深度算：
  ```toml
  [project]
  dependencies = ["agentos-plugin-sdk>=0.2.0", "pyyaml"]

  [tool.uv.sources]
  agentos-plugin-sdk = { path = "../../../sdk", editable = true }   # tools/ 下三级；pipeline/ 下四级
  ```
- **传输**：stdio 上的 MCP JSON-RPC。**stdout 被 JSON-RPC 独占**——插件日志一律走 stderr（SDK logger 已配好），不要 `print()`。
- **生命周期**：懒启动（首次调用才 spawn）→ 握手时按 `config_files` 注入配置 → `notifications/on_load` 触发 `@plugin.on_load` → 空闲 GC（默认 300s，`lifecycle.idle_timeout_secs` 覆盖）→ 崩溃自动 respawn 并重试一次 → 目录 mtime 变化触发热重载。
- **环境变量**：内核透传 `LOG_LEVEL` / `LOG_JSON` / `LOG_FORMAT` 给插件进程。

### 4.2 示例 A：工具插件（参考 `plugins/shared/tools/simple/`）

manifest（节选，全文见 `plugins/shared/tools/simple/plugin.json`）：

```jsonc
{
  "id": "simple_tools",
  "name": "Simple Tools Plugin",
  "version": "1.0.0",
  "plugin_type": "tool",
  "language": "python",
  "host_type": "sidecar",
  "entry": "python server.py",
  "capabilities": {
    "tools": [
      {
        "name": "yaml_validate",
        "description": "YAML 配置文件格式校验",
        "render": { "card": "form", "title": "YAML 校验" },
        "input_schema": { "type": "object", "properties": { "content": {"type": "string"}, "file_path": {"type": "string"} } },
        "output_schema": { "type": "object", "required": ["valid", "errors", "warnings"], "properties": { "valid": {"type": "boolean"}, "errors": {"type": "array"}, "warnings": {"type": "array"} } }
      }
    ],
    "route_signals": [],
    "lifecycle_hooks": ["on_load", "on_unload"]
  },
  "permissions": {},
  "priority": 50
}
```

server.py（`plugins/shared/tools/simple/server.py`）：

```python
from agentos_plugin_sdk import AgentOSPlugin
from system_tools import YAML_VALIDATE_SCHEMA, yaml_validate, ...

def create_plugin() -> AgentOSPlugin:
    plugin = AgentOSPlugin("simple_tools")
    plugin.register_tool("yaml_validate", YAML_VALIDATE_SCHEMA, yaml_validate, "YAML 校验")

    @plugin.on_load
    async def _on_load(params: dict) -> None:
        # 反向调用内核能力：get_capability("service-registry") / ("tool-executor")
        # 拿 CapabilityHandle，handle.call(method, params) 调内核或其它插件
        ...

    return plugin

if __name__ == "__main__":
    create_plugin().run()
```

工具函数就是普通函数，返回 dict（与 output_schema 对齐）。注册两种写法：`plugin.register_tool(name, schema, fn, desc)` 或 `@plugin.tool(name=..., schema=..., description=...)`。

**接线清单**：建目录 → manifest + server.py + pyproject → `uv sync --project <目录>` → default_profile.yaml 置 enabled（或前端开关）→ 把工具名加进目标 agent 的 `tool_ids` → 新会话验证。

### 4.3 示例 B：系统插件 / services / HTTP 端点（参考 `plugins/shared/system/llm/`）

`llm_service` 不暴露 LLM 工具，而是提供内部服务方法 + HTTP 面：

```jsonc
{
  "id": "llm_service",
  "plugin_type": "system",
  "host_type": "sidecar",
  "entry": "python server.py",
  "capabilities": {
    "tools": [],
    "services": [
      {
        "name": "llm.complete_stream",
        "description": "Send a streaming completion request ...",
        "input_schema": { "type": "object", "required": ["model", "messages"], "properties": { "model": {"type": "string"}, "messages": {"type": "array"} } }
      },
      { "name": "llm.health_check", "input_schema": { ... } },
      { "name": "http.handle", "input_schema": { ... } }        // HTTP 端点统一 handler
    ]
  },
  "requires_content": 3,
  "config_files": [
    { "id": "llm", "path": "config/models/llm.yaml", "label": "LLM 模型配置" },
    { "id": "embedding", "path": "config/models/embedding.yaml", "label": "向量模型配置" }
  ],
  "http_endpoints": [
    {
      "route_id": "thinking_mode_health",
      "method": "GET",
      "path": "/ext/llm_service/thinking-mode/healthz",   // 必须落 /ext/{plugin_id}/**
      "auth": "user",                                      // none / user / admin
      "handler_capability": "http.handle",
      "timeout_ms": 5000,
      "max_concurrency": 8
    }
  ],
  "priority": 30
}
```

要点：`services` 经 capability 调用（其它插件 `requires_services` 声明依赖，或经 `service-registry` 反调），不进 LLM 面；`config_files` 声明的配置在握手时注入，插件内 `plugin.get_config()` 读取。

### 4.4 插件间依赖：requires_services（参考 `plugins/shared/system/approval/`）

```jsonc
{
  "id": "approval_service",
  "requires_services": ["human-interaction", "pipeline-executor", "event-bus"],
  "capabilities": { "route_signals": ["wait"], ... }
}
```

条目是**能力角色名**（ns 或 ns.method），注册表映射到提供方插件，不点名插件 id。boot 期依赖闸校验：需要的角色无人提供 → 内核启动被拒。这是插件间唯一合法耦合轴。

### 4.5 示例 C：管道插件（参考 `plugins/shared/pipeline/input/context_build/`）

manifest 关键差异：`plugin_type: "pipeline"` + `pipeline_role` + **`invoke_entry`（必填）**：

```jsonc
{
  "id": "pipeline_context_build",
  "plugin_type": "pipeline",
  "host_type": "sidecar",
  "entry": "python server.py",
  "invoke_entry": "context_build.execute",
  "capabilities": { "tools": [], "route_signals": [], "lifecycle_hooks": ["on_load", "on_unload"] },
  "priority": 50
}
```

实现分两层：`plugin.py` 业务类（继承三角色基类）+ `server.py` MCP 适配层。基类在 `plugins/shared/pipeline/_base/plugin.py`：

- `IInputPlugin` / `IOutputPlugin`：`async def execute(self, ctx: PluginContext) -> PluginResult | OutputResult`
- `ICorePlugin`：`async def execute(self, ctx: PluginContext) -> dict`（返回值直接合并 state）
- `PluginContext`：`state` / `config` / `get_service(name)`
- `PluginResult`：`{state_updates, route_signal, skip_remaining, error}`；OutputResult 同构

server.py 适配层骨架（`plugins/shared/pipeline/input/context_build/server.py`）：

```python
plugin = AgentOSPlugin("context_build_pipeline")

@plugin.tool(
    name="context_build.execute",          # 必须与 manifest invoke_entry 一致
    schema={"type": "object",
            "properties": {"state": {"type": "object"}, "config": {"type": "object", "default": {}}},
            "required": ["state"]},
    description="Execute Context Build pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    from agentos_plugin_sdk.pipeline_types import PluginContext, create_initial_state
    ctx = PluginContext(state=create_initial_state(**state), config=config or {})
    result = await get_instance().execute(ctx)
    if isinstance(result, dict):
        return result                       # Core 插件
    data = {"state_updates": result.state_updates}
    if result.route_signal:
        data["route_signal"] = {"route_type": ..., "target": ..., "reason": ...}
    if result.skip_remaining:
        data["skip_remaining"] = True
    return data

if __name__ == "__main__":
    plugin.run()
```

output 角色插件要产路由信号时，manifest `capabilities.route_signals` 声明可能产出的类型（`next_llm` / `next_tool` / `end` / `wait`），实现里返回 `OutputResult(route_signal=RouteSignal(...))`。完整 output 示例见 `plugins/shared/pipeline/output/task_reminder/`。

### 4.6 测试怎么写

- **位置**：就地 `plugins/shared/**/test_*.py`，CI 必跑镜像在 `tests/plugins/{input,core,output,system,shared}/`。
- **规矩**：每个测试文件必须带分层 marker（`pytestmark = pytest.mark.unit` 等，`--strict-markers` 强制）；用 `importlib.util.spec_from_file_location` 按显式路径加载被测 `plugin.py`（避免同名裸模块串扰）；`PluginContext(state=..., config=...)` 直测 execute；mock 只打外部依赖（网络/DB/时钟），经 `_capability_caller` 注入 AsyncMock 模拟内核反调。
- native 插件测试写在 `#[cfg(test)]`（见 §5.4 示例尾部）或 `kernel/crates/native-sdk-test-plugin`。

---

## 5. Native（Rust cdylib）插件开发

### 5.1 何时选 native

进程内零 IPC（延迟 < 1μs 量级），适合每轮必执行的高频管道步骤。代价：无热加载（cdylib 集合变更 = 内核排空 + 自重启，G8）、编译期耦合（内核与插件必须同 rustc 版本，`rust-toolchain.toml` 锁定 1.85）、panic=abort（panic 会终止进程）。常规路径：先写 sidecar，基准证明开销显著再晋升（`plugins/shared/pipeline/output/sensitive_checker/` 即从 Python 迁移的样板）。

### 5.2 工程骨架与构建

Cargo.toml（参考 `plugins/shared/pipeline/core/tool_core/Cargo.toml`）：

```toml
[package]
name = "pipeline-sensitive-checker-native"
edition = "2021"

[lib]
crate-type = ["cdylib"]        # 产出 .dll/.so/.dylib 供 NativePluginLoader 加载

[dependencies]
agentos-native-sdk = { path = "../../../../../../kernel/crates/native-sdk" }
serde_json = "1"

[profile.release]
panic = "abort"                # cdylib 跨 FFI 标准做法，避免 unwind 跨边界 UB
```

manifest（参考 `plugins/shared/pipeline/output/sensitive_checker/plugin.json`）：

```jsonc
{
  "id": "pipeline_sensitive_checker",
  "plugin_type": "pipeline",
  "pipeline_role": "output",
  "language": "rust",
  "host_type": "in_process",
  "entry": "pipeline_sensitive_checker_native.dll",     // 产物文件名（相对插件目录）
  "invoke_entry": "plugin_execute",                     // 管道入口名（命名治理用）
  "native": { "artifact": "pipeline_sensitive_checker_native" },  // 无扩展名，加载器跨平台重映射 .dll/.so/.dylib
  "lifecycle": { "idle_timeout_secs": 0 }               // native 常驻，不做空闲卸载
}
```

构建与落位：插件目录内 `cargo build --release`，把产物从 `target/release/` 复制到插件目录根（与 `entry` 文件名一致）。产物缺失在**加载期**即报错（契约闸门），不会等到运行。

### 5.3 ABI 契约（`kernel/crates/native-sdk/src/lib.rs`）

- 唯一导出符号：`agentos_plugin_create`，返回裸指针（双重 Box 跨 C-ABI 传递 fat trait 对象）。
- 实现 `PipelinePlugin` trait：`fn execute(&self, ectx: &ExecContext) -> Result<String, String>`，**返回 state_updates 的 JSON 字符串**。
- `ExecContext`：`ctx`（`state_json` / `config_json` / `tenant_id` / `session_id` / `task_id` / `pipeline_id` / `tool_call_json`——`tool_call_json` 含 `{"name": ...}` 即工具调用语义，返回 `{success, data}` 信封）+ `host`（`HostServices`，`call_capability(capability, method, params_json)` 反调内核，与 sidecar 走同一 router）。
- 不用 abi_stable：靠 rustc 版本锁定保证 vtable 一致——**不要用与内核不同的工具链版本编译**。

### 5.4 完整示例（`plugins/shared/pipeline/output/sensitive_checker/src/lib.rs` 节选）

```rust
use agentos_native_sdk::{plugin_into_raw, ExecContext, PipelinePlugin};
use serde_json::{Map, Value};

pub struct SensitiveChecker;

impl PipelinePlugin for SensitiveChecker {
    fn execute(&self, ectx: &ExecContext) -> Result<String, String> {
        let state = ectx.ctx.state_value();
        let config = ectx.ctx.config_value();

        let enabled = config.get("enabled").and_then(|v| v.as_bool()).unwrap_or(true);
        if !enabled {
            return serde_json::to_string(&serde_json::json!({})).map_err(|e| e.to_string());
        }
        let mask = config.get("mask").and_then(|v| v.as_str()).unwrap_or("***");

        // 扫描 state["tool_results"] 中的敏感模式并脱敏 ...
        let mut updates = serde_json::Map::new();
        // updates.insert("tool_results".into(), ...);
        // updates.insert("sensitive_detected".into(), Value::Bool(true));

        serde_json::to_string(&updates).map_err(|e| e.to_string())
    }
}

/// 构造函数（extern "C"）：内核 dlopen 后调它拿 trait 对象裸指针。
#[no_mangle]
pub extern "C" fn agentos_plugin_create() -> *mut () {
    plugin_into_raw(SensitiveChecker)
}
```

业务代码零 unsafe——FFI 全部由 native-sdk 的 `plugin_into_raw` 封装。源文件尾部带 `#[cfg(test)]` 行为契约测试（脱敏正则、state 写回条件、disabled 空转），`cargo test` 直接跑。

### 5.5 生命周期契约

- **永不卸载**：库句柄以 `ManuallyDrop` 持有，逻辑 unload 只从表移除，物理释放随进程退出（Windows 上 `FreeLibrary` 带静态的 cdylib 会访问违例）。
- 内核对 native 的 execute 做 `catch_unwind` 包裹，但 `panic=abort` 下 panic 即进程终止——**native 插件内不要 panic**。
- G8：cdylib 集合变更 = 内核排空在跑任务 + 自重启 + 前端 resync（替换/升级 native 插件前确保无关键任务在跑）。

---

## 6. 外部 MCP 接入（零代码接第三方工具）

不写任何 Python/Rust，用 manifest 直连现成 MCP 服务。约定：`language: "external"`、`entry: "mcp:external"`、`host_type: "sidecar"`（不 spawn 自带进程）。

### 6.1 HTTP 远程形态（参考 `plugins/shared/tools/external_mcp/mcp_registry/plugin.json`）

```jsonc
{
  "id": "mcp_registry_search",
  "plugin_type": "tool",
  "language": "external",
  "host_type": "sidecar",
  "entry": "mcp:external",
  "mcp": {
    "transport": "streamable_http",
    "endpoint": {
      "url": "https://registry.modelcontextprotocol.io",
      "auth": { "type": "api_key", "header_name": "Authorization", "value": "${MCP_REGISTRY_API_KEY}" }
    }
  },
  "capabilities": {
    "tools": [
      { "name": "mcp_registry_search", "description": "Search MCP Registry for registered servers",
        "input_schema": { "type": "object", "required": ["query"], "properties": { "query": {"type": "string"} } } }
    ]
  },
  "config_files": [
    { "id": "api_keys", "path": ".env", "target": "env",
      "fields": [ { "name": "MCP_REGISTRY_API_KEY", "type": "secret", "required": true } ] }
  ]
}
```

### 6.2 本地命令形态（参考 `plugins/shared/tools/external_mcp/omnisearch/plugin.json`）

`"transport": "stdio"` + `endpoint.command/args/env`——内核 spawn 第三方命令（如 `node server.js` 或某 venv 的 python），env 可声明 `PYTHONUTF8: "1"` 等；`${VAR}` 与 `${VAR:-默认值}` 占位在构造时解析。

### 6.3 规矩

- **工具 schema 声明即注册且必须全**：external MCP 工具缺 `input_schema` 拒注册（与内置工具的 warn 补空不同）。
- 密钥走 `config_files` + `target: "env"` + `type: "secret"`，前端插件设置页出现密钥输入框，不落明文。
- `auth.required: false` 时无凭据则跳过鉴权头。

---

## 7. 主题开发

主题有**两条轨**，按需求选：

| 轨 | 形态 | 适合 |
|---|---|---|
| 前端预设（主轨） | `frontend/src/config/themes/presets/*.ts` 导出 `ThemeConfig` | 平台内置主题，与插件体系无关 |
| 插件主题（辅轨） | manifest `contributes.themes`（CSS 变量包，可选 skin 皮肤） | 随插件分发的主题/皮肤，热插拔 |

另有两条补充通道：动态 JSON 主题放 `frontend/public/themes/*.json`（构建期扫描）；用户自定义主题存浏览器 localStorage。

### 7.1 前端预设主题

**四支柱结构**（类型真值源 `frontend/src/types/theme.ts`）：

```ts
export const myTheme: ThemeConfig = {
  id: 'my-theme',
  name: '主题名',
  description: '一句话描述',
  category: 'light',            // light / dark / special
  colors: {                     // 支柱一：颜色
    primary: '#d9738f', secondary: '#b8a1cf', accent: '#f5d6b8',
    background: { main: '...', card: '...', sidebar: '...', input: '...', elevated: '...' },
    text: { primary: '...', secondary: '...', muted: '...', disabled: '...' },
    border: { default: '...', hover: '...', active: '...' },
    status: { success: '...', warning: '...', error: '...', info: '...', running: '...', pending: '...' },
    bubble: { user_bg: '...', user_text: '...', user_radius: '...', ai_bg: '...', ai_text: '...', ai_radius: '...' },
  },
  components: { ... },          // 支柱二：圆角/字体/字号/阴影/按钮/输入框/卡片等
  effects: { glassmorphism: true, animations: true, transitionDuration: 200, ... },  // 支柱三
  backgrounds: { main: { type: 'solid', value: '#fff9f5' }, ... },                    // 支柱四
}
```

完整真实示例照抄结构：`frontend/src/config/themes/presets/moe-soft.ts`（现共 7 个预设：dark / light / deep-space / ocean-breeze / high-contrast / pixel-art / moe-soft）。

**注册三步**（`frontend/src/config/themes/index.ts`）：import 新预设 → 加入 `presetThemes` 映射表 → 在 `themeList` 补一条 `ThemeInfo`（含 `preview` 五色预览，供设置页展示）。注册后自动出现在主题设置页，无需其它登记。

**硬性要求**：配色过 `validateThemeConfig` 校验；文本/背景对比度须达无障碍门槛（high-contrast 预设即 WCAG 2.1 AAA 基准）；文字颜色与背景择优取黑/白，禁止低对比撞色。

### 7.2 插件主题（contributes.themes）

任何插件可在 manifest 声明主题包——纯 CSS 变量键值对，无 JS 执行。真实示例 `plugins/shared/system/visual_customization_demo/plugin.json`：

```jsonc
"contributes": {
  "themes": [
    {
      "id": "gold-lace",
      "name": "金色蕾丝",
      "base": "dark",                     // 打底预设：dark / light
      "variables": {                      // CSS 变量覆盖（后写者胜）
        "--ds-accent-primary": "#D4AF37",
        "--ds-bg-canvas": "#17120A",
        "--ds-bg-panel": "rgba(40, 35, 20, 0.92)",
        "--ds-text-primary": "#F5EECB",
        "--ds-border-active": "#D4AF37",
        "--btn-primary-bg": "#B8860B"
      },
      "backgrounds": { "image": {"enabled": false}, "texture": {"enabled": false} }
    }
  ]
}
```

机制：前端经内核聚合出口 `plugin_contributes` 发现插件主题 → 应用时先取 `base` 指定的内置预设打底，再逐个 `setProperty` 覆盖 variables。批量真实示例见 `plugins/shared/system/dsh_adapter/plugin.json`（16 款皮肤主题）。

### 7.3 皮肤（skin）

`contributes.themes[].skin` 字段进一步激活皮肤能力：CSS 注入 + `hooks.mjs` 装饰层，样式经 `/ext/{pluginId}/styles/skin/...` 三条端点递送。开发规范见 `docs/skin-plugin.md`，运行时实现 `frontend/src/services/skinRuntime.ts`。

---

## 8. Agent 配置

### 8.1 文件位置与层级

```
config/agents/
├── main/agentos.yaml                 # L1 主 Agent（唯一 main）
├── orchestrator/*.yaml               # L2 编排（7 个）
├── executor/                         # L3 执行（general_agent + code/ environment/ generation/ 分组）
├── system/*.yaml                     # evaluator / function_verifier / review
└── task/container_verification_agent.yaml
```

定位规则：`config/agents/` 下递归找 `<agent_id>.yaml`，文件名优先、`config_id` 回退。层级（L1→L2→L3）委托深度上限 3 层，由管道的 level_guard 按 `level` 字段拦截越级。

### 8.2 字段参考（`config/agents/main/agentos.yaml` 为全字段样板）

| 分组 | 字段 |
|---|---|
| 身份 | `config_id` `name` `display_name` `description` `agent_type`（main/orchestrator/executor/system）`category` `level`（L1/L2/L3）`model_tier` `model_name`（executor 可指定如 `deepseek-v4-flash`）`version` `is_active` `status` `tags` `metadata` |
| 提示词 | `system_prompt`（支持 `{{path:...}}` 文件注入、`{{project_root}}` 占位）、`static_vars.items`（静态注入，`type: reference` 或 `{{path:...}}`）、`dynamic_vars.items`（每次执行求值，如 `{{timestamp:...}}`）、`prompt_structure`（include_* 开关 + `layer_order` 提示词分层顺序） |
| 工具面 | **`tool_ids`**（LLM 可见工具白名单，核心字段） |
| 行为约束 | `hard_constraints[]`（硬约束，进提示词）`soft_constraints[]` |
| 运行限额 | `max_iterations`（-1 不限，post 链 stop_check 强制兜底默认 20）`max_reminders` `timeout_seconds` |
| 插件参数 | `plugins.enabled.<plugin_id>`（per-plugin inputs，如 `task_reminder: {max_reminders: 3, cooldown_seconds: 180}`）`plugins.disabled[]` |
| IO 契约 | `input_schema`（用户消息结构）`output_schema` |
| executor 特有 | `deliverables[]`（产出物声明：`output_path: '{{workspace}}/reports/{{task_id}}_report.md'` 等）`recommended_metrics`（默认评估指标如 `file_check`） |
| orchestrator 特有 | `team`（固定外包的 L3 列表） |

### 8.3 消费链（谁读这份 yaml）

- **内核只读一个键**：`tool_ids`（`kernel/crates/config/src/agent_loader.rs` 的 `resolve_agent_tool_ids` 窄接口，mtime 缓存热更新）。内核在构建 LLM 请求时按它过滤工具 schema 注入 `state["tool_schemas"]`。
- **全量配置归 context_build 插件**：管道 prepare 步的 `pipeline_context_build` 按 `state.agent_id` 自行加载 yaml（`plugins/shared/pipeline/input/context_build/plugin.py`），注入 `context.system_prompt`（优先级：state 已有 > agent yaml > 插件默认）、`tool_ids`、`context.agent_name`、`context.agent_level` 等。
- **agent_id 全链传导**：会话创建时写入 initial_state（默认 `agentos`），切换会话 Agent 即换 `agent_id`，后续每轮 prepare/core/post 都按它取配置。`execution_context`（workspace/隔离）同样随 initial_state 与任务参数透传。

### 8.4 修改途径与生效条件

| 途径 | 覆盖 | 生效 |
|---|---|---|
| 前端 `/agents` 页（agent_manager 插件） | 12 个常用字段（config_id/name/display_name/description/agent_type/level/model_tier/system_prompt/tool_ids/max_iterations/timeout_seconds/tags），带 etag 并发保护 + .bak 备份 + 语法校验 | 立即（写的就是 yaml 文件） |
| 直接改 yaml 文件 | 全部字段（static_vars/deliverables/plugins.enabled 等表单没有的） | mtime 缓存，下一个新任务/会话生效，无需重启 |

新增 `tool_ids` 条目时要确认对应插件已启用（§3.3），否则 LLM 面不会有它。

### 8.5 新增一个 Agent 的步骤

1. 在 `config/agents/` 对应层级目录建 `<agent_id>.yaml`（从同层现有 agent 复制改）。
2. 必改：`config_id` / `name` / `level` / `system_prompt` 骨架 / `tool_ids`（只给该 agent 需要的工具）。
3. 需要产出物约束加 `deliverables`；需要固定下游加 `team`。
4. 验证：新会话选该 agent 发消息，观察 `context.agent_name` 与工具面是否符合预期（执行详情可用 `read_execution_detail` 工具分层查看）。

---

## 9. 管道配置

### 9.1 现状

`config/pipelines/autonomous.yaml` 是**唯一现役管道**：所有 Agent 共用它，差异全部由 Agent 配置（system_prompt / tool_ids / model_tier）体现。结构总览与修改须知见 `config/pipelines/README.md`。管道在内核启动期编译（`when` 预编译 AST、引用静态解析、重名冲突启动即 panic），运行时零解析——**改完必须重启内核**。

### 9.2 配置结构

```yaml
name: autonomous

loop_bodies:
  - id: init          # 前处理：workspace/environment 解析，单次执行
    steps: [ ... ]

  - id: main          # agent 自主循环：llm_call ↔ tool_execute
    while: "True"     # 恒真循环；退出靠 step 路由 end + stop_check 兜底
    steps:
      - id: prepare   # input 插件链（context_build → tool_schema → ... → prompt_build → 守卫链）
        steps: [ pipeline_context_build, pipeline_tool_schema, ..., pipeline_prompt_build ]
        context:                    # 自由 KV，merge 进 state 供插件读取（支持 {{state.x}} 模板）
          agent_id: "{{state.agent_id}}"
          model_tier: "{{state.model_tier}}"
      - id: core
        steps:
          - "{{state.core_plugin}}"   # 动态插件：由路由 set 切换 llm_core / tool_core
          - pipeline_spill_guard
        context: { agent_id: "{{state.agent_id}}", temperature: 0.7 }
      - id: post      # output 插件链 + 出口路由
        steps: [ pipeline_track, pipeline_task_reminder, pipeline_stop_check, ... ]
        next: [ ... ]                  # 出口转移 DSL，见 9.3

  - id: exit          # 后处理：workspace 收尾 + 环境释放；run_on_error 保证提前终止也执行
    run_on_error: true
    steps: [ ... ]
```

**step 引用三级命中**（steps 列表项解析顺序）：
① 当前管道内的 step id（组合节点，递归展开）→ ② 公共 step 库 `config/steps/*.yaml` → ③ 插件 id（manifest 里的 id，如 `pipeline_context_build`；注意引用的是**插件 id 不是工具名**）。

### 9.3 G10 路由 DSL（2026-08-15 冻结）

条件永远 `when`、目标永远 `then`、附带写入用 `set`；写在节点/循环体的 `next:` 列表，自上而下首中即走，缺省 when = True：

```yaml
next:
  - when: "raw_tool_calls != [] and raw_tool_calls != None"
    then: loop            # 目标：end / loop / step id（step 级）/ 循环体 id
    set: { core_type: tool_execute, core_plugin: pipeline_tool_core }
  - when: "core_type == 'tool_execute'"
    then: loop
    set: { core_type: llm_call, core_plugin: pipeline_llm_core }
  - then: end             # 兜底
```

`while:` 控制循环体条件；转移优先级：step 级路由设置的 `state.next_phase` > 循环体 `next` > 默认顺序进入下一循环体。旧 DSL 形态（loop_config/routes/exit_routes 等）加载即报错。

### 9.4 per-plugin inputs

给某个管道插件传参的两条通道（走 config，不进 state、不落 trace）：
- 管道 yaml 的 step `context:`（如 `temperature: 0.7`）。
- agent yaml 的 `plugins.enabled.<plugin_id>`（如 `task_reminder: {max_reminders: 3, cooldown_seconds: 180}`）。

插件侧经 `PluginContext.config` / `plugin.get_config()` 读取（native 侧 `ectx.ctx.config_value()`）。

### 9.5 修改流程与验证

1. 改 `config/pipelines/autonomous.yaml`（或前端设置页"管道"可视化编辑器，写同一文件）。
2. 重启内核（启动期编译 + 五类命名冲突检测：body/step id 重复、与插件 id 冲突、Phase 目标不存在）。
3. 跑回归：`pytest tests/test_tool_block_not_end_pipeline.py`（工具块不终结管道的核心行为闸）。

### 9.6 规划中（尚未落地，勿按此编写集成）

以下能力定稿于 `docs/working/管道配置输入契约与动态管道能力设计_20260824.md`，接口未实现：
- 管道顶层 `inputs:` 输入契约声明（source: user/task/trigger/tool/init）。
- 蓝图/实例模型：`pipeline_run.execute(name, inputs)` 出生新管道实例、`chat.send_message(pipeline_id, message)` 续跑；`save+execute` 文件即接口（`config/pipelines/` 为内核保留路径，普通文件工具不可写）。
- 现实出生方式：`chat.send_message` 带 `create: true`（或空 pipeline_id）由引擎生成新 pipeline_id；`task.id = pipeline_id` 单一真值，任务状态由 task_reminder 等任务域插件裁决，内核不回写。

---

## 10. 排障 FAQ

| 症状 | 根因与处置 |
|---|---|
| 新工具 LLM 看不到 | 三层过滤链逐层查：插件在 `config/plugins/default_profile.yaml` enabled？manifest 工具声明带齐 `input_schema`？工具名在目标 agent 的 `tool_ids` 里？ |
| 改了 plugin.json 不生效 | `enabled_plugin_ids` 是启动快照——前端插件设置页关再开（re-enable，触发 G2 复核 + 重注册），或重启内核 |
| 改了插件 Python 代码不生效 | sidecar 空闲 TTL 后才热重载；确认没在 stdout print（破坏 JSON-RPC，日志走 stderr）；急用就重启内核 |
| sidecar 起不来，报 `PYPROJECT_MISSING` / `VENV_INTERPRETER_MISSING` | 插件目录缺 `pyproject.toml` 或 `.venv`——`uv sync --project <插件目录>` 重建；内核不回退 PATH 裸 python |
| native 插件报产物缺失 | `host_type: in_process` 且 `native.artifact` 声明的 cdylib 不在插件目录——`cargo build --release` 后把产物复制到插件目录根，文件名与 `entry` 一致 |
| 流式事件被网关拒绝 | manifest 未声明 `capabilities.streaming`（fail-closed），按 `docs/streaming-protocol.md` 补声明并 re-enable |
| 工具结果前端渲染不对 | `output_schema` / `render` 声明缺失或与返回不符——契约 fail-closed，按实际返回结构补齐 |
| service 方法别的插件调不到 | `services` 不进 LLM 面；调用方声明 `requires_services`（角色名），boot 期闸不满足内核拒启 |
| 管道改完不生效 | 管道启动期编译——重启内核 |
| agent 换了工具白名单不生效 | agent yaml 热生效但只对新任务；确认工具本身已启用（第一条） |
| 前端插件表单/页面没更新 | ui_schema 变化需刷新前端页面 |

---

## 11. 参考资料索引

**协议与规范**
- `docs/plugin-protocol.md` — plugin.json 全字段权威 + 从零开发 echo_tool 完整走查 + SDK 速查 + 调试 FAQ
- `docs/streaming-protocol.md` — 流式事件协议与 `capabilities.streaming` 声明
- `docs/skin-plugin.md` — 皮肤插件（CSS 注入 + hooks.mjs + 递送端点）
- `config/pipelines/README.md` — 管道配置现状与修改须知
- `docs/guides/theme-customization.md` — 主题使用侧说明（预设数量以 `frontend/src/config/themes/index.ts` 为准）

**关键 ADR**
- `docs/decisions/2026-07-13-sidecar-process-model.md` — 双执行路径、按需加载、进程模型宪法
- `docs/decisions/2026-07-24-plugin-runtime-cdylib-wasmtime.md` — cdylib 技术路线（abi_stable 被否）
- `docs/decisions/2026-08-15-plugin-two-track-and-cordis-mechanisms.md` — 两轨终态 + wasm 关闭 + G8/G10
- `docs/decisions/2026-08-18-plugin-dependency-package.md` — `requires_services` 语义
- `docs/decisions/2026-05-14-external-tool-unified-protocol.md` — 外部工具 MCP 优先
- `docs/decisions/2026-08-23-task-chain-state-model-fixes.md` — task = pipeline state 单一真值

**示例插件速查**

| 学什么 | 看哪里 |
|---|---|
| 最小工具插件（sidecar） | `plugins/shared/tools/simple/` |
| services + http_endpoints + config_files | `plugins/shared/system/llm/` |
| requires_services + route_signals | `plugins/shared/system/approval/` |
| 管道 input 插件 / agent 配置自持加载 | `plugins/shared/pipeline/input/context_build/` |
| 管道 output 插件 / 评估闸门 | `plugins/shared/pipeline/output/task_reminder/` |
| native 插件（cdylib） | `plugins/shared/pipeline/output/sensitive_checker/`、`plugins/shared/pipeline/core/tool_core/` |
| native 契约与测试插件 | `kernel/crates/native-sdk/`、`kernel/crates/native-sdk-test-plugin/` |
| 外部 MCP（HTTP 远程 / 本地命令） | `plugins/shared/tools/external_mcp/mcp_registry/`、`.../omnisearch/` |
| 插件主题（contributes.themes） | `plugins/shared/system/visual_customization_demo/`、`plugins/shared/system/dsh_adapter/` |
| 前端预设主题 | `frontend/src/config/themes/presets/moe-soft.ts` + `frontend/src/config/themes/index.ts` |
