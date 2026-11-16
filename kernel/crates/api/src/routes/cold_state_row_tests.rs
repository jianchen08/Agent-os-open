//! cold_state_row 对外契约锁（Should#7 重构后防漂移）：checkpoint 基线 +
//! pipeline_state 表行覆盖的合并顺序、表行-only 基线、双空真孤儿不出口。
//! 两类 DB 故障路径维持返回 None 不变（调用方跳行），仅新增 warn 留痕。

use super::*;
use agentos_core::traits::StorageBackend;
use std::sync::Arc;

fn sqlite() -> Arc<agentos_engine::SqliteStore> {
    Arc::new(agentos_engine::SqliteStore::open_memory().unwrap())
}

#[tokio::test]
async fn table_fields_override_stale_checkpoint_scalars() {
    let store = sqlite();
    let dyn_store: Arc<dyn StorageBackend> = store.clone();
    let ckpt = serde_json::json!({ "task.status": "pending", "agent.id": "agentos" });
    StorageBackend::save_checkpoint(dyn_store.as_ref(), "pipe-cold", "default", 3, &ckpt)
        .await
        .unwrap();
    // 出生字段已过期：表行最新值必须覆盖（completed 赢过 pending）
    StorageBackend::upsert_state_field(
        dyn_store.as_ref(),
        "pipe-cold",
        "default",
        "task.status",
        &serde_json::json!("completed"),
    )
    .await
    .unwrap();

    let row = cold_state_row(&dyn_store, "pipe-cold", "default")
        .await
        .expect("有持久痕迹应出口");
    assert_eq!(
        row["task.status"], "completed",
        "表行最新值覆盖 checkpoint 过期值"
    );
    assert_eq!(
        row["agent.id"], "agentos",
        "checkpoint 独有字段保留（表未写不覆盖）"
    );
}

#[tokio::test]
async fn fields_only_without_checkpoint_still_emits_row() {
    // running 中任务 interval 未到无 checkpoint：只靠表行基线可见
    let store = sqlite();
    let dyn_store: Arc<dyn StorageBackend> = store.clone();
    StorageBackend::upsert_state_field(
        dyn_store.as_ref(),
        "pipe-cold-b",
        "default",
        "title",
        &serde_json::json!("刚提交的任务"),
    )
    .await
    .unwrap();

    let row = cold_state_row(&dyn_store, "pipe-cold-b", "default")
        .await
        .expect("表行单独在场即出口");
    assert_eq!(row["title"], "刚提交的任务");
}

#[tokio::test]
async fn orphan_without_any_persisted_trace_returns_none() {
    let dyn_store: Arc<dyn StorageBackend> = sqlite();
    assert!(
        cold_state_row(&dyn_store, "pipe-orphan", "default")
            .await
            .is_none(),
        "checkpoint 与表行双空 = 真孤儿不出口"
    );
}

#[tokio::test]
async fn run_status_projection_missing_stays_missing() {
    // runs 退役收尾（ADR 2026-10-08）：读面不补写不伪造——运行簿记投影缺失
    // （崩溃/reap 形态）时冷行如实缺 run_status 键，谁终态谁随写（旧 runs
    // 表权威 overlay 的 fill-if-absent 已废）。
    let store = sqlite();
    let dyn_store: Arc<dyn StorageBackend> = store.clone();
    StorageBackend::upsert_state_field(
        dyn_store.as_ref(),
        "pipe-cold-rs",
        "default",
        "task.status",
        &serde_json::json!("running"),
    )
    .await
    .unwrap();

    let row = cold_state_row(&dyn_store, "pipe-cold-rs", "default")
        .await
        .expect("有持久痕迹应出口");
    assert!(row.get("run_status").is_none(), "投影缺失不得伪造运行状态");
}

#[tokio::test]
async fn run_status_projection_passes_through_as_written() {
    // 表行 run_status 簿记键 = 投影真值，冷行原样出口（同组输入反差对照：
    // 在场即如实带出，缺失即如实缺键）。
    let store = sqlite();
    let dyn_store: Arc<dyn StorageBackend> = store.clone();
    StorageBackend::upsert_state_field(
        dyn_store.as_ref(),
        "pipe-cold-rs2",
        "default",
        "run_status",
        &serde_json::json!("completed"),
    )
    .await
    .unwrap();

    let row = cold_state_row(&dyn_store, "pipe-cold-rs2", "default")
        .await
        .expect("有持久痕迹应出口");
    assert_eq!(
        row["run_status"], "completed",
        "投影在场的 run_status 原样出口"
    );
}
