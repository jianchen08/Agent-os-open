// @feature: FP-0.2.〇 存储账本 | @vision: V3 可观测 | @ci: rust-test
//! traces 热查询索引回归（ADR 2026-09-18 换轨后）。
//!
//! 轨迹读路径按 `(pipeline_id, tenant_id)` 定位、按 seq 升序回放
//! （op 流定位 = (pipeline_id, seq)；旧 run/branch 键查询与
//! idx_traces_run_tenant 索引已随 runs/branches 表一并退役）。
//! 无该复合索引时管道轨迹回放退化为全表扫描。
//!
//! 断言（sqlite_master / PRAGMA / EXPLAIN 权威）：文件库 open 后
//! idx_traces_pipeline_seq 存在，列序 = (pipeline_id, tenant_id, seq)
//! （pipeline_id 等值前导），且管道轨迹窗口查询走该索引。

use agentos_engine::SqliteStore;

#[test]
fn open_creates_idx_traces_pipeline_seq_index() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("idx_traces_pipeline_seq_test.db");
    let _store = SqliteStore::open(path.to_str().unwrap()).unwrap();

    let conn = rusqlite::Connection::open(&path).unwrap();
    let index_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master \
             WHERE type = 'index' AND name = 'idx_traces_pipeline_seq'",
            [],
            |r| r.get(0),
        )
        .expect("查询 sqlite_master 应成功");
    assert_eq!(index_count, 1, "idx_traces_pipeline_seq 索引必须存在");

    // 列序性质断言：首列 pipeline_id（等值前导），次列 tenant_id，末列 seq（日志序回放）
    let cols: Vec<String> = {
        let mut stmt = conn
            .prepare("PRAGMA index_info(idx_traces_pipeline_seq)")
            .unwrap();
        let rows = stmt.query_map([], |r| r.get::<_, String>(2)).unwrap();
        rows.collect::<Result<Vec<_>, _>>().unwrap()
    };
    assert_eq!(
        cols,
        vec![
            "pipeline_id".to_string(),
            "tenant_id".to_string(),
            "seq".to_string()
        ],
        "索引列序必须为 (pipeline_id, tenant_id, seq)"
    );

    // 管道轨迹窗口查询计划：走该索引（等值前导 + 有序回放，无排序临时 B 树）
    let plan: String = {
        let mut stmt = conn
            .prepare(
                "EXPLAIN QUERY PLAN SELECT seq, patch_data FROM traces \
                 WHERE pipeline_id = ?1 AND tenant_id = ?2 ORDER BY seq ASC",
            )
            .unwrap();
        let rows = stmt
            .query_map(rusqlite::params!["pipe-x", "default"], |r| {
                r.get::<_, String>(3)
            })
            .unwrap();
        let mut out = String::new();
        for row in rows {
            out.push_str(&row.unwrap());
            out.push('\n');
        }
        out
    };
    assert!(
        plan.contains("idx_traces_pipeline_seq"),
        "管道轨迹查询必须走 idx_traces_pipeline_seq，实际计划：{plan}"
    );
}
