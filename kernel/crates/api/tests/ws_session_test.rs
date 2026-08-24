// @feature: FP-0.2.八 多租户 | @vision: V4 多用户 | @ci: rust-test
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
        .emit_event("thread-1", "widget_event", serde_json::json!({"x": 1}))
        .await;
    assert!(delivered);
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
