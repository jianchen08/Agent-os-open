# dsh_adapter — DSH 插件适配器

> task_dsh_plugin_adapter。让 DSH 的非 MCP 后端工具（Node runtime 桥接）
> 与前端视觉组件（vendor 移植 + render 意图路由）在灵汐稳定运行。
> **DSH 源码零改动**（只读参考 `D:\reference_repos\deepseek-harness`，
> commit `47f9438` / 0.1.0-rc.5 锁定，MIT 出处见各文件头）。

## 为什么是 tool 型插件（而非任务书原写的 system 级）

ADR 附录D①：只有 `plugin_type == "tool"` 的 `capabilities.tools` 会注册进
CapabilityRegistry 暴露给 LLM/tool-executor。适配器的桥接工具必须可达，
故落位 `plugins/shared/tools/`（system 型的服务性职责由 contributes 承担，
见 plugin.json `contributes.dsh_adapter`）。

## 结构

```
dsh_adapter/
├── plugin.json            # 3 工具契约（dsh_read/dsh_glob 带 output_schema+render）+ contributes
├── server.py              # MCP sidecar 入口（@plugin.tool 注册面）
├── translator.py          # 清单翻译器（纯函数：DSH 包 → 灵汐注册清单）
├── bridge.py              # Node runtime 宿主（spawn/JSON-RPC/超时/惰性boot/重启）
└── runtime/
    └── dsh-rpc-bridge.mjs # 通道 A fork：boot DSH cordis context，经 stdio 暴露工具
```

## 通道 A 工作原理（runtime 改造桥接）

`runtime/dsh-rpc-bridge.mjs` 以**绝对路径导入** DSH 仓库已构建产物
（`apps/cli/node_modules/@deepseek-ai/*`），boot 最小 cordis context：

```
SystemPrompt → ToolRuntime(dsh-tools) → LocalFileSystem → SubprocessLocal
→ ToolFs(read/write/edit/…) → ToolFsSearch(glob/grep, sampleOverCapGlobResults=true)
```

协议（newline-delimited JSON-RPC over stdio，stderr 走日志）：

| 方法 | 说明 |
|---|---|
| `initialize {cwd}` | boot context + 返回工具契约清单（name/description/input/output schema） |
| `tool/call {name, args, timeoutMs}` | 直接执行（构造最小 ToolRunContext），返回灵汐信封 `{success, data, error, duration_ms}` |
| `shutdown` | dispose + exit 0 |

**有意跳过 DSH 侧 pre/post-execute 钩子管道**：准入由灵汐
isolation_guard/security/approval 把关，输出兜底由 tool_core 的
output_schema 校验 + spill_guard 执行（见 docs/dsh_hook_translation.md）。

### 环境要求

- Node ≥ 20；`AGENTOS_DSH_REPO_ROOT` 指向已构建的 deepseek-harness 仓库
  （默认 `D:\reference_repos\deepseek-harness`，需含 `apps/cli/node_modules`）
- DSH 仓库升级 = 重跑 e2e（`AGENTOS_DSH_E2E=1 pytest
  plugins/shared/tools/tests/test_dsh_adapter.py`）+ 更新 plugin.json 锁定契约

## 闭环验证（output_schema 消费端）

1. LLM 调 `dsh_read` → tool_core 经 tool-executor → sidecar server.py →
   bridge → Node DSH `read` 真实执行；
2. 返回值经 tool_core 按 `output_schema` 校验（fail-closed，违规转错误
   回传 LLM）；
3. 前端按 `render: {card: "read"}` 路由到 vendor ReadBlock（行号 gutter +
   窗口计数），`dsh_glob` 路由 SearchBlock。

## 范围外（诚实边界）

- MCP 工具：`external_mcp` 天然直连，适配器不管
- 功能型前端包（conversation/trajectory/subagent）：依赖 DSH 事件投影服务
- Cordis 运行时/slot 注册/事件投影：零引入
