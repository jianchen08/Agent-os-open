// @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: rust-test
//! 任务 4 + 任务 7 验收测试：**冷路径统一读表** + **消息整体进 blob / slots 纯索引**。
//!
//! 任务 4（docs/tasks/task_messages_op_trace_unification.md）：
//! store 新增 `load_message_history(pipeline_id, tenant_id) -> Vec<serde_json::Value>`——
//! `message_slots` join `blobs`，按 seq 升序重建完整消息对象（含 tool_calls 解析回对象）。
//! **不做旧 messages 表回退**（零兼容原则）。
//!
//! 任务 7：整条消息 JSON（role+content+tool_calls+reasoning_content+tool_result envelope）
//! 进 blobs（内容寻址去重）；`message_slots` 行删内容列、纯索引；对外接口形状不变
//! （`get_slot_messages_by_pipeline` 读时重建 MessageRecord，content_preview 填全文）。
//!
//! 断言：
//! - load_message_history：按 seq 升序（gap 不影响）、每条含 role/content **全文**（非
//!   preview）/tool_calls 解析回对象/自带 seq 字段；tool 消息保留 `tool_result` envelope；
//! - 同一内容消息写两遍，blobs 表只有一份；
//! - 读时重建：`MessageRecord.content_preview` 为全文（长文不截断）；
//! - slots 纯索引：表无 content_preview/tool_calls_json/reasoning_content/status/error 列。
//!
//! `load_message_history` 由主线实现（当前不存在）→ 本文件为 **red（编译失败，TDD red 阶段）**。
//!
//! API 形状假设（若主线实现为 `Result<Vec<Value>, _>` 或 trait 方法，仅需在调用点
//! 补 `.unwrap()` / 改经 `&dyn StorageBackend` 调用，断言不变）：
//! `SqliteStore::load_message_history(&self, pipeline_id: &str, tenant_id: &str)
//! -> Result<Vec<serde_json::Value>, StorageError>`（主线已落地为 Result 包装形态）

use agentos_core::traits::MessageQueryOpts;
use agentos_engine::SqliteStore;
use serde_json::{json, Value};

fn set(seq: u32, msg: Value) -> Value {
    json!({ "op": "set", "seq": seq, "msg": msg })
}

fn clear(seq: u32) -> Value {
    json!({ "op": "set", "seq": seq, "msg": null })
}

/// 带 tool_calls 的 assistant 消息（OpenAI 结构）。
fn assistant_with_tool_calls() -> Value {
    json!({
        "role": "assistant",
        "content": "需要调用工具",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": { "name": "file_read", "arguments": "{\"path\": \"/tmp/a\"}" }
            }
        ]
    })
}

/// 带 tool_result envelope 的 tool 消息（任务 3 零兼容清理后 envelope 是消息持久形态的一部分）。
fn tool_msg_with_envelope() -> Value {
    json!({
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "文件内容 HISTORY_ENVELOPE_MARKER",
        "tool_result": {
            "call_id": "call_1",
            "tool_name": "file_read",
            "success": true,
            "data": { "size": 3 }
        }
    })
}

fn blob_count(store: &SqliteStore) -> i64 {
    store
        .with_conn(|c| -> Result<i64, rusqlite::Error> {
            Ok(c.query_row("SELECT COUNT(*) FROM blobs", [], |r| r.get(0))?)
        })
        .expect("数 blobs 应成功")
}

fn table_columns(store: &SqliteStore, table: &str) -> Vec<String> {
    store
        .with_conn(|c| -> Result<Vec<String>, rusqlite::Error> {
            let mut stmt = c.prepare(&format!("PRAGMA table_info({table})"))?;
            let rows = stmt.query_map([], |r| r.get::<_, String>(1))?;
            Ok(rows.collect::<Result<Vec<_>, _>>()?)
        })
        .expect("读表结构应成功")
}

// ── 任务 4：load_message_history（新 API，当前不存在 = 编译 red）────────

/// 按 seq 升序重建完整消息：全文（非 preview）、tool_calls 解析回对象、自带 seq。
#[test]
fn load_message_history_rebuilds_full_messages_in_seq_order() {
    let store = SqliteStore::open_memory().unwrap();

    // 长文（>200 字符）验证"全文"而非 content_preview 截断
    let long_content = "长".repeat(250) + "HISTORY_FULLTEXT_END";
    store
        .apply_messages_ops_to_table(
            "p_hist",
            "default",
            &[
                set(0, json!({ "role": "user", "content": long_content })),
                set(1, assistant_with_tool_calls()),
                set(2, tool_msg_with_envelope()),
                set(3, json!({ "role": "user", "content": "会被删掉" })),
                set(4, json!({ "role": "assistant", "content": "末条" })),
            ],
        )
        .unwrap();
    // 清空槽 3（留 gap）：升序读取应跳过 gap、顺序仍正确
    store
        .apply_messages_ops_to_table("p_hist", "default", &[clear(3)])
        .unwrap();

    let hist = store
        .load_message_history("p_hist", "default")
        .expect("load_message_history 应成功");

    // 按 seq 升序，gap 不影响
    let seqs: Vec<u64> = hist.iter().filter_map(|m| m["seq"].as_u64()).collect();
    assert_eq!(seqs, vec![0, 1, 2, 4], "应按 seq 升序重建（槽 3 为 gap）");

    // 每条含 role + content 全文
    for m in &hist {
        assert!(
            m.get("role").and_then(|v| v.as_str()).is_some(),
            "每条消息应含 role：{}",
            m
        );
        assert!(
            m.get("content").and_then(|v| v.as_str()).is_some(),
            "每条消息应含 content：{}",
            m
        );
    }
    // 全文（非 preview）：250+ 字符完整读回
    assert_eq!(
        hist[0]["content"].as_str(),
        Some(long_content.as_str()),
        "content 应为 blob 全文，而非 200 字符 preview"
    );

    // tool_calls 解析回对象（数组，非 JSON 字符串）
    let tc = hist[1]["tool_calls"]
        .as_array()
        .expect("tool_calls 应解析回数组对象（非字符串）");
    assert_eq!(tc[0]["id"], "call_1");
    assert_eq!(tc[0]["function"]["name"], "file_read");
    assert_eq!(tc[0]["function"]["arguments"], "{\"path\": \"/tmp/a\"}");
}

/// tool 消息的 `tool_result` envelope 字段在 load_message_history 返回中保留
/// （envelope 搬家后是消息持久形态的一部分，随整条消息 JSON 进 blob）。
#[test]
fn load_message_history_preserves_tool_result_envelope() {
    let store = SqliteStore::open_memory().unwrap();
    store
        .apply_messages_ops_to_table("p_hist_env", "default", &[set(0, tool_msg_with_envelope())])
        .unwrap();

    let hist = store
        .load_message_history("p_hist_env", "default")
        .expect("load_message_history 应成功");
    assert_eq!(hist.len(), 1);
    assert_eq!(hist[0]["role"], "tool");
    assert_eq!(hist[0]["tool_call_id"], "call_1");
    let env = hist[0]
        .get("tool_result")
        .and_then(|v| v.as_object())
        .expect("tool_result envelope 应保留在返回消息里");
    assert_eq!(env.get("call_id").and_then(|v| v.as_str()), Some("call_1"));
    assert_eq!(env.get("tool_name").and_then(|v| v.as_str()), Some("file_read"));
    assert_eq!(env.get("success").and_then(|v| v.as_bool()), Some(true));
}

/// 空 pipeline：返回空数组（不回退旧 messages 表）。
#[test]
fn load_message_history_empty_pipeline_returns_empty() {
    let store = SqliteStore::open_memory().unwrap();
    let hist = store
        .load_message_history("p_never_written", "default")
        .expect("空 pipeline 也应成功返回空历史");
    assert!(hist.is_empty(), "无槽位写入的 pipeline 应返回空历史");
}

// ── 任务 7：blob 去重 + slots 纯索引 + 读时重建 ─────────────────────

/// 同一内容消息写两遍（两个槽位 + 重复 apply），blobs 表只有一份。
#[test]
fn identical_message_written_twice_stores_single_blob() {
    let store = SqliteStore::open_memory().unwrap();
    let msg = json!({ "role": "user", "content": "DEDUP_MARKER 同一条消息写两遍" });

    // 两个槽位、同内容
    store
        .apply_messages_ops_to_table("p_dedup", "default", &[set(0, msg.clone()), set(1, msg.clone())])
        .unwrap();
    assert_eq!(blob_count(&store), 1, "同内容消息 → blobs 只一份（内容寻址去重）");

    // 再整体写一遍：blob 数不变
    store
        .apply_messages_ops_to_table("p_dedup", "default", &[set(1, msg)])
        .unwrap();
    assert_eq!(blob_count(&store), 1, "重复写入不得新增 blob");

    // 不同内容 → 新 blob
    store
        .apply_messages_ops_to_table(
            "p_dedup",
            "default",
            &[set(2, json!({ "role": "user", "content": "DEDUP_MARKER 另一条不同消息" }))],
        )
        .unwrap();
    assert_eq!(blob_count(&store), 2, "不同内容才有新 blob");
}

/// 接口形状不变：`get_slot_messages_by_pipeline` 的 MessageRecord 读时重建，
/// content_preview 填**全文**（长文不截断——存储收敛，前端零改动）。
#[test]
fn slot_read_rebuilds_full_content_preview() {
    let store = SqliteStore::open_memory().unwrap();
    let long_content = "全文重建标记".repeat(80); // 400 字符 > 200
    store
        .apply_messages_ops_to_table(
            "p_preview",
            "default",
            &[set(0, json!({ "role": "assistant", "content": long_content }))],
        )
        .unwrap();

    let rows = store
        .get_slot_messages_by_pipeline("p_preview", "default", MessageQueryOpts::default())
        .unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(
        rows[0].content_preview.as_deref(),
        Some(long_content.as_str()),
        "content_preview 应为读时重建的全文（非存储列的 200 字符截断）"
    );
}

/// slots 纯索引：`message_slots` 表无内容列（content_preview/tool_calls_json/
/// reasoning_content/status/error 全部退役），索引列（seq/message_id/blob_id）保留。
#[test]
fn message_slots_table_is_pure_index_without_content_columns() {
    let store = SqliteStore::open_memory().unwrap();
    let cols = table_columns(&store, "message_slots");

    for banned in [
        "content_preview",
        "tool_calls_json",
        "reasoning_content",
        "status",
        "error",
        "role",
    ] {
        assert!(
            !cols.iter().any(|c| c == banned),
            "message_slots 应为纯索引，内容列 {banned} 应删除（内容在 blobs），实际列：{:?}",
            cols
        );
    }
    for required in ["tenant_id", "pipeline_id", "seq", "message_id", "blob_id", "run_id", "created_at"] {
        assert!(
            cols.iter().any(|c| c == required),
            "纯索引列 {required} 应保留，实际列：{:?}",
            cols
        );
    }
}
