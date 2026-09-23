// @feature: FP-0.2.〇 存储账本 | @vision: V3 可嵌入 | @ci: rust-test
//! record_run_start 运行簿记回归（ADR 2026-09-18：runs/branches 退役，state 唯一真值）。
//!
//! 旧 create_run 的 runs/branches 双写已退役；运行开始簿记 = 单事务批量 upsert
//! pipeline_state 运行键（run_id / run_status='running' / run_started_at /
//! run_config_hash）——任一键失败整批回滚（upsert_state_fields 事务形态）。
//!
//! 行为断言（公开 API，真实依赖不 mock）：
//! - record_run_start 成功 → 四个运行键各恰好一行（标量编码 str 原文）；
//! - get_run 按 run_id 反查所属管道合成投影（status/created_at/config_hash/
//!   pipeline_id；current_branch/current_seq 为退役后的恒定兼容值）；
//! - 同管道重复 record_run_start 覆盖为最新运行（每管道至多一条当前运行投影）。

use agentos_core::traits::StorageBackend;
use agentos_core::types::RunStatus;
use agentos_engine::SqliteStore;

fn count(store: &SqliteStore, sql: &str) -> i64 {
    store
        .with_conn(|c| -> Result<i64, rusqlite::Error> { c.query_row(sql, [], |r| r.get(0)) })
        .expect("计数查询应成功")
}

#[test]
fn record_run_start_writes_four_run_keys_atomically() {
    let store = SqliteStore::open_memory().unwrap();
    store
        .record_run_start("pipe-ok", "tenant-1", "run-ok", "hash-1")
        .unwrap();

    // 四个运行键各恰好一行，值按标量 str 原文落库
    for (key, expected) in [
        ("run_id", "run-ok"),
        ("run_status", "running"),
        ("run_config_hash", "hash-1"),
    ] {
        let value: String = store
            .with_conn(|c| {
                c.query_row(
                    "SELECT value FROM pipeline_state \
                     WHERE pipeline_id = 'pipe-ok' AND tenant_id = 'tenant-1' AND field_key = ?1",
                    [key],
                    |r| r.get(0),
                )
            })
            .unwrap_or_else(|e| panic!("运行键 {key} 必须落库: {e}"));
        assert_eq!(value, expected, "运行键 {key} 的标量值必须为原文");
        assert_eq!(
            count(
                &store,
                &format!(
                    "SELECT COUNT(*) FROM pipeline_state \
                     WHERE pipeline_id = 'pipe-ok' AND tenant_id = 'tenant-1' AND field_key = '{key}'"
                )
            ),
            1,
            "运行键 {key} 必须恰好一行"
        );
    }
    assert_eq!(
        count(
            &store,
            "SELECT COUNT(*) FROM pipeline_state \
             WHERE pipeline_id = 'pipe-ok' AND tenant_id = 'tenant-1' AND field_key = 'run_started_at'"
        ),
        1,
        "run_started_at 必须随簿记同批落库"
    );
}

#[tokio::test]
async fn get_run_projects_run_bookkeeping_by_reverse_lookup() {
    let store = SqliteStore::open_memory().unwrap();
    store
        .record_run_start("pipe-a", "tenant-a", "run-a", "hash-a")
        .unwrap();

    // get_run 按 task_local 租户反查：必须在租户 a 的作用域内读
    let run = agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant-a", "s"),
        StorageBackend::get_run(&store, "run-a"),
    )
    .await
    .unwrap();
    assert_eq!(run.run_id, "run-a");
    assert_eq!(run.config_hash, "hash-a");
    assert_eq!(run.tenant_id, "tenant-a");
    assert!(
        matches!(run.status, RunStatus::Running),
        "簿记初始态 running"
    );
    assert_eq!(
        run.pipeline_id.as_deref(),
        Some("pipe-a"),
        "投影必须携带反查所得的所属管道"
    );
    assert!(
        !run.created_at.is_empty(),
        "created_at = state.run_started_at"
    );
    assert!(run.ended_at.is_none(), "未结束的运行无 ended_at");
    assert_eq!(run.current_branch, "main", "导航指针退役后的恒定兼容值");
    assert_eq!(run.current_seq, 0, "导航指针退役后的恒定兼容值");

    // 租户隔离：其他租户按同一 run_id 反查不得命中
    let hits = store
        .with_conn(|c| {
            c.query_row(
                "SELECT COUNT(*) FROM pipeline_state \
                 WHERE field_key = 'run_id' AND value = 'run-a' AND tenant_id = 'tenant-b'",
                [],
                |r| r.get::<_, i64>(0),
            )
        })
        .unwrap();
    assert_eq!(hits, 0, "run 簿记必须按租户隔离落库");
}

#[tokio::test]
async fn record_run_start_overwrites_to_latest_run_per_pipeline() {
    let store = SqliteStore::open_memory().unwrap();
    store
        .record_run_start("pipe-x", "default", "run-old", "hash-old")
        .unwrap();
    store
        .record_run_start("pipe-x", "default", "run-new", "hash-new")
        .unwrap();

    // 每管道至多一条当前运行投影：旧 run_id 被覆盖，反查只命中新 run
    assert!(
        StorageBackend::get_run(&store, "run-old").await.is_err(),
        "被覆盖的旧 run_id 反查必须 NotFound"
    );
    let runs = StorageBackend::list_runs_by_pipeline(&store, "pipe-x", "default")
        .await
        .unwrap();
    assert_eq!(runs.len(), 1, "每管道至多一条当前运行投影");
    assert_eq!(runs[0].run_id, "run-new");
    assert_eq!(runs[0].config_hash, "hash-new");
}
