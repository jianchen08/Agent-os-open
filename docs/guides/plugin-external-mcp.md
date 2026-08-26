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

## 3. 规矩

- **工具 schema 声明即注册且必须全**：external MCP 工具缺 `input_schema` 拒注册（与内置工具的 warn 补空不同）。
- 密钥走 `config_files` + `target: "env"` + `type: "secret"`，前端插件设置页出现密钥输入框，不落明文。
- `auth.required: false` 时无凭据则跳过鉴权头。
- 接入后同样走三层可见性链：default_profile 启用 + 工具名加进 agent `tool_ids`（见[总览](plugin-development.md#6-llm-能看到哪些工具三层过滤链)）。
