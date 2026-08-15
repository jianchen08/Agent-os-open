// @feature: FP-0.2.〇 管道引擎（tool_core 热路径基准） | @ci: rust-test
//! 任务 E（剩余项清仓批次计划）：tool_core 热路径基准——sidecar(JSON-RPC 往返) vs native(进程内直调)。
//!
//! 问题：tool_core 该不该留在 cdylib(InProcess) 轨？
//! 方法：同一份大 state（messages 数组 JSON，3 档 ~10KB/100KB/1MB）测三种场景的单次 invoke 开销：
//!
//! - **sidecar 通道**：`invoke_pipeline_plugin("pipeline_level_guard")`——真 spawn Python sidecar
//!   （level_guard 与 tool_core 同为 pipeline 插件、同 `execute(state, config)` 契约、
//!   core_type=llm_call 时读小字段即返），大 state 全量经 JSON-RPC/stdio 往返。
//!   测得的是**通道价格**（序列化 + IPC + Python 解析），不要求业务等价。
//!   tool_core 本身无 Python sidecar 形态（plugins/shared/pipeline/core/tool_core/ 只有 cdylib），
//!   这正是"若回 sidecar 轨"的等价通道成本。
//! - **native 直调**：`invoke_pipeline_plugin("pipeline_tool_core")`（真实生产 cdylib，
//!   raw_tool_calls=[] 空转快路径）——state to_string → spawn_blocking → C-ABI →
//!   插件内 from_str → updates to_string → 内核 from_str。
//! - **纯序列化基线**：裸 `serde_json::to_string` + `from_str` 同一 state——把序列化
//!   价格从总开销里分离出来。
//!
//! 全部 `#[ignore]`：普通 `cargo test` 轮不跑（不拖慢 CI），手动执行：
//! `cargo test -p agentos-invoker --test tool_core_bench -- --ignored --nocapture`
//!
//! 零新依赖：手写 std::time::Instant 计时，不引 criterion。

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use agentos_core::traits::{PluginInvoker, PluginLoader};
use agentos_core::types::{ContentLoader, PluginContext, TenantContext};
use agentos_mcp::{CapabilityRouter, McpError};
use agentos_plugin_loader::PluginLoaderImpl;

// ── 仓库根定位（tests/ 在 kernel/crates/invoker 下，上三级为项目根）──────────

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_path_buf()
}

// ── 空存储 mock（构造 PluginContext::new 必需的 ContentLoader）────────────────
// 与 invoker.rs 单测里的 MockStorage 同构：全方法空实现，本基准不触任何持久化。

struct NullStorage;

#[async_trait::async_trait]
impl agentos_core::traits::StorageBackend for NullStorage {
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
    async fn create_branch(
        &self,
        _branch: agentos_core::types::Branch,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn update_run_status(
        &self,
        _run_id: &str,
        _status: agentos_core::types::RunStatus,
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
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
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
    async fn append_execution_record(
        &self,
        _record: &agentos_core::types::ExecutionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn list_execution_records(
        &self,
        _pipeline_run_id: &str,
        _opts: agentos_core::traits::MessageQueryOpts,
    ) -> Result<Vec<agentos_core::types::ExecutionRecord>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn count_execution_records(
        &self,
        _pipeline_run_id: &str,
    ) -> Result<u64, agentos_core::types::StorageError> {
        Ok(0)
    }
    async fn delete_execution_records_by_session(
        &self,
        _pipeline_run_id: &str,
    ) -> Result<u64, agentos_core::types::StorageError> {
        Ok(0)
    }
    async fn save_run_summary(
        &self,
        _summary: &agentos_core::types::PipelineRunSummary,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn get_run_summary(
        &self,
        _run_id: &str,
    ) -> Result<Option<agentos_core::types::PipelineRunSummary>, agentos_core::types::StorageError>
    {
        Ok(None)
    }
    async fn update_run_summary(
        &self,
        _run_id: &str,
        _updates: &Value,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn list_run_summaries(
        &self,
        _limit: Option<usize>,
    ) -> Result<Vec<agentos_core::types::PipelineRunSummary>, agentos_core::types::StorageError>
    {
        Ok(vec![])
    }
    async fn create_memory(
        &self,
        _memory: &agentos_core::types::MemoryRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn get_memory(
        &self,
        _id: &str,
    ) -> Result<Option<agentos_core::types::MemoryRecord>, agentos_core::types::StorageError> {
        Ok(None)
    }
    async fn list_memory(
        &self,
        _memory_type: Option<&str>,
        _limit: usize,
        _offset: usize,
    ) -> Result<Vec<agentos_core::types::MemoryRecord>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn search_memory(
        &self,
        _query: &str,
        _top_k: usize,
    ) -> Result<Vec<agentos_core::types::MemoryRecord>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn delete_memory(
        &self,
        _memory_id: &str,
    ) -> Result<bool, agentos_core::types::StorageError> {
        Ok(false)
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
    async fn delete_user(&self, _user_id: &str) -> Result<bool, agentos_core::types::StorageError> {
        Ok(false)
    }
}

// ── Noop 路由器（对齐 invoker e2e 测试：tool-executor.invoke 即返成功）────────

struct BenchRouter;

#[async_trait::async_trait]
impl CapabilityRouter for BenchRouter {
    async fn handle(
        &self,
        capability: &str,
        method: &str,
        _params: Value,
    ) -> Result<Value, McpError> {
        match (capability, method) {
            // 模拟 bash echo 成功（native 工具执行场景用）。
            ("tool-executor", "invoke") => Ok(json!({
                "success": true,
                "data": {"output": "bench-ok\n", "exit_code": 0},
                "duration_ms": 0.1,
            })),
            ("event-bus", "emit") => Ok(json!({"status": "emitted"})),
            _ => Ok(json!({})),
        }
    }
}

// ── 大 state 构造：贴近真实管道形态的 messages 数组 ──────────────────────────
//
// 每条消息 ~500B（role/content/timestamp/metadata 四键，节点密度接近生产），
// 追加直到整体 JSON 序列化尺寸达到目标档位。state 顶层带管道常用标量键，
// 且 core_type=llm_call + raw_tool_calls=[]——level_guard 与 tool_core 都走各自的
// 空转快路径，测得的就是纯通道/调用开销，不含业务计算。

fn bench_message(i: usize) -> Value {
    let role = if i % 2 == 0 { "user" } else { "assistant" };
    let content = format!(
        "bench message {i}: the agent analyzed the workspace state, consulted prior context, \
         and produced a reasoned step covering module boundaries, dependency ordering, \
         and rollback safety. deterministic filler {i:04} to keep payload size stable."
    );
    json!({
        "role": role,
        "content": content,
        "timestamp": format!("2026-08-15T12:00:{:02}.000Z", i % 60),
        "metadata": {"seq": i, "source": "bench", "tokens_estimate": content.len() / 4},
    })
}

fn build_state(target_bytes: usize) -> (Value, usize) {
    // 按单条消息的序列化长度推算条数（消息近似等长），避免逐条重序列化整个数组。
    let sample = bench_message(0);
    let per_msg = serde_json::to_string(&sample).unwrap().len() + 1; // +1 逗号
    let fixed = 160; // 顶层标量键的序列化开销（实测约 150B，留余量）
    let count = (target_bytes.saturating_sub(fixed) / per_msg).max(1);
    let messages: Vec<Value> = (0..count).map(bench_message).collect();
    let s = json!({
        "messages": messages,
        "session_id": "bench-session",
        "pipeline_id": "bench-pipeline",
        "iteration": 3,
        "core_type": "llm_call",
        "raw_tool_calls": [],
        "ended": false,
    });
    let size = serde_json::to_string(&s).unwrap().len();
    (s, size)
}

fn make_ctx(state: Value) -> PluginContext {
    PluginContext::new(
        state,
        json!({}),
        TenantContext::new("bench-tenant", "bench-session"),
        uuid::Uuid::new_v4(),
        ContentLoader::new(Arc::new(NullStorage), "bench-run".into(), "main".into(), 0),
    )
}

/// 三档 state 尺寸（字节）与每档迭代次数。
const TIERS: [(usize, usize); 3] = [(10 * 1024, 200), (100 * 1024, 100), (1024 * 1024, 50)];

// ── 计时统计 ────────────────────────────────────────────────────────────────

struct Stats {
    n: usize,
    p50_ms: f64,
    mean_ms: f64,
    min_ms: f64,
    max_ms: f64,
    total_ms: f64,
}

fn stats(durs: &[Duration]) -> Stats {
    let mut us: Vec<f64> = durs.iter().map(|d| d.as_secs_f64() * 1000.0).collect();
    us.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = us.len();
    let total: f64 = us.iter().sum();
    Stats {
        n,
        p50_ms: us[n / 2],
        mean_ms: total / n as f64,
        min_ms: us[0],
        max_ms: us[n - 1],
        total_ms: total,
    }
}

fn report_row(scenario: &str, size: usize, s: &Stats) {
    eprintln!(
        "| {scenario:<34} | {:>9} B | {:>4} | {:>9.3} | {:>9.3} | {:>9.3} | {:>9.3} | {:>10.3} | {:>8.1} |",
        size, s.n, s.p50_ms, s.mean_ms, s.min_ms, s.max_ms, s.total_ms,
        s.n as f64 * size as f64 / 1024.0 / 1024.0 / (s.total_ms / 1000.0),
    );
}

fn header() {
    eprintln!();
    eprintln!("| scenario                            | state bytes |   n |  p50 (ms) | mean (ms) |  min (ms) |  max (ms) | total (ms) | MB/s |");
    eprintln!("|-------------------------------------|-------------|-----|-----------|-----------|-----------|-----------|------------|------|");
}

// ═══ 场景 b：native 直调（tool_core cdylib，生产产物）═══════════════════════

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "基准测试：cargo test -p agentos-invoker --test tool_core_bench bench_native -- --ignored --nocapture"]
async fn bench_native_tool_core() {
    let plugins_dir = repo_root().join("plugins/shared");
    let dll = plugins_dir.join("pipeline/core/tool_core/pipeline_tool_core_native.dll");
    if !dll.exists() {
        eprintln!(
            "SKIP bench_native_tool_core: cdylib 不存在（{}），先构建 pipeline-tool-core-native",
            dll.display()
        );
        return;
    }
    let core_root = plugins_dir
        .join("pipeline/core")
        .to_string_lossy()
        .to_string();
    let loader = Arc::new(PluginLoaderImpl::new(plugins_dir, None));
    loader.discover(&[&core_root]).await.unwrap();
    let native_loader = Arc::new(agentos_plugin_loader::NativePluginLoader::new());
    let invoker = agentos_invoker::PluginInvokerImpl::new(loader).set_native_loader(native_loader);
    invoker.set_router(Arc::new(BenchRouter));

    header();
    for &(target, n_iters) in TIERS.iter() {
        let (state, size) = build_state(target);
        let ctx = make_ctx(state);

        // 首调含 cdylib 加载——单独计为冷启动。
        let cold_start = Instant::now();
        let first = invoker
            .invoke_pipeline_plugin("pipeline_tool_core", &ctx)
            .await;
        let cold_ms = cold_start.elapsed().as_secs_f64() * 1000.0;
        assert!(first.is_ok(), "tool_core invoke failed: {:?}", first.err());
        if target == TIERS[0].0 {
            eprintln!("native 冷启动（含 cdylib LoadLibrary）: {cold_ms:.1} ms");
        }

        // warmup 3 次后正式计时。
        for _ in 0..3 {
            invoker
                .invoke_pipeline_plugin("pipeline_tool_core", &ctx)
                .await
                .unwrap();
        }
        let mut durs = Vec::with_capacity(n_iters);
        for _ in 0..n_iters {
            let t = Instant::now();
            let r = invoker
                .invoke_pipeline_plugin("pipeline_tool_core", &ctx)
                .await;
            durs.push(t.elapsed());
            assert!(r.is_ok(), "tool_core invoke failed: {:?}", r.err());
        }
        report_row("native: tool_core cdylib 空转", size, &stats(&durs));
    }

    // 附加数据点：100KB state + 1 个工具调用（真实工作形态——开销占比参照）。
    let (mut state, size) = build_state(100 * 1024);
    state["raw_tool_calls"] = json!([{
        "name": "bash_execute",
        "id": "call_bench1",
        "args": {"command": "echo bench-ok"},
    }]);
    let ctx = make_ctx(state);
    invoker
        .invoke_pipeline_plugin("pipeline_tool_core", &ctx)
        .await
        .unwrap();
    let mut durs = Vec::with_capacity(50);
    for _ in 0..50 {
        let t = Instant::now();
        let r = invoker
            .invoke_pipeline_plugin("pipeline_tool_core", &ctx)
            .await;
        durs.push(t.elapsed());
        assert!(r.is_ok(), "tool_core invoke (1 tool) failed: {:?}", r.err());
    }
    report_row("native: tool_core cdylib +1 工具调用", size, &stats(&durs));

    // Windows 下 cdylib 进程退出段析构会 ACCESS_VIOLATION（dlclose 限制，当前树上
    // 仓库自带的 e2e_native_plugins_load_and_execute 同样复现）——基准数字在上方已
    // 全部产出，这里显式跳过析构直接退出，让 bench 退出码干净。
    eprintln!("（注：数字已全部产出；跳过 cdylib 析构退出，规避已知 Windows 退出段 AV）");
    std::process::exit(0);
}

// ═══ 场景 a：sidecar 通道（Python JSON-RPC 往返，level_guard 代理）══════════

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "基准测试：cargo test -p agentos-invoker --test tool_core_bench bench_sidecar -- --ignored --nocapture（需 python + mcp 依赖）"]
async fn bench_sidecar_channel() {
    let plugins_dir = repo_root().join("plugins/shared");
    let input_root = plugins_dir
        .join("pipeline/input")
        .to_string_lossy()
        .to_string();
    let loader = Arc::new(PluginLoaderImpl::new(plugins_dir, None));
    loader.discover(&[&input_root]).await.unwrap();
    let invoker = agentos_invoker::PluginInvokerImpl::new(loader);
    // PYTHONPATH 注入（对齐内核 main 装配：sidecar 需解析 agentos_plugin_sdk）。
    invoker.set_pythonpath_src(repo_root());
    invoker.set_router(Arc::new(BenchRouter));

    let (state, size0) = build_state(TIERS[0].0);
    let ctx = make_ctx(state);

    // 冷启动：首次 invoke 含 spawn python + initialize 握手（秒级）——单独计时。
    let cold_start = Instant::now();
    let first = invoker
        .invoke_pipeline_plugin("pipeline_level_guard", &ctx)
        .await;
    let cold_ms = cold_start.elapsed().as_secs_f64() * 1000.0;
    if let Err(e) = first {
        eprintln!(
            "SKIP bench_sidecar_channel: sidecar spawn/invoke 失败（python 或依赖缺失?）: {:?}",
            e
        );
        return;
    }
    eprintln!("sidecar 冷启动（spawn python + initialize 握手）: {cold_ms:.0} ms");

    header();
    // 10KB 档（sidecar 已暖）。
    {
        let n_iters = TIERS[0].1;
        for _ in 0..3 {
            invoker
                .invoke_pipeline_plugin("pipeline_level_guard", &ctx)
                .await
                .unwrap();
        }
        let mut durs = Vec::with_capacity(n_iters);
        for _ in 0..n_iters {
            let t = Instant::now();
            let r = invoker
                .invoke_pipeline_plugin("pipeline_level_guard", &ctx)
                .await;
            durs.push(t.elapsed());
            assert!(r.is_ok(), "level_guard invoke failed: {:?}", r.err());
        }
        report_row(
            "sidecar: JSON-RPC 往返（level_guard）",
            size0,
            &stats(&durs),
        );
    }
    // 100KB / 1MB 档。
    for &(target, n_iters) in TIERS.iter().skip(1) {
        let (state, size) = build_state(target);
        let ctx = make_ctx(state);
        for _ in 0..3 {
            invoker
                .invoke_pipeline_plugin("pipeline_level_guard", &ctx)
                .await
                .unwrap();
        }
        let mut durs = Vec::with_capacity(n_iters);
        for _ in 0..n_iters {
            let t = Instant::now();
            let r = invoker
                .invoke_pipeline_plugin("pipeline_level_guard", &ctx)
                .await;
            durs.push(t.elapsed());
            assert!(r.is_ok(), "level_guard invoke failed: {:?}", r.err());
        }
        report_row("sidecar: JSON-RPC 往返（level_guard）", size, &stats(&durs));
    }

    // 清理：杀掉 sidecar 子进程，避免孤儿 python 残留。
    let _ = invoker.force_unload("pipeline_level_guard").await;
}

// ═══ 场景 c：纯序列化基线（serde_json to_string + from_str）════════════════

#[test]
#[ignore = "基准测试：cargo test -p agentos-invoker --test tool_core_bench bench_serde -- --ignored --nocapture"]
fn bench_serde_baseline() {
    header();
    for &(target, n_iters) in TIERS.iter() {
        let (state, size) = build_state(target);

        // 序列化 only（native 路径内核侧固定支付一次）。
        let mut durs = Vec::with_capacity(n_iters);
        for _ in 0..n_iters {
            let t = Instant::now();
            let s = serde_json::to_string(&state).unwrap();
            durs.push(t.elapsed());
            assert!(!s.is_empty());
        }
        report_row("serde: to_string only", size, &stats(&durs));

        // 完整往返（每迭代 to_string + from_str——sidecar/native 两轨共同的 JSON 价格下限）。
        let mut durs = Vec::with_capacity(n_iters);
        for _ in 0..n_iters {
            let t = Instant::now();
            let wire = serde_json::to_string(&state).unwrap();
            let v: Value = serde_json::from_str(&wire).unwrap();
            durs.push(t.elapsed());
            assert!(v.is_object());
        }
        report_row("serde: to_string + from_str", size, &stats(&durs));
    }
}
