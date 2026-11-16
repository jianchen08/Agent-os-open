//! MCP 反向调用面（client 子模块）：stdin 原始回写、pending 响应解析、
//! 反向调用并发闸门（R-1 排队制，拍板② 2026-10-10）、sidecar 主动
//! request/notification 的路由派发与有界通知队列。
//!
//! 由 client.rs `mod reverse` 挂载：父模块消费项经其 `use`/`pub use` 再导出，
//! 既有引用路径（`crate::client::ReverseGate`、`agentos_mcp::reverse_call_levels`）
//! 不变；`JsonRpcResponse` 与 `NOTIFICATION_QUEUE_CAPACITY` 仍归父模块，经
//! `super::` 现取。
use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde_json::Value;
use tokio::io::AsyncWriteExt;
use tokio::sync::{oneshot, Mutex, Semaphore};
use tracing::warn;

use crate::capability::{parse_capability_method_with, CapabilityRouter};
use crate::error::McpError;

use super::{JsonRpcResponse, NOTIFICATION_QUEUE_CAPACITY};

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
            message: format!("write error: {e}"),
        })?;
    writer
        .write_all(b"\n")
        .await
        .map_err(|e| McpError::Transport {
            message: format!("write newline error: {e}"),
        })?;
    writer.flush().await.map_err(|e| McpError::Transport {
        message: format!("flush error: {e}"),
    })?;
    Ok(())
}

/// 读循环分支1：响应帧处理——按字符串 id 查 pending 表，命中则 resolve
/// oneshot（未命中：无任务在等该 id，帧丢弃）。
pub(super) async fn resolve_pending_response(
    line_str: &str,
    id: &str,
    pending: &Arc<Mutex<HashMap<String, oneshot::Sender<JsonRpcResponse>>>>,
) {
    let mut pending_map = pending.lock().await;
    let Some(sender) = pending_map.remove(id) else {
        return;
    };
    // 匹配 pending → 这是 response
    if let Ok(response) = serde_json::from_str::<JsonRpcResponse>(line_str) {
        if sender.send(response).is_err() {
            tracing::warn!(
                "[mcp] pending receiver dropped; JSON-RPC response id={id} not delivered"
            );
        }
    }
}

// ── 反向调用并发闸门（整改 R-1：排队制，拍板② 2026-10-10）─────────────────

/// 反向调用并发上限缺省（`AGENTOS_MCP_REVERSE_MAX_CONCURRENT` 未配置时生效）。
/// 上限只防失控不设瓶颈——MCP 反向调用（event-bus 流式、审批、能力调用）常态
/// 即高并发，128 远高于单 sidecar 真实峰值。
pub(super) const DEFAULT_REVERSE_MAX_CONCURRENT: usize = 128;

/// 排队等待上限缺省秒（`AGENTOS_MCP_REVERSE_QUEUE_WAIT_SECS` 未配置时生效）。
/// 排队也要有界：等不到许可的反向调用回 JSON-RPC error，否则极端积压下等待
/// 任务无界堆积，排队等于没治。
const DEFAULT_REVERSE_QUEUE_WAIT_SECS: f64 = 30.0;

/// 进程级反向调用水位：跨全部 sidecar 聚合（/metrics gauge 的采样源）。
static REVERSE_QUEUED_TOTAL: AtomicU64 = AtomicU64::new(0);
static REVERSE_ACTIVE_TOTAL: AtomicU64 = AtomicU64::new(0);

/// 内核当前 MCP 反向调用水位（排队等待数，执行中数）——agentos-kernel 的
/// 指标 flush 节拍每秒采样，经聚合器真接写入 `/metrics`（gauge，零新增面）。
pub fn reverse_call_levels() -> (u64, u64) {
    (
        REVERSE_QUEUED_TOTAL.load(Ordering::Relaxed),
        REVERSE_ACTIVE_TOTAL.load(Ordering::Relaxed),
    )
}

/// 排队等待超限（acquire 在 queue_wait 内未获得许可）。
struct QueueWaitElapsed;

/// 反向调用并发闸门：信号量 acquire-with-wait（= 有界排队，零拒绝）。
///
/// 正常高并发全部排队依次通过；仅当排队等待超过上限时该次调用失败，调用方
/// 回 JSON-RPC error（极端积压的有界兜底）。per-handle 超时默认不开——
/// 86400 合法长等待（审批/人机交互族）由排队上限兜底，缺省不掐断长调用。
/// per-McpClient 一闸：各 sidecar 独立配额，互不挤占。
pub struct ReverseGate {
    /// 并发许可（permits = 并发上限）。
    semaphore: Semaphore,
    /// 并发上限（错误消息与观测口径用）。
    max_concurrent: usize,
    /// 排队等待上限。
    queue_wait: Duration,
    /// per-handle 超时（None = 不开）。
    handle_timeout: Option<Duration>,
    /// 当前排队等许可的任务数（per-gate 观测面，测试断言用）。
    pub(super) queued: AtomicU64,
    /// 当前执行中的反向调用数（per-gate 观测面，测试断言用）。
    pub(super) active: AtomicU64,
}

/// 持许可守卫：drop 归还许可并扣减 active 水位（panic 路径同样守恒）。
struct ReversePermit<'a> {
    /// RAII 持有：守卫 drop 时随之外层 drop 归还许可（字段本身不被读取）。
    _permit: tokio::sync::SemaphorePermit<'a>,
    gate_active: &'a AtomicU64,
}

impl Drop for ReversePermit<'_> {
    fn drop(&mut self) {
        self.gate_active.fetch_sub(1, Ordering::Relaxed);
        REVERSE_ACTIVE_TOTAL.fetch_sub(1, Ordering::Relaxed);
    }
}

impl ReverseGate {
    /// 按显式配置建闸（生产走 [`ReverseGate::from_env`]，测试注入精确值）。
    pub fn new(
        max_concurrent: usize,
        queue_wait: Duration,
        handle_timeout: Option<Duration>,
    ) -> Self {
        Self {
            semaphore: Semaphore::new(max_concurrent.max(1)),
            max_concurrent: max_concurrent.max(1),
            queue_wait,
            handle_timeout,
            queued: AtomicU64::new(0),
            active: AtomicU64::new(0),
        }
    }

    /// 按环境变量建闸（缺省 128 并发 / 30s 排队等待 / per-handle 超时不开）。
    pub fn from_env() -> Self {
        Self::new(
            resolve_reverse_max_concurrent(
                std::env::var("AGENTOS_MCP_REVERSE_MAX_CONCURRENT")
                    .ok()
                    .as_deref(),
            ),
            Duration::from_secs_f64(resolve_reverse_queue_wait_secs(
                std::env::var("AGENTOS_MCP_REVERSE_QUEUE_WAIT_SECS")
                    .ok()
                    .as_deref(),
            )),
            resolve_reverse_handle_timeout(
                std::env::var("AGENTOS_MCP_REVERSE_HANDLE_TIMEOUT_SECS")
                    .ok()
                    .as_deref(),
            ),
        )
    }

    /// acquire-with-wait：有空位立即通过；满载则排队等待（不拒绝），仅当等待
    /// 超过 queue_wait 上限时失败（调用方回 JSON-RPC error）。
    async fn acquire(&self) -> Result<ReversePermit<'_>, QueueWaitElapsed> {
        self.queued.fetch_add(1, Ordering::Relaxed);
        REVERSE_QUEUED_TOTAL.fetch_add(1, Ordering::Relaxed);
        let waited = tokio::time::timeout(self.queue_wait, self.semaphore.acquire()).await;
        self.queued.fetch_sub(1, Ordering::Relaxed);
        REVERSE_QUEUED_TOTAL.fetch_sub(1, Ordering::Relaxed);
        match waited {
            // 闸只增不减不 close，AcquireError 不可达；防御性按等待超限同路收口。
            Ok(Ok(permit)) => {
                self.active.fetch_add(1, Ordering::Relaxed);
                REVERSE_ACTIVE_TOTAL.fetch_add(1, Ordering::Relaxed);
                Ok(ReversePermit {
                    _permit: permit,
                    gate_active: &self.active,
                })
            }
            Ok(Err(_)) | Err(_) => Err(QueueWaitElapsed),
        }
    }
}

/// 解析并发上限：非法/非正值回缺省（0 会使闸永久拒绝，视同非法）。
pub(super) fn resolve_reverse_max_concurrent(raw: Option<&str>) -> usize {
    raw.and_then(|v| v.trim().parse::<usize>().ok())
        .filter(|&n| n > 0)
        .unwrap_or(DEFAULT_REVERSE_MAX_CONCURRENT)
}

/// 解析排队等待上限秒（f64，支持 0.1 一类小值注入）：非法/非正/非有限回缺省。
pub(super) fn resolve_reverse_queue_wait_secs(raw: Option<&str>) -> f64 {
    raw.and_then(|v| v.trim().parse::<f64>().ok())
        .filter(|s| *s > 0.0 && s.is_finite())
        .unwrap_or(DEFAULT_REVERSE_QUEUE_WAIT_SECS)
}

/// 解析 per-handle 超时秒：未设/非法/非正 = 不开（缺省语义）。
pub(super) fn resolve_reverse_handle_timeout(raw: Option<&str>) -> Option<Duration> {
    raw.and_then(|v| v.trim().parse::<f64>().ok())
        .filter(|s| *s > 0.0 && s.is_finite())
        .map(Duration::from_secs_f64)
}

/// 读循环分支2：sidecar 主动发起的 request（有 method + id）→ 派发独立处理任务。
///
/// 处理必须离开读循环：同宿主自反调用（插件经 router 调同宿主工具）写 stdin
/// 后等待响应，而响应只能被读循环读出——内联 await = 读循环等自己，死锁至
/// request_timeout；非自反的长反向调用同样队头阻塞读循环，令同宿主其他
/// pending 响应延迟解析造成假超时。派发即返回，读循环继续消费下一帧。
/// 处理任务内所有失败路径（router 错误 / stdin 回写失败）已在
/// handle_incoming_request 内 warn 留痕；任务自身 panic 的 JoinError 由
/// 看护任务 warn，不静默。
pub(super) fn spawn_request_dispatch(
    msg: Value,
    method: String,
    id: Value,
    router: Option<Arc<dyn CapabilityRouter>>,
    stdin: Arc<Mutex<tokio::process::ChildStdin>>,
    gate: Arc<ReverseGate>,
) {
    let task = tokio::spawn(async move {
        dispatch_incoming_request(&msg, &method, &id, &router, &stdin, &gate).await;
    });
    tokio::spawn(async move {
        if let Err(e) = task.await {
            warn!("[mcp] 反向调用处理任务异常终止: {e}");
        }
    });
}

/// 读循环分支2：sidecar 主动发起的 request（有 method + id）→ 路由反向调用。
pub(super) async fn dispatch_incoming_request(
    msg: &Value,
    method: &str,
    id: &Value,
    router: &Option<Arc<dyn CapabilityRouter>>,
    stdin: &Arc<Mutex<tokio::process::ChildStdin>>,
    gate: &ReverseGate,
) {
    let raw_id = id.clone();
    // 日志/错误消息用规范化字符串形式（数值 id 转十进制）
    let id_repr = match id {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    };
    // R-1 排队制：acquire-with-wait——满载时本任务在此排队（读循环已派发返回，
    // 不受影响），仅等待超限时回 JSON-RPC error（-32000，server error 区段）。
    let _permit = match gate.acquire().await {
        Ok(permit) => permit,
        Err(QueueWaitElapsed) => {
            let resp = serde_json::json!({
                "jsonrpc": "2.0",
                "id": raw_id,
                "error": {"code": -32000, "message": format!(
                    "mcp reverse call queue wait timeout after {:.1}s (max {} concurrent)",
                    gate.queue_wait.as_secs_f64(),
                    gate.max_concurrent
                )}
            });
            warn!(
                "[mcp] reverse call id={id_repr} exceeded queue wait limit ({:?}); answering error",
                gate.queue_wait
            );
            if let Err(e) = write_raw_line(stdin, &resp.to_string()).await {
                warn!("[mcp] failed to write queue-wait-timeout response for id={id_repr}: {e}");
            }
            return;
        }
    };
    handle_incoming_request(method, msg, &raw_id, &id_repr, router, stdin, gate).await;
}

/// notification 有界派发队列（FIFO + 满载丢最旧）。
///
/// notification（流式 chunk 推送为主）是 best-effort 单向通知：读循环不允许
/// 被慢消费阻塞（否则响应 resolve 时效受损），也不能无界积压——容量打满后
/// 淘汰最旧条目并累计计数告警。丢最旧保完成帧：流式正文丢头仍会终结，丢尾
/// 则前端等不到完成标记而悬挂。条目随连接生命周期：reader loop 结束置关闭
/// 位，工作任务排空残留后退出。
pub(super) struct NotificationQueue {
    /// FIFO 队列体。锁只护出入队（无 await 持锁），工作任务在锁外串行处理。
    inner: std::sync::Mutex<VecDeque<(String, Value)>>,
    /// 空队列/关闭唤醒：push 与 close 各补一个许可，pop 侧不丢唤醒。
    notify: tokio::sync::Notify,
    /// reader loop 结束置位：队列排空后工作任务退出。
    closed: AtomicBool,
    /// 满载淘汰最旧的累计条数（过载观测面）。
    dropped: AtomicU64,
}

impl NotificationQueue {
    pub(super) fn new() -> Self {
        Self {
            inner: std::sync::Mutex::new(VecDeque::new()),
            notify: tokio::sync::Notify::new(),
            closed: AtomicBool::new(false),
            dropped: AtomicU64::new(0),
        }
    }

    /// 入队（同步返回，不阻塞读循环）。容量满则淘汰最旧并计数告警。
    pub(super) fn push(&self, item: (String, Value)) {
        let evicted = {
            let mut q = self
                .inner
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            q.push_back(item);
            if q.len() > NOTIFICATION_QUEUE_CAPACITY {
                q.pop_front().is_some()
            } else {
                false
            }
        };
        if evicted {
            let total = self.dropped.fetch_add(1, Ordering::Relaxed) + 1;
            warn!(
                target: "mcp:notification",
                total_dropped = total,
                capacity = NOTIFICATION_QUEUE_CAPACITY,
                "notification queue full; dropped oldest (best-effort)"
            );
        }
        self.notify.notify_one();
    }

    /// 取出最旧条目；空且未关闭时挂起等待；空且已关闭时返回 None
    /// （工作任务退出点）。
    pub(super) async fn pop(&self) -> Option<(String, Value)> {
        loop {
            let item = self
                .inner
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .pop_front();
            if item.is_some() {
                return item;
            }
            if self.closed.load(Ordering::Relaxed) {
                return None;
            }
            self.notify.notified().await;
        }
    }

    /// reader loop 结束调用：置关闭位并唤醒工作任务（排空残留后退出）。
    pub(super) fn close(&self) {
        self.closed.store(true, Ordering::Relaxed);
        self.notify.notify_one();
    }

    /// 当前队列长度（测试观测面）。
    #[cfg(test)]
    pub(super) fn len(&self) -> usize {
        self.inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .len()
    }

    /// 满载淘汰累计条数（测试观测面）。
    #[cfg(test)]
    pub(super) fn dropped_count(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }
}

/// 读循环分支3：sidecar 主动发起的 notification（有 method 无 id）→
/// 入有序派发队列（满载丢最旧、同步返回，不阻塞读循环）。
pub(super) fn dispatch_notification(method: String, msg: &Value, queue: &NotificationQueue) {
    let params = msg.get("params").cloned().unwrap_or(Value::Null);
    queue.push((method, params));
}

/// 处理 sidecar 主动发起的 JSON-RPC request。
///
/// method 形如 `pipeline-executor.resume`，拆分后路由到 CapabilityRouter；
/// 结果通过 stdin 回写为 JSON-RPC response。`raw_id` 为请求的原始 id 值
/// （string 或 number，按 JSON-RPC 2.0 要求原样回显），`id_repr` 为其字符串
/// 形式（仅用于日志）。
pub(super) async fn handle_incoming_request(
    method: &str,
    msg: &Value,
    raw_id: &Value,
    id_repr: &str,
    router: &Option<Arc<dyn CapabilityRouter>>,
    stdin: &Arc<Mutex<tokio::process::ChildStdin>>,
    gate: &ReverseGate,
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

    // R-1 per-handle 超时：默认不开（86400 合法长等待由排队上限兜底）；
    // 显式开启（AGENTOS_MCP_REVERSE_HANDLE_TIMEOUT_SECS）后超限按 -32603
    // 内部错误回话，router.handle future 被取消（tokio::timeout 语义）。
    let result = match gate.handle_timeout {
        Some(limit) => {
            match tokio::time::timeout(limit, router.handle(capability, cap_method, params)).await {
                Ok(r) => r,
                Err(_) => Err(McpError::Protocol {
                    message: format!("reverse call handle timeout after {limit:?}"),
                }),
            }
        }
        None => router.handle(capability, cap_method, params).await,
    };
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

/// notification 有序派发工作任务：从有界 FIFO 队列逐条取出并串行处理。
///
/// 同一 sidecar 连接的 notification（流式 chunk 为主）是有序增量，处理
/// 完成序必须等于发送序；串行 await 同时保证 router.handle 内部副作用
/// （如 event-bus.emit 的 WS 推送）按序完成。队列在 reader loop 结束
/// （stdout EOF）置关闭位，排空残留后本任务退出。
pub(super) fn spawn_notification_worker(
    queue: Arc<NotificationQueue>,
    router: Option<Arc<dyn CapabilityRouter>>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        while let Some((method, params)) = queue.pop().await {
            handle_incoming_notification(&method, params, &router).await;
        }
    })
}

/// 处理 sidecar 主动发起的 JSON-RPC notification（无 id，fire-and-forget）。
///
/// method 形如 `event-bus.emit`，拆分后路由到 CapabilityRouter。
/// 与 handle_incoming_request 的区别：不回 response（notification 协议无响应）。
/// 用于流式 chunk 高频推送（sidecar 边生成边推，内核推前端）。
pub(super) async fn handle_incoming_notification(
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
