//! FrontendEventBus 测试——唯一出口 push_to_* / 序号 / 限流 / 广播（ADR §3.5）。

use agentos_session::event_bus::{EmitScope, FrontendEventBus, RateLimitConfig};
use agentos_session::ConnectionRegistry;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

struct MockSink {
    id: u64,
    received: Arc<Mutex<Vec<serde_json::Value>>>,
}
static NEXT_ID: AtomicU64 = AtomicU64::new(1);
impl MockSink {
    fn new() -> (Arc<Self>, Arc<Mutex<Vec<serde_json::Value>>>) {
        let received = Arc::new(Mutex::new(Vec::new()));
        (
            Arc::new(MockSink {
                id: NEXT_ID.fetch_add(1, Ordering::SeqCst),
                received: received.clone(),
            }),
            received,
        )
    }
}
#[async_trait::async_trait]
impl agentos_session::EventSink for MockSink {
    async fn send_text(&self, text: &str) -> bool {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(text) {
            self.received.lock().unwrap().push(v);
        }
        true
    }
    fn id(&self) -> u64 {
        self.id
    }
}

fn bus_with_user(user_id: &str) -> (FrontendEventBus, Arc<Mutex<Vec<serde_json::Value>>>) {
    let registry = Arc::new(ConnectionRegistry::new());
    let (sink, received) = MockSink::new();
    registry.register(user_id, sink);
    (FrontendEventBus::new(registry), received)
}

// ── 基本投递与信封 ──

#[tokio::test]
async fn emit_to_thread_delivers_widget_event_envelope() {
    let (bus, received) = bus_with_user("user-A");
    bus.register_thread("thread-1", "user-A");
    bus.emit(
        "cost_panel",
        "cost.tick",
        serde_json::json!({"tokens": 1280}),
        EmitScope::Thread("thread-1".into()),
        "plugin-cost",
    )
    .await;

    let msgs = received.lock().unwrap();
    assert_eq!(msgs.len(), 1);
    let msg = &msgs[0];
    // 信封：{type:"widget_event", data:{widget_id,event,data}, metadata, sequence}
    assert_eq!(msg["type"], "widget_event");
    assert_eq!(msg["data"]["widget_id"], "cost_panel");
    assert_eq!(msg["data"]["event"], "cost.tick");
    assert_eq!(msg["data"]["data"]["tokens"], 1280);
    assert!(msg["sequence"].is_u64(), "widget_event 必须带 sequence");
}

#[tokio::test]
async fn emit_assigns_monotonic_sequence_per_thread() {
    // 同一 thread 连续 emit，sequence 单调递增
    let (bus, received) = bus_with_user("user-A");
    bus.register_thread("thread-1", "user-A");

    for _ in 0..3 {
        bus.emit("w", "e", serde_json::json!({}), EmitScope::Thread("thread-1".into()), "p")
            .await;
    }
    let msgs = received.lock().unwrap();
    let seqs: Vec<u64> = msgs.iter().map(|m| m["sequence"].as_u64().unwrap()).collect();
    assert_eq!(seqs.len(), 3);
    assert!(seqs.windows(2).all(|w| w[1] > w[0]), "sequence 应严格递增");
}

#[tokio::test]
async fn emit_thread_routes_via_registry_thread_map() {
    let registry = Arc::new(ConnectionRegistry::new());
    let (sink, received) = MockSink::new();
    registry.register("user-A", sink);
    registry.register_thread("thread-1", "user-A");
    let bus = FrontendEventBus::new(registry);

    let delivered = bus
        .emit("w", "e", json!({}), EmitScope::Thread("thread-1".into()), "p")
        .await;
    assert!(delivered > 0, "thread scope 有连接应投递成功");
    assert_eq!(received.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn emit_to_user_routes_directly() {
    let (bus, received) = bus_with_user("user-A");
    let delivered = bus
        .emit("w", "e", json!({}), EmitScope::User("user-A".into()), "p")
        .await;
    assert!(delivered > 0);
    assert_eq!(received.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn emit_broadcast_delivers_to_all() {
    let registry = Arc::new(ConnectionRegistry::new());
    let (sink_a, recv_a) = MockSink::new();
    let (sink_b, recv_b) = MockSink::new();
    registry.register("user-A", sink_a);
    registry.register("user-B", sink_b);
    let bus = FrontendEventBus::new(registry);

    let count = bus
        .emit("w", "e", json!({}), EmitScope::Broadcast, "p")
        .await;
    assert_eq!(count, 2, "广播应投递给全部连接");
    assert_eq!(recv_a.lock().unwrap().len(), 1);
    assert_eq!(recv_b.lock().unwrap().len(), 1);
}

// ── 限流：per-plugin 令牌桶（ADR §3.5 第6条）──

#[tokio::test]
async fn rate_limit_drops_events_beyond_burst() {
    // 突发上限 burst=5，快速发 10 条，应只投递 5 条（其余丢弃）
    let registry = Arc::new(ConnectionRegistry::new());
    let (sink, received) = MockSink::new();
    registry.register("user-A", sink);
    registry.register_thread("thread-1", "user-A");
    let bus = FrontendEventBus::with_rate_limit(
        registry,
        RateLimitConfig {
            burst: 5,
            refill_per_sec: 1, // 低速补充，确保突发期间不补
        },
    );

    let mut delivered_count = 0usize;
    for _ in 0..10 {
        if bus
            .emit("w", "e", json!({}), EmitScope::Thread("thread-1".into()), "p")
            .await
            > 0
        {
            delivered_count += 1;
        }
    }
    assert_eq!(
        delivered_count, 5,
        "突发 5 条后应丢弃（令牌桶耗尽），实际投递 {delivered_count}"
    );
    assert_eq!(
        received.lock().unwrap().len(),
        5,
        "sink 应只收到 5 条"
    );
}

#[tokio::test]
async fn rate_limit_is_per_plugin_independent() {
    // 两个不同 plugin 各自独立限流，互不影响
    let registry = Arc::new(ConnectionRegistry::new());
    let (sink, received) = MockSink::new();
    registry.register("user-A", sink);
    registry.register_thread("thread-1", "user-A");
    let bus = FrontendEventBus::with_rate_limit(
        registry,
        RateLimitConfig {
            burst: 3,
            refill_per_sec: 1,
        },
    );

    // plugin-A 发 3 条（满突发），plugin-B 也发 3 条（独立桶）
    for _ in 0..3 {
        bus.emit("w", "e", json!({}), EmitScope::Thread("thread-1".into()), "plugin-A")
            .await;
    }
    for _ in 0..3 {
        bus.emit("w", "e", json!({}), EmitScope::Thread("thread-1".into()), "plugin-B")
            .await;
    }
    assert_eq!(
        received.lock().unwrap().len(),
        6,
        "两个 plugin 各 3 条，互不挤占令牌"
    );
}

use serde_json::json;
