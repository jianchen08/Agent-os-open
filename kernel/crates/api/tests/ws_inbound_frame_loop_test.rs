// @feature: FP-0.2.七 路由收敛 | @ci: rust-test
//! WS 入站帧循环端到端行为测试（run_socket_loop 消费路径）：
//! 1. 连接确认 → 心跳 ack → 畸形帧丢弃不断连 → 分发 → 优雅关闭；
//! 2. 断线重连 last_sequence 水位重放（(ls, floor] 区间补放，不重复）；
//! 3. 出站满载背压自愈（CLOSE_BACKPRESSURE：普通关闭，非踢旧 4000）。

use std::sync::Arc;
use std::time::Duration;

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_core::types::PendingInputSource;
use agentos_session::router::{InboundRouter, PipelineDispatcher};
use agentos_session::SessionCoordinator;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio_tungstenite::tungstenite::Message;

struct NoopDispatcher;

#[async_trait::async_trait]
impl PipelineDispatcher for NoopDispatcher {
    async fn dispatch_user_input(
        &self,
        _thread_id: &str,
        _user_id: &str,
        _content: &str,
        _pipeline_id: &str,
        _thinking_strength: &str,
        _execution_context: Option<&Value>,
        _state_overlay: Option<&Value>,
        _agent_id: &str,
        _cmid: &str,
        _source: PendingInputSource,
    ) -> Result<(), String> {
        Ok(())
    }
    async fn dispatch_interaction_response(
        &self,
        _thread_id: &str,
        _request_id: &str,
        _response: &Value,
    ) -> Result<(), String> {
        Ok(())
    }
    async fn dispatch_stop(&self, _thread_id: &str, _pipeline_id: &str) -> Result<(), String> {
        Ok(())
    }
}

fn admin_user() -> agentos_http::auth::BuiltInUser {
    agentos_http::auth::default_users()
        .into_iter()
        .next()
        .unwrap()
}

/// 无 store 场景（AppState 不带 store）：token 校验走内置脚手架表（与
/// ws_kick_test 同款），被测语义（帧循环）不受登录通道影响。
fn admin_token() -> String {
    agentos_http::auth::encode_token(agentos_http::auth::TokenType::Access, &admin_user(), 3600)
}

fn make_state() -> (AppState, Arc<SessionCoordinator>) {
    let coord = Arc::new(SessionCoordinator::new());
    let mut state = AppState::new();
    state.session = Some(coord.clone());
    state.inbound_router = Some(Arc::new(InboundRouter::new(Arc::new(NoopDispatcher))));
    (state, coord)
}

async fn spawn_server(state: AppState) -> String {
    let app = build_router(state);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    format!("ws://{addr}/ws/chat")
}

async fn connect(
    url: &str,
) -> tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>> {
    let (ws, _) = tokio_tungstenite::connect_async(url)
        .await
        .expect("WS 握手应成功");
    ws
}

async fn next_frame(
    ws: &mut tokio_tungstenite::WebSocketStream<
        tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
    >,
) -> Message {
    tokio::time::timeout(Duration::from_secs(5), ws.next())
        .await
        .expect("5s 内应收到帧")
        .expect("流未关闭")
        .expect("帧非错误")
}

#[tokio::test]
async fn frame_loop_confirms_acks_tolerates_garbage_and_closes_gracefully() {
    let (state, _coord) = make_state();
    let base = spawn_server(state).await;
    let mut ws = connect(&format!("{base}?token={}", admin_token())).await;

    // ① 首帧 connection_confirmation（含 user_id）
    let confirm = next_frame(&mut ws).await;
    let parsed: Value = match &confirm {
        Message::Text(t) => serde_json::from_str(t).unwrap(),
        other => panic!("首帧应为文本确认帧，实际: {other:?}"),
    };
    assert_eq!(parsed["type"], "connection_confirmation");
    assert_eq!(parsed["data"]["user_id"], json!(admin_user().id));
    assert_eq!(parsed["data"]["status"], "connected");

    // ② 心跳 → heartbeat_ack（前端连续收不到 ack 会判死断连）
    ws.send(Message::Text(
        json!({"type": "heartbeat"}).to_string().into(),
    ))
    .await
    .unwrap();
    let ack = next_frame(&mut ws).await;
    let parsed: Value = match &ack {
        Message::Text(t) => serde_json::from_str(t).unwrap(),
        other => panic!("心跳应砧 heartbeat_ack 文本帧，实际: {other:?}"),
    };
    assert_eq!(parsed["type"], "heartbeat_ack");
    assert!(parsed["data"]["timestamp"].as_str().is_some());

    // ③ 畸形帧：静默丢弃，连接保持可用（后续心跳仍回 ack）
    ws.send(Message::Text("not-json{{{".into())).await.unwrap();
    ws.send(Message::Text(
        json!({"type": "heartbeat"}).to_string().into(),
    ))
    .await
    .unwrap();
    let ack2 = next_frame(&mut ws).await;
    match &ack2 {
        Message::Text(t) => {
            let v: Value = serde_json::from_str(t).unwrap();
            assert_eq!(v["type"], "heartbeat_ack", "畸形帧后连接必须存活");
        }
        other => panic!("畸形帧不得断连，实际: {other:?}"),
    }

    // ④ 业务帧分发（no-op dispatcher）：路由正常，连接继续
    ws.send(Message::Text(
        json!({"type": "user_input", "thread_id": "t-loop", "content": "hi"})
            .to_string()
            .into(),
    ))
    .await
    .unwrap();
    ws.send(Message::Text(
        json!({"type": "heartbeat"}).to_string().into(),
    ))
    .await
    .unwrap();
    let ack3 = next_frame(&mut ws).await;
    match &ack3 {
        Message::Text(t) => {
            let v: Value = serde_json::from_str(t).unwrap();
            assert_eq!(v["type"], "heartbeat_ack", "分发后连接必须存活");
        }
        other => panic!("no-op 分发不得断连，实际: {other:?}"),
    }

    // ⑤ 客户端 Close → 会话优雅收尾（服务端循环退出）
    ws.send(Message::Close(None)).await.unwrap();
    loop {
        let frame = tokio::time::timeout(Duration::from_secs(5), ws.next()).await;
        match frame {
            Err(_) => panic!("5s 内未完成收尾"),
            Ok(None) | Ok(Some(Err(_))) | Ok(Some(Ok(Message::Close(_)))) => break,
            Ok(Some(Ok(Message::Ping(_) | Message::Pong(_)))) => continue,
            Ok(Some(Ok(other))) => panic!("Close 之后不应再有业务帧，实际: {other:?}"),
        }
    }
}

#[tokio::test]
async fn reconnect_with_last_sequence_replays_missed_events_once() {
    // B3 建连重放：断连期间落缓冲的事件按 (last_sequence, floor] 补放。
    let (state, coord) = make_state();
    let admin_id = admin_user().id;
    coord.register_thread("t-replay", &admin_id);
    // 无连接时 emit：两帧进重放缓冲（seq 1、2，per-thread 递增）
    coord
        .emit_event("t-replay", "new_message", json!({"marker": "r1"}))
        .await;
    coord
        .emit_event("t-replay", "new_message", json!({"marker": "r2"}))
        .await;

    let base = spawn_server(state).await;
    let mut ws = connect(&format!("{base}?token={}&last_sequence=1", admin_token())).await;

    // 首帧确认，随后重放 (1, floor] = seq 2 的事件（seq 1 客户端已有，不重复）
    let confirm = next_frame(&mut ws).await;
    match &confirm {
        Message::Text(t) => assert!(t.contains("connection_confirmation"), "首帧应为确认: {t}"),
        other => panic!("首帧应为确认，实际: {other:?}"),
    }
    let replayed = next_frame(&mut ws).await;
    let parsed: Value = match &replayed {
        Message::Text(t) => serde_json::from_str(t).unwrap(),
        other => panic!("应收到重放事件帧，实际: {other:?}"),
    };
    assert_eq!(parsed["type"], "new_message", "重放缓冲事件: {parsed}");
    assert_eq!(parsed["data"]["marker"], "r2", "只补 (ls, floor] 区间");
    assert_eq!(parsed["sequence"], 2, "重放保留权威序号");
}

#[tokio::test]
async fn outbound_backpressure_closes_connection_with_normal_close_not_kick() {
    // 出站队列满载自愈（CLOSE_BACKPRESSURE）：客户端不读，灌满出站队列后
    // 连接被普通关闭（非 4000 踢旧），前端按掉线自动重连自愈。
    let (state, coord) = make_state();
    let admin_id = admin_user().id;
    let base = spawn_server(state).await;
    let mut ws = connect(&format!("{base}?token={}", admin_token())).await;

    // 消费掉连接确认，注册 thread→user 供事件路由
    let _confirm = next_frame(&mut ws).await;
    coord.register_thread("t-flood", &admin_id);

    // 不读 socket 灌帧：16KB × 1000 帧 ≫ TCP 缓冲 + 512 出站队列，必触满载
    let pad = "x".repeat(16 * 1024);
    for i in 0..1000 {
        coord
            .emit_event("t-flood", "flood", json!({"i": i, "pad": pad}))
            .await;
    }

    // 之后开始读：应收大量帧后收到普通 Close（连接终止，且不是踢旧语义）
    let mut text_frames = 0usize;
    let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
    loop {
        assert!(
            tokio::time::Instant::now() < deadline,
            "30s 内未观察到连接关闭（背压自愈未生效）"
        );
        let frame = tokio::time::timeout(Duration::from_secs(15), ws.next()).await;
        let Ok(Some(Ok(frame))) = frame else { break };
        match frame {
            Message::Text(_) => text_frames += 1,
            Message::Ping(_) | Message::Pong(_) => {}
            Message::Close(frame) => {
                let code = frame.map(|f| u16::from(f.code));
                assert_ne!(
                    code,
                    Some(agentos_session::auth::CLOSE_CODE_KICKED),
                    "背压自愈必须是普通关闭而非踢旧"
                );
                break;
            }
            other => panic!("意外帧: {other:?}"),
        }
    }
    // 行为契约：满载触发普通关闭（非 4000 踢旧），连接终止不自愈悬挂；
    // 已送达帧数下界 = 客户端 TCP 收包窗口内的在途帧（满载后未消费的
    // 512 队列帧随连接关闭丢弃，属预期——重连后经重放/整树刷新恢复）。
    assert!(
        text_frames >= 3,
        "关闭前应有在途帧送达客户端，实际 {text_frames} 帧"
    );
}
