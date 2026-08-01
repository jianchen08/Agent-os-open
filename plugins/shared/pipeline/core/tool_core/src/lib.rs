//! # tool_core 原生插件（cdylib，abi_stable trait 对象）
//!
//! 取代 Python 边车 `pipeline_tool_core`。经 `ctx.host`（HostServices trait 对象）
//! 调内核 capability（tool-executor / event-bus），与 sidecar 走同一 router，去掉
//! tool_core 自身 MCP 进/出 2 跳。
//!
//! 业务代码零 unsafe——所有 FFI 由 abi_stable 在 trait 边界处理。

use std::collections::HashMap;

use abi_stable::export_root_module;
use abi_stable::prefix_type::PrefixTypeTrait;
use abi_stable::std_types::{ROption, RResult, RString};
use agentos_native_sdk::{
    create_plugin_value, HostServicesBox, NativePluginModule, NativePluginModule_Ref,
    PipelinePlugin, PipelinePlugin_TO, PluginCtx,
};
use serde_json::{json, Value};

mod json_repair;
mod messages;
mod types;

use types::{StateKey, ToolCall, ToolResult};

/// tool_core 插件实例。
pub struct ToolCore;

impl PipelinePlugin for ToolCore {
    fn execute(&self, ctx: &PluginCtx) -> RResult<RString, RString> {
        let state = ctx.state_value();
        // 执行主流程（返回 state_updates HashMap）。
        let updates = run(&state, &ctx.host);
        // 序列化为 JSON 字符串（trait 边界用 RString 传递）。
        match serde_json::to_string(&updates) {
            Ok(s) => RResult::ROk(RString::from(s)),
            Err(e) => RResult::RErr(RString::from(format!("serialize state_updates: {e}"))),
        }
    }
}

/// 构造函数（extern "C"，供 RootModule 函数指针字段）。
extern "C" fn create_tool_core() -> PipelinePlugin_TO<'static, abi_stable::std_types::RBox<()>> {
    create_plugin_value(ToolCore)
}

/// 导出 root module。
#[export_root_module]
pub fn get_library() -> NativePluginModule_Ref {
    NativePluginModule {
        create_plugin: create_tool_core,
    }
    .leak_into_prefix()
}

/// 执行工具调用核心流程（对齐 plugin.py:609-1050）。
///
/// 返回 state_updates HashMap（由 trait 实现序列化为 JSON）。
fn run(state: &Value, host: &ROption<HostServicesBox>) -> HashMap<String, Value> {
    let tool_calls_raw = state.get(StateKey::RAW_TOOL_CALLS).and_then(|v| v.as_array());

    let tool_calls_raw = match tool_calls_raw {
        Some(arr) if !arr.is_empty() => arr,
        _ => {
            // raw_tool_calls 为空或非数组：空转返回（对齐 plugin.py:613-619）。
            let mut m = HashMap::new();
            m.insert(StateKey::RAW_RESULT.into(), Value::String("No tool calls to execute".into()));
            m.insert(StateKey::RAW_ERROR.into(), Value::Null);
            m.insert(StateKey::RAW_TOOL_CALLS.into(), json!([]));
            m.insert(StateKey::TOOL_RESULTS.into(), json!([]));
            return m;
        }
    };

    let mut results: Vec<ToolResult> = Vec::with_capacity(tool_calls_raw.len());
    let mut last_result_text = String::new();

    for raw in tool_calls_raw {
        let tc = match ToolCall::parse(raw) {
            Ok(tc) => tc,
            Err(fail_result) => {
                last_result_text = format!("Error: {}", fail_result.error.as_deref().unwrap_or("unknown"));
                results.push(fail_result);
                continue;
            }
        };

        // 拦截检查（对齐 plugin.py:698 _check_tool_blocked）。
        if let Some(blocked) = types::check_tool_blocked(&tc.name, state) {
            emit_tool_event(host, state, "tool_start", &tc, None);
            emit_tool_event(host, state, "tool_result", &tc, Some(&blocked));
            last_result_text = format!("Error: {}", blocked.error.as_deref().unwrap_or("unknown"));
            results.push(blocked);
            continue;
        }

        // 执行工具（经 ctx.host 调内核 tool-executor.invoke）。
        let result = execute_single_tool(&tc, host, state);
        last_result_text = result_text_for(&result);
        results.push(result);
    }

    // messages 重建（OpenAI 规范，对齐 plugin.py:784-859）。
    let current_messages = messages::rebuild(state, tool_calls_raw, &results);

    // 多模态图片收集 + 注入（对齐 plugin.py:861-963）。
    let (current_messages, _mm_emitted) =
        messages::inject_multimodal(state, current_messages, &results, host);

    // state_updates 组装（对齐 plugin.py:1019-1050）。
    let mut updates: HashMap<String, Value> = HashMap::new();
    updates.insert(StateKey::TOOL_RESULTS.into(), json!(results));
    updates.insert(StateKey::RAW_RESULT.into(), Value::String(last_result_text));
    updates.insert(StateKey::RAW_TOOL_CALLS.into(), json!([]));
    updates.insert("_executed_tool_calls".into(), json!(tool_calls_raw));
    updates.insert("messages".into(), json!(current_messages));

    // 聚合错误 + 任务级副作用（对齐 plugin.py:965-1017）。
    collect_side_effects(state, &results, &mut updates);

    updates
}

/// 执行单个工具（对齐 plugin.py _execute_single_tool）。
///
/// 经 ctx.host 调内核 `tool-executor.invoke`，内核反查 tool_name → plugin_id
/// 调对应工具 sidecar，返回 ToolExecutionResult。发 tool_start/tool_result 事件。
///
/// 会话身份注入（治理缺口修复）：从 state 提取 session_id/thread_id 拼接为
/// `_owner` 注入工具参数——bash 等有状态工具凭此区分跨会话的 pid 级操作
/// （continue/input/terminate/read_log 越权校验），防止多会话互相劫持。
fn execute_single_tool(tc: &ToolCall, host: &ROption<HostServicesBox>, state: &Value) -> ToolResult {
    // 发 tool_start 事件（host 为 RNone 时 emit 内部跳过）。
    emit_tool_event(host, state, "tool_start", tc, None);

    let start = std::time::Instant::now();

    // 会话身份（owner）：session_id/thread_id 均有时拼接（线程级隔离更细），
    // 仅其一存在时用它。两者都缺 → 不注入（插件侧走宽松兜底）。
    let session_id = state.get("session_id").and_then(|v| v.as_str()).unwrap_or("");
    let thread_id = state.get("thread_id").and_then(|v| v.as_str()).unwrap_or("");
    let owner = match (session_id, thread_id) {
        (s, t) if !s.is_empty() && !t.is_empty() => format!("{s}/{t}"),
        (_, t) if !t.is_empty() => t.to_string(),
        (s, _) if !s.is_empty() => s.to_string(),
        _ => String::new(),
    };

    let result = match host {
        ROption::RSome(h) => {
            let mut args = tc.args.clone();
            if args.is_object() && !owner.is_empty() {
                args["_owner"] = json!(owner);
            }
            let invoke_params = json!({
                "tool_name": tc.name,
                "args": args,
            });
            let params_json = serde_json::to_string(&invoke_params).unwrap_or_else(|_| "{}".into());
            // 调内核 tool-executor.invoke（经 HostServices → CapabilityRouter）。
            match h.call_capability(
                RString::from("tool-executor"),
                RString::from("invoke"),
                RString::from(params_json),
            ) {
                RResult::ROk(resp_json) => {
                    let resp: Value = serde_json::from_str(resp_json.as_str()).unwrap_or(json!({}));
                    parse_tool_executor_response(&tc.name, resp, start.elapsed().as_secs_f64() * 1000.0)
                }
                RResult::RErr(e) => ToolResult::failed(
                    &tc.name,
                    &format!("host capability call failed: {}", e.as_str()),
                    start.elapsed().as_secs_f64() * 1000.0,
                ),
            }
        }
        ROption::RNone => ToolResult::failed(
            &tc.name,
            "host unavailable (capability channel not injected)",
            0.0,
        ),
    };

    // 发 tool_result 事件。
    emit_tool_event(host, state, "tool_result", tc, Some(&result));
    result
}

/// 解析 tool-executor.invoke 的响应（对齐内核 ToolExecutionResult + Python normalize）。
fn parse_tool_executor_response(tool_name: &str, resp: Value, duration_ms: f64) -> ToolResult {
    let success = resp.get("success").and_then(|v| v.as_bool()).unwrap_or(false);
    if success {
        let data = resp.get("data").cloned().unwrap_or(Value::Null);
        ToolResult::succeeded(tool_name, data, duration_ms)
    } else {
        let error = resp
            .get("error")
            .and_then(|v| v.as_str())
            .unwrap_or("tool execution failed");
        ToolResult::failed(tool_name, error, duration_ms)
    }
}

/// 收集任务级副作用，写进 state_updates（对齐 plugin.py:965-1017）。
fn collect_side_effects(state: &Value, results: &[ToolResult], updates: &mut HashMap<String, Value>) {
    // 聚合错误（all_failed / has_task_failed）。
    let all_failed = !results.is_empty() && results.iter().all(|r| !r.success);
    let mut raw_error: Option<String> = None;
    if all_failed {
        let summary = results
            .iter()
            .map(|r| format!("{}: {}", r.tool_name, r.error.as_deref().unwrap_or("unknown")))
            .collect::<Vec<_>>()
            .join("; ");
        raw_error = Some(format!("所有工具执行失败: {summary}"));
    }

    let mut has_task_failed = false;
    for r in results {
        if let Some(data) = r.data.as_object() {
            if let Some(meta) = data.get("metadata").and_then(|m| m.as_object()) {
                if meta.get("task_failed").and_then(|v| v.as_bool()).unwrap_or(false) {
                    has_task_failed = true;
                    if raw_error.is_none() {
                        raw_error = Some(
                            data.get("error")
                                .and_then(|v| v.as_str())
                                .unwrap_or("任务系统级失败")
                                .to_string(),
                        );
                    }
                    break;
                }
            }
        }
    }
    if let Some(err) = raw_error {
        updates.insert(StateKey::RAW_ERROR.into(), Value::String(err));
    }

    // submitted_task_ids / latest_task / evaluation / conversation。
    let mut submitted_task_ids: Vec<Value> = state
        .get("submitted_task_ids")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let mut evaluation_completed = false;
    let mut conversation_activated = false;
    let mut latest_task_id = String::new();
    let mut latest_task_workspace = String::new();

    for r in results {
        if !r.success {
            continue;
        }
        let Some(data) = r.data.as_object() else { continue };
        let meta = data
            .get("metadata")
            .and_then(|m| m.as_object())
            .or_else(|| r.metadata.as_ref().and_then(|m| m.as_object()));

        if let Some(meta) = meta {
            let action = meta.get("action").and_then(|v| v.as_str()).unwrap_or("");
            if action == "task_submit" || action == "task_submit_container" {
                if let Some(tid) = data.get("task_id").and_then(|v| v.as_str()) {
                    if !submitted_task_ids.iter().any(|v| v.as_str() == Some(tid)) {
                        submitted_task_ids.push(Value::String(tid.to_string()));
                    }
                    latest_task_id = tid.to_string();
                }
                let ws = data
                    .get("resolved_workspace")
                    .and_then(|v| v.as_str())
                    .or_else(|| data.get("workspace").and_then(|v| v.as_str()));
                if let Some(ws) = ws {
                    latest_task_workspace = ws.to_string();
                }
            }
        }

        if r.tool_name == "task_evaluate" {
            if let Some(meta) = meta {
                if meta.get("result").and_then(|v| v.as_str()) == Some("completed") {
                    evaluation_completed = true;
                }
            }
        }

        if r.tool_name == "human_interaction" {
            let mut conv_flag = data.get("conversation_mode").is_some();
            if !conv_flag {
                for key in &["output", "data"] {
                    if let Some(inner) = data.get(*key).and_then(|v| v.as_object()) {
                        if inner.contains_key("conversation_mode") {
                            conv_flag = true;
                            break;
                        }
                    }
                }
            }
            if conv_flag {
                conversation_activated = true;
            }
        }
    }

    if !submitted_task_ids.is_empty() {
        updates.insert("submitted_task_ids".into(), Value::Array(submitted_task_ids));
    }
    if !latest_task_id.is_empty() && !state.as_object().map(|o| o.contains_key("task_id")).unwrap_or(false) {
        updates.insert("task_id".into(), Value::String(latest_task_id));
    }
    if !latest_task_workspace.is_empty()
        && !state.as_object().map(|o| o.contains_key("workspace")).unwrap_or(false)
    {
        updates.insert("workspace".into(), Value::String(latest_task_workspace));
    }
    if evaluation_completed {
        updates.insert("task_evaluation_completed".into(), Value::Bool(true));
    }
    if has_task_failed {
        updates.insert(StateKey::ENDED.into(), Value::Bool(true));
    }
    if conversation_activated {
        updates.insert(
            "_pending_route_signal".into(),
            json!({
                "route_type": "wait",
                "reason": "human_interaction: user arrived, entering conversation",
            }),
        );
    }
}

/// 取工具结果的预览文本（用于 raw_result，对齐 plugin.py last_result_text）。
fn result_text_for(r: &ToolResult) -> String {
    if r.success {
        let preview = if r.data.is_object() {
            messages::serialize_for_content(&r.data).unwrap_or_else(|_| r.data.to_string())
        } else {
            r.data.to_string()
        };
        preview.chars().take(200).collect()
    } else {
        format!("Error: {}", r.error.as_deref().unwrap_or("unknown"))
    }
}

/// 发送工具事件到前端（经 ctx.host → event-bus.emit）。
///
/// `kind` = "tool_start" | "tool_result"。`result` 为 None 时发 start，Some 时发 result。
fn emit_tool_event(
    host: &ROption<HostServicesBox>,
    state: &Value,
    kind: &str,
    tc: &ToolCall,
    result: Option<&ToolResult>,
) {
    let ROption::RSome(host) = host else {
        return;
    };

    // 路由字段（前端 resolvePipelineId/extractMessageId 硬门控）。
    let thread_id = state
        .get("session_id")
        .and_then(|v| v.as_str())
        .or_else(|| state.get("thread_id").and_then(|v| v.as_str()))
        .unwrap_or("");
    let pipeline_id = state.get("pipeline_id").and_then(|v| v.as_str()).unwrap_or("");
    let message_id = state.get("message_id").and_then(|v| v.as_str()).unwrap_or("");

    let mut payload = serde_json::Map::new();
    payload.insert("thread_id".into(), Value::String(thread_id.into()));
    payload.insert("pipeline_id".into(), Value::String(pipeline_id.into()));
    payload.insert("message_id".into(), Value::String(message_id.into()));
    payload.insert("call_id".into(), Value::String(tc.call_id.clone().unwrap_or_default()));
    payload.insert("tool_name".into(), Value::String(tc.name.clone()));

    if kind == "tool_start" {
        payload.insert("args".into(), tc.args.clone());
    } else if let Some(r) = result {
        let preview = if r.success {
            messages::serialize_for_content(&r.data).unwrap_or_else(|_| r.data.to_string())
        } else {
            r.error.clone().unwrap_or_default()
        };
        payload.insert("result".into(), Value::String(preview.chars().take(200).collect()));
        payload.insert("result_data".into(), r.data.clone());
        payload.insert("success".into(), Value::Bool(r.success));
        payload.insert("duration_ms".into(), json!((r.duration_ms * 10.0).round() / 10.0));
        if let Some(err) = &r.error {
            payload.insert("error".into(), Value::String(err.clone()));
        }
    }

    let params = json!({
        "event": kind,
        "payload": Value::Object(payload),
    });
    let params_json = serde_json::to_string(&params).unwrap_or_default();
    // fire-and-forget：忽略返回（event-bus.emit 不需要结果）。
    let _ = host.call_capability(
        RString::from("event-bus"),
        RString::from("emit"),
        RString::from(params_json),
    );
}
