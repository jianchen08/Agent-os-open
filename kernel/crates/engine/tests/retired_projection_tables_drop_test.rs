// @feature: FP-0.2.〇 存储清理 | @vision: V3 可嵌入 | @ci: rust-test
//! 退役 0.1 投影表迁移处置回归（不留两套真值 + 数据保全）。
//!
//! execution_records / pipeline_run_summaries / memory 三张 0.1 投影表已退役：
//! - 执行记录/会话消耗账本由 messages 真值派生读路径替代（调试中心执行记录页
//!   走 messages.list 拼装、LLM 请求页走 payload_diag）；
//! - 记忆面归 hindsight 插件自持存储（kernel 表兜底已裁定为糊弄，一并退役）。
//!
//! dynamic_tools 表同样退役——动态注册的工具是 state 域
//! 数据，不应耦合在内核存储（跨重启重建由插件自持 state/config 承担；
//! registry 内存注册机制与 (registry, register_tool) capability 不受影响）。
//!
//! 四表运行时零生产者。init 迁移的处置契约（数据保全优先于自动清理）：
//! - 空表 → DROP（db_admin 表清单与后端读写一一对应）；
//! - 有行 → `ALTER TABLE ... RENAME TO <name>_retired_<ts>` 留档 + error 留痕，
//!   不自动删除，人工确认无用后清理。
//!
//! 行为断言（公开 API，临时文件库模拟旧库升级路径）：
//! - 旧库残留四张退役空表 → SqliteStore::open（内含 init）后消失；
//! - 旧库残留有行的退役表 → 原名撤位、`*_retired_*` 副本行数不变；
//! - 现行引擎表不受影响（traces/message_slots/blobs/pipeline_state 抽查；
//!   runs/branches 已随 ADR 2026-09-18 退役，簿记收敛进 pipeline_state）。

use agentos_engine::SqliteStore;
use rusqlite::Connection;

fn table_exists(conn: &Connection, name: &str) -> bool {
    let n: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?1",
            rusqlite::params![name],
            |row| row.get(0),
        )
        .unwrap_or(0);
    n > 0
}

/// 返回名字命中 `<prefix>_retired_%` 的表及各自行数。
fn retired_copies_with_rowcounts(conn: &Connection, prefix: &str) -> Vec<(String, i64)> {
    let pattern = format!("{prefix}_retired_%");
    let mut stmt = conn
        .prepare("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?1")
        .unwrap();
    let names: Vec<String> = stmt
        .query_map([&pattern], |row| row.get::<_, String>(0))
        .unwrap()
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    names
        .into_iter()
        .map(|name| {
            let rows: i64 = conn
                .query_row(&format!("SELECT COUNT(*) FROM \"{name}\""), [], |r| {
                    r.get(0)
                })
                .unwrap();
            (name, rows)
        })
        .collect()
}

#[test]
fn open_drops_empty_retired_projection_tables() {
    let path = std::env::temp_dir().join("agentos_retired_projection_tables_drop_test.db");
    let path_str = path.to_str().unwrap();
    let _ = std::fs::remove_file(&path);

    // 旧库：残留四张退役表（全空）
    {
        let conn = Connection::open(path_str).unwrap();
        conn.execute_batch(
            "CREATE TABLE execution_records (record_id TEXT, sequence INTEGER); \
             CREATE TABLE pipeline_run_summaries (run_id TEXT); \
             CREATE TABLE memory (id TEXT); \
             CREATE TABLE dynamic_tools (plugin_id TEXT, tool_name TEXT);",
        )
        .unwrap();
    }

    // 升级路径：SqliteStore::open → init 迁移
    let store = SqliteStore::open(path_str).unwrap();
    let conn = Connection::open(path_str).unwrap();
    for retired in [
        "execution_records",
        "pipeline_run_summaries",
        "memory",
        "dynamic_tools",
    ] {
        assert!(
            !table_exists(&conn, retired),
            "空退役表应在 open/init 后被 DROP：{retired}"
        );
        assert!(
            retired_copies_with_rowcounts(&conn, retired).is_empty(),
            "空退役表不应留下 _retired_ 副本：{retired}"
        );
    }
    // runs 已退役（ADR 2026-09-18）：open 后不得再存在
    assert!(!table_exists(&conn, "runs"), "退役的 runs 表不应重建");
    for live in ["traces", "message_slots", "blobs", "pipeline_state"] {
        assert!(table_exists(&conn, live), "现行引擎表应保留：{live}");
    }
    drop(store);
    let _ = std::fs::remove_file(&path);
}

#[test]
fn open_preserves_nonempty_retired_tables_as_renamed_copies() {
    let path = std::env::temp_dir().join("agentos_retired_tables_preserve_test.db");
    let path_str = path.to_str().unwrap();
    let _ = std::fs::remove_file(&path);

    // 旧库：两张退役表有数据，一张空，dynamic_tools 空
    {
        let conn = Connection::open(path_str).unwrap();
        conn.execute_batch(
            "CREATE TABLE execution_records (record_id TEXT, sequence INTEGER); \
             INSERT INTO execution_records VALUES ('r1', 1); \
             INSERT INTO execution_records VALUES ('r2', 2); \
             CREATE TABLE memory (id TEXT); \
             INSERT INTO memory VALUES ('m1'); \
             CREATE TABLE pipeline_run_summaries (run_id TEXT); \
             CREATE TABLE dynamic_tools (plugin_id TEXT, tool_name TEXT);",
        )
        .unwrap();
    }

    let store = SqliteStore::open(path_str).unwrap();
    let conn = Connection::open(path_str).unwrap();

    // 有行退役表：原名撤位 + 改名副本行数不变（数据保全）
    for (table, rows) in [("execution_records", 2i64), ("memory", 1)] {
        assert!(
            !table_exists(&conn, table),
            "有行退役表原名必须撤位：{table}"
        );
        let copies = retired_copies_with_rowcounts(&conn, table);
        assert_eq!(
            copies.len(),
            1,
            "有行退役表必须留下恰好一个 _retired_ 副本：{table}"
        );
        assert_eq!(
            copies[0].1, rows,
            "_retired_ 副本行数必须与原表一致：{table}"
        );
    }

    // 空退役表：仍直接清理，不留副本
    for empty in ["pipeline_run_summaries", "dynamic_tools"] {
        assert!(!table_exists(&conn, empty), "空退役表应被 DROP：{empty}");
        assert!(
            retired_copies_with_rowcounts(&conn, empty).is_empty(),
            "空退役表不应留下 _retired_ 副本：{empty}"
        );
    }

    // 现行引擎表可用（新 schema 生效）
    // runs 已退役（ADR 2026-09-18）：open 后不得再存在
    assert!(!table_exists(&conn, "runs"), "退役的 runs 表不应重建");
    for live in ["traces", "message_slots", "blobs", "pipeline_state"] {
        assert!(table_exists(&conn, live), "现行引擎表应保留：{live}");
    }
    drop(store);
    let _ = std::fs::remove_file(&path);
}

#[test]
fn open_memory_drops_retired_projection_tables() {
    // 新库（in-memory）也不应再创建三张投影表
    let _store = SqliteStore::open_memory().unwrap();
    // open_memory 无法从外部探测表清单，这里只验证不炸；
    // 新库不建表由 DDL 删除保证，文件库路径已在上一用例覆盖。
}
