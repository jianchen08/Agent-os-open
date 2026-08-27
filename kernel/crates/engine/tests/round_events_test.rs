// @feature: 聊天流式顺序（DSH 形态：每轮一条消息） | @ci: rust-test
//! 轮次观察点契约测试：循环体一次迭代 = 一轮消息。
//!
//! 验证（TDD 规格）：
//! 1. test_round_events_fire_per_iteration —— 两轮循环各发 start/end 一次、顺序
//!    严格 start→end；每轮独立 message_id（a_ 前缀）；end 携带本轮 assistant
//!    完整持久形态（含 tool_calls）；首轮 end 附 user_message，次轮不附。
//! 2. test_round_record_id_matches_event_id_and_table_order —— 每轮 assistant
//!    落库 record_id == 该轮事件 message_id（流式占位与 DB 重载逐轮同构）；
//!    message_slots 按 seq 升序对位轮次事件顺序（后端顺序不变式）。

use std::collections::HashMap;
use std::future::Future;
use std::path::Path;
use std::pin::Pin;
use std::sync::{Arc, Mutex};

use agentos_core::traits::{MessageQueryOpts, PluginInvoker, StorageBackend};
use agentos_core::types::{
    LoopBody, PipelineConfig, PipelineStep, PluginContext, PluginError,
    PluginResult, Route, RouteAction, RouteNext, StepItem, ToolExecutionResult,
};
use agentos_engine::compiler::compile_pipeline;
use agentos_engine::{PipelineExecutor, RoundEnd, RoundEvents, RoundStart, SqliteStore};
use async_trait::async_trait;
use serde_json::json;

enum Event {
    Start(RoundStart),
    End(RoundEnd),
}

struct Recorder {
    events: Mutex<Vec<Event>>,
}

impl Recorder {
    fn new() -> Self {
        Self {
            events: Mutex::new(Vec::new()),
        }
    }

    fn starts(&self) -> Vec<RoundStart> {
        self.events
            .lock()
            .unwrap()
            .iter()
            .filter_map(|e| match e {
                Event::Start(s) => Some(s.clone()),
                _ => None,
            })
            .collect()
    }

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

impl RoundEvents for Recorder {
    fn on_round_start(&self, ev: RoundStart) -> Pin<Box<dyn Future<Output = ()> + Send + '_>> {
        let events = &self.events;
        Box::pin(async move {
            events.lock().unwrap().push(Event::Start(ev));
        })
    }

    fn on_round_end(&self, ev: RoundEnd) -> Pin<Box<dyn Future<Output = ()> + Send + '_>> {
        let events = &self.events;
        Box::pin(async move {
            events.lock().unwrap().push(Event::End(ev));
        })
    }
}

/// 按调用序返回不同结果的 invoker：llm_core 第 1 次调用产出带工具调用的回复，
/// 第 2 次起产出纯文本回复；tool_core 返回工具结果消息。
struct RoundInvoker {
    calls: Mutex<Vec<String>>,
}

impl RoundInvoker {
    fn new() -> Self {
        Self {
            calls: Mutex::new(Vec::new()),
        }
    }
}

#[async_trait]
impl PluginInvoker for RoundInvoker {
    async fn invoke_pipeline_plugin(
        &self,
        plugin_id: &str,
        _ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError> {
        self.calls.lock().unwrap().push(plugin_id.to_string());
        let count = self
            .calls
            .lock()
            .unwrap()
            .iter()
            .filter(|p| p.as_str() == plugin_id)
            .count();
        match plugin_id {
            "pipeline_llm_core" => {
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

fn two_round_config() -> PipelineConfig {
    PipelineConfig {
        name: "round_events_test".into(),
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
    }
}

fn make_executor(invoker: Arc<RoundInvoker>, store: Arc<SqliteStore>, recorder: Arc<Recorder>) -> PipelineExecutor {
    let store_dyn: Arc<dyn StorageBackend> = store;
    PipelineExecutor::new(
        invoker as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        agentos_core::types::TenantContext::new("tenant_test", "session_test"),
        vec!["pipeline_llm_core".to_string(), "pipeline_tool_core".to_string()],
        store_dyn,
        "run_round_events",
        "main",
    )
    .with_round_events(recorder)
}

fn initial_state() -> serde_json::Value {
    json!({
        "pipeline_id": "p_rounds",
        "message": "hi",
        "agent_id": "agentos",
        "core_plugin": "pipeline_llm_core",
        "core_type": "llm_call",
        "ended": false,
        "suspended": false,
        "session_id": "thread-abc",
        "messages": [{ "role": "user", "content": "hi", "seq": 0 }],
    })
}

#[tokio::test]
async fn test_round_events_fire_per_iteration() {
    let _tmp = tempfile::tempdir().unwrap();
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let recorder = Arc::new(Recorder::new());
    let executor = make_executor(Arc::new(RoundInvoker::new()), store, recorder.clone());
    let compiled = compile_pipeline(
        &two_round_config(),
        &Default::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");
    let final_state = executor.run_compiled(&compiled, initial_state()).await.expect("run ok");

    let starts = recorder.starts();
    let ends = recorder.ends();
    assert_eq!(starts.len(), 2, "两轮循环应发两次 round_start");
    assert_eq!(ends.len(), 2, "两轮循环应发两次 round_end");
    // 顺序严格 start → end → start → end（发射顺序 = 轮次顺序）
    assert!(matches!(&recorder.events.lock().unwrap()[..], [Event::Start(_), Event::End(_), Event::Start(_), Event::End(_)]));
    // 每轮独立 message_id（a_ 前缀、互不相同）
    assert_ne!(starts[0].message_id, starts[1].message_id);
    assert!(starts[0].message_id.starts_with("a_"));
    assert!(starts[1].message_id.starts_with("a_"));
    // end 与 start 的 message_id 一一对应
    assert_eq!(ends[0].message_id, starts[0].message_id);
    assert_eq!(ends[1].message_id, starts[1].message_id);
    // 首轮 end 携带本轮 assistant（含 tool_calls 字段），次轮为纯文本
    let round1 = ends[0].assistant.as_ref().expect("round1 assistant");
    assert_eq!(round1.get("content").and_then(|v| v.as_str()), Some("第一步回复"));
    assert!(round1.get("tool_calls").is_some());
    // 第二轮回复不应再携带首轮工具调用
    let round2 = ends[1].assistant.as_ref().expect("round2 assistant");
    assert_eq!(round2.get("content").and_then(|v| v.as_str()), Some("第二步回复"));
    assert!(round2.get("tool_calls").is_none());
    // 引擎每轮都附 user 消息（供 api 桥接在首个有产出的轮次做认领回传）
    assert!(ends[0].user_message.is_some());
    assert!(ends[1].user_message.is_some());
    // 末轮 message_id 留在 state（tool/llm 后发事件均按寻址）
    assert_eq!(
        final_state.get("message_id").and_then(|v| v.as_str()),
        Some(starts[1].message_id.as_str())
    );
}

#[tokio::test]
async fn test_tool_iteration_reuses_open_round() {
    // DSL 交替形态回归（用户复现的「尾部整段重复工具卡」根因锚）：
    // LLM 迭代 → 工具迭代（core_plugin=pipeline_tool_core）→ LLM 迭代。
    // 断言：只有 2 个轮次（LLM 回合），工具迭代不新开轮——工具事件挂打开轮
    // 消息（其 id 与 LLM 轮 new_message 的 toolCalls 卡片同键，不重复建卡）。
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let recorder = Arc::new(Recorder::new());
    // invoker 按调用序：llm#1 带工具，tool#1 出工具结果，llm#2 纯文本
    struct AltInvoker {
        calls: Mutex<Vec<String>>,
    }
    impl AltInvoker {
        fn new() -> Self {
            Self { calls: Mutex::new(Vec::new()) }
        }
    }
    #[async_trait]
    impl PluginInvoker for AltInvoker {
        async fn invoke_pipeline_plugin(
            &self,
            plugin_id: &str,
            _ctx: &PluginContext,
        ) -> Result<PluginResult, PluginError> {
            self.calls.lock().unwrap().push(plugin_id.to_string());
            let count = self
                .calls
                .lock()
                .unwrap()
                .iter()
                .filter(|p| p.as_str() == plugin_id)
                .count();
            match plugin_id {
                "pipeline_llm_core" => {
                    if count == 1 {
                        Ok(PluginResult {
                            state_updates: HashMap::from([
                                ("messages".to_string(), json!({
                                    "_ops": [{
                                        "op": "set",
                                        "msg": {
                                            "role": "assistant",
                                            "content": "第一轮回复",
                                            "tool_calls": [
                                                { "id": "call_alt_1", "name": "file_read", "arguments": "{}" }
                                            ],
                                        },
                                    }]
                                })),
                                ("raw_result".to_string(), json!("第一轮回复")),
                                ("raw_tool_calls".to_string(), json!([{ "type": "function", "id": "call_alt_1", "name": "file_read", "arguments": "{}" }])),
                            ]),
                            ..Default::default()
                        })
                    } else {
                        Ok(PluginResult {
                            state_updates: HashMap::from([
                                ("messages".to_string(), json!({
                                    "_ops": [{ "op": "set", "msg": { "role": "assistant", "content": "第二轮回复" } }]
                                })),
                                ("raw_result".to_string(), json!("第二轮回复")),
                                ("raw_tool_calls".to_string(), json!([])),
                            ]),
                            ..Default::default()
                        })
                    }
                }
                "pipeline_tool_core" => Ok(PluginResult {
                    state_updates: HashMap::from([
                        ("messages".to_string(), json!({
                            "_ops": [{ "op": "set", "msg": { "role": "tool", "content": "工具结果", "tool_call_id": "call_alt_1" } }]
                        })),
                        ("raw_result".to_string(), json!("工具结果")),
                        ("raw_tool_calls".to_string(), json!([])),
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

    let invoker = Arc::new(AltInvoker::new());
    let store_dyn: Arc<dyn StorageBackend> = store;
    let executor = PipelineExecutor::new(
        invoker as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        agentos_core::types::TenantContext::new("tenant_test", "session_test"),
        vec!["pipeline_llm_core".to_string(), "pipeline_tool_core".to_string()],
        store_dyn,
        "run_alt_rounds",
        "main",
    )
    .with_round_events(recorder.clone());

    let config = PipelineConfig {
        name: "round_alt".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "core".into(),
                steps: vec![StepItem::Bare("{{state.core_plugin}}".into())],
                when: None,
                context: HashMap::new(),
                routes: vec![
                    Route {
                        when: "raw_tool_calls != [] and raw_tool_calls != None".into(),
                        then: RouteAction {
                            next: RouteNext::Loop,
                            set: HashMap::from([("core_plugin".to_string(), json!("pipeline_tool_core"))]),
                        },
                    },
                    Route {
                        when: "raw_tool_calls == [] and core_plugin == \"pipeline_tool_core\"".into(),
                        then: RouteAction {
                            next: RouteNext::Loop,
                            set: HashMap::from([("core_plugin".to_string(), json!("pipeline_llm_core"))]),
                        },
                    },
                    Route {
                        when: "True".into(),
                        then: RouteAction { next: RouteNext::End, set: HashMap::new() },
                    },
                ],
                loop_config: None,
            }],
            while_cond: Some("True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: agentos_core::types::CheckpointConfig::default(),
    };
    let compiled = compile_pipeline(&config, &Default::default(), executor.plugin_ids()).expect("compile ok");

    let final_state = executor
        .run_compiled(
            &compiled,
            json!({
                "pipeline_id": "p_alt_rounds",
                "message": "hi",
                "core_plugin": "pipeline_llm_core",
                "core_type": "llm_call",
                "ended": false,
                "suspended": false,
                "session_id": "thread-alt",
                "messages": [{ "role": "user", "content": "hi", "seq": 0 }],
            }),
        )
        .await
        .expect("run ok");

    let starts = recorder.starts();
    let ends = recorder.ends();
    assert_eq!(starts.len(), 2, "LLM/工具/LLM 三次迭代应只开 2 个轮次");
    assert_eq!(ends.len(), 2);
    assert_eq!(starts[0].message_id, ends[0].message_id);
    assert_eq!(starts[1].message_id, ends[1].message_id);
    assert_ne!(starts[0].message_id, starts[1].message_id);
    // 第 1 轮 assistant = 带工具的回复，第 2 轮 = 纯文本终条
    assert!(ends[0].assistant.as_ref().unwrap().get("tool_calls").is_some());
    assert_eq!(
        ends[1].assistant.as_ref().unwrap().get("content").and_then(|v| v.as_str()),
        Some("第二轮回复")
    );
    // 最终 state 的 message_id = 第 2 轮 id（工具迭代沿用打开轮 id，未覆盖）
    assert_eq!(
        final_state.get("message_id").and_then(|v| v.as_str()),
        Some(starts[1].message_id.as_str())
    );
}

#[tokio::test]
async fn test_round_record_id_matches_event_id_and_table_order() {
    let _tmp = tempfile::tempdir().unwrap();
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let recorder = Arc::new(Recorder::new());
    let executor = make_executor(Arc::new(RoundInvoker::new()), store.clone(), recorder.clone());
    let compiled = compile_pipeline(
        &two_round_config(),
        &Default::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");
    executor.run_compiled(&compiled, initial_state()).await.expect("run ok");

    let starts = recorder.starts();
    let rows = store
        .get_slot_messages_by_pipeline("p_rounds", "tenant_test", MessageQueryOpts::default())
        .expect("读表应成功");

    // 升序 seq 是后端顺序不变式
    let mut seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    let orig = seqs.clone();
    seqs.sort();
    assert_eq!(seqs, orig, "message_slots 应恒按 seq 升序");
    // 角色序列 = assistant(轮1), tool(轮1), assistant(轮2), tool(轮2) —— 按 seq
    // 升序与轮次事件顺序逐轮对位（初始 user 消息仅存 state，不落表行）
    let roles: Vec<&str> = rows.iter().map(|r| r.role.as_str()).collect();
    assert_eq!(roles, vec!["assistant", "tool", "assistant", "tool"]);
    // 每轮 assistant 落库 record_id == 该轮事件 message_id（流式占位与 DB 重载同键）
    let assistant_ids: Vec<&str> = rows
        .iter()
        .filter(|r| r.role == "assistant")
        .map(|r| r.message_id.as_str())
        .collect();
    assert_eq!(assistant_ids, vec![starts[0].message_id.as_str(), starts[1].message_id.as_str()]);
}
