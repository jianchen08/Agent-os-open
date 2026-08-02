//! P7: 引擎执行验证 — config crate 转换产物可被 PipelineExecutor 正确执行
//!
//! 验证链路（AC5）：
//! config/pipelines/default.yaml（0.1 扁平格式）→ load_pipeline_definition →
//! to_engine_config（steps 模型）→ PipelineExecutor.run（MockInvoker + NullStorage）

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use agentos_config::load_pipeline_definition;
use agentos_core::traits::{PluginInvoker, StorageBackend};
use agentos_core::types::{
    Branch, Message, MessageRecord, PluginContext, PluginError, PluginResult, RunRecord,
    RunStatus, ToolExecutionResult, TraceEntry,
};
use agentos_engine::PipelineExecutor;
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
    async fn get_messages(
        &self,
        _run_id: &str,
        _branch_id: &str,
    ) -> Result<Vec<MessageRecord>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn get_messages_by_pipeline(
        &self,
        _pipeline_id: &str,
        _opts: agentos_core::traits::MessageQueryOpts,
    ) -> Result<Vec<MessageRecord>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn next_sequence(&self, _pipeline_id: &str) -> Result<u32, agentos_core::types::StorageError> {
        Ok(1)
    }
    async fn get_recent_messages(
        &self,
        _run_id: &str,
        _branch_id: &str,
        _n: usize,
    ) -> Result<Vec<Message>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, agentos_core::types::StorageError> {
        Ok(vec![])
    }
    async fn append_trace(&self, _entry: TraceEntry) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn create_branch(&self, _branch: Branch) -> Result<(), agentos_core::types::StorageError> {
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
    #[allow(clippy::too_many_arguments)]
    async fn append_message(
        &self,
        _message_id: &str,
        _run_id: &str,
        _branch_id: &str,
        _seq_in_branch: u32,
        _role: &str,
        _blob_id: Option<&str>,
        _content_preview: Option<&str>,
        _pipeline_id: Option<&str>,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn store_blob(&self, _data: &[u8], _mime_type: &str) -> Result<String, agentos_core::types::StorageError> {
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
    async fn delete_session(&self, _thread_id: &str) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
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
    async fn count_execution_records(&self, _pipeline_run_id: &str) -> Result<u64, agentos_core::types::StorageError> {
        Ok(0)
    }
    async fn delete_execution_records_by_session(&self, _pipeline_run_id: &str) -> Result<u64, agentos_core::types::StorageError> {
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
    ) -> Result<Option<agentos_core::types::PipelineRunSummary>, agentos_core::types::StorageError> {
        Ok(None)
    }
    async fn update_run_summary(
        &self,
        _run_id: &str,
        _updates: &serde_json::Value,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn list_run_summaries(
        &self,
        _limit: Option<usize>,
    ) -> Result<Vec<agentos_core::types::PipelineRunSummary>, agentos_core::types::StorageError> {
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
    async fn delete_memory(&self, _id: &str) -> Result<bool, agentos_core::types::StorageError> {
        Ok(false)
    }
}

/// 写入一份 default.yaml 形态的管道配置（0.1 扁平格式，含 input/output 路由 + 插件链 + 核心插件）。
fn write_default_pipeline(config_dir: &Path) {
    let pipelines = config_dir.join("pipelines");
    std::fs::create_dir_all(&pipelines).unwrap();
    std::fs::write(
        pipelines.join("default.yaml"),
        r#"
name: agentos_agent

input_routes:
  - name: tool_execute
    condition: "core_type == 'tool_execute'"
    target: core
    plugins: [tool_schema, param_inject, security_check]
    priority: 10
  - name: llm_call
    condition: "core_type == 'llm_call'"
    target: core
    plugins: [multimodal_preprocessor, context_window_guard, tool_schema, prompt_build]
    priority: 20
  - name: default
    condition: "True"
    target: core
    plugins: [pause_guard, tool_schema, param_inject, prompt_build]
    priority: 30

output_routes:
  - route_type: next_tool
    condition: "raw_tool_calls != []"
    priority: 1
  - route_type: next_llm
    condition: "True"
    priority: 50
  - route_type: end
    condition: "True"
    priority: 99

plugins:
  - name: tool_schema
    config:
      enabled: true
  - name: prompt_build
    config:
      enabled: true
  - name: stop_check
    config:
      enabled: true
  - name: result_format
    config:
      max_result_length: 2000

core_plugins:
  llm_call:
    class: plugins.shared.core.llm_core.plugin.LLMCore
    config:
      default_params:
        temperature: 0.7
  tool_execute:
    class: plugins.shared.core.tool_core.plugin.ToolCore
    config:
      timeout: 300
"#,
    )
    .unwrap();
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

// ── AC5: 转换产物可被 PipelineExecutor 正确执行 ─────────────────

/// 全链路：default.yaml → PipelineDefinition → PipelineConfig → executor.run。
///
/// 验证：
/// 1. 转换不报错（to_engine_config 产出合法 PipelineConfig）
/// 2. prepare 阶段 input 插件被调用（pipeline_tool_schema 等，带 pipeline_ 前缀）
/// 3. core 阶段动态 core_plugin 被调用（pipeline_llm_core）
/// 4. post 阶段 output 插件被调用（pipeline_stop_check / pipeline_result_format）
/// 5. 无工具调用时 end 路由终止管道（ended=true），raw_result 保留
#[tokio::test]
async fn test_converted_pipeline_executes_steps() {
    let tmp = tempfile::tempdir().unwrap();
    write_default_pipeline(tmp.path());

    let def = load_pipeline_definition(tmp.path(), "default").expect("load pipeline");
    let engine_cfg = def.to_engine_config();
    assert_eq!(engine_cfg.name, "agentos_agent");
    assert!(engine_cfg.loop_config.enabled);

    // 插件 id 集合（含 pipeline_ 前缀，对齐转换器）
    let plugin_ids = [
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
    let executor = make_executor(Arc::clone(&invoker), &plugin_ids);

    let initial_state = json!({
        "message": "你好",
        "agent_id": "agentos",
        "core_type": "llm_call",
        "core_plugin": "pipeline_llm_core",
        "ended": false,
        "suspended": false,
    });
    let final_state = executor
        .run(
            &engine_cfg,
            &agentos_core::types::StepLibrary::default(),
            initial_state,
        )
        .await
        .expect("executor run should succeed");

    // prepare 阶段 input 插件被调用（去重后仍在链中）
    assert!(
        invoker.call_count("pipeline_tool_schema") >= 1,
        "pipeline_tool_schema should be invoked"
    );
    assert!(
        invoker.call_count("pipeline_pause_guard") >= 1,
        "pipeline_pause_guard should be invoked (default route)"
    );
    assert!(
        invoker.call_count("pipeline_multimodal_preprocessor") >= 1,
        "pipeline_multimodal_preprocessor should be invoked (llm_call route)"
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
}

/// 有工具调用时：next_tool 路由 → 循环切到 tool_execute 核心插件。
///
/// 验证：
/// 1. llm_core 返回非空 raw_tool_calls → next_tool 路由（priority 1）命中
/// 2. 路由 set core_type=tool_execute + core_plugin=pipeline_tool_core
/// 3. 下一轮 core 调用 pipeline_tool_core（工具执行）
/// 4. tool_core 返回空工具调用 + 文本 → end 路由终止
#[tokio::test]
async fn test_converted_pipeline_routes_tool_calls_to_loop() {
    let tmp = tempfile::tempdir().unwrap();
    write_default_pipeline(tmp.path());

    let def = load_pipeline_definition(tmp.path(), "default").unwrap();
    let mut engine_cfg = def.to_engine_config();
    // 安全阀：测试防挂死。0.1 语义是无限循环靠 end 路由终止（转换器默认 -1），
    // 此处仅测试侧设有限值——若路由逻辑有 bug 导致循环无法终止，会在第 10 轮
    // 被安全阀截断并暴露断言失败，而非 SIGKILL 无诊断。
    engine_cfg.loop_config.max_iterations = 10;

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
        .run(
            &engine_cfg,
            &agentos_core::types::StepLibrary::default(),
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
