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

use agentos_core::traits::AuthType;

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
    /// HTTP transport: 通过 HTTP POST 传 JSON-RPC（连远程第三方 MCP server）
    Http {
        url: String,
        /// 额外请求头（auth 在 connect 时解析 env 后并入 reqwest 默认头）。
        headers: HashMap<String, String>,
        /// 鉴权配置（value 含 ${ENV_VAR} 占位，connect 时解析）。
        auth: Option<agentos_core::traits::EndpointAuth>,
    },
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
    /// 300s 须覆盖 sidecar 端 LLM 调用的 first_token_timeout（llm.yaml 默认
    /// 120s）并留足余量——否则 reasoning model 首 token 接近 120s 时内核先
    /// 超时掐断，sidecar 的最终响应永远到不了内核（pending 已被移除）。
    /// 调用方可用 [`McpClient::with_request_timeout`] 覆盖。
    request_timeout: Duration,
    /// HTTP transport 的 reqwest 客户端（connect 时构建，含解析后的 auth 默认头）。
    /// stdio 模式为 None。
    http_client: Option<reqwest::Client>,
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
            http_client: None,
        }
    }

    /// 创建 HTTP transport 客户端
    ///
    /// `headers`：额外请求头（如 `X-Also-Search`）。`auth`：鉴权配置，其 `value`
    /// 可含 `${ENV_VAR}` 占位，在 [`connect`](McpClient::connect) 时解析（查找顺序：
    /// 进程环境 → `.env` overlay；两处均缺失则连接失败早暴露，但
    /// `auth.required == Some(false)` 时跳过该鉴权头照常连接，由服务端 401 说话）。
    /// reqwest 客户端在 connect 时构建，把解析后的 auth 并入默认头。
    pub fn new_http(
        url: impl Into<String>,
        headers: HashMap<String, String>,
        auth: Option<agentos_core::traits::EndpointAuth>,
    ) -> Self {
        Self {
            transport: McpTransport::Http {
                url: url.into(),
                headers,
                auth,
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
            http_client: None,
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
                // Windows：npx/npm 等是 .cmd 批处理，CreateProcess 只找 .exe——
                // 无扩展名命令先按 PATHEXT 探测解析成全路径，否则 spawn 报
                // "program not found"（design_generate/browser_test 等 MCP 起不来）。
                #[cfg(windows)]
                let resolved = resolve_windows_command(command);
                #[cfg(not(windows))]
                let resolved = command.clone();
                let mut cmd = Command::new(&resolved);
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

                // .env 增量叠加：用户在设置页填写的 API Key 写入 .env 后，
                // 内核进程环境仍是启动时的旧值——spawn 时把 .env 的增量
                // （新变量 + .env 自身的更新）直接叠加给子进程，sidecar
                // 无需重启内核即可拿到新 key（见 env_file 模块说明）。
                for (k, v) in crate::env_file::env_delta_overlay() {
                    cmd.env(k, v);
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
            McpTransport::Http { url, headers, auth } => {
                // HTTP 模式：不 spawn 子进程。构建 reqwest 客户端（解析 ${ENV_VAR}
                // 鉴权占位 → 并入默认头），存入 self.http_client 供 send_request 复用。
                let client = self.build_http_client(url, headers, auth)?;
                self.http_client = Some(client);
                Ok(())
            }
        }
    }

    /// 启动 stdout 读取循环
    ///
    /// 区分三类消息：
    /// - response（对内核之前请求的响应）：有 id 且在 pending 表中 → resolve oneshot
    /// - incoming request（sidecar 主动反向调用）：有 method 且有 id → 路由到 router
    /// - notification（sidecar 单向通知）：有 method 无 id → 记录后交给 router（异步）
    ///
    /// id 类型：JSON-RPC 2.0 允许 string / number。内核自身出站请求用 uuid 字符串，
    /// 但 sidecar 反向请求的 id 由对端 SDK 生成——官方 Python SDK（mcp v2）用自增
    /// 整数 id。反向调用分支必须同时接受两种 id，响应按原始 id 值回显（JSON-RPC
    /// 要求 response.id === request.id，含类型）。
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
                            // sidecar stdout 关闭（进程退出/崩溃）：记 warn 让
                            // 崩溃在日志里可见，进行中的 send_request 不静默等满超时。
                            tracing::warn!("[mcp] sidecar stdout EOF（进程退出）");
                            // EOF 即清空 pending（drop 所有 oneshot sender）→ rx 端
                            // 立即收到 channel closed → send_request 快速失败返回
                            // 错误，不阻塞调用方至超时。
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
                                        if sender.send(response).is_err() {
                                            tracing::warn!(
                                                "[mcp] pending receiver dropped; JSON-RPC response id={id} not delivered"
                                            );
                                        }
                                    }
                                    continue;
                                }
                            }

                            // 分支2：sidecar 主动发起的 request（有 method + id）
                            // id 可为 string 或 number（官方 Python SDK v2 反向请求用
                            // 整数 id）——仅认字符串会漏到分支3被当 notification 处理，
                            // router 照常被调但不回响应，sidecar 反向调用必超时。
                            if let (Some(method), Some(id)) = (
                                msg.get("method").and_then(|v| v.as_str()),
                                msg.get("id").filter(|v| !v.is_null()),
                            ) {
                                let raw_id = id.clone();
                                // 日志/错误消息用规范化字符串形式（数值 id 转十进制）
                                let id_repr = match id {
                                    Value::String(s) => s.clone(),
                                    other => other.to_string(),
                                };
                                handle_incoming_request(
                                    method, &msg, &raw_id, &id_repr, &router, &stdin,
                                )
                                .await;
                                continue;
                            }
                            // 分支3：sidecar 主动发起的 notification（有 method 无 id）
                            // 用于流式 chunk 推送：fire-and-forget，内核不回 response。
                            if let Some(method) =
                                msg.get("method").and_then(|v| v.as_str()).map(String::from)
                            {
                                let params = msg.get("params").cloned().unwrap_or(Value::Null);
                                // 关键：notification 处理用 spawn 并发，不阻塞 reader loop！
                                // handle_incoming_notification 内部会 await session.emit_event
                                // （WS 发送），若串行 await 会让后续 notification 攒批排队，
                                // 破坏流式 chunk 的实时推送。
                                // notification 是 fire-and-forget，无需等结果，spawn 后继续读下一行。
                                let router_clone = router.clone();
                                tokio::spawn(async move {
                                    handle_incoming_notification(&method, params, &router_clone)
                                        .await;
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
    /// 非 UTF-8 容错：stderr 按原始字节读行（`read_until(b'\n')`）+
    /// `String::from_utf8_lossy` 解码（非法字节替换为 U+FFFD）。Windows 宿主上
    /// Python sidecar 的 stderr（如含 GBK 中文路径的 traceback）可能含非法
    /// UTF-8 字节——严格 UTF-8 解码会返回 `InvalidData` 中断消费循环，sidecar
    /// 继续写 stderr 填满管道缓冲（~64KB）后 `write` 阻塞、进程卡死、无法响应
    /// MCP 请求。按字节读行 + lossy 解码保证读取永不因编码中断，杜绝管道反压。
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

    /// 构建 HTTP reqwest 客户端：解析 `${ENV_VAR}` 鉴权占位、合并额外头、设超时。
    fn build_http_client(
        &self,
        url: &str,
        headers: &HashMap<String, String>,
        auth: &Option<agentos_core::traits::EndpointAuth>,
    ) -> Result<reqwest::Client, McpError> {
        use reqwest::header::{HeaderMap, HeaderName, HeaderValue};

        // 先校验 URL 合法（早暴露配置错误）。
        reqwest::Url::parse(url).map_err(|e| McpError::ConnectionFailed {
            message: format!("invalid MCP http url {}: {}", url, e),
        })?;

        let mut header_map = HeaderMap::new();
        for (k, v) in headers {
            match (
                HeaderName::try_from(k.as_str()),
                HeaderValue::try_from(v.as_str()),
            ) {
                (Ok(name), Ok(val)) => {
                    header_map.append(name, val);
                }
                _ => {
                    tracing::warn!("[mcp] HTTP 端点非法 header 跳过 | {}={}", k, v);
                }
            }
        }
        // 鉴权头（${ENV_VAR} 解析，查找顺序：进程环境 → .env overlay）。
        // 引用未设置变量时：auth.required 缺省/true → 报错早暴露（不静默放行）；
        // 显式 false（可选凭据，如 langchain_hub 的 LANGSMITH_API_KEY）→
        // 跳过该鉴权头照常连接，由服务端 401 说话（GAP-4b）。
        if let Some(a) = auth {
            if a.auth_type != AuthType::None {
                let optional_auth = a.required == Some(false);
                let resolved = match resolve_env_placeholders(&a.value) {
                    Ok(v) => Some(v),
                    Err(e) if optional_auth => {
                        tracing::info!(
                            "[mcp] HTTP 端点鉴权为可选（required=false）且变量未配置，跳过鉴权头 | {}",
                            e
                        );
                        None
                    }
                    Err(e) => return Err(e),
                };
                if let Some(resolved) = resolved {
                    let (name, val) = match a.auth_type {
                        AuthType::Bearer => (
                            reqwest::header::AUTHORIZATION,
                            format!("Bearer {}", resolved),
                        ),
                        AuthType::ApiKey => {
                            let name =
                                HeaderName::try_from(a.header_name.as_str()).map_err(|e| {
                                    McpError::ConnectionFailed {
                                        message: format!(
                                            "invalid auth header_name {}: {}",
                                            a.header_name, e
                                        ),
                                    }
                                })?;
                            (name, resolved)
                        }
                        AuthType::None => unreachable!(),
                    };
                    let val =
                        HeaderValue::from_str(&val).map_err(|e| McpError::ConnectionFailed {
                            message: format!("invalid auth header value: {}", e),
                        })?;
                    header_map.append(name, val);
                }
            }
        }

        reqwest::Client::builder()
            .default_headers(header_map)
            .timeout(self.request_timeout)
            .build()
            .map_err(|e| McpError::ConnectionFailed {
                message: format!("build http client: {}", e),
            })
    }

    /// HTTP transport：POST 一个 JSON-RPC 请求，解析 plain JSON 响应。
    ///
    /// 绕开 stdio 的 pending/reader-loop（外部第三方 MCP server 不会反向调用内核
    /// capability，无需异步配对通道）。SSE 流式响应（`text/event-stream`）暂不支持，
    /// 命中时返回清晰错误（plain JSON 覆盖标准非流式调用，流式留作后续）。
    async fn http_post(&self, url: &str, request: &JsonRpcRequest) -> Result<Value, McpError> {
        let client = self
            .http_client
            .as_ref()
            .ok_or_else(|| McpError::ConnectionFailed {
                message: "http client not built (connect not called)".to_string(),
            })?;
        let resp =
            client
                .post(url)
                .json(request)
                .send()
                .await
                .map_err(|e| McpError::Transport {
                    message: format!("http post: {}", e),
                })?;
        let status = resp.status();
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            let snippet: String = body.chars().take(200).collect();
            return Err(McpError::Protocol {
                message: format!("http {} {}: {}", status.as_u16(), url, snippet),
            });
        }
        // SSE 流式暂不支持：探测 content-type，给出清晰错误而非解析失败。
        if let Some(ct) = resp.headers().get(reqwest::header::CONTENT_TYPE) {
            if ct
                .as_bytes()
                .windows(b"text/event-stream".len())
                .any(|w| w.eq_ignore_ascii_case(b"text/event-stream"))
            {
                return Err(McpError::Protocol {
                    message: "SSE 流式 HTTP 响应暂不支持（plain JSON only）".to_string(),
                });
            }
        }
        let response: JsonRpcResponse = resp.json().await.map_err(|e| McpError::Protocol {
            message: format!("parse http json-rpc response: {}", e),
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

        // HTTP transport：直接 POST，绕开 stdio 的 pending/reader-loop 配对机制。
        if let McpTransport::Http { url, .. } = &self.transport {
            return self.http_post(url, &request).await;
        }

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
        // 300s 须覆盖 sidecar 端 first_token_timeout（llm.yaml 默认 120s）并留足
        // 余量——否则 reasoning model 首 token 接近上限时内核先掐断，sidecar 的
        // 最终响应永远到不了内核。可用 with_request_timeout 按插件覆盖。）
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
    #[deprecated(
        since = "0.2.1",
        note = "use initialize(&config) to pass plugin config"
    )]
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
        } else if let McpTransport::Http { url, .. } = &self.transport {
            // HTTP transport：fire-and-forget POST notification（忽略响应体）。
            let client = self
                .http_client
                .as_ref()
                .ok_or_else(|| McpError::ConnectionFailed {
                    message: "http client not built (connect not called)".to_string(),
                })?;
            let _ = client
                .post(url)
                .header(reqwest::header::CONTENT_TYPE, "application/json")
                .body(notif_str)
                .send()
                .await
                .map_err(|e| McpError::Transport {
                    message: format!("http notification: {}", e),
                })?;
        } else {
            return Err(McpError::ConnectionFailed {
                message: "not connected (no stdin, not http)".to_string(),
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
    /// 只 kill 直接子进程（sidecar 本体）会让 bash 工具拉起的孙进程变孤儿；
    /// 先整树杀（kill_process_tree），再对直接子进程 kill 兜底。idle GC 卸载 /
    /// 崩溃清理 / 热重载 respawn 三条 kill 路径均收敛到此方法。
    pub async fn kill(&mut self) -> Result<(), McpError> {
        if let Some(child) = &self.child {
            let mut child = child.lock().await;
            let pid = child.id();
            if let Some(pid) = pid {
                // 整树杀（bash 等孙进程一并清理，防孤儿）
                kill_process_tree(pid).await;
                // 树杀已含直接子进程；kill() 仅作兜底，失败不报错（避免噪音）
                if let Err(e) = child.kill().await {
                    tracing::debug!(
                        "[mcp] best-effort child.kill() fallback failed (tree-kill likely already terminated it): {e}"
                    );
                }
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
        // 负 pid → 进程组：组长被杀，组内全部子孙进程一并终止（POSIX 语义）。
        // SAFETY: libc::kill 是 POSIX FFI，不解引用指针、不涉及内存不安全；参数合法——
        // pid 为操作系统进程号（远小于 i32::MAX，`pid as i32` 不会截断），取负值表示进程组，
        // SIGKILL 为合法信号常量。
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
    writer
        .write_all(b"\n")
        .await
        .map_err(|e| McpError::Transport {
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
/// 结果通过 stdin 回写为 JSON-RPC response。`raw_id` 为请求的原始 id 值
/// （string 或 number，按 JSON-RPC 2.0 要求原样回显），`id_repr` 为其字符串
/// 形式（仅用于日志）。
async fn handle_incoming_request(
    method: &str,
    msg: &Value,
    raw_id: &Value,
    id_repr: &str,
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
            "id": raw_id,
            "error": {"code": -32601, "message": format!("method not found: {method}")}
        });
        if let Err(e) = write_raw_line(stdin, &resp.to_string()).await {
            tracing::warn!(
                "[mcp] failed to write -32601 (method not found) response for id={id_repr}: {e}"
            );
        }
        return;
    };

    let Some(router) = router else {
        let resp = serde_json::json!({
            "jsonrpc": "2.0",
            "id": raw_id,
            "error": {"code": -32601, "message": "no capability router configured"}
        });
        if let Err(e) = write_raw_line(stdin, &resp.to_string()).await {
            tracing::warn!(
                "[mcp] failed to write -32601 (no router) response for id={id_repr}: {e}"
            );
        }
        return;
    };

    let result = router.handle(capability, cap_method, params).await;
    let resp = match result {
        Ok(value) => serde_json::json!({
            "jsonrpc": "2.0",
            "id": raw_id,
            "result": value
        }),
        Err(e) => serde_json::json!({
            "jsonrpc": "2.0",
            "id": raw_id,
            "error": {"code": -32603, "message": e.to_string()}
        }),
    };
    if let Err(e) = write_raw_line(stdin, &resp.to_string()).await {
        tracing::warn!("[mcp] failed to write JSON-RPC response for id={id_repr}: {e}");
    }
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
pub fn build_declared_capabilities_from_namespaces<T: AsRef<str>>(namespaces: &[T]) -> Value {
    namespaces
        .iter()
        .map(|ns| {
            (
                ns.as_ref().to_string(),
                Value::Object(serde_json::Map::new()),
            )
        })
        .collect()
}

/// Windows 下解析无扩展名命令为可执行全路径（PATHEXT 语义）。
///
/// npm 生态的 `npx`/`npm`/`pnpm` 在 Windows 上是 `.cmd` 批处理，而
/// `Command::new("npx")` 走 CreateProcess 只找 `.exe`，直接报 program not found。
/// 按 `PATHEXT`（缺省 `.COM;.EXE;.BAT;.CMD`）逐个 PATH 目录探测，命中即返回
/// 全路径（`.bat`/`.cmd` 由 Rust 标准库自动经 cmd.exe 启动并转义参数）。
/// 已带扩展名/路径分隔符的命令原样返回；探测不到也原样返回（让 spawn 报
/// 真实错误，不吞掉病因）。
#[cfg(windows)]
fn resolve_windows_command(command: &str) -> String {
    if command.contains('.') || command.contains('\\') || command.contains('/') {
        return command.to_string();
    }
    let pathext = std::env::var("PATHEXT").unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".to_string());
    let paths = std::env::var("PATH").unwrap_or_default();
    for dir in paths.split(';').filter(|s| !s.is_empty()) {
        for ext in pathext.split(';').filter(|s| !s.is_empty()) {
            let candidate = std::path::Path::new(dir).join(format!("{command}{ext}"));
            if candidate.is_file() {
                return candidate.to_string_lossy().into_owned();
            }
        }
    }
    command.to_string()
}

/// 把字符串里的 `${ENV_VAR}` 占位替换为环境变量值（含 `.env` overlay 回退）。
///
/// 查找顺序（GAP-4a，与 stdio sidecar 的 spawn 叠加同语义）：**进程环境 →
/// 项目 `.env` overlay**（[`crate::env_file::env_delta_overlay`] 的增量——只补
/// 进程环境缺失的变量，绝不用 `.env` 覆盖系统显式设置的环境变量）。用户经
/// 设置页把 key 写入 `.env` 后，HTTP MCP connect 时即可解析，无需重启内核
/// （配合 invoker 的 `.env` mtime 指纹触发客户端重建，改动下次调用即生效）。
///
/// 引用的变量两处均未设置时报错（早暴露，不静默放行）——外部 MCP 端点的鉴权值
/// 缺失通常意味着配置未就绪，连出去也会被 401 拒绝，不如在 connect 时直接失败。
/// 可选凭据（`auth.required == false`）由 [`McpClient`] 的 `build_http_client`
/// 捕获该错误后跳过鉴权头，本函数不感知 required 语义。
///
/// 默认值语法（shell 标准，用于可选变量——如 omnisearch 的限流 key，缺失只降级
/// 不阻断）：`${VAR:-default}`（未设置**或为空**用 default）、`${VAR-default}`
/// （仅未设置用 default）。无默认值的未设置变量仍走早失败路径。
pub fn resolve_env_placeholders(raw: &str) -> Result<String, McpError> {
    // 每次 resolve 构造一次 overlay 查找表（一次 .env 文件读）。resolve 只在
    // 客户端 connect / stdio env 构造时调用（低频），无需常驻缓存；且
    // env_delta_overlay 每次现读文件正是「写完即生效」语义的一部分。
    let overlay: HashMap<String, String> =
        crate::env_file::env_delta_overlay().into_iter().collect();
    resolve_env_placeholders_with(raw, &overlay)
}

/// [`resolve_env_placeholders`] 的可注入版本：overlay 作为参数传入，便于单测
/// mock .env 增量（不必真实写文件/改 AGENTOS_CONFIG_ROOT）。
fn resolve_env_placeholders_with(
    raw: &str,
    overlay: &HashMap<String, String>,
) -> Result<String, McpError> {
    let mut out = String::with_capacity(raw.len());
    let mut rest = raw;
    loop {
        match rest.find("${") {
            None => {
                out.push_str(rest);
                break;
            }
            Some(start) => {
                out.push_str(&rest[..start]);
                let after = &rest[start + 2..];
                match after.find('}') {
                    None => {
                        // 未闭合占位——按原样保留，不报错。
                        out.push_str(&rest[start..]);
                        break;
                    }
                    Some(end) => {
                        let spec = &after[..end];
                        // shell 标准默认值语法："${VAR:-def}"（未设置或空用 def）、
                        // "${VAR-def}"（仅未设置用 def）；环境变量名不含 '-'，可安全分割。
                        let (var, default): (&str, Option<&str>) =
                            if let Some(pos) = spec.find(":-") {
                                (&spec[..pos], Some(&spec[pos + 2..]))
                            } else if let Some(pos) = spec.find('-') {
                                (&spec[..pos], Some(&spec[pos + 1..]))
                            } else {
                                (spec, None)
                            };
                        let val = match default {
                            // ":-" 与 "-" 的差别只在空串处理：spec 含 ":-" 时空串也取默认。
                            Some(d) => {
                                let empty_means_default = spec.contains(":-");
                                match lookup_env_var(var, overlay) {
                                    Some(v) if !(empty_means_default && v.is_empty()) => v,
                                    _ => d.to_string(),
                                }
                            }
                            None => lookup_env_var(var, overlay).ok_or_else(|| {
                                McpError::ConnectionFailed {
                                    message: format!(
                                        "HTTP MCP 端点引用了未设置的环境变量（进程环境与 .env 均未提供）: ${{{var}}}"
                                    ),
                                }
                            })?,
                        };
                        out.push_str(&val);
                        rest = &after[end + 1..];
                    }
                }
            }
        }
    }
    Ok(out)
}

/// 单变量查找：进程环境优先，缺失时回退 `.env` overlay（增量）。
///
/// overlay 来自 [`crate::env_file::env_delta_overlay`]——其本身已按「系统环境
/// 变量 > .env」过滤（只含进程环境缺失的变量），此处再显式按 进程环境 →
/// overlay 的顺序查找，与 stdio sidecar 继承的子进程环境同语义。
fn lookup_env_var(var: &str, overlay: &HashMap<String, String>) -> Option<String> {
    std::env::var(var)
        .ok()
        .or_else(|| overlay.get(var).cloned())
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
        let client = McpClient::new_http("http://localhost:8080/mcp", HashMap::new(), None);
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
            headers: HashMap::new(),
            auth: None,
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
        // logger / config-reader 已作为死能力删除（W-A8+M），不再声明。
        let caps = build_declared_capabilities(true);
        for ns in [
            "pipeline-executor",
            "tenant-context",
            "event-bus",
            "metrics",
            "tool-executor",
            "service-registry",
            "frontend",
        ] {
            assert!(
                caps.get(ns).is_some(),
                "build_declared_capabilities(true) 缺少 {ns}——sidecar 拿不到该 capability 句柄"
            );
        }
        for dead in ["logger", "config-reader"] {
            assert!(
                caps.get(dead).is_none(),
                "死能力 {dead} 不应再声明给 sidecar"
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

    // ── HTTP transport 测试 ──────────────────────────────────────────

    /// 启动一个 mock HTTP MCP server：回显收到的 id + 固定 result，并把原始请求
    /// （headers+body）存入共享 state 供断言。返回 (url, captured_raw_request)。
    async fn spawn_mock_mcp_server(
        result: Value,
    ) -> (String, Arc<std::sync::Mutex<Option<Vec<u8>>>>) {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let captured: Arc<std::sync::Mutex<Option<Vec<u8>>>> =
            Arc::new(std::sync::Mutex::new(None));
        let cap2 = captured.clone();
        tokio::spawn(async move {
            let (mut sock, _) = listener.accept().await.unwrap();
            let mut data = Vec::new();
            let mut tmp = [0u8; 4096];
            // 读完 headers + Content-Length 指定的 body
            loop {
                let n = sock.read(&mut tmp).await.unwrap();
                if n == 0 {
                    break;
                }
                data.extend_from_slice(&tmp[..n]);
                if let Some(hdr_end) = subseq(&data, b"\r\n\r\n") {
                    let cl = extract_content_length(&data[..hdr_end]);
                    let body_start = hdr_end + 4;
                    if data.len() >= body_start + cl {
                        break;
                    }
                }
            }
            *cap2.lock().unwrap() = Some(data.clone());
            // 解析 body 取 id 回显
            let body_start = subseq(&data, b"\r\n\r\n")
                .map(|p| p + 4)
                .unwrap_or(data.len());
            let body_str = String::from_utf8_lossy(&data[body_start..]);
            let id = serde_json::from_str::<Value>(&body_str)
                .ok()
                .and_then(|v| v.get("id").cloned())
                .unwrap_or(Value::Null);
            let resp = serde_json::json!({
                "jsonrpc": "2.0", "id": id, "result": result
            })
            .to_string();
            let out = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                resp.len(),
                resp
            );
            let _ = sock.write_all(out.as_bytes()).await;
        });
        (url, captured)
    }

    fn subseq(hay: &[u8], needle: &[u8]) -> Option<usize> {
        hay.windows(needle.len()).position(|w| w == needle)
    }

    fn extract_content_length(headers: &[u8]) -> usize {
        let s = String::from_utf8_lossy(headers).to_ascii_lowercase();
        for line in s.split("\r\n") {
            if let Some(rest) = line.strip_prefix("content-length:") {
                return rest.trim().parse().unwrap_or(0);
            }
        }
        0
    }

    #[tokio::test]
    async fn test_http_send_request_roundtrip() {
        let expected = serde_json::json!({
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "mock", "version": "1.0"}
        });
        let (url, captured) = spawn_mock_mcp_server(expected.clone()).await;

        // bearer 鉴权 + env 占位 + 额外头
        std::env::set_var("MCP_TEST_KEY", "secret-token-xyz");
        let auth = agentos_core::traits::EndpointAuth {
            auth_type: AuthType::Bearer,
            header_name: "Authorization".to_string(),
            value: "${MCP_TEST_KEY}".to_string(),
            required: None,
        };
        let mut headers = HashMap::new();
        headers.insert("X-Also-Search".to_string(), "smithery.ai".to_string());

        let mut client = McpClient::new_http(url, headers, Some(auth));
        client.connect().await.unwrap();
        let result = client.send_request("initialize", None).await.unwrap();
        assert_eq!(result, expected);

        // 校验收到的请求带上了 auth 头和额外头
        let raw = captured.lock().unwrap().clone().unwrap();
        let raw_s = String::from_utf8_lossy(&raw).to_ascii_lowercase();
        assert!(
            raw_s.contains("authorization: bearer secret-token-xyz"),
            "auth header missing: {raw_s}"
        );
        assert!(
            raw_s.contains("x-also-search: smithery.ai"),
            "extra header missing: {raw_s}"
        );
        std::env::remove_var("MCP_TEST_KEY");
    }

    #[tokio::test]
    async fn test_http_connect_fails_on_missing_env() {
        // 引用未设置的环境变量 → connect 早失败（不静默放行）
        std::env::remove_var("MCP_TEST_KEY_DEFINITELY_UNSET");
        let auth = agentos_core::traits::EndpointAuth {
            auth_type: AuthType::ApiKey,
            header_name: "Authorization".to_string(),
            value: "${MCP_TEST_KEY_DEFINITELY_UNSET}".to_string(),
            required: None,
        };
        let mut client = McpClient::new_http("http://127.0.0.1:1", HashMap::new(), Some(auth));
        let res = client.connect().await;
        assert!(res.is_err(), "connect should fail on missing env var");
    }

    // ── GAP-4b：尊重 auth.required == false ────────────────────────────

    #[tokio::test]
    async fn test_http_optional_auth_missing_env_skips_header_and_connects() {
        // required=false + 占位变量缺失 → connect 成功、请求不带该鉴权头
        // （照常连接，由服务端 401 说话——langchain_hub 场景）
        let expected = serde_json::json!({"ok": true});
        let (url, captured) = spawn_mock_mcp_server(expected.clone()).await;

        std::env::remove_var("MCP_OPT_AUTH_KEY_DEFINITELY_UNSET");
        let auth = agentos_core::traits::EndpointAuth {
            auth_type: AuthType::ApiKey,
            header_name: "x-api-key".to_string(),
            value: "${MCP_OPT_AUTH_KEY_DEFINITELY_UNSET}".to_string(),
            required: Some(false),
        };
        let mut client = McpClient::new_http(url, HashMap::new(), Some(auth));
        client
            .connect()
            .await
            .expect("required=false 时缺变量不应 connect 失败");
        let result = client.send_request("tools/list", None).await.unwrap();
        assert_eq!(result, expected);

        // 断言实际发出的请求头集合：鉴权头必须缺席
        let raw = captured.lock().unwrap().clone().unwrap();
        let raw_s = String::from_utf8_lossy(&raw).to_ascii_lowercase();
        assert!(
            !raw_s.contains("x-api-key"),
            "可选鉴权未配置时不应携带 x-api-key 头: {raw_s}"
        );
        assert!(
            !raw_s.contains("authorization:"),
            "可选鉴权未配置时不应携带 authorization 头: {raw_s}"
        );
    }

    #[tokio::test]
    async fn test_http_optional_auth_with_env_still_sends_header() {
        // required=false 但变量已配置 → 鉴权头照常发送（可选 ≠ 永不发送）
        let expected = serde_json::json!({"ok": true});
        let (url, captured) = spawn_mock_mcp_server(expected.clone()).await;

        std::env::set_var("MCP_OPT_AUTH_KEY_SET", "secret-abc");
        let auth = agentos_core::traits::EndpointAuth {
            auth_type: AuthType::ApiKey,
            header_name: "x-api-key".to_string(),
            value: "${MCP_OPT_AUTH_KEY_SET}".to_string(),
            required: Some(false),
        };
        let mut client = McpClient::new_http(url, HashMap::new(), Some(auth));
        client.connect().await.unwrap();
        client.send_request("tools/list", None).await.unwrap();

        let raw = captured.lock().unwrap().clone().unwrap();
        let raw_s = String::from_utf8_lossy(&raw).to_ascii_lowercase();
        assert!(
            raw_s.contains("x-api-key: secret-abc"),
            "可选鉴权已配置时仍应发送鉴权头: {raw_s}"
        );
        std::env::remove_var("MCP_OPT_AUTH_KEY_SET");
    }

    #[tokio::test]
    async fn test_http_connect_fails_on_missing_env_required_explicit_true() {
        // required=true（显式）+ 变量缺失 → 保持既有硬失败（与缺省 None 同语义）
        std::env::remove_var("MCP_REQ_AUTH_KEY_DEFINITELY_UNSET");
        let auth = agentos_core::traits::EndpointAuth {
            auth_type: AuthType::Bearer,
            header_name: "Authorization".to_string(),
            value: "${MCP_REQ_AUTH_KEY_DEFINITELY_UNSET}".to_string(),
            required: Some(true),
        };
        let mut client = McpClient::new_http("http://127.0.0.1:1", HashMap::new(), Some(auth));
        let res = client.connect().await;
        assert!(res.is_err(), "required=true 时缺变量应 connect 失败");
    }

    #[test]
    fn test_resolve_env_placeholders() {
        std::env::set_var("MCP_PH_TEST", "value123");
        assert_eq!(
            resolve_env_placeholders("Bearer ${MCP_PH_TEST}").unwrap(),
            "Bearer value123"
        );
        // 多占位 + 原文混排
        assert_eq!(
            resolve_env_placeholders("a${MCP_PH_TEST}b${MCP_PH_TEST}c").unwrap(),
            "avalue123bvalue123c"
        );
        // 无占位原样返回
        assert_eq!(resolve_env_placeholders("plain").unwrap(), "plain");
        std::env::remove_var("MCP_PH_TEST");
        // 未设置变量 → 报错
        assert!(resolve_env_placeholders("${MCP_PH_TEST_DEFINITELY_UNSET}").is_err());
    }

    #[test]
    fn test_resolve_env_placeholders_default_syntax() {
        // ${VAR:-def}：未设置 → 用默认值
        std::env::remove_var("MCP_PH_OPT_UNSET");
        assert_eq!(
            resolve_env_placeholders("${MCP_PH_OPT_UNSET:-}").unwrap(),
            ""
        );
        assert_eq!(
            resolve_env_placeholders("k=${MCP_PH_OPT_UNSET:-fallback}").unwrap(),
            "k=fallback"
        );
        // ${VAR:-def}：已设置（非空）→ 用环境值
        std::env::set_var("MCP_PH_OPT_SET", "real");
        assert_eq!(
            resolve_env_placeholders("${MCP_PH_OPT_SET:-fallback}").unwrap(),
            "real"
        );
        // ${VAR:-def}：已设置但为空串 → 用默认值（shell ":-" 语义）
        std::env::set_var("MCP_PH_OPT_EMPTY", "");
        assert_eq!(
            resolve_env_placeholders("${MCP_PH_OPT_EMPTY:-fallback}").unwrap(),
            "fallback"
        );
        // ${VAR-def}：仅未设置才用默认；空串保留空串（shell "-" 语义）
        assert_eq!(
            resolve_env_placeholders("${MCP_PH_OPT_EMPTY-fallback}").unwrap(),
            ""
        );
        assert_eq!(
            resolve_env_placeholders("${MCP_PH_OPT_UNSET-fallback}").unwrap(),
            "fallback"
        );
        std::env::remove_var("MCP_PH_OPT_SET");
        std::env::remove_var("MCP_PH_OPT_EMPTY");
    }

    // ── GAP-4a：resolve_env_placeholders 的 .env overlay 回退 ──────────

    #[test]
    fn test_resolve_placeholders_overlay_fallback_when_env_missing() {
        // 进程环境未设置、overlay（.env 增量）有值 → 解析成功（mock overlay）
        std::env::remove_var("MCP_PH_OVERLAY_ONLY");
        let overlay =
            HashMap::from([("MCP_PH_OVERLAY_ONLY".to_string(), "from_dotenv".to_string())]);
        assert_eq!(
            resolve_env_placeholders_with("Bearer ${MCP_PH_OVERLAY_ONLY}", &overlay).unwrap(),
            "Bearer from_dotenv"
        );
        // 性质断言：多处占位混排时同规则成立
        assert_eq!(
            resolve_env_placeholders_with("a${MCP_PH_OVERLAY_ONLY}b", &overlay).unwrap(),
            "afrom_dotenvb"
        );
    }

    #[test]
    fn test_resolve_placeholders_process_env_priority_over_overlay() {
        // 两者都有 → 进程环境优先（mock overlay）
        std::env::set_var("MCP_PH_BOTH", "from_process");
        let overlay = HashMap::from([("MCP_PH_BOTH".to_string(), "from_dotenv".to_string())]);
        assert_eq!(
            resolve_env_placeholders_with("${MCP_PH_BOTH}", &overlay).unwrap(),
            "from_process"
        );
        // 性质断言（优先级可逆）：进程环境删除后，同输入改取 overlay 值
        std::env::remove_var("MCP_PH_BOTH");
        assert_eq!(
            resolve_env_placeholders_with("${MCP_PH_BOTH}", &overlay).unwrap(),
            "from_dotenv"
        );
    }

    #[test]
    fn test_resolve_placeholders_overlay_respects_default_syntax() {
        // ${VAR:-def}：进程环境与 overlay 均缺失 → 默认值；overlay 有值 →
        // overlay 值优先于默认值（查找顺序在默认值之前）
        std::env::remove_var("MCP_PH_DEF_VAR");
        let empty: HashMap<String, String> = HashMap::new();
        assert_eq!(
            resolve_env_placeholders_with("${MCP_PH_DEF_VAR:-fallback}", &empty).unwrap(),
            "fallback"
        );
        let overlay = HashMap::from([("MCP_PH_DEF_VAR".to_string(), "dotenv_val".to_string())]);
        assert_eq!(
            resolve_env_placeholders_with("${MCP_PH_DEF_VAR:-fallback}", &overlay).unwrap(),
            "dotenv_val"
        );
        // 无默认值语法：两处均缺失 → 仍走早失败（GAP-4a 不放松错误路径）
        assert!(resolve_env_placeholders_with("${MCP_PH_DEF_VAR}", &empty).is_err());
    }

    #[test]
    fn test_resolve_placeholders_reads_project_dotenv_file() {
        // 真实文件依赖的集成路径（GAP-4a 主场景）：临时目录作项目根写 .env，
        // AGENTOS_CONFIG_ROOT 指向其 config 子目录（project_env_path 取 config
        // root 的父目录下 .env）。该变量是进程级全局，与 env_file::tests 共享
        // 互斥锁串行（见 TEST_ENV_MUTEX）。
        let _guard = crate::env_file::tests::TEST_ENV_MUTEX
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        let var = format!("MCP_DOTENV_IT_{}", Uuid::new_v4().simple());
        let tmp =
            std::env::temp_dir().join(format!("agentos_mcp_envit_{}", Uuid::new_v4().simple()));
        std::fs::create_dir_all(tmp.join("config")).unwrap();
        std::fs::write(tmp.join(".env"), format!("{var}=from_dotenv\n")).unwrap();

        // panic 安全地恢复 AGENTOS_CONFIG_ROOT（断言失败也不能污染其他测试）
        struct RestoreEnvRoot(Option<String>);
        impl Drop for RestoreEnvRoot {
            fn drop(&mut self) {
                match &self.0 {
                    Some(v) => std::env::set_var("AGENTOS_CONFIG_ROOT", v),
                    None => std::env::remove_var("AGENTOS_CONFIG_ROOT"),
                }
            }
        }
        let _restore = RestoreEnvRoot(std::env::var("AGENTOS_CONFIG_ROOT").ok());
        std::env::set_var("AGENTOS_CONFIG_ROOT", tmp.join("config"));

        // 1) 进程环境缺失 → .env overlay 提供值（设置页填 key 免重启生效路径）
        std::env::remove_var(&var);
        assert_eq!(
            resolve_env_placeholders(&format!("Bearer ${{{var}}}")).unwrap(),
            "Bearer from_dotenv"
        );
        // 2) 两者都有 → 进程环境优先（真实 env_delta_overlay 语义：系统环境
        //    显式设置的变量不进 overlay，此处非 vacuous 断言）
        std::env::set_var(&var, "from_process");
        assert_eq!(
            resolve_env_placeholders(&format!("Bearer ${{{var}}}")).unwrap(),
            "Bearer from_process"
        );

        std::env::remove_var(&var);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn test_manifest_mcp_endpoint_deserialize() {
        // 模拟 external_mcp 插件 plugin.json 的关键字段：endpoint 嵌套在 mcp 下。
        let json = r#"{
            "id": "external_resource_search",
            "name": "External Resource Search",
            "version": "1.0.0",
            "plugin_type": "tool",
            "language": "python",
            "host_type": "sidecar",
            "entry": "mcp:external",
            "capabilities": {},
            "mcp": {
                "transport": "streamable_http",
                "endpoint": {
                    "url": "https://registry.modelcontextprotocol.io",
                    "headers": {"X-Also-Search": "smithery.ai"},
                    "auth": {"type": "api_key", "header_name": "Authorization", "value": "${RESOURCE_SEARCH_API_KEY}"}
                }
            }
        }"#;
        let m: agentos_core::traits::PluginManifest = serde_json::from_str(json).unwrap();
        let cfg = m.mcp.expect("mcp config should deserialize");
        assert_eq!(
            cfg.transport,
            agentos_core::traits::McpTransport::StreamableHttp
        );
        let ep = cfg.endpoint.expect("endpoint should deserialize");
        assert_eq!(
            ep.url.as_deref(),
            Some("https://registry.modelcontextprotocol.io")
        );
        assert_eq!(ep.headers.get("X-Also-Search").unwrap(), "smithery.ai");
        let auth = ep.auth.expect("auth present");
        assert_eq!(auth.auth_type, AuthType::ApiKey);
        assert_eq!(auth.header_name, "Authorization");
        assert_eq!(auth.value, "${RESOURCE_SEARCH_API_KEY}");
        // 未声明 required → None（按必需处理，保持既有硬失败语义）
        assert_eq!(auth.required, None);
    }

    #[test]
    fn test_manifest_auth_required_false_deserialize() {
        // langchain_hub 风格：auth.required=false 必须能经反序列化到达内核
        // 逻辑（GAP-4b——此前该声明被 serde 静默丢弃）
        let json = r#"{
            "id": "langchain_like",
            "name": "LangChain Like",
            "version": "1.0.0",
            "plugin_type": "tool",
            "language": "external",
            "host_type": "sidecar",
            "entry": "mcp:external",
            "capabilities": {},
            "mcp": {
                "transport": "streamable_http",
                "endpoint": {
                    "url": "https://api.example.com/v1/search",
                    "auth": {"type": "api_key", "header_name": "x-api-key", "value": "${OPT_API_KEY}", "required": false}
                }
            }
        }"#;
        let m: agentos_core::traits::PluginManifest = serde_json::from_str(json).unwrap();
        let auth = m
            .mcp
            .expect("mcp config should deserialize")
            .endpoint
            .expect("endpoint should deserialize")
            .auth
            .expect("auth present");
        assert_eq!(auth.required, Some(false));
    }
}
