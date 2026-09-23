// @feature: FP-0.2.〇 管道引擎 | @vision: V2 可观测 | @ci: rust-test
//! pipeline_loop 未覆盖分支补测：构造器注入、挂起短路、step 级跳转、
//! 动态 step 模板、插件错误收集、body 级钩子分发。
//!
//! 与 pipeline_execution_test.rs 同构（MockInvoker + NullStorage），只聚焦
//! 生产分支的可观察行为：state 终态、插件调用序列、副作用。

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use agentos_core::traits::{MessageQueryOpts, PluginInvoker, StorageBackend};
use agentos_core::types::{
    LoopBody, MessageRecord, PipelineConfig, PipelineStep, PluginContext, PluginError,
    PluginResult, Route, RouteAction, RouteNext, RunRecord, StepItem, StepLibrary,
    ToolExecutionResult, TraceEntry,
};
use agentos_engine::compiler::compile_pipeline;
use agentos_engine::{EngineMetrics, PipelineExecutor};
use async_trait::async_trait;
use serde_json::json;

/// 可编程 MockInvoker：按 plugin_id 返回预设结果或错误，并统计调用序列。
struct MockInvoker {
    results: Mutex<HashMap<String, PluginResult>>,
    errors: Mutex<HashMap<String, PluginError>>,
    calls: Mutex<Vec<String>>,
}

impl MockInvoker {
    fn new() -> Self {
        Self {
            results: Mutex::new(HashMap::new()),
            errors: Mutex::new(HashMap::new()),
            calls: Mutex::new(Vec::new()),
        }
    }

    /// 预设更新（每次调用都返回同一份 state_updates）。
    fn set_result(&self, plugin_id: &str, result: PluginResult) {
        self.results
            .lock()
            .unwrap()
            .insert(plugin_id.to_string(), result);
    }

    /// 预设失败：invoke 直接上抛 PluginError（覆盖 invoker error 分支）。
    fn set_error(&self, plugin_id: &str, error: PluginError) {
        self.errors
            .lock()
            .unwrap()
            .insert(plugin_id.to_string(), error);
    }

    fn calls(&self) -> Vec<String> {
        self.calls.lock().unwrap().clone()
    }

    fn call_count(&self, plugin_id: &str) -> usize {
        self.calls()
            .iter()
            .filter(|p| p.as_str() == plugin_id)
            .count()
    }
}

#[async_trait]
impl PluginInvoker for MockInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        _ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        self.calls.lock().unwrap().push(plugin_id.to_string());
        if let Some(e) = self.errors.lock().unwrap().get(plugin_id) {
            return Err(e.clone());
        }
        Ok(self
            .results
            .lock()
            .unwrap()
            .get(plugin_id)
            .cloned()
            .unwrap_or_default())
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
        _hook: agentos_core::traits::LifecycleHook,
        _ctx: &agentos_core::traits::HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
}

/// 零实现存储：本文件的用例不落库（run_compiled 的持久化路径只 warn 不阻断）。
struct NullStorage;

#[async_trait]
impl StorageBackend for NullStorage {
    async fn get_run(&self, _run_id: &str) -> Result<RunRecord, agentos_core::types::StorageError> {
        Err(agentos_core::types::StorageError::NotFound("null".into()))
    }
    async fn get_messages_by_pipeline(
        &self,
        _pipeline_id: &str,
        _opts: MessageQueryOpts,
    ) -> Result<Vec<MessageRecord>, agentos_core::types::StorageError> {
        Ok(Vec::new())
    }
    async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, agentos_core::types::StorageError> {
        Err(agentos_core::types::StorageError::NotFound("null".into()))
    }
    async fn append_trace(
        &self,
        _entry: TraceEntry,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn store_blob(
        &self,
        _data: &[u8],
        _mime_type: &str,
    ) -> Result<String, agentos_core::types::StorageError> {
        Err(agentos_core::types::StorageError::NotFound("null".into()))
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
        Ok(Vec::new())
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
        Ok(Vec::new())
    }
    async fn get_step_traces_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, agentos_core::types::StorageError> {
        Ok(Vec::new())
    }
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
    async fn update_user_password(
        &self,
        _user_id: &str,
        _password_hash: &str,
        _must_change_password: bool,
    ) -> Result<bool, agentos_core::types::StorageError> {
        Ok(false)
    }
    async fn delete_user(&self, _user_id: &str) -> Result<bool, agentos_core::types::StorageError> {
        Ok(false)
    }
}

fn make_executor(invoker: Arc<MockInvoker>, plugin_ids: &[&str]) -> PipelineExecutor {
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage);
    PipelineExecutor::new(
        invoker as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        agentos_core::types::TenantContext::new("tenant_test", "session_test"),
        plugin_ids.iter().map(|s| s.to_string()),
        store,
        "run_branches",
    )
}

/// 单循环体、单 step 的最小配置。
///
/// while 恒真由 step 级 `True → End` 路由收口（与 default.yaml 同构）：
/// 单轮执行后置 ended=true，主轮循环下一圈开头即 break——避免恒真 while
/// 撞 max_rounds 上限（那是另一条专测路径）。
fn single_body_config(steps: Vec<StepItem>) -> PipelineConfig {
    PipelineConfig {
        name: "branch_test".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "s1".into(),
                steps,
                when: None,
                context: HashMap::new(),
                routes: vec![Route {
                    when: "True".into(),
                    then: RouteAction {
                        next: RouteNext::End,
                        set: HashMap::new(),
                    },
                }],
                loop_config: None,
            }],
            while_cond: Some("True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: HashMap::new(),
        max_rounds: None,
    }
}

async fn run(
    executor: &PipelineExecutor,
    config: &PipelineConfig,
    initial: serde_json::Value,
) -> Result<serde_json::Value, agentos_core::types::EngineError> {
    let compiled = compile_pipeline(config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");
    executor.run_compiled(&compiled, initial).await
}

// ── with_metrics：注入共享计数器（聚合器 snapshot 的数据源）──

#[tokio::test]
async fn with_metrics_shares_counter_handle_with_caller() {
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result("p1", PluginResult::default());
    let metrics = Arc::new(EngineMetrics::new());
    let executor = make_executor(Arc::clone(&invoker), &["p1"]).with_metrics(Arc::clone(&metrics));

    assert!(
        Arc::ptr_eq(executor.metrics(), &metrics),
        "metrics() 应返回注入的同一实例（聚合器共享计数）"
    );

    let config = single_body_config(vec![StepItem::Bare("p1".into())]);
    run(&executor, &config, json!({})).await.expect("run ok");

    // 注入的共享计数器应被本次 run 累加（非内部私有副本在计数）
    assert!(
        metrics
            .pipeline_exec_total
            .load(std::sync::atomic::Ordering::Relaxed)
            > 0,
        "注入的计数器应记录本次管道执行"
    );
}

// ── 跨轮复用的 `_plugin_errors` 清零 ──

/// 上一轮残留的 `_plugin_errors` 必须在本轮 run 起始清零——否则
/// stage_finalize 会把上轮错误重复提取（错误归因跨轮串味）。
#[tokio::test]
async fn stale_plugin_errors_are_cleared_at_run_start() {
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result("p1", PluginResult::default());
    let executor = make_executor(Arc::clone(&invoker), &["p1"]);

    let config = single_body_config(vec![StepItem::Bare("p1".into())]);
    let final_state = run(
        &executor,
        &config,
        json!({"_plugin_errors": [{"plugin": "stale", "error": "上轮残留"}]}),
    )
    .await
    .expect("run ok");

    let errs = final_state["_plugin_errors"]
        .as_array()
        .expect("_plugin_errors 应存在且为数组");
    assert!(
        !errs.iter().any(|e| e["plugin"].as_str() == Some("stale")),
        "上轮残留错误必须被清零: {errs:?}"
    );
}

// ── 挂起短路：suspended 初始为真 → 循环体与 step 层均不执行 ──

/// `suspended=true` 初始 state → 主轮循环第一行即 break（不跑任何插件）；
/// 对照：suspended=false 时插件被调用。
#[tokio::test]
async fn suspended_initial_state_short_circuits_body_loop() {
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result("p1", PluginResult::default());
    let executor = make_executor(Arc::clone(&invoker), &["p1"]);
    let config = single_body_config(vec![StepItem::Bare("p1".into())]);

    let final_state = run(&executor, &config, json!({"suspended": true}))
        .await
        .expect("挂起不是错误");
    assert_eq!(
        invoker.call_count("p1"),
        0,
        "挂起态不得执行任何插件（等待唤醒）"
    );
    assert_eq!(final_state["suspended"], json!(true), "挂起标志保持");
    assert_eq!(final_state["ended"], json!(false), "挂起 ≠ 结束");

    // 对照：非挂起态照常执行并正常终止
    let final_state = run(&executor, &config, json!({})).await.expect("run ok");
    assert_eq!(invoker.call_count("p1"), 1, "非挂起态应执行");
    assert!(final_state.get("suspended").is_none());
}

/// `ended=true` 初始 state → 循环体同样短路（run 已结束不得继续推进）。
#[tokio::test]
async fn ended_initial_state_short_circuits_body_loop() {
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result("p1", PluginResult::default());
    let executor = make_executor(Arc::clone(&invoker), &["p1"]);
    let config = single_body_config(vec![StepItem::Bare("p1".into())]);

    let final_state = run(&executor, &config, json!({"ended": true}))
        .await
        .expect("已结束不是错误");
    assert_eq!(invoker.call_count("p1"), 0, "已结束不得继续执行插件");
    assert_eq!(final_state["ended"], json!(true), "结束标志保持");
}

// ── step 级跳转（routes 的 Step 目标）──

/// step 级 routes 命中 `Step` 指向**同循环体内**的 step id → 跳转执行该 step，
/// 跳过的中间 step 不被调用（顺序推进被跳转打断）。
#[tokio::test]
async fn step_level_route_jumps_within_body() {
    let invoker = Arc::new(MockInvoker::new());
    for p in ["plug_alpha", "plug_beta", "plug_gamma"] {
        invoker.set_result(p, PluginResult::default());
    }
    let executor = make_executor(
        Arc::clone(&invoker),
        &["plug_alpha", "plug_beta", "plug_gamma"],
    );

    // step 级 routes：alpha 的 Step 目标写 "gamma"（step id，非插件名——
    // step id 与 item 引用名同名会触发编译期自引用环拒绝）
    let config = PipelineConfig {
        name: "step_jump".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![
                PipelineStep {
                    id: "alpha".into(),
                    steps: vec!["plug_alpha".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![Route {
                        when: "True".into(),
                        then: RouteAction {
                            next: RouteNext::Step("gamma".into()),
                            set: HashMap::new(),
                        },
                    }],
                    loop_config: None,
                },
                PipelineStep {
                    id: "beta".into(),
                    steps: vec!["plug_beta".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                },
                PipelineStep {
                    id: "gamma".into(),
                    steps: vec!["plug_gamma".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                },
            ],
            while_cond: None, // 单次执行体（while 缺省 = 非循环体）
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: HashMap::new(),
        max_rounds: None,
    };

    run(&executor, &config, json!({})).await.expect("run ok");
    let calls = invoker.calls();
    assert_eq!(
        calls,
        vec!["plug_alpha".to_string(), "plug_gamma".to_string()],
        "step 跳转应跳过中间 step（只调首项与目标）: {calls:?}"
    );
}

// ── 组级 when 门（PipelineStep.when）──

/// step 级 when 为假 → 整组跳过（零调用）；为真 → 执行。
/// 两组配置对照，锁"when 门真实生效"。
#[tokio::test]
async fn step_group_when_gate_skips_whole_group() {
    for (when_cond, expect_called) in [("False", false), ("True", true)] {
        let invoker = Arc::new(MockInvoker::new());
        invoker.set_result("gated", PluginResult::default());
        let executor = make_executor(Arc::clone(&invoker), &["gated"]);

        let config = PipelineConfig {
            name: format!("gate_{when_cond}"),
            loop_bodies: vec![LoopBody {
                id: "main".into(),
                steps: vec![PipelineStep {
                    id: "gate_step".into(),
                    steps: vec!["gated".into()],
                    when: Some(when_cond.to_string()),
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                }],
                while_cond: None, // 单次执行体：门为假时整组零调用，为真时执行一次
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
            initial_state: HashMap::new(),
            max_rounds: None,
        };
        run(&executor, &config, json!({})).await.expect("run ok");
        assert_eq!(
            invoker.call_count("gated") > 0,
            expect_called,
            "when={when_cond} 时调用情况不符"
        );
    }
}

// ── 动态 step 模板（`{{state.x}}` 解析为 step id 或插件 id）──

/// 动态 step：模板求值命中的插件（lookup_plugin）被调用；
/// 对照：模板求值落空 → 记 error 但不阻断后续（继续后续步骤）。
#[tokio::test]
async fn dynamic_step_template_resolves_plugin_and_reports_miss() {
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result("dyn_target", PluginResult::default());
    invoker.set_result("after", PluginResult::default());
    let executor = make_executor(Arc::clone(&invoker), &["dyn_target", "after"]);

    let config = single_body_config(vec![
        StepItem::Bare("{{state.pick}}".into()),
        StepItem::Bare("after".into()),
    ]);

    // 命中：state.pick = dyn_target（插件 id）
    run(&executor, &config, json!({"pick": "dyn_target"}))
        .await
        .expect("命中应成功");
    let calls = invoker.calls();
    assert_eq!(
        calls,
        vec!["dyn_target".to_string(), "after".to_string()],
        "动态模板应解析出插件并继续后续: {calls:?}"
    );

    // 落空：state.pick 不存在 → 记 error 日志但继续执行后续步骤（不中断 run，
    // 且落空点不产生 _plugin_errors 条目——那是插件执行失败的收集面）。
    let invoker2 = Arc::new(MockInvoker::new());
    invoker2.set_result("after", PluginResult::default());
    let executor2 = make_executor(Arc::clone(&invoker2), &["after"]);
    let compiled2 = compile_pipeline(&config, &StepLibrary::default(), executor2.plugin_ids())
        .expect("compile ok");
    let final_state = executor2
        .run_compiled(&compiled2, json!({}))
        .await
        .expect("模板落空不应让 run 失败");
    assert_eq!(
        invoker2.calls(),
        vec!["after".to_string()],
        "落空后应继续执行后续步骤"
    );
    assert!(
        final_state.get("_plugin_errors").is_none(),
        "模板落空不产生插件错误条目（不是插件执行失败）: {}",
        final_state["_plugin_errors"]
    );
}

// ── 插件返回 failure / invoker 上抛错误：收集后继续后续步骤 ──

/// 插件 result.error 非空 → 记入 `_plugin_errors` 且不中断 run；
/// 对照：invoker 上抛 PluginError 同样收集后继续。
#[tokio::test]
async fn plugin_failures_are_collected_and_execution_continues() {
    // ① result.error（插件自报失败）
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result(
        "plug_failing",
        PluginResult {
            error: Some(agentos_core::types::PluginError {
                message: "插件自报失败".into(),
                code: Some("E_PLUGIN".into()),
                source: Some("test".into()),
            }),
            ..Default::default()
        },
    );
    invoker.set_result("plug_after", PluginResult::default());
    let executor = make_executor(Arc::clone(&invoker), &["plug_failing", "plug_after"]);
    let config = single_body_config(vec![
        StepItem::Bare("plug_failing".into()),
        StepItem::Bare("plug_after".into()),
    ]);

    let final_state = run(&executor, &config, json!({}))
        .await
        .expect("插件自报失败不应让 run 失败");
    assert_eq!(
        invoker.calls(),
        vec!["plug_failing".to_string(), "plug_after".to_string()],
        "失败后应继续后续步骤"
    );
    let errs = final_state["_plugin_errors"].as_array().unwrap();
    assert!(
        errs.iter()
            .any(|e| e["plugin_id"].as_str() == Some("plug_failing")),
        "自报失败应记入 _plugin_errors: {errs:?}"
    );

    // ② invoker 上抛（调用层失败）
    let invoker2 = Arc::new(MockInvoker::new());
    invoker2.set_error(
        "plug_broken",
        agentos_core::types::PluginError {
            message: "调不通".into(),
            code: Some("E_INVOKE".into()),
            source: Some("test".into()),
        },
    );
    invoker2.set_result("plug_after", PluginResult::default());
    let executor2 = make_executor(Arc::clone(&invoker2), &["plug_broken", "plug_after"]);
    let config2 = single_body_config(vec![
        StepItem::Bare("plug_broken".into()),
        StepItem::Bare("plug_after".into()),
    ]);
    let final_state2 = run(&executor2, &config2, json!({}))
        .await
        .expect("invoker 上抛不应让 run 失败");
    assert_eq!(
        invoker2.calls(),
        vec!["plug_broken".to_string(), "plug_after".to_string()],
        "上抛后应继续后续步骤"
    );
    let errs2 = final_state2["_plugin_errors"].as_array().unwrap();
    assert!(
        errs2
            .iter()
            .any(|e| e["plugin_id"].as_str() == Some("plug_broken")),
        "invoker 上抛应记入 _plugin_errors: {errs2:?}"
    );
}

// ── max_rounds 非法值与超限（fail-closed）──

/// max_rounds=0 非法（0 = 无限轮被禁用）→ run 入口拒绝，不执行任何插件。
#[tokio::test]
async fn max_rounds_zero_is_rejected_before_execution() {
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result("p1", PluginResult::default());
    let executor = make_executor(Arc::clone(&invoker), &["p1"]).with_max_rounds(0);
    let config = single_body_config(vec![StepItem::Bare("p1".into())]);

    let compiled =
        compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids()).unwrap();
    let err = executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect_err("max_rounds=0 应拒绝");
    assert!(
        format!("{err}").contains("max_rounds=0 非法"),
        "错误文案应说明非法原因: {err}"
    );
    assert_eq!(invoker.call_count("p1"), 0, "拒绝应发生在执行之前");
}

/// while 恒真 + 小上限：轮数超限 fail-closed 终止 run（非静默 break）。
/// 配置刻意不带 End 路由（单轮不收口），让 while 恒真把轮数推到上限。
#[tokio::test]
async fn max_rounds_exceeded_terminates_with_error() {
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result("plug_tick", PluginResult::default());
    let executor = make_executor(Arc::clone(&invoker), &["plug_tick"]).with_max_rounds(3);
    let config = PipelineConfig {
        name: "loop_forever".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "spin".into(),
                steps: vec!["plug_tick".into()],
                when: None,
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
            while_cond: Some("True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: HashMap::new(),
        max_rounds: None,
    };

    let compiled =
        compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids()).unwrap();
    let err = executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect_err("恒真 while 超限应报错");
    let msg = format!("{err}");
    assert!(
        msg.contains("轮数") || msg.contains("max_rounds"),
        "错误文案应说明超限: {msg}"
    );
    assert!(
        invoker.call_count("plug_tick") >= 3,
        "应至少跑到上限轮数: {}",
        invoker.call_count("plug_tick")
    );
}

// ── 只读访问器 ──

/// plugin_ids() 暴露构造时注入的集合（compile_pipeline 的未知引用校验依据）。
#[tokio::test]
async fn plugin_ids_accessor_exposes_injected_set() {
    let invoker = Arc::new(MockInvoker::new());
    let executor = make_executor(Arc::clone(&invoker), &["a", "b"]);
    let ids = executor.plugin_ids();
    assert_eq!(ids.len(), 2);
    assert!(ids.contains("a") && ids.contains("b"));
}
