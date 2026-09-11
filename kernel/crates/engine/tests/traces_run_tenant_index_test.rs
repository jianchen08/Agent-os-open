// @feature: FP-0.2.〇 存储账本 | @vision: V3 可嵌入 | @ci: rust-test
//! traces 热查询索引回归。
//!
//! 轨迹热查询按 `run_id IN (...) AND tenant_id` 过滤 traces
//! （get_step_traces_by_thread 经 message_slots 反查 run_id 集合后扫 traces），
//! 既有 idx_traces_branch_seq(branch_id, seq_in_branch) 覆盖不到该路径——
//! 无 (run_id, tenant_id) 索引时热查询全表扫描。
//!
//! 断言（sqlite_master / PRAGMA 权威）：文件库 open 后 idx_traces_run_tenant
//! 存在，且列序 = (run_id, tenant_id)（run_id 为 IN 等值前导列）。

use agentos_engine::SqliteStore;

#[test]
fn open_creates_idx_traces_run_tenant_index() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("idx_traces_test.db");
    let _store = SqliteStore::open(path.to_str().unwrap()).unwrap();

    let conn = rusqlite::Connection::open(&path).unwrap();
    let index_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master \
             WHERE type = 'index' AND name = 'idx_traces_run_tenant'",
            [],
            |r| r.get(0),
        )
        .expect("查询 sqlite_master 应成功");
    assert_eq!(index_count, 1, "idx_traces_run_tenant 索引必须存在");

    // 列序性质断言：首列 run_id（查询的 IN 前导列），次列 tenant_id
    let cols: Vec<String> = {
        let mut stmt = conn
            .prepare("PRAGMA index_info(idx_traces_run_tenant)")
            .unwrap();
        let rows = stmt.query_map([], |r| r.get::<_, String>(2)).unwrap();
        rows.collect::<Result<Vec<_>, _>>().unwrap()
    };
    assert_eq!(
        cols,
        vec!["run_id".to_string(), "tenant_id".to_string()],
        "索引列序必须为 (run_id, tenant_id)"
    );
}
