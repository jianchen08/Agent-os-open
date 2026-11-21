// @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: rust-test
//! pipeline_loop 单元测试（原单体 tests.rs 按主题拆分，共享辅助留主壳）。

use super::*;

use crate::compiler::compile_pipeline;

use crate::SqliteStore;

use agentos_core::traits::{
    HostType, ManifestCapabilities, PluginManifest, PluginType, StepCapability, ToolCapability,
};
use agentos_core::types::StepCheck;

use agentos_core::types::{
    LoopBody, LoopConfig, PipelineConfig, PipelineStep, Route, StepItem, StepLibrary,
};
use async_trait::async_trait;

use serde_json::json;

use std::collections::HashMap;

use std::sync::atomic::{AtomicUsize, Ordering};

use std::sync::Mutex;

use agentos_core::traits::StorageBackend;

use agentos_core::types::{
    CheckpointConfig, MessageRecord, RunRecord, RunStatus, ToolExecutionResult, TraceEntry,
};

struct MockInvoker {
    results: Mutex<HashMap<String, PluginResult>>,
    /// 按调用序弹出的结果序列（耗尽后回落静态 results；逐次不同写入用）。
    sequences: Mutex<HashMap<String, Vec<PluginResult>>>,
    calls: Mutex<HashMap<String, usize>>,
    configs: Mutex<Vec<(String, serde_json::Value)>>,
    /// 预设 invoker 级 Err（模拟 sidecar 不可达等 invoker 自身报错）。
    errs: Mutex<HashMap<String, PluginError>>,
}

impl MockInvoker {
    fn new() -> Self {
        Self {
            results: Mutex::new(HashMap::new()),
            sequences: Mutex::new(HashMap::new()),
            calls: Mutex::new(HashMap::new()),
            configs: Mutex::new(Vec::new()),
            errs: Mutex::new(HashMap::new()),
        }
    }

    fn set_result(&self, plugin_id: &str, result: PluginResult) {
        self.results
            .lock()
            .unwrap()
            .insert(plugin_id.to_string(), result);
    }

    /// 预设按调用序弹出的结果序列（耗尽后回落静态 results）。
    fn set_sequence(&self, plugin_id: &str, sequence: Vec<PluginResult>) {
        self.sequences
            .lock()
            .unwrap()
            .insert(plugin_id.to_string(), sequence);
    }

    fn set_err(&self, plugin_id: &str, err: PluginError) {
        self.errs.lock().unwrap().insert(plugin_id.to_string(), err);
    }

    fn call_count(&self, plugin_id: &str) -> usize {
        *self.calls.lock().unwrap().get(plugin_id).unwrap_or(&0)
    }

    /// 某插件每次 invoke 收到的 ctx.config（按调用顺序；无调用 = 空 Vec）。
    fn captured_configs(&self, plugin_id: &str) -> Vec<serde_json::Value> {
        self.configs
            .lock()
            .unwrap()
            .iter()
            .filter(|(p, _)| p == plugin_id)
            .map(|(_, c)| c.clone())
            .collect()
    }
}

#[async_trait]
impl PluginInvoker for MockInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        // 计数
        *self
            .calls
            .lock()
            .unwrap()
            .entry(plugin_id.to_string())
            .or_insert(0) += 1;
        // 捕获每插件收到的 config（per-plugin inputs 经此通道）
        self.configs
            .lock()
            .unwrap()
            .push((plugin_id.to_string(), ctx.config.clone()));
        // invoker 级 Err 优先（模拟 sidecar 不可达）
        if let Some(e) = self.errs.lock().unwrap().get(plugin_id).cloned() {
            return Err(e);
        }
        // 结果序列次之（按调用序弹出；耗尽回落静态 results）
        if let Some(next) = self
            .sequences
            .lock()
            .unwrap()
            .get_mut(plugin_id)
            .and_then(|v| (!v.is_empty()).then(|| v.remove(0)))
        {
            return Ok(next);
        }
        // 返回预设结果（缺失则返回空成功）
        let result = self
            .results
            .lock()
            .unwrap()
            .get(plugin_id)
            .cloned()
            .unwrap_or_default();
        Ok(result)
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
        _plugin_id: &str,
        _hook: agentos_core::traits::LifecycleHook,
        _context: &agentos_core::traits::HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
}

struct NullStorage {
    checkpoints: Mutex<Vec<i64>>,
    trace_plugin_ids: Mutex<Vec<String>>,
    /// 收到的完整轨迹条目（补丁形态断言用；append_trace 全量留档）。
    traces: Mutex<Vec<TraceEntry>>,
    /// true 时 record_run_start 返回 Err（persist_run_start 持久化故障注入）。
    fail_record_run_start: std::sync::atomic::AtomicBool,
    /// true 时 apply_messages_ops_to_table 返回 Err（messages 表写持久化故障注入，
    /// DB-first 判别测试用：表写失败内存必须零副作用）。
    fail_messages_ops: std::sync::atomic::AtomicBool,
    /// 收到的 run_status 投影序列（upsert_state_field 调用序，终态映射表测试读取）。
    run_statuses: Mutex<Vec<RunStatus>>,
}

impl Default for NullStorage {
    fn default() -> Self {
        Self {
            checkpoints: Mutex::new(Vec::new()),
            trace_plugin_ids: Mutex::new(Vec::new()),
            traces: Mutex::new(Vec::new()),
            fail_record_run_start: std::sync::atomic::AtomicBool::new(false),
            fail_messages_ops: std::sync::atomic::AtomicBool::new(false),
            run_statuses: Mutex::new(Vec::new()),
        }
    }
}

impl NullStorage {
    /// 每次 save_checkpoint 收到的 step_no（升序）。
    fn saved_step_nos(&self) -> Vec<i64> {
        self.checkpoints.lock().unwrap().clone()
    }

    fn save_count(&self) -> usize {
        self.checkpoints.lock().unwrap().len()
    }

    /// step 窗口轨迹的 plugin_id 序列（排除引擎窗口 run_init/body_enter/
    /// body_route/run_finalize）——「无产出 step 不落 trace」断言用。
    fn step_trace_plugin_ids(&self) -> Vec<String> {
        self.trace_plugin_ids
            .lock()
            .unwrap()
            .iter()
            .filter(|id| {
                !matches!(
                    id.as_str(),
                    "run_init" | "body_enter" | "body_route" | "run_finalize"
                )
            })
            .cloned()
            .collect()
    }

    /// 收到的 run_status 投影（终态写面 upsert_state_field 调用序）。
    fn recorded_run_statuses(&self) -> Vec<RunStatus> {
        self.run_statuses.lock().unwrap().clone()
    }
}

#[async_trait]
impl StorageBackend for NullStorage {
    async fn record_run_start(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _run_id: &str,
        _config_hash: &str,
        _started_at: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        if self
            .fail_record_run_start
            .load(std::sync::atomic::Ordering::SeqCst)
        {
            return Err(agentos_core::types::StorageError::Database(
                "injected record_run_start failure".into(),
            ));
        }
        Ok(())
    }
    async fn get_run(&self, _run_id: &str) -> Result<RunRecord, agentos_core::types::StorageError> {
        Err(agentos_core::types::StorageError::NotFound("null".into()))
    }
    async fn apply_messages_ops_to_table(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _ops: &[serde_json::Value],
    ) -> Result<(), agentos_core::types::StorageError> {
        if self.fail_messages_ops.load(Ordering::SeqCst) {
            return Err(agentos_core::types::StorageError::Database(
                "injected apply_messages_ops_to_table failure".into(),
            ));
        }
        Ok(())
    }
    async fn get_messages_by_pipeline(
        &self,
        _pipeline_id: &str,
        _opts: agentos_core::traits::MessageQueryOpts,
    ) -> Result<Vec<MessageRecord>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn append_trace(
        &self,
        entry: TraceEntry,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.trace_plugin_ids
            .lock()
            .unwrap()
            .push(entry.plugin_id.clone());
        self.traces.lock().unwrap().push(entry);
        Ok(())
    }
    async fn save_checkpoint(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        step_no: i64,
        _state: &serde_json::Value,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.checkpoints.lock().unwrap().push(step_no);
        Ok(())
    }
    async fn upsert_state_field(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        key: &str,
        value: &serde_json::Value,
    ) -> Result<(), agentos_core::types::StorageError> {
        // 终态写面（runs 表退役后引擎经 upsert_state_fields 批量写 run_status）
        if key == "run_status" {
            if let Some(s) = value.as_str() {
                let status = match s {
                    "running" => RunStatus::Running,
                    "suspended" => RunStatus::Suspended,
                    "completed" => RunStatus::Completed,
                    "failed" => RunStatus::Failed,
                    "cancelled" => RunStatus::Cancelled,
                    _ => RunStatus::Running,
                };
                self.run_statuses.lock().unwrap().push(status);
            }
        }
        Ok(())
    }
    async fn store_blob(
        &self,
        _data: &[u8],
        _mime_type: &str,
    ) -> Result<String, agentos_core::types::StorageError> {
        Ok("null".to_string())
    }
    async fn create_session(
        &self,
        _session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn get_session(
        &self,
        _thread_id: &str,
    ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        Ok(None)
    }
    async fn list_sessions(
        &self,
        _filter: agentos_core::traits::SessionListFilter,
    ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn update_session(
        &self,
        _session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn delete_session(
        &self,
        _thread_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        Ok(Vec::new())
    }
    async fn link_pipeline_session(
        &self,
        _pipeline_id: &str,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn list_pipeline_ids_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn get_step_traces_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError> {
        Ok(vec![])
    }

    // ── users（0.5.0 最小持久化）：NullStorage 不实现，返回空
    async fn create_user(
        &self,
        _user: &agentos_core::types::UserRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn get_user_by_id(
        &self,
        _user_id: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        Ok(None)
    }
    async fn get_user_by_username(
        &self,
        _username: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        Ok(None)
    }
    async fn list_users(
        &self,
    ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        Ok(Vec::new())
    }
    async fn update_last_login(
        &self,
        _user_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn delete_user(&self, _user_id: &str) -> Result<bool, agentos_core::types::StorageError> {
        Ok(false)
    }
    async fn update_user_password(
        &self,
        _user_id: &str,
        _password_hash: &str,
        _must_change_password: bool,
    ) -> Result<bool, agentos_core::types::StorageError> {
        Ok(false)
    }
}

struct Fixture {
    executor: PipelineExecutor,
    invoker: Arc<MockInvoker>,
    store: Arc<NullStorage>,
}

impl Fixture {
    fn build(plugin_ids: &[&str]) -> Self {
        let invoker = Arc::new(MockInvoker::new());
        let store = Arc::new(NullStorage::default());
        let executor = PipelineExecutor::new(
            invoker.clone() as Arc<dyn PluginInvoker>,
            PathBuf::from("."),
            TenantContext::new("tenant_test", "session_test"),
            plugin_ids.iter().map(|s| s.to_string()),
            store.clone() as Arc<dyn StorageBackend>,
            "run_test",
        );
        Self {
            executor,
            invoker,
            store,
        }
    }

    async fn run(
        &self,
        config: &PipelineConfig,
        library: &StepLibrary,
        initial: serde_json::Value,
    ) -> serde_json::Value {
        // 旧 PipelineExecutor::run 兼容路径已删除（生产零调用）；测试显式
        // 编译后走 run_compiled（G10 生产路径，语义一致）。
        let compiled = compile_pipeline(config, library, &self.executor.plugin_ids)
            .expect("compile should succeed");
        self.executor
            .run_compiled(&compiled, initial)
            .await
            .expect("run should succeed")
    }
}

fn atomic_step(id: &str, plugin: &str) -> PipelineStep {
    PipelineStep {
        id: id.to_string(),
        steps: vec![plugin.into()],
        when: None,
        context: HashMap::new(),
        routes: vec![],
        loop_config: None,
    }
}

fn updates(pairs: &[(&str, serde_json::Value)]) -> HashMap<String, serde_json::Value> {
    pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.clone()))
        .collect()
}

fn gated_body(steps: Vec<StepItem>) -> PipelineConfig {
    PipelineConfig {
        name: "gate".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "body".into(),
                when: None,
                steps,
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    }
}

struct LabelCapturingInvoker {
    seen: Mutex<Vec<(String, String)>>,
}

#[async_trait]
impl PluginInvoker for LabelCapturingInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        _ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        // invoke await = 既有等待点：标签此刻应指向本插件（invoke_plugin 设置）。
        let label =
            agentos_core::task_activity::current_label().unwrap_or_else(|| "<none>".to_string());
        self.seen
            .lock()
            .unwrap()
            .push((plugin_id.to_string(), label));
        Ok(PluginResult::default())
    }
    async fn invoke_tool(
        &self,
        _p: &str,
        _t: &str,
        _i: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        Ok(ToolExecutionResult::success(serde_json::Value::Null))
    }
    async fn send_lifecycle_hook(
        &self,
        _p: &str,
        _h: agentos_core::traits::LifecycleHook,
        _c: &agentos_core::traits::HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
}

struct CountingInvoker {
    counter: Arc<AtomicUsize>,
    /// 第 N 次调用后 set ended=true（0=永不）。stop_after=3 表示第 3 次起 set ended。
    stop_after: usize,
    /// 第 N 次调用后 set suspended=true（0=永不）。优先级低于 stop_after。
    set_suspended_after: usize,
}

#[async_trait]
impl PluginInvoker for CountingInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        _ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        let n = self.counter.fetch_add(1, Ordering::SeqCst) + 1;
        let mut updates = HashMap::new();
        updates.insert("round".into(), json!(n));
        if self.stop_after > 0 && n >= self.stop_after {
            updates.insert("ended".into(), json!(true));
        } else if self.set_suspended_after > 0 && n >= self.set_suspended_after {
            updates.insert("suspended".into(), json!(true));
        }
        Ok(PluginResult {
            state_updates: updates,
            ..Default::default()
        })
    }
    async fn invoke_tool(
        &self,
        _p: &str,
        _t: &str,
        _i: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        Ok(ToolExecutionResult::success(serde_json::Value::Null))
    }
    async fn send_lifecycle_hook(
        &self,
        _p: &str,
        _h: agentos_core::traits::LifecycleHook,
        _c: &agentos_core::traits::HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
}

fn make_executor(invoker: Arc<dyn PluginInvoker>, plugin_ids: &[&str]) -> PipelineExecutor {
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage::default());
    PipelineExecutor::new(
        invoker,
        PathBuf::from("."),
        TenantContext::new("t", "s"),
        plugin_ids.iter().map(|s| s.to_string()),
        store,
        "r",
    )
}

struct SequenceInvoker {
    counter: Arc<AtomicUsize>,
    results: Vec<HashMap<String, serde_json::Value>>,
}

#[async_trait]
impl PluginInvoker for SequenceInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        _ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        let n = self.counter.fetch_add(1, Ordering::SeqCst);
        let updates = self.results.get(n).cloned().unwrap_or_default();
        Ok(PluginResult {
            state_updates: updates,
            ..Default::default()
        })
    }
    async fn invoke_tool(
        &self,
        _p: &str,
        _t: &str,
        _i: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        Ok(ToolExecutionResult::success(serde_json::Value::Null))
    }
    async fn send_lifecycle_hook(
        &self,
        _p: &str,
        _h: agentos_core::traits::LifecycleHook,
        _c: &agentos_core::traits::HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
}

fn routed_loop_body(steps: Vec<StepItem>) -> PipelineConfig {
    PipelineConfig {
        name: "routed_loop".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "post".into(),
                when: None,
                steps,
                context: HashMap::new(),
                routes: vec![
                    Route {
                        when: "flag == true".into(),
                        then: agentos_core::types::RouteAction {
                            next: RouteNext::Loop,
                            set: updates(&[("core_type", json!("llm_call"))]),
                        },
                    },
                    Route {
                        when: "True".into(),
                        then: agentos_core::types::RouteAction {
                            next: RouteNext::End,
                            set: HashMap::new(),
                        },
                    },
                ],
                loop_config: None,
            }],
            while_cond: Some("True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    }
}

fn test_manifest(id: &str) -> PluginManifest {
    PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: id.to_string(),
        name: format!("Test {id}"),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Pipeline,
        language: "python".to_string(),
        host_type: HostType::Sidecar,
        host_group: None,
        entry: "server.py".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        restricted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        provides: None,
        visibility: Default::default(),
        persistent_fields: vec![],
        export_fields: vec![],
        aux_venvs: Vec::new(),
    }
}

fn tool_cap(name: &str) -> ToolCapability {
    ToolCapability {
        name: name.to_string(),
        description: None,
        input_schema: None,
        output_schema: None,
        category: None,
        ui: None,
        render: None,
        isolation_capable: None,
        smoke: None,
        timeout_ms: None,
    }
}

fn compiled_with_checks(
    fixture: &Fixture,
    config: &PipelineConfig,
    body_checks: &[(String, Vec<StepCheck>)],
    safety: &[&str],
) -> crate::compiler::CompiledPipeline {
    crate::compiler::compile_pipeline_with_checks(
        config,
        &StepLibrary::default(),
        &fixture.executor.plugin_ids,
        None,
        body_checks,
        &safety.iter().map(|s| s.to_string()).collect::<Vec<_>>(),
    )
    .expect("compile with checks ok")
}

fn check(at: &str, when: Option<&str>, then: &str) -> StepCheck {
    StepCheck {
        at: at.to_string(),
        when: when.map(|s| s.to_string()),
        then: then.to_string(),
    }
}

fn two_step_config() -> PipelineConfig {
    PipelineConfig {
        name: "p".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s1", "a"), atomic_step("s2", "b")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    }
}

fn always_loop_body(plugin: &str) -> PipelineConfig {
    PipelineConfig {
        name: "ctrl".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s1", plugin)],
            while_cond: Some("True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    }
}

fn make_bus_executor(bus: Option<Arc<HookEventBus>>, run_id: &str) -> PipelineExecutor {
    let invoker = Arc::new(MockInvoker::new());
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage::default());
    PipelineExecutor::new(
        invoker as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("tenant_test", "session_test"),
        ["pipeline_dummy"].iter().map(|s| s.to_string()),
        store,
        run_id,
    )
    .with_hook_bus(bus)
}

fn lifecycle_bus_config() -> PipelineConfig {
    PipelineConfig {
        name: "lifecycle_bus".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("only", "pipeline_dummy")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: HashMap::new(),
        max_rounds: None,
    }
}

fn oracle_messages_chars(state: &serde_json::Value) -> i64 {
    state["messages"]
        .as_array()
        .expect("messages 应为数组")
        .iter()
        .map(|m| {
            let mut c = m.clone();
            if let Some(o) = c.as_object_mut() {
                o.remove("seq");
            }
            serde_json::to_string(&c).unwrap().len() as i64
        })
        .sum()
}

fn messages_update(ops: Vec<serde_json::Value>) -> HashMap<String, serde_json::Value> {
    let mut m = HashMap::new();
    m.insert("messages".to_string(), json!({ "_ops": ops }));
    m
}

const CONTEXT_GATE_WHEN: &str = "(track.llm_usage.last_input_tokens + (track.messages_chars - track.messages_chars_at_llm) * 2.0) / track.model_context_window > 0.6 or (track.llm_usage.last_input_tokens == none and len(messages) > 40)";

fn noop_pipeline_config() -> PipelineConfig {
    PipelineConfig {
        name: "noop".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    }
}

fn bare_end_post() -> PipelineStep {
    PipelineStep {
        id: "post".into(),
        steps: vec![],
        when: None,
        context: HashMap::new(),
        routes: vec![Route {
            when: "True".into(),
            then: agentos_core::types::RouteAction {
                next: RouteNext::End,
                set: HashMap::new(),
            },
        }],
        loop_config: None,
    }
}

struct ForeachInvoker {
    /// 每次 invoke 捕获的 current_call（invoke 顺序；并行下乱序，只做集合断言）。
    seen_calls: Mutex<Vec<serde_json::Value>>,
    /// 每次 invoke 捕获的 state.chain（串行链式可见性断言）。
    seen_chain: Mutex<Vec<serde_json::Value>>,
    /// 每次 invoke 观测的 (迭代 state 含 messages, base_state 含 messages)
    /// ——写集克隆共享基座断言（空 reads = 全量克隆回退）。
    seen_base: Mutex<Vec<(bool, bool)>>,
    /// 声明 reads（非空才参与共享基座切分；空 = 旧全量行为）。
    declared_reads: Mutex<Vec<String>>,
    /// 是否返回 messages op（消息归并序断言用）。
    emit_ops: bool,
    /// 命中该 current_call 时置 ended=true（终止传播断言用）。
    end_on: Option<serde_json::Value>,
}

impl ForeachInvoker {
    fn new(emit_ops: bool) -> Self {
        Self {
            seen_calls: Mutex::new(Vec::new()),
            seen_chain: Mutex::new(Vec::new()),
            seen_base: Mutex::new(Vec::new()),
            declared_reads: Mutex::new(Vec::new()),
            emit_ops,
            end_on: None,
        }
    }

    /// 声明插件 state.reads（触发并行迭代的写集克隆路径）。
    fn with_declared_reads(mut self, reads: Vec<String>) -> Self {
        self.declared_reads = Mutex::new(reads);
        self
    }
}

#[async_trait]
impl PluginInvoker for ForeachInvoker {
    fn plugin_declared_reads(&self, _plugin_id: &str) -> Option<Vec<String>> {
        let reads = self.declared_reads.lock().unwrap().clone();
        (!reads.is_empty()).then_some(reads)
    }

    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        let call = ctx
            .state
            .get("current_call")
            .cloned()
            .unwrap_or(serde_json::Value::Null);
        self.seen_chain.lock().unwrap().push(
            ctx.state
                .get("chain")
                .cloned()
                .unwrap_or(serde_json::Value::Null),
        );
        self.seen_calls.lock().unwrap().push(call.clone());
        self.seen_base.lock().unwrap().push((
            ctx.state.get("messages").is_some(),
            ctx.base_state.is_some_and(|b| b.contains_key("messages")),
        ));
        let mut ups = updates(&[
            ("tool_results", json!([{ "echo": call }])),
            ("_full_tool_results", json!([{ "full": call }])),
            ("_executed_tool_calls", json!([call.clone()])),
            ("submitted_task_ids", json!([call])),
            ("chain", json!(call)),
        ]);
        if self.emit_ops {
            let op = json!({
                "op": "set",
                "msg": {"role": "tool", "tool_call_id": call, "content": call}
            });
            ups.insert("messages".into(), json!({ "_ops": [op] }));
        }
        if self.end_on.as_ref() == Some(&call) {
            ups.insert("ended".into(), json!(true));
        }
        Ok(PluginResult {
            state_updates: ups,
            ..Default::default()
        })
    }
    async fn invoke_tool(
        &self,
        _p: &str,
        _t: &str,
        _i: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        Ok(ToolExecutionResult::success(serde_json::Value::Null))
    }
    async fn send_lifecycle_hook(
        &self,
        _p: &str,
        _h: agentos_core::traits::LifecycleHook,
        _c: &agentos_core::traits::HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
}

fn foreach_loop_config(max_concurrency: i32) -> LoopConfig {
    LoopConfig {
        enabled: true,
        max_iterations: -1,
        over: Some("state.raw_tool_calls".into()),
        as_iter: Some("current_call".into()),
        max_concurrency,
        collect: vec![
            "tool_results".into(),
            "_full_tool_results".into(),
            "_executed_tool_calls".into(),
        ],
        collect_append: vec!["submitted_task_ids".into()],
        consume: true,
        consume_keys: vec!["pre_decided_results".into()],
    }
}

fn foreach_step(loop_config: LoopConfig) -> PipelineStep {
    PipelineStep {
        id: "tool_exec".into(),
        steps: vec!["tc".into()],
        when: None,
        context: HashMap::new(),
        routes: vec![],
        loop_config: Some(loop_config),
    }
}

fn foreach_pipeline(step: PipelineStep) -> PipelineConfig {
    PipelineConfig {
        name: "foreach_test".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![step],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    }
}

fn foreach_initial(calls: &[&str]) -> serde_json::Value {
    let calls: Vec<_> = calls
        .iter()
        .map(|c| json!({"name": "bash", "id": c}))
        .collect();
    json!({
        "raw_tool_calls": calls,
        "pre_decided_results": [
            {"call_id": "a", "tool_name": "bash", "success": false, "error": "denied"}
        ],
        "submitted_task_ids": ["t0"],
    })
}

struct CrossRoundInvoker {
    feed_no: AtomicUsize,
    stop_no: AtomicUsize,
    /// 每次 feed 调用捕获的 state.submitted_task_ids（跨轮累计的插件视角观察）。
    seen_submitted: Mutex<Vec<serde_json::Value>>,
}

#[async_trait]
impl PluginInvoker for CrossRoundInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        match plugin_id {
            "feed" => {
                self.seen_submitted.lock().unwrap().push(
                    ctx.state
                        .get("submitted_task_ids")
                        .cloned()
                        .unwrap_or(serde_json::Value::Null),
                );
                let n = self.feed_no.fetch_add(1, Ordering::SeqCst);
                let src = if n == 0 {
                    vec![
                        json!({"name": "bash", "id": "a"}),
                        json!({"name": "bash", "id": "b"}),
                    ]
                } else {
                    vec![json!({"name": "bash", "id": "c"})]
                };
                Ok(PluginResult {
                    state_updates: updates(&[("raw_tool_calls", json!(src))]),
                    ..Default::default()
                })
            }
            "tc" => {
                let call = ctx
                    .state
                    .get("current_call")
                    .cloned()
                    .unwrap_or(serde_json::Value::Null);
                Ok(PluginResult {
                    state_updates: updates(&[
                        ("tool_results", json!([{ "echo": call }])),
                        ("submitted_task_ids", json!([call])),
                    ]),
                    ..Default::default()
                })
            }
            "stopper" => {
                let n = self.stop_no.fetch_add(1, Ordering::SeqCst);
                let mut ups = HashMap::new();
                if n >= 1 {
                    ups.insert("should_stop".into(), json!(true));
                }
                Ok(PluginResult {
                    state_updates: ups,
                    ..Default::default()
                })
            }
            _ => Ok(PluginResult::default()),
        }
    }
    async fn invoke_tool(
        &self,
        _p: &str,
        _t: &str,
        _i: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        Ok(ToolExecutionResult::success(serde_json::Value::Null))
    }
    async fn send_lifecycle_hook(
        &self,
        _p: &str,
        _h: agentos_core::traits::LifecycleHook,
        _c: &agentos_core::traits::HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
}

struct EnsureRecordingInvoker {
    events: Mutex<Vec<String>>,
}

#[async_trait]
impl PluginInvoker for EnsureRecordingInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        _ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        self.events
            .lock()
            .unwrap()
            .push(format!("invoke:{plugin_id}"));
        Ok(PluginResult::default())
    }
    async fn invoke_tool(
        &self,
        _p: &str,
        _t: &str,
        _i: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        Ok(ToolExecutionResult::success(serde_json::Value::Null))
    }
    async fn send_lifecycle_hook(
        &self,
        _p: &str,
        _h: agentos_core::traits::LifecycleHook,
        _c: &agentos_core::traits::HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
    async fn ensure_hosts_ready(&self, plugin_ids: &[String]) {
        let mut ids = plugin_ids.to_vec();
        ids.sort();
        self.events
            .lock()
            .unwrap()
            .push(format!("ensure:{}", ids.join(",")));
    }
}

use crate::round_events::{RoundEnd, RoundEvents, RoundStart};

use agentos_core::types::{PendingInputRecord, PendingInputSource};

struct InjectScenarioInvoker {
    counter: Arc<AtomicUsize>,
    results: Vec<HashMap<String, serde_json::Value>>,
    seen_messages: Mutex<Vec<Vec<serde_json::Value>>>,
    store: Option<Arc<dyn StorageBackend>>,
    enqueue_tenant: String,
    /// call 序号 → invoke 前入队的记录（模拟 LLM 调用期间新消息到达）。
    enqueue_on_call: HashMap<usize, PendingInputRecord>,
}

#[async_trait]
impl PluginInvoker for InjectScenarioInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        let n = self.counter.fetch_add(1, Ordering::SeqCst);
        if let (Some(store), Some(rec)) = (self.store.as_ref(), self.enqueue_on_call.get(&n)) {
            store
                .enqueue_pending_input(&self.enqueue_tenant, rec)
                .await
                .expect("scenario enqueue should succeed");
        }
        let msgs = ctx
            .state
            .get("messages")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        self.seen_messages.lock().unwrap().push(msgs);
        let updates = self.results.get(n).cloned().unwrap_or_default();
        Ok(PluginResult {
            state_updates: updates,
            ..Default::default()
        })
    }
    async fn invoke_tool(
        &self,
        _p: &str,
        _t: &str,
        _i: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        Ok(ToolExecutionResult::success(serde_json::Value::Null))
    }
    async fn send_lifecycle_hook(
        &self,
        _p: &str,
        _h: agentos_core::traits::LifecycleHook,
        _c: &agentos_core::traits::HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
}

fn pending_record(
    pipeline: &str,
    content: &str,
    cmid: &str,
    created_at: &str,
) -> PendingInputRecord {
    PendingInputRecord {
        id: format!("p_{cmid}"),
        tenant_id: "tenant_inject".to_string(),
        created_at: created_at.to_string(),
        request: agentos_core::types::DispatchRequest {
            thread_id: "thread_inject".to_string(),
            user_id: "u1".to_string(),
            pipeline_id: pipeline.to_string(),
            content: content.to_string(),
            client_message_id: cmid.to_string(),
            source: PendingInputSource::User,
            execution_context: None,
            state_overlay: None,
            pipeline_config_id: None,
        },
    }
}

fn injection_loop_body(steps: Vec<StepItem>) -> PipelineConfig {
    PipelineConfig {
        name: "inject_loop".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "post".into(),
                when: None,
                steps,
                context: HashMap::new(),
                routes: vec![
                    Route {
                        when: "flag == true".into(),
                        then: agentos_core::types::RouteAction {
                            next: RouteNext::Loop,
                            set: HashMap::new(),
                        },
                    },
                    Route {
                        when: "True".into(),
                        then: agentos_core::types::RouteAction {
                            next: RouteNext::End,
                            set: HashMap::new(),
                        },
                    },
                ],
                loop_config: None,
            }],
            while_cond: Some("True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    }
}

struct RoundEndCapture {
    ends: Mutex<Vec<RoundEnd>>,
}

impl RoundEvents for RoundEndCapture {
    fn on_round_start(
        &self,
        _ev: RoundStart,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + '_>> {
        Box::pin(async {})
    }
    fn on_round_end(
        &self,
        ev: RoundEnd,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + '_>> {
        self.ends.lock().unwrap().push(ev);
        Box::pin(async {})
    }
}

struct ConsumedCapture {
    events: Mutex<Vec<crate::round_events::PendingConsumed>>,
}

impl RoundEvents for ConsumedCapture {
    fn on_round_start(
        &self,
        _ev: RoundStart,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + '_>> {
        Box::pin(async {})
    }
    fn on_round_end(
        &self,
        _ev: RoundEnd,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + '_>> {
        Box::pin(async {})
    }
    fn on_pending_inputs_consumed(
        &self,
        ev: crate::round_events::PendingConsumed,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + '_>> {
        self.events.lock().unwrap().push(ev);
        Box::pin(async {})
    }
}

// ── LLM 连败熔断（llm_fail_loop，装机 2026-11-18 634adfc6a364 家族根修）──

/// LLM 类声明（metrics_class=llm）插件连败达阈值 → 署名 llm_fail_loop 终态化，
/// 不再继续烧轮（防线缺口实证：连败 169 次无熔断，全家挂起 80+ 分钟）。
#[tokio::test]
async fn llm_fail_loop_breaker_terminates_run_at_threshold() {
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_err(
        "llm_core",
        PluginError {
            message: "LLM provider down".into(),
            code: None,
            source: None,
        },
    );
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage::default());
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("tenant_test", "session_test"),
        ["llm_core"].iter().map(|s| s.to_string()),
        store,
        "run_llm_breaker",
    )
    .with_metrics_classes([("llm_core".to_string(), "llm".to_string())])
    .with_llm_fail_loop_max(3);
    let compiled = compile_pipeline(
        &always_loop_body("llm_core"),
        &StepLibrary::default(),
        &executor.plugin_ids,
    )
    .expect("compile should succeed");
    let final_state = executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("breaker 经署名终态（非 Err 上抛）");

    assert_eq!(final_state["ended"], json!(true), "连败达阈值必须署名终态");
    assert_eq!(
        final_state["router.stop_reason"],
        json!("llm_fail_loop"),
        "终态原因 = 引擎 LLM 连败熔断署名"
    );
    assert_eq!(
        invoker.call_count("llm_core"),
        3,
        "达阈值即终止，不得继续烧轮"
    );
}

/// 业务失败形态（PluginResult.error，非 invoker Err）同计连败：两类失败
/// 出口（invoke_item_plugin Ok-with-error / Err）都进熔断计数。
#[tokio::test]
async fn llm_fail_loop_counts_business_failures_too() {
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result(
        "llm_core",
        PluginResult {
            error: Some(PluginError {
                message: "llm.complete_stream 执行失败".into(),
                code: Some("MCP_CALL_FAILED".into()),
                source: None,
            }),
            ..Default::default()
        },
    );
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage::default());
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("tenant_test", "session_test"),
        ["llm_core"].iter().map(|s| s.to_string()),
        store,
        "run_llm_breaker_biz",
    )
    .with_metrics_classes([("llm_core".to_string(), "llm".to_string())])
    .with_llm_fail_loop_max(2);
    let compiled = compile_pipeline(
        &always_loop_body("llm_core"),
        &StepLibrary::default(),
        &executor.plugin_ids,
    )
    .expect("compile should succeed");
    let final_state = executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run");
    assert_eq!(final_state["router.stop_reason"], json!("llm_fail_loop"));
    assert_eq!(invoker.call_count("llm_core"), 2);
}

/// 成功即清零（自愈不误伤）：败、败、成、败、败、败（阈值 3）——成功轮清零
/// 后须再连败 3 次才熔断（第 6 次调用终止）；若无清零，成功后的第 1 次失败
/// （第 4 次调用）即误触发。防拟合：两个序列形态对「清零与否」可辨。
#[tokio::test]
async fn llm_fail_loop_resets_on_success() {
    let invoker = Arc::new(MockInvoker::new());
    let fail = || PluginResult {
        error: Some(PluginError {
            message: "瞬态失败".into(),
            code: None,
            source: None,
        }),
        ..Default::default()
    };
    invoker.set_sequence(
        "llm_core",
        vec![
            fail(),
            fail(),
            PluginResult::default(), // 成功轮（清零，不置 ended 继续循环）
            fail(),
            fail(),
            fail(), // 清零后第 3 次连败 → 此处熔断
        ],
    );
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage::default());
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("tenant_test", "session_test"),
        ["llm_core"].iter().map(|s| s.to_string()),
        store,
        "run_llm_breaker_reset",
    )
    .with_metrics_classes([("llm_core".to_string(), "llm".to_string())])
    .with_llm_fail_loop_max(3);
    let compiled = compile_pipeline(
        &always_loop_body("llm_core"),
        &StepLibrary::default(),
        &executor.plugin_ids,
    )
    .expect("compile should succeed");
    let final_state = executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run");

    assert_eq!(
        final_state["ended"],
        json!(true),
        "清零后再连败 3 次正常熔断"
    );
    assert_eq!(
        final_state["router.stop_reason"],
        json!("llm_fail_loop"),
        "清零后的新一轮连败达阈值照常熔断"
    );
    assert_eq!(
        invoker.call_count("llm_core"),
        6,
        "第 6 次调用才熔断 = 成功轮确实清零（无清零第 4 次即触发）"
    );
}

/// 非声明类（Other，如编排插件）失败不进熔断计数：连败烧到轮上限报错
/// （call_count=5 证明第 3 轮未被熔断截断——若误计，3 轮即终）。
#[tokio::test]
async fn llm_fail_loop_ignores_non_llm_plugins() {
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_err(
        "orchestrator",
        PluginError {
            message: "编排插件挂了".into(),
            code: None,
            source: None,
        },
    );
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage::default());
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("tenant_test", "session_test"),
        ["orchestrator"].iter().map(|s| s.to_string()),
        store,
        "run_llm_breaker_skip",
    )
    .with_max_rounds(5);
    let compiled = compile_pipeline(
        &always_loop_body("orchestrator"),
        &StepLibrary::default(),
        &executor.plugin_ids,
    )
    .expect("compile should succeed");
    let outcome = executor.run_compiled(&compiled, json!({})).await;
    assert!(
        outcome.is_err(),
        "无 LLM 熔断 → 烧到轮上限按既有 fail-closed 报错"
    );
    assert_eq!(
        invoker.call_count("orchestrator"),
        5,
        "非 LLM 类连败不得被熔断提前截断（3 轮即终 = 误计）"
    );
}

/// env 阈值解析三支（持锁防并行竞态）：合法覆盖 / 非法回退 / 未设缺省。
#[test]
fn llm_fail_loop_max_env_resolution_branches() {
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    let _guard = ENV_LOCK.lock().unwrap();
    let original = std::env::var(ENV_LLM_FAIL_LOOP_MAX).ok();

    std::env::set_var(ENV_LLM_FAIL_LOOP_MAX, "8");
    assert_eq!(
        PipelineExecutor::resolve_llm_fail_loop_max(),
        8,
        "合法正整数覆盖缺省"
    );

    std::env::set_var(ENV_LLM_FAIL_LOOP_MAX, "0");
    assert_eq!(
        PipelineExecutor::resolve_llm_fail_loop_max(),
        DEFAULT_LLM_FAIL_LOOP_MAX,
        "0 = 非法（无禁用出口），回退缺省"
    );

    std::env::set_var(ENV_LLM_FAIL_LOOP_MAX, "not-a-number");
    assert_eq!(
        PipelineExecutor::resolve_llm_fail_loop_max(),
        DEFAULT_LLM_FAIL_LOOP_MAX,
        "非整数回退缺省"
    );

    match original {
        Some(v) => std::env::set_var(ENV_LLM_FAIL_LOOP_MAX, v),
        None => std::env::remove_var(ENV_LLM_FAIL_LOOP_MAX),
    }
}

/// builder 注入 0 同样回退缺省（与 env 口径一致，无禁用出口）。
#[test]
fn llm_fail_loop_max_builder_zero_falls_back_to_default() {
    let invoker = Arc::new(MockInvoker::new());
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage::default());
    let executor = PipelineExecutor::new(
        invoker as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("t", "s"),
        std::iter::empty::<String>(),
        store,
        "r_builder_zero",
    )
    .with_llm_fail_loop_max(0);
    assert_eq!(executor.llm_fail_loop_max, DEFAULT_LLM_FAIL_LOOP_MAX);
}

mod messages_and_recovery;
mod persist_and_gates;
mod pipeline_core;
