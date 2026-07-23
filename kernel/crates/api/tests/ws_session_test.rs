//! ws_session 接入层测试——握手鉴权 + EventSink 适配（P2 接线）。

use agentos_session::auth::{authenticate_handshake, HandshakeAuth};
use agentos_session::{EventSink, SessionCoordinator};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

/// 测试用 sink。
struct CountingSink {
    id: u64,
    count: Arc<AtomicU64>,
    received: Arc<Mutex<Vec<String>>>,
}
static NEXT: AtomicU64 = AtomicU64::new(1);
#[async_trait::async_trait]
impl EventSink for CountingSink {
    async fn send_text(&self, _text: &str) -> bool {
        self.count.fetch_add(1, Ordering::SeqCst);
        if let Ok(mut r) = self.received.lock() {
            r.push(_text.to_string());
        }
        true
    }
    fn id(&self) -> u64 {
        self.id
    }
}
fn make_sink() -> (Arc<CountingSink>, Arc<AtomicU64>, Arc<Mutex<Vec<String>>>) {
    let count = Arc::new(AtomicU64::new(0));
    let received = Arc::new(Mutex::new(Vec::new()));
    let sink = Arc::new(CountingSink {
        id: NEXT.fetch_add(1, Ordering::SeqCst),
        count: count.clone(),
        received: received.clone(),
    });
    (sink, count, received)
}

#[tokio::test]
async fn session_coordinator_registers_and_routes() {
    // 验证 SessionCoordinator（P2 session crate 出口）在 api 侧可用
    let coord = SessionCoordinator::new();
    let (sink, count, _received) = make_sink();
    coord.register("user-A", sink);
    coord.register_thread("thread-1", "user-A");

    let delivered = coord
        .emit_widget("thread-1", "w", "e", serde_json::json!({"x": 1}), "p")
        .await;
    assert_eq!(delivered, 1);
    assert_eq!(count.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn handshake_auth_rejects_missing_token_via_session_auth() {
    // session crate 的 authenticate_handshake（ws_session::authenticate 复用它）
    let result = authenticate_handshake("", &|_| Some(("u".into(), "n".into())));
    assert!(matches!(result, HandshakeAuth::Rejected { .. }));
}

#[tokio::test]
async fn handshake_auth_passes_with_verifier() {
    let result = authenticate_handshake("tok", &|_| Some(("u-1".into(), "alice".into())));
    assert_eq!(
        result,
        HandshakeAuth::Ok {
            user_id: "u-1".into(),
            username: "alice".into(),
        }
    );
}

#[tokio::test]
async fn reconnect_replay_after_emit() {
    // 验证 api 侧 SessionCoordinator 重连回放全链路
    let coord = SessionCoordinator::default();
    let (sink1, _, _) = make_sink();
    coord.register("user-A", sink1);
    coord.register_thread("thread-1", "user-A");

    for i in 1..=3 {
        coord
            .emit_widget("thread-1", "w", "e", serde_json::json!({"i": i}), "p")
            .await;
    }
    // 重连：新 sink，last_sequence=1 → 回放 seq 2,3
    let (sink2, _, recv2) = make_sink();
    let outcome = coord
        .handle_reconnect("thread-1", "user-A", sink2, 1)
        .await;
    assert!(outcome.replayed);
    assert!(!outcome.resync_required);
    let msgs = recv2.lock().unwrap();
    // 应含 connection_confirmation + seq2 widget + seq3 widget
    let widget_count = msgs
        .iter()
        .filter(|m| m.contains("\"widget_event\""))
        .count();
    assert_eq!(widget_count, 2, "应回放 seq 2,3 两个 widget 事件");
}
