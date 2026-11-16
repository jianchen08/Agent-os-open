//! D12 持久账本装配契约：db 接线（with_db）时消费走 store 持久账本
//! （跨 AppState 实例/重启判已消费）；db 未接线退回进程内集合。

use super::*;

#[test]
fn db_wired_consume_uses_persistent_ledger_across_state_instances() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("jti_glue.db");
    let sqlite = Arc::new(agentos_engine::SqliteStore::open(db_path.to_str().unwrap()).unwrap());

    let state = AppState::new().with_db(sqlite);
    assert!(
        state.consume_refresh_jti("jti-glue-1"),
        "db 接线下首次消费必须放行"
    );
    assert!(
        !state.consume_refresh_jti("jti-glue-1"),
        "同 jti 二次消费必须拒绝"
    );

    // 同库文件新开 AppState（模拟内核重启）：已消费 jti 不得复活
    let sqlite2 = Arc::new(agentos_engine::SqliteStore::open(db_path.to_str().unwrap()).unwrap());
    let state2 = AppState::new().with_db(sqlite2);
    assert!(
        !state2.consume_refresh_jti("jti-glue-1"),
        "重启后已消费 jti 必须仍判已消费（持久账本）"
    );
}

#[test]
fn db_unwired_consume_falls_back_to_in_memory_set() {
    let state = AppState::new();
    assert!(state.db.is_none(), "前置：裸 AppState 未接线 db");
    assert!(state.consume_refresh_jti("jti-mem-1"), "首次消费放行");
    assert!(!state.consume_refresh_jti("jti-mem-1"), "旧值复用拒绝");
}
