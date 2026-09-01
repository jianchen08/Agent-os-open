# Sidecar 插件开发（Python）

> 返回 [开发指南索引](README.md)。前置阅读：[插件开发总览](plugin-development.md)。
> manifest 全字段见 [docs/guides/plugin-protocol.md](guides/plugin-protocol.md)。

## 1. 运行时模型

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

## 2. 示例 A：工具插件（参考 `plugins/shared/tools/simple/`）

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

**接线清单**：建目录 → manifest + server.py + pyproject → `uv sync --project <目录>` → 把工具名加进目标 agent 的 `tool_ids` → 新会话验证。注册无需任何动作：watcher 自动发现并注册（默认启用；profile 显式禁用的才需要启用开关）。

## 3. 示例 B：系统插件 / services / HTTP 端点（参考 `plugins/shared/system/llm/`）

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

## 4. 插件间依赖：requires_services（参考 `plugins/shared/system/approval/`）

```jsonc
{
  "id": "approval_service",
  "requires_services": ["human-interaction", "pipeline-executor", "event-bus"],
  "capabilities": { "route_signals": ["wait"], ... }
}
```

条目是**能力角色名**（ns 或 ns.method），注册表映射到提供方插件，不点名插件 id。boot 期依赖闸校验：需要的角色无人提供 → 内核启动被拒。这是插件间唯一合法耦合轴。

## 5. 示例 C：管道插件（参考 `plugins/shared/pipeline/input/context_build/`）

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
- `PluginResult`：`{state_updates, skip_remaining, error}`；`OutputResult` 是其无新增字段的子类（仅类型语义区分）。出口裁决不走返回值——插件经 `state_updates` 写入路由 DSL 条件依赖的字段即可

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
    if getattr(result, "skip_remaining", False):
        data["skip_remaining"] = True
    return data

if __name__ == "__main__":
    plugin.run()
```

出口转移（下一轮跑什么、何时结束）写在管道 YAML 的 `next:` 路由 DSL（见 [pipeline-configuration.md](pipeline-configuration.md) §3）——output 插件经 `state_updates` 写入 DSL 条件依赖的字段参与裁决（如 task_reminder 写任务状态供评估闸门判定）。manifest 的 `capabilities.route_signals` 与 `OutputResult.route_signal` 是历史声明位/字段，执行链不消费，新插件无需声明。完整 output 示例见 `plugins/shared/pipeline/output/task_reminder/`。

## 6. 测试怎么写

- **位置**：就地 `plugins/shared/**/test_*.py`，CI 必跑镜像在 `tests/plugins/{input,core,output,system,shared}/`。
- **规矩**：每个测试文件必须带分层 marker（`pytestmark = pytest.mark.unit` 等，`--strict-markers` 强制）；用 `importlib.util.spec_from_file_location` 按显式路径加载被测 `plugin.py`（避免同名裸模块串扰）；`PluginContext(state=..., config=...)` 直测 execute；mock 只打外部依赖（网络/DB/时钟），经 `_capability_caller` 注入 AsyncMock 模拟内核反调。
- 关键路径测试须走真实依赖，断言可观察行为（输入→输出/副作用），不断言内部实现细节。
