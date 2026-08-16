// @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: rust-test
//! 消息槽位持久化（op-based 新模型）验证。
//!
//! 内核只两个槽位原语（见 docs/message_persistence_design.md）：
//! - `set(seq, msg|null)`：统一 append / modify / delete（msg 为空=清空留 gap）；
//! - `insert(at, msg)`：在位置 at 插入，后段 seq 顺延 +1（id 不变）。
//!
//! 验证的不变量：
//! - `seq` = 稳定逻辑槽位：append 分配、modify 不变、delete 留 gap、insert 顺延；
//! - `message_id` = 内容派生（与 seq 解耦）：内容变→id 变、位置变；同内容→同 id；
//! - 压缩：前段 summary 占最小 seq（id 变）、中段删 gap、后段 seq/id 都不变、不顺延；
//! - `ORDER BY seq` 顺序天然正确；gap 下 `before/after_sequence` 游标仍正确。
//!
//! 这些断言共同证明 B1（两套 seq 写入器导致的 seq 冲突）在新模型下不复存在。

use agentos_core::traits::MessageQueryOpts;
use agentos_core::types::MessageRecord;
use agentos_engine::SqliteStore;
use serde_json::json;

/// `set(seq, msg)`：在槽位 seq 写消息（append=新末槽 / modify=已存在槽）。
fn set(seq: u32, msg: serde_json::Value) -> serde_json::Value {
    json!({ "op": "set", "seq": seq, "msg": msg })
}

/// `set(seq, null)`：清空槽位 seq（delete=留 gap，后段不动）。
fn clear(seq: u32) -> serde_json::Value {
    json!({ "op": "set", "seq": seq, "msg": null })
}

/// `insert(at, msg)`：在位置 at 插入槽位，seq>=at 后段顺延 +1。
fn insert_at(at: u32, msg: serde_json::Value) -> serde_json::Value {
    json!({ "op": "insert", "at": at, "msg": msg })
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
fn slot_set_append_assigns_stable_seq_and_distinct_content_derived_ids() {
    let store = SqliteStore::open_memory().unwrap();
    let pid = "p1";

    // 模拟一轮：用户消息（槽 0）+ assistant 回复（槽 1）
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                set(0, user_msg("你好")),
                set(1, assistant_msg("你好！有什么可以帮你的吗？")),
            ],
        )
        .expect("apply ops 应成功");

    let rows = read_all(&store, pid);
    assert_eq!(rows.len(), 2, "应有两条消息");
    assert_eq!(rows[0].seq_in_branch, 0, "首条 seq=0");
    assert_eq!(rows[1].seq_in_branch, 1, "次条 seq=1");
    assert_eq!(rows[0].role, "user");
    assert_eq!(rows[1].role, "assistant");
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
            &[set(0, user_msg("第一问")), set(1, assistant_msg("第一答"))],
        )
        .unwrap();
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[set(2, user_msg("第二问")), set(3, assistant_msg("第二答"))],
        )
        .unwrap();

    let rows = read_all(&store, pid);
    let seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    assert_eq!(seqs, vec![0, 1, 2, 3], "seq 应稳定递增、无冲突");
    let roles: Vec<&str> = rows.iter().map(|r| r.role.as_str()).collect();
    assert_eq!(roles, vec!["user", "assistant", "user", "assistant"]);
    let mut sorted = seqs.clone();
    sorted.sort_unstable();
    sorted.dedup();
    assert_eq!(sorted.len(), seqs.len(), "seq 不应有重复");
}

#[test]
fn slot_modify_same_slot_changes_id_keeps_seq() {
    // set 同一槽位、内容变了 → message_id 变，seq 不变（modify 语义）。
    let store = SqliteStore::open_memory().unwrap();
    let pid = "p3";

    store
        .apply_messages_ops_to_table(pid, "default", &[set(0, user_msg("原始"))])
        .unwrap();
    let id_before = read_all(&store, pid)[0].message_id.clone();

    store
        .apply_messages_ops_to_table(pid, "default", &[set(0, user_msg("被修改"))])
        .unwrap();
    let rows = read_all(&store, pid);
    assert_eq!(rows.len(), 1, "同槽位 set 不应新增行");
    assert_eq!(rows[0].seq_in_branch, 0, "seq 不变（位置稳定）");
    assert_ne!(
        rows[0].message_id, id_before,
        "内容变 → message_id 必须变（内容派生）"
    );
}

#[test]
fn slot_delete_clears_slot_leaves_gap() {
    // set(seq, null) = 清空槽位（留 gap）。
    let store = SqliteStore::open_memory().unwrap();
    let pid = "p3b";

    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                set(0, user_msg("a")),
                set(1, assistant_msg("b")),
                set(2, user_msg("c")),
            ],
        )
        .unwrap();
    store
        .apply_messages_ops_to_table(pid, "default", &[clear(1)])
        .unwrap();

    let rows = read_all(&store, pid);
    let seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    assert_eq!(seqs, vec![0, 2], "清空槽 1 留 gap，槽 0/2 保留");
}

#[test]
fn slot_identical_content_yields_identical_id() {
    // 两条不同槽位、相同内容的消息 → message_id 相同（id 是内容派生，与槽位无关）。
    let store = SqliteStore::open_memory().unwrap();
    let pid = "p4";

    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[set(0, user_msg("重复内容")), set(1, user_msg("重复内容"))],
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
    // 命门：压缩前段、保后段、留 gap、不顺延。初始 m0..m4 占 seq 0..4。
    let store = SqliteStore::open_memory().unwrap();
    let pid = "p5";

    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                set(0, user_msg("m0")),
                set(1, assistant_msg("m1")),
                set(2, user_msg("m2")),
                set(3, assistant_msg("m3")),
                set(4, user_msg("m4")),
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

    // 压缩：清空 seq 1、2（gap），seq 0 内容替换成 summary（id 变）；后段 m3/m4 不动。
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                clear(1),
                clear(2),
                set(0, assistant_msg("[summary of m0..m2]")),
            ],
        )
        .unwrap();

    let rows = read_all(&store, pid);
    let seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    // 1、2 是 gap；0(summary)、3、4 在；后段未顺延（仍是 3、4）
    assert_eq!(
        seqs,
        vec![0, 3, 4],
        "前段 summary 占 seq0，中段留 gap，后段不顺延"
    );

    let after_m3 = rows.iter().find(|r| r.seq_in_branch == 3).unwrap();
    let after_m4 = rows.iter().find(|r| r.seq_in_branch == 4).unwrap();
    assert_eq!(after_m3.message_id, id_m3, "后段 m3 的 message_id 不应变");
    assert_eq!(after_m4.message_id, id_m4, "后段 m4 的 message_id 不应变");

    let after_m0 = rows.iter().find(|r| r.seq_in_branch == 0).unwrap();
    assert_ne!(
        after_m0.message_id, id_m0,
        "前段 summary 内容变 → message_id 应变"
    );
}

#[test]
fn slot_insert_shifts_back_seq_but_keeps_id() {
    // insert(at, msg)：在中间插入，后段 seq 顺延 +1，后段 message_id 不变。
    let store = SqliteStore::open_memory().unwrap();
    let pid = "p5b";

    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[
                set(0, user_msg("a")),
                set(1, assistant_msg("b")),
                set(2, user_msg("c")),
            ],
        )
        .unwrap();
    let rows = read_all(&store, pid);
    let id_b = rows
        .iter()
        .find(|r| r.seq_in_branch == 1)
        .unwrap()
        .message_id
        .clone();
    let id_c = rows
        .iter()
        .find(|r| r.seq_in_branch == 2)
        .unwrap()
        .message_id
        .clone();

    // 在位置 1 插入 X：b 顺延到 seq2、c 顺延到 seq3、X 占 seq1（id 不变）。
    store
        .apply_messages_ops_to_table(pid, "default", &[insert_at(1, user_msg("X"))])
        .unwrap();

    let rows = read_all(&store, pid);
    let seqs: Vec<u32> = rows.iter().map(|r| r.seq_in_branch).collect();
    assert_eq!(seqs, vec![0, 1, 2, 3], "insert 后 seq 连续 0,1,2,3");

    let roles: Vec<&str> = rows.iter().map(|r| r.role.as_str()).collect();
    let contents: Vec<String> = rows
        .iter()
        .map(|r| r.content_preview.clone().unwrap_or_default())
        .collect();
    assert_eq!(contents, vec!["a", "X", "b", "c"], "顺序应为 a,X,b,c");
    let _ = roles;

    // 后段 id 不变（只是 seq 顺延）：b 原 seq1 现 seq2，c 原 seq2 现 seq3。
    let after_b = rows.iter().find(|r| r.seq_in_branch == 2).unwrap();
    let after_c = rows.iter().find(|r| r.seq_in_branch == 3).unwrap();
    assert_eq!(
        after_b.message_id, id_b,
        "b 的 message_id 不变（仅 seq 顺延）"
    );
    assert_eq!(
        after_c.message_id, id_c,
        "c 的 message_id 不变（仅 seq 顺延）"
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
                set(0, user_msg("a")),
                clear(1),
                clear(2),
                set(3, assistant_msg("b")),
                set(4, user_msg("c")),
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

// ═══════════════════════════════════════════════════════════
// GAP-3：写入批次原子性（G8 重启在途消息 blob NULL 的病根）
// ═══════════════════════════════════════════════════════════

/// GAP-3 不变量：任何批次的写入落库后，message_slots 行的 blob_id 恒非空。
/// （此前 blob/slot 各自 autocommit，G8 exit(75) 可在两条语句间截断——
/// 事务包裹后 slot 可见 ⟹ blob 已提交，消息正文不再丢失。）
#[test]
fn test_apply_ops_all_slots_have_blob() {
    let store = agentos_engine::SqliteStore::open_memory().unwrap();
    store
        .apply_messages_ops_to_table("p1", "t1", &[
            serde_json::json!({"op":"set","seq":1,"msg":{"role":"user","content":"a"}}),
            serde_json::json!({"op":"set","seq":2,"msg":{"role":"assistant","content":"b"}}),
            serde_json::json!({"op":"insert","at":1,"msg":{"role":"system","content":"c"}}),
            serde_json::json!({"op":"set","seq":9,"_message_id":"m9","msg":{"role":"user","content":"d"}}),
        ])
        .unwrap();
    let rows = store.load_message_history("p1", "t1").unwrap();
    assert!(rows.len() >= 4, "四条消息应全部落库：{rows:?}");
    // decode 成功即意味着 blob 完整（NULL blob 行会变成空对象——反向验证内容非空）
    for m in &rows {
        let has_content = m.get("content").and_then(|c| c.as_str()).is_some_and(|s| !s.is_empty())
            || m.get("role").is_some();
        assert!(has_content, "每条消息应可从 blob 完整重建：{m:?}");
    }
}

// ═══════════════════════════════════════════════════════════
// GAP-3 后半：checkpoint 瘦身 + user 重放幂等（resume 重复消费病根）
// ═══════════════════════════════════════════════════════════

/// save_checkpoint 剥离易变 per-run 键（message/input/message_id/suspended/
/// thinking_strength/_assistant_id_assigned/_pending_message_ops）——这些属于
/// "本轮运行"而非"管道累计状态"，写进 checkpoint 只会在恢复时覆盖下一轮的
/// 新输入（重启后旧 user 消息被重放消费）。累计标量（track.*/task.* 等）保留。
#[test]
fn test_save_checkpoint_strips_volatile_run_keys() {
    let store = agentos_engine::SqliteStore::open_memory().unwrap();
    let state = serde_json::json!({
        "pipeline_id": "p1",
        "message": "旧轮消息",
        "input": "旧轮输入",
        "message_id": "m_old",
        "suspended": true,
        "thinking_strength": "high",
        "_assistant_id_assigned": true,
        "_pending_message_ops": [{"op": "set"}],
        "track.total_tokens": 1234,
        "task.status": "running",
        "messages": [
            {"role": "user", "content": "u1", "seq": 1},
            {"role": "assistant", "content": "a1", "seq": 2},
        ],
    });
    store.save_checkpoint("p1", "t1", 3, &state).unwrap();
    let (_, slim) = store.load_latest_checkpoint("p1", "t1").unwrap().unwrap();

    for k in [
        "message",
        "input",
        "message_id",
        "suspended",
        "thinking_strength",
        "_assistant_id_assigned",
        "_pending_message_ops",
        "messages",
    ] {
        assert!(slim.get(k).is_none(), "易变键 {k} 不应进 checkpoint: {slim}");
    }
    // 累计标量保留 + 水位
    assert_eq!(slim["track.total_tokens"], 1234);
    assert_eq!(slim["task.status"], "running");
    assert_eq!(slim["ckpt_max_seq"], 2);
}
