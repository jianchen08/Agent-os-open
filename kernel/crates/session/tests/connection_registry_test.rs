// @feature: FP-0.2.八 多租户核心系统 | @vision: V4 多用户 | @ci: rust-test

//! connection_registry 测试——多端并存扇出 / 配额超限 LRU 踢旧 / user·thread
//! 查找 / 最后一条连接注销清映射（ADR 2026-10-01-multi-frontend-connection）。
//!
//! 真实可运行测试，用 mock EventSink（不依赖 axum WS）。

use agentos_session::{ConnectionRegistry, EventSink};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

/// 测试用 sink：记录 send 是否被调用 + 唯一 id（全局单调 = 注册序，LRU 依据）。
struct MockSink {
    id: u64,
    sent: Arc<std::sync::Mutex<Vec<String>>>,
}

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

impl MockSink {
    fn new() -> (Arc<Self>, Arc<std::sync::Mutex<Vec<String>>>) {
        let sent = Arc::new(std::sync::Mutex::new(Vec::new()));
        let sink = Arc::new(MockSink {
            id: NEXT_ID.fetch_add(1, Ordering::SeqCst),
            sent: sent.clone(),
        });
        (sink, sent)
    }
}

/// 发送恒失败的 sink：构造 broadcast/send_to_user 死连接清理路径。
struct FailingSink {
    id: u64,
}

impl FailingSink {
    fn new() -> Arc<Self> {
        Arc::new(FailingSink {
            id: NEXT_ID.fetch_add(1, Ordering::SeqCst),
        })
    }
}

#[async_trait::async_trait]
impl EventSink for FailingSink {
    async fn send_text(&self, _text: &str) -> bool {
        false
    }
    fn id(&self) -> u64 {
        self.id
    }
}

#[async_trait::async_trait]
impl EventSink for MockSink {
    async fn send_text(&self, text: &str) -> bool {
        self.sent.lock().unwrap().push(text.to_string());
        true
    }
    fn id(&self) -> u64 {
        self.id
    }
}

#[tokio::test]
async fn register_first_connection_has_no_eviction() {
    let registry = ConnectionRegistry::new();
    let (sink, _sent) = MockSink::new();

    let evicted = registry.register("user-A", sink.clone());
    assert!(evicted.is_empty(), "首个连接不应逐出任何连接");
    assert_eq!(registry.sinks_of_user("user-A").len(), 1);
}

#[tokio::test]
async fn same_user_multiple_connections_coexist() {
    // 多端并存：同 user 第二条连接不踢旧（ADR 2026-10-01 取代 B10 互踢）
    let registry = ConnectionRegistry::new();
    let (sink_a1, sent_a1) = MockSink::new();
    let (sink_a2, sent_a2) = MockSink::new();

    registry.register("user-A", sink_a1.clone());
    let evicted = registry.register("user-A", sink_a2.clone());
    assert!(evicted.is_empty(), "配额内新连接不逐出旧连接");

    let sinks = registry.sinks_of_user("user-A");
    assert_eq!(sinks.len(), 2, "两条连接应并存");
    let ids: Vec<u64> = sinks.iter().map(|s| s.id()).collect();
    assert!(ids.contains(&sink_a1.id()) && ids.contains(&sink_a2.id()));
    assert_eq!(registry.active_count(), 2);
    assert!(sent_a1.lock().unwrap().is_empty() && sent_a2.lock().unwrap().is_empty());
}

#[tokio::test]
async fn over_quota_evicts_oldest_lru() {
    // 超限 LRU：上限 2 时注册第 3 条 → 最旧（最小 conn_id）被逐出并返回
    let registry = ConnectionRegistry::with_max_conns(2);
    let (sink1, _) = MockSink::new();
    let (sink2, _) = MockSink::new();
    let (sink3, _) = MockSink::new();

    registry.register("user-A", sink1.clone());
    registry.register("user-A", sink2.clone());
    let evicted = registry.register("user-A", sink3.clone());

    assert_eq!(evicted.len(), 1, "超限逐出恰一条");
    assert_eq!(evicted[0].id(), sink1.id(), "被逐出的应是最旧连接");
    let ids: Vec<u64> = registry
        .sinks_of_user("user-A")
        .iter()
        .map(|s| s.id())
        .collect();
    assert_eq!(ids.len(), 2, "逐出后回到配额内");
    assert!(ids.contains(&sink2.id()) && ids.contains(&sink3.id()));
    assert!(!ids.contains(&sink1.id()));
    assert_eq!(registry.active_count(), 2);
}

#[tokio::test]
async fn different_users_coexist_independently() {
    // 不同 user 互不影响；配额按 user 独立计数
    let registry = ConnectionRegistry::with_max_conns(1);
    let (sink_a, _) = MockSink::new();
    let (sink_b, _) = MockSink::new();

    registry.register("user-A", sink_a.clone());
    let evicted = registry.register("user-B", sink_b.clone());
    assert!(evicted.is_empty(), "不同 user 不互相逐出");

    assert_eq!(registry.sinks_of_user("user-A").len(), 1);
    assert_eq!(registry.sinks_of_user("user-B").len(), 1);
}

#[tokio::test]
async fn sinks_of_user_returns_empty_for_unknown() {
    let registry = ConnectionRegistry::new();
    assert!(registry.sinks_of_user("nobody").is_empty());
}

#[tokio::test]
async fn unregister_removes_only_targeted_connection() {
    let registry = ConnectionRegistry::new();
    let (sink1, _) = MockSink::new();
    let (sink2, _) = MockSink::new();
    registry.register("user-A", sink1.clone());
    registry.register("user-A", sink2.clone());

    registry.unregister("user-A", sink1.id());
    let ids: Vec<u64> = registry
        .sinks_of_user("user-A")
        .iter()
        .map(|s| s.id())
        .collect();
    assert_eq!(ids, vec![sink2.id()], "只移除目标连接，他连接不受影响");
}

#[tokio::test]
async fn unregister_unknown_sink_id_is_noop() {
    // 幽灵注销（已逐出/未注册的 sink id）：不得误删其他连接
    let registry = ConnectionRegistry::new();
    let (sink1, _) = MockSink::new();
    registry.register("user-A", sink1.clone());

    registry.unregister("user-A", u64::MAX);
    assert_eq!(
        registry.sinks_of_user("user-A").len(),
        1,
        "未知 sink id 注销应为 no-op"
    );
}

#[tokio::test]
async fn register_thread_user_maps_thread_to_user() {
    let registry = ConnectionRegistry::new();
    registry.register_thread("thread-1", "user-A");
    assert_eq!(
        registry.get_user_for_thread("thread-1"),
        Some("user-A".to_string())
    );
    assert_eq!(registry.get_user_for_thread("unknown"), None);
}

#[tokio::test]
async fn push_to_thread_fans_out_to_all_user_connections() {
    // send_to_thread 反查 thread→user→连接集合，多端各收一份
    let registry = ConnectionRegistry::new();
    let (sink1, sent1) = MockSink::new();
    let (sink2, sent2) = MockSink::new();
    registry.register("user-A", sink1);
    registry.register("user-A", sink2);
    registry.register_thread("thread-1", "user-A");

    let delivered = registry.send_to_thread("thread-1", "hello").await;
    assert!(delivered, "thread 有活跃连接应投递成功");
    assert_eq!(sent1.lock().unwrap().len(), 1, "连接 1 应各收一份");
    assert_eq!(sent2.lock().unwrap().len(), 1, "连接 2 应各收一份");
    assert_eq!(sent1.lock().unwrap()[0], "hello");
}

#[tokio::test]
async fn push_to_thread_returns_false_when_no_connection() {
    let registry = ConnectionRegistry::new();
    registry.register_thread("thread-1", "user-A"); // user 无活跃连接
    let delivered = registry.send_to_thread("thread-1", "hello").await;
    assert!(!delivered, "无活跃连接应返回 false");
}

#[tokio::test]
async fn push_to_user_fans_out_and_unknown_user_fails() {
    let registry = ConnectionRegistry::new();
    let (sink1, sent1) = MockSink::new();
    let (sink2, sent2) = MockSink::new();
    registry.register("user-A", sink1);
    registry.register("user-A", sink2);

    let delivered = registry.send_to_user("user-A", "ping").await;
    assert!(delivered);
    assert_eq!(sent1.lock().unwrap().len(), 1);
    assert_eq!(sent2.lock().unwrap().len(), 1);

    assert!(
        !registry.send_to_user("user-B", "ping").await,
        "未知 user 应返回 false"
    );
}

#[tokio::test]
async fn send_failure_on_one_connection_does_not_affect_others() {
    // 同 user 一条死连接：扇出不传染——存活连接仍收到；死连接不在本路径
    // 注销（清理归 broadcast 背压兜底/连接自身关闭路径，与单连接时代一致）
    let registry = ConnectionRegistry::new();
    registry.register("user-A", FailingSink::new());
    let (live, sent_live) = MockSink::new();
    registry.register("user-A", live.clone());

    assert!(registry.send_to_user("user-A", "ping").await);
    assert_eq!(sent_live.lock().unwrap().len(), 1, "存活连接应收到");
    assert_eq!(
        registry.sinks_of_user("user-A").len(),
        2,
        "send_to_user 不做死连接注销（语义归 broadcast/关闭路径）"
    );
}

#[tokio::test]
async fn broadcast_delivers_to_all_connections_across_users() {
    let registry = ConnectionRegistry::new();
    let (sink_a1, sent_a1) = MockSink::new();
    let (sink_a2, sent_a2) = MockSink::new();
    let (sink_b, sent_b) = MockSink::new();
    registry.register("user-A", sink_a1);
    registry.register("user-A", sink_a2);
    registry.register("user-B", sink_b);

    let count = registry.broadcast("announce").await;
    assert_eq!(count, 3, "应投递给所有活跃连接（含同 user 多连接）");
    assert_eq!(sent_a1.lock().unwrap()[0], "announce");
    assert_eq!(sent_a2.lock().unwrap()[0], "announce");
    assert_eq!(sent_b.lock().unwrap()[0], "announce");
}

/// 为 user 注册覆盖三张映射的 thread 条目。
fn register_thread_triple(registry: &ConnectionRegistry, thread_id: &str, user_id: &str) {
    registry.register_thread(thread_id, user_id);
    registry.register_thread_pipeline(thread_id, "pipe-1");
    registry.register_thread_agent(thread_id, "agent-1");
}

fn assert_thread_triple_gone(registry: &ConnectionRegistry, thread_id: &str) {
    assert_eq!(
        registry.get_user_for_thread(thread_id),
        None,
        "注销后 thread→user 条目应清除"
    );
    assert_eq!(
        registry.get_pipeline_for_thread(thread_id),
        None,
        "注销后 thread→pipeline 条目应清除"
    );
    assert_eq!(
        registry.get_agent_for_thread(thread_id),
        None,
        "注销后 thread→agent 条目应清除"
    );
}

#[tokio::test]
async fn unregister_purges_thread_maps_only_on_last_connection() {
    // purge 时机：最后一条连接注销才清映射；仅注销其中一条时保留
    let registry = ConnectionRegistry::new();
    let (sink1, _) = MockSink::new();
    let (sink2, _) = MockSink::new();
    registry.register("user-A", sink1.clone());
    registry.register("user-A", sink2.clone());
    register_thread_triple(&registry, "thread-1", "user-A");
    register_thread_triple(&registry, "thread-2", "user-A");

    registry.unregister("user-A", sink1.id());
    assert_eq!(
        registry.get_user_for_thread("thread-1"),
        Some("user-A".to_string()),
        "仍有一条连接存活，映射不得清除"
    );
    assert_eq!(registry.sinks_of_user("user-A").len(), 1);

    registry.unregister("user-A", sink2.id());
    assert!(registry.sinks_of_user("user-A").is_empty());
    assert_thread_triple_gone(&registry, "thread-1");
    assert_thread_triple_gone(&registry, "thread-2");
    assert!(
        registry.list_threads().is_empty(),
        "最后一条连接注销后列表应为空（无历史残留）"
    );
}

#[tokio::test]
async fn unregister_keeps_other_users_thread_maps() {
    // 多连接共享注册表：映射按 thread 归属 user 唯一，注销 user-A 不得
    // 波及 user-B 名下条目。
    let registry = ConnectionRegistry::new();
    let (sink_a, _) = MockSink::new();
    let (sink_b, _) = MockSink::new();
    registry.register("user-A", sink_a.clone());
    registry.register("user-B", sink_b.clone());
    register_thread_triple(&registry, "thread-a", "user-A");
    register_thread_triple(&registry, "thread-b", "user-B");

    registry.unregister("user-A", sink_a.id());

    assert_thread_triple_gone(&registry, "thread-a");
    assert_eq!(
        registry.get_user_for_thread("thread-b"),
        Some("user-B".to_string()),
        "其他连接名下条目不得误删"
    );
    assert_eq!(
        registry.get_pipeline_for_thread("thread-b"),
        Some("pipe-1".to_string())
    );
}

#[tokio::test]
async fn stale_unregister_keeps_surviving_connections_and_maps() {
    // 已不存在的 sink id 注销（旧连接 finally 迟到）：不得波及存活连接与映射
    let registry = ConnectionRegistry::new();
    let (sink1, _) = MockSink::new();
    let (sink2, _) = MockSink::new();
    registry.register("user-A", sink1.clone());
    registry.register("user-A", sink2.clone());
    register_thread_triple(&registry, "thread-1", "user-A");

    registry.unregister("user-A", u64::MAX);

    assert_eq!(
        registry.sinks_of_user("user-A").len(),
        2,
        "存活连接不受影响"
    );
    assert_eq!(
        registry.get_user_for_thread("thread-1"),
        Some("user-A".to_string()),
        "迟到注销不得清除映射"
    );
}

#[tokio::test]
async fn broadcast_dead_connection_purges_thread_maps_only_when_last() {
    // broadcast 清理发送失败的连接 = 注销语义；仅当该 user 最后一条连接死亡
    // 时才清 thread 映射
    let registry = ConnectionRegistry::new();
    registry.register("user-A", FailingSink::new());
    let (sink_ok, _) = MockSink::new();
    registry.register("user-B", sink_ok);
    register_thread_triple(&registry, "thread-a", "user-A");
    register_thread_triple(&registry, "thread-b", "user-B");

    let delivered = registry.broadcast("announce").await;
    assert_eq!(delivered, 1, "死连接不计入投递成功");
    assert!(registry.sinks_of_user("user-A").is_empty(), "死连接被清理");
    assert_thread_triple_gone(&registry, "thread-a");
    assert_eq!(
        registry.get_user_for_thread("thread-b"),
        Some("user-B".to_string()),
        "存活连接名下条目不受影响"
    );
}

#[tokio::test]
async fn memory_stats_counts_all_connections() {
    let registry = ConnectionRegistry::new();
    let (sink_a1, _) = MockSink::new();
    let (sink_a2, _) = MockSink::new();
    let (sink_b, _) = MockSink::new();
    registry.register("user-A", sink_a1);
    registry.register("user-A", sink_a2);
    registry.register("user-B", sink_b);

    let stats = registry.memory_stats();
    assert_eq!(stats.connections, 3, "连接数 = 各 user 连接集合总和");
    assert_eq!(registry.active_count(), 3);
}
