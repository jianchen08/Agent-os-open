// @feature: FP-0.2.一 插件协议 | @ci: rust-test
// 由 capability_router.rs 的主 #[cfg(test)] 测试块体平移而来（保留私有项访问）。

use super::*;
use serde_json::json;

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
                .join("../../../config/kernel_capabilities"),
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
    // 引擎管道家族（llm_core）不声明 streaming 也放行（内核 LLM 路径器官），
    // 但命名空间必须 a_（内核签发）。
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
    async fn invoke_pipeline_plugin(
        &self,
        _plugin_id: &str,
        _ctx: &agentos_core::types::PluginContext,
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

/// 固定返回错误的 invoker（验证 tool-executor.invoke 的错误归一化）。
struct ErroringInvoker;
#[async_trait::async_trait]
impl agentos_core::traits::PluginInvoker for ErroringInvoker {
    async fn invoke_pipeline_plugin(
        &self,
        _plugin_id: &str,
        _ctx: &agentos_core::types::PluginContext,
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

// ── M2：service-registry capability（用真实内存 SqliteStore 验证端到端）──

/// 构造一个注入了内存 store 的路由器。
// ── event-bus.emit_domain（ADR 2026-08-28 事件下沉底座） ──────────────

#[tokio::test]
async fn emit_domain_broadcasts_to_domain_broadcaster() {
    let got: std::sync::Arc<std::sync::Mutex<Vec<(String, Vec<(String, serde_json::Value)>)>>> =
        std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink = got.clone();
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_domain_broadcaster(Arc::new(move |name: &str, tags: Vec<(String, serde_json::Value)>| {
            sink.lock().unwrap().push((name.to_string(), tags));
        }));
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
        json!({"pipeline_id": pid, "status": "failed", "task.status": "failed"}),
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
    assert_eq!(row["status"], "failed");
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

// ── F-REVIEW-2：pipeline-executor.get_run_status（复盘轮询真实完成）──

#[tokio::test]
async fn test_pipeline_executor_get_run_status_ok() {
    // 建 run（模拟 start_run 后的 runs 表记录）→ get_run_status 返回状态
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store.clone());
    store
        .create_run("run_status_1", "hash", "default")
        .await
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
    store
        .update_run_status(
            "run_status_1",
            agentos_core::types::RunStatus::Completed,
            None,
            None,
        )
        .await
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
    // 先建 run 并记录管道归属（SqliteStore 的 set_run_pipeline 真实写）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let router =
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_store(store.clone());

    agentos_tenant::scope(
        agentos_core::types::TenantContext::new("tenant_sr", "thread_sr"),
        async {
            let tenant = agentos_tenant::current_or_default("default").tenant_id;
            store.create_run("run_sr_1", "h", &tenant).await.unwrap();
            store
                .set_run_pipeline("run_sr_1", "pipe_task_9")
                .await
                .unwrap();
            store.create_run("run_sr_2", "h", &tenant).await.unwrap();
            store
                .set_run_pipeline("run_sr_2", "pipe_task_9")
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

            // resume：恢复最新 suspended run
            let r3 = router
                .handle(
                    "pipeline-executor",
                    "resume_pipeline",
                    json!({"pipeline_id": "pipe_task_9"}),
                )
                .await
                .unwrap();
            assert_eq!(r3["run_id"], "run_sr_2");
            let got2 = store.get_run("run_sr_2").await.unwrap();
            assert_eq!(got2.status, agentos_core::types::RunStatus::Running);

            // 无管道 → 幂等 ok（run_id 空）
            let r4 = router
                .handle(
                    "pipeline-executor",
                    "resume_pipeline",
                    json!({"pipeline_id": "pipe_ghost"}),
                )
                .await
                .unwrap();
            assert_eq!(r4["run_id"], "");
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
