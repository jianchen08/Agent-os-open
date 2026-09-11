// @feature: FP-0.2.〇 管道引擎多循环体 | @vision: V3 可嵌入 | @ci: rust-test
//! 多循环体模型验收：init → main → exit 顺序推进、ended 只结束当前体、
//! run_on_error 收尾语义、exit_routes Phase 转移。
//!
//! 验证（TDD 规格）：
//! 1. test_init_main_exit_sequential —— init 单次 → main 循环（ended 终止本
//!    体）→ exit 收尾（ended=true 下照常执行）；current_phase 依次记录
//! 2. test_exit_routes_phase_transition_skips_main —— exit_routes 命中
//!    Phase("exit") → 跳过 main 直接转移
//! 3. test_run_on_error_runs_exit_when_ended_at_start —— 初始 ended=true：
//!    init/main 跳过，run_on_error 的 exit 仍执行
//! 4. test_window_traces_* / test_should_stop_* / test_run_init_* —— 凡 state
//!    变更必可追溯（ADR 2026-09-11）：引擎窗口（run_init/body_enter/
//!    body_route/run_finalize）把 step 窗口外的基线、相位切换、体间转移、
//!    should_stop 折算、终态折算全部落轨迹

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use agentos_core::traits::{PluginInvoker, StorageBackend};
use agentos_core::types::{
    LoopBody, MessageRecord, PipelineConfig, PipelineStep, PluginContext, PluginError,
    PluginResult, Route, RouteAction, RouteNext, RunRecord, RunStatus, StepLibrary, TenantContext,
    ToolExecutionResult, TraceEntry,
};
use agentos_engine::compiler::compile_pipeline;
use agentos_engine::PipelineExecutor;
use async_trait::async_trait;
use serde_json::json;

/// 记录 (plugin_id, 调用时 current_phase) 的 MockInvoker；可编程返回结果。
struct PhaseRecordingInvoker {
    calls: Mutex<Vec<(String, String)>>,
    results: Mutex<HashMap<String, PluginResult>>,
}

impl PhaseRecordingInvoker {
    fn new() -> Self {
        Self {
            calls: Mutex::new(Vec::new()),
            results: Mutex::new(HashMap::new()),
        }
    }

    fn set_result(&self, plugin_id: &str, result: PluginResult) {
        self.results
            .lock()
            .unwrap()
            .insert(plugin_id.to_string(), result);
    }

    fn call_count(&self, plugin_id: &str) -> usize {
        self.calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(p, _)| p == plugin_id)
            .count()
    }

    /// (plugin_id, phase) 调用序列
    fn sequence(&self) -> Vec<(String, String)> {
        self.calls.lock().unwrap().clone()
    }
}

#[async_trait]
impl PluginInvoker for PhaseRecordingInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        let phase = ctx
            .state
            .get("current_phase")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        self.calls
            .lock()
            .unwrap()
            .push((plugin_id.to_string(), phase));
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

/// 空 StorageBackend（仅构造 ContentLoader 用，持久化全部成功空操作）；
/// append_trace 带录制，供轨迹窗口断言。
struct NullStorage {
    traces: Mutex<Vec<TraceEntry>>,
}

impl NullStorage {
    fn new() -> Self {
        Self {
            traces: Mutex::new(Vec::new()),
        }
    }

    fn recorded_traces(&self) -> Vec<TraceEntry> {
        self.traces.lock().unwrap().clone()
    }
}

#[async_trait]
impl StorageBackend for NullStorage {
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
        self.traces.lock().unwrap().push(entry);
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
        unreachable!("NullStorage 不提供口令更新")
    }

    async fn delete_user(&self, _user_id: &str) -> Result<bool, agentos_core::types::StorageError> {
        Ok(false)
    }
}

/// 三体管道构造：init（单次）/ main（循环）/ exit（run_on_error 收尾）。
fn make_three_body_config(exit_routes: Vec<Route>) -> PipelineConfig {
    PipelineConfig {
        name: "multi_body".into(),
        loop_bodies: vec![
            LoopBody {
                id: "init".into(),
                steps: vec![PipelineStep {
                    id: "init_s".into(),
                    steps: vec!["p_init".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                }],
                while_cond: None,
                exit_routes,
                run_on_error: false,
            },
            LoopBody {
                id: "main".into(),
                steps: vec![PipelineStep {
                    id: "main_s".into(),
                    steps: vec!["p_main".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                }],
                while_cond: Some("True".into()),
                exit_routes: vec![],
                run_on_error: false,
            },
            LoopBody {
                id: "exit".into(),
                steps: vec![PipelineStep {
                    id: "exit_s".into(),
                    steps: vec!["p_exit".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                }],
                while_cond: None,
                exit_routes: vec![],
                run_on_error: true,
            },
        ],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    }
}

fn make_executor(invoker: Arc<PhaseRecordingInvoker>) -> (PipelineExecutor, Arc<NullStorage>) {
    let store: Arc<NullStorage> = Arc::new(NullStorage::new());
    let executor = PipelineExecutor::new(
        invoker as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        TenantContext::new("t", "s"),
        vec![
            "p_init".to_string(),
            "p_main".to_string(),
            "p_exit".to_string(),
        ],
        store.clone(),
        "r",
        "b",
    );
    (executor, store)
}

/// init 单次 → main 循环（ended 终止本体）→ exit 收尾（ended=true 照常执行）。
#[tokio::test]
async fn test_init_main_exit_sequential() {
    let invoker = Arc::new(PhaseRecordingInvoker::new());
    // p_main 第一轮设置 ended=true → main 循环一轮即止
    let mut main_result = PluginResult::default();
    main_result
        .state_updates
        .insert("ended".to_string(), json!(true));
    invoker.set_result("p_main", main_result);
    let config = make_three_body_config(vec![]);
    let (executor, _store) = make_executor(Arc::clone(&invoker));

    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");
    let final_state = executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");

    // 各体插件各执行一次，phase 记录正确
    assert_eq!(invoker.call_count("p_init"), 1);
    assert_eq!(invoker.call_count("p_main"), 1);
    assert_eq!(invoker.call_count("p_exit"), 1);
    let seq = invoker.sequence();
    assert_eq!(seq[0], ("p_init".to_string(), "init".to_string()));
    assert_eq!(seq[1], ("p_main".to_string(), "main".to_string()));
    assert_eq!(seq[2], ("p_exit".to_string(), "exit".to_string()));
    // ended 保持 true（exit 收尾不改写）；current_phase 停在 exit
    assert_eq!(
        final_state.get("ended").and_then(|v| v.as_bool()),
        Some(true)
    );
    assert_eq!(
        final_state.get("current_phase").and_then(|v| v.as_str()),
        Some("exit")
    );
}

/// exit_routes 命中 Phase("exit") → 跳过 main 直接转移。
#[tokio::test]
async fn test_exit_routes_phase_transition_skips_main() {
    let invoker = Arc::new(PhaseRecordingInvoker::new());
    let exit_routes = vec![Route {
        when: "skip_main == true".into(),
        then: RouteAction {
            next: RouteNext::Phase("exit".into()),
            set: HashMap::new(),
        },
    }];
    let config = make_three_body_config(exit_routes);
    let (executor, _store) = make_executor(Arc::clone(&invoker));

    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");
    let final_state = executor
        .run_compiled(&compiled, json!({"skip_main": true}))
        .await
        .expect("run ok");

    assert_eq!(invoker.call_count("p_init"), 1);
    assert_eq!(invoker.call_count("p_main"), 0, "Phase 转移应跳过 main");
    assert_eq!(invoker.call_count("p_exit"), 1);
    // 跳转后顺序推进到 exit 结束；next_phase 已被消费（不残留）
    assert!(final_state.get("next_phase").is_none());
    assert_eq!(
        final_state.get("current_phase").and_then(|v| v.as_str()),
        Some("exit")
    );
}

/// 初始 ended=true：init/main 跳过（非 run_on_error），exit 仍执行（收尾语义）。
#[tokio::test]
async fn test_run_on_error_runs_exit_when_ended_at_start() {
    let invoker = Arc::new(PhaseRecordingInvoker::new());
    let config = make_three_body_config(vec![]);
    let (executor, _store) = make_executor(Arc::clone(&invoker));

    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");
    let final_state = executor
        .run_compiled(&compiled, json!({"ended": true}))
        .await
        .expect("run ok");

    assert_eq!(
        invoker.call_count("p_init"),
        0,
        "init 非收尾体，ended 时跳过"
    );
    assert_eq!(
        invoker.call_count("p_main"),
        0,
        "main 非收尾体，ended 时跳过"
    );
    assert_eq!(
        invoker.call_count("p_exit"),
        1,
        "exit run_on_error 必须执行"
    );
    assert_eq!(
        final_state.get("ended").and_then(|v| v.as_bool()),
        Some(true)
    );
}

/// 转移死循环防护：Phase 在 init 与 exit 间互跳 → run 报错而非死循环。
#[tokio::test]
async fn test_phase_loop_guard_errors() {
    let invoker = Arc::new(PhaseRecordingInvoker::new());
    // init exit_routes 恒命中 → Phase("exit")；exit exit_routes 恒命中 → Phase("init")
    let exit_routes = vec![Route {
        when: "True".into(),
        then: RouteAction {
            next: RouteNext::Phase("exit".into()),
            set: HashMap::new(),
        },
    }];
    let mut config = make_three_body_config(exit_routes);
    config.loop_bodies[2].exit_routes = vec![Route {
        when: "True".into(),
        then: RouteAction {
            next: RouteNext::Phase("init".into()),
            set: HashMap::new(),
        },
    }];
    let (executor, _store) = make_executor(Arc::clone(&invoker));

    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");
    let result = executor.run_compiled(&compiled, json!({})).await;
    assert!(
        result.is_err(),
        "Phase 互跳应被转移防护截断为错误：{:?}",
        result
    );
}

/// 转移目标不存在（Phase 指向未知 body）→ 编译期显式错误（G10 语义升级）。
#[tokio::test]
async fn test_phase_target_missing_errors() {
    let invoker = Arc::new(PhaseRecordingInvoker::new());
    let exit_routes = vec![Route {
        when: "True".into(),
        then: RouteAction {
            next: RouteNext::Phase("ghost".into()),
            set: HashMap::new(),
        },
    }];
    let config = make_three_body_config(exit_routes);
    let (executor, _store) = make_executor(Arc::clone(&invoker));

    let err = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect_err("Phase 目标不存在应在编译期报错");
    assert!(err.to_string().contains("ghost"), "err: {err}");
}

/// 凡 state 变更必可追溯（ADR 2026-09-11）：next_phase 全生命周期——step 窗口
/// 记出生（init_s）、体间窗口记消费删除（body_route 的 null 标记，RFC 7396）；
/// 无产出的 step 不落（省略语义不变）。
#[tokio::test]
async fn test_window_traces_record_inter_body_state_changes() {
    let invoker = Arc::new(PhaseRecordingInvoker::new());
    // p_init 经 state_updates 写 next_phase（step 级路由的转移意图）
    let mut init_result = PluginResult::default();
    init_result
        .state_updates
        .insert("next_phase".to_string(), json!("exit"));
    invoker.set_result("p_init", init_result);
    let config = make_three_body_config(vec![]);
    let (executor, store) = make_executor(Arc::clone(&invoker));

    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");
    let final_state = executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");

    // 窗口序列：run_init → body_enter(init) → init_s(step 窗口记出生)
    // → body_route(记消费删除) → body_enter(exit) → run_finalize。
    let traces = store.recorded_traces();
    let ids: Vec<String> = traces.iter().map(|t| t.plugin_id.clone()).collect();
    assert_eq!(
        ids,
        vec![
            "run_init",
            "body_enter",
            "init_s",
            "body_route",
            "body_enter",
            "run_finalize"
        ],
        "窗口轨迹序列: {ids:?}"
    );

    // 出生在 step 窗口：init_s 记 next_phase 写入
    assert_eq!(
        traces[2].patch_data,
        json!({"next_phase": "exit"}),
        "step 窗口应记 next_phase 出生"
    );
    // 消费删除在体间窗口：body_route 记 null 标记（RFC 7396 删除语义）
    assert_eq!(
        traces[3].patch_data,
        json!({"next_phase": null}),
        "体间转移窗口应记 next_phase 消费删除标记"
    );
    assert!(final_state.get("next_phase").is_none(), "消费后不残留");

    // run_finalize：run_status 终态折算进轨迹，值与最终 state 同源（性质断言）
    assert_eq!(
        traces[5].patch_data.get("run_status"),
        final_state.get("run_status"),
        "run_finalize 轨迹的 run_status 应与最终 state 一致"
    );
    assert!(final_state.get("run_status").is_some());
}

/// exit_routes 的 set 写入 + End 折算进 body_route 窗口：命中即写署名键并
/// ended=true，窗口一次记全；stop 后后续循环体不进入。
#[tokio::test]
async fn test_exit_route_set_and_end_recorded_in_body_route() {
    let invoker = Arc::new(PhaseRecordingInvoker::new());
    let exit_routes = vec![Route {
        when: "skip_main == true".into(),
        then: RouteAction {
            next: RouteNext::End,
            set: HashMap::from([("route_marker".to_string(), json!("skipped"))]),
        },
    }];
    let config = make_three_body_config(exit_routes);
    let (executor, store) = make_executor(Arc::clone(&invoker));

    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");
    let final_state = executor
        .run_compiled(&compiled, json!({"skip_main": true}))
        .await
        .expect("run ok");

    let traces = store.recorded_traces();
    let ids: Vec<String> = traces.iter().map(|t| t.plugin_id.clone()).collect();
    assert_eq!(
        ids,
        vec!["run_init", "body_enter", "body_route", "run_finalize"],
        "End 终止后直接收尾: {ids:?}"
    );
    assert_eq!(
        traces[2].patch_data,
        json!({"route_marker": "skipped", "ended": true}),
        "体间转移窗口应含 route set 写入与 End 折算"
    );
    assert_eq!(invoker.call_count("p_main"), 0, "End 后 main/exit 不进入");
    assert_eq!(
        final_state.get("route_marker").and_then(|v| v.as_str()),
        Some("skipped")
    );
}

/// 体入口窗口：每个实际进入的循环体各落一条 current_phase 切换轨迹。
#[tokio::test]
async fn test_body_enter_traces_phase_transitions() {
    let invoker = Arc::new(PhaseRecordingInvoker::new());
    // p_main 首轮设 ended=true 终止 main 循环（三体顺序走完）
    let mut main_result = PluginResult::default();
    main_result
        .state_updates
        .insert("ended".to_string(), json!(true));
    invoker.set_result("p_main", main_result);
    let config = make_three_body_config(vec![]);
    let (executor, store) = make_executor(Arc::clone(&invoker));

    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");
    executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("run ok");

    let traces = store.recorded_traces();
    let enters: Vec<&serde_json::Value> = traces
        .iter()
        .filter(|t| t.plugin_id == "body_enter")
        .map(|t| &t.patch_data)
        .collect();
    assert_eq!(
        enters,
        vec![
            &json!({"current_phase": "init"}),
            &json!({"current_phase": "main"}),
            &json!({"current_phase": "exit"}),
        ],
        "三体依次进入，current_phase 切换逐条可追溯"
    );
}

/// 续 run 覆盖写：run_started_at 每 run 重写 → run_init 只含该键的变更
/// （ended 已存在且未变 → 不入 diff）；不残留上一轮窗口账。
#[tokio::test]
async fn test_run_init_records_only_changed_baseline_keys_on_resume() {
    let invoker = Arc::new(PhaseRecordingInvoker::new());
    // p_main 首轮设 ended=true 终止 main（每轮可复跑）
    let mut main_result = PluginResult::default();
    main_result
        .state_updates
        .insert("ended".to_string(), json!(true));
    invoker.set_result("p_main", main_result);
    let config = make_three_body_config(vec![]);
    let (executor, store) = make_executor(Arc::clone(&invoker));

    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");
    let first = executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect("first run ok");

    // 同 executor 第二轮：以上一轮终态为起点（模拟挂起恢复续跑）
    executor
        .run_compiled(&compiled, first.clone())
        .await
        .expect("second run ok");

    let traces = store.recorded_traces();
    let run_inits: Vec<&TraceEntry> = traces
        .iter()
        .filter(|t| t.plugin_id == "run_init")
        .collect();
    assert_eq!(run_inits.len(), 2, "两轮各一条 run_init");
    // 首 run：ended 新增 + run_started_at 新增
    assert_eq!(run_inits[0].patch_data.get("ended"), Some(&json!(false)));
    assert!(run_inits[0].patch_data.get("run_started_at").is_some());
    // 续 run：仅 run_started_at 覆盖写是变更；ended 原值未动不入 diff
    let resume_patch = &run_inits[1].patch_data;
    assert_eq!(
        resume_patch
            .as_object()
            .map(|o| o.keys().cloned().collect::<Vec<_>>()),
        Some(vec!["run_started_at".to_string()]),
        "续 run 基线只有 run_started_at 覆盖写: {resume_patch}"
    );
    assert_ne!(
        resume_patch.get("run_started_at"),
        first.get("run_started_at"),
        "墙钟每 run 重算，值必须变化"
    );
}

/// 轮边界 should_stop→ended 折算（引擎职责）进 body_route 窗口：should_stop
/// 的出生在 step 窗口（main_s），折算在体间窗口，两段各自可追溯。
#[tokio::test]
async fn test_should_stop_conversion_recorded_in_body_route() {
    let invoker = Arc::new(PhaseRecordingInvoker::new());
    // p_main 设置 should_stop → 轮边界折算 ended=true，main 一轮即止
    let mut main_result = PluginResult::default();
    main_result
        .state_updates
        .insert("should_stop".to_string(), json!(true));
    invoker.set_result("p_main", main_result);
    // skip_main=false → exit_routes 不命中 → 顺序推进 init→main→exit
    let exit_routes = vec![Route {
        when: "skip_main == true".into(),
        then: RouteAction {
            next: RouteNext::Phase("exit".into()),
            set: HashMap::new(),
        },
    }];
    let config = make_three_body_config(exit_routes);
    let (executor, store) = make_executor(Arc::clone(&invoker));

    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");
    let final_state = executor
        .run_compiled(&compiled, json!({"skip_main": false}))
        .await
        .expect("run ok");

    let traces = store.recorded_traces();
    let ids: Vec<String> = traces.iter().map(|t| t.plugin_id.clone()).collect();
    assert_eq!(
        ids,
        vec![
            "run_init",
            "body_enter",
            "body_enter",
            "main_s",
            "body_route",
            "body_enter",
            "run_finalize"
        ],
        "窗口轨迹序列: {ids:?}"
    );
    // should_stop 出生在 step 窗口（main_s）
    assert_eq!(
        traces[3].patch_data,
        json!({"should_stop": true}),
        "step 窗口记 should_stop 写入"
    );
    // 折算在体间窗口：body_route 恰含 ended=true（exit 体结束时的第二个转移
    // 窗口无变更，不落轨迹）
    let route_win = traces
        .iter()
        .find(|t| t.plugin_id == "body_route")
        .expect("应有 body_route 窗口");
    assert_eq!(route_win.patch_data, json!({"ended": true}));
    assert_eq!(
        final_state.get("ended").and_then(|v| v.as_bool()),
        Some(true)
    );
}
