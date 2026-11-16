# 日志体系（0.2 新架构）

0.2 采用 **Rust 内核 + Python sidecar 插件** 的多进程架构。日志体系的设计目标是：
让分布在两个语言运行时、数十个进程里的日志，能汇聚到统一的 sink，并按追踪字段串联。

## 三层架构

```
┌────────────────────────────────────────────────────────────────────┐
│  统一日志 sink（内核 tracing：logs/kernel.log.YYYY-MM-DD 单日志面）│
└────────────────▲──────────────────────────────▲────────────────────┘
                │ tracing 宏                   │ stderr reader 转发
   ┌────────────┴──────────┐      ┌────────────┴────────────────┐
   │ Rust 内核（tracing）     │      │ Python sidecar（logging）     │
   │ api/engine/invoker/mcp │      │ 经 setup_sidecar_logging 配置  │
   │ → tracing::info! 等     │      │ 输出到 stderr（stdout 被占用）  │
   └────────────────────────┘      └─────────────────────────────┘
```

| 层 | 运行时 | 框架 | 输出 |
|---|---|---|---|
| Rust 内核 | tokio 进程内 | `tracing` + `tracing-subscriber`(EnvFilter) | 直接进 tracing sink |
| Python sidecar | 独立进程/进程内 | `logging` + `agentos_plugin_sdk.logging` 统一基础设施 | stderr → 内核 reader 转发 |
| Prompt 审计 | sidecar 内 | 独立 `_prompt_logger` | `data/logs/prompt_audit.log` |

### 为什么 sidecar 日志走 stderr 转发？

sidecar 的 **stdout 被 JSON-RPC 协议占用**（内核与 sidecar 的命令通道），所以日志只能走
stderr。内核在 spawn sidecar 时 `stderr(Stdio::piped())` 并启动一个 stderr reader 协程
（`kernel/crates/mcp/src/client.rs::start_stderr_reader`），逐行消费并以
`tracing::info!(target: "sidecar", "[plugin_id] {line}")` 转发到统一 sink。

**这也修复了一个真实缺陷**：原本 stderr 被 pipe 却从不读取，管道缓冲（~64KB）填满后会
**反向阻塞 sidecar 进程**。现在 stderr 持续被消费，sidecar 不会再卡死。

## 环境变量开关

### 日志级别与格式（进程级，内核 spawn 时透传给 sidecar）

| 变量 | 作用 | 默认 |
|---|---|---|
| `RUST_LOG` | 内核 tracing 的 EnvFilter（如 `info,agentos_mcp=debug`） | `info` |
| `LOG_LEVEL` | sidecar Python logging 级别 | `INFO` |
| `LOG_JSON` | `1`/`true` → sidecar 输出 JSON 格式（生产） | `False` |
| `LOG_FORMAT` | sidecar 自定义格式字符串 | 内置彩色格式 |

内核 spawn sidecar 时会把 `LOG_LEVEL`/`LOG_JSON`/`LOG_FORMAT` 透传（
`kernel/crates/invoker/src/invoker.rs` 的 `extra_env` 块）。

### Prompt 审计落盘（默认关）

| 变量 | 作用 | 默认 |
|---|---|---|
| `AGENTOS_LOG_PROMPT_BODY` | `1`/`true` → 落盘发给远端 API 的完整 messages 请求体 | 关 |
| `AGENTOS_LOG_PROMPT_FILE` | prompt 审计文件路径 | `data/logs/prompt_audit.log` |

> ⚠️ **隐私警告**：请求体含 `api_key`、用户消息。开启时经基础脱敏
> （`sk-`/`Bearer`/`api_key` 字段掩码），但**非穷举**。开启即表示信任本地存储。
> 落盘点在 `plugins/shared/llm/adapter.py::completion()`，provider 适配后、
> litellm 调用前——记录的是真正发往远端 API 边界的请求体。

## 跨进程链路追踪

sidecar 进程按 plugin_id 缓存复用（一个 sidecar 服务多次请求），所以 **per-request 上下文
不能走 env**（env 是进程级常量）。改用 **JSON-RPC params 注入**：

1. 内核 `invoke_pipeline_plugin` 在 `tool_args` 里追加 `_log_ctx`
   （从 `ctx.state` 抽取 `pipeline_id`/`request_id`/`session_id`/`agent_id`）。
2. SDK `_handle_tools_call` 调 handler 前 `LogContext.scoped(**_log_ctx)` 绑定
   （基于 contextvars，async 安全，请求结束自动恢复，防并发污染）。
3. handler 内 `logger.info(...)` 自动带这些字段。

**串联内核与 sidecar 日志**：在统一 sink 里按 `pipeline_id` 过滤，即可看到一次 pipeline
执行在内核与所有 sidecar 里的完整日志链路。

## 日志留存口径

各日志面的轮转与保留上限（机制均已在实现，本节是成文口径；合规问「日志留多久」
以本表为准）：

| 日志面 | 轮转机制 | 保留上限 | 落盘位置 |
|---|---|---|---|
| 内核日志（含 sidecar 转发行） | tracing-appender **daily** 轮转（UTC 口径） | **30 份**（约 30 天） | `<日志目录>/kernel.log.YYYY-MM-DD` |
| prompt 审计 | 独立 `RotatingFileHandler`（按体积） | 单文件 20MB × 备份 3 份（卷上限约 80MB） | `data/logs/prompt_audit.log` |

- **内核 30 份 daily**：`kernel/crates/api/src/bin/agentos-kernel.rs` 的
  `RollingFileAppender`（`.rotation(DAILY)` + `.max_log_files(30)`；daily 轮转无限
  累积会让 logs/ 目录无界增长，30 份封顶）。stdout 不双写，仅文件层构建失败
  （只读盘/无目录权限）时降级为 stdout 唯一诊断面。
- **UTC 口径**（BUG-71 裁决，2026-09-23）：文件名 = 写入时刻的 UTC 日期，轮转
  边界 = UTC 午夜 = 本地 08:00（UTC+8）；本地日 X 的日志行横跨两个文件，本地
  午夜 00:00 时最新文件仍叫 X-1。**外部判活不走文件名**，统一看
  `<日志目录>/kernel.liveness` 标记文件。
- **日志目录**：dev（CWD 可写）落相对 `logs`；装机态 CWD=只读安装目录时落用户
  空间 `<USER_ROOT>/logs`（`resolve_log_dir`，ADR 2026-09-28）。
- **prompt 审计**：`plugins/shared/llm/adapter.py` 的 `_prompt_logger` 挂
  独立 `RotatingFileHandler`（`maxBytes=20MB, backupCount=3`），不依赖父 logger
  （保证 sidecar 进程里一定有 handler）；默认关，开关见上节。
- **观察项（挂监控，另案）**：2026-09-26 实测内核日志单日约 82MB——属日志降噪
  问题非轮替问题，单日体积告警与降噪另案处理，留存口径不因此调整。

## 关键文件

| 文件 | 作用 |
|---|---|
| `kernel/crates/api/src/bin/agentos-kernel.rs` | 内核 tracing 初始化 + daily 轮转/30 份上限 + 日志目录解析 |
| `kernel/crates/mcp/src/client.rs` | `start_stderr_reader`（sidecar 日志汇聚） |
| `kernel/crates/invoker/src/invoker.rs` | LOG_* env 透传 + `_log_ctx` 注入 |
|  `plugins/sdk/src/agentos_plugin_sdk/logging/` | 统一日志基础设施（LoggingConfig/LogContext/Formatter） |
| `plugins/sdk/src/agentos_plugin_sdk/_logging.py` | sidecar `setup_sidecar_logging` |
| `plugins/sdk/src/agentos_plugin_sdk/server.py` | `_handle_tools_call` 的 `_log_ctx` 绑定 |
| `plugins/shared/llm/adapter.py` | prompt 审计落盘 + 脱敏 |
