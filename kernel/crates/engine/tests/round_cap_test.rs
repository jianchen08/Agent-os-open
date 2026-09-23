// @feature: FP-0.2.〇 管道引擎轮数硬上限 | @vision: V1 可观测可干预 | @ci: rust-test
//! 主循环轮数硬上限（max_rounds，fail-closed）+ DSL step 循环 max_iterations=0
//! 非法值守卫。
//!
//! 1. while 恒真（插件永不写 ended）→ 缺省 200 轮兜底报错，非悬挂
//! 2. 配置较小 max_rounds → 按配置值截断
//! 3. 正常提前收敛的循环不受上限影响
//! 4. max_rounds=0 → run 入口拒绝（0 = 无限轮非法）
//! 5. DSL loop_config.max_iterations=0 → 报错终止 run（旧语义 0 = 无限）
//! 6. DSL loop_config.max_iterations>0 → 上限语义保持（回归控制）

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use agentos_core::traits::{PluginInvoker, StorageBackend};
use agentos_core::types::{
    LoopBody, LoopConfig, MessageRecord, PipelineConfig, PipelineStep, PluginContext, PluginError,
    PluginResult, RunRecord, StepLibrary, TenantContext, ToolExecutionResult, TraceEntry,
};
use agentos_engine::compiler::compile_pipeline;
use agentos_engine::PipelineExecutor;
use async_trait::async_trait;
use serde_json::json;

/// 计数 MockInvoker：统计每插件调用次数；`end_after` 声明的插件从第 N 次调用
/// 起在 state_updates 写 ended=true（收敛测试用）。
struct CountingInvoker {
    calls: Mutex<HashMap<String, usize>>,
    end_after: HashMap<String, usize>,
}

impl CountingInvoker {
    fn new(end_after: HashMap<String, usize>) -> Self {
        Self {
            calls: Mutex::new(HashMap::new()),
            end_after,
        }
    }

    fn call_count(&self, plugin_id: &str) -> usize {
        *self.calls.lock().unwrap().get(plugin_id).unwrap_or(&0)
    }
}

#[async_trait]
impl PluginInvoker for CountingInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        _ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        let n = {
            let mut calls = self.calls.lock().unwrap();
            let n = calls.entry(plugin_id.to_string()).or_insert(0);
            *n += 1;
            *n
        };
        let mut result = PluginResult::default();
        if let Some(threshold) = self.end_after.get(plugin_id) {
            if n >= *threshold {
                result
                    .state_updates
                    .insert("ended".to_string(), json!(true));
            }
        }
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

/// 空 StorageBackend（持久化全部成功空操作，与 multi_body_test 同款）。
struct NullStorage;

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
        _entry: TraceEntry,
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

/// 单循环体管道：main 体 while 驱动（或单次执行），step 引用 `p_main`。
fn make_config(while_cond: Option<String>, loop_config: Option<LoopConfig>) -> PipelineConfig {
    PipelineConfig {
        name: "round_cap".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "main_s".into(),
                steps: vec!["p_main".into()],
                when: None,
                context: HashMap::new(),
                routes: vec![],
                loop_config,
            }],
            while_cond,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: HashMap::new(),
        max_rounds: None,
    }
}

fn make_executor(invoker: Arc<CountingInvoker>) -> PipelineExecutor {
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage);
    PipelineExecutor::new(
        invoker as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        TenantContext::new("t", "s"),
        vec!["p_main".to_string()],
        store,
        "r",
    )
}

async fn run(
    config: &PipelineConfig,
    executor: &PipelineExecutor,
) -> Result<serde_json::Value, agentos_core::types::EngineError> {
    let compiled = compile_pipeline(config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");
    executor.run_compiled(&compiled, json!({})).await
}

/// (a) while 恒真 + 缺省上限：200 轮处 fail-closed 报错，非悬挂。
#[tokio::test]
async fn while_true_hits_default_cap_and_errors() {
    let invoker = Arc::new(CountingInvoker::new(HashMap::new()));
    let config = make_config(Some("True".into()), None);
    let executor = make_executor(Arc::clone(&invoker));

    let result = run(&config, &executor).await;
    let err = result.expect_err("while 恒真必须在上限处报错终止");
    let msg = format!("{err}");
    assert!(msg.contains("轮数超限"), "err: {msg}");
    assert!(msg.contains("200"), "错误应点名缺省上限 200：{msg}");
    assert_eq!(
        invoker.call_count("p_main"),
        200,
        "上限 = 恰好执行 200 轮后报错"
    );
}

/// (b) 配置较小 max_rounds 生效：3 轮截断。
#[tokio::test]
async fn small_max_rounds_truncates_loop() {
    let invoker = Arc::new(CountingInvoker::new(HashMap::new()));
    let config = make_config(Some("True".into()), None);
    let executor = make_executor(Arc::clone(&invoker)).with_max_rounds(3);

    let result = run(&config, &executor).await;
    let err = result.expect_err("超限应报错");
    let msg = format!("{err}");
    assert!(msg.contains("3"), "错误应点名配置的上限：{msg}");
    assert_eq!(invoker.call_count("p_main"), 3);
}

/// (c) 正常提前收敛的循环不受上限影响（第 2 轮插件写 ended → 2 轮即止，run 成功）。
#[tokio::test]
async fn early_converging_loop_unaffected_by_cap() {
    let mut end_after = HashMap::new();
    end_after.insert("p_main".to_string(), 2usize);
    let invoker = Arc::new(CountingInvoker::new(end_after));
    let config = make_config(Some("True".into()), None);
    let executor = make_executor(Arc::clone(&invoker));

    let final_state = run(&config, &executor)
        .await
        .expect("提前收敛的循环应正常完成");
    assert_eq!(invoker.call_count("p_main"), 2);
    assert_eq!(
        final_state.get("ended").and_then(|v| v.as_bool()),
        Some(true)
    );
}

/// (d) max_rounds=0 非法：run 入口拒绝，零插件调用。
#[tokio::test]
async fn zero_max_rounds_rejected_at_entry() {
    let invoker = Arc::new(CountingInvoker::new(HashMap::new()));
    let config = make_config(Some("True".into()), None);
    let executor = make_executor(Arc::clone(&invoker)).with_max_rounds(0);

    let result = run(&config, &executor).await;
    let err = result.expect_err("max_rounds=0 必须被拒绝");
    let msg = format!("{err}");
    assert!(msg.contains("非法"), "err: {msg}");
    assert!(msg.contains("max_rounds"), "err: {msg}");
    assert_eq!(invoker.call_count("p_main"), 0, "入口拒绝，零执行");
}

/// (e) DSL step 循环 max_iterations=0 非法：报错终止 run（旧语义 0 = 无限循环）。
#[tokio::test]
async fn dsl_loop_zero_iterations_rejected() {
    let invoker = Arc::new(CountingInvoker::new(HashMap::new()));
    let config = make_config(
        None,
        Some(LoopConfig {
            enabled: true,
            max_iterations: 0,
        }),
    );
    let executor = make_executor(Arc::clone(&invoker));

    let result = run(&config, &executor).await;
    let err = result.expect_err("max_iterations=0 必须报错");
    let msg = format!("{err}");
    assert!(msg.contains("max_iterations=0"), "err: {msg}");
    assert!(msg.contains("main_s"), "错误应定位到 step：{msg}");
    assert_eq!(invoker.call_count("p_main"), 0, "非法配置零执行");
}

/// (f) 回归控制：DSL step 循环 max_iterations>0 上限语义保持（2 轮即止，run 成功）。
#[tokio::test]
async fn dsl_loop_positive_bound_still_works() {
    let invoker = Arc::new(CountingInvoker::new(HashMap::new()));
    let config = make_config(
        None,
        Some(LoopConfig {
            enabled: true,
            max_iterations: 2,
        }),
    );
    let executor = make_executor(Arc::clone(&invoker));

    run(&config, &executor)
        .await
        .expect("正整数上限的 DSL 循环应正常完成");
    assert_eq!(
        invoker.call_count("p_main"),
        2,
        "max_iterations=2 恰执行 2 轮"
    );
}
