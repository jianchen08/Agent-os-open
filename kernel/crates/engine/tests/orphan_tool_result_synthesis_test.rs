// @feature: FP-0.2.〇 管道引擎 | @ci: rust-test
//! 孤儿工具卡根修契约测试：interrupted/error 终局为在途 tool_calls 合成终态
//! tool 结果消息（恢复 tool_calls/tool 一一配对）。
//!
//! 缺陷（装机库 382ec1ee323e 实证）：run 被 stop/用户抢占/错误终局时，
//! assistant 已发起但尚无 role=tool 结果消息的 tool_calls 永远等不到结果——
//! 历史留下孤儿 tool_calls，前端渲染永动计时等待卡。契约：
//! - Cancelled/Failed 终局：落库历史 tool_calls/tool 一一配对，合成结果
//!   content 以稳定标记 `[interrupted]` 开头，envelope success=false；
//! - Completed 不合成（正常路径工具都已返回）；
//! - Suspended 不合成（human_interaction 等待恢复期间在途合法）。
//!
//! 同范式：LangGraph interrupt 恢复合成 ToolMessage("rejected by user")；
//! LibreChat v0.8.8 #15517 Forward Run Abort Signal to Foreground Tool Calls。

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::{Arc, Mutex};

use agentos_core::traits::{MessageQueryOpts, PluginInvoker};
use agentos_core::types::{
    LoopBody, MessageRecord, PipelineConfig, PipelineStep, PluginContext, PluginError,
    PluginResult, Route, RouteAction, RouteNext, StepLibrary, ToolExecutionResult,
};
use agentos_engine::compiler::compile_pipeline;
use agentos_engine::{PipelineExecutor, SqliteStore};
use async_trait::async_trait;
use serde_json::{json, Value};

const PID: &str = "p-orphan-synth";
const MARKER: &str = "[interrupted]";

/// 可编程 MockInvoker：按 plugin_id 返回预设结果（外部依赖 mock；落库走
/// 真实 SqliteStore 内存实例——禁 mock 内部落库真依赖）。
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
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        _ctx: &PluginContext<'a>,
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

/// 单步管道配置：core 动态插件 + 路由（工具调用 → tool_execute 循环；
/// 其余 → End）。`with_tool_route=false` 时只留 End 路由（构造
/// dropped_tool_calls 型 error 终局）。
fn make_config(with_tool_route: bool) -> PipelineConfig {
    let mut routes = Vec::new();
    if with_tool_route {
        routes.push(Route {
            when: "raw_tool_calls != [] and raw_tool_calls != None".into(),
            then: RouteAction {
                next: RouteNext::Loop,
                set: HashMap::from([
                    ("core_type".to_string(), json!("tool_execute")),
                    ("core_plugin".to_string(), json!("pipeline_tool_core")),
                ]),
            },
        });
    }
    routes.push(Route {
        when: "True".into(),
        then: RouteAction {
            next: RouteNext::End,
            set: HashMap::new(),
        },
    });
    PipelineConfig {
        name: "orphan_synth".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "core".into(),
                steps: vec!["{{state.core_plugin}}".into()],
                when: None,
                context: HashMap::new(),
                routes,
                loop_config: None,
            }],
            while_cond: Some("True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: HashMap::new(),
        max_rounds: None,
    }
}

/// executor + 真实内存 SQLite（断言读与落库同一真值）。
fn make_executor(invoker: Arc<MockInvoker>, store: Arc<SqliteStore>) -> PipelineExecutor {
    let plugin_ids = ["pipeline_llm_core", "pipeline_tool_core"]
        .iter()
        .map(|s| s.to_string());
    let store_dyn: Arc<dyn agentos_core::traits::StorageBackend> = store;
    PipelineExecutor::new(
        invoker as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        agentos_core::types::TenantContext::new("default", "session_orphan"),
        plugin_ids,
        store_dyn,
        "run_orphan",
    )
}

fn initial_state() -> Value {
    json!({
        "pipeline_id": PID,
        "message": "帮我查一下",
        "agent_id": "agentos",
        "core_type": "llm_call",
        "core_plugin": "pipeline_llm_core",
        "ended": false,
        "suspended": false,
    })
}

/// assistant tool_calls 消息（OpenAI 规范形状，对齐 tool_core 补造路径）。
fn assistant_tool_call_msg(call_id: &str, tool_name: &str) -> Value {
    json!({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": { "name": tool_name, "arguments": "{}" },
        }],
    })
}

/// llm_core 结果：发 assistant tool_calls 消息 op + 非空 raw_tool_calls
/// （真实 llm_core 同款双发射：消息进历史、语义键驱动路由）。
fn llm_core_issues_call(call_id: &str, tool_name: &str) -> PluginResult {
    PluginResult {
        state_updates: HashMap::from([
            (
                "messages".to_string(),
                json!({ "_ops": [
                    { "op": "set", "msg": assistant_tool_call_msg(call_id, tool_name) },
                ]}),
            ),
            (
                "raw_tool_calls".to_string(),
                json!([{ "id": call_id, "name": tool_name, "arguments": {} }]),
            ),
        ]),
        ..Default::default()
    }
}

/// 读全部落库消息（默认租户，与 executor 侧 TenantContext 同源）。
fn read_messages(store: &SqliteStore) -> Vec<MessageRecord> {
    store
        .get_slot_messages_by_pipeline(PID, "default", MessageQueryOpts::default())
        .expect("读 message_slots 应成功")
}

/// 断言落库历史 tool_calls/tool 一一配对，返回 call_id → 该 id 的 tool 行。
fn assert_paired_and_map_tool_rows(
    messages: &[MessageRecord],
) -> HashMap<String, Vec<&MessageRecord>> {
    let mut issued: Vec<String> = Vec::new();
    let mut by_call: HashMap<String, Vec<&MessageRecord>> = HashMap::new();
    for m in messages {
        if m.role == "assistant" {
            if let Some(tc) = m.tool_calls_json.as_deref() {
                let v: Value = serde_json::from_str(tc).expect("tool_calls_json 合法 JSON");
                for call in v.as_array().expect("tool_calls 是数组") {
                    let id = call["id"].as_str().expect("tool_call id").to_string();
                    issued.push(id);
                }
            }
        }
        if m.role == "tool" {
            let id = m.tool_call_id.clone().expect("tool 消息必带 tool_call_id");
            by_call.entry(id).or_default().push(m);
        }
    }
    let mut unique = issued.clone();
    unique.sort();
    unique.dedup();
    assert_eq!(
        issued.len(),
        unique.len(),
        "前置：本测试每场景只发一个 tool_call"
    );
    for id in &issued {
        assert!(
            by_call.contains_key(id),
            "落库历史必须 tool_calls/tool 一一配对：{id} 无结果消息"
        );
    }
    by_call
}

/// 场景一（用户 stop 中断 → Cancelled 终局）：工具结果消息被中断吞掉
/// （tool_core 只署名 user_requested、不发结果 op——装机实证缺陷形态），
/// 引擎须合成带稳定标记的终态结果消息恢复配对。
#[tokio::test]
async fn cancelled_terminal_synthesizes_orphan_tool_result() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result(
        "pipeline_llm_core",
        llm_core_issues_call("call_stop_1", "human_interaction"),
    );
    // 工具被中断：stop 署名收尾，结果消息从未落库（缺陷形态）
    invoker.set_result(
        "pipeline_tool_core",
        PluginResult {
            state_updates: HashMap::from([
                ("ended".to_string(), json!(true)),
                ("router.stop_reason".to_string(), json!("user_requested")),
                ("raw_tool_calls".to_string(), json!([])),
            ]),
            ..Default::default()
        },
    );
    let executor = make_executor(Arc::clone(&invoker), Arc::clone(&store));
    let compiled = compile_pipeline(
        &make_config(true),
        &StepLibrary::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");

    let final_state = executor
        .run_compiled(&compiled, initial_state())
        .await
        .expect("run 应成功收尾");

    assert_eq!(
        final_state["run_status"], "cancelled",
        "user_requested 署名 → Cancelled 终局"
    );

    let messages = read_messages(&store);
    let by_call = assert_paired_and_map_tool_rows(&messages);
    let rows = &by_call["call_stop_1"];
    assert_eq!(rows.len(), 1, "合成结果消息恰好一条，禁重复");
    let synth = rows[0];
    assert!(
        synth
            .content_preview
            .as_deref()
            .unwrap_or_default()
            .starts_with(MARKER),
        "合成 content 必须以稳定标记开头：{:?}",
        synth.content_preview
    );
    assert_eq!(synth.status.as_deref(), Some("failed"), "中断=失败终态");
    let envelope: Value =
        serde_json::from_str(synth.tool_result_json.as_deref().unwrap()).expect("envelope 落库");
    assert_eq!(envelope["success"], false);
    assert_eq!(envelope["call_id"], "call_stop_1");
    assert_eq!(envelope["tool_name"], "human_interaction");
    // 内存 state 与表同构（一次 apply 两落点）
    let state_msgs = final_state["messages"].as_array().unwrap();
    assert!(
        state_msgs.iter().any(|m| {
            m["role"] == "tool"
                && m["tool_call_id"] == "call_stop_1"
                && m["content"]
                    .as_str()
                    .unwrap_or_default()
                    .starts_with(MARKER)
        }),
        "内存 messages 亦含合成结果（内存/表一致）"
    );
}

/// 场景二（error 终局 → Failed）：无工具回环路由，已发起调用随 run 异常
/// 终局被吞（dropped_tool_calls 署名 Failed），合成结果恢复配对。
#[tokio::test]
async fn failed_terminal_synthesizes_orphan_tool_result() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result(
        "pipeline_llm_core",
        llm_core_issues_call("call_err_1", "web_search"),
    );
    let executor = make_executor(Arc::clone(&invoker), Arc::clone(&store));
    let compiled = compile_pipeline(
        &make_config(false),
        &StepLibrary::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");

    let final_state = executor
        .run_compiled(&compiled, initial_state())
        .await
        .expect("run 应成功收尾");

    assert_eq!(
        final_state["run_status"], "failed",
        "未消费工具调用 + 无署名 → dropped_tool_calls 折算 Failed"
    );

    let messages = read_messages(&store);
    let by_call = assert_paired_and_map_tool_rows(&messages);
    let rows = &by_call["call_err_1"];
    assert_eq!(rows.len(), 1);
    assert!(
        rows[0]
            .content_preview
            .as_deref()
            .unwrap_or_default()
            .starts_with(MARKER),
        "合成 content 以稳定标记开头：{:?}",
        rows[0].content_preview
    );
    assert_eq!(rows[0].status.as_deref(), Some("failed"));
}

/// 对照（completed 终局）：正常配对路径零合成——真实结果消息原样在场，
/// 无标记消息、无重复行。
#[tokio::test]
async fn completed_terminal_does_not_synthesize() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result(
        "pipeline_llm_core",
        llm_core_issues_call("call_ok_1", "file_read"),
    );
    // 工具正常返回：真实结果消息 + 纯文本 → End
    invoker.set_result(
        "pipeline_tool_core",
        PluginResult {
            state_updates: HashMap::from([
                (
                    "messages".to_string(),
                    json!({ "_ops": [
                        { "op": "set", "msg": {
                            "role": "tool",
                            "tool_call_id": "call_ok_1",
                            "content": "fn main() {}",
                            "tool_result": {
                                "call_id": "call_ok_1",
                                "tool_name": "file_read",
                                "success": true,
                                "error": null,
                                "data": { "output": "fn main() {}" },
                                "metadata": null,
                                "duration_ms": 12.5,
                            },
                        }},
                    ]}),
                ),
                ("raw_result".to_string(), json!("文件读完了")),
                ("raw_tool_calls".to_string(), json!([])),
            ]),
            ..Default::default()
        },
    );
    let executor = make_executor(Arc::clone(&invoker), Arc::clone(&store));
    let compiled = compile_pipeline(
        &make_config(true),
        &StepLibrary::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");

    let final_state = executor
        .run_compiled(&compiled, initial_state())
        .await
        .expect("run 应成功收尾");
    assert_eq!(final_state["run_status"], "completed");

    let messages = read_messages(&store);
    let by_call = assert_paired_and_map_tool_rows(&messages);
    let rows = &by_call["call_ok_1"];
    assert_eq!(rows.len(), 1, "只有真实结果消息一条");
    assert_eq!(rows[0].content_preview.as_deref(), Some("fn main() {}"));
    assert_eq!(rows[0].status.as_deref(), Some("completed"));
    assert!(
        !messages.iter().any(|m| m
            .content_preview
            .as_deref()
            .unwrap_or_default()
            .starts_with(MARKER)),
        "completed 终局禁止合成标记消息"
    );
}

/// 对照（suspended 终局）：human_interaction 挂起等待恢复期间在途合法，
/// 禁合成（合成即伪造用户侧答案）。
#[tokio::test]
async fn suspended_terminal_does_not_synthesize() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result(
        "pipeline_llm_core",
        llm_core_issues_call("call_susp_1", "human_interaction"),
    );
    invoker.set_result(
        "pipeline_tool_core",
        PluginResult {
            state_updates: HashMap::from([
                ("suspended".to_string(), json!(true)),
                ("raw_tool_calls".to_string(), json!([])),
            ]),
            ..Default::default()
        },
    );
    let executor = make_executor(Arc::clone(&invoker), Arc::clone(&store));
    let compiled = compile_pipeline(
        &make_config(true),
        &StepLibrary::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");

    let final_state = executor
        .run_compiled(&compiled, initial_state())
        .await
        .expect("run 应挂起收尾");
    assert_eq!(final_state["run_status"], "suspended");

    let messages = read_messages(&store);
    assert!(
        !messages
            .iter()
            .any(|m| m.role == "tool" && m.tool_call_id.as_deref() == Some("call_susp_1")),
        "挂起期间在途调用必须保持无结果（等待恢复），禁合成"
    );
}

/// 场景三（引擎 Err 终局）：循环体推进 Err（此处用轮数上限超限构造）——
/// run 以 Err 上抛、api 层标 failed，合成仍须发生（error 终局同刀覆盖）。
#[tokio::test]
async fn engine_error_terminal_synthesizes_orphan_tool_result() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let invoker = Arc::new(MockInvoker::new());
    // 恒定发新工具调用 + 恒不署名 → 每轮新孤儿，直至轮数上限 fail-closed
    invoker.set_result(
        "pipeline_llm_core",
        llm_core_issues_call("call_loop_1", "forever_tool"),
    );
    let executor = make_executor(Arc::clone(&invoker), Arc::clone(&store));
    let compiled = compile_pipeline(
        &make_config(true),
        &StepLibrary::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");

    let result = executor
        .with_max_rounds(3)
        .run_compiled(&compiled, initial_state())
        .await;

    assert!(result.is_err(), "轮数上限 fail-closed 必 Err 上抛");

    let messages = read_messages(&store);
    let by_call = assert_paired_and_map_tool_rows(&messages);
    let rows = &by_call["call_loop_1"];
    assert_eq!(rows.len(), 1);
    assert!(
        rows[0]
            .content_preview
            .as_deref()
            .unwrap_or_default()
            .starts_with(MARKER),
        "Err 终局合成 content 以稳定标记开头：{:?}",
        rows[0].content_preview
    );
    assert_eq!(rows[0].status.as_deref(), Some("failed"));
}

/// 幂等对照：同一管道连续两 run，首 run 已治愈的孤儿不重复合成；
/// 全历史配对在次 run 终局后保持。
#[tokio::test]
async fn synthesis_is_not_repeated_for_healed_orphans() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result(
        "pipeline_llm_core",
        llm_core_issues_call("call_heal_1", "human_interaction"),
    );
    invoker.set_result(
        "pipeline_tool_core",
        PluginResult {
            state_updates: HashMap::from([
                ("ended".to_string(), json!(true)),
                ("router.stop_reason".to_string(), json!("user_requested")),
                ("raw_tool_calls".to_string(), json!([])),
            ]),
            ..Default::default()
        },
    );
    let executor = make_executor(Arc::clone(&invoker), Arc::clone(&store));
    let compiled = compile_pipeline(
        &make_config(true),
        &StepLibrary::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");

    // 首 run：中断 → 合成一次
    executor
        .run_compiled(&compiled, initial_state())
        .await
        .expect("首 run 收尾");
    let first = read_messages(&store);
    let first_count = first
        .iter()
        .filter(|m| m.role == "tool" && m.tool_call_id.as_deref() == Some("call_heal_1"))
        .count();
    assert_eq!(first_count, 1, "首 run 合成恰好一条");

    // 次 run：llm_core 不再发调用（纯文本收尾）——历史孤儿已治愈，禁再合成
    invoker.set_result(
        "pipeline_llm_core",
        PluginResult {
            state_updates: HashMap::from([
                ("raw_result".to_string(), json!("好的")),
                ("raw_tool_calls".to_string(), json!([])),
            ]),
            ..Default::default()
        },
    );
    let state2 = json!({
        "pipeline_id": PID,
        "message": "继续",
        "agent_id": "agentos",
        "core_type": "llm_call",
        "core_plugin": "pipeline_llm_core",
        "ended": false,
        "suspended": false,
    });
    executor
        .run_compiled(&compiled, state2)
        .await
        .expect("次 run 收尾");
    let second = read_messages(&store);
    let second_count = second
        .iter()
        .filter(|m| m.role == "tool" && m.tool_call_id.as_deref() == Some("call_heal_1"))
        .count();
    assert_eq!(second_count, 1, "治愈后禁重复合成");

    let issued: HashSet<String> = second
        .iter()
        .filter(|m| m.role == "assistant")
        .filter_map(|m| m.tool_calls_json.as_deref())
        .filter_map(|tc| serde_json::from_str::<Value>(tc).ok())
        .flat_map(|v| {
            v.as_array()
                .unwrap_or(&Vec::new())
                .iter()
                .filter_map(|c| c["id"].as_str().map(String::from))
                .collect::<Vec<_>>()
        })
        .collect();
    for id in issued {
        assert!(
            second
                .iter()
                .any(|m| m.role == "tool" && m.tool_call_id.as_deref() == Some(id.as_str())),
            "两 run 后全历史仍配对：{id}"
        );
    }
}

/// 合成通道落表恒失败的 StorageBackend（只注入 role=tool 合成消息的 op 故障；
/// 插件侧 assistant op 正常放行——插件 messages op 自 DB-first 起为硬失败，
/// 会先于合成点终止 run，注入必须限定合成消息。其余全默认/空实现）。
/// 必选方法集对齐 persist_retry_test::FlakyStorage 现役口径。
struct MessagesOpFailStore {
    ops_failed: Arc<std::sync::atomic::AtomicUsize>,
}

#[async_trait]
impl agentos_core::traits::StorageBackend for MessagesOpFailStore {
    async fn apply_messages_ops_to_table(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        ops: &[Value],
    ) -> Result<(), agentos_core::types::StorageError> {
        let is_synthetic = ops.iter().any(|op| {
            op.get("msg")
                .and_then(|m| m.get("role"))
                .and_then(|r| r.as_str())
                == Some("tool")
        });
        if !is_synthetic {
            return Ok(());
        }
        self.ops_failed
            .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        Err(agentos_core::types::StorageError::Database(
            "injected messages-op failure".into(),
        ))
    }
    async fn get_run(
        &self,
        _run_id: &str,
    ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
        Err(agentos_core::types::StorageError::NotFound("null".into()))
    }
    async fn get_messages_by_pipeline(
        &self,
        _pipeline_id: &str,
        _opts: MessageQueryOpts,
    ) -> Result<Vec<MessageRecord>, agentos_core::types::StorageError> {
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
        Ok(false)
    }
    async fn delete_user(&self, _user_id: &str) -> Result<bool, agentos_core::types::StorageError> {
        Ok(false)
    }
}

/// 软降级分支：合成消息落库失败只 warn + 计 persist 失败指标，不改写终态
/// ——Cancelled 终局照常收尾（终局点不因合成失败掩盖原始终态）。
#[tokio::test]
async fn synthesis_write_failure_degrades_softly() {
    let ops_failed = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let metrics = Arc::new(agentos_engine::EngineMetrics::new());
    let invoker = Arc::new(MockInvoker::new());
    invoker.set_result(
        "pipeline_llm_core",
        llm_core_issues_call("call_fail_1", "human_interaction"),
    );
    invoker.set_result(
        "pipeline_tool_core",
        PluginResult {
            state_updates: HashMap::from([
                ("ended".to_string(), json!(true)),
                ("router.stop_reason".to_string(), json!("user_requested")),
                ("raw_tool_calls".to_string(), json!([])),
            ]),
            ..Default::default()
        },
    );
    let store_dyn: Arc<dyn agentos_core::traits::StorageBackend> = Arc::new(MessagesOpFailStore {
        ops_failed: Arc::clone(&ops_failed),
    });
    let executor = PipelineExecutor::new(
        Arc::clone(&invoker) as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        agentos_core::types::TenantContext::new("default", "session_orphan"),
        ["pipeline_llm_core", "pipeline_tool_core"]
            .iter()
            .map(|s| s.to_string()),
        store_dyn,
        "run_orphan_fail",
    )
    .with_metrics(Arc::clone(&metrics));
    let compiled = compile_pipeline(
        &make_config(true),
        &StepLibrary::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");

    let final_state = executor
        .run_compiled(&compiled, initial_state())
        .await
        .expect("合成写失败不得翻转 run 结果");

    assert_eq!(
        final_state["run_status"], "cancelled",
        "终态不受合成失败影响"
    );
    assert!(
        ops_failed.load(std::sync::atomic::Ordering::SeqCst) >= 1,
        "合成消息 op 尝试落表（失败注入命中；插件侧 assistant op 正常放行）"
    );
    assert!(
        metrics.snapshot().persist_failures >= 1,
        "合成落库失败计入 persist 失败指标（强观测）"
    );
}

/// 无孤儿早退分支：error 终局（dropped_tool_calls）但历史无 assistant
/// tool_calls（语义键在场而消息缺席）→ 零合成零副作用。
#[tokio::test]
async fn failed_terminal_without_orphans_is_noop() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let invoker = Arc::new(MockInvoker::new());
    // 只写语义键不发消息 op：异常终局闸命中，但孤儿集为空
    invoker.set_result(
        "pipeline_llm_core",
        PluginResult {
            state_updates: HashMap::from([(
                "raw_tool_calls".to_string(),
                json!([{ "id": "call_ghost", "name": "ghost_tool", "arguments": {} }]),
            )]),
            ..Default::default()
        },
    );
    let executor = make_executor(Arc::clone(&invoker), Arc::clone(&store));
    let compiled = compile_pipeline(
        &make_config(false),
        &StepLibrary::default(),
        executor.plugin_ids(),
    )
    .expect("compile ok");

    let final_state = executor
        .run_compiled(&compiled, initial_state())
        .await
        .expect("run 应成功收尾");
    assert_eq!(final_state["run_status"], "failed");

    let messages = read_messages(&store);
    assert!(
        !messages.iter().any(|m| m.role == "tool"),
        "无孤儿时零合成：{messages:?}"
    );
}
