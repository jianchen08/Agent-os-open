//! 消息增量 ops 构造（OpenAI 规范）+ 多模态图片处理——单调用契约。
//!
//! 对齐 plugin.py `_build_message_ops`：assistant 补造（快照缺配对消息时）+
//! tool 结果 op（SDK build_tool_result_ops 单调用分支）+ 多模态 user 消息 op。
//! 这是 LLM 上下文协议，不能改字段格式。

use agentos_native_sdk::HostServices;
use serde_json::{json, Value};

use crate::types::ToolResult;

/// 构造本调用的消息增量 ops（对齐 plugin.py `_build_message_ops`）。
///
/// 返回 op 数组（`{"op":"set","msg":...}`，set 无 seq = 引擎 append）。
/// 多模态事件（tool_multimedia_result）在此路径内发（有图才发）。
pub fn build_message_ops(
    state: &Value,
    raw_call: &Value,
    result: &ToolResult,
    display: &ToolResult,
    host: Option<&dyn HostServices>,
) -> Vec<Value> {
    let mut ops: Vec<Value> = Vec::new();

    // 快照已有 assistant tool_calls 消息则不补造（对齐 plugin.py 配对检查：
    // tool_calls 键须在场且非 null）。
    let has_tool_call_msg = state
        .get("messages")
        .and_then(|v| v.as_array())
        .is_some_and(|msgs| {
            msgs.iter().any(|m| {
                m.get("role").and_then(|v| v.as_str()) == Some("assistant") && {
                    m.get("tool_calls").is_some_and(|v| !v.is_null())
                }
            })
        });
    if !has_tool_call_msg {
        ops.push(json!({ "op": "set", "msg": assistant_message(state, raw_call) }));
    }

    // tool 结果 op：构造后按 output_truncated 追加截断注记（对齐 plugin.py
    // `_truncation_note`：file_write/file_append 追加续写引导）。
    let mut tool_msg = tool_result_message(raw_call, display);
    let output_truncated = state
        .get("output_truncated")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    if output_truncated && display.success {
        let note = truncation_note(&display.data, &display.tool_name);
        let content = tool_msg
            .get("content")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        tool_msg["content"] = Value::String(format!("{content}{note}"));
    }
    ops.push(json!({ "op": "set", "msg": tool_msg }));

    // 多模态：从全文结果收集图片（截断可能丢 base64，图片必须存活）。
    let pending = collect_pending_images(result);
    if !pending.is_empty() {
        emit_multimodal_event(state, &pending, host);
        ops.push(json!({ "op": "set", "msg": multimodal_message(state, &pending) }));
    }
    ops
}

/// 补造 assistant tool_calls 消息（OpenAI 规范，对齐 plugin.py
/// `_build_assistant_message`）。
fn assistant_message(state: &Value, raw_call: &Value) -> Value {
    let name = raw_call.get("name").and_then(|v| v.as_str()).unwrap_or("");
    let arguments = raw_call
        .get("args")
        .cloned()
        .or_else(|| raw_call.get("arguments").cloned())
        .unwrap_or(json!({}));
    // arguments 原样透传字符串；对象转 JSON 字符串（OpenAI 规范）。
    let arguments_str = match arguments {
        Value::String(s) => s,
        other => other.to_string(),
    };
    let call_id = raw_call
        .get("id")
        .and_then(|v| v.as_str())
        .map(String::from)
        .unwrap_or_else(|| "call_0".into());
    let mut msg = json!({
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": { "name": name, "arguments": arguments_str },
            }
        ],
    });
    if let Some(thinking) = state
        .get(crate::types::StateKey::RAW_THINKING)
        .filter(|v| !v.is_null())
    {
        msg["reasoning_content"] = thinking.clone();
    }
    msg
}

/// 构造本调用的 role=tool 结果消息（对齐 SDK `build_tool_result_ops` 单调用
/// 分支：envelope 七键封闭，形状 = 契约夹具锚定）。
fn tool_result_message(raw_call: &Value, entry: &ToolResult) -> Value {
    let call_id = raw_call
        .get("id")
        .and_then(|v| v.as_str())
        .map(String::from)
        .unwrap_or_else(|| "call_0".into());
    let content = if entry.success {
        serialize_for_content(&entry.data).unwrap_or_else(|_| entry.data.to_string())
    } else {
        format!("Error: {}", entry.error.as_deref().unwrap_or("unknown"))
    };
    json!({
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
        "tool_result": {
            "call_id": call_id,
            "tool_name": entry.tool_name,
            "success": entry.success,
            "error": entry.error,
            "data": entry.data,
            "metadata": entry.metadata,
            "duration_ms": entry.duration_ms,
        },
    })
}

/// max_tokens 截断注记（对齐 plugin.py `_truncation_note`）。
fn truncation_note(data: &Value, tool_name: &str) -> String {
    let mut note = match data.get("lines").filter(|v| !v.is_null()) {
        Some(lines) => format!(
            "\n\n⚠️ 本次输出因达到 max_tokens 被截断，结果可能基于不完整参数。 已写入 {lines} 行。"
        ),
        None => "\n\n⚠️ 本次输出因达到 max_tokens 被截断，结果可能基于不完整参数。".to_string(),
    };
    if tool_name == "file_write" || tool_name == "file_append" {
        note.push_str(" 如内容未写完，请用 file_write(action=append) 追加续写，勿用 write 覆盖。");
    }
    note
}

/// 从全文结果收集图片（对齐 plugin.py `_collect_pending_images`）：
/// data.base64_data+mime_type / data.images[] / metadata.multimodal_content
/// data URL 三来源。
fn collect_pending_images(result: &ToolResult) -> Vec<PendingImage> {
    let mut pending: Vec<PendingImage> = Vec::new();
    if let Some(data) = result.data.as_object() {
        if let (Some(b64), Some(mime)) = (
            data.get("base64_data").and_then(|v| v.as_str()),
            data.get("mime_type").and_then(|v| v.as_str()),
        ) {
            pending.push(PendingImage {
                base64: b64.to_string(),
                mime_type: mime.to_string(),
                path: data
                    .get("path")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
            });
        }
        if let Some(imgs) = data.get("images").and_then(|v| v.as_array()) {
            for img in imgs {
                let Some(b64) = img.get("base64").and_then(|v| v.as_str()) else {
                    continue;
                };
                pending.push(PendingImage {
                    base64: b64.to_string(),
                    mime_type: img
                        .get("mime_type")
                        .and_then(|v| v.as_str())
                        .unwrap_or("image/png")
                        .to_string(),
                    path: img
                        .get("path")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string(),
                });
            }
        }
    }
    if let Some(meta) = result.metadata.as_ref().and_then(|m| m.as_object()) {
        let Some(blocks) = meta.get("multimodal_content").and_then(|v| v.as_array()) else {
            return pending;
        };
        for block in blocks {
            if block.get("type").and_then(|v| v.as_str()) != Some("image_url") {
                continue;
            }
            let url = block
                .get("image_url")
                .and_then(|v| v.as_object())
                .and_then(|o| o.get("url"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let Some(rest) = url.strip_prefix("data:") else {
                continue;
            };
            if let Some((mime, b64)) = rest.split_once(";base64,") {
                pending.push(PendingImage {
                    base64: b64.to_string(),
                    mime_type: mime.to_string(),
                    path: String::new(),
                });
            }
        }
    }
    pending
}

/// 发 tool_multimedia_result 事件（仅 mime_type + path，不含 base64）。
fn emit_multimodal_event(state: &Value, pending: &[PendingImage], host: Option<&dyn HostServices>) {
    let Some(host) = host else { return };
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
    let multimedia: Vec<Value> = pending
        .iter()
        .map(|img| json!({ "mime_type": img.mime_type, "path": img.path }))
        .collect();
    let payload = json!({
        "thread_id": thread_id,
        "pipeline_id": pipeline_id,
        "message_id": message_id,
        "count": pending.len(),
        "multimedia": multimedia,
    });
    let params = json!({ "event": "tool_multimedia_result", "payload": payload });
    let params_json = serde_json::to_string(&params).unwrap_or_default();
    // fire-and-forget：忽略返回（观测面失败不阻断主流程）。
    let _ = host.call_capability("event-bus", "emit", &params_json);
}

/// 视觉可用 → 多模态 user 消息；不可用 → 文本引导（对齐 plugin.py
/// `_build_multimodal_message`）。
fn multimodal_message(state: &Value, pending: &[PendingImage]) -> Value {
    let supports_vision = state
        .get("llm_supports_vision")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    if supports_vision {
        let mut blocks: Vec<Value> = vec![json!({
            "type": "text",
            "text": format!("[工具截图] 共 {} 张图片，请分析截图内容：", pending.len()),
        })];
        for img in pending {
            blocks.push(json!({
                "type": "image_url",
                "image_url": { "url": format!("data:{};base64,{}", img.mime_type, img.base64) },
            }));
        }
        json!({ "role": "user", "name": "tool_images", "content": blocks })
    } else {
        let paths: Vec<&str> = pending
            .iter()
            .map(|i| i.path.as_str())
            .filter(|p| !p.is_empty())
            .collect();
        let paths_str = if paths.is_empty() {
            "见工具返回".to_string()
        } else {
            paths.join(", ")
        };
        let content = format!(
            "[工具截图] 已保存 {} 张截图（{}）。当前模型不支持图片分析，请使用 mcp__4_5v_mcp__analyze_image 工具分析截图内容，获取文本描述后继续验证。",
            pending.len(),
            paths_str
        );
        json!({ "role": "user", "name": "tool_images", "content": content })
    }
}

/// 序列化工具结果数据为消息 content。
///
/// 文本直通（ADR 2026-11-09-tool-result-content-verbatim）：**任何工具**的
/// 文本载荷都以原文逐字进上下文——data 本身是字符串，或对象含字符串
/// `content` 字段（文本载荷统一键）。YAML 块标量会给每行强制加缩进，模型从
/// 渲染文本复制片段回写 `search_replace` 必带多余空格而恒失败（任务
/// 2393a90baea9 实证 8 次 `old_str not found`，每行恰多 2 空格）。
/// 其余形态（结构化结果）仍走 YAML（对齐 Python
/// `tool_result_protocol.serialize_for_content`；双车道契约夹具锚定）。
///
/// 失败回退 to_string（对齐 plugin.py serialize_for_content 的兜底语义）。
pub fn serialize_for_content(data: &Value) -> Result<String, String> {
    // None / Null → 空字符串（避免 "null" 进上下文）。
    if data.is_null() {
        return Ok(String::new());
    }
    if let Value::String(text) = data {
        return Ok(text.clone());
    }
    if let Some(Value::String(text)) = data.get("content") {
        return Ok(text.clone());
    }
    serde_yaml::to_string(data).map_err(|e| e.to_string())
}

struct PendingImage {
    base64: String,
    mime_type: String,
    path: String,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::StateKey;

    #[test]
    fn assistant_synthesized_with_thinking_and_call_fallback() {
        let state = json!({ StateKey::RAW_THINKING: "think" });
        let raw_call = json!({"name": "f", "args": {"a": 1}});
        let msg = assistant_message(&state, &raw_call);
        assert_eq!(msg["role"], "assistant");
        assert_eq!(msg["tool_calls"][0]["id"], "call_0", "无 id 兜底 call_0");
        assert_eq!(msg["tool_calls"][0]["function"]["name"], "f");
        let args: Value = serde_json::from_str(
            msg["tool_calls"][0]["function"]["arguments"]
                .as_str()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(args, json!({"a": 1}));
        assert_eq!(msg["reasoning_content"], "think");
        // raw_thinking 缺席 → 无 reasoning_content 键。
        let msg2 = assistant_message(&json!({}), &raw_call);
        assert!(msg2.get("reasoning_content").is_none());
    }

    #[test]
    fn assistant_arguments_string_passthrough() {
        let raw_call = json!({"name": "f", "id": "c9", "arguments": "{\"a\":1}"});
        let msg = assistant_message(&json!({}), &raw_call);
        assert_eq!(msg["tool_calls"][0]["id"], "c9");
        assert_eq!(msg["tool_calls"][0]["function"]["arguments"], "{\"a\":1}");
    }

    #[test]
    fn tool_op_success_and_failure_contract() {
        // 成功：content = serialize(data)，envelope 七键封闭。
        let call = json!({"name": "bash_execute", "id": "call_abc"});
        let ok = ToolResult::succeeded("bash_execute", json!({"output": "hi\n"}), 5.0);
        let msg = tool_result_message(&call, &ok);
        assert_eq!(msg["role"], "tool");
        assert_eq!(msg["tool_call_id"], "call_abc");
        assert_eq!(
            msg["tool_result"].as_object().unwrap().len(),
            7,
            "envelope 七键封闭"
        );
        assert!(msg["content"].as_str().unwrap().contains("hi"));
        // 失败：content = "Error: {error}"，data 归 null。
        let bad = ToolResult::failed("f", "boom", 1.0);
        let msg2 = tool_result_message(&json!({"name": "f", "id": "c1"}), &bad);
        assert_eq!(msg2["content"], "Error: boom");
        assert_eq!(msg2["tool_result"]["data"], Value::Null);
    }

    #[test]
    fn content_field_passthrough_verbatim() {
        // 文本直通：多行原文逐字，行首缩进/引号/反斜杠原样，不补尾换行
        // （事故 2393a90baea9 根因：YAML 块标量给每行 +2 空格）。
        let text = "# 标题\n\n  2. **缩进行保留** —— \"引号\" \\反斜杠\n- 列表尾行";
        let data = json!({
            "file": "D:\\w\\a.md",
            "lines": 5,
            "size": 42,
            "content": text,
        });
        let out = serialize_for_content(&data).unwrap();
        assert_eq!(out, text, "content 必须逐字直通");
        let msg = tool_result_message(
            &json!({"name": "file_read", "id": "call_fr"}),
            &ToolResult::succeeded("file_read", data, 1.0),
        );
        assert_eq!(msg["content"], text, "role=tool 消息正文 = 文件原文");
        // 空字符串同样直通（空文件不该渲染成 "content: ''"）。
        assert_eq!(serialize_for_content(&json!({"content": ""})).unwrap(), "");
        // 纯字符串结果同样直通（任何工具的文本载荷）。
        assert_eq!(
            serialize_for_content(&json!("plain text")).unwrap(),
            "plain text"
        );
        assert_eq!(serialize_for_content(&json!("42")).unwrap(), "42");
        // 无 content 键 → 仍 YAML（键序 BTreeMap 字典序）。
        assert_eq!(
            serialize_for_content(&json!({"b": 2, "a": 1})).unwrap(),
            "a: 1\nb: 2\n"
        );
        // content 非字符串 → 仍 YAML（不误吞结构化载荷）。
        assert_eq!(
            serialize_for_content(&json!({"content": {"x": 1}})).unwrap(),
            "content:\n  x: 1\n"
        );
    }

    #[test]
    fn truncation_note_file_write_guides_append() {
        let note = truncation_note(&json!({"lines": 3}), "file_write");
        assert!(note.contains("已写入 3 行"), "{note}");
        assert!(note.contains("action=append"), "{note}");
        // 非 file_write：无续写引导；无 lines：无行数句。
        let plain = truncation_note(&json!({}), "bash_execute");
        assert!(plain.contains("max_tokens"));
        assert!(!plain.contains("已写入"));
        assert!(!plain.contains("action=append"));
    }

    fn multimodal_state(vision: bool) -> Value {
        json!({
            "session_id": "sess-1",
            "pipeline_id": "pipe-1",
            "message_id": "msg-1",
            "llm_supports_vision": vision,
        })
    }

    struct RecordingEventHost {
        events: std::sync::Mutex<Vec<String>>,
    }

    impl HostServices for RecordingEventHost {
        fn call_capability(
            &self,
            _capability: &str,
            _method: &str,
            params_json: &str,
        ) -> Result<&str, &str> {
            self.events.lock().unwrap().push(params_json.to_string());
            Ok("{}")
        }
    }

    #[test]
    fn multimodal_vision_true_injects_user_blocks_and_event() {
        let host = RecordingEventHost {
            events: std::sync::Mutex::new(Vec::new()),
        };
        let result = ToolResult {
            tool_name: "screenshot".into(),
            success: true,
            error: None,
            data: json!({"base64_data": "AAAA", "mime_type": "image/png", "path": "/s/a.png"}),
            metadata: Some(json!({
                "multimodal_content": [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BBBB"}}
                ]
            })),
            duration_ms: 1.0,
            extra: None,
        };
        let pending = collect_pending_images(&result);
        assert_eq!(pending.len(), 2);
        emit_multimodal_event(&multimodal_state(true), &pending, Some(&host));
        let events = host.events.lock().unwrap();
        assert_eq!(events.len(), 1);
        let params: Value = serde_json::from_str(&events[0]).unwrap();
        let payload = &params["payload"];
        assert_eq!(params["event"], "tool_multimedia_result");
        assert_eq!(payload["count"], 2);
        assert_eq!(
            payload["multimedia"][0],
            json!({"mime_type": "image/png", "path": "/s/a.png"})
        );
        assert!(
            !events.join("").contains("base64"),
            "事件载荷不含 base64（图片正文只进消息 op）"
        );

        let msg = multimodal_message(&multimodal_state(true), &pending);
        assert_eq!(msg["role"], "user");
        assert_eq!(msg["name"], "tool_images");
        let blocks = msg["content"].as_array().unwrap();
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

    #[test]
    fn multimodal_without_vision_guides_text_analysis() {
        let result = ToolResult {
            tool_name: "screenshot".into(),
            success: true,
            error: None,
            data: json!({"images": [{"base64": "CCCC", "path": "/s/b.png"}]}),
            metadata: None,
            duration_ms: 1.0,
            extra: None,
        };
        let pending = collect_pending_images(&result);
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].mime_type, "image/png", "mime 缺省 image/png");
        let msg = multimodal_message(&multimodal_state(false), &pending);
        let content = msg["content"].as_str().unwrap();
        assert!(content.contains("不支持图片分析"), "{content}");
        assert!(content.contains("/s/b.png"), "{content}");
    }

    /// 契约夹具一致性（ADR 2026-09-28）：与 SDK Python `build_tool_result_ops`
    /// 双车道消费同一份夹具（plugins/sdk/tests/contracts/），两边必须同绿——
    /// 漂移即红；删值实验 = 改坏任一期望值两车道同时红。
    #[test]
    fn fixture_parity_with_sdk_tool_result_protocol() {
        let path = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../sdk/tests/contracts/tool_result_messages.fixture.json"
        );
        let raw = std::fs::read_to_string(path).expect("契约夹具存在");
        let fixture: Value = serde_json::from_str(&raw).expect("夹具 JSON 合法");
        let vectors = fixture["vectors"].as_array().expect("vectors 数组");
        assert!(vectors.len() >= 5, "夹具向量数异常（被清空即红）");
        for v in vectors {
            let name = v["name"].as_str().unwrap_or("?");
            let tool_calls = v["tool_calls"].as_array().expect("tool_calls");
            let expected = v["expected_msgs"].as_array().expect("expected_msgs");
            let entries = v["results"].as_array().expect("results");
            for (i, tc) in tool_calls.iter().enumerate() {
                let entry = &entries[i];
                let result = ToolResult {
                    tool_name: entry["tool_name"].as_str().unwrap_or("unknown").to_string(),
                    success: entry["success"].as_bool().unwrap_or(false),
                    error: entry["error"].as_str().map(String::from),
                    data: entry["data"].clone(),
                    metadata: entry.get("metadata").cloned().filter(|m| !m.is_null()),
                    duration_ms: entry["duration_ms"].as_f64().unwrap_or(0.0),
                    extra: None,
                };
                let got = tool_result_message(tc, &result);
                assert_eq!(
                    got, expected[i],
                    "向量 {name}[{i}]: 消息体不一致（夹具=双语言单一真值）"
                );
            }
        }
    }
}
