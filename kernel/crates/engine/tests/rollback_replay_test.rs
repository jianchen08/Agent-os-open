// @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: rust-test
//! 任务 6 验收测试：**回退语义**（实录回放重建 + 补偿执行 + Rollback 轨迹）。
//!
//! 目标架构（docs/tasks/task_messages_op_trace_unification.md 任务 6，依赖任务 1/3/5）：
//! - **重建**：读该 pipeline 全部 traces 的 messages ops 实录（op+seq+指纹），
//!   回放上界（≤ 目标时刻）以内的 ops 得到"槽位→指纹"映射，按指纹从 blobs 回查全文，
//!   重建目标时刻的 `state["messages"]`；
//! - **补偿执行**：重建结果与当前 message_slots 表 diff 生成补偿 ops（恢复旧指纹内容 /
//!   清空新增槽），补偿 ops 走同一套三落点 apply；落一条 `PatchType::Rollback`
//!   （"rollback"）trace entry——append-only，旧轨迹不抹；回退后新轮正常追加。
//!
//! ## 本文件对新 API 形状的假设（主线实现时可调整，需同步改调用点）
//!
//! ```text
//! agentos_engine::replay::rebuild_messages_at(
//!     store: &SqliteStore, pipeline_id: &str, tenant_id: &str,
//!     upto_created_at: &str,                    // 回放上界：实录 trace 的 created_at ≤ 此值（含）
//! ) -> Result<Vec<serde_json::Value>, agentos_core::types::StorageError>
//!                                            // 按 seq 升序、元素带 seq + 全文；存储读失败传播
//!
//! agentos_engine::replay::rollback(
//!     store: &SqliteStore, pipeline_id: &str, tenant_id: &str,
//!     upto_created_at: &str,                    // 回退目标时刻（同上，含）
//! ) -> Result<(), agentos_core::types::StorageError>
//! ```
//!
//! 上界用 `created_at`（rfc3339 字符串）而非 step 号：step 级 seq_in_branch 当前恒为 0、
//! 回退边界文档语义是"目标步/时刻"，时间戳是两者最中立的投影；若主线选择
//! `upto_step: u64`，仅需把取上界的辅助换成"最后一条 step 轨迹的 step 号"。
//!
//! 驱动方式：实录落点在 `PipelineExecutor` 的 per-step ops_ledger（任务 3 已落地形态），
//! 因此用两轮真实管道执行（MockInvoker + SqliteStore）产生阶段一/阶段二的 ops 实录，
//! 两轮之间的 traces 最大 created_at 即回退上界。
//!
//! `replay` 模块由主线实现（当前不存在）→ 本文件为
//! **red（编译失败，TDD red 阶段）**。

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use agentos_core::traits::{MessageQueryOpts, PluginInvoker, StorageBackend};
use agentos_core::types::{
    LoopBody, LoopConfig, PipelineConfig, PipelineStep, PluginContext, PluginError, PluginResult,
    Route, RouteAction, RouteNext, StepItem, TenantContext, ToolExecutionResult,
};
use agentos_engine::{replay, PipelineExecutor, SqliteStore};
use async_trait::async_trait;
use serde_json::{json, Value};

fn set(seq: u64, msg: Value) -> Value {
    json!({ "op": "set", "seq": seq, "msg": msg })
}

fn user_msg(content: &str) -> Value {
    json!({ "role": "user", "content": content })
}

fn assistant_msg(content: &str) -> Value {
    json!({ "role": "assistant", "content": content })
}

// ── 驱动：MockInvoker + default.yaml + 真实 SqliteStore（对齐 ops_trace_ledger_test）──

struct MockInvoker {
    results: Mutex<HashMap<String, PluginResult>>,
}

impl MockInvoker {
    fn new() -> Self {
        Self {
            results: Mutex::new(HashMap::new()),
        }
    }

    fn set_result(&self, plugin_id: &str, result: PluginResult) {
        self.results
            .lock()
            .unwrap()
            .insert(plugin_id.to_string(), result);
    }
}

#[async_trait]
impl PluginInvoker for MockInvoker {
    async fn invoke_pipeline_plugin(
        &self,
        plugin_id: &str,
        _ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError> {
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

/// 构造新格式（多循环体）管道配置：main 体 = prepare/core/post + 路由，
/// 语义对齐原 0.1 平铺 default.yaml 的转换产物（插件链 + next_tool/end 路由）。
fn make_engine_config() -> PipelineConfig {
    let prepare_plugins = [
        "pipeline_tool_schema",
        "pipeline_param_inject",
        "pipeline_multimodal_preprocessor",
        "pipeline_context_window_guard",
        "pipeline_prompt_build",
        "pipeline_pause_guard",
    ]
    .map(|s| s.to_string())
    .to_vec();
    PipelineConfig {
        name: "agentos_agent".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![
                PipelineStep {
                    id: "prepare".into(),
                    steps: prepare_plugins
                        .iter()
                        .map(|s| StepItem::Bare(s.to_string()))
                        .collect(),
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                },
                PipelineStep {
                    id: "core".into(),
                    steps: vec!["{{state.core_plugin}}".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                },
                PipelineStep {
                    id: "post".into(),
                    steps: vec![
                        "pipeline_stop_check".into(),
                        "pipeline_result_format".into(),
                    ],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![
                        Route {
                            when: "raw_tool_calls != [] and raw_tool_calls != None".into(),
                            then: RouteAction {
                                next: RouteNext::Loop,
                                set: HashMap::from([
                                    ("core_type".to_string(), json!("tool_execute")),
                                    ("core_plugin".to_string(), json!("pipeline_tool_core")),
                                ]),
                            },
                        },
                        Route {
                            when: "True".into(),
                            then: RouteAction {
                                next: RouteNext::End,
                                set: HashMap::new(),
                            },
                        },
                    ],
                    loop_config: None,
                },
            ],
            loop_config: Some(LoopConfig {
                enabled: true,
                max_iterations: -1,
            }),
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
    }
}

/// 跑一轮管道：core 插件 emit 一批 messages ops，返回该轮结束时的 state
/// （messages 实录经 executor 的 ops_ledger 落 traces，表/blobs 同步落地）。
async fn run_round(
    store: &Arc<SqliteStore>,
    run_id: &str,
    pipeline_id: &str,
    ops: Vec<Value>,
) -> Value {
    let engine_cfg = make_engine_config();

    let plugin_ids = [
        "pipeline_tool_schema",
        "pipeline_param_inject",
        "pipeline_multimodal_preprocessor",
        "pipeline_context_window_guard",
        "pipeline_prompt_build",
        "pipeline_pause_guard",
        "pipeline_llm_core",
        "pipeline_stop_check",
        "pipeline_result_format",
    ];

    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result(
        "pipeline_llm_core",
        PluginResult {
            state_updates: HashMap::from([
                ("messages".to_string(), json!({ "_ops": ops })),
                ("raw_result".to_string(), json!("回复文本")),
                ("raw_tool_calls".to_string(), json!([])),
            ]),
            ..Default::default()
        },
    );

    let store_dyn: Arc<dyn StorageBackend> = Arc::clone(store) as Arc<dyn StorageBackend>;
    let executor = PipelineExecutor::new(
        invoker as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        TenantContext::new("default", "session_test"),
        plugin_ids.iter().map(|s| s.to_string()),
        store_dyn,
        run_id,
        "main",
    );

    let initial_state = json!({
        "pipeline_id": pipeline_id,
        "message": "你好",
        "agent_id": "agentos",
        "core_type": "llm_call",
        "core_plugin": "pipeline_llm_core",
        "ended": false,
        "suspended": false,
    });
    executor
        .run(
            &engine_cfg,
            &agentos_core::types::StepLibrary::default(),
            initial_state,
        )
        .await
        .expect("run should succeed")
}

// ── 辅助 ────────────────────────────────────────────────────────

/// 取 traces 里最大 created_at 作为回放上界（含此刻及之前的全部实录）。
fn latest_trace_created_at(store: &SqliteStore) -> String {
    store
        .with_conn(|c| -> Result<Option<String>, rusqlite::Error> {
            c.query_row("SELECT MAX(created_at) FROM traces", [], |r| {
                r.get::<_, Option<String>>(0)
            })
        })
        .unwrap()
        .unwrap_or_default()
}

/// 读全部 traces 的 (patch_type, patch_data) 原文，按写入序。
fn all_traces(store: &SqliteStore) -> Vec<(String, String)> {
    store
        .with_conn(|c| -> Result<Vec<(String, String)>, rusqlite::Error> {
            let mut stmt =
                c.prepare("SELECT patch_type, patch_data FROM traces ORDER BY rowid ASC")?;
            let rows =
                stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?;
            rows.collect::<Result<Vec<_>, _>>()
        })
        .unwrap()
}

/// 内存队列的 (seq, role, content) 快照，便于与重建结果比对。
fn queue_snapshot(arr: &[Value]) -> Vec<(u64, String, String)> {
    arr.iter()
        .map(|m| {
            (
                m["seq"].as_u64().expect("元素应带 seq"),
                m["role"].as_str().unwrap_or("").to_string(),
                m["content"].as_str().unwrap_or("").to_string(),
            )
        })
        .collect()
}

/// 表侧槽位 → content 快照（读时重建后的全文）。
fn table_snapshot(store: &SqliteStore, pipeline_id: &str) -> Vec<(u32, String)> {
    store
        .get_slot_messages_by_pipeline(pipeline_id, "default", MessageQueryOpts::default())
        .unwrap()
        .into_iter()
        .map(|r| (r.seq_in_branch, r.content_preview.unwrap_or_default()))
        .collect()
}

// ── 任务 6：重建 + 补偿执行 ─────────────────────────────────────

/// 重建结果 = 中间时刻（上界）的队列：长度、seq、role、内容逐条相等；
/// 上界之后的变更（追加/modify）不在其中。
#[tokio::test]
async fn rebuild_messages_at_restores_midpoint_queue() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());

    // 阶段一：建立队列 seq 0..4（实录落 traces）
    let final1 = run_round(
        &store,
        "run_rb1_a",
        "p_rb1",
        vec![
            set(0, user_msg("m0")),
            set(1, assistant_msg("m1")),
            set(2, user_msg("m2")),
            set(3, assistant_msg("m3")),
            set(4, user_msg("m4")),
        ],
    )
    .await;

    // 中间时刻：已落实录的最大 created_at（含阶段一全部 ops）
    let boundary = latest_trace_created_at(&store);
    std::thread::sleep(Duration::from_millis(20)); // 保证阶段二 created_at 严格大于上界
    let midpoint = queue_snapshot(final1["messages"].as_array().expect("队列应存在"));
    assert_eq!(midpoint.len(), 5, "前置校验：阶段一队列应有 5 条");

    // 阶段二：上界之后继续变更——seq 5 追加 + seq 2 modify
    run_round(
        &store,
        "run_rb1_b",
        "p_rb1",
        vec![
            set(5, assistant_msg("m5-late")),
            set(2, user_msg("m2-modified")),
        ],
    )
    .await;
    let table_now = table_snapshot(&store, "p_rb1");
    assert_eq!(
        table_now.len(),
        6,
        "前置校验：阶段二后表侧应有 6 个槽位，实际：{:?}",
        table_now
    );

    // 重建回到中间时刻
    let rebuilt =
        replay::rebuild_messages_at(&store, "p_rb1", "default", &boundary).expect("重建应成功");

    let rebuilt_snapshot = queue_snapshot(&rebuilt);
    assert_eq!(
        rebuilt_snapshot, midpoint,
        "重建结果应等于中间时刻的队列（长度/seq/role/内容）"
    );

    // 上界之后的变更不得泄入重建结果
    let raw = serde_json::to_string(&rebuilt).unwrap();
    assert!(
        !raw.contains("m5-late"),
        "上界后的追加（seq 5）不应出现：{}",
        raw
    );
    assert!(
        !raw.contains("m2-modified"),
        "上界后的 modify（seq 2）不应出现：{}",
        raw
    );
    assert!(raw.contains("m2"), "中间时刻的 seq 2 原内容应在：{}", raw);
}

/// 补偿执行：rollback 后表回到目标状态、traces 追加 rollback 条目（旧轨迹完好）、
/// 回退后新轮正常追加。
#[tokio::test]
async fn rollback_restores_table_and_records_rollback_trace() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());

    // 阶段一：seq 0..4
    run_round(
        &store,
        "run_rb2_a",
        "p_rb2",
        vec![
            set(0, user_msg("r0")),
            set(1, assistant_msg("r1")),
            set(2, user_msg("r2")),
            set(3, assistant_msg("r3")),
            set(4, user_msg("r4")),
        ],
    )
    .await;

    let boundary = latest_trace_created_at(&store);
    std::thread::sleep(Duration::from_millis(20));
    let traces_before = all_traces(&store);

    // 阶段二：seq 5 追加 + seq 2 modify
    run_round(
        &store,
        "run_rb2_b",
        "p_rb2",
        vec![
            set(5, assistant_msg("r5-late")),
            set(2, user_msg("r2-modified")),
        ],
    )
    .await;

    // 补偿执行：回到中间时刻
    replay::rollback(&store, "p_rb2", "default", &boundary).expect("rollback 应成功");

    // 表侧 = 目标状态：槽位回到 0..4，槽 2 恢复旧内容（非 modified）
    let table = table_snapshot(&store, "p_rb2");
    let seqs: Vec<u32> = table.iter().map(|(s, _)| *s).collect();
    assert_eq!(
        seqs,
        vec![0, 1, 2, 3, 4],
        "回退后表应回到目标槽位集合（seq 5 清空）"
    );
    let (_, slot2) = table.iter().find(|(s, _)| *s == 2).unwrap();
    assert_eq!(
        slot2, "r2",
        "回退后槽 2 应恢复旧内容（补偿 set 旧指纹内容），实际：{:?}",
        table
    );

    // traces：出现 rollback 条目；旧轨迹 append-only 完好
    let traces_after = all_traces(&store);
    assert!(
        traces_after.iter().any(|(ptype, _)| ptype == "rollback"),
        "补偿执行后 traces 应出现 PatchType::Rollback（rollback）条目"
    );
    assert!(
        traces_after.len() > traces_before.len(),
        "rollback 条目是追加，traces 总数应增加"
    );
    for (idx, entry) in traces_before.iter().enumerate() {
        assert_eq!(
            &traces_after[idx], entry,
            "旧轨迹不得被抹改（append-only），第 {idx} 条被改动"
        );
    }

    // 回退后新轮正常追加
    run_round(
        &store,
        "run_rb2_c",
        "p_rb2",
        vec![set(5, assistant_msg("r5-new-round"))],
    )
    .await;
    let table = table_snapshot(&store, "p_rb2");
    let seqs: Vec<u32> = table.iter().map(|(s, _)| *s).collect();
    assert_eq!(seqs, vec![0, 1, 2, 3, 4, 5], "回退后继续对话应正常追加");
    let (_, slot5) = table.iter().find(|(s, _)| *s == 5).unwrap();
    assert_eq!(slot5, "r5-new-round");
}

/// 存储读失败 → rollback 返回 Err 且不写任何补偿（表保持失败前状态，
/// 不生成清空/恢复 ops）。注入方式：DROP traces 表（ledger_ops_upto 的
/// prepare 失败 = 真实存储读错误；SqliteStore 是具体类型，无法包 wrapper
/// 注入，用 SQL 级破坏最直接）。
#[tokio::test]
async fn rollback_storage_read_failure_propagates_without_compensation() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());

    // 阶段一：seq 0..1
    run_round(
        &store,
        "run_rb3_a",
        "p_rb3",
        vec![set(0, user_msg("b0")), set(1, assistant_msg("b1"))],
    )
    .await;
    let boundary = latest_trace_created_at(&store);
    std::thread::sleep(Duration::from_millis(20));

    // 阶段二（上界之后）：seq 2 追加
    run_round(
        &store,
        "run_rb3_b",
        "p_rb3",
        vec![set(2, assistant_msg("b2-late"))],
    )
    .await;
    let table_before = table_snapshot(&store, "p_rb3");
    assert_eq!(table_before.len(), 3, "前置校验：失败注入前表有 3 个槽位");

    // 注入存储读失败：traces 不可读
    store
        .with_conn(|c| c.execute("DROP TABLE traces", []))
        .expect("drop traces 应成功");

    let result = replay::rollback(&store, "p_rb3", "default", &boundary);
    assert!(
        result.is_err(),
        "存储读失败应传播为 Err（而非吞错生成清空补偿）：{result:?}"
    );

    // 不写补偿：表与失败前逐位一致（若吞错重建为空，会把 seq 0..2 全清空）
    let table_after = table_snapshot(&store, "p_rb3");
    assert_eq!(
        table_before, table_after,
        "rollback 失败不得写补偿 ops（表保持原状）"
    );
}
