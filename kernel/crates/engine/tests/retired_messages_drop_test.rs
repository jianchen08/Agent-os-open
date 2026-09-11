// @feature: FP-0.2.〇 存储清理 | @vision: V3 可嵌入 | @ci: rust-test
//! 旧 messages 投影表与旧宽表 message_slots 的迁移处置验证。
//!
//! 0.2 消息真值 = message_slots ⨝ blobs（读时重建），旧 messages 投影表已退役，
//! 旧宽表 message_slots（含 content_preview 等内容列）数据格式也不再支持。
//! init 迁移处置契约（数据保全优先于自动清理）：
//! - 空表 → DROP（db_admin 表清单与后端实际读写一一对应）；
//! - 有行 → `ALTER TABLE ... RENAME TO <name>_retired_<ts>` 留档 + error 留痕，
//!   并按现行 DDL 重建可用的纯索引表；人工确认无用后清理 _retired_ 表。
//!
//! 行为断言（公开 API，临时文件库模拟旧库升级路径）：
//! - 有行旧 messages / 旧宽表 message_slots → 原名撤位、副本行数不变、新表可用；
//! - 空旧 messages 表 → 仍被清理；
//! - 现行引擎表不受影响（message_slots/blobs 抽查）。

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

/// 旧宽表检测（与 migrate_drop_legacy_message_slots 同判据）：message_slots
/// 是否还含内容列。
fn message_slots_has_legacy_content_col(conn: &Connection) -> bool {
    let mut stmt = conn.prepare("PRAGMA table_info(message_slots)").unwrap();
    let rows = stmt.query_map([], |r| r.get::<_, String>(1)).unwrap();
    for c in rows {
        match c {
            Ok(col) if col == "content_preview" || col == "tool_calls_json" => return true,
            _ => {}
        }
    }
    false
}

#[test]
fn open_preserves_nonempty_legacy_messages_as_renamed_copy() {
    let path = std::env::temp_dir().join("agentos_retired_messages_preserve_test.db");
    let path_str = path.to_str().unwrap();
    let _ = std::fs::remove_file(&path);

    // 旧库：残留含数据的退役 messages 表
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
        "有行旧 messages 表原名必须撤位"
    );
    let copies = retired_copies_with_rowcounts(&conn, "messages");
    assert_eq!(copies.len(), 1, "必须留下恰好一个 _retired_ 副本");
    assert_eq!(copies[0].1, 1, "副本行数必须与原表一致");
    assert!(table_exists(&conn, "message_slots"), "现行槽位表应保留");
    assert!(table_exists(&conn, "blobs"), "现行 blobs 表应保留");
    drop(store);
    let _ = std::fs::remove_file(&path);
}

#[test]
fn open_drops_empty_legacy_messages_table() {
    let path = std::env::temp_dir().join("agentos_retired_messages_drop_test.db");
    let path_str = path.to_str().unwrap();
    let _ = std::fs::remove_file(&path);

    // 旧库：残留空退役 messages 表
    {
        let conn = Connection::open(path_str).unwrap();
        conn.execute_batch("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT);")
            .unwrap();
    }

    let store = SqliteStore::open(path_str).unwrap();
    let conn = Connection::open(path_str).unwrap();
    assert!(
        !table_exists(&conn, "messages"),
        "空旧 messages 表应在 open/init 后被 DROP"
    );
    assert!(
        retired_copies_with_rowcounts(&conn, "messages").is_empty(),
        "空表不应留下 _retired_ 副本"
    );
    assert!(table_exists(&conn, "message_slots"), "现行槽位表应保留");
    drop(store);
    let _ = std::fs::remove_file(&path);
}

#[test]
fn open_preserves_legacy_wide_message_slots_and_rebuilds_pure_table() {
    let path = std::env::temp_dir().join("agentos_legacy_slots_wide_test.db");
    let path_str = path.to_str().unwrap();
    let _ = std::fs::remove_file(&path);

    // 旧库：宽表 message_slots（含内容列 content_preview）且有一行数据
    {
        let conn = Connection::open(path_str).unwrap();
        conn.execute_batch(
            "CREATE TABLE message_slots (\
                 tenant_id TEXT NOT NULL DEFAULT 'default', \
                 pipeline_id TEXT NOT NULL, \
                 seq INTEGER NOT NULL, \
                 message_id TEXT NOT NULL, \
                 role TEXT, \
                 content_preview TEXT, \
                 tool_calls_json TEXT, \
                 run_id TEXT, \
                 created_at TEXT NOT NULL, \
                 PRIMARY KEY (tenant_id, pipeline_id, seq)); \
             INSERT INTO message_slots (tenant_id, pipeline_id, seq, message_id, role, content_preview, created_at) \
             VALUES ('default', 'p-legacy', 0, 'mc_legacy', 'user', '旧消息全文', '2026-09-01T00:00:00+00:00');",
        )
        .unwrap();
    }

    let store = SqliteStore::open(path_str).unwrap();
    let conn = Connection::open(path_str).unwrap();

    // 旧宽表撤位保全
    assert!(
        retired_copies_with_rowcounts(&conn, "message_slots").len() == 1
            && retired_copies_with_rowcounts(&conn, "message_slots")[0].1 == 1,
        "有行旧宽表必须留下行数一致的 _retired_ 副本"
    );

    // 新纯索引表重建生效：无内容列
    assert!(
        !message_slots_has_legacy_content_col(&conn),
        "重建后的 message_slots 不应再有内容列"
    );

    // 新表对现行引擎写入可用（apply_messages_ops_to_table 走通）
    store
        .apply_messages_ops_to_table(
            "p-new",
            "default",
            &[serde_json::json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "新消息"}})],
        )
        .unwrap();
    drop(store);
    let _ = std::fs::remove_file(&path);
}
