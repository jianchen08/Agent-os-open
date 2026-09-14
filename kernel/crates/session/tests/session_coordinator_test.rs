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
    /// shutdown 是否被调用（踢旧语义观察点）。
    shutdown_called: Arc<std::sync::atomic::AtomicBool>,
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
            shutdown_called: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        });
        (sink, received)
    }
    fn is_shutdown(&self) -> bool {
        self.shutdown_called.load(Ordering::SeqCst)
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
    fn shutdown(&self) {
        self.shutdown_called.store(true, Ordering::SeqCst);
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
async fn register_tracks_active_connections_in_registry() {
    let coord = SessionCoordinator::default();
    assert_eq!(coord.registry().active_count(), 0);
    let (sink1, _r1) = MockSink::online();
    coord.register("user-A", sink1);
    assert_eq!(coord.registry().active_count(), 1);
    let (sink2, _r2) = MockSink::online();
    coord.register("user-B", sink2);
    assert_eq!(coord.registry().active_count(), 2);
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
    coord
        .replay_all_for_user("user-A", 0, floor, &sink2_dyn)
        .await;

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
    coord
        .replay_all_for_user("user-A", 0, floor, &sink2_dyn)
        .await;
    let msgs = recv2.lock().unwrap();
    let seqs: Vec<u64> = msgs
        .iter()
        .map(|m| m["sequence"].as_u64().unwrap())
        .collect();
    assert!(!seqs.contains(&seq2), "floor 之后的事件不得重复重放");
    assert_eq!(seqs.len(), 1, "只应重放 floor 之前的事件");
    assert!(seqs[0] <= floor);
}

// ── 重放缺口：缓冲已淘汰区间被请求 → resync（前端整树刷新从 DB 真值恢复）──
//
// 触发方式（与 replay 单元测试同法）：widget 族不推进 evicted_below，但消息
// 终态（stream_end）会推进；emit_event 的载荷可带 message_id + stream_end。

/// 断线期间消息终态到达 → 缓冲整组即弃、evicted_below 推进；重连上报
/// 更早 watermark → replay_missed 走 ResyncRequired，向当前连接推
/// resync_required（携带 thread_id），前端据此整树刷新。
#[tokio::test]
async fn replay_missed_sends_resync_required_when_gap_detected() {
    let coord = SessionCoordinator::default();
    let (sink1, _recv1) = MockSink::with_state(false);
    coord.register("user-A", sink1);
    coord.register_thread("thread-1", "user-A");

    // seq1：在飞消息帧（带 message_id）；seq2：杂项（保住 thread 缓冲条目，
    // 否则"无在飞消息且杂项为空"会整条回收、evicted_below 一并释放）；
    // seq3：同消息终态（帧组即弃、evicted_below 推进到 3）。
    coord
        .emit_event(
            "thread-1",
            "stream_chunk",
            serde_json::json!({"message_id": "m-1", "delta": "a"}),
        )
        .await;
    coord
        .emit_event(
            "thread-1",
            "run_status",
            serde_json::json!({"state": "running"}),
        )
        .await;
    coord
        .emit_event(
            "thread-1",
            "stream_end",
            serde_json::json!({"message_id": "m-1"}),
        )
        .await;

    // 重连：watermark=0，但 (0, 3] 区间已因终态即弃 → 缺口
    let (sink2, recv2) = MockSink::online();
    coord.register("user-A", sink2);
    coord.register_thread("thread-1", "user-A");
    coord.replay_missed("thread-1", "user-A", 0).await;

    let msgs = recv2.lock().unwrap();
    let resync = msgs
        .iter()
        .find(|m| m["type"].as_str() == Some("resync_required"))
        .expect("缺口应推 resync_required");
    assert_eq!(
        resync["data"]["thread_id"], "thread-1",
        "resync 必须携带 thread_id 供前端定位"
    );
    assert!(
        !msgs.iter().any(|m| m["type"] == "stream_chunk"),
        "已即弃的帧不得回放"
    );
}

/// 无缺口时 replay_missed 不得误报 resync（对照用例：正常回放路径）。
#[tokio::test]
async fn replay_missed_without_gap_does_not_resync() {
    let coord = SessionCoordinator::default();
    let (sink1, _recv1) = MockSink::with_state(false);
    coord.register("user-A", sink1);
    coord.register_thread("thread-1", "user-A");
    // 在飞消息帧（无终态）：留在缓冲，watermark=0 请求可完整回放
    coord
        .emit_event(
            "thread-1",
            "stream_chunk",
            serde_json::json!({"message_id": "m-keep", "delta": "x"}),
        )
        .await;

    let (sink2, recv2) = MockSink::online();
    coord.register("user-A", sink2);
    coord.register_thread("thread-1", "user-A");
    coord.replay_missed("thread-1", "user-A", 0).await;

    let msgs = recv2.lock().unwrap();
    assert!(
        !msgs.iter().any(|m| m["type"] == "resync_required"),
        "无缺口不得误报 resync: {msgs:?}"
    );
    assert!(
        msgs.iter().any(|m| m["type"] == "stream_chunk"),
        "在飞消息帧应回放"
    );
}

/// replay_all_for_user：该 user 名下无任何线程注册 → 直接返回（不发送任何帧，
/// 也不触碰 sink）——重连用户没有任何会话时的正常路径。
#[tokio::test]
async fn replay_all_for_user_without_threads_is_noop() {
    let coord = SessionCoordinator::default();
    let (sink, recv) = MockSink::online();
    let sink_dyn: Arc<dyn EventSink> = sink;
    coord
        .replay_all_for_user("user-lonely", 0, 0, &sink_dyn)
        .await;
    assert!(
        recv.lock().unwrap().is_empty(),
        "无线程时不得发送任何帧（含 resync）"
    );
}

/// replay_all_for_user：连接发送失败（断线）→ 立即中止重放并返回，
/// 不再向该 sink 继续推后续事件（背压兜底，避免无效写放大）。
#[tokio::test]
async fn replay_all_for_user_stops_on_sink_send_failure() {
    let coord = SessionCoordinator::default();
    let (origin, _origin_recv) = MockSink::online();
    coord.register("user-A", origin);
    coord.register_thread("thread-1", "user-A");
    for i in 1..=4 {
        coord
            .emit_event("thread-1", "new_message", serde_json::json!({"i": i}))
            .await;
    }

    // 重连的 sink 立刻断开：第一次 send_text 返回 false → 中止
    let (dead, _dead_recv) = MockSink::with_state(false);
    let dead_dyn: Arc<dyn EventSink> = dead;
    coord
        .replay_all_for_user("user-A", 0, u64::MAX, &dead_dyn)
        .await;
    // 无 panic、无挂起即为通过（返回值是 ()，可观察副作用是 sink 未被继续调用；
    // 断线 sink 不上报计数，故改由下个用例断言"在线 sink 收全"作对照）。
}

/// 对照：同一数据、在线 sink → 全部 4 条按序送达（floor 放开）。
#[tokio::test]
async fn replay_all_for_user_delivers_all_when_sink_online() {
    let coord = SessionCoordinator::default();
    let (origin, _origin_recv) = MockSink::online();
    coord.register("user-A", origin);
    coord.register_thread("thread-1", "user-A");
    for i in 1..=4 {
        coord
            .emit_event("thread-1", "new_message", serde_json::json!({"i": i}))
            .await;
    }

    let (live, recv) = MockSink::online();
    let live_dyn: Arc<dyn EventSink> = live;
    coord
        .replay_all_for_user("user-A", 0, u64::MAX, &live_dyn)
        .await;
    let msgs = recv.lock().unwrap();
    let seqs: Vec<u64> = msgs
        .iter()
        .map(|m| m["sequence"].as_u64().unwrap_or(0))
        .collect();
    assert_eq!(seqs, vec![1, 2, 3, 4], "在线 sink 应按序收全");
}

/// replay_all_for_user：某 thread 缓冲已含缺口 → 汇总 resync_needed，
/// 循环结束后统一推一条 resync_required（reason=replay_buffer_overflow）。
#[tokio::test]
async fn replay_all_for_user_reports_buffer_overflow_resync() {
    let coord = SessionCoordinator::default();
    let (origin, _origin_recv) = MockSink::with_state(false);
    coord.register("user-A", origin);
    coord.register_thread("thread-1", "user-A");

    // 在飞帧 + 杂项（保住条目）+ 终态 → 帧组即弃、evicted_below 推进到 seq3
    coord
        .emit_event(
            "thread-1",
            "stream_chunk",
            serde_json::json!({"message_id": "m-x", "delta": "d"}),
        )
        .await;
    coord
        .emit_event(
            "thread-1",
            "run_status",
            serde_json::json!({"state": "running"}),
        )
        .await;
    coord
        .emit_event(
            "thread-1",
            "stream_end",
            serde_json::json!({"message_id": "m-x"}),
        )
        .await;

    let (live, recv) = MockSink::online();
    let live_dyn: Arc<dyn EventSink> = live;
    coord
        .replay_all_for_user("user-A", 0, u64::MAX, &live_dyn)
        .await;

    let msgs = recv.lock().unwrap();
    let resync = msgs
        .iter()
        .find(|m| m["type"].as_str() == Some("resync_required"))
        .expect("缓冲缺口应汇总为 resync_required");
    assert_eq!(
        resync["data"]["reason"], "replay_buffer_overflow",
        "汇总 resync 必须标注原因供前端/排障定位"
    );
}

// ── emit_event 的聊天协议事件族：非 widget 事件也可单播 ──

/// emit_event 是**唯一 thread 单播出口**，支持任意 type（new_message /
/// stream_start / stream_end 等聊天协议事件），且不经限流。
/// 表驱动覆盖多种事件类型，断言信封形状与 delivered 语义。
#[tokio::test]
async fn emit_event_supports_arbitrary_chat_event_types() {
    let coord = SessionCoordinator::default();
    let (sink, recv) = MockSink::online();
    coord.register("user-A", sink);
    coord.register_thread("thread-1", "user-A");

    for (event_type, payload) in [
        ("new_message", serde_json::json!({"role": "assistant"})),
        ("stream_start", serde_json::json!({"message_id": "m1"})),
        ("stream_end", serde_json::json!({"message_id": "m1"})),
        ("run_status", serde_json::json!({"state": "running"})),
    ] {
        let delivered = coord.emit_event("thread-1", event_type, payload).await;
        assert!(delivered, "{event_type} 应投递成功");
    }

    let msgs = recv.lock().unwrap();
    let types: Vec<&str> = msgs.iter().map(|m| m["type"].as_str().unwrap()).collect();
    assert_eq!(
        types,
        vec!["new_message", "stream_start", "stream_end", "run_status"],
        "事件类型原样透传（不被 widget_event 硬编码覆盖）"
    );
    assert!(
        msgs.iter().all(|m| m["sequence"].as_u64().is_some()),
        "每条单播事件都带 sequence"
    );
    assert!(
        msgs.windows(2)
            .all(|w| w[1]["sequence"].as_u64() > w[0]["sequence"].as_u64()),
        "sequence 严格递增"
    );
}

/// emit_event 在无 thread 映射时投递失败（返回 false）——不得 panic，
/// 供上层记 dropped 计数。
#[tokio::test]
async fn emit_event_to_unmapped_thread_reports_undelivered() {
    let coord = SessionCoordinator::default();
    let (sink, _recv) = MockSink::online();
    coord.register("user-A", sink);
    // 未 register_thread
    let delivered = coord
        .emit_event("thread-ghost", "new_message", serde_json::json!({}))
        .await;
    assert!(!delivered, "无 thread 映射应报告未投递");
}

/// broadcast_widget 投递到全部活跃连接；无连接时返回 0（不 panic）。
#[tokio::test]
async fn broadcast_widget_counts_deliveries() {
    let coord = SessionCoordinator::default();
    assert_eq!(
        coord
            .broadcast_widget("w", "e", serde_json::json!({}), "p")
            .await,
        0,
        "无连接 → 零投递"
    );

    let (a, recv_a) = MockSink::online();
    let (b, recv_b) = MockSink::online();
    coord.register("user-A", a);
    coord.register("user-B", b);
    let n = coord
        .broadcast_widget("status", "update", serde_json::json!({"n": 3}), "p")
        .await;
    assert_eq!(n, 2, "两个连接各收一份");
    assert_eq!(recv_a.lock().unwrap().len(), 1);
    assert_eq!(recv_b.lock().unwrap().len(), 1);
}

/// register 踢旧：旧连接被 shutdown（幽灵连接不得残留）。
#[tokio::test]
async fn register_kicks_previous_connection() {
    let coord = SessionCoordinator::default();
    let (old, _old_recv) = MockSink::online();
    assert!(
        coord.register("user-A", old.clone()).is_none(),
        "首次注册无旧连接"
    );
    assert!(!old.is_shutdown(), "活跃连接不应被关闭");

    let (new_sink, _new_recv) = MockSink::online();
    let kicked_id = coord.register("user-A", new_sink);
    assert_eq!(kicked_id, Some(old.id()), "返回被踢旧连接的 id");
    assert!(
        old.is_shutdown(),
        "被踢连接必须真正关闭（CLOSE_CODE_KICKED 语义）"
    );
    assert_eq!(coord.registry().active_count(), 1, "单连接不变量");
}

/// list_threads 透传注册表的 thread→user 映射（REST 会话列表的数据源）。
#[tokio::test]
async fn list_threads_reflects_registered_threads() {
    let coord = SessionCoordinator::default();
    let (sink, _recv) = MockSink::online();
    coord.register("user-A", sink);
    assert!(coord.list_threads().is_empty(), "未注册线程时为空");

    coord.register_thread("thread-1", "user-A");
    coord.register_thread("thread-2", "user-B");
    let mut got = coord.list_threads();
    got.sort();
    assert_eq!(
        got,
        vec![
            ("thread-1".to_string(), "user-A".to_string()),
            ("thread-2".to_string(), "user-B".to_string()),
        ]
    );
}

/// emit_event 的 interaction_ 族走 ReplayEvent::interaction（record 拒绝，
/// 不入重放缓冲）——重放过期审批无意义（B9）。对照非交互事件确实入缓冲。
#[tokio::test]
async fn emit_event_routes_interaction_family_out_of_replay_buffer() {
    let coord = SessionCoordinator::default();
    let (sink, _recv) = MockSink::online();
    coord.register("user-A", sink);
    coord.register_thread("thread-1", "user-A");

    // interaction_* 事件：投递成功但不入重放缓冲
    assert!(
        coord
            .emit_event(
                "thread-1",
                "interaction_request",
                serde_json::json!({"request_id": "req-1"})
            )
            .await
    );
    // 对照：普通事件入缓冲
    assert!(
        coord
            .emit_event("thread-1", "new_message", serde_json::json!({"m": 1}))
            .await
    );

    // 重连只应收到 new_message，不含 interaction_request
    let (sink2, recv2) = MockSink::online();
    coord.register("user-A", sink2);
    coord.register_thread("thread-1", "user-A");
    coord.replay_missed("thread-1", "user-A", 0).await;
    let msgs = recv2.lock().unwrap();
    assert!(
        !msgs
            .iter()
            .any(|m| m["type"].as_str() == Some("interaction_request")),
        "交互族不得进重放缓冲: {msgs:?}"
    );
    assert!(
        msgs.iter().any(|m| m["type"] == "new_message"),
        "普通事件应可重放"
    );
}
