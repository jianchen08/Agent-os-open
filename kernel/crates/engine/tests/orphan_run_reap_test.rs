// @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: rust-test
//! B2 修复验证：启动时清扫孤儿 run。
//!
//! 进程崩溃会留下 `status='running'` 的 run 永远卡住（persist_run_end 未执行）。
//! `reap_orphan_runs` 在内核启动时把所有 `running` run 标记为 `failed` + 补 `ended_at`，
//! 让历史/会话状态不悬空。已结束（completed/failed/suspended）的 run 不受影响。

use agentos_core::traits::StorageBackend;
use agentos_core::types::RunStatus;
use agentos_engine::SqliteStore;

#[tokio::test]
async fn reap_marks_orphan_running_as_failed_leaves_others() {
    let store = SqliteStore::open_memory().unwrap();
    // 两个 run，create_run 默认 status='running'
    store.create_run("r_orphan", "h", "default").unwrap();
    store.create_run("r_done", "h", "default").unwrap();
    // r_done 正常结束 → completed
    store
        .update_run_status("r_done", RunStatus::Completed, None, None)
        .await
        .unwrap();

    let reaped = store.reap_orphan_runs("default").expect("reap 应成功");
    assert_eq!(reaped, 1, "只应清扫 1 个 running 孤儿");

    let orphan = store.get_run("r_orphan").await.expect("get_run 应成功");
    assert_eq!(orphan.status, RunStatus::Failed, "孤儿 run 应被标记 failed");
    assert!(orphan.ended_at.is_some(), "应补 ended_at");

    let done = store.get_run("r_done").await.expect("get_run 应成功");
    assert_eq!(done.status, RunStatus::Completed, "已完成的 run 不应被动");
}

#[tokio::test]
async fn reap_is_idempotent() {
    // 重复清扫：第二次无 running run，返回 0。
    let store = SqliteStore::open_memory().unwrap();
    store.create_run("r1", "h", "default").unwrap();
    assert_eq!(store.reap_orphan_runs("default").unwrap(), 1);
    assert_eq!(store.reap_orphan_runs("default").unwrap(), 0);
    assert_eq!(store.reap_orphan_runs("default").unwrap(), 0);
}
