// @feature: FP-0.2.〇 存储账本 | @vision: V3 可嵌入 | @ci: rust-test
//! traces 保留期 + blobs 孤儿清扫（ADR 2026-09-11）行为回归。
//!
//! 断言可观察行为：过期 traces 与孤儿 blob 在 purge 后消失、新鲜/被引数据
//! 完好、返回删除行数正确且幂等（二次清扫零删除）；保留期解析（env 覆盖 /
//! 0 = 禁用 / 误配回落默认）在 `trace_retention_days` 上断言——0 的禁用
//! 语义 = 解析返回 0，接线侧（agentos-kernel）据此不启动清扫任务。

use agentos_core::types::StorageError;
use agentos_engine::store::trace_retention_days;
use agentos_engine::SqliteStore;

/// 环境变量互斥锁：同二进制并行测试线程间 set/remove 与读取的竞态防护
/// （与 storage_factory 测试同规）。
static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// 直插一条指定 created_at 的 trace（绕过 append_trace 的时钟，控制新旧）。
fn insert_trace(store: &SqliteStore, trace_id: &str, created_at: &str, tenant_id: &str) {
    store
        .with_conn::<_, StorageError>(|conn| {
            conn.execute(
                "INSERT INTO traces (trace_id, pipeline_id, seq, plugin_id, \
                 patch_type, patch_data, tenant_id, created_at) \
                 VALUES (?1, 'pipe-ret', 0, 'p', 'state_update', '{}', ?2, ?3)",
                rusqlite::params![trace_id, tenant_id, created_at],
            )?;
            Ok(())
        })
        .unwrap();
}

fn insert_blob(store: &SqliteStore, blob_id: &str, payload: &[u8]) {
    store
        .with_conn::<_, StorageError>(|conn| {
            conn.execute(
                "INSERT INTO blobs (blob_id, mime_type, size_bytes, data, created_at) \
                 VALUES (?1, 'text/plain', ?2, ?3, '2026-09-11T00:00:00+00:00')",
                rusqlite::params![blob_id, payload.len() as i64, payload],
            )?;
            Ok(())
        })
        .unwrap();
}

fn insert_slot(
    store: &SqliteStore,
    tenant_id: &str,
    pipeline_id: &str,
    seq: i64,
    blob_id: Option<&str>,
) {
    store
        .with_conn::<_, StorageError>(|conn| {
            conn.execute(
                "INSERT INTO message_slots (tenant_id, pipeline_id, seq, message_id, blob_id, \
                 run_id, created_at) VALUES (?1, ?2, ?3, ?4, ?5, 'run-ret', '2026-09-11T00:00:00+00:00')",
                rusqlite::params![tenant_id, pipeline_id, seq, format!("m-{seq}"), blob_id],
            )?;
            Ok(())
        })
        .unwrap();
}

fn scalar(store: &SqliteStore, sql: &str) -> i64 {
    store
        .with_conn::<_, StorageError>(|conn| {
            conn.query_row(sql, [], |r| r.get::<_, i64>(0))
                .map_err(StorageError::from)
        })
        .unwrap()
}

fn surviving_trace_ids(store: &SqliteStore) -> Vec<String> {
    store
        .with_conn::<_, StorageError>(|conn| {
            let mut stmt = conn.prepare("SELECT trace_id FROM traces ORDER BY trace_id")?;
            let rows = stmt.query_map([], |r| r.get::<_, String>(0))?;
            let out = rows.collect::<Result<Vec<_>, _>>()?;
            Ok(out)
        })
        .unwrap()
}

/// 过期行消失、恰在 cutoff（边界，严格小于语义）与新鲜的行完好、返回计数
/// 正确、跨租户同清（保留是全局策略）、幂等。
#[tokio::test]
async fn purge_traces_older_than_keeps_boundary_and_fresh_drops_expired_across_tenants() {
    let store = SqliteStore::open_memory().unwrap();
    let cutoff = chrono::Utc::now();
    insert_trace(
        &store,
        "t-old-default",
        "2020-01-01T00:00:00+00:00",
        "default",
    );
    insert_trace(
        &store,
        "t-old-tenant-b",
        "2020-06-01T12:00:00+00:00",
        "tenant-b",
    );
    insert_trace(&store, "t-boundary", &cutoff.to_rfc3339(), "default");
    insert_trace(&store, "t-fresh", "2099-01-01T00:00:00+00:00", "default");

    let purged = store.purge_traces_older_than(cutoff).await.unwrap();

    assert_eq!(purged, 2, "返回计数 = 实删过期行数（跨租户）");
    assert_eq!(
        surviving_trace_ids(&store),
        vec!["t-boundary".to_string(), "t-fresh".to_string()],
        "幸存者 = 恰在 cutoff 的边界行 + 新鲜行（严格小于，边界不删）"
    );
    assert_eq!(
        store.purge_traces_older_than(cutoff).await.unwrap(),
        0,
        "窗口外已无行，二次清扫零删除（幂等）"
    );
    assert_eq!(scalar(&store, "SELECT COUNT(*) FROM traces"), 2);
}

/// 孤儿 blob 消失、被 message_slots 引用的 blob（跨租户）内容完好、NULL
/// blob_id 槽位不碍清扫、返回计数正确、幂等。
#[tokio::test]
async fn purge_orphan_blobs_drops_unreferenced_keeps_slot_referenced_across_tenants() {
    let store = SqliteStore::open_memory().unwrap();
    insert_blob(&store, "blob-ref-default", b"referenced-default");
    insert_blob(&store, "blob-ref-tenant-b", b"referenced-tenant-b");
    insert_blob(&store, "blob-orphan", b"orphan");
    insert_slot(&store, "default", "pipe-1", 0, Some("blob-ref-default"));
    insert_slot(&store, "tenant-b", "pipe-1", 0, Some("blob-ref-tenant-b"));
    // blob_id 为空的槽位是合法形态：不构成引用，也不构成清扫障碍
    insert_slot(&store, "default", "pipe-1", 1, None);

    let purged = store.purge_orphan_blobs().await.unwrap();

    assert_eq!(purged, 1, "返回计数 = 实删孤儿 blob 行数");
    assert_eq!(scalar(&store, "SELECT COUNT(*) FROM blobs"), 2);
    let kept: Vec<u8> = store
        .with_conn::<_, StorageError>(|conn| {
            conn.query_row(
                "SELECT data FROM blobs WHERE blob_id = 'blob-ref-default'",
                [],
                |r| r.get(0),
            )
            .map_err(StorageError::from)
        })
        .unwrap();
    assert_eq!(
        kept, b"referenced-default",
        "被引 blob 内容完好（读回对比）"
    );
    assert_eq!(
        store.purge_orphan_blobs().await.unwrap(),
        0,
        "孤儿已清空，二次清扫零删除（幂等）"
    );
}

/// 保留天数解析：未配置 = 默认 90；合法覆盖生效；0 = 禁用清扫；解析失败
/// 回落默认（家政不阻断启动）。
#[test]
fn trace_retention_days_env_override_disable_and_fallback() {
    let _guard = ENV_LOCK.lock().unwrap();
    std::env::remove_var("AGENTOS_TRACE_RETENTION_DAYS");
    assert_eq!(trace_retention_days(), 90, "未配置 = 默认 90 天");

    std::env::set_var("AGENTOS_TRACE_RETENTION_DAYS", "7");
    assert_eq!(trace_retention_days(), 7, "合法覆盖生效");

    std::env::set_var("AGENTOS_TRACE_RETENTION_DAYS", "0");
    assert_eq!(trace_retention_days(), 0, "0 = 禁用清扫");

    std::env::set_var("AGENTOS_TRACE_RETENTION_DAYS", "not-a-number");
    assert_eq!(trace_retention_days(), 90, "解析失败回落默认，不阻断启动");

    std::env::remove_var("AGENTOS_TRACE_RETENTION_DAYS");
}
