// @feature: FP-0.2.〇 存储清理 | @vision: V3 可嵌入 | @ci: rust-test
//! 退役 messages 表清理验证（2026-08-19 调试中心数据库管理页审查）。
//!
//! 0.2 消息真值 = message_slots ⨝ blobs（读时重建），旧 messages 投影表已退役。
//! 存量库会残留该表（现行 DDL 不再创建也不删除），导致 db_admin 表清单出现
//! "页面有条目、后端无读写"的死表。init 迁移负责 DROP，保证表清单一一对应。
//!
//! 行为断言（公开 API，临时文件库模拟旧库升级路径）：
//! - 旧库残留 messages 表 → SqliteStore::open（内含 init）后消失；
//! - 现行引擎表不受影响（以 message_slots/blobs 为代表抽查）。

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
fn open_drops_retired_messages_table() {
    let path = std::env::temp_dir().join("agentos_retired_messages_drop_test.db");
    let path_str = path.to_str().unwrap();
    let _ = std::fs::remove_file(&path);

    // 旧库：残留退役 messages 表
    {
        let conn = Connection::open(path_str).unwrap();
        conn.execute_batch(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT); \
             INSERT INTO messages (content) VALUES ('legacy');",
        )
        .unwrap();
    }

    // 升级路径：SqliteStore::open → init 迁移
    let store = SqliteStore::open(path_str).unwrap();
    let conn = Connection::open(path_str).unwrap();
    assert!(
        !table_exists(&conn, "messages"),
        "退役 messages 表应在 open/init 后被 DROP"
    );
    assert!(table_exists(&conn, "message_slots"), "现行槽位表应保留");
    assert!(table_exists(&conn, "blobs"), "现行 blobs 表应保留");
    drop(store);
    let _ = std::fs::remove_file(&path);
}
