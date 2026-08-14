// @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: rust-test
//! 内存侧"按稳定 seq 应用槽位 op"验证（op-based 新模型的 state 半边）。
//!
//! 引擎把插件 emit 的 `set/insert` op 应用到内存 `state["messages"]`：
//! 内存数组是**稠密**的（删除会紧凑），每个元素自带稳定 `seq`（≠ 数组下标）。
//! 这与表侧 `apply_messages_ops_to_table`（稀疏、留 gap）是同一组 op 的两个落点。
//! 见 docs/message_persistence_design.md。

use agentos_engine::apply_slot_ops_to_array;
use serde_json::{json, Value};

fn msg_with_seq(seq: u64, role: &str, content: &str) -> Value {
    json!({ "seq": seq, "role": role, "content": content })
}

fn set(seq: u64, msg: Value) -> Value {
    json!({ "op": "set", "seq": seq, "msg": msg })
}

fn clear(seq: u64) -> Value {
    json!({ "op": "set", "seq": seq, "msg": null })
}

fn insert_at(at: u64, msg: Value) -> Value {
    json!({ "op": "insert", "at": at, "msg": msg })
}

/// 取数组里各元素的 (seq, content) 序列，便于断言顺序与内容。
fn snapshot(arr: &[Value]) -> Vec<(u64, String)> {
    arr.iter()
        .map(|m| {
            let seq = m.get("seq").and_then(|v| v.as_u64()).unwrap_or(u64::MAX);
            let content = m
                .get("content")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            (seq, content)
        })
        .collect()
}

#[test]
fn state_set_append_into_empty_and_growth() {
    let mut arr: Vec<Value> = vec![];
    apply_slot_ops_to_array(
        &mut arr,
        &[
            set(0, json!({ "role": "user", "content": "你好" })),
            set(1, json!({ "role": "assistant", "content": "嗨" })),
        ],
    );
    assert_eq!(
        snapshot(&arr),
        vec![(0, "你好".into()), (1, "嗨".into())],
        "append 应按 seq 升序落入稠密数组"
    );
    // 新元素应自带 seq 字段
    assert_eq!(arr[0].get("seq").and_then(|v| v.as_u64()), Some(0));
    assert_eq!(arr[1].get("seq").and_then(|v| v.as_u64()), Some(1));
}

#[test]
fn state_set_modify_replaces_content_keeps_seq() {
    let mut arr = vec![msg_with_seq(0, "user", "原始")];
    apply_slot_ops_to_array(
        &mut arr,
        &[set(0, json!({ "role": "user", "content": "被修改" }))],
    );
    assert_eq!(arr.len(), 1, "modify 不应新增元素");
    assert_eq!(
        snapshot(&arr),
        vec![(0, "被修改".into())],
        "内容更新、seq 不变"
    );
}

#[test]
fn state_clear_removes_element_others_keep_seq() {
    // 删除中间元素：数组紧凑，但幸存元素 seq 不变。
    let mut arr = vec![
        msg_with_seq(0, "user", "a"),
        msg_with_seq(1, "assistant", "b"),
        msg_with_seq(2, "user", "c"),
    ];
    apply_slot_ops_to_array(&mut arr, &[clear(1)]);
    assert_eq!(
        snapshot(&arr),
        vec![(0, "a".into()), (2, "c".into())],
        "删 seq1 后数组紧凑，seq0/seq2 保留"
    );
}

#[test]
fn state_insert_shifts_back_seq() {
    // insert(at)：seq>=at 的后段 seq+1，新元素占 at。
    let mut arr = vec![
        msg_with_seq(0, "user", "a"),
        msg_with_seq(1, "assistant", "b"),
        msg_with_seq(2, "user", "c"),
    ];
    apply_slot_ops_to_array(
        &mut arr,
        &[insert_at(1, json!({ "role": "system", "content": "X" }))],
    );
    assert_eq!(
        snapshot(&arr),
        vec![
            (0, "a".into()),
            (1, "X".into()),
            (2, "b".into()),
            (3, "c".into()),
        ],
        "insert 后 a,X,b,c，后段 seq 顺延"
    );
}

#[test]
fn state_compression_combo_front_summary_back_unchanged() {
    // 复合：清空 seq1/2 + 改写 seq0 为 summary → summary@0、m3@3、m4@4（后段 seq 不变）。
    let mut arr = vec![
        msg_with_seq(0, "user", "m0"),
        msg_with_seq(1, "assistant", "m1"),
        msg_with_seq(2, "user", "m2"),
        msg_with_seq(3, "assistant", "m3"),
        msg_with_seq(4, "user", "m4"),
    ];
    apply_slot_ops_to_array(
        &mut arr,
        &[
            clear(1),
            clear(2),
            set(0, json!({ "role": "assistant", "content": "[summary]" })),
        ],
    );
    assert_eq!(
        snapshot(&arr),
        vec![(0, "[summary]".into()), (3, "m3".into()), (4, "m4".into()),],
        "压缩后稠密数组为 summary,m3,m4；后段 seq 不变（顺延只发生在 insert）"
    );
}
