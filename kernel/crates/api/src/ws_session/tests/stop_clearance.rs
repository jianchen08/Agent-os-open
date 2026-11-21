// @feature: FP-0.2.WS停止清除 stop 残留标记跨链误杀收口 | @ci: rust-test
use super::*;

use agentos_core::traits::StorageBackend;
use agentos_core::types::{PendingInputRecord, PendingInputSource};
use agentos_engine::SqliteStore;
use serde_json::json;

// ── 派发链收尾清除：待取消标记寿命 = 所属派发链寿命 ─────────────────────
//
// 事故（2026-10-19 装机现场，docs/working/Stop残留标记误杀_台账.md）：
// stop_generation 落标（无 Running run + 派发链在途）后，若该链的 run 已过
// 起手消费点（如 run 在途但运行投影查询失配）或链未建行即终了，标记无人消费
// 跨链残留——下一次无关派发的 run 起手把它当本次的首 token 前取消，零执行
// 误杀（现场：残留 57 分钟后"再来试试"被秒杀且消息静默丢失）。
//
// 修复语义：链收尾（run_pipeline_round_scoped）test-and-clear 残留标记；
// 起手消费在前、收尾清除在后，同管道链串行无竞争；缺陷A 首 token 前取消
// 仍由起手消费先行生效，语义不变。

/// 链收尾必须消费掉上一轮派发残留的待取消标记：清除后再 consume 必为 false
/// （下一次派发的 run 起手不得被残留标记误杀）。
#[tokio::test]
async fn cleanup_consumes_residual_marker_left_by_previous_dispatch() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    store
        .upsert_state_fields(
            "P-residual",
            "default",
            &json!({ "pending_cancel": true })
                .as_object()
                .unwrap()
                .clone(),
        )
        .unwrap();
    let store_dyn: Arc<dyn StorageBackend> = store.clone();

    EngineDispatcher::clear_dispatch_scoped_cancel(Some(&store_dyn), "P-residual", "default").await;

    assert!(
        !store
            .consume_pending_cancel("P-residual", "default")
            .unwrap(),
        "残留标记必须被链收尾清除，不得留给下一次派发的起手消费"
    );
}

/// 对照组：无标记时收尾清除为无害空转（不得凭空造标记/报错）；
/// 无 store（降级路径，标记面本不存在）不炸。
#[tokio::test]
async fn cleanup_is_noop_without_marker_and_safe_without_store() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let store_dyn: Arc<dyn StorageBackend> = store.clone();
    EngineDispatcher::clear_dispatch_scoped_cancel(Some(&store_dyn), "P-clean", "default").await;
    assert!(
        !store.consume_pending_cancel("P-clean", "default").unwrap(),
        "无标记时清除必须是空转：不得引入可被消费的标记"
    );

    // 无 store（spawn_chain 降级路径）：清除面本不存在，静默跳过
    EngineDispatcher::clear_dispatch_scoped_cancel(None, "P-clean", "default").await;
}

/// 已被 run 起手正常消费的标记（取消生效场景）：收尾再清是幂等空转，
/// 不得把"已消费"翻回任何可观测状态（防拟合：修复不得破坏缺陷A 正常链路）。
#[tokio::test]
async fn cleanup_after_normal_consumption_stays_idempotent() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    store
        .upsert_state_fields(
            "P-consumed",
            "default",
            &json!({ "pending_cancel": true })
                .as_object()
                .unwrap()
                .clone(),
        )
        .unwrap();
    // 起手消费先行（缺陷A 生效路径）：test-and-clear 返回 true
    assert!(store
        .consume_pending_cancel("P-consumed", "default")
        .unwrap());
    let store_dyn: Arc<dyn StorageBackend> = store.clone();

    EngineDispatcher::clear_dispatch_scoped_cancel(Some(&store_dyn), "P-consumed", "default").await;

    assert!(
        !store
            .consume_pending_cancel("P-consumed", "default")
            .unwrap(),
        "收尾清除不得复活已消费的标记"
    );
}

// ── 停止清队来源分治（聊天消息流权威化设计真值 20261027 不变量 5）────────
// 事故（2026-10-27 装机实证）：regenerate 重跑以 System 来源入队，停止时被
// 整队清掉、前端 returned 只回填 user 来源 → 重跑静默蒸发。停止的「退回」
// 语义边界 = 用户输入；System 条目留队交链尾 drain 消费。

#[tokio::test]
async fn stop_generation_returns_only_user_source_keeps_system_in_queue() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let store_dyn: Arc<dyn StorageBackend> = store.clone();
    store
        .create_session(&session_record(Some("P-src")))
        .await
        .unwrap();

    let mk = |id: &str, source: PendingInputSource| PendingInputRecord {
        id: id.to_string(),
        tenant_id: "default".to_string(),
        created_at: "2026-10-27T00:00:00+00:00".to_string(),
        request: agentos_core::types::DispatchRequest {
            thread_id: "T-src".to_string(),
            user_id: "u1".to_string(),
            pipeline_id: "P-src".to_string(),
            content: format!("content-{id}"),
            client_message_id: format!("cmid-{id}"),
            source,
            execution_context: None,
            state_overlay: None,
            pipeline_config_id: None,
        },
    };
    store_dyn
        .enqueue_pending_input("default", &mk("p-user", PendingInputSource::User))
        .await
        .unwrap();
    store_dyn
        .enqueue_pending_input("default", &mk("p-sys", PendingInputSource::System))
        .await
        .unwrap();

    // 会话接线：捕获 returned 事件（无注册连接 = 用户租户解析走 seed）。
    let coordinator = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    coordinator.register_thread("T-src", "u1");
    coordinator.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let mut state = crate::routes::AppState::new();
    state.store = Some(store_dyn.clone());
    state.session = Some(coordinator.clone());
    seed_default_user(&*store_dyn, "u1").await;
    let dispatcher = EngineDispatcher::new(state);
    dispatcher.dispatch_stop("T-src", "P-src").await.unwrap();

    // User 条目出队；System 条目留队（drain 消费）。
    let remaining = store_dyn
        .list_pending_inputs("default", "P-src")
        .await
        .unwrap();
    let ids: Vec<&str> = remaining.iter().map(|r| r.id.as_str()).collect();
    assert_eq!(
        ids,
        vec!["p-sys"],
        "System 来源（regenerate 重跑）必须留队，不得被停止清掉"
    );

    // returned 事件只携带退回的 user 条目。
    let returned: Vec<String> = {
        let emitted = frames.lock().unwrap();
        emitted
            .iter()
            .filter(|f| f.contains("\"returned\""))
            .cloned()
            .collect()
    };
    assert_eq!(returned.len(), 1, "returned 事件必须发出");
    let payload: serde_json::Value = serde_json::from_str(&returned[0]).unwrap();
    let items = payload["data"]["items"].as_array().unwrap();
    assert_eq!(items.len(), 1, "只退回 user 来源条目");
    assert_eq!(items[0]["id"], "p-user");
}

// ── force_pending_input：强发单条，其余留队（2026-10-27 用户实测反例驱动）──
// 旧前端编排「DELETE→停止→重发」复用停止的队列退回语义，把其余排队条目一并
// 退回输入框（内核日志 04:26:09 returned=4 实证）。新原语：目标重排至队首 +
// 停当前回复（不退回）+ 链尾 drain 首个消费目标。

#[tokio::test]
async fn force_pending_reorders_target_first_and_keeps_rest_queued() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let store_dyn: Arc<dyn StorageBackend> = store.clone();
    store
        .create_session(&session_record(Some("P-force")))
        .await
        .unwrap();
    // 三条排队（created_at 交错 = FIFO 序 a→b→c）+ 一个 Running run（在飞回复）
    let mk = |id: &str, at: &str| PendingInputRecord {
        id: id.to_string(),
        tenant_id: "default".to_string(),
        created_at: at.to_string(),
        request: agentos_core::types::DispatchRequest {
            thread_id: "T-force".to_string(),
            user_id: "u1".to_string(),
            pipeline_id: "P-force".to_string(),
            content: format!("内容-{id}"),
            client_message_id: format!("cmid-{id}"),
            source: PendingInputSource::User,
            execution_context: None,
            state_overlay: None,
            pipeline_config_id: None,
        },
    };
    for (id, at) in [
        ("a", "2026-10-27T00:00:01Z"),
        ("b", "2026-10-27T00:00:02Z"),
        ("c", "2026-10-27T00:00:03Z"),
    ] {
        store_dyn
            .enqueue_pending_input("default", &mk(id, at))
            .await
            .unwrap();
    }
    store_dyn
        .record_run_start(
            "P-force",
            "default",
            "run-force",
            "cfg",
            "2026-10-27T00:00:00Z",
        )
        .await
        .unwrap();

    let coordinator = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    coordinator.register_thread("T-force", "u1");
    coordinator.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let mut state = crate::routes::AppState::new();
    state.store = Some(store_dyn.clone());
    state.session = Some(coordinator.clone());
    seed_default_user(&*store_dyn, "u1").await;
    let dispatcher = EngineDispatcher::new(state);
    dispatcher
        .dispatch_force_pending("u1", "T-force", "P-force", "b")
        .await
        .unwrap();

    // 目标置队首，其余原序留队（不退回输入框）
    let ids: Vec<String> = store_dyn
        .list_pending_inputs("default", "P-force")
        .await
        .unwrap()
        .into_iter()
        .map(|r| r.id)
        .collect();
    assert_eq!(ids, vec!["b", "a", "c"], "目标必须重排至队首，其余原序留队");

    // 在飞 run 被停（suspended 信号）
    let run = store_dyn.get_run("run-force").await.unwrap();
    assert_eq!(run.status, RunStatus::Suspended, "强发必须停当前回复");

    // 零退回：不得发 returned 事件（其余条目留队语义）
    let frames_snap = frames.lock().unwrap();
    assert!(
        !frames_snap.iter().any(|f| f.contains("\"returned\"")),
        "强发路径不得退回队列条目"
    );
    // 全量同步事件（updated）必须发出
    assert!(
        frames_snap.iter().any(|f| f.contains("\"updated\"")),
        "重排后必须发 pending_inputs_changed 全量同步"
    );
}

#[tokio::test]
async fn force_pending_rejects_missing_target() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let store_dyn: Arc<dyn StorageBackend> = store.clone();
    store
        .create_session(&session_record(Some("P-force2")))
        .await
        .unwrap();
    let coordinator = Arc::new(agentos_session::SessionCoordinator::new());
    coordinator.register_thread("T-force2", "u1");
    let mut state = crate::routes::AppState::new();
    state.store = Some(store_dyn.clone());
    state.session = Some(coordinator.clone());
    seed_default_user(&*store_dyn, "u1").await;
    let dispatcher = EngineDispatcher::new(state);
    let err = dispatcher
        .dispatch_force_pending("u1", "T-force2", "P-force2", "ghost")
        .await
        .unwrap_err();
    assert!(err.contains("不在队列中"), "err: {err}");
}
