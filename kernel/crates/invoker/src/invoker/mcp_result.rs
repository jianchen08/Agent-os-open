//! 工具/管道调用返回归一纯函数族：标准 MCP 出参白名单过滤、content 协议
//! 归一、OnError 判定与 sidecar/native 两决策树。
//!
//! 自 invoker.rs 按关注点纯搬运拆出（2026-10-12 文件长度机械闸首个应用案例，
//! ADR docs/decisions/2026-10-12-file-length-mechanical-gate.md）。

use super::*;

/// 标准 MCP 出参归一：参数白名单按工具声明的 input_schema 过滤。
///
/// 内核/管道会向工具参数注入会话上下文（_call_context/session_id/workspace/
/// pipeline_id/thread_id/parent_agent_level/…，清单随调用方增长），外部标准
/// MCP server（FastMCP 等）对未声明参数严格校验直接拒——所以以 tools/list
/// 观测回填的 input_schema.properties 为**白名单**（声明即透传，标准协议
/// 字段进内部不变形）；schema 未知的工具退化为黑名单（剥下划线前缀 + 已知
/// 注入键）。外部工具声明下划线参数属非标准用法，约定不支持。
pub(super) fn sanitize_external_mcp_arguments(
    manifest: &PluginManifest,
    tool_name: &str,
    inputs: &serde_json::Value,
) -> serde_json::Value {
    const INJECTED: [&str; 14] = [
        "session_id",
        "workspace",
        "working_dir",
        "tenant_id",
        "plugin_id",
        "_log_ctx",
        "pipeline_id",
        "thread_id",
        "parent_agent_level",
        "user_id",
        "client_message_id",
        "message_id",
        "intent",
        "timestamp",
    ];
    let declared_props: Option<&serde_json::Map<String, serde_json::Value>> = manifest
        .capabilities
        .tools
        .iter()
        .find(|t| t.name == tool_name)
        .and_then(|t| t.input_schema.as_ref())
        .and_then(|s| s.get("properties"))
        .and_then(|p| p.as_object());

    match inputs {
        serde_json::Value::Object(map) => serde_json::Value::Object(
            map.iter()
                .filter(|(k, v)| {
                    if k.starts_with('_') || INJECTED.contains(&k.as_str()) {
                        return false;
                    }
                    match declared_props {
                        Some(props) => props.contains_key(*k) && !v.is_null(),
                        None => true,
                    }
                })
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect(),
        ),
        other => other.clone(),
    }
}

/// 从 MCP tools/call 响应中提取内部 JSON 值。
///
/// Python SDK 的 McpServer 返回格式为：
/// ```json
/// { "content": [{ "type": "text", "text": "<json_string>" }], "isError": false }
/// ```
/// 其中 `text` 字段是工具实际返回值的 JSON 字符串。
/// 本函数提取 `content[0].text`，解析为 `serde_json::Value` 返回。
///
/// 如果 `isError` 为 true 或解析失败，返回包含错误信息的 JSON 对象。
pub(super) fn extract_mcp_content(mcp_result: &serde_json::Value) -> serde_json::Value {
    if mcp_result
        .get("isError")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
    {
        let err_msg = mcp_result
            .get("content")
            .and_then(|c| c.as_array())
            .and_then(|arr| arr.first())
            .and_then(|item| item.get("text"))
            .and_then(|t| t.as_str())
            .unwrap_or("MCP tool returned isError=true");
        // 显式失败信封（success=false）：MCP 协议层失败在提取点就落显式状态键，
        // 不经裸 error 键猜测（归一化决策树只认显式 success，见
        // normalize_mcp_tool_result）。
        return serde_json::json!({"success": false, "error": err_msg});
    }

    // ── 标准 MCP content 协议归一（external MCP 一行接入配套）────────
    // 内部 sidecar 约定：content[0].text 是业务 JSON 字符串（优先照旧直通）。
    // 其余一律按标准 MCP 语义转换：text 项拼接、image 项转内部 images 通道
    // （tool_core inject_multimodal 来源 2，截图回传视觉模型）、resource 取
    // text 并入——标准协议字段进来，内部多模态通道出去，转换只此一处。
    let items = mcp_result.get("content").and_then(|c| c.as_array());
    let Some(items) = items else {
        return mcp_result.clone();
    };

    let first_text_json = items
        .first()
        .and_then(|item| item.get("text"))
        .and_then(|t| t.as_str())
        .and_then(|s| serde_json::from_str::<serde_json::Value>(s).ok());
    let has_image = items
        .iter()
        .any(|item| item.get("type").and_then(|v| v.as_str()) == Some("image"));

    if let (Some(val), false) = (first_text_json, has_image) {
        return val;
    }

    let mut texts: Vec<String> = Vec::new();
    let mut images: Vec<serde_json::Value> = Vec::new();
    for item in items {
        match item.get("type").and_then(|v| v.as_str()) {
            Some("text") => {
                let t = item.get("text").and_then(|t| t.as_str()).unwrap_or("");
                if !t.is_empty() {
                    texts.push(t.to_string());
                }
            }
            Some("image") => images.push(serde_json::json!({
                "base64": item.get("data").and_then(|d| d.as_str()).unwrap_or(""),
                "mime_type": item
                    .get("mimeType")
                    .and_then(|m| m.as_str())
                    .unwrap_or("image/png"),
            })),
            Some("resource") => {
                let t = item
                    .get("resource")
                    .and_then(|r| r.get("text"))
                    .and_then(|t| t.as_str())
                    .unwrap_or("");
                if !t.is_empty() {
                    texts.push(t.to_string());
                }
            }
            _ => {}
        }
    }
    let mut output = serde_json::Map::new();
    if !texts.is_empty() {
        output.insert(
            "text".into(),
            serde_json::Value::String(texts.join(
                "
",
            )),
        );
    }
    if !images.is_empty() {
        output.insert("images".into(), serde_json::Value::Array(images));
    }
    serde_json::json!({ "success": true, "output": output })
}

/// 中央返回处的 OnError 判定（pipeline 形态）：调用层 `Err`（崩溃/MCP/解析）
/// 或 Ok 携带 error 字段都算插件报错——业务失败若不计，`lifecycle.plugin_error_total`
/// 恒零。None = 无报错（不发事件）。
pub(super) fn pipeline_error_params(
    result: &Result<PluginResult, PluginError>,
) -> Option<(String, Option<String>)> {
    match result {
        Err(e) => Some((e.message.clone(), e.code.clone())),
        Ok(r) => r
            .error
            .as_ref()
            .map(|e| (e.message.clone(), e.code.clone())),
    }
}

/// 同 [`pipeline_error_params`]（tool 形态）：Ok 携带 success=false 也算报错。
pub(super) fn tool_error_params(
    result: &Result<ToolExecutionResult, PluginError>,
) -> Option<(String, Option<String>)> {
    match result {
        Err(e) => Some((e.message.clone(), e.code.clone())),
        Ok(r) if !r.success => Some((
            r.error
                .clone()
                .unwrap_or_else(|| "tool execution failed".to_string()),
            None,
        )),
        Ok(_) => None,
    }
}

/// MCP 工具调用返回归一（sidecar 决策树，`invoke_mcp_tool` 的纯函数核心）。
///
/// 输入是 `extract_mcp_content` 提取的工具 handler 原始返回（业务 dict）。按形状分流：
/// - ②-a 真 ToolExecutionResult 信封（同时带 success + data）→ 直接 from_value；
/// - ②-b Python ToolResult 形状（带 success 但无 data）→ 归一化：
///   - (A) 带 output 键（builtin_tools 的 ToolResult.to_dict()）→ data = output；
///   - (B) 无 output 键（server.py 解包直返、恰带 success 键的业务 dict）→ data = inner；
///   - success=false → failure(error)；
/// - ③ 纯业务数据（无 success 键）→ success(data=inner)——**顶层 error 键不再是
///   失败信号**。工具成败只认显式 success 键（工具执行状态统一承载，废除按结果
///   文本猜失败的兼容分支）：业务载荷的 error 字段（如 task_manage get 详情返回
///   的下级任务失败原因）与执行失败信封必须可区分，否则成功查询会被翻转成
///   工具报错（事故 2026-09-28）。MCP isError=true 的协议层失败由
///   `extract_mcp_content` 在提取点显式转 `{success: false, error}`，走 ②-b。
///
/// K7：②-b 的 `success` 键存在但非 bool（字符串/整数状态码等信封漂移）→
/// `PARSE_ERROR`（与 ②-a 对信封解析失败同判）。禁止 `unwrap_or(true)` 把失败
/// 工具调用包装成成功污染下游 state——原始信封此时已丢，成功偏置无法追溯。
pub(super) fn normalize_mcp_tool_result(
    inner: Value,
    tool_name: &str,
) -> Result<ToolExecutionResult, PluginError> {
    if inner.get("success").is_some() && inner.get("data").is_some() {
        // ②-a 真 ToolExecutionResult 信封（from_value 对非 bool success 同样报错）
        serde_json::from_value(inner).map_err(|e| PluginError {
            message: format!("failed to parse MCP response as ToolExecutionResult: {e}"),
            code: Some("PARSE_ERROR".to_string()),
            source: Some("plugin-invoker".to_string()),
        })
    } else if inner.get("success").is_some() {
        // ②-b 带 success 但无 data。与流式 tool_result 事件使用同一个 success
        // 信号（tool_core/src/lib.rs:351）。
        let ok = inner
            .get("success")
            .and_then(|v| v.as_bool())
            .ok_or_else(|| PluginError {
                message: format!(
                    "MCP tool '{}' returned non-boolean 'success' field: {:?} (envelope drift, refusing to default to success)",
                    tool_name, inner["success"]
                ),
                code: Some("PARSE_ERROR".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
        if ok {
            let data = if inner.get("output").is_some() {
                inner
                    .get("output")
                    .cloned()
                    .unwrap_or(serde_json::Value::Null)
            } else {
                inner.clone()
            };
            Ok(ToolExecutionResult {
                success: true,
                data,
                error: None,
                duration_ms: None,
                metadata: inner.get("metadata").cloned(),
            })
        } else {
            let err_msg = inner
                .get("error")
                .and_then(|v| v.as_str())
                .unwrap_or("tool execution failed");
            Ok(ToolExecutionResult {
                success: false,
                data: Value::Null,
                error: Some(err_msg.to_string()),
                duration_ms: None,
                metadata: inner.get("metadata").cloned(),
            })
        }
    } else {
        // ③ 纯业务数据（无 success 键）→ 包成 success 信封。顶层 error 键不再
        // 是失败信号（成败只认显式 success 键，见函数 docstring）。
        Ok(ToolExecutionResult::success(inner))
    }
}

/// B2：native 工具调用返回归一（对齐 [`PluginInvokerImpl::invoke_tool`] sidecar
/// 决策树的归一层：纯业务数据包 success 信封）。
/// 插件 execute 的返回 JSON 按形状分流：
/// - 带 `success`（bool）→ ToolExecutionResult 信封形态（新工具插件的约定返回）：
///   - `success=true` → success(data 字段，缺省 Null)，保留 duration_ms；
///   - `success=false` → failure(error 字段，缺省通用文案)。
/// - 无 `success` → 旧 pipeline 插件（忽略 tool_call，返回 state_updates）→ 纯业务
///   数据包 success 信封——旧插件零破坏。
///
/// 与 sidecar 决策树口径一致：成败只认显式 `success` 键，顶层 `error` 键不是
/// 失败信号（旧插件 state_updates 可能天然含 `error` 键，按错误猜测会破坏零
/// 兼容承诺）。sidecar 的 MCP isError=true 协议层失败由 `extract_mcp_content`
/// 在提取点显式转 `{success: false, error}`，与 native 的 Err 错误通道同判。
pub(super) fn normalize_native_tool_output(inner: &Value) -> ToolExecutionResult {
    match inner.get("success").and_then(|v| v.as_bool()) {
        Some(true) => {
            let mut result =
                ToolExecutionResult::success(inner.get("data").cloned().unwrap_or(Value::Null));
            result.duration_ms = inner.get("duration_ms").and_then(|v| v.as_u64());
            result.metadata = inner.get("metadata").cloned();
            result
        }
        Some(false) => {
            let err_msg = inner
                .get("error")
                .and_then(|v| v.as_str())
                .unwrap_or("native tool execution failed");
            ToolExecutionResult::failure(err_msg)
        }
        None => ToolExecutionResult::success(inner.clone()),
    }
}
