// @feature: FP-0.2.一 插件协议 | @ci: rust-test
// 由 capability_router.rs 的主 #[cfg(test)] 测试块体平移而来（保留私有项访问）。

use super::*;
use agentos_core::traits::MessageQueryOpts;
use agentos_core::types::TraceEntry;
use serde_json::json;

/// PipelineResumerFn 测试 sink 收集的恢复调用记录：
/// (pipeline_id, thread_id, user_id, state_overlay)。
type DispatchedResumes = std::sync::Mutex<Vec<(String, String, String, Option<serde_json::Value>)>>;

fn router_with_metrics() -> (KernelCapabilityRouter, MetricsAggregator) {
    let agg = MetricsAggregator::new();
    // pipeline-state.list 摘要测试预接任务域出口声明（声明收集本身在
    // routes::state_summary_tests 覆盖；此处聚焦 list 行为）。
    let r = KernelCapabilityRouter::with_metrics(agg.clone()).with_export_fields_lookup(Arc::new(
        || crate::capability_router::ExportFields::from_manifests(&[test_task_export_manifest()]),
    ));
    (r, agg)
}

/// 任务域出口声明的测试 manifest（task.*/lineage.*/task.owned.*/workspace 等）。
fn test_task_export_manifest() -> agentos_core::traits::PluginManifest {
    agentos_core::traits::PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: "task_service".to_string(),
        name: "task_service".to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: agentos_core::traits::PluginType::System,
        language: "python".to_string(),
        host_type: agentos_core::traits::HostType::Sidecar,
        host_group: None,
        entry: "python server.py".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        restricted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        aux_venvs: Vec::new(),
        export_fields: [
            "task.goal",
            "task.status",
            "task.id",
            "task.ended_at",
            "task.submitted_by",
            "task.owned.*",
            "lineage.parent_pipeline_id",
            "lineage.origin_session_id",
            "lineage.root",
            "workspace",
            "ws_meta",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect(),
        provides: None,
        visibility: Default::default(),
    }
}

#[tokio::test]
async fn test_metrics_record_counter() {
    let (router, agg) = router_with_metrics();
    let params = json!({
        "_plugin_id": "llm_service",
        "name": "tokens_used",
        "value": 1280,
        "metric_type": "counter",
        "labels": {"model": "deepseek"},
        "unit": "tokens",
        "help": "Total tokens used"
    });
    let res = router.handle("metrics", "record", params).await.unwrap();
    assert_eq!(res["status"], "recorded");
    assert_eq!(res["plugin_id"], "llm_service");

    let views = agg.query(
        Some("llm_service"),
        Some("tokens_used"),
        None,
        &Labels::new(),
    );
    assert_eq!(views.len(), 1);
    assert_eq!(views[0].latest, Some(1280.0));
    assert_eq!(views[0].unit.as_deref(), Some("tokens"));
    // labels 透传
    assert_eq!(views[0].labels.get("model").unwrap(), "deepseek");
}

#[tokio::test]
async fn test_metrics_record_accumulates_counter() {
    let (router, agg) = router_with_metrics();
    for _ in 0..3 {
        router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id":"p1","name":"calls","value":10,"metric_type":"counter"}),
            )
            .await
            .unwrap();
    }
    let views = agg.query(Some("p1"), Some("calls"), None, &Labels::new());
    // 3 次 ×10 = 30（counter 累加）
    assert_eq!(views[0].latest, Some(30.0));
}

#[tokio::test]
async fn test_metrics_record_gauge_overwrites() {
    let (router, agg) = router_with_metrics();
    router
        .handle(
            "metrics",
            "record",
            json!({"_plugin_id":"p1","name":"conn","value":10,"metric_type":"gauge"}),
        )
        .await
        .unwrap();
    router
        .handle(
            "metrics",
            "record",
            json!({"_plugin_id":"p1","name":"conn","value":7,"metric_type":"gauge"}),
        )
        .await
        .unwrap();
    let views = agg.query(Some("p1"), Some("conn"), None, &Labels::new());
    // gauge 同桶 avg：(10+7)/2 = 8.5
    assert!((views[0].latest.unwrap() - 8.5).abs() < 0.01);
}

#[tokio::test]
async fn test_metrics_record_histogram() {
    let (router, agg) = router_with_metrics();
    router
        .handle(
            "metrics",
            "record",
            json!({"_plugin_id":"p1","name":"lat","value":0.02,"metric_type":"histogram"}),
        )
        .await
        .unwrap();
    let views = agg.query(Some("p1"), Some("lat"), None, &Labels::new());
    let h = views[0].histogram.as_ref().unwrap();
    assert_eq!(h.count, 1);
}

#[tokio::test]
async fn test_metrics_record_rejects_too_many_labels() {
    let (router, _agg) = router_with_metrics();
    let mut labels = serde_json::Map::new();
    for i in 0..21 {
        labels.insert(format!("k{i}"), json!(i.to_string()));
    }
    let res = router
        .handle(
            "metrics",
            "record",
            json!({"_plugin_id":"p1","name":"m","value":1.0,"labels":labels}),
        )
        .await;
    assert!(res.is_err());
}

#[tokio::test]
async fn test_metrics_record_rejects_newline_in_label() {
    let (router, _agg) = router_with_metrics();
    let res = router
        .handle(
            "metrics",
            "record",
            json!({"_plugin_id":"p1","name":"m","value":1.0,
                       "labels":{"k":"a\nb"}}),
        )
        .await;
    assert!(res.is_err(), "newline in label value must be rejected");
}

#[tokio::test]
async fn test_metrics_record_unknown_type() {
    let (router, _agg) = router_with_metrics();
    let res = router
        .handle(
            "metrics",
            "record",
            json!({"_plugin_id":"p1","name":"m","value":1.0,"metric_type":"bogus"}),
        )
        .await;
    assert!(res.is_err());
}

#[tokio::test]
async fn test_metrics_record_missing_name() {
    let (router, _agg) = router_with_metrics();
    let res = router
        .handle("metrics", "record", json!({"_plugin_id":"p1","value":1.0}))
        .await;
    assert!(res.is_err());
}

mod streaming_events;
mod tool_invoke;

use self::streaming_events::{router_with_session, router_with_streaming_gate, CaptureSink};
use self::tool_invoke::{CaptureInvoker, DomainSink};

#[tokio::test]
async fn emit_domain_broadcasts_to_domain_broadcaster() {
    let got: DomainSink = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink = got.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_domain_broadcaster(Arc::new(
            move |name: &str, tags: Vec<(String, serde_json::Value)>| {
                sink.lock().unwrap().push((name.to_string(), tags));
            },
        ));
    let res = router
        .handle(
            "event-bus",
            "emit_domain",
            json!({"event": "task_completed",
                   "tags": {"pipeline_id": "p1", "task_id": "t1", "user_id": "u1"}}),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "emitted");
    let captured = got.lock().unwrap();
    assert_eq!(captured.len(), 1);
    assert_eq!(captured[0].0, "task_completed");
    let tag = |k: &str| {
        captured[0]
            .1
            .iter()
            .find(|(tk, _)| tk == k)
            .map(|(_, v)| v.clone())
            .unwrap_or(serde_json::Value::Null)
    };
    assert_eq!(tag("pipeline_id"), json!("p1"));
    assert_eq!(tag("task_id"), json!("t1"));
}

#[tokio::test]
async fn emit_domain_without_broadcaster_fails_closed_and_missing_event_rejected() {
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new());
    let err = router
        .handle("event-bus", "emit_domain", json!({"event": "x"}))
        .await
        .expect_err("broadcaster 未装配须显式失败");
    assert!(err.to_string().contains("domain_broadcaster"), "{err}");
    let err = router
        .handle("event-bus", "emit_domain", json!({"tags": {}}))
        .await
        .expect_err("缺 event 须拒绝");
    assert!(err.to_string().contains("event"), "{err}");
}

/// pipeline-state.update 声明制测试调用方（manifest state.writes 等价声明）。
const TEST_STATE_WRITER: &str = "test_state_writer";

/// 测试写面声明：task.* 与触发器注册表（含 legacy 前缀）——模拟任务域
/// 写者插件的 manifest `state.writes`。其他 plugin_id = 未声明（应拒绝）。
fn test_state_writes_lookup() -> crate::capability_router::StateWritesLookupFn {
    Arc::new(|plugin_id: &str| {
        if plugin_id == TEST_STATE_WRITER {
            vec![
                "task.*".to_string(),
                "trigger.registry.*".to_string(),
                "task.trigger.registry.*".to_string(),
                "approval.*".to_string(),
            ]
        } else {
            Vec::new()
        }
    })
}

fn router_with_store() -> KernelCapabilityRouter {
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().expect("open_memory"));
    KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store)
        .with_export_fields_lookup(Arc::new(|| {
            crate::capability_router::ExportFields::from_manifests(&[test_task_export_manifest()])
        }))
        .with_state_writes_lookup(test_state_writes_lookup())
} // ── GAP-2：pipeline-state 域（CONDITION 触发器的求值上下文源） ──────

#[tokio::test]
async fn test_pipeline_state_lists_registry_rows_with_task_fields() {
    // 唯一租户隔离（global registry 进程级共享，避免污染其它测试）
    let tenant = format!("tenant_gap2_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_gap2_{}", uuid::Uuid::new_v4().simple());
    let other_pid = format!("pipe_other_{}", uuid::Uuid::new_v4().simple());
    let reg = agentos_session::pipeline_state_registry::global_registry();
    reg.get_or_init(
        &tenant,
        &pid,
        "th_gap2",
        "agentos",
        json!({
            "pipeline_id": pid,
            "status": "completed",
            "task.id": "t42",
            "task.goal": "喝水提醒",
            "task.status": "completed",
            "lineage.parent_pipeline_id": "pipe_parent",
            "lineage.origin_session_id": "sess_root",
            "messages": [{"role": "user"}, {"role": "assistant"}],
        }),
    );
    // 不同租户的管道：不得泄漏
    reg.get_or_init(
        "tenant_gap2_alien",
        &other_pid,
        "th_alien",
        "agentos",
        json!({"pipeline_id": other_pid, "status": "running"}),
    );

    let router = router_with_store();
    let rows = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_gap2"),
        router.handle("pipeline-state", "list", json!({})),
    )
    .await
    .unwrap();

    let arr = rows.as_array().expect("返回应为行数组");
    let row = arr
        .iter()
        .find(|r| r.get("pipeline_id").and_then(|v| v.as_str()) == Some(pid.as_str()))
        .expect("本租户管道行应存在");
    // task.*/lineage.* 扁平键出口（条件表达式 task.status == 'completed' 可求值）
    assert_eq!(row["task.id"], "t42");
    assert_eq!(row["task.status"], "completed");
    assert_eq!(row["task.goal"], "喝水提醒");
    assert_eq!(row["lineage.parent_pipeline_id"], "pipe_parent");
    assert_eq!(row["lineage.origin_session_id"], "sess_root");
    assert_eq!(row["source"], "memory");
    assert_eq!(row["thread_id"], "th_gap2");
    // messages 不出口（只给条数）——与 /api/v1/pipelines/state 同契约
    assert!(row.get("messages").is_none());
    assert_eq!(row["message_count"], 2);
    // 租户隔离性质：异租户管道不出现在结果里
    assert!(
        !arr.iter()
            .any(|r| { r.get("pipeline_id").and_then(|v| v.as_str()) == Some(other_pid.as_str()) }),
        "异租户管道不得泄漏"
    );
}

#[tokio::test]
async fn test_reverse_call_tenant_passthrough_scopes_kernel_read_faces() {
    // 刀1 acceptance（maintenance 通道实测口径）：sidecar 反调经
    // PluginScopedRouter（生产装配形态，host_acquire 同构）→ 内核
    // pipeline-state.list 读面——params 携带 `_tenant_id: maintenance` 时读面
    // 按 maintenance 解析（任务详情 404 的同根机制）；无 `_tenant_id` 时按
    // default 回退（maintenance 行不得泄漏）。
    let tenant = format!("maintenance_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_mtn_{}", uuid::Uuid::new_v4().simple());
    let reg = agentos_session::pipeline_state_registry::global_registry();
    reg.get_or_init(
        &tenant,
        &pid,
        "th_mtn",
        "agentos",
        json!({
            "pipeline_id": pid,
            "task.id": "task-mtn-1",
            "task.status": "running",
        }),
    );

    // 生产组合：PluginScopedRouter 包共享内核路由器（身份=插件 id）。
    let kernel = Arc::new(router_with_store());
    let scoped = agentos_invoker::PluginScopedRouter::new(
        "task_evaluate",
        kernel as Arc<dyn CapabilityRouter>,
    );

    // ① 反调带 `_tenant_id: maintenance` → maintenance 行可见（404 根因消除）
    let rows = scoped
        .handle(
            "pipeline-state",
            "list",
            json!({"_tenant_id": tenant, "_session_id": "th_mtn"}),
        )
        .await
        .unwrap();
    let arr = rows.as_array().expect("返回应为行数组");
    let row = arr
        .iter()
        .find(|r| r.get("pipeline_id").and_then(|v| v.as_str()) == Some(pid.as_str()))
        .expect("maintenance 反调必须看到本租户任务行（详情 404 同根）");
    assert_eq!(row["task.id"], "task-mtn-1");

    // ② 同一反调无 `_tenant_id` → default 回退语义：maintenance 行不可见
    let rows = scoped
        .handle("pipeline-state", "list", json!({}))
        .await
        .unwrap();
    let arr = rows.as_array().expect("返回应为行数组");
    assert!(
        !arr.iter()
            .any(|r| r.get("pipeline_id").and_then(|v| v.as_str()) == Some(pid.as_str())),
        "无租户反调按 default 回退，maintenance 行不得泄漏"
    );
}

#[tokio::test]
async fn test_pipeline_state_cold_rows_carry_latest_run_status() {
    // 冷兜底行（checkpoint + 表行合并）原样携带表行 run_status 簿记键——投影
    // 即真值（runs 退役收尾 ADR 2026-10-08：读面不补写不伪造，旧 runs 表权威
    // overlay 已废）。
    let tenant = format!("tenant_cold_rs_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_cold_rs_{}", uuid::Uuid::new_v4().simple());
    let run_id = format!("run_cold_rs_{}", uuid::Uuid::new_v4().simple());

    let store: std::sync::Arc<dyn agentos_core::traits::StorageBackend> =
        std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().expect("open_memory"));
    // trait 写面按 task_local 租户路由：与生产一致， 种子在租户作用域内完成
    agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_cold_rs"),
        async {
            store
                .record_run_start(&pid, &tenant, &run_id, "h", "2026-10-15T00:00:00+00:00")
                .await
                .unwrap();
            store
                .upsert_state_fields(
                    &pid,
                    &tenant,
                    &json!({ "run_status": "cancelled" })
                        .as_object()
                        .unwrap()
                        .clone(),
                )
                .await
                .unwrap();
            // 出生投影只有任务域字段：run_status 由簿记键单独带出
            store
                .upsert_state_field(&pid, &tenant, "task.status", &serde_json::json!("running"))
                .await
                .unwrap();
        },
    )
    .await;

    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_export_fields_lookup(Arc::new(|| {
            crate::capability_router::ExportFields::from_manifests(&[test_task_export_manifest()])
        }));
    let rows = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_cold_rs"),
        router.handle("pipeline-state", "list", json!({})),
    )
    .await
    .unwrap();

    let arr = rows.as_array().expect("返回应为行数组");
    let row = arr
        .iter()
        .find(|r| r.get("pipeline_id").and_then(|v| v.as_str()) == Some(pid.as_str()))
        .expect("冷兜底行应出口");
    assert_eq!(row["source"], "checkpoint");
    assert_eq!(
        row["run_status"], "cancelled",
        "冷行应原样携带表行 run_status 簿记键"
    );
    assert_eq!(row["task.status"], "running", "任务域字段保持原值");
}

#[tokio::test]
async fn test_pipeline_state_cold_rows_missing_run_status_stays_missing() {
    // 运行簿记投影缺失（崩溃/reap 形态：有出生投影无 run_status 键）→ 冷行
    // 如实缺 run_status——读面不补写不伪造（谁终态谁随写，runs 退役收尾
    // ADR 2026-10-08，旧 overlay 会把缺键 fabrication 成 running）。
    let tenant = format!("tenant_cold_rsx_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_cold_rsx_{}", uuid::Uuid::new_v4().simple());

    let store: std::sync::Arc<dyn agentos_core::traits::StorageBackend> =
        std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().expect("open_memory"));
    agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_cold_rsx"),
        async {
            // 出生投影只有任务域字段（无 record_run_start：run_status 缺失形态）
            store
                .upsert_state_field(&pid, &tenant, "task.status", &serde_json::json!("running"))
                .await
                .unwrap();
        },
    )
    .await;

    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_export_fields_lookup(Arc::new(|| {
            crate::capability_router::ExportFields::from_manifests(&[test_task_export_manifest()])
        }));
    let rows = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_cold_rsx"),
        router.handle("pipeline-state", "list", json!({})),
    )
    .await
    .unwrap();

    let arr = rows.as_array().expect("返回应为行数组");
    let row = arr
        .iter()
        .find(|r| r.get("pipeline_id").and_then(|v| v.as_str()) == Some(pid.as_str()))
        .expect("表行单独在场即出口");
    assert_eq!(row["source"], "checkpoint");
    assert!(
        row.get("run_status").is_none(),
        "簿记投影缺失不得伪造运行状态"
    );
    assert_eq!(row["task.status"], "running", "任务域字段保持原值");
}

#[tokio::test]
async fn test_pipeline_state_rows_are_flat_for_condition_eval() {
    // 性质断言：行结构是「顶层扁平点号键」（非嵌套 state 子对象）——
    // 插件侧 condition_parser（扁平键优先）直接以行为求值上下文。
    let tenant = format!("tenant_gap2b_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_gap2b_{}", uuid::Uuid::new_v4().simple());
    let reg = agentos_session::pipeline_state_registry::global_registry();
    reg.get_or_init(
        &tenant,
        &pid,
        "th",
        "agentos",
        json!({"pipeline_id": pid, "task.status": "failed"}),
    );

    let router = router_with_store();
    let rows = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th"),
        router.handle("pipeline-state", "list", json!({})),
    )
    .await
    .unwrap();
    let row = rows
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r.get("pipeline_id").and_then(|v| v.as_str()) == Some(pid.as_str()))
        .unwrap();
    assert!(
        row.get("task.status").is_some() && row.get("state").is_none(),
        "state 字段应提升到行顶层（扁平键），不嵌套在 state 子对象里"
    );
    assert_eq!(row["task.status"], "failed");
}

#[tokio::test]
async fn test_pipeline_state_list_state_rows_without_checkpoint() {
    // 任务归属链语义：running 中任务 interval 未到不会有 checkpoint，
    // cold_state_row 返回 None 时 pipeline_state 表行（出生字段创建即落表）
    // 可独立兜底——整行不出口会让任务面板看不到刚提交的任务。
    // task.owned.<id>.* 前缀键也须出口（提交者管道的任务登记）。
    let tenant = format!("tenant_nockpt_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_nockpt_{}", uuid::Uuid::new_v4().simple());
    let parent_pid = format!("pipe_parent_{}", uuid::Uuid::new_v4().simple());
    let router = router_with_store();
    let store = router
        .store
        .as_ref()
        .expect("router_with_store 已注 store")
        .clone();
    // 任务执行管道：无 checkpoint，只有出生字段 + track 行（表行独立兜底）
    store
        .upsert_state_field(&pid, &tenant, "task.id", &json!(pid))
        .await
        .unwrap();
    store
        .upsert_state_field(&pid, &tenant, "task.goal", &json!("AI行业近月发展调研"))
        .await
        .unwrap();
    store
        .upsert_state_field(&pid, &tenant, "task.status", &json!("running"))
        .await
        .unwrap();
    store
        .upsert_state_field(
            &pid,
            &tenant,
            "lineage.parent_pipeline_id",
            &json!(parent_pid),
        )
        .await
        .unwrap();
    // 提交者管道：task.owned 登记键（前缀出口）
    store
        .upsert_state_field(
            &parent_pid,
            &tenant,
            &format!("task.owned.{pid}.title"),
            &json!("AI行业近月发展调研"),
        )
        .await
        .unwrap();
    store
        .upsert_state_field(
            &parent_pid,
            &tenant,
            &format!("task.owned.{pid}.status"),
            &json!("running"),
        )
        .await
        .unwrap();

    let rows = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_nockpt"),
        router.handle("pipeline-state", "list", json!({})),
    )
    .await
    .unwrap();
    let arr = rows.as_array().expect("返回应为行数组");
    let row = arr
        .iter()
        .find(|r| r.get("pipeline_id").and_then(|v| v.as_str()) == Some(pid.as_str()))
        .unwrap_or_else(|| {
            panic!("无 checkpoint 的表行管道应出口（整行丢弃 = 刚提交任务不可见）; rows={arr:?}")
        });
    assert_eq!(row["task.goal"], "AI行业近月发展调研");
    assert_eq!(row["task.status"], "running");
    assert_eq!(row["lineage.parent_pipeline_id"], parent_pid);
    let parent_row = arr
        .iter()
        .find(|r| r.get("pipeline_id").and_then(|v| v.as_str()) == Some(parent_pid.as_str()))
        .unwrap_or_else(|| panic!("提交者管道行应出口; rows={arr:?}"));
    assert_eq!(
        parent_row[&format!("task.owned.{pid}.title")],
        "AI行业近月发展调研",
        "task.owned.* 前缀键必须出口（被白名单裁掉 = 登记任务整行不可见）"
    );
}

#[tokio::test]
async fn test_pipeline_state_list_db_fallback_overlays_completed_status() {
    // 冷管道兜底（registry 未命中 = 重启后未再轮）：pipeline-state.list 必须从
    // DB 补回。checkpoint 拍在终态回写前（task.status=pending），pipeline_state
    // 表才是最新真值（completed）。修复前冷任务查询返回空列表 → task_manage
    // "任务不存在"；修复后须返回 pipeline_state 表覆盖后的 completed。
    let tenant = format!("tenant_flbk_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_flbk_{}", uuid::Uuid::new_v4().simple());
    let router = router_with_store();
    // 内存 registry 不注册该管道（模拟重启后内存丢失）
    let store = router
        .store
        .as_ref()
        .expect("router_with_store 已注 store")
        .clone();
    store
        .save_checkpoint(
            &pid,
            &tenant,
            9,
            &json!({
                "pipeline_id": pid,
                "status": "active",
                "ended": true,
                "current_phase": "exit",
                "task.id": pid,
                "task.goal": "写 hello.txt 并自动评估",
                "task.status": "pending",
                "track.total_tokens": 6595,
            }),
        )
        .await
        .unwrap();
    // pipeline_state 表 = 终态回写后的最新真值
    store
        .upsert_state_field(&pid, &tenant, "task.status", &json!("completed"))
        .await
        .unwrap();
    store
        .upsert_state_field(
            &pid,
            &tenant,
            "task.ended_at",
            &json!("2026-08-18T00:40:16Z"),
        )
        .await
        .unwrap();

    let rows = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_flbk"),
        router.handle("pipeline-state", "list", json!({})),
    )
    .await
    .unwrap();
    let arr = rows.as_array().expect("返回应为行数组");
    let row = arr
        .iter()
        .find(|r| r.get("pipeline_id").and_then(|v| v.as_str()) == Some(pid.as_str()))
        .unwrap_or_else(|| {
            panic!("冷管道兜底行应存在（registry 丢失后查询不到 = bug）; rows={arr:?}")
        });
    // pipeline_state 表最新值必须覆盖 checkpoint 的过期 pending
    assert_eq!(
        row["task.status"], "completed",
        "冷兜底须返回 pipeline_state 表最新 completed"
    );
    assert_eq!(row["task.goal"], "写 hello.txt 并自动评估");
    assert_eq!(row["source"], "checkpoint");
    assert_eq!(
        row["thread_id"], pid,
        "任务管道 thread_id 回退自身 pipeline_id"
    );
}

// ── 职责边界：pipeline-state.update 任务域写面 ──────────────────────

#[tokio::test]
async fn test_pipeline_state_update_writes_task_fields_both_paths() {
    // 任务域插件（task_evaluate 等）经 update 写 task.* 键：热路径 registry
    // 常驻 state + 冷路径 pipeline_state 表双落点，list 聚合立即可见。
    let tenant = format!("tenant_upd_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_upd_{}", uuid::Uuid::new_v4().simple());
    let router = router_with_store();
    let store = router
        .store
        .as_ref()
        .expect("router_with_store 已注 store")
        .clone();
    // 先注册管道（模拟任务管道出生）
    let reg = agentos_session::pipeline_state_registry::global_registry();
    reg.get_or_init(
        &tenant,
        &pid,
        "th_upd",
        "agentos",
        json!({"pipeline_id": pid, "task.id": pid, "task.status": "pending"}),
    );

    let r = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_upd"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": TEST_STATE_WRITER,
                "pipeline_id": pid,
                "fields": {"task.status": "completed", "task.ended_at": "2026-08-24T00:00:00Z"},
            }),
        ),
    )
    .await;
    assert!(r.is_ok(), "update 应成功: {r:?}");

    // 冷路径：pipeline_state 表已落（重启后冷恢复读它）
    let fields = store.load_pipeline_state(&pid, &tenant).await.unwrap();
    assert_eq!(fields.get("task.status"), Some(&json!("completed")));
    assert_eq!(
        fields.get("task.ended_at"),
        Some(&json!("2026-08-24T00:00:00Z"))
    );

    // list 聚合立即可见（任务树数据源）
    let rows = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_upd"),
        router.handle("pipeline-state", "list", json!({})),
    )
    .await
    .unwrap();
    let row = rows
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r.get("pipeline_id").and_then(|v| v.as_str()) == Some(pid.as_str()))
        .unwrap();
    assert_eq!(row["task.status"], "completed");

    // 热路径：registry 常驻 state 已更新（守卫在全部 await 之后获取，
    // 避免跨 await 持锁形态）
    let entry = reg.get(&tenant, &pid).expect("registry 应有条目");
    let st = entry.read();
    assert_eq!(st.state["task.status"], "completed");
    assert_eq!(st.state["task.ended_at"], "2026-08-24T00:00:00Z");
}

#[tokio::test]
async fn test_pipeline_state_update_rejects_undeclared_and_unknown_plugin() {
    // 声明制（ADR 2026-10-02）：调用方 manifest state.writes 未声明的键拒绝
    // （如管道运行域 iteration）；无声明的 plugin_id 一律拒绝（fail-closed）。
    let tenant = format!("tenant_upd2_{}", uuid::Uuid::new_v4().simple());
    let router = router_with_store();
    let r = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_upd2"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": TEST_STATE_WRITER,
                "pipeline_id": "pipe_x",
                "fields": {"iteration": 5},
            }),
        ),
    )
    .await;
    let err = r.expect_err("未在 state.writes 声明的键必须拒绝（管道运行域归引擎）");
    assert!(err.to_string().contains("state.writes"), "{err}");

    // 无声明插件：任务域键也不放行（声明制而非前缀制）
    let r2 = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_upd2"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": "undeclared_plugin",
                "pipeline_id": "pipe_x",
                "fields": {"task.status": "running"},
            }),
        ),
    )
    .await;
    assert!(r2.is_err(), "未声明插件的 task.* 键必须拒绝: {r2:?}");

    // 缺 _plugin_id：拒绝（写面只接受插件上下文调用）
    let r3 = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_upd2"),
        router.handle(
            "pipeline-state",
            "update",
            json!({"pipeline_id": "pipe_x", "fields": {"task.status": "running"}}),
        ),
    )
    .await;
    let err3 = r3.expect_err("缺 _plugin_id 必须拒绝");
    assert!(err3.to_string().contains("_plugin_id"), "{err3}");
}

#[tokio::test]
async fn test_pipeline_state_update_prefix_wildcard_and_non_task_namespace() {
    // 前缀通配声明生效，且非 task.* 命名空间同规（不再有域特判）：
    // 声明 trigger.registry.* → trigger.registry.t1 可写。
    let tenant = format!("tenant_upd3_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_upd3_{}", uuid::Uuid::new_v4().simple());
    let router = router_with_store();
    let r = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_upd3"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": TEST_STATE_WRITER,
                "pipeline_id": pid,
                "fields": {"trigger.registry.t1": {"trigger_id": "t1"}},
            }),
        ),
    )
    .await;
    assert!(r.is_ok(), "前缀通配声明的非 task 键应可写: {r:?}");
}

#[tokio::test]
async fn test_service_registry_disabled_without_store() {
    // 不注入 store → service-registry 应返回错误
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new());
    let res = router
        .handle("service-registry", "memory.get", json!({"id": "x"}))
        .await;
    assert!(
        res.is_err(),
        "service-registry must error when store not injected"
    );
}

#[tokio::test]
async fn test_service_registry_unknown_method() {
    let router = router_with_store();
    let res = router
        .handle("service-registry", "bogus.op", json!({}))
        .await;
    assert!(
        res.is_err(),
        "unknown service-registry domain/op must error"
    );
}

// ── traces.list_by_pipeline / pipeline-runs.list_by_pipeline ────────────────
// pipeline_id 是执行态唯一坐标：绑真会话的任务管道在 pipeline_sessions 落
// (task→thread-xxx) 映射，按 thread_id=pipeline_id 查恒空——插件侧
// （task_manage recent_activities / elapsed_seconds）按管道直查。

#[tokio::test]
async fn test_traces_list_by_pipeline_bypasses_session_mapping() {
    // run↔管道映射按生产链路落全（record_run_start 写 state 运行键 + run_id 经
    // message_slots 落槽）→ 两条 step 级 trace；pipeline_sessions 只落
    // (task→thread-xxx) 真会话映射（无自环行）——按旧 traces.list(thread_id=task)
    // 查恒空，list_by_pipeline 仍能查到。
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store.clone());
    store
        .record_run_start(
            "task_tp",
            "default",
            "run_tp_1",
            "hash",
            "2026-10-15T00:00:00+00:00",
        )
        .await
        .unwrap();
    let user_msg = json!({"role": "user", "content": "kickoff"});
    // 引擎真实链路 merge_and_project 给每个 op 注入 _run_id（write_slot_to_
    // table_locked 落 message_slots.run_id）——run_ids_of_pipelines 经槽表反查
    // run 集合（生产语义，非测试捷径）。
    store
        .apply_messages_ops_to_table(
            "task_tp",
            "default",
            &[json!({"op": "set", "seq": 0, "msg": user_msg, "_run_id": "run_tp_1"})],
        )
        .await
        .unwrap();
    for (i, plugin) in ["core", "post"].iter().enumerate() {
        store
            .append_trace(TraceEntry {
                trace_id: format!("trace_tp_{i}"),
                pipeline_id: "task_tp".to_string(),
                // 写入侧 seq 恒 0，存储层按 (pipeline_id, MAX(seq)+1) 分配日志序
                seq: 0,
                plugin_id: plugin.to_string(),
                patch_type: agentos_core::types::PatchType::StateUpdate,
                patch_data: json!({"k": i}),
                created_at: chrono::Utc::now().to_rfc3339(),
            })
            .await
            .unwrap();
    }
    store
        .link_pipeline_session("task_tp", "thread-xxx", "default")
        .await
        .unwrap();

    // 旧读面：thread_id=task → 会话映射反查无 (thread-xxx 下无 task_tp) → 空。
    // 注意 pipeline_sessions 是 (task_tp → thread-xxx)，按 thread_id=task_tp
    // 查不到任何行——这正是绑真会话任务 recent_activities 恒空的历史根因。
    let via_thread = router
        .handle(
            "service-registry",
            "traces.list",
            json!({"thread_id": "task_tp"}),
        )
        .await
        .unwrap();
    assert_eq!(
        via_thread.as_array().map(Vec::len),
        Some(0),
        "绑真会话任务按 thread_id=task 查恒空（历史根因）"
    );

    // 新读面：按 pipeline_id 直查 → 命中 2 条 step 级轨迹。
    let via_pipeline = router
        .handle(
            "service-registry",
            "traces.list_by_pipeline",
            json!({"pipeline_id": "task_tp"}),
        )
        .await
        .unwrap();
    let rows = via_pipeline
        .as_array()
        .expect("traces list_by_pipeline rows");
    assert_eq!(rows.len(), 2, "按管道直查命中全部 step 轨迹");
    let plugin_ids: Vec<&str> = rows
        .iter()
        .map(|r| r["plugin_id"].as_str().unwrap_or(""))
        .collect();
    assert_eq!(plugin_ids, vec!["core", "post"], "日志序（seq 升序）");

    let missing = router
        .handle(
            "service-registry",
            "traces.list_by_pipeline",
            json!({"pipeline_id": "no_such_task"}),
        )
        .await
        .unwrap();
    assert_eq!(
        missing.as_array().map(Vec::len),
        Some(0),
        "无 run → 空列表非报错"
    );
}

#[tokio::test]
async fn test_pipeline_runs_list_by_pipeline_returns_current_run_projection() {
    // ADR 2026-09-18（runs 表退役）：list_by_pipeline 返回该管道的当前运行投影
    // （每管道至多一条，含 created_at/ended_at/status），终态 run 带 ended_at
    // （elapsed_seconds 终点数据源）；同管道新一轮 record_run_start 覆盖投影。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store.clone());
    store
        .record_run_start(
            "task_lp",
            "default",
            "run_lp_1",
            "hash",
            "2026-10-15T00:00:00+00:00",
        )
        .await
        .unwrap();

    let res = router
        .handle(
            "service-registry",
            "pipeline-runs.list_by_pipeline",
            json!({"pipeline_id": "task_lp"}),
        )
        .await
        .unwrap();
    let rows = res.as_array().expect("runs list_by_pipeline rows");
    assert_eq!(rows.len(), 1, "管道当前运行投影恰一条");
    assert!(
        rows.iter()
            .all(|r| r.get("created_at").and_then(|v| v.as_str()).is_some()),
        "每条 run 带 created_at（耗时起点数据源）"
    );

    // 终态落库后投影带 ended_at + 终态 status（set_run_status_projection 对
    // 终态补 run_ended_at）
    sqlite
        .set_run_status_projection(
            "task_lp",
            "default",
            agentos_core::types::RunStatus::Completed,
        )
        .unwrap();
    let res = router
        .handle(
            "service-registry",
            "pipeline-runs.list_by_pipeline",
            json!({"pipeline_id": "task_lp"}),
        )
        .await
        .unwrap();
    let completed = res.as_array().unwrap().first().expect("投影仍在").clone();
    assert_eq!(completed["run_id"], "run_lp_1");
    assert_eq!(completed["status"], "completed");
    assert!(
        completed.get("ended_at").is_some(),
        "终态 run 带 ended_at（耗时终点数据源）"
    );

    // 同管道新一轮 run 开跑：覆盖为当前运行投影（仍至多一条）
    store
        .record_run_start(
            "task_lp",
            "default",
            "run_lp_2",
            "hash",
            "2026-10-15T00:00:00+00:00",
        )
        .await
        .unwrap();
    let res = router
        .handle(
            "service-registry",
            "pipeline-runs.list_by_pipeline",
            json!({"pipeline_id": "task_lp"}),
        )
        .await
        .unwrap();
    let rows = res.as_array().unwrap();
    assert_eq!(rows.len(), 1, "runs 表退役后每管道至多一条投影");
    assert_eq!(rows[0]["run_id"], "run_lp_2", "投影指向最新 run");
    assert_eq!(rows[0]["status"], "running");

    let empty = router
        .handle(
            "service-registry",
            "pipeline-runs.list_by_pipeline",
            json!({"pipeline_id": "no_such_task"}),
        )
        .await
        .unwrap();
    assert_eq!(
        empty.as_array().map(Vec::len),
        Some(0),
        "无 run → 空列表非报错"
    );
}

// ── F-REVIEW-2：pipeline-executor.get_run_status（复盘轮询真实完成）──

#[tokio::test]
async fn test_pipeline_executor_get_run_status_ok() {
    // 建 run（模拟 start_run 后的运行簿记）→ get_run_status 返回状态
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store.clone());
    sqlite
        .record_run_start(
            "task_status_1",
            "default",
            "run_status_1",
            "hash",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    let res = router
        .handle(
            "pipeline-executor",
            "get_run_status",
            json!({"run_id": "run_status_1"}),
        )
        .await
        .unwrap();
    assert_eq!(res["run_id"], "run_status_1");
    assert_eq!(res["status"], "running", "新建 run 状态应为 running");
    // 更新为 completed 后再次查询应反映真实状态（复盘据此落 completed）
    sqlite
        .set_run_status_projection(
            "task_status_1",
            "default",
            agentos_core::types::RunStatus::Completed,
        )
        .unwrap();
    let res2 = router
        .handle(
            "pipeline-executor",
            "get_run_status",
            json!({"run_id": "run_status_1"}),
        )
        .await
        .unwrap();
    assert_eq!(res2["status"], "completed");
    assert!(
        res2.get("ended_at").is_some(),
        "completed run 应有 ended_at"
    );
}

#[tokio::test]
async fn test_pipeline_executor_get_run_status_errors() {
    // 无 store 注入 → 报错（与服务注册一致，不静默）
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new());
    let res = router
        .handle(
            "pipeline-executor",
            "get_run_status",
            json!({"run_id": "x"}),
        )
        .await;
    assert!(res.is_err(), "store 未注入必须报错");

    // 缺 run_id 参数 → 报错
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let router2 =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store.clone());
    let res2 = router2
        .handle("pipeline-executor", "get_run_status", json!({}))
        .await;
    assert!(res2.is_err(), "缺 run_id 必须报错");

    // run 不存在 → 报错（调用方降级为保持 running）
    let res3 = router2
        .handle(
            "pipeline-executor",
            "get_run_status",
            json!({"run_id": "ghost"}),
        )
        .await;
    assert!(res3.is_err(), "不存在的 run 必须报错");
}

// ── G3：registry.register_tool 运行时动态注册 ──

#[tokio::test]
async fn g3_register_tool_calls_registrar() {
    use std::sync::Mutex;
    let captured: Arc<Mutex<Vec<(String, String)>>> = Arc::new(Mutex::new(Vec::new()));
    let cap2 = captured.clone();
    let registrar: DynamicToolRegistrar = Arc::new(move |pid, tool| {
        cap2.lock().unwrap().push((pid.to_string(), tool.name));
        Ok(())
    });
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_dynamic_tool_registrar(registrar);
    let out = router
        .handle(
            "registry",
            "register_tool",
            json!({
                "_plugin_id": "connector",
                "name": "dyn_query",
                "description": "查询外部系统",
                "input_schema": {"type": "object"},
                "category": "search",
            }),
        )
        .await
        .unwrap();
    assert_eq!(out["status"], "registered");
    assert_eq!(out["plugin_id"], "connector");
    assert_eq!(
        *captured.lock().unwrap(),
        vec![("connector".into(), "dyn_query".into())]
    );
}

#[tokio::test]
async fn g3_register_tool_requires_plugin_context() {
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new());
    let err = router
        .handle("registry", "register_tool", json!({"name": "x"}))
        .await
        .unwrap_err();
    assert!(format!("{err}").contains("_plugin_id"));
}

#[tokio::test]
async fn g3_register_tool_requires_name() {
    let registrar: DynamicToolRegistrar = Arc::new(|_, _| Ok(()));
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_dynamic_tool_registrar(registrar);
    let err = router
        .handle("registry", "register_tool", json!({"_plugin_id": "p"}))
        .await
        .unwrap_err();
    assert!(format!("{err}").contains("name"));
}

#[tokio::test]
async fn g3_register_tool_without_registrar_errors() {
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new());
    let err = router
        .handle(
            "registry",
            "register_tool",
            json!({"_plugin_id": "p", "name": "x"}),
        )
        .await
        .unwrap_err();
    assert!(format!("{err}").contains("未装配"));
}

#[tokio::test]
async fn test_suspend_resume_pipeline_by_id() {
    // GAP-1 统一：task = pipeline——按管道挂起/恢复（stop/resume 映射）。
    // resume_pipeline 三分语义：停泊挂起拉起续跑轮（PipelineResumerFn 派发）、
    // 审批挂起翻 Running 簿记、执行在飞幂等空转（见 resume_pipeline_* 用例族）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let dispatched: Arc<DispatchedResumes> = Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink = dispatched.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_pipeline_resumer(Arc::new(
            move |pipeline_id: String,
                  thread_id: String,
                  user_id: String,
                  state_overlay: Option<serde_json::Value>| {
                let sink = sink.clone();
                Box::pin(async move {
                    sink.lock()
                        .unwrap()
                        .push((pipeline_id, thread_id, user_id, state_overlay));
                    Ok(())
                })
                    as std::pin::Pin<
                        Box<dyn std::future::Future<Output = Result<(), String>> + Send>,
                    >
            },
        ));

    let mut scope_ctx = agentos_core::types::TenantContext::new("tenant_sr", "thread_sr");
    scope_ctx.user_id = Some("u_injected".to_string());
    agentos_tenant::scope(scope_ctx, async {
        let tenant = agentos_tenant::current_or_default("default").tenant_id;
        store
            .record_run_start(
                "pipe_task_9",
                &tenant,
                "run_sr_1",
                "h",
                "2026-10-15T00:00:00+00:00",
            )
            .await
            .unwrap();
        store
            .record_run_start(
                "pipe_task_9",
                &tenant,
                "run_sr_2",
                "h",
                "2026-10-15T00:00:00+00:00",
            )
            .await
            .unwrap();
        store
            .link_pipeline_session("pipe_task_9", "thread_sr", &tenant)
            .await
            .unwrap();

        // suspend：最新 run（run_sr_2）被挂起
        let r = router
            .handle(
                "pipeline-executor",
                "suspend_pipeline",
                json!({"pipeline_id": "pipe_task_9"}),
            )
            .await
            .unwrap();
        assert_eq!(r["status"], "suspended");
        assert_eq!(r["run_id"], "run_sr_2", "应挂起最新 run");
        let got = store.get_run("run_sr_2").await.unwrap();
        assert_eq!(got.status, agentos_core::types::RunStatus::Suspended);

        // 幂等：再次 suspend 返回同 run
        let r2 = router
            .handle(
                "pipeline-executor",
                "suspend_pipeline",
                json!({"pipeline_id": "pipe_task_9"}),
            )
            .await
            .unwrap();
        assert_eq!(r2["run_id"], "run_sr_2");

        // resume：停泊挂起 → 拉起续跑轮；旧 run 保持 Suspended（挂起即其
        // 真实收束史，新 run 另起一行承载执行，不造幽灵 running）。
        // 身份契约（刀3）：反调面身份取执行上下文注入（scope user_id），
        // params.user_id 形参退役不可指派——此处 params 不带 user。
        let r3 = router
            .handle(
                "pipeline-executor",
                "resume_pipeline",
                json!({"pipeline_id": "pipe_task_9"}),
            )
            .await
            .unwrap();
        assert_eq!(r3["run_id"], "run_sr_2");
        assert_eq!(r3["dispatched"], true, "停泊挂起应派发续跑轮");
        let got2 = store.get_run("run_sr_2").await.unwrap();
        assert_eq!(
            got2.status,
            agentos_core::types::RunStatus::Suspended,
            "旧 run 不翻 Running——翻了无人收尾即幽灵 running"
        );
        assert_eq!(
            dispatched.lock().unwrap().as_slice(),
            [(
                "pipe_task_9".to_string(),
                "thread_sr".to_string(),
                "u_injected".to_string(),
                None
            )],
            "续跑派发身份应取执行上下文注入 user（scope 回显），非 params 手传"
        );

        // 孤儿管道（无会话挂载）→ 拒绝派发（回复无人订阅，静默即假成功）
        let r4 = router
            .handle(
                "pipeline-executor",
                "resume_pipeline",
                json!({"pipeline_id": "pipe_ghost"}),
            )
            .await;
        assert!(r4.is_err(), "孤儿/伪造 id 应拒绝派发");
    })
    .await;
}

#[tokio::test]
async fn resume_pipeline_ignores_params_user_prefers_injected() {
    // 刀3 身份契约：反调面 params.user_id 不可指派身份——注入 user 在场时
    // 覆盖手传值（伪造/旧插件传值一律忽略，原头 = 内核执行上下文回显）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let dispatched: Arc<DispatchedResumes> = Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink = dispatched.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_pipeline_resumer(Arc::new(
            move |pipeline_id: String,
                  thread_id: String,
                  user_id: String,
                  state_overlay: Option<serde_json::Value>| {
                let sink = sink.clone();
                Box::pin(async move {
                    sink.lock()
                        .unwrap()
                        .push((pipeline_id, thread_id, user_id, state_overlay));
                    Ok(())
                })
                    as std::pin::Pin<
                        Box<dyn std::future::Future<Output = Result<(), String>> + Send>,
                    >
            },
        ));

    let mut scope_ctx = agentos_core::types::TenantContext::new("t_forge", "th_forge");
    scope_ctx.user_id = Some("u_real".to_string());
    agentos_tenant::scope(scope_ctx, async {
        let tenant = agentos_tenant::current_or_default("default").tenant_id;
        store
            .record_run_start(
                "pipe_forge",
                &tenant,
                "run_f1",
                "h",
                "2026-10-15T00:00:00+00:00",
            )
            .await
            .unwrap();
        store
            .link_pipeline_session("pipe_forge", "th_forge", &tenant)
            .await
            .unwrap();
        router
            .handle(
                "pipeline-executor",
                "suspend_pipeline",
                json!({"pipeline_id": "pipe_forge"}),
            )
            .await
            .unwrap();

        let r = router
            .handle(
                "pipeline-executor",
                "resume_pipeline",
                json!({"pipeline_id": "pipe_forge", "user_id": "u_fake"}),
            )
            .await
            .unwrap();
        assert_eq!(r["dispatched"], true);
        assert_eq!(
            dispatched.lock().unwrap().as_slice(),
            [(
                "pipe_forge".to_string(),
                "th_forge".to_string(),
                "u_real".to_string(),
                None
            )],
            "手传 u_fake 应被忽略，派发身份取注入 user（u_real）"
        );
    })
    .await;
}

#[tokio::test]
async fn resume_pipeline_without_injected_identity_fails_fast() {
    // 刀3 fail-fast：scope 无注入 user（桥仅建 tenant scope，`_user_id` 缺席
    // 形态）→ 显式拒绝，不得以空串透传派发（空身份曾飘到 WS 分发层才炸，
    // 文案误导为「token 持有者已删除」，装机 2026-11-02 实证）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let dispatched: Arc<DispatchedResumes> = Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink = dispatched.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_pipeline_resumer(Arc::new(
            move |pipeline_id: String,
                  thread_id: String,
                  user_id: String,
                  state_overlay: Option<serde_json::Value>| {
                let sink = sink.clone();
                Box::pin(async move {
                    sink.lock()
                        .unwrap()
                        .push((pipeline_id, thread_id, user_id, state_overlay));
                    Ok(())
                })
                    as std::pin::Pin<
                        Box<dyn std::future::Future<Output = Result<(), String>> + Send>,
                    >
            },
        ));

    let scope_ctx = agentos_core::types::TenantContext::new("t_nouser", "th_nouser");
    agentos_tenant::scope(scope_ctx, async {
        let tenant = agentos_tenant::current_or_default("default").tenant_id;
        store
            .record_run_start(
                "pipe_nouser",
                &tenant,
                "run_n1",
                "h",
                "2026-10-15T00:00:00+00:00",
            )
            .await
            .unwrap();
        store
            .link_pipeline_session("pipe_nouser", "th_nouser", &tenant)
            .await
            .unwrap();
        router
            .handle(
                "pipeline-executor",
                "suspend_pipeline",
                json!({"pipeline_id": "pipe_nouser"}),
            )
            .await
            .unwrap();

        let err = router
            .handle(
                "pipeline-executor",
                "resume_pipeline",
                json!({"pipeline_id": "pipe_nouser"}),
            )
            .await
            .unwrap_err();
        assert!(
            format!("{err}").contains("未携带用户身份"),
            "fail-fast 文案应区分「未携带身份」而非「token 持有者已删除」: {err}"
        );
        assert!(
            dispatched.lock().unwrap().is_empty(),
            "无注入身份不得派发续跑轮"
        );
    })
    .await;
}

#[tokio::test]
async fn resume_pipeline_boot_fallback_uses_persisted_owner() {
    // 启动期系统面（插件 on_load 排空自动恢复）无注入身份：回落管道持久化
    // 属主 task.submitted_by 续跑——身份仍非调用方手传（信任模型不变）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let dispatched: Arc<DispatchedResumes> = Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink = dispatched.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_pipeline_resumer(Arc::new(
            move |pipeline_id: String,
                  thread_id: String,
                  user_id: String,
                  state_overlay: Option<serde_json::Value>| {
                let sink = sink.clone();
                Box::pin(async move {
                    sink.lock()
                        .unwrap()
                        .push((pipeline_id, thread_id, user_id, state_overlay));
                    Ok(())
                })
                    as std::pin::Pin<
                        Box<dyn std::future::Future<Output = Result<(), String>> + Send>,
                    >
            },
        ));

    let scope_ctx = agentos_core::types::TenantContext::new("t_boot", "th_boot");
    agentos_tenant::scope(scope_ctx, async {
        let tenant = agentos_tenant::current_or_default("default").tenant_id;
        store
            .record_run_start(
                "pipe_boot",
                &tenant,
                "run_b1",
                "h",
                "2026-10-15T00:00:00+00:00",
            )
            .await
            .unwrap();
        store
            .link_pipeline_session("pipe_boot", "th_boot", &tenant)
            .await
            .unwrap();
        store
            .upsert_state_fields(
                "pipe_boot",
                &tenant,
                &json!({"task.submitted_by": "u_owner"})
                    .as_object()
                    .unwrap()
                    .clone(),
            )
            .await
            .unwrap();
        router
            .handle(
                "pipeline-executor",
                "suspend_pipeline",
                json!({"pipeline_id": "pipe_boot"}),
            )
            .await
            .unwrap();

        let r = router
            .handle(
                "pipeline-executor",
                "resume_pipeline",
                json!({"pipeline_id": "pipe_boot"}),
            )
            .await
            .unwrap();
        assert_eq!(r["dispatched"], true);
        assert_eq!(
            dispatched.lock().unwrap().as_slice(),
            [(
                "pipe_boot".to_string(),
                "th_boot".to_string(),
                "u_owner".to_string(),
                None
            )],
            "无注入身份时应回落持久化属主（u_owner）派发"
        );
    })
    .await;
}

#[tokio::test]
async fn resume_pipeline_approval_pending_flips_only() {
    // 审批挂起（metadata.pending_interaction_request_id 署名，run 在飞未收束）：
    // 翻 Running 簿记，真唤醒走 interaction.respond——不派发续跑轮（重复派发
    // 会在旧 run 仍执行时叠出第二轮）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let dispatched: Arc<DispatchedResumes> = Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink = dispatched.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_pipeline_resumer(Arc::new(
            move |pipeline_id: String,
                  thread_id: String,
                  user_id: String,
                  state_overlay: Option<serde_json::Value>| {
                let sink = sink.clone();
                Box::pin(async move {
                    sink.lock()
                        .unwrap()
                        .push((pipeline_id, thread_id, user_id, state_overlay));
                    Ok(())
                })
                    as std::pin::Pin<
                        Box<dyn std::future::Future<Output = Result<(), String>> + Send>,
                    >
            },
        ));

    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_ap", "thread_ap"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            store
                .record_run_start(
                    "pipe_ap",
                    &tenant,
                    "run_ap_1",
                    "h",
                    "2026-10-15T00:00:00+00:00",
                )
                .await
                .unwrap();
            store
                .link_pipeline_session("pipe_ap", "thread_ap", &tenant)
                .await
                .unwrap();
            router
                .handle(
                    "pipeline-executor",
                    "suspend_pipeline",
                    json!({"pipeline_id": "pipe_ap"}),
                )
                .await
                .unwrap();
            // 审批署名：挂起凭据落 state 键 suspend_request_id（get_run 投影把它
            // 映射回 metadata.pending_interaction_request_id）
            sqlite
                .upsert_state_field("pipe_ap", &tenant, "suspend_request_id", &json!("req_1"))
                .unwrap();

            let r = router
                .handle(
                    "pipeline-executor",
                    "resume_pipeline",
                    json!({"pipeline_id": "pipe_ap"}),
                )
                .await
                .unwrap();
            assert_eq!(r["run_id"], "run_ap_1");
            assert_eq!(r["dispatched"], false, "审批挂起只翻簿记不派发");
            let got = store.get_run("run_ap_1").await.unwrap();
            assert_eq!(got.status, agentos_core::types::RunStatus::Running);
            assert!(dispatched.lock().unwrap().is_empty());
        },
    )
    .await;
}

#[tokio::test]
async fn suspend_persists_pending_interaction_credential_and_resume_clears_it() {
    // 挂起凭据读写接线：suspend 带 approval_id（approval 插件既有形参名）→
    // runs.metadata 落 pending_interaction_request_id（interaction_response
    // 按 request_id 反查唤醒的依据）；resume 翻 Running 同时清除凭据。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_sqlite(sqlite.clone());

    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_s7", "thread_s7"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            store
                .record_run_start(
                    "pipe_s7",
                    &tenant,
                    "run_s7",
                    "h",
                    "2026-10-15T00:00:00+00:00",
                )
                .await
                .unwrap();

            // suspend：status 翻 Suspended + 凭据落 state 键 suspend_request_id
            router
                .handle(
                    "pipeline-executor",
                    "suspend",
                    json!({"run_id": "run_s7", "approval_id": "req_s7"}),
                )
                .await
                .unwrap();
            let got = store.get_run("run_s7").await.unwrap();
            assert_eq!(got.status, agentos_core::types::RunStatus::Suspended);
            let cred = got
                .metadata
                .as_ref()
                .and_then(|m| m.get("pending_interaction_request_id"))
                .and_then(|v| v.as_str())
                .expect("suspend 后投影 metadata 必须带挂起凭据");
            assert_eq!(cred, "req_s7");
            // 凭据真身 = state 标量键 suspend_request_id（反查唤醒的依据）
            let found = sqlite.find_suspended_run_by_request_id("req_s7").unwrap();
            assert!(found.is_some(), "挂起凭据必须可按 request_id 反查");

            // resume：翻 Running + 凭据清除（陈旧凭据会让反查误判仍在等审批）
            router
                .handle("pipeline-executor", "resume", json!({"run_id": "run_s7"}))
                .await
                .unwrap();
            let got = store.get_run("run_s7").await.unwrap();
            assert_eq!(got.status, agentos_core::types::RunStatus::Running);
            let cleared = sqlite.find_suspended_run_by_request_id("req_s7").unwrap();
            assert!(
                cleared.is_none(),
                "resume 后挂起凭据必须清除（按凭据反查不再命中）"
            );
        },
    )
    .await;
}

#[tokio::test]
async fn suspend_without_request_id_leaves_metadata_absent() {
    // 无凭据形参的 suspend（既有调用方形状）不写 metadata：凭据缺失时读面
    // 按"无凭据"跳过，语义与接线前一致。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_sqlite(sqlite.clone());

    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_s7b", "thread_s7b"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            store
                .record_run_start(
                    "pipe_s7b",
                    &tenant,
                    "run_s7b",
                    "h",
                    "2026-10-15T00:00:00+00:00",
                )
                .await
                .unwrap();
            router
                .handle("pipeline-executor", "suspend", json!({"run_id": "run_s7b"}))
                .await
                .unwrap();
            let got = store.get_run("run_s7b").await.unwrap();
            assert_eq!(got.status, agentos_core::types::RunStatus::Suspended);
            assert!(got.metadata.is_none(), "无凭据形参不得凭空写 metadata");
        },
    )
    .await;
}

#[tokio::test]
async fn resume_pipeline_running_in_flight_idempotent() {
    // 最新 run Running（执行在飞）：幂等空转——不翻状态、不重复派发。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let dispatched: Arc<DispatchedResumes> = Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink = dispatched.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_pipeline_resumer(Arc::new(
            move |pipeline_id: String,
                  thread_id: String,
                  user_id: String,
                  state_overlay: Option<serde_json::Value>| {
                let sink = sink.clone();
                Box::pin(async move {
                    sink.lock()
                        .unwrap()
                        .push((pipeline_id, thread_id, user_id, state_overlay));
                    Ok(())
                })
                    as std::pin::Pin<
                        Box<dyn std::future::Future<Output = Result<(), String>> + Send>,
                    >
            },
        ));

    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_rf", "thread_rf"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            store
                .record_run_start(
                    "pipe_rf",
                    &tenant,
                    "run_rf_1",
                    "h",
                    "2026-10-15T00:00:00+00:00",
                )
                .await
                .unwrap();
            store
                .link_pipeline_session("pipe_rf", "thread_rf", &tenant)
                .await
                .unwrap();

            let r = router
                .handle(
                    "pipeline-executor",
                    "resume_pipeline",
                    json!({"pipeline_id": "pipe_rf"}),
                )
                .await
                .unwrap();
            assert_eq!(r["run_id"], "run_rf_1");
            assert_eq!(r["dispatched"], false, "执行在飞不重复派发");
            let got = store.get_run("run_rf_1").await.unwrap();
            assert_eq!(got.status, agentos_core::types::RunStatus::Running);
            assert!(dispatched.lock().unwrap().is_empty());
        },
    )
    .await;
}

#[tokio::test]
async fn resume_pipeline_terminal_history_dispatches_new_round() {
    // 无在飞 run（最新 run 已终态 Cancelled——面板 pause 后 llm_core 中断收束的
    // 主路径，suspended run 不存在）：仍拉起续跑轮，state 由快照恢复。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let dispatched: Arc<DispatchedResumes> = Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink = dispatched.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_pipeline_resumer(Arc::new(
            move |pipeline_id: String,
                  thread_id: String,
                  user_id: String,
                  state_overlay: Option<serde_json::Value>| {
                let sink = sink.clone();
                Box::pin(async move {
                    sink.lock()
                        .unwrap()
                        .push((pipeline_id, thread_id, user_id, state_overlay));
                    Ok(())
                })
                    as std::pin::Pin<
                        Box<dyn std::future::Future<Output = Result<(), String>> + Send>,
                    >
            },
        ));

    let mut th_ctx = agentos_core::types::TenantContext::new("tenant_th", "thread_th");
    th_ctx.user_id = Some("u_th".to_string());
    agentos_tenant::scope(th_ctx, async {
        let tenant = agentos_tenant::current_or_default("default").tenant_id;
        store
            .record_run_start(
                "pipe_th",
                &tenant,
                "run_th_1",
                "h",
                "2026-10-15T00:00:00+00:00",
            )
            .await
            .unwrap();
        sqlite
            .set_run_status_projection(
                "pipe_th",
                &tenant,
                agentos_core::types::RunStatus::Cancelled,
            )
            .unwrap();
        store
            .link_pipeline_session("pipe_th", "thread_th", &tenant)
            .await
            .unwrap();

        let r = router
            .handle(
                "pipeline-executor",
                "resume_pipeline",
                json!({"pipeline_id": "pipe_th"}),
            )
            .await
            .unwrap();
        assert_eq!(
            r["run_id"], "",
            "无在飞 run：run_id 保持空串约定，续跑由新 run 承载"
        );
        assert_eq!(r["dispatched"], true, "终态历史应拉起新续跑轮");
        assert_eq!(dispatched.lock().unwrap().len(), 1);
    })
    .await;
}

#[tokio::test]
async fn resume_pipeline_forwards_state_overlay_to_dispatch() {
    // B13①（ADR 2026-09-06）：resume_pipeline 的 state_overlay 参数必须原样
    // 透传给续跑派发——任务域 resume 链据此携带终态键复位（task.status 回
    // running），内核不解构不解释（零任务域知识）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let dispatched: Arc<DispatchedResumes> = Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink = dispatched.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_pipeline_resumer(Arc::new(
            move |pipeline_id: String,
                  thread_id: String,
                  user_id: String,
                  state_overlay: Option<serde_json::Value>| {
                let sink = sink.clone();
                Box::pin(async move {
                    sink.lock()
                        .unwrap()
                        .push((pipeline_id, thread_id, user_id, state_overlay));
                    Ok(())
                })
                    as std::pin::Pin<
                        Box<dyn std::future::Future<Output = Result<(), String>> + Send>,
                    >
            },
        ));

    let mut ov_ctx = agentos_core::types::TenantContext::new("tenant_ov", "thread_ov");
    ov_ctx.user_id = Some("u_ov".to_string());
    agentos_tenant::scope(ov_ctx, async {
        let tenant = agentos_tenant::current_or_default("default").tenant_id;
        store
            .record_run_start(
                "pipe_ov",
                &tenant,
                "run_ov_1",
                "h",
                "2026-10-15T00:00:00+00:00",
            )
            .await
            .unwrap();
        // 停泊挂起态（无审批署名 → 走续跑拉起分支）
        sqlite
            .set_run_status_projection(
                "pipe_ov",
                &tenant,
                agentos_core::types::RunStatus::Suspended,
            )
            .unwrap();
        store
            .link_pipeline_session("pipe_ov", "thread_ov", &tenant)
            .await
            .unwrap();

        let r = router
            .handle(
                "pipeline-executor",
                "resume_pipeline",
                json!({
                    "pipeline_id": "pipe_ov",
                    "state_overlay": {"task.status": "running", "task_status": "running"},
                }),
            )
            .await
            .unwrap();
        assert_eq!(r["dispatched"], true);
        let got = dispatched.lock().unwrap();
        let (_, _, _, overlay) = &got[0];
        assert_eq!(
            overlay.as_ref().expect("overlay 必须透传")["task.status"],
            json!("running"),
            "任务域复位 overlay 必须原样到达派发闭包"
        );
        assert_eq!(overlay.as_ref().unwrap()["task_status"], json!("running"));
    })
    .await;
}

#[tokio::test]
async fn resume_pipeline_without_resumer_degrades_bookkeeping() {
    // 恢复派发未装配（旧装配/测试形态）：降级既有簿记语义（suspended → running），
    // dispatched:false 显式化——调用方可据此区分簿记与真派发。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store.clone());

    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_nr", "thread_nr"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            store
                .record_run_start(
                    "pipe_nr",
                    &tenant,
                    "run_nr_1",
                    "h",
                    "2026-10-15T00:00:00+00:00",
                )
                .await
                .unwrap();
            store
                .link_pipeline_session("pipe_nr", "thread_nr", &tenant)
                .await
                .unwrap();
            router
                .handle(
                    "pipeline-executor",
                    "suspend_pipeline",
                    json!({"pipeline_id": "pipe_nr"}),
                )
                .await
                .unwrap();

            let r = router
                .handle(
                    "pipeline-executor",
                    "resume_pipeline",
                    json!({"pipeline_id": "pipe_nr"}),
                )
                .await
                .unwrap();
            assert_eq!(r["run_id"], "run_nr_1");
            assert_eq!(r["dispatched"], false);
            let got = store.get_run("run_nr_1").await.unwrap();
            assert_eq!(got.status, agentos_core::types::RunStatus::Running);
        },
    )
    .await;
}

// ── transient.* 四方法（ADR 2026-08-27 方案 §2.3）──────────────────
// 中间态内存寄存器能力面：set/get/list/clear。tenant 取
// agentos_tenant::current_or_default（测试无 scope 时 = default）。

fn router_plain() -> KernelCapabilityRouter {
    KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
}

#[tokio::test]
async fn transient_set_get_roundtrip() {
    let router = router_plain();
    let res = router
        .handle(
            "transient",
            "set",
            json!({
                "pipeline_id": "pipe_t1",
                "key": "chunk:mc_a",
                "value": {"text_len": 3},
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "set");
    let got = router
        .handle(
            "transient",
            "get",
            json!({"pipeline_id": "pipe_t1", "key": "chunk:mc_a"}),
        )
        .await
        .unwrap();
    assert_eq!(got["found"], true);
    assert_eq!(got["value"]["text_len"], json!(3));
    // 未写的键 found=false、value=null
    let miss = router
        .handle(
            "transient",
            "get",
            json!({"pipeline_id": "pipe_t1", "key": "chunk:mc_b"}),
        )
        .await
        .unwrap();
    assert_eq!(miss["found"], false);
    assert_eq!(miss["value"], json!(null));
    // 清理后 get 落空
    router
        .handle(
            "transient",
            "clear",
            json!({"pipeline_id": "pipe_t1", "key": "chunk:mc_a"}),
        )
        .await
        .unwrap();
    let after = router
        .handle(
            "transient",
            "get",
            json!({"pipeline_id": "pipe_t1", "key": "chunk:mc_a"}),
        )
        .await
        .unwrap();
    assert_eq!(after["found"], false);
}

#[tokio::test]
async fn transient_set_overwrites_same_key() {
    let router = router_plain();
    router
        .handle(
            "transient",
            "set",
            json!({"pipeline_id": "pipe_t2", "key": "progress:1", "value": {"pct": 10}}),
        )
        .await
        .unwrap();
    router
        .handle(
            "transient",
            "set",
            json!({"pipeline_id": "pipe_t2", "key": "progress:1", "value": {"pct": 80}}),
        )
        .await
        .unwrap();
    let got = router
        .handle(
            "transient",
            "get",
            json!({"pipeline_id": "pipe_t2", "key": "progress:1"}),
        )
        .await
        .unwrap();
    assert_eq!(got["value"]["pct"], json!(80), "同 key 覆盖取最新值");
}

#[tokio::test]
async fn transient_list_enumerates_pipeline_states() {
    let router = router_plain();
    router
        .handle(
            "transient",
            "set",
            json!({"pipeline_id": "pipe_t3", "key": "chunk:mc_a", "value": {"text_len": 3}}),
        )
        .await
        .unwrap();
    router
        .handle(
            "transient",
            "set",
            json!({"pipeline_id": "pipe_t3", "key": "progress:1", "value": {"pct": 50}}),
        )
        .await
        .unwrap();
    let rows = router
        .handle("transient", "list", json!({"pipeline_id": "pipe_t3"}))
        .await
        .unwrap();
    let states = rows["transient_states"].as_array().unwrap();
    assert_eq!(states.len(), 2);
    let keys: Vec<&str> = states.iter().filter_map(|r| r["key"].as_str()).collect();
    assert!(keys.contains(&"chunk:mc_a"));
    assert!(keys.contains(&"progress:1"));
    // 未写过的管道返回空数组（而非错误）
    let empty = router
        .handle("transient", "list", json!({"pipeline_id": "pipe_ghost"}))
        .await
        .unwrap();
    assert_eq!(empty["transient_states"].as_array().unwrap().len(), 0);
}

#[tokio::test]
async fn transient_missing_params_rejected() {
    let router = router_plain();
    // 缺 pipeline_id / key / value 各档
    let r1 = router.handle("transient", "set", json!({"key": "k"})).await;
    assert!(r1.is_err(), "缺 pipeline_id 必须报错");
    let r2 = router
        .handle("transient", "get", json!({"pipeline_id": "p"}))
        .await;
    assert!(r2.is_err(), "缺 key 必须报错");
    let r3 = router.handle("transient", "list", json!({})).await;
    assert!(r3.is_err(), "缺 pipeline_id 必须报错");
    let r4 = router
        .handle("transient", "clear", json!({"pipeline_id": "p"}))
        .await;
    assert!(r4.is_err(), "缺 key 必须报错");
    // 空串等同缺参
    let r5 = router
        .handle(
            "transient",
            "set",
            json!({"pipeline_id": "", "key": "k", "value": 1}),
        )
        .await;
    assert!(r5.is_err(), "空 pipeline_id 必须报错");
}

// ── 流式拦截点：chunk 累积 + 节流 + stream_end 清键（ADR 2026-08-27 §2.4）──
// 拦截点写进程级全局寄存器（tenant = current_or_default("default")）——
// 测试用唯一 pipeline id 隔离 + 末尾 clear_pipeline 防跨测试残留。

async fn emit_stream_event(
    router: &KernelCapabilityRouter,
    event: &str,
    pipeline_id: &str,
    message_id: &str,
    content: &str,
) {
    let mut payload = json!({
        "thread_id": "thread-1",
        "pipeline_id": pipeline_id,
        "message_id": message_id,
    });
    if !content.is_empty() {
        payload["content"] = json!(content);
    }
    router
        .handle(
            "event-bus",
            "emit",
            json!({
                "_plugin_id": "my_streamer",
                "event": event,
                "payload": payload,
            }),
        )
        .await
        .unwrap();
}

#[tokio::test]
async fn stream_chunk_accumulates_throttled_to_register() {
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());
    let mid = "p_acc_001";
    let pipe = "pipe_stream_intercept_acc";
    // 节流窗内（不足 N 个）：寄存器无 chunk 键
    for i in 0..(agentos_engine::transient::CHUNK_FLUSH_EVERY - 1) {
        emit_stream_event(&router, "stream_chunk", pipe, mid, &format!("c{i}")).await;
    }
    let reg = agentos_engine::global_registry();
    assert!(
        reg.get("default", pipe, &format!("chunk:{mid}")).is_none(),
        "节流窗内不得落寄存器"
    );
    // 第 N 个 chunk：达计数阈值 → 落 A 区（text_len = 2×N，每个 chunk 2 字符）
    emit_stream_event(&router, "stream_chunk", pipe, mid, "xx").await;
    let snap = reg.get("default", pipe, &format!("chunk:{mid}")).unwrap();
    assert_eq!(
        snap["text_len"],
        json!((agentos_engine::transient::CHUNK_FLUSH_EVERY as usize) * 2),
        "chunk 累积快照 text_len = 增量拼接总长"
    );
    // WS 侧照常推送（一次 IPC 两个动作：推 WS + 累积）
    assert!(!received.lock().unwrap().is_empty());
    reg.clear_pipeline("default", pipe);
}

#[tokio::test]
async fn stream_end_clears_chunk_key() {
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());
    let mid = "p_acc_002";
    let pipe = "pipe_stream_intercept_end";
    for _ in 0..agentos_engine::transient::CHUNK_FLUSH_EVERY {
        emit_stream_event(&router, "stream_chunk", pipe, mid, "a").await;
    }
    let reg = agentos_engine::global_registry();
    assert!(reg.get("default", pipe, &format!("chunk:{mid}")).is_some());
    // stream_end：最终形态已落 message_slots，chunk 中间态清键
    emit_stream_event(&router, "stream_end", pipe, mid, "").await;
    assert!(
        reg.get("default", pipe, &format!("chunk:{mid}")).is_none(),
        "stream_end 必须清 chunk 键"
    );
    reg.clear_pipeline("default", pipe);
}

#[tokio::test]
async fn thinking_chunk_accumulates_reasoning_snapshot() {
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());
    let mid = "p_acc_003";
    let pipe = "pipe_stream_intercept_thinking";
    // thinking_chunk 的 content 增量进 reasoning 快照
    for _ in 0..agentos_engine::transient::CHUNK_FLUSH_EVERY {
        emit_stream_event(&router, "thinking_chunk", pipe, mid, "想").await;
    }
    let reg = agentos_engine::global_registry();
    let snap = reg.get("default", pipe, &format!("chunk:{mid}")).unwrap();
    assert_eq!(snap["text_len"], json!(0), "thinking 不进 text");
    assert_eq!(
        snap["reasoning_len"],
        json!((agentos_engine::transient::CHUNK_FLUSH_EVERY as usize) * 3),
        "thinking 增量按 reasoning_len 累积（UTF-8 字节长）"
    );
    reg.clear_pipeline("default", pipe);
}

#[tokio::test]
async fn stream_interception_skips_without_session() {
    // session 未接线：拦截点不执行（emit 成功路径只存在于 session 分支内），
    // 寄存器零写入——热路径零开销语义。
    let router = router_plain();
    let mid = "p_acc_004";
    let pipe = "pipe_stream_intercept_nosess";
    for _ in 0..agentos_engine::transient::CHUNK_FLUSH_EVERY {
        emit_stream_event(&router, "stream_chunk", pipe, mid, "a").await;
    }
    let reg = agentos_engine::global_registry();
    assert!(
        reg.get("default", pipe, &format!("chunk:{mid}")).is_none(),
        "无 session 时不得累积（该分支不属拦截路径）"
    );
}

// ── tool-surface.schemas：LLM 工具面过滤服务（K10 执行时契约）──────────

/// 构造挂了工具注册表的路由器（工具逐个按描述符注册）。
fn router_with_tool_descriptors(
    tools: &[agentos_core::traits::ToolDescriptor],
) -> KernelCapabilityRouter {
    router_with_tool_descriptors_and_forced(tools, Vec::new())
}

/// 带强制注入声明（P1-5 声明化：manifest force_include_tools 并集）的路由器。
fn router_with_tool_descriptors_and_forced(
    tools: &[agentos_core::traits::ToolDescriptor],
    forced: Vec<&str>,
) -> KernelCapabilityRouter {
    use agentos_plugin_loader::CapabilityRegistryImpl;
    let registry = Arc::new(CapabilityRegistryImpl::new());
    for t in tools {
        registry.register_tool("test_plugin", t.clone());
    }
    let forced: Vec<String> = forced.into_iter().map(String::from).collect();
    let lookup: crate::capability_router::ForceIncludeToolsLookupFn =
        Arc::new(move || forced.clone());
    KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_registry(registry)
        .with_force_include_tools_lookup(lookup)
}

fn plain_tool(name: &str) -> agentos_core::traits::ToolDescriptor {
    agentos_core::traits::ToolDescriptor {
        name: name.to_string(),
        description: format!("test tool {name}"),
        plugin_id: "test_plugin".to_string(),
        input_schema: json!({"type": "object", "properties": {}}),
        output_schema: None,
        category: agentos_core::types::ToolCategory::System,
        source: agentos_core::types::ToolSource::Builtin,
        ui: None,
        render: None,
        isolation_capable: None,
    }
}

fn schema_names(result: &serde_json::Value) -> Vec<String> {
    let mut names: Vec<String> = result["schemas"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["function"]["name"].as_str().unwrap().to_string())
        .collect();
    names.sort();
    names
}

#[tokio::test]
async fn tool_surface_filters_by_tool_ids_and_keeps_declared_tools() {
    // 契约（P1-5 声明化）：白名单命中注入；声明方（manifest
    // force_include_tools）声明的工具无视白名单保留。
    let router = router_with_tool_descriptors_and_forced(
        &[
            plain_tool("bash_execute"),
            plain_tool("file_read"),
            plain_tool("forced_demo_tool"),
        ],
        vec!["forced_demo_tool"],
    );
    let result = router
        .handle(
            "tool-surface",
            "schemas",
            json!({"tool_ids": ["bash_execute"]}),
        )
        .await
        .unwrap();
    assert_eq!(
        schema_names(&result),
        vec!["bash_execute".to_string(), "forced_demo_tool".to_string()],
        "白名单命中 + 声明强制工具"
    );
    // OpenAI function-calling 形态
    let first = &result["schemas"][0];
    assert_eq!(first["type"], "function");
    assert!(first["function"]["parameters"].is_object());
    // 换一组声明 → 强制面随声明变（声明驱动，非内核名单）
    let router2 = router_with_tool_descriptors_and_forced(
        &[plain_tool("bash_execute"), plain_tool("forced_demo_tool")],
        vec!["bash_execute"],
    );
    let result2 = router2
        .handle(
            "tool-surface",
            "schemas",
            json!({"tool_ids": ["forced_demo_tool"]}),
        )
        .await
        .unwrap();
    assert_eq!(
        schema_names(&result2),
        vec!["bash_execute".to_string(), "forced_demo_tool".to_string()],
        "强制注入集合随声明增减"
    );
}

#[tokio::test]
async fn tool_surface_empty_whitelist_yields_declared_only() {
    // 契约：空数组 = agent 声明零工具 → 仅声明强制工具（非断链、非全量）。
    let router = router_with_tool_descriptors_and_forced(
        &[plain_tool("bash_execute"), plain_tool("forced_demo_tool")],
        vec!["forced_demo_tool"],
    );
    let result = router
        .handle("tool-surface", "schemas", json!({"tool_ids": []}))
        .await
        .unwrap();
    assert_eq!(schema_names(&result), vec!["forced_demo_tool".to_string()]);
}

// ── G2 工具面稳定（ADR 2026-11-12）：判定/观测失败不遮挡 schema ──

/// 构造挂了多插件工具注册表 + 拦截名单的路由器（工具按描述符的 plugin_id 注册）。
fn router_with_plugin_tools_and_shadowed(
    tools: &[agentos_core::traits::ToolDescriptor],
    shadowed: Vec<String>,
) -> KernelCapabilityRouter {
    use agentos_plugin_loader::CapabilityRegistryImpl;
    let registry = Arc::new(CapabilityRegistryImpl::new());
    for t in tools {
        registry.register_tool(&t.plugin_id, t.clone());
    }
    let lookup: crate::capability_router::ShadowedPluginIdsLookupFn =
        Arc::new(move || shadowed.clone());
    KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_registry(registry)
        .with_shadowed_plugin_ids_lookup(lookup)
}

fn tool_for(name: &str, plugin_id: &str) -> agentos_core::traits::ToolDescriptor {
    agentos_core::traits::ToolDescriptor {
        name: name.to_string(),
        description: format!("test tool {name}"),
        plugin_id: plugin_id.to_string(),
        input_schema: json!({"type": "object", "properties": {}}),
        output_schema: Some(json!({"type": "object"})),
        category: agentos_core::types::ToolCategory::System,
        source: agentos_core::types::ToolSource::Mcp,
        ui: None,
        render: None,
        isolation_capable: None,
    }
}

#[tokio::test]
async fn tool_surface_keeps_declaration_mismatch_tools() {
    // ADR 2026-11-12 事故场景：task_submit 被 G2 判 sanitized（账本拒调清单
    // 命中）后，声明 schema 仍进面、契约表照常；只有调用被显式拦截。
    use agentos_plugin_loader::CapabilityRegistryImpl;
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry.register_tool(
        "task_manage_tool",
        tool_for("task_submit", "task_manage_tool"),
    );
    let rejected: crate::capability_router::RejectedToolNamesLookupFn =
        Arc::new(|| vec![("task_manage_tool".to_string(), "task_submit".to_string())]);
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_registry(registry)
        .with_rejected_tool_names_lookup(rejected);
    let result = router
        .handle(
            "tool-surface",
            "schemas",
            json!({"tool_ids": ["task_submit"]}),
        )
        .await
        .unwrap();
    assert_eq!(
        schema_names(&result),
        vec!["task_submit".to_string()],
        "G2 判拒工具的声明 schema 必须留在工具面（前缀缓存稳定）"
    );
    assert!(
        result["contracts"]
            .as_object()
            .unwrap()
            .contains_key("task_submit"),
        "契约表照常下发（output_schema/render 不被削弱）"
    );
}

#[tokio::test]
async fn tool_surface_keeps_verify_pending_plugin_tools() {
    // ADR 2026-11-12：观测失败/校验失败终态不再遮挡工具面——声明 schema 恒
    // 进面（失败桩），工具数组稳定；调用拦截在 tool-executor.invoke。
    let router = router_with_plugin_tools_and_shadowed(
        &[
            tool_for("hello_pack", "hello_pack"),
            tool_for("bash_execute", "bash"),
        ],
        vec!["hello_pack".to_string()],
    );
    let result = router
        .handle(
            "tool-surface",
            "schemas",
            json!({"tool_ids": ["hello_pack", "bash_execute"]}),
        )
        .await
        .unwrap();
    let names: std::collections::HashSet<String> = schema_names(&result).into_iter().collect();
    assert_eq!(
        names,
        ["hello_pack".to_string(), "bash_execute".to_string()]
            .into_iter()
            .collect::<std::collections::HashSet<_>>(),
        "待复验插件的工具 schema 不得被剔除（缓存稳定性）"
    );
    let contracts = result["contracts"].as_object().unwrap();
    assert!(
        contracts.contains_key("hello_pack"),
        "契约表同样保留声明条目"
    );
}

#[tokio::test]
async fn tool_surface_shadow_keeps_contracts_unfiltered() {
    // 契约表与 schema 面同源：待复验工具的输出契约照常下发（运行期校验不被削弱）。
    let router = router_with_plugin_tools_and_shadowed(
        &[tool_for("hello_pack", "hello_pack")],
        vec!["hello_pack".to_string()],
    );
    let result = router
        .handle(
            "tool-surface",
            "schemas",
            json!({"tool_ids": ["hello_pack"]}),
        )
        .await
        .unwrap();
    let contracts = result["contracts"].as_object().unwrap();
    assert!(contracts.contains_key("hello_pack"), "契约表不被校验态过滤");
}

#[tokio::test]
async fn tool_surface_stays_stable_across_ledger_clear() {
    // 转正（复验通过/账本 ok）不改变工具数组：判定前后面逐字节稳定。
    let pending = std::sync::Arc::new(std::sync::Mutex::new(vec!["hello_pack".to_string()]));
    use agentos_plugin_loader::CapabilityRegistryImpl;
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry.register_tool("hello_pack", tool_for("hello_pack", "hello_pack"));
    let pending_for_lookup = pending.clone();
    let lookup: crate::capability_router::ShadowedPluginIdsLookupFn =
        Arc::new(move || pending_for_lookup.lock().unwrap().clone());
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_registry(registry)
        .with_shadowed_plugin_ids_lookup(lookup);

    let before = router
        .handle(
            "tool-surface",
            "schemas",
            json!({"tool_ids": ["hello_pack"]}),
        )
        .await
        .unwrap();
    assert_eq!(
        schema_names(&before),
        vec!["hello_pack".to_string()],
        "待复验期间 schema 仍在面"
    );

    pending.lock().unwrap().clear();
    let after = router
        .handle(
            "tool-surface",
            "schemas",
            json!({"tool_ids": ["hello_pack"]}),
        )
        .await
        .unwrap();
    assert_eq!(
        schema_names(&after),
        vec!["hello_pack".to_string()],
        "复验通过后工具数组不变（无二次抖动）"
    );
}

#[tokio::test]
async fn tool_surface_shadow_does_not_remove_tool_even_with_force_include() {
    // 强制注入语义不变；校验态不再参与过滤——两条路径都保留声明 schema。
    use agentos_plugin_loader::CapabilityRegistryImpl;
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry.register_tool("hello_pack", tool_for("hello_pack", "hello_pack"));
    let forced: Vec<String> = vec!["hello_pack".to_string()];
    let forced_lookup: crate::capability_router::ForceIncludeToolsLookupFn =
        Arc::new(move || forced.clone());
    let shadowed_lookup: crate::capability_router::ShadowedPluginIdsLookupFn =
        Arc::new(|| vec!["hello_pack".to_string()]);
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_registry(registry)
        .with_force_include_tools_lookup(forced_lookup)
        .with_shadowed_plugin_ids_lookup(shadowed_lookup);
    let result = router
        .handle("tool-surface", "schemas", json!({"tool_ids": []}))
        .await
        .unwrap();
    assert_eq!(
        schema_names(&result),
        vec!["hello_pack".to_string()],
        "校验态不遮挡 schema（强制注入照常，工具不被剔除）"
    );
}

#[tokio::test]
async fn tool_surface_without_declaration_forces_nothing() {
    // P1-5 fail-closed：无任何 force_include_tools 声明 → 零强制注入，
    // 工具面严格等于 tool_ids 白名单（内核不再持有框架名单）。
    let router = router_with_tool_descriptors(&[plain_tool("forced_demo_tool")]);
    let result = router
        .handle("tool-surface", "schemas", json!({"tool_ids": []}))
        .await
        .unwrap();
    assert_eq!(
        schema_names(&result),
        Vec::<String>::new(),
        "无声明时零强制注入"
    );
}

/// 多插件工具注册（一行接入通配测试用：工具归属不同 plugin_id）。
fn router_with_multi_plugin_tools(pairs: &[(&str, &str)]) -> KernelCapabilityRouter {
    use agentos_plugin_loader::CapabilityRegistryImpl;
    let registry = Arc::new(CapabilityRegistryImpl::new());
    for (name, plugin) in pairs {
        let mut d = plain_tool(name);
        d.plugin_id = plugin.to_string();
        registry.register_tool(plugin, d);
    }
    KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_registry(registry)
}

#[tokio::test]
async fn tool_surface_plugin_id_whitelist_includes_all_plugin_tools() {
    // 一行接入：tool_ids 条目等于插件 id → 该插件全部工具入面（动态导入的
    // 多工具 MCP 免逐个罗列），其他插件工具不受影响。
    let router = router_with_multi_plugin_tools(&[
        ("pw_navigate", "playwright"),
        ("pw_click", "playwright"),
        ("other_tool", "other_plugin"),
    ]);
    let result = router
        .handle(
            "tool-surface",
            "schemas",
            json!({"tool_ids": ["playwright"]}),
        )
        .await
        .unwrap();
    assert_eq!(
        schema_names(&result),
        vec!["pw_click".to_string(), "pw_navigate".to_string()],
        "插件名白名单透出该插件全部工具"
    );
}

#[tokio::test]
async fn tool_surface_plugin_id_and_tool_name_whitelist_union() {
    // 插件名条目与精确工具名条目并存 = 并集（两者命中规则独立，无互斥）。
    let router = router_with_multi_plugin_tools(&[
        ("pw_navigate", "playwright"),
        ("pw_click", "playwright"),
        ("other_tool", "other_plugin"),
    ]);
    let result = router
        .handle(
            "tool-surface",
            "schemas",
            json!({"tool_ids": ["playwright", "other_tool"]}),
        )
        .await
        .unwrap();
    assert_eq!(
        schema_names(&result),
        vec![
            "other_tool".to_string(),
            "pw_click".to_string(),
            "pw_navigate".to_string()
        ],
        "插件名 + 精确名取并集"
    );
}

#[tokio::test]
async fn tool_surface_excludes_non_object_input_schema() {
    // input_schema 被改写成非 object 的工具不注入（LLM 严格校验 parameters）。
    let mut broken = plain_tool("broken_schema");
    broken.input_schema = json!("not-an-object");
    let router = router_with_tool_descriptors(&[plain_tool("ok_tool"), broken]);
    let result = router
        .handle(
            "tool-surface",
            "schemas",
            json!({"tool_ids": ["ok_tool", "broken_schema"]}),
        )
        .await
        .unwrap();
    assert_eq!(schema_names(&result), vec!["ok_tool".to_string()]);
}

#[tokio::test]
async fn tool_surface_contracts_unfiltered_by_whitelist() {
    // 契约：输出契约表不过滤——凡声明 output_schema/render 的工具都有条目，
    // 与被白名单收窄的 schema 面解耦（tool_core 按 tool_name 查表）。
    let mut contracted = plain_tool("sample_read");
    contracted.output_schema = Some(json!({"type": "object", "required": ["path"]}));
    contracted.render = Some(json!({"card": "read"}));
    contracted.category = agentos_core::types::ToolCategory::File;
    let router = router_with_tool_descriptors(&[plain_tool("legacy_tool"), contracted]);
    let result = router
        .handle(
            "tool-surface",
            "schemas",
            json!({"tool_ids": ["bash_execute"]}),
        )
        .await
        .unwrap();
    assert!(result["schemas"].as_array().unwrap().is_empty());
    let contracts = result["contracts"].as_object().unwrap();
    assert_eq!(contracts.len(), 1, "只有声明契约的工具进入: {contracts:?}");
    assert_eq!(contracts["sample_read"]["schema"]["required"][0], "path");
    assert_eq!(contracts["sample_read"]["render"]["card"], "read");
    assert!(contracts.get("legacy_tool").is_none());
}

#[tokio::test]
async fn tool_surface_contracts_carry_isolation_capable() {
    // 隔离能力声明经契约表下发（guard 查 state.tool_output_contracts 注入
    // _exec_backend）：显式 true 透传；未声明 → false（恒出现，不做键缺席）。
    let mut capable = plain_tool("bash_execute");
    capable.output_schema = Some(json!({"type": "object"}));
    capable.isolation_capable = Some(true);
    let mut plain = plain_tool("host_only");
    plain.output_schema = Some(json!({"type": "object"}));
    let router = router_with_tool_descriptors(&[capable, plain]);
    let result = router
        .handle(
            "tool-surface",
            "schemas",
            json!({"tool_ids": ["bash_execute", "host_only"]}),
        )
        .await
        .unwrap();
    let contracts = result["contracts"].as_object().unwrap();
    assert_eq!(
        contracts["bash_execute"]["isolation_capable"],
        json!(true),
        "显式声明应透传"
    );
    assert_eq!(
        contracts["host_only"]["isolation_capable"],
        json!(false),
        "未声明恒 false（guard 侧免键缺席分支）"
    );
}

#[tokio::test]
async fn tool_surface_requires_registry() {
    // registry 未注入 → 显式错误（不静默空面）。
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new());
    let err = router
        .handle("tool-surface", "schemas", json!({"tool_ids": ["x"]}))
        .await
        .unwrap_err();
    assert!(err.to_string().contains("registry not injected"));
}

// ── B6：pipeline-state.update DB 批量失败 → 内存不留新值（先 DB 后内存写序）──

/// upsert_state_fields 恒故障的存储 mock。其余必需方法以 unreachable! 桩实现
/// ——update 失败路径若意外耦合其他存储调用会在此炸出（比静默成功更诚实）。
struct FailingBatchStore;

#[async_trait::async_trait]
impl StorageBackend for FailingBatchStore {
    async fn upsert_state_fields(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _fields: &serde_json::Map<String, serde_json::Value>,
    ) -> Result<(), agentos_core::types::StorageError> {
        Err(agentos_core::types::StorageError::Database(
            "injected batch upsert failure".to_string(),
        ))
    }
    async fn get_run(
        &self,
        _run_id: &str,
    ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn get_messages_by_pipeline(
        &self,
        _pipeline_id: &str,
        _opts: MessageQueryOpts,
    ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn append_trace(
        &self,
        _entry: TraceEntry,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn store_blob(
        &self,
        _data: &[u8],
        _mime_type: &str,
    ) -> Result<String, agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn create_session(
        &self,
        _session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn get_session(
        &self,
        _thread_id: &str,
    ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn list_sessions(
        &self,
        _filter: agentos_core::traits::SessionListFilter,
    ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn update_session(
        &self,
        _session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn delete_session(
        &self,
        _thread_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn link_pipeline_session(
        &self,
        _pipeline_id: &str,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn list_pipeline_ids_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn get_step_traces_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn get_step_traces_by_pipeline(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn create_user(
        &self,
        _user: &agentos_core::types::UserRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn get_user_by_id(
        &self,
        _user_id: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn get_user_by_username(
        &self,
        _username: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn list_users(
        &self,
    ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn update_last_login(
        &self,
        _user_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
    async fn update_user_password(
        &self,
        _user_id: &str,
        _password_hash: &str,
        _must_change_password: bool,
    ) -> Result<bool, agentos_core::types::StorageError> {
        unreachable!("mock 不提供口令更新")
    }
    async fn delete_user(&self, _user_id: &str) -> Result<bool, agentos_core::types::StorageError> {
        unreachable!("pipeline-state.update 失败路径不应触碰其他存储方法")
    }
}

#[tokio::test]
async fn test_pipeline_state_update_db_failure_leaves_memory_untouched() {
    // B6 写序倒置回归：DB 批量失败 → 整体报错且内存 registry 不留新值
    //（旧写序内存先行，DB 首键失败 = 内存新值 + DB 半套，重启后旧值复活）。
    let tenant = format!("tenant_b6_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_b6_{}", uuid::Uuid::new_v4().simple());
    let store: std::sync::Arc<dyn StorageBackend> = std::sync::Arc::new(FailingBatchStore);
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store)
        .with_export_fields_lookup(Arc::new(|| {
            crate::capability_router::ExportFields::from_manifests(&[test_task_export_manifest()])
        }));
    // 预置 registry 条目（热路径写面存在，失败时必须保持原值）
    let reg = agentos_session::pipeline_state_registry::global_registry();
    reg.get_or_init(
        &tenant,
        &pid,
        "th_b6",
        "agentos",
        json!({"pipeline_id": pid, "task.status": "pending"}),
    );

    let r = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_b6"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": TEST_STATE_WRITER,
                "pipeline_id": pid,
                "fields": {"task.status": "completed", "task.ended_at": "2026-09-11T00:00:00Z"},
            }),
        ),
    )
    .await;
    assert!(
        r.is_err(),
        "DB 批量失败必须整体报错（能力调用错误），实际 {r:?}"
    );
    // 内存不得留新值：task.status 仍是出生值 pending
    let entry = reg.get(&tenant, &pid).expect("registry 应有条目");
    let st = entry.read();
    assert_eq!(
        st.state["task.status"], "pending",
        "DB 失败后内存必须保持原值（先 DB 后内存写序）: {}",
        st.state
    );
    assert!(
        st.state.get("task.ended_at").is_none(),
        "DB 失败后内存不得有任何新键: {}",
        st.state
    );
    reg.remove(&tenant, &pid);
}

// ═══════════════════════════════════════════════════════════════════════
// 覆盖率补测（内核 Rust 覆盖率战役 2026-09-13）：未覆盖分支的行为面。
// 断行为（输入 → 响应信封/存储副作用/事件帧），mock 仅用于内核边界
// （invoker/存储），路由逻辑全部走真实代码路径。
// ═══════════════════════════════════════════════════════════════════════

// ── pipeline-executor.delete_pipeline：任务删除语义 ──────────────────────

#[tokio::test]
async fn delete_pipeline_removes_runs_and_registry_entries() {
    let tenant = format!("tenant_del_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_del_{}", uuid::Uuid::new_v4().simple());
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store.clone());

    agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_del"),
        async {
            store
                .record_run_start(&pid, &tenant, "run_del_1", "h", "2026-10-15T00:00:00+00:00")
                .await
                .unwrap();
            store
                .link_pipeline_session(&pid, "th_del", &tenant)
                .await
                .unwrap();
            let reg = agentos_session::pipeline_state_registry::global_registry();
            reg.get_or_init(
                &tenant,
                &pid,
                "th_del",
                "agentos",
                json!({"task.status": "running"}),
            );

            let res = router
                .handle(
                    "pipeline-executor",
                    "delete_pipeline",
                    json!({"pipeline_id": pid}),
                )
                .await
                .unwrap();
            assert_eq!(res["status"], "deleted");
            assert_eq!(res["pipeline_id"], json!(pid.clone()));

            // 内存注册表条目随删除逐出（删除清单含本管道）
            assert!(reg.get(&tenant, &pid).is_none(), "删除后注册表条目应逐出");

            // 幂等：再删一次仍 ok（无记录也是 deleted）
            let again = router
                .handle(
                    "pipeline-executor",
                    "delete_pipeline",
                    json!({"pipeline_id": pid}),
                )
                .await
                .unwrap();
            assert_eq!(again["status"], "deleted");
        },
    )
    .await;
}

#[tokio::test]
async fn delete_pipeline_param_and_store_guards() {
    // store 未注入 → 显式报错
    let router_no_store = KernelCapabilityRouter::with_metrics(MetricsAggregator::new());
    assert!(router_no_store
        .handle(
            "pipeline-executor",
            "delete_pipeline",
            json!({"pipeline_id": "p"})
        )
        .await
        .is_err());

    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(sqlite as Arc<dyn StorageBackend>);
    // 缺 pipeline_id / 空串
    assert!(router
        .handle("pipeline-executor", "delete_pipeline", json!({}))
        .await
        .is_err());
    assert!(router
        .handle(
            "pipeline-executor",
            "delete_pipeline",
            json!({"pipeline_id": ""})
        )
        .await
        .is_err());
}

// ── tenant-context.get：多租户上下文查询（F-TENANT-B-KERNEL）────────────

#[tokio::test]
async fn tenant_context_get_returns_default_without_scope() {
    let router = router_plain();
    let res = router
        .handle("tenant-context", "get", json!({}))
        .await
        .unwrap();
    assert_eq!(res["tenant_id"], "default", "无 task_local 时回退 default");
    assert_eq!(res["session_id"], "");
}

#[tokio::test]
async fn tenant_context_get_returns_scoped_context() {
    let router = router_plain();
    let res = agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_ctx_x", "sess_ctx_9"),
        router.handle("tenant-context", "get", json!({})),
    )
    .await
    .unwrap();
    assert_eq!(res["tenant_id"], "tenant_ctx_x");
    assert_eq!(res["session_id"], "sess_ctx_9");
}

// ── known_namespaces / 动态 handler 注册表路由（M2/M4）───────────────────

/// 测试用动态 capability handler：echo 固定信封（验证注册表委托路径）。
struct EchoHandler {
    ns: &'static str,
}
#[async_trait::async_trait]
impl agentos_mcp::CapabilityHandler for EchoHandler {
    fn namespace(&self) -> &str {
        self.ns
    }
    async fn handle(&self, method: &str, _params: Value) -> Result<Value, McpError> {
        Ok(json!({"echoed": format!("{}.{method}", self.ns)}))
    }
}

#[tokio::test]
async fn handler_registry_routes_dynamic_namespace_before_builtin() {
    let reg = Arc::new(agentos_mcp::CapabilityHandlerRegistry::new());
    reg.register(Arc::new(EchoHandler {
        ns: "custom-interact",
    }));
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_handler_registry(reg);

    // 动态 namespace 命中 → 委托 handler（内核内置 match 不参与）
    let res = router
        .handle("custom-interact", "wait_for_choice", json!({"x": 1}))
        .await
        .unwrap();
    assert_eq!(res["echoed"], "custom-interact.wait_for_choice");

    // 内置 namespace 不受影响（registry miss → 走内置 match）
    let tenant_res = router
        .handle("tenant-context", "get", json!({}))
        .await
        .unwrap();
    assert_eq!(tenant_res["tenant_id"], "default");
}

#[tokio::test]
async fn known_namespaces_merges_dynamic_and_builtin_with_dedupe() {
    // 内置 STANDARD_CAPABILITIES 恒在；动态 namespace 追加；重复 namespace 去重
    let reg = Arc::new(agentos_mcp::CapabilityHandlerRegistry::new());
    reg.register(Arc::new(EchoHandler { ns: "event-bus" })); // 与内置重复
    reg.register(Arc::new(EchoHandler { ns: "custom-ns-1" }));
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_handler_registry(reg);

    let ns = router.known_namespaces();
    for builtin in agentos_mcp::STANDARD_CAPABILITIES {
        assert!(
            ns.iter().any(|n| n == builtin),
            "内置 {builtin} 必须在声明面"
        );
    }
    assert!(
        ns.iter().any(|n| n == "custom-ns-1"),
        "动态 namespace 必须合并"
    );
    assert_eq!(
        ns.iter().filter(|n| *n == "event-bus").count(),
        1,
        "重复 namespace 只声明一次"
    );
}

#[tokio::test]
async fn known_namespaces_without_registry_is_builtin_only() {
    let router = router_plain();
    let ns = router.known_namespaces();
    assert_eq!(ns.len(), agentos_mcp::STANDARD_CAPABILITIES.len());
}

// ── 兜底臂：未注册 / 未实现的 capability.method ─────────────────────────

#[tokio::test]
async fn unhandled_capability_method_returns_protocol_error() {
    let router = router_with_store();
    // 各内建 namespace 的未知 method + 完全未知 namespace → 兜底臂统一报错
    for (cap, m) in [
        ("pipeline-executor", "nope"),
        ("event-bus", "nope"),
        ("pipeline-state", "nope"),
        ("transient", "nope"),
        ("no-such-capability", "x"),
    ] {
        let err = router
            .handle(cap, m, json!({}))
            .await
            .expect_err("未实现组合必须报错");
        let msg = format!("{err}");
        assert!(
            msg.contains("not implemented"),
            "{cap}.{m} 应落兜底臂，实际: {msg}"
        );
    }
}

// ── metrics.record 参数校验矩阵（监控设计 §十 注入防护）─────────────────

#[tokio::test]
async fn metrics_record_param_validation_matrix() {
    let (router, agg) = router_with_metrics();
    // 缺 value / value 非数值 → 报错
    assert!(router
        .handle("metrics", "record", json!({"_plugin_id":"p","name":"m"}))
        .await
        .is_err());
    assert!(router
        .handle(
            "metrics",
            "record",
            json!({"_plugin_id":"p","name":"m","value":"not-a-number"})
        )
        .await
        .is_err());
    // label key 过长（>256）
    let long_key = "k".repeat(257);
    assert!(router
        .handle(
            "metrics",
            "record",
            json!({"_plugin_id":"p","name":"m","value":1.0,"labels":{long_key:"v"}})
        )
        .await
        .is_err());
    // label value 过长（>256）
    let long_val = "v".repeat(257);
    assert!(router
        .handle(
            "metrics",
            "record",
            json!({"_plugin_id":"p","name":"m","value":1.0,"labels":{"k":long_val}})
        )
        .await
        .is_err());
    // 双引号（Prometheus 导出安全）
    assert!(router
        .handle(
            "metrics",
            "record",
            json!({"_plugin_id":"p","name":"m","value":1.0,"labels":{"k":"a\"b"}})
        )
        .await
        .is_err());
    // labels 非 object（数组）→ 宽泛放行为空 labels，不报错
    let ok = router
        .handle(
            "metrics",
            "record",
            json!({"_plugin_id":"p","name":"m_array_labels","value":2.0,"labels":[1,2]}),
        )
        .await
        .unwrap();
    assert_eq!(ok["status"], "recorded");
    // metric_type 缺省 = counter；unit/help 可省
    let _ = router
        .handle(
            "metrics",
            "record",
            json!({"_plugin_id":"p","name":"m_defaults","value":3.0}),
        )
        .await
        .unwrap();
    let views = agg.query(Some("p"), Some("m_defaults"), None, &Labels::new());
    assert_eq!(views.len(), 1, "缺省类型照常入库");
}

// ── event-bus.emit 分族路由：session 未接线 / 信封不完整 / 各事件族 ─────

#[tokio::test]
async fn stream_family_without_session_reports_emitted() {
    // session 未接线：引擎照常执行、无前端播报——视为 emitted（非错误）
    let router = router_plain();
    let res = router
        .handle(
            "event-bus",
            "emit",
            json!({
                "event": "stream_chunk",
                "payload": {
                    "thread_id": "t",
                    "pipeline_id": "p",
                    "message_id": "m",
                    "content": "hi",
                },
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "emitted");
}

#[tokio::test]
async fn stream_family_incomplete_envelope_dropped() {
    // 0.1 协议信封缺 message_id → 前端解析不出会丢事件，显式拒绝
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());
    let res = router
        .handle(
            "event-bus",
            "emit",
            json!({
                "event": "stream_chunk",
                "payload": {"thread_id": "thread-1", "pipeline_id": "pipe-1", "content": "hi"},
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "dropped");
    assert_eq!(res["reason"], "incomplete envelope");
    assert!(received.lock().unwrap().is_empty(), "坏信封不得推前端");
}

#[tokio::test]
async fn stream_chunk_empty_content_dropped_but_contentless_events_pass() {
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());
    // stream_chunk content 空 → dropped
    let dropped = router
        .handle(
            "event-bus",
            "emit",
            json!({
                "event": "stream_chunk",
                "payload": {
                    "thread_id": "thread-1",
                    "pipeline_id": "pipe-1",
                    "message_id": "m1",
                    "content": "",
                },
            }),
        )
        .await
        .unwrap();
    assert_eq!(dropped["status"], "dropped");
    assert_eq!(dropped["reason"], "empty content");

    // thinking_start/thinking_end/stream_end 无 content 字段（needs_content=false）
    // → 照常发射，且 data 不伪造 content 键
    for event in ["thinking_start", "thinking_end", "stream_end"] {
        let res = router
            .handle(
                "event-bus",
                "emit",
                json!({
                    "event": event,
                    "payload": {
                        "thread_id": "thread-1",
                        "pipeline_id": "pipe-1",
                        "message_id": "m2",
                    },
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "emitted", "{event} 无 content 也应发射");
    }
    let msgs = received.lock().unwrap().clone();
    let thinking_start = msgs
        .iter()
        .find(|f| f["type"] == "thinking_start")
        .expect("thinking_start 帧应到达 sink");
    assert!(
        thinking_start["data"].get("content").is_none(),
        "无 content 事件不得伪造 content 键"
    );
}

#[tokio::test]
async fn tool_multimedia_result_forwarded_like_tool_family() {
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());
    let res = router
        .handle(
            "event-bus",
            "emit",
            json!({
                "event": "tool_multimedia_result",
                "payload": {
                    "thread_id": "thread-1",
                    "pipeline_id": "pipe-1",
                    "message_id": "m3",
                    "call_id": "call_mm",
                    "tool_name": "screenshot",
                    "media_type": "image/png",
                },
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "emitted");
    let msgs = received.lock().unwrap().clone();
    assert_eq!(msgs.len(), 1);
    assert_eq!(msgs[0]["type"], "tool_multimedia_result");
    assert_eq!(msgs[0]["data"]["media_type"], "image/png");
    assert_eq!(msgs[0]["data"]["_threadId"], "thread-1");
}

// ── 兜底透传：插件自定义事件 + approval.created 域广播 ──────────────────

#[tokio::test]
async fn custom_event_passthrough_injects_route_keys() {
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());
    let res = router
        .handle(
            "event-bus",
            "emit",
            json!({
                "event": "widget_feedback",
                "payload": {
                    "thread_id": "thread-1",
                    "widget_id": "w1",
                    "choice": "a",
                },
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "emitted");
    let msgs = received.lock().unwrap().clone();
    assert_eq!(msgs.len(), 1);
    assert_eq!(msgs[0]["type"], "widget_feedback");
    // 透传业务字段 + 补路由键（缺 pipeline_id/message_id 以空串占位）
    assert_eq!(msgs[0]["data"]["widget_id"], "w1");
    assert_eq!(msgs[0]["data"]["pipeline_id"], "");
    assert_eq!(msgs[0]["data"]["_threadId"], "thread-1");
}

#[tokio::test]
async fn custom_event_without_thread_or_session_dropped() {
    // 有 session 但 thread_id 空 → dropped（no session or empty thread_id）
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());
    let res = router
        .handle("event-bus", "emit", json!({"event": "e1", "payload": {}}))
        .await
        .unwrap();
    assert_eq!(res["status"], "dropped");
    assert!(res["reason"]
        .as_str()
        .unwrap()
        .contains("no session or empty thread_id"));
    assert!(received.lock().unwrap().is_empty());

    // 无 session → 同样 dropped（不报错）
    let res2 = router_plain()
        .handle(
            "event-bus",
            "emit",
            json!({"event": "e2", "payload": {"thread_id": "t"}}),
        )
        .await
        .unwrap();
    assert_eq!(res2["status"], "dropped");
}

#[tokio::test]
async fn approval_created_broadcasts_domain_tags_with_null_placeholders() {
    // approval.created 双腿：前端 WS 透传 + 域事件总线同步广播（缺失 tag 以 Null 占位）
    let broadcasted: DomainSink = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink = broadcasted.clone();
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let coord = Arc::new(agentos_session::SessionCoordinator::default());
    let capture = Arc::new(CaptureSink::with_received(received.clone()))
        as Arc<dyn agentos_session::EventSink>;
    coord.register("user-test", capture);
    coord.register_thread("thread-1", "user-test");
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_session(coord)
        .with_domain_broadcaster(Arc::new(
            move |name: &str, tags: Vec<(String, serde_json::Value)>| {
                sink.lock().unwrap().push((name.to_string(), tags));
            },
        ));
    let res = router
        .handle(
            "event-bus",
            "emit",
            json!({
                "event": "approval.created",
                "payload": {
                    "thread_id": "thread-1",
                    "request_id": "req-9",
                    // run_id/pipeline_id 缺失 → 广播 tag 以 Null 占位（derive 同款）
                },
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "emitted");

    let got = broadcasted.lock().unwrap();
    assert_eq!(got.len(), 1, "approval.created 必须同步广播域事件");
    assert_eq!(got[0].0, "approval.created");
    let tag = |k: &str| {
        got[0]
            .1
            .iter()
            .find(|(tk, _)| tk == k)
            .map(|(_, v)| v.clone())
            .unwrap_or(serde_json::Value::Null)
    };
    assert_eq!(tag("request_id"), json!("req-9"));
    assert_eq!(
        tag("run_id"),
        serde_json::Value::Null,
        "缺失 tag 以 Null 占位"
    );

    // 前端透传腿照常
    assert_eq!(received.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn domain_mirror_routing_follows_single_source_list() {
    // 单源跟随证明（K3，ADR 2026-10-10）：镜像分流名单取自
    // config/kernel/ws_events.yaml domain_mirrors 节——名单里是什么名，域总线
    // 就收到什么名（行为跟随单源，而非硬编码巧合通过）。
    for name in crate::ws_events::domain_mirrors() {
        let broadcasted: DomainSink = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let sink = broadcasted.clone();
        let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
            .with_domain_broadcaster(Arc::new(
                move |n: &str, tags: Vec<(String, serde_json::Value)>| {
                    sink.lock().unwrap().push((n.to_string(), tags));
                },
            ));
        let res = router
            .handle(
                "event-bus",
                "emit",
                json!({"event": name, "payload": {"thread_id": "t"}}),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "dropped", "无 session 装配时透传腿 dropped");
        let got = broadcasted.lock().unwrap();
        assert_eq!(got.len(), 1, "镜像事件必须广播进域总线: {name}");
        assert_eq!(&got[0].0, name, "域总线事件名必须与名单一致: {name}");
    }
}

// ── registry.register_tool：descriptor 构造 + 信封分支补齐 ──────────────

#[tokio::test]
async fn register_tool_descriptor_construction_matrix() {
    use std::sync::Mutex;
    // 非法 category → System 兜底；description 缺省值；output_schema/ui/render 透传
    let captured: Arc<Mutex<Vec<agentos_core::traits::ToolDescriptor>>> =
        Arc::new(Mutex::new(Vec::new()));
    let sink = captured.clone();
    let registrar: DynamicToolRegistrar = Arc::new(move |_pid, tool| {
        sink.lock().unwrap().push(tool);
        Ok(())
    });
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_dynamic_tool_registrar(registrar);
    let out = router
        .handle(
            "registry",
            "register_tool",
            json!({
                "_plugin_id": "connector",
                "name": "dyn_tool",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object", "required": ["r"]},
                "render": {"card": "dyn"},
                "ui": {"icon": "bolt"},
                "category": "bogus-category",
            }),
        )
        .await
        .unwrap();
    assert_eq!(out["status"], "registered");
    let tools_len = {
        let tools = captured.lock().unwrap();
        let tool = &tools[0];
        assert_eq!(
            tool.category,
            agentos_core::types::ToolCategory::System,
            "未知 category 兜底 System"
        );
        assert_eq!(
            tool.description, "dynamically registered tool",
            "缺省 description"
        );
        assert_eq!(tool.output_schema.as_ref().unwrap()["required"][0], "r");
        assert_eq!(tool.render.as_ref().unwrap()["card"], "dyn");
        assert_eq!(tool.ui.as_ref().unwrap()["icon"], "bolt");
        assert_eq!(tool.source, agentos_core::types::ToolSource::Dynamic);
        // 空串 category 同走 System（合法档）
        tools.len()
    };
    let _ = router
        .handle(
            "registry",
            "register_tool",
            json!({"_plugin_id": "connector", "name": "dyn2", "category": ""}),
        )
        .await
        .unwrap();
    assert_eq!(
        captured.lock().unwrap()[tools_len].category,
        agentos_core::types::ToolCategory::System
    );
}

#[tokio::test]
async fn register_tool_registrar_error_propagates_as_protocol_error() {
    let registrar: DynamicToolRegistrar =
        Arc::new(|_pid, _tool| Err("插件未启用（enablement 闸）".to_string()));
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_dynamic_tool_registrar(registrar);
    let err = router
        .handle(
            "registry",
            "register_tool",
            json!({"_plugin_id": "p", "name": "x"}),
        )
        .await
        .unwrap_err();
    let msg = format!("{err}");
    assert!(
        msg.contains("拒绝") && msg.contains("enablement"),
        "注册器失败原因透传: {msg}"
    );
}

#[tokio::test]
async fn register_tool_success_broadcasts_widget_schema_changed() {
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let coord = Arc::new(agentos_session::SessionCoordinator::default());
    let capture = Arc::new(CaptureSink::with_received(received.clone()))
        as Arc<dyn agentos_session::EventSink>;
    coord.register("user-any", capture); // 广播给全部活跃连接，无需 thread 注册
    let registrar: DynamicToolRegistrar = Arc::new(|_, _| Ok(()));
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_session(coord)
        .with_dynamic_tool_registrar(registrar);
    let out = router
        .handle(
            "registry",
            "register_tool",
            json!({"_plugin_id": "connector", "name": "dyn_query"}),
        )
        .await
        .unwrap();
    assert_eq!(out["status"], "registered");
    let msgs = received.lock().unwrap().clone();
    let widget = msgs
        .iter()
        .find(|f| f["type"] == "widget_event")
        .expect("注册成功应广播 schema 工具面刷新事件");
    assert_eq!(widget["data"]["widget_id"], "schema");
    assert_eq!(widget["data"]["event"], "changed");
    assert_eq!(widget["data"]["data"]["plugin_id"], "connector");
    assert_eq!(widget["data"]["data"]["source"], "dynamic_register");
}

// ── service-registry：messages 域 / 参数守卫 / pipeline-runs 过滤 ────────

#[tokio::test]
async fn service_registry_messages_list_with_query_opts() {
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store.clone());
    store
        .apply_messages_ops_to_table(
            "pipe_msgs",
            "default",
            &[
                json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "q1"}}),
                json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "a1"}}),
                json!({"op": "set", "seq": 2, "msg": {"role": "user", "content": "q2"}}),
            ],
        )
        .await
        .unwrap();

    // 无 opts → 全部 3 条
    let all = router
        .handle(
            "service-registry",
            "messages.list",
            json!({"pipeline_id": "pipe_msgs"}),
        )
        .await
        .unwrap();
    assert_eq!(all.as_array().unwrap().len(), 3);

    // limit=2 → 最新 2 条（尾锚定窗口，升序返回）
    let tail = router
        .handle(
            "service-registry",
            "messages.list",
            json!({"pipeline_id": "pipe_msgs", "limit": 2}),
        )
        .await
        .unwrap();
    let seqs: Vec<u32> = tail
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["seq_in_branch"].as_u64().unwrap() as u32)
        .collect();
    assert_eq!(seqs, vec![1, 2], "尾锚定窗口取最新 limit 条且保持升序");

    // after_sequence 游标（断线补漏）
    let after = router
        .handle(
            "service-registry",
            "messages.list",
            json!({"pipeline_id": "pipe_msgs", "after_sequence": 0, "limit": 10}),
        )
        .await
        .unwrap();
    assert_eq!(after.as_array().unwrap().len(), 2, "游标之后的消息");

    // 缺 pipeline_id → 报错
    assert!(router
        .handle("service-registry", "messages.list", json!({}))
        .await
        .is_err());
}

#[tokio::test]
async fn service_registry_param_guards() {
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store);
    // method 不带点（<domain>.<op> 形态违约）
    assert!(router
        .handle("service-registry", "messages", json!({}))
        .await
        .is_err());
    // traces.list 缺 thread_id
    assert!(router
        .handle("service-registry", "traces.list", json!({}))
        .await
        .is_err());
    // traces.list_by_pipeline 缺 pipeline_id
    assert!(router
        .handle("service-registry", "traces.list_by_pipeline", json!({}))
        .await
        .is_err());
    // pipeline-runs.list_by_pipeline 缺 pipeline_id
    assert!(router
        .handle(
            "service-registry",
            "pipeline-runs.list_by_pipeline",
            json!({})
        )
        .await
        .is_err());
}

#[tokio::test]
async fn service_registry_pipeline_runs_list_with_status_filter_and_limit_cap() {
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store.clone());
    // ADR 2026-09-18：运行投影 = pipeline_state 运行簿记键（每管道恰一条）。
    // 三管道三种状态，覆盖 status 过滤与 limit 封顶两个消费面。
    for (pipe, run_id, status_str) in [
        ("pipe_f1", "run_f1", "running"),
        ("pipe_f2", "run_f2", "completed"),
        ("pipe_f3", "run_f3", "failed"),
    ] {
        store
            .record_run_start(pipe, "default", run_id, "h", "2026-10-15T00:00:00+00:00")
            .await
            .unwrap();
        if status_str != "running" {
            store
                .upsert_state_fields(
                    pipe,
                    "default",
                    &json!({ "run_status": status_str })
                        .as_object()
                        .unwrap()
                        .clone(),
                )
                .await
                .unwrap();
        }
    }

    // status 过滤
    let running = router
        .handle(
            "service-registry",
            "pipeline-runs.list",
            json!({"status": "running"}),
        )
        .await
        .unwrap();
    let rows = running.as_array().unwrap();
    assert_eq!(rows.len(), 1, "状态过滤只留 running");
    assert_eq!(rows[0]["run_id"], "run_f1");

    // limit 传超大值 → 被 500 封顶（性质断言：返回行数 ≤ 500）
    let huge = router
        .handle(
            "service-registry",
            "pipeline-runs.list",
            json!({"limit": 99999}),
        )
        .await
        .unwrap();
    assert!(
        huge.as_array().unwrap().len() <= 500,
        "limit 必须被 500 封顶"
    );
    assert_eq!(huge.as_array().unwrap().len(), 3);
}

// ── 内核能力契约入口校验（定义驱动，validate_params 经 handle 接线）──────

fn router_with_real_contracts() -> KernelCapabilityRouter {
    let contracts = Arc::new(
        crate::kernel_capabilities::load_contracts(
            &std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../../config/kernel/kernel_capabilities"),
        )
        .expect("仓库契约必须可加载"),
    );
    KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_capability_contracts(contracts)
}

#[tokio::test]
async fn capability_contract_entry_validation_rejects_malformed_params() {
    let router = router_with_real_contracts();
    // chat.send_message 契约 required: [message, user_id] → 缺参在入口被拒
    let err = router
        .handle("chat", "send_message", json!({}))
        .await
        .expect_err("required 缺失必须被契约校验拒绝");
    assert!(
        format!("{err}").contains("契约校验失败"),
        "拒绝信息应指向契约校验: {err}"
    );
    // pattern 档：thread_id 必须 ^thread- 前缀（pipeline 坐标互填抓红）
    let err2 = router
        .handle(
            "chat",
            "send_message",
            json!({"message": "m", "user_id": "u", "thread_id": "c1b2c3d4e5f6"}),
        )
        .await;
    assert!(err2.is_err(), "^thread- pattern 违约必须拒绝");
}

#[tokio::test]
async fn capability_contract_valid_params_fall_through_to_route() {
    let router = router_with_real_contracts();
    // 参数合法 → 校验放行；chat namespace 内核未路由 → 落兜底臂（非校验错误）
    let err = router
        .handle(
            "chat",
            "send_message",
            json!({"message": "m", "user_id": "u"}),
        )
        .await
        .expect_err("chat 未在内核路由，应落兜底");
    let msg = format!("{err}");
    assert!(
        msg.contains("not implemented") && !msg.contains("契约校验失败"),
        "合法参数应通过校验后落兜底臂: {msg}"
    );
    // 未声明契约的 (namespace, method) → 宽泛放行（既有行为不变）
    let res = router
        .handle("tenant-context", "get", json!({}))
        .await
        .unwrap();
    assert_eq!(res["tenant_id"], "default");
}

// ── tool-surface.schemas：tool_ids 畸形条目容错 ─────────────────────────

#[tokio::test]
async fn tool_surface_ignores_non_string_tool_id_entries() {
    let router = router_with_tool_descriptors(&[plain_tool("ok_tool")]);
    let result = router
        .handle(
            "tool-surface",
            "schemas",
            json!({"tool_ids": [123, null, true, "ok_tool"]}),
        )
        .await
        .unwrap();
    assert_eq!(
        schema_names(&result),
        vec!["ok_tool".to_string()],
        "非字符串条目忽略，字符串条目照常命中"
    );
}

// ── pipeline-state.update 参数守卫补齐 ─────────────────────────────────

#[tokio::test]
async fn pipeline_state_update_param_guards() {
    let router = router_with_store();
    // 缺 pipeline_id
    assert!(router
        .handle("pipeline-state", "update", json!({"fields": {"task.a": 1}}))
        .await
        .is_err());
    // 缺 fields
    assert!(router
        .handle("pipeline-state", "update", json!({"pipeline_id": "p"}))
        .await
        .is_err());
    // fields 非 object
    assert!(router
        .handle(
            "pipeline-state",
            "update",
            json!({"pipeline_id": "p", "fields": [1, 2]})
        )
        .await
        .is_err());
}

// ── suspend/resume 参数与装配守卫 ──────────────────────────────────────

#[tokio::test]
async fn suspend_resume_param_and_store_guards() {
    let router_no_store = KernelCapabilityRouter::with_metrics(MetricsAggregator::new());
    assert!(router_no_store
        .handle("pipeline-executor", "suspend", json!({"run_id": "r"}))
        .await
        .is_err());
    assert!(router_no_store
        .handle("pipeline-executor", "resume", json!({"run_id": "r"}))
        .await
        .is_err());

    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store.clone());
    // 缺 run_id
    assert!(router
        .handle("pipeline-executor", "suspend", json!({}))
        .await
        .is_err());
    assert!(router
        .handle("pipeline-executor", "resume", json!({}))
        .await
        .is_err());
    // run 不存在：suspend 走 get_run 报错
    assert!(router
        .handle("pipeline-executor", "suspend", json!({"run_id": "ghost"}))
        .await
        .is_err());
    // resume 同样 fail-closed：runs 表退役后 resume 先按 run_id 反查运行投影，
    // 无归属即显式协议错误（静默 ok resumed 会掩盖伪造/已清理的凭据）
    let err = router
        .handle("pipeline-executor", "resume", json!({"run_id": "ghost"}))
        .await
        .expect_err("不存在的 run 必须显式报错（fail-closed）");
    assert!(
        format!("{err}").contains("resume 失败"),
        "resume ghost 须以协议错误出口: {err}"
    );

    // suspend 幂等：第二次挂起已挂起 run 直接返回句柄，不再改写
    store
        .record_run_start(
            "pipe_idem",
            "default",
            "run_idem",
            "h",
            "2026-10-15T00:00:00+00:00",
        )
        .await
        .unwrap();
    let first = router
        .handle(
            "pipeline-executor",
            "suspend",
            json!({"run_id": "run_idem"}),
        )
        .await
        .unwrap();
    assert_eq!(first["status"], "suspended");
    let second = router
        .handle(
            "pipeline-executor",
            "suspend",
            json!({"run_id": "run_idem"}),
        )
        .await
        .unwrap();
    assert_eq!(second["run_id"], "run_idem");
    assert_eq!(second["status"], "suspended", "已挂起 run 幂等返回当前句柄");
}

#[tokio::test]
async fn resume_pipeline_param_and_resumer_error_guards() {
    // 缺 pipeline_id / store 未注入 → 报错
    let router_no_store = KernelCapabilityRouter::with_metrics(MetricsAggregator::new());
    assert!(router_no_store
        .handle(
            "pipeline-executor",
            "resume_pipeline",
            json!({"pipeline_id": "p"})
        )
        .await
        .is_err());
    let router = router_with_store();
    assert!(router
        .handle("pipeline-executor", "resume_pipeline", json!({}))
        .await
        .is_err());

    // 续跑派发闭包失败 → 错误透传（"续跑派发失败"）
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let failing = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_pipeline_resumer(Arc::new(|_p, _t, _u, _o| {
            Box::pin(async { Err("引擎排空拒绝".to_string()) })
                as std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), String>> + Send>>
        }));
    let mut rerr_ctx = agentos_core::types::TenantContext::new("tenant_rerr", "thread_rerr");
    rerr_ctx.user_id = Some("u_rerr".to_string());
    agentos_tenant::scope(rerr_ctx, async {
        let tenant = agentos_tenant::current_or_default("default").tenant_id;
        store
            .record_run_start(
                "pipe_rerr",
                &tenant,
                "run_rerr",
                "h",
                "2026-10-15T00:00:00+00:00",
            )
            .await
            .unwrap();
        store
            .upsert_state_fields(
                "pipe_rerr",
                &tenant,
                &json!({ "run_status": "suspended" })
                    .as_object()
                    .unwrap()
                    .clone(),
            )
            .await
            .unwrap();
        store
            .link_pipeline_session("pipe_rerr", "thread_rerr", &tenant)
            .await
            .unwrap();
        let err = failing
            .handle(
                "pipeline-executor",
                "resume_pipeline",
                json!({"pipeline_id": "pipe_rerr"}),
            )
            .await
            .unwrap_err();
        assert!(
            format!("{err}").contains("续跑派发失败"),
            "派发闭包错误须透传: {err}"
        );
    })
    .await;
}

// ── frontend.emit：session 未接线 / event 缺省 ─────────────────────────

#[tokio::test]
async fn frontend_emit_without_session_reports_dropped_not_error() {
    let router = router_plain();
    let res = router
        .handle(
            "frontend",
            "emit",
            json!({"event": "cost_update", "payload": {"thread_id": "t"}}),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "dropped", "无 session 无法投递，非错误");
    // event 缺省 → "unknown"
    let res2 = router
        .handle("frontend", "emit", json!({"payload": {"thread_id": "t"}}))
        .await
        .unwrap();
    assert_eq!(res2["event"], "unknown");
}

// ═══════════════════════════════════════════════════════════════════
// 覆盖率补测批：流式契约网关 / 事件族丢弃 / 挂起恢复翻译 / 工具执行告警
// ═══════════════════════════════════════════════════════════════════

/// 流式声明闸：插件声明了 `events` 清单但事件不在清单内 → 拒绝（留痕）。
/// 与「未声明 capabilities.streaming」是两条独立分支，前者声明了但越清单。
#[tokio::test]
async fn streaming_gate_event_outside_declared_list_emits_diagnostics() {
    let (guard, logs) = crate::test_env::capture_logs();
    let _ = &guard;
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let decl = agentos_core::traits::StreamingCapability {
        conduit: false,
        events: Some(vec!["stream_start".to_string()]),
        part_types: None,
        persist: None,
    };
    let router = router_with_streaming_gate(received.clone(), Some(decl));
    let res = router
        .handle(
            "event-bus",
            "emit",
            json!({
                "_plugin_id": "my_streamer",
                "event": "stream_chunk",
                "payload": {
                    "thread_id": "thread-1",
                    "pipeline_id": "c1b2c3d4e5f64789abcdef0123456789",
                    "message_id": "p_chunk_001",
                    "content": "hi",
                },
            }),
        )
        .await
        .unwrap();
    let text = logs.text();
    assert_eq!(res["status"], "dropped");
    assert_eq!(res["reason"], "event not in declared events");
    assert!(received.lock().unwrap().is_empty());
    assert!(
        text.contains("不在 capabilities.streaming.events 声明内"),
        "越清单拒绝须留痕: {text}"
    );
}

/// 未声明 streaming 的插件发射流式事件 → dropped + 留痕（fail-closed 取证）。
#[tokio::test]
async fn streaming_gate_undeclared_plugin_emits_diagnostics() {
    let (guard, logs) = crate::test_env::capture_logs();
    let _ = &guard;
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_streaming_gate(received.clone(), None);
    let res = router
        .handle(
            "event-bus",
            "emit",
            json!({
                "_plugin_id": "no_decl",
                "event": "stream_chunk",
                "payload": {"thread_id": "thread-1", "pipeline_id": "p1",
                            "message_id": "m1", "content": "x"},
            }),
        )
        .await
        .unwrap();
    let text = logs.text();
    assert_eq!(res["status"], "dropped");
    assert_eq!(res["reason"], "capabilities.streaming not declared");
    assert!(
        text.contains("插件未声明 capabilities.streaming"),
        "未声明拒绝须留痕: {text}"
    );
}

/// 流式信封不完整（缺 message_id）→ 丢弃 + 留痕；reason 指明 incomplete envelope。
#[tokio::test]
async fn stream_family_incomplete_envelope_emits_diagnostics() {
    let (guard, logs) = crate::test_env::capture_logs();
    let _ = &guard;
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());
    let res = router
        .handle(
            "event-bus",
            "emit",
            json!({
                "event": "stream_chunk",
                "payload": {
                    "thread_id": "thread-1",
                    "pipeline_id": "p_envelope",
                    "content": "hi",
                },
            }),
        )
        .await
        .unwrap();
    let text = logs.text();
    assert_eq!(res["status"], "dropped");
    assert_eq!(res["reason"], "incomplete envelope");
    assert!(received.lock().unwrap().is_empty());
    assert!(
        text.contains("流式事件信封不完整"),
        "信封不完整须留痕: {text}"
    );
}

/// 流式 chunk 内容为空（needs_content 事件）→ 丢弃 + 留痕；thinking_start
/// 等无 content 事件不受该闸影响。
#[tokio::test]
async fn stream_chunk_empty_content_dropped_with_diagnostics() {
    let (guard, logs) = crate::test_env::capture_logs();
    let _ = &guard;
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());
    let res = router
        .handle(
            "event-bus",
            "emit",
            json!({
                "event": "stream_chunk",
                "payload": {
                    "thread_id": "thread-1",
                    "pipeline_id": "p_empty_content",
                    "message_id": "m_empty",
                    "content": "",
                },
            }),
        )
        .await
        .unwrap();
    let text = logs.text();
    assert_eq!(res["status"], "dropped");
    assert_eq!(res["reason"], "empty content");
    assert!(received.lock().unwrap().is_empty());
    assert!(
        text.contains("流式事件被丢弃（content 空）"),
        "空内容丢弃须留痕: {text}"
    );
}

/// 交互事件缺 thread_id → 丢弃 + 留痕（前端 useInteractionHandler 无路由键
/// 会渲染到错误会话，宁丢不误投）。
#[tokio::test]
async fn interaction_without_thread_dropped_with_diagnostics() {
    let (guard, logs) = crate::test_env::capture_logs();
    let _ = &guard;
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());
    let res = router
        .handle(
            "event-bus",
            "emit",
            json!({
                "event": "interaction_request",
                "payload": {"request_id": "r1", "kind": "approval"},
            }),
        )
        .await
        .unwrap();
    let text = logs.text();
    assert_eq!(res["status"], "dropped");
    assert!(received.lock().unwrap().is_empty());
    assert!(
        text.contains("交互事件被丢弃（thread_id 空）"),
        "交互事件丢弃须留痕: {text}"
    );
}

/// 自定义事件缺 thread_id → dropped + reason 指明（无 session 或空 thread）。
#[tokio::test]
async fn custom_event_without_thread_reports_drop_reason_with_diagnostics() {
    let (guard, logs) = crate::test_env::capture_logs();
    let _ = &guard;
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());
    let res = router
        .handle(
            "event-bus",
            "emit",
            json!({"event": "widget_feedback", "payload": {"value": 1}}),
        )
        .await
        .unwrap();
    let text = logs.text();
    assert_eq!(res["status"], "dropped");
    assert_eq!(res["reason"], "no session or empty thread_id");
    assert!(
        text.contains("透传事件被丢弃（thread_id 空）"),
        "自定义事件丢弃须留痕: {text}"
    );
}

/// frontend.emit 缺 thread_id（payload 与 params 顶层都无）→ dropped + 留痕。
#[tokio::test]
async fn frontend_emit_without_thread_dropped_with_diagnostics() {
    let (guard, logs) = crate::test_env::capture_logs();
    let _ = &guard;
    let router = router_plain();
    let res = router
        .handle(
            "frontend",
            "emit",
            json!({"event": "tool_progress", "payload": {"percent": 50}}),
        )
        .await
        .unwrap();
    let text = logs.text();
    assert_eq!(res["status"], "dropped");
    assert!(
        text.contains("frontend.emit 被丢弃（thread_id 空）"),
        "frontend.emit 丢弃须留痕: {text}"
    );
}

/// tool-executor.invoke 缺 tool_name → 协议错误（参数校验前置）。
#[tokio::test]
async fn tool_executor_invoke_missing_tool_name_rejected() {
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new());
    let err = router
        .handle("tool-executor", "invoke", json!({"args": {}}))
        .await
        .expect_err("缺 tool_name 必须报错");
    assert!(
        format!("{err}").contains("缺少 tool_name"),
        "错误文案须指明缺参: {err}"
    );
}

/// 缺会话身份（_owner / session_id 皆无）→ 告警留痕但照常执行（fallback 链
/// 由工具插件侧兜底；不阻断是有意设计）。
#[tokio::test]
async fn tool_executor_missing_session_identity_warns_but_executes() {
    let (guard, logs) = crate::test_env::capture_logs();
    let _ = &guard;
    let seen: std::sync::Arc<std::sync::Mutex<Vec<(String, String, Value)>>> =
        std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_invoker(Arc::new(CaptureInvoker::with_captured(seen.clone())));
    let res = router
        .handle(
            "tool-executor",
            "invoke",
            json!({"tool_name": "noop_tool", "args": {"x": 1}, "plugin_id": "p_owner"}),
        )
        .await
        .unwrap();
    let text = logs.text();
    assert_eq!(res["success"], true, "告警不得阻断执行: {res}");
    assert_eq!(seen.lock().unwrap().len(), 1, "工具调用照常发出");
    assert!(
        text.contains("缺少会话身份"),
        "会话身份缺失须告警留痕: {text}"
    );
}

/// 工具连续失败达阈值 → error 级留痕（"同一工具 100% 失败"必须可捞）。
#[tokio::test]
async fn tool_executor_consecutive_failures_emit_error_log() {
    use crate::tools::{ToolFailureAlert, ToolFailureTracker, FAILURE_ALERT_THRESHOLD};

    struct FailingInvoker;
    #[async_trait::async_trait]
    impl agentos_core::traits::PluginInvoker for FailingInvoker {
        async fn invoke_pipeline_plugin<'a>(
            &self,
            _p: &str,
            _c: &agentos_core::types::PluginContext<'a>,
        ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
            unreachable!("只走工具调用")
        }
        async fn send_lifecycle_hook(
            &self,
            _p: &str,
            _h: agentos_core::traits::LifecycleHook,
            _c: &agentos_core::traits::HookContext,
        ) -> Result<(), agentos_core::types::PluginError> {
            unreachable!("只走工具调用")
        }
        async fn invoke_tool(
            &self,
            _p: &str,
            _t: &str,
            _i: &Value,
        ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError>
        {
            Ok(agentos_core::types::ToolExecutionResult {
                success: false,
                data: json!({}),
                error: Some("schema validation failed".into()),
                duration_ms: None,
                metadata: None,
            })
        }
    }

    /// 计数达阈值即产出告警的追踪器（与生产 ConsecutiveFailureTracker 语义同形）。
    struct CountingTracker {
        count: std::sync::Mutex<u32>,
    }
    impl ToolFailureTracker for CountingTracker {
        fn record(
            &self,
            tool_name: &str,
            success: bool,
            _sample: &str,
        ) -> Option<ToolFailureAlert> {
            if success {
                return None;
            }
            let mut n = self.count.lock().unwrap();
            *n += 1;
            if *n >= FAILURE_ALERT_THRESHOLD {
                return Some(ToolFailureAlert {
                    tool_name: tool_name.to_string(),
                    consecutive: *n,
                    error_sample: "schema validation failed".to_string(),
                    since_secs: 3,
                });
            }
            None
        }
    }

    let (guard, logs) = crate::test_env::capture_logs();
    let _ = &guard;
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_invoker(Arc::new(FailingInvoker))
        .with_tool_failure_tracker(Arc::new(CountingTracker {
            count: std::sync::Mutex::new(0),
        }));
    let params = json!({"tool_name": "flaky_tool", "args": {}, "plugin_id": "p_flaky"});
    for _ in 0..FAILURE_ALERT_THRESHOLD {
        let _ = router
            .handle("tool-executor", "invoke", params.clone())
            .await
            .unwrap();
    }
    let text = logs.text();
    assert!(
        text.contains("工具连续失败"),
        "达阈值必须 error 留痕（否则流水日志淹没）: {text}"
    );
    assert!(text.contains("flaky_tool"), "留痕须含工具名: {text}");
}

// ── pipeline-executor：挂起/恢复各失败翻译 ─────────────────────────────

/// suspend_pipeline / resume_pipeline / delete_pipeline / get_run_status 的
/// 参数与装配守卫：缺参、store 未注入都要显式报错（不得静默 no-op）。
#[tokio::test]
async fn pipeline_executor_param_and_store_guards_are_explicit() {
    let no_store = KernelCapabilityRouter::with_metrics(MetricsAggregator::new());
    // 缺 pipeline_id
    for (method, params) in [
        ("suspend_pipeline", json!({})),
        ("resume_pipeline", json!({})),
        ("delete_pipeline", json!({})),
        ("delete_pipeline", json!({"pipeline_id": ""})),
        ("get_run_status", json!({})),
    ] {
        let err = no_store
            .handle("pipeline-executor", method, params)
            .await
            .expect_err("缺参必须报错");
        let msg = format!("{err}");
        assert!(msg.contains("缺少"), "{method} 缺参文案须指明: {msg}");
    }
    // store 未注入（参数合法）
    for (method, params) in [
        ("suspend_pipeline", json!({"pipeline_id": "p1"})),
        ("resume_pipeline", json!({"pipeline_id": "p1"})),
        ("delete_pipeline", json!({"pipeline_id": "p1"})),
        ("get_run_status", json!({"run_id": "r1"})),
    ] {
        let err = no_store
            .handle("pipeline-executor", method, params)
            .await
            .expect_err("store 未注入必须报错");
        let msg = format!("{err}");
        assert!(
            msg.contains("store not injected"),
            "{method} 装配缺失文案须可诊断: {msg}"
        );
    }
}

/// suspend_pipeline：无匹配 run（全部终态或从未运行）→ 幂等 ok 且 run_id 为空
/// （task_manage stop 对已结束任务是 no-op，不是错误）。
#[tokio::test]
async fn suspend_pipeline_without_active_run_is_idempotent_ok() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store.clone());
    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_sp_noop", "th_sp_noop"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            store
                .record_run_start(
                    "pipe_sp_done",
                    &tenant,
                    "run_done",
                    "h",
                    "2026-10-15T00:00:00+00:00",
                )
                .await
                .unwrap();
            sqlite
                .set_run_status_projection(
                    "pipe_sp_done",
                    &tenant,
                    agentos_core::types::RunStatus::Completed,
                )
                .unwrap();

            let res = router
                .handle(
                    "pipeline-executor",
                    "suspend_pipeline",
                    json!({"pipeline_id": "pipe_sp_done"}),
                )
                .await
                .unwrap();
            assert_eq!(res["status"], "suspended");
            assert_eq!(res["run_id"], "", "无在飞 run 时 run_id 为空（幂等）");
            // 终态 run 状态不被改写
            let got = store.get_run("run_done").await.unwrap();
            assert_eq!(got.status, agentos_core::types::RunStatus::Completed);
        },
    )
    .await;
}

/// resume_pipeline：pipeline_id 无会话挂载（孤儿/伪造）→ 显式拒绝派发
/// （静默跑完会把回复发到无人订阅的频道）。
#[tokio::test]
async fn resume_pipeline_without_session_link_is_rejected() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let dispatched: Arc<DispatchedResumes> = Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink = dispatched.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_pipeline_resumer(Arc::new(
            move |pipeline_id: String,
                  thread_id: String,
                  user_id: String,
                  state_overlay: Option<serde_json::Value>| {
                let sink = sink.clone();
                Box::pin(async move {
                    sink.lock()
                        .unwrap()
                        .push((pipeline_id, thread_id, user_id, state_overlay));
                    Ok(())
                })
                    as std::pin::Pin<
                        Box<dyn std::future::Future<Output = Result<(), String>> + Send>,
                    >
            },
        ));

    let err = router
        .handle(
            "pipeline-executor",
            "resume_pipeline",
            json!({"pipeline_id": "pipe_orphan"}),
        )
        .await
        .expect_err("无会话挂载必须拒绝");
    assert!(
        format!("{err}").contains("不属于任何会话"),
        "拒绝文案须指明孤儿 id: {err}"
    );
    assert!(dispatched.lock().unwrap().is_empty(), "拒绝时不得派发续跑");
}

/// resume_pipeline：恢复派发未装配 → 降级纯簿记（翻 Running）+ 留痕，
/// 返回 dispatched=false（诚实反映"没拉起执行"）。
#[tokio::test]
async fn resume_pipeline_without_resumer_degrades_with_diagnostics() {
    let (guard, logs) = crate::test_env::capture_logs();
    let _ = &guard;
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store.clone());
    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_nobo", "th_nobo"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            store
                .record_run_start(
                    "pipe_nobo",
                    &tenant,
                    "run_nobo",
                    "h",
                    "2026-10-15T00:00:00+00:00",
                )
                .await
                .unwrap();
            sqlite
                .set_run_status_projection(
                    "pipe_nobo",
                    &tenant,
                    agentos_core::types::RunStatus::Suspended,
                )
                .unwrap();
            store
                .link_pipeline_session("pipe_nobo", "th_nobo", &tenant)
                .await
                .unwrap();

            let res = router
                .handle(
                    "pipeline-executor",
                    "resume_pipeline",
                    json!({"pipeline_id": "pipe_nobo"}),
                )
                .await
                .unwrap();
            assert_eq!(res["dispatched"], false, "未装配派发须诚实标注");
            assert_eq!(res["run_id"], "run_nobo");
            let got = store.get_run("run_nobo").await.unwrap();
            assert_eq!(
                got.status,
                agentos_core::types::RunStatus::Running,
                "降级路径翻 Running 簿记"
            );
        },
    )
    .await;
    let text = logs.text();
    assert!(
        text.contains("恢复派发未装配，降级纯簿记"),
        "降级须留痕: {text}"
    );
}

/// suspend：带 request_id 时落挂起凭据进 runs.metadata（审批挂起恢复链写侧）；
/// 无 request_id 时 metadata 不带该键。
#[tokio::test]
async fn suspend_records_pending_interaction_credential() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_sqlite(sqlite.clone());
    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_pc", "th_pc"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            for (pipe, run_id, with_request) in [
                ("pipe_pc_yes", "run_pc_yes", true),
                ("pipe_pc_no", "run_pc_no", false),
            ] {
                store
                    .record_run_start(pipe, &tenant, run_id, "h", "2026-10-15T00:00:00+00:00")
                    .await
                    .unwrap();
                let mut params = json!({"run_id": run_id});
                if with_request {
                    params["request_id"] = json!("req-123");
                }
                let res = router
                    .handle("pipeline-executor", "suspend", params)
                    .await
                    .unwrap();
                assert_eq!(res["status"], "suspended");
                let got = sqlite.get_run(run_id).await.unwrap();
                let meta = got.metadata.unwrap_or_else(|| json!({}));
                assert_eq!(
                    meta.get("pending_interaction_request_id")
                        .and_then(|v| v.as_str()),
                    with_request.then_some("req-123"),
                    "挂起凭据按 request_id 在场与否落库（run={run_id}）"
                );
            }
        },
    )
    .await;
}

/// resume：清挂起凭据（陈旧 pending_interaction_request_id 会让后续反查
/// 误判仍在等审批）。
#[tokio::test]
async fn resume_clears_pending_interaction_credential() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_sqlite(sqlite.clone());
    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_rc", "th_rc"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            store
                .record_run_start(
                    "pipe_rc",
                    &tenant,
                    "run_rc",
                    "h",
                    "2026-10-15T00:00:00+00:00",
                )
                .await
                .unwrap();
            router
                .handle(
                    "pipeline-executor",
                    "suspend",
                    json!({"run_id": "run_rc", "request_id": "req-rc"}),
                )
                .await
                .unwrap();
            assert_eq!(
                sqlite
                    .get_run("run_rc")
                    .await
                    .unwrap()
                    .metadata
                    .and_then(|m| m.get("pending_interaction_request_id").cloned()),
                Some(json!("req-rc")),
                "挂起后凭据在场"
            );

            let res = router
                .handle("pipeline-executor", "resume", json!({"run_id": "run_rc"}))
                .await
                .unwrap();
            assert_eq!(res["status"], "resumed");
            // 恢复必须清挂起凭据（state 键 suspend_request_id 置空）：按凭据
            // 反查不再命中，后续 interaction_response 不会误判仍在等审批
            let cleared = sqlite.find_suspended_run_by_request_id("req-rc").unwrap();
            assert!(cleared.is_none(), "恢复后挂起凭据必须清除（反查不得命中）");
        },
    )
    .await;
}

/// get_run_status：命中返回 run 记录本体；未命中报错（编码/查询失败同面）。
#[tokio::test]
async fn get_run_status_returns_record_or_errors() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store.clone());
    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_grs", "th_grs"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            store
                .record_run_start(
                    "pipe_grs",
                    &tenant,
                    "run_grs",
                    "hash_grs",
                    "2026-10-15T00:00:00+00:00",
                )
                .await
                .unwrap();
            let res = router
                .handle(
                    "pipeline-executor",
                    "get_run_status",
                    json!({"run_id": "run_grs"}),
                )
                .await
                .unwrap();
            assert_eq!(res["run_id"], "run_grs");
            assert_eq!(
                res["config_hash"], "",
                "run_config_hash 已退役，get_run_status 投影恒空串"
            );

            let err = router
                .handle(
                    "pipeline-executor",
                    "get_run_status",
                    json!({"run_id": "run_ghost"}),
                )
                .await
                .expect_err("不存在的 run 必须报错");
            assert!(
                format!("{err}").contains("get_run_status 失败"),
                "查询失败文案须可诊断: {err}"
            );
        },
    )
    .await;
}

/// delete_pipeline：删除该管道执行数据并逐出内存 registry；无记录时幂等 ok。
#[tokio::test]
async fn delete_pipeline_removes_data_and_is_idempotent() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store.clone());
    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_del", "th_del"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            let pid = format!("pipe_del_{}", uuid::Uuid::new_v4().simple());
            agentos_session::pipeline_state_registry::global_registry().get_or_init(
                &tenant,
                &pid,
                "th_del",
                "agentos",
                json!({"pipeline_id": pid, "status": "completed"}),
            );
            store
                .record_run_start(&pid, &tenant, "run_del", "h", "2026-10-15T00:00:00+00:00")
                .await
                .unwrap();
            store
                .link_pipeline_session(&pid, "th_del", &tenant)
                .await
                .unwrap();

            let res = router
                .handle(
                    "pipeline-executor",
                    "delete_pipeline",
                    json!({"pipeline_id": pid}),
                )
                .await
                .unwrap();
            assert_eq!(res["status"], "deleted");
            assert_eq!(res["pipeline_id"], pid);
            assert!(
                store
                    .get_thread_id_by_pipeline(&pid)
                    .await
                    .unwrap()
                    .is_none(),
                "删除后 pipeline_sessions 映射应清空"
            );
            assert!(
                agentos_session::pipeline_state_registry::global_registry()
                    .get(&tenant, &pid)
                    .is_none(),
                "内存 registry 条目应被逐出"
            );

            // 幂等：再删一次不报错
            let again = router
                .handle(
                    "pipeline-executor",
                    "delete_pipeline",
                    json!({"pipeline_id": pid}),
                )
                .await
                .unwrap();
            assert_eq!(again["status"], "deleted", "无记录重删仍返回 ok");
        },
    )
    .await;
}

/// unhandled capability：未注册的 capability.method → 协议错误 + 结构化留痕
/// （ns/method/调用方；运行时对账兜底，资源注册收敛 T5.5）。
#[tokio::test]
async fn unhandled_capability_emits_diagnostics() {
    let (guard, logs) = crate::test_env::capture_logs();
    let _ = &guard;
    let router = router_plain();
    let err = router
        .handle("retired-namespace", "do_thing", json!({"k": "v"}))
        .await
        .expect_err("未注册 capability 必须报错");
    let text = logs.text();
    assert!(
        format!("{err}").contains("capability method not implemented"),
        "兜底文案须指明未实现: {err}"
    );
    assert!(
        text.contains("service_unresolved: capability=retired-namespace method=do_thing"),
        "未解析调用须结构化留痕: {text}"
    );
}

/// unhandled capability 观测：service_unresolved 计数按调用方归属进聚合器
/// （T5.5 观测定名；调用方取 invoker 注入的 `_plugin_id` 信任锚点）。
#[tokio::test]
async fn unhandled_capability_records_service_unresolved_metric() {
    let (router, agg) = router_with_metrics();
    let err = router
        .handle(
            "ghost-ns",
            "ghost_method",
            json!({"_plugin_id": "task_service", "k": "v"}),
        )
        .await
        .expect_err("未注册 capability 必须报错");
    assert!(format!("{err}").contains("ghost-ns.ghost_method"));
    let views = agg.query(
        Some("task_service"),
        Some("service_unresolved"),
        None,
        &Labels::new(),
    );
    assert_eq!(views.len(), 1, "service_unresolved series 应按调用方归属");
    let total: f64 = views[0].samples.iter().map(|s| s.value).sum();
    assert!(
        (total - 1.0).abs() < f64::EPSILON,
        "单次未解析调用计 1.0: {total}"
    );
}

// ── session.create 能力臂（日程承载兜底，ADR 2026-11-03-pim-trigger-anchor）──

#[tokio::test]
async fn session_create_with_injected_identity_creates_thread_and_pipeline() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let coord = Arc::new(agentos_session::SessionCoordinator::default());
    let broadcasted: Arc<std::sync::Mutex<Vec<String>>> =
        Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink = broadcasted.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_session(coord)
        .with_domain_broadcaster(Arc::new(
            move |event: &str, _tags: Vec<(String, serde_json::Value)>| {
                sink.lock().unwrap().push(event.to_string());
            },
        ));

    let mut ctx = agentos_core::types::TenantContext::new("t_sc", "th_sc");
    ctx.user_id = Some("u_sc".to_string());
    agentos_tenant::scope(ctx, async {
        let r = router
            .handle(
                "session",
                "create",
                json!({"title": "日程承载管道", "agent_id": "agentos"}),
            )
            .await
            .unwrap();
        let thread_id = r["thread_id"].as_str().unwrap_or_default().to_string();
        let pipeline_id = r["pipeline_ids"][0]
            .as_str()
            .unwrap_or_default()
            .to_string();
        assert!(thread_id.starts_with("thread-"), "应返回 thread 坐标");
        assert_eq!(pipeline_id.len(), 12, "主管道 id = 12 位 hex（统一格式）");

        // pipeline_sessions 映射在账（触发器注入的派发坐标前提）
        let mapped = store.get_thread_id_by_pipeline(&pipeline_id).await.unwrap();
        assert_eq!(
            mapped.as_deref(),
            Some(thread_id.as_str()),
            "主管道必须挂真实会话（孤儿管道拒绝派发的既有守卫）"
        );
        // 会话行落库（跨重启恢复面）
        let sess = store.get_session(&thread_id).await.unwrap();
        assert!(sess.is_some(), "sessions 表必须落行");
        assert!(
            broadcasted
                .lock()
                .unwrap()
                .contains(&"session.created".to_string()),
            "session.created 域事件必须广播（观察者契约）"
        );
    })
    .await;
}

#[tokio::test]
async fn session_create_without_identity_fails_fast() {
    // 反调面身份契约（同 resume_pipeline 刀3）：无注入 user 显式拒绝，
    // 不得以空串/anonymous 落库。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let coord = Arc::new(agentos_session::SessionCoordinator::default());
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store.clone())
        .with_session(coord);

    let err = router
        .handle("session", "create", json!({"title": "x"}))
        .await
        .unwrap_err();
    assert!(
        format!("{err}").contains("未携带用户身份"),
        "fail-fast 文案应区分「未携带身份」: {err}"
    );
}

// ── P-23 片1：pipeline-executor.resume 恢复即清挂起归因键 ──

#[tokio::test]
async fn test_pipeline_executor_resume_clears_suspended_reason() {
    // 恢复成功即清 suspended_reason（与 suspend_request_id 同批同款）：
    // 陈旧归因残留会让恢复后的行被误读为仍处该次挂起。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store)
        .with_sqlite(sqlite.clone());
    sqlite
        .record_run_start(
            "pipe_rz",
            "default",
            "rz",
            "hash",
            "2026-11-04T00:00:00+00:00",
        )
        .unwrap();
    // 模拟 drain 排空落库后的形态：suspended + 归因键在位
    sqlite
        .set_run_status_projection(
            "pipe_rz",
            "default",
            agentos_core::types::RunStatus::Suspended,
        )
        .unwrap();
    sqlite
        .upsert_state_field(
            "pipe_rz",
            "default",
            "suspended_reason",
            &json!("test: drain reason"),
        )
        .unwrap();

    let out = router
        .handle("pipeline-executor", "resume", json!({"run_id": "rz"}))
        .await
        .unwrap();
    assert_eq!(out["status"], "resumed");

    let fields = sqlite.load_pipeline_state("pipe_rz", "default").unwrap();
    assert_eq!(
        fields.get("run_status"),
        Some(&json!("running")),
        "run 翻回 running"
    );
    assert_eq!(
        fields.get("suspended_reason"),
        Some(&serde_json::Value::Null),
        "恢复即清挂起归因（防跨恢复残留）"
    );
}

// ── state.writes 调用方身份归约（合宿宿主键 → 成员并集）───────────────

#[test]
fn state_writes_for_caller_resolves_group_host_union() {
    // 反调身份=进程身份：独占=插件 id，合宿=宿主键。合宿宿主的写必须归约到
    // 成员声明并集，否则成员插件全员被判无持久写权限（task.ws_meta 实证）。
    let entries = vec![
        ("alpha".to_string(), vec!["task.alpha".to_string()]),
        ("beta".to_string(), vec!["task.ws_meta".to_string()]),
        ("gamma".to_string(), vec!["other.key".to_string()]),
    ];
    let host_key_of = |id: &str| match id {
        "alpha" | "beta" => "group:light:2".to_string(),
        other => format!("plugin:{other}"),
    };

    // 独占：直命中 manifest.id
    assert_eq!(
        state_writes_for_caller(&entries, host_key_of, "gamma"),
        vec!["other.key".to_string()]
    );
    // 合宿宿主键：成员声明并集
    let mut group = state_writes_for_caller(&entries, host_key_of, "group:light:2");
    group.sort();
    assert_eq!(
        group,
        vec!["task.alpha".to_string(), "task.ws_meta".to_string()]
    );
    // 未知调用方：空声明（fail-closed）
    assert!(state_writes_for_caller(&entries, host_key_of, "group:none:1").is_empty());
    assert!(state_writes_for_caller(&entries, host_key_of, "ghost").is_empty());
}

// ── pipeline-state.update delete_keys：键真删（审计单一路径形态）──────

/// delete_keys 测试辅助：先在内存 registry 注册管道（模拟 run 出生），再经
/// capability update 真实播种（DB + 内存双落点，与生产链路同构）。
async fn seed_state_via_update(
    router: &KernelCapabilityRouter,
    tenant: &str,
    thread: &str,
    pid: &str,
    fields: serde_json::Value,
) {
    let reg = agentos_session::pipeline_state_registry::global_registry();
    reg.get_or_init(tenant, pid, thread, "agentos", json!({"pipeline_id": pid}));
    let r = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(tenant, thread),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": TEST_STATE_WRITER,
                "pipeline_id": pid,
                "fields": fields,
            }),
        ),
    )
    .await;
    assert!(r.is_ok(), "播种 update 应成功: {r:?}");
}

#[tokio::test]
async fn test_pipeline_state_update_delete_keys_single_and_multi() {
    // AC1+AC2+AC5：update 带 delete_keys 删单/多键——DB 行消失、内存键消失、
    // list 不再返回 task.ended_at（export 白名单成员，断言非空转）；删后写
    // 任意字段，被删键不复活（内存同步缺失的特征缺陷）。
    let tenant = format!("tenant_del_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_del_{}", uuid::Uuid::new_v4().simple());
    let router = router_with_store();
    let store = router.store.as_ref().unwrap().clone();
    seed_state_via_update(
        &router,
        &tenant,
        "th_del",
        &pid,
        json!({
            "approval.tickets.t1": {"id": "t1"},
            "approval.tickets.t2": {"id": "t2"},
            "task.status": "running",
            "task.ended_at": "2026-11-20T00:00:00Z",
        }),
    )
    .await;
    // 播种自检：DB 确有行（防"断言在空库上空转"）
    let seeded = store.load_pipeline_state(&pid, &tenant).await.unwrap();
    assert_eq!(seeded.len(), 4, "播种必须真实落 DB: {seeded:?}");

    // 删多键（含一个不存在的键：幂等进 not_found，不报错）
    let r = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_del"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": TEST_STATE_WRITER,
                "pipeline_id": pid,
                "delete_keys": ["approval.tickets.t1", "approval.tickets.t2",
                                "task.ended_at", "approval.never_existed"],
            }),
        ),
    )
    .await;
    let out = r.expect("delete_keys 删除应成功");
    let mut deleted: Vec<&str> = out["deleted"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    deleted.sort();
    assert_eq!(
        deleted,
        vec![
            "approval.tickets.t1",
            "approval.tickets.t2",
            "task.ended_at"
        ]
    );
    let not_found: Vec<&str> = out["not_found"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(
        not_found,
        vec!["approval.never_existed"],
        "不存在的键幂等报 not_found 不报错"
    );

    // DB 行消失（真实落过 DB 再删，断言非空转）
    let fields = store.load_pipeline_state(&pid, &tenant).await.unwrap();
    assert!(!fields.contains_key("approval.tickets.t1"), "DB 行必须消失");
    assert!(!fields.contains_key("approval.tickets.t2"), "DB 行必须消失");
    assert!(!fields.contains_key("task.ended_at"), "DB 行必须消失");
    assert!(fields.contains_key("task.status"), "未删键不受影响");

    // 内存键消失（复活坑的直接防线：内存不删，下次写会把旧值刷回 DB）
    let reg = agentos_session::pipeline_state_registry::global_registry();
    let entry = reg.get(&tenant, &pid).unwrap();
    {
        let st = entry.read();
        assert!(
            st.state.get("approval.tickets.t1").is_none(),
            "内存键必须消失"
        );
        assert!(st.state.get("task.ended_at").is_none(), "内存键必须消失");
        assert_eq!(st.state["task.status"], "running");
    }

    // list 不再返回被删键（task.ended_at 是 export 白名单成员，断言非空转）
    let rows = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_del"),
        router.handle("pipeline-state", "list", json!({})),
    )
    .await
    .unwrap();
    let row = rows
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r.get("pipeline_id").and_then(|v| v.as_str()) == Some(pid.as_str()))
        .expect("list 应包含该管道");
    assert!(row.get("task.ended_at").is_none(), "list 不再返回被删键");
    assert_eq!(row["task.status"], "running");

    // 复活检查（AC5 关键）：删后对同管道写任意字段，被删键不得复活
    let r2 = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_del"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": TEST_STATE_WRITER,
                "pipeline_id": pid,
                "fields": {"task.status": "completed"},
            }),
        ),
    )
    .await;
    assert!(r2.is_ok());
    let fields2 = store.load_pipeline_state(&pid, &tenant).await.unwrap();
    assert!(
        !fields2.contains_key("approval.tickets.t1"),
        "被删键不得随后续写操作复活（内存同步缺失的特征缺陷）: {fields2:?}"
    );
    assert!(!fields2.contains_key("task.ended_at"), "被删键不得复活");
    let st2 = entry.read();
    assert!(
        st2.state.get("approval.tickets.t1").is_none(),
        "内存侧也不得复活"
    );
    assert_eq!(st2.state["task.status"], "completed");
}

#[tokio::test]
async fn test_pipeline_state_update_delete_keys_fails_closed_on_undeclared() {
    // AC4：未声明的键整批拒绝（fail-closed，无部分删除半态）；未声明插件一律拒绝。
    let tenant = format!("tenant_del2_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_del2_{}", uuid::Uuid::new_v4().simple());
    let router = router_with_store();
    let store = router.store.as_ref().unwrap().clone();
    seed_state_via_update(
        &router,
        &tenant,
        "th_del2",
        &pid,
        json!({
            "approval.tickets.t1": {"id": "t1"},
            "task.status": "running",
        }),
    )
    .await;

    // 混合批次：approval.* 已声明、other.domain.key 未声明 → 整批拒绝
    let r = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_del2"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": TEST_STATE_WRITER,
                "pipeline_id": pid,
                "delete_keys": ["approval.tickets.t1", "other.domain.key"],
            }),
        ),
    )
    .await;
    let err = r.expect_err("未声明键必须整批拒绝（fail-closed）");
    assert!(err.to_string().contains("state.writes"), "{err}");

    // 无部分删除半态：声明过的键也不许在被拒批次里被删掉
    let fields = store.load_pipeline_state(&pid, &tenant).await.unwrap();
    assert!(
        fields.contains_key("approval.tickets.t1"),
        "被拒批次不得部分生效"
    );
    assert!(fields.contains_key("task.status"));

    // 无声明插件：一律拒绝且不留痕迹
    let r2 = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_del2"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": "undeclared_plugin",
                "pipeline_id": pid,
                "delete_keys": ["approval.tickets.t1"],
            }),
        ),
    )
    .await;
    assert!(r2.is_err(), "未声明插件删除必须拒绝: {r2:?}");
    let fields2 = store.load_pipeline_state(&pid, &tenant).await.unwrap();
    assert!(fields2.contains_key("approval.tickets.t1"));
}

#[tokio::test]
async fn test_pipeline_state_update_delete_keys_tenant_isolation() {
    // AC6：租户隔离——只能删本租户的键，其他租户同 pid 同名键不动。
    let tenant = format!("tenant_del3_{}", uuid::Uuid::new_v4().simple());
    let other = format!("tenant_del3o_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_del3_{}", uuid::Uuid::new_v4().simple());
    let router = router_with_store();
    let store = router.store.as_ref().unwrap().clone();
    seed_state_via_update(
        &router,
        &tenant,
        "th_del3",
        &pid,
        json!({"approval.tickets.t1": {"id": "mine"}}),
    )
    .await;
    seed_state_via_update(
        &router,
        &other,
        "th_del3o",
        &pid,
        json!({"approval.tickets.t1": {"id": "theirs"}}),
    )
    .await;

    let r = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_del3"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": TEST_STATE_WRITER,
                "pipeline_id": pid,
                "delete_keys": ["approval.tickets.t1"],
            }),
        ),
    )
    .await;
    assert!(r.is_ok(), "本租户删除应成功: {r:?}");

    let mine = store.load_pipeline_state(&pid, &tenant).await.unwrap();
    assert!(!mine.contains_key("approval.tickets.t1"), "本租户键已删");
    let theirs = store.load_pipeline_state(&pid, &other).await.unwrap();
    assert_eq!(
        theirs.get("approval.tickets.t1"),
        Some(&json!({"id": "theirs"})),
        "其他租户同 pid 同名键不得被删"
    );
}

#[tokio::test]
async fn test_pipeline_state_update_delete_keys_writes_audit_trace() {
    // AC7（审计核心）：删除经 update 单一路径，traces 表留下可追溯记录——
    // patch_type=state_update、patch_data.action=delete_keys + deleted_keys、
    // 发起方 plugin_id、目标 pipeline_id、时间戳（TraceEntry.created_at）。
    let tenant = format!("tenant_del4_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_del4_{}", uuid::Uuid::new_v4().simple());
    let router = router_with_store();
    let store = router.store.as_ref().unwrap().clone();
    seed_state_via_update(
        &router,
        &tenant,
        "th_del4",
        &pid,
        json!({"approval.tickets.t1": {"id": "t1"}}),
    )
    .await;

    let r = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_del4"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": TEST_STATE_WRITER,
                "pipeline_id": pid,
                "delete_keys": ["approval.tickets.t1"],
            }),
        ),
    )
    .await;
    assert!(r.is_ok(), "删除应成功: {r:?}");

    // 从 traces 读回（真实链路：append_trace → SQLite → 读面）
    let traces = store
        .get_step_traces_by_pipeline(&pid, &tenant)
        .await
        .unwrap();
    let del_trace = traces
        .iter()
        .find(|t| {
            t.patch_data.get("action").and_then(|a| a.as_str()) == Some("delete_keys")
                && t.patch_data.get("deleted_keys").is_some_and(|dk| {
                    dk.as_array()
                        .map(|a| a.iter().any(|k| k.as_str() == Some("approval.tickets.t1")))
                        .unwrap_or(false)
                })
        })
        .expect("删除操作必须在 traces 留下可追溯记录");
    assert_eq!(
        del_trace.patch_type,
        agentos_core::types::PatchType::StateUpdate
    );
    assert_eq!(del_trace.plugin_id, TEST_STATE_WRITER, "审计须记录发起方");
    assert_eq!(del_trace.pipeline_id, pid, "审计须记录目标管道");
    assert!(!del_trace.created_at.is_empty(), "审计须带时间戳");
}

#[tokio::test]
async fn test_pipeline_state_update_param_type_errors_rejected_explicitly() {
    // MF1 回归：键存在但类型不符必须显式拒绝——静默降级成缺省会把删除/
    // upsert 意图整段吞掉还返回 updated（假成功）。
    let router = router_with_store();
    // delete_keys 非数组（字符串）
    let err = agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_type1", "th_type1"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": TEST_STATE_WRITER,
                "pipeline_id": "pipe_type1",
                "delete_keys": "approval.tickets.t1",
            }),
        ),
    )
    .await
    .expect_err("delete_keys 非数组必须显式拒绝");
    assert!(err.to_string().contains("delete_keys 必须是数组"), "{err}");

    // fields 非对象（数组）
    let err2 = agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_type1", "th_type1"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": TEST_STATE_WRITER,
                "pipeline_id": "pipe_type1",
                "fields": ["not", "an", "object"],
            }),
        ),
    )
    .await
    .expect_err("fields 非对象必须显式拒绝");
    assert!(err2.to_string().contains("fields 必须是对象"), "{err2}");
}

#[tokio::test]
async fn test_pipeline_state_update_delete_keys_all_not_found_keeps_contract() {
    // MF2 分支②回归：全 not_found（请求键全不存在）也必须返回完整契约体
    // （deleted: [] + not_found: [...]），不得退化成 {status, pipeline_id}。
    let tenant = format!("tenant_del5_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_del5_{}", uuid::Uuid::new_v4().simple());
    let router = router_with_store();
    let store = router.store.as_ref().unwrap().clone();
    seed_state_via_update(
        &router,
        &tenant,
        "th_del5",
        &pid,
        json!({"approval.tickets.t1": {"id": "t1"}}),
    )
    .await;

    let out = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_del5"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": TEST_STATE_WRITER,
                "pipeline_id": pid,
                "delete_keys": ["approval.never_existed"],
            }),
        ),
    )
    .await
    .expect("全 not_found 是幂等成功不是错误");
    assert_eq!(out["deleted"], json!([]), "deleted 恒在场（空数组）");
    assert_eq!(
        out["not_found"],
        json!(["approval.never_existed"]),
        "not_found 恒在场且如实差集"
    );
    // 原键不受影响（真实链路断言）
    let fields = store.load_pipeline_state(&pid, &tenant).await.unwrap();
    assert_eq!(fields.len(), 1, "原键不受影响");
}

#[tokio::test]
async fn test_pipeline_state_update_delete_keys_store_absent_honest_report() {
    // MF2 分支③回归：store 缺席（内存唯一落点）时 deleted = 内存实际删除集
    // （remove 返回 Some），not_found = 差集——不得把不存在的键谎报为 deleted。
    let tenant = format!("tenant_del6_{}", uuid::Uuid::new_v4().simple());
    let pid = format!("pipe_del6_{}", uuid::Uuid::new_v4().simple());
    // 无 store 装配：仅声明面 + 内存
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_state_writes_lookup(test_state_writes_lookup());
    let reg = agentos_session::pipeline_state_registry::global_registry();
    reg.get_or_init(
        &tenant,
        &pid,
        "th_del6",
        "agentos",
        json!({"pipeline_id": pid, "approval.tickets.t1": {"id": "t1"}}),
    );

    let out = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_del6"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "_plugin_id": TEST_STATE_WRITER,
                "pipeline_id": pid,
                "delete_keys": ["approval.tickets.t1", "approval.ghost"],
            }),
        ),
    )
    .await
    .expect("store 缺席删除应成功（内存唯一落点）");
    assert_eq!(
        out["deleted"],
        json!(["approval.tickets.t1"]),
        "deleted = 内存实际删除集"
    );
    assert_eq!(
        out["not_found"],
        json!(["approval.ghost"]),
        "不存在的键诚实进 not_found，不谎报 deleted"
    );

    // 内存键确实消失（真实链路断言）
    let entry = reg.get(&tenant, &pid).unwrap();
    let st = entry.read();
    assert!(st.state.get("approval.tickets.t1").is_none());
}
