// @feature: FP-0.2.八 多租户核心系统 | @vision: V4 多用户 | @ci: rust-test

//! connection_registry 测试——单连接踢旧 / user/thread 查找 / 注销（ADR §7.2 B10）。
//!
//! 真实可运行测试，用 mock EventSink（不依赖 axum WS）。

use agentos_session::{ConnectionRegistry, EventSink};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

/// 测试用 sink：记录 send 是否被调用 + 唯一 id。
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

/// 发送恒失败的 sink：构造 broadcast 死连接清理路径。
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
async fn register_first_connection_has_no_old_to_kick() {
    let registry = ConnectionRegistry::new();
    let (sink, _sent) = MockSink::new();

    let kicked = registry.register("user-A", sink.clone());
    assert!(kicked.is_none(), "首个连接不应踢出任何旧连接");
    assert!(registry.get_by_user("user-A").is_some());
}

#[tokio::test]
async fn register_new_connection_kicks_old_same_user() {
    // B10：每个 user 一条 WS，新连接踢旧连接
    let registry = ConnectionRegistry::new();
    let (old_sink, _) = MockSink::new();
    let (new_sink, _) = MockSink::new();

    registry.register("user-A", old_sink.clone());
    let kicked = registry.register("user-A", new_sink.clone());

    let kicked = kicked.expect("同 user 新连接应踢出旧连接");
    assert_eq!(kicked.id(), old_sink.id(), "被踢出的应为旧 sink");

    // 注册表现在应指向新连接
    let current = registry.get_by_user("user-A").expect("新连接应已注册");
    assert_eq!(current.id(), new_sink.id(), "注册表当前连接应为新 sink");
}

#[tokio::test]
async fn register_different_users_coexist() {
    // 不同 user 不互踢
    let registry = ConnectionRegistry::new();
    let (sink_a, _) = MockSink::new();
    let (sink_b, _) = MockSink::new();

    registry.register("user-A", sink_a.clone());
    let kicked = registry.register("user-B", sink_b.clone());
    assert!(kicked.is_none(), "不同 user 不应互踢");

    assert!(registry.get_by_user("user-A").is_some());
    assert!(registry.get_by_user("user-B").is_some());
}

#[tokio::test]
async fn get_by_user_returns_none_for_unknown() {
    let registry = ConnectionRegistry::new();
    assert!(registry.get_by_user("nobody").is_none());
}

#[tokio::test]
async fn unregister_removes_connection() {
    let registry = ConnectionRegistry::new();
    let (sink, _) = MockSink::new();
    registry.register("user-A", sink.clone());

    registry.unregister("user-A", sink.id());
    assert!(registry.get_by_user("user-A").is_none(), "注销后应查不到");
}

#[tokio::test]
async fn unregister_skips_when_current_is_newer() {
    // 旧连接的 finally 块调用 unregister 时，若已被新连接替换，不应误删新连接
    let registry = ConnectionRegistry::new();
    let (old_sink, _) = MockSink::new();
    let (new_sink, _) = MockSink::new();

    registry.register("user-A", old_sink.clone());
    registry.register("user-A", new_sink.clone()); // 踢掉 old

    // old 连接的清理路径调用 unregister（传 old_sink）
    registry.unregister("user-A", old_sink.id());

    // 新连接应仍在
    let current = registry
        .get_by_user("user-A")
        .expect("新连接不应被旧连接的注销误删");
    assert_eq!(current.id(), new_sink.id());
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
async fn push_to_thread_routes_via_thread_user_map() {
    // send_to_thread 反查 thread→user→连接
    let registry = ConnectionRegistry::new();
    let (sink, sent) = MockSink::new();
    registry.register("user-A", sink);
    registry.register_thread("thread-1", "user-A");

    let delivered = registry.send_to_thread("thread-1", "hello").await;
    assert!(delivered, "thread 有活跃连接应投递成功");
    let msgs = sent.lock().unwrap();
    assert_eq!(msgs.len(), 1);
    assert_eq!(msgs[0], "hello");
}

#[tokio::test]
async fn push_to_thread_returns_false_when_no_connection() {
    let registry = ConnectionRegistry::new();
    registry.register_thread("thread-1", "user-A"); // user 无活跃连接
    let delivered = registry.send_to_thread("thread-1", "hello").await;
    assert!(!delivered, "无活跃连接应返回 false");
}

#[tokio::test]
async fn push_to_user_delivers_to_user_connection() {
    let registry = ConnectionRegistry::new();
    let (sink, sent) = MockSink::new();
    registry.register("user-A", sink);

    let delivered = registry.send_to_user("user-A", "ping").await;
    assert!(delivered);
    assert_eq!(sent.lock().unwrap()[0], "ping");

    assert!(
        !registry.send_to_user("user-B", "ping").await,
        "未知 user 应返回 false"
    );
}

#[tokio::test]
async fn broadcast_delivers_to_all_connections() {
    let registry = ConnectionRegistry::new();
    let (sink_a, sent_a) = MockSink::new();
    let (sink_b, sent_b) = MockSink::new();
    registry.register("user-A", sink_a);
    registry.register("user-B", sink_b);

    let count = registry.broadcast("announce").await;
    assert_eq!(count, 2, "应投递给所有活跃连接");
    assert_eq!(sent_a.lock().unwrap()[0], "announce");
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
async fn unregister_purges_all_thread_maps_of_that_connection() {
    let registry = ConnectionRegistry::new();
    let (sink, _) = MockSink::new();
    registry.register("user-A", sink.clone());
    register_thread_triple(&registry, "thread-1", "user-A");
    register_thread_triple(&registry, "thread-2", "user-A");

    registry.unregister("user-A", sink.id());

    assert!(registry.get_by_user("user-A").is_none());
    assert_thread_triple_gone(&registry, "thread-1");
    assert_thread_triple_gone(&registry, "thread-2");
    assert!(
        registry.list_threads().is_empty(),
        "该连接名下条目清除后列表应为空（无历史残留）"
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
async fn stale_unregister_keeps_thread_maps_of_new_connection() {
    // 旧连接的 finally 块注销（已被新连接替换）不得清除映射条目
    let registry = ConnectionRegistry::new();
    let (old_sink, _) = MockSink::new();
    let (new_sink, _) = MockSink::new();
    registry.register("user-A", old_sink.clone());
    registry.register("user-A", new_sink.clone());
    register_thread_triple(&registry, "thread-1", "user-A");

    registry.unregister("user-A", old_sink.id());

    assert!(registry.get_by_user("user-A").is_some(), "新连接仍在");
    assert_eq!(
        registry.get_user_for_thread("thread-1"),
        Some("user-A".to_string()),
        "被替换连接的注销不得清除映射"
    );
}

#[tokio::test]
async fn broadcast_dead_connection_purges_thread_maps() {
    // broadcast 清理发送失败的连接 = 注销语义：thread 映射同步清除
    let registry = ConnectionRegistry::new();
    registry.register("user-A", FailingSink::new());
    let (sink_ok, _) = MockSink::new();
    registry.register("user-B", sink_ok);
    register_thread_triple(&registry, "thread-a", "user-A");
    register_thread_triple(&registry, "thread-b", "user-B");

    let delivered = registry.broadcast("announce").await;
    assert_eq!(delivered, 1, "死连接不计入投递成功");
    assert!(registry.get_by_user("user-A").is_none(), "死连接被清理");
    assert_thread_triple_gone(&registry, "thread-a");
    assert_eq!(
        registry.get_user_for_thread("thread-b"),
        Some("user-B".to_string()),
        "存活连接名下条目不受影响"
    );
}
