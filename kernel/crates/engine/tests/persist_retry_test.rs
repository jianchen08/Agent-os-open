// @feature: FP-0.2.〇 管道引擎持久化重试 | @vision: V1 可观测可干预 | @ci: rust-test
//! 持久化失败重试（2 次退避后上抛）：
//!
//! 1. append_trace 失败 1 次 → 统一落库核内重试成功，run 正常（性质断言：
//!    失败确实发生 + 失败后发生重试，不锚定窗口数）
//! 2. append_trace 连续 3 次失败 → run 失败且错误可见（含真实退避耗时下界）
//! 3. upsert_state_field 失败 1 次 → 重试成功
//! 4. upsert_state_field 连续 3 次失败 → run 失败且错误可见

use std::collections::HashMap;
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use agentos_core::traits::{PluginInvoker, StorageBackend};
use agentos_core::types::{
    MessageRecord, PipelineConfig, PipelineStep, PluginContext, PluginError, PluginResult,
    RunRecord, StepLibrary, TenantContext, ToolExecutionResult, TraceEntry,
};
use agentos_engine::compiler::compile_pipeline;
use agentos_engine::PipelineExecutor;
use async_trait::async_trait;
use serde_json::json;

/// 空结果 MockInvoker。
struct EmptyInvoker;

#[async_trait]
impl PluginInvoker for EmptyInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        _ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        let mut result = PluginResult::default();
        // 写 state 键：一个普通键（step diff 非空 → persist_step_trace 萰
        // append_trace）+ 一个 persistent_fields 声明键（投影 upsert 用）。
        result
            .state_updates
            .insert(format!("written_by_{plugin_id}"), json!(1));
        result
            .state_updates
            .insert("track.total".to_string(), json!(5));
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

/// 可注入失败的计数桩：append_trace / upsert_state_field 按剩余失败配额失败，
/// 其余方法空操作成功。
struct FlakyStorage {
    append_attempts: AtomicUsize,
    append_failed: AtomicUsize,
    append_failures_remaining: AtomicUsize,
    upsert_attempts: AtomicUsize,
    upsert_failures_remaining: AtomicUsize,
    // ── B5 收尾面：checkpoint / 终态写面（run_status 键）失败配额与计数 ──
    checkpoint_attempts: AtomicUsize,
    checkpoint_failures_remaining: AtomicUsize,
    status_attempts: AtomicUsize,
    status_failures_remaining: AtomicUsize,
    /// 全部 upsert_state_field 调用 (key, value)（补偿标记断言用）。
    upsert_calls: Mutex<Vec<(String, serde_json::Value)>>,
}

impl FlakyStorage {
    fn new(append_failures: usize, upsert_failures: usize) -> Self {
        Self {
            append_attempts: AtomicUsize::new(0),
            append_failed: AtomicUsize::new(0),
            append_failures_remaining: AtomicUsize::new(append_failures),
            upsert_attempts: AtomicUsize::new(0),
            upsert_failures_remaining: AtomicUsize::new(upsert_failures),
            checkpoint_attempts: AtomicUsize::new(0),
            checkpoint_failures_remaining: AtomicUsize::new(0),
            status_attempts: AtomicUsize::new(0),
            status_failures_remaining: AtomicUsize::new(0),
            upsert_calls: Mutex::new(Vec::new()),
        }
    }

    fn append_attempt_count(&self) -> usize {
        self.append_attempts.load(Ordering::SeqCst)
    }

    fn append_failed_count(&self) -> usize {
        self.append_failed.load(Ordering::SeqCst)
    }

    /// 指定键的 upsert_state_field 调用次数（含失败调用——入账在配额判定前）。
    fn upsert_attempts_for(&self, key: &str) -> usize {
        self.upsert_calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(k, _)| k == key)
            .count()
    }
}

fn take_one_fail(counter: &AtomicUsize) -> Result<(), agentos_core::types::StorageError> {
    let remaining = counter.fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| {
        if n > 0 {
            Some(n - 1)
        } else {
            None
        }
    });
    match remaining {
        Ok(_prev) => Err(agentos_core::types::StorageError::Database(
            "injected persistence failure".into(),
        )),
        Err(_) => Ok(()),
    }
}

#[async_trait]
impl StorageBackend for FlakyStorage {
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
        self.append_attempts.fetch_add(1, Ordering::SeqCst);
        if take_one_fail(&self.append_failures_remaining).is_err() {
            self.append_failed.fetch_add(1, Ordering::SeqCst);
            return Err(agentos_core::types::StorageError::Database(
                "injected persistence failure".into(),
            ));
        }
        let _ = entry;
        Ok(())
    }
    async fn upsert_state_field(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        key: &str,
        value: &serde_json::Value,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.upsert_calls
            .lock()
            .unwrap()
            .push((key.to_string(), value.clone()));
        // 终态写面（run_status 键，runs 表退役后引擎经 upsert_state_fields 批量写
        // → 默认 trait 实现逐键落到这里）走独立失败配额与计数。
        if key == "run_status" {
            self.status_attempts.fetch_add(1, Ordering::SeqCst);
            return take_one_fail(&self.status_failures_remaining);
        }
        self.upsert_attempts.fetch_add(1, Ordering::SeqCst);
        take_one_fail(&self.upsert_failures_remaining)
    }
    async fn save_checkpoint(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _step_no: i64,
        _state: &serde_json::Value,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.checkpoint_attempts.fetch_add(1, Ordering::SeqCst);
        take_one_fail(&self.checkpoint_failures_remaining)
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
        unreachable!("FlakyStorage 不提供口令更新")
    }

    async fn delete_user(&self, _user_id: &str) -> Result<bool, agentos_core::types::StorageError> {
        Ok(false)
    }
}

/// 单体单步管道：step 引用 `writer` 插件（写 state 键 → step diff 非空 → 落 trace）。
fn make_config() -> PipelineConfig {
    PipelineConfig {
        name: "persist_retry".into(),
        loop_bodies: vec![agentos_core::types::LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "s1".into(),
                steps: vec!["writer".into()],
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
        initial_state: HashMap::new(),
        max_rounds: None,
    }
}

/// append_trace 失败 1 次 → 统一落库核内重试成功，run 正常完成。
///
/// 性质断言（不锚定窗口数——窗口族轨迹重构后单次 run 含多个窗口落库点）：
/// 注入的 1 次失败确实发生 + 失败后发生重试（总尝试 ≥ 2），run 完成即证明
/// 全部窗口（step/引擎边界）的轨迹最终都落库成功。
#[tokio::test]
async fn append_trace_fails_once_retries_and_succeeds() {
    let store = Arc::new(FlakyStorage::new(1, 0));
    let backend: Arc<dyn StorageBackend> = Arc::clone(&store) as Arc<dyn StorageBackend>;
    let executor = PipelineExecutor::new(
        Arc::new(EmptyInvoker) as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        TenantContext::new("t", "s"),
        vec!["writer".to_string()],
        backend,
        "r",
    );
    let config = make_config();
    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");

    let final_state = executor
        .run_compiled(&compiled, json!({"pipeline_id": "pipe_retry"}))
        .await
        .expect("失败 1 次后重试成功，run 应正常完成");

    assert_eq!(
        store.append_failed_count(),
        1,
        "注入的 1 次失败必须恰好发生（失败未被软吞）"
    );
    assert!(
        store.append_attempt_count() >= 2,
        "失败后必须发生重试（总尝试 ≥ 失败 1 次 + 重试 ≥ 1 次），实际 {} 次",
        store.append_attempt_count()
    );
    assert_eq!(
        final_state
            .get("written_by_writer")
            .and_then(|v| v.as_i64()),
        Some(1),
        "插件产出已 merge 进 state"
    );
}

/// append_trace 连续 3 次失败 → run 失败，错误可见，恰好 3 次尝试（2 次重试耗尽）。
#[tokio::test]
async fn append_trace_fails_three_times_run_fails() {
    let store = Arc::new(FlakyStorage::new(3, 0));
    let backend: Arc<dyn StorageBackend> = Arc::clone(&store) as Arc<dyn StorageBackend>;
    let executor = PipelineExecutor::new(
        Arc::new(EmptyInvoker) as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        TenantContext::new("t", "s"),
        vec!["writer".to_string()],
        backend,
        "r",
    );
    let config = make_config();
    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");

    let started = Instant::now();
    let result = executor
        .run_compiled(&compiled, json!({"pipeline_id": "pipe_retry"}))
        .await;
    let elapsed = started.elapsed();

    let err = result.expect_err("连续 3 次失败必须让 run 失败");
    let msg = format!("{err}");
    assert!(
        msg.contains("injected persistence failure"),
        "错误应可见（上抛的 StorageError）：{msg}"
    );
    assert_eq!(
        store.append_attempt_count(),
        3,
        "首次 + 2 次重试 = 恰好 3 次尝试"
    );
    // 真实退避：100ms + 300ms 两次 sleep（下界断言，只增不假）
    assert!(
        elapsed.as_millis() >= 400,
        "两次退避（100ms+300ms）应真实发生，elapsed = {elapsed:?}"
    );
}

/// upsert_state_field 失败 1 次 → 重试成功（共 2 次尝试），run 正常完成。
#[tokio::test]
async fn upsert_state_field_fails_once_retries_and_succeeds() {
    let store = Arc::new(FlakyStorage::new(0, 1));
    let backend: Arc<dyn StorageBackend> = Arc::clone(&store) as Arc<dyn StorageBackend>;
    let executor = PipelineExecutor::new(
        Arc::new(EmptyInvoker) as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        TenantContext::new("t", "s"),
        vec!["writer".to_string()],
        backend,
        "r",
    )
    .with_persistent_fields(vec!["track.total".to_string()]);
    let config = make_config();
    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");

    executor
        .run_compiled(&compiled, json!({"pipeline_id": "pipe_retry"}))
        .await
        .expect("失败 1 次后重试成功，run 应正常完成");

    assert_eq!(
        store.upsert_attempts_for("track.total"),
        2,
        "1 次失败 + 1 次重试成功（投影键恰好 2 次调用）"
    );
}

/// upsert_state_field 连续 3 次失败 → run 失败，错误可见。
#[tokio::test]
async fn upsert_state_field_fails_three_times_run_fails() {
    let store = Arc::new(FlakyStorage::new(0, 3));
    let backend: Arc<dyn StorageBackend> = Arc::clone(&store) as Arc<dyn StorageBackend>;
    let executor = PipelineExecutor::new(
        Arc::new(EmptyInvoker) as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        TenantContext::new("t", "s"),
        vec!["writer".to_string()],
        backend,
        "r",
    )
    .with_persistent_fields(vec!["track.total".to_string()]);
    let config = make_config();
    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");

    let result = executor
        .run_compiled(&compiled, json!({"pipeline_id": "pipe_retry"}))
        .await;
    let err = result.expect_err("连续 3 次失败必须让 run 失败");
    let msg = format!("{err}");
    assert!(
        msg.contains("injected persistence failure"),
        "错误应可见（上抛的 StorageError）：{msg}"
    );
    assert_eq!(
        store.upsert_attempts_for("track.total"),
        3,
        "首次 + 2 次重试 = 3 次尝试"
    );
}

// ── B5：run 收尾落库（checkpoint / 终态写面 run_status 键）失败重试 + 终态未落库标记 ──

impl FlakyStorage {
    /// 注入收尾面失败配额：save_checkpoint / 终态写面（run_status 键）各自的连续失败次数。
    fn with_end_failure_quotas(self, checkpoint_failures: usize, status_failures: usize) -> Self {
        self.checkpoint_failures_remaining
            .store(checkpoint_failures, Ordering::SeqCst);
        self.status_failures_remaining
            .store(status_failures, Ordering::SeqCst);
        self
    }

    fn checkpoint_attempt_count(&self) -> usize {
        self.checkpoint_attempts.load(Ordering::SeqCst)
    }

    fn status_attempt_count(&self) -> usize {
        self.status_attempts.load(Ordering::SeqCst)
    }

    /// 取「终态未落库」补偿标记（B5）的写入调用 (key, value) 清单。
    fn terminal_persist_markers(&self) -> Vec<(String, serde_json::Value)> {
        self.upsert_calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(k, _)| k == "terminal_persist_failed")
            .cloned()
            .collect()
    }
}

/// 收尾 checkpoint 失败 1 次 → retry_persist 重试成功（2 次尝试），run 正常完成
/// 且不写终态未落库标记（update_run_status 成功）。
#[tokio::test]
async fn run_end_checkpoint_fails_once_retries_and_succeeds() {
    let store = Arc::new(FlakyStorage::new(0, 0).with_end_failure_quotas(1, 0));
    let backend: Arc<dyn StorageBackend> = Arc::clone(&store) as Arc<dyn StorageBackend>;
    let executor = PipelineExecutor::new(
        Arc::new(EmptyInvoker) as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        TenantContext::new("t", "s"),
        vec!["writer".to_string()],
        backend,
        "r",
    );
    let config = make_config();
    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");

    let final_state = executor
        .run_compiled(&compiled, json!({"pipeline_id": "pipe_retry"}))
        .await
        .expect("收尾 checkpoint 失败 1 次重试成功后 run 应正常完成");

    assert_eq!(
        store.checkpoint_attempt_count(),
        2,
        "1 次失败 + 1 次重试 = 2 次尝试（单步管道只有收尾一个 checkpoint 点）"
    );
    assert_eq!(
        final_state
            .get("written_by_writer")
            .and_then(|v| v.as_i64()),
        Some(1)
    );
    assert!(
        store.terminal_persist_markers().is_empty(),
        "终态落库成功时不得写补偿标记"
    );
}

/// 收尾 checkpoint 连续 3 次失败 → run 仍正常完成（终态点无法终止 run），
/// 恰好 3 次尝试（首次 + 2 次重试），且不误写终态未落库标记（那是
/// 终态写面 run_status 键重试耗尽专属语义）。
#[tokio::test]
async fn run_end_checkpoint_exhausts_retries_run_still_completes() {
    let store = Arc::new(FlakyStorage::new(0, 0).with_end_failure_quotas(usize::MAX, 0));
    let backend: Arc<dyn StorageBackend> = Arc::clone(&store) as Arc<dyn StorageBackend>;
    let executor = PipelineExecutor::new(
        Arc::new(EmptyInvoker) as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        TenantContext::new("t", "s"),
        vec!["writer".to_string()],
        backend,
        "r",
    );
    let config = make_config();
    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");

    let final_state = executor
        .run_compiled(&compiled, json!({"pipeline_id": "pipe_retry"}))
        .await
        .expect("收尾失败不得终止已结束的 run");

    assert_eq!(
        store.checkpoint_attempt_count(),
        3,
        "首次 + 2 次重试 = 恰好 3 次尝试"
    );
    assert!(
        final_state
            .get("written_by_writer")
            .and_then(|v| v.as_i64())
            .is_some(),
        "run 产出不受收尾快照失败影响"
    );
    assert!(
        store.terminal_persist_markers().is_empty(),
        "checkpoint 失败不得写终态写面专属的补偿标记"
    );
}

/// 终态写面（run_status 键）连续 3 次失败 → 重试耗尽后 best-effort 写
/// 「终态未落库」补偿标记（含 run_id 与预期终态、时间戳），run 仍正常完成
/// （终态点无法终止 run）。
#[tokio::test]
async fn run_end_status_exhausts_retries_writes_terminal_persist_marker() {
    let store = Arc::new(FlakyStorage::new(0, 0).with_end_failure_quotas(0, usize::MAX));
    let backend: Arc<dyn StorageBackend> = Arc::clone(&store) as Arc<dyn StorageBackend>;
    let executor = PipelineExecutor::new(
        Arc::new(EmptyInvoker) as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        TenantContext::new("t", "s"),
        vec!["writer".to_string()],
        backend,
        "r_terminal",
    );
    let config = make_config();
    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");

    executor
        .run_compiled(&compiled, json!({"pipeline_id": "pipe_retry"}))
        .await
        .expect("终态落库失败不得终止已结束的 run");

    assert_eq!(
        store.status_attempt_count(),
        3,
        "首次 + 2 次重试 = 恰好 3 次尝试"
    );
    let markers = store.terminal_persist_markers();
    assert_eq!(
        markers.len(),
        1,
        "重试耗尽必须恰好写一次补偿标记，实际 {markers:?}"
    );
    let (_, value) = &markers[0];
    assert_eq!(
        value["run_id"], "r_terminal",
        "标记必须带 run_id（G8 排空兜底按它精准匹配）: {value}"
    );
    let intended = value["intended_status"].as_str().unwrap_or("");
    assert_eq!(
        intended, "completed",
        "标记的预期终态必须与控制态折算一致（无控制键 = completed）: {value}"
    );
    assert!(
        value["failed_at"].as_str().is_some_and(|s| !s.is_empty()),
        "标记必须带时间戳: {value}"
    );
}
