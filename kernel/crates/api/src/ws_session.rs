//! WebSocket 会话接入——把 session crate 接到 axum WS（ADR §7.2 P2 接线）。
//!
//! 职责：
//! - [`WsSink`]：axum WebSocket 的 `EventSink` 适配（经 mpsc 通道转发，避免
//!   `SplitSink` 跨 await 共享问题）；
//! - [`run_ws_session`]：握手鉴权 → 注册连接 → 入站路由（user_input/interaction/
//!   stop/heartbeat）→ 出站通道排空到 socket。断线重连由 SessionCoordinator 统一处理。

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use agentos_core::traits::AdrEngine;
use agentos_session::auth::HandshakeAuth;
use agentos_session::router::{InboundRouter, PipelineDispatcher, RouteOutcome};
use agentos_session::{EventSink, SessionCoordinator};
use axum::extract::ws::{Message, WebSocket};
use futures_util::{SinkExt, StreamExt};
use serde_json::json;
use tokio::sync::mpsc;
use tracing::{info, warn};

use crate::auth::verify_access_token;
use crate::routes::AppState;

/// 全局 WS sink id 生成器（连接注册表去重/踢旧用）。
static SINK_ID_SEQ: AtomicU64 = AtomicU64::new(1);

/// axum WebSocket 的 EventSink 适配。
///
/// 通过无界 mpsc 通道转发文本帧：`run_ws_session` 的出站排空任务从接收端
/// 取消息写入 socket，send_text 只往通道投递（不阻塞、跨 await 安全）。
/// 连接关闭时 drop sender，排空任务自然结束。
pub struct WsSink {
    id: u64,
    tx: mpsc::UnboundedSender<String>,
}

impl WsSink {
    /// 创建 sink + 接收端（接收端供出站排空任务消费）。
    pub fn new() -> (Arc<Self>, mpsc::UnboundedReceiver<String>) {
        let (tx, rx) = mpsc::unbounded_channel();
        let sink = Arc::new(Self {
            id: SINK_ID_SEQ.fetch_add(1, Ordering::SeqCst),
            tx,
        });
        (sink, rx)
    }
}

#[async_trait::async_trait]
impl EventSink for WsSink {
    async fn send_text(&self, text: &str) -> bool {
        self.tx.send(text.to_string()).is_ok()
    }
    fn id(&self) -> u64 {
        self.id
    }
}

/// 握手鉴权：从 query 参数取 token，调 verify_access_token。
///
/// 参考 0.1 `app_factory.py:232`（token 缺失/无效 → 4001 拒绝）。
pub fn authenticate(token: Option<&str>) -> HandshakeAuth {
    let token = token.unwrap_or("");
    agentos_session::auth::authenticate_handshake(token, &|t| {
        verify_access_token(t).map(|u| (u.user_id, u.username))
    })
}

/// 运行一次 WS 会话：握手鉴权 → 注册 → 收发循环。
///
/// 返回 close code（供调用方在握手拒绝场景 accept+close）。
/// 握手拒绝时返回 `(4001, reason)`，调用方应 accept 后 close。
/// 握手通过则阻塞至连接关闭，返回 `(1000, "normal closure")`。
pub async fn run_ws_session(
    socket: WebSocket,
    session: Arc<SessionCoordinator>,
    router: Arc<InboundRouter>,
    token: Option<&str>,
    user_id_out: &mut Option<String>,
) -> (u16, String) {
    let auth = authenticate(token);
    let (user_id, username) = match auth {
        HandshakeAuth::Ok { user_id, username } => (user_id, username),
        HandshakeAuth::Rejected { code, reason } => {
            return (code, reason);
        }
    };
    *user_id_out = Some(user_id.clone());

    // 注册连接（含出站通道 sink）
    let (sink, out_rx) = WsSink::new();
    if let Some(old_id) = session.register(&user_id, sink.clone()) {
        info!(user = %user_id, kicked_old = old_id, "WS 踢旧连接（B10 单连接）");
    }

    run_socket_loop(socket, sink, out_rx, session, router, user_id.clone()).await;
    info!(user = %user_id, username = %username, "WS 会话结束");
    (1000, "normal closure".to_string())
}

/// 出站排空 + 入站路由双任务循环，任一结束即关闭会话。
async fn run_socket_loop(
    socket: WebSocket,
    sink: Arc<WsSink>,
    mut out_rx: mpsc::UnboundedReceiver<String>,
    session: Arc<SessionCoordinator>,
    router: Arc<InboundRouter>,
    user_id: String,
) {
    let (mut sender, mut receiver) = socket.split();

    // 发送连接确认
    let confirmation = json!({
        "type": "connection_confirmation",
        "data": {"status": "connected", "mode": "global", "user_id": user_id},
    });
    let _ = sender
        .send(Message::Text(
            serde_json::to_string(&confirmation).unwrap_or_default().into(),
        ))
        .await;
    info!(user = %user_id, "WS 会话已建立");

    // 出站排空任务：从 channel 取消息写入 socket
    let mut send_task = tokio::spawn(async move {
        while let Some(text) = out_rx.recv().await {
            if sender.send(Message::Text(text.into())).await.is_err() {
                break;
            }
        }
        let _ = sender.close().await;
    });

    // 入站循环：路由消息（session/router/user_id move 进 task）
    let session_for_unreg = session.clone();
    let sink_id = sink.id();
    let user_id_for_task = user_id.clone();
    let mut recv_task = tokio::spawn(async move {
        while let Some(Ok(msg)) = receiver.next().await {
            match msg {
                Message::Text(text) => {
                    handle_inbound(&text, &user_id_for_task, &session, &router).await;
                }
                Message::Close(_) => break,
                _ => {}
            }
        }
    });

    // 任一任务结束即关闭会话
    tokio::select! {
        _ = &mut send_task => { recv_task.abort(); }
        _ = &mut recv_task => { send_task.abort(); }
    }
    // 清理：注销连接（仅当仍是当前 sink 时）
    session_for_unreg.registry().unregister(&user_id, sink_id);
}

/// 处理一条入站文本消息：路由 + 心跳 + thread 注册。
async fn handle_inbound(
    text: &str,
    user_id: &str,
    session: &SessionCoordinator,
    router: &InboundRouter,
) {
    let msg: serde_json::Value = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(_) => return,
    };
    // user_input/interaction 携带 thread_id 时建立 thread→user 映射
    if let Some(thread_id) = msg.get("thread_id").and_then(|v| v.as_str()) {
        if !thread_id.is_empty() {
            session.register_thread(thread_id, user_id);
        }
    }
    match router.route(&msg, user_id).await {
        RouteOutcome::Heartbeat => {
            // 回 heartbeat_ack：对照 0.1，前端连续收不到 ack 会判定连接死亡断连
            // （之前只打日志不回 ack，导致 LLM 生成期间连接被前端超时断开）。
            let ack = serde_json::json!({"type": "heartbeat_ack", "data": {"timestamp": chrono::Utc::now().to_rfc3339()}});
            let ack_str = serde_json::to_string(&ack).unwrap_or_default();
            session.registry().send_to_user(user_id, &ack_str).await;
            tracing::debug!(user = user_id, "heartbeat -> ack");
        }
        RouteOutcome::Error(e) => warn!(user = user_id, error = %e, "入站路由错误"),
        RouteOutcome::Handled | RouteOutcome::Ignored => {}
    }
}

/// 基于管道引擎的入站分发器（P2-6：迁 0.1 inbound 分支）。
///
/// user_input → process_via_engine（在租户上下文内执行管道）；
/// interaction_response / stop_generation → 记录（引擎层审批/取消接入待 P3+）。
pub struct EngineDispatcher {
    state: AppState,
}

impl EngineDispatcher {
    pub fn new(state: AppState) -> Self {
        Self { state }
    }
}

#[async_trait::async_trait]
impl PipelineDispatcher for EngineDispatcher {
    async fn dispatch_user_input(
        &self,
        thread_id: &str,
        user_id: &str,
        content: &str,
        pipeline_id: &str,
    ) -> Result<(), String> {
        use agentos_core::types::TenantContext;
        // tenant_id 必须用真正的租户 ID（与 HTTP 路径 resolve_request_tenant_id 同源），
        // 不能用 user_id 顶替——否则消息按 user_id 落库，读取时按 default 查不到，
        // 表现为「刷新后历史消息不显示」。
        // 从 self.state.store 查持久化用户的 tenant_id（一用户一租户 = user_id）。
        let tenant_id =
            crate::auth::resolve_tenant_id_by_user(self.state.store.as_ref(), user_id).await;
        let state = self.state.clone();
        let content = content.to_string();
        // pipeline_id 是前端消息路由键，引擎回推流式事件时用它定位占位气泡。
        // 缺失时回退 thread_id（前端 handleStreamStart 的 resolvePipelineId
        // 不回退 _threadId，故缺失会导致事件被丢弃——这里尽量传真实值）。
        //
        // 防御性校验（后端不盲目信任前端数据）：前端切换/新建会话后，activePipelineId
        // 可能因 React 渲染时序残留旧值，导致消息写到错误的 pipeline 桶（串消息）。
        // 这里查 pipeline_sessions 表校验 pipeline_id 是否确实属于该 thread_id：
        //   - 属于 → 信任前端值
        //   - 不属于 → 用该 thread 的真实 active_pipeline_id（主管道），杜绝串桶
        // 这是与前端源头修复（router.tsx 实时读 sessionStore）互补的双层防线。
        // 注意：resolve 必须在 tenant 构造前调用（TenantContext::new 会 move tenant_id）。
        let route_id = resolve_pipeline_id_for_thread(
            state.store.as_ref(),
            thread_id,
            pipeline_id,
            &tenant_id,
        )
        .await;
        let tenant = TenantContext::new(tenant_id, thread_id.to_string());

        // 生成 assistant message_id（内核权威，sidecar chunk + new_message 共用）。
        // 对照 0.1 bridge.emit_start：在 LLM 调用前就发 stream_start + 确定 message_id，
        // 这样 sidecar 边生成边推的 stream_chunk 能匹配到占位气泡（前端 ensureStreamingPlaceholder）。
        let message_id = format!("a_{}", uuid::Uuid::new_v4().simple());

        if let Some(session) = state.session.as_ref() {
            // 1. stream_start 提前发（在引擎执行前），让前端先建立占位气泡
            let _ = session
                .emit_event(thread_id, "stream_start", serde_json::json!({
                    "pipeline_id": route_id,
                    "message_id": message_id,
                    "_threadId": thread_id,
                }))
                .await;
        }

        // 在租户上下文内执行管道。message_id 注入 state 供 sidecar 流式 chunk 携带
        // （sidecar on_chunk notify 时带上，前端据此把 chunk 路由到占位气泡）。
        let response =
            agentos_tenant::scope(tenant, crate::server::process_via_engine(&state, &content, "agentos", &[], &route_id, thread_id, &message_id))
                .await;

        // 引擎完成后发 new_message 收尾（填充完整内容 + 标记 completed）。
        // 对照 0.1 bridge.emit_finish：完整内容由最终事件推送，chunk 只负责逐字显示。
        if let Some(session) = state.session.as_ref() {
            let delivered = session
                .emit_event(thread_id, "new_message", serde_json::json!({
                    "pipeline_id": route_id,
                    "message_id": message_id,
                    "_threadId": thread_id,
                    "content": response,
                    "parts": [{ "type": "text", "text": response }],
                    "sequence": 1,
                }))
                .await;
            info!(thread = thread_id, delivered = delivered, "new_message 推送完成");
        } else {
            tracing::warn!(thread = thread_id, "session 未启用，引擎结果无法推回前端");
        }
        Ok(())
    }

    async fn dispatch_interaction_response(
        &self,
        _thread_id: &str,
        request_id: &str,
        response: &serde_json::Value,
    ) -> Result<(), String> {
        // 两条唤醒路径各走各的：
        // (A) interaction.respond → 唤醒 human_interaction 工具进程内 Event
        //     （LLM 直调 choice/conversation 路径：wait_for_choice 阻塞在此 Event）。
        // (B) engine.resume → 唤醒审批挂起的管道 run（approval suspend 创建），
        //     仅当 DB 有对应 suspended run 时执行；LLM 直调路径无则跳过，不视为错误。
        info!(request_id = request_id, "interaction_response 已接收");

        let inner = if response.is_object() { response } else { &serde_json::Value::Null };
        let mut respond_failed = false;
        if let Some(invoker) = self.state.invoker.clone() {
            let inputs = serde_json::json!({
                "request_id": request_id,
                "response_type": inner.get("response_type").and_then(|v| v.as_str()).unwrap_or("answered"),
                "selected_option": inner.get("selected_option").and_then(|v| v.as_str()),
                "answers": inner.get("answers"),
                "feedback": inner.get("feedback").and_then(|v| v.as_str()),
            });
            if let Err(e) = invoker
                .invoke_tool("human_interaction_tool", "interaction.respond", &inputs)
                .await
            {
                warn!(request_id = request_id, error = %e.message, "interaction.respond 调用失败");
                respond_failed = true;
            }
        } else {
            warn!(request_id = request_id, "invoker 未注入，跳过 interaction.respond");
        }

        if let Some(db) = self.state.db.as_ref() {
            match db.find_suspended_run_by_request_id(request_id) {
                Ok(Some(run)) => {
                    let meta = run.metadata.clone().unwrap_or_default();
                    let handle = agentos_core::types::SuspendHandle {
                        run_id: run.run_id.clone(),
                        branch_id: meta.get("suspend_branch_id").and_then(|v| v.as_str()).unwrap_or(&run.current_branch).to_string(),
                        seq: meta.get("suspend_seq").and_then(|v| v.as_u64()).unwrap_or(run.current_seq as u64) as u32,
                    };
                    if let Some(engine) = self.state.engine.as_ref() {
                        match engine.resume(&handle, agentos_core::types::WakeEvent::Manual).await {
                            Ok(()) => info!(run_id = %run.run_id, request_id = request_id, "interaction_response 已唤醒 suspended run"),
                            Err(e) => warn!(request_id = request_id, error = %e, "suspended run resume 失败"),
                        }
                    }
                }
                Ok(None) => tracing::debug!(request_id = request_id, "无 suspended run（LLM 直调路径），仅 interaction.respond"),
                Err(e) => warn!(request_id = request_id, error = %e, "查找 suspended run 失败"),
            }
        }

        if respond_failed { Err(format!("interaction.respond 失败: request_id={request_id}")) } else { Ok(()) }
    }

    async fn dispatch_stop(&self, thread_id: &str) -> Result<(), String> {
        // 取消生成：引擎层 cancel 接入待后续阶段
        info!(thread = thread_id, "stop_generation 已接收（引擎取消接入待 P3+）");
        Ok(())
    }
}

/// 解析消息应路由到的真实 pipeline_id（防御性校验，后端不盲目信任前端数据）。
///
/// 校验逻辑：
/// 1. 前端传的 pipeline_id 非空 且 属于该 thread_id（查 pipeline_sessions）→ 信任前端值
/// 2. 否则取该 thread 的真实 active_pipeline_id（主管道）作为权威值
/// 3. 仍取不到 → 回退 thread_id（兼容旧路径，与原 route_id 语义一致）
///
/// 这与前端源头修复（router.tsx 实时读 sessionStore）互补，形成双层防线：
/// 前端尽量传对，后端兜底确保即使前端传错也不串桶。
async fn resolve_pipeline_id_for_thread(
    store: Option<&Arc<dyn agentos_core::traits::StorageBackend>>,
    thread_id: &str,
    frontend_pipeline_id: &str,
    tenant_id: &str,
) -> String {
    let Some(store) = store else {
        return if frontend_pipeline_id.is_empty() {
            thread_id.to_string()
        } else {
            frontend_pipeline_id.to_string()
        };
    };

    // ① 前端值非空且属于该 thread → 信任
    if !frontend_pipeline_id.is_empty() {
        let pids = store
            .list_pipeline_ids_by_thread(thread_id, tenant_id)
            .await
            .unwrap_or_default();
        if pids.iter().any(|p| p == frontend_pipeline_id) {
            return frontend_pipeline_id.to_string();
        }
        warn!(
            thread_id = %thread_id,
            frontend_pid = %frontend_pipeline_id,
            "前端传来的 pipeline_id 不属于该 thread（可能残留旧会话值），改用 thread 真实主管道"
        );
    }

    // ② 取该 thread 的真实 active_pipeline_id
    let tenant = agentos_core::types::TenantContext::new(tenant_id.to_string(), thread_id.to_string());
    let tid = thread_id.to_string();
    let store_clone = store.clone();
    let session = agentos_tenant::scope(tenant, async move {
        store_clone.get_session(&tid).await
    })
    .await
    .ok()
    .flatten();
    if let Some(active) = session.and_then(|s| s.active_pipeline_id) {
        if !active.is_empty() {
            return active;
        }
    }

    // ③ 回退 thread_id（兼容）
    thread_id.to_string()
}

