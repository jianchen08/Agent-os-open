// @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: rust-test
//! B2 修复验证：启动时清扫孤儿运行（state 唯一真值版）。
//!
//! 进程崩溃会留下 `run_status='running'` 的运行投影永远卡住（收束投影未执行）。
//! `reap_orphan_runs` 在内核启动时把所有 `running` 运行标记为 `failed` + 补
//! `run_ended_at`，让历史/会话状态不悬空。已结束（completed/failed/suspended）
//! 的运行投影不受影响。

use agentos_core::traits::StorageBackend;
use agentos_core::types::{RunStatus, TenantContext};
use agentos_engine::SqliteStore;

fn state_fields(store: &SqliteStore, pipeline_id: &str, tenant_id: &str) -> serde_json::Value {
    serde_json::to_value(store.load_pipeline_state(pipeline_id, tenant_id).unwrap())
        .expect("state 序列化不应失败")
}

#[tokio::test]
async fn reap_marks_orphan_running_as_failed_leaves_others() {
    let store = SqliteStore::open_memory().unwrap();
    // 两个运行，record_run_start 落 run_status='running'
    store
        .record_run_start("pipe_orphan", "default", "r_orphan", "h")
        .unwrap();
    store
        .record_run_start("pipe_done", "default", "r_done", "h")
        .unwrap();
    // pipe_done 正常结束 → completed（终态补 run_ended_at）
    store
        .set_run_status_projection("pipe_done", "default", RunStatus::Completed)
        .unwrap();

    let reaped = store.reap_orphan_runs().expect("reap 应成功");
    assert_eq!(reaped, 1, "只应清扫 1 个 running 孤儿");

    let orphan = StorageBackend::get_run(&store, "r_orphan")
        .await
        .expect("get_run 应成功");
    assert_eq!(orphan.status, RunStatus::Failed, "孤儿 run 应被标记 failed");
    assert!(orphan.ended_at.is_some(), "应补 ended_at");

    let done = StorageBackend::get_run(&store, "r_done")
        .await
        .expect("get_run 应成功");
    assert_eq!(done.status, RunStatus::Completed, "已完成的 run 不应被动");
}

#[tokio::test]
async fn reap_is_idempotent() {
    // 重复清扫：第二次无 running run，返回 0。
    let store = SqliteStore::open_memory().unwrap();
    store
        .record_run_start("pipe_1", "default", "r1", "h")
        .unwrap();
    assert_eq!(store.reap_orphan_runs().unwrap(), 1);
    assert_eq!(store.reap_orphan_runs().unwrap(), 0);
    assert_eq!(store.reap_orphan_runs().unwrap(), 0);
}

#[tokio::test]
async fn reap_marks_failed_adds_run_ended_at_keeps_task_fields() {
    // 清扫只动内核持有的运行键：run_status → failed、补 run_ended_at；
    // 任务域字段（task.status 等）不归清扫管，保持原值。
    let store = SqliteStore::open_memory().unwrap();
    store
        .record_run_start("pipe_ghost", "default", "r_ghost", "h")
        .unwrap();
    // 出生投影只有任务域字段（内核不仲裁任务语义，清扫不得触碰）
    store
        .upsert_state_field(
            "pipe_ghost",
            "default",
            "task.status",
            &serde_json::json!("running"),
        )
        .unwrap();

    assert_eq!(store.reap_orphan_runs().unwrap(), 1);

    let fields = state_fields(&store, "pipe_ghost", "default");
    assert_eq!(
        fields.get("run_status").and_then(|v| v.as_str()),
        Some("failed"),
        "reap 应把内核持有的 run_status 投影翻为 failed"
    );
    assert!(
        fields
            .get("run_ended_at")
            .and_then(|v| v.as_str())
            .is_some_and(|s| !s.is_empty()),
        "reap 应补非空 run_ended_at"
    );
    assert_eq!(
        fields.get("task.status").and_then(|v| v.as_str()),
        Some("running"),
        "任务域字段不归清扫管，保持原值"
    );
}

#[tokio::test]
async fn reap_overwrites_stale_running_and_keeps_terminal_projection() {
    // 写面保守规则：表行 run_status 残留 running（轮中派发的过期值，进程死后
    // 永远是谎）→ 改 failed；已有终态（更早正常收束的真值）不覆盖——最新
    // run 状态的纠偏归读面 overlay（按 state 运行键权威状态 fill-if-absent）。
    let store = SqliteStore::open_memory().unwrap();
    store
        .record_run_start("pipe_stale", "default", "r_a", "h")
        .unwrap();
    store
        .set_run_status_projection("pipe_terminal", "default", RunStatus::Completed)
        .unwrap();
    let terminal_ended_before = state_fields(&store, "pipe_terminal", "default")
        .get("run_ended_at")
        .and_then(|v| v.as_str().map(String::from))
        .expect("正常收束的终态投影必须已带 run_ended_at");

    store.reap_orphan_runs().unwrap();

    let stale = state_fields(&store, "pipe_stale", "default");
    assert_eq!(
        stale.get("run_status").and_then(|v| v.as_str()),
        Some("failed"),
        "残留 running 的过期投影应被清扫为 failed"
    );
    assert!(
        stale
            .get("run_ended_at")
            .and_then(|v| v.as_str())
            .is_some_and(|s| !s.is_empty()),
        "被清扫的管道必须补 run_ended_at"
    );
    let terminal = state_fields(&store, "pipe_terminal", "default");
    assert_eq!(
        terminal.get("run_status").and_then(|v| v.as_str()),
        Some("completed"),
        "已有终态投影不被写面覆盖"
    );
    assert_eq!(
        terminal.get("run_ended_at").and_then(|v| v.as_str()),
        Some(terminal_ended_before.as_str()),
        "终态投影的 run_ended_at 不被清扫改写"
    );
}

#[tokio::test]
async fn reap_covers_all_tenants_not_only_default() {
    // 清扫是崩溃家政，不属于任何租户的数据边界：注册用户一用户一租户，
    // 按租户过滤会让非 default 租户的孤儿 run 永远卡 running（历史/会话
    // 状态悬空的危害对全部租户成立）。
    let store = SqliteStore::open_memory().unwrap();
    let setups = [
        ("pipe_default", "default", "r_default"),
        ("pipe_u123", "u-123", "r_u123"),
        ("pipe_u456", "u-456", "r_u456"),
    ];
    for (pipeline_id, tenant, run_id) in setups {
        store
            .record_run_start(pipeline_id, tenant, run_id, "h")
            .unwrap();
    }

    let reaped = store.reap_orphan_runs().expect("reap 应成功");
    assert_eq!(reaped, 3, "全租户孤儿 run 都应被清扫");

    for (pipeline_id, tenant, run_id) in setups {
        let run = agentos_tenant::scope(
            TenantContext::new(tenant, "th_reap_all"),
            StorageBackend::get_run(&store, run_id),
        )
        .await
        .expect("get_run 应成功");
        assert_eq!(
            run.status,
            RunStatus::Failed,
            "{pipeline_id} 应被标记 failed"
        );
        assert!(run.ended_at.is_some(), "{pipeline_id} 应补 ended_at");
    }
}
