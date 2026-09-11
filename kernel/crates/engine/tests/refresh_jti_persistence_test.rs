// @feature: FP-0.2.〇 存储账本 | @vision: V3 可嵌入 | @ci: rust-test
//! consumed_refresh_jtis 持久账本回归（api auth D12 单次轮换的存储侧契约）。
//!
//! 进程内 HashSet 记录重启即清——固定 token 口令的部署里已消费的 refresh token
//! （有效期 7 天）会在重启后复活复用。持久账本契约：
//! - 同一 jti 二次消费 → false（单次轮换）；
//! - 消费后同一库文件新开 store 实例（模拟内核重启）→ 仍判已消费；
//! - 消费写入顺手清理 7 天窗口外旧行（窗口与 refresh TTL 上限对齐，表不无界增长）。

use agentos_engine::SqliteStore;

#[test]
fn consume_refresh_jti_is_single_use() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("jti_single_use.db");
    let store = SqliteStore::open(path.to_str().unwrap()).unwrap();

    assert!(
        store.consume_refresh_jti("jti-1").unwrap(),
        "首次消费必须放行"
    );
    assert!(
        !store.consume_refresh_jti("jti-1").unwrap(),
        "同 jti 二次消费必须拒绝（单次轮换）"
    );
}

#[test]
fn consumed_jti_survives_store_reopen() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("jti_reopen.db");

    let store = SqliteStore::open(path.to_str().unwrap()).unwrap();
    assert!(store.consume_refresh_jti("jti-rotate").unwrap());
    drop(store);

    // 同文件新开实例（模拟内核重启）：已消费 token 不得复活
    let reopened = SqliteStore::open(path.to_str().unwrap()).unwrap();
    assert!(
        !reopened.consume_refresh_jti("jti-rotate").unwrap(),
        "重启后已消费 jti 必须仍判已消费（持久吊销）"
    );
}

#[test]
fn consume_cleanup_drops_entries_older_than_ttl_window() {
    let store = SqliteStore::open_memory().unwrap();
    let now = chrono::Utc::now().timestamp();
    let ttl = 7 * 24 * 60 * 60;
    store
        .with_conn(|c| -> Result<(), rusqlite::Error> {
            let mut stmt =
                c.prepare("INSERT INTO consumed_refresh_jtis (jti, consumed_at) VALUES (?1, ?2)")?;
            // 量级相反的两点：窗口外（8 天前）与窗口内（1 分钟前）
            stmt.execute(("jti-stale", now - ttl - 24 * 60 * 60))?;
            stmt.execute(("jti-fresh", now - 60))?;
            Ok(())
        })
        .unwrap();

    // 消费一个新 jti 触发顺手清理
    assert!(store.consume_refresh_jti("jti-new").unwrap());

    let (stale, fresh, fresh_new): (i64, i64, i64) = store
        .with_conn(|c| -> Result<(i64, i64, i64), rusqlite::Error> {
            c.query_row(
                "SELECT \
                     (SELECT COUNT(*) FROM consumed_refresh_jtis WHERE jti = 'jti-stale'), \
                     (SELECT COUNT(*) FROM consumed_refresh_jtis WHERE jti = 'jti-fresh'), \
                     (SELECT COUNT(*) FROM consumed_refresh_jtis WHERE jti = 'jti-new')",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
        })
        .unwrap();
    assert_eq!(stale, 0, "TTL 窗口外旧行必须被消费写入顺手清理");
    assert_eq!(fresh, 1, "窗口内行不受清理影响");
    assert_eq!(fresh_new, 1, "本次消费的 jti 必须落账");
}
