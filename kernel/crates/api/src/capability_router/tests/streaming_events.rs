use super::*;

use std::sync::Arc;

/// 捕获 event-bus 推送到 sink 的文本（验证 tool 事件转发）。
pub(super) struct CaptureSink {
    received: std::sync::Arc<std::sync::Mutex<Vec<Value>>>,
}

impl CaptureSink {
    /// 外持 received 桥构造：调用方保留句柄供断言。
    pub(super) fn with_received(received: std::sync::Arc<std::sync::Mutex<Vec<Value>>>) -> Self {
        Self { received }
    }
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
pub(super) fn router_with_session(
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
pub(super) fn router_with_streaming_gate(
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

// ── 合宿身份 → 成员 streaming 声明归约（2026-11-10）────────────────────
// 反调 `_plugin_id` 是进程身份：合宿时是 `group:<group>:<slot>`，manifest 里
// 只有成员 id。精确匹配会把整组合宿进程判成"未声明 streaming"→ 事件全被丢
// （实测 group:light:1 的 plugin_error 9 次全拒）。本组用例锁归约口径。

fn streaming_decl(
    conduit: bool,
    events: Option<&[&str]>,
) -> Option<agentos_core::traits::StreamingCapability> {
    Some(agentos_core::traits::StreamingCapability {
        conduit,
        events: events.map(|list| list.iter().map(|s| (*s).to_string()).collect()),
        part_types: None,
        persist: None,
    })
}

/// 全部成员装箱到 light 组（与生产 `host_group: light` 同形）。
fn host_key_of_light(_id: &str) -> String {
    "group:light:1".to_string()
}

#[test]
fn streaming_declaration_exclusive_id_hits_exact_manifest() {
    // 独占身份：精确命中，不做归约。
    let entries = vec![
        (
            "llm_service".to_string(),
            streaming_decl(false, Some(&["plugin_error"])),
        ),
        (
            "other".to_string(),
            streaming_decl(false, Some(&["stream_chunk"])),
        ),
    ];
    let decl = streaming_declaration_for_caller(&entries, host_key_of_light, "llm_service")
        .expect("独占身份必须命中");
    assert!(!decl.conduit);
    assert_eq!(decl.events, Some(vec!["plugin_error".to_string()]));
}

#[test]
fn streaming_declaration_group_key_reduces_to_member_union() {
    // 合宿宿主键：归约到成员声明并集（events 并集 + 去重）。
    let entries = vec![
        (
            "llm_service".to_string(),
            streaming_decl(false, Some(&["plugin_error"])),
        ),
        (
            "tool_core".to_string(),
            streaming_decl(true, Some(&["tool_start", "plugin_error"])),
        ),
        ("idle".to_string(), None),
    ];
    let decl = streaming_declaration_for_caller(&entries, host_key_of_light, "group:light:1")
        .expect("合宿宿主键必须归约到成员声明");
    // 混装组不得升格为 conduit（conduit 取全体成员）。
    assert!(!decl.conduit);
    assert_eq!(
        decl.events,
        Some(vec!["plugin_error".to_string(), "tool_start".to_string()])
    );
}

#[test]
fn streaming_declaration_group_all_conduit_stays_conduit() {
    // 全 conduit 组（生产 group:llm 形态）：保持引擎器官豁免与 a_ 命名空间。
    let entries = vec![
        ("pipeline_llm_core".to_string(), streaming_decl(true, None)),
        ("pipeline_tool_core".to_string(), streaming_decl(true, None)),
    ];
    let decl = streaming_declaration_for_caller(&entries, host_key_of_light, "group:light:1")
        .expect("全 conduit 组必须命中");
    assert!(decl.conduit);
    assert_eq!(decl.events, None); // 成员未设限 → 归约结果同样不设限
}

#[test]
fn streaming_declaration_unknown_caller_is_fail_closed() {
    // 未申报宿主的调用方 → None（fail-closed，声明闸照旧拒绝）。
    let entries = vec![(
        "llm_service".to_string(),
        streaming_decl(false, Some(&["plugin_error"])),
    )];
    assert!(
        streaming_declaration_for_caller(&entries, host_key_of_light, "group:other:2").is_none()
    );
}
