//! interaction_response 接入引擎的 TDD 测试。
//!
//! 验证 EngineDispatcher.dispatch_interaction_response 能根据前端回传的
//! request_id 找到被挂起的 run 并 resume——这是 human_interaction/approval
//! 审批闭环在内核侧的最后一环。
//!
//! 架构背景（0.2 收尾：旧引擎 AdrEngineImpl 已清理，suspend/resume 改走 store）：
//! - approval 插件通过 pipeline-executor.suspend 挂起 run，内核返回 SuspendHandle
//! - suspend 时把 request_id → SuspendHandle 映射存入 run metadata
//! - 前端用户操作后回传 interaction_response(request_id)
//! - dispatch_interaction_response 根据 request_id 查映射，调 update_run_status(Running)
//!
//! 本测试直接操作 SqliteStore 模拟 capability 的挂起/恢复簿记链路
//! （capability_router.rs pipeline-executor.suspend/resume 同构）。
//!
//! @feature: FP-0.2.五 审批闭环补全 | @vision: V2 全能闭环 | @ci: rust-test

use std::sync::Arc;

use agentos_core::traits::StorageBackend;
use agentos_core::types::RunStatus;
use agentos_engine::SqliteStore;
use serde_json::json;

// ═══════════════════════════════════════════════════════════════════
// 测试辅助
// ═══════════════════════════════════════════════════════════════════

fn make_store() -> Arc<SqliteStore> {
    Arc::new(SqliteStore::open_memory().unwrap())
}

/// 模拟 pipeline-executor.suspend：建 run 并置为 Suspended（状态簿记）。
async fn suspend_run(store: &SqliteStore, run_id: &str) {
    store.create_run(run_id, "hash", "default").unwrap();
    store
        .update_run_status(run_id, RunStatus::Suspended, Some("main"), Some(0))
        .await
        .unwrap();
}

// ═══════════════════════════════════════════════════════════════════
// 测试 1：suspend 后 run 的 metadata 存入 request_id 映射
// 模拟 approval 插件的完整闭环：suspend → metadata 写入 → 按 request_id resume
// ═══════════════════════════════════════════════════════════════════

#[tokio::test]
async fn suspend_and_resume_by_request_id() {
    let store = make_store();
    let run_id = "run-approval-001";

    // 1. 挂起 run（模拟 approval 插件调 pipeline-executor.suspend）
    suspend_run(&store, run_id).await;

    // 2. 把 request_id → SuspendHandle 映射存入 run metadata
    //    （approval 插件 suspend 后、返回前端前写入）
    let request_id = "req-approval-001";
    store
        .set_run_metadata(
            run_id,
            &json!({
                "pending_interaction_request_id": request_id,
                "suspend_branch_id": "main",
                "suspend_seq": 0,
            }),
        )
        .unwrap();

    // 3. 根据 request_id 查找并 resume（模拟 dispatch_interaction_response 的核心逻辑）
    let found = store.find_suspended_run_by_request_id(request_id).unwrap();
    assert!(found.is_some(), "必须能按 request_id 找到 suspended run");

    let run_record = found.unwrap();
    assert_eq!(run_record.run_id, run_id);
    assert_eq!(run_record.status, RunStatus::Suspended);

    // 从 metadata 还原 SuspendHandle 并 resume（update_run_status 状态簿记）
    let meta = run_record.metadata.unwrap();
    let branch_id = meta
        .get("suspend_branch_id")
        .and_then(|v| v.as_str())
        .unwrap_or("main")
        .to_string();
    let _seq = meta
        .get("suspend_seq")
        .and_then(|v| v.as_u64())
        .unwrap_or(0) as u32;
    store
        .update_run_status(
            &run_record.run_id,
            RunStatus::Running,
            Some(&branch_id),
            Some(_seq),
        )
        .await
        .unwrap();

    // 4. 确认 run 回到 Running
    let run_after = store.get_run(run_id).await.unwrap();
    assert_eq!(run_after.status, RunStatus::Running);
}

// ═══════════════════════════════════════════════════════════════════
// 测试 2：未知的 request_id 返回 None
// ═══════════════════════════════════════════════════════════════════

#[tokio::test]
async fn unknown_request_id_finds_nothing() {
    let store = make_store();

    let found = store
        .find_suspended_run_by_request_id("nonexistent-req")
        .unwrap();
    assert!(found.is_none(), "未知 request_id 必须返回 None");
}

// ═══════════════════════════════════════════════════════════════════
// 测试 3：非 Suspended 的 run 不被匹配
// ═══════════════════════════════════════════════════════════════════

#[tokio::test]
async fn running_run_not_matched_by_request_id() {
    let store = make_store();
    let run_id = "run-running-001";

    // run 处于 Running（不 suspend）
    store.create_run(run_id, "hash", "default").unwrap();
    store
        .set_run_metadata(
            run_id,
            &json!({"pending_interaction_request_id": "req-running"}),
        )
        .unwrap();

    // Running 状态的 run 不应被 find_suspended_run_by_request_id 匹配
    let found = store
        .find_suspended_run_by_request_id("req-running")
        .unwrap();
    assert!(
        found.is_none(),
        "Running 状态的 run 不应被 interaction_request 匹配"
    );
}
