// @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: rust-test
//! "一次 apply" 集成验证：插件 emit 的 messages op 被同时应用到内存 state 与 DB 表。
//!
//! `apply_messages_op_update` 是新模型的接线核心（详见 docs/message_persistence_design.md）：
//! 引擎收到插件 state_updates 里的 messages op 后，**同一组 op** 既更新 `state["messages"]`
//! （`apply_slot_ops_to_array`，稠密、元素带 seq）又落 `message_slots` 表
//! （`apply_messages_ops_to_table`，稀疏、留 gap）。无 mirror、无 diff。

use agentos_core::traits::{MessageQueryOpts, StorageBackend};
use agentos_engine::{apply_messages_op_update, SqliteStore};
use serde_json::json;

fn set(seq: u64, msg: serde_json::Value) -> serde_json::Value {
    json!({ "op": "set", "seq": seq, "msg": msg })
}

fn clear(seq: u64) -> serde_json::Value {
    json!({ "op": "set", "seq": seq, "msg": null })
}

fn rows(store: &SqliteStore, pid: &str) -> Vec<u32> {
    store
        .get_slot_messages_by_pipeline(pid, "default", MessageQueryOpts::default())
        .unwrap()
        .iter()
        .map(|r| r.seq_in_branch)
        .collect()
}

#[tokio::test]
async fn op_update_applies_to_both_state_and_table() {
    let store = SqliteStore::open_memory().unwrap();
    let backend: &dyn StorageBackend = &store;
    let mut state = json!({"pipeline_id": "p1"});

    apply_messages_op_update(
        &mut state,
        backend,
        "default",
        &[
            set(0, json!({ "role": "user", "content": "你好" })),
            set(1, json!({ "role": "assistant", "content": "嗨" })),
        ],
    )
    .await
    .unwrap();

    // 内存：稠密数组，元素带 seq
    let arr = state["messages"].as_array().unwrap();
    assert_eq!(arr.len(), 2, "内存应有 2 条");
    assert_eq!(arr[0]["seq"].as_u64(), Some(0));
    assert_eq!(arr[1]["seq"].as_u64(), Some(1));

    // 表：同步落了同样的槽位
    assert_eq!(rows(&store, "p1"), vec![0, 1], "表应有 seq 0,1");
}

#[tokio::test]
async fn op_update_delete_compacts_state_leaves_table_gap() {
    // 同一组 op：内存紧凑、表留 gap —— 两侧各自正确，互不干扰。
    let store = SqliteStore::open_memory().unwrap();
    let backend: &dyn StorageBackend = &store;
    let mut state = json!({"pipeline_id": "p2"});

    apply_messages_op_update(
        &mut state,
        backend,
        "default",
        &[
            set(0, json!({ "role": "user", "content": "a" })),
            set(1, json!({ "role": "assistant", "content": "b" })),
            set(2, json!({ "role": "user", "content": "c" })),
        ],
    )
    .await
    .unwrap();
    apply_messages_op_update(&mut state, backend, "default", &[clear(1)])
        .await
        .unwrap();

    // 内存紧凑：seq 0,2（无 gap）
    let arr = state["messages"].as_array().unwrap();
    let mem_seqs: Vec<u64> = arr.iter().map(|m| m["seq"].as_u64().unwrap()).collect();
    assert_eq!(mem_seqs, vec![0, 2], "内存稠密、元素 seq 为 0,2");

    // 表留 gap：seq 0,2（1 是 gap）
    assert_eq!(
        rows(&store, "p2"),
        vec![0, 2],
        "表稀疏、seq 0,2（1 为 gap）"
    );
}

#[tokio::test]
async fn op_update_skips_table_when_pipeline_id_empty() {
    // pipeline_id 为空（首轮未注入/测试场景）：只更内存，不落表，不报错。
    let store = SqliteStore::open_memory().unwrap();
    let backend: &dyn StorageBackend = &store;
    let mut state = json!({});

    apply_messages_op_update(
        &mut state,
        backend,
        "default",
        &[set(0, json!({ "role": "user", "content": "x" }))],
    )
    .await
    .unwrap();

    assert_eq!(
        state["messages"].as_array().unwrap().len(),
        1,
        "内存仍应更新"
    );
    assert!(
        store
            .get_slot_messages_by_pipeline("", "default", MessageQueryOpts::default())
            .unwrap()
            .is_empty(),
        "pipeline_id 为空时不应落表"
    );
}

#[tokio::test]
async fn op_update_append_without_seq_assigns_incrementing_seq() {
    // 插件 emit 无 seq 的 set（= 纯 append）→ 引擎按 state 现有 max 分配递增 seq。
    // 这样 append 类插件（llm_core/tool_core/user push）无需感知 seq。
    let store = SqliteStore::open_memory().unwrap();
    let backend: &dyn StorageBackend = &store;
    let mut state = json!({"pipeline_id": "p3"});

    // 先落下已有历史（seq 0,1）到内存 + 表
    apply_messages_op_update(
        &mut state,
        backend,
        "default",
        &[
            json!({ "op": "set", "seq": 0, "msg": { "role": "user", "content": "a" } }),
            json!({ "op": "set", "seq": 1, "msg": { "role": "assistant", "content": "b" } }),
        ],
    )
    .await
    .unwrap();

    // 再 append 两条（无 seq）→ 引擎接 max=1 分配 seq 2,3
    apply_messages_op_update(
        &mut state,
        backend,
        "default",
        &[
            json!({ "op": "set", "msg": { "role": "user", "content": "c" } }),
            json!({ "op": "set", "msg": { "role": "assistant", "content": "d" } }),
        ],
    )
    .await
    .unwrap();

    // 内存：append 拿到 seq 2、3
    let arr = state["messages"].as_array().unwrap();
    let mem_seqs: Vec<u64> = arr.iter().map(|m| m["seq"].as_u64().unwrap()).collect();
    assert_eq!(mem_seqs, vec![0, 1, 2, 3], "内存 append 分配 seq 2,3");

    // 表：同一批 op 落表，seq 一致
    let tseqs: Vec<u32> = store
        .get_slot_messages_by_pipeline("p3", "default", MessageQueryOpts::default())
        .unwrap()
        .iter()
        .map(|r| r.seq_in_branch)
        .collect();
    assert_eq!(tseqs, vec![0, 1, 2, 3], "表 append 分配 seq 2,3");
}

#[tokio::test]
async fn op_update_append_into_empty_starts_at_seq_zero() {
    let store = SqliteStore::open_memory().unwrap();
    let backend: &dyn StorageBackend = &store;
    let mut state = json!({"pipeline_id": "p4"});

    apply_messages_op_update(
        &mut state,
        backend,
        "default",
        &[json!({ "op": "set", "msg": { "role": "user", "content": "first" } })],
    )
    .await
    .unwrap();

    let arr = state["messages"].as_array().unwrap();
    assert_eq!(arr[0].get("seq").and_then(|v| v.as_u64()), Some(0));
    assert_eq!(
        store
            .get_slot_messages_by_pipeline("p4", "default", MessageQueryOpts::default())
            .unwrap()
            .len(),
        1
    );
}
