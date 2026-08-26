//! WebSocket 会话接入——把 session crate 接到 axum WS（ADR §7.2 P2 接线）。
//!
//! 职责：
//! - [`WsSink`]：axum WebSocket 的 `EventSink` 适配（经 mpsc 通道转发，避免
//!   `SplitSink` 跨 await 共享问题）；
//! - [`run_ws_session`]：握手鉴权 → 注册连接 → 入站路由（user_input/interaction/
//!   stop/heartbeat）→ 出站通道排空到 socket。断线重连由 SessionCoordinator 统一处理。

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use agentos_core::traits::{MessageQueryOpts, StorageBackend};
use agentos_core::types::{
    PatchType, PendingInputRecord, PendingInputSource, TenantContext, TraceEntry,
};
use agentos_session::auth::HandshakeAuth;
use agentos_session::router::{InboundRouter, PipelineDispatcher, RouteOutcome};
use agentos_session::{EventSink, SessionCoordinator};
use axum::extract::ws::{Message, WebSocket};
use futures_util::{SinkExt, StreamExt};
use serde_json::json;
use tokio::sync::mpsc;
use tracing::{debug, info, warn};

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
    fn shutdown(&self) {
        // 关闭哨兵：出站排空任务收到空串即退出并向对端发 Close 帧。
        // （tokio 1.52 的 UnboundedSender 无 close API，receiver 在排空任务
        // 手里，只能经 channel 发信号；正常事件均为非空 JSON，不会误伤。）
        let _ = self.tx.send(String::new());
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
    last_sequence: Option<u64>,
) -> (u16, String) {
    let auth = authenticate(token);
    let (user_id, username) = match auth {
        HandshakeAuth::Ok { user_id, username } => (user_id, username),
        HandshakeAuth::Rejected { code, reason } => {
            // 拒绝必须先发 Close 帧（code=4001）再返回：直接 drop socket 浏览器端
            // 只会收到 1006（abnormal closure），前端 GlobalWebSocket 的
            // 「4001 → refreshToken → 重连」自愈路径永远不触发，表现为掉线后
            // 无限重连失败（“未连接”常驻、发消息无响应）。Close 帧发送失败也不
            // 影响语义（连接随 drop 关闭）。
            let mut rejected_socket = socket;
            let _ = rejected_socket
                .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                    code,
                    reason: reason.clone().into(),
                })))
                .await;
            return (code, reason);
        }
    };
    *user_id_out = Some(user_id.clone());

    // 注册连接（含出站通道 sink）
    let (sink, out_rx) = WsSink::new();
    if let Some(old_id) = session.register(&user_id, sink.clone()) {
        info!(user = %user_id, kicked_old = old_id, "WS 踢旧连接（B10 单连接）");
    }

    run_socket_loop(
        socket,
        sink,
        out_rx,
        session,
        router,
        user_id.clone(),
        last_sequence,
    )
    .await;
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
    last_sequence: Option<u64>,
) {
    let (mut sender, mut receiver) = socket.split();

    // 发送连接确认
    let confirmation = json!({
        "type": "connection_confirmation",
        "data": {"status": "connected", "mode": "global", "user_id": user_id},
    });
    let _ = sender
        .send(Message::Text(
            serde_json::to_string(&confirmation)
                .unwrap_or_default()
                .into(),
        ))
        .await;
    info!(user = %user_id, "WS 会话已建立");

    // 断线重连（last_sequence > 0）：连接建立即按 watermark 重放该 user 全部
    // 线程的缓冲事件。不能依赖 B3（首条带 thread_id 的入站消息触发）——前端
    // 重连后不重发 active_thread_changed、心跳 thread_id 为空，B3 触发饥饿，
    // 断连期间落缓冲的 new_message 永远等不到重放（2026-08-23 真机复现：
    // 回复落库但前端不显示，刷新才出现）。floor 之后的事件经当前连接实时
    // 送达，重放只发 (last_sequence, floor] 防重复。
    let replayed_for_task = Arc::new(std::sync::atomic::AtomicBool::new(false));
    if let Some(ls) = last_sequence.filter(|&l| l > 0) {
        let floor = session.current_sequence().await;
        let sink_for_replay: Arc<dyn agentos_session::EventSink> = sink.clone();
        session
            .replay_all_for_user(&user_id, ls, floor, &sink_for_replay)
            .await;
        // 建连重放已覆盖全部已知线程：预置标志防 B3 对首条入站消息二次重放
        //（stream_chunk 重复推送会导致前端文本双写）。
        replayed_for_task.store(true, std::sync::atomic::Ordering::SeqCst);
    }

    // 出站排空任务：从 channel 取消息写入 socket
    let mut send_task = tokio::spawn(async move {
        while let Some(text) = out_rx.recv().await {
            if text.is_empty() {
                // 踢旧关闭（WsSink::shutdown 发出空串哨兵）：先发带 CLOSE_CODE_KICKED
                // 状态码的 Close 帧再退出——前端 GlobalWebSocket 对 4000 判"被新连接替换"
                // 跳过重连，双客户端互踢风暴（每 ~4.5s 断连、消息发送全断）的断根点。
                // 旧实现只发空 Close（浏览器 onclose=1005），前端按普通掉线 4s 退避重连
                // → A/B 各自重连互踢 → 无限循环。
                let _ = sender
                    .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                        code: agentos_session::auth::CLOSE_CODE_KICKED,
                        reason: "replaced_by_new_connection".into(),
                    })))
                    .await;
                break;
            }
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
    // B3：每连接一次的回放标志（建连重放已消费时为 true）——首个带 thread_id
    // 的入站消息触发 replay_missed。
    let mut recv_task = tokio::spawn(async move {
        while let Some(Ok(msg)) = receiver.next().await {
            match msg {
                Message::Text(text) => {
                    handle_inbound(
                        &text,
                        &user_id_for_task,
                        &session,
                        &router,
                        last_sequence,
                        &replayed_for_task,
                    )
                    .await;
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
    last_sequence: Option<u64>,
    replayed: &std::sync::atomic::AtomicBool,
) {
    let msg: serde_json::Value = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(_) => return,
    };
    // user_input/interaction 携带 thread_id 时建立 thread→user 映射
    if let Some(thread_id) = msg.get("thread_id").and_then(|v| v.as_str()) {
        if !thread_id.is_empty() {
            session.register_thread(thread_id, user_id);
            // B3：首个 thread 注册时回放断线期间该 thread 缺失的事件（每连接仅一次）。
            if let Some(ls) = last_sequence.filter(|&l| l > 0) {
                if !replayed.swap(true, std::sync::atomic::Ordering::SeqCst) {
                    session.replay_missed(thread_id, user_id, ls).await;
                }
            }
        }
    }
    match router.route(&msg, user_id).await {
        RouteOutcome::Heartbeat => {
            // 回 heartbeat_ack：前端连续收不到 ack 会判定连接死亡并断连
            // （LLM 生成期间若无 ack，会被前端超时断开），心跳必须回 ack。
            let ack = serde_json::json!({"type": "heartbeat_ack", "data": {"timestamp": chrono::Utc::now().to_rfc3339()}});
            let ack_str = serde_json::to_string(&ack).unwrap_or_default();
            session.registry().send_to_user(user_id, &ack_str).await;
            tracing::debug!(user = user_id, "heartbeat -> ack");
        }
        RouteOutcome::Error(e) => warn!(user = user_id, error = %e, "入站路由错误"),
        RouteOutcome::Handled | RouteOutcome::Ignored => {}
    }
}

/// 执行 agent 解析（绑定真值进管道 state 的消费点）。
///
/// 优先级：显式传入（chat.send_message 任务派发按 target 选 agent，非空）
/// → registry 线程绑定（热路径，会话编辑后即生效）
/// → DB sessions.agent_id（冷路径，内核重启后 registry 丢失）
/// → "agentos"（默认主 agent）。
///
/// WS 主会话路径（InboundRouter::route_user_input）传空串 = 未指定，
/// 由本函数按绑定解析——硬编码 "agentos" 会使会话 agent 切换
/// 成为纯展示字段（docs/working/管道配置输入契约与动态管道能力设计_20260824.md §4.3）。
async fn resolve_dispatch_agent(
    registry: Option<&agentos_session::ConnectionRegistry>,
    store: Option<&Arc<dyn StorageBackend>>,
    thread_id: &str,
    explicit_agent: &str,
) -> String {
    if !explicit_agent.is_empty() {
        return explicit_agent.to_string();
    }
    if let Some(reg) = registry {
        if let Some(aid) = reg.get_agent_for_thread(thread_id) {
            return aid;
        }
    }
    if let Some(store) = store {
        match store.get_session(thread_id).await {
            Ok(Some(s)) => {
                if let Some(aid) = s.agent_id.filter(|a| !a.is_empty()) {
                    return aid;
                }
            }
            Ok(None) => {}
            Err(e) => {
                warn!(
                    thread_id = %thread_id,
                    error = %e,
                    "get_session 读取失败，agent 绑定解析回退默认 agentos"
                );
            }
        }
    }
    "agentos".to_string()
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

    /// 直接入链执行一轮（无 store 路径：消息不经队列，行为与旧 dispatch 一致）。
    async fn spawn_chain(state: &AppState, tenant: TenantContext, record: PendingInputRecord) {
        let registry = crate::run_chain::RunChainRegistry::global();
        let chain_key = record.route_id.clone();
        let chain_user = record.user_id.clone();
        registry.note_user_pipeline(&chain_user, &chain_key);
        let exec_state = state.clone();
        let exec_thread = record.thread.clone();
        let exec_user = record.user_id.clone();
        registry.enqueue(&chain_key, &chain_user, async move {
            Self::run_pipeline_round(&exec_state, &tenant, &exec_thread, &exec_user, record).await;
        });
    }

    /// 消费一条 pending 输入：发 stream_start + 引擎执行 + 推送结果（流式协议与
    /// 旧 dispatch 完全一致，参数从 rec 取——等待窗口内 PUT 的修改在此生效）。
    async fn run_pipeline_round(
        state: &AppState,
        tenant: &TenantContext,
        thread_id: &str,
        user_id: &str,
        rec: PendingInputRecord,
    ) {
        // 生成 assistant message_id（内核权威，sidecar chunk + new_message 共用）。
        // 激活时才发 stream_start + 确定 message_id（等待窗口内不占位气泡）。
        let message_id = format!("a_{}", uuid::Uuid::new_v4().simple());

        if let Some(session) = state.session.as_ref() {
            // 事件单播坐标注册：推送最终落点是 user 级 WS 连接，thread 只是
            // thread→user 反查索引（connection_registry）。前端 WS 已按当前会话
            // thread 注册；注入路径（触发器 chat.send_message）只持有管道唯一
            // 坐标（12hex pipeline_id）作派发键，若不注册则 send_to_thread 反查
            // 无 user、事件被丢弃——表现为「LLM 日志有、前端收不到」（2026-08-19）。
            // 幂等注册：派发键（thread_id）与 route_id 双坐标均直达 user。
            session.register_thread(thread_id, user_id);
            session.register_thread(&rec.route_id, user_id);
            let _ = session
                .emit_event(
                    thread_id,
                    "stream_start",
                    serde_json::json!({
                        "pipeline_id": rec.route_id,
                        "message_id": message_id,
                        "_threadId": thread_id,
                    }),
                )
                .await;
        }
        // 执行 agent：任务派发显式指定；主会话路径空串 → 按线程绑定解析
        // （registry → DB sessions.agent_id → agentos，2026-08-24 阶段1）。
        let exec_agent = if rec.agent_id.is_empty() {
            resolve_dispatch_agent(
                state.session.as_ref().map(|s| s.registry().as_ref()),
                state.store.as_ref(),
                thread_id,
                "",
            )
            .await
        } else {
            rec.agent_id.clone()
        };
        let exec_thread = thread_id.to_string();
        let exec_user = user_id.to_string();

        // 在租户上下文内执行管道。message_id 注入 state 供 sidecar 流式 chunk 携带
        // （sidecar on_chunk notify 时带上，前端据此把 chunk 路由到占位气泡）。
        let outcome = agentos_tenant::scope(
            tenant.clone(),
            crate::server::process_via_engine(
                state,
                &rec.content,
                &exec_agent,
                &[],
                &rec.route_id,
                &exec_thread,
                &message_id,
                &exec_user,
                &rec.thinking_strength,
                rec.execution_context.as_ref(),
                rec.state_overlay.as_ref(),
                &rec.client_message_id,
            ),
        )
        .await;

        let Some(session) = state.session.as_ref() else {
            tracing::warn!(thread = %exec_thread, "session 未启用，引擎结果无法推回前端");
            return;
        };

        // 失败路径：引擎执行失败（executor.run Err）→ stream_error 收尾，
        // 前端立即解除生成态（不再依赖 90s 强制收尾兜底）。
        // error 为统一错误信封（契约 streaming.json stream_error.error 锁 object，
        // 单一真值源 config/error_codes.json；ENGINE_RUN_FAILED 可重试）。
        if outcome.failed {
            let _ = session
                .emit_event(
                    &exec_thread,
                    "stream_error",
                    serde_json::json!({
                        "pipeline_id": rec.route_id,
                        "message_id": message_id,
                        "_threadId": exec_thread,
                        "error": {
                            "code": "ENGINE_RUN_FAILED",
                            "message": outcome.content,
                            "source": "kernel",
                            "retryable": true,
                            "details": null,
                            "request_id": null,
                        },
                    }),
                )
                .await;
            return;
        }

        // 成功路径：new_message 携带本轮最终 assistant 消息的完整持久形态
        // （content/reasoningContent/toolCalls/sequence），前端与 DB 加载共用
        // 同一个 mapper 生成 parts——流式事件与历史加载冷热同构。
        let seq = outcome
            .final_assistant
            .as_ref()
            .and_then(|m| m.get("seq").and_then(|v| v.as_u64()))
            .unwrap_or(1);
        let fa = outcome.final_assistant.clone().unwrap_or_else(
            || serde_json::json!({"role": "assistant", "content": outcome.content}),
        );
        // 认领回传：user 权威 record（id=compute_message_id 指纹 mc_/seq/内容/cmid）。
        // 表侧落库 id 与指纹一致（write_slot_to_table_locked 无 _message_id 注入时
        // 回落 compute_message_id），前端按 cmid 认领后记入独立 recordId 字段，
        // UI 寻址 id 保持前端 uuid 不变（ADR 2026-08-22 双字段范式）。
        let user_record = outcome.final_user.as_ref().map(|u| {
            serde_json::json!({
                "id": agentos_core::ids::compute_message_id(u),
                "content": u.get("content").cloned().unwrap_or(serde_json::Value::Null),
                "sequence": u.get("seq").and_then(|v| v.as_u64()).unwrap_or(0),
                "metadata": u.get("metadata").cloned().unwrap_or(serde_json::Value::Null),
            })
        });
        let delivered = session
            .emit_event(
                &exec_thread,
                "new_message",
                serde_json::json!({
                    "pipeline_id": rec.route_id,
                    "message_id": message_id,
                    "_threadId": exec_thread,
                    // 触发本轮的 user 消息幂等键（ADR 2026-08-21）：前端据此
                    // 精确认领对应乐观 user 消息（非 FIFO 猜测）。
                    "client_message_id": rec.client_message_id,
                    "sequence": seq,
                    "content": outcome.content,
                    "user_message": user_record,
                    "message": {
                        "id": message_id,
                        "role": fa["role"],
                        "content": outcome.content,
                        "sequence": seq,
                        "reasoningContent": fa.get("reasoning_content"),
                        "toolCalls": fa.get("tool_calls").unwrap_or(&serde_json::Value::Null),
                        "timestamp": chrono::Utc::now().to_rfc3339(),
                        // status 从消息 blob 读取：中断/错误半截消息落库时带
                        // interrupted/error，正常消息缺省 completed（冷热同构）。
                        "status": message_status_from_blob(&fa),
                        "thread_id": exec_thread,
                    },
                }),
            )
            .await;
        info!(
            thread = %exec_thread,
            delivered = delivered,
            "new_message 推送完成"
        );

        // stream_end 收尾：成功路径的终止信号（携带 final_sequence 同步占位 seq），
        // 前端 handleStreamEnd 据此终止流式状态并做最终合并。
        let _ = session
            .emit_event(
                &exec_thread,
                "stream_end",
                serde_json::json!({
                    "pipeline_id": rec.route_id,
                    "message_id": message_id,
                    "_threadId": exec_thread,
                    "final_sequence": seq,
                }),
            )
            .await;
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
        thinking_strength: &str,
        execution_context: Option<&serde_json::Value>,
        state_overlay: Option<&serde_json::Value>,
        agent_id: &str,
        client_message_id: &str,
        source: PendingInputSource,
    ) -> Result<(), String> {
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

        // ── pending 入队（ADR-2026-08-26）──
        // 消息先落持久化队列（pipeline_pending_inputs 表），入链的消费任务在
        // 链空闲时按 FIFO pop 并激活执行。等待窗口内条目可经 PUT/DELETE 修改删除；
        // 消费时从表读最新参数（内容不被闭包捕获）；重启后队列仍在，续跑。
        let record = PendingInputRecord {
            id: format!("p_{}", &uuid::Uuid::new_v4().simple().to_string()[..12]),
            pipeline_id: route_id.clone(),
            tenant_id: tenant.tenant_id.clone(),
            user_id: user_id.to_string(),
            content,
            thread: thread_id.to_string(),
            source,
            agent_id: agent_id.to_string(),
            route_id: route_id.clone(),
            thinking_strength: thinking_strength.to_string(),
            client_message_id: client_message_id.to_string(),
            execution_context: execution_context.cloned(),
            state_overlay: state_overlay.cloned(),
            created_at: chrono::Utc::now().to_rfc3339(),
        };
        // store 未注入（单测/兼容路径）：无持久化队列，直接入链执行本轮（旧行为）。
        let Some(store) = state.store.clone() else {
            Self::spawn_chain(&state, tenant, record).await;
            return Ok(());
        };
        if let Err(e) = store
            .enqueue_pending_input(&tenant.tenant_id, &route_id, &record)
            .await
        {
            return Err(format!("pending 输入入队失败: {e}"));
        }
        // 入队事件（pending_inputs_changed）：前端实时同步队列条。
        emit_pending_inputs_changed(
            &state,
            thread_id,
            &route_id,
            &tenant.tenant_id,
            &store,
            "enqueued",
        )
        .await;

        // ADR-2026-08-15：后台执行必须经 RunChainRegistry 入链——裸 spawn 会让
        // 同会话两条消息并发跑（registry 回写 / msg_sequence 竞态）。链保证同
        // 管道严格 FIFO、跨管道并行；user_id+route_id 兼作排队优先级键。
        // 消费任务在链空闲时 pop 队列：pop 到空即退出（无轮询无自续接）。
        let registry = crate::run_chain::RunChainRegistry::global();
        let chain_key = route_id.clone();
        let exec_chain_key = chain_key.clone();
        let exec_user_key = user_id.to_string();
        registry.note_user_pipeline(&exec_user_key, &chain_key);
        let exec_state = state.clone();
        let exec_thread = thread_id.to_string();
        let exec_user = user_id.to_string();
        let exec_tenant = tenant;
        registry.enqueue(&chain_key, &exec_user_key, async move {
            loop {
                let rec = match store
                    .pop_pending_input(&exec_tenant.tenant_id, &exec_chain_key)
                    .await
                {
                    Ok(Some(rec)) => rec,
                    Ok(None) => break, // 队列空：消费任务退出，链尾自清理
                    Err(e) => {
                        tracing::error!(
                            pipeline = %exec_chain_key,
                            error = %e,
                            "pending 消费出队失败（跳过本轮，队列残留待下轮）"
                        );
                        break;
                    }
                };
                // 消费事件：该条从队列条移入主消息流（前端据此同步）。
                emit_pending_inputs_changed(
                    &exec_state,
                    &exec_thread,
                    &exec_chain_key,
                    &exec_tenant.tenant_id,
                    &store,
                    "consumed",
                )
                .await;
                Self::run_pipeline_round(&exec_state, &exec_tenant, &exec_thread, &exec_user, rec)
                    .await;
            }
        });
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

        let inner = if response.is_object() {
            response
        } else {
            &serde_json::Value::Null
        };
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
            warn!(
                request_id = request_id,
                "invoker 未注入，跳过 interaction.respond"
            );
        }

        if let Some(db) = self.state.db.as_ref() {
            match db.find_suspended_run_by_request_id(request_id) {
                Ok(Some(run)) => {
                    // 0.2 收尾：旧引擎已清理，resume 即 runs 表状态簿记
                    // （新引擎执行流由 state.suspended 插件机制控制，此处恢复
                    // 状态供查询/复盘语义，与 capability pipeline-executor.resume 一致）。
                    match db
                        .update_run_status(
                            &run.run_id,
                            agentos_core::types::RunStatus::Running,
                            None,
                            None,
                        )
                        .await
                    {
                        Ok(()) => {
                            info!(run_id = %run.run_id, request_id = request_id, "interaction_response 已唤醒 suspended run")
                        }
                        Err(e) => {
                            warn!(request_id = request_id, error = %e, "suspended run resume 失败")
                        }
                    }
                }
                Ok(None) => tracing::debug!(
                    request_id = request_id,
                    "无 suspended run（LLM 直调路径），仅 interaction.respond"
                ),
                Err(e) => warn!(request_id = request_id, error = %e, "查找 suspended run 失败"),
            }
        }

        if respond_failed {
            Err(format!("interaction.respond 失败: request_id={request_id}"))
        } else {
            Ok(())
        }
    }

    async fn dispatch_stop(&self, thread_id: &str) -> Result<(), String> {
        // 停止 = 复用 suspend_pipeline 的落库路径（方案 §四.1，批次 C）：
        // 把该管道最新 Running run 置 Suspended —— 这是传输信号，不是终态。
        // llm_core 流式期间轮询到 suspended 后自行中断、落半截消息（status:
        // interrupted）+ 置 ended=true，引擎既有 ended 边界检查让 run 优雅收尾
        // （persist_run_end 覆写为 Completed）。无 Running run（run 已完成才点
        // 停止）时幂等空转，行为同现状。
        let Some(store) = self.state.store.as_ref() else {
            return Ok(());
        };
        // 租户解析：WS 路径注册了 thread→user 映射，反查 user 后按用户租户
        // 解析（run 落库在用户租户）；注册表无映射（冷启动/新连接未注册）回退
        // default，与 suspend_pipeline 能力（current_or_default）语义一致。
        let user_id = self
            .state
            .session
            .as_ref()
            .and_then(|s| s.registry().get_user_for_thread(thread_id));
        let tenant_id = match user_id {
            Some(uid) => crate::auth::resolve_tenant_id_by_user(Some(store), &uid).await,
            None => "default".to_string(),
        };
        // pipeline_id 复用 resolve 路径（thread 的真实主管道；stop 消息不带 pipeline_id）。
        let pipeline_id =
            resolve_pipeline_id_for_thread(Some(store), thread_id, "", &tenant_id).await;
        let runs = store
            .list_runs_by_pipeline(&pipeline_id, &tenant_id)
            .await
            .map_err(|e| format!("stop_generation 查询 run 失败: {e}"))?;
        let target = runs
            .into_iter()
            .find(|r| r.status == agentos_core::types::RunStatus::Running);
        if let Some(run) = target {
            store
                .update_run_status(
                    &run.run_id,
                    agentos_core::types::RunStatus::Suspended,
                    Some(&run.current_branch),
                    Some(run.current_seq),
                )
                .await
                .map_err(|e| format!("stop_generation 置 suspended 失败: {e}"))?;
            info!(
                thread = thread_id,
                run_id = %run.run_id,
                "stop_generation 已落地：run 置 suspended（信号，终态由引擎收尾）"
            );
        } else {
            debug!(
                thread = thread_id,
                pipeline = %pipeline_id,
                "stop_generation 无 running run（幂等空转）"
            );
        }
        Ok(())
    }

    async fn dispatch_active_thread(
        &self,
        user_id: &str,
        thread_id: &str,
        pipeline_id: &str,
    ) -> Result<(), String> {
        // 排队优先级策略键（ADR-2026-08-15）：活跃管道 = 用户当前选中的会话管道。
        // 前端传值经 resolve 校验（防残留旧值把优先级挪到别的管道），失败回退
        // 该 thread 的真实主管道；纯内存写入，即时返回不占收包循环。
        let tenant_id =
            crate::auth::resolve_tenant_id_by_user(self.state.store.as_ref(), user_id).await;
        let route_id = resolve_pipeline_id_for_thread(
            self.state.store.as_ref(),
            thread_id,
            pipeline_id,
            &tenant_id,
        )
        .await;
        crate::run_chain::RunChainRegistry::global().note_user_pipeline(user_id, &route_id);
        // 域事件插座：session.active_changed → 观察总线 + 声明订阅的插件。
        crate::plugin_lifecycle::broadcast_domain_event(
            &self.state,
            "session.active_changed",
            vec![
                ("session_id", serde_json::json!(thread_id)),
                ("pipeline_id", serde_json::json!(route_id.as_str())),
                ("user_id", serde_json::json!(user_id)),
            ],
        )
        .await;
        debug!(user = user_id, thread = thread_id, pipeline = %route_id, "活跃管道已更新");
        Ok(())
    }

    async fn dispatch_regenerate(
        &self,
        user_id: &str,
        thread_id: &str,
        pipeline_id: &str,
        user_message_id: &str,
        new_content: Option<&str>,
    ) -> Result<(), String> {
        // 重新生成原语（方案 §四.3，批次 D）：
        // 定位目标 user 消息 →（可选）set 改写内容 → 对其后全部槽位发 set(seq,null)
        // 截断 → 追加 patch_type='rollback' trace → 以显式 skip_user_append 标志重跑。
        // 重新生成=缺省最后一条 user；回退=指定更早 user；编辑重发=带 new_content。
        // 工具配对完整性：截断点 = 目标 user 消息边界，其后 assistant+tool 整体删除，
        // 剩余 history 配对天然完整（方案 §三 硬约束）。
        let Some(store) = self.state.store.as_ref() else {
            return Err("regenerate disabled: kernel store not injected".to_string());
        };
        let tenant_id = crate::auth::resolve_tenant_id_by_user(Some(store), user_id).await;
        let route_id =
            resolve_pipeline_id_for_thread(Some(store), thread_id, pipeline_id, &tenant_id).await;
        if route_id.is_empty() {
            return Err("regenerate 缺少 pipeline_id".into());
        }

        // 1. 读历史：目标 user 消息（按 message_id 定位；缺省 = 最后一条 user）。
        //    消息层按 (pipeline_id, tenant) 查询，tenant scope 由调用方
        //    （WS 路径）保证——store 实现经 task_local 解析租户。
        let messages = store
            .get_messages_by_pipeline(&route_id, MessageQueryOpts::default())
            .await
            .map_err(|e| format!("regenerate 读历史失败: {e}"))?;
        let target_seq = find_target_user_seq(&messages, user_message_id)
            .ok_or_else(|| format!("regenerate 未找到目标 user 消息: {user_message_id}"))?;

        // 2. 构造截断 ops：目标 user 之后的全部槽位 set(seq,null)（删槽留洞，
        //    context_window_guard 先例；后段 seq/id 不变）+（可选）改写目标内容。
        let mut ops: Vec<serde_json::Value> = Vec::new();
        if let Some(content) = new_content {
            if let Some(target) = messages.iter().find(|m| m.seq_in_branch == target_seq) {
                let mut msg = serde_json::json!({"role": "user", "content": content});
                if let Some(meta) = target.metadata.clone() {
                    msg["metadata"] = meta;
                }
                ops.push(serde_json::json!({"op": "set", "seq": target_seq, "msg": msg}));
            }
        }
        for m in &messages {
            if m.seq_in_branch > target_seq {
                ops.push(serde_json::json!({"op": "set", "seq": m.seq_in_branch, "msg": null}));
            }
        }

        // 3. 表侧落库（内存 state 在重跑时经热路径 registry 的截断后历史重建）。
        if !ops.is_empty() {
            store
                .apply_messages_ops_to_table(&route_id, &tenant_id, &ops)
                .await
                .map_err(|e| format!("regenerate 截断落库失败: {e}"))?;
        }

        // 4. rollback trace（append-only 审计痕：回退目标 + 补偿 ops 实录）。
        // run_id 复用目标消息所在 run——get_step_traces_by_thread 经 message_slots
        // 反查 run_id 集合，合成 id（rb_ 前缀）不在集合内会审计不可见。
        let trace_run_id = messages
            .iter()
            .find(|m| m.seq_in_branch == target_seq)
            .map(|m| m.run_id.clone())
            .filter(|r| !r.is_empty())
            .unwrap_or_else(|| format!("rb_{}", uuid::Uuid::new_v4().simple()));
        let now = chrono::Utc::now().to_rfc3339();
        let entry = TraceEntry {
            trace_id: format!("t_{}", uuid::Uuid::new_v4().simple()),
            run_id: trace_run_id,
            branch_id: "main".to_string(),
            seq_in_branch: 0,
            plugin_id: "chat_regenerate".to_string(),
            patch_type: PatchType::Rollback,
            patch_data: serde_json::json!({
                "rollback_to_user_seq": target_seq,
                "user_message_id": user_message_id,
                "pipeline_id": route_id,
                "messages": { "_ops": ops },
            }),
            created_at: now,
        };
        store
            .append_trace(entry)
            .await
            .map_err(|e| format!("regenerate rollback trace 失败: {e}"))?;

        // 5. 截断事件（与 new_message 同通道）：前端据此收敛本地乐观消息/占位。
        if let Some(session) = self.state.session.as_ref() {
            let _ = session
                .emit_event(
                    thread_id,
                    "messages_truncated",
                    serde_json::json!({
                        "pipeline_id": route_id,
                        "thread_id": thread_id,
                        "truncate_before_seq": target_seq + 1,
                        "regenerate": true,
                        "_threadId": thread_id,
                    }),
                )
                .await;
        }

        // 6. 重跑：目标 user 消息已在截断后历史中，跳过本轮 append。
        let content = new_content
            .map(|c| c.to_string())
            .or_else(|| {
                messages
                    .iter()
                    .find(|m| m.seq_in_branch == target_seq)
                    .and_then(|m| m.content_preview.clone())
            })
            .unwrap_or_default();
        let overlay = serde_json::json!({"_skip_user_append": true});
        // 复用 user_input 同款派发路径（pending 入队 → 链消费 → process_via_engine），
        // 保证与前端发送同一条 FIFO 链、同事件流。
        self.dispatch_user_input(
            thread_id,
            user_id,
            &content,
            &route_id,
            "",
            None,
            Some(&overlay),
            "",
            "",
            agentos_core::types::PendingInputSource::System,
        )
        .await
        .map_err(|e| format!("regenerate 重跑派发失败: {e}"))?;
        Ok(())
    }
}

/// 推送 `pending_inputs_changed` 事件（ADR-2026-08-26）：入队/消费/修改/删除时
/// 前端据 payload 的 items 全量列表同步队列条。事件坐标 = 派发 thread（与
/// new_message 同款单播）；session 未接线时静默跳过（无连接可推）。
async fn emit_pending_inputs_changed(
    state: &AppState,
    thread_id: &str,
    pipeline_id: &str,
    tenant_id: &str,
    store: &Arc<dyn agentos_core::traits::StorageBackend>,
    action: &str,
) {
    let Some(session) = state.session.as_ref() else {
        return;
    };
    let items = match store.list_pending_inputs(tenant_id, pipeline_id).await {
        Ok(rows) => rows
            .into_iter()
            .map(|r| {
                serde_json::json!({
                    "id": r.id,
                    "pipeline_id": r.pipeline_id,
                    "content": r.content,
                    "source": r.source,
                    "created_at": r.created_at,
                })
            })
            .collect::<Vec<_>>(),
        Err(e) => {
            tracing::warn!(
                pipeline = %pipeline_id,
                error = %e,
                "pending_inputs_changed 列表读取失败（事件跳过）"
            );
            Vec::new()
        }
    };
    let _ = session
        .emit_event(
            thread_id,
            "pending_inputs_changed",
            serde_json::json!({
                "pipeline_id": pipeline_id,
                "thread_id": thread_id,
                "action": action,
                "items": items,
            }),
        )
        .await;
}

/// 定位 regenerate 目标 user 消息的 seq（批次 D）。
///
/// user_message_id 非空：按 message_id 精确匹配（历史读路径 record_id 与表侧
/// 落库 id 一致——流式注入 message_id 或内容指纹）；空串：取最后一条 user 消息
/// （重新生成缺省语义）。只接受 role=user——截断边界必须是 user 消息（方案 §三
/// 工具配对完整性硬约束）。找不到返回 None（调用方报错，不静默截断）。
fn find_target_user_seq(
    messages: &[agentos_core::types::MessageRecord],
    user_message_id: &str,
) -> Option<u32> {
    if user_message_id.is_empty() {
        return messages
            .iter()
            .rev()
            .find(|m| m.role == "user")
            .map(|m| m.seq_in_branch);
    }
    messages
        .iter()
        .find(|m| m.role == "user" && m.message_id == user_message_id)
        .map(|m| m.seq_in_branch)
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
        match store
            .list_pipeline_ids_by_thread(thread_id, tenant_id)
            .await
        {
            Ok(pids) if pids.iter().any(|p| p == frontend_pipeline_id) => {
                return frontend_pipeline_id.to_string();
            }
            Ok(_) => {
                warn!(
                    thread_id = %thread_id,
                    frontend_pid = %frontend_pipeline_id,
                    "前端传来的 pipeline_id 不属于该 thread（可能残留旧会话值），改用 thread 真实主管道"
                );
            }
            // 存储故障 ≠ 无关联：报错可见，不得兜成空列表误诊为"前端值不属于该 thread"。
            // 且不再回落主管道改写 route_id（2026-08-22 裁决）——用户在子管道视图
            // 发消息时路由落主管道 = 写错桶；校验不可用即透传前端值（前端已是
            // 权威 activePipelineId 源头），让故障期间路由保持用户所见。
            Err(e) => {
                warn!(
                    thread_id = %thread_id,
                    frontend_pid = %frontend_pipeline_id,
                    error = %e,
                    "list_pipeline_ids_by_thread 查询失败，校验不可用，透传前端 pipeline_id（不回落主管道）"
                );
                return frontend_pipeline_id.to_string();
            }
        }
    }

    // ② 取该 thread 的真实 active_pipeline_id
    let tenant =
        agentos_core::types::TenantContext::new(tenant_id.to_string(), thread_id.to_string());
    let tid = thread_id.to_string();
    let store_clone = store.clone();
    let session =
        match agentos_tenant::scope(tenant, async move { store_clone.get_session(&tid).await })
            .await
        {
            Ok(s) => s,
            Err(e) => {
                warn!(
                    thread_id = %thread_id,
                    error = %e,
                    "get_session 读取失败，active_pipeline_id 解析不可用"
                );
                None
            }
        };
    if let Some(active) = session.and_then(|s| s.active_pipeline_id) {
        if !active.is_empty() {
            return active;
        }
    }

    // ③ 回退 thread_id（兼容）
    thread_id.to_string()
}

/// new_message 事件 message.status 的取值规则：从消息 blob（final_assistant）
/// 读取——中断/错误半截消息落库时带 interrupted/error，正常消息缺省 completed。
fn message_status_from_blob(fa: &serde_json::Value) -> serde_json::Value {
    fa.get("status")
        .cloned()
        .unwrap_or(serde_json::json!("completed"))
}

#[cfg(test)]
mod tests {
    use super::find_target_user_seq;
    use super::message_status_from_blob;
    use super::resolve_dispatch_agent;
    use super::resolve_pipeline_id_for_thread;
    use super::EngineDispatcher;
    use agentos_core::traits::StorageBackend;
    use agentos_core::traits::{MessageQueryOpts, SessionListFilter};
    use agentos_core::types::{
        Branch, MessageRecord, RunRecord, RunStatus, SessionRecord, StorageError, TraceEntry,
    };
    use agentos_session::router::PipelineDispatcher;
    use async_trait::async_trait;
    use serde_json::json;
    use std::sync::{Arc, Mutex};

    /// resolve_pipeline_id_for_thread 行为测试专用 mock：
    /// 只控制 list_pipeline_ids_by_thread（Err / 成员列表）与 get_session（活跃管道），
    /// 其余走 stub（契约 mock 同款）。
    struct ResolveMock {
        list_result: Mutex<Result<Vec<String>, StorageError>>,
        session: Mutex<Option<SessionRecord>>,
    }

    impl ResolveMock {
        fn new(
            list_result: Result<Vec<String>, StorageError>,
            session: Option<SessionRecord>,
        ) -> Self {
            Self {
                list_result: Mutex::new(list_result),
                session: Mutex::new(session),
            }
        }
    }

    fn session_record(active_pipeline_id: Option<&str>) -> SessionRecord {
        SessionRecord {
            thread_id: "T1".to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: active_pipeline_id.map(|s| s.to_string()),
            pipeline_ids: vec![],
            metadata: None,
            created_at: "2026-08-22T00:00:00Z".to_string(),
            updated_at: "2026-08-22T00:00:00Z".to_string(),
            last_active_at: None,
        }
    }

    #[async_trait]
    impl StorageBackend for ResolveMock {
        async fn get_run(&self, _run_id: &str) -> Result<RunRecord, StorageError> {
            Err(StorageError::NotFound("mock".to_string()))
        }
        async fn get_messages_by_pipeline(
            &self,
            _pipeline_id: &str,
            _opts: MessageQueryOpts,
        ) -> Result<Vec<MessageRecord>, StorageError> {
            Ok(vec![])
        }
        async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, StorageError> {
            Ok(vec![1, 2, 3])
        }
        async fn append_trace(&self, _entry: TraceEntry) -> Result<(), StorageError> {
            Ok(())
        }
        async fn create_branch(&self, _branch: Branch) -> Result<(), StorageError> {
            Ok(())
        }
        async fn update_run_status(
            &self,
            _run_id: &str,
            _status: RunStatus,
            _branch: Option<&str>,
            _seq: Option<u32>,
        ) -> Result<(), StorageError> {
            Ok(())
        }
        async fn create_run(
            &self,
            _run_id: &str,
            _config_hash: &str,
            _tenant_id: &str,
        ) -> Result<(), StorageError> {
            Ok(())
        }
        async fn store_blob(&self, _data: &[u8], _mime_type: &str) -> Result<String, StorageError> {
            Ok("mock_blob".to_string())
        }
        async fn create_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
            Ok(())
        }
        async fn get_session(
            &self,
            _thread_id: &str,
        ) -> Result<Option<SessionRecord>, StorageError> {
            Ok(self.session.lock().unwrap().clone())
        }
        async fn list_sessions(
            &self,
            _filter: SessionListFilter,
        ) -> Result<Vec<SessionRecord>, StorageError> {
            Ok(vec![])
        }
        async fn update_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
            Ok(())
        }
        async fn delete_session(&self, _thread_id: &str) -> Result<(), StorageError> {
            Ok(())
        }
        async fn link_pipeline_session(
            &self,
            _pipeline_id: &str,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<(), StorageError> {
            Ok(())
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<String>, StorageError> {
            self.list_result.lock().unwrap().clone()
        }
        async fn get_step_traces_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<TraceEntry>, StorageError> {
            Ok(vec![])
        }
        async fn create_user(
            &self,
            _user: &agentos_core::types::UserRecord,
        ) -> Result<(), StorageError> {
            Ok(())
        }
        async fn get_user_by_id(
            &self,
            _user_id: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, StorageError> {
            Ok(None)
        }
        async fn get_user_by_username(
            &self,
            _username: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, StorageError> {
            Ok(None)
        }
        async fn list_users(&self) -> Result<Vec<agentos_core::types::UserRecord>, StorageError> {
            Ok(Vec::new())
        }
        async fn update_last_login(&self, _user_id: &str) -> Result<(), StorageError> {
            Ok(())
        }
        async fn delete_user(&self, _user_id: &str) -> Result<bool, StorageError> {
            Ok(false)
        }
    }

    async fn resolve(mock: ResolveMock, thread_id: &str, frontend_pipeline_id: &str) -> String {
        let store = Arc::new(mock) as Arc<dyn StorageBackend>;
        resolve_pipeline_id_for_thread(Some(&store), thread_id, frontend_pipeline_id, "tenant-1")
            .await
    }

    #[test]
    fn message_status_reads_from_blob() {
        // blob 带 status（中断/错误半截消息）→ 原样透传
        let interrupted =
            serde_json::json!({"role": "assistant", "content": "半截", "status": "interrupted"});
        assert_eq!(
            message_status_from_blob(&interrupted),
            serde_json::json!("interrupted")
        );
        let errored = serde_json::json!({"role": "assistant", "content": "x", "status": "error"});
        assert_eq!(
            message_status_from_blob(&errored),
            serde_json::json!("error")
        );
    }

    #[test]
    fn message_status_defaults_to_completed_without_blob_status() {
        // 正常消息 blob 无 status → completed（既有行为不变）
        let plain = serde_json::json!({"role": "assistant", "content": "ok"});
        assert_eq!(
            message_status_from_blob(&plain),
            serde_json::json!("completed")
        );
    }

    // ── dispatch_stop：停止 = 复用 suspend_pipeline 落库路径（批次 C）──
    // 传输信号（Suspended → llm_core 轮询感知中断），不是终态。

    fn stop_state(store: Arc<dyn StorageBackend>) -> EngineDispatcher {
        let mut state = crate::routes::AppState::new();
        state.store = Some(store);
        EngineDispatcher::new(state)
    }

    async fn stop_running(store: Arc<dyn StorageBackend>, thread_id: &str) -> Result<(), String> {
        stop_state(store).dispatch_stop(thread_id).await
    }

    #[tokio::test]
    async fn dispatch_stop_suspends_latest_running_run() {
        // 有 running run：按 thread 主管道定位并置 Suspended（传输信号）。
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap())
            as Arc<dyn StorageBackend>;
        store.create_run("run-1", "cfg", "default").await.unwrap();
        store.set_run_pipeline("run-1", "p1").await.unwrap();
        store
            .create_session(&session_record(Some("p1")))
            .await
            .unwrap();

        stop_running(store.clone(), "T1").await.unwrap();

        let run = store.get_run("run-1").await.unwrap();
        assert_eq!(
            run.status,
            RunStatus::Suspended,
            "stop 应把最新 running run 置 suspended"
        );
    }

    #[tokio::test]
    async fn dispatch_stop_no_running_run_is_idempotent_noop() {
        // run 已完成才点停止（竞态）：无 Running run → 幂等空转，不报错、不动已完成 run。
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap())
            as Arc<dyn StorageBackend>;
        store.create_run("run-1", "cfg", "default").await.unwrap();
        store.set_run_pipeline("run-1", "p1").await.unwrap();
        store
            .update_run_status("run-1", RunStatus::Completed, None, None)
            .await
            .unwrap();
        store
            .create_session(&session_record(Some("p1")))
            .await
            .unwrap();

        stop_running(store.clone(), "T1").await.unwrap();

        let run = store.get_run("run-1").await.unwrap();
        assert_eq!(
            run.status,
            RunStatus::Completed,
            "已完成 run 不被 stop 改写"
        );
    }

    // ── dispatch_regenerate：截断 ops + rollback trace + messages_truncated 事件
    //    + skip_user_append 重跑（批次 D 原语）──
    // 真实 SqliteStore + 真实 SessionCoordinator（关键路径走真实依赖，非全 mock）。

    /// 捕获型 EventSink：记录收到的全部文本帧（与 chat_send_handler 测试同款）。
    struct CapturingSink {
        frames: Arc<Mutex<Vec<String>>>,
    }

    #[async_trait]
    impl agentos_session::EventSink for CapturingSink {
        async fn send_text(&self, text: &str) -> bool {
            self.frames.lock().unwrap().push(text.to_string());
            true
        }
        fn id(&self) -> u64 {
            7
        }
    }

    /// 预置 3 槽位历史（user/assistant/tool 配对完整）：seq 0 user、1 assistant
    /// （带 tool_calls）、2 tool（配 seq1 的调用）。截断目标 = seq 0。
    async fn seed_three_slot_history(store: &Arc<dyn StorageBackend>) {
        let user = json!({"role": "user", "content": "第一问"});
        let assistant = json!({
            "role": "assistant", "content": "旧回复",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "bash_execute", "arguments": "{}"}}]
        });
        let tool = json!({
            "role": "tool", "tool_call_id": "call_1",
            "content": "{\"success\":true,\"data\":\"ok\"}", "tool_result": {"success": true}
        });
        // 引擎真实链路 merge_and_project 会给每个 op 注入 _run_id（表侧
        // write_slot_to_table_locked 写入 message_slots.run_id）——预置模拟同款，
        // get_step_traces_by_thread 才能经消息反查到 run 集合。
        store
            .apply_messages_ops_to_table(
                "reg1",
                "default",
                &[
                    json!({"op": "set", "seq": 0, "msg": user, "_run_id": "run-reg1"}),
                    json!({"op": "set", "seq": 1, "msg": assistant, "_run_id": "run-reg1"}),
                    json!({"op": "set", "seq": 2, "msg": tool, "_run_id": "run-reg1"}),
                ],
            )
            .await
            .unwrap();
        store
            .create_run("run-reg1", "cfg", "default")
            .await
            .unwrap();
        store.set_run_pipeline("run-reg1", "reg1").await.unwrap();
        store
            .create_session(&session_record(Some("reg1")))
            .await
            .unwrap();
        // get_step_traces_by_thread 经 pipeline_sessions 映射反查 pipeline_ids
        store
            .link_pipeline_session("reg1", "T1", "default")
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn regenerate_truncates_after_target_and_appends_rollback_trace() {
        // 回退到 seq 0：其后全部槽位（1,2）置空；rollback trace 落库（run_id 复用
        // 消息所在 run，审计可反查）；messages_truncated 事件直达前端连接。
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let store: Arc<dyn StorageBackend> = sqlite.clone();
        seed_three_slot_history(&store).await;
        // 目标 id 用读回的真实 record_id（写表时无 _message_id 注入 = 内容指纹）
        let user_id = store
            .get_messages_by_pipeline("reg1", MessageQueryOpts::default())
            .await
            .unwrap()
            .into_iter()
            .find(|m| m.seq_in_branch == 0)
            .map(|m| m.message_id)
            .unwrap();

        // 完整接线：SessionCoordinator + 注册线程 + 捕获 sink + AppState
        let coordinator = Arc::new(agentos_session::SessionCoordinator::new());
        let frames = Arc::new(Mutex::new(Vec::<String>::new()));
        coordinator.register_thread("T1", "u1");
        coordinator.register(
            "u1",
            Arc::new(CapturingSink {
                frames: frames.clone(),
            }),
        );
        let mut state = crate::routes::AppState::new();
        state.store = Some(store.clone());
        state.session = Some(coordinator.clone());
        let dispatcher = EngineDispatcher::new(state);

        dispatcher
            .dispatch_regenerate("u1", "T1", "reg1", &user_id, None)
            .await
            .unwrap();

        // 表侧截断：seq 0 保留，1/2 成洞
        let rows = sqlite
            .get_slot_messages_by_pipeline("reg1", "default", MessageQueryOpts::default())
            .unwrap();
        let seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
        assert_eq!(seqs, vec![0], "目标 user 之后的槽位全部截断");
        assert_eq!(rows[0].content_preview.as_deref(), Some("第一问"));

        // 事件直达：messages_truncated（与 new_message 同通道）
        let truncated: Vec<String> = {
            let emitted = frames.lock().unwrap();
            emitted
                .iter()
                .filter(|f| f.contains("\"messages_truncated\""))
                .cloned()
                .collect()
        };
        assert_eq!(truncated.len(), 1, "截断事件必须发出");
        let payload: serde_json::Value = serde_json::from_str(&truncated[0]).unwrap();
        assert_eq!(payload["type"], "messages_truncated");
        assert_eq!(payload["data"]["pipeline_id"], "reg1");
        assert_eq!(payload["data"]["truncate_before_seq"], 1);

        // rollback trace：patch_type=rollback，run_id 复用消息 run（审计反查可见）
        let traces = store
            .get_step_traces_by_thread("T1", "default")
            .await
            .unwrap();
        let rollbacks: Vec<&TraceEntry> = traces
            .iter()
            .filter(|t| t.patch_type == agentos_core::types::PatchType::Rollback)
            .collect();
        assert_eq!(rollbacks.len(), 1, "恰好一条 rollback 轨迹");
        assert_eq!(rollbacks[0].run_id, "run-reg1", "run_id 复用消息所在 run");
        assert_eq!(rollbacks[0].plugin_id, "chat_regenerate");
        assert_eq!(rollbacks[0].patch_data["rollback_to_user_seq"], 0);
    }

    #[tokio::test]
    async fn regenerate_default_target_is_last_user_message() {
        // 缺省 user_message_id：定位最后一条 user（seq 2 场景：user/assistant/user）
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap())
            as Arc<dyn StorageBackend>;
        store
            .apply_messages_ops_to_table(
                "reg2",
                "default",
                &[
                    json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "第一问"}}),
                    json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "旧回复"}}),
                    json!({"op": "set", "seq": 2, "msg": {"role": "user", "content": "第二问"}}),
                    json!({"op": "set", "seq": 3, "msg": {"role": "assistant", "content": "旧回复2"}}),
                ],
            )
            .await
            .unwrap();
        store.create_run("reg2", "cfg", "default").await.unwrap();
        store.set_run_pipeline("reg2", "reg2").await.unwrap();
        store
            .create_session(&session_record(Some("reg2")))
            .await
            .unwrap();

        let mut state = crate::routes::AppState::new();
        state.store = Some(store.clone());
        let dispatcher = EngineDispatcher::new(state);
        dispatcher
            .dispatch_regenerate("u1", "T1", "", "", None)
            .await
            .unwrap();

        let rows = store
            .get_messages_by_pipeline("reg2", MessageQueryOpts::default())
            .await
            .unwrap();
        let seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
        assert_eq!(seqs, vec![0, 1, 2], "缺省=最后一条 user（seq 2），其后截断");
        assert_eq!(rows[2].role, "user");
        assert_eq!(rows[2].content_preview.as_deref(), Some("第二问"));
    }

    #[tokio::test]
    async fn regenerate_edit_resend_rewrites_content() {
        // 编辑重发：目标 user 消息内容改写（metadata 保留），其后截断
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap())
            as Arc<dyn StorageBackend>;
        store
            .apply_messages_ops_to_table(
                "reg3",
                "default",
                &[
                    json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "旧问题",
                                                           "metadata": {"client_message_id": "cmid-1"}}}),
                    json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "旧回复"}}),
                ],
            )
            .await
            .unwrap();
        store.create_run("reg3", "cfg", "default").await.unwrap();
        store.set_run_pipeline("reg3", "reg3").await.unwrap();
        store
            .create_session(&session_record(Some("reg3")))
            .await
            .unwrap();

        let mut state = crate::routes::AppState::new();
        state.store = Some(store.clone());
        let dispatcher = EngineDispatcher::new(state);
        dispatcher
            .dispatch_regenerate("u1", "T1", "", "", Some("改写后的问题"))
            .await
            .unwrap();

        let rows = store
            .get_messages_by_pipeline("reg3", MessageQueryOpts::default())
            .await
            .unwrap();
        assert_eq!(rows.len(), 1, "assistant 槽位被截断");
        assert_eq!(rows[0].content_preview.as_deref(), Some("改写后的问题"));
        assert_eq!(
            rows[0]
                .metadata
                .as_ref()
                .and_then(|m| m.get("client_message_id"))
                .and_then(|v| v.as_str()),
            Some("cmid-1"),
            "改写保留原 metadata（cmid 幂等键）"
        );
    }

    #[tokio::test]
    async fn regenerate_unknown_target_returns_error() {
        // 目标 user 消息不存在 → 报错不截断（不静默清空）
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap())
            as Arc<dyn StorageBackend>;
        store
            .apply_messages_ops_to_table(
                "reg4",
                "default",
                &[json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "第一问"}})],
            )
            .await
            .unwrap();
        store.create_run("reg4", "cfg", "default").await.unwrap();
        store.set_run_pipeline("reg4", "reg4").await.unwrap();
        store
            .create_session(&session_record(Some("reg4")))
            .await
            .unwrap();

        let mut state = crate::routes::AppState::new();
        state.store = Some(store.clone());
        let dispatcher = EngineDispatcher::new(state);
        let err = dispatcher
            .dispatch_regenerate("u1", "T1", "reg4", "no_such_id", None)
            .await
            .unwrap_err();
        assert!(err.contains("未找到目标 user 消息"), "err: {err}");

        // 槽位原样（未被误截断）
        let rows = store
            .get_messages_by_pipeline("reg4", MessageQueryOpts::default())
            .await
            .unwrap();
        assert_eq!(rows.len(), 1);
    }

    #[tokio::test]
    async fn find_target_user_seq_prefers_id_else_last_user() {
        // 定位原语：id 精确匹配（只接受 role=user）vs 缺省最后一条 user
        let mk = |id: &str, role: &str, seq: u32| agentos_core::types::MessageRecord {
            message_id: id.to_string(),
            run_id: "r".to_string(),
            branch_id: "main".to_string(),
            seq_in_branch: seq,
            role: role.to_string(),
            blob_id: None,
            content_preview: None,
            created_at: "".to_string(),
            pipeline_id: None,
            tool_calls_json: None,
            tool_call_id: None,
            reasoning_content: None,
            status: None,
            error: None,
            tool_result_json: None,
            metadata: None,
        };
        let msgs = vec![
            mk("m0", "user", 0),
            mk("a1", "assistant", 1),
            mk("m2", "user", 2),
            mk("a3", "assistant", 3),
        ];
        assert_eq!(
            find_target_user_seq(&msgs, "m0"),
            Some(0),
            "显式 id 定位早期 user"
        );
        assert_eq!(
            find_target_user_seq(&msgs, ""),
            Some(2),
            "缺省=最后一条 user（跳过尾部 assistant）"
        );
        assert_eq!(
            find_target_user_seq(&msgs, "a1"),
            None,
            "assistant id 不是合法截断边界"
        );
        assert_eq!(find_target_user_seq(&msgs, "ghost"), None);
    }

    #[tokio::test]
    async fn list_error_returns_frontend_value_not_main_pipeline() {
        // 存储故障时不得把子管道 route_id 改写为主管道（2026-08-22 裁决）。
        // 旧实现：Err 与"不属于"混流后回落 session.active_pipeline_id → P-main（写错桶）。
        let mock = ResolveMock::new(
            Err(StorageError::NotFound("mock query failure".to_string())),
            Some(session_record(Some("P-main"))),
        );
        assert_eq!(resolve(mock, "T1", "P-sub").await, "P-sub");
    }

    #[tokio::test]
    async fn frontend_not_member_falls_to_main_pipeline() {
        // 前端值不属于该 thread（正常校验失败）→ 仍回落主管道（既有防御语义不变）
        let mock = ResolveMock::new(
            Ok(vec!["P-other".to_string()]),
            Some(session_record(Some("P-main"))),
        );
        assert_eq!(resolve(mock, "T1", "P-sub").await, "P-main");
    }

    #[tokio::test]
    async fn frontend_member_trusted() {
        let mock = ResolveMock::new(
            Ok(vec!["P-sub".to_string(), "P-main".to_string()]),
            Some(session_record(Some("P-main"))),
        );
        assert_eq!(resolve(mock, "T1", "P-sub").await, "P-sub");
    }

    #[tokio::test]
    async fn empty_frontend_uses_main_pipeline() {
        let mock = ResolveMock::new(Ok(vec![]), Some(session_record(Some("P-main"))));
        assert_eq!(resolve(mock, "T1", "").await, "P-main");
    }

    #[tokio::test]
    async fn no_session_falls_back_to_thread_id() {
        let mock = ResolveMock::new(Ok(vec![]), None);
        assert_eq!(resolve(mock, "T1", "").await, "T1");
    }

    // ── resolve_dispatch_agent：执行 agent 解析（2026-08-24 阶段1）──
    // 优先级：显式传入（任务派发）→ registry 线程绑定（热）→ DB sessions.agent_id（冷）→ agentos

    fn session_with_agent(agent_id: Option<&str>) -> SessionRecord {
        let mut s = session_record(None);
        s.agent_id = agent_id.map(|a| a.to_string());
        s
    }

    async fn resolve_agent(
        registry: Option<&agentos_session::ConnectionRegistry>,
        mock: Option<ResolveMock>,
        explicit: &str,
    ) -> String {
        let store = mock.map(|m| Arc::new(m) as Arc<dyn StorageBackend>);
        resolve_dispatch_agent(registry, store.as_ref(), "T1", explicit).await
    }

    #[tokio::test]
    async fn dispatch_agent_explicit_wins_over_binding() {
        // 任务派发显式传 agent（非空）→ 不被线程绑定覆盖（chat.send_message 路径语义）
        let registry = agentos_session::ConnectionRegistry::new();
        registry.register_thread_agent("T1", "general_agent");
        let mock = ResolveMock::new(Ok(vec![]), Some(session_with_agent(Some("general_agent"))));
        assert_eq!(
            resolve_agent(Some(&registry), Some(mock), "code_writer_agent").await,
            "code_writer_agent"
        );
    }

    #[tokio::test]
    async fn dispatch_agent_hot_registry_binding_used() {
        // 热路径：registry 线程绑定优先于 DB（会话编辑后即生效）
        let registry = agentos_session::ConnectionRegistry::new();
        registry.register_thread_agent("T1", "general_agent");
        let mock = ResolveMock::new(Ok(vec![]), Some(session_with_agent(Some("agentos"))));
        assert_eq!(
            resolve_agent(Some(&registry), Some(mock), "").await,
            "general_agent"
        );
    }

    #[tokio::test]
    async fn dispatch_agent_cold_db_fallback_used() {
        // 冷路径（registry 无绑定，如内核重启后）：DB sessions.agent_id 兜底
        let registry = agentos_session::ConnectionRegistry::new();
        let mock = ResolveMock::new(Ok(vec![]), Some(session_with_agent(Some("general_agent"))));
        assert_eq!(
            resolve_agent(Some(&registry), Some(mock), "").await,
            "general_agent"
        );
    }

    #[tokio::test]
    async fn dispatch_agent_empty_or_missing_db_binding_falls_to_agentos() {
        // DB 绑定为空串或 None → 兜底主 agent
        let registry = agentos_session::ConnectionRegistry::new();
        let mock = ResolveMock::new(Ok(vec![]), Some(session_with_agent(Some(""))));
        assert_eq!(
            resolve_agent(Some(&registry), Some(mock), "").await,
            "agentos"
        );
        let mock2 = ResolveMock::new(Ok(vec![]), Some(session_with_agent(None)));
        assert_eq!(
            resolve_agent(Some(&registry), Some(mock2), "").await,
            "agentos"
        );
    }

    #[tokio::test]
    async fn dispatch_agent_no_registry_no_db_defaults_agentos() {
        let mock = ResolveMock::new(Ok(vec![]), None);
        assert_eq!(resolve_agent(None, Some(mock), "").await, "agentos");
    }
}
