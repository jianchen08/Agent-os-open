// @feature: FP-0.2.〇 存储账本 | @vision: V3 可观测 | @ci: rust-test
//! 工具调用记录页窗口查询索引回归（monitoring tool-calls 明细/统计）。
//!
//! monitoring 插件按 `WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?` 取
//! 最近 N 条 trace 作明细/聚合窗口：无 (tenant_id, created_at) 索引时该窗口
//! 子查询全表扫描并逐行跳读 patch_data 溢出页尾列（1.5GB/2 万行实测 0.6s，
//! 旧内容谓词版 8.2-8.5s——端点超时内必然失败，BUG-47）。
//!
//! 断言（sqlite_master / EXPLAIN QUERY PLAN 权威）：文件库 open 后
//! idx_traces_tenant_created 存在，列序 = (tenant_id, created_at)，且窗口
//! 子查询走该索引、无 ORDER BY 临时 B 树。

use agentos_core::traits::StorageBackend;
use agentos_core::types::{PatchType, TraceEntry};
use agentos_engine::SqliteStore;

#[tokio::test]
async fn open_creates_idx_traces_tenant_created_and_window_query_uses_it() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("idx_traces_tenant_created_test.db");
    let store = SqliteStore::open(path.to_str().unwrap()).unwrap();

    // 窗口子查询的消费形态需要真实行：走公共 append_trace（tenant 走
    // current_or_default = 'default'，与下方查询参数一致）
    store.create_run("run-1", "hash", "default").unwrap();
    for i in 0..5u32 {
        store
            .append_trace(TraceEntry {
                trace_id: format!("trace_{i}"),
                run_id: "run-1".to_string(),
                branch_id: "main".to_string(),
                seq_in_branch: i,
                plugin_id: "core".to_string(),
                patch_type: PatchType::StateUpdate,
                patch_data: serde_json::json!({"key": i}),
                created_at: chrono::Utc::now().to_rfc3339(),
            })
            .await
            .unwrap();
    }

    // 索引存在 + 列序性质断言：首列 tenant_id（等值前导），次列 created_at
    let index_count: i64 = store
        .with_conn(|c| {
            c.query_row(
                "SELECT COUNT(*) FROM sqlite_master \
                 WHERE type = 'index' AND name = 'idx_traces_tenant_created'",
                [],
                |r| r.get(0),
            )
        })
        .unwrap();
    assert_eq!(index_count, 1, "idx_traces_tenant_created 索引必须存在");

    let cols: Vec<String> = store
        .with_conn(|c| {
            let mut stmt = c.prepare("PRAGMA index_info(idx_traces_tenant_created)")?;
            let rows = stmt.query_map([], |r| r.get::<_, String>(2))?;
            rows.collect()
        })
        .unwrap();
    assert_eq!(
        cols,
        vec!["tenant_id".to_string(), "created_at".to_string()],
        "索引列序必须为 (tenant_id, created_at)"
    );

    // 窗口子查询计划：走该索引，无排序临时 B 树（否则窗口退化为全表扫描）
    let plan: String = store
        .with_conn(|c| {
            let mut stmt = c.prepare(
                "EXPLAIN QUERY PLAN SELECT trace_id FROM traces \
                 WHERE tenant_id = ?1 ORDER BY created_at DESC LIMIT ?2",
            )?;
            let rows = stmt.query_map(rusqlite::params!["default", 50i64], |r| {
                r.get::<_, String>(3)
            })?;
            let mut out = String::new();
            for row in rows {
                out.push_str(&row?);
                out.push('\n');
            }
            Ok::<String, rusqlite::Error>(out)
        })
        .unwrap();
    assert!(
        plan.contains("idx_traces_tenant_created"),
        "窗口子查询必须走 idx_traces_tenant_created，实际计划：{plan}"
    );
    assert!(
        !plan.contains("TEMP B-TREE"),
        "窗口子查询不得出现排序临时 B 树，实际计划：{plan}"
    );
}
