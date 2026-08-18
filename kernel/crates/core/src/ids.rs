//! 内容寻址身份计算（message/blob 指纹）。
//!
//! 单一定义点：引擎写表（message_slots）、轨迹实录（ops 即轨迹的 message_id 指纹）、
//! 回放重建（指纹 → blobs 回查）共用同一组函数，保证"指纹 = 内容锚"处处一致。
//!
//! ## message_id 语义（op 模型定稿）
//!
//! `compute_message_id` 对**整条消息的规范化 JSON** 取 SHA256（前缀 `mc_`）：
//! - 消息是不可变值：内容变 → id 变；同内容（规范化后相同）→ 同 id。
//! - 规范化排除**位置与内部标记**（它们不是内容）：
//!   - 顶层 `seq`（引擎分配的稳定槽位号）
//!   - 顶层 `_` 前缀字段（插件内部标记，如已退役的 `_record_sequence`）
//! - 持久化专用字段（如 tool 消息的 `tool_result` envelope）**参与** hash——
//!   它是消息持久形态的一部分。
//! - key 序确定性：`serde_json::Map` 默认 BTreeMap，序列化按 key 排序
//!   （workspace 未启用 preserve_order），同内容必得同字节流。
//!
//! 与 `compute_blob_id` 的区别：blob_id 只对**字节内容**（消息全文 JSON / 附件字节）
//! 寻址；message_id 是消息身份（规范化后的内容锚）。

use serde_json::Value;
use sha2::{Digest, Sha256};

/// 计算字节内容的 SHA256（十六进制，无前缀）——blob 内容寻址 id。
pub fn compute_blob_id(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    format!("{:x}", hasher.finalize())
}

/// 计算消息的规范化内容 SHA256（前缀 `mc_`）——消息身份指纹。
pub fn compute_message_id(msg: &Value) -> String {
    let canonical = canonical_message(msg);
    // Value 序列化不可能失败（对象键必为 String、无 IO）；若失败宁可 panic
    // 也不能退空串——空串会让所有消息得到同一指纹，静默摧毁去重身份。
    let json =
        serde_json::to_string(&canonical).expect("serde_json Value serialization is infallible");
    let mut hasher = Sha256::new();
    hasher.update(json.as_bytes());
    format!("mc_{:x}", hasher.finalize())
}

/// 消息规范化：克隆并剥离"位置与内部标记"字段（仅顶层）。
fn canonical_message(msg: &Value) -> Value {
    let Some(obj) = msg.as_object() else {
        return msg.clone();
    };
    let mut filtered = obj.clone();
    filtered.remove("seq");
    filtered.retain(|k, _| !k.starts_with('_'));
    Value::Object(filtered)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn blob_id_is_sha256_hex() {
        let id = compute_blob_id(b"hello");
        assert_eq!(
            id,
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        );
        assert_eq!(
            compute_blob_id(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }

    #[test]
    fn same_message_same_id() {
        let a = json!({"role": "user", "content": "你好"});
        let b = json!({"content": "你好", "role": "user"}); // key 序不同
        assert_eq!(
            compute_message_id(&a),
            compute_message_id(&b),
            "key 序不应影响 id"
        );
    }

    #[test]
    fn content_change_changes_id() {
        let a = json!({"role": "user", "content": "v1"});
        let b = json!({"role": "user", "content": "v2"});
        assert_ne!(compute_message_id(&a), compute_message_id(&b));
    }

    #[test]
    fn role_and_tool_calls_are_content() {
        let base = json!({"role": "assistant", "content": ""});
        let with_tc = json!({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "ls", "arguments": "{}"}}]
        });
        assert_ne!(compute_message_id(&base), compute_message_id(&with_tc));
    }

    #[test]
    fn seq_and_internal_fields_excluded() {
        let plain = json!({"role": "user", "content": "hi"});
        let with_seq = json!({"role": "user", "content": "hi", "seq": 42});
        let with_internal = json!({"role": "user", "content": "hi", "_record_sequence": 7});
        assert_eq!(
            compute_message_id(&plain),
            compute_message_id(&with_seq),
            "槽位 seq 不是内容"
        );
        assert_eq!(
            compute_message_id(&plain),
            compute_message_id(&with_internal),
            "内部标记不是内容"
        );
    }

    #[test]
    fn persistent_fields_are_content() {
        // tool_result envelope 等持久化字段参与身份（消息持久形态的一部分）
        let plain = json!({"role": "tool", "content": "ok"});
        let with_env = json!({
            "role": "tool", "content": "ok",
            "tool_result": {"call_id": "c1", "success": true}
        });
        assert_ne!(compute_message_id(&plain), compute_message_id(&with_env));
    }

    #[test]
    fn id_prefix_is_mc() {
        let id = compute_message_id(&json!({"role": "user", "content": "x"}));
        assert!(id.starts_with("mc_") && id.len() == 3 + 64);
    }
}
