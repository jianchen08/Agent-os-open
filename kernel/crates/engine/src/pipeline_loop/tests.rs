// 由 pipeline_loop.rs 的主 #[cfg(test)] 测试块体平移而来（保留私有项访问）。

use super::*;
use crate::compiler::{compile_pipeline, HookFile};
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
    Branch, CheckpointConfig, MessageRecord, RunRecord, RunStatus, ToolExecutionResult, TraceEntry,
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
        self.errs
            .lock()
            .unwrap()
            .insert(plugin_id.to_string(), err);
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
    async fn invoke_pipeline_plugin(
        &self,
        plugin_id: &str,
        ctx: &PluginContext,
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
    traces: Mutex<usize>,
    /// true 时 set_run_pipeline 返回 Err（persist_run_start 持久化故障注入）。
    fail_set_run_pipeline: std::sync::atomic::AtomicBool,
}

impl Default for NullStorage {
    fn default() -> Self {
        Self {
            checkpoints: Mutex::new(Vec::new()),
            traces: Mutex::new(0),
            fail_set_run_pipeline: std::sync::atomic::AtomicBool::new(false),
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

    /// 收到的 append_trace 次数（项级 when 跳过 / 无产出 step 不落 trace 测试用）。
    fn trace_count(&self) -> usize {
        *self.traces.lock().unwrap()
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
        _entry: TraceEntry,
    ) -> Result<(), agentos_core::types::StorageError> {
        *self.traces.lock().unwrap() += 1;
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
    async fn create_branch(
        &self,
        _branch: Branch,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn update_run_status(
        &self,
        _run_id: &str,
        _status: RunStatus,
        _branch: Option<&str>,
        _seq: Option<u32>,
    ) -> Result<(), agentos_core::types::StorageError> {
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
    // 插件无产出 + inputs 不进 diff → 0 trace
    assert_eq!(fixture.store.trace_count(), 0, "inputs 不得产生轨迹");
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
    assert_eq!(
        fixture.store.trace_count(),
        0,
        "整 step 被 when 门架空 → 不落 trace"
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
    async fn invoke_pipeline_plugin(
        &self,
        _plugin_id: &str,
        _ctx: &PluginContext,
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
    async fn invoke_pipeline_plugin(
        &self,
        _plugin_id: &str,
        _ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError> {
        let n = self.counter.fetch_add(1, Ordering::SeqCst);
        let updates = self
            .results
            .get(n)
            .cloned()
            .unwrap_or_default();
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
    };
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    // good 仍被调用
    assert_eq!(fixture.invoker.call_count("good"), 1);
    assert_eq!(state["ok"], json!(true));
    // 插件错误收集到 _plugin_errors（引擎内部键，api 层提取为 plugin_errors）
    let errs = state["_plugin_errors"].as_array().expect("_plugin_errors 应为数组");
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
    };
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    let errs = state["_plugin_errors"].as_array().expect("_plugin_errors 应为数组");
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
    };
    let state = fixture
        .run(&config, &StepLibrary::default(), json!({}))
        .await;
    let errs = state["_plugin_errors"].as_array().expect("_plugin_errors 应为数组");
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
    let config = PipelineConfig {
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
        .run(
            &config,
            &StepLibrary::default(),
            json!({}),
        )
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
    reg.set("tenant_test", pipe, "chunk:mid_stream", json!({"text_len": 3}));
    reg.set("tenant_test", pipe, "chunk:mid_plugin", json!({"text_len": 5}));
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
    assert!(reg.resolve_step_of("tenant_test", pipe, "mid_stream").is_none());
    assert!(reg.resolve_step_of("tenant_test", pipe, "mid_plugin").is_none());
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
    assert!(executor.bind_step_message(&json!({}), "core").is_none(), "无 id 零操作");
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
    assert_eq!(reg.resolve_step_of("tenant_test", pipe, "m1").as_deref(), Some("inner"));
    // 内层守卫收尾：归属恢复为外层 step（流式窗口关闭，执行权回到外层剩余项）
    drop(inner);
    assert_eq!(reg.resolve_step_of("tenant_test", pipe, "m1").as_deref(), Some("outer"), "内层守卫收尾须恢复外层绑定");
    // 外层守卫收尾：首次登记（无 prev）→ 直接清除
    drop(outer);
    assert!(reg.resolve_step_of("tenant_test", pipe, "m1").is_none(), "外层守卫收尾清除绑定");
    reg.clear_pipeline("tenant_test", pipe);
}

// ── 步骤服务接线（服务化提案 §3.2/§3.4：编译期 method 携带 → 运行期 _step_method）──

/// 测试 manifest（单入口纯步骤插件形状：无 tools/services/steps，隐式默认注册）。
fn test_manifest(id: &str) -> PluginManifest {
    PluginManifest {
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
    let index =
        crate::compiler::build_step_service_index(&[composite]).expect("index build ok");
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
        final_state["injected"], json!(true),
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
    let index =
        crate::compiler::build_step_service_index(&[composite]).expect("index build ok");
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
        .filter_map(|c| c.get("_pipe_hook").and_then(|h| h.get("event")).and_then(|e| e.as_str()))
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
    };
    let compiled = compiled_with_hooks(
        &fixture,
        &config,
        &[("main".to_string(), vec![step_hook_file("step_start", "watcher.on_step")])],
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
    assert_eq!(step_ids, vec!["main:s1", "main:s2"], "payload 定位到具体 step");
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
