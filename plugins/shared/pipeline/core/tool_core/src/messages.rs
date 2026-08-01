//! messages 重建（OpenAI 规范）+ 多模态图片处理。
//!
//! 对齐 plugin.py:784-963。这是 LLM 上下文协议，不能改字段格式。

use abi_stable::std_types::{ROption, RString};
use agentos_native_sdk::HostServicesBox;
use serde_json::{json, Value};

use crate::types::{StateKey, ToolResult};

/// 重建 messages：追加 assistant tool_calls 消息 + tool 结果消息（对齐 plugin.py:784-859）。
pub fn rebuild(state: &Value, tool_calls_raw: &[Value], results: &[ToolResult]) -> Vec<Value> {
    let mut current: Vec<Value> = state
        .get("messages")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    // 是否已有 assistant tool_calls 消息。
    let has_tool_call_msg = current
        .iter()
        .any(|m| m.get("role").and_then(|v| v.as_str()) == Some("assistant")
            && m.get("tool_calls").is_some());

    // 预解析 tc_ids（保持 assistant 消息与 tool 结果使用一致 id）。
    let tc_ids: Vec<String> = tool_calls_raw
        .iter()
        .map(|tc| {
            tc.get("id")
                .and_then(|v| v.as_str())
                .map(String::from)
                .unwrap_or_else(|| format!("call_{}", &uuid::Uuid::new_v4().simple().to_string()[..8]))
        })
        .collect();

    // 若无 assistant tool_calls 消息，先构造一条（对齐 plugin.py:801-823）。
    if !has_tool_call_msg && !tool_calls_raw.is_empty() {
        let tool_calls_json: Vec<Value> = tool_calls_raw
            .iter()
            .enumerate()
            .map(|(i, tc)| {
                let id = tc_ids.get(i).cloned().unwrap_or_default();
                let name = tc.get("name").and_then(|v| v.as_str()).unwrap_or("");
                // arguments 原样透传（OpenAI 规范要求字符串；与 LLMCore 一致不做 dict→str）。
                let arguments = tc
                    .get("args")
                    .cloned()
                    .or_else(|| tc.get("arguments").cloned())
                    .unwrap_or_else(|| Value::String(String::new()));
                // 若 arguments 是对象，转成 JSON 字符串（OpenAI 规范）。
                let arguments_str = match arguments {
                    Value::String(s) => Value::String(s),
                    other => Value::String(other.to_string()),
                };
                json!({
                    "id": id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": arguments_str,
                    }
                })
            })
            .collect();

        let mut assistant_msg = json!({
            "role": "assistant",
            "content": "",
            "tool_calls": tool_calls_json,
        });
        // reasoning_content（与 LLMCore 一致）。
        if let Some(rc) = state.get(StateKey::RAW_THINKING) {
            if !rc.is_null() {
                assistant_msg
                    .as_object_mut()
                    .expect("assistant_msg is object")
                    .insert("reasoning_content".into(), rc.clone());
            }
        }
        current.push(assistant_msg);
    }

    // 追加 tool 结果消息（对齐 plugin.py:830-859）。
    let output_truncated = state.get("output_truncated").and_then(|v| v.as_bool()).unwrap_or(false);
    for (i, result) in results.iter().enumerate() {
        let tc_id = tc_ids.get(i).cloned().unwrap_or_else(|| {
            format!("call_{}", &uuid::Uuid::new_v4().simple().to_string()[..8])
        });
        let result_data = if result.success { &result.data } else { &Value::Null };
        let content = if result.success {
            let serialized = serialize_for_content(result_data).unwrap_or_else(|_| result_data.to_string());
            if output_truncated {
                let tool_name = result.tool_name.as_str();
                let written_lines = result_data.get("lines").cloned();
                let note = match written_lines {
                    Some(lines) => format!(
                        "\n\n⚠️ 本次输出因达到 max_tokens 被截断，结果可能基于不完整参数。 已写入 {lines} 行。"
                    ),
                    None => "\n\n⚠️ 本次输出因达到 max_tokens 被截断，结果可能基于不完整参数。".to_string(),
                };
                let note = if tool_name == "file_write" || tool_name == "file_append" {
                    format!("{note} 如内容未写完，请用 file_write(action=append) 追加续写，勿用 write 覆盖。")
                } else {
                    note
                };
                format!("{serialized}{note}")
            } else {
                serialized
            }
        } else {
            format!("Error: {}", result.error.as_deref().unwrap_or("unknown"))
        };
        current.push(json!({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": content,
        }));
    }

    current
}

/// 收集多模态图片并注入 messages（对齐 plugin.py:861-963）。
///
/// 返回 (更新后的 messages, 是否发出了 tool_multimedia_result 事件)。
pub fn inject_multimodal(
    state: &Value,
    mut messages: Vec<Value>,
    results: &[ToolResult],
    host: &ROption<HostServicesBox>,
) -> (Vec<Value>, bool) {
    // 三个来源收集 pending_images。
    let mut pending: Vec<PendingImage> = Vec::new();

    for r in results {
        let Some(data) = r.data.as_object() else { continue };
        // 来源 1：base64_data + mime_type。
        if let (Some(b64), Some(mime)) = (
            data.get("base64_data").and_then(|v| v.as_str()),
            data.get("mime_type").and_then(|v| v.as_str()),
        ) {
            pending.push(PendingImage {
                base64: b64.to_string(),
                mime_type: mime.to_string(),
                path: data.get("path").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            });
        }
        // 来源 2：images[]。
        if let Some(imgs) = data.get("images").and_then(|v| v.as_array()) {
            for img in imgs {
                if let Some(b64) = img.get("base64").and_then(|v| v.as_str()) {
                    pending.push(PendingImage {
                        base64: b64.to_string(),
                        mime_type: img.get("mime_type").and_then(|v| v.as_str()).unwrap_or("image/png").to_string(),
                        path: img.get("path").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                    });
                }
            }
        }
    }

    // 来源 3：metadata.multimodal_content 的 data URL。
    for r in results {
        let Some(meta) = r.metadata.as_ref().and_then(|m| m.as_object()) else { continue };
        let Some(mm) = meta.get("multimodal_content").and_then(|v| v.as_array()) else { continue };
        for block in mm {
            if block.get("type").and_then(|v| v.as_str()) == Some("image_url") {
                let url = block
                    .get("image_url")
                    .and_then(|v| v.as_object())
                    .and_then(|o| o.get("url"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                if let Some(rest) = url.strip_prefix("data:") {
                    if let Some((mime, b64)) = rest.split_once(";base64,") {
                        pending.push(PendingImage {
                            base64: b64.to_string(),
                            mime_type: mime.to_string(),
                            path: String::new(),
                        });
                    }
                }
            }
        }
    }

    if pending.is_empty() {
        return (messages, false);
    }

    // 发 tool_multimedia_result 事件（仅 mime_type + path，不含 base64，对齐 plugin.py:904-917）。
    if let ROption::RSome(host) = host {
        let thread_id = state.get("session_id").and_then(|v| v.as_str())
            .or_else(|| state.get("thread_id").and_then(|v| v.as_str()))
            .unwrap_or("");
        let pipeline_id = state.get("pipeline_id").and_then(|v| v.as_str()).unwrap_or("");
        let message_id = state.get("message_id").and_then(|v| v.as_str()).unwrap_or("");
        let multimedia: Vec<Value> = pending
            .iter()
            .map(|img| {
                json!({
                    "mime_type": img.mime_type,
                    "path": img.path,
                })
            })
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
        // fire-and-forget：忽略返回。
        let _ = host.call_capability(
            RString::from("event-bus"),
            RString::from("emit"),
            RString::from(params_json),
        );
    }

    // 模型视觉能力：首版从 state 读标志（prepare 链注入），对齐 Python ModelCapabilityRegistry。
    let supports_vision = state.get("llm_supports_vision").and_then(|v| v.as_bool()).unwrap_or(false);

    if supports_vision {
        // 路径 A：注入多模态 user 消息。
        let mut content_blocks: Vec<Value> = vec![json!({
            "type": "text",
            "text": format!("[工具截图] 共 {} 张图片，请分析截图内容：", pending.len()),
        })];
        for img in &pending {
            let data_url = format!("data:{};base64,{}", img.mime_type, img.base64);
            content_blocks.push(json!({
                "type": "image_url",
                "image_url": { "url": data_url },
            }));
        }
        messages.push(json!({
            "role": "user",
            "name": "tool_images",
            "content": content_blocks,
        }));
    } else {
        // 路径 B：提示 agent 调 MCP 分析。
        let paths: Vec<&str> = pending.iter().filter(|i| !i.path.is_empty()).map(|i| i.path.as_str()).collect();
        let paths_str = if paths.is_empty() { "见工具返回".to_string() } else { paths.join(", ") };
        let content = format!(
            "[工具截图] 已保存 {} 张截图（{}）。当前模型不支持图片分析，请使用 mcp__4_5v_mcp__analyze_image 工具分析截图内容，获取文本描述后继续验证。",
            pending.len(),
            paths_str
        );
        messages.push(json!({
            "role": "user",
            "name": "tool_images",
            "content": content,
        }));
    }

    (messages, true)
}

/// 序列化工具结果数据为消息 content（对齐 Python get_format_manager().serialize，默认 YAML）。
///
/// 失败回退 to_string（对齐 plugin.py:834-836 的 except 分支）。
pub fn serialize_for_content(data: &Value) -> Result<String, String> {
    // None / Null → 空字符串（避免 "null" 进上下文）。
    if data.is_null() {
        return Ok(String::new());
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

    #[test]
    fn rebuild_appends_assistant_and_tool() {
        let state = json!({});
        let tool_calls = vec![json!({"name": "bash_execute", "id": "call_abc", "args": {"command": "echo hi"}})];
        let results = vec![ToolResult::succeeded("bash_execute", json!({"output": "hi\n"}), 5.0)];
        let msgs = rebuild(&state, &tool_calls, &results);
        // 期望：assistant tool_calls + tool result
        assert_eq!(msgs.len(), 2);
        assert_eq!(msgs[0]["role"], "assistant");
        assert_eq!(msgs[0]["tool_calls"][0]["function"]["name"], "bash_execute");
        assert_eq!(msgs[1]["role"], "tool");
        assert_eq!(msgs[1]["tool_call_id"], "call_abc");
        assert!(msgs[1]["content"].as_str().unwrap().contains("hi"));
    }

    #[test]
    fn rebuild_skips_assistant_if_exists() {
        let state = json!({
            "messages": [{"role": "assistant", "tool_calls": [{"id": "x", "type": "function", "function": {"name": "f"}}]}]
        });
        let tool_calls = vec![json!({"name": "f", "id": "x", "args": {}})];
        let results = vec![ToolResult::succeeded("f", json!("ok"), 1.0)];
        let msgs = rebuild(&state, &tool_calls, &results);
        // 已有 assistant tool_calls → 只追加 1 条 tool 消息
        assert_eq!(msgs.len(), 2);
    }

    #[test]
    fn failed_result_content_is_error() {
        let state = json!({});
        let tool_calls = vec![json!({"name": "f", "id": "c1", "args": {}})];
        let results = vec![ToolResult::failed("f", "boom", 1.0)];
        let msgs = rebuild(&state, &tool_calls, &results);
        assert_eq!(msgs[1]["content"], "Error: boom");
    }

    #[test]
    fn serialize_yaml_for_object() {
        let data = json!({"a": 1, "b": "x"});
        let s = serialize_for_content(&data).unwrap();
        assert!(s.contains("a: 1"));
        assert!(s.contains("b: x"));
    }

    #[test]
    fn serialize_null_is_empty() {
        assert_eq!(serialize_for_content(&Value::Null).unwrap(), "");
    }
}
