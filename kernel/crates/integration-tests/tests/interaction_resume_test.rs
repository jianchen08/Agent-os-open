//! interaction_response 接入引擎的 TDD 测试（RED 阶段）。
//!
//! 验证 EngineDispatcher.dispatch_interaction_response 能根据前端回传的
//! request_id 找到被挂起的 run 并 resume——这是 human_interaction/approval
//! 审批闭环在内核侧的最后一环。
//!
//! 架构背景：
//! - approval 插件通过 pipeline-executor.suspend 挂起 run，内核返回 SuspendHandle
//! - suspend 时把 request_id → SuspendHandle 映射存入 run metadata
//! - 前端用户操作后回传 interaction_response(request_id)
//! - dispatch_interaction_response 根据 request_id 查映射，调 engine.resume
//!
//! [来源: ws_session.rs dispatch_interaction_response 占位 P3+ 注释，
//!        三步 capability 设计第二步 handler 注册表架构]

use std::collections::HashMap;
use std::sync::Arc;

use async_trait::async_trait;
use agentos_core::traits::{AdrEngine, HookContext, LifecycleHook, PluginInvoker, StorageBackend};
use agentos_core::types::{
    PluginContext, PluginError, PluginResult, RunStatus, ToolExecutionResult, WakeEvent,
};
use agentos_engine::{AdrEngineImpl, SqliteStore};
use serde_json::json;

// ═══════════════════════════════════════════════════════════════════
// 测试辅助
// ═══════════════════════════════════════════════════════════════════

struct MockInvoker;

#[async_trait]
impl PluginInvoker for MockInvoker {
    async fn invoke_pipeline_plugin(
        &self,
        _plugin_id: &str,
        _ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError> {
        Ok(PluginResult {
            state_updates: HashMap::new(),
            route_signal: None,
            skip_remaining: false,
            error: None,
        })
    }

    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        Ok(ToolExecutionResult::success(json!({"ok": true})))
    }

    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: LifecycleHook,
        _ctx: &HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
}

fn make_engine() -> (AdrEngineImpl, Arc<SqliteStore>) {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let engine = AdrEngineImpl::new(store.clone(), Arc::new(MockInvoker), "default");
    (engine, store)
}

// ═══════════════════════════════════════════════════════════════════
// RED 测试 1：suspend 后 run 的 metadata 存入 request_id 映射
// 模拟 approval 插件的完整闭环：suspend → metadata 写入 → 按 request_id resume
// ═══════════════════════════════════════════════════════════════════

#[tokio::test]
async fn suspend_and_resume_by_request_id() {
    let (engine, store) = make_engine();

    // 1. 启动 run 并挂起（模拟 approval 插件调 pipeline-executor.suspend）
    let run_id = engine.start_run(&json!({})).await.unwrap();
    let handle = engine.suspend(&run_id).await.unwrap();
    assert_eq!(handle.run_id, run_id);

    // 2. 把 request_id → SuspendHandle 映射存入 run metadata
    //    （approval 插件 suspend 后、返回前端前写入）
    let request_id = "req-approval-001";
    store
        .set_run_metadata(
            &run_id,
            &json!({
                "pending_interaction_request_id": request_id,
                "suspend_branch_id": handle.branch_id,
                "suspend_seq": handle.seq,
            }),
        )
        .unwrap();

    // 3. 根据 request_id 查找并 resume（模拟 dispatch_interaction_response 的核心逻辑）
    let found = store
        .find_suspended_run_by_request_id(request_id)
        .unwrap();
    assert!(found.is_some(), "必须能按 request_id 找到 suspended run");

    let run_record = found.unwrap();
    assert_eq!(run_record.run_id, run_id);

    // 从 metadata 还原 SuspendHandle 并 resume
    let meta = run_record.metadata.unwrap();
    let restored_handle = agentos_core::types::SuspendHandle {
        run_id: run_record.run_id.clone(),
        branch_id: meta
            .get("suspend_branch_id")
            .and_then(|v| v.as_str())
            .unwrap_or("main")
            .to_string(),
        seq: meta
            .get("suspend_seq")
            .and_then(|v| v.as_u64())
            .unwrap_or(0) as u32,
    };
    engine.resume(&restored_handle, WakeEvent::Manual).await.unwrap();

    // 4. 确认 run 回到 Running
    let run_after = store.get_run(&run_id).await.unwrap();
    assert_eq!(run_after.status, RunStatus::Running);
}

// ═══════════════════════════════════════════════════════════════════
// RED 测试 2：未知的 request_id 返回 None
// ═══════════════════════════════════════════════════════════════════

#[tokio::test]
async fn unknown_request_id_finds_nothing() {
    let (_engine, store) = make_engine();

    let found = store
        .find_suspended_run_by_request_id("nonexistent-req")
        .unwrap();
    assert!(found.is_none(), "未知 request_id 必须返回 None");
}

// ═══════════════════════════════════════════════════════════════════
// RED 测试 3：非 Suspended 的 run 不被匹配
// ═══════════════════════════════════════════════════════════════════

#[tokio::test]
async fn running_run_not_matched_by_request_id() {
    let (engine, store) = make_engine();

    // run 处于 Running（不 suspend）
    let run_id = engine.start_run(&json!({})).await.unwrap();
    store
        .set_run_metadata(
            &run_id,
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
