// @feature: FP-0.2.WS停止清除 stop 误写待取消标记（Suspended+链在飞） | @ci: rust-test
//!
//! 2026-10-29 用户实证：停止后「立即发送」无效果/消息消失。
//! 根因：`suspend_pipeline_execution` 只找 `Running` 的 run；run 已被 stop 置
//! `Suspended` 但仍在长步骤里没退出时，它落到 `has_chain` 分支写
//! `pending_cancel=true`——该标记由**下一条 run 起手消费**，把用户点「立即发送」
//! 的那条消息零执行取消。
//!
//! 本组测试锁定四条误写路径 + 队列 cmid 幂等：
//! 1. 连续两次 stop（第二次时 run 已 Suspended、链在飞）不得落标；
//! 2. run 已 Suspended + 链在飞，stop 不得落标；
//! 3. stop 后 force（run 已 Suspended、链在飞）不得落标，目标必须可执行；
//! 4. run 已终态但链仍在 drain，stop 不得落标；
//! 5. 同一 client_message_id 重复入队只能留一条。

use super::*;

use agentos_core::traits::StorageBackend;
use agentos_core::types::{PendingInputRecord, PendingInputSource};
use agentos_engine::SqliteStore;
use serde_json::json;

/// 占链模拟「run 在飞/派发在途」：释放句柄 drop（或 send）前 `has_chain` 恒 true。
/// 管道 id 用测试专名，避免污染并行测试共享的进程级链注册表。
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

async fn stop_pipeline(store: Arc<dyn StorageBackend>, pipeline_id: &str) -> Result<(), String> {
    dispatcher_with(store)
        .dispatch_stop("T1", pipeline_id)
        .await
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

fn pending_record(id: &str, pipeline_id: &str, cmid: &str, at: &str) -> PendingInputRecord {
    PendingInputRecord {
        id: id.to_string(),
        pipeline_id: pipeline_id.to_string(),
        tenant_id: "default".to_string(),
        user_id: "u1".to_string(),
        content: format!("内容-{id}"),
        thread: "T1".to_string(),
        source: PendingInputSource::User,
        agent_id: String::new(),
        route_id: pipeline_id.to_string(),
        thinking_strength: String::new(),
        client_message_id: cmid.to_string(),
        execution_context: None,
        state_overlay: None,
        pipeline_config_id: None,
        created_at: at.to_string(),
    }
}

#[tokio::test]
async fn dispatch_stop_twice_does_not_poison_next_run() {
    let store = Arc::new(SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
        .create_session(&session_record(Some("P-twice")))
        .await
        .unwrap();
    store
        .record_run_start(
            "P-twice",
            "default",
            "run-twice",
            "cfg",
            "2026-10-29T00:00:00Z",
        )
        .await
        .unwrap();
    let _release = occupy_chain("P-twice", "u1");

    // 第一次：Running → Suspended（正常路径）
    stop_pipeline(store.clone(), "P-twice").await.unwrap();
    assert_eq!(
        store.get_run("run-twice").await.unwrap().status,
        RunStatus::Suspended,
        "第一次 stop 应置 suspended"
    );

    // 第二次：run 已 Suspended、链仍在飞——不得落 pending_cancel
    stop_pipeline(store.clone(), "P-twice").await.unwrap();

    assert!(
        !has_pending_cancel(&store, "P-twice").await,
        "第二次 stop 不得写 pending_cancel：该标记会由下一条 run 起手消费并零执行取消"
    );
}

#[tokio::test]
async fn dispatch_stop_when_run_already_suspended_does_not_set_pending_cancel() {
    let store = Arc::new(SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
        .create_session(&session_record(Some("P-susp")))
        .await
        .unwrap();
    store
        .record_run_start(
            "P-susp",
            "default",
            "run-susp",
            "cfg",
            "2026-10-29T00:00:00Z",
        )
        .await
        .unwrap();
    // run 已被停止、但长步骤未退出：状态 Suspended，链仍占用。
    store
        .upsert_state_fields(
            "P-susp",
            "default",
            &json!({ "run_status": "suspended" })
                .as_object()
                .unwrap()
                .clone(),
        )
        .await
        .unwrap();
    let _release = occupy_chain("P-susp", "u1");

    stop_pipeline(store.clone(), "P-susp").await.unwrap();

    assert!(
        !has_pending_cancel(&store, "P-susp").await,
        "run 已在停止中（Suspended）时 stop 必须幂等：不得落标误杀下一条 run"
    );
}

#[tokio::test]
async fn dispatch_force_after_stop_does_not_cancel_target() {
    let store = Arc::new(SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
        .create_session(&session_record(Some("P-force-stop")))
        .await
        .unwrap();
    seed_default_user(&*store, "u1").await;
    store
        .record_run_start(
            "P-force-stop",
            "default",
            "run-force-stop",
            "cfg",
            "2026-10-29T00:00:00Z",
        )
        .await
        .unwrap();
    store
        .enqueue_pending_input(
            "default",
            "P-force-stop",
            &pending_record(
                "p_target",
                "P-force-stop",
                "cmid-target",
                "2026-10-29T00:00:01Z",
            ),
        )
        .await
        .unwrap();
    let _release = occupy_chain("P-force-stop", "u1");

    // 先停止：run 置 Suspended，链仍在长步骤里；队列中的 user 条目按设计
    // 退回输入框（dispatch_stop 的 returned 语义）。
    stop_pipeline(store.clone(), "P-force-stop").await.unwrap();

    // 用户重发（新 id/新 cmid）——这是「立即发送」的目标
    store
        .enqueue_pending_input(
            "default",
            "P-force-stop",
            &pending_record(
                "p_resend",
                "P-force-stop",
                "cmid-resend",
                "2026-10-29T00:00:03Z",
            ),
        )
        .await
        .unwrap();

    // 再点「立即发送」：目标置队首 + 停当前回复（已停）——不得再落下一条的取消标
    let dispatcher = dispatcher_with(store.clone());
    dispatcher
        .dispatch_force_pending("u1", "T1", "P-force-stop", "p_resend")
        .await
        .unwrap();

    assert!(
        !has_pending_cancel(&store, "P-force-stop").await,
        "stop 后 force 不得写 pending_cancel：目标消息会被起手消费零执行取消"
    );
    let rows = store
        .list_pending_inputs("default", "P-force-stop")
        .await
        .unwrap();
    assert!(
        rows.iter().any(|r| r.id == "p_resend"),
        "目标条目必须留在队列中可执行"
    );
}

#[tokio::test]
async fn dispatch_stop_when_run_terminal_but_chain_draining_does_not_set_pending_cancel() {
    let store = Arc::new(SqliteStore::open_memory().unwrap()) as Arc<dyn StorageBackend>;
    store
        .create_session(&session_record(Some("P-drain")))
        .await
        .unwrap();
    store
        .record_run_start(
            "P-drain",
            "default",
            "run-drain",
            "cfg",
            "2026-10-29T00:00:00Z",
        )
        .await
        .unwrap();
    // run 已收尾（completed），但链仍在 drain/收尾窗口中。
    store
        .upsert_state_fields(
            "P-drain",
            "default",
            &json!({ "run_status": "completed" })
                .as_object()
                .unwrap()
                .clone(),
        )
        .await
        .unwrap();
    let _release = occupy_chain("P-drain", "u1");

    stop_pipeline(store.clone(), "P-drain").await.unwrap();

    assert!(
        !has_pending_cancel(&store, "P-drain").await,
        "run 终态但链在 drain 时 stop 不得落标：会取消 drain 即将消费的队列条目"
    );
}

#[tokio::test]
async fn enqueue_same_client_message_id_is_idempotent() {
    let store: Arc<dyn StorageBackend> = Arc::new(SqliteStore::open_memory().unwrap());

    // 同一条消息（同 cmid）因网络重试/前端重发再次入队：id 是新生成的。
    store
        .enqueue_pending_input(
            "default",
            "P-idem",
            &pending_record("p_first", "P-idem", "cmid-same", "2026-10-29T00:00:01Z"),
        )
        .await
        .unwrap();
    store
        .enqueue_pending_input(
            "default",
            "P-idem",
            &pending_record("p_retry", "P-idem", "cmid-same", "2026-10-29T00:00:02Z"),
        )
        .await
        .unwrap();

    let rows = store
        .list_pending_inputs("default", "P-idem")
        .await
        .unwrap();
    assert_eq!(
        rows.len(),
        1,
        "同一 client_message_id 重复入队必须幂等（当前 INSERT OR IGNORE 只按主键 id 去重，会插两条并执行两次）"
    );
}
