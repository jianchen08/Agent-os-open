// @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: rust-test
//! P7: 引擎执行验证 — 多循环体 PipelineConfig 可被 PipelineExecutor 正确执行
//!
//! 验证链路（AC5）：
//! 构造（或 YAML 直解析）PipelineConfig（loop_bodies）→ PipelineExecutor.run
//! （MockInvoker + NullStorage / SqliteStore）

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use agentos_core::traits::{MessageQueryOpts, PluginInvoker, StorageBackend};
use agentos_core::types::{
    Branch, LoopBody, MessageRecord, PipelineConfig, PipelineStep, PluginContext, PluginError,
    PluginResult, Route, RouteAction, RouteNext, RunRecord, RunStatus, StepItem, StepLibrary,
    ToolExecutionResult, TraceEntry,
};
use agentos_engine::compiler::compile_pipeline;
use agentos_engine::{PipelineExecutor, SqliteStore};
use async_trait::async_trait;
use serde_json::json;

/// 可编程 MockInvoker：按 plugin_id 返回预设结果，并统计调用序列。
struct MockInvoker {
    results: Mutex<HashMap<String, PluginResult>>,
    calls: Mutex<Vec<String>>,
}

impl MockInvoker {
    fn new() -> Self {
        Self {
            results: Mutex::new(HashMap::new()),
            calls: Mutex::new(Vec::new()),
        }
    }

    fn set_result(&self, plugin_id: &str, result: PluginResult) {
        self.results
            .lock()
            .unwrap()
            .insert(plugin_id.to_string(), result);
    }

    fn call_count(&self, plugin_id: &str) -> usize {
        self.calls
            .lock()
            .unwrap()
            .iter()
            .filter(|p| p.as_str() == plugin_id)
            .count()
    }
}

#[async_trait]
impl PluginInvoker for MockInvoker {
    async fn invoke_pipeline_plugin(
        &self,
        plugin_id: &str,
        _ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError> {
        self.calls.lock().unwrap().push(plugin_id.to_string());
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

/// 空 StorageBackend（仅构造 ContentLoader 用，持久化全部成功空操作）。
struct NullStorage;

#[async_trait]
impl StorageBackend for NullStorage {
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
    async fn create_branch(
        &self,
        _branch: Branch,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn update_run_status(
        &self,
        _run_id: &str,
        _status: RunStatus,
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
    // ── users（0.5.0 最小持久化）：NullStorage 不实现，返回空
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

/// 构造新格式（多循环体）管道配置：main 体 = prepare/core/post，语义对齐
/// 原 0.1 平铺 default.yaml 的转换产物（插件链 + 路由仲裁）。
fn make_engine_config() -> PipelineConfig {
    let prepare_plugins = [
        "pipeline_tool_schema",
        "pipeline_param_inject",
        "pipeline_security_check",
        "pipeline_multimodal_preprocessor",
        "pipeline_context_window_guard",
        "pipeline_prompt_build",
        "pipeline_pause_guard",
    ]
    .map(|s| s.to_string())
    .to_vec();

    // post 路由：有工具调用 → 切 tool_execute 循环；其余（纯文本/工具已执行完）→ end
    let post_routes = vec![
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
    ];

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
                    routes: post_routes,
                    loop_config: None,
                },
            ],
            while_cond: Some("True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
    }
}

/// 构造 PipelineExecutor（MockInvoker + NullStorage）。
fn make_executor(invoker: Arc<MockInvoker>, plugin_ids: &[&str]) -> PipelineExecutor {
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage);
    PipelineExecutor::new(
        invoker as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        agentos_core::types::TenantContext::new("tenant_test", "session_test"),
        plugin_ids.iter().map(|s| s.to_string()),
        store,
        "run_p7",
        "main",
    )
}

/// 构造 PipelineExecutor（MockInvoker + 真实 SqliteStore，用于验证 messages 落表）。
fn make_executor_with_store(
    invoker: Arc<MockInvoker>,
    plugin_ids: &[&str],
    store: Arc<SqliteStore>,
    run_id: &str,
) -> PipelineExecutor {
    let store_dyn: Arc<dyn StorageBackend> = store;
    PipelineExecutor::new(
        invoker as Arc<dyn PluginInvoker>,
        Path::new(".").to_path_buf(),
        agentos_core::types::TenantContext::new("default", "session_test"),
        plugin_ids.iter().map(|s| s.to_string()),
        store_dyn,
        run_id,
        "main",
    )
}

/// 默认插件 id 集合（对齐 make_engine_config 的 prepare/core/post 引用）。
const DEFAULT_PLUGIN_IDS: [&str; 10] = [
    "pipeline_tool_schema",
    "pipeline_param_inject",
    "pipeline_security_check",
    "pipeline_multimodal_preprocessor",
    "pipeline_context_window_guard",
    "pipeline_prompt_build",
    "pipeline_pause_guard",
    "pipeline_llm_core",
    "pipeline_stop_check",
    "pipeline_result_format",
];

// ── AC5: 多循环体配置可被 PipelineExecutor 正确执行 ─────────────

/// 全链路：PipelineConfig（main 循环体）→ executor.run。
///
/// 验证：
/// 1. prepare 阶段 input 插件被调用（pipeline_tool_schema 等）
/// 2. core 阶段动态 core_plugin 被调用（pipeline_llm_core）
/// 3. post 阶段 output 插件被调用（pipeline_stop_check / pipeline_result_format）
/// 4. 无工具调用时 end 路由终止管道（ended=true），raw_result 保留
/// 5. state["current_phase"] 记录当前循环体 id
#[tokio::test]
async fn test_pipeline_executes_steps() {
    let engine_cfg = make_engine_config();
    assert_eq!(engine_cfg.name, "agentos_agent");
    assert_eq!(engine_cfg.loop_bodies.len(), 1);
    assert_eq!(
        engine_cfg.loop_bodies[0].while_cond.as_deref(),
        Some("True")
    );

    let invoker = Arc::new(MockInvoker::new());
    // llm_core 返回纯文本回复（无工具调用）→ end 路由终止
    invoker.set_result(
        "pipeline_llm_core",
        PluginResult {
            state_updates: HashMap::from([
                ("raw_result".to_string(), json!("你好，我是灵汐")),
                ("raw_tool_calls".to_string(), json!([])),
            ]),
            ..Default::default()
        },
    );
    let executor = make_executor(Arc::clone(&invoker), &DEFAULT_PLUGIN_IDS);

    let initial_state = json!({
        "message": "你好",
        "agent_id": "agentos",
        "core_type": "llm_call",
        "core_plugin": "pipeline_llm_core",
        "ended": false,
        "suspended": false,
    });
    let final_state = executor
        .run_compiled(
            &compile_pipeline(&engine_cfg, &StepLibrary::default(), executor.plugin_ids())
                .expect("compile ok"),
            initial_state,
        )
        .await
        .expect("executor run should succeed");

    // prepare 阶段 input 插件被调用
    assert!(
        invoker.call_count("pipeline_tool_schema") >= 1,
        "pipeline_tool_schema should be invoked"
    );
    assert!(
        invoker.call_count("pipeline_pause_guard") >= 1,
        "pipeline_pause_guard should be invoked"
    );
    assert!(
        invoker.call_count("pipeline_multimodal_preprocessor") >= 1,
        "pipeline_multimodal_preprocessor should be invoked"
    );

    // core 阶段动态 core_plugin 被调用
    assert!(
        invoker.call_count("pipeline_llm_core") >= 1,
        "pipeline_llm_core should be invoked"
    );

    // post 阶段 output 插件被调用
    assert!(
        invoker.call_count("pipeline_stop_check") >= 1,
        "pipeline_stop_check should be invoked"
    );
    assert!(
        invoker.call_count("pipeline_result_format") >= 1,
        "pipeline_result_format should be invoked"
    );

    // 无工具调用 → end 路由终止管道；raw_result 保留
    assert_eq!(
        final_state.get("ended").and_then(|v| v.as_bool()),
        Some(true),
        "pipeline should end via end route"
    );
    assert_eq!(
        final_state.get("raw_result").and_then(|v| v.as_str()),
        Some("你好，我是灵汐")
    );
    // current_phase 记录当前循环体
    assert_eq!(
        final_state.get("current_phase").and_then(|v| v.as_str()),
        Some("main")
    );
}

/// 有工具调用时：next_tool 路由 → 循环切到 tool_execute 核心插件。
///
/// 验证：
/// 1. llm_core 返回非空 raw_tool_calls → next_tool 路由命中
/// 2. 路由 set core_type=tool_execute + core_plugin=pipeline_tool_core
/// 3. 下一轮 core 调用 pipeline_tool_core（工具执行）
/// 4. tool_core 返回空工具调用 + 文本 → end 路由终止
#[tokio::test]
async fn test_pipeline_routes_tool_calls_to_loop() {
    // 安全阀：测试防挂死（路由 bug 时第 10 轮截断暴露断言失败）
    let engine_cfg = make_engine_config();

    let plugin_ids = [
        "pipeline_tool_schema",
        "pipeline_param_inject",
        "pipeline_security_check",
        "pipeline_multimodal_preprocessor",
        "pipeline_context_window_guard",
        "pipeline_prompt_build",
        "pipeline_pause_guard",
        "pipeline_llm_core",
        "pipeline_tool_core",
        "pipeline_stop_check",
        "pipeline_result_format",
    ];

    let invoker = Arc::new(MockInvoker::new());
    // llm_core：返回一个工具调用 → next_tool 路由
    invoker.set_result(
        "pipeline_llm_core",
        PluginResult {
            state_updates: HashMap::from([
                (
                    "raw_tool_calls".to_string(),
                    json!([{ "name": "file_read", "arguments": {} }]),
                ),
                ("raw_result".to_string(), json!("")),
            ]),
            ..Default::default()
        },
    );
    // tool_core：工具执行后返回纯文本 → end 路由
    invoker.set_result(
        "pipeline_tool_core",
        PluginResult {
            state_updates: HashMap::from([
                ("raw_result".to_string(), json!("文件内容已读取")),
                ("raw_tool_calls".to_string(), json!([])),
            ]),
            ..Default::default()
        },
    );
    let executor = make_executor(Arc::clone(&invoker), &plugin_ids);

    let initial_state = json!({
        "message": "读取文件",
        "agent_id": "agentos",
        "core_type": "llm_call",
        "core_plugin": "pipeline_llm_core",
        "ended": false,
        "suspended": false,
    });
    let final_state = executor
        .run_compiled(
            &compile_pipeline(&engine_cfg, &StepLibrary::default(), executor.plugin_ids())
                .expect("compile ok"),
            initial_state,
        )
        .await
        .expect("executor run should succeed");

    // 两轮核心调用：llm_core（第一轮）+ tool_core（第二轮）
    assert!(
        invoker.call_count("pipeline_llm_core") >= 1,
        "pipeline_llm_core should be invoked in round 1"
    );
    assert!(
        invoker.call_count("pipeline_tool_core") >= 1,
        "pipeline_tool_core should be invoked in round 2 via next_tool route"
    );
    // 最终 raw_result 来自 tool_core
    assert_eq!(
        final_state.get("raw_result").and_then(|v| v.as_str()),
        Some("文件内容已读取")
    );
    // 管道正常终止
    assert_eq!(
        final_state.get("ended").and_then(|v| v.as_bool()),
        Some(true)
    );
}

// ── op-based 消息持久化接线验证（Step 1b-slice2）─────────────
//
// 验证：插件在 state_updates 里 emit `messages: {_ops:[set/insert]}` 时，
// 引擎 merge_and_project 把同一组 op "一次 apply" 到内存 state 与 message_slots 表。
// 详见 docs/message_persistence_design.md。
#[tokio::test]
async fn wiring_messages_ops_applied_to_state_and_table() {
    let engine_cfg = make_engine_config();

    let invoker = Arc::new(MockInvoker::new());
    // llm_core emit messages op（新模型）+ 纯文本回复（→ end 路由终止）
    invoker.set_result(
        "pipeline_llm_core",
        PluginResult {
            state_updates: HashMap::from([
                (
                    "messages".to_string(),
                    json!({
                        "_ops": [
                            { "op": "set", "seq": 0, "msg": { "role": "user", "content": "你好" } },
                            { "op": "set", "seq": 1, "msg": { "role": "assistant", "content": "嗨" } },
                        ]
                    }),
                ),
                ("raw_result".to_string(), json!("嗨")),
                ("raw_tool_calls".to_string(), json!([])),
            ]),
            ..Default::default()
        },
    );

    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let executor = make_executor_with_store(
        Arc::clone(&invoker),
        &DEFAULT_PLUGIN_IDS,
        Arc::clone(&store),
        "run_wiring",
    );

    let initial_state = json!({
        "pipeline_id": "p_wiring",
        "message": "你好",
        "agent_id": "agentos",
        "core_type": "llm_call",
        "core_plugin": "pipeline_llm_core",
        "ended": false,
        "suspended": false,
    });

    let final_state = executor
        .run_compiled(
            &compile_pipeline(&engine_cfg, &StepLibrary::default(), executor.plugin_ids())
                .expect("compile ok"),
            initial_state,
        )
        .await
        .expect("run should succeed");

    // 内存 state：messages 已应用 op（含 seq）
    let arr = final_state
        .get("messages")
        .and_then(|v| v.as_array())
        .expect("messages 应存在");
    assert_eq!(arr.len(), 2, "内存应有 2 条消息");
    assert_eq!(arr[0].get("seq").and_then(|v| v.as_u64()), Some(0));
    assert_eq!(arr[1].get("seq").and_then(|v| v.as_u64()), Some(1));

    // 表：同一组 op 已落 message_slots
    let rows = store
        .get_slot_messages_by_pipeline("p_wiring", "default", MessageQueryOpts::default())
        .expect("读表应成功");
    let seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    assert_eq!(seqs, vec![0, 1], "表应有 seq 0,1（op 一次落表）");
    let roles: Vec<&str> = rows.iter().map(|r| r.role.as_str()).collect();
    assert_eq!(roles, vec!["user", "assistant"]);
}

/// G10：while 条件循环——body 无 loop_config 但有 while_cond，循环继续条件
/// 每轮对 state 求值，假则退出（正常推进后续循环体）。
#[tokio::test]
async fn test_while_cond_drives_body_loop() {
    let invoker = Arc::new(MockInvoker::new());
    // count 插件每轮把 state.count +1
    invoker.set_result(
        "counter",
        PluginResult {
            state_updates: HashMap::from([("count".to_string(), json!(1))]),
            ..Default::default()
        },
    );
    // 这里用 state_updates 是"设置"，需要叠加计数语义：插件读当前 count 写回 +1。
    // MockInvoker 的结果是静态预设，无法读 state；改用 after_merge 检查最终次数：
    // while "count < 3" 循环 3 次后退出（count 由插件写 1，merge 覆盖）——
    // 为验证"循环轮数受 while 控制"，用 max_iterations 兜底断言调用次数。
    let config = PipelineConfig {
        name: "while_test".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "tick".into(),
                steps: vec!["counter".into()],
                when: None,
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
            // while 恒假 → 一次都不执行（循环模式由 while 开启，
            // 第一轮开头求值为假即退出）
            while_cond: Some("False".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
    };
    let executor = make_executor(Arc::clone(&invoker), &["counter"]);
    let final_state = executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
                .expect("compile ok"),
            json!({}),
        )
        .await
        .expect("run should succeed");
    assert_eq!(
        invoker.call_count("counter"),
        0,
        "while=False 第一轮即退出，零调用"
    );
    assert_eq!(final_state["ended"], json!(false));
}

/// G10：while 条件为真时循环执行，条件变为假后退出。
#[tokio::test]
async fn test_while_cond_false_after_state_change_exits() {
    let invoker = Arc::new(MockInvoker::new());
    // flipper 每轮把 state.done 置 True（第二轮起 while 为假退出）
    invoker.set_result(
        "flipper",
        PluginResult {
            state_updates: HashMap::from([("done".to_string(), json!(true))]),
            ..Default::default()
        },
    );
    let config = PipelineConfig {
        name: "while_test2".into(),
        loop_bodies: vec![LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "flip".into(),
                steps: vec!["flipper".into()],
                when: None,
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
            while_cond: Some("done != True".into()),
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
    };
    let executor = make_executor(Arc::clone(&invoker), &["flipper"]);
    let final_state = executor
        .run_compiled(
            &compile_pipeline(&config, &StepLibrary::default(), executor.plugin_ids())
                .expect("compile ok"),
            json!({}),
        )
        .await
        .expect("run should succeed");
    // 第一轮执行（done 缺失 → 条件真）；插件置 done=true；第二轮条件假退出
    assert_eq!(
        invoker.call_count("flipper"),
        1,
        "done 置真后 while 假，只执行一轮"
    );
    assert_eq!(final_state["done"], json!(true));
    assert_eq!(final_state["ended"], json!(false), "while 退出 ≠ ended");
}
