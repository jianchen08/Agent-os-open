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
    // B3：每连接一次的回放标志——首个带 thread_id 的入站消息触发 replay_missed。
    let replayed_for_task = Arc::new(std::sync::atomic::AtomicBool::new(false));
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
                    "agentos",
                    &[],
                    &route_id,
                    &exec_thread,
                    &message_id,
                    &exec_user,
                    &exec_thinking,
                    exec_ctx.as_ref(),
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
            let delivered = session
                .emit_event(
                    &exec_thread,
                    "new_message",
                    serde_json::json!({
                        "pipeline_id": route_id,
                        "message_id": message_id,
                        "_threadId": exec_thread,
                        "sequence": seq,
                        "content": outcome.content,
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
                    let _meta = run.metadata.clone().unwrap_or_default();
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
    let tenant =
        agentos_core::types::TenantContext::new(tenant_id.to_string(), thread_id.to_string());
    let tid = thread_id.to_string();
    let store_clone = store.clone();
    let session = agentos_tenant::scope(tenant, async move { store_clone.get_session(&tid).await })
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
