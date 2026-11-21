use super::*;

use agentos_session::router::InboundRouter;

use super::support::{
    mint_access_token, next_close_code, next_text_frame, spawn_ws_server, ws_connect, ws_e2e_state,
};
use futures_util::{SinkExt as _, StreamExt as _};
use tokio_tungstenite::tungstenite::Message as WsMessage;

// ═══════════════════════════════════════════════════════════════════
// 覆盖率补测（2026-09-13 冲刺第二批）：握手鉴权矩阵 + WS 会话生命周期
// e2e（真实 axum 服务 + tungstenite 客户端）+ 派发链直跑/忙队路径。
// mock 仅用于内核边界（dispatcher/存储）；会话收发循环走真实代码路径。
// ═══════════════════════════════════════════════════════════════════

#[test]
fn authenticate_accepts_valid_access_token() {
    match super::super::authenticate(Some(&mint_access_token("u-auth-1", "alice"))) {
        agentos_session::auth::HandshakeAuth::Ok { user_id, username } => {
            assert_eq!(user_id, "u-auth-1");
            assert_eq!(username, "alice");
        }
        agentos_session::auth::HandshakeAuth::Rejected { code, reason } => {
            panic!("有效 access token 应放行，实际拒绝 code={code} reason={reason}")
        }
    }
}

#[test]
fn authenticate_rejects_missing_garbage_and_refresh_token() {
    // 缺 token 与垃圾 token：4001 拒绝（区分码见 session::auth 常量）
    match super::super::authenticate(None) {
        agentos_session::auth::HandshakeAuth::Rejected { code, .. } => {
            assert_eq!(code, agentos_session::auth::REJECT_CODE_NO_TOKEN)
        }
        agentos_session::auth::HandshakeAuth::Ok { .. } => panic!("缺失 token 必须拒绝"),
    }
    match super::super::authenticate(Some("garbage-token")) {
        agentos_session::auth::HandshakeAuth::Rejected { code, .. } => {
            assert_eq!(code, agentos_session::auth::REJECT_CODE_INVALID_TOKEN)
        }
        agentos_session::auth::HandshakeAuth::Ok { .. } => panic!("垃圾 token 必须拒绝"),
    }
    // refresh token 不得用于 WS 握手（复用通道降权）
    let user = agentos_http::auth::BuiltInUser {
        id: "u-auth-2".to_string(),
        username: "bob".to_string(),
        password: String::new(),
        email: String::new(),
        role: "user".to_string(),
        tenant_id: "default".to_string(),
        created_at: "2026-09-13T00:00:00Z".to_string(),
        must_change_password: false,
    };
    let refresh =
        agentos_http::auth::encode_token(agentos_http::auth::TokenType::Refresh, &user, 3600);
    assert!(matches!(
        super::super::authenticate(Some(&refresh)),
        agentos_session::auth::HandshakeAuth::Rejected { .. }
    ));
}

// ── e2e：真实 axum 服务 + tungstenite 客户端（run_ws_session 全链路）──

type ClientWs =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

#[tokio::test]
async fn ws_confirmation_heartbeat_survives_malformed_and_unregisters_on_close() {
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let addr = spawn_ws_server(ws_e2e_state(coord.clone())).await;
    let token = mint_access_token("u-e2e-1", "E2E");
    let mut ws = ws_connect(format!("ws://{addr}/ws/chat?token={token}")).await;

    // 连接确认信封（首帧）
    let conf = next_text_frame(&mut ws, "connection_confirmation").await;
    assert_eq!(conf["type"], "connection_confirmation");
    assert_eq!(conf["data"]["status"], "connected");
    assert_eq!(conf["data"]["mode"], "global");
    assert_eq!(conf["data"]["user_id"], "u-e2e-1");

    // 心跳 → ack
    ws.send(WsMessage::text(json!({"type": "heartbeat"}).to_string()))
        .await
        .unwrap();
    let ack = next_text_frame(&mut ws, "heartbeat_ack").await;
    assert_eq!(ack["type"], "heartbeat_ack");
    assert!(ack["data"]["timestamp"].as_str().is_some());

    // 畸形帧：静默丢弃，连接存活（后续心跳仍有 ack）
    ws.send(WsMessage::text("{{not json".to_string()))
        .await
        .unwrap();
    ws.send(WsMessage::text(json!({"type": "heartbeat"}).to_string()))
        .await
        .unwrap();
    let ack2 = next_text_frame(&mut ws, "post-malformed heartbeat_ack").await;
    assert_eq!(ack2["type"], "heartbeat_ack");

    // 客户端关闭 → 服务端收尾注销（registry 无残留）
    let _ = ws.send(WsMessage::Close(None)).await;
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    while tokio::time::Instant::now() < deadline {
        if coord.registry().sinks_of_user("u-e2e-1").is_empty() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert!(
        coord.registry().sinks_of_user("u-e2e-1").is_empty(),
        "连接关闭后必须从注册表注销"
    );
}

#[tokio::test]
async fn ws_second_connection_kicks_first_with_two_stage_close() {
    // 配额 1：同用户第二连接触发超限 LRU 踢旧（ADR 2026-10-01：配额内多端
    // 并存不踢，超限才踢最旧——两段式协议转作超限踢旧通道）
    let coord = Arc::new(agentos_session::SessionCoordinator::with_max_conns(1));
    let addr = spawn_ws_server(ws_e2e_state(coord.clone())).await;
    let token = mint_access_token("u-kick-1", "Kicked");
    let mut first = ws_connect(format!("ws://{addr}/ws/chat?token={token}")).await;
    let conf = next_text_frame(&mut first, "first confirmation").await;
    assert_eq!(conf["type"], "connection_confirmation");

    // 同用户第二连接：超限踢最旧
    let mut second = ws_connect(format!("ws://{addr}/ws/chat?token={token}")).await;
    let conf2 = next_text_frame(&mut second, "second confirmation").await;
    assert_eq!(conf2["type"], "connection_confirmation");

    // 被逐出的旧连接两段式收尾：先应用层 kicked 文本帧，再 Close(4000)
    let kicked = next_text_frame(&mut first, "kicked text").await;
    assert_eq!(kicked["type"], "kicked");
    assert_eq!(kicked["data"]["reason"], "replaced_by_new_connection");
    let code = next_close_code(&mut first, "kicked close").await;
    assert_eq!(
        code,
        agentos_session::auth::CLOSE_CODE_KICKED,
        "超限踢旧 Close 必须带 4000（前端据此跳过重连）"
    );
    let _ = second.send(WsMessage::Close(None)).await;
}

#[tokio::test]
async fn ws_two_connections_same_user_both_receive_frames() {
    // 多端并存主路径（ADR 2026-10-01）：配额内同用户两条连接并存，事件按
    // 连接扇出——两端各收一份（心跳 ack 为最轻量可观察扇出帧）。
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let addr = spawn_ws_server(ws_e2e_state(coord.clone())).await;
    let token = mint_access_token("u-multi-1", "MultiFrontend");

    let mut ws1 = ws_connect(format!("ws://{addr}/ws/chat?token={token}")).await;
    let conf1 = next_text_frame(&mut ws1, "ws1 confirmation").await;
    assert_eq!(conf1["type"], "connection_confirmation");

    let mut ws2 = ws_connect(format!("ws://{addr}/ws/chat?token={token}")).await;
    let conf2 = next_text_frame(&mut ws2, "ws2 confirmation").await;
    assert_eq!(conf2["type"], "connection_confirmation");

    assert_eq!(
        coord.registry().sinks_of_user("u-multi-1").len(),
        2,
        "同用户两条连接必须并存（不互踢）"
    );

    // 一端心跳 → ack 扇出到两端（各收一份）
    ws1.send(WsMessage::text(json!({"type": "heartbeat"}).to_string()))
        .await
        .unwrap();
    let ack1 = next_text_frame(&mut ws1, "ws1 heartbeat_ack").await;
    assert_eq!(ack1["type"], "heartbeat_ack");
    let ack2 = next_text_frame(&mut ws2, "ws2 heartbeat_ack").await;
    assert_eq!(ack2["type"], "heartbeat_ack");

    // 一端断开：他端不受影响（注册表仍持有一条连接，心跳继续有 ack）
    let _ = ws1.send(WsMessage::Close(None)).await;
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    while tokio::time::Instant::now() < deadline {
        if coord.registry().sinks_of_user("u-multi-1").len() == 1 {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert_eq!(
        coord.registry().sinks_of_user("u-multi-1").len(),
        1,
        "一端断开后另一端应保留"
    );
    ws2.send(WsMessage::text(json!({"type": "heartbeat"}).to_string()))
        .await
        .unwrap();
    let ack3 = next_text_frame(&mut ws2, "ws2 post-peer-exit heartbeat_ack").await;
    assert_eq!(ack3["type"], "heartbeat_ack", "存活连接不受他端断开影响");
    let _ = ws2.send(WsMessage::Close(None)).await;
}

/// 流式回声分发器：受理 user_input 后异步经 [`SessionCoordinator::emit_event`]
/// 发射 stream_start → stream_chunk → stream_end——与生产 SessionRoundEvents
/// 同一出口（thread 单播唯一路径 = registry.send_to_thread 反查 + send_to_user
/// 按连接扇出）。首帧延迟可调，为"晚注册连接收到后续帧"留建连窗口。
struct StreamEchoDispatcher {
    session: Arc<agentos_session::SessionCoordinator>,
    first_emit_delay_ms: u64,
    step_gap_ms: u64,
}

#[async_trait]
impl PipelineDispatcher for StreamEchoDispatcher {
    async fn dispatch_user_input(
        &self,
        request: agentos_core::types::DispatchRequest,
    ) -> Result<(), String> {
        let session = self.session.clone();
        let thread = request.thread_id.clone();
        let first = self.first_emit_delay_ms;
        let gap = self.step_gap_ms;
        tokio::spawn(async move {
            let step = |ms: u64| tokio::time::sleep(std::time::Duration::from_millis(ms));
            let envelope = |extra: &str, value: &str| {
                json!({
                    "pipeline_id": format!("pipe-{thread}"),
                    "message_id": format!("a_echo_{thread}"),
                    "_threadId": thread,
                    extra: value,
                })
            };
            step(first).await;
            let _ = session
                .emit_event(&thread, "stream_start", envelope("_threadId", &thread))
                .await;
            step(gap).await;
            let _ = session
                .emit_event(&thread, "stream_chunk", envelope("text", "回声正文"))
                .await;
            step(gap).await;
            let _ = session
                .emit_event(&thread, "stream_end", envelope("_threadId", &thread))
                .await;
        });
        Ok(())
    }
    async fn dispatch_interaction_response(
        &self,
        _thread_id: &str,
        _request_id: &str,
        _response: &serde_json::Value,
    ) -> Result<(), String> {
        Ok(())
    }
    async fn dispatch_stop(&self, _thread_id: &str, _pipeline_id: &str) -> Result<(), String> {
        Ok(())
    }
}

/// 从一条真 socket 连接持续收集文本帧，直到谓词命中（该帧计入）或 5s 总时限
/// （panic 并报已收帧数，避免静默通过）。
async fn collect_frames_until(
    ws: &mut ClientWs,
    label: &str,
    pred: impl Fn(&serde_json::Value) -> bool,
) -> Vec<serde_json::Value> {
    let mut frames = Vec::new();
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    while tokio::time::Instant::now() < deadline {
        match tokio::time::timeout(std::time::Duration::from_millis(500), ws.next()).await {
            Ok(Some(Ok(WsMessage::Text(t)))) => {
                let v: serde_json::Value =
                    serde_json::from_str(t.as_str()).expect("文本帧应为 JSON");
                let hit = pred(&v);
                frames.push(v);
                if hit {
                    return frames;
                }
            }
            Ok(Some(Ok(_))) => continue,
            Ok(Some(Err(e))) => panic!("{label}: 流错误 {e}"),
            Ok(None) => panic!("{label}: 流已关闭"),
            Err(_) => {}
        }
    }
    panic!("{label}: 5s 内未等到目标帧（已收 {} 帧）", frames.len());
}

fn frame_type_names(frames: &[serde_json::Value]) -> Vec<&str> {
    frames
        .iter()
        .filter_map(|f| f.get("type").and_then(|t| t.as_str()))
        .collect()
}

#[tokio::test]
async fn ws_user_input_stream_frames_fan_out_to_both_connections() {
    // 流式扇出回归用例（多前端连接 ADR 验收口径①）：同 user 两真连接，A 发
    // user_input 后 A、B 都必须收到 stream 类帧。thread→user 映射经真实入站
    // 帧注册（handle_inbound），发射走生产同路 emit_event → send_to_thread
    // 反查 → send_to_user 按连接集合扇出。
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let mut state = crate::routes::AppState::new();
    state.session = Some(coord.clone());
    state.inbound_router = Some(Arc::new(InboundRouter::new(Arc::new(
        StreamEchoDispatcher {
            session: coord.clone(),
            first_emit_delay_ms: 150,
            step_gap_ms: 100,
        },
    ))));
    let addr = spawn_ws_server(state).await;
    let token = mint_access_token("u-fan-1", "FanoutUser");

    let mut ws_a = ws_connect(format!("ws://{addr}/ws/chat?token={token}")).await;
    assert_eq!(
        next_text_frame(&mut ws_a, "A confirmation").await["type"],
        "connection_confirmation"
    );
    let mut ws_b = ws_connect(format!("ws://{addr}/ws/chat?token={token}")).await;
    assert_eq!(
        next_text_frame(&mut ws_b, "B confirmation").await["type"],
        "connection_confirmation"
    );

    ws_a.send(WsMessage::text(
        json!({
            "type": "active_thread_changed",
            "thread_id": "T-fan-1",
            "pipeline_id": "pipe-fan-1",
        })
        .to_string(),
    ))
    .await
    .unwrap();
    ws_a.send(WsMessage::text(
        json!({
            "type": "user_input",
            "thread_id": "T-fan-1",
            "content": "hi",
            "client_message_id": "cm-fan-1",
        })
        .to_string(),
    ))
    .await
    .unwrap();

    let is_end =
        |v: &serde_json::Value| v.get("type").and_then(|t| t.as_str()) == Some("stream_end");
    let frames_a = collect_frames_until(&mut ws_a, "A", is_end).await;
    let frames_b = collect_frames_until(&mut ws_b, "B", is_end).await;

    for (label, frames) in [("A", frames_a), ("B", frames_b)] {
        let types = frame_type_names(&frames);
        assert!(
            types.contains(&"user_input_ack"),
            "{label} 必须收到受理回执（实收 {types:?}）"
        );
        assert!(
            types.contains(&"stream_chunk"),
            "{label} 必须收到 stream_chunk（实收 {types:?}）"
        );
        assert!(
            types.contains(&"stream_end"),
            "{label} 必须收到 stream_end（实收 {types:?}）"
        );
        let chunk = frames
            .iter()
            .find(|f| f["type"] == "stream_chunk")
            .expect("stream_chunk 已断言在场");
        assert_eq!(
            chunk["data"]["text"], "回声正文",
            "{label} 流文本在 data 契约位（前端 eventPayload 同源）"
        );
    }
    let _ = ws_a.send(WsMessage::Close(None)).await;
    let _ = ws_b.send(WsMessage::Close(None)).await;
}

#[tokio::test]
async fn ws_late_joining_connection_receives_subsequent_stream_frames() {
    // 流式扇出回归用例（多前端连接 ADR 验收口径②）：B 晚于 user_input 注册
    // （派发受理后、流中段建连，不发任何帧）也必须收到注册之后的 stream 帧。
    // 扇出按 user 连接集合路由，不要求 B 先注册 thread 映射。
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    let mut state = crate::routes::AppState::new();
    state.session = Some(coord.clone());
    state.inbound_router = Some(Arc::new(InboundRouter::new(Arc::new(
        StreamEchoDispatcher {
            session: coord.clone(),
            first_emit_delay_ms: 400,
            step_gap_ms: 300,
        },
    ))));
    let addr = spawn_ws_server(state).await;
    let token = mint_access_token("u-fan-2", "LateJoinUser");

    let mut ws_a = ws_connect(format!("ws://{addr}/ws/chat?token={token}")).await;
    assert_eq!(
        next_text_frame(&mut ws_a, "A confirmation").await["type"],
        "connection_confirmation"
    );
    ws_a.send(WsMessage::text(
        json!({
            "type": "user_input",
            "thread_id": "T-fan-2",
            "content": "hi",
            "client_message_id": "cm-fan-2",
        })
        .to_string(),
    ))
    .await
    .unwrap();
    let ack = next_text_frame(&mut ws_a, "A ack").await;
    assert_eq!(
        ack["type"], "user_input_ack",
        "受理回执先于流式帧（派发已发生）"
    );

    // 派发受理后建连：晚于 user_input，早于首帧发射（400ms 窗口）
    let mut ws_b = ws_connect(format!("ws://{addr}/ws/chat?token={token}")).await;
    assert_eq!(
        next_text_frame(&mut ws_b, "B confirmation").await["type"],
        "connection_confirmation"
    );

    let is_end =
        |v: &serde_json::Value| v.get("type").and_then(|t| t.as_str()) == Some("stream_end");
    let frames_a = collect_frames_until(&mut ws_a, "A", is_end).await;
    let frames_b = collect_frames_until(&mut ws_b, "B", is_end).await;

    let types_a = frame_type_names(&frames_a);
    for expected in ["stream_start", "stream_chunk", "stream_end"] {
        assert!(
            types_a.contains(&expected),
            "全程在场的 A 必须收全流式帧，缺 {expected}（实收 {types_a:?}）"
        );
    }
    let types_b = frame_type_names(&frames_b);
    for expected in ["stream_chunk", "stream_end"] {
        assert!(
            types_b.contains(&expected),
            "晚注册的 B 必须收到注册后的后续帧，缺 {expected}（实收 {types_b:?}）"
        );
    }
    let chunk_b = frames_b
        .iter()
        .find(|f| f["type"] == "stream_chunk")
        .expect("B 的 stream_chunk 已断言在场");
    assert_eq!(
        chunk_b["data"]["text"], "回声正文",
        "B 收到的流文本契约位同源"
    );
    let _ = ws_a.send(WsMessage::Close(None)).await;
    let _ = ws_b.send(WsMessage::Close(None)).await;
}

#[tokio::test]
async fn ws_ticket_path_establishes_session_and_replayed_ticket_rejected() {
    let state = ws_e2e_state(Arc::new(agentos_session::SessionCoordinator::new()));
    let ticket = state.ws_tickets.issue("u-ticket-1", "Ticker");
    let addr = spawn_ws_server(state).await;

    // 有效票据：会话建立（票据消费返回用户身份）
    let mut ws = ws_connect(format!("ws://{addr}/ws/chat?ticket={ticket}")).await;
    let conf = next_text_frame(&mut ws, "ticket confirmation").await;
    assert_eq!(conf["type"], "connection_confirmation");
    assert_eq!(conf["data"]["user_id"], "u-ticket-1");
    let _ = ws.send(WsMessage::Close(None)).await;

    // 同票据重放：一次性语义 → 4001 拒绝
    let mut replay = ws_connect(format!("ws://{addr}/ws/chat?ticket={ticket}")).await;
    let code = next_close_code(&mut replay, "replayed ticket").await;
    assert_eq!(code, 4001, "已消费票据重放必须拒绝");
}

#[tokio::test]
async fn ws_reconnect_watermark_replays_buffered_events() {
    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    // 断线期间：事件照常落重放缓冲（无连接可投递）
    coord.register_thread("thread-rp", "u-rp-1");
    coord
        .emit_event("thread-rp", "new_message", json!({"marker": "rp-1"}))
        .await;
    coord
        .emit_event("thread-rp", "new_message", json!({"marker": "rp-2"}))
        .await;
    let addr = spawn_ws_server(ws_e2e_state(coord.clone())).await;

    // 带 watermark 重连：confirmation 先行，随后重放 (last_sequence, floor]
    let token = mint_access_token("u-rp-1", "Replayer");
    let mut ws = ws_connect(format!("ws://{addr}/ws/chat?token={token}&last_sequence=1")).await;
    let conf = next_text_frame(&mut ws, "reconnect confirmation").await;
    assert_eq!(conf["type"], "connection_confirmation");
    let replayed = next_text_frame(&mut ws, "replayed event").await;
    assert_eq!(replayed["type"], "new_message");
    assert_eq!(
        replayed["data"]["marker"], "rp-2",
        "watermark 之后的缓冲事件必须经建连重放送达"
    );
    let _ = ws.send(WsMessage::Close(None)).await;
}

// ── D3：握手后验用户存在性（P1-11）────────────────────────────────
//
// token 校验是同步纯 token 面（签名/时效/access 类型），已删用户的未过期
// token 仍能通过；票据签发与消费之间用户同样可被删。存在性校验收口在
// run_ws_session_with_auth（两凭证路径汇合点），store 侧用户查询三态
// （在库/已删/存储故障）经 tests::ResolveMock 注入（StorageError 经
// tests 域 glob 导入）。

/// store 侧用户查询注入的 e2e 态：ResolveMock 的 get_user_by_id 三态直通。
fn store_backed_state(
    user_lookup: Result<Option<agentos_core::types::UserRecord>, StorageError>,
) -> crate::routes::AppState {
    let mut state = ws_e2e_state(Arc::new(agentos_session::SessionCoordinator::new()));
    let mock = super::ResolveMock::new(Ok(vec![]), None);
    *mock.user_lookup.lock().unwrap() = Some(user_lookup);
    state.store = Some(Arc::new(mock));
    state
}

#[tokio::test]
async fn ws_deleted_user_token_rejected_with_4001() {
    // 已删用户持未过期 token：token 校验放行、存在性校验 4001 拒绝
    // （修复前本用例红：连接建立收 confirmation，永远等不到 Close）。
    let addr = spawn_ws_server(store_backed_state(Ok(None))).await;
    let token = mint_access_token("u-deleted-1", "Ghost");
    let mut ws = ws_connect(format!("ws://{addr}/ws/chat?token={token}")).await;
    let code = next_close_code(&mut ws, "deleted user close").await;
    assert_eq!(code, 4001, "已删用户必须握手拒绝（4001），不得占连接");
}

#[tokio::test]
async fn ws_ticket_of_deleted_user_rejected() {
    // 票据路径同收口：签发后用户被删，消费票据仍须存在性拒绝。
    let state = store_backed_state(Ok(None));
    let ticket = state.ws_tickets.issue("u-deleted-2", "Ghost2");
    let addr = spawn_ws_server(state).await;
    let mut ws = ws_connect(format!("ws://{addr}/ws/chat?ticket={ticket}")).await;
    let code = next_close_code(&mut ws, "deleted user ticket close").await;
    assert_eq!(code, 4001, "票据消费路径同样验存在性");
}

#[tokio::test]
async fn ws_existing_user_token_establishes_session() {
    // 正向对照（防过度拒绝）：在库用户照常建连收 confirmation。
    let state = store_backed_state(Ok(Some(super::synthetic_default_user("u-live-1"))));
    let addr = spawn_ws_server(state).await;
    let token = mint_access_token("u-live-1", "LiveUser");
    let mut ws = ws_connect(format!("ws://{addr}/ws/chat?token={token}")).await;
    let conf = next_text_frame(&mut ws, "live user confirmation").await;
    assert_eq!(conf["type"], "connection_confirmation");
    assert_eq!(conf["data"]["user_id"], "u-live-1");
    let _ = ws.send(WsMessage::Close(None)).await;
}

#[tokio::test]
async fn ws_user_lookup_store_failure_fails_closed() {
    // 存储故障 fail-closed（对齐分发期 resolve_tenant_id_by_user 同一 tri-state
    // 的拒绝语义）：查不了 = 不放行，4001 走前端 refreshToken→重连自愈链。
    let state = store_backed_state(Err(StorageError::Database("injected".to_string())));
    let addr = spawn_ws_server(state).await;
    let token = mint_access_token("u-any-1", "Anyone");
    let mut ws = ws_connect(format!("ws://{addr}/ws/chat?token={token}")).await;
    let code = next_close_code(&mut ws, "store failure close").await;
    assert_eq!(code, 4001, "用户查询存储故障必须 fail-closed 拒绝");
}
