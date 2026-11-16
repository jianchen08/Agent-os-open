// @feature: 管道引擎 run 超时执法（timeout_seconds 墙钟看门狗）| @vision: V1 可观测可干预
//! run 主循环对 `state.timeout_seconds`（agent_config_load 注入，stop_check 同源
//! 语义：单次 run 执行 ≤ timeout_seconds）的引擎层执法：
//!
//! 1. 预算在场（≥0）：run 从起始锚点计墙钟，轮次等待（step 插件调用 / 轮次
//!    事件）被剩余预算包界——到点放弃冻结等待、强制署名终态化
//!    （`ended` + `router.stop_reason="timeout"`，既有 FAILED_STOP_REASONS
//!    折算路径 → failed），run_finalize / persist_run_end 正常落库；
//! 2. 键缺席 / 负值：不设防——行为与旧版完全一致（冻结 run 不被终态化）；
//! 3. 预算内正常完成不被误杀（不提前执法，边界单调：未到点不终态化）。
//!
//! 时钟纪律：tokio paused time（`start_paused`）作可注入 fake clock，
//! `advance` 按真实预算量推时断言时序不变量——禁止零延迟 mock。
//! 冻结模拟：invoker 的插件调用 future 永不就绪（外部依赖挂起，mock 属
//! 外部依赖替身，合规）。

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use agentos_core::traits::{PluginInvoker, StorageBackend};
use agentos_core::types::{
    MessageRecord, PipelineConfig, PluginContext, PluginError, PluginResult, RunRecord,
    StepLibrary, TenantContext, ToolExecutionResult, TraceEntry,
};
use agentos_engine::compiler::compile_pipeline;
use agentos_engine::PipelineExecutor;
use async_trait::async_trait;
use serde_json::{json, Value};

/// 冻结注入 Invoker：对 `hang_plugin` 的调用先置位 entered（测试同步点：预算
/// 包装已武装）再永久挂起（模拟 LLM 流停滞 / sidecar 卡死——装机 2026-10-15
/// 案例：resource_search_tool in-flight 卡死 30min+）；对 `finisher_plugin`
/// 的调用立即成功并写 `ended=true`（预算内正常收束对照面）。
struct WatchdogInvoker {
    hang_plugin: &'static str,
    finisher_plugin: &'static str,
    hang_entered: AtomicBool,
}

impl WatchdogInvoker {
    fn new() -> Self {
        Self {
            hang_plugin: "p_hang",
            finisher_plugin: "p_finish",
            hang_entered: AtomicBool::new(false),
        }
    }

    fn hang_entered(&self) -> bool {
        self.hang_entered.load(Ordering::SeqCst)
    }
}

#[async_trait]
impl PluginInvoker for WatchdogInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        _ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        if plugin_id == self.hang_plugin {
            self.hang_entered.store(true, Ordering::SeqCst);
            // 外部依赖挂起替身：永不就绪（冻结语义），非零延迟 mock。
            std::future::pending().await
        } else if plugin_id == self.finisher_plugin {
            Ok(PluginResult {
                state_updates: std::collections::HashMap::from([(
                    "ended".to_string(),
                    json!(true),
                )]),
                ..PluginResult::default()
            })
        } else {
            Ok(PluginResult::default())
        }
    }

    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        Ok(ToolExecutionResult::success(Value::Null))
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

/// 录制型 StorageBackend：持久化空操作 + 录制 upsert_state_fields 批次
/// （断言终态 run_status=failed + run_ended_at 真实落库面）。
#[derive(Default)]
struct RecordingStorage {
    upserts: Mutex<Vec<(String, Value)>>,
}

impl RecordingStorage {
    fn upsert_keys_snapshot(&self) -> Vec<(String, Value)> {
        self.upserts.lock().unwrap().clone()
    }
}

#[async_trait]
impl StorageBackend for RecordingStorage {
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
        _session_id: &str,
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
    ) -> Result<Vec<TraceEntry>, agentos_core::types::StorageError> {
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
    async fn list_users(
        &self,
    ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn get_user_by_username(
        &self,
        _username: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        Err(agentos_core::types::StorageError::NotFound("null".into()))
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
        unreachable!("RecordingStorage 不提供口令更新")
    }
    async fn delete_user(&self, _user_id: &str) -> Result<bool, agentos_core::types::StorageError> {
        Ok(false)
    }
    async fn upsert_state_fields(
        &self,
        pipeline_id: &str,
        _tenant_id: &str,
        fields: &serde_json::Map<String, Value>,
    ) -> Result<(), agentos_core::types::StorageError> {
        let mut upserts = self.upserts.lock().unwrap();
        for (key, value) in fields {
            upserts.push((format!("{pipeline_id}:{key}"), value.clone()));
        }
        Ok(())
    }
}

/// 单步单循环体管道：唯一 step 调用 `p_hang`（冻结注入点）。
fn hang_pipeline() -> (
    PipelineExecutor,
    agentos_engine::compiler::CompiledPipeline,
    Arc<WatchdogInvoker>,
    Arc<RecordingStorage>,
) {
    let yaml = "name: wd\n\
                loop_bodies:\n\
                \x20- id: main\n\
                \x20  steps:\n\
                \x20  - id: s\n\
                \x20    steps:\n\
                \x20    - p_hang\n";
    let invoker = Arc::new(WatchdogInvoker::new());
    let storage = Arc::new(RecordingStorage::default());
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        TenantContext::new("t", "s"),
        vec!["p_hang".to_string()],
        storage.clone() as Arc<dyn StorageBackend>,
        "r-wd",
    );
    let config: PipelineConfig = serde_yaml::from_str(yaml).expect("yaml 解析成功");
    let compiled = compile_pipeline(
        &config,
        &StepLibrary::default(),
        &["p_hang".to_string()].into_iter().collect(),
    )
    .expect("compile ok");
    (executor, compiled, invoker, storage)
}

/// 轮询至挂起调用已被进入（预算包装已武装）。
async fn wait_hang_entered(invoker: &WatchdogInvoker) {
    while !invoker.hang_entered() {
        tokio::time::sleep(Duration::from_millis(1)).await;
    }
}

/// (1) 冻结 beyond 预算 → 强制终态化：Ok 返回 + failed + timeout 署名 +
/// run_ended_at 落库；预算未尽不提前执法（边界单调性质）。
#[tokio::test(start_paused = true)]
async fn hang_beyond_budget_finalizes_failed_with_timeout_signature() {
    let (executor, compiled, invoker, storage) = hang_pipeline();
    let started = tokio::time::Instant::now();
    // executor/compiled move 进任务（run_compiled 借用两者，spawn 需 'static）。
    let handle = tokio::spawn(async move {
        executor
            .run_compiled(&compiled, json!({ "timeout_seconds": 60 }))
            .await
    });

    wait_hang_entered(&invoker).await;

    // 边界下侧：预算未尽（59s < 60s）不得提前终态化。
    tokio::time::advance(Duration::from_secs(59)).await;
    assert!(!handle.is_finished(), "预算未尽（59s/60s）不得提前终态化");

    // 边界上侧：过点 → 冻结等待被放弃，run 强制终态化。
    tokio::time::advance(Duration::from_secs(2)).await;
    let state = tokio::time::timeout(Duration::from_secs(5), handle)
        .await
        .expect("过点 5s（虚拟钟）run 仍未终态化——看门狗未执法")
        .expect("run 任务不得 panic")
        .expect("run_compiled 应 Ok 返回（走既有收尾路径，非 Err 翻车）");

    // 字面值断言 + 性质断言（终态化时刻 ≥ 预算上界）。
    assert_eq!(
        state["run_status"],
        json!("failed"),
        "终态映射 failed: {state}"
    );
    assert_eq!(
        state["router.stop_reason"],
        json!("timeout"),
        "署名词须命中 FAILED_STOP_REASONS 既有词表"
    );
    assert_eq!(state["ended"], json!(true), "控制键 ended 置位");
    assert!(
        tokio::time::Instant::now().duration_since(started) >= Duration::from_secs(60),
        "终态化时刻必须 ≥ 预算上界（不得提前执法）"
    );

    // 落库面：run_status=failed + run_ended_at 经 persist_run_end upsert。
    let upserts = storage.upsert_keys_snapshot();
    assert!(
        upserts
            .iter()
            .any(|(k, v)| k.ends_with(":run_status") && v == &json!("failed")),
        "终态 run_status=failed 必须落库: {upserts:?}"
    );
    assert!(
        upserts
            .iter()
            .any(|(k, v)| k.ends_with(":run_ended_at") && v.is_string()),
        "run_ended_at 必须落库: {upserts:?}"
    );
}

/// (2) 键缺席 / 负值 = 不设防：行为与旧版一致（冻结 run 不被终态化）。
/// 可枚举输入展开：缺席、-1（stop_check 无限制哨兵同款）。
#[tokio::test(start_paused = true)]
async fn without_budget_watchdog_stays_off() {
    for state0 in [json!({}), json!({ "timeout_seconds": -1 })] {
        let (executor, compiled, invoker, _storage) = hang_pipeline();
        let handle = tokio::spawn(async move { executor.run_compiled(&compiled, state0).await });
        wait_hang_entered(&invoker).await;
        tokio::time::advance(Duration::from_secs(3_600)).await;
        assert!(
            !handle.is_finished(),
            "无预算（缺席/负值）看门狗不得执法——旧版行为不变"
        );
        handle.abort();
    }
}

/// (3) timeout_seconds=0：立即到点（stop_check 同语义：elapsed > 0 恒真），
/// 零推进即终态化。
#[tokio::test(start_paused = true)]
async fn zero_budget_finalizes_immediately() {
    let (executor, compiled, invoker, _storage) = hang_pipeline();
    let handle = tokio::spawn(async move {
        executor
            .run_compiled(&compiled, json!({ "timeout_seconds": 0 }))
            .await
    });
    wait_hang_entered(&invoker).await;
    let state = tokio::time::timeout(Duration::from_secs(5), handle)
        .await
        .expect("0 预算应立即终态化")
        .expect("run 任务不得 panic")
        .expect("run_compiled 应 Ok 返回");
    assert_eq!(state["run_status"], json!("failed"));
    assert_eq!(state["router.stop_reason"], json!("timeout"));
}

/// (4) 预算内正常完成不被误杀：插件当轮写 ended 收束，run 正常 Completed，
/// 署名不得被看门狗污染（符号量级相反对照面）。
#[tokio::test(start_paused = true)]
async fn completing_within_budget_is_untouched() {
    let yaml = "name: wd_ok\n\
                loop_bodies:\n\
                \x20- id: main\n\
                \x20  steps:\n\
                \x20  - id: s\n\
                \x20    steps:\n\
                \x20    - p_finish\n";
    let invoker = Arc::new(WatchdogInvoker::new());
    let storage = Arc::new(RecordingStorage::default());
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        TenantContext::new("t", "s"),
        vec!["p_finish".to_string()],
        storage.clone() as Arc<dyn StorageBackend>,
        "r-ok",
    );
    let config: PipelineConfig = serde_yaml::from_str(yaml).expect("yaml 解析成功");
    let compiled = compile_pipeline(
        &config,
        &StepLibrary::default(),
        &["p_finish".to_string()].into_iter().collect(),
    )
    .expect("compile ok");

    let state = executor
        .run_compiled(&compiled, json!({ "timeout_seconds": 60 }))
        .await
        .expect("预算内正常收束");
    assert_eq!(
        state["run_status"],
        json!("completed"),
        "正常收束不得被看门狗改写: {state}"
    );
    assert_ne!(state["router.stop_reason"], json!("timeout"));
    let upserts = storage.upsert_keys_snapshot();
    assert!(
        upserts
            .iter()
            .any(|(k, v)| k.ends_with(":run_status") && v == &json!("completed")),
        "正常终态 completed 落库: {upserts:?}"
    );
}
