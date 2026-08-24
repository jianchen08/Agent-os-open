// @feature: FP-0.2.八 多租户核心系统 | @vision: V4 多用户 | @ci: rust-test

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

    // emit 3 条 widget 事件到 thread（widget 事件经统一出口 emit_event 单播）
    for i in 1..=3 {
        coord
            .emit_event("thread-1", "widget_event", serde_json::json!({"i": i}))
            .await;
    }
    // 在线时应直接投递
    assert_eq!(received.lock().unwrap().len(), 3);
}

#[tokio::test]
async fn replay_missed_delivers_buffered_events_to_current_connection() {
    // B3：连接已建立后，replay_missed 把该 thread 缺失的事件投递到当前连接。
    let coord = SessionCoordinator::default();
    let (sink1, _recv1) = MockSink::online();
    coord.register("user-A", sink1);
    coord.register_thread("thread-1", "user-A");
    // emit 3 条聊天事件（经 emit_event，B4 已记录进缓冲；全局 seq 1,2,3）
    for i in 1..=3 {
        coord
            .emit_event("thread-1", "new_message", serde_json::json!({"i": i}))
            .await;
    }

    // 模拟重连：新连接（踢旧），replay_missed(last=1) → 投递 seq 2,3
    let (sink2, recv2) = MockSink::online();
    coord.register("user-A", sink2);
    coord.register_thread("thread-1", "user-A");
    coord.replay_missed("thread-1", "user-A", 1).await;

    let msgs = recv2.lock().unwrap();
    let delivered: Vec<u64> = msgs
        .iter()
        .filter(|m| m["type"].as_str() == Some("new_message"))
        .map(|m| m["sequence"].as_u64().unwrap_or(0))
        .collect();
    assert_eq!(delivered, vec![2, 3], "应回放 seq 2,3 到当前连接");
}

#[tokio::test]
async fn metrics_emit_widget_increments_push_counter() {
    let coord = SessionCoordinator::default();
    let (sink, _recv) = MockSink::online();
    coord.register("user-A", sink);
    coord.register_thread("thread-1", "user-A");
    coord
        .emit_event("thread-1", "widget_event", serde_json::json!({}))
        .await;
    let snap = coord.metrics().snapshot();
    assert_eq!(
        snap.event_bus_push_total, 1,
        "emit_event 投递成功应 inc push"
    );
    assert_eq!(snap.event_bus_dropped_total, 0);
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

/// 建连重放（FIX 2026-08-23）：replay_all_for_user 按 watermark 一次性重放
/// 该 user 名下全部线程的缓冲事件——B3（首条入站 thread_id 触发）在前端重连
/// 后永远等不到触发（前端不重发 active_thread_changed、心跳 thread 为空），
/// 断连期间落缓冲的 new_message 无此方法则永不重放（真机：回复不显示直到刷新）。
#[tokio::test]
async fn replay_all_for_user_delivers_missed_events_for_all_user_threads() {
    let coord = SessionCoordinator::default();

    // user-A 有两条线程（主会话 + 子任务派发键），断连期间各落缓冲事件
    let (sink1, _recv1) = MockSink::with_state(false);
    coord.register("user-A", sink1);
    coord.register_thread("thread-main", "user-A");
    coord.register_thread("pipeline-sub", "user-A");
    // user-B 的线程不应被 user-A 的重放波及
    coord.register_thread("thread-b", "user-B");
    coord
        .emit_event("thread-main", "new_message", serde_json::json!({"m": 1}))
        .await;
    coord
        .emit_event("pipeline-sub", "stream_chunk", serde_json::json!({"c": 1}))
        .await;
    coord
        .emit_event("thread-b", "new_message", serde_json::json!({"m": "B"}))
        .await;

    // 重连：watermark=0（全部未见过），floor 取当前序（无并发新事件）
    let (sink2, recv2) = MockSink::online();
    let floor = coord.current_sequence().await;
    let sink2_dyn: Arc<dyn EventSink> = sink2;
    coord.replay_all_for_user("user-A", 0, floor, &sink2_dyn).await;

    let (seq_main, seq_sub) = {
        let msgs = recv2.lock().unwrap();
        let types: Vec<&str> = msgs.iter().map(|m| m["type"].as_str().unwrap()).collect();
        assert!(types.contains(&"new_message"), "主线程 new_message 应重放");
        assert!(types.contains(&"stream_chunk"), "子任务线程 chunk 应重放");
        assert!(
            !msgs.iter().any(|m| m["data"]["m"] == "B"),
            "别人的线程事件不得混入"
        );
        let seq_main = msgs
            .iter()
            .find(|m| m["type"] == "new_message")
            .and_then(|m| m["sequence"].as_u64())
            .expect("重放事件携带全局 sequence");
        let seq_sub = msgs
            .iter()
            .find(|m| m["type"] == "stream_chunk")
            .and_then(|m| m["sequence"].as_u64())
            .expect("重放事件携带全局 sequence");
        (seq_main, seq_sub)
    };

    // watermark 推进到主线程事件序后：只剩子任务事件重放
    let (sink3, recv3) = MockSink::online();
    let sink3_dyn: Arc<dyn EventSink> = sink3;
    coord
        .replay_all_for_user("user-A", seq_main, floor, &sink3_dyn)
        .await;
    let msgs = recv3.lock().unwrap();
    assert_eq!(msgs.len(), 1, "只应重放 (watermark, floor] 内的子任务事件");
    assert_eq!(msgs[0]["type"], "stream_chunk");
    assert_eq!(
        msgs[0]["sequence"].as_u64().unwrap(),
        seq_sub,
        "重放事件保持原 sequence"
    );
}

/// floor 语义：floor 之后的 sequence 经活动连接实时送达，重放不得重复推送。
#[tokio::test]
async fn replay_all_for_user_respects_floor_to_avoid_duplicates() {
    let coord = SessionCoordinator::default();
    let (sink1, _recv1) = MockSink::online();
    coord.register("user-A", sink1);
    coord.register_thread("thread-1", "user-A");

    coord
        .emit_event("thread-1", "new_message", serde_json::json!({"m": 1}))
        .await;
    // floor 定格在 seq1 之后、seq2 之前——seq2 视为“已实时送达”，不重放
    let floor = coord.current_sequence().await;
    coord
        .emit_event("thread-1", "stream_chunk", serde_json::json!({"c": 2}))
        .await;
    let seq2 = coord.current_sequence().await;

    let (sink2, recv2) = MockSink::online();
    let sink2_dyn: Arc<dyn EventSink> = sink2;
    coord.replay_all_for_user("user-A", 0, floor, &sink2_dyn).await;
    let msgs = recv2.lock().unwrap();
    let seqs: Vec<u64> = msgs.iter().map(|m| m["sequence"].as_u64().unwrap()).collect();
    assert!(!seqs.contains(&seq2), "floor 之后的事件不得重复重放");
    assert_eq!(seqs.len(), 1, "只应重放 floor 之前的事件");
    assert!(seqs[0] <= floor);
}
