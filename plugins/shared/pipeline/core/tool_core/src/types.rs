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
    /// guards 预填的预定结果（ADR 2026-09-28 结果预填机制）：拦截方直出拒绝
    /// 结果，本插件命中即跳过执行（幂等）。写入纪律：多 guard 各自读现值经
    /// SDK merge_pre_decided 合并后整体写回（有 call_id 后写赢、无 id 按工具
    /// 名去重兜底），本插件消费即清——键里永远只有本轮条目，无跨轮陈旧命中面。
    pub const PRE_DECIDED_RESULTS: &'static str = "pre_decided_results";
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
    /// 协议扩展位（pre_decided_results entry 七键之外的字段，如
    /// security_check 的 retry_allowed/arguments [BUG-41]）：仅随
    /// tool_results 状态输出透传（duplicate_check 等下游按键认读），
    /// 不进 messages envelope（envelope 七键封闭，rebuild 显式构造）。
    #[serde(default, flatten)]
    pub extra: Option<serde_json::Map<String, Value>>,
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
            extra: None,
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
            extra: None,
        }
    }

    /// 从 pre_decided_results 的单个 entry 构造结果（ADR 2026-09-28）。
    ///
    /// entry 形状由 SDK `tool_result_entry` 构造（七键 + 扩展位）；七键直达
    /// 对应字段，扩展位整体收入 `extra` 随 tool_results 透传（envelope 封闭
    /// 不受影响）。`success` 缺省 false（fail-closed：形状残缺按失败处理，
    /// 不带进执行）。
    pub fn from_pre_decided_entry(tc: &ToolCall, entry: &Value) -> Self {
        const KNOWN_ENTRY_KEYS: [&str; 7] = [
            "call_id",
            "tool_name",
            "success",
            "error",
            "data",
            "metadata",
            "duration_ms",
        ];
        let extra = entry.as_object().and_then(|o| {
            let leftover: serde_json::Map<String, Value> = o
                .iter()
                .filter(|(k, _)| !KNOWN_ENTRY_KEYS.contains(&k.as_str()))
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect();
            (!leftover.is_empty()).then_some(leftover)
        });
        Self {
            tool_name: entry
                .get("tool_name")
                .and_then(|v| v.as_str())
                .map(String::from)
                .unwrap_or_else(|| tc.name.clone()),
            success: entry.get("success").and_then(|v| v.as_bool()).unwrap_or(false),
            error: entry.get("error").and_then(|v| v.as_str()).map(String::from),
            data: entry.get("data").cloned().unwrap_or(Value::Null),
            metadata: entry
                .get("metadata")
                .cloned()
                .filter(|m: &Value| !m.is_null()),
            duration_ms: entry.get("duration_ms").and_then(|v| v.as_f64()).unwrap_or(0.0),
            extra,
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
        extra: None,
    }
}
