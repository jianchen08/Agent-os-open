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
    fn execute(&self, ectx: &ExecContext) -> Result<&str, &str> {
        let state = ectx.ctx.state_value();
        // 执行主流程（返回 state_updates HashMap）。
        let updates = run(&state, &ectx.ctx.config_value(), ectx.host);
        let buf = unsafe { &mut *self.out_buf.get() };
        // Err 分支同契约（2026-09-02 收口）：序列化错误也写自持缓冲借 &str——
        // 旧 `?` 上抛 `format!` 的 String（dll 堆）交内核 drop = 跨堆 free UB。
        match serde_json::to_string(&updates) {
            Ok(json) => {
                *buf = json;
                Ok(buf.as_str())
            }
            Err(e) => {
                *buf = format!("serialize state_updates: {e}");
                Err(buf.as_str())
            }
        }
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
/// 工具输出超长截断（2026-09-09 用户裁定）：头尾保留 + 中部省略标记。
/// 全文经 `_full_tool_results` 交 tool_cache_writer 写缓存；LLM/前端要全文
/// 重发同一调用即可命中缓存取回（缓存 TTL 内），无需新基建。
const TOOL_OUTPUT_MAX_CHARS: usize = 16 * 1024;
const TOOL_OUTPUT_HEAD_CHARS: usize = 12 * 1024;
const TOOL_OUTPUT_TAIL_CHARS: usize = 2 * 1024;

/// 截断参数（manifest config_files → config/system/tool_core_config.yaml 的
/// `tool_output` 命名空间；缺省用此处默认值）。
#[derive(Debug, Clone, Copy)]
struct ToolOutputLimits {
    max_chars: usize,
    head_chars: usize,
    tail_chars: usize,
}

impl Default for ToolOutputLimits {
    fn default() -> Self {
        Self {
            max_chars: TOOL_OUTPUT_MAX_CHARS,
            head_chars: TOOL_OUTPUT_HEAD_CHARS,
            tail_chars: TOOL_OUTPUT_TAIL_CHARS,
        }
    }
}

impl ToolOutputLimits {
    fn from_config(config: &Value) -> Self {
        let ns = config.get("tool_output");
        let get = |k: &str, d: usize| {
            ns.and_then(|n| n.get(k))
                .and_then(|v| v.as_u64())
                .map(|v| v as usize)
                .unwrap_or(d)
        };
        Self {
            max_chars: get("max_chars", TOOL_OUTPUT_MAX_CHARS),
            head_chars: get("head_chars", TOOL_OUTPUT_HEAD_CHARS),
            tail_chars: get("tail_chars", TOOL_OUTPUT_TAIL_CHARS),
        }
    }
}

fn truncate_tool_output(text: &str, lim: &ToolOutputLimits) -> String {
    let total = text.chars().count();
    if total <= lim.max_chars {
        return text.to_string();
    }
    let head: String = text.chars().take(lim.head_chars).collect();
    let tail: String = text
        .chars()
        .skip(total.saturating_sub(lim.tail_chars))
        .collect();
    let omitted = total - lim.head_chars - lim.tail_chars;
    format!(
        "{head}\n…[工具输出已截断：省略 {omitted} 字符；完整结果已缓存，重发同一工具调用即可取回]…\n{tail}"
    )
}

/// 深拷贝并截断 data 中的超长字符串（消息/事件/TOOL_RESULTS 展示用）。
fn truncated_clone(r: &ToolResult, lim: &ToolOutputLimits) -> ToolResult {
    let mut c = r.clone();
    c.data = truncate_value_strings(c.data, lim);
    c
}

fn truncate_value_strings(v: Value, lim: &ToolOutputLimits) -> Value {
    match v {
        Value::String(s) => {
            if s.chars().count() > lim.max_chars {
                truncate_tool_output(&s, lim).into()
            } else {
                Value::String(s)
            }
        }
        Value::Array(a) => Value::Array(
            a.into_iter().map(|v| truncate_value_strings(v, lim)).collect(),
        ),
        Value::Object(o) => Value::Object(
            o.into_iter()
                .map(|(k, val)| (k, truncate_value_strings(val, lim)))
                .collect(),
        ),
        other => other,
    }
}

fn run(state: &Value, config: &Value, host: Option<&dyn HostServices>) -> HashMap<String, Value> {
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

    let limits = ToolOutputLimits::from_config(config);
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
            emit_tool_event(host, state, "tool_start", &tc, None, &limits);
            emit_tool_event(host, state, "tool_result", &tc, Some(&blocked), &limits);
            last_result_text = format!("Error: {}", blocked.error.as_deref().unwrap_or("unknown"));
            results.push(blocked);
            continue;
        }

        // 执行工具（经 host 调内核 tool-executor.invoke）。
        let result = execute_single_tool(&tc, host, state, &limits);
        last_result_text = result_text_for(&result, &limits);
        results.push(result);
    }

    // messages 重建（OpenAI 规范，对齐 plugin.py:784-859）。
    // 记录重建前的 messages 长度，用于识别本插件新增的消息（assistant tool_calls[若补造] + tool 结果）。
    let original_len = state
        .get("messages")
        .and_then(|v| v.as_array())
        .map(|a| a.len())
        .unwrap_or(0);

    // 全文留档：tool_cache_writer 据此写缓存全文（2026-09-09 用户裁定）。
    // 展示面（messages/TOOL_RESULTS/事件/RAW_RESULT）用截断版——LLM 要全文
    // 重发同一调用命中缓存即得。
    let full_results = results.clone();
    let display: Vec<ToolResult> = results.iter().map(|r| truncated_clone(r, &limits)).collect();

    let current_messages = messages::rebuild(state, tool_calls_raw, &display);

    // 多模态图片收集 + 注入（对齐 plugin.py:861-963）——读全文结果
    // （截断可能丢 base64 图片，图片必须存活）。
    let (current_messages, _mm_emitted) =
        messages::inject_multimodal(state, current_messages, &full_results, host);

    // state_updates 组装（对齐 plugin.py:1019-1050）。
    let mut updates: HashMap<String, Value> = HashMap::new();
    updates.insert("_full_tool_results".into(), json!(full_results));
    updates.insert(StateKey::TOOL_RESULTS.into(), json!(display));
    updates.insert(StateKey::RAW_RESULT.into(), Value::String(truncate_tool_output(&last_result_text, &limits)));
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
fn execute_single_tool(
    tc: &ToolCall,
    host: Option<&dyn HostServices>,
    state: &Value,
    limits: &ToolOutputLimits,
) -> ToolResult {
    // 发 tool_start 事件（host 为 None 时 emit 内部跳过）。
    emit_tool_event(host, state, "tool_start", tc, None, limits);

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
    emit_tool_event(host, state, "tool_result", tc, Some(&result), limits);
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
fn result_text_for(r: &ToolResult, limits: &ToolOutputLimits) -> String {
    if r.success {
        let preview = if r.data.is_object() {
            truncate_tool_output(
                &messages::serialize_for_content(&r.data).unwrap_or_else(|_| r.data.to_string()),
                limits,
            )
        } else {
            r.data.to_string()
        };
        preview.chars().take(200).collect()
    } else {
        format!("Error: {}", r.error.as_deref().unwrap_or("unknown"))
    }
}

/// 流式契约降级（streaming.json tool_result.error 要求 string）：进入流式
/// 载荷前把结构化 error 转串——对象优先取 message 字段；无 message 则整体
/// JSON 串化兜底；string 原样。完整原始结构由调用方留插件日志供排障。
fn error_to_contract_string(err: &Value) -> String {
    match err {
        Value::String(s) => s.clone(),
        Value::Object(map) => match map.get("message").and_then(|v| v.as_str()) {
            Some(message) => message.to_string(),
            None => err.to_string(),
        },
        other => other.to_string(),
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
    limits: &ToolOutputLimits,
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
        // result 为展示版结果文本（冷热一致性契约）：与持久化 content 同源——
        // 成功 = serialize_for_content（超长截断，全文在 tool_cache），
        // 失败 = "Error: {error}"（对齐 messages.rs:110）。
        let result_text = if r.success {
            truncate_tool_output(
                &messages::serialize_for_content(&r.data).unwrap_or_else(|_| r.data.to_string()),
                limits,
            )
        } else {
            format!("Error: {}", r.error.as_deref().unwrap_or("unknown"))
        };
        payload.insert("result".into(), Value::String(result_text));
        // 契约 result_data 要 object（streaming.json）：失败路径 data 为 Null、
        // 工具也可返回标量/数组——非对象进载荷会被契约网关 fail-closed 整事件
        // 丢弃，省略该字段（结果全文仍在 result）。
        if r.data.is_object() {
            payload.insert("result_data".into(), r.data.clone());
        }
        payload.insert("success".into(), Value::Bool(r.success));
        payload.insert("duration_ms".into(), json!((r.duration_ms * 10.0).round() / 10.0));
        if let Some(err) = &r.error {
            // 统一错误信封（单一真值源 config/kernel/error_codes.json）。流式契约
            // （streaming.json）error 要 string，信封对象会被契约网关 fail-closed
            // 整事件丢弃（agent 收不到错误详情）——降级为 string 进载荷：
            // message 优先，完整信封留插件日志（stderr → 内核日志）供排障。
            // 持久化 envelope（messages.rs）不受影响，REST 历史消息契约不变。
            let envelope = json!({
                "code": "TOOL_EXEC_FAILED",
                "message": err,
                "source": "plugin",
                "retryable": false,
                "details": null,
                "request_id": null,
            });
            // 原始 envelope 结构留日志（stderr 直出；错误正文已经
            // error_to_contract_string 串化进 payload，此处为排查保真底稿）。
            eprintln!("pipeline_tool_core tool_result error envelope: {envelope}");
            payload.insert(
                "error".into(),
                Value::String(error_to_contract_string(&envelope)),
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
        ) -> Result<&str, &str> {
            *self.last_params.lock().unwrap() = Some(params_json.to_string());
            Ok("{}")  // 借用协议：'static 字面量即实现方持有的 &str
        }
    }

    /// 流式契约（config/kernel/kernel_capabilities/streaming.json tool_result）：
    /// error 类型为 string。对象载荷会被内核流式契约网关 fail-closed 整事件
    /// 丢弃（失败时 agent 收不到错误详情）——失败事件 error 必须是 string
    /// 且保留原始错误信息。
    #[test]
    fn test_emit_tool_event_failure_error_is_contract_string() {
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

        emit_tool_event(Some(&host), &state, "tool_result", &tc, Some(&result), &ToolOutputLimits::default());

        let params: Value =
            serde_json::from_str(host.last_params.lock().unwrap().as_deref().unwrap()).unwrap();
        let payload = &params["payload"];
        assert_eq!(payload["success"], false);
        assert!(payload["error"].is_string(), "error 必须是 string：{payload}");
        assert!(payload["error"].as_str().unwrap().contains("command not found"));
    }

    /// 失败路径 data 为 Null：result_data 契约要 object，null 载荷会被网关
    /// 在 error 修复后继续以 result_data 违规拒掉整个事件——非对象必须缺席。
    #[test]
    fn test_emit_tool_event_failure_omits_null_result_data() {
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

        emit_tool_event(Some(&host), &state, "tool_result", &tc, Some(&result), &ToolOutputLimits::default());

        let params: Value =
            serde_json::from_str(host.last_params.lock().unwrap().as_deref().unwrap()).unwrap();
        let payload = &params["payload"];
        assert!(payload.get("result_data").is_none());
        // 结果全文仍在 result（string）里，信息不丢。
        assert_eq!(payload["result"], "Error: command not found");
    }

    /// 成功但数据为标量（工具可返回纯业务数据非对象）：result_data 省略，
    /// 载荷不产生 object 违规；result 全文保留。
    #[test]
    fn test_emit_tool_event_success_scalar_data_omits_result_data() {
        let host = CapturingHost {
            last_params: std::sync::Mutex::new(None),
        };
        let state = json!({
            "session_id": "sess-1",
            "pipeline_id": "pipe-1",
            "message_id": "msg-1",
        });
        let tc = ToolCall {
            name: "file_read".into(),
            args: json!({"path": "a.txt"}),
            call_id: Some("call_s1".into()),
        };
        let result = ToolResult::succeeded("file_read", json!("plain text"), 0.5);

        emit_tool_event(Some(&host), &state, "tool_result", &tc, Some(&result), &ToolOutputLimits::default());

        let params: Value =
            serde_json::from_str(host.last_params.lock().unwrap().as_deref().unwrap()).unwrap();
        let payload = &params["payload"];
        assert_eq!(payload["success"], true);
        assert!(payload.get("result_data").is_none());
        assert!(payload["result"].is_string());
    }

    /// 结构化降级（进入流式载荷前）：对象优先取 message 字段转 string；
    /// 无 message 的对象整体 JSON 串化兜底；string 原样透传。
    #[test]
    fn test_error_to_contract_string_downgrade() {
        // message 优先。
        let with_message = json!({"code": "X", "message": "boom detail", "retryable": false});
        assert_eq!(error_to_contract_string(&with_message), "boom detail");
        // 无 message → JSON 兜底。
        let no_message = json!({"code": "X", "retryable": false});
        let downgraded = error_to_contract_string(&no_message);
        assert!(downgraded.contains("\"code\""));
        assert!(downgraded.contains("\"X\""));
        // string 原样。
        assert_eq!(error_to_contract_string(&json!("raw error")), "raw error");
        // 非 string 的 message 字段 → JSON 兜底（不丢结构）。
        let non_string_message = json!({"message": 42});
        assert!(error_to_contract_string(&non_string_message).contains("42"));
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

        emit_tool_event(Some(&host), &state, "tool_result", &tc, Some(&result), &ToolOutputLimits::default());

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

#[cfg(test)]
mod truncation_tests {
    use super::*;

    // 契约字面值（用户裁定 2026-09-09：>16K 触发，头 12K 尾 2K）。
    // 断言一律用字面值而非实现常量——实现常量漂移时测试必须红，
    // 断言引用被测常量会与实现同步漂移沦为同义反复。
    const CONTRACT_MAX: usize = 16_384;
    const CONTRACT_HEAD: usize = 12_288;
    const CONTRACT_TAIL: usize = 2_048;

    fn parse_omitted(out: &str) -> usize {
        let start = out.find("省略 ").expect("标记含省略数") + "省略 ".len();
        let end = start + out[start..].find(" 字符").expect("省略数后随单位");
        out[start..end].parse().expect("省略数为十进制整数")
    }

    #[test]
    fn short_output_untouched() {
        assert_eq!(truncate_tool_output("hello", &ToolOutputLimits::default()), "hello");
    }

    /// 边界对：恰好 16384 不触发（闭区间阈值），16385 即触发。
    /// 省略数契约 = total − head − tail（max 只做触发判断）。
    #[test]
    fn boundary_at_max_chars_untouched_one_over_truncates() {
        let lim = ToolOutputLimits::default();
        let exact = "x".repeat(CONTRACT_MAX);
        assert_eq!(
            truncate_tool_output(&exact, &lim),
            exact,
            "恰好契约上限 16384 不触发截断"
        );
        for over_by in [1usize, 100] {
            let over = "x".repeat(CONTRACT_MAX + over_by);
            let out = truncate_tool_output(&over, &lim);
            assert!(out.contains("工具输出已截断"), "超契约上限 {} 字符即触发", over_by);
            assert_eq!(
                parse_omitted(&out),
                CONTRACT_MAX + over_by - CONTRACT_HEAD - CONTRACT_TAIL,
                "省略数 = total − head − tail（超限 {} 组）", over_by
            );
            assert!(out.starts_with('x') && out.ends_with('x'), "头尾保留");
        }
    }

    #[test]
    fn long_output_keeps_head_tail_and_marker() {
        let big = format!("{}中间内容{}", "a".repeat(20_000), "b".repeat(20_000));
        let total = big.chars().count();
        let out = truncate_tool_output(&big, &ToolOutputLimits::default());
        assert!(out.chars().count() <= CONTRACT_MAX + 200, "截断后不超契约上限+标记");
        assert!(out.contains("工具输出已截断"), "含截断标记");
        assert!(out.starts_with('a'), "保留头部");
        assert!(out.ends_with('b'), "保留尾部");
        // 头/尾精确性（非仅首尾字符）：保留段与输入前 12288 / 后 2048
        // 字符逐字符一致——锚定契约值本身。
        let out_chars: Vec<char> = out.chars().collect();
        let head: String = out_chars[..CONTRACT_HEAD].iter().collect();
        assert_eq!(
            head,
            big.chars().take(CONTRACT_HEAD).collect::<String>(),
            "头部保留段 = 输入前 12288 字符"
        );
        let tail: String = out_chars[out_chars.len() - CONTRACT_TAIL..].iter().collect();
        assert_eq!(
            tail,
            big.chars().skip(total - CONTRACT_TAIL).collect::<String>(),
            "尾部保留段 = 输入后 2048 字符"
        );
        // 标记内省略数字自洽：omitted == total − head − tail（性质断言）。
        assert_eq!(
            parse_omitted(&out),
            total - CONTRACT_HEAD - CONTRACT_TAIL,
            "省略数自洽"
        );
    }

    #[test]
    fn truncated_clone_caps_long_strings_and_keeps_error() {
        let r = ToolResult {
            tool_name: "t".into(),
            success: true,
            error: None,
            data: serde_json::json!({"big": "x".repeat(50_000), "small": "ok"}),
            metadata: None,
            duration_ms: 1.0,
        };
        let d = truncated_clone(&r, &ToolOutputLimits::default());
        let s = d.data["big"].as_str().unwrap();
        assert!(s.contains("工具输出已截断"), "长字符串被截断");
        assert_eq!(d.data["small"], "ok", "短字段不动");
        assert_eq!(r.data["big"], "x".repeat(50_000), "原结果不受影响（全文留档）");
    }

    /// 数组嵌套分支：数组元素内的超长字符串同样截断、短元素不动、
    /// 原结果不受影响（truncate_value_strings 的 Array 递归面）。
    #[test]
    fn truncated_clone_handles_nested_arrays() {
        let long = "z".repeat(CONTRACT_MAX + 1);
        let r = ToolResult {
            tool_name: "t".into(),
            success: true,
            error: None,
            data: serde_json::json!({"items": [long, "ok"]}),
            metadata: None,
            duration_ms: 1.0,
        };
        let d = truncated_clone(&r, &ToolOutputLimits::default());
        let item0 = d.data["items"][0].as_str().unwrap();
        assert!(item0.contains("工具输出已截断"), "数组内长字符串被截断");
        assert_eq!(d.data["items"][1], "ok", "数组内短元素不动");
        assert_eq!(
            r.data["items"][0].as_str().unwrap().len(),
            CONTRACT_MAX + 1,
            "原结果不受影响"
        );
    }
}
