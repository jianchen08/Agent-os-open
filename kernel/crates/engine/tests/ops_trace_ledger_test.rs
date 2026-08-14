// @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: rust-test
//! 任务 3 验收测试：**ops 即轨迹**（traces 表的 messages 实录）。
//!
//! 目标架构（docs/tasks/task_messages_op_trace_unification.md 任务 3）：
//! 插件 emit 的 messages op 被 apply 时，除内存 state 与 message_slots 表外，
//! 还要在 traces 表落一份**实录**——形态 `{"messages": {"_ops": [...]}}`，
//! 每个 op 只含 `op + seq + message_id 指纹`（delete 记 `message_id: null`），
//! **绝无 msg 全文**（全文只在 blobs）。轨迹是实录而非 diff 推断。
//!
//! 实录落点（主线实现形态）：`PipelineExecutor` 持 per-step `ops_ledger` 缓冲，
//! merge_and_project 应用插件 ops 时降级指纹累积，`persist_step_trace` 在 step
//! 边界拼进 patch_data 落 traces——因此本文件经**真实管道执行**（MockInvoker +
//! SqliteStore）驱动实录，而非裸调 `apply_messages_op_update`（后者只写内存+表，
//! 无 run/branch 上下文，不落 trace）。
//!
//! 不变量断言：
//! - 管道跑完后 traces 存在 messages 实录条目，op 形态 = op/seq/mc_ 指纹，无全文；
//! - delete（msg=null）的实录 message_id 为 null 或缺失（与 set 实录可区分）；
//! - 实录指纹与 message_slots 表侧 message_id 同源（同一内容锚）；
//! - 标量字段的 step diff 仍在 trace（与实录共存于同一 patch_data）；
//! - `get_step_traces_by_thread` 读回的轨迹内容与 branch 读一致。

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use agentos_core::traits::{MessageQueryOpts, PluginInvoker, StorageBackend};
use agentos_core::types::{
    LoopBody, LoopConfig, PluginContext, PluginError, PluginResult, PipelineConfig, PipelineStep,
    Route, RouteAction, RouteNext, TenantContext, ToolExecutionResult,
};
use agentos_engine::{PipelineExecutor, SqliteStore};
use async_trait::async_trait;
use serde_json::{json, Value};

// ── op 构造辅助（对齐既有 message_*_test.rs 模式）─────────────────

fn set(seq: u64, msg: Value) -> Value {
    json!({ "op": "set", "seq": seq, "msg": msg })
}

fn clear(seq: u64) -> Value {
    json!({ "op": "set", "seq": seq, "msg": null })
}

// ── traces 读取辅助（经公开的 with_conn 出口直接 SQL，形状无关）────

/// 读全部 traces 的 (patch_type, patch_data 原文)，按写入序。
///
/// 不经 branch/run 归属（实录的 run_id/branch_id 归属由实现决定，测试不做假设），
/// 只扫 patch_data 本身——这是对"traces 里有没有实录"最不强加形状的读法。
fn all_traces(store: &SqliteStore) -> Vec<(String, String)> {
    store
        .with_conn(|c| -> Result<Vec<(String, String)>, rusqlite::Error> {
            let mut stmt =
                c.prepare("SELECT patch_type, patch_data FROM traces ORDER BY rowid ASC")?;
            let rows = stmt.query_map([], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })?;
            Ok(rows.collect::<Result<Vec<_>, _>>()?)
        })
        .expect("读 traces 应成功")
}

/// 从 traces 里抽出含 messages 实录（`messages._ops` 数组）的 patch_data。
fn ledger_entries(store: &SqliteStore) -> Vec<Value> {
    all_traces(store)
        .into_iter()
        .filter_map(|(_, data)| serde_json::from_str::<Value>(&data).ok())
        .filter(|d| d.pointer("/messages/_ops").and_then(|v| v.as_array()).is_some())
        .collect()
}

/// 汇总全部实录 ops（跨多条 trace 条目）。
fn all_ledger_ops(store: &SqliteStore) -> Vec<Value> {
    ledger_entries(store)
        .iter()
        .flat_map(|d| d["messages"]["_ops"].as_array().unwrap().clone())
        .collect()
}

/// 单条实录 op 的形态契约：只有 op + seq + 指纹，绝无 msg 全文。
fn assert_ledger_op_shape(op: &Value) {
    assert_eq!(
        op.get("op").and_then(|v| v.as_str()),
        Some("set"),
        "实录 op 应为 set 原语：{:?}",
        op
    );
    assert!(
        op.get("seq").and_then(|v| v.as_u64()).is_some(),
        "实录必须带稳定 seq 槽位号：{:?}",
        op
    );
    match op.get("message_id") {
        None | Some(Value::Null) => {} // delete：指纹为 null 或缺失
        Some(Value::String(s)) => assert!(
            s.starts_with("mc_"),
            "指纹应为 mc_ 前缀的内容派生 id：{:?}",
            s
        ),
        Some(other) => panic!("message_id 形态异常：{}", other),
    }
    let keys: Vec<&str> = op.as_object().unwrap().keys().map(|k| k.as_str()).collect();
    assert!(
        keys.iter().all(|k| matches!(*k, "op" | "seq" | "at" | "message_id" | "blob_id")),
        "实录 op 绝无 msg 全文字段（只允许 op/seq/at/message_id/blob_id 定位字段），实际字段：{:?}",
        keys
    );
    // blob_id（若有）是全文 blob 定位符（裸 SHA256 hex），不是内容
    if let Some(Value::String(b)) = op.get("blob_id") {
        assert_eq!(b.len(), 64, "blob_id 应为 64 hex：{b}");
    }
}

// ── 全链路驱动：MockInvoker + default.yaml + 真实 SqliteStore ──────

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

/// 构造新格式（多循环体）管道配置：main 体 = prepare/core/post + 路由。
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
                    steps: prepare_plugins,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                },
                PipelineStep {
                    id: "core".into(),
                    steps: vec!["{{state.core_plugin}}".into()],
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
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
    }
}

/// 跑一条真实管道（MockInvoker + SqliteStore）：core 插件 emit messages ops + 标量。
async fn run_pipeline_emit_ops(
    store: &Arc<SqliteStore>,
    run_id: &str,
    pipeline_id: &str,
    ops: Vec<Value>,
) {
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
                (
                    "messages".to_string(),
                    json!({ "_ops": ops }),
                ),
                ("turn_count".to_string(), json!(7)),
                ("raw_result".to_string(), json!("STEP_DIFF_MARKER 回复文本")),
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
        .expect("run should succeed");
}

// ── 任务 3 核心：ops 即轨迹 ──────────────────────────────────────

/// set 追加的 op 在 traces 里留下 op+seq+指纹的实录，且**无任何全文**。
#[tokio::test]
async fn emitted_ops_leave_fingerprint_ledger_in_traces_without_fulltext() {
    const USER_FULL: &str = "LEDGER_FULLTEXT_MARKER_USER 这是一条用于验证 trace 不含全文的用户消息";
    const ASSISTANT_FULL: &str = "LEDGER_FULLTEXT_MARKER_ASSISTANT 助手回复全文标记";

    let store = Arc::new(SqliteStore::open_memory().unwrap());
    run_pipeline_emit_ops(
        &store,
        "run_ledger_a",
        "p_ledger_a",
        vec![
            set(0, json!({ "role": "user", "content": USER_FULL })),
            set(1, json!({ "role": "assistant", "content": ASSISTANT_FULL })),
        ],
    )
    .await;

    let ledgers = ledger_entries(&store);
    assert!(
        !ledgers.is_empty(),
        "管道跑完后 traces 应存在 messages 实录条目（ops 即轨迹）"
    );

    let ops = all_ledger_ops(&store);
    assert_eq!(ops.len(), 2, "两个 set op 应各留一条实录");
    for op in &ops {
        assert_ledger_op_shape(op);
    }
    let seqs: Vec<u64> = ops.iter().filter_map(|o| o["seq"].as_u64()).collect();
    assert_eq!(seqs, vec![0, 1], "实录 seq 与插件 emit 的 op 一致");

    // 绝无全文：全部 trace patch_data 序列化原文不含消息正文标记
    for (_, raw) in all_traces(&store) {
        assert!(
            !raw.contains(USER_FULL) && !raw.contains("LEDGER_FULLTEXT_MARKER_USER"),
            "trace 不得含消息全文（全文只在 blobs）：{}",
            raw
        );
        assert!(
            !raw.contains(ASSISTANT_FULL) && !raw.contains("LEDGER_FULLTEXT_MARKER_ASSISTANT"),
            "trace 不得含消息全文（全文只在 blobs）：{}",
            raw
        );
    }
}

/// delete（set seq, null）的实录 message_id 为 null 或缺失，与 set 实录可区分。
#[tokio::test]
async fn delete_op_ledger_records_null_fingerprint() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    run_pipeline_emit_ops(
        &store,
        "run_ledger_b",
        "p_ledger_b",
        vec![
            set(0, json!({ "role": "user", "content": "a" })),
            set(1, json!({ "role": "assistant", "content": "b" })),
            set(2, json!({ "role": "user", "content": "c" })),
            clear(1),
        ],
    )
    .await;

    let ops = all_ledger_ops(&store);
    assert_eq!(ops.len(), 4, "4 个 op（3 set + 1 delete）应各留一条实录");

    // delete 实录：seq=1 且指纹为 null/缺失
    let del = ops.iter().find(|o| {
        o["seq"].as_u64() == Some(1) && o.get("message_id").map_or(true, |v| v.is_null())
    });
    assert!(
        del.is_some(),
        "delete（set seq, null）的实录 message_id 应为 null 或缺失，全部实录：{:?}",
        ops
    );

    // 同槽位的 set 实录仍在（带指纹）→ delete 与 set 可区分
    let set1 = ops.iter().find(|o| {
        o["seq"].as_u64() == Some(1)
            && o.get("message_id")
                .and_then(|v| v.as_str())
                .map_or(false, |s| s.starts_with("mc_"))
    });
    assert!(set1.is_some(), "槽 1 的 set 实录应带 mc_ 指纹，全部实录：{:?}", ops);

    // 表侧 delete 已生效：槽位 0,2（1 为 gap）
    let rows = store
        .get_slot_messages_by_pipeline("p_ledger_b", "default", MessageQueryOpts::default())
        .unwrap();
    let seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    assert_eq!(seqs, vec![0, 2], "表侧 delete 留 gap：槽 0,2");
}

/// 实录指纹与 message_slots 表侧 message_id 同源（同一内容锚）。
#[tokio::test]
async fn ledger_fingerprint_matches_slot_message_id() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    run_pipeline_emit_ops(
        &store,
        "run_ledger_c",
        "p_ledger_c",
        vec![
            set(0, json!({ "role": "user", "content": "指纹同源验证" })),
            set(1, json!({ "role": "assistant", "content": "回复" })),
        ],
    )
    .await;

    let ops = all_ledger_ops(&store);
    let seq0_fingerprint = ops
        .iter()
        .find(|o| o["seq"].as_u64() == Some(0))
        .and_then(|o| o.get("message_id"))
        .and_then(|v| v.as_str())
        .expect("seq 0 的实录应带 message_id 指纹")
        .to_string();
    assert!(seq0_fingerprint.starts_with("mc_"), "mc_ 前缀");

    let rows = store
        .get_slot_messages_by_pipeline("p_ledger_c", "default", MessageQueryOpts::default())
        .unwrap();
    let row0 = rows.iter().find(|r| r.seq_in_branch == 0).expect("槽 0 应在表");
    assert_eq!(
        row0.message_id, seq0_fingerprint,
        "实录指纹应与表侧 message_id 同源（内容锚一处定义）"
    );
}

// ── 标量 step diff 与实录共存 + thread 读回一致性 ─────────────────

/// 标量 step diff 仍在 trace，且与 messages 实录共存于同一 patch_data。
#[tokio::test]
async fn step_trace_keeps_scalar_diff_besides_messages_ledger() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    run_pipeline_emit_ops(
        &store,
        "run_ledger_d",
        "p_ledger_d",
        vec![
            set(0, json!({ "role": "user", "content": "FULLTEXT_MARKER_d_用户问" })),
            set(1, json!({ "role": "assistant", "content": "FULLTEXT_MARKER_d_助手答" })),
        ],
    )
    .await;

    let traces = store.get_traces("main", 0, u32::MAX).unwrap();
    assert!(!traces.is_empty(), "管道跑完应有 step 轨迹");

    // 标量 diff 仍在：turn_count 出现在某条 trace 的 patch_data 里
    let entry = traces
        .iter()
        .find(|t| t.patch_data.get("turn_count").and_then(|v| v.as_u64()) == Some(7))
        .expect("标量字段的 step diff 应仍在 trace");

    // 同一条目里应有 messages 实录（不再被 REDUNDANT_KEYS 整个过滤掉）
    let ops = entry
        .patch_data
        .pointer("/messages/_ops")
        .and_then(|v| v.as_array())
        .expect("同一 trace 条目应含 messages 实录（标量 diff 与实录共存）");
    assert_eq!(ops.len(), 2, "core 插件 emit 了 2 个 set op");
    for op in ops {
        assert_ledger_op_shape(op);
    }
    let seqs: Vec<u64> = ops.iter().filter_map(|o| o["seq"].as_u64()).collect();
    assert_eq!(seqs, vec![0, 1]);

    // 实录无全文（raw_result 已过滤 + 指纹化，正文标记词不出现）
    let raw = serde_json::to_string(&entry.patch_data).unwrap();
    assert!(
        !raw.contains("FULLTEXT_MARKER_d_用户问") && !raw.contains("FULLTEXT_MARKER_d_助手答"),
        "实录条目不得含消息全文：{}",
        raw
    );
}

/// `get_step_traces_by_thread`（trait 方法）读回的实录内容与 branch 读一致。
#[tokio::test]
async fn get_step_traces_by_thread_reads_back_ledger_content() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let pipeline_id = "p_ledger_e";
    let run_id = "run_ledger_e";
    run_pipeline_emit_ops(
        &store,
        run_id,
        pipeline_id,
        vec![
            set(0, json!({ "role": "user", "content": "FULLTEXT_MARKER_e_问" })),
            set(1, json!({ "role": "assistant", "content": "FULLTEXT_MARKER_e_答" })),
        ],
    )
    .await;

    // 会话 ↔ 管道映射（trait 公开方法）
    let backend: &dyn StorageBackend = store.as_ref();
    backend
        .link_pipeline_session(pipeline_id, "th_ledger_e", "default")
        .await
        .unwrap();

    // run 归属链接（实现侧发现路径未定，两条都种上）：
    // (a) 目标架构：message_slots.run_id（任务 7 后 slots 纯索引含 run_id）；
    store
        .with_conn(|c| -> Result<usize, rusqlite::Error> {
            Ok(c.execute(
                "UPDATE message_slots SET run_id = ?1 WHERE tenant_id = ?2 AND pipeline_id = ?3",
                rusqlite::params![run_id, "default", pipeline_id],
            )?)
        })
        .unwrap();
    // (b) 现状：经 messages 表反查 run_id（表退役后此 INSERT 失败，忽略——路径 (a) 兜底）。
    let _ = store.with_conn(|c| -> Result<(), rusqlite::Error> {
        c.execute(
            "INSERT OR IGNORE INTO messages \
             (message_id, run_id, branch_id, seq_in_branch, role, content_preview, tenant_id, created_at, pipeline_id) \
             VALUES ('m_seed_rb_e', ?1, 'main', 0, 'user', 'seed', 'default', ?2, ?3)",
            rusqlite::params![run_id, chrono::Utc::now().to_rfc3339(), pipeline_id],
        )?;
        Ok(())
    });

    let via_branch = store.get_traces("main", 0, u32::MAX).unwrap();
    assert!(!via_branch.is_empty(), "branch 侧应能读到 step 轨迹");

    let via_thread = backend
        .get_step_traces_by_thread("th_ledger_e", "default")
        .await
        .unwrap();
    assert!(
        !via_thread.is_empty(),
        "thread 侧应能发现该管道的 step 轨迹"
    );

    // branch 读到的每条轨迹都能按 trace_id 在 thread 读回中找到，patch_data 一致
    for t in &via_branch {
        let twin = via_thread.iter().find(|x| x.trace_id == t.trace_id);
        let twin = twin.expect("step 轨迹应可经 get_step_traces_by_thread 读回");
        assert_eq!(
            twin.patch_data, t.patch_data,
            "thread 读回的 patch_data 应与 branch 读一致（trace_id={}）",
            t.trace_id
        );
    }

    // thread 读回的实录条目本身也无全文
    for t in &via_thread {
        let raw = serde_json::to_string(&t.patch_data).unwrap();
        assert!(
            !raw.contains("FULLTEXT_MARKER_e_问") && !raw.contains("FULLTEXT_MARKER_e_答"),
            "thread 读回的实录不得含消息全文：{}",
            raw
        );
    }
}
