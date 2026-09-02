//! MCP JSON-RPC 客户端
//!
//! 实现基于 JSON-RPC 2.0 的 MCP 协议客户端，支持 stdio 和 HTTP 两种 transport。
//! 通过 stdin/stdout 与 Python 边车进程通信，完成 initialize 握手和 tools/call 调用。
//!

use std::collections::HashMap;
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::{mpsc, oneshot, Mutex};
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
    result: Option<Value>,
    error: Option<JsonRpcError>,
}

/// JSON-RPC 错误
#[derive(Debug, Deserialize)]
struct JsonRpcError {
    code: i64,
    message: String,
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
    /// stdio 写失败标记：stdin 写入/冲刷失败 = sidecar 进程已死铁证
    /// （管道破裂/句柄关闭）。与 [`is_alive`](Self::is_alive) 的 try_wait 快照
    /// 互补——Windows 退出通知未到/进程被强杀时 try_wait 有竞态盲区，
    /// 写失败标记让死亡判定在下次调用前即可成立。仅 stdio transport 置位。
    stdio_dead: Arc<AtomicBool>,
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

/// 出网 URL 边界守卫（安全审查摘出）。
///
/// 默认禁止 MCP HTTP 对私网/特殊段发包（RFC1918、ULA、链路本地=云元数据
/// 169.254.169.254、CGNAT、组播、未指定地址）；环回（127.0.0.0/8、::1）默认
/// 放行——本地 LLM 网关（ollama/one-api 等）是合法产品形态；设
/// `AGENTOS_MCP_BLOCK_LOOPBACK=1` 连环回一并禁止（加固部署）。
/// 主机名在连接时解析并逐个校验解析 IP；解析失败时放行并告警（无法分类时
/// 不误伤内网私有 DNS 部署；DNS 重绑定须域名解析被控，已属配置面失守）。
/// 配置来源：插件 manifest 的 mcp.http.url / 内核配置——均可能经配置写入面
/// 被篡改（A-2），此处为纵深防线。
fn ip_address_blocked(ip: std::net::IpAddr, block_loopback: bool) -> bool {
    match ip {
        std::net::IpAddr::V4(v4) => {
            let [a, b, _c, _d] = v4.octets();
            if a == 127 {
                // 环回：默认放行（本地 LLM 网关合法用例）；加固模式（block_loopback=true）禁止
                return block_loopback;
            }
            let private_net = a == 10
                || (a == 172 && (16..=31).contains(&b))
                || (a == 192 && b == 168)
                || (a == 169 && b == 254)
                || (a == 100 && (64..=127).contains(&b)) // CGNAT 100.64.0.0/10
                || a == 0;
            let special = (224..=239).contains(&a) || a >= 240; // 组播 + 保留
            private_net || special
        }
        std::net::IpAddr::V6(v6) => {
            let segments = v6.segments();
            if segments == [0, 0, 0, 0, 0, 0, 0, 1] {
                return block_loopback; // ::1 环回
            }
            let is_ula = (segments[0] & 0xfe00) == 0xfc00; // fc00::/7
            let is_link_local = (segments[0] & 0xffc0) == 0xfe80; // fe80::/10
            let is_multicast = (segments[0] & 0xff00) == 0xff00; // ff00::/8
            let is_unspecified = segments == [0; 8];
            is_ula || is_link_local || is_multicast || is_unspecified
        }
    }
}

/// 校验出网 URL 通过边界检查；非法 URL / 命中禁止段 → Err（fail-closed）。
fn is_outbound_url_allowed(url: &str) -> Result<(), McpError> {
    let parsed = reqwest::Url::parse(url).map_err(|e| McpError::ConnectionFailed {
        message: format!("invalid MCP http url {}: {}", url, e),
    })?;
    let host = parsed.host_str().unwrap_or_default().to_string();
    if host.is_empty() {
        return Err(McpError::ConnectionFailed {
            message: format!("MCP http url 无 host: {url}"),
        });
    }
    let block_loopback = std::env::var("AGENTOS_MCP_BLOCK_LOOPBACK").as_deref() == Ok("1");
    // 字面 IP 直判
    if let Ok(ip) = host.parse::<std::net::IpAddr>() {
        return if ip_address_blocked(ip, block_loopback) {
            Err(McpError::ConnectionFailed {
                message: format!(
                    "MCP http 出网被拒：{url} 命中禁止网段（私网/元数据/特殊段，环回{}放行）",
                    if block_loopback { "不" } else { "" },
                ),
            })
        } else {
            Ok(())
        };
    }
    // 主机名：解析后逐个 IP 校验
    use std::net::ToSocketAddrs;
    match (host.as_str(), 0).to_socket_addrs() {
        Ok(addrs) => {
            for addr in addrs {
                if ip_address_blocked(addr.ip(), block_loopback) {
                    return Err(McpError::ConnectionFailed {
                        message: format!("MCP http 出网被拒：{url} 解析到禁止网段 {}", addr.ip()),
                    });
                }
            }
            Ok(())
        }
        Err(e) => {
            tracing::warn!(
                "[mcp] 出网守卫：主机 {host} 解析失败，放行（无法分类，不误伤内网私有 DNS）| {e}"
            );
            Ok(())
        }
    }
}

/// 出网守卫单测（安全审查 2026-08-20 B-2）。
#[cfg(test)]
mod outbound_guard_tests {
    use super::*;

    #[test]
    fn test_private_and_special_ipv4_blocked() {
        for ip in [
            "10.0.0.1",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.1.1",
            "169.254.169.254", // 云元数据
            "100.64.0.1",      // CGNAT
            "0.0.0.1",
            "224.0.0.1",
            "240.0.0.1",
        ] {
            let addr: std::net::IpAddr = ip.parse().unwrap();
            assert!(ip_address_blocked(addr, false), "应拦截 {ip}");
        }
    }

    #[test]
    fn test_loopback_and_public_allowed() {
        let loopback: std::net::IpAddr = "127.0.0.1".parse().unwrap();
        assert!(!ip_address_blocked(loopback, false), "环回默认放行");
        assert!(ip_address_blocked(loopback, true), "加固模式禁环回");

        let v6_loopback: std::net::IpAddr = "::1".parse().unwrap();
        assert!(!ip_address_blocked(v6_loopback, false));

        for ip in ["8.8.8.8", "1.1.1.1", "223.5.5.5"] {
            let addr: std::net::IpAddr = ip.parse().unwrap();
            assert!(!ip_address_blocked(addr, false), "公网应放行 {ip}");
        }
    }

    #[test]
    fn test_ipv6_special_blocked() {
        for ip in ["fc00::1", "fd12:3456::1", "fe80::1", "ff02::1", "::"] {
            let addr: std::net::IpAddr = ip.parse().unwrap();
            assert!(ip_address_blocked(addr, false), "应拦截 {ip}");
        }
    }

    #[test]
    fn test_outbound_url_guard() {
        // 私网/元数据 → 拒绝
        assert!(is_outbound_url_allowed("http://192.168.1.5:8080/sse").is_err());
        assert!(is_outbound_url_allowed("http://10.0.0.2:3000").is_err());
        assert!(is_outbound_url_allowed("http://169.254.169.254/latest/meta-data").is_err());
        // 环回/localhost → 放行（本地 LLM 网关合法用例；localhost 解析到环回）
        assert!(is_outbound_url_allowed("http://127.0.0.1:11434").is_ok());
        assert!(is_outbound_url_allowed("http://localhost:11434").is_ok());
        // 公网 → 放行
        assert!(is_outbound_url_allowed("https://api.openai.com/v1").is_ok());
        // 非法 URL → fail-closed
        assert!(is_outbound_url_allowed("not-a-url").is_err());
    }
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
            stdio_dead: Arc::new(AtomicBool::new(false)),
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
            stdio_dead: Arc::new(AtomicBool::new(false)),
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

            // notification 有序派发：FIFO 通道 + 单工作任务串行处理。
            // 流式 chunk（block_start/text_delta/…）是有序增量，前端按到达序
            // 组装正文——处理完成序必须等于发送序。每条 spawn 并发的完成序
            // ≠ 发送序（各任务在 router.handle 与 WS 发送间独立调度），负载
            // 抖动下相邻 chunk 换序，前端本地流式文本被打碎（与服务端权威
            // 文本指纹不一致 → new_message 合并去重失效 → 回复气泡里重复
            // 两份且其中一份语序错乱）。工作任务串行 await 处理，reader loop
            // 只入队（unbounded send 永不阻塞）——chunk 推送不被读循环阻塞，
            // 顺序同时保住。
            let (notification_tx, notification_rx) = mpsc::unbounded_channel::<(String, Value)>();
            spawn_notification_worker(notification_rx, router.clone());

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
                            // 入有序派发队列（send 同步返回，不阻塞读循环），
                            // 处理由单工作任务按 FIFO 串行执行。
                            if let Some(method) =
                                msg.get("method").and_then(|v| v.as_str()).map(String::from)
                            {
                                let params = msg.get("params").cloned().unwrap_or(Value::Null);
                                let _ = notification_tx.send((method, params));
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

        // 先校验 URL 合法 + 出网边界（早暴露配置错误；顺带防 SSRF 到内网/云元数据）。
        is_outbound_url_allowed(url)?;

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
                .map_err(|e| {
                    self.mark_stdio_dead();
                    McpError::Transport {
                        message: format!("write error: {}", e),
                    }
                })?;
            writer.write_all(b"\n").await.map_err(|e| {
                self.mark_stdio_dead();
                McpError::Transport {
                    message: format!("write newline error: {}", e),
                }
            })?;
            writer.flush().await.map_err(|e| {
                self.mark_stdio_dead();
                McpError::Transport {
                    message: format!("flush error: {}", e),
                }
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

    /// 记录一次 stdin 写入/冲刷失败（stdio 下 = sidecar 进程已死铁证）。
    ///
    /// 写入失败只可能发生在已关闭的管道句柄上——进程死后 try_wait 的退出
    /// 通知可能未到（Windows 强杀/句柄竞态），写失败标记让判死在下次调用
    /// 前即可成立，不依赖退出状态快照。
    fn mark_stdio_dead(&self) {
        self.stdio_dead.store(true, Ordering::SeqCst);
    }

    /// stdio 运输下判定 sidecar 进程是否已死（供 invoker 复用/驱逐与崩溃恢复）。
    ///
    /// 证据三选一（任一命中即死）：
    /// 1. [`stdio_dead`](Self::stdio_dead) 写失败标记——进程死亡铁证；
    /// 2. child 已清——stdio 存活期恒持有 child，缺失 = 已被 kill（防御
    ///    kill 后未驱逐的残留缓存，杜绝"永远判活 → 复用死管道"）；
    /// 3. 退出快照（try_wait 已捕获退出状态）。
    ///
    /// HTTP 运输恒 false：远端进程不在本地，网络错误不等价于目标死亡。
    pub async fn is_dead(&self) -> bool {
        if !matches!(self.transport, McpTransport::Stdio { .. }) {
            return false;
        }
        if self.stdio_dead.load(Ordering::SeqCst) {
            return true;
        }
        let Some(child) = &self.child else {
            return true;
        };
        let mut child = child.lock().await;
        match child.try_wait() {
            Ok(None) => false,
            Ok(Some(_)) | Err(_) => true,
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
        // 复用的防御边界：kill 后 child=None 本身已判死；重置标记避免同一
        // client 对象被异常复用（重新 connect）时残留陈旧死亡证据。
        self.stdio_dead.store(false, Ordering::SeqCst);
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
        Ok(value) => {
            // MCP 官方 SDK 的 JSONRPCResponse.result 契约是 object（dict[str, Any]）：
            // 数组/标量/Null 会让对端 pydantic 校验失败、响应被静默丢弃 → 发起方
            // pending 永不 resolve → 超时（如 service-registry.
            // memory.search 返回 Vec 即中招）。非 object 一律包 {"__raw__": value}，
            // SDK KernelChannel.send_request 对称解包；object 原样（零迁移成本）。
            let result_value = if value.is_object() {
                value
            } else {
                serde_json::json!({ "__raw__": value })
            };
            serde_json::json!({
                "jsonrpc": "2.0",
                "id": raw_id,
                "result": result_value
            })
        }
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

/// notification 有序派发工作任务：从 FIFO 通道逐条取出并串行处理。
///
/// 同一 sidecar 连接的 notification（流式 chunk 为主）是有序增量，处理
/// 完成序必须等于发送序；串行 await 同时保证 router.handle 内部副作用
/// （如 event-bus.emit 的 WS 推送）按序完成。通道在 reader loop 结束
/// （stdout EOF）时 sender 归零，本任务排空残留后自然退出。
fn spawn_notification_worker(
    mut rx: mpsc::UnboundedReceiver<(String, Value)>,
    router: Option<Arc<dyn CapabilityRouter>>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        while let Some((method, params)) = rx.recv().await {
            handle_incoming_notification(&method, params, &router).await;
        }
    })
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
    use serde_json::json;

    /// python 可执行名（Windows 为 python，其余为 python3）。
    fn python_exe() -> &'static str {
        #[cfg(windows)]
        {
            "python"
        }
        #[cfg(not(windows))]
        {
            "python3"
        }
    }

    /// python 是否可用（不可用时跳过，避免 CI 无 python 环境失败）。
    fn python_available() -> bool {
        std::process::Command::new(python_exe())
            .arg("--version")
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    }

    /// stdio 写失败标记 / child 缺失（kill 后残留缓存）证据 → 判死。
    #[tokio::test]
    async fn is_dead_by_write_failure_mark() {
        let client = McpClient::new_stdio("cat", vec![]);
        // 模拟一次 stdin 写失败（进程已死铁证）
        client.mark_stdio_dead();
        assert!(client.is_dead().await, "写失败标记后必须判死");
    }

    /// child 被清（kill 后未驱逐的残留缓存）→ stdio 判死（杜绝永远判活）。
    #[tokio::test]
    async fn is_dead_by_missing_child() {
        let client = McpClient::new_stdio("cat", vec![]);
        // 未 connect / 已 kill 的 client 均无 child：stdlib 存活期恒持有 child
        assert!(
            client.is_dead().await,
            "stdio client 无 child（未连接/已 kill）必须判死"
        );
        client.mark_stdio_dead();
        assert!(client.is_dead().await);
    }

    /// HTTP transport 永不判死（远端进程不在本地，网络错误不等价死亡）。
    #[tokio::test]
    async fn is_dead_http_never() {
        let client = McpClient::new_http("http://127.0.0.1:9", HashMap::new(), None);
        assert!(!client.is_dead().await, "HTTP transport 不得判死");
        // HTTP 也不应被写失败标记影响（send_request 不经过 stdio 标记路径）
        assert!(!client.is_dead().await);
    }

    /// 从回显替身的 stdout 读一行（回显进程把写入 stdin 的行原样回显）。
    ///
    /// 实现：spawn_echo_stdio 的消费任务把读到的每一行存入共享 capture，
    /// 本函数轮询 capture 直到出现新行（响应行由 handle_incoming_request
    /// 写入 stdin 后回显进程立即回显）。
    async fn read_echo_line(capture: &Arc<std::sync::Mutex<Vec<String>>>) -> String {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        loop {
            if let Some(line) = capture.lock().unwrap().pop() {
                return line;
            }
            assert!(tokio::time::Instant::now() < deadline, "等待 cat 回显超时");
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

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
        // 逻辑（GAP-4b：该声明不得被 serde 静默丢弃）
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

    // ── 反向调用路由（handle_incoming_request / handle_incoming_notification）──

    /// 测试用 router：记录调用并返回可配置结果。
    struct RecordingRouter {
        calls: Arc<std::sync::Mutex<Vec<(String, String, Value)>>>,
        result: Value,
        fail: bool,
    }

    impl RecordingRouter {
        fn new(result: Value) -> Self {
            Self {
                calls: Arc::new(std::sync::Mutex::new(Vec::new())),
                result,
                fail: false,
            }
        }
        fn failing() -> Self {
            Self {
                calls: Arc::new(std::sync::Mutex::new(Vec::new())),
                result: Value::Null,
                fail: true,
            }
        }
    }

    #[async_trait::async_trait]
    impl CapabilityRouter for RecordingRouter {
        async fn handle(
            &self,
            capability: &str,
            method: &str,
            params: Value,
        ) -> Result<Value, McpError> {
            self.calls
                .lock()
                .unwrap()
                .push((capability.to_string(), method.to_string(), params));
            if self.fail {
                Err(McpError::Protocol {
                    message: "boom".to_string(),
                })
            } else {
                Ok(self.result.clone())
            }
        }
        fn known_namespaces(&self) -> Vec<String> {
            vec!["pipeline-executor".to_string(), "event-bus".to_string()]
        }
    }

    /// 用 python 作为 stdin 回显替身（`-u` 无缓冲）：写入的 JSON-RPC response 行
    /// 会原样出现在其 stdout 上，测试据此断言回写内容。
    /// 返回 (stdin, 捕获的 stdout 行队列, child)——child 必须由调用方持有，
    /// 否则 kill_on_drop 会在函数返回时立即杀掉回显进程。
    async fn spawn_echo_stdio() -> (
        Arc<Mutex<tokio::process::ChildStdin>>,
        Arc<std::sync::Mutex<Vec<String>>>,
        tokio::process::Child,
    ) {
        let script = "import sys\nfor line in sys.stdin:\n    sys.stdout.write(line)\n    sys.stdout.flush()";
        let mut child = tokio::process::Command::new(python_exe())
            .args(["-u", "-c", script])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .unwrap();
        let stdin = Arc::new(Mutex::new(child.stdin.take().unwrap()));
        let stdout = child.stdout.take().unwrap();
        let capture: Arc<std::sync::Mutex<Vec<String>>> =
            Arc::new(std::sync::Mutex::new(Vec::new()));
        let cap2 = capture.clone();
        tokio::spawn(async move {
            use tokio::io::AsyncBufReadExt;
            let mut reader = BufReader::new(stdout);
            let mut buf = Vec::new();
            loop {
                buf.clear();
                match reader.read_until(b'\n', &mut buf).await {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {
                        cap2.lock()
                            .unwrap()
                            .push(String::from_utf8_lossy(&buf).trim().to_string());
                    }
                }
            }
        });
        (stdin, capture, child)
    }

    #[tokio::test]
    async fn test_incoming_request_routes_and_echoes_id() {
        let router = Arc::new(RecordingRouter::new(json!({"ok": true})));
        let (stdin, capture, _child) = spawn_echo_stdio().await;

        // 字符串 id 反向调用 → 路由成功，回写 result（id 原样回显）
        let msg = json!({"jsonrpc": "2.0", "id": "req-1", "method": "pipeline-executor.resume", "params": {"x": 1}});
        handle_incoming_request(
            "pipeline-executor.resume",
            &msg,
            &msg["id"],
            "req-1",
            &Some(router.clone()),
            &stdin,
        )
        .await;
        let line = read_echo_line(&capture).await;
        let resp: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(resp["id"], "req-1");
        assert_eq!(resp["result"]["ok"], true);
        assert_eq!(router.calls.lock().unwrap().len(), 1);

        // 数值 id 反向调用 → id 按原类型回显（JSON-RPC 2.0 要求 response.id === request.id）
        let msg2 =
            json!({"jsonrpc": "2.0", "id": 7, "method": "event-bus.emit", "params": {"e": "tick"}});
        handle_incoming_request(
            "event-bus.emit",
            &msg2,
            &msg2["id"],
            "7",
            &Some(router.clone()),
            &stdin,
        )
        .await;
        let line2 = read_echo_line(&capture).await;
        let resp2: Value = serde_json::from_str(&line2).unwrap();
        assert_eq!(resp2["id"], 7, "数值 id 必须按原类型回显");
        assert_eq!(router.calls.lock().unwrap().len(), 2);
    }

    #[tokio::test]
    async fn test_incoming_request_non_object_result_wrapped() {
        // 非 object 结果（数组/标量）必须包 {"__raw__": value}——官方 SDK 的
        // result 契约是 object，裸数组会让对端 pydantic 校验失败。
        let router = Arc::new(RecordingRouter::new(json!([1, 2, 3])));
        let (stdin, capture, _child) = spawn_echo_stdio().await;

        let msg = json!({"jsonrpc": "2.0", "id": "r2", "method": "pipeline-executor.resume"});
        handle_incoming_request(
            "pipeline-executor.resume",
            &msg,
            &msg["id"],
            "r2",
            &Some(router.clone()),
            &stdin,
        )
        .await;
        let line = read_echo_line(&capture).await;
        let resp: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(resp["result"]["__raw__"], json!([1, 2, 3]));
    }

    #[tokio::test]
    async fn test_incoming_request_error_and_no_router() {
        // router 处理失败 → -32603 错误响应
        let router = Arc::new(RecordingRouter::failing());
        let (stdin, capture, _child) = spawn_echo_stdio().await;
        let msg = json!({"jsonrpc": "2.0", "id": "r3", "method": "pipeline-executor.resume"});
        handle_incoming_request(
            "pipeline-executor.resume",
            &msg,
            &msg["id"],
            "r3",
            &Some(router.clone()),
            &stdin,
        )
        .await;
        let line = read_echo_line(&capture).await;
        let resp: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(resp["error"]["code"], -32603);

        // router 为 None → known_namespaces 为空 → 解析失败走 method not found 分支
        let (stdin2, capture2, _child2) = spawn_echo_stdio().await;
        let msg2 = json!({"jsonrpc": "2.0", "id": "r4", "method": "pipeline-executor.resume"});
        handle_incoming_request(
            "pipeline-executor.resume",
            &msg2,
            &msg2["id"],
            "r4",
            &None,
            &stdin2,
        )
        .await;
        let line2 = read_echo_line(&capture2).await;
        let resp2: Value = serde_json::from_str(&line2).unwrap();
        assert_eq!(resp2["error"]["code"], -32601);
        assert!(resp2["error"]["message"]
            .as_str()
            .unwrap()
            .contains("method not found"));
    }

    #[tokio::test]
    async fn test_incoming_request_unknown_method_rejected() {
        // 非 capability method（MCP 标准方法 / 未知 namespace）→ -32601 method not found
        let router = Arc::new(RecordingRouter::new(json!({})));
        let (stdin, capture, _child) = spawn_echo_stdio().await;
        let msg = json!({"jsonrpc": "2.0", "id": "r5", "method": "tools/list"});
        handle_incoming_request(
            "tools/list",
            &msg,
            &msg["id"],
            "r5",
            &Some(router.clone()),
            &stdin,
        )
        .await;
        let line = read_echo_line(&capture).await;
        let resp: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(resp["error"]["code"], -32601);
        assert!(resp["error"]["message"]
            .as_str()
            .unwrap()
            .contains("method not found"));
        assert!(
            router.calls.lock().unwrap().is_empty(),
            "未知 method 不应路由"
        );
    }

    #[tokio::test]
    async fn test_incoming_notification_routes_and_ignores_result() {
        let router = Arc::new(RecordingRouter::new(json!({"ignored": true})));
        handle_incoming_notification(
            "event-bus.emit",
            json!({"e": "chunk"}),
            &Some(router.clone()),
        )
        .await;
        let calls = router.calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "event-bus");
        assert_eq!(calls[0].1, "emit");
        assert_eq!(calls[0].2["e"], "chunk");
    }

    #[tokio::test]
    async fn test_incoming_notification_unknown_and_no_router() {
        // 未知 method → 丢弃（不 panic、不路由）
        let router = Arc::new(RecordingRouter::new(json!({})));
        handle_incoming_notification(
            "notifications/initialized",
            Value::Null,
            &Some(router.clone()),
        )
        .await;
        assert!(router.calls.lock().unwrap().is_empty());

        // router 为 None → 静默丢弃
        handle_incoming_notification("event-bus.emit", Value::Null, &None).await;
    }

    #[tokio::test]
    async fn test_incoming_notification_handler_error_swallowed() {
        // notification 处理失败只记日志，不 panic（fire-and-forget 语义）
        let router = Arc::new(RecordingRouter::failing());
        handle_incoming_notification("event-bus.emit", Value::Null, &Some(router.clone())).await;
        assert_eq!(router.calls.lock().unwrap().len(), 1);
    }

    /// 记录【处理完成序】的 router：偶数序号无延迟、奇数序号延迟 1ms——
    /// 若处理是并发的，短任务会越过前面的长任务（完成序 ≠ 提交序）；
    /// 串行 FIFO 派发下完成序必须恒等于提交序。
    struct JitteredDelayRouter {
        completions: Arc<std::sync::Mutex<Vec<u64>>>,
    }

    #[async_trait::async_trait]
    impl CapabilityRouter for JitteredDelayRouter {
        async fn handle(
            &self,
            _capability: &str,
            _method: &str,
            params: Value,
        ) -> Result<Value, McpError> {
            let seq = params["seq"].as_u64().unwrap_or(0);
            if seq % 2 == 1 {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
            self.completions.lock().unwrap().push(seq);
            Ok(json!({}))
        }
        fn known_namespaces(&self) -> Vec<String> {
            vec!["event-bus".to_string()]
        }
    }

    #[tokio::test]
    async fn test_notification_worker_preserves_arrival_order() {
        // 流式 chunk 是有序增量：派发工作任务必须串行处理（FIFO），
        // 完成序 == 发送序。交错延迟构造并发场景下的确定性乱序（奇数
        // 追平偶数），串行派发下不受影响。
        let completions = Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = Arc::new(JitteredDelayRouter {
            completions: completions.clone(),
        });
        let (tx, rx) = mpsc::unbounded_channel::<(String, Value)>();
        let worker = spawn_notification_worker(rx, Some(router));

        const N: u64 = 32;
        for i in 0..N {
            tx.send((
                "event-bus.emit".to_string(),
                json!({ "event": "text_delta", "seq": i }),
            ))
            .unwrap();
        }
        drop(tx); // reader loop EOF 语义：sender 归零，worker 排空后退出
        worker.await.unwrap();

        let got = completions.lock().unwrap().clone();
        let expected: Vec<u64> = (0..N).collect();
        assert_eq!(
            got, expected,
            "notification 处理完成序必须等于发送序（流式增量有序契约）"
        );
    }

    // ── stdio send_request / send_notification 错误路径 ────────────────

    #[tokio::test]
    async fn test_send_request_not_connected_returns_error() {
        let client = McpClient::new_stdio("cat", vec![]);
        let err = client.send_request("tools/list", None).await.unwrap_err();
        assert!(matches!(err, McpError::ConnectionFailed { .. }));
    }

    #[tokio::test]
    async fn test_send_notification_not_connected_returns_error() {
        let client = McpClient::new_stdio("cat", vec![]);
        let err = client
            .send_notification("notifications/initialized", None)
            .await
            .unwrap_err();
        assert!(matches!(err, McpError::ConnectionFailed { .. }));
    }

    #[tokio::test]
    async fn test_send_request_http_without_connect_returns_error() {
        // HTTP transport 但未 connect（http_client 未构建）→ 明确报错
        let client = McpClient::new_http("http://127.0.0.1:1", HashMap::new(), None);
        let err = client.send_request("tools/list", None).await.unwrap_err();
        assert!(matches!(err, McpError::ConnectionFailed { .. }));
    }

    #[tokio::test]
    async fn test_send_notification_http_without_connect_returns_error() {
        let client = McpClient::new_http("http://127.0.0.1:1", HashMap::new(), None);
        let err = client
            .send_notification("notifications/initialized", None)
            .await
            .unwrap_err();
        assert!(matches!(err, McpError::ConnectionFailed { .. }));
    }

    #[tokio::test]
    async fn test_send_request_timeout_when_no_response() {
        if !python_available() {
            return;
        }
        // sidecar 读一行后静默（不回响应）→ 短超时后返回 Timeout（不阻塞到默认 300s）
        let script = "import sys; sys.stdin.readline(); import time; time.sleep(30)";
        let mut client = McpClient::new_stdio(
            python_exe().to_string(),
            vec!["-c".to_string(), script.to_string()],
        );
        client = client.with_request_timeout(Duration::from_millis(200));
        client.connect().await.unwrap();
        let err = client.send_request("tools/list", None).await.unwrap_err();
        assert!(matches!(err, McpError::Timeout { .. }));
        client.kill().await.unwrap();
    }

    #[tokio::test]
    async fn test_send_request_sidecar_error_response() {
        if !python_available() {
            return;
        }
        // sidecar 返回 JSON-RPC error → 映射为 Protocol 错误（回显请求 id 才能配对）
        let script = "import sys, json; req=json.loads(sys.stdin.readline()); print(json.dumps({'jsonrpc':'2.0','id':req['id'],'error':{'code':-32000,'message':'sidecar boom'}}), flush=True)";
        let mut client = McpClient::new_stdio(
            python_exe().to_string(),
            vec!["-c".to_string(), script.to_string()],
        );
        client.connect().await.unwrap();
        let err = client.send_request("tools/list", None).await.unwrap_err();
        assert!(matches!(err, McpError::Protocol { .. }));
        assert!(err.to_string().contains("sidecar boom"));
        client.kill().await.unwrap();
    }

    #[tokio::test]
    async fn test_send_request_sidecar_result_without_id_ignored() {
        if !python_available() {
            return;
        }
        // sidecar 输出无 id 的 JSON（非 response/request/notification）→ 忽略；
        // 随后 sidecar 退出 → EOF 清空 pending → 请求快速失败（channel closed），
        // 不 panic、不误配对、不阻塞到超时
        let script = "import sys; sys.stdin.readline(); print('{\"unrelated\": true}', flush=True)";
        let mut client = McpClient::new_stdio(
            python_exe().to_string(),
            vec!["-c".to_string(), script.to_string()],
        );
        client = client.with_request_timeout(Duration::from_millis(300));
        client.connect().await.unwrap();
        let err = client.send_request("tools/list", None).await.unwrap_err();
        assert!(
            matches!(err, McpError::Protocol { .. }),
            "EOF 清空 pending 应快速失败: {err}"
        );
        client.kill().await.unwrap();
    }

    #[tokio::test]
    async fn test_initialize_sets_initialized_flag() {
        if !python_available() {
            return;
        }
        // initialize 成功 → initialized 置 true；失败（sidecar 崩溃）→ 保持 false
        // sidecar 回显请求 id 并消费 initialized 通知后退出
        let script = "import sys, json; req=json.loads(sys.stdin.readline()); print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':{'ok':True}}), flush=True); sys.stdin.readline()";
        let mut client = McpClient::new_stdio(
            python_exe().to_string(),
            vec!["-c".to_string(), script.to_string()],
        );
        client.connect().await.unwrap();
        let result = client.initialize(&json!({"k": "v"})).await;
        assert!(result.is_ok());
        assert!(
            *client.initialized.lock().await,
            "initialize 后应置 initialized"
        );

        // 崩溃 sidecar：initialize 快速失败，initialized 保持 false
        let script2 = "import sys; sys.stdin.readline(); sys.exit(1)";
        let mut client2 = McpClient::new_stdio(
            python_exe().to_string(),
            vec!["-c".to_string(), script2.to_string()],
        );
        client2.connect().await.unwrap();
        tokio::time::sleep(Duration::from_millis(300)).await;
        let result2 = client2.initialize(&json!({})).await;
        assert!(result2.is_err());
        assert!(!*client2.initialized.lock().await, "失败不应置 initialized");
    }

    #[tokio::test]
    async fn test_http_post_error_branches() {
        // 未 connect → 明确报错
        let client = McpClient::new_http("http://127.0.0.1:1", HashMap::new(), None);
        let request = JsonRpcRequest {
            jsonrpc: "2.0",
            id: "1".to_string(),
            method: "tools/list".to_string(),
            params: None,
        };
        let err = client
            .http_post("http://127.0.0.1:1", &request)
            .await
            .unwrap_err();
        assert!(matches!(err, McpError::ConnectionFailed { .. }));

        // 连接拒绝 → Transport 错误
        let mut client2 = McpClient::new_http("http://127.0.0.1:1", HashMap::new(), None);
        client2.connect().await.unwrap();
        let err2 = client2
            .http_post("http://127.0.0.1:1", &request)
            .await
            .unwrap_err();
        assert!(matches!(err2, McpError::Transport { .. }));
    }

    #[tokio::test]
    async fn test_http_post_sse_and_error_response() {
        // SSE content-type → 明确报错（流式暂不支持）
        let (url, _) = spawn_raw_http_server(
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\ndata: x\n\n",
        )
        .await;
        let mut client = McpClient::new_http(url.clone(), HashMap::new(), None);
        client.connect().await.unwrap();
        let request = JsonRpcRequest {
            jsonrpc: "2.0",
            id: "1".to_string(),
            method: "tools/list".to_string(),
            params: None,
        };
        let err = client.http_post(&url, &request).await.unwrap_err();
        assert!(matches!(err, McpError::Protocol { .. }));
        assert!(err.to_string().contains("SSE"));

        // JSON-RPC error 响应 → Protocol 错误（带 code/message）
        let (url2, _) = spawn_raw_http_server(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"jsonrpc\":\"2.0\",\"id\":1,\"error\":{\"code\":-32601,\"message\":\"method not found\"}}",
        )
        .await;
        let mut client2 = McpClient::new_http(url2.clone(), HashMap::new(), None);
        client2.connect().await.unwrap();
        let err2 = client2.http_post(&url2, &request).await.unwrap_err();
        assert!(matches!(err2, McpError::Protocol { .. }));
        assert!(err2.to_string().contains("-32601"));

        // 非 2xx 状态 → Protocol 错误（带状态码与响应体片段）
        let (url3, _) = spawn_raw_http_server(
            "HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\n\r\n{\"error\":\"boom\"}",
        )
        .await;
        let mut client3 = McpClient::new_http(url3.clone(), HashMap::new(), None);
        client3.connect().await.unwrap();
        let err3 = client3.http_post(&url3, &request).await.unwrap_err();
        assert!(matches!(err3, McpError::Protocol { .. }));
        assert!(err3.to_string().contains("500"));
    }

    /// 启动一个返回固定原始 HTTP 响应的 mock server。
    async fn spawn_raw_http_server(raw_response: &'static str) -> (String, ()) {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move {
            let (mut sock, _) = listener.accept().await.unwrap();
            let mut buf = [0u8; 4096];
            let _ = sock.read(&mut buf).await;
            let _ = sock.write_all(raw_response.as_bytes()).await;
        });
        (url, ())
    }

    #[tokio::test]
    async fn test_http_send_notification_roundtrip() {
        // HTTP notification：fire-and-forget POST，忽略响应体
        let (url, captured) = spawn_mock_mcp_server(json!({"ok": true})).await;
        let mut client = McpClient::new_http(url, HashMap::new(), None);
        client.connect().await.unwrap();
        client
            .send_notification("notifications/initialized", Some(json!({"a": 1})))
            .await
            .unwrap();

        let raw = captured.lock().unwrap().clone().unwrap();
        let raw_s = String::from_utf8_lossy(&raw);
        assert!(
            raw_s.contains("notifications/initialized"),
            "notification method 应出现在请求体: {raw_s}"
        );
        assert!(
            raw_s.contains("\"a\":1"),
            "notification params 应出现在请求体: {raw_s}"
        );
    }

    #[tokio::test]
    async fn test_http_send_request_roundtrip_with_error_result() {
        // 响应含 error 字段 → send_request 返回 Protocol 错误
        let (url, _) = spawn_raw_http_server(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"jsonrpc\":\"2.0\",\"id\":1,\"error\":{\"code\":-32000,\"message\":\"nope\"}}",
        )
        .await;
        let mut client = McpClient::new_http(url, HashMap::new(), None);
        client.connect().await.unwrap();
        let err = client.send_request("tools/list", None).await.unwrap_err();
        assert!(matches!(err, McpError::Protocol { .. }));
        assert!(err.to_string().contains("nope"));
    }

    #[tokio::test]
    async fn test_http_send_request_result_missing() {
        // 响应无 result 也无 error → Protocol 错误
        let (url, _) = spawn_raw_http_server(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"jsonrpc\":\"2.0\",\"id\":1}",
        )
        .await;
        let mut client = McpClient::new_http(url, HashMap::new(), None);
        client.connect().await.unwrap();
        let err = client.send_request("tools/list", None).await.unwrap_err();
        assert!(matches!(err, McpError::Protocol { .. }));
    }

    #[tokio::test]
    async fn test_http_connect_rejects_private_net() {
        // 出网守卫：私网 URL 在 connect 时即拒绝（fail-closed）
        let mut client = McpClient::new_http("http://192.168.1.5:8080/mcp", HashMap::new(), None);
        let err = client.connect().await.unwrap_err();
        assert!(matches!(err, McpError::ConnectionFailed { .. }));
    }

    #[tokio::test]
    async fn test_http_connect_invalid_header_skipped() {
        // 非法 header 名/值 → 跳过并告警，connect 仍成功
        let (url, _) = spawn_mock_mcp_server(json!({"ok": true})).await;
        let mut headers = HashMap::new();
        headers.insert("bad header name".to_string(), "v".to_string());
        headers.insert("x-ok".to_string(), "1".to_string());
        let mut client = McpClient::new_http(url, headers, None);
        client.connect().await.unwrap();
        let result = client.send_request("tools/list", None).await.unwrap();
        assert_eq!(result["ok"], true);
    }

    #[tokio::test]
    async fn test_http_connect_invalid_auth_header_name() {
        // ApiKey 且 header_name 非法 → connect 报错
        let auth = agentos_core::traits::EndpointAuth {
            auth_type: AuthType::ApiKey,
            header_name: "bad header name".to_string(),
            value: "secret".to_string(),
            required: None,
        };
        let mut client = McpClient::new_http("http://127.0.0.1:1", HashMap::new(), Some(auth));
        let err = client.connect().await.unwrap_err();
        assert!(matches!(err, McpError::ConnectionFailed { .. }));
    }

    #[test]
    fn test_builder_methods() {
        // with_working_dir / with_extra_env / with_plugin_id / with_request_timeout
        let client = McpClient::new_stdio("python3", vec![])
            .with_working_dir("/tmp/plugin_dir")
            .with_extra_env(vec![("PYTHONPATH".to_string(), "/shared".to_string())])
            .with_plugin_id("my-plugin")
            .with_request_timeout(Duration::from_secs(7));
        assert_eq!(
            client.working_dir.as_deref(),
            Some(std::path::Path::new("/tmp/plugin_dir"))
        );
        assert_eq!(
            client.extra_env,
            vec![("PYTHONPATH".to_string(), "/shared".to_string())]
        );
        assert_eq!(client.plugin_id.as_deref(), Some("my-plugin"));
        assert_eq!(client.request_timeout, Duration::from_secs(7));
    }

    #[tokio::test]
    async fn test_pid_and_kill_when_not_connected() {
        let client = McpClient::new_stdio("cat", vec![]);
        assert!(client.pid().await.is_none(), "未连接时 pid 应为 None");

        let mut client2 = McpClient::new_stdio("cat", vec![]);
        client2.kill().await.unwrap();
        assert!(!client2.is_alive().await);
    }

    #[tokio::test]
    async fn test_http_post_parse_error() {
        // 200 但响应体不是合法 JSON-RPC → Protocol 错误
        let (url, _) = spawn_raw_http_server(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\nnot-json",
        )
        .await;
        let mut client = McpClient::new_http(url.clone(), HashMap::new(), None);
        client.connect().await.unwrap();
        let request = JsonRpcRequest {
            jsonrpc: "2.0",
            id: "1".to_string(),
            method: "tools/list".to_string(),
            params: None,
        };
        let err = client.http_post(&url, &request).await.unwrap_err();
        assert!(matches!(err, McpError::Protocol { .. }));
    }

    #[test]
    fn test_resolve_env_placeholders_unclosed_and_empty() {
        // 未闭合占位 → 原样保留不报错
        assert_eq!(
            resolve_env_placeholders("a${UNCLOSED").unwrap(),
            "a${UNCLOSED"
        );
        // 空占位 ${} → 变量名为空 → 未设置报错
        assert!(resolve_env_placeholders("${}").is_err());
        // 空串输入
        assert_eq!(resolve_env_placeholders("").unwrap(), "");
    }

    #[test]
    fn test_resolve_env_placeholders_with_overlay_empty_var() {
        // overlay 提供空串 + ":-" 默认值 → 空串触发默认值（shell ":-" 语义）
        let overlay = HashMap::from([("MCP_EMPTY_OVL".to_string(), String::new())]);
        assert_eq!(
            resolve_env_placeholders_with("${MCP_EMPTY_OVL:-fb}", &overlay).unwrap(),
            "fb"
        );
        // overlay 提供空串 + "-" 默认值 → 保留空串
        assert_eq!(
            resolve_env_placeholders_with("${MCP_EMPTY_OVL-fb}", &overlay).unwrap(),
            ""
        );
    }

    #[cfg(windows)]
    #[test]
    fn test_resolve_windows_command_passthrough() {
        // 已带扩展名/路径分隔符 → 原样返回
        assert_eq!(resolve_windows_command("npx.cmd"), "npx.cmd");
        assert_eq!(resolve_windows_command("C:\\tools\\npx"), "C:\\tools\\npx");
        assert_eq!(resolve_windows_command("tools/npx"), "tools/npx");
        // 无扩展名且 PATH 中不存在 → 原样返回（让 spawn 报真实错误）
        assert_eq!(
            resolve_windows_command("definitely_not_a_real_cmd_xyz"),
            "definitely_not_a_real_cmd_xyz"
        );
    }
}
