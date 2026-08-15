// @feature: FP-0.2.〇 管道引擎（G8 优雅重启排空） | @ci: rust-test
//! G8 优雅重启端点测试（restart-as-unload）。
//!
//! 排空语义：在途 running runs → suspended（可 resume 续跑）。
//! 自退出经 `AGENTOS_DISABLE_SELF_EXIT=1` 逃生门禁用（测试进程不能真 exit 75）。

use std::sync::Arc;

use agentos_api::routes::AppState;
use agentos_core::types::RunStatus;
use agentos_engine::SqliteStore;

/// 排空：running runs 全部 → suspended；已结束状态不受影响。
#[tokio::test]
async fn g8_restart_suspends_running_runs() {
    std::env::set_var("AGENTOS_DISABLE_SELF_EXIT", "1");
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    store.create_run("r1", "hash", "default").unwrap();
    store.create_run("r2", "hash", "default").unwrap();
    // r2 先完成——不应被排空动到。
    use agentos_core::traits::StorageBackend;
    store
        .update_run_status("r2", RunStatus::Completed, None, None)
        .await
        .unwrap();

    let mut state = AppState::new();
    state.db = Some(store.clone());
    let resp = agentos_api::routes::system_restart_handler(axum::extract::State(state)).await;

    assert_eq!(resp.0["success"], true, "restart 响应应成功: {}", resp.0);
    assert_eq!(resp.0["suspended_runs"], 1, "只有 1 个 running run 被排空");
    assert_eq!(resp.0["exit_code"], 75);

    let r1 = StorageBackend::get_run(&*store, "r1").await.unwrap();
    assert_eq!(r1.status, RunStatus::Suspended, "r1 应为 suspended");
    let r2 = StorageBackend::get_run(&*store, "r2").await.unwrap();
    assert_eq!(
        r2.status,
        RunStatus::Completed,
        "completed run 不受排空影响"
    );
}

/// 无 db 接线时 restart 仍可用（suspended_runs=0，诚实降级）。
#[tokio::test]
async fn g8_restart_without_db_degrades() {
    std::env::set_var("AGENTOS_DISABLE_SELF_EXIT", "1");
    let state = AppState::new();
    let resp = agentos_api::routes::system_restart_handler(axum::extract::State(state)).await;
    assert_eq!(resp.0["success"], true);
    assert_eq!(resp.0["suspended_runs"], 0);
}

/// A3：共享排空函数直测——逃生门（AGENTOS_DISABLE_SELF_EXIT=1）生效时只排空
/// 不退出：plugin_watcher 的 cdylib 自动重启与端点共用此路径，测试进程必须
/// 存活到断言结束（若逃生门失效，测试进程直接 exit 75 报错，此测试即红）。
#[tokio::test]
async fn drain_and_exit75_suspends_without_exit_under_escape_hatch() {
    std::env::set_var("AGENTOS_DISABLE_SELF_EXIT", "1");
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    store.create_run("d1", "hash", "default").unwrap();
    let suspended =
        agentos_api::routes::drain_and_exit75(Some(&store), "test: watcher cdylib change").await;
    assert_eq!(suspended, 1, "应排空 1 个 running run");
    use agentos_core::traits::StorageBackend;
    let run = StorageBackend::get_run(&*store, "d1").await.unwrap();
    assert_eq!(run.status, RunStatus::Suspended, "run 应被排空为 suspended");
    // 无 db → 0（诚实降级，不 panic）
    let none = agentos_api::routes::drain_and_exit75(None, "test").await;
    assert_eq!(none, 0);
}
