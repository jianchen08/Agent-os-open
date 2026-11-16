//! # tool_core 原生插件（cdylib，直接 trait 对象）——单调用契约的工具执行核。
//!
//! 现役实现（manifest `host_type: in_process` 直载）。引擎并行 for-each 循环
//! 对 ``state.raw_tool_calls`` 逐元素执行，把当前调用注入迭代局部 state 顶层
//! 键 ``current_call``（形状 ``{name, args|arguments, id}``）；本插件契约是
//! **单调用**：
//!
//! 读 current_call → 取预填结果（state 里 guard 拦截优先；否则内核经
//! manifest ``native.pre_dispatch`` 已在 async 面执行完工具，结果注入
//! config ``_pre_decided_results``）→ 输出契约校验（tool_output_contracts，
//! 违规 fail-closed）→ 截断（config ``tool_output`` 命名空间，缺省 16384/
//! 12288/2048）→ 发 tool_start/tool_result 事件（event-bus，短调用）→ 产单条
//! tool 消息 op（契约夹具双车道）→ collect 键各写单元素数组（tool_results/
//! _full_tool_results/_executed_tool_calls）与 submitted_task_ids 增量
//! （collect_append 键，父侧追加）。
//!
//! **执行面不在本插件**：工具是跨进程调用，等待只该占内核 async future，不该
//! 占阻塞线程。内核预派发结果缺席 = 装配错误，本插件 fail-closed 出失败结果，
//! 不回落同步 bridge（native 同步 ABI 里 block_on 等返回会把线程钉住整个工具
//! 时长）。
//!
//! 业务代码零 unsafe——所有 FFI 由 native-sdk 的 plugin_into_raw 封装。

use std::collections::HashMap;

use agentos_native_sdk::{plugin_into_raw, ExecContext, HostServices, PipelinePlugin};
use serde_json::{json, Value};

mod messages;
mod output_validate;
mod types;

use types::{round1, StateKey, ToolCall, ToolResult};

/// 内核异步预派发结果在 config 中的键（与内核 invoker `tool_pre_dispatch` 同源约定）。
const CONFIG_PRE_DECIDED_RESULTS: &str = "_pre_decided_results";

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

/// 工具输出超长截断契约：>max 触发，头 head 尾 tail + 中部省略标记。全文经
/// `_full_tool_results` 交 tool_cache_writer 写缓存；LLM 要全文重发同一调用
/// 即可命中缓存取回。
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
            a.into_iter()
                .map(|v| truncate_value_strings(v, lim))
                .collect(),
        ),
        Value::Object(o) => Value::Object(
            o.into_iter()
                .map(|(k, val)| (k, truncate_value_strings(val, lim)))
                .collect(),
        ),
        other => other,
    }
}

/// 执行单调用核心流程（对齐 plugin.py `execute`）。
///
/// 返回 state_updates HashMap；current_call 缺失/为空 = 零动作（空 map）。
fn run(state: &Value, config: &Value, host: Option<&dyn HostServices>) -> HashMap<String, Value> {
    // 单调用契约：引擎 for-each 循环注入的迭代局部键。循环体不会以空调用
    // 触发本步骤；直接调用（step context 注入）缺失迭代变量时同样零动作。
    let raw_call = match state.get(StateKey::CURRENT_CALL) {
        Some(v) if v.is_object() && v.as_object().is_some_and(|o| !o.is_empty()) => v,
        _ => return HashMap::new(),
    };

    let limits = ToolOutputLimits::from_config(config);

    let result = match ToolCall::parse(raw_call) {
        // args JSON 无效：失败结果直出（不发事件——解析面失败不进执行面）。
        Err(fail_result) => *fail_result,
        Ok(tc) => {
            let mut result = match match_pre_decided(state, config, &tc) {
                // 预填命中：guard 拦截（ADR 2026-09-28 结果预填）或内核异步预派发
                // （native.pre_dispatch）——执行已在内核 async 面完成，本插件只做
                // 整形/校验/事件，不在此同步等待工具返回。
                Some(entry) => ToolResult::from_pre_decided_entry(&tc, entry),
                // 预派发是硬契约：结果缺席 = 内核装配错误，fail-closed 出失败结果
                // （禁止回落同步 bridge——跨进程等待必须留在内核 async 面）。
                None => {
                    eprintln!(
                        "pipeline_tool_core: kernel pre-dispatch result missing for tool '{}'",
                        tc.name
                    );
                    ToolResult::failed(
                        &tc.name,
                        "tool_core: kernel pre-dispatch result missing (native.pre_dispatch)",
                        0.0,
                    )
                }
            };
            // output_schema 消费端：成功结果按内核注入的 tool_output_contracts
            // 校验，违规 fail-closed 转失败——错误带回 LLM 自我修正。放在
            // tool_result 事件发送前，保证冷热一致（事件里的 success/error 与
            // 持久化 tool_result 同源）。
            if result.success && output_validate::validation_enabled(state) {
                if let Some(err) = validate_output_contract(state, &tc.name, &result.data) {
                    result = ToolResult::failed(
                        &tc.name,
                        &format!("output_schema validation failed: {err}"),
                        result.duration_ms,
                    );
                }
            }
            // 事件时序与执行路径 UX 对齐：start → result（预派发已先行完成，
            // 事件在整形后补记；前端工具卡由 LLM 流式块在输出阶段已建立）。
            emit_tool_event(host, state, "tool_start", &tc, None, &limits);
            emit_tool_event(host, state, "tool_result", &tc, Some(&result), &limits);
            result
        }
    };

    // 全文留档：tool_cache_writer 据此写缓存全文。展示面（messages/
    // tool_results/事件/raw_result）用截断版——LLM 要全文重发同一调用命中
    // 缓存即得。
    let display = truncated_clone(&result, &limits);
    collect_side_effects(state, raw_call, &result, &display, &limits, host)
}

/// 命中预填结果（对齐 plugin.py `_match_pre_decided`）：state 里 guard 拦截
/// 优先；无命中再读内核异步预派发注入 config 的结果。有 id 按 call_id 匹配，
/// 无 id（或 id 未命中）按工具名兜底（名字匹配只对没有 call_id 的 entry 生效）。
fn match_pre_decided<'a>(state: &'a Value, config: &'a Value, tc: &ToolCall) -> Option<&'a Value> {
    match_entries(
        state
            .get(StateKey::PRE_DECIDED_RESULTS)
            .and_then(Value::as_array),
        tc,
    )
    .or_else(|| {
        match_entries(
            config
                .get(CONFIG_PRE_DECIDED_RESULTS)
                .and_then(Value::as_array),
            tc,
        )
    })
}

fn match_entries<'a>(entries: Option<&'a Vec<Value>>, tc: &ToolCall) -> Option<&'a Value> {
    let entries = entries?;
    if let Some(id) = tc.call_id.as_deref().filter(|s| !s.is_empty()) {
        for entry in entries {
            if entry.get("call_id").and_then(|v| v.as_str()) == Some(id) {
                return Some(entry);
            }
        }
    }
    entries.iter().find(|entry| {
        let idless = entry
            .get("call_id")
            .and_then(|v| v.as_str())
            .map(str::is_empty)
            .unwrap_or(true);
        idless && entry.get("tool_name").and_then(|v| v.as_str()) == Some(tc.name.as_str())
    })
}

/// 取工具输出契约并校验（无契约/无 schema 返回 None = 通过）。
///
/// 契约由 tool_schema 插件经 tool-surface capability 写入 state["tool_output_contracts"]
/// （tool_name → {schema, render}）；render 供前端路由，schema 供本处校验。
fn validate_output_contract(state: &Value, tool_name: &str, data: &Value) -> Option<String> {
    let contract = state.get("tool_output_contracts")?.get(tool_name)?;
    let schema = contract.get("schema")?;
    if schema.is_null() {
        return None;
    }
    output_validate::validate(schema, data)
}

/// state_updates 组装（对齐 plugin.py `_collect_side_effects`）。
///
/// collect 键各写单元素数组（引擎 loop_config.collect 按迭代序归并拼接）、
/// raw_result 预览、messages 增量 ops；submitted_task_ids 写增量；
/// raw_error 仅 task_failed 级才置（并行归并 last-wins，平时写 None 会抹掉
/// 其他迭代的错误）。
fn collect_side_effects(
    state: &Value,
    raw_call: &Value,
    result: &ToolResult,
    display: &ToolResult,
    limits: &ToolOutputLimits,
    host: Option<&dyn HostServices>,
) -> HashMap<String, Value> {
    let mut updates: HashMap<String, Value> = HashMap::new();
    updates.insert(StateKey::TOOL_RESULTS.into(), json!([display]));
    updates.insert("_full_tool_results".into(), json!([result]));
    updates.insert("_executed_tool_calls".into(), json!([raw_call]));
    updates.insert(
        StateKey::RAW_RESULT.into(),
        Value::String(truncate_tool_output(
            &result_text_for(result, limits),
            limits,
        )),
    );
    updates.insert(
        "messages".into(),
        json!({ "_ops": messages::build_message_ops(state, raw_call, result, display, host) }),
    );

    let data = &result.data;
    // 任务系统级失败（data.metadata.task_failed）：置 ended + raw_error。
    if data
        .get("metadata")
        .and_then(|m| m.get("task_failed"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
    {
        let error = data
            .get("error")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or("任务系统级失败");
        updates.insert(StateKey::RAW_ERROR.into(), Value::String(error.into()));
        updates.insert(StateKey::ENDED.into(), Value::Bool(true));
    }

    // meta 解析：data.metadata（dict）优先，缺位回退信封顶层 metadata。
    let meta = data
        .get("metadata")
        .filter(|m| m.is_object())
        .or_else(|| result.metadata.as_ref().filter(|m| m.is_object()));

    if result.success {
        let action = meta
            .as_ref()
            .and_then(|m| m.get("action"))
            .and_then(|v| v.as_str());
        if (action == Some("task_submit") || action == Some("task_submit_container"))
            && data.is_object()
        {
            if let Some(task_id) = data
                .get("task_id")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
            {
                let in_snapshot = state
                    .get("submitted_task_ids")
                    .and_then(|v| v.as_array())
                    .is_some_and(|a| a.iter().any(|v| v.as_str() == Some(task_id)));
                if !in_snapshot {
                    // 增量语义（collect_append 键）：只写本调用新 id，父侧追加。
                    updates.insert("submitted_task_ids".into(), json!([task_id]));
                }
                // latest 落键仅当快照缺失（并行归并 last-wins，缺省才写）。
                if !state.as_object().is_some_and(|o| o.contains_key("task_id")) {
                    updates.insert("task_id".into(), Value::String(task_id.into()));
                }
                let workspace = data
                    .get("resolved_workspace")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty())
                    .or_else(|| {
                        data.get("workspace")
                            .and_then(|v| v.as_str())
                            .filter(|s| !s.is_empty())
                    });
                if let Some(ws) = workspace {
                    if !state
                        .as_object()
                        .is_some_and(|o| o.contains_key("workspace"))
                    {
                        updates.insert("workspace".into(), Value::String(ws.into()));
                    }
                }
            }
        }

        if result.tool_name == "task_evaluate"
            && meta
                .as_ref()
                .and_then(|m| m.get("result"))
                .and_then(|v| v.as_str())
                == Some("completed")
        {
            updates.insert("task_evaluation_completed".into(), Value::Bool(true));
            // 完成终态投影：评估完成写面（pipeline-state.update）只落 DB/内存
            // registry，引擎在飞 state 当轮不同步——run.completed 终态载荷带
            // 陈旧未决 task.status，任务域事件派生（未决态不派生）被拦、父
            // 通知断（2026-10-25 装机 a3615d3c5bb5 实锤）。工具结果携带的
            // task.* 终态字段经本投影同轮并入在飞 state，终态载荷/收尾投影/
            // 派生三面同源。
            if let Some(fields) = data.get("terminal_fields").and_then(|v| v.as_object()) {
                for (key, value) in fields {
                    if key.starts_with("task.") {
                        updates.insert(key.clone(), value.clone());
                    }
                }
            }
        }

        if result.tool_name == "human_interaction" && data.is_object() {
            let mut conv_flag = data.get("conversation_mode").is_some();
            if !conv_flag {
                for key in ["output", "data"] {
                    if data
                        .get(key)
                        .is_some_and(|inner| inner.get("conversation_mode").is_some())
                    {
                        conv_flag = true;
                        break;
                    }
                }
            }
            if conv_flag {
                updates.insert(
                    "_pending_route_signal".into(),
                    json!({
                        "route_type": "wait",
                        "reason": "human_interaction: user arrived, entering conversation",
                    }),
                );
            }
        }
    }

    updates
}

/// 取工具结果的预览文本（用于 raw_result，对齐 plugin.py 预览口径）。
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
    let pipeline_id = state
        .get("pipeline_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let message_id = state
        .get("message_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");

    let mut payload = serde_json::Map::new();
    payload.insert("thread_id".into(), Value::String(thread_id.into()));
    payload.insert("pipeline_id".into(), Value::String(pipeline_id.into()));
    payload.insert("message_id".into(), Value::String(message_id.into()));
    payload.insert(
        "call_id".into(),
        Value::String(tc.call_id.clone().unwrap_or_default()),
    );
    payload.insert("tool_name".into(), Value::String(tc.name.clone()));

    if kind == "tool_start" {
        payload.insert("args".into(), tc.args.clone());
    } else if let Some(r) = result {
        // result 为展示版结果文本（冷热一致性契约）：与持久化 content 同源——
        // 成功 = serialize_for_content（超长截断，全文在 tool_cache），
        // 失败 = "Error: {error}"。
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
            payload.insert(
                "result_data".into(),
                truncate_value_strings(r.data.clone(), limits),
            );
        }
        payload.insert("success".into(), Value::Bool(r.success));
        payload.insert("duration_ms".into(), json!(round1(r.duration_ms)));
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
    // fire-and-forget：忽略返回（event-bus.emit 不需要结果）。返回串是 exe 侧
    // 缓冲借用（跨分配器契约），不存引用、不释放。
    let _ = host.call_capability("event-bus", "emit", &params_json);
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 记录全部 capability 调用的 mock（现役只发 event-bus，返回空对象）。
    struct MockHost {
        calls: std::sync::Mutex<Vec<(String, String)>>,
    }

    impl MockHost {
        fn new() -> Self {
            Self {
                calls: std::sync::Mutex::new(Vec::new()),
            }
        }

        fn count(&self, capability: &str) -> usize {
            self.calls
                .lock()
                .unwrap()
                .iter()
                .filter(|(c, _)| c == capability)
                .count()
        }

        /// 第 n 次 capability 调用的参数（下标越界即 panic = 测试失败）。
        fn params_of(&self, capability: &str, n: usize) -> Value {
            let calls = self.calls.lock().unwrap();
            let (_, params) = calls
                .iter()
                .filter(|(c, _)| c == capability)
                .nth(n)
                .expect("capability 调用次数不足");
            serde_json::from_str(params).expect("params JSON 合法")
        }

        /// 全部 event-bus 事件名（按发射序）。
        fn event_names(&self) -> Vec<String> {
            self.calls
                .lock()
                .unwrap()
                .iter()
                .filter(|(c, _)| c == "event-bus")
                .map(|(_, p)| {
                    serde_json::from_str::<Value>(p)
                        .ok()
                        .and_then(|v| v["event"].as_str().map(String::from))
                        .unwrap_or_default()
                })
                .collect()
        }
    }

    impl HostServices for MockHost {
        fn call_capability(
            &self,
            capability: &str,
            _method: &str,
            params_json: &str,
        ) -> Result<&str, &str> {
            self.calls
                .lock()
                .unwrap()
                .push((capability.to_string(), params_json.to_string()));
            Ok("{}")
        }
    }

    /// 当前调用工具名（预派发替身的 entry 键）。
    fn current_tool_name(state: &Value) -> String {
        state[StateKey::CURRENT_CALL]["name"]
            .as_str()
            .unwrap_or("unknown")
            .to_string()
    }

    /// 内核 native.pre_dispatch 成功结果的测试替身（按 current_call 工具名）。
    fn predecided_success(state: &Value, data: Value) -> Value {
        json!({ CONFIG_PRE_DECIDED_RESULTS: [{
            "tool_name": current_tool_name(state),
            "success": true,
            "error": null,
            "data": data,
            "metadata": null,
            "duration_ms": 1.5,
        }]})
    }

    /// 内核 native.pre_dispatch 失败结果（success=false + error 文本）。
    fn predecided_failure(state: &Value, error: &str) -> Value {
        json!({ CONFIG_PRE_DECIDED_RESULTS: [{
            "tool_name": current_tool_name(state),
            "success": false,
            "error": error,
            "data": null,
            "metadata": null,
            "duration_ms": 2.0,
        }]})
    }

    /// 自定义 entry 注入（metadata/扩展位等场景）。
    fn predecided_with(state: &Value, mut entry: Value) -> Value {
        if let Some(obj) = entry.as_object_mut() {
            obj.entry("tool_name")
                .or_insert_with(|| Value::String(current_tool_name(state)));
        }
        json!({ CONFIG_PRE_DECIDED_RESULTS: [entry] })
    }

    fn call_state(current_call: Value) -> Value {
        json!({ StateKey::CURRENT_CALL: current_call })
    }

    // ══ ① 正常执行成功 ════════════════════════════════════════════

    /// 单调用成功全链路：事件序列、预派发结果整形、collect 键单元素数组、
    /// messages op、raw_result 预览（对齐 plugin.py 测试 ①）。
    #[test]
    fn single_call_success_events_collect_and_ops() {
        let host = MockHost::new();
        let state = json!({
            StateKey::CURRENT_CALL: {"name": "bash_execute", "id": "call_1", "args": {"command": "echo hi"}},
            "session_id": "sess-1",
            "pipeline_id": "pipe-1",
            "message_id": "msg-1",
            "messages": [
                {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "bash_execute", "arguments": "{}"}}]}
            ],
        });
        let updates = run(
            &state,
            &predecided_success(&state, json!({"output": "hi"})),
            Some(&host),
        );

        // 事件序列：tool_start → tool_result。
        assert_eq!(host.event_names(), ["tool_start", "tool_result"]);
        let start_payload = host.params_of("event-bus", 0)["payload"].clone();
        assert_eq!(start_payload["tool_name"], "bash_execute");
        assert_eq!(start_payload["call_id"], "call_1");
        assert_eq!(start_payload["thread_id"], "sess-1");
        let result_payload = host.params_of("event-bus", 1)["payload"].clone();
        assert_eq!(result_payload["success"], true);
        assert!(result_payload["result"].as_str().unwrap().contains("hi"));
        assert_eq!(result_payload["result_data"], json!({"output": "hi"}));
        assert!(result_payload.get("error").is_none());

        // 执行面已全部移交内核预派发：本插件零 tool-executor 调用。
        assert_eq!(host.count("tool-executor"), 0);

        // collect 键各为单元素数组。
        let results = updates[StateKey::TOOL_RESULTS].as_array().unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0]["success"], true);
        assert_eq!(results[0]["tool_name"], "bash_execute");
        let full = updates["_full_tool_results"].as_array().unwrap();
        assert_eq!(full.len(), 1);
        assert_eq!(full[0]["error"], Value::Null);
        assert_eq!(full[0]["metadata"], Value::Null);
        assert_eq!(full[0]["data"], json!({"output": "hi"}));
        assert_eq!(
            updates["_executed_tool_calls"],
            json!([{"name": "bash_execute", "id": "call_1", "args": {"command": "echo hi"}}])
        );

        // messages op：快照已有 assistant 配对消息 → 仅 tool op。
        let ops = updates["messages"]["_ops"].as_array().unwrap();
        assert_eq!(ops.len(), 1);
        assert_eq!(ops[0]["op"], "set");
        let msg = &ops[0]["msg"];
        assert_eq!(msg["role"], "tool");
        assert_eq!(msg["tool_call_id"], "call_1");
        let envelope = &msg["tool_result"];
        let keys: Vec<&String> = envelope.as_object().unwrap().keys().collect();
        assert_eq!(
            keys.len(),
            7,
            "envelope 七键封闭（call_id/tool_name/success/error/data/metadata/duration_ms）"
        );
        assert_eq!(envelope["call_id"], "call_1");

        // raw_result 预览 ≤ 200 字符。
        let raw_result = updates[StateKey::RAW_RESULT].as_str().unwrap();
        assert!(raw_result.chars().count() <= 200);
    }

    /// 成功预览硬上限 200 字符（serialize 后取前 200）。
    #[test]
    fn raw_result_preview_truncated_at_200_chars() {
        let host = MockHost::new();
        let state = call_state(json!({"name": "f", "id": "c1"}));
        let updates = run(
            &state,
            &predecided_success(&state, json!({"output": "x".repeat(500)})),
            Some(&host),
        );
        assert_eq!(
            updates[StateKey::RAW_RESULT]
                .as_str()
                .unwrap()
                .chars()
                .count(),
            200
        );
    }

    /// 快照缺配对消息时补造 assistant（带 reasoning_content），角色序
    /// [assistant, tool]（对齐 plugin.py 测试 ① assistant 分支）。
    #[test]
    fn assistant_message_synthesized_when_missing() {
        let host = MockHost::new();
        let state = json!({
            StateKey::CURRENT_CALL: {"name": "f", "id": "c9", "args": {"a": 1}},
            StateKey::RAW_THINKING: "think",
        });
        let updates = run(
            &state,
            &predecided_success(&state, json!({"ok": true})),
            Some(&host),
        );
        let ops = updates["messages"]["_ops"].as_array().unwrap();
        let roles: Vec<&str> = ops
            .iter()
            .map(|op| op["msg"]["role"].as_str().unwrap())
            .collect();
        assert_eq!(roles, ["assistant", "tool"]);
        let assistant = &ops[0]["msg"];
        assert_eq!(assistant["tool_calls"][0]["id"], "c9");
        assert_eq!(assistant["tool_calls"][0]["function"]["name"], "f");
        let args: Value = serde_json::from_str(
            assistant["tool_calls"][0]["function"]["arguments"]
                .as_str()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(args, json!({"a": 1}));
        assert_eq!(assistant["reasoning_content"], "think");
    }

    // ══ ② 超长输出截断（run 级边界对：恰好阈值 / 超阈值）═══════════

    // 契约字面值（commit f5549c070 钉过字面值断言——禁引用实现常量，
    // 实现常量漂移时测试必须红）。
    const CONTRACT_MAX: usize = 16_384;
    const CONTRACT_HEAD: usize = 12_288;
    const CONTRACT_TAIL: usize = 2_048;

    #[test]
    fn run_display_truncation_boundary() {
        for (total, truncated) in [
            (CONTRACT_MAX, false),    // 恰好契约上限不触发（闭区间阈值）
            (CONTRACT_MAX + 1, true), // 超限 1 字符即触发
            (CONTRACT_MAX + 5000, true),
        ] {
            let host = MockHost::new();
            let state = call_state(json!({"name": "f", "id": "c1"}));
            let updates = run(
                &state,
                &predecided_success(&state, json!({"blob": "x".repeat(total)})),
                Some(&host),
            );

            let full = &updates["_full_tool_results"][0];
            assert_eq!(
                full["data"]["blob"].as_str().unwrap().chars().count(),
                total,
                "全文留档不截断（total={total}）"
            );
            let shown = updates[StateKey::TOOL_RESULTS][0]["data"]["blob"]
                .as_str()
                .unwrap();
            if !truncated {
                assert_eq!(
                    shown.chars().count(),
                    total,
                    "恰好上限不触发（total={total}）"
                );
            } else {
                assert!(shown.contains("工具输出已截断"), "total={total}");
                assert!(shown.starts_with('x'), "头保留");
                assert!(shown.ends_with('x'), "尾保留");
                assert!(shown.chars().count() < total);
                let head = shown.chars().take(CONTRACT_HEAD).collect::<String>();
                assert!(
                    head.chars().all(|c| c == 'x') && head.chars().count() == CONTRACT_HEAD,
                    "头保留段 = 前 12288 字符"
                );
                let omitted: usize = shown
                    .split("省略 ")
                    .nth(1)
                    .and_then(|s| s.split(" 字符").next())
                    .and_then(|s| s.parse().ok())
                    .expect("截断标记含省略数");
                assert_eq!(
                    omitted,
                    total - CONTRACT_HEAD - CONTRACT_TAIL,
                    "省略数 = total − head − tail（total={total}）"
                );
            }
        }
    }

    /// 事件 result 文本按限长截断（超大载荷不整段进事件）。
    #[test]
    fn event_result_text_truncated_for_huge_payload() {
        let host = MockHost::new();
        let state = call_state(json!({"name": "f", "id": "c1"}));
        run(
            &state,
            &predecided_success(&state, json!({"blob": "y".repeat(50_000)})),
            Some(&host),
        );
        let payload = host.params_of("event-bus", 1)["payload"].clone();
        let result_text = payload["result"].as_str().unwrap();
        assert!(
            result_text.contains("工具输出已截断"),
            "事件 result 文本按限长截断"
        );
        assert!(result_text.chars().count() < 50_000);
    }

    // ══ ③④ 预填短路（ADR 2026-09-28）══════════════════════════════

    /// 命中预填结果：零 tool-executor 调用（幂等——有结果就不执行），事件照发，
    /// 预填不清写 pre_decided_results（引擎 consume_keys 负责清源）。
    #[test]
    fn pre_decided_hit_by_call_id_skips_execution() {
        let host = MockHost::new();
        let state = json!({
            StateKey::CURRENT_CALL: {"name": "bash_execute", "id": "call_x", "args": {"command": "ls"}},
            StateKey::PRE_DECIDED_RESULTS: [
                {
                    "call_id": "call_x",
                    "tool_name": "bash_execute",
                    "success": false,
                    "error": "Agent level L1 not allowed to call: bash_execute",
                    "data": null,
                    "metadata": {"decided_by": "level_guard"},
                    "duration_ms": 0.0,
                }
            ],
        });
        let updates = run(&state, &json!({}), Some(&host));

        // 幂等核心断言：有结果就不执行。
        assert_eq!(
            host.count("tool-executor"),
            0,
            "预定命中不得调 tool-executor"
        );
        // 事件照发（start + result，与执行路径 UX 一致）。
        assert_eq!(host.event_names(), ["tool_start", "tool_result"]);
        let result_payload = host.params_of("event-bus", 1)["payload"].clone();
        assert_eq!(result_payload["success"], false);
        assert!(result_payload["error"].as_str().unwrap().contains("L1"));

        let results = updates[StateKey::TOOL_RESULTS].as_array().unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0]["success"], false);
        assert!(results[0]["error"].as_str().unwrap().contains("L1"));
        assert_eq!(results[0]["metadata"], json!({"decided_by": "level_guard"}));
        // 预填不清写 pre_decided_results（引擎 consume_keys 负责）。
        assert!(!updates.contains_key(StateKey::PRE_DECIDED_RESULTS));
        // 全文留档同含预填结果。
        assert_eq!(updates["_full_tool_results"].as_array().unwrap().len(), 1);
    }

    /// 协议扩展位透传（BUG-41）：retry_allowed/arguments 收入 extra 随
    /// tool_results 输出（duplicate_check 认读），不进 messages envelope
    /// （七键封闭）。
    #[test]
    fn pre_decided_extras_passthrough_to_tool_results_only() {
        let host = MockHost::new();
        let state = json!({
            StateKey::CURRENT_CALL: {"name": "bash_execute", "id": "call_r"},
            StateKey::PRE_DECIDED_RESULTS: [
                {
                    "call_id": "call_r",
                    "tool_name": "bash_execute",
                    "success": false,
                    "error": "审批通道故障",
                    "retry_allowed": true,
                    "arguments": {"command": "ls"},
                }
            ],
        });
        let updates = run(&state, &json!({}), Some(&host));
        assert_eq!(host.count("tool-executor"), 0);
        let results = updates[StateKey::TOOL_RESULTS].as_array().unwrap();
        assert_eq!(
            results[0]["retry_allowed"], true,
            "扩展位随 tool_results 透传"
        );
        assert_eq!(results[0]["arguments"], json!({"command": "ls"}));
        // envelope 七键封闭：配对消息不带扩展位。
        let ops = updates["messages"]["_ops"].as_array().unwrap();
        let tool_msg = ops
            .iter()
            .map(|o| &o["msg"])
            .find(|m| m["role"] == "tool")
            .unwrap();
        assert!(
            tool_msg["tool_result"].get("retry_allowed").is_none(),
            "envelope 不带扩展位"
        );
    }

    /// 名字兜底：无 id 调用按工具名命中名字条目（保住旧按名执法对幻觉调用
    /// 的拦截面）。
    #[test]
    fn pre_decided_name_fallback_for_idless_call() {
        let host = MockHost::new();
        let state = json!({
            StateKey::CURRENT_CALL: {"name": "file_write", "args": {"path": "x"}},
            StateKey::PRE_DECIDED_RESULTS: [
                {
                    "tool_name": "file_write",
                    "success": false,
                    "error": "工具被权限策略拦截: 幻觉调用",
                    "metadata": {"decided_by": "level_guard"},
                    "duration_ms": 0.0,
                }
            ],
        });
        let updates = run(&state, &json!({}), Some(&host));
        assert_eq!(host.count("tool-executor"), 0);
        let results = updates[StateKey::TOOL_RESULTS].as_array().unwrap();
        assert_eq!(results[0]["success"], false, "无 id 调用按工具名兜底命中");
        assert!(results[0]["error"].as_str().unwrap().contains("幻觉调用"));
    }

    /// entry 解析缺省：success 缺省 fail-closed 为 false；tool_name 回退调用名。
    #[test]
    fn pre_decided_fail_closed_defaults() {
        let host = MockHost::new();
        let state = json!({
            StateKey::CURRENT_CALL: {"name": "bash_execute", "id": "call_z"},
            StateKey::PRE_DECIDED_RESULTS: [{"call_id": "call_z", "data": {"x": 1}}],
        });
        let updates = run(&state, &json!({}), Some(&host));
        assert_eq!(host.count("tool-executor"), 0);
        let results = updates[StateKey::TOOL_RESULTS].as_array().unwrap();
        assert_eq!(results[0]["success"], false, "success 缺省 fail-closed");
        assert_eq!(
            results[0]["tool_name"], "bash_execute",
            "tool_name 缺省回退调用名"
        );
        assert_eq!(results[0]["data"], json!({"x": 1}));
    }

    /// call_id 不匹配（state 预填属于其他调用）：不误用；回落内核预派发结果。
    #[test]
    fn pre_decided_call_id_mismatch_falls_back_to_pre_dispatch() {
        let host = MockHost::new();
        let state = json!({
            StateKey::CURRENT_CALL: {"name": "file_read", "id": "call_y", "args": {"path": "a.txt"}},
            StateKey::PRE_DECIDED_RESULTS: [
                {
                    "call_id": "call_stale",
                    "tool_name": "bash_execute",
                    "success": false,
                    "error": "过期预填",
                    "duration_ms": 0.0,
                }
            ],
        });
        let updates = run(
            &state,
            &predecided_with(
                &state,
                json!({"call_id": "call_y", "success": true, "data": {"path": "a.txt"}}),
            ),
            Some(&host),
        );
        let results = updates[StateKey::TOOL_RESULTS].as_array().unwrap();
        assert_eq!(results[0]["success"], true, "call_id 匹配的预派发结果生效");
        assert_eq!(results[0]["data"], json!({"path": "a.txt"}));
    }

    /// entry 字段直达 + 残缺 fail-closed + duration 规整（types 单元面）。
    #[test]
    fn from_pre_decided_entry_field_mapping() {
        let tc = ToolCall {
            name: "bash_execute".into(),
            args: json!({}),
            call_id: Some("call_z".into()),
        };
        // 残缺 entry（缺 success/tool_name/error）：按失败处理、名字回退。
        let r = ToolResult::from_pre_decided_entry(&tc, &json!({"data": {"x": 1}}));
        assert!(!r.success, "success 缺省 fail-closed");
        assert_eq!(r.tool_name, "bash_execute");
        assert!(r.error.is_none());
        assert_eq!(r.data, json!({"x": 1}));
        assert!(r.metadata.is_none());
        // 完整 entry：字段逐一直达。
        let r2 = ToolResult::from_pre_decided_entry(
            &tc,
            &json!({
                "tool_name": "bash_execute",
                "success": false,
                "error": "权限不足",
                "metadata": {"decided_by": "level_guard"},
                "duration_ms": 3.0,
            }),
        );
        assert_eq!(r2.error.as_deref(), Some("权限不足"));
        assert_eq!(r2.metadata, Some(json!({"decided_by": "level_guard"})));
        assert_eq!(r2.duration_ms, 3.0);
    }

    // ══ ⑤ args JSON 无效 ══════════════════════════════════════════

    /// 截断 JSON 不可修复：失败结果直出（零执行零事件），调用仍留档。
    #[test]
    fn invalid_args_json_fails_with_split_advice() {
        let host = MockHost::new();
        let raw_call = json!({"name": "file_write", "id": "c1", "args": "{\"path\": \"a.txt\", \"content\": "});
        let state = call_state(raw_call.clone());
        let updates = run(&state, &json!({}), Some(&host));

        assert_eq!(host.count("tool-executor"), 0, "解析失败不进执行面");
        assert!(host.event_names().is_empty(), "解析失败不发事件");
        let results = updates[StateKey::TOOL_RESULTS].as_array().unwrap();
        assert_eq!(results[0]["success"], false);
        assert!(results[0]["error"]
            .as_str()
            .unwrap()
            .contains("拆分为多个小步骤"));
        assert_eq!(results[0]["duration_ms"], 0.0);
        // 调用仍留档（整形面完整）。
        assert_eq!(updates["_executed_tool_calls"], json!([raw_call]));
    }

    /// args JSON 字符串解析后进整形面（tool_start 事件 args 用解析结果）；
    /// 非对象 args 归一空对象。
    #[test]
    fn args_string_parsed_and_non_object_normalized() {
        let host = MockHost::new();
        let state = call_state(json!({"name": "f", "id": "c1", "args": "{\"a\": 1}"}));
        run(
            &state,
            &predecided_success(&state, json!({"ran": true})),
            Some(&host),
        );
        assert_eq!(
            host.params_of("event-bus", 0)["payload"]["args"],
            json!({"a": 1}),
            "JSON 字符串参数解析后进事件"
        );

        let host2 = MockHost::new();
        let state2 = call_state(json!({"name": "f", "id": "c2", "args": [1, 2]}));
        run(
            &state2,
            &predecided_success(&state2, json!({"ran": true})),
            Some(&host2),
        );
        assert_eq!(
            host2.params_of("event-bus", 0)["payload"]["args"],
            json!({}),
            "非对象 args 归一空对象"
        );
    }

    // ══ ⑥ 输出契约违规 fail-closed ════════════════════════════════

    /// 违规 fail-closed 转失败，且校验发生在 tool_result 事件前（事件与
    /// 持久化结果 success/error 同源，冷热一致）。
    #[test]
    fn output_schema_violation_fails_closed() {
        let host = MockHost::new();
        let state = json!({
            StateKey::CURRENT_CALL: {"name": "bash_execute", "id": "c1"},
            "tool_output_validation": "on",
            "tool_output_contracts": {
                "bash_execute": {
                    "schema": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                        "required": ["status"],
                    },
                }
            },
        });
        let updates = run(
            &state,
            &predecided_success(&state, json!({"exit_code": 0})), // 缺 required 字段 status
            Some(&host),
        );

        let results = updates[StateKey::TOOL_RESULTS].as_array().unwrap();
        assert_eq!(results[0]["success"], false, "违规 fail-closed");
        assert!(results[0]["error"]
            .as_str()
            .unwrap()
            .contains("output_schema validation failed"));
        // 事件同源。
        let result_payload = host.params_of("event-bus", 1)["payload"].clone();
        assert_eq!(result_payload["success"], false);
        assert!(result_payload["error"]
            .as_str()
            .unwrap()
            .contains("output_schema validation failed"));
    }

    /// 合规放行 + 开关关闭（tool_output_validation == "off"）整体跳过。
    #[test]
    fn output_schema_pass_and_off_switch() {
        let state = json!({
            StateKey::CURRENT_CALL: {"name": "bash_execute", "id": "c1"},
            "tool_output_contracts": {
                "bash_execute": {"schema": {"type": "object", "required": ["status"]}}
            },
        });
        // 合规放行。
        let host = MockHost::new();
        let updates = run(
            &state,
            &predecided_success(&state, json!({"status": "completed"})),
            Some(&host),
        );
        assert_eq!(
            updates[StateKey::TOOL_RESULTS][0]["success"],
            true,
            "合规放行"
        );
        assert_eq!(host.count("tool-executor"), 0);

        // 开关关闭：违规数据也放行。
        let host2 = MockHost::new();
        let state_off = json!({
            StateKey::CURRENT_CALL: {"name": "bash_execute", "id": "c1"},
            "tool_output_validation": "off",
            "tool_output_contracts": {
                "bash_execute": {"schema": {"type": "object", "required": ["status"]}}
            },
        });
        let updates2 = run(
            &state_off,
            &predecided_success(&state_off, json!({"missing": true})),
            Some(&host2),
        );
        assert_eq!(updates2[StateKey::TOOL_RESULTS][0]["success"], true);
    }

    /// 契约取面单元：无契约放行 / 契约存在 schema null（仅 render）放行。
    #[test]
    fn validate_output_contract_fail_closed() {
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
        assert_eq!(
            validate_output_contract(&state, "bash_execute", &json!({"status": "completed"})),
            None
        );
        // 未声明契约的工具放行（存量工具零负担）。
        assert_eq!(
            validate_output_contract(&state, "file_read", &json!("anything")),
            None
        );
        // 契约存在但 schema 为 null（仅 render）放行。
        let render_only =
            json!({"tool_output_contracts": {"x": {"schema": null, "render": {"card": "read"}}}});
        assert_eq!(
            validate_output_contract(&render_only, "x", &json!({})),
            None
        );
    }

    /// 校验开关：tool_output_validation == "off" 时整体跳过。
    #[test]
    fn validation_switch_off() {
        assert!(!output_validate::validation_enabled(
            &json!({"tool_output_validation": "off"})
        ));
        assert!(output_validate::validation_enabled(&json!({})));
    }

    // ══ ⑦ 任务系统级失败 ══════════════════════════════════════════

    /// data.metadata.task_failed → 置 ended + raw_error（取 data.error 文本）。
    #[test]
    fn task_failed_metadata_sets_ended_and_raw_error() {
        let host = MockHost::new();
        let state = call_state(json!({"name": "task_status", "id": "c1"}));
        let updates = run(
            &state,
            &predecided_with(
                &state,
                json!({
                    "success": true,
                    "data": {
                        "error": "任务系统级失败: 内核不可用",
                        "metadata": {"task_failed": true},
                    },
                }),
            ),
            Some(&host),
        );
        assert_eq!(updates[StateKey::ENDED], Value::Bool(true));
        assert_eq!(
            updates[StateKey::RAW_ERROR],
            Value::String("任务系统级失败: 内核不可用".into())
        );
    }

    /// 平时不写 raw_error/ended（并行归并 last-wins：写 None 会抹掉其他迭代
    /// 的错误）。
    #[test]
    fn normal_success_does_not_write_raw_error() {
        let host = MockHost::new();
        let state = call_state(json!({"name": "f", "id": "c1"}));
        let updates = run(
            &state,
            &predecided_success(&state, json!({"ok": true})),
            Some(&host),
        );
        assert!(
            !updates.contains_key(StateKey::RAW_ERROR),
            "平时不写 raw_error"
        );
        assert!(!updates.contains_key(StateKey::ENDED));
    }

    // ══ ⑧ submitted_task_ids 增量去重 ═════════════════════════════

    /// 快照已有的 id 不重复写增量；快照没有 → 单元素增量 + latest 落键。
    #[test]
    fn submitted_task_ids_delta_dedup() {
        // 快照已有 → 增量键不写。
        let host = MockHost::new();
        let state = json!({
            StateKey::CURRENT_CALL: {"name": "task_submit", "id": "c1"},
            "submitted_task_ids": ["t-1"],
        });
        let updates = run(
            &state,
            &predecided_with(
                &state,
                json!({
                    "success": true,
                    "data": {"task_id": "t-1", "workspace": "/ws"},
                    "metadata": {"action": "task_submit"},
                }),
            ),
            Some(&host),
        );
        assert!(
            !updates.contains_key("submitted_task_ids"),
            "快照已有的 id 不重复写增量"
        );

        // 快照没有 → 写单元素增量 + latest 落键。
        let host2 = MockHost::new();
        let state2 = call_state(json!({"name": "task_submit", "id": "c1"}));
        let updates2 = run(
            &state2,
            &predecided_with(
                &state2,
                json!({
                    "success": true,
                    "data": {"task_id": "t-1", "workspace": "/ws"},
                    "metadata": {"action": "task_submit"},
                }),
            ),
            Some(&host2),
        );
        assert_eq!(updates2["submitted_task_ids"], json!(["t-1"]));
        assert_eq!(
            updates2["task_id"],
            Value::String("t-1".into()),
            "快照缺 task_id 时落 latest"
        );
        assert_eq!(updates2["workspace"], Value::String("/ws".into()));
    }

    /// 快照已有 task_id/workspace 键时不覆盖（并行归并 last-wins 缺省才写）；
    /// 增量键仍写（空快照不含新 id）。
    #[test]
    fn submitted_latest_keys_not_overwritten_when_snapshot_has_them() {
        let host = MockHost::new();
        let state = json!({
            StateKey::CURRENT_CALL: {"name": "task_submit", "id": "c1"},
            "submitted_task_ids": [],
            "task_id": "t-0",
            "workspace": "/ws-0",
        });
        let updates = run(
            &state,
            &predecided_with(
                &state,
                json!({
                    "success": true,
                    "data": {"task_id": "t-1", "workspace": "/ws"},
                    "metadata": {"action": "task_submit_container"},
                }),
            ),
            Some(&host),
        );
        assert_eq!(updates["submitted_task_ids"], json!(["t-1"]));
        assert!(!updates.contains_key("task_id"), "快照已有键不覆盖");
        assert!(!updates.contains_key("workspace"), "快照已有键不覆盖");
    }

    /// 信封顶层 metadata result=completed → task_evaluation_completed 投影键
    /// （task_reminder 三信号②的证据源）。
    #[test]
    fn task_evaluate_completed_signal() {
        let host = MockHost::new();
        let state = call_state(json!({"name": "task_evaluate", "id": "c1"}));
        let updates = run(
            &state,
            &predecided_with(
                &state,
                json!({
                    "success": true,
                    "data": {"task_id": "t-1"},
                    "metadata": {"result": "completed"},
                }),
            ),
            Some(&host),
        );
        assert_eq!(updates["task_evaluation_completed"], Value::Bool(true));
    }

    /// 完成结果携带终态投影字段（data.terminal_fields）→ task.* 键并入
    /// state_updates（在飞 state 同轮同步，run.completed 终态载荷带终态——
    /// 任务域事件派生判据）；非 task.* 前缀键不投影。
    #[test]
    fn task_evaluate_completed_projects_terminal_fields() {
        let host = MockHost::new();
        let state = call_state(json!({"name": "task_evaluate", "id": "c1"}));
        let updates = run(
            &state,
            &predecided_with(
                &state,
                json!({
                    "success": true,
                    "data": {
                        "task_id": "t-1",
                        "terminal_fields": {
                            "task.status": "completed",
                            "task.ended_at": "2026-10-25T15:36:44+00:00",
                            "task.eval_summary": "done",
                            "non_task.key": "skip"
                        }
                    },
                    "metadata": {"result": "completed"},
                }),
            ),
            Some(&host),
        );
        assert_eq!(updates["task.status"], Value::String("completed".into()));
        assert_eq!(
            updates["task.ended_at"],
            Value::String("2026-10-25T15:36:44+00:00".into())
        );
        assert_eq!(updates["task.eval_summary"], Value::String("done".into()));
        assert!(
            !updates.contains_key("non_task.key"),
            "非 task.* 前缀键不得投影"
        );
    }

    /// 对照：评估未通过（result=retry）即使携带 terminal_fields 也不投影——
    /// 终态投影只认通过裁决，误投影会把未完成任务写死终态。
    #[test]
    fn task_evaluate_retry_no_terminal_projection() {
        let host = MockHost::new();
        let state = call_state(json!({"name": "task_evaluate", "id": "c1"}));
        let updates = run(
            &state,
            &predecided_with(
                &state,
                json!({
                    "success": true,
                    "data": {
                        "task_id": "t-1",
                        "terminal_fields": {"task.status": "completed"}
                    },
                    "metadata": {"result": "retry"},
                }),
            ),
            Some(&host),
        );
        assert!(
            !updates.contains_key("task.status"),
            "未通过裁决不得投影终态字段"
        );
    }

    /// 对照：评估未通过（result=retry）不写投影键——收束判据只认通过裁决，
    /// 误写会把未完成任务提前放行。
    #[test]
    fn task_evaluate_retry_no_completed_signal() {
        let host = MockHost::new();
        let state = call_state(json!({"name": "task_evaluate", "id": "c1"}));
        for result in ["retry", "partial_pass", "failed"] {
            let updates = run(
                &state,
                &predecided_with(
                    &state,
                    json!({
                        "success": true,
                        "data": {"task_id": "t-1"},
                        "metadata": {"result": result},
                    }),
                ),
                Some(&host),
            );
            assert!(
                !updates.contains_key("task_evaluation_completed"),
                "result={result} 不得写完成投影键"
            );
        }
    }

    /// human_interaction 会话激活 → _pending_route_signal 路由信号。
    #[test]
    fn human_interaction_route_signal() {
        let host = MockHost::new();
        let state = call_state(json!({"name": "human_interaction", "id": "c1"}));
        let updates = run(
            &state,
            &predecided_success(&state, json!({"conversation_mode": true})),
            Some(&host),
        );
        assert_eq!(
            updates["_pending_route_signal"],
            json!({
                "route_type": "wait",
                "reason": "human_interaction: user arrived, entering conversation",
            })
        );
    }

    // ══ ⑨ no-op：current_call 缺失/为空/非对象 = 零动作 ═══════════

    #[test]
    fn missing_current_call_is_noop() {
        for current_call in [&Value::Null, &json!({}), &json!("not-a-dict")] {
            let host = MockHost::new();
            let updates = run(&call_state(current_call.clone()), &json!({}), Some(&host));
            assert!(
                updates.is_empty(),
                "current_call={current_call} 应零动作，实际: {updates:?}"
            );
            assert_eq!(host.count("tool-executor"), 0);
            assert!(host.event_names().is_empty());
        }
        // 键整体缺失同口径。
        let host = MockHost::new();
        let updates = run(&json!({}), &json!({}), Some(&host));
        assert!(updates.is_empty());
        assert_eq!(host.count("tool-executor"), 0);
    }

    // ══ 预派发失败路径 ════════════════════════════════════════════

    /// 预派发失败：错误文本进结果与事件（流式契约 error 必须 string）、
    /// result_data 缺席、raw_result = "Error: ..."。
    #[test]
    fn pre_dispatch_failure_error_text_and_event_contract() {
        let host = MockHost::new();
        let state = call_state(json!({"name": "bash_execute", "id": "c1"}));
        let updates = run(
            &state,
            &predecided_failure(&state, "command not found"),
            Some(&host),
        );

        let results = updates[StateKey::TOOL_RESULTS].as_array().unwrap();
        assert_eq!(results[0]["success"], false);
        assert_eq!(
            results[0]["error"],
            Value::String("command not found".into())
        );
        let payload = host.params_of("event-bus", 1)["payload"].clone();
        assert_eq!(payload["success"], false);
        assert_eq!(
            payload["error"], "command not found",
            "流式契约 error 必须 string"
        );
        assert_eq!(payload["result"], "Error: command not found");
        assert!(
            payload.get("result_data").is_none(),
            "失败路径 data 为 null，result_data 缺席"
        );
        assert_eq!(updates[StateKey::RAW_RESULT], "Error: command not found");
    }

    /// 预派发失败 entry 缺 error 文案（非内核产物，正常不可达）：success 仍
    /// fail-closed 为 false，事件 result 降级 "Error: unknown"、error 字段缺席。
    #[test]
    fn pre_dispatch_failure_missing_error_defaults() {
        let host = MockHost::new();
        let state = call_state(json!({"name": "f", "id": "c1"}));
        let updates = run(
            &state,
            &predecided_with(&state, json!({"success": false})),
            Some(&host),
        );
        assert_eq!(updates[StateKey::TOOL_RESULTS][0]["success"], false);
        assert_eq!(updates[StateKey::TOOL_RESULTS][0]["error"], Value::Null);
        let payload = host.params_of("event-bus", 1)["payload"].clone();
        assert!(payload.get("error").is_none());
        assert_eq!(payload["result"], "Error: unknown");
    }

    /// 预派发结果缺席（内核装配错误）：fail-closed 出失败结果，不回落同步
    /// bridge 等待（native.pre_dispatch 硬契约）。
    #[test]
    fn missing_pre_dispatch_fails_closed() {
        let host = MockHost::new();
        let state = call_state(json!({"name": "f", "id": "c1"}));
        let updates = run(&state, &json!({}), Some(&host));
        let results = updates[StateKey::TOOL_RESULTS].as_array().unwrap();
        assert_eq!(results[0]["success"], false);
        let err = results[0]["error"].as_str().unwrap();
        assert!(err.contains("pre-dispatch result missing"), "{err}");
        assert_eq!(host.count("tool-executor"), 0, "禁止同步 bridge 回退");
    }

    /// host 未注入不改变契约：预派发结果照常整形（事件发送内部跳过），
    /// 结果缺席仍 fail-closed。
    #[test]
    fn host_none_still_shapes_pre_dispatch_result() {
        let state = call_state(json!({"name": "f", "id": "c1"}));
        let updates = run(
            &state,
            &predecided_success(&state, json!({"ok": true})),
            None,
        );
        let results = updates[StateKey::TOOL_RESULTS].as_array().unwrap();
        assert_eq!(results[0]["success"], true);

        let updates_missing = run(&state, &json!({}), None);
        let results_missing = updates_missing[StateKey::TOOL_RESULTS].as_array().unwrap();
        assert_eq!(results_missing[0]["success"], false);
        assert!(results_missing[0]["error"]
            .as_str()
            .unwrap()
            .contains("pre-dispatch result missing"));
    }

    // ══ 事件契约（流式网关 fail-closed 面）════════════════════════

    /// 流式契约（config/kernel/kernel_capabilities/streaming.json tool_result）：
    /// error 类型为 string。对象载荷会被内核流式契约网关 fail-closed 整事件
    /// 丢弃（失败时 agent 收不到错误详情）——失败事件 error 必须是 string
    /// 且保留原始错误信息。
    #[test]
    fn emit_tool_event_failure_error_is_contract_string() {
        let host = MockHost::new();
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

        emit_tool_event(
            Some(&host),
            &state,
            "tool_result",
            &tc,
            Some(&result),
            &ToolOutputLimits::default(),
        );

        let params = host.params_of("event-bus", 0);
        let payload = &params["payload"];
        assert_eq!(payload["success"], false);
        assert!(
            payload["error"].is_string(),
            "error 必须是 string：{payload}"
        );
        assert!(payload["error"]
            .as_str()
            .unwrap()
            .contains("command not found"));
    }

    /// 失败路径 data 为 Null：result_data 契约要 object，null 载荷会被网关
    /// 在 error 修复后继续以 result_data 违规拒掉整个事件——非对象必须缺席。
    #[test]
    fn emit_tool_event_failure_omits_null_result_data() {
        let host = MockHost::new();
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

        emit_tool_event(
            Some(&host),
            &state,
            "tool_result",
            &tc,
            Some(&result),
            &ToolOutputLimits::default(),
        );

        let params = host.params_of("event-bus", 0);
        let payload = &params["payload"];
        assert!(payload.get("result_data").is_none());
        // 结果全文仍在 result（string）里，信息不丢。
        assert_eq!(payload["result"], "Error: command not found");
    }

    /// 成功但数据为标量（工具可返回纯业务数据非对象）：result_data 省略，
    /// 载荷不产生 object 违规；result 全文保留。
    #[test]
    fn emit_tool_event_success_scalar_data_omits_result_data() {
        let host = MockHost::new();
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

        emit_tool_event(
            Some(&host),
            &state,
            "tool_result",
            &tc,
            Some(&result),
            &ToolOutputLimits::default(),
        );

        let params = host.params_of("event-bus", 0);
        let payload = &params["payload"];
        assert_eq!(payload["success"], true);
        assert!(payload.get("result_data").is_none());
        assert!(payload["result"].is_string());
    }

    /// 结构化降级（进入流式载荷前）：对象优先取 message 字段转 string；
    /// 无 message 的对象整体 JSON 串化兜底；string 原样透传。
    #[test]
    fn error_to_contract_string_downgrade() {
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
    fn emit_tool_event_success_has_no_error() {
        let host = MockHost::new();
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

        emit_tool_event(
            Some(&host),
            &state,
            "tool_result",
            &tc,
            Some(&result),
            &ToolOutputLimits::default(),
        );

        let params = host.params_of("event-bus", 0);
        let payload = &params["payload"];
        assert_eq!(payload["success"], true);
        assert!(payload.get("error").is_none());
    }

    // ══ 多模态（run 级）═══════════════════════════════════════════

    // ══ 多模态（run 级）═══════════════════════════════════════════

    /// 截图回传视觉模型：multimedia 事件（不含 base64）+ 多模态 user 消息 op
    /// 注入（对齐 plugin.py 测试 多模态①）。
    #[test]
    fn multimodal_images_emitted_and_user_message_injected() {
        let host = MockHost::new();
        let state = json!({
            StateKey::CURRENT_CALL: {"name": "screenshot", "id": "c1"},
            "llm_supports_vision": true,
        });
        let updates = run(
            &state,
            &predecided_with(
                &state,
                json!({
                    "success": true,
                    "data": {"base64_data": "AAAA", "mime_type": "image/png", "path": "/s/a.png"},
                    "metadata": {
                        "multimodal_content": [
                            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BBBB"}}
                        ]
                    },
                }),
            ),
            Some(&host),
        );

        let kinds = host.event_names();
        assert!(kinds.contains(&"tool_multimedia_result".to_string()));
        assert_eq!(
            kinds,
            ["tool_start", "tool_result", "tool_multimedia_result"],
            "事件发射序：工具事件在前，多模态事件随后"
        );
        let mm_index = kinds
            .iter()
            .position(|k| k == "tool_multimedia_result")
            .unwrap();
        let mm = host.params_of("event-bus", mm_index);
        assert_eq!(mm["payload"]["count"], 2);
        assert_eq!(
            mm["payload"]["multimedia"][0],
            json!({"mime_type": "image/png", "path": "/s/a.png"})
        );
        assert!(
            !serde_json::to_string(&mm["payload"])
                .unwrap()
                .contains("base64\":"),
            "事件不含 base64"
        );

        let ops = updates["messages"]["_ops"].as_array().unwrap();
        let user_msg = ops
            .iter()
            .map(|op| &op["msg"])
            .find(|m| m["role"] == "user")
            .expect("应注入多模态 user 消息");
        assert_eq!(user_msg["name"], "tool_images");
        let blocks = user_msg["content"].as_array().unwrap();
        assert_eq!(blocks[0]["type"], "text");
        assert_eq!(
            blocks.iter().filter(|b| b["type"] == "image_url").count(),
            2
        );
        assert!(blocks[1]["image_url"]["url"]
            .as_str()
            .unwrap()
            .starts_with("data:image/png;base64,"));
    }

    /// 无视觉能力：文本引导分析（对齐 plugin.py 测试 多模态②）。
    #[test]
    fn multimodal_without_vision_guides_text_analysis() {
        let host = MockHost::new();
        let state = json!({
            StateKey::CURRENT_CALL: {"name": "screenshot", "id": "c1"},
            "llm_supports_vision": false,
        });
        let updates = run(
            &state,
            &predecided_success(
                &state,
                json!({"images": [{"base64": "CCCC", "path": "/s/b.png"}]}),
            ),
            Some(&host),
        );
        let ops = updates["messages"]["_ops"].as_array().unwrap();
        let user_msg = ops
            .iter()
            .map(|op| &op["msg"])
            .find(|m| m["role"] == "user")
            .expect("应注入文本引导 user 消息");
        let content = user_msg["content"].as_str().unwrap();
        assert!(content.contains("不支持图片分析"), "{content}");
        assert!(content.contains("/s/b.png"), "{content}");
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
        assert_eq!(
            truncate_tool_output("hello", &ToolOutputLimits::default()),
            "hello"
        );
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
            assert!(
                out.contains("工具输出已截断"),
                "超契约上限 {over_by} 字符即触发"
            );
            assert_eq!(
                parse_omitted(&out),
                CONTRACT_MAX + over_by - CONTRACT_HEAD - CONTRACT_TAIL,
                "省略数 = total − head − tail（超限 {over_by} 组）"
            );
            assert!(out.starts_with('x') && out.ends_with('x'), "头尾保留");
        }
    }

    #[test]
    fn long_output_keeps_head_tail_and_marker() {
        let big = format!("{}中间内容{}", "a".repeat(20_000), "b".repeat(20_000));
        let total = big.chars().count();
        let out = truncate_tool_output(&big, &ToolOutputLimits::default());
        assert!(
            out.chars().count() <= CONTRACT_MAX + 200,
            "截断后不超契约上限+标记"
        );
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
        let tail: String = out_chars[out_chars.len() - CONTRACT_TAIL..]
            .iter()
            .collect();
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
            extra: None,
        };
        let d = truncated_clone(&r, &ToolOutputLimits::default());
        let s = d.data["big"].as_str().unwrap();
        assert!(s.contains("工具输出已截断"), "长字符串被截断");
        assert_eq!(d.data["small"], "ok", "短字段不动");
        assert_eq!(
            r.data["big"],
            "x".repeat(50_000),
            "原结果不受影响（全文留档）"
        );
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
            extra: None,
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
