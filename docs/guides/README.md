# 开发指南索引

> 面向要在本仓库开发插件、配置 Agent 与管道的贡献者。
> **分工原则**：协议权威管字段与契约，分篇教程管具体怎么做，参考篇管排障与手册——同类内容只住一处，篇间用指针互链。

## 上手路径（新手从这里开始）

| 步骤 | 读哪篇 |
|---|---|
| 1. 建立全景：一切皆插件 / 插件类型与宿主 / 目录与注册机制 / 命名约定 | [plugin-development.md](plugin-development.md)（总览） |
| 2. 按需求进分篇（具体怎么做） | 见下表 |
| 3. 卡住了查对照表 | [troubleshooting.md](troubleshooting.md) |

## 分篇教程（具体怎么做）

| 我想… | 读哪篇 |
|---|---|
| 用 Python 写插件（工具 / 服务 / 管道步骤） | [plugin-sidecar-python.md](plugin-sidecar-python.md) |
| 用 Rust 写高性能原生插件（cdylib） | [plugin-native-rust.md](plugin-native-rust.md) |
| 零代码接入第三方 MCP 工具 | [plugin-external-mcp.md](plugin-external-mcp.md) |
| 开发主题 / 皮肤 | [theme-development.md](theme-development.md) |
| 配置 Agent（提示词 / 工具面 / 约束） | [agent-configuration.md](agent-configuration.md) |
| 配置管道（步骤 / 路由 DSL） | [pipeline-configuration.md](pipeline-configuration.md) |

## 协议权威（字段与契约）

| 文档 | 定位 |
|---|---|
| [plugin-protocol.md](plugin-protocol.md) | `plugin.json` 全字段权威 + echo_tool 从零走查 + SDK 速查 |
| [streaming-protocol.md](streaming-protocol.md) | 流式事件协议（`capabilities.streaming` 声明规范） |

## 背景与进阶

| 文档 | 定位 |
|---|---|
| [contract-files-tutorial.md](contract-files-tutorial.md) | 内核契约文件怎么来的（traits.rs / 决策文档 / manifest 演进 / 契约间依赖） |
| [logging.md](logging.md) | 日志体系（双语言运行时日志汇聚与追踪字段） |

## 参考手册

| 文档 | 定位 |
|---|---|
| [troubleshooting.md](troubleshooting.md) | 排障对照表（为什么不生效） |
| [theme-customization.md](theme-customization.md) | 主题使用侧（怎么切换主题，用户视角） |
| [ci-cd-guide.md](ci-cd-guide.md) | 测试与 CI 手册 |
| [ai-coding-spec.md](ai-coding-spec.md) | AI 辅助编程总纲（编码纪律） |

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
| requires_services + 审批闭环 | `plugins/shared/system/approval/` |
| 管道 input 插件 / agent 配置自持加载 | `plugins/shared/pipeline/input/context_build/` |
| 管道 output 插件 / 评估闸门 | `plugins/shared/pipeline/output/task_reminder/` |
| native 插件（cdylib） | `plugins/shared/pipeline/output/sensitive_checker/`、`plugins/shared/pipeline/core/tool_core/` |
| native 契约与测试插件 | `kernel/crates/native-sdk/`、`kernel/crates/native-sdk-test-plugin/` |
| 外部 MCP（HTTP 远程 / 本地命令） | `plugins/shared/tools/external_mcp/mcp_registry/`、`.../omnisearch/` |
| 插件主题（contributes.themes） | `plugins/shared/system/visual_customization_demo/` |
| 插件皮肤（skin + hooks + 三端点） | `plugins/shared/system/dsh_adapter/`（含递送层参考实现） |
| 前端预设主题 | `frontend/src/config/themes/presets/moe-soft.ts` + `frontend/src/config/themes/index.ts` |
