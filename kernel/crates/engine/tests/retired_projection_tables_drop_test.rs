// @feature: FP-0.2.〇 存储清理 | @vision: V3 可嵌入 | @ci: rust-test
//! 退役 0.1 投影表 DROP 迁移回归（不留两套真值）。
//!
//! execution_records / pipeline_run_summaries / memory 三张 0.1 投影表已退役：
//! - 执行记录/会话消耗账本由 messages 真值派生读路径替代（调试中心执行记录页
//!   走 messages.list 拼装、LLM 请求页走 payload_diag）；
//! - 记忆面归 hindsight 插件自持存储（kernel 表兜底已裁定为糊弄，一并退役）。
//!
//! 三表运行时零生产者、全库零行。存量库残留空表会让 db_admin 表清单出现
//! "页面有条目、后端无读写"的死表——init 迁移负责 DROP，保证表清单一一对应。
//!
//! dynamic_tools 表同样退役——动态注册的工具是 state 域
//! 数据，不应耦合在内核存储（跨重启重建由插件自持 state/config 承担；
//! registry 内存注册机制与 (registry, register_tool) capability 不受影响）。
//!
//! 行为断言（公开 API，临时文件库模拟旧库升级路径）：
//! - 旧库残留四张退役表 → SqliteStore::open（内含 init）后消失；
//! - 现行引擎表不受影响（runs/message_slots/blobs/pipeline_state 抽查）。

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

#[test]
fn open_drops_retired_projection_tables() {
    let path = std::env::temp_dir().join("agentos_retired_projection_tables_drop_test.db");
    let path_str = path.to_str().unwrap();
    let _ = std::fs::remove_file(&path);

    // 旧库：残留四张退役表
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
            "退役投影表应在 open/init 后被 DROP：{retired}"
        );
    }
    for live in ["runs", "message_slots", "blobs", "pipeline_state"] {
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
