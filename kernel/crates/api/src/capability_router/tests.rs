// @feature: FP-0.2.一 插件协议 | @ci: rust-test
// 由 capability_router.rs 的主 #[cfg(test)] 测试块体平移而来（保留私有项访问）。

use super::*;
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
        pipeline_role: None,
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
        granted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
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

/// 捕获 event-bus 推送到 sink 的文本（验证 tool 事件转发）。
struct CaptureSink {
    received: std::sync::Arc<std::sync::Mutex<Vec<Value>>>,
}
#[async_trait::async_trait]
impl agentos_session::EventSink for CaptureSink {
    async fn send_text(&self, text: &str) -> bool {
        if let Ok(v) = serde_json::from_str::<Value>(text) {
            self.received.lock().unwrap().push(v);
        }
        true
    }
    fn id(&self) -> u64 {
        1
    }
}

/// 构建带 session 的 router + 捕获 sink（验证 event-bus.emit 转发到前端）。
fn router_with_session(
    received: std::sync::Arc<std::sync::Mutex<Vec<Value>>>,
) -> KernelCapabilityRouter {
    use agentos_session::SessionCoordinator;
    let coord = Arc::new(SessionCoordinator::default());
    let sink = Arc::new(CaptureSink { received }) as Arc<dyn agentos_session::EventSink>;
    coord.register("user-test", sink);
    coord.register_thread("thread-1", "user-test");
    let agg = MetricsAggregator::new();
    KernelCapabilityRouter::with_metrics(agg).with_session(coord)
}

/// 带契约 + 声明查询器 + session 的 router（streaming 声明闸测试）。
fn router_with_streaming_gate(
    received: std::sync::Arc<std::sync::Mutex<Vec<Value>>>,
    decl: Option<agentos_core::traits::StreamingCapability>,
) -> KernelCapabilityRouter {
    use agentos_session::SessionCoordinator;
    let coord = Arc::new(SessionCoordinator::default());
    let sink = Arc::new(CaptureSink { received }) as Arc<dyn agentos_session::EventSink>;
    coord.register("user-test", sink);
    coord.register_thread("thread-1", "user-test");
    let contracts = Arc::new(
        crate::kernel_capabilities::load_contracts(
            &std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../../config/kernel/kernel_capabilities"),
        )
        .expect("仓库契约必须可加载"),
    );
    let lookup: StreamingDeclarationLookupFn = Arc::new(move |_pid| decl.clone());
    KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_session(coord)
        .with_capability_contracts(contracts)
        .with_streaming_declaration_lookup(lookup)
}

#[tokio::test]
async fn streaming_gate_rejects_undeclared_plugin() {
    // 插件未声明 capabilities.streaming → 事件被拒（fail-closed），sink 无事件。
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_streaming_gate(received.clone(), None);
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
    assert_eq!(res["status"], "dropped");
    assert!(res["reason"].as_str().unwrap().contains("streaming"));
    assert!(received.lock().unwrap().is_empty());
}

#[tokio::test]
async fn streaming_gate_declared_plugin_emits() {
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let decl = agentos_core::traits::StreamingCapability {
        conduit: false,
        events: Some(vec![
            "stream_start".to_string(),
            "stream_chunk".to_string(),
            "stream_end".to_string(),
        ]),
        part_types: None,
        persist: Some(false),
    };
    let router = router_with_streaming_gate(received.clone(), Some(decl.clone()));
    // 声明内的事件 → 放行（p_ 命名空间 + thread_id 齐备）
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
    assert_eq!(res["status"], "emitted");
    assert_eq!(received.lock().unwrap().len(), 1);
    // 声明外的 streaming 契约事件（tool_start 也在 10 事件内）→ 声明闸拒绝
    let res2 = router
        .handle(
            "event-bus",
            "emit",
            json!({
                "_plugin_id": "my_streamer",
                "event": "tool_start",
                "payload": {
                    "thread_id": "thread-1",
                    "pipeline_id": "c1b2c3d4e5f64789abcdef0123456789",
                    "message_id": "p_tool_001",
                    "call_id": "c1",
                    "tool_name": "f",
                },
            }),
        )
        .await
        .unwrap();
    assert_eq!(
        res2["status"], "dropped",
        "tool_start 未在 events 声明内应被声明闸拒绝（fail-closed）"
    );
}

#[tokio::test]
async fn streaming_gate_rejects_event_outside_declaration() {
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let decl = agentos_core::traits::StreamingCapability {
        conduit: false,
        events: Some(vec!["stream_start".to_string()]),
        part_types: None,
        persist: None,
    };
    let router = router_with_streaming_gate(received.clone(), Some(decl));
    // events 声明只含 stream_start → stream_chunk 被拒
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
    assert_eq!(res["status"], "dropped");
    assert!(res["reason"].as_str().unwrap().contains("declared events"));
}

#[tokio::test]
async fn streaming_gate_engine_conduit_bypasses_declaration() {
    // 引擎管道器官（manifest 声明 capabilities.streaming.conduit=true，P1-2
    // 声明化）豁免 events 清单发射闸，但命名空间必须 a_（内核签发）。
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let decl = agentos_core::traits::StreamingCapability {
        conduit: true,
        events: None,
        part_types: None,
        persist: None,
    };
    let router = router_with_streaming_gate(received.clone(), Some(decl));
    let res = router
        .handle(
            "event-bus",
            "emit",
            json!({
                "_plugin_id": "pipeline_llm_core",
                "event": "stream_chunk",
                "payload": {
                    "thread_id": "thread-1",
                    "pipeline_id": "c1b2c3d4e5f64789abcdef0123456789",
                    "message_id": "a_0123456789abcdef0123456789abcdef",
                    "content": "hi",
                },
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "emitted");
    // 但 llm_core 自造 p_ id → 命名空间执法拒绝
    let res2 = router
        .handle(
            "event-bus",
            "emit",
            json!({
                "_plugin_id": "pipeline_llm_core",
                "event": "stream_chunk",
                "payload": {
                    "thread_id": "thread-1",
                    "pipeline_id": "c1b2c3d4e5f64789abcdef0123456789",
                    "message_id": "p_selfmade_001",
                    "content": "hi",
                },
            }),
        )
        .await
        .unwrap();
    assert_eq!(res2["status"], "dropped");
}

#[tokio::test]
async fn streaming_gate_engine_id_without_conduit_declaration_rejected() {
    // P1-2 fail-closed：引擎管道 id 不再凭 id 豁免——manifest 未声明
    // capabilities.streaming.conduit（lookup None）时按普通插件执法。
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_streaming_gate(received.clone(), None);
    let res = router
        .handle(
            "event-bus",
            "emit",
            json!({
                "_plugin_id": "pipeline_llm_core",
                "event": "stream_chunk",
                "payload": {
                    "thread_id": "thread-1",
                    "pipeline_id": "c1b2c3d4e5f64789abcdef0123456789",
                    "message_id": "a_0123456789abcdef0123456789abcdef",
                    "content": "hi",
                },
            }),
        )
        .await
        .unwrap();
    assert_eq!(
        res["status"], "dropped",
        "未声明 conduit 的插件（含引擎 id）不得豁免发射闸"
    );
    assert!(res["reason"].as_str().unwrap().contains("not declared"));
}

#[tokio::test]
async fn test_event_bus_tool_start_forwarded_with_fields() {
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());

    // 模拟 tool_core 经 host_call → event-bus.emit 上报的 tool_start 事件。
    let params = json!({
        "event": "tool_start",
        "payload": {
            "thread_id": "thread-1",
            "pipeline_id": "pipe-1",
            "message_id": "msg-1",
            "call_id": "call_abc",
            "tool_name": "bash_execute",
            "args": {"command": "echo hi"},
        }
    });
    let res = router.handle("event-bus", "emit", params).await.unwrap();
    assert_eq!(res["status"], "emitted");
    assert_eq!(res["event"], "tool_start");

    // sink 应收到透传后的事件（含路由键 + 业务字段）。
    let msgs = received.lock().unwrap().clone();
    assert_eq!(msgs.len(), 1);
    let ev = &msgs[0];
    assert_eq!(ev["type"], "tool_start");
    assert_eq!(ev["data"]["call_id"], "call_abc");
    assert_eq!(ev["data"]["tool_name"], "bash_execute");
    assert_eq!(ev["data"]["args"]["command"], "echo hi");
    assert_eq!(ev["data"]["pipeline_id"], "pipe-1");
    assert_eq!(ev["data"]["message_id"], "msg-1");
    assert_eq!(ev["data"]["_threadId"], "thread-1");
}

#[tokio::test]
async fn test_event_bus_tool_result_forwarded() {
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());

    let params = json!({
        "event": "tool_result",
        "payload": {
            "thread_id": "thread-1",
            "pipeline_id": "pipe-1",
            "message_id": "msg-1",
            "call_id": "call_abc",
            "tool_name": "bash_execute",
            "result": "hi\n",
            "success": true,
            "duration_ms": 5.2,
        }
    });
    let res = router.handle("event-bus", "emit", params).await.unwrap();
    assert_eq!(res["event"], "tool_result");

    let msgs = received.lock().unwrap().clone();
    assert_eq!(msgs.len(), 1);
    assert_eq!(msgs[0]["type"], "tool_result");
    assert_eq!(msgs[0]["data"]["call_id"], "call_abc");
    assert_eq!(msgs[0]["data"]["success"], true);
    assert_eq!(msgs[0]["data"]["duration_ms"], 5.2);
}

#[tokio::test]
async fn test_event_bus_tool_event_no_thread_id_dropped() {
    // thread_id 缺失 → 前端无法路由，应丢弃（不推 sink）。
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());

    let params = json!({
        "event": "tool_start",
        "payload": {"call_id": "c1", "tool_name": "f"}
    });
    let _ = router.handle("event-bus", "emit", params).await.unwrap();
    assert!(received.lock().unwrap().is_empty());
}

#[tokio::test]
async fn test_event_bus_interaction_request_forwarded() {
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());
    let params = json!({
        "event": "interaction_request",
        "payload": {
            "thread_id": "thread-1",
            "request_id": "req-abc",
            "interaction_mode": "choice",
            "title": "请选择",
            "options": [{"id": "a", "label": "方案A"}],
        }
    });
    let res = router.handle("event-bus", "emit", params).await.unwrap();
    assert_eq!(res["status"], "emitted");
    assert_eq!(res["event"], "interaction_request");
    let msgs = received.lock().unwrap().clone();
    assert_eq!(msgs.len(), 1);
    assert_eq!(msgs[0]["type"], "interaction_request");
    assert_eq!(msgs[0]["data"]["request_id"], "req-abc");
    assert_eq!(msgs[0]["data"]["_threadId"], "thread-1");
}

#[tokio::test]
async fn test_event_bus_interaction_event_no_thread_id_dropped() {
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());
    let params = json!({
        "event": "interaction_request",
        "payload": {"request_id": "req-abc", "title": "x"}
    });
    let _ = router.handle("event-bus", "emit", params).await.unwrap();
    assert!(received.lock().unwrap().is_empty());
}

// ── frontend.emit：插件 → 内核 → 前端一次性事件出口（ADR §3.5，
//    task_observability 任务 1/2 共享前置）──

#[tokio::test]
async fn test_frontend_emit_cost_update_forwarded() {
    // track 插件经 frontend.emit 推 cost_update：payload 携带路由键 +
    // 单轮/累计 token 指标，整体透传并补齐路由键后推前端。
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());

    let params = json!({
        "event": "cost_update",
        "payload": {
            "thread_id": "thread-1",
            "pipeline_id": "pipe-1",
            "message_id": "msg-1",
            "input_tokens": 60632,
            "output_tokens": 512,
            "cached_tokens": 60000,
            "total_tokens": 61144,
            "cache_hit_ratio": 0.9895,
            "cumulative": {
                "total_input": 2458025,
                "total_output": 30000,
                "total_cached": 2331456,
                "missed": 126569
            }
        }
    });
    let res = router.handle("frontend", "emit", params).await.unwrap();
    assert_eq!(res["status"], "emitted");
    assert_eq!(res["event"], "cost_update");

    let msgs = received.lock().unwrap().clone();
    assert_eq!(msgs.len(), 1);
    let ev = &msgs[0];
    assert_eq!(ev["type"], "cost_update");
    assert_eq!(ev["data"]["pipeline_id"], "pipe-1");
    assert_eq!(ev["data"]["message_id"], "msg-1");
    assert_eq!(ev["data"]["_threadId"], "thread-1");
    assert_eq!(ev["data"]["input_tokens"], 60632);
    assert_eq!(ev["data"]["cached_tokens"], 60000);
    assert_eq!(ev["data"]["cumulative"]["missed"], 126569);
    // sequence 信封由 SessionCoordinator 分配（与 tool 事件同空间）
    assert!(ev.get("sequence").is_some());
}

#[tokio::test]
async fn test_frontend_emit_tool_progress_forwarded() {
    // bash 工具执行中经 frontend.emit 推 tool_progress（stdout 增量）。
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());

    let params = json!({
        "event": "tool_progress",
        "payload": {
            "thread_id": "thread-1",
            "pipeline_id": "pipe-1",
            "message_id": "msg-1",
            "call_id": "call_abc",
            "tool_name": "bash_execute",
            "delta": "build ok\n",
            "bytes_read": 4096,
            "elapsed_ms": 2100
        }
    });
    let res = router.handle("frontend", "emit", params).await.unwrap();
    assert_eq!(res["event"], "tool_progress");

    let msgs = received.lock().unwrap().clone();
    assert_eq!(msgs.len(), 1);
    assert_eq!(msgs[0]["type"], "tool_progress");
    assert_eq!(msgs[0]["data"]["call_id"], "call_abc");
    assert_eq!(msgs[0]["data"]["delta"], "build ok\n");
    assert_eq!(msgs[0]["data"]["pipeline_id"], "pipe-1");
    assert_eq!(msgs[0]["data"]["_threadId"], "thread-1");
}

#[tokio::test]
async fn test_frontend_emit_thread_id_top_level_fallback() {
    // thread_id 允许在 params 顶层（ADR scope 语义）——payload 内缺失时兜底。
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());

    let params = json!({
        "event": "termination_status",
        "thread_id": "thread-1",
        "payload": {
            "pipeline_id": "pipe-1",
            "convergence": "converging",
            "remaining_budget_percent": 73.5
        }
    });
    let res = router.handle("frontend", "emit", params).await.unwrap();
    assert_eq!(res["event"], "termination_status");

    let msgs = received.lock().unwrap().clone();
    assert_eq!(msgs.len(), 1);
    assert_eq!(msgs[0]["type"], "termination_status");
    assert_eq!(msgs[0]["data"]["_threadId"], "thread-1");
    assert_eq!(msgs[0]["data"]["convergence"], "converging");
}

#[tokio::test]
async fn test_frontend_emit_no_thread_id_dropped() {
    // thread_id 缺失（payload 与顶层都无）→ 前端无法路由，丢弃。
    let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_session(received.clone());

    let params = json!({
        "event": "cost_update",
        "payload": {"pipeline_id": "pipe-1", "total_tokens": 100}
    });
    let _ = router.handle("frontend", "emit", params).await.unwrap();
    assert!(received.lock().unwrap().is_empty());
}

// ── tool-executor._call_context 透传（task_observability 任务 2）──

/// 捕获 invoke_tool 收到的 (plugin_id, tool_name, inputs)。
struct CaptureInvoker {
    captured: std::sync::Arc<std::sync::Mutex<Vec<(String, String, Value)>>>,
}
#[async_trait::async_trait]
impl agentos_core::traits::PluginInvoker for CaptureInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        _ctx: &agentos_core::types::PluginContext<'a>,
    ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
        Err(agentos_core::types::PluginError {
            message: "not used in test".into(),
            code: None,
            source: None,
        })
    }
    async fn invoke_tool(
        &self,
        plugin_id: &str,
        tool_name: &str,
        inputs: &Value,
    ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError> {
        self.captured.lock().unwrap().push((
            plugin_id.to_string(),
            tool_name.to_string(),
            inputs.clone(),
        ));
        Ok(agentos_core::types::ToolExecutionResult {
            success: true,
            data: json!({"output": "ok"}),
            error: None,
            duration_ms: Some(1),
            metadata: None,
        })
    }
    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: agentos_core::traits::LifecycleHook,
        _context: &agentos_core::traits::HookContext,
    ) -> Result<(), agentos_core::types::PluginError> {
        Ok(())
    }
}

/// 构造带单工具注册表（bash_execute → plugin_bash）+ 捕获 invoker 的 router。
fn router_with_tool_invoke(
    captured: std::sync::Arc<std::sync::Mutex<Vec<(String, String, Value)>>>,
) -> KernelCapabilityRouter {
    use agentos_core::traits::ToolDescriptor;
    use agentos_core::types::{ToolCategory, ToolSource};
    use agentos_plugin_loader::CapabilityRegistryImpl;
    let registry = CapabilityRegistryImpl::new();
    registry.register_tool(
        "plugin_bash",
        ToolDescriptor {
            name: "bash_execute".into(),
            description: String::new(),
            plugin_id: "plugin_bash".into(),
            input_schema: json!({}),
            output_schema: None,
            category: ToolCategory::System,
            source: ToolSource::Mcp,
            ui: None,
            render: None,
        },
    );
    KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_invoker(Arc::new(CaptureInvoker { captured }))
        .with_registry(Arc::new(registry))
}

#[tokio::test]
async fn test_tool_executor_invoke_merges_call_context_into_args() {
    // tool_core 在 params 级携带 _call_context（前端路由键）→
    // 内核合入 tool args 透传给工具 sidecar（bash 据此推 tool_progress）。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_tool_invoke(captured.clone());

    let params = json!({
        "tool_name": "bash_execute",
        "args": {"command": "echo hi", "session_id": "sess-1"},
        "_call_context": {
            "call_id": "call_abc",
            "pipeline_id": "pipe-1",
            "message_id": "msg-1",
            "thread_id": "sess-1"
        }
    });
    let res = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();
    assert_eq!(res["success"], true);

    let calls = captured.lock().unwrap().clone();
    assert_eq!(calls.len(), 1);
    let (plugin_id, tool_name, inputs) = &calls[0];
    assert_eq!(plugin_id, "plugin_bash");
    assert_eq!(tool_name, "bash_execute");
    assert_eq!(inputs["command"], "echo hi");
    assert_eq!(inputs["session_id"], "sess-1");
    assert_eq!(inputs["_call_context"]["call_id"], "call_abc");
    assert_eq!(inputs["_call_context"]["pipeline_id"], "pipe-1");
    assert_eq!(inputs["_call_context"]["message_id"], "msg-1");
    assert_eq!(inputs["_call_context"]["thread_id"], "sess-1");
}

#[tokio::test]
async fn test_tool_executor_invoke_without_call_context_untouched() {
    // 无 _call_context（旧 tool_core / 无进度需求的工具）→ args 原样透传。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_tool_invoke(captured.clone());

    let params = json!({
        "tool_name": "bash_execute",
        "args": {"command": "echo hi", "session_id": "sess-1"},
    });
    let _ = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();

    let calls = captured.lock().unwrap().clone();
    assert_eq!(calls.len(), 1);
    let inputs = &calls[0].2;
    assert!(inputs.get("_call_context").is_none());
    assert_eq!(inputs["command"], "echo hi");
}

// ── tool-executor.invoke 目标插件解析（显式 plugin_id 优先于注册表反查）──

#[tokio::test]
async fn test_tool_executor_explicit_plugin_id_wins_over_registry() {
    // 调用方显式传 plugin_id（系统插件工具如 hindsight.recall 不在 CapabilityRegistry，
    // 反查必然失败）→ 必须优先用显式 plugin_id，而不是注册表反查结果。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    // 注册表把 bash_execute → plugin_bash；但显式 plugin_id 指向 system 插件 hindsight。
    let router = router_with_tool_invoke(captured.clone());

    let params = json!({
        "tool_name": "bash_execute",
        "plugin_id": "hindsight",
        "args": {"query": "where did I leave the keys", "session_id": "sess-1"},
    });
    let res = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();
    assert_eq!(res["success"], true);

    let calls = captured.lock().unwrap().clone();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].0, "hindsight", "显式 plugin_id 应优先于注册表反查");
    assert_eq!(calls[0].1, "bash_execute");
}

#[tokio::test]
async fn test_tool_executor_empty_plugin_id_falls_back_to_registry() {
    // 显式 plugin_id 为空字符串 → 视为未提供，回退注册表反查。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_tool_invoke(captured.clone());

    let params = json!({
        "tool_name": "bash_execute",
        "plugin_id": "",
        "args": {"command": "echo hi"},
    });
    let _ = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();

    let calls = captured.lock().unwrap().clone();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].0, "plugin_bash", "空 plugin_id 应回退注册表反查");
}

#[tokio::test]
async fn test_tool_executor_internal_keys_stripped_from_args() {
    // _owner/_log_ctx/tenant_id/plugin_id 是内核/SDK 内部元数据，不得透传给工具 handler。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_tool_invoke(captured.clone());

    let params = json!({
        "tool_name": "bash_execute",
        "plugin_id": "plugin_bash",
        "args": {
            "command": "echo hi",
            "session_id": "sess-1",
            "_owner": "user-1",
            "_log_ctx": {"request_id": "req-1"},
            "tenant_id": "tenant-a",
            "plugin_id": "spoofed",
            "pipeline_id": "pipe-1"
        },
        "_log_ctx": {"request_id": "req-1"},
    });
    let _ = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();

    let calls = captured.lock().unwrap().clone();
    let inputs = &calls[0].2;
    // 内部元数据全部剥离
    assert!(inputs.get("_owner").is_none(), "_owner 应被剥离");
    assert!(inputs.get("_log_ctx").is_none(), "_log_ctx 应被剥离");
    assert!(inputs.get("tenant_id").is_none(), "tenant_id 应被剥离");
    assert!(
        inputs.get("plugin_id").is_none(),
        "args 级 plugin_id 应被剥离（防伪造）"
    );
    // 业务字段保留（session_id/pipeline_id 是 param_inject 注入的显式参数）
    assert_eq!(inputs["command"], "echo hi");
    assert_eq!(inputs["session_id"], "sess-1");
    assert_eq!(inputs["pipeline_id"], "pipe-1");
}

#[tokio::test]
async fn test_tool_executor_registry_fallback_without_explicit_id() {
    // 无显式 plugin_id → 注册表 tool_name → plugin_id 反查。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_tool_invoke(captured.clone());

    let params = json!({
        "tool_name": "bash_execute",
        "args": {"command": "echo hi", "session_id": "sess-1"},
    });
    let _ = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();

    let calls = captured.lock().unwrap().clone();
    assert_eq!(calls[0].0, "plugin_bash");
}

#[tokio::test]
async fn test_tool_executor_unregistered_tool_fails_closed() {
    // 反查失败（无注册表/工具未注册）+ 无显式 plugin_id → fail-closed 报
    // "工具未注册"，不落 invoker（无"工具名当插件 ID"兜底）。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_invoker(
        Arc::new(CaptureInvoker {
            captured: captured.clone(),
        }),
    );

    let params = json!({
        "tool_name": "some_tool",
        "args": {"x": 1},
    });
    let res = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();

    assert_eq!(res["success"], false);
    assert!(
        res["error"].as_str().unwrap().contains("未注册"),
        "应报工具未注册，got: {}",
        res["error"].as_str().unwrap()
    );
    let calls = captured.lock().unwrap().clone();
    assert!(calls.is_empty(), "未注册工具不应触达 invoker");
}

// ── tool-executor.invoke 调用路径自愈（BUG-37）────────────────────────────
// 注册表条目因 G2 启动期误剔等原因丢失且 watcher 不重扫 → 工具永久不可用。
// 自愈闭包 = "未注册但 manifest 在册 → 重注册再反查"（有界：每次调用至多一次）。

/// 自愈测试夹具：共享注册表 + 记录 heal 触发次数。
/// heal 闭包模拟生产装配（manifest 在册 → 重注册 → 返回描述符）。
fn router_with_heal(
    captured: std::sync::Arc<std::sync::Mutex<Vec<(String, String, Value)>>>,
    registry: Arc<agentos_plugin_loader::CapabilityRegistryImpl>,
    heal_calls: std::sync::Arc<std::sync::Mutex<usize>>,
) -> KernelCapabilityRouter {
    use agentos_core::traits::ToolDescriptor;
    use agentos_core::types::{ToolCategory, ToolSource};
    let reg_for_heal = registry.clone();
    let heal: ToolRegistryHealFn = Arc::new(move |tool_name: &str| {
        let reg = reg_for_heal.clone();
        let calls = heal_calls.clone();
        Box::pin(async move {
            *calls.lock().unwrap() += 1;
            // 模拟生产自愈：manifest 在册 → reenable 重注册（幂等），返回描述符。
            if tool_name != "project_state" {
                return None;
            }
            let descriptor = ToolDescriptor {
                name: tool_name.to_string(),
                description: "healed".into(),
                plugin_id: "project_state_tool".into(),
                input_schema: json!({"type": "object"}),
                output_schema: None,
                category: ToolCategory::System,
                source: ToolSource::Mcp,
                ui: None,
                render: None,
            };
            reg.register_tool("project_state_tool", descriptor.clone());
            Some(descriptor)
        })
    });
    KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_invoker(Arc::new(CaptureInvoker { captured }))
        .with_registry(registry)
        .with_tool_registry_heal(heal)
}

#[tokio::test]
async fn test_tool_executor_unregistered_tool_self_heals_and_invokes() {
    // 注册表条目丢失（G2 误剔形态：registry 无 project_state）→ 反查 miss
    // 触发自愈重注册 → 本次调用按重注册结果路由到正确插件，且注册表恢复。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let heal_calls = std::sync::Arc::new(std::sync::Mutex::new(0usize));
    let registry = Arc::new(agentos_plugin_loader::CapabilityRegistryImpl::new());
    let router = router_with_heal(captured.clone(), registry.clone(), heal_calls.clone());

    let params = json!({
        "tool_name": "project_state",
        "args": {"action": "query"},
    });
    let res = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();

    assert_eq!(res["success"], true, "自愈后调用应成功: {}", res["error"]);
    let calls = captured.lock().unwrap().clone();
    assert_eq!(calls.len(), 1, "自愈后应落 invoker 执行");
    assert_eq!(calls[0].0, "project_state_tool", "应路由到重注册的插件");
    assert_eq!(calls[0].1, "project_state");
    // 注册表条目恢复（后续反查不再依赖自愈）
    assert!(
        registry.get_tool("project_state").is_some(),
        "自愈应把工具重注册进共享注册表"
    );
    // 下次调用直接命中注册表，不再触发自愈（有界性）
    let _ = router
        .handle(
            "tool-executor",
            "invoke",
            json!({"tool_name": "project_state", "args": {"action": "query"}}),
        )
        .await
        .unwrap();
    assert_eq!(*heal_calls.lock().unwrap(), 1, "注册表恢复后不应再触发自愈");
}

#[tokio::test]
async fn test_tool_executor_heal_miss_still_fails_closed() {
    // 自愈闭包返回 None（manifest 不在册 / 插件已禁用）→ 保持 fail-closed
    // 报"未注册"，不落 invoker。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let heal_calls = std::sync::Arc::new(std::sync::Mutex::new(0usize));
    let registry = Arc::new(agentos_plugin_loader::CapabilityRegistryImpl::new());
    let router = router_with_heal(captured.clone(), registry.clone(), heal_calls.clone());

    let res = router
        .handle(
            "tool-executor",
            "invoke",
            json!({"tool_name": "ghost_tool", "args": {}}),
        )
        .await
        .unwrap();

    assert_eq!(res["success"], false);
    assert!(
        res["error"].as_str().unwrap().contains("未注册"),
        "自愈未命中应保持 fail-closed，got: {}",
        res["error"].as_str().unwrap()
    );
    assert_eq!(*heal_calls.lock().unwrap(), 1, "应触发过一次自愈尝试");
    assert!(
        captured.lock().unwrap().is_empty(),
        "自愈未命中不得触达 invoker"
    );
}

/// 固定返回错误的 invoker（验证 tool-executor.invoke 的错误归一化）。
struct ErroringInvoker;
#[async_trait::async_trait]
impl agentos_core::traits::PluginInvoker for ErroringInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        _ctx: &agentos_core::types::PluginContext<'a>,
    ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
        Err(agentos_core::types::PluginError {
            message: "not used".into(),
            code: None,
            source: None,
        })
    }
    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &Value,
    ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError> {
        Err(agentos_core::types::PluginError {
            message: "sidecar crashed".into(),
            code: Some("MCP_TOOL_CALL_FAILED".into()),
            source: None,
        })
    }
    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: agentos_core::traits::LifecycleHook,
        _context: &agentos_core::traits::HookContext,
    ) -> Result<(), agentos_core::types::PluginError> {
        Ok(())
    }
}

#[tokio::test]
async fn test_tool_executor_invoke_error_normalized_to_failure_json() {
    // invoke_tool 返回 Err → capability 层归一化为 {"success": false, "error": ...}，
    // 不把内核 PluginError 泄漏给 sidecar。
    use agentos_core::traits::ToolDescriptor;
    use agentos_core::types::{ToolCategory, ToolSource};
    use agentos_plugin_loader::CapabilityRegistryImpl;
    let registry = CapabilityRegistryImpl::new();
    registry.register_tool(
        "plugin_bash",
        ToolDescriptor {
            name: "bash_execute".into(),
            description: String::new(),
            plugin_id: "plugin_bash".into(),
            input_schema: json!({}),
            output_schema: None,
            category: ToolCategory::System,
            source: ToolSource::Mcp,
            ui: None,
            render: None,
        },
    );
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_invoker(Arc::new(ErroringInvoker))
        .with_registry(Arc::new(registry));

    let res = router
        .handle(
            "tool-executor",
            "invoke",
            json!({"tool_name": "bash_execute", "args": {"command": "echo hi"}}),
        )
        .await
        .unwrap();
    assert_eq!(res["success"], false);
    assert!(res["error"].as_str().unwrap().contains("sidecar crashed"));
}

#[tokio::test]
async fn test_tool_executor_missing_invoker_returns_protocol_error() {
    // 未注入 invoker → Protocol 错误（配置缺失早暴露）。
    // 工具先经注册表反查可达（否则 fail-closed 走"未注册"分支）。
    use agentos_core::traits::ToolDescriptor;
    use agentos_core::types::{ToolCategory, ToolSource};
    use agentos_plugin_loader::CapabilityRegistryImpl;
    let registry = CapabilityRegistryImpl::new();
    registry.register_tool(
        "plugin_bash",
        ToolDescriptor {
            name: "bash_execute".into(),
            description: String::new(),
            plugin_id: "plugin_bash".into(),
            input_schema: json!({}),
            output_schema: None,
            category: ToolCategory::System,
            source: ToolSource::Mcp,
            ui: None,
            render: None,
        },
    );
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_registry(Arc::new(registry));
    let res = router
        .handle(
            "tool-executor",
            "invoke",
            json!({"tool_name": "bash_execute", "args": {}}),
        )
        .await;
    assert!(res.is_err());
    let err = res.unwrap_err();
    match err {
        agentos_mcp::McpError::Protocol { message } => {
            assert!(message.contains("未配置 invoker"), "got: {message}")
        }
        other => panic!("expected Protocol error, got {other:?}"),
    }
}

// ── event-bus.emit_domain（ADR 2026-08-28 事件下沉底座，用真实内存 SqliteStore 验证端到端）──

type DomainSink = std::sync::Arc<std::sync::Mutex<Vec<(String, Vec<(String, serde_json::Value)>)>>>;

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

fn router_with_store() -> KernelCapabilityRouter {
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().expect("open_memory"));
    KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_store(store)
        .with_export_fields_lookup(Arc::new(|| {
            crate::capability_router::ExportFields::from_manifests(&[test_task_export_manifest()])
        }))
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
async fn test_pipeline_state_cold_rows_carry_latest_run_status() {
    // BUG-2 幽灵 running：冷兜底行（checkpoint + 表行合并）缺 run_status 时，
    // 消费方（插件 reconcile / 前端三源推断）把死管道猜成 running。
    // 契约：冷行以 runs 表最新 run 的权威状态补齐 run_status（fill-if-absent）。
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
                .record_run_start(&pid, &tenant, &run_id, "h")
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
            // 出生投影只有任务域字段：run_status 缺失（崩溃形态）
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
        "冷行应携带 runs 表最新 run 的权威状态"
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
async fn test_pipeline_state_update_rejects_non_task_keys() {
    // 写面仅允许 task.* 前缀键——管道运行域字段（iteration/status/suspended）
    // 归引擎，插件不得触碰。
    let tenant = format!("tenant_upd2_{}", uuid::Uuid::new_v4().simple());
    let router = router_with_store();
    let r = agentos_tenant::scope(
        agentos_core::types::TenantContext::new(&tenant, "th_upd2"),
        router.handle(
            "pipeline-state",
            "update",
            json!({
                "pipeline_id": "pipe_x",
                "fields": {"iteration": 5},
            }),
        ),
    )
    .await;
    assert!(
        r.is_err(),
        "非 task.* 键必须拒绝（管道运行域归引擎）: {r:?}"
    );
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
        .record_run_start("task_tp", "default", "run_tp_1", "hash")
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
        .record_run_start("task_lp", "default", "run_lp_1", "hash")
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
        .record_run_start("task_lp", "default", "run_lp_2", "hash")
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
        .record_run_start("task_status_1", "default", "run_status_1", "hash")
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

// ── G6：granted_capabilities 白名单单点校验 ──

#[tokio::test]
async fn g6_granted_capability_allowed() {
    let lookup: GrantsLookupFn = Arc::new(|pid| {
        assert_eq!(pid, "p1");
        Some(vec!["event-bus".to_string()])
    });
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_grants_lookup(lookup);
    let out = router
        .handle(
            "event-bus",
            "emit",
            json!({"_plugin_id": "p1", "type": "x"}),
        )
        .await
        .unwrap();
    // event-bus.emit 返回 200-ish json（不因授权被拒即通过）。
    assert!(out.get("status").is_some() || !out.is_null());
}

#[tokio::test]
async fn g6_ungranted_capability_denied() {
    let lookup: GrantsLookupFn = Arc::new(|_| Some(vec!["config-reader".to_string()]));
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_grants_lookup(lookup);
    let err = router
        .handle(
            "event-bus",
            "emit",
            json!({"_plugin_id": "p1", "type": "x"}),
        )
        .await
        .unwrap_err();
    assert!(
        format!("{}", err).contains("not granted"),
        "应被白名单拒绝: {}",
        err
    );
}

#[tokio::test]
async fn g6_undeclared_grants_default_allow() {
    // 未声明 granted_capabilities（None）→ 默认全授予（存量兼容）。
    let lookup: GrantsLookupFn = Arc::new(|_| None);
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_grants_lookup(lookup);
    let out = router
        .handle(
            "event-bus",
            "emit",
            json!({"_plugin_id": "p1", "type": "x"}),
        )
        .await
        .unwrap();
    assert!(out.get("status").is_some() || !out.is_null());
}

// ── G6 strict 开关（AGENTOS_GRANTS_STRICT=1，审计变更#3）──

#[tokio::test]
async fn g6_strict_denies_undeclared_grants() {
    // 契约（strict fail-closed）：开启 strict 后，未声明 granted_capabilities
    // 的插件反向调用一律拒绝，错误信息标明未声明。
    let lookup: GrantsLookupFn = Arc::new(|_| None);
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_grants_lookup(lookup)
        .with_grants_strict();
    let err = router
        .handle(
            "event-bus",
            "emit",
            json!({"_plugin_id": "p1", "type": "x"}),
        )
        .await
        .unwrap_err();
    let msg = format!("{}", err);
    assert!(
        msg.contains("not granted") && msg.contains("no granted_capabilities declared"),
        "strict 拒绝未声明者，且信息指向未声明: {msg}"
    );
}

#[tokio::test]
async fn g6_strict_allows_declared_grants() {
    // 契约（strict 只收紧未声明者）：已声明白名单且命中的调用照常放行。
    let lookup: GrantsLookupFn = Arc::new(|pid| {
        assert_eq!(pid, "p1");
        Some(vec!["event-bus".to_string()])
    });
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_grants_lookup(lookup)
        .with_grants_strict();
    let out = router
        .handle(
            "event-bus",
            "emit",
            json!({"_plugin_id": "p1", "type": "x"}),
        )
        .await
        .unwrap();
    assert!(out.get("status").is_some() || !out.is_null());
}

#[tokio::test]
async fn g6_strict_still_denies_ungranted_capability() {
    // 契约（strict 叠加白名单语义）：声明了白名单但 namespace 不在名单内，
    // strict 下照旧拒绝——白名单制语义不因开关改变。
    let lookup: GrantsLookupFn = Arc::new(|_| Some(vec!["config-reader".to_string()]));
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_grants_lookup(lookup)
        .with_grants_strict();
    let err = router
        .handle(
            "event-bus",
            "emit",
            json!({"_plugin_id": "p1", "type": "x"}),
        )
        .await
        .unwrap_err();
    assert!(
        format!("{}", err).contains("not granted"),
        "白名单不命中时 strict 照常拒绝: {}",
        err
    );
}

#[tokio::test]
async fn g6_no_lookup_no_check() {
    // 未装配授权查询器 → 不校验（旧装配兼容）。
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new());
    let out = router
        .handle(
            "event-bus",
            "emit",
            json!({"_plugin_id": "p1", "type": "x"}),
        )
        .await
        .unwrap();
    assert!(out.get("status").is_some() || !out.is_null());
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
    assert!(format!("{}", err).contains("_plugin_id"));
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
    assert!(format!("{}", err).contains("name"));
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
    assert!(format!("{}", err).contains("未装配"));
}

#[tokio::test]
async fn g3_envelope_gate_applies_to_registry_namespace() {
    // 信封闸（G6 单点）：声明了白名单但不含 "registry" → 拒绝（信封二道闸验证）。
    let registrar: DynamicToolRegistrar = Arc::new(|_, _| Ok(()));
    let lookup: GrantsLookupFn = Arc::new(|_| Some(vec!["config-reader".to_string()]));
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_dynamic_tool_registrar(registrar)
        .with_grants_lookup(lookup);
    let err = router
        .handle(
            "registry",
            "register_tool",
            json!({"_plugin_id": "p", "name": "x"}),
        )
        .await
        .unwrap_err();
    assert!(
        format!("{}", err).contains("not granted"),
        "信封闸应先于注册拒绝: {}",
        err
    );
}

#[tokio::test]
async fn g3_envelope_grant_allows_registration() {
    // granted 含 "registry" → 信封闸放行,注册成功。
    let registrar: DynamicToolRegistrar = Arc::new(|_, _| Ok(()));
    let lookup: GrantsLookupFn = Arc::new(|_| Some(vec!["registry".to_string()]));
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_dynamic_tool_registrar(registrar)
        .with_grants_lookup(lookup);
    let out = router
        .handle(
            "registry",
            "register_tool",
            json!({"_plugin_id": "p", "name": "x"}),
        )
        .await
        .unwrap();
    assert_eq!(out["status"], "registered");
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

    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_sr", "thread_sr"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            store
                .record_run_start("pipe_task_9", &tenant, "run_sr_1", "h")
                .await
                .unwrap();
            store
                .record_run_start("pipe_task_9", &tenant, "run_sr_2", "h")
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
            // 真实收束史，新 run 另起一行承载执行，不造幽灵 running）
            let r3 = router
                .handle(
                    "pipeline-executor",
                    "resume_pipeline",
                    json!({"pipeline_id": "pipe_task_9", "user_id": "u_1"}),
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
                    "u_1".to_string(),
                    None
                )],
                "续跑派发应带 pipeline/thread/user 三坐标走统一派发链（无 overlay 时为 None）"
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
        },
    )
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
                .record_run_start("pipe_ap", &tenant, "run_ap_1", "h")
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
                .record_run_start("pipe_s7", &tenant, "run_s7", "h")
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
                .record_run_start("pipe_s7b", &tenant, "run_s7b", "h")
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
                .record_run_start("pipe_rf", &tenant, "run_rf_1", "h")
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

    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_th", "thread_th"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            store
                .record_run_start("pipe_th", &tenant, "run_th_1", "h")
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
        },
    )
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

    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_ov", "thread_ov"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            store
                .record_run_start("pipe_ov", &tenant, "run_ov_1", "h")
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
                        "user_id": "u_ov",
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
        },
    )
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
                .record_run_start("pipe_nr", &tenant, "run_nr_1", "h")
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
    // 契约（P1-5 声明化）：白名单命中注入；声明方（spill_guard manifest
    // force_include_tools）声明的工具无视白名单保留。
    let router = router_with_tool_descriptors_and_forced(
        &[
            plain_tool("bash_execute"),
            plain_tool("file_read"),
            plain_tool("spill_retrieve"),
        ],
        vec!["spill_retrieve"],
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
        vec!["bash_execute".to_string(), "spill_retrieve".to_string()],
        "白名单命中 + 声明强制工具"
    );
    // OpenAI function-calling 形态
    let first = &result["schemas"][0];
    assert_eq!(first["type"], "function");
    assert!(first["function"]["parameters"].is_object());
    // 换一组声明 → 强制面随声明变（声明驱动，非内核名单）
    let router2 = router_with_tool_descriptors_and_forced(
        &[plain_tool("bash_execute"), plain_tool("spill_retrieve")],
        vec!["bash_execute"],
    );
    let result2 = router2
        .handle(
            "tool-surface",
            "schemas",
            json!({"tool_ids": ["spill_retrieve"]}),
        )
        .await
        .unwrap();
    assert_eq!(
        schema_names(&result2),
        vec!["bash_execute".to_string(), "spill_retrieve".to_string()],
        "强制注入集合随声明增减"
    );
}

#[tokio::test]
async fn tool_surface_empty_whitelist_yields_declared_only() {
    // 契约：空数组 = agent 声明零工具 → 仅声明强制工具（非断链、非全量）。
    let router = router_with_tool_descriptors_and_forced(
        &[plain_tool("bash_execute"), plain_tool("spill_retrieve")],
        vec!["spill_retrieve"],
    );
    let result = router
        .handle("tool-surface", "schemas", json!({"tool_ids": []}))
        .await
        .unwrap();
    assert_eq!(schema_names(&result), vec!["spill_retrieve".to_string()]);
}

// ── BUG-51：观测失败插件遮挡（verify_incomplete/verify_failed 不进 LLM 工具面）──

/// 构造挂了多插件工具注册表 + 遮挡名单的路由器（工具按描述符的 plugin_id 注册）。
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
    }
}

#[tokio::test]
async fn tool_surface_shadows_verify_pending_plugin_tools() {
    // BUG-51 契约：观测失败按声明注册的插件，工具在 LLM 工具面不可见（注册面
    // 保留，服务/HTTP 面不受影响）；白名单/插件 id 两种配法都不得透出。
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
    assert_eq!(
        schema_names(&result),
        vec!["bash_execute".to_string()],
        "遮挡插件的工具不得进入 LLM 工具面"
    );
}

#[tokio::test]
async fn tool_surface_shadow_keeps_contracts_unfiltered() {
    // 契约表不过滤（既有语义）：遮挡的工具若仍被非 LLM 通道调用，输出校验照常。
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
    assert!(
        contracts.contains_key("hello_pack"),
        "契约表与被过滤的 schema 面解耦"
    );
}

#[tokio::test]
async fn tool_surface_unshadows_when_ledger_clears() {
    // 转正：复验通过（账本 ok）后遮挡名单不再含该插件 → 工具自动回面。
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
    assert!(schema_names(&before).is_empty(), "待复验期间工具不可见");

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
        "复验通过（账本清除）后自动转正"
    );
}

#[tokio::test]
async fn tool_surface_shadow_overrides_force_include() {
    // 必败工具不得借 force_include_tools 强制注入复活（遮挡优先于强制注入）。
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
    assert!(
        schema_names(&result).is_empty(),
        "遮挡插件的工具即使被声明强制注入也不回面"
    );
}

#[tokio::test]
async fn tool_surface_without_declaration_forces_nothing() {
    // P1-5 fail-closed：无任何 force_include_tools 声明 → 零强制注入，
    // 工具面严格等于 tool_ids 白名单（内核不再持有框架名单）。
    let router = router_with_tool_descriptors(&[plain_tool("spill_retrieve")]);
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
    let mut contracted = plain_tool("dsh_read");
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
    assert_eq!(contracts["dsh_read"]["schema"]["required"][0], "path");
    assert_eq!(contracts["dsh_read"]["render"]["card"], "read");
    assert!(contracts.get("legacy_tool").is_none());
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
                .record_run_start(&pid, &tenant, "run_del_1", "h")
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
    let capture = Arc::new(CaptureSink {
        received: received.clone(),
    }) as Arc<dyn agentos_session::EventSink>;
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
    let capture = Arc::new(CaptureSink {
        received: received.clone(),
    }) as Arc<dyn agentos_session::EventSink>;
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
            .record_run_start(pipe, "default", run_id, "h")
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

// ── G6：params 无 _plugin_id → 不做白名单校验（内核自身调用路径）─────────

#[tokio::test]
async fn g6_lookup_present_without_plugin_id_skips_check() {
    let lookup: GrantsLookupFn = Arc::new(|_| Some(vec![])); // 名单为空 = 严格
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_grants_lookup(lookup);
    // 无 _plugin_id（非插件上下文）→ 校验跳过，照常路由
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
        .record_run_start("pipe_idem", "default", "run_idem", "h")
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
    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_rerr", "thread_rerr"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            store
                .record_run_start("pipe_rerr", &tenant, "run_rerr", "h")
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
        },
    )
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
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_invoker(
        Arc::new(CaptureInvoker {
            captured: seen.clone(),
        }),
    );
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
                .record_run_start("pipe_sp_done", &tenant, "run_done", "h")
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
                .record_run_start("pipe_nobo", &tenant, "run_nobo", "h")
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
                    .record_run_start(pipe, &tenant, run_id, "h")
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
                .record_run_start("pipe_rc", &tenant, "run_rc", "h")
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
                .record_run_start("pipe_grs", &tenant, "run_grs", "hash_grs")
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
            assert_eq!(res["config_hash"], "hash_grs");

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
                .record_run_start(&pid, &tenant, "run_del", "h")
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

/// unhandled capability：未注册的 capability.method → 协议错误 + 留痕
/// （logger/config-reader 等死能力的残留调用落点）。
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
        text.contains("unhandled capability call"),
        "残留调用须留痕: {text}"
    );
}
