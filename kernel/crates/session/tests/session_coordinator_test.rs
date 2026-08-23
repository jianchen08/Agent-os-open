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
async fn reconnect_replays_buffered_events() {
    let coord = SessionCoordinator::default();

    // 第一次连接：emit 事件，建立缓冲
    let (sink1, _recv1) = MockSink::online();
    coord.register("user-A", sink1.clone());
    coord.register_thread("thread-1", "user-A");
    for i in 1..=3 {
        coord
            .emit_event("thread-1", "widget_event", serde_json::json!({"i": i}))
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
async fn emit_event_records_chat_events_for_reconnect_replay() {
    // B4：经 emit_event 发的聊天事件（new_message/stream_start 等）应进重放缓冲，
    // 断线重连时能回放（之前 emit_event 不记录 → 刷新重连丢这些事件）。
    let coord = SessionCoordinator::default();
    let (sink1, _recv1) = MockSink::online();
    coord.register("user-A", sink1);
    coord.register_thread("thread-1", "user-A");

    coord
        .emit_event(
            "thread-1",
            "new_message",
            serde_json::json!({"content": "hi"}),
        )
        .await;
    coord
        .emit_event(
            "thread-1",
            "stream_start",
            serde_json::json!({"message_id": "m1"}),
        )
        .await;

    // 重连，last_sequence=0 → 应回放全部聊天事件
    let (sink2, recv2) = MockSink::online();
    let outcome = coord.handle_reconnect("thread-1", "user-A", sink2, 0).await;
    assert!(outcome.replayed, "应成功回放（无溢出）");

    let msgs = recv2.lock().unwrap();
    let types: Vec<&str> = msgs.iter().filter_map(|m| m["type"].as_str()).collect();
    assert!(types.contains(&"new_message"), "应回放 new_message");
    assert!(types.contains(&"stream_start"), "应回放 stream_start");
}

#[tokio::test]
async fn emit_event_skips_interaction_family_in_replay() {
    // B9 保留：interaction_* 即使经 emit_event 也不进重放缓冲（重放过期审批无意义）。
    let coord = SessionCoordinator::default();
    let (sink1, _recv1) = MockSink::online();
    coord.register("user-A", sink1);
    coord.register_thread("thread-1", "user-A");

    coord
        .emit_event(
            "thread-1",
            "interaction_request",
            serde_json::json!({"req": "x"}),
        )
        .await;

    let (sink2, recv2) = MockSink::online();
    coord.handle_reconnect("thread-1", "user-A", sink2, 0).await;

    let msgs = recv2.lock().unwrap();
    let has_interaction = msgs
        .iter()
        .any(|m| m["type"].as_str() == Some("interaction_request"));
    assert!(!has_interaction, "interaction_* 不应进重放缓冲（B9）");
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
async fn reconnect_returns_resync_when_buffer_overflowed() {
    // 小容量缓冲，溢出后重连触发 resync
    let coord = SessionCoordinator::with_replay_capacity(2);
    let (sink1, _recv1) = MockSink::online();
    coord.register("user-A", sink1);
    coord.register_thread("thread-1", "user-A");
    // emit 5 条聊天事件（逐条存，溢出丢旧，触发 resync）
    for i in 1..=5 {
        coord
            .emit_event(
                "thread-1",
                "stream_chunk",
                serde_json::json!({"chunk": format!("chunk{i}")}),
            )
            .await;
    }

    let (sink2, _recv2) = MockSink::online();
    let outcome = coord.handle_reconnect("thread-1", "user-A", sink2, 0).await;
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
    let kicked = coord.handle_reconnect("thread-1", "user-A", sink2, 0).await;
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
        coord
            .emit_event(
                "thread-1",
                "stream_chunk",
                serde_json::json!({"chunk": format!("c{i}")}),
            )
            .await;
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
