//! ADR 引擎实现——极简调度器 + 状态账本
//!
//! ADR ①极简主义：引擎仅为调度器与状态账本，不含业务逻辑。
//! 只负责：按配置顺序调用插件、维护状态一致性、记录变更日志（Append-Only Patch）。
//!
//! [来源: docs/working/adr_engine_design.md §3.3]
//! [来源: docs/tasks/task_06_pipeline_engine.md]

use std::collections::HashMap;
use std::sync::Arc;

use async_trait::async_trait;
use agentos_core::traits::{AdrEngine, HookContext, LifecycleHook, PluginInvoker, StorageBackend};
use agentos_core::types::{
    CompositeStep, ContentLoader, EngineError, PatchType, PluginContext, PluginResult, RouteType,
    RunStatus, StepResult, SuspendHandle, TraceEntry, WakeEvent,
};
use sha2::{Digest, Sha256};
use tracing::{info, warn};
use uuid::Uuid;

use crate::store::SqliteStore;

/// ADR 引擎实现。
///
/// 引擎核心循环（ADR 版）：
/// 1. 从 config 加载步骤序列（YAML 定义）
/// 2. for each step in steps:
///    a. 构造 PluginContext（从 SQLite 读取当前状态 + 按需加载 BLOB 内容）
///    b. 通过 PluginInvoker 调用插件 execute(ctx) -> PluginResult
///    c. 将 PluginResult.state_updates 作为 Patch 追加到 traces 表
///    d. 如果 PluginResult 有 route_signal：NextLlm/NextTool/End/Wait
///    e. 如果出错：按 ErrorPolicy 处理（Abort/Skip/Retry/Fallback）
/// 3. 记录运行结束到 runs 表
pub struct AdrEngineImpl {
    store: Arc<SqliteStore>,
    invoker: Arc<dyn PluginInvoker>,
    default_tenant_id: String,
}

impl AdrEngineImpl {
    pub fn new(
        store: Arc<SqliteStore>,
        invoker: Arc<dyn PluginInvoker>,
        default_tenant_id: impl Into<String>,
    ) -> Self {
        Self {
            store,
            invoker,
            default_tenant_id: default_tenant_id.into(),
        }
    }

    fn config_hash(config: &serde_json::Value) -> Result<String, EngineError> {
        let mut hasher = Sha256::new();
        let bytes = serde_json::to_vec(config).map_err(|e| EngineError::Other {
            message: format!("config serialization failed: {}", e),
        })?;
        hasher.update(&bytes);
        Ok(format!("{:x}", hasher.finalize()))
    }

    async fn replay_state(
        &self,
        branch_id: &str,
        current_seq: u32,
    ) -> Result<HashMap<String, serde_json::Value>, EngineError> {
        let traces = self
            .store
            .get_traces(branch_id, 0, current_seq)
            .map_err(EngineError::Storage)?;
        let mut state = HashMap::new();
        for trace in traces {
            if trace.patch_type == PatchType::StateUpdate {
                if let Some(obj) = trace.patch_data.as_object() {
                    for (key, value) in obj {
                        state.insert(key.clone(), value.clone());
                    }
                }
            }
        }
        Ok(state)
    }

    /// 构造 PluginContext，注入 step.inputs 作为 config（AC-05-10）。
    fn build_plugin_context(
        &self,
        run_id: &str,
        branch_id: &str,
        step: &CompositeStep,
        state: HashMap<String, serde_json::Value>,
    ) -> Result<PluginContext, EngineError> {
        let state_json = serde_json::to_value(&state).map_err(|e| EngineError::Other {
            message: format!("state serialization failed: {}", e),
        })?;
        let content_loader = ContentLoader::new(
            self.store.clone(),
            run_id.to_string(),
            branch_id.to_string(),
            0,
        );
        let tenant_id =
            agentos_tenant::current_or_default(&self.default_tenant_id).tenant_id;
        Ok(PluginContext::new(
            state_json,
            step.inputs.clone(),
            agentos_core::types::TenantContext::new(tenant_id, run_id),
            Uuid::new_v4(),
            content_loader,
        ))
    }

    /// 构造标签化 HookContext（ADR ⑨）。
    fn build_hook_context(&self, run_id: &str, branch_id: &str, seq: u32) -> HookContext {
        let tenant_id =
            agentos_tenant::current_or_default(&self.default_tenant_id).tenant_id;
        let mut ctx = HookContext::new();
        ctx.set("run_id", serde_json::json!(run_id));
        ctx.set("branch_id", serde_json::json!(branch_id));
        ctx.set("seq", serde_json::json!(seq));
        ctx.set("tenant_id", serde_json::json!(tenant_id));
        ctx
    }

    /// 在租户上下文作用域内执行 future。
    ///
    /// 已存在 task_local（如 HTTP 入口注入）则原样执行，尊重外部租户；
    /// 否则用 `self.default_tenant_id` 建立 scope，保证同一次 run 的写入/读取租户一致。
    /// 这是 store 层隐式租户过滤与 per-engine 默认租户之间的桥梁。
    async fn scoped<F, R>(&self, f: F) -> R
    where
        F: std::future::Future<Output = R>,
    {
        match agentos_tenant::current() {
            Some(_) => f.await,
            None => {
                agentos_tenant::scope(
                    agentos_core::types::TenantContext::new(&self.default_tenant_id, ""),
                    f,
                )
                .await
            }
        }
    }

    /// 分发生命周期钩子事件（AC-05-8）。
    async fn dispatch_hook(&self, plugin_id: &str, hook: LifecycleHook, ctx: &HookContext) {
        if let Err(e) = self.invoker.send_lifecycle_hook(plugin_id, hook, ctx).await {
            warn!(
                "Lifecycle hook dispatch failed: plugin={} error={}",
                plugin_id, e
            );
        }
    }

    async fn append_trace(
        &self,
        run_id: &str,
        branch_id: &str,
        seq: u32,
        plugin_id: &str,
        patch_type: PatchType,
        patch_data: serde_json::Value,
    ) -> Result<(), EngineError> {
        let entry = TraceEntry {
            trace_id: Uuid::new_v4().to_string(),
            run_id: run_id.to_string(),
            branch_id: branch_id.to_string(),
            seq_in_branch: seq,
            plugin_id: plugin_id.to_string(),
            patch_type,
            patch_data,
            created_at: chrono::Utc::now().to_rfc3339(),
        };
        self.store.append_trace(entry).await?;
        Ok(())
    }

    /// 记录步骤的 Patch（state_updates + route_signal + error）。
    async fn record_step_patches(
        &self,
        run_id: &str,
        branch_id: &str,
        seq: u32,
        step: &CompositeStep,
        result: &PluginResult,
    ) -> Result<(), EngineError> {
        if !result.state_updates.is_empty() {
            let patch_data =
                serde_json::to_value(&result.state_updates).map_err(|e| EngineError::Other {
                    message: format!("state_updates serialization: {}", e),
                })?;
            self.append_trace(
                run_id,
                branch_id,
                seq,
                &step.plugin,
                PatchType::StateUpdate,
                patch_data,
            )
            .await?;
        }
        if let Some(ref route_signal) = result.route_signal {
            let signal_data =
                serde_json::to_value(route_signal).map_err(|e| EngineError::Other {
                    message: format!("route_signal serialization: {}", e),
                })?;
            self.append_trace(
                run_id,
                branch_id,
                seq,
                &step.plugin,
                PatchType::RouteSignal,
                signal_data,
            )
            .await?;
        }
        if let Some(ref err) = result.error {
            let err_data = serde_json::to_value(err).map_err(|e| EngineError::Other {
                message: format!("error serialization: {}", e),
            })?;
            self.append_trace(
                run_id,
                branch_id,
                seq,
                &step.plugin,
                PatchType::Error,
                err_data,
            )
            .await?;
            warn!(
                "Step returned error: run={} step={} error={}",
                run_id, step.name, err.message
            );
        }
        Ok(())
    }

    /// 消费路由信号并更新 run 状态（AC-05-5）。
    async fn apply_route_signal(
        &self,
        run_id: &str,
        branch_id: &str,
        seq: u32,
        route_type: Option<&RouteType>,
    ) -> Result<(), EngineError> {
        let (status, log_msg) = match route_type {
            Some(RouteType::End) => (RunStatus::Completed, "Run completed via End signal"),
            Some(RouteType::Wait) => (RunStatus::Suspended, "Run suspended via Wait signal"),
            _ => (RunStatus::Running, "Step completed, continuing"),
        };
        self.store
            .update_run_status(run_id, status, Some(branch_id), Some(seq))
            .await?;
        info!("{}: run={}", log_msg, run_id);
        Ok(())
    }
}

#[async_trait]
impl AdrEngine for AdrEngineImpl {
    async fn start_run(&self, config: &serde_json::Value) -> Result<String, EngineError> {
        let config = config.clone();
        self.scoped(async move {
            let run_id = Uuid::new_v4().to_string();
            let config_hash = Self::config_hash(&config)?;
            let tenant_ctx = agentos_tenant::current_or_default(&self.default_tenant_id);
            self.store
                .create_run(&run_id, &config_hash, &tenant_ctx.tenant_id)
                .map_err(EngineError::Storage)?;
            // 触发 OnPipelineStart 钩子（AC-05-8）
            let hook_ctx = self.build_hook_context(&run_id, "main", 0);
            self.dispatch_hook("__engine__", LifecycleHook::OnPipelineStart, &hook_ctx)
                .await;
            info!(
                "Run started: id={} tenant={}",
                run_id, tenant_ctx.tenant_id
            );
            Ok(run_id)
        })
        .await
    }

    async fn execute_step(
        &self,
        run_id: &str,
        step: &CompositeStep,
    ) -> Result<StepResult, EngineError> {
        let run_id = run_id.to_string();
        let step = step.clone();
        self.scoped(async move {
            let run = self.store.get_run(&run_id).await?;
            if run.status != RunStatus::Running {
                return Err(EngineError::InvalidState {
                    run_id: run_id.clone(),
                    reason: format!("expected 'running', got '{:?}'", run.status),
                });
            }

            let branch_id = run.current_branch.clone();
            let state = self.replay_state(&branch_id, run.current_seq).await?;
            let ctx = self.build_plugin_context(&run_id, &branch_id, &step, state)?;

            info!(
                "Executing step: run={} step={} plugin={}",
                run_id, step.name, step.plugin
            );

            let plugin_result = self
                .invoker
                .invoke_pipeline_plugin(&step.plugin, &ctx)
                .await?;

            let next_seq = run.current_seq + 1;
            self.record_step_patches(&run_id, &branch_id, next_seq, &step, &plugin_result)
                .await?;

            let route_type = plugin_result.route_signal.as_ref().map(|s| &s.route_type);
            self.apply_route_signal(&run_id, &branch_id, next_seq, route_type)
                .await?;

            Ok(StepResult {
                state_updates: plugin_result.state_updates,
                route_signal: plugin_result.route_signal,
            })
        })
        .await
    }

    async fn suspend(&self, run_id: &str) -> Result<SuspendHandle, EngineError> {
        let run_id = run_id.to_string();
        self.scoped(async move {
            let run = self.store.get_run(&run_id).await?;
            if run.status == RunStatus::Suspended {
                return Err(EngineError::InvalidState {
                    run_id: run_id.clone(),
                    reason: "already suspended".to_string(),
                });
            }
            if run.status == RunStatus::Completed || run.status == RunStatus::Failed {
                return Err(EngineError::InvalidState {
                    run_id: run_id.clone(),
                    reason: format!("run is already {:?}", run.status),
                });
            }
            let branch_id = run.current_branch.clone();
            let seq = run.current_seq;
            self.store
                .update_run_status(&run_id, RunStatus::Suspended, Some(&branch_id), Some(seq))
                .await?;
            info!(
                "Run suspended: run={} branch={} seq={}",
                run_id, branch_id, seq
            );
            Ok(SuspendHandle {
                run_id,
                branch_id,
                seq,
            })
        })
        .await
    }

    async fn resume(&self, handle: &SuspendHandle, event: WakeEvent) -> Result<(), EngineError> {
        let handle = handle.clone();
        self.scoped(async move {
            let run = self.store.get_run(&handle.run_id).await?;
            if run.status != RunStatus::Suspended {
                return Err(EngineError::InvalidState {
                    run_id: handle.run_id.clone(),
                    reason: format!("expected 'suspended', got '{:?}'", run.status),
                });
            }

            // 将 WakeEvent 序列化为 Lifecycle patch 追加到 traces 表（AC-05-7）
            let event_data = serde_json::to_value(&event).map_err(|e| EngineError::Other {
                message: format!("WakeEvent serialization: {}", e),
            })?;
            self.append_trace(
                &handle.run_id,
                &handle.branch_id,
                handle.seq + 1,
                "__engine__",
                PatchType::Lifecycle,
                event_data,
            )
            .await?;

            self.store
                .update_run_status(
                    &handle.run_id,
                    RunStatus::Running,
                    Some(&handle.branch_id),
                    Some(handle.seq + 1),
                )
                .await?;

            info!(
                "Run resumed: run={} branch={} seq={} event={:?}",
                handle.run_id, handle.branch_id, handle.seq, event
            );
            Ok(())
        })
        .await
    }

    async fn rollback(&self, run_id: &str, target_seq: u32) -> Result<String, EngineError> {
        let run_id = run_id.to_string();
        self.scoped(async move {
            let run = self.store.get_run(&run_id).await?;
            if run.status == RunStatus::Completed || run.status == RunStatus::Failed {
                return Err(EngineError::InvalidState {
                    run_id: run_id.clone(),
                    reason: format!("cannot rollback a {:?} run", run.status),
                });
            }

            let parent_branch = run.current_branch.clone();
            let uuid_short = &Uuid::new_v4().to_string()[..8];
            let new_branch_id = format!("{}.rollback.{}", parent_branch, uuid_short);

            let branch = agentos_core::types::Branch {
                branch_id: new_branch_id.clone(),
                run_id: run_id.clone(),
                parent_branch: Some(parent_branch.clone()),
                parent_seq: Some(target_seq),
                created_at: chrono::Utc::now().to_rfc3339(),
            };
            self.store.create_branch(branch).await?;

            let traces = self
                .store
                .get_traces(&parent_branch, 0, target_seq)
                .map_err(EngineError::Storage)?;
            for trace in traces {
                if trace.patch_type == PatchType::StateUpdate {
                    self.append_trace(
                        &run_id,
                        &new_branch_id,
                        trace.seq_in_branch,
                        &trace.plugin_id,
                        PatchType::StateUpdate,
                        trace.patch_data.clone(),
                    )
                    .await?;
                }
            }

            self.append_trace(
                &run_id,
                &new_branch_id,
                target_seq + 1,
                "__engine__",
                PatchType::Rollback,
                serde_json::json!({"parent_branch": parent_branch, "target_seq": target_seq}),
            )
            .await?;

            self.store
                .update_run_status(
                    &run_id,
                    RunStatus::Running,
                    Some(&new_branch_id),
                    Some(target_seq + 1),
                )
                .await?;

            info!(
                "Rollback completed: run={} new_branch={}",
                run_id, new_branch_id
            );
            Ok(new_branch_id)
        })
        .await
    }

    async fn end_run(&self, run_id: &str) -> Result<(), EngineError> {
        let run_id = run_id.to_string();
        self.scoped(async move {
            let run = self.store.get_run(&run_id).await?;
            if run.status == RunStatus::Completed || run.status == RunStatus::Failed {
                return Err(EngineError::InvalidState {
                    run_id: run_id.clone(),
                    reason: format!("run is already {:?}", run.status),
                });
            }

            // 触发 OnPipelineEnd 钩子（AC-05-8）
            let hook_ctx = self.build_hook_context(&run_id, &run.current_branch, run.current_seq);
            self.dispatch_hook("__engine__", LifecycleHook::OnPipelineEnd, &hook_ctx)
                .await;

            self.store
                .update_run_status(
                    &run_id,
                    RunStatus::Completed,
                    Some(&run.current_branch),
                    Some(run.current_seq),
                )
                .await?;

            info!("Run ended: run={}", run_id);
            Ok(())
        })
        .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::StorageBackend;
    use agentos_core::types::{PluginError, RouteSignal, ToolExecutionResult};
    use serde_json::json;

    struct MockInvoker {
        results: HashMap<String, PluginResult>,
    }

    #[async_trait]
    impl PluginInvoker for MockInvoker {
        async fn invoke_pipeline_plugin(
            &self,
            plugin_id: &str,
            _ctx: &PluginContext,
        ) -> Result<PluginResult, PluginError> {
            Ok(self.results.get(plugin_id).cloned().unwrap_or_default())
        }
        async fn invoke_tool(
            &self,
            _plugin_id: &str,
            _tool_name: &str,
            _inputs: &serde_json::Value,
        ) -> Result<ToolExecutionResult, PluginError> {
            Ok(ToolExecutionResult::success(json!({})))
        }
        async fn send_lifecycle_hook(
            &self,
            _plugin_id: &str,
            _hook: LifecycleHook,
            _context: &HookContext,
        ) -> Result<(), PluginError> {
            Ok(())
        }
    }

    fn make_engine(
        invoker_results: Vec<(&str, PluginResult)>,
    ) -> (AdrEngineImpl, Arc<SqliteStore>) {
        let store = Arc::new(SqliteStore::open_memory().unwrap());
        let mut invoker = MockInvoker {
            results: HashMap::new(),
        };
        for (id, result) in invoker_results {
            invoker.results.insert(id.to_string(), result);
        }
        let engine = AdrEngineImpl::new(store.clone(), Arc::new(invoker), "default");
        (engine, store)
    }

    fn make_step(name: &str, plugin: &str) -> CompositeStep {
        CompositeStep {
            name: name.to_string(),
            plugin: plugin.to_string(),
            inputs: json!({}),
            outputs: HashMap::new(),
        }
    }

    #[tokio::test]
    async fn test_start_run() {
        let (engine, store) = make_engine(vec![]);
        let run_id = engine.start_run(&json!({"test": true})).await.unwrap();
        assert!(!run_id.is_empty());
        let run = store.get_run(&run_id).await.unwrap();
        assert_eq!(run.status, RunStatus::Running);
        assert_eq!(run.current_branch, "main");
        assert_eq!(run.current_seq, 0);
    }

    #[tokio::test]
    async fn test_execute_step_state_update() {
        let mut state_updates = HashMap::new();
        state_updates.insert("key1".to_string(), json!("value1"));
        let result = PluginResult {
            state_updates,
            ..Default::default()
        };
        let (engine, store) = make_engine(vec![("test_plugin", result)]);
        let run_id = engine.start_run(&json!({})).await.unwrap();
        let step = make_step("step1", "test_plugin");
        let step_result = engine.execute_step(&run_id, &step).await.unwrap();
        assert_eq!(
            step_result.state_updates.get("key1"),
            Some(&json!("value1"))
        );
        let run = store.get_run(&run_id).await.unwrap();
        assert_eq!(run.current_seq, 1);
        let traces = store.get_traces("main", 1, 1).unwrap();
        assert_eq!(traces.len(), 1);
        assert_eq!(traces[0].patch_type, PatchType::StateUpdate);
    }

    #[tokio::test]
    async fn test_execute_step_route_end() {
        let result = PluginResult {
            route_signal: Some(RouteSignal::new(RouteType::End)),
            ..Default::default()
        };
        let (engine, store) = make_engine(vec![("end_plugin", result)]);
        let run_id = engine.start_run(&json!({})).await.unwrap();
        engine
            .execute_step(&run_id, &make_step("end_step", "end_plugin"))
            .await
            .unwrap();
        let run = store.get_run(&run_id).await.unwrap();
        assert_eq!(run.status, RunStatus::Completed);
    }

    #[tokio::test]
    async fn test_execute_step_route_wait() {
        let result = PluginResult {
            route_signal: Some(RouteSignal::new(RouteType::Wait)),
            ..Default::default()
        };
        let (engine, store) = make_engine(vec![("wait_plugin", result)]);
        let run_id = engine.start_run(&json!({})).await.unwrap();
        engine
            .execute_step(&run_id, &make_step("wait_step", "wait_plugin"))
            .await
            .unwrap();
        let run = store.get_run(&run_id).await.unwrap();
        assert_eq!(run.status, RunStatus::Suspended);
    }

    #[tokio::test]
    async fn test_execute_step_route_next_llm() {
        let result = PluginResult {
            route_signal: Some(RouteSignal::new(RouteType::NextLlm)),
            ..Default::default()
        };
        let (engine, store) = make_engine(vec![("llm_plugin", result)]);
        let run_id = engine.start_run(&json!({})).await.unwrap();
        engine
            .execute_step(&run_id, &make_step("llm_step", "llm_plugin"))
            .await
            .unwrap();
        let run = store.get_run(&run_id).await.unwrap();
        assert_eq!(run.status, RunStatus::Running);
        assert_eq!(run.current_seq, 1);
    }

    #[tokio::test]
    async fn test_suspend_and_resume() {
        let (engine, store) = make_engine(vec![]);
        let run_id = engine.start_run(&json!({})).await.unwrap();
        let handle = engine.suspend(&run_id).await.unwrap();
        assert_eq!(handle.run_id, run_id);
        let run = store.get_run(&run_id).await.unwrap();
        assert_eq!(run.status, RunStatus::Suspended);
        engine.resume(&handle, WakeEvent::Manual).await.unwrap();
        let run = store.get_run(&run_id).await.unwrap();
        assert_eq!(run.status, RunStatus::Running);
    }

    #[tokio::test]
    async fn test_resume_appends_wake_event_trace() {
        let (engine, store) = make_engine(vec![]);
        let run_id = engine.start_run(&json!({})).await.unwrap();
        let handle = engine.suspend(&run_id).await.unwrap();
        engine.resume(&handle, WakeEvent::Timer).await.unwrap();
        // WakeEvent 应作为 Lifecycle patch 追加到 traces 表
        let traces = store.get_traces("main", 0, 10).unwrap();
        assert!(traces.iter().any(|t| t.patch_type == PatchType::Lifecycle));
    }

    #[tokio::test]
    async fn test_suspend_already_suspended() {
        let (engine, _store) = make_engine(vec![]);
        let run_id = engine.start_run(&json!({})).await.unwrap();
        engine.suspend(&run_id).await.unwrap();
        let result = engine.suspend(&run_id).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_rollback() {
        let results = vec![
            (
                "p1",
                PluginResult {
                    state_updates: {
                        let mut m = HashMap::new();
                        m.insert("step1".to_string(), json!("data1"));
                        m
                    },
                    ..Default::default()
                },
            ),
            (
                "p2",
                PluginResult {
                    state_updates: {
                        let mut m = HashMap::new();
                        m.insert("step2".to_string(), json!("data2"));
                        m
                    },
                    ..Default::default()
                },
            ),
            (
                "p3",
                PluginResult {
                    state_updates: {
                        let mut m = HashMap::new();
                        m.insert("step3".to_string(), json!("data3"));
                        m
                    },
                    ..Default::default()
                },
            ),
        ];
        let (engine, store) = make_engine(results);
        let run_id = engine.start_run(&json!({})).await.unwrap();
        for (name, plugin) in [("s1", "p1"), ("s2", "p2"), ("s3", "p3")] {
            engine
                .execute_step(&run_id, &make_step(name, plugin))
                .await
                .unwrap();
        }
        let run = store.get_run(&run_id).await.unwrap();
        assert_eq!(run.current_seq, 3);
        let new_branch = engine.rollback(&run_id, 1).await.unwrap();
        assert!(new_branch.starts_with("main.rollback."));
        let run = store.get_run(&run_id).await.unwrap();
        assert_eq!(run.current_branch, new_branch);
        assert_eq!(run.status, RunStatus::Running);
        let traces = store.get_traces(&new_branch, 0, 2).unwrap();
        assert!(!traces.is_empty());
    }

    #[tokio::test]
    async fn test_rollback_preserves_original_data() {
        let (engine, store) = make_engine(vec![(
            "p1",
            PluginResult {
                state_updates: {
                    let mut m = HashMap::new();
                    m.insert("key".to_string(), json!("value"));
                    m
                },
                ..Default::default()
            },
        )]);
        let run_id = engine.start_run(&json!({})).await.unwrap();
        engine
            .execute_step(&run_id, &make_step("s1", "p1"))
            .await
            .unwrap();
        engine.rollback(&run_id, 0).await.unwrap();
        let original_traces = store.get_traces("main", 0, 1).unwrap();
        assert_eq!(original_traces.len(), 1);
    }

    #[tokio::test]
    async fn test_end_run() {
        let (engine, store) = make_engine(vec![]);
        let run_id = engine.start_run(&json!({})).await.unwrap();
        engine.end_run(&run_id).await.unwrap();
        let run = store.get_run(&run_id).await.unwrap();
        assert_eq!(run.status, RunStatus::Completed);
        assert!(run.ended_at.is_some());
    }

    #[tokio::test]
    async fn test_end_run_already_completed() {
        let (engine, _store) = make_engine(vec![]);
        let run_id = engine.start_run(&json!({})).await.unwrap();
        engine.end_run(&run_id).await.unwrap();
        let result = engine.end_run(&run_id).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_execute_step_invalid_state() {
        let (engine, _store) = make_engine(vec![]);
        let run_id = engine.start_run(&json!({})).await.unwrap();
        engine.suspend(&run_id).await.unwrap();
        let result = engine.execute_step(&run_id, &make_step("s1", "p1")).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_state_replay_across_steps() {
        let (engine, store) = make_engine(vec![
            (
                "p1",
                PluginResult {
                    state_updates: {
                        let mut m = HashMap::new();
                        m.insert("key1".to_string(), json!("val1"));
                        m
                    },
                    ..Default::default()
                },
            ),
            (
                "p2",
                PluginResult {
                    state_updates: {
                        let mut m = HashMap::new();
                        m.insert("key2".to_string(), json!("val2"));
                        m
                    },
                    ..Default::default()
                },
            ),
        ]);
        let run_id = engine.start_run(&json!({})).await.unwrap();
        engine
            .execute_step(&run_id, &make_step("s1", "p1"))
            .await
            .unwrap();
        engine
            .execute_step(&run_id, &make_step("s2", "p2"))
            .await
            .unwrap();
        let traces = store.get_traces("main", 0, 2).unwrap();
        assert_eq!(traces.len(), 2);
        let state = engine.replay_state("main", 2).await.unwrap();
        assert_eq!(state.get("key1"), Some(&json!("val1")));
        assert_eq!(state.get("key2"), Some(&json!("val2")));
    }

    #[tokio::test]
    async fn test_step_inputs_injected() {
        let (engine, _store) = make_engine(vec![("p1", PluginResult::default())]);
        let run_id = engine.start_run(&json!({})).await.unwrap();
        let step = CompositeStep {
            name: "step1".to_string(),
            plugin: "p1".to_string(),
            inputs: json!({"custom_input": "hello"}),
            outputs: HashMap::new(),
        };
        // step.inputs 应被注入 PluginContext.config，验证不 panic
        engine.execute_step(&run_id, &step).await.unwrap();
    }
}
