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

#[tokio::test]
async fn message_id_injected_once_into_first_assistant_append() {
    // A1：state["message_id"] 存在时，本轮首个 assistant 追加 op 的 record_id
    // 用内核 message_id（`_message_id` 内部字段），与前端流式占位对齐；
    // 多轮迭代的后续 assistant 追加不再注入（_assistant_id_assigned 置位）；
    // modify 旧槽位的 op（显式旧 seq）不注入。
    let store = SqliteStore::open_memory().unwrap();
    let backend: &dyn StorageBackend = &store;
    let mut state = json!({
        "pipeline_id": "p5",
        "message_id": "a_run_001",
        "_assistant_id_assigned": false,
        "messages": []
    });

    // 历史已有 user（seq 0）——用显式 seq 模拟已存在槽位
    apply_messages_op_update(
        &mut state,
        backend,
        "default",
        &[set(0, json!({ "role": "user", "content": "你好" }))],
    )
    .await
    .unwrap();

    // 本轮首个 assistant 追加（无 seq → 引擎分配 seq 1）：应注入 message_id
    apply_messages_op_update(
        &mut state,
        backend,
        "default",
        &[json!({ "op": "set", "msg": { "role": "assistant", "content": "第一轮回复" } })],
    )
    .await
    .unwrap();

    let rec = store
        .get_slot_messages_by_pipeline("p5", "default", MessageQueryOpts::default())
        .unwrap();
    let first = rec.iter().find(|r| r.seq_in_branch == 1).expect("seq1 存在");
    assert_eq!(
        first.message_id, "a_run_001",
        "首个 assistant 的 record_id 应为内核 message_id"
    );

    // 第二轮迭代的 assistant 追加（seq 2）：不再注入，回退内容指纹
    apply_messages_op_update(
        &mut state,
        backend,
        "default",
        &[json!({ "op": "set", "msg": { "role": "assistant", "content": "第二轮回复" } })],
    )
    .await
    .unwrap();
    let rec = store
        .get_slot_messages_by_pipeline("p5", "default", MessageQueryOpts::default())
        .unwrap();
    let second = rec.iter().find(|r| r.seq_in_branch == 2).expect("seq2 存在");
    assert_ne!(
        second.message_id, "a_run_001",
        "后续 assistant 不应复用同一 message_id"
    );
    assert!(
        second.message_id.starts_with("mc_"),
        "无注入时回退内容指纹（mc_ 前缀）"
    );

    // 内存消息不带 id（注入只影响表侧 record_id）
    let arr = state["messages"].as_array().unwrap();
    assert!(arr.iter().all(|m| m.get("id").is_none()), "内存消息不应携带 id");

    // 标志已置位
    assert_eq!(
        state["_assistant_id_assigned"].as_bool(),
        Some(true),
        "本轮已注入过 message_id"
    );
}

#[tokio::test]
async fn message_id_injection_skips_modify_ops_on_old_slots() {
    // A1：对旧槽位的 modify op（显式旧 seq）不注入 message_id——
    // 防止 context_window_guard 等改写历史 assistant 时误挂本轮 message_id。
    let store = SqliteStore::open_memory().unwrap();
    let backend: &dyn StorageBackend = &store;
    let mut state = json!({
        "pipeline_id": "p6",
        "message_id": "a_run_002",
        "_assistant_id_assigned": false,
        "messages": []
    });
    apply_messages_op_update(
        &mut state,
        backend,
        "default",
        &[set(0, json!({ "role": "assistant", "content": "历史回复" }))],
    )
    .await
    .unwrap();

    // 同批 op 含：历史槽位 modify（seq 0，assistant）+ 新 append（无 seq，user）
    apply_messages_op_update(
        &mut state,
        backend,
        "default",
        &[
            set(0, json!({ "role": "assistant", "content": "历史回复(改写)" })),
            json!({ "op": "set", "msg": { "role": "user", "content": "新消息" } }),
        ],
    )
    .await
    .unwrap();

    let rec = store
        .get_slot_messages_by_pipeline("p6", "default", MessageQueryOpts::default())
        .unwrap();
    let mod_row = rec.iter().find(|r| r.seq_in_branch == 0).expect("seq0 存在");
    assert_ne!(
        mod_row.message_id, "a_run_002",
        "modify 旧槽位不得注入本轮 message_id"
    );
    assert!(
        mod_row.message_id.starts_with("mc_"),
        "modify 回退内容指纹（内容变了指纹也变）"
    );
}
