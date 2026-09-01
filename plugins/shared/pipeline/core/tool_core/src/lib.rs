//! # tool_core 原生插件（cdylib，直接 trait 对象）
//!
//! 取代 Python 边车 `pipeline_tool_core`。经 `ectx.host`（HostServices trait 对象）
//! 调内核 capability（tool-executor / event-bus），与 sidecar 走同一 router，去掉
//! tool_core 自身 MCP 进/出 2 跳。
//!
//! 业务代码零 unsafe——所有 FFI 由 native-sdk 的 plugin_into_raw 封装。

use std::collections::HashMap;

use agentos_native_sdk::{
    plugin_into_raw, ExecContext, HostServices, PipelinePlugin,
};
use serde_json::{json, Value};

mod json_repair;
mod messages;
mod output_validate;
mod types;

use types::{StateKey, ToolCall, ToolResult};

/// tool_core 插件实例。
///
/// 跨分配器契约：`execute` 的返回 JSON 存放于 [`ToolCore::out_buf`](内部缓冲，
/// dll 堆)，以 `&str` 借给内核——内核立即拷贝消费，缓冲分配/释放都在 dll 堆。
/// 线程安全：内核侧 execute 在 blocking 线程**串行**调用（单实例不重入），
/// `UnsafeCell` 写读不重叠；`Send+Sync` 显式承诺成立（无并发写）。
pub struct ToolCore {
    /// execute 结果缓冲（借给内核读，下一次 execute 前有效）。
    out_buf: std::cell::UnsafeCell<String>,
}

impl Default for ToolCore {
    fn default() -> Self {
        Self::new()
    }
}

impl ToolCore {
    pub fn new() -> Self {
        Self {
            out_buf: std::cell::UnsafeCell::new(String::with_capacity(64 * 1024)),
        }
    }
}

// SAFETY: out_buf 仅在 execute（内核 blocking 线程串行调用，单实例不重入）
// 的 &self 独占借用期间写入；借出的 &str 由调用方同步拷贝消费，无并发写面。
unsafe impl Send for ToolCore {}
unsafe impl Sync for ToolCore {}

impl PipelinePlugin for ToolCore {
    fn execute(&self, ectx: &ExecContext) -> Result<&str, String> {
        let state = ectx.ctx.state_value();
        // 执行主流程（返回 state_updates HashMap）。
        let updates = run(&state, ectx.host);
        // 序列化进自持缓冲，返回借用（跨分配器契约：不返回 String）。
        let json = serde_json::to_string(&updates).map_err(|e| format!("serialize state_updates: {e}"))?;
        let buf = unsafe { &mut *self.out_buf.get() };
        *buf = json;
        Ok(buf.as_str())
    }
}

/// 构造函数（extern "C"）：内核 dlopen 后调它拿 trait 对象裸指针。
#[no_mangle]
pub extern "C" fn agentos_plugin_create() -> *mut () {
    plugin_into_raw(ToolCore::new())
}

/// 执行工具调用核心流程（对齐 plugin.py:609-1050）。
///
/// 返回 state_updates HashMap。
fn run(state: &Value, host: Option<&dyn HostServices>) -> HashMap<String, Value> {
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

        // 执行工具（经 host 调内核 tool-executor.invoke）。
        let result = execute_single_tool(&tc, host, state);
        last_result_text = result_text_for(&result);
        results.push(result);
    }

    // messages 重建（OpenAI 规范，对齐 plugin.py:784-859）。
    // 记录重建前的 messages 长度，用于识别本插件新增的消息（assistant tool_calls[若补造] + tool 结果）。
    let original_len = state
        .get("messages")
        .and_then(|v| v.as_array())
        .map(|a| a.len())
        .unwrap_or(0);
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
    // op-based：只 emit 新增消息的 set op（无 seq → 引擎分配递增 seq + 落 message_slots）。
    // 旧表仍由 project_messages 投影（读侧暂未切换，行为不变）。
    if current_messages.len() > original_len {
        let new_msgs = &current_messages[original_len..];
        let ops: Vec<Value> = new_msgs.iter().map(|m| json!({ "op": "set", "msg": m })).collect();
        updates.insert("messages".into(), json!({ "_ops": ops }));
    }

    // 聚合错误 + 任务级副作用（对齐 plugin.py:965-1017）。
    collect_side_effects(state, &results, &mut updates);

    updates
}

/// 执行单个工具（对齐 plugin.py _execute_single_tool）。
///
/// 经 host 调内核 `tool-executor.invoke`，内核反查 tool_name → plugin_id
/// 调对应工具 sidecar，返回 ToolExecutionResult。发 tool_start/tool_result 事件。
fn execute_single_tool(tc: &ToolCall, host: Option<&dyn HostServices>, state: &Value) -> ToolResult {
    // 发 tool_start 事件（host 为 None 时 emit 内部跳过）。
    emit_tool_event(host, state, "tool_start", tc, None);

    let start = std::time::Instant::now();

    let result = match host {
        Some(h) => {
            let invoke_params = build_invoke_params(tc, state);
            let params_json = serde_json::to_string(&invoke_params).unwrap_or_else(|_| "{}".into());
            // 跨分配器契约：返回值是 exe 侧缓冲的 &str 借用（不持有、不释放，
            // 立即转 Value 消费内完成）。旧 `String` 返回会把 exe 分配的串交
            // dll drop = 反方向跨堆 free UB（2026-09-01 真机本崩点）。
            match h.call_capability("tool-executor", "invoke", &params_json) {
                Ok(resp_json) => {
                    let resp: Value = serde_json::from_str(resp_json).unwrap_or(json!({}));
                    parse_tool_executor_response(&tc.name, resp, start.elapsed().as_secs_f64() * 1000.0)
                }
                Err(e) => ToolResult::failed(
                    &tc.name,
                    &format!("host capability call failed: {e}"),
                    start.elapsed().as_secs_f64() * 1000.0,
                ),
            }
        }
        None => ToolResult::failed(
            &tc.name,
            "host unavailable (capability channel not injected)",
            0.0,
        ),
    };

    // output_schema 消费端（task_dsh_plugin_adapter 任务 1）：成功结果按内核注入的
    // tool_output_contracts 校验，违规 fail-closed 转失败——错误带回 LLM 自我修正，
    // 插件错误由引擎统一 warn+继续（ADR 2026-08-18）。放在 tool_result 事件发送前，
    // 保证冷热一致（事件里的 success/error 与持久化 tool_result 同源）。DSH tools/
    // post-execute 兜底语义的对应实现，见 docs/dsh_hook_translation.md。
    let mut result = result;
    if result.success && output_validate::validation_enabled(state) {
        if let Some(err) = validate_output_contract(state, &tc.name, &result.data) {
            result = ToolResult::failed(
                &tc.name,
                &format!("output_schema validation failed: {err}"),
                result.duration_ms,
            );
        }
    }

    // 发 tool_result 事件。
    emit_tool_event(host, state, "tool_result", tc, Some(&result));
    result
}

/// 构造 tool-executor.invoke 参数。
///
/// `_call_context`（task_observability 任务 2）：本次调用的前端路由键
/// （call_id/pipeline_id/message_id/thread_id），内核透传给工具 sidecar，
/// 供 bash 等长任务工具执行中经 frontend.emit 推 tool_progress 进度。
fn build_invoke_params(tc: &ToolCall, state: &Value) -> Value {
    let thread_id = state
        .get("session_id")
        .and_then(|v| v.as_str())
        .or_else(|| state.get("thread_id").and_then(|v| v.as_str()))
        .unwrap_or("");
    let pipeline_id = state.get("pipeline_id").and_then(|v| v.as_str()).unwrap_or("");
    let message_id = state.get("message_id").and_then(|v| v.as_str()).unwrap_or("");
    json!({
        "tool_name": tc.name,
        "args": tc.args,
        "_call_context": {
            "call_id": tc.call_id.clone().unwrap_or_default(),
            "pipeline_id": pipeline_id,
            "message_id": message_id,
            "thread_id": thread_id,
        },
    })
}

/// 解析 tool-executor.invoke 的响应（对齐内核 ToolExecutionResult + Python normalize）。
fn parse_tool_executor_response(tool_name: &str, resp: Value, duration_ms: f64) -> ToolResult {
    let success = resp.get("success").and_then(|v| v.as_bool()).unwrap_or(false);
    if success {
        let data = resp.get("data").cloned().unwrap_or(Value::Null);
        let mut result = ToolResult::succeeded(tool_name, data, duration_ms);
        // 信封顶层 metadata（task_failed / result=completed 等副作用信号）
        // 由内核 ToolExecutionResult.metadata 透传，collect_side_effects 消费。
        result.metadata = resp.get("metadata").cloned();
        result
    } else {
        let error = resp
            .get("error")
            .and_then(|v| v.as_str())
            .unwrap_or("tool execution failed");
        ToolResult::failed(tool_name, error, duration_ms)
    }
}

/// 取工具输出契约并校验（无契约/无 schema 返回 None = 通过）。
///
/// 契约由 tool_schema 插件经 tool-surface capability 写入 state["tool_output_contracts"]
/// （tool_name → {schema, render}）；render 供前端路由，schema 供本处校验。
fn validate_output_contract(state: &Value, tool_name: &str, data: &Value) -> Option<String> {
    let contract = state
        .get("tool_output_contracts")?
        .get(tool_name)?;
    let schema = contract.get("schema")?;
    if schema.is_null() {
        return None;
    }
    output_validate::validate(schema, data)
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

/// 发送工具事件到前端（经 host → event-bus.emit）。
///
/// `kind` = "tool_start" | "tool_result"。`result` 为 None 时发 start，Some 时发 result。
fn emit_tool_event(
    host: Option<&dyn HostServices>,
    state: &Value,
    kind: &str,
    tc: &ToolCall,
    result: Option<&ToolResult>,
) {
    let Some(host) = host else {
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
        // result 为全量结果文本（冷热一致性契约）：与持久化 content 同源——
        // 成功 = serialize_for_content 全文，失败 = "Error: {error}"（对齐
        // messages.rs:110）。不再截断 200 字符，前端实时与刷新后读到的文本一致。
        let result_text = if r.success {
            messages::serialize_for_content(&r.data).unwrap_or_else(|_| r.data.to_string())
        } else {
            format!("Error: {}", r.error.as_deref().unwrap_or("unknown"))
        };
        payload.insert("result".into(), Value::String(result_text));
        payload.insert("result_data".into(), r.data.clone());
        payload.insert("success".into(), Value::Bool(r.success));
        payload.insert("duration_ms".into(), json!((r.duration_ms * 10.0).round() / 10.0));
        if let Some(err) = &r.error {
            // 统一错误信封（单一真值源 config/error_codes.json）：前端按
            // source 渲染来源标签、按 retryable 驱动重试。持久化 envelope
            // （messages.rs）保持字符串，REST 历史消息契约不变。
            payload.insert(
                "error".into(),
                json!({
                    "code": "TOOL_EXEC_FAILED",
                    "message": err,
                    "source": "plugin",
                    "retryable": false,
                    "details": null,
                    "request_id": null,
                }),
            );
        }
    }

    let params = json!({
        "event": kind,
        "payload": Value::Object(payload),
    });
    let params_json = serde_json::to_string(&params).unwrap_or_default();
    // fire-and-forget：忽略返回（event-bus.emit 不需要结果）。响应走借用缓冲
    // （跨分配器契约：exe 分配的 String 不跨界 drop）。
    // fire-and-forget：忽略返回（event-bus.emit 不需要结果）。返回串是 exe 侧
    // 缓冲借用（跨分配器契约），不存引用、不释放。
    let _ = host.call_capability("event-bus", "emit", &params_json);
}

#[cfg(test)]
mod tests {
    #[test]
    fn test_collect_side_effects_evaluation_completed_via_envelope_metadata() {
        // 内核信封顶层 metadata（result=completed）→ task_evaluation_completed
        // 投影键写入 state_updates（task_reminder 三信号②的证据源）。
        let state = json!({});
        let result = parse_tool_executor_response(
            "task_evaluate",
            json!({
                "success": true,
                "data": {"overall_passed": true, "task_id": "t1"},
                "metadata": {"action": "auto_complete", "result": "completed"},
            }),
            1.0,
        );
        let mut updates = HashMap::new();
        collect_side_effects(&state, &[result], &mut updates);
        assert_eq!(
            updates.get("task_evaluation_completed"),
            Some(&Value::Bool(true))
        );
    }

    use super::*;

    /// 捕获 host 调用的最小 mock（记录最后一次 call_capability 参数）。
    struct CapturingHost {
        last_params: std::sync::Mutex<Option<String>>,
    }

    impl HostServices for CapturingHost {
        fn call_capability(
            &self,
            _capability: &str,
            _method: &str,
            params_json: &str,
        ) -> Result<&str, String> {
            *self.last_params.lock().unwrap() = Some(params_json.to_string());
            Ok("{}")  // 借用协议：'static 字面量即实现方持有的 &str
        }
    }

    /// 统一错误模型：tool_result 失败事件的 error 为信封对象
    /// （code=TOOL_EXEC_FAILED, source=plugin, retryable=false），
    /// 前端据此渲染来源标签；成功事件不带 error 字段。
    #[test]
    fn test_emit_tool_event_failure_error_envelope() {
        let host = CapturingHost {
            last_params: std::sync::Mutex::new(None),
        };
        let state = json!({
            "session_id": "sess-1",
            "pipeline_id": "pipe-1",
            "message_id": "msg-1",
        });
        let tc = ToolCall {
            name: "bash_execute".into(),
            args: json!({"command": "echo hi"}),
            call_id: Some("call_abc".into()),
        };
        let result = ToolResult::failed("bash_execute", "command not found", 1.5);

        emit_tool_event(Some(&host), &state, "tool_result", &tc, Some(&result));

        let params: Value =
            serde_json::from_str(host.last_params.lock().unwrap().as_deref().unwrap()).unwrap();
        let payload = &params["payload"];
        assert_eq!(payload["success"], false);
        assert_eq!(payload["error"]["code"], "TOOL_EXEC_FAILED");
        assert_eq!(payload["error"]["message"], "command not found");
        assert_eq!(payload["error"]["source"], "plugin");
        assert_eq!(payload["error"]["retryable"], false);
        assert!(payload["error"]["details"].is_null());
        assert!(payload["error"]["request_id"].is_null());
    }

    /// 成功路径不带 error 字段（前端按 success=true 渲染成功态）。
    #[test]
    fn test_emit_tool_event_success_has_no_error() {
        let host = CapturingHost {
            last_params: std::sync::Mutex::new(None),
        };
        let state = json!({
            "session_id": "sess-1",
            "pipeline_id": "pipe-1",
            "message_id": "msg-1",
        });
        let tc = ToolCall {
            name: "bash_execute".into(),
            args: json!({"command": "echo hi"}),
            call_id: Some("call_abc".into()),
        };
        let result = ToolResult::succeeded("bash_execute", json!({"status": "ok"}), 0.5);

        emit_tool_event(Some(&host), &state, "tool_result", &tc, Some(&result));

        let params: Value =
            serde_json::from_str(host.last_params.lock().unwrap().as_deref().unwrap()).unwrap();
        let payload = &params["payload"];
        assert_eq!(payload["success"], true);
        assert!(payload.get("error").is_none());
    }

    /// task_observability 任务 2：invoke 参数必须携带 _call_context 路由键，
    /// bash 等长任务工具据此经 frontend.emit 推 tool_progress 进度。
    #[test]
    fn test_build_invoke_params_contains_call_context() {
        let state = json!({
            "session_id": "sess-1",
            "pipeline_id": "pipe-1",
            "message_id": "msg-1",
        });
        let tc = ToolCall {
            name: "bash_execute".into(),
            args: json!({"command": "echo hi"}),
            call_id: Some("call_abc".into()),
        };
        let params = build_invoke_params(&tc, &state);
        assert_eq!(params["tool_name"], "bash_execute");
        assert_eq!(params["args"]["command"], "echo hi");
        assert_eq!(params["_call_context"]["call_id"], "call_abc");
        assert_eq!(params["_call_context"]["pipeline_id"], "pipe-1");
        assert_eq!(params["_call_context"]["message_id"], "msg-1");
        assert_eq!(params["_call_context"]["thread_id"], "sess-1");
    }

    /// 路由键缺失时的兜底：空字符串（内核/前端门控丢弃，不 panic）。
    #[test]
    fn test_build_invoke_params_empty_context_defaults() {
        let state = json!({});
        let tc = ToolCall {
            name: "file_read".into(),
            args: json!({}),
            call_id: None,
        };
        let params = build_invoke_params(&tc, &state);
        assert_eq!(params["_call_context"]["call_id"], "");
        assert_eq!(params["_call_context"]["thread_id"], "");
    }

    /// task_dsh_plugin_adapter 任务 1：声明了 output_schema 的工具，违规数据
    /// 被 fail-closed 拦截；无契约/开关关闭时放行。
    #[test]
    fn test_validate_output_contract_fail_closed() {
        let state = json!({
            "tool_output_contracts": {
                "bash_execute": {
                    "schema": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                        "required": ["status"]
                    },
                    "render": {"card": "terminal"}
                }
            }
        });
        // 违规：缺 required 字段。
        let err = validate_output_contract(&state, "bash_execute", &json!({"pid": 1}));
        assert!(err.unwrap().contains("missing required field `status`"));
        // 合规放行。
        assert_eq!(validate_output_contract(&state, "bash_execute", &json!({"status": "completed"})), None);
        // 未声明契约的工具放行（存量 41 工具零负担）。
        assert_eq!(validate_output_contract(&state, "file_read", &json!("anything")), None);
        // 契约存在但 schema 为 null（仅 render）放行。
        let render_only = json!({"tool_output_contracts": {"x": {"schema": null, "render": {"card": "read"}}}});
        assert_eq!(validate_output_contract(&render_only, "x", &json!({})), None);
    }

    /// 校验开关：tool_output_validation == "off" 时整体跳过。
    #[test]
    fn test_validation_switch_off() {
        assert!(!output_validate::validation_enabled(&json!({"tool_output_validation": "off"})));
        assert!(output_validate::validation_enabled(&json!({})));
    }

    // ══ check_tool_blocked 契约（从 Python TestCheckToolBlocked 迁移，0.2 native 化）══

    /// level_guard 拦截 → 失败结果含权限原因；同 blocked_tools 外的工具不拦截。
    #[test]
    fn test_level_guard_block_returns_failure() {
        let state = json!({
            "security.level_decision": {
                "allowed": false,
                "reason": "Agent level L1 not allowed to call: file_write",
                "blocked_tools": ["file_write"],
            },
        });
        let r = types::check_tool_blocked("file_write", &state);
        assert!(r.is_some());
        let r = r.unwrap();
        assert!(!r.success);
        assert!(r.error.unwrap_or_default().contains("权限"));
        // 不在 blocked_tools 的工具不受影响。
        assert!(types::check_tool_blocked("file_read", &state).is_none());
    }

    /// blocked_tools 缺失 = 全拦（fail-closed）。
    #[test]
    fn test_level_guard_missing_blocked_tools_blocks_all() {
        let state = json!({
            "security.level_decision": {"allowed": false, "reason": "deny all"},
        });
        let r = types::check_tool_blocked("file_write", &state);
        assert!(r.is_some());
        assert!(!r.unwrap().success);
    }

    /// isolation_guard 拦截 → 失败结果含隔离原因。
    #[test]
    fn test_isolation_block_returns_failure() {
        let state = json!({
            "execution_contexts": [
                {"tool_name": "bash_execute", "provider": "denied",
                 "blocked": true, "reason": "policy_fallback_denied"},
            ],
        });
        let r = types::check_tool_blocked("bash_execute", &state);
        assert!(r.is_some());
        let r = r.unwrap();
        assert!(!r.success);
        assert!(r.error.unwrap_or_default().contains("隔离"));
        // 同 context 内其它工具不受影响。
        assert!(types::check_tool_blocked("file_read", &state).is_none());
    }

    /// security_check 拦截 → 失败结果含安全原因。
    #[test]
    fn test_security_check_block_returns_failure() {
        let state = json!({
            "security.decision": {"allowed": false, "reason": "危险操作 rm -rf /"},
        });
        let r = types::check_tool_blocked("bash_execute", &state);
        assert!(r.is_some());
        let r = r.unwrap();
        assert!(!r.success);
        assert!(r.error.unwrap_or_default().contains("安全"));
    }

    /// 无拦截决策 / allowed=true → None（正常执行）。
    #[test]
    fn test_no_block_decision_returns_none() {
        assert!(types::check_tool_blocked("file_read", &json!({})).is_none());
        let allowed = json!({
            "security.level_decision": {"allowed": true, "reason": "ok"},
            "security.decision": {"allowed": true, "reason": "ok"},
        });
        assert!(types::check_tool_blocked("file_write", &allowed).is_none());
    }
}
