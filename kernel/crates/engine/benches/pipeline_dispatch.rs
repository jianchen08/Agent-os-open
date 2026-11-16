// @feature: FP-0.2.〇 管道引擎（三热点性能基准之一） | @ci: rust-bench
//! 管道事件分发基准（整改方案 §6 拍板⑨）。
//!
//! 测什么：`PipelineExecutor::run_compiled` 的纯引擎事件路径——轮次事件分发
//! （RoundEvents 回调）、step 边界窗口轨迹（journal diff → TraceEntry 构造 →
//! append_trace 投递）、while 条件 AST 求值、循环体转移决策。插件调用与持久化
//! 全走零成本 mock（进程内 invoker + NullStorage），把 LLM/IPC/SQLite 从测量中
//! 剥离——SQLite 写是热点二（sqlite_write bench）的领地，两热点测量面不重叠。
//!
//! 确定性可重复：固定轮数（while: "iteration < N"）+ 固定 step 数 + 插件返回值
//! 仅由 state 驱动（iteration+1 / 常量键值）——每次 run_compiled 工作量同构，
//! 无真实时钟/网络/IO 依赖（trace_id/message_id 的 UUID 内容差异不影响控制流）。
//!
//! 运行：cd kernel && cargo bench -p agentos-engine --bench pipeline_dispatch

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;

use criterion::{criterion_group, criterion_main, BenchmarkId, Criterion};
use serde_json::{json, Value};

use agentos_core::traits::{PluginInvoker, StorageBackend};
use agentos_core::types::{
    CheckpointConfig, LoopBody, PipelineConfig, PipelineStep, PluginContext, PluginResult,
    StepLibrary, TenantContext, ToolExecutionResult,
};
use agentos_engine::compiler::compile_pipeline;
use agentos_engine::{PipelineExecutor, RoundEnd, RoundEvents, RoundStart};

// ── mock 面──────────────────────────────────────────────────────────────────

/// 确定性进程内 invoker：返回值只由 ctx.state 驱动，零 IO。
///
/// - `bench_inc`：`iteration ← state.iteration + 1`（while 条件的推进器）；
/// - `bench_touch_a/b`：常量键值覆盖写（模拟插件小产出，state 尺寸恒定）。
struct BenchInvoker;

#[async_trait::async_trait]
impl PluginInvoker for BenchInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, agentos_core::types::PluginError> {
        let updates = match plugin_id {
            "bench_inc" => {
                let n = ctx
                    .state
                    .get("iteration")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0);
                [("iteration".to_string(), json!(n + 1))]
                    .into_iter()
                    .collect()
            }
            "bench_touch_a" => [("bench_a".to_string(), json!(touch_payload_a()))]
                .into_iter()
                .collect(),
            _ => [("bench_b".to_string(), touch_payload_b())]
                .into_iter()
                .collect(),
        };
        Ok(PluginResult {
            state_updates: updates,
            ..Default::default()
        })
    }

    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &Value,
    ) -> Result<ToolExecutionResult, agentos_core::types::PluginError> {
        Ok(ToolExecutionResult::success(Value::Null))
    }

    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: agentos_core::traits::LifecycleHook,
        _context: &agentos_core::traits::HookContext,
    ) -> Result<(), agentos_core::types::PluginError> {
        Ok(())
    }
}

/// 常量产出 A：~64B 标量（典型控制键/短状态写）。
fn touch_payload_a() -> String {
    "bench-a-payload-0123456789-0123456789-0123456789-0123456789-0123456789".to_string()
}

/// 常量产出 B：~256B 对象（典型轻量插件结构化产出），内容逐字节固定。
fn touch_payload_b() -> Value {
    json!({
        "kind": "bench_touch_b",
        "fields": {
            "seq_marker": "0123456789abcdef",
            "body": "fixed deterministic payload for step produce; overwrites same key",
            "nested": {"depth": 1, "leaf": [1, 2, 3, 4]}
        }
    })
}

/// NullStorage：热路径（append_trace/checkpoint/投影）全 no-op——事件路径的
/// 持久化面计零成本，测量只含引擎侧构造与分发（与 pipeline_loop/tests.rs 的
/// NullStorage 同构；benches 在 crate 外无私有访问权，独立实现同一形状）。
struct NullStorage;

#[async_trait::async_trait]
impl StorageBackend for NullStorage {
    async fn get_run(
        &self,
        _run_id: &str,
    ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
        Err(agentos_core::types::StorageError::NotFound("bench".into()))
    }
    async fn get_messages_by_pipeline(
        &self,
        _pipeline_id: &str,
        _opts: agentos_core::traits::MessageQueryOpts,
    ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn append_trace(
        &self,
        _entry: agentos_core::types::TraceEntry,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn store_blob(
        &self,
        _data: &[u8],
        _mime_type: &str,
    ) -> Result<String, agentos_core::types::StorageError> {
        Ok("bench_blob".into())
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
        Err(agentos_core::types::StorageError::NotFound("null".into()))
    }
    async fn delete_user(&self, _user_id: &str) -> Result<bool, agentos_core::types::StorageError> {
        Ok(false)
    }
}

/// 轮次事件计数消费者：模拟 api 层桥接的回调负载（原子计数，零 IO）。
#[derive(Default)]
struct CountingRoundEvents {
    starts: AtomicI64,
    ends: AtomicI64,
}

impl RoundEvents for CountingRoundEvents {
    fn on_round_start(
        &self,
        _ev: RoundStart,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + '_>> {
        Box::pin(async {
            self.starts.fetch_add(1, Ordering::Relaxed);
        })
    }

    fn on_round_end(
        &self,
        _ev: RoundEnd,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + '_>> {
        Box::pin(async {
            self.ends.fetch_add(1, Ordering::Relaxed);
        })
    }
}

// ── 工作量构造───────────────────────────────────────────────────────────────

/// 构造基准管道：单循环体 main（while: "iteration < N"）× 3 step
/// （bench_inc / bench_touch_a / bench_touch_b）——每轮 3 次 step 事件分发。
/// checkpoint 关闭：周期全量快照是存储热点（热点二）的干扰项，此处不测。
fn bench_config(rounds: usize) -> PipelineConfig {
    let step = |id: &str, plugin: &str| PipelineStep {
        id: id.to_string(),
        steps: vec![plugin.into()],
        when: None,
        context: HashMap::new(),
        routes: vec![],
        loop_config: None,
    };
    PipelineConfig {
        name: "bench_dispatch".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![
                step("tick", "bench_inc"),
                step("touch_a", "bench_touch_a"),
                step("touch_b", "bench_touch_b"),
            ],
            while_cond: Some(format!("iteration < {rounds}")),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: CheckpointConfig {
            enabled: false,
            interval_steps: 0,
        },
        initial_state: HashMap::new(),
        max_rounds: None,
    }
}

/// 基准主体：N 轮 × 3 step 的 run_compiled 全事件路径（两档规模验线性）。
fn bench_pipeline_dispatch(c: &mut Criterion) {
    let rt = tokio::runtime::Runtime::new().expect("tokio runtime");
    let plugin_ids: HashSet<String> = ["bench_inc", "bench_touch_a", "bench_touch_b"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage);
    let round_events = Arc::new(CountingRoundEvents::default());
    let executor = PipelineExecutor::new(
        Arc::new(BenchInvoker),
        PathBuf::from("."),
        TenantContext::new("bench-tenant", "bench-session"),
        plugin_ids.clone(),
        store,
        "bench-run",
    )
    .with_round_events(round_events.clone());

    let mut group = c.benchmark_group("pipeline_dispatch");
    for rounds in [10usize, 25] {
        let config = bench_config(rounds);
        let library = StepLibrary {
            steps: HashMap::new(),
        };
        let compiled =
            compile_pipeline(&config, &library, &plugin_ids).expect("bench pipeline compiles");
        let initial = json!({
            "pipeline_id": "bench-pipeline",
            "session_id": "bench-session",
            "iteration": 0,
        });
        group.bench_with_input(
            BenchmarkId::new(
                "run_compiled",
                format!("rounds={rounds}_steps={}", rounds * 3),
            ),
            &rounds,
            |b, _| {
                b.iter(|| {
                    let state = rt.block_on(
                        executor.run_compiled(&compiled, criterion::black_box(initial.clone())),
                    );
                    criterion::black_box(state).expect("run should succeed")
                })
            },
        );
    }
    group.finish();

    // 工作量完整性自检（断可观察行为：分发到消费者的轮次事件数 ≥ 最大档轮数；
    // 不达标说明基准没真正走到轮次分发路径，数字不可信）。
    let starts = round_events.starts.load(Ordering::Relaxed);
    assert!(
        starts >= 25,
        "round_start 回调必须按轮分发（实际 {starts}）"
    );
}

criterion_group!(benches, bench_pipeline_dispatch);
criterion_main!(benches);
