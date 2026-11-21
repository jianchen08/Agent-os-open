// @feature: FP-0.2.〇 插件调用单次超时包装(冻结根因根修,invoke_plugin 单次上限三分支取最小) | @vision: V1 | @ci: rust-test
//! 冻结根因（装机版缺陷清偿计划 20261015 §二）：插件管线步调用无单次上限——
//! 挂死成员冻结整 run，stop_check 轮边界执法等不到轮结束。3a3622520 run 级
//! 看门狗兜住症状；本刀根修：`invoke_plugin` 单次调用恒有上界，三分支取最小：
//! ① state `plugin_call_timeout_seconds`（步级/run 级配置出口）；② run 剩余
//! 预算（`timeout_seconds` 同源看门狗；单次调用永不越过 run 预算）；③ 兜底
//! 缺省（依据见引擎 `DEFAULT_PLUGIN_CALL_TIMEOUT_SECS`）。到点走既有步失败
//! 路径：`_plugin_errors` 记 `code=STEP_CALL_TIMEOUT`，与 run 级署名
//! `router.stop_reason="timeout"` 可辨别；run 由 stop_check/熔断/轮上限接管。
//!
//! 时钟纪律：tokio paused time + `advance` 可调延迟（复用
//! run_timeout_watchdog_test 基建形态），禁零延迟 mock；冻结模拟 = 永不就绪
//! future（外部依赖挂起替身，合规）。

use std::collections::HashSet;
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

/// 冻结/慢速注入 Invoker：`p_hang` 先置位 entered（同步点：单次超时包装已
/// 武装）再永久挂起（模拟 sidecar 卡死——装机 2026-10-15 案例：
/// resource_search_tool inflight 挂 30min+）。`p_finish` 立即成功并写
/// `ended=true`——同轮**后续 step** 照常执行即「步失败后 run 继续推进」的
/// 直接证据。`p_slow` 挂 5s 虚拟钟后成功（限内慢成员不被误杀的对照面）。
struct CallTimeoutInvoker {
    hang_plugin: &'static str,
    finisher_plugin: &'static str,
    slow_plugin: &'static str,
    hang_entered: AtomicBool,
    slow_entered: AtomicBool,
}

impl CallTimeoutInvoker {
    fn new() -> Self {
        Self {
            hang_plugin: "p_hang",
            finisher_plugin: "p_finish",
            slow_plugin: "p_slow",
            hang_entered: AtomicBool::new(false),
            slow_entered: AtomicBool::new(false),
        }
    }

    fn hang_entered(&self) -> bool {
        self.hang_entered.load(Ordering::SeqCst)
    }

    fn slow_entered(&self) -> bool {
        self.slow_entered.load(Ordering::SeqCst)
    }
}

#[async_trait]
impl PluginInvoker for CallTimeoutInvoker {
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
        } else if plugin_id == self.slow_plugin {
            self.slow_entered.store(true, Ordering::SeqCst);
            // 可调延迟慢成员：5s 虚拟钟（timer，可被 advance 驱动），限内合法。
            tokio::time::sleep(Duration::from_secs(5)).await;
            Ok(PluginResult::default())
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
/// （断言终态 run_status 真实落库面）。
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

/// 两步单循环体管道：`p_hang`（冻结注入点）+ `p_finish`（续行证明步——
/// 挂死步失败后照常执行，写 ended 收束）。
fn hang_then_finish_pipeline() -> (PipelineConfig, Vec<&'static str>) {
    let yaml = "name: pct\n\
                loop_bodies:\n\
                \x20- id: main\n\
                \x20  steps:\n\
                \x20  - id: s\n\
                \x20    steps:\n\
                \x20    - p_hang\n\
                \x20    - p_finish\n";
    (
        serde_yaml::from_str(yaml).expect("yaml 解析成功"),
        vec!["p_hang", "p_finish"],
    )
}

/// 单步单循环体管道：唯一 step 调用 `plugin_id`（慢速注入点）。
fn single_step_pipeline(plugin_id: &'static str) -> (PipelineConfig, Vec<&'static str>) {
    let yaml = format!(
        "name: pct\n\
         loop_bodies:\n\
         \x20- id: main\n\
         \x20  steps:\n\
         \x20  - id: s\n\
         \x20    steps:\n\
         \x20    - {plugin_id}\n"
    );
    (
        serde_yaml::from_str(&yaml).expect("yaml 解析成功"),
        vec![plugin_id],
    )
}

/// 组装执行器 + 编译产物（invoker 已 Arc 共享供同步点轮询）。
fn setup(
    config: PipelineConfig,
    known: &[&str],
) -> (
    PipelineExecutor,
    agentos_engine::compiler::CompiledPipeline,
    Arc<CallTimeoutInvoker>,
    Arc<RecordingStorage>,
) {
    let invoker = Arc::new(CallTimeoutInvoker::new());
    let storage = Arc::new(RecordingStorage::default());
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        TenantContext::new("t", "s"),
        known.iter().map(|s| s.to_string()),
        storage.clone() as Arc<dyn StorageBackend>,
        "r-pct",
    );
    let compiled = compile_pipeline(
        &config,
        &StepLibrary::default(),
        &known.iter().map(|s| s.to_string()).collect::<HashSet<_>>(),
    )
    .expect("compile ok");
    (executor, compiled, invoker, storage)
}

/// 轮询至挂起调用已被进入（单次超时包装已武装）。
async fn wait_hang_entered(invoker: &CallTimeoutInvoker) {
    while !invoker.hang_entered() {
        tokio::time::sleep(Duration::from_millis(1)).await;
    }
}

/// 轮询至慢速调用已被进入。
async fn wait_slow_entered(invoker: &CallTimeoutInvoker) {
    while !invoker.slow_entered() {
        tokio::time::sleep(Duration::from_millis(1)).await;
    }
}

/// 断言 `_plugin_errors` 含指定 code 的条目（步失败署名面）。
fn assert_step_call_timeout_recorded(state: &Value) {
    let errors = state["_plugin_errors"]
        .as_array()
        .expect("_plugin_errors 数组");
    assert!(
        errors
            .iter()
            .any(|e| e["code"] == json!("STEP_CALL_TIMEOUT")),
        "单次超时须以 STEP_CALL_TIMEOUT 署名进 _plugin_errors: {errors:?}"
    );
}

/// (1) 配置档：`plugin_call_timeout_seconds` 收紧上界（30s < 基础界）——
/// 到点步失败、run 继续。边界单调：29s 不得执法。
///
/// [2026-10-27 调整] 原兜底缺省档（无配置 600s 包界挂死）随基础策略档迁
/// invoker 单点（`agentos_invoker::invoker::resolve_call_limit` /
/// `with_raisable_call_timeout`，manifest 自报 ∩ state 收紧 ∩ 缺省 600s，
/// L1 让位 + M3 单一解析点，ADR 2026-11-05-timeout-taxonomy-and-merge）：
/// 引擎在本层只做 run 级封顶（配置/预算在场才加壳；state 键解析单源 =
/// `agentos_invoker::invoker::state_plugin_call_timeout`），缺省档用例随迁
/// invoker 车道。
#[tokio::test(start_paused = true)]
async fn config_tier_overrides_default() {
    let started = tokio::time::Instant::now();
    let (config, known) = hang_then_finish_pipeline();
    let (executor, compiled, invoker, _storage) = setup(config, &known);
    let handle = tokio::spawn(async move {
        executor
            .run_compiled(&compiled, json!({ "plugin_call_timeout_seconds": 30 }))
            .await
    });

    wait_hang_entered(&invoker).await;
    tokio::time::advance(Duration::from_secs(29)).await;
    tokio::task::yield_now().await; // advance 后让唤醒任务真实执行（边界断言有牙）
    assert!(!handle.is_finished(), "配置上界未尽（29s/30s）不得提前执法");

    tokio::time::advance(Duration::from_secs(2)).await;
    let state = tokio::time::timeout(Duration::from_secs(5), handle)
        .await
        .expect("配置上界到点后 run 仍未完成——配置档未生效")
        .expect("run 任务不得 panic")
        .expect("run_compiled 应 Ok 返回");

    assert_eq!(
        state["run_status"],
        json!("completed"),
        "步失败后继续收束: {state}"
    );
    assert_ne!(state["router.stop_reason"], json!("timeout"));
    assert_step_call_timeout_recorded(&state);
    assert!(
        tokio::time::Instant::now().duration_since(started) >= Duration::from_secs(30),
        "步失败时刻必须 ≥ 配置上界"
    );
}

/// (3) 预算档：`timeout_seconds` 在场且小于缺省（45s < 600s）——单次调用被
/// 剩余预算包界；预算同时是 run 级契约，耗尽后 run 级看门狗照常终态化。
/// 两署名同场可辨：`_plugin_errors` 有 STEP_CALL_TIMEOUT（步级）+
/// `router.stop_reason="timeout"`（run 级）。
#[tokio::test(start_paused = true)]
async fn budget_tier_bounds_call_and_signatures_distinguishable() {
    let (config, known) = hang_then_finish_pipeline();
    let (executor, compiled, invoker, storage) = setup(config, &known);
    let handle = tokio::spawn(async move {
        executor
            .run_compiled(&compiled, json!({ "timeout_seconds": 45 }))
            .await
    });

    wait_hang_entered(&invoker).await;
    tokio::time::advance(Duration::from_secs(44)).await;
    tokio::task::yield_now().await; // advance 后让唤醒任务真实执行（边界断言有牙）
    assert!(!handle.is_finished(), "预算未尽（44s/45s）不得提前执法");

    tokio::time::advance(Duration::from_secs(2)).await;
    let state = tokio::time::timeout(Duration::from_secs(5), handle)
        .await
        .expect("预算到点后 run 仍未完成")
        .expect("run 任务不得 panic")
        .expect("run_compiled 应 Ok 返回");

    assert_eq!(
        state["run_status"],
        json!("failed"),
        "预算耗尽 run 级折算 failed: {state}"
    );
    assert_eq!(
        state["router.stop_reason"],
        json!("timeout"),
        "run 级 timeout 署名在场: {state}"
    );
    assert_step_call_timeout_recorded(&state);

    let upserts = storage.upsert_keys_snapshot();
    assert!(
        upserts
            .iter()
            .any(|(k, v)| k.ends_with(":run_status") && v == &json!("failed")),
        "终态 run_status=failed 必须落库: {upserts:?}"
    );
}

/// (4a) 最小值语义·配置紧于预算：配置 30s < 预算 40s → 步失败在 30s（配置
/// 生效，预算不得放宽到 40s），预算未尽 run 正常收束。
#[tokio::test(start_paused = true)]
async fn config_tighter_than_budget_wins() {
    let (config, known) = hang_then_finish_pipeline();
    let (executor, compiled, invoker, _storage) = setup(config, &known);
    let handle = tokio::spawn(async move {
        executor
            .run_compiled(
                &compiled,
                json!({ "timeout_seconds": 40, "plugin_call_timeout_seconds": 30 }),
            )
            .await
    });

    wait_hang_entered(&invoker).await;
    tokio::time::advance(Duration::from_secs(29)).await;
    tokio::task::yield_now().await; // advance 后让唤醒任务真实执行（边界断言有牙）
    assert!(!handle.is_finished(), "配置上界未尽（29s/30s）不得提前执法");

    tokio::time::advance(Duration::from_secs(2)).await;
    let state = tokio::time::timeout(Duration::from_secs(5), handle)
        .await
        .expect("配置上界到点后 run 仍未完成——min 语义未取配置档")
        .expect("run 任务不得 panic")
        .expect("run_compiled 应 Ok 返回");

    assert_eq!(
        state["run_status"],
        json!("completed"),
        "预算未尽（30s<40s）正常收束: {state}"
    );
    assert_ne!(state["router.stop_reason"], json!("timeout"));
    assert_step_call_timeout_recorded(&state);
}

/// (4b) 最小值语义·预算紧于配置：预算 25s < 配置 30s → 步失败在 25s（单次
/// 调用永不越过 run 剩余预算），预算同步耗尽 run 级照常署名 failed+timeout，
/// 步级 STEP_CALL_TIMEOUT 与 run 级署名同场可辨。
#[tokio::test(start_paused = true)]
async fn budget_tighter_than_config_caps_call() {
    let (config, known) = hang_then_finish_pipeline();
    let (executor, compiled, invoker, _storage) = setup(config, &known);
    let handle = tokio::spawn(async move {
        executor
            .run_compiled(
                &compiled,
                json!({ "timeout_seconds": 25, "plugin_call_timeout_seconds": 30 }),
            )
            .await
    });

    wait_hang_entered(&invoker).await;
    tokio::time::advance(Duration::from_secs(24)).await;
    tokio::task::yield_now().await; // advance 后让唤醒任务真实执行（边界断言有牙）
    assert!(!handle.is_finished(), "预算未尽（24s/25s）不得提前执法");

    tokio::time::advance(Duration::from_secs(2)).await;
    let state = tokio::time::timeout(Duration::from_secs(5), handle)
        .await
        .expect("预算到点后 run 仍未完成——min 语义未取预算档")
        .expect("run 任务不得 panic")
        .expect("run_compiled 应 Ok 返回");

    assert_eq!(state["run_status"], json!("failed"));
    assert_eq!(state["router.stop_reason"], json!("timeout"));
    assert_step_call_timeout_recorded(&state);
}

/// (5) 限内慢成员零回归：5s 虚拟钟慢调用 < 配置上界 30s → 不被误杀，run
/// 正常收束、零插件错误（守卫只杀越界者，不杀慢工）。
#[tokio::test(start_paused = true)]
async fn slow_member_within_limit_is_untouched() {
    let (config, known) = single_step_pipeline("p_slow");
    let (executor, compiled, invoker, storage) = setup(config, &known);
    let handle = tokio::spawn(async move {
        executor
            .run_compiled(&compiled, json!({ "plugin_call_timeout_seconds": 30 }))
            .await
    });

    wait_slow_entered(&invoker).await;
    // 慢调用进行中（4s < 5s）：不得被放弃。
    tokio::time::advance(Duration::from_secs(4)).await;
    tokio::task::yield_now().await; // advance 后让唤醒任务真实执行（边界断言有牙）
    assert!(!handle.is_finished(), "限内慢调用不得被中途放弃");

    tokio::time::advance(Duration::from_secs(2)).await;
    let state = tokio::time::timeout(Duration::from_secs(5), handle)
        .await
        .expect("限内慢成员后 run 仍未完成")
        .expect("run 任务不得 panic")
        .expect("run_compiled 应 Ok 返回");

    assert_eq!(
        state["run_status"],
        json!("completed"),
        "限内慢成员正常收束: {state}"
    );
    assert!(
        state["_plugin_errors"]
            .as_array()
            .map(|a| a.is_empty())
            .unwrap_or(true),
        "限内慢成员不得记录插件错误: {state}"
    );
    let upserts = storage.upsert_keys_snapshot();
    assert!(
        upserts
            .iter()
            .any(|(k, v)| k.ends_with(":run_status") && v == &json!("completed")),
        "正常终态 completed 落库: {upserts:?}"
    );
}

/// (6) 错误面单点广播：步超时失败经单点 handler 广播 `plugin_step_failed`
/// 域事件（hook_bus 观察路径）——api 订阅者据此转 WS plugin_error 实时面。
/// 配置档 30s 切挂死调用，断言事件携 pipeline/thread/plugin/code 明细。
#[tokio::test(start_paused = true)]
async fn step_timeout_broadcasts_plugin_step_failed_domain_event() {
    let bus = Arc::new(agentos_hooks::HookEventBus::new(64));
    let mut rx = bus.subscribe();
    let (config, known) = hang_then_finish_pipeline();
    let invoker = Arc::new(CallTimeoutInvoker::new());
    let storage = Arc::new(RecordingStorage::default());
    let executor = PipelineExecutor::new(
        invoker.clone() as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        TenantContext::new("t", "s"),
        known.iter().map(|s| s.to_string()),
        storage.clone() as Arc<dyn StorageBackend>,
        "r-pct-ev",
    )
    .with_hook_bus(Some(bus));
    let compiled = compile_pipeline(
        &config,
        &StepLibrary::default(),
        &known.iter().map(|s| s.to_string()).collect::<HashSet<_>>(),
    )
    .expect("compile ok");
    let handle = tokio::spawn(async move {
        executor
            .run_compiled(&compiled, json!({"plugin_call_timeout_seconds": 30}))
            .await
    });

    wait_hang_entered(&invoker).await;
    tokio::time::advance(Duration::from_secs(31)).await;
    let state = tokio::time::timeout(Duration::from_secs(5), handle)
        .await
        .expect("过点后 run 仍未完成")
        .expect("run 任务不得 panic")
        .expect("run_compiled 应 Ok 返回");
    assert_step_call_timeout_recorded(&state);

    // 观察面：plugin_step_failed 域事件到达订阅者，明细齐备。总线上还有
    // OnPipelineStart 等先行事件——循环过滤到目标事件（有界防挂死）。
    let mut event = None;
    for _ in 0..16 {
        let ev = tokio::time::timeout(Duration::from_millis(200), rx.recv())
            .await
            .expect("200ms 内未收到总线事件")
            .expect("总线不应关闭");
        let is_target = ev.hook == agentos_core::traits::LifecycleHook::DomainEvent
            && ev.ctx.get("event").and_then(|v| v.as_str()) == Some("plugin_step_failed");
        if is_target {
            event = Some(ev);
            break;
        }
    }
    let event = event.expect("未收到 plugin_step_failed 域事件");
    assert_eq!(event.hook, agentos_core::traits::LifecycleHook::DomainEvent);
    assert_eq!(
        event.ctx.get("event").and_then(|v| v.as_str()),
        Some("plugin_step_failed")
    );
    assert_eq!(
        event.ctx.get("plugin_id").and_then(|v| v.as_str()),
        Some("p_hang")
    );
    assert_eq!(
        event.ctx.get("code").and_then(|v| v.as_str()),
        Some(agentos_core::traits::STEP_CALL_TIMEOUT_CODE)
    );
    assert!(event.ctx.get("message").and_then(|v| v.as_str()).is_some());
}
