//! interaction_response 接入引擎的 TDD 测试。
//!
//! 验证 EngineDispatcher.dispatch_interaction_response 能根据前端回传的
//! request_id 找到被挂起的 run 并 resume——这是 human_interaction/approval
//! 审批闭环在内核侧的最后一环。
//!
//! 架构背景（ADR 2026-09-18：runs/branches 表退役，state 唯一真值）：
//! - approval 插件通过 pipeline-executor.suspend 挂起 run（state.run_status='suspended'），
//!   内核返回 resume 凭据
//! - suspend 时把凭据落 state 标量键 `suspend_request_id`（find_suspended_run_by_request_id
//!   的反查键；resume 时清空——陈旧凭据会让反查误判仍在等审批）
//! - 前端用户操作后回传 interaction_response(request_id)
//! - dispatch_interaction_response 根据 request_id 反查挂起投影，写回 run_status='running'
//!
//! 本测试直接操作 SqliteStore 模拟 capability 的挂起/恢复簿记链路
//! （capability_router.rs pipeline-executor.suspend/resume 同构）。
//!
//! @feature: FP-0.2.五 审批闭环补全 | @vision: V2 全能闭环 | @ci: rust-test

use std::sync::Arc;

use agentos_core::traits::StorageBackend;
use agentos_core::types::RunStatus;
use agentos_engine::SqliteStore;
use serde_json::{json, Value};

// ═══════════════════════════════════════════════════════════════════
// 测试辅助
// ═══════════════════════════════════════════════════════════════════

fn make_store() -> Arc<SqliteStore> {
    Arc::new(SqliteStore::open_memory().unwrap())
}

/// 模拟 pipeline-executor.suspend：写运行簿记并置为 Suspended（state 运行键）。
fn suspend_run(store: &SqliteStore, pipeline_id: &str, run_id: &str) {
    store
        .record_run_start(pipeline_id, "default", run_id, "hash")
        .unwrap();
    store
        .set_run_status_projection(pipeline_id, "default", RunStatus::Suspended)
        .unwrap();
}

/// 模拟 pipeline-executor.resume：状态回写 Running + 清挂起凭据。
fn resume_run(store: &SqliteStore, pipeline_id: &str) {
    store
        .set_run_status_projection(pipeline_id, "default", RunStatus::Running)
        .unwrap();
    let mut fields = serde_json::Map::new();
    fields.insert("suspend_request_id".to_string(), Value::Null);
    store
        .upsert_state_fields(pipeline_id, "default", &fields)
        .unwrap();
}

// ═══════════════════════════════════════════════════════════════════
// 测试 1：suspend 后挂起凭据（suspend_request_id）落 state 并可反查
// 模拟 approval 插件的完整闭环：suspend → 凭据写入 → 按 request_id resume
// ═══════════════════════════════════════════════════════════════════

#[tokio::test]
async fn suspend_and_resume_by_request_id() {
    let store = make_store();
    let run_id = "run-approval-001";
    let pipeline_id = "pipe-approval-001";

    // 1. 挂起 run（模拟 approval 插件调 pipeline-executor.suspend）
    suspend_run(&store, pipeline_id, run_id);

    // 2. 把挂起凭据落 state 键 suspend_request_id
    //    （approval 插件 suspend 后、返回前端前写入）
    let request_id = "req-approval-001";
    let mut fields = serde_json::Map::new();
    fields.insert("suspend_request_id".to_string(), json!(request_id));
    store
        .upsert_state_fields(pipeline_id, "default", &fields)
        .unwrap();

    // 3. 根据 request_id 查找并 resume（模拟 dispatch_interaction_response 的核心逻辑）
    let found = store.find_suspended_run_by_request_id(request_id).unwrap();
    assert!(found.is_some(), "必须能按 request_id 找到 suspended run");

    let run_record = found.unwrap();
    assert_eq!(run_record.run_id, run_id);
    assert_eq!(run_record.status, RunStatus::Suspended);
    // 挂起凭据经 metadata 兼容面可见（suspend_request_id 映射回
    // pending_interaction_request_id）
    let meta = run_record.metadata.clone().unwrap();
    assert_eq!(
        meta.get("pending_interaction_request_id")
            .and_then(|v| v.as_str()),
        Some(request_id),
        "metadata 必须携带挂起凭据"
    );

    resume_run(&store, pipeline_id);

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
// 测试 3：不在等审批的 run 不被凭据匹配
// （resume 已清挂起凭据——陈旧 suspend_request_id 会让反查误判仍在等审批）
// ═══════════════════════════════════════════════════════════════════

#[tokio::test]
async fn running_run_not_matched_by_request_id() {
    let store = make_store();
    let run_id = "run-running-001";
    let pipeline_id = "pipe-running-001";

    // run 挂起过（凭据已落），随后 resume（Running + 凭据清空）
    suspend_run(&store, pipeline_id, run_id);
    let mut fields = serde_json::Map::new();
    fields.insert("suspend_request_id".to_string(), json!("req-running"));
    store
        .upsert_state_fields(pipeline_id, "default", &fields)
        .unwrap();
    resume_run(&store, pipeline_id);

    // 凭据已清的 run 不应被 find_suspended_run_by_request_id 匹配
    let found = store
        .find_suspended_run_by_request_id("req-running")
        .unwrap();
    assert!(
        found.is_none(),
        "已 resume（凭据清空）的 run 不应被 interaction_request 匹配"
    );
}
