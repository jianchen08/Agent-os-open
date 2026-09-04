# 外部 MCP 接入（零代码接第三方工具）

> 返回 [开发指南索引](README.md)。前置阅读：[插件开发总览](plugin-development.md)。

不写任何 Python/Rust，用 manifest 直连现成 MCP 服务。约定：`language: "external"`、`entry: "mcp:external"`、`host_type: "sidecar"`（不 spawn 自带进程）。

## 1. HTTP 远程形态（参考 `plugins/shared/tools/external_mcp/mcp_registry/plugin.json`）

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

## 2. 本地命令形态（参考 `plugins/shared/tools/external_mcp/omnisearch/plugin.json`）

`"transport": "stdio"` + `endpoint.command/args/env`——内核 spawn 第三方命令（如 `node server.js` 或某 venv 的 python），env 可声明 `PYTHONUTF8: "1"` 等；`${VAR}` 与 `${VAR:-默认值}` 占位在构造时解析。

## 2.5 一行安装形态（零声明，工具自动导入）

**安装门 ≠ 使用门，两道闸分开**（2026-09-03 裁定）：

- **安装门**：manifest 不写 `capabilities.tools`（或写空数组）→ G2 安装期
  spawn 服务并调 `tools/list`，把返回的**全部工具**（名/描述/inputSchema）
  观测导入注册面——多工具服务免抄 schema：

  ```jsonc
  {
    "id": "playwright",
    "plugin_type": "tool", "language": "external",
    "host_type": "sidecar", "entry": "mcp:external",
    "mcp": {
      "transport": "stdio",
      "endpoint": { "command": "npx", "args": ["-y", "@playwright/mcp@latest"] }
    }
  }
  ```

  观测失败（服务没起来）→ 不自拟工具，注册面零工具；watcher resync 见
  「零工具的动态 MCP」自动补观测（服务恢复即导入）。服务端工具集升级后，
  触碰 manifest 或切换一次 enable 即重导入。
- **使用门（显式，安装永不自动）**：导入的工具对 LLM 不可见，直到目标 agent
  的 `tool_ids` 显式加入。两种配法并存（并集）：精确工具名逐个罗列，或
  **写插件名一条透出该插件全部工具**（如 `tool_ids: [..., "playwright"]`）。
  空白名单 = 零工具可见，安装动作不改任何 agent 的白名单。

## 3. 规矩

- **工具 schema 声明即注册且必须全**（静态模式）：声明了 tools 的 external MCP
  工具缺 `input_schema` 拒注册；零声明走 §2.5 观测导入，两态互斥。
- **env 引用即声明**（2026-09-03 裁定）：endpoint 里 `${VAR}` 引用由内核在装载期自动生成 `config_files[target="env"]` 声明并出口设置页，手写声明仍合法但不再必须；想自定义表单文案（label/type=secret/required）可按上例手写覆盖。
- `auth.required: false` 时无凭据则跳过鉴权头。
- 接入后 watcher 自动发现注册（默认启用），把工具名（或插件名）加进目标 agent `tool_ids` 后 LLM 可见（见[总览](plugin-development.md#6-llm-能看到哪些工具三层过滤链)）。
