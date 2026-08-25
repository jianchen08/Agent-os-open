//! WebSocket 会话接入——把 session crate 接到 axum WS（ADR §7.2 P2 接线）。
//!
//! 职责：
//! - [`WsSink`]：axum WebSocket 的 `EventSink` 适配（经 mpsc 通道转发，避免
//!   `SplitSink` 跨 await 共享问题）；
//! - [`run_ws_session`]：握手鉴权 → 注册连接 → 入站路由（user_input/interaction/
//!   stop/heartbeat）→ 出站通道排空到 socket。断线重连由 SessionCoordinator 统一处理。

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use agentos_core::traits::StorageBackend;
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
            // 事件单播坐标注册：推送最终落点是 user 级 WS 连接，thread 只是
            // thread→user 反查索引（connection_registry）。前端 WS 已按当前会话
            // thread 注册；注入路径（触发器 chat.send_message）只持有管道唯一
            // 坐标（32hex pipeline_id）作派发键，若不注册则 send_to_thread 反查
            // 无 user、事件被丢弃——表现为「LLM 日志有、前端收不到」（2026-08-19）。
            // 幂等注册：派发键（thread_id）与 route_id 双坐标均直达 user。
            session.register_thread(thread_id, user_id);
            session.register_thread(&route_id, user_id);
            // 1. stream_start 提前发（在引擎执行前），让前端先建立占位气泡
            let _ = session
                .emit_event(
                    thread_id,
                    "stream_start",
                    serde_json::json!({
                        "pipeline_id": route_id,
                        "message_id": message_id,
                        "_threadId": thread_id,
                    }),
                )
                .await;
        }

        // 引擎执行移入后台任务：user_input 若内联 await 在 WS 入站循环上，
        // human_interaction 等阻塞工具挂起等待用户响应期间（timeout 最长 86400s），
        // 同连接后续的 interaction_response / stop_generation / 心跳全部无人读——
        // 前端表现为「点击选项无反应（消息已发出但内核没读）、工具卡片永久转圈、
        // 连接 ping 超时被踢后靠重连碰运气」。stream_start 已同步发出；引擎结果
        // 本就经事件流回推（stream_error / new_message / stream_end），调用方
        // 不依赖执行完成。
        let exec_state = state.clone();
        let exec_thread = thread_id.to_string();
        let exec_user = user_id.to_string();
        let exec_thinking = thinking_strength.to_string();
        let exec_ctx = execution_context.cloned();
        // GAP-1 阶段 1：自由 state overlay（chat.send_message 的 state 参数 +
        // 引擎写入的 lineage 扁平键）透传给引擎，并入 initial_state。
        let exec_overlay = state_overlay.cloned();
        // 执行 agent（任务派发显式指定；主会话路径空串 → 按线程绑定解析，
        // registry → DB sessions.agent_id → agentos，2026-08-24 阶段1）：
        // 决定引擎加载的 agent 配置（人格/tool_ids/技能）。
        let exec_agent = resolve_dispatch_agent(
            state.session.as_ref().map(|s| s.registry().as_ref()),
            state.store.as_ref(),
            &exec_thread,
            &agent_id,
        )
        .await;
        // 前端幂等键（ADR 2026-08-21）：随 user 消息 metadata 落库回显。
        let exec_cmid = client_message_id.to_string();
        // ADR-2026-08-15：后台执行必须经 RunChainRegistry 入链——裸 spawn 会让
        // 同会话两条消息并发跑（registry 回写 / msg_sequence 竞态）。链保证同
        // 管道严格 FIFO、跨管道并行；user_id+route_id 兼作排队优先级键（用户
        // 当前选中的管道优先获得全局并发槽位，此处 user_input 派发兼作兜底：
        // 未收到 active_thread_changed 通知时以最近发消息的管道为活跃管道）。
        let registry = crate::run_chain::RunChainRegistry::global();
        let chain_key = route_id.clone();
        registry.note_user_pipeline(user_id, &chain_key);
        registry.enqueue(&chain_key, user_id, async move {
            // 在租户上下文内执行管道。message_id 注入 state 供 sidecar 流式 chunk 携带
            // （sidecar on_chunk notify 时带上，前端据此把 chunk 路由到占位气泡）。
            let outcome = agentos_tenant::scope(
                tenant,
                crate::server::process_via_engine(
                    &exec_state,
                    &content,
                    &exec_agent,
                    &[],
                    &route_id,
                    &exec_thread,
                    &message_id,
                    &exec_user,
                    &exec_thinking,
                    exec_ctx.as_ref(),
                    exec_overlay.as_ref(),
                    &exec_cmid,
                ),
            )
            .await;

            let Some(session) = exec_state.session.as_ref() else {
                tracing::warn!(thread = %exec_thread, "session 未启用，引擎结果无法推回前端");
                return;
            };

            // 失败路径：引擎执行失败（executor.run Err）→ stream_error 收尾，
            // 前端立即解除生成态（不再依赖 90s 强制收尾兜底）。
            if outcome.failed {
                let _ = session
                    .emit_event(
                        &exec_thread,
                        "stream_error",
                        serde_json::json!({
                            "pipeline_id": route_id,
                            "message_id": message_id,
                            "_threadId": exec_thread,
                            "error": outcome.content,
                        }),
                    )
                    .await;
                return;
            }

            // 成功路径：new_message 携带本轮最终 assistant 消息的完整持久形态
            // （content/reasoningContent/toolCalls/sequence），前端与 DB 加载共用
            // 同一个 mapper 生成 parts——流式事件与历史加载冷热同构。
            // 无 assistant 产出（如仅工具调用未落文本）时回退纯 content 形态。
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
                        "pipeline_id": route_id,
                        "message_id": message_id,
                        "_threadId": exec_thread,
                        // 触发本轮的 user 消息幂等键（ADR 2026-08-21）：前端据此
                        // 精确认领对应乐观 user 消息（非 FIFO 猜测）。
                        "client_message_id": exec_cmid,
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
                            "status": "completed",
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
                        "pipeline_id": route_id,
                        "message_id": message_id,
                        "_threadId": exec_thread,
                        "final_sequence": seq,
                    }),
                )
                .await;
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
        // 取消生成：引擎层 cancel 接入待后续阶段
        info!(
            thread = thread_id,
            "stop_generation 已接收（引擎取消接入待 P3+）"
        );
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

#[cfg(test)]
mod tests {
    use super::resolve_dispatch_agent;
    use super::resolve_pipeline_id_for_thread;
    use agentos_core::traits::StorageBackend;
    use agentos_core::traits::{MessageQueryOpts, SessionListFilter};
    use agentos_core::types::{
        Branch, MessageRecord, RunRecord, RunStatus, SessionRecord, StorageError, TraceEntry,
    };
    use async_trait::async_trait;
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
