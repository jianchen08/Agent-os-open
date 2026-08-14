//! 工具结果 envelope 持久化 round-trip 契约测试（op 模型版）。
//!
//! 契约：role=tool 消息自带的结构化工具结果（`tool_result` envelope：
//! call_id / tool_name / success / error / data / metadata / duration_ms）
//! 随消息**整体进 blob**（消息是不可变值，envelope 是其持久形态的一部分），
//! 经 `apply_messages_ops_to_table` 落 message_slots 后，`get_messages_by_pipeline`
//! （读时重建）必须能完整读回——前端"刷新后冷数据与实时流式数据结构一致"的基石。
//!
//! envelope 同时是 status/error 的权威来源（success=false → failed+error），
//! 优于历史 `"Error: "` 前缀推断；无 envelope 的消息回退前缀推断不劣化。
use agentos_core::traits::{MessageQueryOpts, StorageBackend};
use agentos_engine::SqliteStore;
use serde_json::{json, Value};

const PID: &str = "p-roundtrip-1";
const TENANT: &str = "default";

/// 播种：消息数组逐条 set（seq = 下标）。
fn seed(store: &SqliteStore, msgs: &[Value]) {
    let ops: Vec<Value> = msgs
        .iter()
        .enumerate()
        .map(|(i, m)| json!({ "op": "set", "seq": i as u64, "msg": m }))
        .collect();
    store.apply_messages_ops_to_table(PID, TENANT, &ops).unwrap();
}

/// 成功工具调用的消息数组（assistant tool_calls + tool 消息带 envelope）。
fn enriched_messages_success() -> Vec<Value> {
    vec![
        json!({
            "role": "user",
            "content": "写文件",
        }),
        json!({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_r1",
                "type": "function",
                "function": { "name": "file_write", "arguments": "{\"file_path\":\"a.rs\"}" }
            }]
        }),
        json!({
            "role": "tool",
            "tool_call_id": "call_r1",
            "content": "added: 2\nlines: 1\n",
            "tool_result": {
                "call_id": "call_r1",
                "tool_name": "file_write",
                "success": true,
                "error": null,
                "data": { "added": 2, "lines": 1, "old_content": "", "new_content": "fn main() {}" },
                "metadata": { "container_task_id": "task_ctid_1" },
                "duration_ms": 123.4
            }
        }),
    ]
}

#[tokio::test]
async fn tool_result_envelope_roundtrips_through_slot_blob() {
    let store = SqliteStore::open_memory().unwrap();
    seed(&store, &enriched_messages_success());

    let rows = store
        .get_messages_by_pipeline(PID, MessageQueryOpts::default())
        .await
        .unwrap();
    assert_eq!(rows.len(), 3);

    let tool_row = rows.iter().find(|r| r.role == "tool").expect("tool 行必须存在");
    let envelope = tool_row
        .tool_result_json
        .as_deref()
        .and_then(|s| serde_json::from_str::<Value>(s).ok())
        .expect("envelope 随消息持久化并读时重建");

    assert_eq!(envelope["call_id"], "call_r1");
    assert_eq!(envelope["tool_name"], "file_write");
    assert_eq!(envelope["success"], true);
    assert_eq!(envelope["error"], Value::Null);
    assert_eq!(envelope["data"]["added"], 2);
    assert_eq!(envelope["data"]["new_content"], "fn main() {}");
    assert_eq!(envelope["metadata"]["container_task_id"], "task_ctid_1");
    assert_eq!(envelope["duration_ms"], 123.4);

    // 既有字段不回归：tool_call_id / status
    assert_eq!(tool_row.tool_call_id.as_deref(), Some("call_r1"));
    assert_eq!(tool_row.status.as_deref(), Some("completed"));
    assert_eq!(tool_row.error, None);
}

#[tokio::test]
async fn envelope_drives_status_and_error_over_prefix_inference() {
    let store = SqliteStore::open_memory().unwrap();
    // content 不带 "Error: " 前缀，但 envelope success=false —— envelope 是权威来源。
    seed(&store, &[json!({
        "role": "tool",
        "tool_call_id": "call_f1",
        "content": "操作未成功",
        "tool_result": {
            "call_id": "call_f1",
            "tool_name": "bash_execute",
            "success": false,
            "error": "boom",
            "data": null,
            "metadata": null,
            "duration_ms": 5.0
        }
    })]);

    let rows = store
        .get_messages_by_pipeline(PID, MessageQueryOpts::default())
        .await
        .unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].status.as_deref(), Some("failed"));
    assert_eq!(rows[0].error.as_deref(), Some("boom"));
}

#[tokio::test]
async fn modify_replaces_whole_message_no_preserve_semantics() {
    // 新语义（消息是不可变值）：modify = 槽位替换为**另一条消息**——
    // 无 envelope 的版本替换后，读回就是无 envelope（替换即替换，无"列保留"）。
    let store = SqliteStore::open_memory().unwrap();
    seed(&store, &enriched_messages_success());

    let unenriched_tool: Value = {
        let mut m = enriched_messages_success()[2].clone();
        m.as_object_mut().unwrap().remove("tool_result");
        m
    };
    store
        .apply_messages_ops_to_table(
            PID,
            TENANT,
            &[json!({ "op": "set", "seq": 2, "msg": unenriched_tool })],
        )
        .unwrap();

    let rows = store
        .get_messages_by_pipeline(PID, MessageQueryOpts::default())
        .await
        .unwrap();
    let tool_row = rows.iter().find(|r| r.role == "tool").expect("tool 行必须存在");
    assert!(
        tool_row.tool_result_json.is_none(),
        "无 envelope 的新版本消息替换后，读回应无 envelope（内容变 = 新消息）"
    );
    // content 前缀兜底推断接管 status
    assert_eq!(tool_row.status.as_deref(), Some("completed"));
}

#[tokio::test]
async fn legacy_prefix_inference_fallback_without_envelope() {
    let store = SqliteStore::open_memory().unwrap();
    // 无 envelope 的消息：前缀推断语义保持。
    seed(&store, &[json!({
        "role": "tool",
        "tool_call_id": "call_l1",
        "content": "Error: legacy boom",
    })]);

    let rows = store
        .get_messages_by_pipeline(PID, MessageQueryOpts::default())
        .await
        .unwrap();
    assert_eq!(rows[0].status.as_deref(), Some("failed"));
    assert_eq!(rows[0].error.as_deref(), Some("legacy boom"));
}
