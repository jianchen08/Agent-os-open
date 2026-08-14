// @feature: FP-0.2.spill_guard 管道结束清理 | @vision: V3 可嵌入 | @ci: rust-test
//! spill_guard 配套：管道 run 结束时向声明 on_pipeline_end 的插件分发钩子。
//!
//! 验证（TDD 规格）：
//! 1. test_run_dispatches_on_pipeline_end —— 注册插件在 run 结束后收到
//!    OnPipelineEnd，HookContext 携带 pipeline_id / run_id 标签
//! 2. test_run_without_hook_plugins_skips_dispatch —— 未注册则不分发
//! 3. test_dispatch_is_best_effort —— 分发失败（Err）不影响 run 返回值

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use agentos_core::traits::{PluginInvoker, StorageBackend};
use agentos_core::types::PluginContext;
use agentos_core::types::{PluginError, PluginResult, ToolExecutionResult};
use agentos_engine::{PipelineExecutor, SqliteStore};
use async_trait::async_trait;
use serde_json::json;

/// 记录生命周期钩子分发的 MockInvoker。
struct HookRecordingInvoker {
    hooks: Mutex<Vec<(String, String, Option<String>)>>, // (plugin_id, hook_name, pipeline_id 标签)
    fail_for: Mutex<Vec<String>>,                        // 对这些 plugin_id 返回 Err
}

impl HookRecordingInvoker {
    fn new() -> Self {
        Self {
            hooks: Mutex::new(Vec::new()),
            fail_for: Mutex::new(Vec::new()),
        }
    }

    fn fail_on(&self, plugin_id: &str) {
        self.fail_for.lock().unwrap().push(plugin_id.to_string());
    }

    fn hook_calls(&self, plugin_id: &str) -> Vec<String> {
        self.hooks
            .lock()
            .unwrap()
            .iter()
            .filter(|(p, _, _)| p == plugin_id)
            .map(|(_, h, _)| h.clone())
            .collect()
    }

    fn last_pipeline_id(&self) -> Option<String> {
        self.hooks
            .lock()
            .unwrap()
            .last()
            .map(|(_, _, pid)| pid.clone())
            .flatten()
    }
}

#[async_trait]
impl PluginInvoker for HookRecordingInvoker {
    async fn invoke_pipeline_plugin(
        &self,
        _plugin_id: &str,
        _ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError> {
        Ok(PluginResult::default())
    }

    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        Ok(ToolExecutionResult::success(serde_json::Value::Null))
    }

    async fn send_lifecycle_hook(
        &self,
        plugin_id: &str,
        hook: agentos_core::traits::LifecycleHook,
        context: &agentos_core::traits::HookContext,
    ) -> Result<(), PluginError> {
        let hook_name = match hook {
            agentos_core::traits::LifecycleHook::OnLoad => "on_load",
            agentos_core::traits::LifecycleHook::OnUnload => "on_unload",
            agentos_core::traits::LifecycleHook::OnPipelineStart => "on_pipeline_start",
            agentos_core::traits::LifecycleHook::OnPipelineEnd => "on_pipeline_end",
            agentos_core::traits::LifecycleHook::OnError => "on_error",
        };
        let pid = context.get("pipeline_id").and_then(|v| v.as_str()).map(String::from);
        self.hooks
            .lock()
            .unwrap()
            .push((plugin_id.to_string(), hook_name.to_string(), pid));
        if self.fail_for.lock().unwrap().iter().any(|p| p == plugin_id) {
            return Err(PluginError {
                message: "hook dispatch failed (test)".into(),
                code: None,
                source: None,
            });
        }
        Ok(())
    }
}

/// 最小管道配置：单个 step，含一个无产出插件，无循环。
fn minimal_config() -> agentos_core::types::PipelineConfig {
    agentos_core::types::PipelineConfig {
        name: "end_hook_test".into(),
        loop_config: agentos_core::types::LoopConfig::default(),
        steps: vec![agentos_core::types::PipelineStep {
            id: "only".into(),
            steps: vec!["pipeline_dummy".into()],
            context: HashMap::new(),
            routes: vec![],
            loop_config: None,
        }],
        checkpoint: agentos_core::types::CheckpointConfig::default(),
    }
}

fn make_executor(
    invoker: Arc<HookRecordingInvoker>,
    hooks: &[&str],
    store: Arc<SqliteStore>,
) -> PipelineExecutor {
    let store_dyn: Arc<dyn StorageBackend> = store;
    PipelineExecutor::new(
        invoker as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        agentos_core::types::TenantContext::new("tenant_test", "session_test"),
        vec!["pipeline_dummy".to_string()],
        store_dyn,
        "run_end_hook",
        "main",
    )
    .with_pipeline_end_hook_plugins(hooks.iter().map(|s| s.to_string()))
}

/// run 结束后注册插件收到 OnPipelineEnd（带 pipeline_id 标签）。
#[tokio::test]
async fn test_run_dispatches_on_pipeline_end() {
    let tmp = tempfile::tempdir().unwrap();
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let invoker = Arc::new(HookRecordingInvoker::new());
    let executor = make_executor(invoker.clone(), &["spill_retrieve_tool"], store);
    let state = json!({"pipeline_id": "pipe-xyz", "message": "hi"});
    let final_state = executor
        .run(&minimal_config(), &Default::default(), state)
        .await
        .expect("run ok");
    assert!(final_state.get("pipeline_id").is_some());
    let calls = invoker.hook_calls("spill_retrieve_tool");
    assert_eq!(
        calls,
        vec!["on_pipeline_end".to_string()],
        "run 结束后应恰分发一次 OnPipelineEnd"
    );
    assert_eq!(invoker.last_pipeline_id().as_deref(), Some("pipe-xyz"));
}

/// 未注册 hook 插件时不分发（零开销）。
#[tokio::test]
async fn test_run_without_hook_plugins_skips_dispatch() {
    let tmp = tempfile::tempdir().unwrap();
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let invoker = Arc::new(HookRecordingInvoker::new());
    let executor = make_executor(invoker.clone(), &[], store);
    executor
        .run(&minimal_config(), &Default::default(), json!({"pipeline_id": "p"}))
        .await
        .expect("run ok");
    assert!(invoker.hooks.lock().unwrap().is_empty(), "未注册 → 不分发");
}

/// 分发失败（Err）是 best-effort：不影响 run 完成返回值。
#[tokio::test]
async fn test_dispatch_is_best_effort() {
    let tmp = tempfile::tempdir().unwrap();
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let invoker = Arc::new(HookRecordingInvoker::new());
    invoker.fail_on("spill_retrieve_tool");
    let executor = make_executor(invoker.clone(), &["spill_retrieve_tool"], store);
    let result = executor
        .run(&minimal_config(), &Default::default(), json!({"pipeline_id": "p"}))
        .await;
    assert!(result.is_ok(), "钩子分发失败不得让 run 失败");
    assert_eq!(invoker.hook_calls("spill_retrieve_tool").len(), 1);
}

