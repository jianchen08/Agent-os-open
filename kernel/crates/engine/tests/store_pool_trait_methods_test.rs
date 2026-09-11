// @feature: FP-0.2.〇 存储账本 | @vision: V3 可嵌入 | @ci: rust-test
//! 热路径六方法（get_run / set_run_pipeline / list_runs_by_pipeline / get_blob /
//! append_trace / update_run_status）经 blocking() 走专用 DB 线程池的回归。
//!
//! 行为契约在迁池前后不变（本文件断言读写语义本身）；重点守护跨池派发最容易
//! 丢失的 task_local 租户语义：池线程没有 task_local，租户必须由 async 包装在
//! 派发前于调用方 task 读取后随闭包带入——实现若把 current_or_default 挪进池内
//! 闭包，读写会全部落 default 租户，租户隔离测试即翻红。

use std::sync::Arc;

use agentos_core::traits::StorageBackend;
use agentos_core::types::{PatchType, RunStatus, StorageError, TenantContext, TraceEntry};
use agentos_engine::SqliteStore;

fn trace(run_id: &str, seq: u32) -> TraceEntry {
    TraceEntry {
        trace_id: format!("t-{seq}"),
        run_id: run_id.to_string(),
        branch_id: "main".to_string(),
        seq_in_branch: seq,
        plugin_id: "plugin_under_test".to_string(),
        patch_type: PatchType::StateUpdate,
        patch_data: serde_json::json!({ "k": seq }),
        created_at: "2026-09-11T00:00:00+00:00".to_string(),
    }
}

/// append_trace / get_run / update_run_status 的租户来自 task_local：
/// 经池派发后仍按调用方租户读写，租户隔离不因迁池失效。
#[tokio::test]
async fn trait_run_methods_carry_task_local_tenant_through_db_pool() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    store.create_run("run-a", "hash", "tenant-a").unwrap();

    agentos_tenant::scope(TenantContext::new("tenant-a", "s"), async {
        StorageBackend::append_trace(&*store, trace("run-a", 1))
            .await
            .unwrap();
        StorageBackend::update_run_status(
            &*store,
            "run-a",
            RunStatus::Completed,
            Some("main"),
            Some(1),
        )
        .await
        .unwrap();
        let run = StorageBackend::get_run(&*store, "run-a").await.unwrap();
        assert_eq!(run.tenant_id, "tenant-a");
        assert!(
            matches!(run.status, RunStatus::Completed),
            "终态写入必须生效: {:?}",
            run.status
        );
        assert_eq!(run.current_seq, 1);
    })
    .await;

    // 其他租户读同一 run_id：隔离仍生效
    agentos_tenant::scope(TenantContext::new("tenant-b", "s"), async {
        assert!(
            StorageBackend::get_run(&*store, "run-a").await.is_err(),
            "tenant-b 不得读到 tenant-a 的 run"
        );
    })
    .await;

    // 轨迹租户归属：必须落在调用方 task_local 的租户（default 租户有残留 = 实现在池内取租户）
    let (count_a, count_b, count_default): (i64, i64, i64) = store
        .with_conn(|c| -> Result<(i64, i64, i64), rusqlite::Error> {
            c.query_row(
                "SELECT \
                     (SELECT COUNT(*) FROM traces WHERE tenant_id = 'tenant-a'), \
                     (SELECT COUNT(*) FROM traces WHERE tenant_id = 'tenant-b'), \
                     (SELECT COUNT(*) FROM traces WHERE tenant_id = 'default')",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
        })
        .unwrap();
    assert_eq!(count_a, 1, "轨迹必须按调用方租户落库");
    assert_eq!(count_b, 0);
    assert_eq!(
        count_default, 0,
        "task_local 租户在场时不得落 default 租户（池线程读不到 task_local 的症状）"
    );
}

/// 其余三个池包装方法（set_run_pipeline / list_runs_by_pipeline / get_blob）
/// 迁池后读写语义不变。
#[tokio::test]
async fn trait_pool_wrappers_preserve_semantics() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    StorageBackend::create_run(&*store, "run-p", "hash", "tenant-p")
        .await
        .unwrap();
    StorageBackend::set_run_pipeline(&*store, "run-p", "pipe-1")
        .await
        .unwrap();

    let runs = StorageBackend::list_runs_by_pipeline(&*store, "pipe-1", "tenant-p")
        .await
        .unwrap();
    assert_eq!(runs.len(), 1, "按管道反查 run 必须命中");
    assert_eq!(runs[0].run_id, "run-p");

    let missing = StorageBackend::list_runs_by_pipeline(&*store, "pipe-none", "tenant-p")
        .await
        .unwrap();
    assert!(missing.is_empty(), "无 run 的管道必须返回空列表");

    let blob_id = StorageBackend::store_blob(&*store, b"payload", "text/plain")
        .await
        .unwrap();
    assert_eq!(
        StorageBackend::get_blob(&*store, &blob_id).await.unwrap(),
        b"payload".to_vec(),
        "blob 回读必须与写入一致"
    );
    assert!(
        matches!(
            StorageBackend::get_blob(&*store, "no-such-blob").await,
            Err(StorageError::NotFound(_))
        ),
        "缺失 blob 必须报 NotFound"
    );
}
