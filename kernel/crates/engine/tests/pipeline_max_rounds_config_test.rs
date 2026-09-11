// @feature: FP-0.2.引擎 轮数硬上限 | @vision: V1 可观测可干预 | @ci: rust-test
//! 管道 YAML `max_rounds:` 字段接线（ADR 2026-09-11-engine-loop-cap 归档补记）：
//! YAML → PipelineConfig → CompiledPipeline → executor 注入全链路。
//!
//! 1. YAML 带 `max_rounds: 50` → 编译产物携带 Some(50)，按 api 接线模式注入后
//!    while 恒真循环恰 50 轮 fail-closed 报错
//! 2. YAML 无该字段 → None（旧 YAML 行为完全不变）：不注入，引擎缺省 200 生效；
//!    None 序列化不出现 `max_rounds` 键（旧 YAML config_hash 稳定的机理）
//! 3. YAML `max_rounds: 0` → Some(0) 注入后 run 入口报错拒绝（零执行）

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use agentos_core::traits::{PluginInvoker, StorageBackend};
use agentos_core::types::{
    EngineError, MessageRecord, PipelineConfig, PluginContext, PluginError, PluginResult,
    RunRecord, RunStatus, StepLibrary, TenantContext, ToolExecutionResult, TraceEntry,
};
use agentos_engine::compiler::compile_pipeline;
use agentos_engine::PipelineExecutor;
use async_trait::async_trait;
use serde_json::json;

/// 计数 MockInvoker：统计每插件调用次数，永不写 ended（构造恒真循环）。
struct CountingInvoker {
    calls: Mutex<HashMap<String, usize>>,
}

impl CountingInvoker {
    fn new() -> Self {
        Self {
            calls: Mutex::new(HashMap::new()),
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
        let mut calls = self.calls.lock().unwrap();
        *calls.entry(plugin_id.to_string()).or_insert(0) += 1;
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
        _plugin_id: &str,
        _hook: agentos_core::traits::LifecycleHook,
        _context: &agentos_core::traits::HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
}

/// 空 StorageBackend（持久化全部成功空操作，与 round_cap_test 同款）。
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
        Err(agentos_core::types::StorageError::NotFound("null".into()))
    }
    async fn get_user_by_username(
        &self,
        _username: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        Err(agentos_core::types::StorageError::NotFound("null".into()))
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

/// 解析管道 YAML 文本为 PipelineConfig（api 层 PipelineFile 归一后的同一引擎类型）。
fn parse_yaml(yaml: &str) -> PipelineConfig {
    serde_yaml::from_str(yaml).expect("yaml 解析成功")
}

/// while 恒真单循环体管道的 YAML 模板（`max_rounds` 行可省/自定义）。
fn while_true_yaml(max_rounds_line: Option<&str>) -> String {
    let mut yaml = String::from(
        "name: cap_yaml\n\
         loop_bodies:\n\
         \x20 - id: main\n\
         \x20   while: \"True\"\n\
         \x20   steps:\n\
         \x20     - id: s\n\
         \x20       steps:\n\
         \x20         - p_main\n",
    );
    if let Some(line) = max_rounds_line {
        yaml.push_str(line);
    }
    yaml
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
        "b",
    )
}

/// api 层接线模式（server.rs 构造点同款）：编译产物携带值则注入，None 用缺省。
fn wire(
    compiled: &agentos_engine::compiler::CompiledPipeline,
    executor: PipelineExecutor,
) -> PipelineExecutor {
    match compiled.max_rounds {
        Some(n) => executor.with_max_rounds(n),
        None => executor,
    }
}

/// (a) YAML `max_rounds: 50`：值流经 PipelineConfig → CompiledPipeline，
/// 按接线模式注入后 while 恒真循环恰 50 轮 fail-closed 报错。
#[tokio::test]
async fn yaml_max_rounds_flows_through_compiled_to_executor() {
    let config = parse_yaml(&while_true_yaml(Some("max_rounds: 50\n")));
    assert_eq!(config.max_rounds, Some(50), "YAML 字段进入 PipelineConfig");

    let compiled = compile_pipeline(
        &config,
        &StepLibrary::default(),
        &["p_main".to_string()].into_iter().collect(),
    )
    .expect("compile ok");
    assert_eq!(
        compiled.max_rounds,
        Some(50),
        "编译产物透传 YAML 声明的上限"
    );

    let invoker = Arc::new(CountingInvoker::new());
    let executor = wire(&compiled, make_executor(Arc::clone(&invoker)));
    let err = executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect_err("恒真循环应在上限处报错");
    let msg = format!("{err}");
    assert!(msg.contains("轮数超限"), "err: {msg}");
    assert!(msg.contains("50"), "错误应点名 YAML 配置的上限：{msg}");
    assert_eq!(
        invoker.call_count("p_main"),
        50,
        "executor 实际收到 50：恰执行 50 轮后终止"
    );
}

/// (b) YAML 无 `max_rounds` 字段：None 贯穿（旧 YAML 行为完全不变），
/// 不注入 → 引擎缺省 200 生效；None 序列化无 `max_rounds` 键（config_hash 稳定机理）。
#[tokio::test]
async fn yaml_without_field_keeps_default_behavior() {
    let config = parse_yaml(&while_true_yaml(None));
    assert_eq!(config.max_rounds, None, "缺字段 → None");

    // 旧 YAML 兼容机理：None 不序列化进 config JSON（不改变既有 config_hash）
    let serialized = serde_json::to_value(&config).expect("serialize ok");
    assert!(
        serialized.get("max_rounds").is_none(),
        "None 不得出现在序列化产物（否则旧 YAML config_hash 漂移）: {serialized}"
    );

    let compiled = compile_pipeline(
        &config,
        &StepLibrary::default(),
        &["p_main".to_string()].into_iter().collect(),
    )
    .expect("compile ok");
    assert_eq!(compiled.max_rounds, None, "编译产物携带 None");

    let invoker = Arc::new(CountingInvoker::new());
    let executor = wire(&compiled, make_executor(Arc::clone(&invoker)));
    let err = executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect_err("恒真循环应在缺省上限处报错");
    let msg = format!("{err}");
    assert!(msg.contains("200"), "缺省 200 生效：{msg}");
    assert_eq!(
        invoker.call_count("p_main"),
        200,
        "缺省行为与旧 YAML 完全一致"
    );
}

/// (c) YAML `max_rounds: 0`：Some(0) 透传，注入后 run 入口报错拒绝（零执行）。
#[tokio::test]
async fn yaml_zero_max_rounds_rejected_at_run_entry() {
    let config = parse_yaml(&while_true_yaml(Some("max_rounds: 0\n")));
    assert_eq!(config.max_rounds, Some(0));

    let compiled = compile_pipeline(
        &config,
        &StepLibrary::default(),
        &["p_main".to_string()].into_iter().collect(),
    )
    .expect("compile ok");
    assert_eq!(
        compiled.max_rounds,
        Some(0),
        "0 值原样透传（不在编译期拦截）"
    );

    let invoker = Arc::new(CountingInvoker::new());
    let executor = wire(&compiled, make_executor(Arc::clone(&invoker)));
    let err = executor
        .run_compiled(&compiled, json!({}))
        .await
        .expect_err("max_rounds=0 必须被 run 入口拒绝");
    match &err {
        EngineError::Config { message } => {
            assert!(message.contains("max_rounds=0"), "err: {message}");
            assert!(message.contains("非法"), "err: {message}");
        }
        other => panic!("期望 EngineError::Config，得到 {other:?}"),
    }
    assert_eq!(invoker.call_count("p_main"), 0, "入口拒绝，零插件调用");
}

/// (d) 回归控制：缺字段反序列化配 `LoopBody`/`initial_state` 等既有键零影响
/// （serde default 只补新键，不改既有解析）。
#[test]
fn yaml_deserialize_of_legacy_shape_unchanged() {
    let yaml = "name: legacy\n\
                loop_bodies:\n\
                \x20 - id: main\n\
                \x20   steps:\n\
                \x20     - id: s\n\
                \x20       steps:\n\
                \x20         - p\n\
                initial_state:\n\
                \x20 k: v\n";
    let config: PipelineConfig = serde_yaml::from_str(yaml).expect("legacy yaml 解析成功");
    assert_eq!(config.name, "legacy");
    assert_eq!(config.max_rounds, None);
    assert_eq!(
        config.initial_state.get("k").and_then(|v| v.as_str()),
        Some("v"),
        "既有键解析不受新字段影响"
    );
}
