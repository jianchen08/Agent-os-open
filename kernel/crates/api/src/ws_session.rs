// @feature: FP-0.2.WS停止清除 派发链收尾清除 pending_cancel | @ci: rust-test
//! WebSocket 会话接入——把 session crate 接到 axum WS（ADR §7.2 P2 接线）。
//!
//! 职责：
//! - [`WsSink`]：axum WebSocket 的 `EventSink` 适配（经 mpsc 通道转发，避免
//!   `SplitSink` 跨 await 共享问题）；
//! - [`run_ws_session`]：握手鉴权 → 注册连接 → 入站路由（user_input/interaction/
//!   stop/heartbeat）→ 出站通道排空到 socket。断线重连由 SessionCoordinator 统一处理。

use std::collections::HashMap;
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc, Mutex,
};

use agentos_core::{
    traits::{MessageQueryOpts, StorageBackend},
    types::{PatchType, PendingInputRecord, PendingInputSource, TenantContext, TraceEntry},
};
use agentos_http::auth::verify_access_token;
use agentos_session::{
    auth::HandshakeAuth,
    router::{InboundRouter, PipelineDispatcher, RouteOutcome},
    EventSink, SessionCoordinator,
};
use axum::extract::ws::{Message, WebSocket};
use futures_util::{SinkExt, StreamExt};
use serde_json::json;

use tokio::sync::{mpsc, watch};
use tracing::{debug, info, warn};

use crate::routes::AppState;

/// 全局 WS sink id 生成器（连接注册表去重/超限踢旧 LRU 比较用；全局单调递增，
/// 最小 id = 最早建立的连接）。
static SINK_ID_SEQ: AtomicU64 = AtomicU64::new(1);

/// 关闭信号值：无。
const CLOSE_NONE: u8 = 0;
/// 关闭信号值：超限踢旧（每 user 连接数超限 LRU 踢最旧，ADR 2026-10-01）——
/// 前端对 CLOSE_CODE_KICKED 判"被替换"跳过重连。
const CLOSE_KICKED: u8 = 1;
/// 关闭信号值：出站队列满载自愈——普通关闭，前端按掉线自动重连，经重放/
/// 整树刷新恢复（区别于踢旧：这里要鼓励重连）。
const CLOSE_BACKPRESSURE: u8 = 2;
/// 关闭信号值：鉴权类拒绝关闭（用户→租户解析存储故障 fail-closed，K8 修订
/// ADR 2026-10-10）——先清空出站队列再发 Close(4001)，前端 4001 →
/// refreshToken → 重连自愈（对齐握手拒绝码 REJECT_CODE_INVALID_TOKEN）。
const CLOSE_AUTH_REJECT: u8 = 3;

/// 入站帧 JSON 解析失败累计（限频 warn 的采样基数）。
static INBOUND_PARSE_ERRORS: AtomicU64 = AtomicU64::new(0);

/// 外来 thread 注册拒绝累计（限频 warn 的采样基数）：拒绝是常态防御动作，
/// 攻击者刷帧时不刷日志，但首条与采样必须留痕可审计。
static THREAD_OWNERSHIP_REJECTS: AtomicU64 = AtomicU64::new(0);

/// REST chat 同步等待桥：client_message_id → outcome sender。
///
/// chat_handler 入队前注册（cmid 由其生成，`http_` 前缀与前端 uuid cmid 空间
/// 区分）；消费循环一轮结束后按 rec.client_message_id 命中即发送并移除；
/// DELETE/清空逐出排队条目时通知失败 outcome（防 REST 请求挂死）。remove 语义
/// 先到先得：条目被消费循环 pop 后表行已删，DELETE 侧 list 查不到即不通知，
/// 两个通知点天然互斥。进程内瞬态：重启即失，对应 HTTP 连接同断，无挂死窗口。
static OUTCOME_WAITERS: std::sync::OnceLock<
    std::sync::Mutex<
        std::collections::HashMap<
            String,
            tokio::sync::oneshot::Sender<crate::server::EngineOutcome>,
        >,
    >,
> = std::sync::OnceLock::new();

/// 注册同步等待者（chat_handler 专用）。
pub(crate) fn register_outcome_waiter(
    cmid: String,
    tx: tokio::sync::oneshot::Sender<crate::server::EngineOutcome>,
) {
    // C5：条目级 insert/remove/send 无跨条目不变量，锁中毒恢复安全
    //（与 panic 顺着 await 链扩散到整个连接/进程相比是明确更优语义）。
    OUTCOME_WAITERS
        .get_or_init(Default::default)
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .insert(cmid, tx);
}

/// 通知等待者（命中即移除；未命中静默跳过——非 REST 路径 cmid 恒 miss）。
pub(crate) fn notify_outcome_waiter(cmid: &str, outcome: crate::server::EngineOutcome) {
    if let Some(map) = OUTCOME_WAITERS.get() {
        // C5：条目级操作无跨条目不变量，中毒恢复安全（不 panic 扩散）。
        if let Some(tx) = map
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .remove(cmid)
        {
            let _ = tx.send(outcome);
        }
    }
}

/// 移除等待者且不通知（同步等待侧超时自清理专用，防条目泄漏；
/// 事后到达的 notify 因条目已删变为 no-op，先到先得语义不变）。
pub(crate) fn remove_outcome_waiter(cmid: &str) {
    if let Some(map) = OUTCOME_WAITERS.get() {
        // C5：条目级操作无跨条目不变量，中毒恢复安全（不 panic 扩散）。
        map.lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .remove(cmid);
    }
}

/// 在飞 REST 同步等待桥条目数（堆栈级诊断面 memory-breakdown 消费；
/// oneshot 即弃、无内存账本面——这是审批/交互等待在内核的唯一内存驻留）。
pub fn outcome_waiters_count() -> usize {
    OUTCOME_WAITERS
        .get()
        .map(|map| {
            map.lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .len()
        })
        .unwrap_or(0)
}

/// 单连接出站帧队列容量（帧数）。正常排空速度远快于生产速度，此上限只在
/// 消费端停滞（休眠机器 / 半死 TCP）时触顶——把每连接积压钉在
/// `WS_OUTBOUND_CAPACITY × 单帧大小`，而非随断连时长无限增长
/// （2026-09-09 minidump 归因：无界 mpsc 是内核驻留内存的候选结构之一）。
const WS_OUTBOUND_CAPACITY: usize = 512;

/// axum WebSocket 的 EventSink 适配。
///
/// 帧队列 = **有界** mpsc + 关闭信号走 watch（`shutdown` 是同步方法，队列满时
/// 空串哨兵会 `try_send` 失败，故关闭不依赖队列）。`send_text` 用 `try_send`：
/// 队列满 = 消费端停滞 → 返回 `false`（registry 既有语义：注销该连接，背压
/// 兜底）+ 置关闭标志让排空任务发 Close 帧自愈——前端重连后经重放/整树刷新
/// 恢复，内存不随断连时长增长。
pub struct WsSink {
    id: u64,
    tx: mpsc::Sender<String>,
    close_tx: watch::Sender<u8>,
    /// 只告警一次：Full 后连接即将被注销/关闭，逐帧 warn 是噪声。
    full_warned: AtomicBool,
}

impl WsSink {
    /// 创建 sink + （帧接收端, 关闭信号接收端），均供出站排空任务消费。
    pub fn new() -> (Arc<Self>, mpsc::Receiver<String>, watch::Receiver<u8>) {
        let (tx, rx) = mpsc::channel(WS_OUTBOUND_CAPACITY);
        let (close_tx, close_rx) = watch::channel(CLOSE_NONE);
        let sink = Arc::new(Self {
            id: SINK_ID_SEQ.fetch_add(1, Ordering::SeqCst),
            tx,
            close_tx,
            full_warned: AtomicBool::new(false),
        });
        (sink, rx, close_rx)
    }
}

#[async_trait::async_trait]
impl EventSink for WsSink {
    async fn send_text(&self, text: &str) -> bool {
        match self.tx.try_send(text.to_string()) {
            Ok(()) => true,
            Err(mpsc::error::TrySendError::Full(_)) => {
                if !self.full_warned.swap(true, Ordering::Relaxed) {
                    warn!(
                        target: "agentos-ws",
                        sink = self.id,
                        capacity = WS_OUTBOUND_CAPACITY,
                        "出站队列已满（消费端停滞）：注销本连接交由重连自愈"
                    );
                }
                self.close_tx.send_replace(CLOSE_BACKPRESSURE);
                false
            }
            Err(mpsc::error::TrySendError::Closed(_)) => false,
        }
    }
    fn id(&self) -> u64 {
        self.id
    }
    fn pending_frames(&self) -> Option<usize> {
        // 实测在飞帧数：容量 - 剩余许可（诊断面消费端停滞积压直读）。
        Some(self.tx.max_capacity().saturating_sub(self.tx.capacity()))
    }
    fn shutdown(&self) {
        // 关闭信号（watch 置位，队列满也可达）：出站排空任务收到即向对端发
        // kicked 文本帧 + Close 帧。
        let _ = self.close_tx.send_replace(CLOSE_KICKED);
    }
    fn shutdown_auth_reject(&self) {
        // 鉴权类拒绝关闭信号（watch 置位，队列满也可达）：排空任务先清空
        // 已入队的出站帧再发 Close(4001)——拒绝回执帧入队先于本置位，
        // 队列先于 Close 排空，送达序由构造保证。
        let _ = self.close_tx.send_replace(CLOSE_AUTH_REJECT);
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
    run_ws_session_with_auth(
        socket,
        session,
        router,
        authenticate(token),
        user_id_out,
        last_sequence,
        None,
    )
    .await
}

/// 握手拒绝收尾：先发 Close 帧（带拒绝码）再返回 (code, reason)。直接 drop
/// socket 浏览器端只会收到 1006（abnormal closure），前端 GlobalWebSocket 的
/// 「4001 → refreshToken → 重连」自愈路径永远不触发，表现为掉线后无限重连
/// 失败（“未连接”常驻、发消息无响应）。Close 帧发送失败也不影响语义
/// （连接随 drop 关闭）。
async fn reject_handshake(socket: WebSocket, code: u16, reason: &str) -> (u16, String) {
    let mut socket = socket;
    let _ = socket
        .send(Message::Close(Some(axum::extract::ws::CloseFrame {
            code,
            reason: reason.to_string().into(),
        })))
        .await;
    (code, reason.to_string())
}

/// 运行一次 WS 会话（鉴权已由调用方完成）：`?ticket=` 一次性票据路径在
/// ws_handler 消费票据得到 HandshakeAuth 后走本入口；`?token=` 路径经
/// [`run_ws_session`] 包装（token → authenticate → 本函数）。
#[allow(clippy::too_many_arguments)]
pub async fn run_ws_session_with_auth(
    socket: WebSocket,
    session: Arc<SessionCoordinator>,
    router: Arc<InboundRouter>,
    auth: HandshakeAuth,
    user_id_out: &mut Option<String>,
    last_sequence: Option<u64>,
    store: Option<std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
) -> (u16, String) {
    let (user_id, username) = match auth {
        HandshakeAuth::Ok { user_id, username } => (user_id, username),
        HandshakeAuth::Rejected { code, reason } => {
            // warn 留痕：拒绝路径（含原因码）进内核日志，前端重连风暴时
            // 可按日志追溯拒绝原因与频次。
            warn!(code = code, reason = %reason, "WS 握手鉴权拒绝（accept+close）");
            return reject_handshake(socket, code, &reason).await;
        }
    };

    // 握手后验用户存在性（P1-11/D3）：token 校验是同步纯 token 面（无 store
    // 可查），已删用户的未过期 token 仍能过 authenticate；票据签发与消费之间
    // 用户同样可被删。此处按 user_id 复用 auth 单一来源查询
    // （find_user_by_id，与分发期 resolve_tenant_id_by_user 同源同 tri-state）
    // 收口：查无此人 / 存储故障一律 4001 拒绝（fail-closed，不让死凭证占
    // 连接等到分发被拒）。无 store（测试 / 无持久化装配）无用户表可查，
    // 跳过——与分发期 None→default 租户同口径。
    if let Some(store) = &store {
        match agentos_http::auth::find_user_by_id(Some(store), &user_id).await {
            Ok(Some(_)) => {}
            Ok(None) => {
                warn!(user = %user_id, "WS 握手拒绝：用户不存在（已删用户凭证）");
                return reject_handshake(
                    socket,
                    agentos_session::auth::REJECT_CODE_INVALID_TOKEN,
                    "用户不存在或已被删除",
                )
                .await;
            }
            Err(e) => {
                warn!(user = %user_id, error = %e, "WS 握手拒绝：用户查询存储故障（fail-closed）");
                return reject_handshake(
                    socket,
                    agentos_session::auth::REJECT_CODE_INVALID_TOKEN,
                    "用户校验失败，请稍后重试",
                )
                .await;
            }
        }
    }
    *user_id_out = Some(user_id.clone());

    // 注册连接（含出站通道 sink）：多端并存（ADR 2026-10-01），仅每 user
    // 连接数超限时由 coordinator LRU 踢最旧并关闭。
    let (sink, out_rx, close_rx) = WsSink::new();
    if let Some(evicted_id) = session.register(&user_id, sink.clone()) {
        info!(user = %user_id, evicted = evicted_id, "WS 连接数超限，踢最旧连接");
    }

    run_socket_loop(
        socket,
        sink,
        out_rx,
        close_rx,
        session,
        router,
        user_id.clone(),
        last_sequence,
        store,
    )
    .await;
    info!(user = %user_id, username = %username, "WS 会话结束");
    (1000, "normal closure".to_string())
}

/// 出站排空 + 入站路由双任务循环，任一结束即关闭会话。
#[allow(clippy::too_many_arguments)]
async fn run_socket_loop(
    socket: WebSocket,
    sink: Arc<WsSink>,
    mut out_rx: mpsc::Receiver<String>,
    mut close_rx: watch::Receiver<u8>,
    session: Arc<SessionCoordinator>,
    router: Arc<InboundRouter>,
    user_id: String,
    last_sequence: Option<u64>,
    store: Option<std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
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
    // 重连后不重发 active_thread_changed、心跳 thread_id 为空，B3 会触发饥饿，
    // 断连期间落缓冲的 new_message 永远等不到重放。floor 之后的事件经当前
    // 连接实时送达，重放只发 (last_sequence, floor] 防重复。
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

    // 出站排空任务：从 channel 取消息写入 socket；关闭信号（超限踢旧 /
    // 出站满载自愈）→ 按原因分路收尾。
    // 堆栈级诊断插桩：per-connection 任务注册活动槽（标签 = 既有等待点
    // select! 的等待语义），断开自动注销——task-dump 可见每连接任务。
    let out_activity = agentos_core::task_activity::global_registry()
        .register(&format!("ws-outbound-drain:{user_id}"));
    let mut send_task = tokio::spawn(agentos_core::task_activity::scope(
        out_activity,
        async move {
            agentos_core::task_activity::set_current_label("waiting outbound frames");
            loop {
                tokio::select! {
                    _ = close_rx.changed() => {
                        let signal = *close_rx.borrow_and_update();
                        match signal {
                            CLOSE_KICKED => {
                                // 超限踢旧关闭（WsSink::shutdown，每 user 连接数超限
                                // LRU 踢最旧）：两段式通知——先发应用层 kicked 文本帧，
                                // 再发带 CLOSE_CODE_KICKED 状态码的 Close 帧。前端
                                // GlobalWebSocket 对 4000 判"被替换"跳过重连；若按普通
                                // 掉线处理，被踢端退避重连又超限再被踢，形成循环。代理链
                                // （Vite dev proxy 等）可能吞掉 Close 帧状态码（浏览器端
                                // 退化为 1006），文本帧先于 Close 送达即可让前端照常置位
                                // 防重连——两段缺一不可。
                                let kicked = json!({
                                    "type": "kicked",
                                    "data": {"reason": "replaced_by_new_connection"},
                                });
                                let _ = sender
                                    .send(Message::Text(
                                        serde_json::to_string(&kicked).unwrap_or_default().into(),
                                    ))
                                    .await;
                                let _ = sender
                                    .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                                        code: agentos_session::auth::CLOSE_CODE_KICKED,
                                        reason: "replaced_by_new_connection".into(),
                                    })))
                                    .await;
                                break;
                            }
                            CLOSE_BACKPRESSURE => {
                                // 满载自愈：普通关闭（非 4000）——前端按掉线自动重连，
                                // 重连后经重放/整树刷新恢复，不判"被替换"。
                                break;
                            }
                            CLOSE_AUTH_REJECT => {
                                // 鉴权类拒绝（租户解析存储故障 fail-closed，K8 修订）：
                                // 先清空已入队的出站帧（拒绝回执在置位前入队，
                                // 队列不清空会随关闭丢失——回执契约 ADR 2026-09-29
                                // 不因拒绝而 regression），再发 Close(4001)：前端
                                // 走 refreshToken → 重连自愈。
                                while let Ok(text) = out_rx.try_recv() {
                                    if sender
                                        .send(Message::Text(text.into()))
                                        .await
                                        .is_err()
                                    {
                                        break;
                                    }
                                }
                                let _ = sender
                                    .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                                        code: agentos_session::auth::REJECT_CODE_INVALID_TOKEN,
                                        reason: "auth reject: tenant resolution failed".into(),
                                    })))
                                    .await;
                                break;
                            }
                            _ => continue,
                        }
                    }
                    text = out_rx.recv() => match text {
                        Some(text) => {
                            if sender.send(Message::Text(text.into())).await.is_err() {
                                break;
                            }
                        }
                        None => break,
                    }
                }
            }
            let _ = sender.close().await;
        },
    ));

    // 入站循环：路由消息（session/router/user_id move 进 task）
    let session_for_unreg = session.clone();
    let sink_id = sink.id();
    let user_id_for_task = user_id.clone();
    // B3：每连接一次的回放标志（建连重放已消费时为 true）——首个带 thread_id
    // 的入站消息触发 replay_missed。
    let in_activity = agentos_core::task_activity::global_registry()
        .register(&format!("ws-inbound-router:{user_id}"));
    let mut recv_task = tokio::spawn(agentos_core::task_activity::scope(
        in_activity,
        async move {
            agentos_core::task_activity::set_current_label("waiting inbound frames");
            while let Some(Ok(msg)) = receiver.next().await {
                match msg {
                    Message::Text(text) => {
                        agentos_core::task_activity::set_current_label("routing inbound message");
                        handle_inbound(
                            &text,
                            &user_id_for_task,
                            &session,
                            &router,
                            last_sequence,
                            &replayed_for_task,
                            store.as_ref(),
                        )
                        .await;
                        agentos_core::task_activity::set_current_label("waiting inbound frames");
                    }
                    Message::Close(_) => break,
                    _ => {}
                }
            }
        },
    ));

    // 任一任务结束即关闭会话
    tokio::select! {
        _ = &mut send_task => { recv_task.abort(); }
        _ = &mut recv_task => { send_task.abort(); }
    }
    // 清理：注销本连接（按 sink id 比对；已被逐出/未注册时 no-op，
    // 不影响同 user 其余连接）
    session_for_unreg.registry().unregister(&user_id, sink_id);
}

/// thread 归属校验：请求者租户下该 thread 确有管道关联才放行。
///
/// 规则：`pipeline_sessions` 在请求者租户下非空 → 放行；查询失败或无关联
/// 一律拒绝（fail-closed）。合法新会话不受影响——新会话的映射注册由 REST
/// create_session 与轮次 on_round_start 的服务端路径承载，而携带无关联
/// thread_id 的本帧派发也会因解析不出管道被 dispatch 拒绝，注册本无意义。
/// 无 store（单测/嵌入式降级模式）时无解析依据，维持旧行为放行。
async fn thread_owned_by_user(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    thread_id: &str,
    user_id: &str,
) -> bool {
    let Some(store) = store else {
        return true;
    };
    // 租户解析失败即拒绝注册（fail-closed，K8 修订 ADR 2026-10-10）：
    // 映射是事件单播/回放的路由依据，租户未知时放行 = 潜在跨租户劫持面。
    let tenant_id = match agentos_http::auth::resolve_tenant_id_by_user(Some(store), user_id).await
    {
        Ok(t) => t,
        Err(e) => {
            let n = THREAD_OWNERSHIP_REJECTS.fetch_add(1, Ordering::Relaxed);
            if n == 0 || n % 100 == 0 {
                warn!(
                    count = n + 1,
                    user_id = %user_id,
                    error = %e,
                    "thread 归属校验租户解析失败，拒绝注册（fail-closed）"
                );
            }
            return false;
        }
    };
    match store
        .list_pipeline_ids_by_thread(thread_id, &tenant_id)
        .await
    {
        Ok(pids) if !pids.is_empty() => true,
        Ok(_) => {
            let n = THREAD_OWNERSHIP_REJECTS.fetch_add(1, Ordering::Relaxed);
            if n == 0 || n % 100 == 0 {
                warn!(
                    count = n + 1,
                    user_id = %user_id,
                    thread_id = %thread_id,
                    "拒绝注册非本租户 thread 映射（thread_id 来自客户端帧，事件单播/回放路由依据）"
                );
            }
            false
        }
        Err(e) => {
            let n = THREAD_OWNERSHIP_REJECTS.fetch_add(1, Ordering::Relaxed);
            if n == 0 || n % 100 == 0 {
                warn!(
                    count = n + 1,
                    user_id = %user_id,
                    thread_id = %thread_id,
                    error = %e,
                    "thread 归属校验查询失败，拒绝注册（fail-closed，不排除外来 thread）"
                );
            }
            false
        }
    }
}

/// 处理一条入站文本消息：路由 + 心跳 + thread 注册。
async fn handle_inbound(
    text: &str,
    user_id: &str,
    session: &SessionCoordinator,
    router: &InboundRouter,
    last_sequence: Option<u64>,
    replayed: &std::sync::atomic::AtomicBool,
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
) {
    let msg: serde_json::Value = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(e) => {
            // 畸形帧丢弃必须可观测（否则"前端发了什么"不可追溯）：限频采样
            // warn（首条 + 每 100 条一条，重连风暴/坏客户端不刷日志），
            // 丢弃语义不变——协议面只收合法 JSON 帧。
            let n = INBOUND_PARSE_ERRORS.fetch_add(1, Ordering::Relaxed);
            if n == 0 || n % 100 == 0 {
                warn!(
                    count = n + 1,
                    user_id = %user_id,
                    error = %e,
                    "WS 入站帧 JSON 解析失败（丢弃）"
                );
            }
            return;
        }
    };
    // user_input/interaction 携带 thread_id 时建立 thread→user 映射。
    // 归属校验先行：thread_id 完全来自客户端帧，而该映射是事件单播
    // （send_to_thread 反查）与断线回放（replay_missed）的唯一路由依据——
    // 不校验即任意已认证连接可借他人 thread_id 劫持其事件流（水平越权）。
    if let Some(thread_id) = msg.get("thread_id").and_then(|v| v.as_str()) {
        if !thread_id.is_empty() && thread_owned_by_user(store, thread_id, user_id).await {
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
        RouteOutcome::Error(e) => {
            // 路由错误必须对客户端可见（ADR 2026-09-29 回执契约）：只写日志 =
            // 前端「思考中」无限等待（装机 R302 实证 8 分钟无声）。user_input
            // 回 user_input_ack(ok:false)，其余类型回 route_error 通用错误帧，
            // 帧坐标尽力提取（缺 thread_id 的违约帧对应槽位为空串）。
            let msg_type = msg.get("type").and_then(|v| v.as_str()).unwrap_or("");
            let thread_id = agentos_session::router::field_or_data(&msg, "thread_id").unwrap_or("");
            let cmid =
                agentos_session::router::field_or_data(&msg, "client_message_id").unwrap_or("");
            let frame = if msg_type == "user_input" {
                serde_json::json!({
                    "type": "user_input_ack",
                    "client_message_id": cmid,
                    "thread_id": thread_id,
                    "ok": false,
                    "error": e,
                })
            } else {
                serde_json::json!({
                    "type": "route_error",
                    "data": {
                        "message_type": msg_type,
                        "thread_id": thread_id,
                        "client_message_id": cmid,
                        "error": e,
                    },
                })
            };
            let frame_str = serde_json::to_string(&frame).unwrap_or_default();
            session.registry().send_to_user(user_id, &frame_str).await;
            warn!(user = user_id, thread_id = %thread_id, error = %e, "入站路由错误（已回错误帧）");
        }
        RouteOutcome::Handled => {
            // user_input 派发受理（直跑或入队均算受理）即回执 ok:true——前端
            // 恒挂超时据此撤销；其余类型无回执消费方，保持零额外帧。
            let msg_type = msg.get("type").and_then(|v| v.as_str()).unwrap_or("");
            if msg_type == "user_input" {
                let thread_id =
                    agentos_session::router::field_or_data(&msg, "thread_id").unwrap_or("");
                let cmid =
                    agentos_session::router::field_or_data(&msg, "client_message_id").unwrap_or("");
                let ack = serde_json::json!({
                    "type": "user_input_ack",
                    "client_message_id": cmid,
                    "thread_id": thread_id,
                    "ok": true,
                });
                let ack_str = serde_json::to_string(&ack).unwrap_or_default();
                session.registry().send_to_user(user_id, &ack_str).await;
                tracing::debug!(user = user_id, thread_id = %thread_id, "user_input -> ack ok");
            }
        }
        RouteOutcome::Ignored => {}
    }
}

/// 执行 agent 解析（会话绑定链，仅供派发簿记：registry 登记 / run 日志）。
///
/// 优先级：显式传入（chat.send_message 任务派发，非空）→ registry 线程绑定
/// （热）→ 主管道 state `agent.id`（冷，绑定真值）→ "agentos"（默认主 agent）。
///
/// 冷读单源 state `agent.id`：经会话活跃管道定位 state 行读取；sessions 表
/// agent_id 已降为投影（F2 收敛），不作绑定读源。run 的执行身份同样只认该键
/// （2026-08-31）——热启动在内存 state、冷启动在 DB，续跑派发无需携带身份。
async fn resolve_dispatch_agent(
    registry: Option<&agentos_session::ConnectionRegistry>,
    store: Option<&Arc<dyn StorageBackend>>,
    tenant_id: &str,
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
                if let Some(pid) = s.active_pipeline_id.filter(|p| !p.is_empty()) {
                    match store.load_pipeline_state(&pid, tenant_id).await {
                        Ok(fields) => {
                            if let Some(aid) = fields
                                .get("agent.id")
                                .and_then(|v| v.as_str())
                                .filter(|a| !a.is_empty())
                            {
                                return aid.to_string();
                            }
                        }
                        Err(e) => {
                            warn!(
                                thread_id = %thread_id,
                                pipeline = %pid,
                                error = %e,
                                "管道 state 读取失败，agent 绑定解析回退默认 agentos"
                            );
                        }
                    }
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

/// 帧字段 overlay 名单收集（名单制传输 2026-10-23，widget 声明推导）。
///
/// 从 manifests 收集启用插件 ui_schema `widgets[*].frame_field`（字符串）并集：
/// widget 即字段声明面（用途/来源/传输去向一体），`frame_field` = 该 widget 的
/// 交互值随 user_input 帧以该字段名传输、写入同名 state 键。disabled 插件不参与
/// （enabled_plugin_ids 过滤）；去重排序保证名单稳定。纯函数便于单测。
fn frame_overlay_fields_from_manifests(
    manifests: &[agentos_core::traits::PluginManifest],
    enabled_plugin_ids: &std::collections::HashSet<String>,
) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for m in manifests {
        if !enabled_plugin_ids.contains(&m.id) {
            continue;
        }
        let Some(widgets) = m
            .ui_schema
            .as_ref()
            .and_then(|ui| ui.get("widgets"))
            .and_then(|w| w.as_array())
        else {
            continue;
        };
        for w in widgets {
            if let Some(f) = w.get("frame_field").and_then(|v| v.as_str()) {
                if !f.is_empty() {
                    out.push(f.to_string());
                }
            }
        }
    }
    out.sort();
    out.dedup();
    out
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

    /// 消费循环：FIFO pop pending 队列逐条执行直到空。入队链任务与直跑尾部的
    /// 共用体——直跑占链竞态窗口内入队的消息、重启遗留的队列残留都由此收走。
    async fn drain_pending_queue(
        state: AppState,
        tenant: TenantContext,
        thread_id: String,
        route_id: String,
        user_id: String,
        store: Arc<dyn agentos_core::traits::StorageBackend>,
    ) {
        loop {
            let rec = match store.pop_pending_input(&tenant.tenant_id, &route_id).await {
                Ok(Some(rec)) => rec,
                Ok(None) => break, // 队列空：消费任务退出，链尾自清理
                Err(e) => {
                    tracing::error!(
                        pipeline = %route_id,
                        error = %e,
                        "pending 消费出队失败（跳过本轮，队列残留待下轮）"
                    );
                    // 残留 waiter 失败收口：消费任务因出队错误退出，队列中
                    // 条目本轮不会被消费——REST /chat 同步桥的等待者若不
                    // 通知会一直挂到超时层且条目泄漏。逐条发 failed outcome
                    // （list 一并失败时退化为超时层兜底，不额外重试）。
                    if let Ok(remaining) = store
                        .list_pending_inputs(&tenant.tenant_id, &route_id)
                        .await
                    {
                        for rec in remaining {
                            notify_outcome_waiter(
                                &rec.client_message_id,
                                crate::server::EngineOutcome {
                                    content: format!("pending 消费出队失败，消息未被本轮处理: {e}"),
                                    final_assistant: None,
                                    failed: true,
                                    degraded: false,
                                    plugin_errors: Vec::new(),
                                    consumed_pending_cmids: Vec::new(),
                                },
                            );
                        }
                    }
                    // 消费循环因存储故障退出：前端收尾快照（queue_depth/stopping）
                    // 必须清——最后一条 run 的 round_finished 带着 queue_depth>0，
                    // 不补这一帧会让前端永久停在「运行中」（2026-10-29 审查）。
                    if let Some(session) = state.session.as_ref() {
                        emit_round_finished_event(session, &thread_id, &route_id, false, false, 0)
                            .await;
                    }
                    break;
                }
            };
            // 队列生命周期留痕（2026-10-21 装机排障）：消费出队是队列条目消失
            // 的正路，此前零日志——条目蒸发不可归因。id/cmid/长度随日志落盘。
            tracing::info!(
                pipeline = %route_id,
                input = %rec.id,
                cmid = %rec.client_message_id,
                content_len = rec.content.chars().count(),
                "pending 队列条目已出队（消费路径）"
            );
            // 消费事件：该条从队列条移入主消息流（前端据此同步）。
            emit_pending_inputs_changed(
                &state,
                &thread_id,
                &route_id,
                &tenant.tenant_id,
                &store,
                "consumed",
            )
            .await;
            let cmid = rec.client_message_id.clone();
            let outcome =
                Self::run_pipeline_round_scoped(&state, &tenant, &thread_id, &user_id, rec).await;
            let cancelled = outcome_cancelled(&outcome);
            let depth = pending_depth(&store, &tenant.tenant_id, &route_id).await;
            if let Some(session) = state.session.as_ref() {
                emit_round_finished_event(
                    session,
                    &thread_id,
                    &route_id,
                    outcome.failed,
                    cancelled,
                    depth,
                )
                .await;
            }
            notify_outcome_waiter(&cmid, outcome);
        }
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
        let cmid = record.client_message_id.clone();
        let exec_route = record.route_id.clone();
        registry.enqueue(&chain_key, &chain_user, async move {
            let outcome = Self::run_pipeline_round_scoped(
                &exec_state,
                &tenant,
                &exec_thread,
                &exec_user,
                record,
            )
            .await;
            let cancelled = outcome_cancelled(&outcome);
            if let Some(session) = exec_state.session.as_ref() {
                emit_round_finished_event(
                    session,
                    &exec_thread,
                    &exec_route,
                    outcome.failed,
                    cancelled,
                    0,
                )
                .await;
            }
            notify_outcome_waiter(&cmid, outcome);
        });
    }

    /// 派发链收尾清除：本派发期间落、未被 run 起手消费的待取消标记随链终结清除。
    ///
    /// 标记寿命 = 所属派发链寿命（2026-10-19 装机实证，docs/working/
    /// Stop残留标记误杀_台账.md）：stop_generation 落标（无 Running run + 链在途）
    /// 后，若该链的 run 已过起手消费点（如 run 在途但运行投影查询失配）或链未
    /// 建行即终了，标记跨链残留——下一次无关派发的 run 起手把它当本次的首 token
    /// 前取消，零执行误杀（现场：残留 57 分钟后新消息被秒杀且静默丢失）。
    /// 起手消费在前、收尾清除在后（同管道链串行），幂等无害；
    /// 缺陷A 首 token 前取消仍由起手消费先行生效，语义不变。
    async fn clear_dispatch_scoped_cancel(
        store: Option<&Arc<dyn StorageBackend>>,
        route_id: &str,
        tenant_id: &str,
    ) {
        if let Some(store) = store {
            let _ = store.consume_pending_cancel(route_id, tenant_id).await;
        }
    }

    /// 派发链作用域跑轮：run_pipeline_round + 链收尾清除待取消标记。
    /// 全部派发执行调用点（drain_pending_queue / spawn_chain / 直跑块）必须经此
    /// 入口，保证标记清理不因调用面遗漏而残留。
    /// 执行中止面（dispatch_stop / dispatch_force_pending 共用）：Running run 置
    /// suspended 并唤醒取消等待者；run 未建行但派发在途 → 落待取消标记（引擎
    /// run 创建起手消费，终态 Cancelled）。返回是否落了任一取消信号（false =
    /// 链空闲的真幂等空转，不得残留标记）。
    async fn suspend_pipeline_execution(
        store: &Arc<dyn StorageBackend>,
        tenant_id: &str,
        target_pipeline: &str,
    ) -> bool {
        let runs = match store
            .list_runs_by_pipeline(target_pipeline, tenant_id)
            .await
        {
            Ok(r) => r,
            Err(e) => {
                warn!(pipeline = %target_pipeline, error = %e, "执行中止查询 run 失败（跳过挂起）");
                return false;
            }
        };
        // 当前运行投影（每管道至多一条，ADR 2026-09-18）。判定按四分支：
        // - Running：落 suspended + 唤醒停止信号（正常取消）；
        // - Suspended：已在停止中（长步骤未退出）——幂等返回 true，**不得**
        //   落 pending_cancel（该标记由下一条 run 起手消费，会零执行误杀
        //   用户点「立即发送」的那条消息；2026-10-29 实证）；
        // - 其它终态（Completed/Failed/Cancelled）：链上只剩 drain/收尾，
        //   没有「当前回复」可停——返回 false，不得落标；
        // - 无投影 + 链在途：首 token 前空窗，落 pending_cancel（唯一合法落标面）。
        match runs.first().map(|r| &r.status) {
            Some(agentos_core::types::RunStatus::Running) => {
                let run = runs.first().expect("match 已确认首条存在");
                // runs 表退役（ADR 2026-09-18）：簿记 = state 运行键
                let mut fields = serde_json::Map::new();
                fields.insert("run_status".to_string(), serde_json::json!("suspended"));
                if let Err(e) = store
                    .upsert_state_fields(target_pipeline, tenant_id, &fields)
                    .await
                {
                    warn!(pipeline = %target_pipeline, error = %e, "执行中止置 suspended 失败");
                    return false;
                }
                info!(
                    pipeline = %target_pipeline,
                    run_id = %run.run_id,
                    "执行中止已落地：run 置 suspended（信号，终态由引擎收尾）"
                );
                // 事件驱动取消（用户裁定 2026-10-21）：落标即唤醒 llm 插件的
                // wait_run_stop 长轮询等待者——取消感知从 0.5s 轮询间隔降为
                // 信号传播本身。
                crate::run_signal::RunSignalHub::global().signal(&run.run_id);
                true
            }
            Some(agentos_core::types::RunStatus::Suspended) => {
                // 已在停止中：幂等。重复 stop / stop 后 force 不得污染下一条 run。
                debug!(
                    pipeline = %target_pipeline,
                    "执行中止幂等：run 已 Suspended（停止中），不落待取消标记"
                );
                true
            }
            Some(_) => {
                // run 已终态：链在 drain 时停止无对象，落标会取消队列里下一条。
                false
            }
            None => {
                // 首 token 前空窗（缺陷A，台账 R1 ④immediate）：取消在 run 建立前到达，
                // 无运行投影可挂起——但派发链在途（user_input 已受理、引擎 run
                // 尚未建行）时取消必须生效：落管道级待取消标记，run 创建起手由引擎
                // 消费（终态 Cancelled），禁静默吞。链空闲才是真幂等空转，且不得设
                // 标记（否则下一轮被误杀）。
                if !crate::run_chain::RunChainRegistry::global().has_chain(target_pipeline) {
                    return false;
                }
                let mut fields = serde_json::Map::new();
                fields.insert("pending_cancel".to_string(), serde_json::json!(true));
                if let Err(e) = store
                    .upsert_state_fields(target_pipeline, tenant_id, &fields)
                    .await
                {
                    warn!(pipeline = %target_pipeline, error = %e, "执行中止落待取消标记失败");
                    return false;
                }
                info!(
                    pipeline = %target_pipeline,
                    "执行中止首 token 前到达（无运行投影、派发在途）：落待取消标记，run 创建起手消费"
                );
                true
            }
        }
    }

    async fn run_pipeline_round_scoped(
        state: &AppState,
        tenant: &TenantContext,
        thread_id: &str,
        user_id: &str,
        rec: PendingInputRecord,
    ) -> crate::server::EngineOutcome {
        let route_id = rec.route_id.clone();
        let outcome = Self::run_pipeline_round(state, tenant, thread_id, user_id, rec).await;
        // 轮边界注入消费的队列条目（设计真值 docs/working/待输入队列轮边界注入_方案_20261025.md）：
        // 这些 cmid 不经本消费循环独立成轮，等待者漏通知会挂到超时层——run 收尾
        // 以同一 outcome 批量通知（失败轮同样通知，好于超时兜底）。
        for cmid in &outcome.consumed_pending_cmids {
            notify_outcome_waiter(cmid, outcome.clone());
        }
        Self::clear_dispatch_scoped_cancel(state.store.as_ref(), &route_id, &tenant.tenant_id)
            .await;
        outcome
    }

    /// 消费一条 pending 输入：发 stream_start + 引擎执行 + 推送结果（流式协议与
    /// 旧 dispatch 完全一致，参数从 rec 取——等待窗口内 PUT 的修改在此生效）。
    /// 返回引擎 outcome——消费循环据此通知 REST chat 同步等待者。
    async fn run_pipeline_round(
        state: &AppState,
        tenant: &TenantContext,
        thread_id: &str,
        user_id: &str,
        rec: PendingInputRecord,
    ) -> crate::server::EngineOutcome {
        // 生成 run 级 message_id：仅失败路径收尾（stream_error/plugin_error）使用
        // ——成功路径的消息事件由引擎轮次观察点逐轮发射（见 SessionRoundEvents）。
        let message_id = format!("a_{}", uuid::Uuid::new_v4().simple());
        // 执行 agent（派发簿记：registry 登记 / run 日志）。run 的执行身份
        // 不取此值——身份是管道 state 持久键 agent.id，由 agent_config_load 与
        // 内核工具面消费（热启动在内存 state、冷启动在 DB）。
        let exec_agent = if rec.agent_id.is_empty() {
            resolve_dispatch_agent(
                state.session.as_ref().map(|s| s.registry().as_ref()),
                state.store.as_ref(),
                &tenant.tenant_id,
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
                &rec.route_id,
                &exec_thread,
                &message_id,
                &exec_user,
                &rec.thinking_strength,
                rec.execution_context.as_ref(),
                rec.state_overlay.as_ref(),
                rec.pipeline_config_id.as_deref(),
                &rec.client_message_id,
            ),
        )
        .await;

        let Some(session) = state.session.as_ref() else {
            tracing::warn!(thread = %exec_thread, "session 未启用，引擎结果无法推回前端");
            return outcome;
        };

        // 失败路径：引擎执行失败（executor.run Err）→ stream_error 收尾，
        // 前端立即解除生成态（不再依赖 90s 强制收尾兜底）。error 为统一错误
        // 信封（契约 streaming.json stream_error.error 锁 object，单一真值源
        // config/kernel/error_codes.json；ENGINE_RUN_FAILED 可重试）。
        if outcome.failed {
            emit_stream_error_event(
                session,
                &exec_thread,
                &rec.route_id,
                &message_id,
                "ENGINE_RUN_FAILED",
                &outcome.content,
            )
            .await;
            return outcome;
        }

        // 插件错误可见性出口：本轮管道执行中插件失败（引擎 warn+继续的假成功）
        // 逐个发射 `plugin_error` 事件——非终止信号，前端只弹通知。必须在空回复
        // 防线之前发射：该防线提前 return，晚于此处会把携带真实失败原因的
        // plugin_error 事件一并吞掉（LLM 调用失败轮次用户只能看到无归因兜底文案）。
        emit_plugin_error_events(session, &exec_thread, &rec.route_id, &message_id, &outcome).await;

        // 空回复防线：run 结束但没有任何 assistant 消息（LLM 调用失败被引擎
        // error_policy warn+继续吞掉、或 core 轮次无产出）→ 不发 new_message、
        // 不把 outcome.content 兜底当 assistant 内容回发——content 无 raw_result
        // 时是用户输入原文，回发即前端 assistant 气泡回显用户消息。fail-closed：
        // stream_error 显式报错（NO_ASSISTANT_REPLY），前端标记失败。
        // 报错文案携带真实原因：plugin_errors 在场（网络超时/连接失败/认证/
        // 限流等）时附首条错误原文，不做「检查配置与密钥」式的固定归因——
        // 网络故障曾被误报成配置问题（2026-10-08 用户报告）。
        if outcome.final_assistant.is_none() {
            let mut failed_outcome = outcome;
            failed_outcome.failed = true;
            failed_outcome.content = no_assistant_reply_content(&failed_outcome.plugin_errors);
            emit_stream_error_event(
                session,
                &exec_thread,
                &rec.route_id,
                &message_id,
                "NO_ASSISTANT_REPLY",
                &failed_outcome.content,
            )
            .await;
            return failed_outcome;
        }

        // 阶段④成功出口：实时事件已由引擎轮次观察点逐轮发射（stream_start →
        // 8 事件增量 → new_message/stream_end，一轮 = 一条消息，与 message_slots
        // 逐轮持久化同构）。插件错误事件已在上方空回复防线前统一发射。
        outcome
    }
}

/// run 级收尾事件（pipeline_round_finished）：一次用户输入触发的整次执行结束。
///
/// 引擎逐轮发射的 stream_end 只收尾当轮消息——多轮执行的工具轮间不能用
/// stream_end 判定「整次执行结束」，否则前端生成态在工具执行期间被提前掐断，
/// 执行中发送分支失效（乐观气泡与待发队列同屏互斥破坏）。本事件是整次执行
/// 的唯一成功终止信号；失败路径 stream_error 已先行终止，此处再发幂等兜底。
///
/// `queue_depth`：发出事件时该管道队列剩余条目数——前端据此区分「run 结束
/// 且真空闲」与「run 结束但链尾 drain 还要继续消费」；`cancelled`：用户停止
/// 导致的终态（有产出的中断消息 status=interrupted），前端不再把它显示成
/// completed。
async fn emit_round_finished_event(
    session: &SessionCoordinator,
    thread_id: &str,
    route_id: &str,
    failed: bool,
    cancelled: bool,
    queue_depth: usize,
) {
    let _ = session
        .emit_event(
            thread_id,
            "pipeline_round_finished",
            serde_json::json!({
                "pipeline_id": route_id,
                "_threadId": thread_id,
                "failed": failed,
                "cancelled": cancelled,
                "queue_depth": queue_depth,
            }),
        )
        .await;
}

/// 停止终态判定：llm_core 中断半截消息落库 status=interrupted（有产出停止）。
/// 零产出停止走 stream_error(NO_ASSISTANT_REPLY)（failed 路径），不在此列。
fn outcome_cancelled(outcome: &crate::server::EngineOutcome) -> bool {
    outcome
        .final_assistant
        .as_ref()
        .and_then(|m| m.get("status"))
        .and_then(|v| v.as_str())
        == Some("interrupted")
}

/// 队列剩余深度（事件快照）。查询失败按 0（前端退化为只看 run_status）。
async fn pending_depth(
    store: &Arc<dyn agentos_core::traits::StorageBackend>,
    tenant_id: &str,
    pipeline_id: &str,
) -> usize {
    store
        .list_pending_inputs(tenant_id, pipeline_id)
        .await
        .map(|rows| rows.len())
        .unwrap_or(0)
}

/// assistant 消息的权威 seq 提取（new_message / stream_end 共用）：
/// 元素带 seq → 原值；契约外缺失 → null（绝不伪造默认序号，对齐 server.rs
/// 冷恢复「不做缺 seq 补位」零兼容契约——假序号会污染前端排序与断线补漏游标）。
fn assistant_authoritative_seq(final_assistant: Option<&serde_json::Value>) -> serde_json::Value {
    final_assistant
        .and_then(|m| m.get("seq"))
        .and_then(|v| v.as_u64())
        .into()
}

/// 轮次事件桥接：把引擎 [`agentos_engine::RoundEvents`] 翻译为流式契约事件
/// （stream_start/new_message/stream_end）——**一轮 = 一条消息**（DSH 形态）。
///
/// 与旧 run 级发射的区别（顺序混乱根因修复，2026-08-27）：
/// - stream_start 逐轮发射（各自 `a_` message_id），8 事件增量/工具事件按本轮 id 寻址；
/// - new_message 携带**该轮** assistant 完整持久形态（非"最后一条"）——前端占位
///   与 DB message_slots 逐轮记录同键同序，流式期间渲染与刷新重放一致。
///
/// [来源: docs/working/chat_stream_order_diagnosis_20260827.md]
pub(crate) struct SessionRoundEvents {
    session: Arc<SessionCoordinator>,
    thread_id: String,
    route_id: String,
    user_id: String,
    /// 待输入队列事件/对账读写面（轮边界注入消费事件发射用，设计真值
    /// docs/working/待输入队列轮边界注入_方案_20261025.md）。
    store: Arc<dyn StorageBackend>,
    tenant_id: String,
    /// user 认领回传已随首个有产出的轮次 new_message 发出（后续轮次不重复携带）。
    user_claimed: AtomicBool,
    /// 本 run 各轮的入口保留槽位（message_id → seq，ADR
    /// 2026-11-06-round-entry-slot-placeholder）：on_round_start 登记，
    /// on_round_end 消费——轮无 assistant（真零产出）时按此删除本轮占位行
    /// （生命周期封闭于单轮；有产出轮次由出口投影 fill 管辖，桥不删）。
    pending_round_seqs: Mutex<HashMap<String, i64>>,
}

impl SessionRoundEvents {
    pub fn new(
        session: Arc<SessionCoordinator>,
        thread_id: impl Into<String>,
        route_id: impl Into<String>,
        user_id: impl Into<String>,
        store: Arc<dyn StorageBackend>,
        tenant_id: impl Into<String>,
    ) -> Self {
        Self {
            session,
            thread_id: thread_id.into(),
            route_id: route_id.into(),
            user_id: user_id.into(),
            store,
            tenant_id: tenant_id.into(),
            user_claimed: AtomicBool::new(false),
            pending_round_seqs: Mutex::new(HashMap::new()),
        }
    }
}

impl agentos_engine::RoundEvents for SessionRoundEvents {
    fn on_round_start(
        &self,
        ev: agentos_engine::RoundStart,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + '_>> {
        let session = self.session.clone();
        let thread_id = self.thread_id.clone();
        let route_id = self.route_id.clone();
        let user_id = self.user_id.clone();
        Box::pin(async move {
            // 事件单播坐标注册：thread 只是 thread→user 反查索引（connection_registry）。
            // 注入路径（触发器 chat.send_message）只持有管道唯一坐标（12hex pipeline_id）
            // 作派发键，不注册则 send_to_thread 反查无 user、事件被丢弃。幂等注册。
            session.register_thread(&thread_id, &user_id);
            session.register_thread(&route_id, &user_id);
            // 入口保留槽位登记（ADR 2026-11-06 决策2/4）：轮无 assistant 的
            // on_round_end 按此删除本轮占位行。
            if let Ok(mut slots) = self.pending_round_seqs.lock() {
                slots.insert(ev.message_id.clone(), ev.sequence);
            }
            let _ = session
                .emit_event(
                    &thread_id,
                    "stream_start",
                    serde_json::json!({
                        "pipeline_id": route_id,
                        "message_id": ev.message_id,
                        // 入口保留的权威槽位（只增兼容：旧前端忽略该字段）——
                        // 前端占位气泡从第一毫秒起有权威排序坐标。
                        "sequence": ev.sequence,
                        "_threadId": thread_id,
                    }),
                )
                .await;
        })
    }

    fn on_round_end(
        &self,
        ev: agentos_engine::RoundEnd,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + '_>> {
        let session = self.session.clone();
        let thread_id = self.thread_id.clone();
        let route_id = self.route_id.clone();
        let store = self.store.clone();
        let tenant_id = self.tenant_id.clone();
        Box::pin(async move {
            // 消费本轮入口保留槽位（ADR 2026-11-06 决策4/5）：轮无 assistant
            // （真零产出，文本/思考/保留 tool_calls 三空——出口裁决在 llm_core）
            // → 删除本轮占位行，生命周期封闭于单轮。seq + record_id 双条件在
            // store 侧防误删；引擎侧同判定点已先删（幂等双保险，行已删则此处
            // 不命中）。失败 warn：残留行由下轮 run 起手 sweep 收口。
            let reserved_seq = self
                .pending_round_seqs
                .lock()
                .ok()
                .and_then(|mut slots| slots.remove(&ev.message_id));
            if ev.assistant.is_none() {
                if let Some(reserved) = reserved_seq {
                    if !ev.pipeline_id.is_empty() {
                        if let Err(e) = store
                            .delete_streaming_placeholder(
                                &ev.pipeline_id,
                                &tenant_id,
                                reserved,
                                &ev.message_id,
                            )
                            .await
                        {
                            warn!(
                                pipeline = %ev.pipeline_id,
                                message_id = %ev.message_id,
                                seq = reserved,
                                error = %e,
                                "零产出轮占位行删除失败（残留行由 run 起手 sweep 收口）"
                            );
                        }
                    }
                }
            }
            // seq 直传不伪造（对齐 server.rs 冷恢复「不做缺 seq 补位」契约）：
            // assistant 元素自带引擎分配的稳定 seq；缺失时发 null 挂空占位。
            let seq = assistant_authoritative_seq(ev.assistant.as_ref());
            // 认领回传（仅首个有产出的轮次）：user 权威 record（mc_ 指纹/seq/内容/cmid）
            // + cmid 幂等键——前端据此精确认领乐观 user 消息（非 FIFO 猜测）。
            // 前处理体（init）等无产出的轮次不携带也不置位；后续轮次重复携带会因
            // 幂等键二次认领（无害但多余），故只发一次。
            let include_user_payload = if ev.assistant.is_some() {
                !self.user_claimed.swap(true, Ordering::SeqCst)
            } else {
                false
            };
            let user_record = if include_user_payload {
                ev.user_message.as_ref().map(|u| {
                    serde_json::json!({
                        "id": agentos_core::ids::compute_message_id(u),
                        "content": u.get("content").cloned().unwrap_or(serde_json::Value::Null),
                        "sequence": u.get("seq").and_then(|v| v.as_u64()).unwrap_or(0),
                        "metadata": u.get("metadata").cloned().unwrap_or(serde_json::Value::Null),
                    })
                })
            } else {
                None
            };
            let client_message_id = if include_user_payload {
                ev.user_message
                    .as_ref()
                    .and_then(|u| u.get("metadata"))
                    .and_then(|m| m.get("client_message_id"))
                    .and_then(|v| v.as_str())
                    .map(str::to_string)
            } else {
                None
            };

            if let Some(assistant) = ev.assistant.as_ref() {
                let mut payload = serde_json::json!({
                    "pipeline_id": route_id,
                    "message_id": ev.message_id,
                    "_threadId": thread_id,
                    "sequence": seq,
                    "content": assistant.get("content").cloned().unwrap_or(serde_json::Value::Null),
                    "message": {
                        "id": ev.message_id,
                        "role": "assistant",
                        "content": assistant.get("content").cloned().unwrap_or(serde_json::Value::Null),
                        "sequence": seq,
                        "reasoningContent": assistant.get("reasoning_content"),
                        "toolCalls": assistant.get("tool_calls").unwrap_or(&serde_json::Value::Null),
                        "timestamp": chrono::Utc::now().to_rfc3339(),
                        // status 从消息 blob 读取：中断/错误半截消息落库时带
                        // interrupted/error，正常消息缺省 completed（冷热同构）。
                        "status": message_status_from_blob(assistant),
                        "thread_id": thread_id,
                        // 执行身份戳记从 blob 透传（blob 里有什么带什么）：消息
                        // 生成时的管道执行身份，前端气泡卡名/卡头像的数据源；
                        // blob 无该字段时写 null（前端 Message.agentId 可空）。
                        "agentId": assistant.get("agent_id"),
                    },
                });
                // 消息 metadata 透传（notify_judge 打标消费面，ADR
                // 2026-10-14-ws-new-message-metadata）：blob 携带对象形态
                // metadata 才附键（无则省略该键）——只增不删，旧消费端零感知；
                // blob 既有字段原样透传无额外查询，与 REST 冷路径同构。
                if let Some(meta) = assistant.get("metadata").filter(|m| m.is_object()) {
                    if let Some(obj) = payload.get_mut("message").and_then(|m| m.as_object_mut()) {
                        obj.insert("metadata".into(), meta.clone());
                    }
                }
                if let Some(cmid) = &client_message_id {
                    if let Some(obj) = payload.as_object_mut() {
                        obj.insert("client_message_id".into(), serde_json::json!(cmid));
                    }
                }
                if let Some(ur) = &user_record {
                    if let Some(obj) = payload.as_object_mut() {
                        obj.insert("user_message".into(), ur.clone());
                    }
                }
                let _ = session.emit_event(&thread_id, "new_message", payload).await;
            }
            let _ = session
                .emit_event(
                    &thread_id,
                    "stream_end",
                    serde_json::json!({
                        "pipeline_id": route_id,
                        "message_id": ev.message_id,
                        "_threadId": thread_id,
                        "final_sequence": seq,
                    }),
                )
                .await;
        })
    }

    /// 轮边界注入消费事件（设计真值 docs/working/待输入队列轮边界注入_方案_20261025.md）：
    /// 发 pending_inputs_changed("consumed") 同步队列条移除，payload 附加
    /// `consumed_client_message_ids`——前端据此把对应 queued 乐观气泡原地升级
    /// completed（注入消息无独立消费轮的 stream_start/认领链路）。
    fn on_pending_inputs_consumed(
        &self,
        ev: agentos_engine::PendingConsumed,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + '_>> {
        let session = self.session.clone();
        let store = self.store.clone();
        let thread_id = self.thread_id.clone();
        let tenant_id = self.tenant_id.clone();
        Box::pin(async move {
            emit_pending_inputs_changed_with(
                &session,
                &store,
                &thread_id,
                &ev.pipeline_id,
                &tenant_id,
                "consumed",
                &ev.records,
            )
            .await;
        })
    }
}

/// 失败出口统一收尾：stream_error 事件（ENGINE_RUN_FAILED / NO_ASSISTANT_REPLY
/// 两出口共用构造）。error 信封锁 object——契约 streaming.json，单一真值源
/// config/kernel/error_codes.json；code 可重试语义由各调用点注释声明。
async fn emit_stream_error_event(
    session: &SessionCoordinator,
    thread_id: &str,
    route_id: &str,
    message_id: &str,
    code: &str,
    error_message: &str,
) {
    let _ = session
        .emit_event(
            thread_id,
            "stream_error",
            serde_json::json!({
                "pipeline_id": route_id,
                "message_id": message_id,
                "_threadId": thread_id,
                "error": {
                    "code": code,
                    "message": error_message,
                    "source": "kernel",
                    "retryable": true,
                    "details": null,
                    "request_id": null,
                },
            }),
        )
        .await;
}

/// 空回复防线的报错文案：携带真实失败原因。
///
/// `plugin_errors`（引擎 `_plugin_errors` 提取，`[{plugin_id, code, message}]`）
/// 在场时附首条非空错误原文（网络超时/连接失败/认证/限流等原始异常文本，
/// 截断到 280 字符防 litellm 堆栈淹没）——文案不做「检查配置与密钥」式的
/// 固定归因，网络故障曾被误报成配置问题（2026-10-08 用户报告）；
/// 无错误收集（core 轮次无产出）时给中性指引。
fn no_assistant_reply_content(plugin_errors: &[serde_json::Value]) -> String {
    let cause = plugin_errors.iter().find_map(|err| {
        let msg = err.get("message")?.as_str()?.trim();
        (!msg.is_empty()).then(|| msg.to_string())
    });
    match cause {
        Some(err) => {
            let brief: String = if err.chars().count() > 280 {
                format!("{}…", err.chars().take(280).collect::<String>())
            } else {
                err
            };
            format!("本轮对话未产生回复（模型调用失败：{brief}）")
        }
        None => "本轮对话未产生回复（模型调用失败或未执行），请重试".to_string(),
    }
}

/// 插件错误可见性出口：本轮管道执行中插件失败（引擎 warn+继续的假成功）逐个
/// 发射 `plugin_error` 事件。非终止信号——消息本身正常收尾（new_message/
/// stream_end 照常），前端只弹通知中心（errorSource=plugin），不标记消息失败。
/// 统一错误信封（code/message/source/retryable，单一真值源 config/kernel/error_codes.json）；
/// code 缺省 PLUGIN_EXEC_FAILED，retryable=false（插件失败重跑同轮无意义）。
async fn emit_plugin_error_events(
    session: &SessionCoordinator,
    thread_id: &str,
    route_id: &str,
    message_id: &str,
    outcome: &crate::server::EngineOutcome,
) {
    for err in &outcome.plugin_errors {
        let plugin_id = err
            .get("plugin_id")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown");
        let code = err
            .get("code")
            .and_then(|v| v.as_str())
            .unwrap_or("PLUGIN_EXEC_FAILED");
        let message = err
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("插件执行失败");
        let _ = session
            .emit_event(
                thread_id,
                "plugin_error",
                serde_json::json!({
                    "pipeline_id": route_id,
                    "message_id": message_id,
                    "_threadId": thread_id,
                    "plugin_id": plugin_id,
                    "error": {
                        "code": code,
                        "message": message,
                        "source": "plugin",
                        "retryable": false,
                        "details": null,
                        "request_id": null,
                    },
                }),
            )
            .await;
    }
}

/// 用户→租户解析失败的拒分发收尾（K8 修订，ADR 2026-10-10）：
/// 返回错误串进 `Err`（router 转 `RouteOutcome::Error` → handle_inbound 回
/// 错误帧）。StoreError 额外对该用户全部连接下 4001 族关闭信号——
/// 基础设施故障不得降级 default 租户继续分发（fail-closed）。
fn reject_on_tenant_resolve_failure(
    state: &AppState,
    user_id: &str,
    err: agentos_http::auth::TenantResolveError,
) -> String {
    match err {
        agentos_http::auth::TenantResolveError::NotFound => {
            // 直接拒绝（拍板①）：已删用户 token 不再分发；解析点已 warn 留痕。
            format!("拒绝分发：用户 {user_id} 不存在（token 持有者已删除）")
        }
        agentos_http::auth::TenantResolveError::StoreError(e) => {
            // fail-closed：4001 族关闭该用户全部连接（refreshToken → 重连自愈）。
            if let Some(session) = state.session.as_ref() {
                for sink in session.registry().sinks_of_user(user_id) {
                    sink.shutdown_auth_reject();
                }
            }
            format!("拒绝分发：用户 {user_id} 租户解析存储故障（fail-closed）: {e}")
        }
    }
}

#[async_trait::async_trait]
impl PipelineDispatcher for EngineDispatcher {
    async fn frame_overlay_fields(&self) -> Vec<String> {
        // manifests / enabled 双活读（watcher 热发现与 enable 开关即时可见）
        let enabled = self.state.enabled_plugin_ids.read().await;
        let manifests = self.state.manifests.read().await;
        frame_overlay_fields_from_manifests(&manifests, &enabled)
    }

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
        pipeline_config_id: Option<&str>,
        client_message_id: &str,
        source: PendingInputSource,
    ) -> Result<(), String> {
        // 身份来源二分（刀2，ADR docs/decisions/2026-10-17-reverse-call-user-
        // identity-injection.md）：反调链（插件工具 → chat.send_message）在桥
        // 重建的身份 scope 内到达本派发器——tenant/user 以注入身份为准，形参
        // user_id 不可指派身份（与 `_identity` 信任模型同构）；无注入 user 的
        // 外部调用面（WS 前端 / HTTP / 通道桥 / 触发器）维持 user→tenant 解析。
        // 注入身份取自 task_local，须在 tokio::spawn 前由调用方（chat handler）
        // 捕获并 scope 重建——跨 spawn 丢失是 task_local 的防泄露 feature。
        let injected_identity = agentos_tenant::current()
            .filter(|c| c.user_id.as_deref().is_some_and(|u| !u.is_empty()));
        // tenant_id 必须用真正的租户 ID（与 HTTP 路径 resolve_request_tenant_id 同源），
        // 不能用 user_id 顶替——否则消息按 user_id 落库，读取时按 default 查不到，
        // 表现为「刷新后历史消息不显示」。
        // 外部面从 self.state.store 查持久化用户的 tenant_id（一用户一租户 = user_id）。
        // 解析失败拒分发（K8 修订 ADR 2026-10-10）：NotFound 直接拒绝；
        // StoreError fail-closed + 4001 族关闭——关闭后 handle_inbound 的常规
        // 错误帧会随连接终结丢失，user_input 的带坐标回执（回执契约
        // ADR 2026-09-29）必须在此先行补发。注入面跳过反查（原头可信）。
        let (tenant_id, effective_user) = match injected_identity {
            Some(ctx) => {
                let u = ctx.user_id.clone().unwrap_or_default();
                (ctx.tenant_id, u)
            }
            None => {
                match agentos_http::auth::resolve_tenant_id_by_user(
                    self.state.store.as_ref(),
                    user_id,
                )
                .await
                {
                    Ok(t) => (t, user_id.to_string()),
                    Err(e) => {
                        if matches!(e, agentos_http::auth::TenantResolveError::StoreError(_)) {
                            if let Some(session) = self.state.session.as_ref() {
                                let ack = json!({
                                    "type": "user_input_ack",
                                    "client_message_id": client_message_id,
                                    "thread_id": thread_id,
                                    "ok": false,
                                    "error": e.to_string(),
                                });
                                let ack_str = serde_json::to_string(&ack).unwrap_or_default();
                                session.registry().send_to_user(user_id, &ack_str).await;
                            }
                        }
                        return Err(reject_on_tenant_resolve_failure(&self.state, user_id, e));
                    }
                }
            }
        };
        let state = self.state.clone();
        let content = content.to_string();
        // pipeline_id 是前端消息路由键，引擎回推流式事件时用它定位占位气泡。
        //
        // 防御性校验（后端不盲目信任前端数据）：前端切换/新建会话后，activePipelineId
        // 可能因 React 渲染时序残留旧值，导致消息写到错误的 pipeline 桶（串消息）。
        // 这里查 pipeline_sessions 表校验 pipeline_id 是否确实属于该 thread_id：
        //   - 属于 → 信任前端值
        //   - 不属于 → 用该 thread 的真实 active_pipeline_id（主管道），杜绝串桶
        // 这是与前端源头修复（router.tsx 实时读 sessionStore）互补的双层防线。
        // 注意：resolve 必须在 tenant 构造前调用（TenantContext::new 会 move tenant_id）。
        let route_id = match state.store.as_ref() {
            Some(store) => {
                resolve_pipeline_id_for_thread(store, thread_id, pipeline_id, &tenant_id).await
            }
            // 无 store（降级模式）：无解析依据，透传前端值；空值由下方守卫拒绝
            None => pipeline_id.to_string(),
        };
        if route_id.is_empty() {
            // 会话无活跃管道且前端未带 pipeline_id：显式报错，不做 thread 回退
            // （执行坐标缺失属于协议违约，静默回退会写出幽灵管道数据）。
            return Err(
                "会话无可派发管道：pipeline_id 缺失或不属于该会话（拒绝静默换管道）".into(),
            );
        }
        // run scope 租户回填 user（刀2）：run 全链（引擎 → PluginContext.tenant →
        // `_identity` 正向注入 → 插件反调回带）由此拿到执行者用户。下游簿记
        // （pending record 归属 / run chain 优先级键 / 引擎 user）统一以
        // effective_user 为准（外部面 = 形参本体；注入面 = 注入身份）。
        let mut tenant = TenantContext::new(tenant_id, thread_id.to_string());
        tenant.user_id = Some(effective_user.clone());
        let user_id: &str = &effective_user;

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
            pipeline_config_id: pipeline_config_id.map(str::to_string),
            created_at: chrono::Utc::now().to_rfc3339(),
        };
        // store 未注入（单测/兼容路径）：无持久化队列，直接入链执行本轮（旧行为）。
        let Some(store) = state.store.clone() else {
            Self::spawn_chain(&state, tenant, record).await;
            return Ok(());
        };

        // ADR-2026-08-15：后台执行必须经 RunChainRegistry 入链——裸 spawn 会让
        // 同会话两条消息并发跑（registry 回写 / msg_sequence 竞态）。链保证同
        // 管道严格 FIFO、跨管道并行；user_id+route_id 兼作排队优先级键。
        let registry = crate::run_chain::RunChainRegistry::global();
        registry.note_user_pipeline(user_id, &route_id);

        // 条件入队（ADR-2026-09-11，修订 ADR-2026-08-26 的无条件入队）：
        // 链空闲 → 直跑本轮，不经持久化队列、不发 enqueued/consumed——前端乐观
        // 气泡与待处理队列条由此互斥（链空时消息"立即开始"，不存在等待视图）；
        // 链忙 → 持久化入队走消费循环，队列条语义收窄为"真正在等待的消息"
        // （等待窗口内 PUT/DELETE 编辑仍生效，重启后队列仍在）。直跑尾部 drain
        // 收走"占链竞态窗口内入队"的消息与重启遗留残留，FIFO 不破。
        let direct_state = state.clone();
        let direct_tenant = tenant.clone();
        let direct_thread = thread_id.to_string();
        let direct_route = route_id.clone();
        let direct_user = user_id.to_string();
        let direct_store = store.clone();
        // 链忙分支的入队副本（直跑闭包 move 走原 record；两分支互斥，仅一份生效）。
        let queued_record = record.clone();
        let ran_direct = registry.try_enqueue_direct(&route_id, user_id, async move {
            // 先跑本条（先到先跑），尾部 drain 消费队列直到空。
            let cmid = record.client_message_id.clone();
            let outcome = Self::run_pipeline_round_scoped(
                &direct_state,
                &direct_tenant,
                &direct_thread,
                &direct_user,
                record,
            )
            .await;
            let cancelled = outcome_cancelled(&outcome);
            let depth = pending_depth(&direct_store, &direct_tenant.tenant_id, &direct_route).await;
            if let Some(session) = direct_state.session.as_ref() {
                emit_round_finished_event(
                    session,
                    &direct_thread,
                    &direct_route,
                    outcome.failed,
                    cancelled,
                    depth,
                )
                .await;
            }
            notify_outcome_waiter(&cmid, outcome);
            Self::drain_pending_queue(
                direct_state,
                direct_tenant,
                direct_thread,
                direct_route,
                direct_user,
                direct_store,
            )
            .await;
        });
        if ran_direct {
            return Ok(());
        }

        // 链忙：消息落持久化队列，入链的消费任务在链空闲时按 FIFO pop 并激活
        // 执行。等待窗口内条目可经 PUT/DELETE 修改删除；消费时从表读最新参数
        // （内容不被闭包捕获）；重启后队列仍在，续跑。
        if let Err(e) = store
            .enqueue_pending_input(&tenant.tenant_id, &route_id, &queued_record)
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

        // 消费任务在链空闲时 pop 队列：pop 到空即退出（无轮询无自续接）。
        let chain_key = route_id.clone();
        let exec_user_key = user_id.to_string();
        registry.enqueue(&chain_key, &exec_user_key, {
            let exec_state = state.clone();
            let exec_thread = thread_id.to_string();
            let exec_route = route_id.clone();
            let exec_user = user_id.to_string();
            let exec_tenant = tenant;
            async move {
                Self::drain_pending_queue(
                    exec_state,
                    exec_tenant,
                    exec_thread,
                    exec_route,
                    exec_user,
                    store,
                )
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

        let mut respond_failed = false;
        // (A) 路径按服务角色路由（P1-3 声明驱动）：(namespace, method) 从插件
        // provides 声明的 interaction-respond 协议角色解析；应答载荷原样透传
        // （{request_id, response} 信封），载荷键契约由交互插件自持。
        let target =
            crate::server::resolve_interaction_responder(&self.state.manifests.read().await);
        match target {
            Some((namespace, method)) => {
                let inputs = serde_json::json!({
                    "request_id": request_id,
                    "response": response,
                });
                match self.state.capability_handlers.as_ref() {
                    Some(registry) => {
                        if let Err(e) = registry.route(&namespace, &method, inputs).await {
                            warn!(request_id = request_id, error = %e, "interaction.respond 调用失败");
                            respond_failed = true;
                        }
                    }
                    None => {
                        warn!(
                            request_id = request_id,
                            "capability registry 未注入，跳过 interaction.respond"
                        );
                        respond_failed = true;
                    }
                }
            }
            None => {
                warn!(
                    request_id = request_id,
                    "无插件声明 interaction-respond 协议角色，跳过 interaction.respond（fail-closed）"
                );
            }
        }

        if let Some(db) = self.state.db.as_ref() {
            match db.find_suspended_run_by_request_id(request_id) {
                Ok(Some(run)) => {
                    // resume 即运行簿记（runs 表退役，ADR 2026-09-18）：恢复
                    // Running 投影供查询/复盘语义（新引擎执行流由 state.suspended
                    // 插件机制控制，与 capability pipeline-executor.resume 一致）。
                    match run.pipeline_id.clone() {
                        Some(pipeline_id) => {
                            let mut fields = serde_json::Map::new();
                            fields.insert("run_status".to_string(), serde_json::json!("running"));
                            fields
                                .insert("suspend_request_id".to_string(), serde_json::Value::Null);
                            // 同批清挂起归因（P-23 片1）：与 suspend_request_id
                            // 同生命周期，恢复即清防跨恢复残留。
                            fields.insert("suspended_reason".to_string(), serde_json::Value::Null);
                            match db.upsert_state_fields(&pipeline_id, &run.tenant_id, &fields) {
                                Ok(()) => {
                                    info!(run_id = %run.run_id, request_id = request_id, "interaction_response 已唤醒 suspended run")
                                }
                                Err(e) => {
                                    warn!(request_id = request_id, error = %e, "suspended run resume 失败")
                                }
                            }
                        }
                        None => {
                            warn!(run_id = %run.run_id, request_id = request_id, "suspended run 无管道归属（state 无 run_id 键），跳过簿记")
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

    async fn dispatch_stop(&self, thread_id: &str, pipeline_id: &str) -> Result<(), String> {
        // 停止 = 复用 suspend_pipeline 的落库路径（方案 §四.1，批次 C）：
        // 把目标管道最新 Running run 置 Suspended —— 这是传输信号，不是终态。
        // llm_core 流式期间轮询到 suspended 后自行中断、落半截消息（status:
        // interrupted）+ 置 ended=true + router.stop_reason=user_requested，
        // 引擎既有 ended 边界检查让 run 优雅收尾（persist_run_end 落
        // Cancelled，不再覆写为 Completed）。无 Running run（run 已完成才点
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
            Some(uid) => {
                match agentos_http::auth::resolve_tenant_id_by_user(Some(store), &uid).await {
                    Ok(t) => t,
                    Err(e) => {
                        return Err(reject_on_tenant_resolve_failure(&self.state, &uid, e));
                    }
                }
            }
            // 注册表无映射（冷启动/新连接未注册）回退 default，与
            // suspend_pipeline 能力（current_or_default）语义一致——这是
            // 映射缺失（合法状态），不属解析失败，不在 K8 拒绝面内。
            None => "default".to_string(),
        };
        // 停止目标 = 前端正在查看的管道（一切管道相关操作必须携带管道 ID）。
        // 子任务管道挂自己的 thread（非会话 thread 成员），thread 成员校验会把它
        // 改写回主管道（历史 bug：点停止永远停不住子任务管道）——此处前端值
        // 非空即直采，仅空串（旧客户端）回退 thread 主管道。
        let target_pipeline = if pipeline_id.is_empty() {
            resolve_pipeline_id_for_thread(store, thread_id, "", &tenant_id).await
        } else {
            pipeline_id.to_string()
        };
        if target_pipeline.is_empty() {
            debug!(
                thread = thread_id,
                "stop_generation 无可定位管道（旧客户端未带 pipeline_id 且会话无主管道），幂等空转"
            );
            return Ok(());
        }
        let suspended = Self::suspend_pipeline_execution(store, &tenant_id, &target_pipeline).await;
        if !suspended {
            debug!(
                thread = thread_id,
                pipeline = %target_pipeline,
                "stop_generation 无 running run 且无在途派发（幂等空转）"
            );
        }
        // 用户裁定（2026-09-11）：停止 = 停当前 run + 待处理队列退回输入框。
        // 必须在此同步清队列（先于引擎收尾后的 drain_pending_queue）——否则
        // 排队消息会在停止后立即被消费，退回语义被消费竞争吃掉。退回条目随
        // pending_inputs_changed（action="returned"）发还前端回填输入框；
        // 其余客户端同通道收到空列表，队列条一致清空。
        // 来源分治（2026-10-27 装机实证：regenerate 重跑以 System 来源入队，
        // 被本清队一并出队后前端 returned 只回填 user 来源 → 重跑静默蒸发）：
        // 只退回 User 来源（用户输入回输入框是「退回」一词的语义边界）；
        // Trigger/Task/Http/System 条目留队，交链尾 drain 正常消费。
        match store
            .list_pending_inputs(&tenant_id, &target_pipeline)
            .await
        {
            Ok(rows) if !rows.is_empty() => {
                let mut returned: Vec<serde_json::Value> = Vec::new();
                for r in &rows {
                    if r.source != agentos_core::types::PendingInputSource::User {
                        continue;
                    }
                    returned.push(serde_json::json!({
                        "id": r.id,
                        "content": r.content,
                        "source": &r.source,
                        // cmid 随条目透出：前端按它撤下对应的 queued 乐观气泡
                        // （气泡正被回填输入框，不撤会双份显示）。
                        "client_message_id": &r.client_message_id,
                        "created_at": r.created_at,
                    }));
                    if let Err(e) = store
                        .delete_pending_input(&tenant_id, &target_pipeline, &r.id)
                        .await
                    {
                        warn!(
                            pipeline = %target_pipeline,
                            input = %r.id,
                            error = %e,
                            "stop_generation 退回队列条目删除失败（继续清理其余条目）"
                        );
                    }
                }
                if let Some(session) = self.state.session.as_ref() {
                    let _ = session
                        .emit_event(
                            thread_id,
                            "pending_inputs_changed",
                            serde_json::json!({
                                "pipeline_id": target_pipeline,
                                "thread_id": thread_id,
                                "action": "returned",
                                "items": returned,
                            }),
                        )
                        .await;
                }
                info!(
                    thread = thread_id,
                    pipeline = %target_pipeline,
                    returned = returned.len(),
                    "stop_generation user 来源队列条目已退回（非 user 来源留队交 drain）"
                );
            }
            Ok(_) => {}
            Err(e) => warn!(
                pipeline = %target_pipeline,
                error = %e,
                "stop_generation 待处理队列读取失败（跳过退回）"
            ),
        }
        Ok(())
    }

    /// 队列条目强制发送（2026-10-27 权威化，替代前端「DELETE→停止→重发」编排）。
    ///
    /// 语义 = 目标条目重排至队首 + 停当前回复（**不退回队列**）+ 链尾 drain
    /// 首个消费目标（原参数原样：cmid/thinking_strength/execution_context 随
    /// record 走）；链空闲则直接直跑目标、尾部 drain 接续其余条目。其余排队
    /// 条目全程留队继续排队——只影响被点名的这一条。
    /// 与 dispatch_stop 的边界：停止按钮 = 停 run + 队列退回输入框（2026-09-11
    /// 裁定），本原语不动该语义。
    async fn dispatch_force_pending(
        &self,
        user_id: &str,
        thread_id: &str,
        pipeline_id: &str,
        input_id: &str,
    ) -> Result<(), String> {
        let Some(store) = self.state.store.as_ref() else {
            return Err("force_pending_input disabled: kernel store not injected".to_string());
        };
        let user_id = self
            .state
            .session
            .as_ref()
            .and_then(|s| s.registry().get_user_for_thread(thread_id))
            .unwrap_or_else(|| user_id.to_string());
        let tenant_id =
            match agentos_http::auth::resolve_tenant_id_by_user(Some(store), &user_id).await {
                Ok(t) => t,
                Err(e) => return Err(reject_on_tenant_resolve_failure(&self.state, &user_id, e)),
            };
        let target_pipeline = if pipeline_id.is_empty() {
            resolve_pipeline_id_for_thread(store, thread_id, "", &tenant_id).await
        } else {
            pipeline_id.to_string()
        };
        if target_pipeline.is_empty() {
            return Err("force_pending_input 缺少 pipeline_id".into());
        }

        // 1. 目标置队首（FIFO 键 created_at 置最早）+ 存在性校验：一条 UPDATE
        //    完成。不再 list→pop 全量→逐条回队——该三段式与链尾 drain、引擎轮
        //    边界注入并发时会重复执行或丢条目（2026-10-29 诊断）。
        let promoted = store
            .promote_pending_input(&tenant_id, &target_pipeline, input_id)
            .await
            .map_err(|e| format!("force_pending_input 目标置队首失败: {e}"))?;
        if !promoted {
            return Err("目标条目不在队列中（可能已被消费），队列视图已刷新".into());
        }

        // 2. 停当前回复（不退回队列）：run 在飞 → suspended；停止中/终态 → 幂等；
        //    首 token 前空窗 → 待取消标记。链尾 drain 随后弹出队首（= 目标）首个消费。
        let suspended = Self::suspend_pipeline_execution(store, &tenant_id, &target_pipeline).await;
        if !suspended {
            // 链空闲（无 run 可停、无在途派发）：占链跑 drain——目标已在队首，
            // drain 首个消费它，其余条目按原序接续（复用消费循环，不再复制
            // 一份「跑目标 + emit + notify + drain」的直跑逻辑）。
            let registry = crate::run_chain::RunChainRegistry::global();
            registry.note_user_pipeline(&user_id, &target_pipeline);
            let mut tenant = TenantContext::new(tenant_id.clone(), thread_id.to_string());
            tenant.user_id = Some(user_id.clone());
            let drain_state = self.state.clone();
            let drain_thread = thread_id.to_string();
            let drain_route = target_pipeline.clone();
            let drain_user = user_id.clone();
            let drain_store = store.clone();
            registry.try_enqueue_direct(&target_pipeline, &user_id, async move {
                Self::drain_pending_queue(
                    drain_state,
                    tenant,
                    drain_thread,
                    drain_route,
                    drain_user,
                    drain_store,
                )
                .await;
            });
        }
        info!(
            thread = thread_id,
            pipeline = %target_pipeline,
            input = %input_id,
            suspended_now = suspended,
            "force_pending_input 已落地：目标条目置队首（其余留队），停当前回复不退回"
        );

        // 3. 队列全量同步（重排后的权威列表，多端一致）。
        emit_pending_inputs_changed(
            &self.state,
            thread_id,
            &target_pipeline,
            &tenant_id,
            store,
            "updated",
        )
        .await;
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
        // 租户解析失败拒登记（K8 修订 ADR 2026-10-10）。
        let tenant_id =
            match agentos_http::auth::resolve_tenant_id_by_user(self.state.store.as_ref(), user_id)
                .await
            {
                Ok(t) => t,
                Err(e) => {
                    return Err(reject_on_tenant_resolve_failure(&self.state, user_id, e));
                }
            };
        let route_id = match self.state.store.as_ref() {
            Some(store) => {
                resolve_pipeline_id_for_thread(store, thread_id, pipeline_id, &tenant_id).await
            }
            None => pipeline_id.to_string(),
        };
        if route_id.is_empty() {
            debug!(
                thread = thread_id,
                "dispatch_active_thread 无可定位管道，跳过活跃管道登记"
            );
            return Ok(());
        }
        crate::run_chain::RunChainRegistry::global().note_user_pipeline(user_id, &route_id);
        // 域事件插座：session.active_changed → 观察总线 + 声明订阅的插件。
        crate::plugin_lifecycle::broadcast_domain_event(
            &self.state,
            "session.active_changed",
            vec![
                ("session_id".to_string(), serde_json::json!(thread_id)),
                (
                    "pipeline_id".to_string(),
                    serde_json::json!(route_id.as_str()),
                ),
                ("user_id".to_string(), serde_json::json!(user_id)),
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
        let tenant_id =
            match agentos_http::auth::resolve_tenant_id_by_user(Some(store), user_id).await {
                Ok(t) => t,
                Err(e) => return Err(reject_on_tenant_resolve_failure(&self.state, user_id, e)),
            };
        let route_id =
            resolve_pipeline_id_for_thread(store, thread_id, pipeline_id, &tenant_id).await;
        if route_id.is_empty() {
            return Err("regenerate 缺少 pipeline_id".into());
        }
        // 运行态守卫（与 segment_activate 同款）：链忙（run 在飞/收尾在途）时
        // 拒绝截断——在飞 run 仍在向被截后缀 append，截断落库与其竞争必出分叉
        // （2026-10-27 装机实证：在飞回退 → 空 assistant 槽位 + 重跑滞留队列被
        // 停止清队吞掉）。拒绝走 route_error 通道，用户指引先停止再重试。
        if crate::run_chain::RunChainRegistry::global().has_chain(&route_id) {
            return Err("当前有回复正在进行，请先停止生成再重试/回退".into());
        }

        // 1. 读历史：目标 user 消息（按 message_id 定位；缺省 = 最后一条 user）。
        //    消息层按 (pipeline_id, tenant) 查询，tenant scope 由调用方
        //    （WS 路径）保证——store 实现经 task_local 解析租户。
        let messages = store
            .get_messages_by_pipeline(&route_id, MessageQueryOpts::default())
            .await
            .map_err(|e| format!("regenerate 读历史失败: {e}"))?;
        let target =
            resolve_regenerate_target(store, &route_id, &tenant_id, &messages, user_message_id)
                .await?;
        let target_seq = target.seq();

        // 1b. 审计锚预检：Live 路径要求目标消息带有效 run_id（审计不可见 = 操作
        //     不可执行，不合成占位 id）；Ledger 路径的锚 = 账本轨迹（resolve 时
        //     已验指纹与 role），run 锚随槽位改写/删除不可恢复，以账本锚替代——
        //     rollback trace 如实记录 resolution 与账本指针。
        let trace_run_id = match &target {
            RegenTarget::Live { run_id, .. } => run_id.clone(),
            RegenTarget::Ledger { .. } => String::new(),
        };

        // 2. 构造截断 ops：被换后缀先冻结为段（消息段模型 §5 写路径——截断前
        //    冻结，切换无损不变量；与截断 ops 同批同事务）→ 目标 user 之后的全部
        //    槽位 set(seq,null)（删槽留洞，context_window_guard 先例；后段 seq/id
        //    不变）→ 目标槽位 op（Live 仅编辑重发改写内容；Ledger 恢复原文/以原文
        //    metadata 写新内容）。
        let mut ops: Vec<serde_json::Value> = Vec::new();
        // 段统一含目标 user 消息（[target_seq..末尾]，base_seq = target_seq）：
        // edit-resend 会原地改写目标槽位，不含 user 消息则旧文本不在任何段里，
        // 切回旧代 = 新用户文本配旧轮次（激活连用户消息一起恢复，编辑/纯重试
        // 两态统一正确）。冻结成员取槽位全文（get_messages 的 preview 是截断
        // 变体，不能作冻结原文）；成员剥离 seq（位置标记属槽位坐标，冻结原文
        // 携带会在写回时变成陈旧位置残留）。
        let full_history = store
            .load_message_history(&route_id, &tenant_id)
            .await
            .map_err(|e| format!("regenerate 读全文历史失败: {e}"))?;
        let replaced_suffix: Vec<serde_json::Value> = full_history
            .into_iter()
            .filter_map(|mut m| {
                let seq = m.get("seq").and_then(|v| v.as_u64())? as u32;
                if seq < target_seq {
                    return None;
                }
                if let Some(o) = m.as_object_mut() {
                    o.remove("seq");
                }
                Some(m)
            })
            .collect();
        ops.push(freeze_segment_op(target_seq, replaced_suffix));
        match &target {
            RegenTarget::Live { seq, .. } => {
                if let Some(content) = new_content {
                    if let Some(target) = messages.iter().find(|m| m.seq_in_branch == *seq) {
                        let mut msg = serde_json::json!({"role": "user", "content": content});
                        if let Some(meta) = target.metadata.clone() {
                            msg["metadata"] = meta;
                        }
                        ops.push(serde_json::json!({"op": "set", "seq": seq, "msg": msg}));
                    }
                }
            }
            RegenTarget::Ledger {
                seq, original_msg, ..
            } => {
                let mut msg = match new_content {
                    Some(content) => serde_json::json!({"role": "user", "content": content}),
                    None => original_msg.clone(),
                };
                if let Some(meta) = original_msg.get("metadata") {
                    msg["metadata"] = meta.clone();
                }
                ops.push(serde_json::json!({"op": "set", "seq": seq, "msg": msg}));
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
        // Live 路径 run_id = 目标消息所在 run（1b 预检锚定，审计反查可见）；
        // Ledger 路径 run 锚不可恢复，如实记空并以账本指针替代（可反查原文）。
        let now = chrono::Utc::now().to_rfc3339();
        let mut patch_data = serde_json::json!({
            "rollback_to_user_seq": target_seq,
            "user_message_id": user_message_id,
            "run_id": trace_run_id,
            "pipeline_id": route_id,
            "messages": { "_ops": ops },
        });
        if let RegenTarget::Ledger {
            ledger_trace_id,
            ledger_blob_id,
            ..
        } = &target
        {
            patch_data["resolution"] = serde_json::json!("trace_ledger");
            patch_data["ledger_trace_id"] = serde_json::json!(ledger_trace_id);
            patch_data["ledger_blob_id"] = serde_json::json!(ledger_blob_id);
        }
        let entry = TraceEntry {
            trace_id: format!("t_{}", uuid::Uuid::new_v4().simple()),
            pipeline_id: route_id.clone(),
            seq: 0,
            plugin_id: "chat_regenerate".to_string(),
            patch_type: PatchType::Rollback,
            patch_data,
            created_at: now,
        };
        store
            .append_trace(entry)
            .await
            .map_err(|e| format!("regenerate rollback trace 失败: {e}"))?;

        // 5. 截断事件（与 new_message 同通道）：前端据此收敛本地消息视图。
        //    编辑重发时补 updated_messages（改写后目标 user 消息权威全文，按
        //    seq 原地更新）——前端不再乐观改写，内容收敛唯一走本事件。
        if let Some(session) = self.state.session.as_ref() {
            let mut payload = serde_json::json!({
                "pipeline_id": route_id,
                "thread_id": thread_id,
                "truncate_before_seq": target_seq + 1,
                "regenerate": true,
                "_threadId": thread_id,
            });
            if let Some(content) = new_content {
                payload["updated_messages"] = serde_json::json!([
                    { "sequence": target_seq, "role": "user", "content": content }
                ]);
            }
            let _ = session
                .emit_event(thread_id, "messages_truncated", payload)
                .await;
        }

        // 6. 重跑：目标 user 消息已在截断后历史中（Live 原样在场 / Ledger 已
        //    恢复原文），跳过本轮 append。Ledger 回退重跑用账本原文全文（槽位
        //    preview 是压缩变体或短摘要，都不是用户指向的那条话）。
        let content = match &target {
            RegenTarget::Live { seq, .. } => new_content
                .map(|c| c.to_string())
                .or_else(|| {
                    messages
                        .iter()
                        .find(|m| m.seq_in_branch == *seq)
                        .and_then(|m| m.content_preview.clone())
                })
                .unwrap_or_default(),
            RegenTarget::Ledger { original_msg, .. } => new_content
                .map(|c| c.to_string())
                .or_else(|| {
                    original_msg
                        .get("content")
                        .and_then(|v| v.as_str())
                        .map(str::to_string)
                })
                .unwrap_or_default(),
        };
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
            None,
            "",
            agentos_core::types::PendingInputSource::System,
        )
        .await
        .map_err(|e| format!("regenerate 重跑派发失败: {e}"))?;
        Ok(())
    }

    /// 激活消息段（多代切换，消息段模型 §2.5 后缀语义 + §7.3 补全）。
    ///
    /// 单批 ops 单事务，失败零写入：
    /// ① 当前 `[seg.base_seq..末尾]` 非空后缀先整体冻结为新段（无损，
    ///    append-only；visible_to=''，id 服务端 seg_&lt;uuid&gt;，preview 取首条）；
    /// ② 目标段成员自 base_seq 逐槽 set 写回（目标更长则新增槽）；当前后缀
    ///    更长则多余槽 set null（写回按目标段内容，多余槽不清理会双活）。
    /// 运行态守卫：run 活跃期间拒绝（后缀替换会与引擎 append 竞争）。
    /// 幂等：当前后缀与目标段逐条同指纹 → 已激活态，零变化仅 ack。
    /// 多端并发后写赢（输家目标段仍在段表，可再次切换，零丢失）。
    async fn dispatch_segment_activate(
        &self,
        user_id: &str,
        thread_id: &str,
        pipeline_id: &str,
        segment_id: &str,
    ) -> Result<(), String> {
        let Some(store) = self.state.store.as_ref() else {
            return Err("segment_activate disabled: kernel store not injected".to_string());
        };
        if segment_id.is_empty() {
            return Err("segment_activate 缺少 segmentId".into());
        }
        let tenant_id =
            match agentos_http::auth::resolve_tenant_id_by_user(self.state.store.as_ref(), user_id)
                .await
            {
                Ok(t) => t,
                Err(e) => {
                    return Err(reject_on_tenant_resolve_failure(&self.state, user_id, e));
                }
            };
        let route_id =
            resolve_pipeline_id_for_thread(store, thread_id, pipeline_id, &tenant_id).await;
        if route_id.is_empty() {
            return Err("segment_activate 缺少 pipeline_id".into());
        }
        // 运行态守卫（§7.3-1）：复用既有「管道是否在跑」判定（runs 表退役后
        // 由 store 读 state run_status 控制键合成）。
        let runs = store
            .list_runs_by_pipeline(&route_id, &tenant_id)
            .await
            .map_err(|e| format!("segment_activate 查询 run 失败: {e}"))?;
        if runs
            .iter()
            .any(|r| r.status == agentos_core::types::RunStatus::Running)
        {
            return Err("任务运行中".to_string());
        }
        // 目标段：必须存在且属于本管道（段 id 全局唯一，跨管道引用 = 客户端错）。
        let segment = store
            .get_message_segment(segment_id, &tenant_id)
            .await
            .map_err(|e| format!("segment_activate 读段失败: {e}"))?
            .ok_or_else(|| format!("segment_activate 段不存在: {segment_id}"))?;
        if segment.pipeline_id != route_id {
            return Err(format!(
                "segment_activate 段 {segment_id} 不属于管道 {route_id}"
            ));
        }
        let members = segment.members.clone().unwrap_or_default();

        // 当前 [base_seq..末尾] 后缀（全文 + 槽位 seq，成员剥离位置标记）。
        let full_history = store
            .load_message_history(&route_id, &tenant_id)
            .await
            .map_err(|e| format!("segment_activate 读历史失败: {e}"))?;
        let mut suffix: Vec<(u32, serde_json::Value)> = full_history
            .into_iter()
            .filter_map(|mut m| {
                let seq = m.get("seq").and_then(|v| v.as_u64())? as u32;
                if seq < segment.base_seq as u32 {
                    return None;
                }
                if let Some(o) = m.as_object_mut() {
                    o.remove("seq");
                }
                Some((seq, m))
            })
            .collect();
        suffix.sort_by_key(|(seq, _)| *seq);

        // 幂等预检（§7.3-2）：当前后缀与目标段逐条同指纹 → 已激活态，零变化仅 ack
        // （指纹规范化剥离 seq/_ 前缀字段，带槽位 seq 的后缀与冻结成员可比）。
        let already_active = suffix.len() == members.len()
            && suffix.iter().zip(members.iter()).all(|((_, cur), tgt)| {
                agentos_core::ids::compute_message_id(cur)
                    == agentos_core::ids::compute_message_id(tgt)
            });
        if already_active {
            emit_segment_activated(&self.state, thread_id, &route_id, segment_id).await;
            return Ok(());
        }

        // 冻结查重（分支重复增殖根修）：当前后缀与同 base_seq 其他已有段逐条
        // 同指纹 → 该内容已有归档行，跳过冻结（无损不变量不破坏——已有段仍可
        // 切换激活）。幂等预检只覆盖「后缀==目标段」；A↔B 来回切换时被换后缀
        // 与对方段同内容，不查重则每切一次段表涨一行，‹i/n› 的 n 虚增长。
        let mut suffix_already_archived = false;
        if !suffix.is_empty() {
            let segments = store
                .list_message_segments(&route_id, &tenant_id)
                .await
                .map_err(|e| format!("segment_activate 查询段清单失败: {e}"))?;
            for seg in segments {
                if seg.base_seq != segment.base_seq
                    || seg.id == segment_id
                    || seg.base_len != suffix.len() as i64
                {
                    continue;
                }
                let existing = store
                    .get_message_segment(&seg.id, &tenant_id)
                    .await
                    .map_err(|e| format!("segment_activate 读段失败: {e}"))?;
                if let Some(members) = existing.and_then(|s| s.members) {
                    if members.len() == suffix.len()
                        && members.iter().zip(suffix.iter()).all(|(tgt, (_, cur))| {
                            agentos_core::ids::compute_message_id(cur)
                                == agentos_core::ids::compute_message_id(tgt)
                        })
                    {
                        suffix_already_archived = true;
                        break;
                    }
                }
            }
        }

        // ①当前后缀先整体冻结（冻结先于覆写——切换无损不变量；同内容已有
        // 归档行则跳过，见上方查重）；②目标段内容逐槽写回；当前更长则按剩余
        // 后缀的实际槽位逐槽 set null。
        let mut ops: Vec<serde_json::Value> = Vec::new();
        if !suffix.is_empty() && !suffix_already_archived {
            let frozen: Vec<serde_json::Value> = suffix.iter().map(|(_, m)| m.clone()).collect();
            ops.push(freeze_segment_op(segment.base_seq as u32, frozen));
        }
        for (i, member) in members.iter().enumerate() {
            let seq = segment.base_seq + i as i64;
            let mut op = serde_json::json!({"op": "set", "seq": seq, "msg": member});
            // 成员携带的身份与 run 审计锚投影到 op 内部字段（store 写槽位行时消费）：
            // 缺了它们，写回后的槽位行 run_id=NULL → regenerate 审计门禁拒
            // 「目标 user 消息无有效 run_id」且前端失去稳定消息 id（用户实证）。
            // 旧段成员（本修复前冻结）无此二字段 → 保持原行为（写回 NULL）。
            if let Some(mid) = member.get("message_id").and_then(|v| v.as_str()) {
                op["_message_id"] = serde_json::json!(mid);
            }
            if let Some(rid) = member.get("run_id").and_then(|v| v.as_str()) {
                op["_run_id"] = serde_json::json!(rid);
            }
            ops.push(op);
        }
        for (seq, _) in suffix.iter().skip(members.len()) {
            ops.push(serde_json::json!({"op": "set", "seq": seq, "msg": serde_json::Value::Null}));
        }
        store
            .apply_messages_ops_to_table(&route_id, &tenant_id, &ops)
            .await
            .map_err(|e| format!("segment_activate 落库失败: {e}"))?;

        // trace 落痕（chat_regenerate 审计痕形态推广）：patch_data 记目标段行 +
        // 全部 ops（含新冻结段的 freeze_segment op 全文——DR 重放即重建段表）。
        let entry = TraceEntry {
            trace_id: format!("t_{}", uuid::Uuid::new_v4().simple()),
            pipeline_id: route_id.clone(),
            seq: 0,
            plugin_id: "chat_regenerate".to_string(),
            patch_type: PatchType::Rollback,
            patch_data: serde_json::json!({
                "action": "segment_activate",
                "pipeline_id": route_id,
                "activated_segment": {
                    "id": segment.id,
                    "base_seq": segment.base_seq,
                    "base_len": segment.base_len,
                    "visible_to": segment.visible_to,
                    "preview": segment.preview,
                },
                "messages": { "_ops": ops },
            }),
            created_at: chrono::Utc::now().to_rfc3339(),
        };
        store
            .append_trace(entry)
            .await
            .map_err(|e| format!("segment_activate trace 失败: {e}"))?;

        emit_segment_activated(&self.state, thread_id, &route_id, segment_id).await;
        Ok(())
    }
}

/// segment_activated ack 事件（多端对账，与 messages_truncated 同通道下发）。
mod dispatch_support;

use dispatch_support::{
    emit_pending_inputs_changed, emit_pending_inputs_changed_with, emit_segment_activated,
    freeze_segment_op, message_status_from_blob, resolve_pipeline_id_for_thread,
    resolve_regenerate_target, RegenTarget,
};
#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use super::frame_overlay_fields_from_manifests;
    use agentos_core::{
        traits::{MessageQueryOpts, PluginManifest, SessionListFilter, StorageBackend},
        types::{
            MessageRecord, PendingInputSource, RunRecord, RunStatus, SessionRecord, StorageError,
            TraceEntry,
        },
    };
    use agentos_session::{router::PipelineDispatcher, EventSink};
    use async_trait::async_trait;
    use serde_json::json;

    use super::dispatch_support::find_target_user_seq;
    use super::{
        assistant_authoritative_seq, emit_pending_inputs_changed, emit_round_finished_event,
        message_status_from_blob, notify_outcome_waiter, register_outcome_waiter,
        remove_outcome_waiter, resolve_dispatch_agent, resolve_pipeline_id_for_thread,
        thread_owned_by_user, EngineDispatcher, WsSink, CLOSE_AUTH_REJECT, CLOSE_BACKPRESSURE,
        CLOSE_KICKED, CLOSE_NONE, WS_OUTBOUND_CAPACITY,
    };

    /// resolve_pipeline_id_for_thread 行为测试专用 mock：
    /// 只控制 list_pipeline_ids_by_thread（Err / 成员列表）与 get_session（活跃管道），
    /// 其余走 stub（契约 mock 同款）。
    /// get_user_by_id 注入态（K8 修订测试用）：外层 None = 合成 default 租户
    /// 用户（"用户在库"事实语义，默认）；Some(Ok(None)) = 已删用户 /
    /// Some(Err(..)) = 存储故障 / Some(Ok(Some(u))) = 指定租户用户。
    type UserLookup = Mutex<Option<Result<Option<agentos_core::types::UserRecord>, StorageError>>>;

    fn synthetic_default_user(user_id: &str) -> agentos_core::types::UserRecord {
        agentos_core::types::UserRecord {
            user_id: user_id.to_string(),
            username: format!("u-{user_id}"),
            password: String::new(),
            email: None,
            role: "user".to_string(),
            tenant_id: "default".to_string(),
            created_at: "2026-10-10T00:00:00Z".to_string(),
            last_login_at: None,
            must_change_password: false,
        }
    }

    struct ResolveMock {
        list_result: Mutex<Result<Vec<String>, StorageError>>,
        session: Mutex<Option<SessionRecord>>,
        /// get_session 恒故障开关（冷路径解析失败分支测试用，默认 false）。
        get_session_err: Mutex<bool>,
        /// pop_pending_input 恒故障开关（pending 消费出队失败分支测试用）。
        pop_pending_err: Mutex<bool>,
        /// list_pending_inputs 的返回（默认空列表；可注入故障或预置行）。
        list_pending_result:
            Mutex<Result<Vec<agentos_core::types::PendingInputRecord>, StorageError>>,
        /// load_pipeline_state 返回的持久化标量字段（agent 绑定冷读真值用）。
        state_fields: Mutex<std::collections::HashMap<String, serde_json::Value>>,
        /// get_user_by_id 注入态（None = 合成 default 租户用户）。
        user_lookup: UserLookup,
        /// list_pipeline_ids_by_thread 收到的租户实参（分发租户解析面断言用）。
        seen_tenant_args: Mutex<Vec<String>>,
    }

    impl ResolveMock {
        fn new(
            list_result: Result<Vec<String>, StorageError>,
            session: Option<SessionRecord>,
        ) -> Self {
            Self {
                list_result: Mutex::new(list_result),
                session: Mutex::new(session),
                get_session_err: Mutex::new(false),
                pop_pending_err: Mutex::new(false),
                list_pending_result: Mutex::new(Ok(Vec::new())),
                state_fields: Mutex::new(std::collections::HashMap::new()),
                user_lookup: Mutex::new(None),
                seen_tenant_args: Mutex::new(Vec::new()),
            }
        }

        /// get_session 恒故障变体（fail-closed / 回退默认分支测试用）。
        fn new_failing_get_session(list_result: Result<Vec<String>, StorageError>) -> Self {
            let mock = Self::new(list_result, None);
            *mock.get_session_err.lock().unwrap() = true;
            mock
        }

        /// 预置 load_pipeline_state 返回的持久化标量字段（绑定真值）。
        fn set_state_fields(&self, fields: serde_json::Value) {
            let mut map = self.state_fields.lock().unwrap();
            if let Some(obj) = fields.as_object() {
                for (k, v) in obj {
                    map.insert(k.clone(), v.clone());
                }
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

    /// 播种测试用户（tenant=default，对齐 admin 种子形态）：K8 修订
    /// （ADR 2026-10-10）后租户解析按真值走、无降级，真实 SqliteStore
    /// 场景的测试用户必须先在库。
    async fn seed_default_user(store: &dyn StorageBackend, user_id: &str) {
        store
            .create_user(&synthetic_default_user(user_id))
            .await
            .unwrap();
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
            if *self.get_session_err.lock().unwrap() {
                return Err(StorageError::Database(
                    "injected get_session failure".to_string(),
                ));
            }
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
        async fn delete_session(&self, _thread_id: &str) -> Result<Vec<String>, StorageError> {
            Ok(Vec::new())
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
            tenant_id: &str,
        ) -> Result<Vec<String>, StorageError> {
            self.seen_tenant_args
                .lock()
                .unwrap()
                .push(tenant_id.to_string());
            self.list_result.lock().unwrap().clone()
        }
        async fn load_pipeline_state(
            &self,
            _pipeline_id: &str,
            _tenant_id: &str,
        ) -> Result<std::collections::HashMap<String, serde_json::Value>, StorageError> {
            Ok(self.state_fields.lock().unwrap().clone())
        }
        async fn pop_pending_input(
            &self,
            _tenant_id: &str,
            _pipeline_id: &str,
        ) -> Result<Option<agentos_core::types::PendingInputRecord>, StorageError> {
            if *self.pop_pending_err.lock().unwrap() {
                return Err(StorageError::Database(
                    "injected pop_pending_input failure".to_string(),
                ));
            }
            Ok(None)
        }
        async fn list_pending_inputs(
            &self,
            _tenant_id: &str,
            _pipeline_id: &str,
        ) -> Result<Vec<agentos_core::types::PendingInputRecord>, StorageError> {
            self.list_pending_result.lock().unwrap().clone()
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
            user_id: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, StorageError> {
            // 注入态优先；默认合成 default 租户用户（"用户在库"事实语义，
            // 租户解析按真值走——K8 修订后无降级）。
            match self.user_lookup.lock().unwrap().clone() {
                None => Ok(Some(synthetic_default_user(user_id))),
                Some(injected) => injected,
            }
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

        async fn update_user_password(
            &self,
            _user_id: &str,
            _password_hash: &str,
            _must_change_password: bool,
        ) -> Result<bool, StorageError> {
            unreachable!("ResolveMock 不提供口令更新")
        }
        async fn delete_user(&self, _user_id: &str) -> Result<bool, StorageError> {
            Ok(false)
        }
    }

    async fn resolve(mock: ResolveMock, thread_id: &str, frontend_pipeline_id: &str) -> String {
        let store = Arc::new(mock) as Arc<dyn StorageBackend>;
        resolve_pipeline_id_for_thread(&store, thread_id, frontend_pipeline_id, "tenant-1").await
    }

    mod cancel_poisoning;
    mod dispatch_user_input;
    mod e2e_fullchain;
    mod handle_inbound_tests;
    mod regenerate;
    mod segments;
    mod stop_clearance;
    mod support;

    use self::dispatch_guards::frame_types;
    use self::support::{seed_three_slot_history, CapturingSink};
    mod dispatch_guards;
    // ── emit_stream_error_event：统一错误信封 ──────────────────────────────

    #[tokio::test]
    async fn stream_error_event_carries_unified_error_envelope() {
        let coord = agentos_session::SessionCoordinator::new();
        let frames = Arc::new(Mutex::new(Vec::<String>::new()));
        coord.register("u-se", Arc::new(CapturingSink::with_frames(frames.clone())));
        coord.register_thread("T-se", "u-se");
        super::emit_stream_error_event(
            &coord,
            "T-se",
            "pipe-se",
            "a_msg1",
            "ENGINE_RUN_FAILED",
            "引擎冒烟",
        )
        .await;
        let all = frame_types(&frames).await;
        let frame = all
            .iter()
            .find(|(t, _)| t == "stream_error")
            .map(|(_, v)| v.clone())
            .expect("stream_error 帧必须发出");
        assert_eq!(frame["data"]["pipeline_id"], "pipe-se");
        assert_eq!(frame["data"]["message_id"], "a_msg1");
        assert_eq!(frame["data"]["_threadId"], "T-se");
        assert_eq!(frame["data"]["error"]["code"], "ENGINE_RUN_FAILED");
        assert_eq!(frame["data"]["error"]["message"], "引擎冒烟");
        assert_eq!(frame["data"]["error"]["source"], "kernel");
        assert_eq!(frame["data"]["error"]["retryable"], true);
    }

    // ── emit_pending_inputs_changed：成功路径携带 items ────────────────────

    #[tokio::test]
    async fn pending_inputs_changed_carries_seeded_items() {
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let store: Arc<dyn StorageBackend> = sqlite.clone();
        let pipe = format!(
            "pipe-pin-{}",
            &uuid::Uuid::new_v4().simple().to_string()[..8]
        );
        store
            .enqueue_pending_input(
                "default",
                &pipe,
                &agentos_core::types::PendingInputRecord {
                    id: "p_pin1".to_string(),
                    pipeline_id: pipe.clone(),
                    tenant_id: "default".to_string(),
                    user_id: "u-pin".to_string(),
                    content: "排队中".to_string(),
                    thread: "T-pin".to_string(),
                    source: agentos_core::types::PendingInputSource::User,
                    agent_id: String::new(),
                    route_id: pipe.clone(),
                    thinking_strength: String::new(),
                    client_message_id: String::new(),
                    execution_context: None,
                    state_overlay: None,
                    pipeline_config_id: None,
                    created_at: "2026-09-13T00:00:00Z".to_string(),
                },
            )
            .await
            .unwrap();

        let coord = agentos_session::SessionCoordinator::new();
        let frames = Arc::new(Mutex::new(Vec::<String>::new()));
        coord.register(
            "u-pin",
            Arc::new(CapturingSink::with_frames(frames.clone())),
        );
        coord.register_thread("T-pin", "u-pin");
        let mut state = crate::routes::AppState::new();
        state.store = Some(store.clone());
        state.session = Some(Arc::new(coord));
        super::emit_pending_inputs_changed(&state, "T-pin", &pipe, "default", &store, "enqueued")
            .await;

        let all = frame_types(&frames).await;
        let frame = all
            .iter()
            .find(|(t, _)| t == "pending_inputs_changed")
            .map(|(_, v)| v.clone())
            .expect("事件必须发出");
        assert_eq!(frame["data"]["action"], "enqueued");
        assert_eq!(frame["data"]["pipeline_id"], pipe);
        assert_eq!(frame["data"]["items"].as_array().unwrap().len(), 1);
        assert_eq!(frame["data"]["items"][0]["content"], "排队中");
    }

    // ── 补充分支：plugin_error 零错误空转 / WsSink 关闭与断连通道 ──────

    /// 捕获型 sink（事件帧断言用，同本模块 CapturingSink）。
    struct EventCapture {
        frames: Arc<Mutex<Vec<String>>>,
    }

    #[async_trait::async_trait]
    impl agentos_session::EventSink for EventCapture {
        async fn send_text(&self, text: &str) -> bool {
            self.frames.lock().unwrap().push(text.to_string());
            true
        }
        fn id(&self) -> u64 {
            99
        }
    }

    #[tokio::test]
    async fn plugin_error_zero_errors_emits_nothing() {
        // 零错误：不发任何帧（plugin_error 是非终止信号，正常收尾路径零噪声）
        let coord = agentos_session::SessionCoordinator::new();
        let frames = Arc::new(Mutex::new(Vec::<String>::new()));
        coord.register(
            "u1",
            Arc::new(EventCapture {
                frames: frames.clone(),
            }),
        );
        coord.register_thread("thread-ev-zero", "u1");
        let outcome = crate::server::EngineOutcome {
            content: String::new(),
            final_assistant: None,
            failed: false,
            degraded: false,
            plugin_errors: Vec::new(),
            consumed_pending_cmids: Vec::new(),
        };
        super::emit_plugin_error_events(&coord, "thread-ev-zero", "pipe-ev", "m", &outcome).await;
        assert!(frames.lock().unwrap().is_empty(), "零错误零帧");
    }

    #[tokio::test]
    async fn ws_sink_shutdown_signals_kicked_before_queue_pressure() {
        // 队列未满时 shutdown → watch 置 kicked（超限踢旧信号不依赖队列容量）
        let (sink, _rx, mut close_rx) = WsSink::new();
        assert!(sink.send_text("normal").await);
        sink.shutdown();
        assert_eq!(
            *close_rx.borrow_and_update(),
            CLOSE_KICKED,
            "shutdown 必须置 kicked 关闭信号"
        );
    }

    #[tokio::test]
    async fn ws_sink_send_after_receiver_dropped_returns_false() {
        // 接收端已弃（排空任务退出）→ Closed → false（registry 据此注销连接）
        let (sink, rx, _close_rx) = WsSink::new();
        drop(rx);
        assert!(!sink.send_text("orphan").await, "Closed 通道应投递失败");
    }

    // ── 帧字段 overlay 名单收集（widget 声明推导）────────────────────────

    /// 最小 manifest 构造（仅 id/ui_schema 参与收集语义，其余字段取缺省形态）
    fn manifest_with_ui_schema(id: &str, ui_schema: Option<serde_json::Value>) -> PluginManifest {
        PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
            id: id.to_string(),
            name: id.to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: agentos_core::traits::PluginType::Pipeline,
            language: "python".to_string(),
            host_type: agentos_core::traits::HostType::Sidecar,
            host_group: None,
            entry: "python server.py".to_string(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            restricted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema,
            contributes: None,
            enabled: None,
            activation: None,
            persistent_fields: vec![],
            export_fields: vec![],
            aux_venvs: Vec::new(),
            provides: None,
        }
    }

    #[test]
    fn frame_fields_collected_from_widget_declarations() {
        // widget 声明 frame_field → 进名单；多插件并集去重排序
        let manifests = vec![
            manifest_with_ui_schema(
                "pipeline_llm_core",
                Some(serde_json::json!({"widgets": [
                    {"id": "model_id", "type": "model_select", "frame_field": "model_id"},
                    {"id": "thinking_strength", "type": "form"},
                ]})),
            ),
            manifest_with_ui_schema(
                "other_plugin",
                Some(serde_json::json!({"widgets": [
                    {"id": "w", "type": "x", "frame_field": "another_field"},
                    {"id": "no_field", "type": "y"},
                ]})),
            ),
        ];
        let enabled: std::collections::HashSet<String> =
            ["pipeline_llm_core".to_string(), "other_plugin".to_string()].into();
        assert_eq!(
            frame_overlay_fields_from_manifests(&manifests, &enabled),
            vec!["another_field".to_string(), "model_id".to_string()],
            "frame_field 声明进名单（去重排序），未声明键的 widget 不贡献"
        );
    }

    #[test]
    fn frame_fields_skip_disabled_and_malformed() {
        // disabled 插件不参与；ui_schema 缺失 / widgets 非数组 / frame_field 非字符串
        // 均安全跳过；空串不进名单
        let manifests = vec![
            manifest_with_ui_schema(
                "disabled_plugin",
                Some(serde_json::json!({"widgets": [
                    {"id": "w", "frame_field": "hidden_field"}
                ]})),
            ),
            manifest_with_ui_schema("no_ui", None),
            manifest_with_ui_schema(
                "malformed",
                Some(serde_json::json!({"widgets": [
                    {"id": "w1", "frame_field": 42},
                    {"id": "w2", "frame_field": ""},
                    "not-an-object"
                ]})),
            ),
        ];
        let enabled: std::collections::HashSet<String> = ["malformed".to_string()].into();
        assert!(
            frame_overlay_fields_from_manifests(&manifests, &enabled).is_empty(),
            "disabled/缺失/畸形声明一律不进名单"
        );
    }
}
