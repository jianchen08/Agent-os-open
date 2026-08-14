//! 回退语义（任务 6）：实录回放重建 + 补偿执行。
//!
//! 常规读写零回放（message_slots 表即最新态真值）；**回退到历史时刻**是唯一走
//! 轨迹重放的场景：traces 的 messages 实录（`{op, seq, message_id, blob_id}`）
//! 按写入序回放 ≤ 目标时刻的 ops，按实录 `blob_id` 从 blobs 取全文，重建目标
//! 队列；与当前表 diff 生成补偿 ops，走同一套表侧 apply；补偿动作本身追加一条
//! `PatchType::Rollback` 轨迹——append-only，历史不抹（对齐 DSH 补偿式回退）。
//!
//! 边界约定：
//! - run 反查经 `message_slots.run_id` lineage——首轮纯 user push（run 建立前落表，
//!   run_id 为 NULL）的行不在回放范围；回退默认发生在多轮完整会话上。
//! - 回放上界用 trace `created_at`（rfc3339，含边界）——"目标步/时刻"的中立投影。
//! - 缺 blob 的实录条目跳过（不阻断重建）；caller 侧（server/registry）的内存态
//!   刷新不在本模块职责内。

use std::collections::{BTreeMap, BTreeSet};

use agentos_core::types::StorageError;
use rusqlite::Connection;

use crate::pipeline_loop::op_ledger_entry;
use crate::store::SqliteStore;

/// 回放该 pipeline 全部 messages 实录 ops（写入序，`created_at <= upto` 含边界）。
///
/// 补偿轨迹（Rollback）的 ops 也在 `patch_data.messages._ops` 里——它们描述的
/// 正是回退后的状态，回放时天然正确（再次回退到更早时刻 = 在补偿之上继续回放）。
fn ledger_ops_upto(
    store: &SqliteStore,
    pipeline_id: &str,
    tenant_id: &str,
    upto_created_at: &str,
) -> Result<Vec<serde_json::Value>, StorageError> {
    store.with_conn(|conn| {
        let mut stmt = conn.prepare(
            "SELECT t.patch_data FROM traces t \
             WHERE t.created_at <= ?1 \
               AND t.run_id IN (SELECT DISTINCT run_id FROM message_slots \
                                WHERE pipeline_id = ?2 AND tenant_id = ?3 \
                                  AND run_id IS NOT NULL) \
             ORDER BY t.rowid ASC",
        )?;
        let rows = stmt.query_map(
            rusqlite::params![upto_created_at, pipeline_id, tenant_id],
            |r| r.get::<_, String>(0),
        )?;
        let mut ops = Vec::new();
        for data in rows {
            let data = data?;
            let Ok(v) = serde_json::from_str::<serde_json::Value>(&data) else {
                continue;
            };
            if let Some(arr) = v
                .get("messages")
                .and_then(|m| m.get("_ops"))
                .and_then(|o| o.as_array())
            {
                ops.extend(arr.iter().cloned());
            }
        }
        Ok(ops)
    })
}

/// 按 blob_id 取全文（blobs 内容寻址、append-only——被覆盖/删除的旧版本仍在）。
fn blob_message(conn: &Connection, blob_id: &str) -> Option<serde_json::Value> {
    let data: Vec<u8> = conn
        .query_row(
            "SELECT data FROM blobs WHERE blob_id = ?1",
            rusqlite::params![blob_id],
            |r| r.get(0),
        )
        .ok()?;
    serde_json::from_slice(&data).ok()
}

/// 重建目标时刻的消息队列（seq 升序、元素带 `seq` + 全文）。
///
/// 回放语义 = 表侧 apply 语义的只读版：`set(seq, 有blob)` 写槽、`set(seq, null)`
/// 清槽；重建失败（无实录/无 blob）返回力所能及的部分（缺行跳过，不报错——
/// 调用方可比对长度判断完整性）。
pub fn rebuild_messages_at(
    store: &SqliteStore,
    pipeline_id: &str,
    tenant_id: &str,
    upto_created_at: &str,
) -> Vec<serde_json::Value> {
    let Ok(ops) = ledger_ops_upto(store, pipeline_id, tenant_id, upto_created_at) else {
        return vec![];
    };
    // 槽位 → blob_id（回放；delete 移除）
    let mut slots: BTreeMap<i64, String> = BTreeMap::new();
    for op in &ops {
        let kind = op.get("op").and_then(|v| v.as_str()).unwrap_or("");
        // set 按 seq、insert 按 at 寻址
        let Some(addr) = op
            .get("seq")
            .or_else(|| op.get("at"))
            .and_then(|v| v.as_i64())
        else {
            continue;
        };
        match op.get("blob_id").and_then(|v| v.as_str()) {
            Some(b) => {
                slots.insert(addr, b.to_string());
            }
            None => {
                slots.remove(&addr);
            }
        }
        let _ = kind; // 寻址与 blob 有无已足够表达 set/insert 语义
    }
    // blobs 回查全文 + 塞回 seq
    store
        .with_conn::<Vec<serde_json::Value>, rusqlite::Error>(|conn| {
            let mut out = Vec::with_capacity(slots.len());
            for (seq, blob_id) in &slots {
                let Some(mut msg) = blob_message(conn, blob_id) else {
                    continue;
                };
                if let Some(o) = msg.as_object_mut() {
                    o.insert("seq".to_string(), serde_json::json!(seq));
                }
                out.push(msg);
            }
            Ok(out)
        })
        .unwrap_or_default()
}

/// 补偿执行：把 message_slots 表回退到目标时刻的状态。
///
/// 1. 重建目标队列；2. 与当前表 diff 生成补偿 ops（恢复旧内容 / 清空多余槽，
///    同 `set` 原语）；3. 走表侧 apply；4. 追加 `PatchType::Rollback` 轨迹
/// （记录回退目标与补偿实录——旧轨迹 append-only 完好）。
pub fn rollback(
    store: &SqliteStore,
    pipeline_id: &str,
    tenant_id: &str,
    upto_created_at: &str,
) -> Result<(), StorageError> {
    let target = rebuild_messages_at(store, pipeline_id, tenant_id, upto_created_at);
    let current = store.load_message_history(pipeline_id, tenant_id)?;

    let target_map: BTreeMap<i64, &serde_json::Value> = target
        .iter()
        .filter_map(|m| m.get("seq").and_then(|s| s.as_i64()).map(|s| (s, m)))
        .collect();
    let current_map: BTreeMap<i64, &serde_json::Value> = current
        .iter()
        .filter_map(|m| m.get("seq").and_then(|s| s.as_i64()).map(|s| (s, m)))
        .collect();

    let mut ops = Vec::new();
    let keys: BTreeSet<i64> = target_map.keys().chain(current_map.keys()).cloned().collect();
    for k in keys {
        match (target_map.get(&k), current_map.get(&k)) {
            (Some(t), Some(c)) if t == c => {} // 已一致，无需补偿
            (Some(t), _) => {
                // 恢复/改写为目标内容（op 的 msg 不带 seq——seq 在 op 层）
                let mut m = (*t).clone();
                if let Some(o) = m.as_object_mut() {
                    o.remove("seq");
                }
                ops.push(serde_json::json!({ "op": "set", "seq": k, "msg": m }));
            }
            (None, Some(_)) => {
                // 目标时刻没有的槽位（上界之后新增）→ 清空留 gap
                ops.push(serde_json::json!({ "op": "set", "seq": k, "msg": null }));
            }
            (None, None) => unreachable!(),
        }
    }

    if !ops.is_empty() {
        store.apply_messages_ops_to_table(pipeline_id, tenant_id, &ops)?;
    }

    // Rollback 轨迹（append-only；补偿 ops 以指纹+blob 实录形式记录）
    let ledger: Vec<serde_json::Value> = ops.iter().filter_map(op_ledger_entry).collect();
    let entry_patch = serde_json::json!({
        "rollback_to": upto_created_at,
        "pipeline_id": pipeline_id,
        "messages": { "_ops": ledger },
    });
    let now = chrono::Utc::now().to_rfc3339();
    store.with_conn(|conn| {
        conn.execute(
            "INSERT INTO traces (trace_id, run_id, branch_id, seq_in_branch, plugin_id, \
             patch_type, patch_data, tenant_id, created_at) \
             VALUES (?1, ?2, 'main', 0, 'rollback', 'rollback', ?3, ?4, ?5)",
            rusqlite::params![
                format!("t_{}", uuid::Uuid::new_v4().simple()),
                format!("rb_{}", uuid::Uuid::new_v4().simple()),
                serde_json::to_string(&entry_patch).unwrap_or_default(),
                tenant_id,
                now,
            ],
        )?;
        Ok(())
    })
}
