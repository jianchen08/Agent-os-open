// @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: rust-test
// 由 pipeline_loop.rs 的主 #[cfg(test)] 测试块体平移而来（保留私有项访问）。

use super::*;
use crate::compiler::{compile_pipeline, HookFile};
use crate::SqliteStore;
use agentos_core::traits::{
    HostType, ManifestCapabilities, PluginManifest, PluginType, StepCapability, ToolCapability,
};
use agentos_core::types::{LoopBody, PipelineConfig, PipelineStep, Route, StepItem, StepLibrary};
use async_trait::async_trait;
use serde_json::json;
use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

use agentos_core::traits::StorageBackend;
use agentos_core::types::{
    CheckpointConfig, MessageRecord, RunRecord, RunStatus, ToolExecutionResult, TraceEntry,
};

// ── 测试基础设施 ──────────────────────────────────────────

/// 可编程的 MockInvoker：按 plugin_id 返回预设的 PluginResult。
/// 同时统计每个插件被调用的次数 + 捕获每次收到 ctx.config（inputs 通道测试用）。
struct MockInvoker {
    results: Mutex<HashMap<String, PluginResult>>,
    calls: Mutex<HashMap<String, usize>>,
    configs: Mutex<Vec<(String, serde_json::Value)>>,
    /// 预设 invoker 级 Err（模拟 sidecar 不可达等 invoker 自身报错）。
    errs: Mutex<HashMap<String, PluginError>>,
}

impl MockInvoker {
    fn new() -> Self {
        Self {
            results: Mutex::new(HashMap::new()),
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

/// 空 StorageBackend，仅用于构造 ContentLoader。
/// 记录收到的 `save_checkpoint` step_no（checkpoint 计步测试读取；其余测试不关心，
/// 默认 trait 实现为 no-op，这里显式 override 以便观察按步计数触发时机）。
struct NullStorage {
    checkpoints: Mutex<Vec<i64>>,
    trace_plugin_ids: Mutex<Vec<String>>,
    /// true 时 set_run_pipeline 返回 Err（persist_run_start 持久化故障注入）。
    fail_set_run_pipeline: std::sync::atomic::AtomicBool,
    /// 收到的 update_run_status 终态序列（终态映射表测试读取）。
    run_statuses: Mutex<Vec<RunStatus>>,
}

impl Default for NullStorage {
    fn default() -> Self {
        Self {
            checkpoints: Mutex::new(Vec::new()),
            trace_plugin_ids: Mutex::new(Vec::new()),
            fail_set_run_pipeline: std::sync::atomic::AtomicBool::new(false),
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

    /// 收到的 run 终态（update_run_status 调用序）。
    fn recorded_run_statuses(&self) -> Vec<RunStatus> {
        self.run_statuses.lock().unwrap().clone()
    }
}

#[async_trait]
impl StorageBackend for NullStorage {
    async fn set_run_pipeline(
        &self,
        _run_id: &str,
        _pipeline_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        if self
            .fail_set_run_pipeline
            .load(std::sync::atomic::Ordering::SeqCst)
        {
            return Err(agentos_core::types::StorageError::Database(
                "injected set_run_pipeline failure".into(),
            ));
        }
        Ok(())
    }
    async fn get_run(&self, _run_id: &str) -> Result<RunRecord, agentos_core::types::StorageError> {
        Err(agentos_core::types::StorageError::NotFound("null".into()))
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
        self.trace_plugin_ids.lock().unwrap().push(entry.plugin_id);
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
    async fn update_run_status(
        &self,
        _run_id: &str,
        status: RunStatus,
        _branch: Option<&str>,
        _seq: Option<u32>,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.run_statuses.lock().unwrap().push(status);
        Ok(())
    }
    async fn create_run(
        &self,
        _run_id: &str,
        _config_hash: &str,
        _tenant_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
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

/// 测试夹具：构造一个 PipelineExecutor + MockInvoker（可拿引用设置结果）。
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
            "main",
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

/// 构造简单的 atomic step（引用一个插件名）。
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

/// state_updates 辅助构造。
fn updates(pairs: &[(&str, serde_json::Value)]) -> HashMap<String, serde_json::Value> {
    pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.clone()))
        .collect()
}

// ── G9 step 级 when 门：invoke 前求值，假则零调用 ──

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

#[tokio::test]
async fn when_gate_false_skips_item_zero_calls() {
    let fixture = Fixture::build(&["a", "b"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("a_val", json!(1))]),
            ..Default::default()
        },
    );
    let config = gated_body(vec![
        StepItem::Bare("a".into()),
        StepItem::Gated {
            name: "b".into(),
            when: Some("False".into()),
            inputs: HashMap::new(),
        },
    ]);
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(fixture.invoker.call_count("a"), 1);
    assert_eq!(
        fixture.invoker.call_count("b"),
        0,
        "when=False 的项必须零调用"
    );
    assert_eq!(final_state["a_val"], json!(1));
    assert!(final_state.get("b_val").is_none());
}

#[tokio::test]
async fn when_gate_true_runs_item() {
    let fixture = Fixture::build(&["a"]);
    let config = gated_body(vec![StepItem::Gated {
        name: "a".into(),
        when: Some("True".into()),
        inputs: HashMap::new(),
    }]);
    fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(fixture.invoker.call_count("a"), 1, "when=True 正常执行");
}

#[tokio::test]
async fn invalid_when_expression_fails_compilation() {
    // G10 语义升级：when 语法错误在加载期编译时暴露（不再静默 false 跳过）。
    // 兼容路径 run() 现场编译 → 返回 Err，调用方如实报错。
    let fixture = Fixture::build(&["a"]);
    let config = gated_body(vec![StepItem::Gated {
        name: "a".into(),
        when: Some("this is ((( invalid".into()),
        inputs: HashMap::new(),
    }]);
    let err = compile_pipeline(
        &config,
        &StepLibrary::default(),
        &fixture.executor.plugin_ids,
    )
    .expect_err("invalid when 应在编译期报错");
    assert!(err.to_string().contains("when"), "err: {err}");
}

#[tokio::test]
async fn group_when_false_skips_whole_step() {
    let fixture = Fixture::build(&["a", "b"]);
    let mut config = gated_body(vec![StepItem::Bare("a".into()), StepItem::Bare("b".into())]);
    config.loop_bodies[0].steps[0].when = Some("False".into());
    fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(
        fixture.invoker.call_count("a"),
        0,
        "组级 when=False 组内零调用"
    );
    assert_eq!(fixture.invoker.call_count("b"), 0);
}

#[tokio::test]
async fn gate_reads_state_updated_by_earlier_item() {
    // 门在到达时对 state 求值：前一项把 go 置 True 后，后一项的门放行
    let fixture = Fixture::build(&["a", "b"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("go", json!(true))]),
            ..Default::default()
        },
    );
    let config = gated_body(vec![
        StepItem::Bare("a".into()),
        StepItem::Gated {
            name: "b".into(),
            when: Some("go == True".into()),
            inputs: HashMap::new(),
        },
    ]);
    fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(fixture.invoker.call_count("a"), 1);
    assert_eq!(
        fixture.invoker.call_count("b"),
        1,
        "前项更新 state 后门应放行"
    );
}

// ── per-plugin inputs（config 通道，不进 state / 不落 trace）────────

#[tokio::test]
async fn step_item_inputs_reach_plugin_via_config_without_state_or_trace() {
    // step 项声明 inputs → 插件经 config["inputs"] 收到；不 merge 进 state、
    // 不产生 step diff（插件无其它 state_updates → 0 trace）。
    let fixture = Fixture::build(&["a"]);
    let config = gated_body(vec![StepItem::Gated {
        name: "a".into(),
        when: None,
        inputs: HashMap::from([("mode".into(), json!("strict")), ("limit".into(), json!(5))]),
    }]);
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(
        fixture.invoker.captured_configs("a"),
        vec![json!({ "inputs": { "mode": "strict", "limit": 5 } })],
        "插件应经 config.inputs 收到 step 项声明的输入"
    );
    // 不污染 state：无 inputs / mode / limit 顶层键
    assert!(final_state.get("inputs").is_none());
    assert!(final_state.get("mode").is_none());
    assert!(final_state.get("limit").is_none());
    // 插件无产出 + inputs 不进 diff → 无 step 窗口轨迹（引擎窗口另计）
    assert!(
        fixture.store.step_trace_plugin_ids().is_empty(),
        "inputs 不得产生 step 轨迹"
    );
}

// ── checkpoint 按「配置 step」计数（组级 when 跳过的 step 不计）─────

#[tokio::test]
async fn checkpoint_counts_configured_steps_group_skipped_excluded() {
    // 单次 body，4 个配置 step（s2 组级 when=False 跳过）；interval_steps=2 →
    // 按步：a(1) → c(2) 恰好触发一次 save(step_no=2)；s2 不计步。
    let fixture = Fixture::build(&["a", "b", "c", "d"]);
    let mut config = PipelineConfig {
        name: "ckpt".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![
                atomic_step("s1", "a"),
                atomic_step("s2", "b"),
                atomic_step("s3", "c"),
                atomic_step("s4", "d"),
            ],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        initial_state: HashMap::new(),
        max_rounds: None,
        checkpoint: CheckpointConfig {
            enabled: true,
            interval_steps: 2,
        },
    };
    // 组级 when=False：s2（→b）整个跳过，不计步
    config.loop_bodies[0].steps[1].when = Some("False".into());
    fixture
        .run(
            &config,
            &StepLibrary::default(),
            json!({ "pipeline_id": "p1" }),
        )
        .await;
    // 执行序列：a(1) → [s2 跳过] → c(2) → d(3)。interval=2 → 第 2 步触发一次
    // 中段存档（step_no=2）；run 结束时 persist_run_end 再落最终态（step_no=3）。
    assert_eq!(
            fixture.store.saved_step_nos(),
            vec![2, 3],
            "按步计：第 2 个实际执行的配置 step 触发中段存档；组级 when 跳过的 s2 不计；末尾为 run 结束兜底存档"
        );
    assert_eq!(fixture.store.save_count(), 2, "中段一次 + run 结尾兜底一次");
}

#[tokio::test]
async fn checkpoint_counts_steps_across_loop_rounds_not_rounds() {
    // 每轮 3 个配置 step，循环 2 轮 = 6 步；interval_steps=4 → 按步在第 4 个
    // step 触发一次存档（旧按轮：每 4 轮=12 步才触发）。counting invoker
    // 在第 6 次调用后 set ended 终止：r1 a/b/c + r2 a/b/c。
    let invoker = Arc::new(CountingInvoker {
        counter: Arc::new(AtomicUsize::new(0)),
        stop_after: 6,
        set_suspended_after: 0,
    });
    let store = Arc::new(NullStorage::default());
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("t", "s"),
        ["a", "b", "c"].iter().map(|s| s.to_string()),
        store.clone() as Arc<dyn StorageBackend>,
        "r",
        "b",
    );
    let config = PipelineConfig {
        name: "ckpt_loop".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![
                atomic_step("s1", "a"),
                atomic_step("s2", "b"),
                atomic_step("s3", "c"),
            ],
            while_cond: Some("True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        initial_state: HashMap::new(),
        max_rounds: None,
        checkpoint: CheckpointConfig {
            enabled: true,
            interval_steps: 4,
        },
    };
    executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
                .expect("compile should succeed"),
            json!({ "pipeline_id": "p1" }),
        )
        .await
        .expect("run ok");
    assert_eq!(
            store.saved_step_nos(),
            vec![4, 6],
            "按步计跨轮：第 4 个执行 step 触发中段存档（旧按轮则此时远未到阈值）；末尾 6 = run 结束兜底存档"
        );
}

// ── 项级 when 跳过：零调用 + 整 step 无产出则不落 trace ────────────

#[tokio::test]
async fn when_gate_skipped_item_leaves_no_trace_and_no_call() {
    // a 有 state_updates 产出，但 when=False 跳过 → a 零调用；step 无 context
    // 变更 + 全部项被跳过 = 空 diff → persist_step_trace 早退不落 trace。
    let fixture = Fixture::build(&["a"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("a_val", json!(1))]),
            ..Default::default()
        },
    );
    let config = gated_body(vec![StepItem::Gated {
        name: "a".into(),
        when: Some("False".into()),
        inputs: HashMap::new(),
    }]);
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(fixture.invoker.call_count("a"), 0, "when=False 零调用");
    assert!(final_state.get("a_val").is_none());
    assert!(
        fixture.store.step_trace_plugin_ids().is_empty(),
        "整 step 被 when 门架空 → 不落 step 轨迹"
    );
}

/// 计数式 invoker：每次调用计数 +1，可选在第 N 次后 set `ended` 或 `suspended`。
/// 用于验证循环 / 挂起逻辑。
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

/// 用给定 invoker + plugin_ids 构造一个 PipelineExecutor（NullStorage 后端）。
fn make_executor(invoker: Arc<dyn PluginInvoker>, plugin_ids: &[&str]) -> PipelineExecutor {
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage::default());
    PipelineExecutor::new(
        invoker,
        PathBuf::from("."),
        TenantContext::new("t", "s"),
        plugin_ids.iter().map(|s| s.to_string()),
        store,
        "r",
        "b",
    )
}

/// 按调用次数依次返回预设 state_updates 的 invoker（超出后返回空）。
/// 用于模拟插件跨轮行为序列（如置位 → 清除标志）。
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

// ── 测试用例 ──────────────────────────────────────────────

#[tokio::test]
async fn test_single_step_atomic() {
    // 一个 step 引用原子插件；验证插件被调用 + state_updates 被 merge
    let fixture = Fixture::build(&["echo"]);
    fixture.invoker.set_result(
        "echo",
        PluginResult {
            state_updates: updates(&[("reply", json!("hello"))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "single".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![atomic_step("s1", "echo")],

            while_cond: None,
            exit_routes: vec![],

            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let library = StepLibrary::default();
    let final_state = fixture.run(&config, &library, json!({})).await;
    assert_eq!(fixture.invoker.call_count("echo"), 1);
    assert_eq!(final_state["reply"], json!("hello"));
    // ended 默认 false
    assert_eq!(final_state["ended"], json!(false));
}

#[tokio::test]
async fn test_composite_step() {
    // 一个组合 step 引用两个子 step（命中规则①：当前管道 step id），验证递归执行。
    //
    // 注意 spec 语义：execute_steps 会遍历 config.steps 中【所有】step，
    // 而 find_step 也在 config.steps 中查。如果 child_a/child_b 同时在 config.steps 里，
    // 它们会被顶层 execute_steps 跑一次，又被 parent 的递归引用跑一次（共 2 次）。
    // 为了精确验证"递归执行"（而不是重复触发），这里只把 parent 放进 config.steps，
    // 子 step 放进公共 step 库——它们只能通过 parent 的引用被命中（命中规则②）。
    let fixture = Fixture::build(&["a", "b"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("a_val", json!(1))]),
            ..Default::default()
        },
    );
    fixture.invoker.set_result(
        "b",
        PluginResult {
            state_updates: updates(&[("b_val", json!(2))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "composite".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![PipelineStep {
                id: "parent".into(),
                steps: vec!["child_a".into(), "child_b".into()],
                when: None,
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
    };
    let mut library = StepLibrary::default();
    library
        .steps
        .insert("child_a".to_string(), atomic_step("child_a", "a"));
    library
        .steps
        .insert("child_b".to_string(), atomic_step("child_b", "b"));
    let final_state = fixture.run(&config, &library, json!({})).await;
    assert_eq!(fixture.invoker.call_count("a"), 1);
    assert_eq!(fixture.invoker.call_count("b"), 1);
    assert_eq!(final_state["a_val"], json!(1));
    assert_eq!(final_state["b_val"], json!(2));
}

#[tokio::test]
async fn test_composite_step_double_trigger_semantics() {
    // 补充验证 spec 语义：如果子 step 同时在 config.steps 里，会被执行 2 次
    // （顶层 execute_steps 一次 + parent 递归一次）。这验证"重复触发"是 spec 的
    // 既定行为，而非 bug。
    let fixture = Fixture::build(&["a"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("a_val", json!(1))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "double".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![
                PipelineStep {
                    id: "parent".into(),
                    steps: vec!["child".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                },
                atomic_step("child", "a"),
            ],

            while_cond: None,
            exit_routes: vec![],

            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let _ = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    // 顶层执行 parent（递归触发 child 1 次）+ 顶层执行 child（1 次）= 2 次
    assert_eq!(fixture.invoker.call_count("a"), 2);
}

#[tokio::test]
async fn test_step_library() {
    // step 引用公共 step 库的 id（命中规则②）
    let fixture = Fixture::build(&["lib_plugin"]);
    fixture.invoker.set_result(
        "lib_plugin",
        PluginResult {
            state_updates: updates(&[("lib_out", json!("from_library"))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "with_lib".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![atomic_step("caller", "shared_step")],

            while_cond: None,
            exit_routes: vec![],

            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let mut library = StepLibrary::default();
    library.steps.insert(
        "shared_step".to_string(),
        atomic_step("shared_step", "lib_plugin"),
    );
    let final_state = fixture.run(&config, &library, json!({})).await;
    assert_eq!(fixture.invoker.call_count("lib_plugin"), 1);
    assert_eq!(final_state["lib_out"], json!("from_library"));
}

#[tokio::test]
async fn test_loop() {
    // loop.enabled=true，插件每次递增计数器；第 3 次 set ended=true 后停止
    let counter = Arc::new(AtomicUsize::new(0));
    let executor = make_executor(
        Arc::new(CountingInvoker {
            counter: counter.clone(),
            stop_after: 3,
            set_suspended_after: 0, // 不挂起
        }) as Arc<dyn PluginInvoker>,
        &["counter_plugin"],
    );
    let config = PipelineConfig {
        name: "loop_test".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("body", "counter_plugin")],
            while_cond: Some("True".into()), // 恒真循环，靠 ended 退出
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let state = executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
                .expect("compile should succeed"),
            json!({}),
        )
        .await
        .unwrap();
    // 验证循环到第 3 次因 ended 停止
    assert_eq!(counter.load(Ordering::SeqCst), 3);
    assert_eq!(state["round"], json!(3));
    assert_eq!(state["ended"], json!(true));
}

#[tokio::test]
async fn test_routes() {
    // routes when 条件匹配后 set 字段 + ended
    let fixture = Fixture::build(&[]);
    let config = PipelineConfig::single_body(
        "routes",
        None,
        vec![PipelineStep {
            id: "router".into(),
            steps: vec![], // 不调任何插件
            when: None,
            context: HashMap::new(),
            routes: vec![Route {
                when: "core_type == 'tool_execute'".into(),
                then: agentos_core::types::RouteAction {
                    next: RouteNext::End,
                    set: updates(&[("handled", json!(true))]),
                },
            }],
            loop_config: None,
        }],
    );
    let library = StepLibrary::default();
    let state = fixture
        .run(&config, &library, json!({ "core_type": "tool_execute" }))
        .await;
    assert_eq!(state["handled"], json!(true));
    assert_eq!(state["ended"], json!(true));
}

#[tokio::test]
async fn test_routes_wait() {
    // Wait → suspended=true
    let fixture = Fixture::build(&[]);
    let config = PipelineConfig {
        name: "wait".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![PipelineStep {
                id: "w".into(),
                steps: vec![],
                when: None,
                context: HashMap::new(),
                routes: vec![Route {
                    when: "True".into(),
                    then: agentos_core::types::RouteAction {
                        next: RouteNext::Wait,
                        set: HashMap::new(),
                    },
                }],
                loop_config: None,
            }],

            while_cond: None,
            exit_routes: vec![],

            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(state["suspended"], json!(true));
}

#[tokio::test]
async fn test_unknown_reference_fails_compilation() {
    // G10 语义升级：引用不存在的 step/插件在加载期编译时报错（不静默 error log）。
    let fixture = Fixture::build(&[]);
    let config = PipelineConfig {
        name: "miss".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![
                atomic_step("s1", "ghost_step"), // 既不是 step 也不是已知插件
                atomic_step("s2", "ghost_plugin"),
            ],

            while_cond: None,
            exit_routes: vec![],

            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let err = compile_pipeline(
        &config,
        &StepLibrary::default(),
        &fixture.executor.plugin_ids,
    )
    .expect_err("未知引用应在编译期报错");
    assert!(err.to_string().contains("ghost_step"), "err: {err}");
}

#[tokio::test]
async fn test_context_injection() {
    // step.context 的 {{state.xxx}} 被渲染注入到 state
    let fixture = Fixture::build(&["reader"]);
    // 插件读 ctx.state 里的 injected 字段，写回 state 确认注入生效
    fixture.invoker.set_result(
        "reader",
        PluginResult {
            state_updates: updates(&[("seen", json!("{{state.injected}}"))]),
            ..Default::default()
        },
    );
    // 注意：上面结果也会被渲染，但 state_updates merge 时不渲染。
    // 我们改为：context 注入后，直接用 state.injected 验证（不依赖插件）。
    // 这里再写一个 context 注入测试。
    let _ = fixture;
    let fixture2 = Fixture::build(&[]);
    let mut context = HashMap::new();
    context.insert("injected".to_string(), json!("agent={{state.agent_id}}"));
    let config = PipelineConfig::single_body(
        "ctx",
        None,
        vec![PipelineStep {
            id: "ctx_step".into(),
            steps: vec![],
            when: None,
            context,
            routes: vec![Route {
                when: "injected == 'agent=A1'".into(),
                then: agentos_core::types::RouteAction {
                    next: RouteNext::End,
                    set: updates(&[("matched", json!(true))]),
                },
            }],
            loop_config: None,
        }],
    );
    let state = fixture2
        .run(
            &config,
            &StepLibrary::default(),
            json!({ "agent_id": "A1" }),
        )
        .await;
    // injected 模板被渲染为 "agent=A1"
    assert_eq!(state["injected"], json!("agent=A1"));
    // 路由条件命中（证明注入确实发生在路由求值之前）
    assert_eq!(state["matched"], json!(true));
    assert_eq!(state["ended"], json!(true));
}

#[tokio::test]
async fn test_skip_remaining() {
    // 插件返回 skip_remaining=true → 跳过同一 step.steps 中后续项
    let fixture = Fixture::build(&["first", "second"]);
    fixture.invoker.set_result(
        "first",
        PluginResult {
            state_updates: updates(&[("first_done", json!(true))]),
            skip_remaining: true,
            ..Default::default()
        },
    );
    fixture.invoker.set_result(
        "second",
        PluginResult {
            state_updates: updates(&[("second_done", json!(true))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "skip".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![PipelineStep {
                id: "both".into(),
                steps: vec!["first".into(), "second".into()],
                when: None,
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
    };
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(state["first_done"], json!(true));
    // second 不应被调用
    assert_eq!(fixture.invoker.call_count("second"), 0);
    assert!(state.get("second_done").is_none());
}

#[tokio::test]
async fn test_plugin_error_continues() {
    // 插件返回 error → warn + 继续后续 step（不 panic）
    let fixture = Fixture::build(&["bad", "good"]);
    fixture.invoker.set_result(
        "bad",
        PluginResult {
            error: Some(PluginError {
                message: "boom".into(),
                code: None,
                source: None,
            }),
            ..Default::default()
        },
    );
    fixture.invoker.set_result(
        "good",
        PluginResult {
            state_updates: updates(&[("ok", json!(true))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "err".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![PipelineStep {
                id: "seq".into(),
                steps: vec!["bad".into(), "good".into()],
                when: None,
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
    };
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    // good 仍被调用
    assert_eq!(fixture.invoker.call_count("good"), 1);
    assert_eq!(state["ok"], json!(true));
    // 插件错误收集到 _plugin_errors（引擎内部键，api 层提取为 plugin_errors）
    let errs = state["_plugin_errors"]
        .as_array()
        .expect("_plugin_errors 应为数组");
    assert_eq!(errs.len(), 1);
    assert_eq!(errs[0]["plugin_id"], json!("bad"));
    assert_eq!(errs[0]["code"], json!("PLUGIN_EXEC_FAILED"));
    assert_eq!(errs[0]["message"], json!("boom"));
}

#[tokio::test]
async fn test_plugin_error_continues_with_code() {
    // 插件返回带 code 的 error → 收集保留 code（前端通知按 code 渲染）
    let fixture = Fixture::build(&["bad"]);
    fixture.invoker.set_result(
        "bad",
        PluginResult {
            error: Some(PluginError {
                message: "sidecar crashed".into(),
                code: Some("PLUGIN_CRASHED".into()),
                source: Some("plugin".into()),
            }),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "err_code".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s1", "bad")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    let errs = state["_plugin_errors"]
        .as_array()
        .expect("_plugin_errors 应为数组");
    assert_eq!(errs.len(), 1);
    assert_eq!(errs[0]["plugin_id"], json!("bad"));
    assert_eq!(errs[0]["code"], json!("PLUGIN_CRASHED"));
    assert_eq!(errs[0]["message"], json!("sidecar crashed"));
}

#[tokio::test]
async fn test_invoker_error_collected() {
    // invoker 自身报错（如 sidecar 不可达）→ 同样收集到 _plugin_errors
    let fixture = Fixture::build(&["bad"]);
    fixture.invoker.set_err(
        "bad",
        PluginError {
            message: "sidecar unreachable".into(),
            code: Some("MCP_CALL_FAILED".into()),
            source: None,
        },
    );
    let config = PipelineConfig {
        name: "inv_err".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s1", "bad")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    let errs = state["_plugin_errors"]
        .as_array()
        .expect("_plugin_errors 应为数组");
    assert_eq!(errs.len(), 1);
    assert_eq!(errs[0]["plugin_id"], json!("bad"));
    assert_eq!(errs[0]["code"], json!("MCP_CALL_FAILED"));
    assert_eq!(errs[0]["message"], json!("sidecar unreachable"));
}

#[tokio::test]
async fn test_suspended_stops_loop() {
    // 管道级 loop，插件 set suspended=true → 循环停止（不进入下一轮）
    let counter = Arc::new(AtomicUsize::new(0));
    let executor = make_executor(
        Arc::new(CountingInvoker {
            counter: counter.clone(),
            stop_after: 0,
            set_suspended_after: 2,
        }) as Arc<dyn PluginInvoker>,
        &["p"],
    );
    let config = PipelineConfig {
        name: "suspend_loop".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("body", "p")],
            while_cond: Some("True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let state = executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
                .expect("compile should succeed"),
            json!({}),
        )
        .await
        .unwrap();
    // 跑了 2 轮就停
    assert_eq!(counter.load(Ordering::SeqCst), 2);
    assert_eq!(state["suspended"], json!(true));
    // ended 未被设置
    assert_eq!(state["ended"], json!(false));
}

#[tokio::test]
async fn test_dynamic_plugin_name() {
    // step.steps 里含 {{state.core_plugin}} 模板 → 渲染后命中插件
    let fixture = Fixture::build(&["real_core"]);
    fixture.invoker.set_result(
        "real_core",
        PluginResult {
            state_updates: updates(&[("executed", json!(true))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "dyn".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![PipelineStep {
                id: "dyn_step".into(),
                steps: vec!["{{state.core_plugin}}".into()],
                when: None,
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
    };
    let state = fixture
        .run(
            &config,
            &StepLibrary::default(),
            json!({ "core_plugin": "real_core" }),
        )
        .await;
    assert_eq!(fixture.invoker.call_count("real_core"), 1);
    assert_eq!(state["executed"], json!(true));
}

#[tokio::test]
async fn test_non_object_initial_state_becomes_object() {
    // 非对象 initial_state 应被规范化为空对象，不 panic
    let fixture = Fixture::build(&[]);
    let config = PipelineConfig {
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
    };
    let state = fixture
        .run(&config, &StepLibrary::default(), serde_json::Value::Null)
        .await;
    assert!(state.is_object());
    assert_eq!(state["ended"], json!(false));
}

#[tokio::test]
async fn test_route_step_jumps_to_target_step() {
    // G10 语义升级：RouteNext::Step(id) 真跳转——j 命中后跳到 t 执行，
    // 不再写 state.next_step 记号。跳转目标须在本循环体（编译期校验）。
    let fixture = Fixture::build(&["a"]);
    let config = PipelineConfig {
        name: "jump".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),

            steps: vec![
                PipelineStep {
                    id: "j".into(),
                    steps: vec![],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![Route {
                        when: "True".into(),
                        then: agentos_core::types::RouteAction {
                            next: RouteNext::Step("t".into()),
                            set: HashMap::new(),
                        },
                    }],
                    loop_config: None,
                },
                PipelineStep {
                    id: "t".into(),
                    steps: vec!["a".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                },
            ],

            while_cond: None,
            exit_routes: vec![],

            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(
        fixture.invoker.call_count("a"),
        1,
        "j 跳转到 t 后 t 的插件被执行"
    );
    assert!(
        state.get("next_step").is_none(),
        "Step 真跳转不再写 next_step 记号"
    );
}

#[tokio::test]
async fn test_step_route_self_jump_guard_errors() {
    // 恒跳回自身的 step 路由 → 跳转护栏截断为 Err（不无限执行）。
    // 上限 = steps.len()×4（本例 1 步 → max(4,16)=16 次跳转）：
    // 第 17 次跳转超限返回 Err，插件恰好被调用 17 次（每次跳转前执行一次）。
    let counter = Arc::new(AtomicUsize::new(0));
    let executor = make_executor(
        Arc::new(CountingInvoker {
            counter: counter.clone(),
            stop_after: 0,
            set_suspended_after: 0,
        }) as Arc<dyn PluginInvoker>,
        &["p"],
    );
    let mut jumping = atomic_step("looper", "p");
    jumping.routes = vec![Route {
        when: "True".into(),
        then: agentos_core::types::RouteAction {
            next: RouteNext::Step("looper".into()),
            set: HashMap::new(),
        },
    }];
    let config = PipelineConfig {
        name: "self_jump".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![jumping],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let result = executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
                .expect("compile should succeed"),
            json!({}),
        )
        .await;
    let err = result.expect_err("恒自跳路由应被护栏截断为 Err");
    let msg = err.to_string();
    assert!(
        msg.contains("step 级跳转次数超限") && msg.contains("死循环"),
        "错误信息应说明疑似 step 路由死循环：{msg}"
    );
    assert_eq!(
        counter.load(Ordering::SeqCst),
        17,
        "1 步管道上限 16 次跳转，第 17 次截断（每次跳转前插件执行一次）"
    );
}

#[tokio::test]
async fn test_step_level_loop() {
    // step 自带 loop_config（非管道级）：循环执行 step.steps
    let counter = Arc::new(AtomicUsize::new(0));
    let executor = make_executor(
        Arc::new(CountingInvoker {
            counter: counter.clone(),
            stop_after: 3,
            set_suspended_after: 0,
        }) as Arc<dyn PluginInvoker>,
        &["p"],
    );
    let config = PipelineConfig {
        name: "step_loop".into(),
        loop_bodies: vec![LoopBody {
            // 管道级不循环
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "looper".into(),
                steps: vec!["p".into()],
                when: None,
                context: HashMap::new(),
                routes: vec![],
                loop_config: Some(agentos_core::types::LoopConfig {
                    enabled: true,
                    max_iterations: -1,
                }),
            }],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let state = executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
                .expect("compile should succeed"),
            json!({}),
        )
        .await
        .unwrap();
    // step 自带循环跑了 3 轮（第 3 次 set ended）
    assert_eq!(counter.load(Ordering::SeqCst), 3);
    assert_eq!(state["ended"], json!(true));
}

// ════════════════════════════════════════════════════════════════
// 双轨收敛（审计变更#1）：engine 不再 per-iteration 注入 agent 配置
// ════════════════════════════════════════════════════════════════

#[tokio::test]
async fn test_loop_does_not_inject_agent_config_per_iteration() {
    // 契约（双轨收敛）：agent 全量配置唯一事实源 = context_build 插件；
    // 内核 engine 不再每轮迭代把 agent yaml 注入 state（即使存在
    // config/agents/<id>.yaml），也不打 _agent_config_missing 标记。
    let temp = tempfile::tempdir().unwrap();
    let agents_dir = temp.path().join("agents");
    std::fs::create_dir_all(&agents_dir).unwrap();
    std::fs::write(
        agents_dir.join("reload_test.yaml"),
        "system_prompt: 注入的提示词\ncustom: hello\n",
    )
    .unwrap();

    let fixture = Fixture::build(&["noop"]);
    fixture.invoker.set_result(
        "noop",
        PluginResult {
            state_updates: updates(&[("ended", json!(true))]),
            ..Default::default()
        },
    );

    let executor = make_executor(fixture.invoker.clone(), &["noop"]);

    let config = PipelineConfig {
        name: "reload_test".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s1", "noop")],
            while_cond: Some("True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    // initial_state 只有 agent_id，没有 system_prompt/custom
    let initial = json!({"agent_id": "reload_test"});

    let final_state = executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
                .expect("compile should succeed"),
            initial,
        )
        .await
        .unwrap();

    // loop 已跑过（ended 由插件置位），agent yaml 仍不得注入 state
    assert_eq!(final_state["ended"], json!(true), "loop 应正常执行");
    assert!(
        final_state.get("system_prompt").is_none(),
        "agent 全量配置不得由 engine 注入（context_build 是唯一事实源）"
    );
    assert!(
        final_state.get("_agent_config_missing").is_none(),
        "engine 不读 agent yaml，也不得打配置缺失标记"
    );
}

// ── inject_run_message_id 直接单元测试（A1/E0502 修复区域）──

#[test]
fn test_inject_run_message_id_no_message_id_untouched() {
    // state 无 message_id（或为空）→ ops 原样返回，标志不置位。
    let mut state = json!({"messages": []});
    let ops = vec![json!({"op": "set", "msg": {"role": "assistant", "content": "x"}})];
    let out = inject_run_message_id(&mut state, &ops, -1);
    assert_eq!(out, ops, "无 message_id 时 op 应原样返回");
    assert!(state.get("_assistant_id_assigned").is_none());
}

#[test]
fn test_inject_run_message_id_flag_already_set_untouched() {
    // _assistant_id_assigned=true → 不再注入（每 run 仅一次）。
    let mut state = json!({"message_id": "m_1", "_assistant_id_assigned": true});
    let ops = vec![json!({"op": "set", "msg": {"role": "assistant", "content": "x"}})];
    let out = inject_run_message_id(&mut state, &ops, -1);
    assert_eq!(out, ops);
}

#[test]
fn test_inject_run_message_id_skips_insert_ops() {
    // 非 set 的 op（insert）跳过。
    let mut state = json!({"message_id": "m_2"});
    let ops = vec![json!({"op": "insert", "at": 0, "msg": {"role": "assistant", "content": "x"}})];
    let out = inject_run_message_id(&mut state, &ops, -1);
    assert_eq!(out, ops);
    assert!(state.get("_assistant_id_assigned").is_none());
}

#[test]
fn test_inject_run_message_id_skips_old_slot_and_non_object_msg() {
    // 旧槽位（seq <= entry_max_seq）与 msg 非对象 → 均跳过。
    let mut state = json!({"message_id": "m_3"});
    let ops = vec![
        json!({"op": "set", "seq": 0, "msg": {"role": "assistant", "content": "old"}}),
        json!({"op": "set", "seq": 1, "msg": "not-an-object"}),
        json!({"op": "set", "seq": 2, "msg": null}),
    ];
    let out = inject_run_message_id(&mut state, &ops, 5);
    assert_eq!(out, ops);
    assert!(state.get("_assistant_id_assigned").is_none());
}

#[test]
fn test_inject_run_message_id_skips_msg_with_own_id() {
    // assistant 消息自带 id → 不注入（有权威 id 的不用占位）。
    let mut state = json!({"message_id": "m_4"});
    let ops = vec![json!({
        "op": "set",
        "seq": 7,
        "msg": {"role": "assistant", "content": "x", "id": "own_123"}
    })];
    let out = inject_run_message_id(&mut state, &ops, 1);
    assert_eq!(out, ops);
    assert!(state.get("_assistant_id_assigned").is_none());
}

#[test]
fn test_inject_run_message_id_only_first_assistant_append() {
    // 命中：首个新槽位 assistant 追加 op 挂 _message_id + 置位；同批后续不再注入。
    // 注：注入前引擎已把无 seq 的 set 解析为递增 seq（见 apply_messages_op_update），
    // 故此处传带 seq 的 resolved op。
    let mut state = json!({"message_id": "a_run_9"});
    let ops = vec![
        json!({"op": "set", "seq": 1, "msg": {"role": "user", "content": "q"}}),
        json!({"op": "set", "seq": 2, "msg": {"role": "assistant", "content": "第一轮"}}),
        json!({"op": "set", "seq": 3, "msg": {"role": "assistant", "content": "第二轮"}}),
    ];
    let out = inject_run_message_id(&mut state, &ops, 0);
    assert_eq!(out[0].get("_message_id"), None, "user 不注入");
    assert_eq!(out[1]["_message_id"], "a_run_9", "首个 assistant 注入");
    assert_eq!(out[2].get("_message_id"), None, "同批后续不注入");
    assert_eq!(state["_assistant_id_assigned"], true);
    // 注入只挂 op 上，不改消息体
    assert!(out[1]["msg"].get("id").is_none());
}

// ── apply_slot_ops_to_array 边界分支 ──

#[test]
fn test_apply_slot_ops_insert_shifts_subsequent_seqs() {
    let mut arr = vec![
        json!({"seq": 0, "role": "user"}),
        json!({"seq": 1, "role": "assistant"}),
    ];
    apply_slot_ops_to_array(
        &mut arr,
        &[json!({
            "op": "insert", "at": 0, "msg": {"role": "system"}
        })],
    );
    let seqs: Vec<i64> = arr.iter().map(|m| m["seq"].as_i64().unwrap()).collect();
    assert_eq!(seqs, vec![0, 1, 2], "insert at 0 后段顺延");
    assert_eq!(arr[0]["role"], "system");
}

#[test]
fn test_apply_slot_ops_delete_missing_seq_noop() {
    let mut arr = vec![json!({"seq": 0, "role": "user"})];
    apply_slot_ops_to_array(&mut arr, &[json!({"op": "set", "seq": 5, "msg": null})]);
    assert_eq!(arr.len(), 1, "删除不存在的 seq 应 no-op");
}

#[test]
fn test_apply_slot_ops_ignores_unknown_and_seqless_ops() {
    let mut arr = vec![json!({"seq": 0, "role": "user"})];
    apply_slot_ops_to_array(
        &mut arr,
        &[
            json!({"op": "mystery", "seq": 3, "msg": {"role": "x"}}),
            json!({"op": "set", "msg": {"role": "y"}}),
            json!({"op": "insert", "msg": {"role": "z"}}),
        ],
    );
    assert_eq!(arr.len(), 1, "未知 op / 缺 seq / 缺 at 全部忽略");
}

#[test]
fn test_apply_slot_ops_modify_replaces_same_seq() {
    let mut arr = vec![json!({"seq": 0, "role": "user", "content": "old"})];
    apply_slot_ops_to_array(
        &mut arr,
        &[json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "new"}})],
    );
    assert_eq!(arr.len(), 1);
    assert_eq!(arr[0]["content"], "new");
    assert_eq!(arr[0]["seq"], 0, "modify 保留原 seq");
}

// ── op_ledger_entry 实录降级分支 ──

#[test]
fn test_op_ledger_entry_insert_and_unknown() {
    // insert 实录：{op, at, message_id, blob_id}
    let insert = op_ledger_entry(&json!({
        "op": "insert", "at": 2, "msg": {"role": "assistant", "content": "hi"}
    }));
    let ins = insert.expect("insert 应产生实录");
    assert_eq!(ins["op"], "insert");
    assert_eq!(ins["at"], 2);
    assert!(ins["message_id"].as_str().unwrap().starts_with("mc_"));
    assert!(ins["blob_id"].is_string());

    // 未知 op → 跳过（前向兼容）
    assert!(op_ledger_entry(&json!({"op": "mystery", "seq": 1})).is_none());
}

#[test]
fn test_op_ledger_entry_set_without_msg_ids_null() {
    // set 但 msg 缺失（delete）→ message_id/blob_id 为 null。
    let e = op_ledger_entry(&json!({"op": "set", "seq": 3, "msg": null})).expect("set 应产生实录");
    assert_eq!(e["op"], "set");
    assert_eq!(e["seq"], 3);
    assert!(e["message_id"].is_null());
    assert!(e["blob_id"].is_null());
}

// ── persist_run_start 持久化失败可见性（扫描 2026-08-27 辖区二 Should#2）──

#[tokio::test]
async fn set_run_pipeline_failure_counts_persist_failure_and_run_continues() {
    // set_run_pipeline（run↔pipeline 归属登记）失败不得静默：与同函数
    // create_run/link_pipeline_session 同款 warn + persist_failure 计数；
    // 同时执行面不受阻（登记失败只降级归属可查性，不中断本轮）。
    let fixture = Fixture::build(&["a"]);
    fixture
        .store
        .fail_set_run_pipeline
        .store(true, std::sync::atomic::Ordering::SeqCst);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("x", json!(1))]),
            ..Default::default()
        },
    );
    let config = gated_body(vec![StepItem::Bare("a".into())]);
    let initial = json!({ "pipeline_id": "pipe-mapping-x", "session_id": "thread-mapping-x" });

    let final_state = fixture.run(&config, &StepLibrary::default(), initial).await;

    assert_eq!(
        fixture.invoker.call_count("a"),
        1,
        "归属登记失败不得中断本轮执行"
    );
    assert_eq!(final_state["x"], json!(1));
    let snap = fixture.executor.metrics().snapshot();
    assert!(
        snap.persist_failures >= 1,
        "set_run_pipeline 失败必须计入 persist_failure（原 let _ 静默），实际 {}",
        snap.persist_failures
    );
}

// ── 路由机制：条件分支先于兜底 end 命中时 loop 续跑（不绑定特定插件）──

/// 构造带"条件 loop 分支 + 兜底 end"的 step 路由：验证路由按序首中即停、
/// 条件分支命中时循环续跑、未命中时兜底 end。
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

#[tokio::test]
async fn conditional_loop_route_beats_fallback_end() {
    // 条件分支（flag=true）排在兜底 end 之前：命中时 loop 续跑而非 end。
    // 序列模拟插件跨轮行为：第 1 轮置位 flag，第 2 轮清除（恒置位会无限循环）。
    let counter = Arc::new(AtomicUsize::new(0));
    let invoker = Arc::new(SequenceInvoker {
        counter: counter.clone(),
        results: vec![
            updates(&[("flag", json!(true))]),
            updates(&[("flag", json!(false))]),
        ],
    });
    let executor = make_executor(invoker.clone() as Arc<dyn PluginInvoker>, &["p"]);
    let config = routed_loop_body(vec!["p".into()]);
    let state = executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids)
                .expect("compile should succeed"),
            json!({}),
        )
        .await
        .unwrap();
    // 条件分支命中 → 续跑一轮；标志清除后兜底 end
    assert_eq!(counter.load(Ordering::SeqCst), 2);
    assert_eq!(state["ended"], json!(true));
    assert_eq!(state["core_type"], json!("llm_call"));
}

#[tokio::test]
async fn fallback_end_when_condition_never_matches() {
    // 条件分支未命中（flag=false）→ 兜底 end，不无限循环。
    let fixture = Fixture::build(&["p"]);
    fixture.invoker.set_result(
        "p",
        PluginResult {
            state_updates: updates(&[("flag", json!(false))]),
            ..Default::default()
        },
    );
    let config = routed_loop_body(vec!["p".into()]);
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(fixture.invoker.call_count("p"), 1);
    assert_eq!(state["ended"], json!(true));
}

// ── transient 生命周期接线（ADR 2026-08-27 §2.2 生命周期第二/三清 + §2.4 B 区）──
// 引擎侧方法（clear_transient_for_ops/bind_step_message）写进程级
// global_registry 单例（生产路径）；测试用唯一管道 id + 末尾 clear_pipeline
// 防跨测试残留；租户取 Fixture 默认 tenant_test。

#[test]
fn clear_transient_for_ops_clears_chunk_and_binding() {
    let reg = crate::transient::global_registry();
    let pipe = "pipe_clear_ops";
    reg.set(
        "tenant_test",
        pipe,
        "chunk:mid_stream",
        json!({"text_len": 3}),
    );
    reg.set(
        "tenant_test",
        pipe,
        "chunk:mid_plugin",
        json!({"text_len": 5}),
    );
    reg.set("tenant_test", pipe, "progress:1", json!({"pct": 10}));
    reg.register_message_binding("tenant_test", pipe, "mid_stream", "core");
    reg.register_message_binding("tenant_test", pipe, "mid_plugin", "core");

    let executor = Fixture::build(&["a"]).executor;
    let state = json!({
        "pipeline_id": pipe,
        "message_id": "mid_stream",
        "messages": [],
    });
    let ops = vec![
        // 无 msg.id 的 op（A1 注入路径：message_id 在 state 顶层）
        json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "x"}}),
        // 插件路径：消息自带 id（p_ 命名空间）
        json!({"op": "set", "seq": 2, "msg": {"role": "assistant", "content": "y", "id": "mid_plugin"}}),
        // 未知 op 形态（无 id 可提）不 panic
        json!({"op": "bogus"}),
    ];
    executor.clear_transient_for_ops(&state, &ops, "tenant_test");

    // state["message_id"] 对应键 + 消息自带 id 对应键均清
    assert!(reg.get("tenant_test", pipe, "chunk:mid_stream").is_none());
    assert!(reg.get("tenant_test", pipe, "chunk:mid_plugin").is_none());
    // B 区同清
    assert!(reg
        .resolve_step_of("tenant_test", pipe, "mid_stream")
        .is_none());
    assert!(reg
        .resolve_step_of("tenant_test", pipe, "mid_plugin")
        .is_none());
    // 非消息键不受影响（progress 中间态存活）
    assert!(reg.get("tenant_test", pipe, "progress:1").is_some());
    reg.clear_pipeline("tenant_test", pipe);
}

#[test]
fn clear_transient_for_ops_skips_without_pipeline_id() {
    let reg = crate::transient::global_registry();
    let pipe = "pipe_skip_ops";
    reg.set("tenant_test", pipe, "chunk:mid", json!({"text_len": 3}));
    let executor = Fixture::build(&["a"]).executor;
    // state 无 pipeline_id（首轮未注入场景）→ 零操作，不误清其他管道
    executor.clear_transient_for_ops(&json!({"message_id": "mid"}), &[], "tenant_test");
    assert!(reg.get("tenant_test", pipe, "chunk:mid").is_some());
    reg.clear_pipeline("tenant_test", pipe);
}

#[test]
fn bind_step_message_registers_and_guard_clears_on_drop() {
    let reg = crate::transient::global_registry();
    let pipe = "pipe_bind_guard";
    let executor = Fixture::build(&["a"]).executor;
    let state = json!({"pipeline_id": pipe, "message_id": "m1"});
    {
        let _guard = executor.bind_step_message(&state, "core").expect("应登记");
        assert_eq!(
            reg.resolve_step_of("tenant_test", pipe, "m1").as_deref(),
            Some("core")
        );
        // 守卫在作用域内持活：绑定保留
    }
    // Drop：绑定清除
    assert!(reg.resolve_step_of("tenant_test", pipe, "m1").is_none());
    reg.clear_pipeline("tenant_test", pipe);
}

#[test]
fn bind_step_message_none_when_identifiers_missing() {
    let executor = Fixture::build(&["a"]).executor;
    assert!(
        executor.bind_step_message(&json!({}), "core").is_none(),
        "无 id 零操作"
    );
    assert!(
        executor
            .bind_step_message(&json!({"pipeline_id": "p"}), "core")
            .is_none(),
        "缺 message_id 零操作"
    );
}

#[test]
fn step_binding_guard_nested_override_restores_outer() {
    let reg = crate::transient::global_registry();
    let pipe = "pipe_bind_nested";
    let executor = Fixture::build(&["a"]).executor;
    let state = json!({"pipeline_id": pipe, "message_id": "m1"});
    let outer = executor.bind_step_message(&state, "outer").unwrap();
    // 嵌套 step 覆盖登记（composite 递归：内层 step 流式窗口接管归属）
    let inner = executor.bind_step_message(&state, "inner").unwrap();
    assert_eq!(
        reg.resolve_step_of("tenant_test", pipe, "m1").as_deref(),
        Some("inner")
    );
    // 内层守卫收尾：归属恢复为外层 step（流式窗口关闭，执行权回到外层剩余项）
    drop(inner);
    assert_eq!(
        reg.resolve_step_of("tenant_test", pipe, "m1").as_deref(),
        Some("outer"),
        "内层守卫收尾须恢复外层绑定"
    );
    // 外层守卫收尾：首次登记（无 prev）→ 直接清除
    drop(outer);
    assert!(
        reg.resolve_step_of("tenant_test", pipe, "m1").is_none(),
        "外层守卫收尾清除绑定"
    );
    reg.clear_pipeline("tenant_test", pipe);
}

// ── 步骤服务接线（服务化提案 §3.2/§3.4：编译期 method 携带 → 运行期 _step_method）──

/// 测试 manifest（单入口纯步骤插件形状：无 tools/services/steps，隐式默认注册）。
fn test_manifest(id: &str) -> PluginManifest {
    PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: id.to_string(),
        name: format!("Test {id}"),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::Pipeline,
        pipeline_role: None,
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
        granted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        provides: None,
        persistent_fields: vec![],
        export_fields: vec![],
    }
}

/// 工具能力（复合体形状构造用）。
fn tool_cap(name: &str) -> ToolCapability {
    ToolCapability {
        name: name.to_string(),
        description: None,
        input_schema: None,
        output_schema: None,
        category: None,
        ui: None,
        render: None,
        smoke: None,
        timeout_ms: None,
    }
}

/// 具名步骤服务端到端：编译产物携带 method（步骤名）→ 运行期 config 含
/// `_step_method`（SDK 侧按名分发）→ state_updates 正常 merge。
#[tokio::test]
async fn named_step_service_reaches_plugin_via_step_method_config() {
    let fixture = Fixture::build(&["task_service"]);
    fixture.invoker.set_result(
        "task_service",
        PluginResult {
            state_updates: updates(&[("injected", json!(true))]),
            ..Default::default()
        },
    );
    // 复合体显式声明 steps：task.inject_params 是注册步骤名（非插件 id）
    let composite = {
        let mut m = test_manifest("task_service");
        m.capabilities = ManifestCapabilities {
            steps: vec![StepCapability {
                name: "task.inject_params".into(),
                description: None,
                input_schema: None,
            }],
            tools: vec![tool_cap("task_submit")],
            ..Default::default()
        };
        m
    };
    let index = crate::compiler::build_step_service_index(&[composite]).expect("index build ok");
    let config = PipelineConfig {
        name: "p".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s", "task.inject_params")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let compiled = crate::compiler::compile_pipeline_with_hooks(
        &config,
        &StepLibrary::default(),
        &fixture.executor.plugin_ids,
        Some(&index),
        &[],
        &[],
    )
    .expect("具名步骤服务命中编译通过");
    let final_state = fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    // 编译产物 method 携带 + 运行期 config 注入 _step_method（无 inputs → 仅约定键）
    assert_eq!(
        fixture.invoker.captured_configs("task_service"),
        vec![json!({ "_step_method": "task.inject_params" })],
        "具名步骤服务调用 config 必须携带 _step_method"
    );
    assert_eq!(
        final_state["injected"],
        json!(true),
        "具名步骤 state_updates 正常 merge"
    );
}

/// 复合体直引（插件 id 未在 capabilities.steps 声明）→ 编译期报错（fail-closed，
/// 强制自白条款）；错误文案含插件 id 与修复指引。
#[tokio::test]
async fn composite_direct_reference_fails_compilation() {
    let fixture = Fixture::build(&["task_service"]);
    let composite = {
        let mut m = test_manifest("task_service");
        m.capabilities = ManifestCapabilities {
            tools: vec![tool_cap("task_submit")],
            ..Default::default()
        };
        m
    };
    let index = crate::compiler::build_step_service_index(&[composite]).expect("index build ok");
    let config = PipelineConfig {
        name: "p".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s", "task_service")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let err = crate::compiler::compile_pipeline_with_hooks(
        &config,
        &StepLibrary::default(),
        &fixture.executor.plugin_ids,
        Some(&index),
        &[],
        &[],
    )
    .expect_err("复合体直引必须编译期报错");
    assert!(err.message.contains("task_service"), "err: {err}");
    assert!(err.message.contains("capabilities.steps"), "err: {err}");
}

/// 隐式直引（单入口插件，method None = 默认 execute 入口）：config 不带
/// `_step_method` 键——SDK 侧走现行 execute 路径（现状零改动）。
#[tokio::test]
async fn implicit_direct_reference_config_has_no_step_method_key() {
    let fixture = Fixture::build(&["a"]);
    let config = gated_body(vec![StepItem::Bare("a".into())]);
    fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    let captured = fixture.invoker.captured_configs("a");
    assert_eq!(captured.len(), 1, "隐式直引应恰好调用一次");
    assert!(
        captured[0].get("_step_method").is_none(),
        "隐式直引不得携带 _step_method，实际: {}",
        captured[0]
    );
}

// ── hooks 同步边界分发（服务化提案 §3.6：step 级/body 级两档 + terminate）──

/// 构造带 hooks 的已编译管道（step_hooks 键 = "body:step" 复合键）。
fn compiled_with_hooks(
    fixture: &Fixture,
    config: &PipelineConfig,
    body_hooks: &[(String, Vec<HookFile>)],
    step_hooks: &[(String, Vec<HookFile>)],
) -> crate::compiler::CompiledPipeline {
    crate::compiler::compile_pipeline_with_hooks(
        config,
        &StepLibrary::default(),
        &fixture.executor.plugin_ids,
        None,
        body_hooks,
        step_hooks,
    )
    .expect("compile with hooks ok")
}

fn step_hook_file(on: &str, run: &str) -> HookFile {
    HookFile {
        on: on.to_string(),
        run: run.to_string(),
    }
}

/// step 级钩子：该 step 执行时 step_start/step_end 各分发一次（payload 带
/// 步骤复合键与事件名），且只在本 step 触发。
#[tokio::test]
async fn step_level_hook_receives_start_and_end_once() {
    let fixture = Fixture::build(&["a", "watcher"]);
    let config = PipelineConfig {
        name: "p".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s1", "a")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let compiled = compiled_with_hooks(
        &fixture,
        &config,
        &[],
        &[(
            "main:s1".to_string(),
            vec![
                step_hook_file("step_start", "watcher.on_step"),
                step_hook_file("step_end", "watcher.on_step"),
            ],
        )],
    );
    fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    let configs = fixture.invoker.captured_configs("watcher");
    assert_eq!(configs.len(), 2, "step 级钩子恰好分发两次");
    let events: Vec<&str> = configs
        .iter()
        .filter_map(|c| {
            c.get("_pipe_hook")
                .and_then(|h| h.get("event"))
                .and_then(|e| e.as_str())
        })
        .collect();
    assert_eq!(events, vec!["step_start", "step_end"], "start/end 各一次");
    // payload 携带步骤复合键与事件名（最小上下文）
    let payload = &configs[0]["_pipe_hook"]["payload"];
    assert_eq!(payload["step_id"], json!("main:s1"));
    assert_eq!(payload["event"], json!("step_start"));
    assert!(payload.get("timestamp").is_some(), "payload 含时间戳");
}

/// body 级钩子：body 内每个 step 的边界都收到分发（payload.step_id 逐 step 不同）。
#[tokio::test]
async fn body_level_hook_fires_for_every_step_in_body() {
    let fixture = Fixture::build(&["a", "b", "watcher"]);
    let config = PipelineConfig {
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
    };
    let compiled = compiled_with_hooks(
        &fixture,
        &config,
        &[(
            "main".to_string(),
            vec![step_hook_file("step_start", "watcher.on_step")],
        )],
        &[],
    );
    fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    let configs = fixture.invoker.captured_configs("watcher");
    assert_eq!(configs.len(), 2, "body 内两个 step 各收到一次");
    let step_ids: Vec<&str> = configs
        .iter()
        .filter_map(|c| c["_pipe_hook"]["payload"]["step_id"].as_str())
        .collect();
    assert_eq!(
        step_ids,
        vec!["main:s1", "main:s2"],
        "payload 定位到具体 step"
    );
}

/// terminate 决策：钩子返回 {"decision":"terminate"} → 引擎置 ended=true，
/// 当前循环体后续 step 不再执行。
#[tokio::test]
async fn hook_terminate_decision_ends_loop_body() {
    let fixture = Fixture::build(&["a", "b", "watcher"]);
    fixture.invoker.set_result(
        "watcher",
        PluginResult {
            state_updates: updates(&[("decision", json!("terminate"))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
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
    };
    let compiled = compiled_with_hooks(
        &fixture,
        &config,
        &[],
        &[(
            "main:s1".to_string(),
            vec![step_hook_file("step_end", "watcher.on_step")],
        )],
    );
    let final_state = fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    assert_eq!(final_state["ended"], json!(true), "terminate → ended=true");
    assert_eq!(fixture.invoker.call_count("a"), 1, "s1 执行");
    assert_eq!(fixture.invoker.call_count("b"), 0, "s2 被 terminate 截断");
}

/// 分发异常（invoker 错误）不影响主流程：仅 warn，主 step 照常执行、run 成功。
#[tokio::test]
async fn hook_dispatch_failure_does_not_block_main_flow() {
    let fixture = Fixture::build(&["a", "watcher"]);
    fixture.invoker.set_err(
        "watcher",
        PluginError {
            message: "hook sidecar unreachable".into(),
            code: Some("MCP_CALL_FAILED".into()),
            source: Some("test".into()),
        },
    );
    let config = PipelineConfig {
        name: "p".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s1", "a")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let compiled = compiled_with_hooks(
        &fixture,
        &config,
        &[],
        &[(
            "main:s1".to_string(),
            vec![step_hook_file("step_end", "watcher.on_step")],
        )],
    );
    let final_state = fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run 不因钩子分发失败翻车");
    assert_eq!(fixture.invoker.call_count("a"), 1, "主 step 照常执行");
    assert_eq!(
        final_state.get("ended"),
        Some(&json!(false)),
        "分发失败不得误置 ended（run 开头默认种子 false，终止决策才会置 true）"
    );
}

/// 空表零分发：未声明任何钩子的管道，边界分发点零调用。
#[tokio::test]
async fn no_hooks_yields_zero_dispatch() {
    let fixture = Fixture::build(&["a", "watcher"]);
    let config = PipelineConfig {
        name: "p".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![atomic_step("s1", "a")],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    };
    let compiled = compiled_with_hooks(&fixture, &config, &[], &[]);
    fixture
        .executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");
    assert_eq!(
        fixture.invoker.call_count("watcher"),
        0,
        "空 hooks 表零分发（空集短路零开销）"
    );
    assert_eq!(fixture.invoker.call_count("a"), 1, "主流程不受影响");
}

// ── 控制状态键契约（ADR 2026-08-30）：should_stop 折算 / run_started_at / 终态映射 ──

/// 单步恒真循环配置（每轮调用一次 plugin）。
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

#[tokio::test]
async fn should_stop_breaks_loop_and_normalizes_ended() {
    // 插件只写 state：should_stop=true 在轮边界折算为 ended——循环终止、
    // 终态收束走既有 ended 语义。写方未带署名时按未署名兜底映射。
    let fixture = Fixture::build(&["a"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("should_stop", json!(true))]),
            ..Default::default()
        },
    );
    let config = always_loop_body("a");
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(
        fixture.invoker.call_count("a"),
        1,
        "should_stop 当轮折算终止，第 2 轮不再进入"
    );
    assert_eq!(final_state["ended"], json!(true), "折算为 ended 落 state");
    assert_eq!(
        fixture.store.recorded_run_statuses(),
        vec![RunStatus::Completed],
        "未署名的终止请求按缺省映射兜底"
    );
}

#[tokio::test]
async fn stop_reason_signature_maps_run_status() {
    // 终态映射表：谁写终止谁署名（router.stop_reason），引擎只查表——
    // 预算超线署名 budget_exhausted 落 Failed，不再被 user_requested 误标。
    let cases: Vec<(&str, RunStatus)> = vec![
        ("budget_exhausted", RunStatus::Failed),
        ("elapsed_cap", RunStatus::Failed),
        ("timeout", RunStatus::Failed),
        ("task_failed", RunStatus::Failed),
        ("user_requested", RunStatus::Cancelled),
        ("task_cancelled", RunStatus::Cancelled),
        ("task_completed", RunStatus::Completed),
    ];
    for (reason, expected) in cases {
        let fixture = Fixture::build(&["a"]);
        fixture.invoker.set_result(
            "a",
            PluginResult {
                state_updates: updates(&[
                    ("should_stop", json!(true)),
                    ("router.stop_reason", json!(reason)),
                ]),
                ..Default::default()
            },
        );
        let config = always_loop_body("a");
        fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        assert_eq!(
            fixture.store.recorded_run_statuses(),
            vec![expected.clone()],
            "stop_reason={reason} 应映射 {expected:?}"
        );
    }
}

#[tokio::test]
async fn suspended_still_skips_exit_body_and_maps_suspended() {
    // 挂起优先于折算：suspended=true 时整个 run 停止推进、不跑收尾体，
    // 终态落 Suspended（等待恢复）。
    let fixture = Fixture::build(&["a", "exit"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[
                ("should_stop", json!(true)),
                ("suspended", json!(true)),
                ("router.stop_reason", json!("budget_exhausted")),
            ]),
            ..Default::default()
        },
    );
    let mut config = always_loop_body("a");
    config.loop_bodies.push(LoopBody {
        id: "exit".into(),
        steps: vec![atomic_step("exit_finalize", "exit")],
        while_cond: None,
        exit_routes: vec![],
        run_on_error: true,
    });
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(
        fixture.invoker.call_count("exit"),
        0,
        "挂起不跑收尾体（环境保持供恢复）"
    );
    assert_eq!(
        fixture.store.recorded_run_statuses(),
        vec![RunStatus::Suspended]
    );
    assert_eq!(final_state["should_stop"], json!(true));
}

#[tokio::test]
async fn run_started_at_written_per_run() {
    // 耗时锚点：run 初始化写 run_started_at（RFC3339），二次 run 覆盖——
    // track 插件的 elapsed 由此计算，不再继承宿主进程 uptime。
    let fixture = Fixture::build(&["a"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("ended", json!(true))]),
            ..Default::default()
        },
    );
    let config = always_loop_body("a");
    let first = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    let second = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    for state in [&first, &second] {
        let raw = state["run_started_at"]
            .as_str()
            .expect("run_started_at 存在");
        chrono::DateTime::parse_from_rfc3339(raw).expect("RFC3339 可解析");
    }
}

// ── 实际状态 run_status（内核持有，2026-09-03 双状态裁定）─────────────

#[tokio::test]
async fn run_status_terminal_written_from_expected_control_keys() {
    // 预期层插件写 ended=true → 引擎收尾按终态映射单点折算 run_status=completed；
    // state 快照与 runs 表（recorded statuses）同词汇同源。
    let fixture = Fixture::build(&["a"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("ended", json!(true))]),
            ..Default::default()
        },
    );
    let config = gated_body(vec![StepItem::Bare("a".into())]);
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(final_state["run_status"], json!("completed"));
    assert_eq!(
        fixture.store.recorded_run_statuses(),
        vec![RunStatus::Completed],
        "state 实际状态与 runs 表同源同词汇"
    );
}

#[tokio::test]
async fn run_status_reflects_suspension() {
    // 预期层 suspended=true → 实际状态 suspended（挂起在终态映射中优先级最高）
    let fixture = Fixture::build(&["a"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("suspended", json!(true))]),
            ..Default::default()
        },
    );
    let config = gated_body(vec![StepItem::Bare("a".into())]);
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(final_state["run_status"], json!("suspended"));
    assert_eq!(
        fixture.store.recorded_run_statuses(),
        vec![RunStatus::Suspended]
    );
}

#[tokio::test]
async fn run_status_plugin_forgery_dropped_by_terminal_mapping() {
    // 实际状态不可被插件改写：插件伪造 run_status=failed，收束仍由引擎按
    // 控制键折算（ended=true → completed）——伪造值不得存活到终态。
    let fixture = Fixture::build(&["a"]);
    fixture.invoker.set_result(
        "a",
        PluginResult {
            state_updates: updates(&[("run_status", json!("failed")), ("ended", json!(true))]),
            ..Default::default()
        },
    );
    let config = gated_body(vec![StepItem::Bare("a".into())]);
    let final_state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    assert_eq!(
        final_state["run_status"],
        json!("completed"),
        "实际状态只出自终态映射单点，插件伪造值不得落地"
    );
}

#[tokio::test]
async fn merge_drops_run_status_plugin_write() {
    // merge 层拦截（内核持有不变量）：插件直写 run_status 即刻丢弃，state
    // 保持内核写入值；非内核键（ended）照常合并。
    let fixture = Fixture::build(&["a"]);
    let mut state = json!({"run_status": "running"});
    fixture
        .executor
        .merge_and_project(&mut state, &updates(&[("run_status", json!("failed"))]))
        .await;
    assert_eq!(
        state["run_status"],
        json!("running"),
        "插件直写 run_status 必须被丢弃"
    );
    fixture
        .executor
        .merge_and_project(&mut state, &updates(&[("ended", json!(true))]))
        .await;
    assert_eq!(state["ended"], json!(true), "预期层控制键照常合并");
}

// ── P1-4 声明化 volatile 键：checkpoint 快照对比 ─────────────────────

/// checkpoint 剥离集 = 内核自有键（store 内置）∪ 插件声明键（manifest
/// `state.volatile_keys` 声明并集，经 StorageBackend::set_declared_volatile_keys
/// 注入 store），与收窄前内核单表全集行为逐键对账：两类键都不得残留进快照，
/// 持久键照常落档。
#[tokio::test]
async fn run_end_checkpoint_strips_declared_volatile_keys() {
    let sqlite = Arc::new(SqliteStore::open_memory().unwrap());
    // 声明键并集注入（生产侧 api 在 stage_execute 从 manifest 声明收集后传入）。
    sqlite.set_declared_volatile_keys(&[
        "thinking_strength".to_string(),
        "tool_schemas".to_string(),
        "conversation_mode".to_string(),
        "core_type".to_string(),
        "core_plugin".to_string(),
    ]);
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let invoker: Arc<dyn PluginInvoker> = Arc::new(MockInvoker::new());
    let executor = PipelineExecutor::new(
        invoker,
        PathBuf::from("."),
        TenantContext::new("default", "s"),
        std::iter::empty::<String>(),
        store,
        "r_decl",
        "b_decl",
    );
    let final_state = json!({
        "pipeline_id": "pipe_decl",
        "task.status": "running",
        // 内核自有键（store 内置剥离）
        "ended": true,
        "suspended": true,
        "run_id": "old-run",
        "should_stop": true,
        "router.stop_reason": "budget_exhausted",
        // 插件声明键（executor 注入剥离）
        "conversation_mode": true,
        "tool_schemas": [{"name": "x"}],
        "thinking_strength": "high",
        "core_type": "tool_execute",
        "core_plugin": "custom_core_engine",
    });
    executor.persist_run_end(&final_state).await;
    let latest = sqlite
        .load_latest_checkpoint("pipe_decl", "default")
        .unwrap()
        .expect("run 收尾必落 checkpoint");
    let (_, state) = latest;
    for key in [
        "ended",
        "suspended",
        "run_id",
        "should_stop",
        "router.stop_reason",
        "conversation_mode",
        "tool_schemas",
        "thinking_strength",
        "core_type",
        "core_plugin",
    ] {
        assert!(state.get(key).is_none(), "易变键 {key} 不得残留进快照");
    }
    // 持久键照常落档（快照瘦身不吞管道累计状态）
    assert_eq!(state["task.status"], "running");
}

/// 未声明插件键不受剥离影响（声明什么剥什么，缺省零干预）：裸 store
/// 未注入声明键集，conversation_mode 照常落档。
#[tokio::test]
async fn run_end_checkpoint_keeps_keys_without_declaration() {
    let sqlite = Arc::new(SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let invoker: Arc<dyn PluginInvoker> = Arc::new(MockInvoker::new());
    let executor = PipelineExecutor::new(
        invoker,
        PathBuf::from("."),
        TenantContext::new("default", "s"),
        std::iter::empty::<String>(),
        store,
        "r_keep",
        "b_keep",
    );
    let final_state = json!({
        "pipeline_id": "pipe_keep",
        "task.status": "running",
        "conversation_mode": true,
    });
    executor.persist_run_end(&final_state).await;
    let (_, state) = sqlite
        .load_latest_checkpoint("pipe_keep", "default")
        .unwrap()
        .unwrap();
    assert_eq!(
        state["conversation_mode"], true,
        "未声明键不剥离（fail-open 仅对声明面）"
    );
    assert_eq!(state["task.status"], "running");
}

// ── 监控 M2 分类词表：显式映射替代子串猜测（基线锁定）──────────────

/// 基线锁定：`plugin_metrics_class` 对现存插件集的分类结果与原子串启发
/// （2026-09 基线：含 "llm"→Llm；含 "tool"→Tool；其余→Other）完全一致。
/// 期望值逐条显式写死（非测试内重算启发式），表内条目被误删即变红。
#[test]
fn test_plugin_metrics_class_locked_to_current_plugin_set() {
    // LLM 调用面（现存全集）
    for id in ["llm_service", "pipeline_llm_core"] {
        assert_eq!(
            plugin_metrics_class(id),
            PluginMetricsClass::Llm,
            "{id} 必须分类为 Llm（现存插件集基线）"
        );
    }
    // 工具调用面（现存全集）
    for id in [
        "agentos-builtin-tools",
        "bash_tool",
        "download_tool",
        "human_interaction_tool",
        "media_tools",
        "memory_tool",
        "pipeline_tool_cache",
        "pipeline_tool_cache_writer",
        "pipeline_tool_core",
        "pipeline_tool_schema",
        "pipeline_tool_schema_validator",
        "project_create_tool",
        "resource_merge_tool",
        "resource_search_tool",
        "simple_tools",
        "spill_retrieve_tool",
        "task_evaluate_tool",
        "task_manage_tool",
        "task_submit_tool",
        "trigger_setup_tool",
        "web_operate_tool",
    ] {
        assert_eq!(
            plugin_metrics_class(id),
            PluginMetricsClass::Tool,
            "{id} 必须分类为 Tool（现存插件集基线）"
        );
    }
    // 编排/服务面 = Other（不计 LLM/tool；样例覆盖管道输入输出编排、系统服务、
    // 诊断面，含与词表前缀相近的条目如 pipeline_tool_schema 的近邻
    // pipeline_context_window_guard）
    for id in [
        "pipeline_context_build",
        "pipeline_context_window_guard",
        "pipeline_prompt_build",
        "pipeline_stop_check",
        "pipeline_task_reminder",
        "pipeline_multimodal_preprocessor",
        "condition",
        "db_admin",
        "task_service",
        "isolation_service",
        "monitoring",
        "debug_center",
        "user_admin",
    ] {
        assert_eq!(
            plugin_metrics_class(id),
            PluginMetricsClass::Other,
            "{id} 必须分类为 Other（编排/服务插件不计 LLM/tool 指标）"
        );
    }
    // 性质：未登记 id 一律 Other（分类全函数、缺省语义）
    for id in ["", "unknown_plugin", "LLM_SERVICE", "my_toolbox_plugin"] {
        assert_eq!(
            plugin_metrics_class(id),
            PluginMetricsClass::Other,
            "未登记 id {id:?} 必须为 Other（大小写敏感、精确匹配）"
        );
    }
}

/// 词表自身契约：键不重复；不含显式 Other 条目（Other 是缺省，不是登记项）。
#[test]
fn test_metrics_class_table_has_no_duplicate_or_other_entries() {
    let mut keys: Vec<&str> = METRICS_PLUGIN_CLASS.iter().map(|(id, _)| *id).collect();
    keys.sort_unstable();
    let count = keys.len();
    keys.dedup();
    assert_eq!(
        keys.len(),
        count,
        "METRICS_PLUGIN_CLASS 键不得重复：{keys:?}"
    );
    assert!(
        METRICS_PLUGIN_CLASS
            .iter()
            .all(|(_, class)| *class != PluginMetricsClass::Other),
        "Other 是缺省分类，不得显式登记进表"
    );
}

// ── 生命周期事件总线（观察路径）：run 开始/结束进总线 ──────────────
// （自 tests/pipeline_lifecycle_bus_test.rs 平移：llvm-cov 不为集成测试目标
// 产出 SF 段，diff coverage 门禁对 scope 内新增测试文件 fail-loud；内联
// #[cfg(test)] 模块在度量面内，且为本仓 engine 测试主流形态。）

/// 总线测试夹具：executor 注入 HookEventBus（None = 未接线对照组）。
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
        "main",
    )
    .with_hook_bus(bus)
}

/// 单循环体单 step 最小管道。
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

/// 正常完成：恰收 OnPipelineStart → OnPipelineEnd 各一次，目标与标签正确。
#[tokio::test]
async fn lifecycle_bus_run_emits_start_and_end() {
    let bus = Arc::new(HookEventBus::new(16));
    let mut rx = bus.subscribe();
    let executor = make_bus_executor(Some(bus.clone()), "run_bus_ok");
    let compiled = compile_pipeline(
        &lifecycle_bus_config(),
        &StepLibrary::default(),
        &executor.plugin_ids,
    )
    .expect("compile should succeed");
    executor
        .run_compiled(&compiled, json!({"pipeline_id": "pipe-xyz"}))
        .await
        .expect("run should succeed");

    let start = rx.recv().await.expect("start event");
    assert_eq!(
        start.hook,
        agentos_core::traits::LifecycleHook::OnPipelineStart
    );
    assert_eq!(
        start.ctx.get("pipeline_id").and_then(|v| v.as_str()),
        Some("pipe-xyz"),
        "start 事件带 pipeline_id 标签"
    );
    assert_eq!(
        start.ctx.get("run_id").and_then(|v| v.as_str()),
        Some("run_bus_ok"),
        "start 事件带 run_id 标签"
    );
    assert!(
        matches!(start.target, EventTarget::Pipeline(ref p) if p == "pipe-xyz"),
        "start 目标 = Pipeline(pipeline_id)"
    );

    let end = rx.recv().await.expect("end event");
    assert_eq!(end.hook, agentos_core::traits::LifecycleHook::OnPipelineEnd);
    assert_eq!(
        end.ctx.get("pipeline_id").and_then(|v| v.as_str()),
        Some("pipe-xyz")
    );
    assert!(
        matches!(end.target, EventTarget::Pipeline(ref p) if p == "pipe-xyz"),
        "end 目标 = Pipeline(pipeline_id)"
    );

    // 恰好两次（不多发）。
    let extra = rx.try_recv().err();
    assert!(
        matches!(
            extra,
            Some(tokio::sync::broadcast::error::TryRecvError::Empty)
        ),
        "run 只应发 start+end 两个事件"
    );
}

/// 挂起提前跳出循环体的 run 仍发 start+end（与 persist_run_end 同边界）。
#[tokio::test]
async fn lifecycle_bus_suspended_run_still_emits_both() {
    let bus = Arc::new(HookEventBus::new(16));
    let mut rx = bus.subscribe();
    let executor = make_bus_executor(Some(bus.clone()), "run_bus_susp");
    let compiled = compile_pipeline(
        &lifecycle_bus_config(),
        &StepLibrary::default(),
        &executor.plugin_ids,
    )
    .expect("compile should succeed");
    let final_state = executor
        .run_compiled(
            &compiled,
            json!({"pipeline_id": "pipe-susp", "suspended": true}),
        )
        .await
        .expect("run should succeed");
    assert_eq!(
        final_state.get("suspended").and_then(|v| v.as_bool()),
        Some(true),
        "挂起状态保留"
    );

    let start = rx.recv().await.expect("start event");
    let end = rx.recv().await.expect("end event");
    assert_eq!(
        start.hook,
        agentos_core::traits::LifecycleHook::OnPipelineStart
    );
    assert_eq!(end.hook, agentos_core::traits::LifecycleHook::OnPipelineEnd);
    assert_eq!(
        end.ctx.get("pipeline_id").and_then(|v| v.as_str()),
        Some("pipe-susp")
    );
}

/// 引擎 Err 的 run 只发 start（与点对点 end 钩子同边界；失败终态由 api 层
/// run.failed 域事件承载）。
#[tokio::test]
async fn lifecycle_bus_engine_err_emits_start_only() {
    let bus = Arc::new(HookEventBus::new(16));
    let mut rx = bus.subscribe();
    let executor = make_bus_executor(Some(bus.clone()), "run_bus_err");
    let compiled = compile_pipeline(
        &lifecycle_bus_config(),
        &StepLibrary::default(),
        &executor.plugin_ids,
    )
    .expect("compile should succeed");
    // next_phase 指向不存在的循环体 → 循环体转移护栏触发 Err。
    let result = executor
        .run_compiled(
            &compiled,
            json!({"pipeline_id": "pipe-err", "next_phase": "ghost"}),
        )
        .await;
    assert!(result.is_err(), "路由到不存在的循环体应 Err");

    let start = rx.recv().await.expect("start event");
    assert_eq!(
        start.hook,
        agentos_core::traits::LifecycleHook::OnPipelineStart
    );
    let next = rx.try_recv().err();
    assert!(
        matches!(
            next,
            Some(tokio::sync::broadcast::error::TryRecvError::Empty)
        ),
        "Err 的 run 不发 OnPipelineEnd"
    );
}

/// 未注入总线零行为破坏：run 正常完成、不 panic。
#[tokio::test]
async fn lifecycle_bus_none_wired_run_ok() {
    let executor = make_bus_executor(None, "run_bus_none");
    let compiled = compile_pipeline(
        &lifecycle_bus_config(),
        &StepLibrary::default(),
        &executor.plugin_ids,
    )
    .expect("compile should succeed");
    let final_state = executor
        .run_compiled(&compiled, json!({"pipeline_id": "pipe-none"}))
        .await
        .expect("run should succeed");
    assert!(final_state.get("pipeline_id").is_some());
}

/// 事件形态契约锚点：ctx 标签恰含 pipeline_id/run_id（审计与指标订阅者
/// 按标签关联 run 的解析契约）。
#[tokio::test]
async fn lifecycle_bus_event_tags_are_subscriber_contract() {
    let bus = Arc::new(HookEventBus::new(16));
    let mut rx = bus.subscribe();
    let executor = make_bus_executor(Some(bus.clone()), "run_bus_shape");
    let compiled = compile_pipeline(
        &lifecycle_bus_config(),
        &StepLibrary::default(),
        &executor.plugin_ids,
    )
    .expect("compile should succeed");
    executor
        .run_compiled(&compiled, json!({"pipeline_id": "pipe-shape"}))
        .await
        .expect("run should succeed");

    let ev: LifecycleEvent = rx.recv().await.expect("start event");
    let tags: Vec<String> = ev.ctx.tags().iter().map(|(k, _)| k.clone()).collect();
    assert!(tags.contains(&"pipeline_id".to_string()));
    assert!(tags.contains(&"run_id".to_string()));
}

// ── track.messages_chars 记账（W2a 上下文估算账本）─────────────────

/// 测试侧独立口径基准：Σ 剔除 seq 的紧凑 JSON 字节（与引擎定义同契约、
/// 独立实现——防记账实现自证）。
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

/// messages 更新构造（op 声明形态）。
fn messages_update(ops: Vec<serde_json::Value>) -> HashMap<String, serde_json::Value> {
    let mut m = HashMap::new();
    m.insert("messages".to_string(), json!({ "_ops": ops }));
    m
}

/// 三态增量记账：append(+new) → set 替换(new-old) → set 删除(-old)，
/// 逐步断言精确字节值（字面断言）与全量基准一致性（性质断言）。
#[tokio::test]
async fn messages_chars_incremental_append_replace_delete() {
    let fixture = Fixture::build(&[]);
    let msg_a = json!({"role": "user", "content": "你好世界"});
    let msg_b = json!({"role": "assistant", "content": "answer"});
    let msg_c = json!({"role": "user", "content": "replacement"});
    let bytes = |m: &serde_json::Value| serde_json::to_string(m).unwrap().len() as i64;
    let mut state = json!({});

    // 批 1：两个 append（无 seq，引擎分配 0/1）
    fixture
        .executor
        .merge_and_project(
            &mut state,
            &messages_update(vec![
                json!({"op": "set", "msg": msg_a}),
                json!({"op": "set", "msg": msg_b}),
            ]),
        )
        .await;
    assert_eq!(
        state["track.messages_chars"],
        json!(bytes(&msg_a) + bytes(&msg_b))
    );
    assert_eq!(
        state["track.messages_chars"],
        json!(oracle_messages_chars(&state))
    );

    // 批 2：set(seq=0) 替换 A → delta = bytes(C) - bytes(A)
    fixture
        .executor
        .merge_and_project(
            &mut state,
            &messages_update(vec![json!({"op": "set", "seq": 0, "msg": msg_c})]),
        )
        .await;
    assert_eq!(
        state["track.messages_chars"],
        json!(bytes(&msg_b) + bytes(&msg_c)),
        "替换后 = 幸存 B + 新 C"
    );
    assert_eq!(
        state["track.messages_chars"],
        json!(oracle_messages_chars(&state))
    );

    // 批 3：set(seq=0, null) 删除 C → delta = -bytes(C)
    fixture
        .executor
        .merge_and_project(
            &mut state,
            &messages_update(vec![json!({"op": "set", "seq": 0, "msg": null})]),
        )
        .await;
    assert_eq!(state["track.messages_chars"], json!(bytes(&msg_b)));
    assert_eq!(
        state["track.messages_chars"],
        json!(oracle_messages_chars(&state))
    );
    assert_eq!(state["messages"].as_array().unwrap().len(), 1);
}

/// 冷启动兜底：账本缺失且 messages 非空 → 首次 merge 全量重算（含存量消息）；
/// messages 恒空则保持键缺失。
#[tokio::test]
async fn messages_chars_cold_start_recompute() {
    let fixture = Fixture::build(&[]);
    let mut state = json!({
        // 恢复出的历史（自带 seq）——无 track.messages_chars
        "messages": [
            {"seq": 0, "role": "user", "content": "历史问题"},
            {"seq": 1, "role": "assistant", "content": "历史回答"},
        ],
    });
    let msg_new = json!({"role": "user", "content": "新消息"});
    fixture
        .executor
        .merge_and_project(
            &mut state,
            &messages_update(vec![json!({"op": "set", "msg": msg_new})]),
        )
        .await;
    assert_eq!(
        state["track.messages_chars"],
        json!(oracle_messages_chars(&state)),
        "冷启动全量重算 = 存量 + 新增"
    );

    // messages 恒空（无 messages 键 + 空 ops）→ 键保持缺失
    let mut empty_state = json!({});
    fixture
        .executor
        .merge_and_project(&mut empty_state, &messages_update(vec![]))
        .await;
    assert!(
        empty_state.get("track.messages_chars").is_none(),
        "空消息无账可记，保持键缺失"
    );
}

/// 性质：多批混合 ops（append/替换/删除/insert 顺延/同批同 seq 连写）累计记账
/// 与全量重算逐步一致（确定性伪随机序列，含中文与变长内容）。
#[tokio::test]
async fn messages_chars_multi_batch_accumulation_matches_recompute() {
    let fixture = Fixture::build(&[]);
    let mut state = json!({});
    let mut seed: u64 = 0x9E3779B97F4A7C15;
    let mut next_rand = move || {
        seed = seed
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        (seed >> 33) as usize
    };
    let mut msg_no = 0usize;
    let make_msg = |no: usize| json!({"role": "user", "content": format!("内容-{no}-中文-padding-{}", no * 37)});

    for round in 0..12usize {
        let seqs: Vec<i64> = state["messages"]
            .as_array()
            .map(|a| {
                a.iter()
                    .filter_map(|m| m.get("seq").and_then(|s| s.as_i64()))
                    .collect()
            })
            .unwrap_or_default();
        let mut ops: Vec<serde_json::Value> = Vec::new();
        for _ in 0..1 + next_rand() % 3 {
            let pick = next_rand() % 5;
            match pick {
                0 => {
                    // append
                    ops.push(json!({"op": "set", "msg": make_msg(msg_no)}));
                    msg_no += 1;
                }
                1 if !seqs.is_empty() => {
                    // 替换既有 seq（可能替换到本批刚写的消息——同批视角回放）
                    let seq = seqs[next_rand() % seqs.len()];
                    ops.push(json!({"op": "set", "seq": seq, "msg": make_msg(msg_no)}));
                    msg_no += 1;
                }
                2 if !seqs.is_empty() => {
                    // 删除既有 seq
                    let seq = seqs[next_rand() % seqs.len()];
                    ops.push(json!({"op": "set", "seq": seq, "msg": null}));
                }
                3 => {
                    // insert（后段 seq 顺延；字节口径剔除 seq 故内容字节不变）
                    let at = (next_rand() % (seqs.len() + 1)) as i64;
                    ops.push(json!({"op": "insert", "at": at, "msg": make_msg(msg_no)}));
                    msg_no += 1;
                }
                _ => {
                    // 同批同 seq 连写两次（顺序回放：后序 old 含前序效果）
                    if !seqs.is_empty() {
                        let seq = seqs[next_rand() % seqs.len()];
                        ops.push(json!({"op": "set", "seq": seq, "msg": make_msg(msg_no)}));
                        msg_no += 1;
                        ops.push(json!({"op": "set", "seq": seq, "msg": make_msg(msg_no)}));
                        msg_no += 1;
                    }
                }
            }
        }
        if ops.is_empty() {
            continue; // 该轮无可用 op（空数组起步的保护分支）
        }
        fixture
            .executor
            .merge_and_project(&mut state, &messages_update(ops))
            .await;
        assert_eq!(
            state["track.messages_chars"],
            json!(oracle_messages_chars(&state)),
            "第 {round} 轮后累计记账 == 全量重算"
        );
    }
    // 性质收尾：最终数组非平凡（混合序列确实发生了增删改）
    assert!(state["messages"].as_array().unwrap().len() >= 3);
}

/// 全管道接线：插件经 run_compiled 回写 messages ops 后，最终 state 携带
/// track.messages_chars（invoke → merge_and_project 全链）。
#[tokio::test]
async fn messages_chars_booked_in_full_pipeline_run() {
    let fixture = Fixture::build(&["emitter"]);
    fixture.invoker.set_result(
        "emitter",
        PluginResult {
            state_updates: messages_update(vec![
                json!({"op": "set", "msg": {"role": "assistant", "content": "回复"}}),
            ]),
            ..Default::default()
        },
    );
    let final_state = fixture
        .run(
            &gated_body(vec![StepItem::Bare("emitter".into())]),
            &StepLibrary::default(),
            json!({}),
        )
        .await;
    assert_eq!(
        final_state["track.messages_chars"],
        json!(oracle_messages_chars(&final_state))
    );
}

// ── W2a 压缩粗门公式（autonomous.yaml pipeline_context_window_guard 项级 when）──

/// 生产公式（与 config/pipelines/autonomous.yaml 保持同文）：占用比 =
/// （上次真实 input_tokens + 自上次调用以来的字符增量 × 2.0）÷ 模型上下文窗口，
/// 超过 0.6（guard compress_trigger_ratio 缺省同源）放行精确判定；冷启动兜底 =
/// 无 token 锚且消息多也放行 guard 做精确判定。
const CONTEXT_GATE_WHEN: &str = "(track.llm_usage.last_input_tokens + (track.messages_chars - track.messages_chars_at_llm) * 2.0) / track.model_context_window > 0.6 or (track.llm_usage.last_input_tokens == none and len(messages) > 40)";

/// 公式门真假两侧 + 冷启动兜底侧：项级 when 对 state 求值决定 guard 是否执行。
#[tokio::test]
async fn context_gate_formula_when_controls_guard_item() {
    let gate = || StepItem::Gated {
        name: "guard".into(),
        when: Some(CONTEXT_GATE_WHEN.into()),
        inputs: HashMap::new(),
    };
    let config = gated_body(vec![gate()]);

    // 低于阈值：(60000 + 1000*2)/128000 = 0.484 ≤ 0.6 → guard 零调用
    // （state 形态对齐生产：track.llm_usage 为平键持 dict——前缀平键解析命中）
    let below = Fixture::build(&["guard"]);
    below
        .run(
            &config,
            &StepLibrary::default(),
            json!({
                "track.llm_usage": {"last_input_tokens": 60000},
                "track.messages_chars": 9000,
                "track.messages_chars_at_llm": 8000,
                "track.model_context_window": 128000,
                "messages": [{"role": "user", "content": "hi"}],
            }),
        )
        .await;
    assert_eq!(
        below.invoker.call_count("guard"),
        0,
        "占用比低于阈值跳过 guard"
    );

    // 超过阈值：(78000 + 51000*2)/128000 = 1.41 > 0.6 → guard 放行
    let above = Fixture::build(&["guard"]);
    above
        .run(
            &config,
            &StepLibrary::default(),
            json!({
                "track.llm_usage": {"last_input_tokens": 78000},
                "track.messages_chars": 59000,
                "track.messages_chars_at_llm": 8000,
                "track.model_context_window": 128000,
                "messages": [{"role": "user", "content": "hi"}],
            }),
        )
        .await;
    assert_eq!(
        above.invoker.call_count("guard"),
        1,
        "占用比超阈值放行 guard"
    );

    // 冷启动兜底：无 token 锚（公式支 fail-soft 判假）且消息 41 条 → 放行精确判定
    let cold = Fixture::build(&["guard"]);
    let msgs: Vec<serde_json::Value> = (0..41)
        .map(|i| json!({"seq": i, "role": "user", "content": format!("m{i}")}))
        .collect();
    cold.run(
        &config,
        &StepLibrary::default(),
        json!({"messages": msgs, "track.messages_chars": 4100}),
    )
    .await;
    assert_eq!(
        cold.invoker.call_count("guard"),
        1,
        "无锚且消息多时兜底放行"
    );

    // 区分度反向：无锚但消息不多（≤40）→ 两个析取支均假 → 跳过
    let cold_small = Fixture::build(&["guard"]);
    cold_small
        .run(
            &config,
            &StepLibrary::default(),
            json!({"messages": [{"role": "user", "content": "hi"}]}),
        )
        .await;
    assert_eq!(
        cold_small.invoker.call_count("guard"),
        0,
        "无锚且消息少不兜底"
    );
}

// ═══════════════════════════════════════════════════════════════════════════
// user_input 实录（persist_run_start）：首轮 user 消息指纹经
// `_pending_message_ops` 通道落一条 plugin_id="user_input" 轨迹，保证首轮
// user 也在审计/回放范围内；内部字段随即从 state 移除（不泄漏进快照/投影）。
// ═══════════════════════════════════════════════════════════════════════════

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

#[tokio::test]
async fn pending_message_ops_recorded_as_user_input_trace_and_stripped() {
    let fixture = Fixture::build(&[]);
    let config = noop_pipeline_config();
    let state = fixture
        .run(
            &config,
            &StepLibrary::default(),
            json!({
                "pipeline_id": "p_user",
                "session_id": "s_user",
                "messages": [{"role": "user", "content": "首轮", "seq": 0}],
                "_pending_message_ops": [{"op": "set", "seq": 0, "msg": {"role": "user"}}],
            }),
        )
        .await;

    let traces = fixture.store.trace_plugin_ids.lock().unwrap().clone();
    assert!(
        traces.iter().any(|p| p == "user_input"),
        "首轮 user 实录必须落轨迹（审计/回放范围），实际 {traces:?}"
    );
    assert!(
        state.get("_pending_message_ops").is_none(),
        "内部通道字段必须从 state 移除（不泄漏进快照/投影）"
    );
}

#[tokio::test]
async fn empty_pending_message_ops_records_no_user_input_trace() {
    // 空数组是合法输入（无 user 消息轮，如后台续跑）：不得落空轨迹
    let fixture = Fixture::build(&[]);
    let config = noop_pipeline_config();
    fixture
        .run(
            &config,
            &StepLibrary::default(),
            json!({
                "pipeline_id": "p_user_empty",
                "session_id": "s_user",
                "_pending_message_ops": [],
            }),
        )
        .await;
    let traces = fixture.store.trace_plugin_ids.lock().unwrap().clone();
    assert!(
        !traces.iter().any(|p| p == "user_input"),
        "空 ops 不得产生空实录，实际 {traces:?}"
    );

    // 非数组形态（写入方误传）同样静默跳过，不 panic
    let fixture2 = Fixture::build(&[]);
    fixture2
        .run(
            &config,
            &StepLibrary::default(),
            json!({
                "pipeline_id": "p_user_bad",
                "session_id": "s_user",
                "_pending_message_ops": {"not": "an array"},
            }),
        )
        .await;
    let traces2 = fixture2.store.trace_plugin_ids.lock().unwrap().clone();
    assert!(
        !traces2.iter().any(|p| p == "user_input"),
        "非数组形态不得产生实录，实际 {traces2:?}"
    );
}
