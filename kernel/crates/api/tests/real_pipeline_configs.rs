//! 真实管道配置（config/pipelines/*.yaml）过「归一加载 → 引擎编译 → 运行」的
//! 端到端集成测试（内核启动同一条路径）。
//!
//! 生产 YAML 的 `next:` 路由由 pipeline_loader 的 PipelineFile::to_internal 归一
//! （裸 PipelineConfig serde 不认 `next`），编译校验（含 loop_config 校验）与
//! 运行时只在内核编译缓存路径触达——本测试用 `load_pipeline_config_by_name`
//! 加载 `autonomous` / `roleplay` 真实文件，再喂给 compile_pipeline +
//! run_compiled，用脚本化 invoker 驱动完整 merged-round 流程：
//! prepare → core(tool_cache) → tool_exec(for-each) → llm → post 路由。
//!
//! 插件全部 mock（返回 no-op 或脚本化 state_updates），只验证编排语义：
//! for-each 消费 raw_tool_calls、collect 按迭代序、consume 收尾清源、
//! roleplay 的「工具执行过的迭代跳过 llm」门、post 路由终局。

use agentos_api::pipeline_loader::load_pipeline_config_by_name;
use agentos_core::traits::{MessageQueryOpts, PluginInvoker, SessionListFilter, StorageBackend};
use agentos_core::types::{
    MessageRecord, PluginContext, PluginError, PluginResult, RunRecord, SessionRecord, StepLibrary,
    StorageError, TenantContext, ToolExecutionResult, TraceEntry, UserRecord,
};
use agentos_engine::compiler::compile_pipeline;
use agentos_engine::PipelineExecutor;
use async_trait::async_trait;
use serde_json::json;
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

const REPO_CONFIG_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../config");

fn load_pipeline(name: &str) -> agentos_core::types::PipelineConfig {
    load_pipeline_config_by_name(Path::new(REPO_CONFIG_DIR), name)
        .unwrap_or_else(|e| panic!("真 {name}.yaml 归一加载失败: {e}"))
}

use agentos_core::types::PipelineConfig;
use std::path::Path;

/// 从 YAML 里收集全部插件引用（pipeline_ 前缀字符串），作为 plugin_ids。
fn collect_plugin_refs(config: &PipelineConfig) -> HashSet<String> {
    let value = serde_json::to_value(config).expect("serialize");
    let mut ids = HashSet::new();
    fn walk(v: &serde_json::Value, out: &mut HashSet<String>) {
        match v {
            serde_json::Value::String(s) => {
                if s.starts_with("pipeline_") {
                    out.insert(s.clone());
                }
            }
            serde_json::Value::Array(a) => a.iter().for_each(|x| walk(x, out)),
            serde_json::Value::Object(o) => o.values().for_each(|x| walk(x, out)),
            _ => {}
        }
    }
    walk(&value, &mut ids);
    ids
}

/// 脚本化 invoker：按插件名出脚本（逐次消费），并记录每次调用捕获的迭代变量。
#[derive(Default)]
struct ScriptedInvoker {
    scripts: Mutex<HashMap<String, Vec<serde_json::Value>>>,
    /// plugin_id → 每次 invoke 捕获的 state.current_call。
    seen_calls: Mutex<HashMap<String, Vec<serde_json::Value>>>,
    /// plugin_id → 调用次数。
    counts: Mutex<HashMap<String, usize>>,
}

impl ScriptedInvoker {
    fn script(&self, plugin_id: &str, updates: Vec<serde_json::Value>) {
        self.scripts
            .lock()
            .unwrap()
            .insert(plugin_id.to_string(), updates);
    }

    #[allow(dead_code)]
    fn count(&self, plugin_id: &str) -> usize {
        *self.counts.lock().unwrap().get(plugin_id).unwrap_or(&0)
    }

    fn calls(&self, plugin_id: &str) -> Vec<serde_json::Value> {
        self.seen_calls
            .lock()
            .unwrap()
            .get(plugin_id)
            .cloned()
            .unwrap_or_default()
    }
}

#[async_trait]
impl PluginInvoker for ScriptedInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        *self
            .counts
            .lock()
            .unwrap()
            .entry(plugin_id.to_string())
            .or_insert(0) += 1;
        if let Some(call) = ctx.state.get("current_call") {
            if !call.is_null() {
                self.seen_calls
                    .lock()
                    .unwrap()
                    .entry(plugin_id.to_string())
                    .or_default()
                    .push(call.clone());
            }
        }
        let call = self
            .seen_calls
            .lock()
            .unwrap()
            .get(plugin_id)
            .and_then(|v| v.last().cloned());
        let mut next = self
            .scripts
            .lock()
            .unwrap()
            .get_mut(plugin_id)
            .and_then(|v| {
                if v.is_empty() {
                    None
                } else {
                    Some(v.remove(0))
                }
            })
            .unwrap_or_else(|| json!({}));
        // 真 tool_core 契约：_executed_tool_calls = [本迭代执行的调用]。
        if plugin_id == "pipeline_tool_core" {
            if let Some(c) = &call {
                next["_executed_tool_calls"] = json!([c]);
            }
        }
        let state_updates: HashMap<String, serde_json::Value> = serde_json::from_value(next)
            .unwrap_or_else(|e| panic!("脚本 {plugin_id} state_updates 形状非法: {e}"));
        Ok(PluginResult {
            state_updates,
            skip_remaining: false,
            error: None,
        })
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

fn make_executor(invoker: Arc<ScriptedInvoker>, plugin_ids: &HashSet<String>) -> PipelineExecutor {
    let store: Arc<dyn StorageBackend> = Arc::new(NullStorage);
    PipelineExecutor::new(
        invoker as Arc<dyn PluginInvoker>,
        PathBuf::from("."),
        TenantContext::new("t_it", "s_it"),
        plugin_ids.iter().cloned(),
        store,
        "run_it",
    )
}

/// 空 StorageBackend：trace/blob 写面放行（run_init 等引擎窗口必发），读面回空。
struct NullStorage;

#[async_trait]
impl StorageBackend for NullStorage {
    async fn get_run(&self, _run_id: &str) -> Result<RunRecord, StorageError> {
        Err(StorageError::NotFound("null".into()))
    }
    async fn get_messages_by_pipeline(
        &self,
        _pipeline_id: &str,
        _opts: MessageQueryOpts,
    ) -> Result<Vec<MessageRecord>, StorageError> {
        Ok(vec![])
    }
    async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, StorageError> {
        Ok(vec![])
    }
    async fn append_trace(&self, _entry: TraceEntry) -> Result<(), StorageError> {
        Ok(())
    }
    async fn store_blob(&self, _data: &[u8], _mime_type: &str) -> Result<String, StorageError> {
        Ok("blob_null".into())
    }
    async fn create_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
        Ok(())
    }
    async fn get_session(&self, _thread_id: &str) -> Result<Option<SessionRecord>, StorageError> {
        Ok(None)
    }
    async fn list_sessions(
        &self,
        _filter: SessionListFilter,
    ) -> Result<Vec<SessionRecord>, StorageError> {
        Ok(vec![])
    }
    async fn update_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
        Ok(())
    }
    async fn delete_session(&self, _thread_id: &str) -> Result<Vec<String>, StorageError> {
        Ok(vec![])
    }
    async fn link_pipeline_session(
        &self,
        _pipeline_id: &str,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<(), StorageError> {
        Ok(())
    }
    async fn list_pipeline_ids_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<String>, StorageError> {
        Ok(vec![])
    }
    async fn get_step_traces_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, StorageError> {
        Ok(vec![])
    }
    async fn create_user(&self, _user: &UserRecord) -> Result<(), StorageError> {
        Ok(())
    }
    async fn get_user_by_id(&self, _user_id: &str) -> Result<Option<UserRecord>, StorageError> {
        Ok(None)
    }
    async fn get_user_by_username(
        &self,
        _username: &str,
    ) -> Result<Option<UserRecord>, StorageError> {
        Ok(None)
    }
    async fn list_users(&self) -> Result<Vec<UserRecord>, StorageError> {
        Ok(vec![])
    }
    async fn update_last_login(&self, _user_id: &str) -> Result<(), StorageError> {
        Ok(())
    }
    async fn update_user_password(
        &self,
        _user_id: &str,
        _password_hash: &str,
        _must_change_password: bool,
    ) -> Result<bool, StorageError> {
        Ok(false)
    }
    async fn delete_user(&self, _user_id: &str) -> Result<bool, StorageError> {
        Ok(false)
    }
}

/// tool_core 单迭代脚本：collect 键单元素 + 一条 tool 消息 op。
fn tool_core_updates() -> serde_json::Value {
    json!({
        "tool_results": [{"echo": "x"}],
        "_full_tool_results": [{"full": "x"}],
        "messages": {"_ops": [{"op": "set", "msg": {"role": "tool", "tool_call_id": "it", "content": "ok"}}]},
    })
}

#[tokio::test]
async fn autonomous_yaml_compiles_and_runs_merged_round() {
    // 真 autonomous.yaml：归一加载 → 编译（含 loop_config 校验）→ 两迭代走完
    // merged-round：迭代1 llm 产 2 调用；迭代2 for-each 执行 + llm 终局。
    let config = load_pipeline("autonomous");
    let plugin_ids = collect_plugin_refs(&config);
    let inv = Arc::new(ScriptedInvoker::default());
    inv.script(
        "pipeline_llm_core",
        vec![
            // 迭代1：产出两个工具调用
            json!({
                "raw_tool_calls": [
                    {"name": "bash", "id": "c1", "args": {"command": "ls"}},
                    {"name": "file_read", "id": "c2", "args": {"path": "a.py"}}
                ],
                "raw_result": "calling tools",
                "messages": {"_ops": [{"op": "set", "msg": {"role": "assistant", "content": "calling tools"}}]},
            }),
            // 迭代2：看到工具结果后终局
            json!({
                "raw_tool_calls": [],
                "raw_result": "done",
                "task.status": "completed",
                "messages": {"_ops": [{"op": "set", "msg": {"role": "assistant", "content": "done"}}]},
            }),
        ],
    );
    inv.script(
        "pipeline_tool_core",
        vec![tool_core_updates(), tool_core_updates()],
    );
    let executor = make_executor(Arc::clone(&inv), &plugin_ids);
    let compiled = compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids())
        .expect("真 autonomous.yaml 必须通过编译校验（含 loop_config）");
    let final_state = executor
        .run_compiled(
            &compiled,
            // 任务域键是平铺点号键（StateKeys 契约）——嵌套对象会遮蔽平铺写面。
            json!({"task.id": "t1", "task.status": "running", "session_id": "s1"}),
        )
        .await
        .expect("run 成功");

    // 编排断言：merged-round 两迭代；for-each 逐调用各一次；终局。
    assert_eq!(
        inv.count("pipeline_llm_core"),
        2,
        "llm 每迭代一次（merged-round）"
    );
    assert_eq!(inv.count("pipeline_tool_core"), 2, "每个调用一次迭代");
    let mut got_ids: Vec<String> = inv
        .calls("pipeline_tool_core")
        .iter()
        .map(|c| c["id"].as_str().unwrap_or("").to_string())
        .collect();
    got_ids.sort();
    assert_eq!(got_ids, vec!["c1", "c2"], "迭代变量 = raw_tool_calls 元素");
    // collect 置换 + consume 清源 + 终局路由。
    assert_eq!(
        final_state["tool_results"].as_array().map(Vec::len),
        Some(2)
    );
    assert_eq!(
        final_state["raw_tool_calls"],
        json!([]),
        "consume 清 over 源键"
    );
    assert_eq!(
        final_state["pre_decided_results"],
        json!([]),
        "consume_keys 清预填"
    );
    assert_eq!(final_state["task.status"], json!("completed"));
    assert_eq!(final_state["ended"], json!(true), "post 路由终局");
}

#[tokio::test]
async fn autonomous_yaml_for_each_five_calls_batch() {
    // max_concurrency=4 下 5 个调用：collect 拼接完整、流程终局。
    let config = load_pipeline("autonomous");
    let plugin_ids = collect_plugin_refs(&config);
    let inv = Arc::new(ScriptedInvoker::default());
    let calls: Vec<serde_json::Value> = (0..5)
        .map(|i| json!({"name": "bash", "id": format!("c{i}"), "args": {}}))
        .collect();
    inv.script(
        "pipeline_llm_core",
        vec![
            json!({
                "raw_tool_calls": calls,
                "raw_result": "batch",
                "messages": {"_ops": [{"op": "set", "msg": {"role": "assistant", "content": "batch"}}]},
            }),
            json!({
                "raw_tool_calls": [],
                "raw_result": "done",
                "task.status": "completed",
                "messages": {"_ops": [{"op": "set", "msg": {"role": "assistant", "content": "done"}}]},
            }),
        ],
    );
    let tool_script: Vec<serde_json::Value> = (0..5)
        .map(|_| {
            let mut u = tool_core_updates();
            u["messages"] = json!({"_ops": []});
            u
        })
        .collect();
    inv.script("pipeline_tool_core", tool_script);
    let executor = make_executor(Arc::clone(&inv), &plugin_ids);
    let compiled = compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids())
        .expect("compile");
    let final_state = executor
        .run_compiled(
            &compiled,
            json!({"task.id": "t1", "task.status": "running"}),
        )
        .await
        .expect("run 成功");
    assert_eq!(inv.count("pipeline_tool_core"), 5);
    assert_eq!(
        final_state["tool_results"].as_array().map(Vec::len),
        Some(5)
    );
    assert_eq!(final_state["ended"], json!(true));
}

#[tokio::test]
async fn roleplay_yaml_text_round_ends_without_tools() {
    // 真 roleplay.yaml：纯文本轮一轮终局，tool_core 零调用。
    let config = load_pipeline("roleplay");
    let plugin_ids = collect_plugin_refs(&config);
    let inv = Arc::new(ScriptedInvoker::default());
    inv.script(
        "pipeline_llm_core",
        vec![json!({
            "raw_tool_calls": [],
            "raw_result": "hello",
            "messages": {"_ops": [{"op": "set", "msg": {"role": "assistant", "content": "hello"}}]},
        })],
    );
    let executor = make_executor(Arc::clone(&inv), &plugin_ids);
    let compiled = compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids())
        .expect("真 roleplay.yaml 必须通过编译校验");
    let final_state = executor
        .run_compiled(&compiled, json!({"session_id": "s1"}))
        .await
        .expect("run 成功");
    assert_eq!(inv.count("pipeline_llm_core"), 1);
    assert_eq!(inv.count("pipeline_tool_core"), 0, "纯文本轮零工具迭代");
    assert_eq!(final_state["ended"], json!(true), "纯文本轮即终局");
}

#[tokio::test]
async fn roleplay_yaml_tool_round_executes_then_ends_without_llm() {
    // 工具轮：执行过的迭代跳过 llm（旧行为「执行完即终局、结果不回灌」）。
    let config = load_pipeline("roleplay");
    let plugin_ids = collect_plugin_refs(&config);
    let inv = Arc::new(ScriptedInvoker::default());
    inv.script(
        "pipeline_llm_core",
        vec![json!({
            "raw_tool_calls": [{"name": "bash", "id": "c1", "args": {}}],
            "raw_result": "calling",
            "messages": {"_ops": [{"op": "set", "msg": {"role": "assistant", "content": "calling"}}]},
        })],
    );
    inv.script("pipeline_tool_core", vec![tool_core_updates()]);
    let executor = make_executor(Arc::clone(&inv), &plugin_ids);
    let compiled = compile_pipeline(&config, &StepLibrary::default(), &executor.plugin_ids())
        .expect("compile");
    let final_state = executor
        .run_compiled(&compiled, json!({"session_id": "s1"}))
        .await
        .expect("run 成功");
    assert_eq!(
        inv.count("pipeline_llm_core"),
        1,
        "工具执行过的迭代不得再进 llm"
    );
    assert_eq!(inv.count("pipeline_tool_core"), 1);
    assert_eq!(final_state["raw_tool_calls"], json!([]));
    assert_eq!(final_state["ended"], json!(true), "工具执行完本轮即终局");
}
