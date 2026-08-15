// @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: rust-test
//! 任务 5 验收测试：**checkpoint 瘦身**。
//!
//! 目标架构（docs/tasks/task_messages_op_trace_unification.md 任务 5）：
//! `save_checkpoint` 存的 state **剥离 messages**（全文只在 blobs，checkpoint 不再
//! 与 blobs 重复存消息队列），只存标量 + 写入 `ckpt_max_seq` 水位（= 消息队列
//! 当前最大 seq）。读方（任务 4）不消费 checkpoint 的 messages——**零兼容**：
//! 旧格式含 messages 的 checkpoint load 后一律 `remove("messages")`，无双格式识别。
//!
//! 断言方式：
//! - 写侧：经 trait `save_checkpoint` 写入后，直接 SQL 读 `pipeline_checkpoints.state_json`
//!   （经公开的 `SqliteStore::with_conn` 出口）验证持久形态；
//! - 读侧：`load_latest_checkpoint` 返回值同样无 messages、有 ckpt_max_seq；
//! - 零兼容：库里手工塞入含 messages 的旧格式 checkpoint，load 也必须剥离。
//!
//! 当前实现存全量 state（含 messages 全文）→ 本文件测试为 **red（TDD red 阶段）**。

use agentos_core::traits::StorageBackend;
use agentos_engine::SqliteStore;
use serde_json::{json, Value};

/// 构造一个含 messages 队列（seq 0..4）+ 标量的完整 state。
fn full_state() -> Value {
    json!({
        "pipeline_id": "p_ckpt",
        "turn_count": 3,
        "system_message": "你是灵汐",
        "CKPT_FULLTEXT_MARKER": "scalar-marker-kept",
        "messages": [
            { "seq": 0, "role": "user", "content": "CKPT_MSG_q1" },
            { "seq": 1, "role": "assistant", "content": "CKPT_MSG_a1" },
            { "seq": 2, "role": "user", "content": "CKPT_MSG_q2" },
            { "seq": 3, "role": "assistant", "content": "CKPT_MSG_a2" },
            { "seq": 4, "role": "user", "content": "CKPT_MSG_q3" },
        ]
    })
}

/// 直接 SQL 读某 pipeline 最新 checkpoint 的 state_json 原文。
fn raw_state_json(store: &SqliteStore, pipeline_id: &str) -> String {
    store
        .with_conn(|c| -> Result<String, rusqlite::Error> {
            c.query_row(
                "SELECT state_json FROM pipeline_checkpoints \
                 WHERE pipeline_id = ?1 AND tenant_id = 'default' \
                 ORDER BY step_no DESC LIMIT 1",
                rusqlite::params![pipeline_id],
                |r| r.get::<_, String>(0),
            )
        })
        .expect("读 pipeline_checkpoints.state_json 应成功")
}

/// 写侧：checkpoint 剥离 messages、写入 ckpt_max_seq 水位，标量保留。
#[tokio::test]
async fn checkpoint_strips_messages_and_writes_max_seq_watermark() {
    let store = SqliteStore::open_memory().unwrap();
    let backend: &dyn StorageBackend = &store;

    backend
        .save_checkpoint("p_ckpt", "default", 5, &full_state())
        .await
        .unwrap();

    let raw = raw_state_json(&store, "p_ckpt");
    let saved: Value = serde_json::from_str(&raw).unwrap();

    // 无 messages：key 不存在，原文也不含消息全文标记
    assert!(
        saved.get("messages").is_none(),
        "checkpoint 不应含 messages 全文，实际：{}",
        raw
    );
    assert!(
        !raw.contains("CKPT_MSG_"),
        "checkpoint 原文不得含消息内容（全文只在 blobs）：{}",
        raw
    );

    // 有 ckpt_max_seq 水位 = 消息队列最大 seq（4）
    assert_eq!(
        saved.get("ckpt_max_seq").and_then(|v| v.as_u64()),
        Some(4),
        "应写入 ckpt_max_seq 水位 = max seq，实际：{}",
        raw
    );

    // 标量字段保留
    assert_eq!(
        saved.get("turn_count").and_then(|v| v.as_u64()),
        Some(3),
        "标量 turn_count 应保留"
    );
    assert_eq!(
        saved.get("system_message").and_then(|v| v.as_str()),
        Some("你是灵汐"),
        "标量 system_message 应保留"
    );
    assert_eq!(
        saved.get("pipeline_id").and_then(|v| v.as_str()),
        Some("p_ckpt"),
        "pipeline_id 应保留"
    );

    // 读侧（trait）：load_latest_checkpoint 返回的 state 同样瘦身
    let (step_no, loaded) = backend
        .load_latest_checkpoint("p_ckpt", "default")
        .await
        .unwrap()
        .expect("应有 checkpoint");
    assert_eq!(step_no, 5, "step_no 应保留");
    assert!(
        loaded.get("messages").is_none(),
        "load 返回的 state 不应含 messages"
    );
    assert_eq!(
        loaded.get("ckpt_max_seq").and_then(|v| v.as_u64()),
        Some(4),
        "load 返回的 state 应含 ckpt_max_seq 水位"
    );
    assert_eq!(
        loaded.get("turn_count").and_then(|v| v.as_u64()),
        Some(3),
        "load 返回的标量应保留"
    );
}

/// 零兼容：库里已有的旧格式全量 checkpoint（含 messages），load 后一律剥离。
///
/// 双格式兼容被明确禁止（零兼容原则）：不做"识别旧格式并读出 messages"，
/// 而是无条件 `remove("messages")`——旧数据直接丢弃，需要时清库重跑。
#[tokio::test]
async fn legacy_full_checkpoint_load_strips_messages_unconditionally() {
    let store = SqliteStore::open_memory().unwrap();
    let backend: &dyn StorageBackend = &store;

    // 手工模拟旧格式：直接把含 messages 全文的 state 塞进 pipeline_checkpoints
    // （绕过 save_checkpoint 的剥离逻辑，构造"升级前留下的旧 checkpoint"）
    let legacy = full_state();
    store
        .with_conn(|c| -> Result<usize, rusqlite::Error> {
            c.execute(
                "INSERT INTO pipeline_checkpoints \
                 (checkpoint_id, pipeline_id, step_no, state_json, tenant_id, created_at) \
                 VALUES ('cp_p_legacy_1', 'p_legacy', 1, ?1, 'default', ?2)",
                rusqlite::params![
                    serde_json::to_string(&legacy).unwrap(),
                    chrono::Utc::now().to_rfc3339()
                ],
            )
        })
        .expect("塞入旧格式 checkpoint 应成功");

    let (_, loaded) = backend
        .load_latest_checkpoint("p_legacy", "default")
        .await
        .unwrap()
        .expect("旧 checkpoint 应能 load");
    assert!(
        loaded.get("messages").is_none(),
        "零兼容：load 后一律 remove(messages)，不做旧格式识别，实际：{}",
        loaded
    );
    assert_eq!(
        loaded.get("turn_count").and_then(|v| v.as_u64()),
        Some(3),
        "旧 checkpoint 的标量仍应可用"
    );
}
