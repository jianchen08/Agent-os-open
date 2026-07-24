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

use crate::capability::{parse_capability_method, CapabilityRouter};
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
    /// 等待响应的 oneshot 发送器
    pending: Arc<Mutex<HashMap<String, oneshot::Sender<JsonRpcResponse>>>>,
    /// 是否已初始化
    initialized: Arc<Mutex<bool>>,
    /// Capability 路由器——处理 sidecar 反向调用内核能力。
    /// None 时 sidecar 反向调用将被拒绝（返回 method not found）。
    router: Option<Arc<dyn CapabilityRouter>>,
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
            pending: Arc::new(Mutex::new(HashMap::new())),
            initialized: Arc::new(Mutex::new(false)),
            router: None,
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
            pending: Arc::new(Mutex::new(HashMap::new())),
            initialized: Arc::new(Mutex::new(false)),
            router: None,
        }
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

                self.child = Some(Arc::new(Mutex::new(child)));
                self.stdin = Some(Arc::new(Mutex::new(stdin)));
                self.stdout = Some(Arc::new(Mutex::new(BufReader::new(stdout))));

                // 启动 stdout 读取循环
                self.start_reader_loop().await;

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
                        Ok(0) => break, // EOF
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
                            }
                            // 其余（notification 无 id 等）忽略
                        }
                        Err(_) => break,
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

        // 等待响应（超时 30 秒）
        let response = tokio::time::timeout(Duration::from_secs(30), rx)
            .await
            .map_err(|_| McpError::Timeout { timeout_secs: 30 })?
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
        // 仅在配置了 router 时声明全部能力；否则声明空（sidecar 反调会被拒）。
        let capabilities: Value = if self.router.is_some() {
            serde_json::json!({
                "pipeline-executor": {},
                "config-reader": {},
                "tenant-context": {},
                "event-bus": {},
                "logger": {},
                "metrics": {}
            })
        } else {
            serde_json::json!({})
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

    /// 终止子进程
    pub async fn kill(&mut self) -> Result<(), McpError> {
        if let Some(child) = &self.child {
            let mut child = child.lock().await;
            child.kill().await.map_err(|e| McpError::Transport {
                message: format!("kill error: {}", e),
            })?;
        }
        self.child = None;
        self.stdin = None;
        self.stdout = None;
        *self.initialized.lock().await = false;
        Ok(())
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

    // 非 capability method（如 MCP 标准方法）不处理
    let Some((capability, cap_method)) = parse_capability_method(method) else {
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

impl Drop for McpClient {
    fn drop(&mut self) {
        // kill_on_drop(true) 会在 Drop 时自动 kill 子进程
    }
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
}
