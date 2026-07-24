//! SessionCoordinator 测试——断线重连全链路（ADR §7.2）。
//!
//! emit 记录到重放缓冲 → 断线 → 重连上报 last_sequence → 回放续传 / resync。

use agentos_session::{EventSink, SessionCoordinator};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

struct MockSink {
    id: u64,
    received: Arc<Mutex<Vec<serde_json::Value>>>,
    /// 是否模拟"断线"（send 返回 false）。
    online: Arc<std::sync::atomic::AtomicBool>,
}
static NEXT_ID: AtomicU64 = AtomicU64::new(1);
impl MockSink {
    fn online() -> (Arc<Self>, Arc<Mutex<Vec<serde_json::Value>>>) {
        Self::with_state(true)
    }
    fn with_state(online: bool) -> (Arc<Self>, Arc<Mutex<Vec<serde_json::Value>>>) {
        let received = Arc::new(Mutex::new(Vec::new()));
        let sink = Arc::new(MockSink {
            id: NEXT_ID.fetch_add(1, Ordering::SeqCst),
            received: received.clone(),
            online: Arc::new(std::sync::atomic::AtomicBool::new(online)),
        });
        (sink, received)
    }
}
#[async_trait::async_trait]
impl EventSink for MockSink {
    async fn send_text(&self, text: &str) -> bool {
        if !self.online.load(Ordering::SeqCst) {
            return false; // 模拟断线
        }
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(text) {
            self.received.lock().unwrap().push(v);
        }
        true
    }
    fn id(&self) -> u64 {
        self.id
    }
}

#[tokio::test]
async fn emit_records_to_replay_buffer_for_thread_scope() {
    let coord = SessionCoordinator::default();
    let (sink, received) = MockSink::online();
    coord.register("user-A", sink);
    coord.register_thread("thread-1", "user-A");

    // emit 3 条 widget 事件到 thread
    for i in 1..=3 {
        coord
            .emit_widget("thread-1", "cost", "tick", serde_json::json!({"i": i}), "p")
            .await;
    }
    // 在线时应直接投递
    assert_eq!(received.lock().unwrap().len(), 3);
}

#[tokio::test]
async fn reconnect_replays_buffered_events() {
    let coord = SessionCoordinator::default();

    // 第一次连接：emit 事件，建立缓冲
    let (sink1, _recv1) = MockSink::online();
    coord.register("user-A", sink1.clone());
    coord.register_thread("thread-1", "user-A");
    for i in 1..=3 {
        coord
            .emit_widget("thread-1", "cost", "tick", serde_json::json!({"i": i}), "p")
            .await;
    }

    // 断线后重连：新 sink，上报 last_sequence=1（请求 (1,3] = seq 2,3）
    let (sink2, recv2) = MockSink::online();
    let outcome = coord
        .handle_reconnect("thread-1", "user-A", sink2.clone(), 1)
        .await;

    assert!(outcome.replayed, "应成功回放（无溢出）");
    assert!(!outcome.resync_required);
    // 新 sink 应收到回放的事件（seq 2,3）+ 连接确认
    let msgs = recv2.lock().unwrap();
    let widget_seqs: Vec<u64> = msgs
        .iter()
        .filter(|m| m["type"] == "widget_event")
        .map(|m| m["sequence"].as_u64().unwrap())
        .collect();
    assert_eq!(widget_seqs, vec![2, 3], "应回放 seq 2,3");
}

#[tokio::test]
async fn reconnect_returns_resync_when_buffer_overflowed() {
    // 小容量缓冲，溢出后重连触发 resync
    let coord = SessionCoordinator::with_replay_capacity(2);
    let (sink1, _recv1) = MockSink::online();
    coord.register("user-A", sink1);
    coord.register_thread("thread-1", "user-A");
    // emit 5 条 widget（超容量，中间帧丢但保留最新；widget 丢不触发 resync）
    // 为触发 resync，改用流式事件（逐条存，溢出丢旧）
    for i in 1..=5 {
        coord
            .emit_stream("thread-1", &format!("chunk{i}"))
            .await;
    }

    let (sink2, _recv2) = MockSink::online();
    let outcome = coord
        .handle_reconnect("thread-1", "user-A", sink2, 0)
        .await;
    assert!(
        outcome.resync_required,
        "缓冲溢出（请求区间含已丢失流式事件）应返回 resync_required"
    );
}

#[tokio::test]
async fn reconnect_kicks_old_connection() {
    let coord = SessionCoordinator::default();
    let (sink1, _recv1) = MockSink::online();
    coord.register("user-A", sink1.clone());

    // 重连（同 user 新 sink）应踢旧
    let (sink2, _recv2) = MockSink::online();
    let kicked = coord
        .handle_reconnect("thread-1", "user-A", sink2, 0)
        .await;
    assert!(kicked.kicked_old_sink_id.is_some(), "应踢出旧连接");
    assert_eq!(
        kicked.kicked_old_sink_id,
        Some(sink1.id()),
        "被踢的应为旧 sink"
    );
}

#[tokio::test]
async fn reconnect_sends_connection_confirmation() {
    let coord = SessionCoordinator::default();
    let (sink, recv) = MockSink::online();
    coord.handle_reconnect("thread-x", "user-A", sink, 0).await;
    let msgs = recv.lock().unwrap();
    assert!(
        msgs.iter().any(|m| m["type"] == "connection_confirmation"),
        "重连后应发送 connection_confirmation"
    );
}

// ── 监控 M2：session crate 自采指标（监控设计 §三 通道1）──

#[tokio::test]
async fn metrics_emit_widget_increments_push_counter() {
    let coord = SessionCoordinator::default();
    let (sink, _recv) = MockSink::online();
    coord.register("user-A", sink);
    coord.register_thread("thread-1", "user-A");
    coord
        .emit_widget("thread-1", "cost", "tick", serde_json::json!({}), "p")
        .await;
    let snap = coord.metrics().snapshot();
    assert_eq!(snap.event_bus_push_total, 1, "emit_widget 投递成功应 inc push");
    assert_eq!(snap.event_bus_dropped_total, 0);
}

#[tokio::test]
async fn metrics_reconnect_kick_increments_counters() {
    let coord = SessionCoordinator::default();
    let (sink1, _recv1) = MockSink::online();
    coord.register("user-A", sink1);
    // 重连踢旧 → kick_old + replay_hit（无溢出走回放路径）
    let (sink2, _recv2) = MockSink::online();
    coord.handle_reconnect("thread-1", "user-A", sink2, 0).await;
    let snap = coord.metrics().snapshot();
    assert!(snap.kick_old_total >= 1, "踢旧应 inc kick_old");
    assert_eq!(snap.replay_hits_total, 1, "成功回放应 inc replay_hit");
}

#[tokio::test]
async fn metrics_resync_increments_replay_miss() {
    let coord = SessionCoordinator::with_replay_capacity(2);
    let (sink1, _recv1) = MockSink::online();
    coord.register("user-A", sink1);
    coord.register_thread("thread-1", "user-A");
    for i in 1..=5 {
        coord.emit_stream("thread-1", &format!("c{i}")).await;
    }
    let (sink2, _recv2) = MockSink::online();
    let outcome = coord.handle_reconnect("thread-1", "user-A", sink2, 0).await;
    assert!(outcome.resync_required);
    let snap = coord.metrics().snapshot();
    assert_eq!(snap.replay_misses_total, 1, "resync 应 inc replay_miss");
}

#[tokio::test]
async fn metrics_connections_gauge_tracks_registry_size() {
    let coord = SessionCoordinator::default();
    assert_eq!(coord.metrics().snapshot().connections, 0);
    let (sink1, _r1) = MockSink::online();
    coord.register("user-A", sink1);
    assert_eq!(coord.metrics().snapshot().connections, 1);
    let (sink2, _r2) = MockSink::online();
    coord.register("user-B", sink2);
    assert_eq!(coord.metrics().snapshot().connections, 2);
}
