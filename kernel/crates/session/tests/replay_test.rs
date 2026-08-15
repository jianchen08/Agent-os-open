// @feature: FP-0.2.八 多租户核心系统 | @vision: V4 多用户 | @ci: rust-test

//! 断线重放测试——per-thread 环形缓冲 / last_sequence 续传 / 溢出 resync /
//! B9 交互族不进重放 / widget 溢出保留最新帧（ADR §7.2）。

use agentos_session::replay::{EventFamily, ReplayBuffer, ReplayConfig, ReplayEvent, ReplayResult};

fn stream_event(seq: u64, payload: &str) -> ReplayEvent {
    let _ = EventFamily::Stream; // 触发 import 使用
    ReplayEvent::new(seq, payload)
}

fn widget_event(seq: u64, widget_id: &str, payload: &str) -> ReplayEvent {
    ReplayEvent::widget(seq, widget_id, payload)
}

fn interaction_event(seq: u64, request_id: &str) -> ReplayEvent {
    ReplayEvent::interaction(seq, request_id)
}

// ── 基本记录与续传 ──

#[tokio::test]
async fn replay_returns_events_after_last_sequence() {
    let buf = ReplayBuffer::new(ReplayConfig::default());
    // 记录 seq 1..=3
    for s in 1..=3 {
        buf.record("thread-1", stream_event(s, &format!("m{s}")))
            .await;
    }
    // 客户端上报 last_sequence=1，应回放 (1, 3] = seq 2,3
    let result = buf.replay("thread-1", 1).await;
    match result {
        ReplayResult::Events { events, .. } => {
            let seqs: Vec<u64> = events.iter().map(|e| e.sequence).collect();
            assert_eq!(seqs, vec![2, 3]);
        }
        _ => panic!("应返回 Events 而非 resync"),
    }
}

#[tokio::test]
async fn replay_returns_all_when_last_sequence_zero() {
    let buf = ReplayBuffer::new(ReplayConfig::default());
    for s in 1..=2 {
        buf.record("thread-1", stream_event(s, "m")).await;
    }
    let result = buf.replay("thread-1", 0).await;
    match result {
        ReplayResult::Events { events, .. } => {
            assert_eq!(events.len(), 2);
        }
        _ => panic!("last_sequence=0 应回放全部"),
    }
}

#[tokio::test]
async fn replay_empty_when_caught_up() {
    let buf = ReplayBuffer::new(ReplayConfig::default());
    buf.record("thread-1", stream_event(5, "m")).await;
    // last_sequence=5 = 已是最新，应返回空（无新事件）
    let result = buf.replay("thread-1", 5).await;
    match result {
        ReplayResult::Events { events, .. } => {
            assert!(events.is_empty(), "已追平应返回空事件列表");
        }
        _ => panic!("应返回空 Events"),
    }
}

#[tokio::test]
async fn replay_isolated_per_thread() {
    let buf = ReplayBuffer::new(ReplayConfig::default());
    buf.record("thread-1", stream_event(1, "a")).await;
    buf.record("thread-2", stream_event(1, "b")).await;
    let r1 = buf.replay("thread-1", 0).await;
    let r2 = buf.replay("thread-2", 0).await;
    if let (ReplayResult::Events { events: e1, .. }, ReplayResult::Events { events: e2, .. }) =
        (r1, r2)
    {
        assert_eq!(e1.len(), 1);
        assert_eq!(e2.len(), 1);
        assert_ne!(e1[0].payload, e2[0].payload);
    } else {
        panic!("应返回 Events");
    }
}

// ── 溢出 → resync_required（流式族逐条存，FIFO 先到先丢）──

#[tokio::test]
async fn overflow_stream_events_triggers_resync_when_gap_in_replay_range() {
    // 容量 3，记 5 条流式事件 → seq 1,2 被淘汰。
    // 客户端 last_sequence=0 请求 (0,5]，但 1,2 已丢 → resync_required
    let buf = ReplayBuffer::new(ReplayConfig {
        capacity: 3,
        ttl_secs: 300,
    });
    for s in 1..=5 {
        buf.record("thread-1", stream_event(s, "m")).await;
    }
    let result = buf.replay("thread-1", 0).await;
    assert!(
        matches!(result, ReplayResult::ResyncRequired),
        "请求区间含已丢失事件应返回 ResyncRequired"
    );
}

#[tokio::test]
async fn overflow_returns_events_when_replay_range_within_buffer() {
    // 容量 3，记 5 条（seq 1,2 被淘汰，剩 3,4,5）。
    // 客户端 last_sequence=2 请求 (2,5] = seq 3,4,5，全在缓冲内 → 正常回放
    let buf = ReplayBuffer::new(ReplayConfig {
        capacity: 3,
        ttl_secs: 300,
    });
    for s in 1..=5 {
        buf.record("thread-1", stream_event(s, "m")).await;
    }
    let result = buf.replay("thread-1", 2).await;
    match result {
        ReplayResult::Events { events, .. } => {
            let seqs: Vec<u64> = events.iter().map(|e| e.sequence).collect();
            assert_eq!(seqs, vec![3, 4, 5]);
        }
        _ => panic!("区间在缓冲内应正常回放"),
    }
}

// ── widget 溢出保留最新帧（状态快照语义）──

#[tokio::test]
async fn widget_overflow_keeps_latest_frame_per_widget_id() {
    // 容量 2，记 3 个同 widget_id 的 widget 事件。
    // widget 溢出时只保留每个 widget_id 最新一帧，所以缓冲里 widget 的最新帧不丢。
    let buf = ReplayBuffer::new(ReplayConfig {
        capacity: 2,
        ttl_secs: 300,
    });
    buf.record("thread-1", widget_event(1, "cost_panel", "v1"))
        .await;
    buf.record("thread-1", widget_event(2, "cost_panel", "v2"))
        .await;
    buf.record("thread-1", widget_event(3, "cost_panel", "v3"))
        .await;

    // last_sequence=0 请求全部：流式族无丢失，widget 最新帧 v3 应可见
    let result = buf.replay("thread-1", 0).await;
    match result {
        ReplayResult::Events { events, .. } => {
            // 应至少含最新 widget 帧（payload v3）
            let widget_payloads: Vec<&str> = events
                .iter()
                .filter(|e| e.family == EventFamily::Widget)
                .map(|e| e.payload.as_str())
                .collect();
            assert!(
                widget_payloads.contains(&"v3"),
                "widget 溢出应保留最新帧 v3，实际: {:?}",
                widget_payloads
            );
        }
        _ => panic!("widget 溢出不应触发 resync（保留最新帧）"),
    }
}

// ── B9：交互族不进重放缓冲 ──

#[tokio::test]
async fn interaction_events_not_recorded_in_replay_buffer() {
    let buf = ReplayBuffer::new(ReplayConfig::default());
    // 交互族记录时应被拒绝（不进缓冲）
    let accepted = buf.record("thread-1", interaction_event(1, "req-1")).await;
    assert!(!accepted, "B9：interaction_request 类事件不进重放缓冲");
    // 回放应无该事件
    let result = buf.replay("thread-1", 0).await;
    if let ReplayResult::Events { events, .. } = result {
        assert!(events.is_empty());
    }
}

// ── TTL 淘汰 ──

#[tokio::test]
async fn expired_events_evicted_by_ttl() {
    // TTL 极短，事件过期后被淘汰 → 请求过期区间触发 resync
    let buf = ReplayBuffer::new(ReplayConfig {
        capacity: 100,
        ttl_secs: 0, // 立即过期
    });
    buf.record("thread-1", stream_event(1, "m1")).await;
    // 等待使其过期
    tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    let result = buf.replay("thread-1", 0).await;
    // 过期事件不在缓冲 → 区间含丢失 → resync（或空缓冲则空事件）
    match result {
        ReplayResult::ResyncRequired => { /* TTL 过期导致丢失，合理 */ }
        ReplayResult::Events { events, .. } => {
            assert!(events.is_empty(), "过期事件应被淘汰");
        }
    }
}

// ── resync 后返回 latest_sequence 供前端刷新基准 ──

#[tokio::test]
async fn replay_result_events_include_latest_sequence() {
    let buf = ReplayBuffer::new(ReplayConfig::default());
    for s in 1..=3 {
        buf.record("thread-1", stream_event(s, "m")).await;
    }
    let result = buf.replay("thread-1", 0).await;
    if let ReplayResult::Events {
        events,
        latest_sequence,
    } = result
    {
        assert_eq!(events.len(), 3);
        assert_eq!(latest_sequence, 3, "应返回最新 sequence 供前端续传基准");
    } else {
        panic!("应返回 Events");
    }
}
