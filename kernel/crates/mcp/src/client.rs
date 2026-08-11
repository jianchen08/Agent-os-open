//! MCP JSON-RPC 客户端
//!
//! 实现基于 JSON-RPC 2.0 的 MCP 协议客户端，支持 stdio 和 HTTP 两种 transport。
//! 通过 stdin/stdout 与 Python 边车进程通信，完成 initialize 握手和 tools/call 调用。
//!
//! [来源: docs/tasks/task_05_plugin_system.md AC-04-4]

use std::collections::HashMap;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::{oneshot, Mutex};
use uuid::Uuid;

use crate::capability::{parse_capability_method_with, CapabilityRouter, STANDARD_CAPABILITIES};
use crate::error::McpError;

// tracing 的 warn 宏用于 reader_loop 的降级日志
use tracing::warn;

/// JSON-RPC 2.0 请求
#[derive(Debug, Serialize)]
struct JsonRpcRequest {
    jsonrpc: &'static str,
    id: String,
    method: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    params: Option<Value>,
}

/// JSON-RPC 2.0 响应
#[derive(Debug, Deserialize)]
struct JsonRpcResponse {
    #[allow(dead_code)]
    jsonrpc: Option<String>,
    #[allow(dead_code)]
    id: Option<String>,
    result: Option<Value>,
    error: Option<JsonRpcError>,
}

/// JSON-RPC 错误
#[derive(Debug, Deserialize)]
struct JsonRpcError {
    code: i64,
    message: String,
    #[allow(dead_code)]
    data: Option<Value>,
}

/// MCP transport 类型
#[derive(Debug, Clone)]
pub enum McpTransport {
    /// stdio transport: fork 子进程，通过 stdin/stdout 传 JSON-RPC
    Stdio { command: String, args: Vec<String> },
    /// HTTP transport: 通过 HTTP POST 传 JSON-RPC
    Http { url: String },
}

/// MCP 客户端
///
/// 管理 MCP 边车进程的生命周期和 JSON-RPC 通信。
/// 支持 initialize 握手 → tools/list → tools/call 流程。
///
/// 双向通信：
/// - 内核→sidecar：send_request / send_notification（已有）
/// - sidecar→内核：reader loop 识别 incoming request，路由到 CapabilityRouter（新增）
pub struct McpClient {
    /// 传输方式
    transport: McpTransport,
    /// 子进程工作目录（stdio 模式下设置，确保插件相对路径可解析）
    working_dir: Option<std::path::PathBuf>,
    /// 额外环境变量（注入给 sidecar 子进程，如 PYTHONPATH 指向公共依赖）
    extra_env: Vec<(String, String)>,
    /// 子进程（stdio 模式）
    child: Option<Arc<Mutex<Child>>>,
    /// stdin 写入端
    stdin: Option<Arc<Mutex<tokio::process::ChildStdin>>>,
    /// stdout 读取端
    stdout: Option<Arc<Mutex<BufReader<tokio::process::ChildStdout>>>>,
    /// stderr 读取端
    ///
    /// Python sidecar 的 `logging` 默认输出到 stderr（stdout 被 JSON-RPC 协议占用）。
    /// 必须消费 stderr，否则管道缓冲（典型 64KB）填满后会反向阻塞 sidecar 进程。
    /// 每行经 `start_stderr_reader` 转发到内核 tracing，实现 sidecar 日志的统一汇聚。
    stderr: Option<Arc<Mutex<BufReader<tokio::process::ChildStderr>>>>,
    /// 插件标识，用于 stderr 转发时区分来源。
    plugin_id: Option<String>,
    /// 等待响应的 oneshot 发送器
    pending: Arc<Mutex<HashMap<String, oneshot::Sender<JsonRpcResponse>>>>,
    /// 是否已初始化
    initialized: Arc<Mutex<bool>>,
    /// Capability 路由器——处理 sidecar 反向调用内核能力。
    /// None 时 sidecar 反向调用将被拒绝（返回 method not found）。
    router: Option<Arc<dyn CapabilityRouter>>,
    /// JSON-RPC 请求等待响应的超时（默认 300s）。
    ///
    /// 背景（llm_core.execute 120s 超时修复）：原实现硬编码 120s，与 sidecar 端
    /// LLM 调用的 first_token_timeout（llm.yaml 默认 120s）形成竞态——reasoning
    /// model 首 token 接近 120s 时，内核先超时掐断，sidecar 的最终响应永远到不了
    /// 内核（pending 已被移除）。默认 300s 覆盖 sidecar 端默认 first_token_timeout
    /// （120s）并留足余量；调用方可用 [`McpClient::with_request_timeout`] 覆盖。
    request_timeout: Duration,
}

impl McpClient {
    /// 创建 stdio transport 客户端
    pub fn new_stdio(command: impl Into<String>, args: Vec<String>) -> Self {
        Self {
            transport: McpTransport::Stdio {
                command: command.into(),
                args,
            },
            working_dir: None,
            extra_env: Vec::new(),
            child: None,
            stdin: None,
            stdout: None,
            stderr: None,
            plugin_id: None,
            pending: Arc::new(Mutex::new(HashMap::new())),
            initialized: Arc::new(Mutex::new(false)),
            router: None,
            request_timeout: Duration::from_secs(300),
        }
    }

    /// 创建 HTTP transport 客户端
    pub fn new_http(url: impl Into<String>) -> Self {
        Self {
            transport: McpTransport::Http { url: url.into() },
            working_dir: None,
            extra_env: Vec::new(),
            child: None,
            stdin: None,
            stdout: None,
            stderr: None,
            plugin_id: None,
            pending: Arc::new(Mutex::new(HashMap::new())),
            initialized: Arc::new(Mutex::new(false)),
            router: None,
            request_timeout: Duration::from_secs(300),
        }
    }

    /// 设置 JSON-RPC 请求等待响应的超时。
    ///
    /// 调用方（如内核 invoker 对 LLM 类插件）可根据 sidecar 端调用时长
    /// （llm.yaml call_timeout / first_token_timeout）配置，避免内核先于
    /// sidecar 掐断导致"响应永远到不了"的假超时。
    pub fn with_request_timeout(mut self, timeout: Duration) -> Self {
        self.request_timeout = timeout;
        self
    }

    /// 设置子进程工作目录（stdio 模式下生效）。
    ///
    /// 插件 entry 如 `python3 server.py` 需要在插件目录下执行，
    /// 否则 server.py 的相对路径无法解析。
    pub fn with_working_dir(mut self, dir: impl Into<std::path::PathBuf>) -> Self {
        self.working_dir = Some(dir.into());
        self
    }

    /// 设置 Capability 路由器（启用 sidecar→内核反向调用）。
    ///
    /// 设置后，reader loop 会识别 sidecar 主动发来的 JSON-RPC request，
    /// 按 `<capability>.<method>` 路由到 router，并通过 stdin 回写 response。
    ///
    /// 未设置时，sidecar 反向调用将被拒绝（返回 method not found）。
    pub fn with_router(mut self, router: Arc<dyn CapabilityRouter>) -> Self {
        self.router = Some(router);
        self
    }

    /// 设置额外环境变量（注入给 sidecar 子进程）。
    ///
    /// 用于注入 PYTHONPATH 等公共依赖路径，让 sidecar 能 import 公共业务包。
    pub fn with_extra_env(mut self, env: Vec<(String, String)>) -> Self {
        self.extra_env = env;
        self
    }

    /// 设置插件标识，用于 stderr 转发时区分日志来源（前缀 `[plugin_id]`）。
    pub fn with_plugin_id(mut self, id: impl Into<String>) -> Self {
        self.plugin_id = Some(id.into());
        self
    }

    /// 启动子进程并连接（stdio 模式）
    pub async fn connect(&mut self) -> Result<(), McpError> {
        match &self.transport {
            McpTransport::Stdio { command, args } => {
                let mut cmd = Command::new(command);
                cmd.args(args)
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped())
                    .kill_on_drop(true);

                // Unix：让 sidecar 成为新进程组组长。kill 时对 -pid 发进程组
                // 信号可连孙进程（bash 工具拉起的进程树）一起杀——防 sidecar
                // 被卸载/崩溃后 bash 孙进程变孤儿（治理缺口，见 kill_process_tree）。
                #[cfg(unix)]
                {
                    cmd.process_group(0);
                }

                // 设置工作目录（插件目录），确保 server.py 等相对路径可解析
                if let Some(ref dir) = self.working_dir {
                    cmd.current_dir(dir);
                }

                // 注入额外环境变量（如 PYTHONPATH 指向公共依赖 src/）
                if !self.extra_env.is_empty() {
                    cmd.envs(self.extra_env.iter().map(|(k, v)| (k.as_str(), v.as_str())));
                }

                let mut child = cmd.spawn().map_err(|e| McpError::SpawnFailed {
                    command: command.clone(),
                    message: e.to_string(),
                })?;

                let stdin = child
                    .stdin
                    .take()
                    .ok_or_else(|| McpError::ConnectionFailed {
                        message: "failed to get stdin".to_string(),
                    })?;
                let stdout = child
                    .stdout
                    .take()
                    .ok_or_else(|| McpError::ConnectionFailed {
                        message: "failed to get stdout".to_string(),
                    })?;
                // stderr 不 take 会导致管道缓冲填满后阻塞 sidecar；这里 take 出来
                // 交给 start_stderr_reader 消费并转发到 tracing。
                let stderr = child.stderr.take();

                self.child = Some(Arc::new(Mutex::new(child)));
                self.stdin = Some(Arc::new(Mutex::new(stdin)));
                self.stdout = Some(Arc::new(Mutex::new(BufReader::new(stdout))));
                if let Some(stderr) = stderr {
                    self.stderr = Some(Arc::new(Mutex::new(BufReader::new(stderr))));
                }

                // 启动 stdout 读取循环
                self.start_reader_loop().await;
                // 启动 stderr 读取循环（消费 sidecar 日志，转发到 tracing）
                self.start_stderr_reader().await;

                Ok(())
            }
            McpTransport::Http { .. } => {
                // HTTP 模式不需要启动子进程
                Ok(())
            }
        }
    }

    /// 启动 stdout 读取循环
    ///
    /// 区分两类消息：
    /// - response（对内核之前请求的响应）：有 id 且在 pending 表中 → resolve oneshot
    /// - incoming request（sidecar 主动反向调用）：有 method 且不在 pending 表 → 路由到 router
    /// - notification（sidecar 单向通知）：有 method 无 id → 当前忽略（无标准场景）
    async fn start_reader_loop(&self) {
        if let Some(stdout) = &self.stdout {
            let stdout = Arc::clone(stdout);
            let pending = Arc::clone(&self.pending);
            let router = self.router.clone();
            // stdin 在 stdio 模式下必然存在（与 stdout 同时建立）；
            // reader loop 仅在 stdout 存在时启动，故 stdin 安全解包。
            let Some(stdin) = self.stdin.clone() else {
                warn!("reader_loop skipped: stdin is None in stdio mode");
                return;
            };

            tokio::spawn(async move {
                let mut reader = stdout.lock().await;
                let mut line = String::new();

                loop {
                    line.clear();
                    match reader.read_line(&mut line).await {
                        Ok(0) => {
                            // sidecar stdout 关闭（进程退出/崩溃）。原静默 break 会导致
                            // 进行中的 send_request（如 initialize）等到 120s 超时才失败，
                            // 这里记 warn 让"sidecar 崩溃"在日志里可见。
                            tracing::warn!("[mcp] sidecar stdout EOF（进程退出）");
                            // 关键修复（工具调用"调用前卡死"根因之二）：
                            // sidecar 崩溃时，进行中的 send_request 的 oneshot 若不 resolve，
                            // 调用方只能等满 120s 超时——用户感知为"工具调用卡死"。
                            // 这里清空 pending（drop 所有 oneshot sender）→ rx 端立即收到
                            // channel closed → send_request 快速失败返回错误，不阻塞调用方。
                            let mut pending_map = pending.lock().await;
                            pending_map.clear();
                            break; // EOF
                        }
                        Ok(_) => {
                            let line_str = line.trim().to_string();
                            if line_str.is_empty() {
                                continue;
                            }
                            // 先尝试解析为通用 JSON（同时覆盖 response 和 request）
                            let msg: Value = match serde_json::from_str(&line_str) {
                                Ok(v) => v,
                                Err(_) => continue,
                            };

                            // 分支1：可能是对内核请求的响应（有 id，无 method）
                            if let Some(id) = msg.get("id").and_then(|v| v.as_str()) {
                                let mut pending_map = pending.lock().await;
                                if let Some(sender) = pending_map.remove(id) {
                                    // 匹配 pending → 这是 response
                                    if let Ok(response) =
                                        serde_json::from_str::<JsonRpcResponse>(&line_str)
                                    {
                                        let _ = sender.send(response);
                                    }
                                    continue;
                                }
                            }

                            // 分支2：sidecar 主动发起的 request（有 method + id）
                            if let (Some(method), Some(id)) = (
                                msg.get("method").and_then(|v| v.as_str()),
                                msg.get("id").and_then(|v| v.as_str()).map(String::from),
                            ) {
                                handle_incoming_request(method, &msg, &id, &router, &stdin)
                                    .await;
                                continue;
                            }
                            // 分支3：sidecar 主动发起的 notification（有 method 无 id）
                            // 用于流式 chunk 推送：fire-and-forget，内核不回 response。
                            if let Some(method) = msg.get("method").and_then(|v| v.as_str()).map(String::from) {
                                let params = msg.get("params").cloned().unwrap_or(Value::Null);
                                // 关键：notification 处理用 spawn 并发，不阻塞 reader loop！
                                // handle_incoming_notification 内部会 await session.emit_event
                                //（WS 发送），若串行 await 会让后续 notification 攒批排队
                                // （sidecar 实时写了 45 个跨 11s，内核却攒批 2s 内才处理完）。
                                // notification 是 fire-and-forget，无需等结果，spawn 后继续读下一行。
                                let router_clone = router.clone();
                                tokio::spawn(async move {
                                    handle_incoming_notification(&method, params, &router_clone).await;
                                });
                            }
                            // 其余忽略
                        }
                        Err(_) => break,
                    }
                }
            });
        }
    }

    /// 启动 stderr 读取循环
    ///
    /// 消费 sidecar 子进程的 stderr（Python `logging` 默认输出目标），逐行转发到
    /// 内核 tracing。这是 sidecar 日志汇聚到统一 sink 的核心机制：
    /// - 不消费会导致管道缓冲填满（~64KB）后阻塞 sidecar；
    /// - 每行带 `[plugin_id]` 前缀，target 统一 `sidecar`，便于在内核日志中区分来源。
    ///
    /// 日志级别映射：sidecar 的 stderr 行本身不带级别信息（Python logging 默认格式
    /// 含级别名，但格式可配），这里统一以 INFO 转发，避免误判；EOF 记 WARN。
    /// 若 sidecar 行内已含 `ERROR`/`WARNING` 等关键字，仍按 INFO 转发（保真原文），
    /// 由日志聚合工具按内容过滤。
    ///
    /// 非 UTF-8 容错（llm_core.execute 120s 超时修复根因）：原实现用
    /// `read_line`（严格 UTF-8 解码），Windows 宿主上 Python sidecar 的 stderr
    /// （如含 GBK 中文路径的 traceback）产生非法字节时返回 `InvalidData` →
    /// break 退出消费循环 → sidecar 继续写 stderr 管道，缓冲填满（~64KB）后
    /// `write` 阻塞 → sidecar 进程卡死 → 无法响应 MCP 请求 → 内核等待超时。
    /// 改为 `read_until(b'\n')` 按原始字节读行 + `String::from_utf8_lossy`
    /// 解码（非法字节替换为 U+FFFD），读取永不因编码中断，杜绝管道阻塞。
    async fn start_stderr_reader(&self) {
        if let Some(stderr) = &self.stderr {
            let stderr = Arc::clone(stderr);
            let plugin_id = self.plugin_id.clone().unwrap_or_else(|| "?".to_string());

            tokio::spawn(async move {
                let mut reader = stderr.lock().await;
                let mut buf: Vec<u8> = Vec::new();

                loop {
                    buf.clear();
                    // read_until 按字节读，不校验 UTF-8（read_line 会因非法字节报
                    // InvalidData 并 break，导致 stderr 管道阻塞 sidecar）。
                    match reader.read_until(b'\n', &mut buf).await {
                        Ok(0) => {
                            // stderr 关闭（通常与 sidecar 退出同时发生）。
                            tracing::warn!(
                                target: "sidecar",
                                "[{}] stderr EOF",
                                plugin_id
                            );
                            break;
                        }
                        Ok(_) => {
                            // lossy 解码：非 UTF-8 字节替换为 U+FFFD，不中断消费。
                            let line = String::from_utf8_lossy(&buf);
                            let trimmed = line.trim();
                            if trimmed.is_empty() {
                                continue;
                            }
                            tracing::info!(target: "sidecar", "[{}] {}", plugin_id, trimmed);
                        }
                        Err(e) => {
                            tracing::warn!(
                                target: "sidecar",
                                "[{}] stderr read error: {}",
                                plugin_id,
                                e
                            );
                            break;
                        }
                    }
                }
            });
        }
    }

    /// 发送 JSON-RPC 请求并等待响应
    async fn send_request(&self, method: &str, params: Option<Value>) -> Result<Value, McpError> {
        let id = Uuid::new_v4().to_string();
        let request = JsonRpcRequest {
            jsonrpc: "2.0",
            id: id.clone(),
            method: method.to_string(),
            params,
        };

        let request_str = serde_json::to_string(&request).map_err(|e| McpError::Protocol {
            message: format!("serialize error: {}", e),
        })?;

        // 注册 oneshot 等待响应
        let (tx, rx) = oneshot::channel();
        {
            let mut pending = self.pending.lock().await;
            pending.insert(id.clone(), tx);
        }

        // 发送请求
        if let Some(stdin) = &self.stdin {
            let mut writer = stdin.lock().await;
            writer
                .write_all(request_str.as_bytes())
                .await
                .map_err(|e| McpError::Transport {
                    message: format!("write error: {}", e),
                })?;
            writer
                .write_all(b"\n")
                .await
                .map_err(|e| McpError::Transport {
                    message: format!("write newline error: {}", e),
                })?;
            writer.flush().await.map_err(|e| McpError::Transport {
                message: format!("flush error: {}", e),
            })?;
        } else {
            return Err(McpError::ConnectionFailed {
                message: "not connected (stdin is None)".to_string(),
            });
        }

        // 等待响应（超时默认 300s：LLM 调用尤其是 reasoning model 首次可能较慢。
        // 原硬编码 120s 与 sidecar 端 first_token_timeout（llm.yaml 默认 120s）
        // 形成竞态——reasoning model 首 token 接近 120s 时内核先掐断，sidecar 的
        // 最终响应永远到不了内核。默认 300s 覆盖并留足余量，可用
        // with_request_timeout 按插件覆盖。）
        let response = tokio::time::timeout(self.request_timeout, rx)
            .await
            .map_err(|_| McpError::Timeout {
                timeout_secs: self.request_timeout.as_secs(),
            })?
            .map_err(|_| McpError::Protocol {
                message: "response channel closed".to_string(),
            })?;

        if let Some(error) = response.error {
            return Err(McpError::Protocol {
                message: format!("[{}] {}", error.code, error.message),
            });
        }

        response.result.ok_or_else(|| McpError::Protocol {
            message: "response has no result".to_string(),
        })
    }

    /// MCP initialize 握手（无配置参数）。
    ///
    /// 向后兼容包装：等价于 `initialize(&Value::Null)`。
    ///
    /// # Deprecated
    ///
    /// 请使用 `initialize(&config)` 传递插件配置。
    #[deprecated(since = "0.2.1", note = "use initialize(&config) to pass plugin config")]
    pub async fn initialize_without_config(&self) -> Result<Value, McpError> {
        self.initialize(&Value::Null).await
    }

    /// MCP initialize 握手
    ///
    /// 发送 initialize 请求完成 MCP 协议握手。
    /// config 参数为插件配置 JSON，将包含在 initialize params 的 `config` 字段中。
    pub async fn initialize(&self, config: &Value) -> Result<Value, McpError> {
        // 声明内核可被 sidecar 反向调用的 capability 名单。
        // 从 router 的 known_namespaces() 动态派生——router 是注册表时，
        // 运行时注册的 namespace（如插件的 human-interaction）自动出现在声明里，
        // sidecar SDK 据此创建 CapabilityHandle。无 router 时声明空。
        let capabilities: Value = match &self.router {
            Some(r) => build_declared_capabilities_from_namespaces(&r.known_namespaces()),
            None => build_declared_capabilities(false),
        };

        let params = serde_json::json!({
            "protocolVersion": "2024-11-05",
            "capabilities": capabilities,
            "clientInfo": {
                "name": "agentos",
                "version": "0.2.0"
            },
            "config": config
        });

        // DEBT: protocolVersion 硬编码为 "2024-11-05"。ceiling: MCP 协议升级时。
        // upgrade: MCP 发布新 spec 版本时更新。
        let result = self.send_request("initialize", Some(params)).await?;

        // 发送 initialized 通知（fire-and-forget，不等响应）
        self.send_notification("notifications/initialized", None)
            .await?;

        *self.initialized.lock().await = true;

        Ok(result)
    }

    /// 发送 JSON-RPC notification（不等响应）。
    ///
    /// 用于生命周期钩子等 fire-and-forget 场景。
    pub async fn send_notification(
        &self,
        method: &str,
        params: Option<Value>,
    ) -> Result<(), McpError> {
        let notification = serde_json::json!({
            "jsonrpc": "2.0",
            "method": method,
            "params": params.unwrap_or(Value::Null)
        });

        let notif_str = serde_json::to_string(&notification).map_err(|e| McpError::Protocol {
            message: format!("serialize notification error: {}", e),
        })?;

        if let Some(stdin) = &self.stdin {
            let mut writer = stdin.lock().await;
            writer
                .write_all(notif_str.as_bytes())
                .await
                .map_err(|e| McpError::Transport {
                    message: format!("write error: {}", e),
                })?;
            writer
                .write_all(b"\n")
                .await
                .map_err(|e| McpError::Transport {
                    message: format!("write newline error: {}", e),
                })?;
            writer.flush().await.map_err(|e| McpError::Transport {
                message: format!("flush error: {}", e),
            })?;
        } else {
            // DEBT: HTTP transport 的 notification 发送尚未实现。ceiling: 当前仅支持 stdio。
            // upgrade: 引入 reqwest/hyper 后实现 HTTP POST notification。
            return Err(McpError::Transport {
                message: "notification via HTTP transport not implemented".to_string(),
            });
        }

        Ok(())
    }

    /// 通过 notifications/on_config_change 通知插件配置变更。
    ///
    /// 配置热重载后调用此方法，将新配置推送给已连接的插件进程。
    ///
    /// # Errors
    ///
    /// 当前仅支持 stdio transport。HTTP transport 模式下会返回
    /// `McpError::Transport`（HTTP notification 尚未实现）。
    pub async fn send_config_change(&self, config: &Value) -> Result<(), McpError> {
        self.send_notification(
            "notifications/on_config_change",
            Some(serde_json::json!({ "config": config })),
        )
        .await
    }

    /// 调用 tools/list 获取可用工具列表
    pub async fn list_tools(&self) -> Result<Value, McpError> {
        self.send_request("tools/list", None).await
    }

    /// 调用 tools/call 执行工具
    pub async fn call_tool(&self, name: &str, arguments: &Value) -> Result<Value, McpError> {
        let params = serde_json::json!({
            "name": name,
            "arguments": arguments
        });

        let result = self
            .send_request("tools/call", Some(params))
            .await
            .map_err(|e| McpError::ToolCallFailed {
                tool_name: name.to_string(),
                message: e.to_string(),
            })?;

        Ok(result)
    }

    /// 检查子进程是否存活
    pub async fn is_alive(&self) -> bool {
        if let Some(child) = &self.child {
            let mut child = child.lock().await;
            match child.try_wait() {
                Ok(None) => true,
                Ok(Some(_)) | Err(_) => false,
            }
        } else {
            false
        }
    }

    /// 获取子进程 PID
    pub async fn pid(&self) -> Option<u32> {
        if let Some(child) = &self.child {
            let child = child.lock().await;
            child.id()
        } else {
            None
        }
    }

    /// 终止子进程及其整棵进程树
    ///
    /// 治理缺口修复：原实现只 kill 直接子进程（sidecar 本体），bash 工具
    /// 拉起的孙进程会变孤儿。现在先整树杀（kill_process_tree），再对直接
    /// 子进程 kill 兜底。idle GC 卸载 / 崩溃清理 / 热重载 respawn 三条
    /// kill 路径均收敛到此方法。
    pub async fn kill(&mut self) -> Result<(), McpError> {
        if let Some(child) = &self.child {
            let mut child = child.lock().await;
            let pid = child.id();
            if let Some(pid) = pid {
                // 整树杀（bash 等孙进程一并清理，防孤儿）
                kill_process_tree(pid).await;
                // 树杀已含直接子进程；kill() 仅作兜底，失败不报错（避免噪音）
                let _ = child.kill().await;
            } else {
                child.kill().await.map_err(|e| McpError::Transport {
                    message: format!("kill error: {}", e),
                })?;
            }
        }
        self.child = None;
        self.stdin = None;
        self.stdout = None;
        self.stderr = None;
        *self.initialized.lock().await = false;
        Ok(())
    }
}

/// 杀整棵进程树（sidecar 的孙进程——bash 等——一并清理，防孤儿）。
///
/// 平台策略：
/// - Windows: `taskkill /PID <pid> /T /F`（递归枚举并强制终止整棵树）
/// - Unix: `kill(-pid, SIGKILL)` 进程组信号（spawn 时 process_group(0)
///   保证 sidecar 是进程组组长，子孙进程同组）
///
/// best-effort：失败不阻断调用方（随后 child.kill() 兜底直接子进程）。
async fn kill_process_tree(pid: u32) {
    #[cfg(windows)]
    {
        let _ = std::process::Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    }
    #[cfg(unix)]
    {
        // 负 pid → 进程组（组长被杀，组内全部子孙进程一并终止）
        unsafe {
            libc::kill(-(pid as i32), libc::SIGKILL);
        }
    }
}

/// 向 stdin 写入一行原始 JSON（不加协议包装）。
///
/// 用于回写 sidecar 反向调用的 response——response 必须用与 request 相同的 id。
async fn write_raw_line(
    stdin: &Arc<Mutex<tokio::process::ChildStdin>>,
    json_str: &str,
) -> Result<(), McpError> {
    let mut writer = stdin.lock().await;
    writer
        .write_all(json_str.as_bytes())
        .await
        .map_err(|e| McpError::Transport {
            message: format!("write error: {}", e),
        })?;
    writer.write_all(b"\n").await.map_err(|e| McpError::Transport {
        message: format!("write newline error: {}", e),
    })?;
    writer.flush().await.map_err(|e| McpError::Transport {
        message: format!("flush error: {}", e),
    })?;
    Ok(())
}

/// 处理 sidecar 主动发起的 JSON-RPC request。
///
/// method 形如 `pipeline-executor.resume`，拆分后路由到 CapabilityRouter；
/// 结果通过 stdin 回写为 JSON-RPC response（相同 id）。
async fn handle_incoming_request(
    method: &str,
    msg: &Value,
    id: &str,
    router: &Option<Arc<dyn CapabilityRouter>>,
    stdin: &Arc<Mutex<tokio::process::ChildStdin>>,
) {
    let params = msg.get("params").cloned().unwrap_or(Value::Null);

    // namespace 白名单从 router 动态获取（M2 注册表改造）。
    // router 为 None 时用编译期 STANDARD_CAPABILITIES 兜底（实际下面会因 router
    // 为 None 直接返回 no router configured，这里的解析仅决定是否 -32601）。
    let known_ns = router
        .as_ref()
        .map(|r| r.known_namespaces())
        .unwrap_or_default();
    // 非 capability method（如 MCP 标准方法）不处理
    let Some((capability, cap_method)) = parse_capability_method_with(method, &known_ns) else {
        let resp = serde_json::json!({
            "jsonrpc": "2.0",
            "id": id,
            "error": {"code": -32601, "message": format!("method not found: {method}")}
        });
        let _ = write_raw_line(stdin, &resp.to_string()).await;
        return;
    };

    let Some(router) = router else {
        let resp = serde_json::json!({
            "jsonrpc": "2.0",
            "id": id,
            "error": {"code": -32601, "message": "no capability router configured"}
        });
        let _ = write_raw_line(stdin, &resp.to_string()).await;
        return;
    };

    let result = router.handle(capability, cap_method, params).await;
    let resp = match result {
        Ok(value) => serde_json::json!({
            "jsonrpc": "2.0",
            "id": id,
            "result": value
        }),
        Err(e) => serde_json::json!({
            "jsonrpc": "2.0",
            "id": id,
            "error": {"code": -32603, "message": e.to_string()}
        }),
    };
    let _ = write_raw_line(stdin, &resp.to_string()).await;
}

/// 处理 sidecar 主动发起的 JSON-RPC notification（无 id，fire-and-forget）。
///
/// method 形如 `event-bus.emit`，拆分后路由到 CapabilityRouter。
/// 与 handle_incoming_request 的区别：不回 response（notification 协议无响应）。
/// 用于流式 chunk 高频推送（sidecar 边生成边推，内核推前端）。
async fn handle_incoming_notification(
    method: &str,
    params: Value,
    router: &Option<Arc<dyn CapabilityRouter>>,
) {
    tracing::debug!(target: "mcp:notification", method = %method, "收到 sidecar notification");
    let known_ns = router
        .as_ref()
        .map(|r| r.known_namespaces())
        .unwrap_or_default();
    let Some((capability, cap_method)) = parse_capability_method_with(method, &known_ns) else {
        tracing::debug!(target: "mcp:notification", method = %method, "notification 非 capability method，丢弃");
        return;
    };
    let Some(router) = router else {
        return;
    };
    // 调用 router.handle，忽略返回值（notification 不需要 response）。
    // event-bus.emit handler 内部负责把 chunk 推到前端。
    if let Err(e) = router.handle(capability, cap_method, params).await {
        tracing::warn!(
            target: "mcp:notification",
            capability, method = cap_method, error = %e,
            "capability notification 处理失败",
        );
    }
}

impl Drop for McpClient {
    fn drop(&mut self) {
        // kill_on_drop(true) 会在 Drop 时自动 kill 子进程
    }
}

/// 构造 initialize 握手时声明给 sidecar 的 capability 名单。
///
/// `router_present` 为 true 时声明全部标准能力（与
/// [`crate::capability::STANDARD_CAPABILITIES`] 对齐），sidecar SDK 据此为每个
/// capability 创建 [`crate::capability::CapabilityHandle`]；为 false 时声明空，
/// sidecar 反向调用会被内核拒收。
///
/// 抽成独立纯函数便于单测覆盖（避免依赖 `McpClient` 的进程 I/O）。
pub fn build_declared_capabilities(router_present: bool) -> Value {
    if router_present {
        build_declared_capabilities_from_namespaces(STANDARD_CAPABILITIES)
    } else {
        serde_json::json!({})
    }
}

/// 从动态 namespace 列表派生 initialize 声明。
///
/// 与 [`build_declared_capabilities`] 的区别：namespace 不再写死为
/// [`STANDARD_CAPABILITIES`]，而是由调用方传入（通常来自
/// [`crate::handler_registry::CapabilityHandlerRegistry::namespaces`] 或
/// [`crate::capability::CapabilityRouter::known_namespaces`]）。
///
/// 这让运行时注册的 namespace（如插件通过 manifest `provides.capabilities`
/// 注册的 `human-interaction`）自动出现在 initialize 声明里，sidecar SDK
/// 据此创建对应的 `CapabilityHandle`，无需改内核常量。
pub fn build_declared_capabilities_from_namespaces<T: AsRef<str>>(
    namespaces: &[T],
) -> Value {
    namespaces
        .iter()
        .map(|ns| (ns.as_ref().to_string(), Value::Object(serde_json::Map::new())))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_new_stdio_client() {
        let client =
            McpClient::new_stdio("python3", vec!["-m".to_string(), "mcp.server".to_string()]);
        assert!(matches!(client.transport, McpTransport::Stdio { .. }));
    }

    #[test]
    fn test_new_http_client() {
        let client = McpClient::new_http("http://localhost:8080/mcp");
        assert!(matches!(client.transport, McpTransport::Http { .. }));
    }

    #[tokio::test]
    async fn test_connect_nonexistent_command() {
        let mut client = McpClient::new_stdio("nonexistent_command_99999", vec![]);
        let result = client.connect().await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_connect_echo_server() {
        // 使用 `cat` 作为最简单的 stdio echo 服务器（回显 stdin）
        let mut client = McpClient::new_stdio("cat", vec![]);
        let result = client.connect().await;
        assert!(result.is_ok());
        assert!(client.is_alive().await);
        client.kill().await.unwrap();
    }

    #[tokio::test]
    async fn test_is_alive_not_connected() {
        let client = McpClient::new_stdio("cat", vec![]);
        assert!(!client.is_alive().await);
    }

    #[tokio::test]
    async fn test_kill_clears_state() {
        let mut client = McpClient::new_stdio("cat", vec![]);
        client.connect().await.unwrap();
        assert!(client.is_alive().await);

        client.kill().await.unwrap();
        assert!(!client.is_alive().await);
    }

    #[test]
    fn test_transport_variants() {
        let stdio = McpTransport::Stdio {
            command: "python3".to_string(),
            args: vec!["server.py".to_string()],
        };
        let http = McpTransport::Http {
            url: "http://localhost:8080".to_string(),
        };
        assert!(matches!(stdio, McpTransport::Stdio { .. }));
        assert!(matches!(http, McpTransport::Http { .. }));
    }

    #[tokio::test]
    async fn test_pid_after_connect() {
        let mut client = McpClient::new_stdio("cat", vec![]);
        client.connect().await.unwrap();
        let pid = client.pid().await;
        assert!(pid.is_some());
        assert!(pid.unwrap() > 0);
        client.kill().await.unwrap();
    }

    #[test]
    fn test_declared_capabilities_when_router_present() {
        // router 存在时，声明全部标准能力（含 service-registry / tool-executor）。
        let caps = build_declared_capabilities(true);
        for ns in [
            "pipeline-executor",
            "config-reader",
            "tenant-context",
            "event-bus",
            "logger",
            "metrics",
            "tool-executor",
            "service-registry",
        ] {
            assert!(
                caps.get(ns).is_some(),
                "build_declared_capabilities(true) 缺少 {ns}——sidecar 拿不到该 capability 句柄"
            );
        }
    }

    #[test]
    fn test_declared_capabilities_when_no_router() {
        // 无 router 时声明空，sidecar 反调会被拒。
        let caps = build_declared_capabilities(false);
        assert!(caps.as_object().map(|o| o.is_empty()).unwrap_or(true));
    }

    #[test]
    fn test_declared_capabilities_from_dynamic_namespaces() {
        // 从 router 提供的动态 namespace 列表派生声明。
        // 这是 M2 的收口：initialize 声明与白名单统一到注册表单一来源。
        let namespaces = vec![
            "pipeline-executor".to_string(),
            "human-interaction".to_string(), // 注册表里动态加的，不在 STANDARD_CAPABILITIES
        ];
        let caps = build_declared_capabilities_from_namespaces(&namespaces);
        assert!(caps.get("pipeline-executor").is_some());
        assert!(
            caps.get("human-interaction").is_some(),
            "动态注册的 namespace 必须出现在 initialize 声明里"
        );
        assert_eq!(
            caps.as_object().map(|o| o.len()).unwrap_or(0),
            2,
            "声明数量应等于 namespace 数量"
        );
    }
}
