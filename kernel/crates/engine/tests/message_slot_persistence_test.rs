// @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: rust-test
//! 消息槽位持久化（op-based 新模型）验证。
//!
//! 验证新设计（见 docs/message_persistence_design.md）的核心不变量：
//! - 单写入器：只通过 `apply_messages_ops_to_table`（身份/seq 感知 op）落表；
//! - `seq` = 稳定逻辑槽位：append 分配、永不变更、删除留 gap（≠ 数组下标）；
//! - `message_id` = 内容派生：内容变→id 变、位置（seq）不变；同内容→同 id；
//! - 压缩：前段 summary 占最小 seq（id 变）、中段槽删 gap、后段 seq/id 都不变、不顺延；
//! - `ORDER BY seq` 顺序天然正确；gap 下 `before/after_sequence` 游标仍正确。
//!
//! 这些断言共同证明 B1（两套 seq 写入器导致的 seq 冲突）在新模型下不复存在。

use agentos_core::traits::MessageQueryOpts;
use agentos_core::types::MessageRecord;
use agentos_engine::SqliteStore;
use serde_json::json;

/// 构造一条 upsert op：在稳定槽位 `seq` 写入消息 `msg`。
fn upsert(seq: u32, msg: serde_json::Value) -> serde_json::Value {
    json!({ "op": "upsert", "seq": seq, "msg": msg })
}

/// 构造一条 delete op：删除稳定槽位 `seq`（留 gap）。
fn delete(seq: u32) -> serde_json::Value {
    json!({ "op": "delete", "seq": seq })
}

fn user_msg(content: &str) -> serde_json::Value {
    json!({ "role": "user", "content": content })
}

fn assistant_msg(content: &str) -> serde_json::Value {
    json!({ "role": "assistant", "content": content })
}

/// 读取槽位表的便捷封装（默认租户、无游标、无 limit）。
fn read_all(store: &SqliteStore, pipeline: &str) -> Vec<MessageRecord> {
    store
        .get_slot_messages_by_pipeline(pipeline, "default", MessageQueryOpts::default())
        .expect("读 message_slots 应成功")
}

#[test]
fn slot_append_assigns_stable_seq_and_distinct_content_derived_ids() {
    let store = SqliteStore::open_memory().unwrap();
    let pid = "p1";

    // 模拟一轮：用户消息（槽 0）+ assistant 回复（槽 1）
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                upsert(0, user_msg("你好")),
                upsert(1, assistant_msg("你好！有什么可以帮你的吗？")),
            ],
        )
        .expect("apply ops 应成功");

    let rows = read_all(&store, pid);
    assert_eq!(rows.len(), 2, "应有两条消息");
    assert_eq!(rows[0].seq_in_branch, 0, "首条 seq=0");
    assert_eq!(rows[1].seq_in_branch, 1, "次条 seq=1");
    assert_eq!(rows[0].role, "user");
    assert_eq!(rows[1].role, "assistant");
    // 内容派生 id 非空且互异（证明 id 与内容绑定，而非按下标生成）
    assert!(!rows[0].message_id.is_empty(), "message_id 不应为空");
    assert_ne!(
        rows[0].message_id, rows[1].message_id,
        "不同内容的 message_id 必须不同"
    );
}

#[test]
fn slot_multi_turn_no_seq_collision() {
    // B1 回归：多轮对话下所有 seq 必须互异、顺序正确（旧模型首轮 user/assistant 都 seq=1）。
    let store = SqliteStore::open_memory().unwrap();
    let pid = "p2";

    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                upsert(0, user_msg("第一问")),
                upsert(1, assistant_msg("第一答")),
            ],
        )
        .unwrap();
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                upsert(2, user_msg("第二问")),
                upsert(3, assistant_msg("第二答")),
            ],
        )
        .unwrap();

    let rows = read_all(&store, pid);
    let seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    assert_eq!(seqs, vec![0, 1, 2, 3], "seq 应稳定递增、无冲突");
    // 顺序：user, assistant, user, assistant
    let roles: Vec<&str> = rows.iter().map(|r| r.role.as_str()).collect();
    assert_eq!(roles, vec!["user", "assistant", "user", "assistant"]);
    // 无重复 seq
    let mut sorted = seqs.clone();
    sorted.sort_unstable();
    sorted.dedup();
    assert_eq!(sorted.len(), seqs.len(), "seq 不应有重复");
}

#[test]
fn slot_content_derived_id_changes_when_content_changes_same_slot() {
    // 同一槽位 seq，内容变了 → message_id 必须变，seq 不变（位置稳定）。
    let store = SqliteStore::open_memory().unwrap();
    let pid = "p3";

    store
        .apply_messages_ops_to_table(pid, "default", &[upsert(0, user_msg("原始"))])
        .unwrap();
    let id_before = read_all(&store, pid)[0].message_id.clone();

    store
        .apply_messages_ops_to_table(pid, "default", &[upsert(0, user_msg("被修改"))])
        .unwrap();
    let rows = read_all(&store, pid);
    assert_eq!(rows.len(), 1, "同槽位 upsert 不应新增行");
    assert_eq!(rows[0].seq_in_branch, 0, "seq 不变（位置稳定）");
    assert_ne!(
        rows[0].message_id, id_before,
        "内容变 → message_id 必须变（内容派生）"
    );
}

#[test]
fn slot_identical_content_yields_identical_id() {
    // 两条不同槽位、相同内容的消息 → message_id 相同（证明 id 是内容派生，与槽位无关）。
    let store = SqliteStore::open_memory().unwrap();
    let pid = "p4";

    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                upsert(0, user_msg("重复内容")),
                upsert(1, user_msg("重复内容")),
            ],
        )
        .unwrap();
    let rows = read_all(&store, pid);
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0].seq_in_branch, 0);
    assert_eq!(rows[1].seq_in_branch, 1);
    assert_eq!(
        rows[0].message_id, rows[1].message_id,
        "相同内容 → 相同 message_id（内容派生）"
    );
}

#[test]
fn slot_compression_leaves_gap_and_back_unchanged() {
    // 命门：压缩前段、保后段、留 gap、不顺延。
    // 初始：m0..m4 占 seq 0..4。
    let store = SqliteStore::open_memory().unwrap();
    let pid = "p5";

    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                upsert(0, user_msg("m0")),
                upsert(1, assistant_msg("m1")),
                upsert(2, user_msg("m2")),
                upsert(3, assistant_msg("m3")),
                upsert(4, user_msg("m4")),
            ],
        )
        .unwrap();
    let rows = read_all(&store, pid);
    let id_m3 = rows
        .iter()
        .find(|r| r.seq_in_branch == 3)
        .unwrap()
        .message_id
        .clone();
    let id_m4 = rows
        .iter()
        .find(|r| r.seq_in_branch == 4)
        .unwrap()
        .message_id
        .clone();
    let id_m0 = rows
        .iter()
        .find(|r| r.seq_in_branch == 0)
        .unwrap()
        .message_id
        .clone();

    // 压缩：把 seq 1、2 删掉（gap），seq 0 内容替换成 summary（id 变）；后段 m3/m4 不动。
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                delete(1),
                delete(2),
                upsert(0, assistant_msg("[summary of m0..m2]")),
            ],
        )
        .unwrap();

    let rows = read_all(&store, pid);
    let seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    // 1、2 是 gap；0(summary)、3、4 在；后段未顺延（仍是 3、4，不是 1、2）
    assert_eq!(
        seqs,
        vec![0, 3, 4],
        "前段 summary 占 seq0，中段留 gap，后段不顺延"
    );

    // 后段 id 不变
    let after_m3 = rows.iter().find(|r| r.seq_in_branch == 3).unwrap();
    let after_m4 = rows.iter().find(|r| r.seq_in_branch == 4).unwrap();
    assert_eq!(after_m3.message_id, id_m3, "后段 m3 的 message_id 不应变");
    assert_eq!(after_m4.message_id, id_m4, "后段 m4 的 message_id 不应变");

    // 前段 summary 的 id 变了（内容变 → id 变）
    let after_m0 = rows.iter().find(|r| r.seq_in_branch == 0).unwrap();
    assert_ne!(
        after_m0.message_id, id_m0,
        "前段 summary 内容变 → message_id 应变"
    );
}

#[test]
fn slot_cursor_paging_works_across_gaps() {
    // gap 下 after_sequence/before_sequence 游标仍正确。
    let store = SqliteStore::open_memory().unwrap();
    let pid = "p6";

    // 构造 seq 0,3,4（1、2 为 gap）
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                upsert(0, user_msg("a")),
                delete(1),
                delete(2),
                upsert(3, assistant_msg("b")),
                upsert(4, user_msg("c")),
            ],
        )
        .unwrap();

    let after = store
        .get_slot_messages_by_pipeline(
            pid,
            "default",
            MessageQueryOpts {
                after_sequence: Some(0),
                ..Default::default()
            },
        )
        .unwrap();
    let seqs: Vec<u32> = after.iter().map(|r| r.seq_in_branch).collect();
    assert_eq!(seqs, vec![3, 4], "after_sequence=0 应跳过 gap 返回 3,4");

    let before = store
        .get_slot_messages_by_pipeline(
            pid,
            "default",
            MessageQueryOpts {
                before_sequence: Some(4),
                ..Default::default()
            },
        )
        .unwrap();
    let seqs: Vec<u32> = before.iter().map(|r| r.seq_in_branch).collect();
    assert_eq!(
        seqs,
        vec![0, 3],
        "before_sequence=4 应返回 0,3（gap 不影响）"
    );
}
