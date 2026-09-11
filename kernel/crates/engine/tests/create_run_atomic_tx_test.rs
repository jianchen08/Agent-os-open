// @feature: FP-0.2.〇 存储账本 | @vision: V3 可嵌入 | @ci: rust-test
//! create_run 双写事务性回归：runs 与 branches main 必须同一事务落库。
//!
//! 两条 INSERT 各自 autocommit 时，进程在两条语句之间被截断会留下"有 run、
//! 无分支"的半态——轨迹读路径按分支定位，半态不可恢复。事务化后任一条失败
//! 整批回滚（语义对齐 apply_messages_ops_to_table 的既有事务形态）。
//!
//! 行为断言（公开 API，真实依赖不 mock）：
//! - create_run 成功 → runs 与 branches(main) 各恰好一行；
//! - 第二条 INSERT 失败（branches 主键预置碰撞）→ runs 行必须被回滚
//!   （autocommit 实现下该 runs 行会残留为半态）。

use agentos_engine::SqliteStore;

fn count(store: &SqliteStore, sql: &str) -> i64 {
    store
        .with_conn(|c| -> Result<i64, rusqlite::Error> { c.query_row(sql, [], |r| r.get(0)) })
        .expect("计数查询应成功")
}

#[test]
fn create_run_persists_run_and_main_branch_together() {
    let store = SqliteStore::open_memory().unwrap();
    store.create_run("run-ok", "hash-1", "tenant-1").unwrap();
    assert_eq!(
        count(&store, "SELECT COUNT(*) FROM runs WHERE run_id = 'run-ok'"),
        1,
        "runs 行必须落库"
    );
    assert_eq!(
        count(
            &store,
            "SELECT COUNT(*) FROM branches WHERE run_id = 'run-ok' AND branch_id = 'main'"
        ),
        1,
        "main 分支行必须随 run 同批落库"
    );
}

#[test]
fn create_run_rolls_back_run_row_when_branch_insert_fails() {
    let store = SqliteStore::open_memory().unwrap();
    // 预置同主键 (branch_id='main', run_id='run-clash') 的分支行 →
    // create_run 事务内第二条 INSERT 必失败（主键碰撞）
    store
        .with_conn(|c| -> Result<(), rusqlite::Error> {
            c.execute(
                "INSERT INTO branches (branch_id, run_id, tenant_id, created_at) \
                 VALUES ('main', 'run-clash', 'tenant-1', '2026-09-11T00:00:00+00:00')",
                [],
            )?;
            Ok(())
        })
        .unwrap();

    assert!(
        store.create_run("run-clash", "hash-1", "tenant-1").is_err(),
        "分支行碰撞必须令 create_run 失败"
    );

    assert_eq!(
        count(
            &store,
            "SELECT COUNT(*) FROM runs WHERE run_id = 'run-clash'"
        ),
        0,
        "分支写入失败时 runs 行必须整批回滚，不得残留半态"
    );
}
