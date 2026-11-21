// @feature: FP-0.2.WS强制发送 停当前run+投递+触发新一轮 | @ci: rust-test
//!
//! 强制发送通道（2026-11-14 修「送消息给下级/前端强制发送发了没反应」）。
//!
//! 旧行为：消息一律走 `dispatch_user_input`——链忙则排队等当前 run 的自然边界，
//! 目标管道已停则无人驱动，外部表现「发了没反应」。
//! 新语义（用户定义）：停当前 running（引擎层停止信号，不退回队列）→ 投递
//! 本条 → 触发新一轮。本组测试锁定两步时序的可观测结果：
//! 1. 目标管道在飞 run 被置 Suspended（停止真的落地）；
//! 2. 本条消息真的进入该管道队列（可被消费循环取走，而非只在内存里丢）；
//! 3. run 已在停止中时幂等，不落 pending_cancel（防零执行误杀下一条 run）。

use super::*;

use agentos_core::traits::StorageBackend;
use agentos_core::types::PendingInputSource;
use agentos_engine::SqliteStore;

/// 占链模拟「run 在飞」：释放句柄 drop 前 `has_chain` 恒 true，派发走链忙
/// 入队分支（不真跑引擎）。管道 id 用测试专名，避免污染并行测试的进程级链表。
fn occupy_chain(pipeline_id: &str, user_id: &str) -> tokio::sync::oneshot::Sender<()> {
    let registry = crate::run_chain::RunChainRegistry::global();
    let (tx, rx) = tokio::sync::oneshot::channel::<()>();
    let occupied = registry.try_enqueue_direct(pipeline_id, user_id, async move {
        let _ = rx.await;
    });
    assert!(occupied, "测试占链失败（管道 id 冲突）: {pipeline_id}");
    tx
}

fn dispatcher_with(store: Arc<dyn StorageBackend>) -> EngineDispatcher {
    let mut state = crate::routes::AppState::new();
    state.store = Some(store);
    EngineDispatcher::new(state)
}

async fn has_pending_cancel(store: &Arc<dyn StorageBackend>, pipeline_id: &str) -> bool {
    store
        .load_pipeline_state(pipeline_id, "default")
        .await
        .unwrap()
        .get("pending_cancel")
        .and_then(|v| v.as_bool())
        == Some(true)
}

/// 生产接线同形播种：显式 pipeline_id 派发前须经成员校验（pipeline_sessions
/// 绑定真值），生产由 chat.send_message 派发段 link_pipeline_session 写入，
/// 测试补齐同一绑定再派发。
async fn link_pipeline(store: &Arc<dyn StorageBackend>, pipeline_id: &str, thread_id: &str) {
    store
        .link_pipeline_session(pipeline_id, thread_id, "default")
        .await
        .unwrap();
}

/// 强制发送：在飞 run 必须被停（suspended），本条消息必须真的进队列可被消费。
#[tokio::test]
async fn force_input_stops_running_and_enqueues_message() {
    let store = Arc::new(SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
        .create_session(&session_record(Some("P-force-chan")))
        .await
        .unwrap();
    link_pipeline(&store, "P-force-chan", "T-force-chan").await;
    store
        .record_run_start(
            "P-force-chan",
            "default",
            "run-force-chan",
            "cfg",
            "2026-11-14T00:00:00Z",
        )
        .await
        .unwrap();
    seed_default_user(&*store, "u1").await;
    // 占用链：强制发送的投递段会走「链忙 → 持久化入队」，不真跑引擎。
    let _release = occupy_chain("P-force-chan", "u1");

    let dispatcher = dispatcher_with(store.clone());
    dispatcher
        .dispatch_force_input(agentos_core::types::DispatchRequest {
            thread_id: ("T-force-chan").to_string(),
            user_id: ("u1").to_string(),
            pipeline_id: ("P-force-chan").to_string(),
            content: ("立刻改成新方案").to_string(),
            client_message_id: ("").to_string(),
            source: PendingInputSource::Task,
            execution_context: None,
            state_overlay: None,
            pipeline_config_id: None,
        })
        .await
        .unwrap();

    // ① 停当前 run：引擎层停止信号已落地
    assert_eq!(
        store.get_run("run-force-chan").await.unwrap().status,
        RunStatus::Suspended,
        "强制发送必须先停当前 running run"
    );

    // ② 消息真的投递进该管道队列（可被消费循环取走）
    let drained = store
        .pop_pending_input("default", "P-force-chan")
        .await
        .unwrap();
    let rec = drained.expect("强制发送的消息必须进队列（可被消费），不得静默丢弃");
    assert_eq!(
        rec.request.content, "立刻改成新方案",
        "投递内容必须原样保留"
    );
    assert_eq!(
        rec.request.pipeline_id, "P-force-chan",
        "投递坐标必须是目标管道"
    );
    assert_eq!(
        rec.request.source,
        PendingInputSource::Task,
        "任务侧送消息的来源标注必须透传（前端队列条按来源展示）"
    );
}

/// run 已在停止中（Suspended）：幂等，不落待取消标记——该标记会被下一条 run
/// 起手消费并零执行取消（= 新的强制发送消息被秒杀），此时消息仍须留在队列可跑。
#[tokio::test]
async fn force_input_idempotent_when_run_already_stopped() {
    let store = Arc::new(SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
        .create_session(&session_record(Some("P-force-idem")))
        .await
        .unwrap();
    link_pipeline(&store, "P-force-idem", "T-force-idem").await;
    store
        .record_run_start(
            "P-force-idem",
            "default",
            "run-force-idem",
            "cfg",
            "2026-11-14T00:00:00Z",
        )
        .await
        .unwrap();
    seed_default_user(&*store, "u1").await;
    // 第一次停止：Running → Suspended
    let dispatcher = dispatcher_with(store.clone());
    dispatcher
        .dispatch_stop("T-force-idem", "P-force-idem")
        .await
        .unwrap();
    let _release = occupy_chain("P-force-idem", "u1");

    dispatcher
        .dispatch_force_input(agentos_core::types::DispatchRequest {
            thread_id: ("T-force-idem").to_string(),
            user_id: ("u1").to_string(),
            pipeline_id: ("P-force-idem").to_string(),
            content: ("继续按新指令跑").to_string(),
            client_message_id: ("").to_string(),
            source: PendingInputSource::Task,
            execution_context: None,
            state_overlay: None,
            pipeline_config_id: None,
        })
        .await
        .unwrap();

    assert!(
        !has_pending_cancel(&store, "P-force-idem").await,
        "run 已 Suspended 时强发不得落 pending_cancel：该标记由下一条 run 起手消费，\
         会把本条消息零执行取消"
    );
    let rec = store
        .pop_pending_input("default", "P-force-idem")
        .await
        .unwrap()
        .expect("已停止管道的强制发送消息同样必须进队列");
    assert_eq!(rec.request.content, "继续按新指令跑");
}

/// 强制通道不得污染默认派发：普通 dispatch_user_input 不触发停止段
/// （force 缺省的调用方行为逐字节不变）。
#[tokio::test]
async fn plain_dispatch_does_not_stop_current_run() {
    let store = Arc::new(SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
        .create_session(&session_record(Some("P-plain-chan")))
        .await
        .unwrap();
    link_pipeline(&store, "P-plain-chan", "T-plain-chan").await;
    store
        .record_run_start(
            "P-plain-chan",
            "default",
            "run-plain-chan",
            "cfg",
            "2026-11-14T00:00:00Z",
        )
        .await
        .unwrap();
    seed_default_user(&*store, "u1").await;
    let _release = occupy_chain("P-plain-chan", "u1");

    let dispatcher = dispatcher_with(store.clone());
    dispatcher
        .dispatch_user_input(agentos_core::types::DispatchRequest {
            thread_id: ("T-plain-chan").to_string(),
            user_id: ("u1").to_string(),
            pipeline_id: ("P-plain-chan").to_string(),
            content: ("排队即可").to_string(),
            client_message_id: ("").to_string(),
            source: PendingInputSource::Task,
            execution_context: None,
            state_overlay: None,
            pipeline_config_id: None,
        })
        .await
        .unwrap();

    assert_eq!(
        store.get_run("run-plain-chan").await.unwrap().status,
        RunStatus::Running,
        "普通派发必须保持排队语义：不得停当前 run"
    );
    let rec = store
        .pop_pending_input("default", "P-plain-chan")
        .await
        .unwrap()
        .expect("普通派发照常入队");
    assert_eq!(rec.request.content, "排队即可");
}
