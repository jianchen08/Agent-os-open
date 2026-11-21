// @feature: FP-0.2.〇 管道引擎 入口占位行（消息槽位与身份同刻落账） | @ci: rust-test
//! 入口占位行契约测试（设计真值
//! docs/decisions/2026-11-06-round-entry-slot-placeholder.md）。
//!
//! 五条契约（TDD 规格）：
//! 1. 迭代开始（message_id 铸造点）后 message_slots 存在本轮占位行
//!    （status=streaming、空 content、record_id=本轮 id、run_id=当前 run），
//!    seq=保留值（内存消息 max+1）。
//! 2. 轮末投影后同 seq UPDATE 为终态且无双行（seq 分配权单点：出口 append
//!    与入口保留同基准，ON CONFLICT 同槽覆写）。
//! 3. 真零产出轮后占位行被删（生命周期封闭于单轮，不跨轮复用）。
//! 4. run 起手 sweep 收口前代 streaming 行（空删、有内容标 interrupted、
//!    非 streaming 行与异管道行不动）。
//! 5. 入口建行失败时轮次照常完成且回落既有出口 INSERT 行为（降级=等价
//!    旧世界，非吞错）。

use std::collections::HashMap;
use std::future::Future;
use std::path::Path;
use std::pin::Pin;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use agentos_core::traits::{MessageQueryOpts, PluginInvoker, SessionListFilter, StorageBackend};
use agentos_core::types::{
    LoopBody, MessageRecord, PipelineConfig, PipelineStep, PluginContext, PluginError,
    PluginResult, Route, RouteAction, RouteNext, StepItem, StorageError, ToolExecutionResult,
};
use agentos_engine::compiler::compile_pipeline;
use agentos_engine::{PipelineExecutor, RoundEnd, RoundEvents, RoundStart, SqliteStore};
use async_trait::async_trait;
use serde_json::json;

/// 占位 blob 的稳定判据（与引擎实现同词汇）：role=assistant + status=streaming。
fn placeholder_blob(message_id: &str, seq: i64) -> serde_json::Value {
    json!({
        "op": "set",
        "seq": seq,
        "msg": { "role": "assistant", "content": "", "status": "streaming" },
        "_message_id": message_id,
        "_run_id": "run_seed",
    })
}

async fn read_rows(store: &Arc<SqliteStore>, pipeline_id: &str) -> Vec<MessageRecord> {
    let dyn_store: Arc<dyn StorageBackend> = store.clone();
    dyn_store
        .get_messages_by_pipeline(pipeline_id, MessageQueryOpts::default())
        .await
        .expect("read message_slots")
}

/// 轮次观察点录制器：on_round_start 时刻快照 message_slots 全量行——
/// 占位行「在轮次开始时已存在」的行为断言数据源（不在回调内 panic，
/// 快照后置断言）。
struct SlotRecorder {
    events: Mutex<Vec<Event>>,
    store: Arc<SqliteStore>,
}

enum Event {
    Start(RoundStart, Vec<MessageRecord>),
    #[allow(dead_code)] // 记录面保留 End 快照；当前断言只消费 starts
    End(RoundEnd),
}

impl SlotRecorder {
    fn new(store: Arc<SqliteStore>) -> Self {
        Self {
            events: Mutex::new(Vec::new()),
            store,
        }
    }

    fn starts(&self) -> Vec<(RoundStart, Vec<MessageRecord>)> {
        self.events
            .lock()
            .unwrap()
            .iter()
            .filter_map(|e| match e {
                Event::Start(s, rows) => Some((s.clone(), rows.clone())),
                _ => None,
            })
            .collect()
    }

    #[allow(dead_code)] // 预留断言面，当前用例未消费
    fn ends(&self) -> Vec<RoundEnd> {
        self.events
            .lock()
            .unwrap()
            .iter()
            .filter_map(|e| match e {
                Event::End(e) => Some(e.clone()),
                _ => None,
            })
            .collect()
    }
}

impl RoundEvents for SlotRecorder {
    fn on_round_start(&self, ev: RoundStart) -> Pin<Box<dyn Future<Output = ()> + Send + '_>> {
        let store = self.store.clone();
        Box::pin(async move {
            let rows = read_rows(&store, &ev.pipeline_id).await;
            self.events.lock().unwrap().push(Event::Start(ev, rows));
        })
    }

    fn on_round_end(&self, ev: RoundEnd) -> Pin<Box<dyn Future<Output = ()> + Send + '_>> {
        Box::pin(async move {
            self.events.lock().unwrap().push(Event::End(ev));
        })
    }
}

/// 按调用序返回不同结果的 invoker：llm_core 第 1 次调用产出带工具调用的回复，
/// 第 2 次起产出纯文本回复；tool_core 返回工具结果消息（与 round_events_test
/// 同款两轮形态）。`silent_llm = true` 时 llm_core 零 messages 产出（真零产出轮）。
struct RoundInvoker {
    calls: Mutex<Vec<String>>,
    /// 每次插件调用时 state 内的本轮消息槽位（`_message_seq`）快照：
    /// 断言 tool_core 能读到与 stream_start.sequence 同源的权威值。
    seen_seqs: Mutex<Vec<(String, Option<i64>)>>,
    silent_llm: bool,
}

impl RoundInvoker {
    fn new() -> Self {
        Self {
            calls: Mutex::new(Vec::new()),
            seen_seqs: Mutex::new(Vec::new()),
            silent_llm: false,
        }
    }

    fn silent() -> Self {
        Self {
            calls: Mutex::new(Vec::new()),
            seen_seqs: Mutex::new(Vec::new()),
            silent_llm: true,
        }
    }

    fn seen_seqs(&self) -> Vec<(String, Option<i64>)> {
        self.seen_seqs.lock().unwrap().clone()
    }
}

#[async_trait]
impl PluginInvoker for RoundInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        _ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        self.calls.lock().unwrap().push(plugin_id.to_string());
        self.seen_seqs.lock().unwrap().push((
            plugin_id.to_string(),
            _ctx.state.get("_message_seq").and_then(|v| v.as_i64()),
        ));
        let count = self
            .calls
            .lock()
            .unwrap()
            .iter()
            .filter(|p| p.as_str() == plugin_id)
            .count();
        match plugin_id {
            "pipeline_llm_core" => {
                if self.silent_llm {
                    // 真零产出：文本/思考/保留 tool_calls 三空，零 messages 键
                    //（llm_core 三路判据的零产出臂，2026-11-04 契约）。
                    return Ok(PluginResult {
                        state_updates: HashMap::from([("raw_result".to_string(), json!(""))]),
                        ..Default::default()
                    });
                }
                if count == 1 {
                    Ok(PluginResult {
                        state_updates: HashMap::from([
                            (
                                "messages".to_string(),
                                json!({
                                    "_ops": [{
                                        "op": "set",
                                        "msg": {
                                            "role": "assistant",
                                            "content": "第一步回复",
                                            "tool_calls": [
                                                { "id": "call_1", "name": "dummy_tool", "arguments": "{}" }
                                            ],
                                        },
                                    }]
                                }),
                            ),
                            ("raw_result".to_string(), json!("第一步回复")),
                            (
                                "raw_tool_calls".to_string(),
                                json!([{ "type": "function", "id": "call_1", "name": "dummy_tool", "arguments": "{}" }]),
                            ),
                        ]),
                        ..Default::default()
                    })
                } else {
                    Ok(PluginResult {
                        state_updates: HashMap::from([
                            (
                                "messages".to_string(),
                                json!({
                                    "_ops": [
                                        { "op": "set", "msg": { "role": "assistant", "content": "第二步回复" } }
                                    ]
                                }),
                            ),
                            ("raw_result".to_string(), json!("第二步回复")),
                            ("raw_tool_calls".to_string(), json!([])),
                        ]),
                        ..Default::default()
                    })
                }
            }
            "pipeline_tool_core" => Ok(PluginResult {
                state_updates: HashMap::from([
                    (
                        "messages".to_string(),
                        json!({
                            "_ops": [
                                { "op": "set", "msg": { "role": "tool", "content": "工具结果", "tool_call_id": "call_1" } }
                            ]
                        }),
                    ),
                    ("raw_result".to_string(), json!("工具结果")),
                ]),
                ..Default::default()
            }),
            _ => Ok(PluginResult::default()),
        }
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

/// 两轮循环配置：llm#1 带工具调用 → tool 结果 → llm#2 纯文本（与
/// round_events_test 同款，保证轮次形态可比）。
fn two_round_config(name: &str) -> PipelineConfig {
    PipelineConfig {
        name: name.into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "core".into(),
                steps: vec![
                    StepItem::Bare("pipeline_llm_core".into()),
                    StepItem::Bare("pipeline_tool_core".into()),
                ],
                when: None,
                context: HashMap::new(),
                routes: vec![
                    Route {
                        when: "raw_tool_calls != [] and raw_tool_calls != None".into(),
                        then: RouteAction {
                            next: RouteNext::Loop,
                            set: HashMap::new(),
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
            }],
            while_cond: Some("True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: agentos_core::types::CheckpointConfig::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    }
}

/// 单步非循环体（单次执行 = 一轮）：零产出 sweep 测试用。
fn single_step_config(name: &str) -> PipelineConfig {
    PipelineConfig {
        name: name.into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "core".into(),
                steps: vec![StepItem::Bare("pipeline_llm_core".into())],
                when: None,
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: agentos_core::types::CheckpointConfig::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    }
}

fn make_executor(
    invoker: Arc<dyn PluginInvoker>,
    store: Arc<SqliteStore>,
    recorder: Arc<SlotRecorder>,
    run_id: &str,
) -> PipelineExecutor {
    let store_dyn: Arc<dyn StorageBackend> = store;
    PipelineExecutor::new(
        invoker,
        Path::new(".").to_path_buf(),
        agentos_core::types::TenantContext::new("default", "session_slot"),
        vec![
            "pipeline_llm_core".to_string(),
            "pipeline_tool_core".to_string(),
        ],
        store_dyn,
        run_id,
    )
    .with_round_events(recorder)
}

fn initial_state(pipeline_id: &str) -> serde_json::Value {
    json!({
        "pipeline_id": pipeline_id,
        "message": "hi",
        "agent_id": "agentos",
        "core_plugin": "pipeline_llm_core",
        "core_type": "llm_call",
        "ended": false,
        "suspended": false,
        "session_id": "thread-slot",
        "messages": [{ "role": "user", "content": "hi", "seq": 0 }],
    })
}

// ── 契约 1：迭代开始建占位行 ─────────────────────────────────────

#[tokio::test]
async fn round_start_creates_streaming_placeholder_with_reserved_seq() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let recorder = Arc::new(SlotRecorder::new(store.clone()));
    let executor = make_executor(
        Arc::new(RoundInvoker::new()),
        store.clone(),
        recorder.clone(),
        "run_slot_entry",
    );
    let compiled = compile_pipeline(
        &two_round_config("slot_entry"),
        &Default::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");
    executor
        .run_compiled(&compiled, initial_state("p_slot"))
        .await
        .expect("run ok");

    let starts = recorder.starts();
    assert_eq!(starts.len(), 2, "两轮各一次 round_start");
    // 保留值 = 内存消息 max+1：首轮 user(seq 0) → 保留 1；次轮 user+assistant+tool
    // → 保留 3。
    assert_eq!(starts[0].0.sequence, 1, "首轮保留 seq = user(0)+1");
    assert_eq!(
        starts[1].0.sequence, 3,
        "次轮保留 seq = 前三轮消息 max(2)+1"
    );

    for (start, rows) in &starts {
        let seq = start.sequence as u32;
        let slot = rows
            .iter()
            .find(|r| r.seq_in_branch == seq)
            .unwrap_or_else(|| panic!("round_start 时刻 seq {seq} 槽位应已有占位行"));
        assert_eq!(slot.role, "assistant", "占位行 role=assistant");
        assert_eq!(
            slot.status.as_deref(),
            Some("streaming"),
            "占位行 status=streaming"
        );
        assert_eq!(
            slot.content_preview.as_deref(),
            Some(""),
            "占位行空 content"
        );
        assert_eq!(
            slot.message_id, start.message_id,
            "占位行 record_id=本轮铸造的 message_id"
        );
        assert_eq!(slot.run_id, "run_slot_entry", "占位行 run_id=当前 run");
    }
}

// ── 契约 1b：本轮槽位随 state 透传给插件（工具事件载荷来源）──────

/// tool_core 只能经 state 拿本轮消息槽位：`_message_seq` 必须与
/// stream_start.sequence 同源（开轮保留值），工具事件据此携带宿主 seq，
/// 工具卡容器才不在中断后因缺 seq 垫底。
#[tokio::test]
async fn plugins_read_round_message_seq_from_state() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let recorder = Arc::new(SlotRecorder::new(store.clone()));
    let invoker = Arc::new(RoundInvoker::new());
    let executor = make_executor(
        invoker.clone(),
        store.clone(),
        recorder.clone(),
        "run_slot_state_seq",
    );
    let compiled = compile_pipeline(
        &two_round_config("slot_state_seq"),
        &Default::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");
    executor
        .run_compiled(&compiled, initial_state("p_slot_state"))
        .await
        .expect("run ok");

    let starts = recorder.starts();
    let seq_of = |plugin: &str| -> Vec<Option<i64>> {
        invoker
            .seen_seqs()
            .into_iter()
            .filter(|(p, _)| p == plugin)
            .map(|(_, seq)| seq)
            .collect()
    };
    // 首轮开轮保留 1（user seq 0 + 1），次轮保留 3（前三消息 max 2 + 1）——
    // 与 round_start 事件同值；两轮形态见 two_round_config。
    assert_eq!(
        seq_of("pipeline_llm_core"),
        vec![Some(1), Some(3)],
        "LLM 步读到本轮保留槽位"
    );
    assert_eq!(
        seq_of("pipeline_tool_core"),
        vec![Some(1), Some(3)],
        "tool_core 读到宿主消息槽位（工具事件载荷 sequence 来源）"
    );
    assert_eq!(
        seq_of("pipeline_llm_core"),
        starts
            .iter()
            .map(|(s, _)| Some(s.sequence))
            .collect::<Vec<_>>(),
        "state 槽位与 RoundStart.sequence（stream_start.sequence）同源"
    );
}

// ── 契约 2：轮末投影同 seq UPDATE，绝不双行 ─────────────────────

#[tokio::test]
async fn exit_projection_fills_placeholder_same_seq_no_double_row() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let recorder = Arc::new(SlotRecorder::new(store.clone()));
    let executor = make_executor(
        Arc::new(RoundInvoker::new()),
        store.clone(),
        recorder.clone(),
        "run_slot_fill",
    );
    let compiled = compile_pipeline(
        &two_round_config("slot_fill"),
        &Default::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");
    let final_state = executor
        .run_compiled(&compiled, initial_state("p_slot"))
        .await
        .expect("run ok");

    let starts = recorder.starts();
    assert_eq!(starts.len(), 2);
    let rows = read_rows(&store, "p_slot").await;

    // 绝不双行：全表恰 4 行（assistant1 + tool2 + assistant3 + tool4；user 消息
    // 由生产派发层落表，本夹具无派发层故表内无 user 行）。seq 集合精确——
    // 占位行被出口投影同槽 UPDATE，未产生第二行。
    let mut seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    seqs.sort();
    assert_eq!(seqs, vec![1, 2, 3, 4], "占位行同槽 UPDATE，无双行无空洞");

    // 入口保留的 seq 即终态 seq（seq 分配权单点：投影不重排不另配）。
    for (start, _) in &starts {
        let slot = rows
            .iter()
            .find(|r| r.seq_in_branch == start.sequence as u32)
            .expect("保留槽位终态存在");
        assert_eq!(
            slot.message_id, start.message_id,
            "终态行 record_id=本轮 message_id（流式占位与 DB 重载同键）"
        );
        assert_ne!(
            slot.status.as_deref(),
            Some("streaming"),
            "终态行不再 streaming"
        );
    }
    let slot1 = rows.iter().find(|r| r.seq_in_branch == 1).unwrap();
    assert_eq!(slot1.content_preview.as_deref(), Some("第一步回复"));
    let slot3 = rows.iter().find(|r| r.seq_in_branch == 3).unwrap();
    assert_eq!(slot3.content_preview.as_deref(), Some("第二步回复"));

    // 内存数组不被占位行污染：最终 state messages 恰 5 条（user+两轮
    // assistant+两轮 tool；占位行 DB-only，不进内存数组）。
    let msgs = final_state
        .get("messages")
        .and_then(|v| v.as_array())
        .expect("messages array");
    assert_eq!(msgs.len(), 5, "占位行不进内存数组");
}

// ── 契约 3：真零产出轮删占位行 ─────────────────────────────────

#[tokio::test]
async fn zero_output_round_deletes_placeholder() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let recorder = Arc::new(SlotRecorder::new(store.clone()));
    let executor = make_executor(
        Arc::new(RoundInvoker::silent()),
        store.clone(),
        recorder.clone(),
        "run_slot_zero",
    );
    let compiled = compile_pipeline(
        &single_step_config("slot_zero"),
        &Default::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");
    executor
        .run_compiled(&compiled, initial_state("p_zero"))
        .await
        .expect("run ok");

    // 轮开始时占位行在场（建行确实发生了），轮结束（真零产出）后被删。
    let starts = recorder.starts();
    assert_eq!(starts.len(), 1);
    let (start, rows_at_start) = &starts[0];
    assert!(
        rows_at_start
            .iter()
            .any(|r| r.seq_in_branch == start.sequence as u32
                && r.status.as_deref() == Some("streaming")),
        "零产出轮开始时占位行已在场"
    );

    let rows = read_rows(&store, "p_zero").await;
    assert!(
        rows.is_empty(),
        "真零产出轮后占位行已删（生命周期封闭于单轮；user 行由生产派发层落表，本夹具无派发层故表为空）"
    );
    assert!(
        rows.iter()
            .all(|r| r.seq_in_branch != start.sequence as u32),
        "保留槽位已无行"
    );
    assert!(
        rows.iter()
            .all(|r| r.status.as_deref() != Some("streaming")),
        "无 streaming 残留"
    );
}

// ── 契约 4：run 起手 sweep 收口前代 streaming 行 ────────────────

#[tokio::test]
async fn run_start_sweep_closes_stale_streaming_rows() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let dyn_store: Arc<dyn StorageBackend> = store.clone();
    // 预置前代（run_seed）残留：user0（正常）+ streaming 空行@1 + streaming
    // 有内容行@2；另管道 p_other 一条 streaming 空行（不得被本管道 sweep 波及）。
    dyn_store
        .apply_messages_ops_to_table(
            "p_sweep",
            "default",
            &[
                json!({
                    "op": "set", "seq": 0,
                    "msg": { "role": "user", "content": "hi" },
                    "_run_id": "run_seed",
                }),
                placeholder_blob("a_stale_empty", 1),
                {
                    let mut op = placeholder_blob("a_stale_partial", 2);
                    op["msg"]["content"] = json!("半截思考");
                    op["_run_id"] = json!("run_seed");
                    op
                },
            ],
        )
        .await
        .expect("seed stale rows");
    dyn_store
        .apply_messages_ops_to_table("p_other", "default", &[placeholder_blob("a_other", 0)])
        .await
        .expect("seed other pipeline row");

    let recorder = Arc::new(SlotRecorder::new(store.clone()));
    let executor = make_executor(
        Arc::new(RoundInvoker::silent()),
        store.clone(),
        recorder.clone(),
        "run_fresh",
    );
    let compiled = compile_pipeline(
        &single_step_config("sweep_fixture"),
        &Default::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");
    executor
        .run_compiled(&compiled, initial_state("p_sweep"))
        .await
        .expect("run ok");

    let rows = read_rows(&store, "p_sweep").await;
    // 空 streaming 残留@1：删行（本 run 自建占位也走 seq 1，零产出轮后同删——
    // 最终 seq 1 无行）。
    assert!(
        rows.iter().all(|r| r.seq_in_branch != 1),
        "前代空 streaming 行已删，且本轮自建占位行零产出后同收口"
    );
    // 有内容 streaming 残留@2：标 interrupted（内容保全，不删用户可见产出）。
    let partial = rows
        .iter()
        .find(|r| r.seq_in_branch == 2)
        .expect("有内容残留行仍在");
    assert_eq!(partial.status.as_deref(), Some("interrupted"));
    assert_eq!(partial.content_preview.as_deref(), Some("半截思考"));
    // 非 streaming 行（user@0）不动。
    assert_eq!(rows.len(), 2, "仅剩 user 与 interrupted 两行");
    let user_row = rows.iter().find(|r| r.seq_in_branch == 0).unwrap();
    assert_eq!(user_row.role, "user");

    // 异管道 streaming 行不受本管道 sweep 波及。
    let other = read_rows(&store, "p_other").await;
    assert_eq!(other.len(), 1, "异管道行保留");
    assert_eq!(other[0].status.as_deref(), Some("streaming"));
}

// ── 契约 5：入口建行失败回落出口 INSERT ─────────────────────────

/// 占位写面注入失败的包装 store：凡携带 status=streaming 消息的 op 一律失败
///（即入口建行失败），其余 op 委托真实 store——出口 INSERT（正常 assistant，
/// 无 status）不受影响。语义化注入对调用序不敏感，比首调计数更稳。
struct StreamingWriteFailStore {
    inner: SqliteStore,
    failed_attempts: AtomicUsize,
}

impl StreamingWriteFailStore {
    fn new(inner: SqliteStore) -> Self {
        Self {
            inner,
            failed_attempts: AtomicUsize::new(0),
        }
    }

    fn failure_count(&self) -> usize {
        self.failed_attempts.load(Ordering::SeqCst)
    }
}

#[async_trait]
impl StorageBackend for StreamingWriteFailStore {
    async fn apply_messages_ops_to_table(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        ops: &[serde_json::Value],
    ) -> Result<(), StorageError> {
        let hits_placeholder = ops.iter().any(|op| {
            op.get("msg")
                .and_then(|m| m.get("status"))
                .and_then(|v| v.as_str())
                == Some("streaming")
        });
        if hits_placeholder {
            self.failed_attempts.fetch_add(1, Ordering::SeqCst);
            return Err(StorageError::Database(
                "injected placeholder failure".into(),
            ));
        }
        StorageBackend::apply_messages_ops_to_table(&self.inner, pipeline_id, tenant_id, ops).await
    }

    async fn sweep_stale_streaming_slots(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        current_run_id: &str,
    ) -> Result<agentos_core::traits::StreamingSlotSweep, StorageError> {
        StorageBackend::sweep_stale_streaming_slots(
            &self.inner,
            pipeline_id,
            tenant_id,
            current_run_id,
        )
        .await
    }

    async fn delete_streaming_placeholder(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        seq: i64,
        message_id: &str,
    ) -> Result<bool, StorageError> {
        StorageBackend::delete_streaming_placeholder(
            &self.inner,
            pipeline_id,
            tenant_id,
            seq,
            message_id,
        )
        .await
    }

    async fn get_run(&self, run_id: &str) -> Result<agentos_core::types::RunRecord, StorageError> {
        StorageBackend::get_run(&self.inner, run_id).await
    }

    async fn get_messages_by_pipeline(
        &self,
        pipeline_id: &str,
        opts: MessageQueryOpts,
    ) -> Result<Vec<MessageRecord>, StorageError> {
        StorageBackend::get_messages_by_pipeline(&self.inner, pipeline_id, opts).await
    }

    async fn get_blob(&self, blob_id: &str) -> Result<Vec<u8>, StorageError> {
        StorageBackend::get_blob(&self.inner, blob_id).await
    }

    async fn append_trace(
        &self,
        entry: agentos_core::types::TraceEntry,
    ) -> Result<(), StorageError> {
        StorageBackend::append_trace(&self.inner, entry).await
    }

    async fn store_blob(&self, data: &[u8], mime_type: &str) -> Result<String, StorageError> {
        StorageBackend::store_blob(&self.inner, data, mime_type).await
    }

    async fn create_session(
        &self,
        session: &agentos_core::types::SessionRecord,
    ) -> Result<(), StorageError> {
        StorageBackend::create_session(&self.inner, session).await
    }

    async fn get_session(
        &self,
        thread_id: &str,
    ) -> Result<Option<agentos_core::types::SessionRecord>, StorageError> {
        StorageBackend::get_session(&self.inner, thread_id).await
    }

    async fn list_sessions(
        &self,
        filter: SessionListFilter,
    ) -> Result<Vec<agentos_core::types::SessionRecord>, StorageError> {
        StorageBackend::list_sessions(&self.inner, filter).await
    }

    async fn update_session(
        &self,
        session: &agentos_core::types::SessionRecord,
    ) -> Result<(), StorageError> {
        StorageBackend::update_session(&self.inner, session).await
    }

    async fn delete_session(&self, thread_id: &str) -> Result<Vec<String>, StorageError> {
        StorageBackend::delete_session(&self.inner, thread_id).await
    }

    async fn link_pipeline_session(
        &self,
        pipeline_id: &str,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        StorageBackend::link_pipeline_session(&self.inner, pipeline_id, thread_id, tenant_id).await
    }

    async fn list_pipeline_ids_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<String>, StorageError> {
        StorageBackend::list_pipeline_ids_by_thread(&self.inner, thread_id, tenant_id).await
    }

    async fn get_step_traces_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<agentos_core::types::TraceEntry>, StorageError> {
        StorageBackend::get_step_traces_by_thread(&self.inner, thread_id, tenant_id).await
    }

    async fn create_user(
        &self,
        user: &agentos_core::types::UserRecord,
    ) -> Result<(), StorageError> {
        StorageBackend::create_user(&self.inner, user).await
    }

    async fn get_user_by_id(
        &self,
        user_id: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, StorageError> {
        StorageBackend::get_user_by_id(&self.inner, user_id).await
    }

    async fn get_user_by_username(
        &self,
        username: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, StorageError> {
        StorageBackend::get_user_by_username(&self.inner, username).await
    }

    async fn list_users(&self) -> Result<Vec<agentos_core::types::UserRecord>, StorageError> {
        StorageBackend::list_users(&self.inner).await
    }

    async fn update_last_login(&self, user_id: &str) -> Result<(), StorageError> {
        StorageBackend::update_last_login(&self.inner, user_id).await
    }

    async fn update_user_password(
        &self,
        user_id: &str,
        password_hash: &str,
        must_change_password: bool,
    ) -> Result<bool, StorageError> {
        StorageBackend::update_user_password(
            &self.inner,
            user_id,
            password_hash,
            must_change_password,
        )
        .await
    }

    async fn delete_user(&self, user_id: &str) -> Result<bool, StorageError> {
        StorageBackend::delete_user(&self.inner, user_id).await
    }
}

#[tokio::test]
async fn placeholder_create_failure_falls_back_to_exit_insert() {
    let store = Arc::new(StreamingWriteFailStore::new(
        SqliteStore::open_memory().unwrap(),
    ));
    let dyn_store: Arc<dyn StorageBackend> = store.clone();
    let recorder = {
        // SlotRecorder 只收 SqliteStore；此处直接经 dyn_store 快照不可行——
        // 简化为不挂观察点（降级路径行为断言不依赖事件）。
        struct NoopRecorder;
        impl RoundEvents for NoopRecorder {
            fn on_round_start(
                &self,
                _ev: RoundStart,
            ) -> Pin<Box<dyn Future<Output = ()> + Send + '_>> {
                Box::pin(async {})
            }
            fn on_round_end(&self, _ev: RoundEnd) -> Pin<Box<dyn Future<Output = ()> + Send + '_>> {
                Box::pin(async {})
            }
        }
        Arc::new(NoopRecorder)
    };
    let invoker = Arc::new(RoundInvoker::new());
    let executor = PipelineExecutor::new(
        invoker as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        agentos_core::types::TenantContext::new("default", "session_slot"),
        vec![
            "pipeline_llm_core".to_string(),
            "pipeline_tool_core".to_string(),
        ],
        dyn_store.clone(),
        "run_slot_fallback",
    )
    .with_round_events(recorder);
    let compiled = compile_pipeline(
        &two_round_config("slot_fallback"),
        &Default::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");

    // 入口建行失败不阻断轮次：run 照常完成。
    let final_state = executor
        .run_compiled(&compiled, initial_state("p_fallback"))
        .await
        .expect("入口建行失败不得阻断轮次");
    assert_eq!(
        store.failure_count(),
        2,
        "两轮各一次入口建行失败注入确实发生（非路径未触发假绿）"
    );

    // 回落既有出口 INSERT 行为：assistant 正常落库（等价旧世界），无 streaming 残留。
    let rows = {
        let rows = dyn_store
            .get_messages_by_pipeline("p_fallback", MessageQueryOpts::default())
            .await
            .expect("read rows");
        rows
    };
    let mut seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    seqs.sort();
    assert_eq!(
        seqs,
        vec![1, 2, 3, 4],
        "回落世界行形态与旧世界一致（无双行）"
    );
    let slot1 = rows.iter().find(|r| r.seq_in_branch == 1).unwrap();
    assert_eq!(slot1.content_preview.as_deref(), Some("第一步回复"));
    assert_ne!(slot1.status.as_deref(), Some("streaming"));
    let slot3 = rows.iter().find(|r| r.seq_in_branch == 3).unwrap();
    assert_eq!(slot3.content_preview.as_deref(), Some("第二步回复"));
    let _ = final_state; // run 正常返回即契约的一半
}
