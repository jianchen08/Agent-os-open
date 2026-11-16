// @feature: FP-0.2.〇 运行簿记 | @vision: V4 可观测 | @ci: rust-test
//! run_started_at 单写者回归（装机版缺陷清偿计划 刀5）。
//!
//! 缺陷（已实锤）：run_started_at 双写——引擎 run_init 写 state（T1）与
//! `record_run_start` 落库时自产第二时间戳（T2，值差毫秒级）；读面在内存行
//! 与 DB 兜底间可能取到不同值，插件层字符串相等 renewal 判定每轮误判「新
//! run」重置提醒预算（TaskReminder iter=0 反复，评估耗尽兜底被解除武装）。
//!
//! 单写者契约（本文件锁定）：
//! - 值唯一 origin = 引擎 run_init（run_compiled 起手一次）；
//! - `record_run_start` 只持久化调用方传入的 started_at，不自产时间戳；
//! - 多轮 run 全程（state 与 DB 两读面）run_started_at 恒定。

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use agentos_core::traits::{PluginInvoker, StorageBackend};
use agentos_core::types::{
    LoopBody, PipelineConfig, PipelineStep, PluginContext, PluginError, PluginResult, StepLibrary,
    ToolExecutionResult,
};
use agentos_engine::compiler::compile_pipeline;
use agentos_engine::{PipelineExecutor, SqliteStore};
use async_trait::async_trait;
use serde_json::json;

/// 捕获每次调用所见 `state.run_started_at` 的 MockInvoker；第 N 次调用可编程
/// 返回终止标志（ended=true）以构造多轮 run。
struct StartedAtCaptureInvoker {
    seen_started_at: Mutex<Vec<String>>,
    /// 第 `ended_on_call` 次调用起返回 ended=true（1 起）。
    ended_on_call: usize,
    calls: Mutex<Vec<String>>,
}

impl StartedAtCaptureInvoker {
    fn new(ended_on_call: usize) -> Self {
        Self {
            seen_started_at: Mutex::new(Vec::new()),
            ended_on_call,
            calls: Mutex::new(Vec::new()),
        }
    }

    fn seen_started_at(&self) -> Vec<String> {
        self.seen_started_at.lock().unwrap().clone()
    }

    fn call_count(&self) -> usize {
        self.calls.lock().unwrap().len()
    }
}

#[async_trait]
impl PluginInvoker for StartedAtCaptureInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        let started_at = ctx
            .state
            .get("run_started_at")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let n = {
            let mut calls = self.calls.lock().unwrap();
            calls.push(started_at.clone());
            self.seen_started_at.lock().unwrap().push(started_at);
            calls.len()
        };
        let mut result = PluginResult::default();
        if n >= self.ended_on_call {
            result
                .state_updates
                .insert("ended".to_string(), json!(true));
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

/// 单步循环管道：while True + ended 终止（轮数由 invoker 编程）。
fn looping_config() -> PipelineConfig {
    PipelineConfig {
        name: "started_at_single_writer".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "step".into(),
                when: None,
                steps: vec!["p".into()],
                context: HashMap::new(),
                routes: vec![],
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

fn read_db_started_at(store: &SqliteStore, pipeline_id: &str) -> Option<String> {
    store
        .with_conn(|c| {
            c.query_row(
                "SELECT value FROM pipeline_state \
                 WHERE pipeline_id = ?1 AND tenant_id = 't' AND field_key = 'run_started_at'",
                [pipeline_id],
                |r| r.get::<_, String>(0),
            )
        })
        .ok()
}

async fn run_two_rounds(
    store: Arc<SqliteStore>,
) -> (serde_json::Value, Arc<StartedAtCaptureInvoker>) {
    let invoker = Arc::new(StartedAtCaptureInvoker::new(2));
    let executor = PipelineExecutor::new(
        Arc::clone(&invoker) as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        agentos_core::types::TenantContext::new("t", "s"),
        vec!["p".to_string()],
        store,
        "run-sw-1",
    );
    let config = looping_config();
    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");
    let final_state = executor
        .run_compiled(&compiled, json!({"pipeline_id": "pipe-sw"}))
        .await
        .expect("run ok");
    (final_state, invoker)
}

/// 多轮 run：轮次推进时 run_started_at 恒定（单写者断言），且 DB 落库值与
/// state 值同源同串（双写漂移 = 旧缺陷形态，禁止回归）。
#[tokio::test]
async fn multi_round_run_started_at_constant_and_db_matches_state() {
    let store = Arc::new(SqliteStore::open_memory().expect("open memory db"));
    let (final_state, invoker) = run_two_rounds(Arc::clone(&store)).await;

    assert!(
        invoker.call_count() >= 2,
        "前置：本测试必须覆盖多轮 run（实际 {} 轮）",
        invoker.call_count()
    );
    let seen = invoker.seen_started_at();
    let first = &seen[0];
    for (round, value) in seen.iter().enumerate() {
        assert_eq!(
            value,
            first,
            "第 {} 轮所见 run_started_at 必须与首轮恒定，实际 {} vs {}",
            round + 1,
            value,
            first
        );
    }

    let state_started_at = final_state
        .get("run_started_at")
        .and_then(|v| v.as_str())
        .expect("final_state 必须携带 run_started_at");
    assert_eq!(state_started_at, first, "终态与逐轮所见同串");

    let db_started_at =
        read_db_started_at(&store, "pipe-sw").expect("运行簿记必须落库 run_started_at");
    assert_eq!(
        db_started_at, state_started_at,
        "DB 簿记值必须与 state 同源（单写者）：双写自产第二时间戳即本测试红"
    );
}

/// 单轮 run 同契约（≥2 组有区分度输入）：簿记落库值 = 调用方传入值，
/// `record_run_start` 不自产时间戳。
#[tokio::test]
async fn single_round_run_started_at_db_matches_state() {
    let store = Arc::new(SqliteStore::open_memory().expect("open memory db"));
    let invoker = Arc::new(StartedAtCaptureInvoker::new(1));
    let executor = PipelineExecutor::new(
        Arc::clone(&invoker) as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        agentos_core::types::TenantContext::new("t", "s"),
        vec!["p".to_string()],
        Arc::clone(&store) as Arc<dyn StorageBackend>,
        "run-sw-2",
    );
    let config = looping_config();
    let compiled = compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
        .expect("compile ok");
    let final_state = executor
        .run_compiled(&compiled, json!({"pipeline_id": "pipe-sw-1"}))
        .await
        .expect("run ok");

    assert_eq!(invoker.call_count(), 1);
    let state_started_at = final_state
        .get("run_started_at")
        .and_then(|v| v.as_str())
        .expect("final_state 必须携带 run_started_at");
    let db_started_at =
        read_db_started_at(&store, "pipe-sw-1").expect("运行簿记必须落库 run_started_at");
    assert_eq!(db_started_at, state_started_at, "单轮 run 同样单写者同源");
}

/// 值 origin 锁定（store 层）：`record_run_start` 落库的 run_started_at
/// 必须等于调用方显式传入的 started_at，不得以自身时钟覆盖。
#[test]
fn record_run_start_persists_caller_supplied_started_at() {
    let store = SqliteStore::open_memory().expect("open memory db");
    let caller_value = "2026-10-15T01:02:03.456789+00:00";
    store
        .record_run_start(
            "pipe-caller",
            "tenant-c",
            "run-caller",
            "hash-c",
            caller_value,
        )
        .expect("record_run_start ok");
    let stored: String = store
        .with_conn(|c| {
            c.query_row(
                "SELECT value FROM pipeline_state \
                 WHERE pipeline_id = 'pipe-caller' AND tenant_id = 'tenant-c' \
                 AND field_key = 'run_started_at'",
                [],
                |r| r.get(0),
            )
        })
        .expect("run_started_at 已落库");
    assert_eq!(
        stored, caller_value,
        "run_started_at 值 origin = 引擎 run_init；store 只持久化不自产"
    );
}
