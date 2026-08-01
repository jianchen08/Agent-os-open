//! 状态键常量 + 工具调用/结果数据结构。
//!
//! StateKey 对齐 Python `pipeline_types.py` 的 StateKeys。

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

/// serde default：返回 Value::Null（serde 的 default 属性要求函数路径，不能直接写 Value::Null 变体）。
fn default_null_value() -> Value {
    Value::Null
}

/// 状态键常量（snake_case，对齐 Python StateKeys）。
pub struct StateKey;
impl StateKey {
    pub const RAW_TOOL_CALLS: &'static str = "raw_tool_calls";
    pub const RAW_RESULT: &'static str = "raw_result";
    pub const RAW_ERROR: &'static str = "raw_error";
    pub const RAW_THINKING: &'static str = "raw_thinking";
    pub const TOOL_RESULTS: &'static str = "tool_results";
    pub const ENDED: &'static str = "ended";
}

/// 解析后的工具调用（对齐 Python tool_call dict）。
#[derive(Debug, Clone)]
pub struct ToolCall {
    /// 工具名（如 "bash_execute"）。
    pub name: String,
    /// 调用参数（已解析为对象）。
    pub args: Value,
    /// 工具调用 ID（OpenAI tool_call_id 配对用）。可能为空（由上层兜底生成）。
    pub call_id: Option<String>,
}

impl ToolCall {
    /// 从 raw_tool_calls 的单个元素解析。
    ///
    /// 对齐 plugin.py:626-679：name 取 "name"；args 取 "args" 或 "arguments"，
    /// 可能是对象也可能是 JSON 字符串（需容错修复）；id 取 "id"。
    pub fn parse(raw: &Value) -> Result<ToolCall, ToolResult> {
        let name = raw.get("name").and_then(|v| v.as_str()).unwrap_or("unknown").to_string();
        let mut args = raw.get("args").cloned().unwrap_or_else(|| raw.get("arguments").cloned().unwrap_or(json!({})));
        let call_id = raw.get("id").and_then(|v| v.as_str()).map(String::from);

        // args 是 JSON 字符串时尝试解析 + 容错修复。
        if let Some(s) = args.as_str() {
            match serde_json::from_str::<Value>(s) {
                Ok(parsed) => args = parsed,
                Err(_) => {
                    // 容错修复（对齐 _repair_json_string，7 步状态机）。
                    match crate::json_repair::repair_json_string(s) {
                        Some(repaired) => match serde_json::from_str::<Value>(&repaired) {
                            Ok(parsed) => args = parsed,
                            Err(_) => return Err(args_parse_failed(&name)),
                        },
                        None => return Err(args_parse_failed(&name)),
                    }
                }
            }
        }
        if !args.is_object() {
            args = json!({});
        }
        Ok(ToolCall { name, args, call_id })
    }
}

/// 工具执行结果（对齐 Python result dict + ToolExecutionResult）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolResult {
    pub tool_name: String,
    pub success: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    /// 工具返回数据（成功时；失败时可能为空）。
    #[serde(default = "default_null_value")]
    pub data: Value,
    /// 工具返回的元数据（task_failed / action 等）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub metadata: Option<Value>,
    pub duration_ms: f64,
}

impl ToolResult {
    pub fn succeeded(tool_name: &str, data: Value, duration_ms: f64) -> Self {
        Self {
            tool_name: tool_name.to_string(),
            success: true,
            error: None,
            data,
            metadata: None,
            duration_ms,
        }
    }

    pub fn failed(tool_name: &str, error: &str, duration_ms: f64) -> Self {
        Self {
            tool_name: tool_name.to_string(),
            success: false,
            error: Some(error.to_string()),
            data: Value::Null,
            metadata: None,
            duration_ms,
        }
    }
}

/// 构造 args 解析失败的错误结果（对齐 plugin.py:663-673）。
fn args_parse_failed(tool_name: &str) -> ToolResult {
    ToolResult {
        tool_name: tool_name.to_string(),
        success: false,
        error: Some(format!(
            "工具 {tool_name} 的调用参数 JSON 格式无效（可能参数内容过长导致被截断）。请将操作拆分为多个小步骤：\n\
             1. 如果是 file_write：请分多次写入，每次写入一个章节或部分内容\n\
             2. 如果是其他工具：请减少参数中的文本量\n\
             3. 不要一次性传入大量文本作为参数"
        )),
        data: Value::Null,
        metadata: None,
        duration_ms: 0.0,
    }
}

/// 拦截检查：被安全/隔离/权限策略拦截的工具返回失败结果（对齐 plugin.py:160-204）。
///
/// 三层判定：
/// 1. level_guard：security.level_decision.allowed == false 且 tool 在 blocked_tools
/// 2. isolation_guard：execution_contexts 中该 tool 被 blocked
/// 3. security_check：security.decision.allowed == false
pub fn check_tool_blocked(tool_name: &str, state: &Value) -> Option<ToolResult> {
    // level_guard
    if let Some(ld) = state.get("security.level_decision").and_then(|v| v.as_object()) {
        if ld.get("allowed").and_then(|v| v.as_bool()) == Some(false) {
            let blocked_tools = ld.get("blocked_tools").and_then(|v| v.as_array());
            let blocked = match blocked_tools {
                Some(arr) => arr.iter().any(|v| v.as_str() == Some(tool_name)),
                None => true, // 空列表 = 全拦
            };
            if blocked {
                let reason = ld.get("reason").and_then(|v| v.as_str()).unwrap_or("权限不足");
                return Some(ToolResult::failed(
                    tool_name,
                    &format!("工具被权限策略拦截: {reason}"),
                    0.0,
                ));
            }
        }
    }

    // isolation_guard
    if let Some(ctxs) = state.get("execution_contexts").and_then(|v| v.as_array()) {
        for ctx in ctxs {
            if ctx.get("tool_name").and_then(|v| v.as_str()) == Some(tool_name) {
                if ctx.get("blocked").and_then(|v| v.as_bool()).unwrap_or(false) {
                    let reason = ctx.get("reason").and_then(|v| v.as_str()).unwrap_or("隔离策略阻止");
                    return Some(ToolResult::failed(
                        tool_name,
                        &format!("工具被隔离策略拦截: {reason}"),
                        0.0,
                    ));
                }
            }
        }
    }

    // security_check
    if let Some(sd) = state.get("security.decision").and_then(|v| v.as_object()) {
        if sd.get("allowed").and_then(|v| v.as_bool()) == Some(false) {
            let reason = sd.get("reason").and_then(|v| v.as_str()).unwrap_or("安全检查拦截");
            return Some(ToolResult::failed(
                tool_name,
                &format!("工具被安全检查拦截: {reason}"),
                0.0,
            ));
        }
    }

    None
}
