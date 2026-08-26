# 开发指南索引

> 面向要在本仓库开发插件、配置 Agent 与管道的贡献者。按"要做什么"选择分篇阅读；
> 所有示例均指向仓库内真实插件，可直接对照源码。
>
> **字段级权威**：`plugin.json` 全字段规范见 [docs/plugin-protocol.md](../plugin-protocol.md)。

| 我想… | 读哪篇 |
|---|---|
| 了解插件体系全景 / 选宿主形态 / 目录与注册机制 / 命名约定 | [plugin-development.md](plugin-development.md) |
| 想懂内核契约文件怎么来的（traits.rs / 决策文档 / manifest 演进 / 契约间依赖） | [contract_files_tutorial.md](contract_files_tutorial.md) |
| 用 Python 写一个插件（工具 / 服务 / 管道步骤） | [plugin-sidecar-python.md](plugin-sidecar-python.md) |
| 用 Rust 写高性能原生插件（cdylib） | [plugin-native-rust.md](plugin-native-rust.md) |
| 不写代码，接入第三方 MCP 工具 | [plugin-external-mcp.md](plugin-external-mcp.md) |
| 开发主题 / 皮肤 | [theme-development.md](theme-development.md) |
| 配置一个 Agent（提示词 / 工具面 / 约束） | [agent-configuration.md](agent-configuration.md) |
| 配置管道（步骤 / 路由 DSL） | [pipeline-configuration.md](pipeline-configuration.md) |
| 排查"为什么不生效" | [troubleshooting.md](troubleshooting.md) |

**推荐阅读顺序**：新手从 plugin-development.md 总览入手 → 按需求进 sidecar 或 native 分篇 →
用 agent-configuration.md / pipeline-configuration.md 完成接线 → 遇到问题查 troubleshooting.md。

## 规范与协议文档

- `docs/plugin-protocol.md` — plugin.json manifest 全字段权威 + 从零开发 echo_tool 完整走查 + SDK 速查
- `docs/streaming-protocol.md` — 流式事件协议与 `capabilities.streaming` 声明
- `docs/skin-plugin.md` — 皮肤插件（CSS 注入 + hooks.mjs + 递送端点）
- `docs/guides/theme-customization.md` — 主题使用侧说明（用户视角）
- `config/pipelines/README.md` — 管道配置现状与修改须知

## 关键 ADR

- `docs/decisions/2026-07-13-sidecar-process-model.md` — 双执行路径、按需加载、进程模型宪法
- `docs/decisions/2026-07-24-plugin-runtime-cdylib-wasmtime.md` — cdylib 技术路线（abi_stable 被否）
- `docs/decisions/2026-08-15-plugin-two-track-and-cordis-mechanisms.md` — 两轨终态 + wasm 关闭 + G8/G10
- `docs/decisions/2026-08-18-plugin-dependency-package.md` — `requires_services` 语义
- `docs/decisions/2026-05-14-external-tool-unified-protocol.md` — 外部工具 MCP 优先
- `docs/decisions/2026-08-23-task-chain-state-model-fixes.md` — task = pipeline state 单一真值

## 示例插件速查

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
