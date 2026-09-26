// @feature: FP-0.2.八 多租户核心系统 | @vision: V4 多用户 | @ci: rust-test

//! 入站路由测试——user_input/interaction_response/stop_generation/heartbeat 分发（ADR §7.2）。

use agentos_core::types::PendingInputSource;
use agentos_session::router::{InboundRouter, PipelineDispatcher, RouteOutcome};
use async_trait::async_trait;
use std::sync::Arc;
use std::sync::Mutex;

/// (thread_id, user_id, content, thinking_strength, client_message_id, agent_id,
///  state_overlay)
type UserInputRecord = (
    String,
    String,
    String,
    String,
    String,
    String,
    Option<serde_json::Value>,
);

/// regenerate 记录：(thread_id, pipeline_id, user_message_id, new_content)
type RegenerateRecord = (String, String, String, Option<String>);

/// 记录型 mock dispatcher，捕获每次调用。
#[derive(Default)]
struct MockDispatcher {
    user_inputs: Arc<Mutex<Vec<UserInputRecord>>>,
    interactions: Arc<Mutex<Vec<(String, String)>>>, // (thread_id, request_id)
    stops: Arc<Mutex<Vec<(String, String)>>>,        // (thread_id, pipeline_id)
    regenerates: Arc<Mutex<Vec<RegenerateRecord>>>,
}

#[async_trait]
impl PipelineDispatcher for MockDispatcher {
    async fn dispatch_user_input(
        &self,
        thread_id: &str,
        user_id: &str,
        content: &str,
        _pipeline_id: &str,
        thinking_strength: &str,
        _execution_context: Option<&serde_json::Value>,
        state_overlay: Option<&serde_json::Value>,
        agent_id: &str,
        _pipeline_config_id: Option<&str>,
        client_message_id: &str,
        _source: PendingInputSource,
    ) -> Result<(), String> {
        self.user_inputs.lock().unwrap().push((
            thread_id.into(),
            user_id.into(),
            content.into(),
            thinking_strength.into(),
            client_message_id.into(),
            agent_id.into(),
            state_overlay.cloned(),
        ));
        Ok(())
    }
    async fn dispatch_interaction_response(
        &self,
        thread_id: &str,
        request_id: &str,
        _response: &serde_json::Value,
    ) -> Result<(), String> {
        self.interactions
            .lock()
            .unwrap()
            .push((thread_id.into(), request_id.into()));
        Ok(())
    }
    async fn dispatch_stop(&self, thread_id: &str, pipeline_id: &str) -> Result<(), String> {
        self.stops
            .lock()
            .unwrap()
            .push((thread_id.into(), pipeline_id.into()));
        Ok(())
    }
    async fn dispatch_regenerate(
        &self,
        _user_id: &str,
        thread_id: &str,
        pipeline_id: &str,
        user_message_id: &str,
        new_content: Option<&str>,
    ) -> Result<(), String> {
        self.regenerates.lock().unwrap().push((
            thread_id.into(),
            pipeline_id.into(),
            user_message_id.into(),
            new_content.map(|s| s.to_string()),
        ));
        Ok(())
    }
}

fn router() -> (InboundRouter, Arc<MockDispatcher>) {
    let dispatcher = Arc::new(MockDispatcher::default());
    let r = InboundRouter::new(dispatcher.clone());
    (r, dispatcher)
}

#[tokio::test]
async fn user_input_routed_to_dispatcher() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "user_input",
        "thread_id": "thread-1",
        "data": {"content": "hello"},
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let inputs = dispatcher.user_inputs.lock().unwrap();
    assert_eq!(inputs.len(), 1);
    assert_eq!(inputs[0].0, "thread-1");
    assert_eq!(inputs[0].1, "user-A");
    assert_eq!(inputs[0].2, "hello");
    assert_eq!(inputs[0].3, "", "未指定强度 → 空串");
}

#[tokio::test]
async fn user_input_carries_thinking_strength() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "user_input",
        "thread_id": "thread-1",
        "content": "hi",
        "thinking_strength": "high",
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let inputs = dispatcher.user_inputs.lock().unwrap();
    assert_eq!(inputs[0].3, "high", "顶层 thinking_strength 应透传");
}

#[tokio::test]
async fn user_input_carries_thinking_strength_via_data_envelope() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "user_input",
        "thread_id": "thread-1",
        "data": {"content": "hi", "thinking_strength": "low"},
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let inputs = dispatcher.user_inputs.lock().unwrap();
    assert_eq!(inputs[0].3, "low", "data 信封 thinking_strength 应透传");
}

#[tokio::test]
async fn user_input_carries_client_message_id_top_level() {
    // ADR 2026-08-21 消息幂等契约：前端 GlobalWebSocket.sendUserInput 把
    // client_message_id 放顶层，路由必须提取并透传给引擎（落库回显）。
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "user_input",
        "thread_id": "thread-1",
        "content": "hi",
        "client_message_id": "0198abcd-1111",
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let inputs = dispatcher.user_inputs.lock().unwrap();
    assert_eq!(
        inputs[0].4, "0198abcd-1111",
        "顶层 client_message_id 应透传"
    );
}

#[tokio::test]
async fn user_input_carries_client_message_id_via_data_envelope() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "user_input",
        "thread_id": "thread-1",
        "data": {"content": "hi", "client_message_id": "0198abcd-2222"},
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let inputs = dispatcher.user_inputs.lock().unwrap();
    assert_eq!(
        inputs[0].4, "0198abcd-2222",
        "data 信封 client_message_id 应透传"
    );
}

#[tokio::test]
async fn user_input_without_client_message_id_defaults_empty() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "user_input",
        "thread_id": "thread-1",
        "content": "hi",
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let inputs = dispatcher.user_inputs.lock().unwrap();
    assert_eq!(inputs[0].4, "", "无幂等键 → 空串（触发器/旧客户端路径）");
}

#[tokio::test]
async fn user_input_dispatches_unspecified_agent_id() {
    // 帧不带 agent_id = 未指定：agent_id 槽位传空串（解析归 dispatch 实现
    // 侧），且不合成交道 overlay——缺省行为与无此字段时代码路径逐字节一致。
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "user_input",
        "thread_id": "thread-1",
        "data": {"content": "hello"},
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let inputs = dispatcher.user_inputs.lock().unwrap();
    assert_eq!(
        inputs[0].5, "",
        "帧不带 agent_id → agent_id 槽位为空串（未指定）"
    );
    assert!(
        inputs[0].6.is_none(),
        "帧不带 agent_id → 不合成 overlay（沿用既有管道身份）"
    );
}

// ── agent_id 透传（消息级执行身份：帧显式携带 → dispatcher 收到 + 单键
//    overlay {"agent.id"}；形态非法按缺失处理）──────────────────────────

#[tokio::test]
async fn user_input_carries_agent_id_top_level_with_identity_overlay() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "user_input",
        "thread_id": "thread-1",
        "content": "hi",
        "agent_id": "persona_x",
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let inputs = dispatcher.user_inputs.lock().unwrap();
    assert_eq!(inputs[0].5, "persona_x", "帧内 agent_id 透传到 dispatcher");
    assert_eq!(
        inputs[0].6,
        Some(serde_json::json!({"agent.id": "persona_x"})),
        "合成单键 overlay：身份经 agent.id 持久键消息级落入管道 state"
    );
}

#[tokio::test]
async fn user_input_carries_agent_id_via_data_envelope() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "user_input",
        "thread_id": "thread-1",
        "data": {"content": "hi", "agent_id": "persona_y"},
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let inputs = dispatcher.user_inputs.lock().unwrap();
    assert_eq!(inputs[0].5, "persona_y", "data 信封位置同样提取");
    assert_eq!(
        inputs[0].6,
        Some(serde_json::json!({"agent.id": "persona_y"}))
    );
}

#[tokio::test]
async fn user_input_invalid_agent_id_treated_as_absent() {
    // 非法形态一律按缺失处理（锁定：忽略而非拒绝——可选提示性字段不得让
    // 消息投递失败，与 execution_context 非对象按缺失同法）。逐一覆盖：
    // 含 ".."、含空白、超 128 字符、空串、非字符串。
    for (label, bad) in [
        ("含路径穿越点", serde_json::json!("../evil")),
        ("含空白", serde_json::json!("bad id")),
        ("超 128 字符", serde_json::json!("a".repeat(129))),
        ("空串", serde_json::json!("")),
        ("非字符串", serde_json::json!(42)),
    ] {
        let (router, dispatcher) = router();
        let msg = serde_json::json!({
            "type": "user_input",
            "thread_id": "thread-1",
            "content": "hi",
            "agent_id": bad,
        });
        let outcome = router.route(&msg, "user-A").await;
        assert_eq!(outcome, RouteOutcome::Handled, "{label}: 消息仍应投递");
        let inputs = dispatcher.user_inputs.lock().unwrap();
        assert_eq!(inputs[0].5, "", "{label}: agent_id 按缺失（空串）");
        assert!(inputs[0].6.is_none(), "{label}: 不合成 overlay");
    }
}

#[tokio::test]
async fn user_input_agent_id_boundary_length_accepted() {
    // 恰 128 字符（上界）合法：形态校验拒绝的是越界而非长度本身。
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "user_input",
        "thread_id": "thread-1",
        "content": "hi",
        "agent_id": "a".repeat(128),
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let inputs = dispatcher.user_inputs.lock().unwrap();
    assert_eq!(inputs[0].5, "a".repeat(128));
    assert!(inputs[0].6.is_some());
}

#[tokio::test]
async fn interaction_response_routed_to_dispatcher() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "interaction_response",
        "thread_id": "thread-1",
        "data": {"request_id": "req-42"},
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let inter = dispatcher.interactions.lock().unwrap();
    assert_eq!(inter.len(), 1);
    assert_eq!(inter[0].1, "req-42");
}

#[tokio::test]
async fn stop_generation_routed_to_dispatcher() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "stop_generation",
        "thread_id": "thread-1",
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let stops = dispatcher.stops.lock().unwrap();
    assert_eq!(stops.len(), 1);
    assert_eq!(stops[0], ("thread-1".to_string(), String::new()));
}

#[tokio::test]
async fn stop_generation_routes_pipeline_id() {
    // 前端携带 pipeline_id（正在查看的管道，含子任务管道）→ 透传给 dispatcher
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "stop_generation",
        "thread_id": "thread-1",
        "pipeline_id": "p-subtask",
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let stops = dispatcher.stops.lock().unwrap();
    assert_eq!(stops[0], ("thread-1".to_string(), "p-subtask".to_string()));
}

// ── regenerate 路由（批次 D）：重新生成/回退/编辑重发 ──

#[tokio::test]
async fn regenerate_routed_with_default_target() {
    // 缺省 user_message_id（=最后一条 user）→ 空串透传，实现侧解析
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "regenerate",
        "thread_id": "thread-1",
        "pipeline_id": "p1",
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let regens = dispatcher.regenerates.lock().unwrap();
    assert_eq!(regens.len(), 1);
    assert_eq!(regens[0].0, "thread-1");
    assert_eq!(regens[0].1, "p1");
    assert_eq!(regens[0].2, "", "缺省 user_message_id → 空串（=重新生成）");
    assert_eq!(regens[0].3, None, "无 new_content");
}

#[tokio::test]
async fn regenerate_routed_with_edit_resend_content() {
    // 编辑重发：指定目标 user 消息 + new_content（data 信封位置兼容）
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "regenerate",
        "thread_id": "thread-1",
        "data": {
            "pipeline_id": "p1",
            "user_message_id": "mc_abc",
            "new_content": "改写后的问题",
        },
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let regens = dispatcher.regenerates.lock().unwrap();
    assert_eq!(regens[0].1, "p1");
    assert_eq!(regens[0].2, "mc_abc");
    assert_eq!(regens[0].3.as_deref(), Some("改写后的问题"));
}

#[tokio::test]
async fn regenerate_pure_retry_empty_new_content_maps_to_none() {
    // 前端 sendRegenerate 恒带 new_content 字段：纯重试时为空串——空串是
    // 「无编辑意图」，必须按缺省（None）透传；若按 Some("") 走编辑分支，
    // 目标 user 槽位会被改写为空内容（真机 E2E 实证缺陷）
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "regenerate",
        "thread_id": "thread-1",
        "pipeline_id": "p1",
        "user_message_id": "mc_last",
        "new_content": "",
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let regens = dispatcher.regenerates.lock().unwrap();
    assert_eq!(regens[0].2, "mc_last");
    assert_eq!(
        regens[0].3, None,
        "空串 new_content → None（纯重试，不改写）"
    );
}

#[tokio::test]
async fn regenerate_missing_thread_id_returns_error() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({"type": "regenerate"});
    let outcome = router.route(&msg, "user-A").await;
    assert!(matches!(outcome, RouteOutcome::Error(_)));
    assert!(
        dispatcher.regenerates.lock().unwrap().is_empty(),
        "缺 thread_id 不应分发"
    );
}

#[tokio::test]
async fn regenerate_default_noop_dispatcher_is_handled() {
    // trait 默认实现（no-op）覆盖：未实现 dispatch_regenerate 的 dispatcher
    // 收到 regenerate → Handled（向后兼容：旧实现不受新消息类型影响）
    struct NoRegenerate;
    #[async_trait]
    impl PipelineDispatcher for NoRegenerate {
        async fn dispatch_user_input(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: Option<&serde_json::Value>,
            _: Option<&serde_json::Value>,
            _: &str,
            _: Option<&str>,
            _: &str,
            _: PendingInputSource,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_interaction_response(
            &self,
            _: &str,
            _: &str,
            _: &serde_json::Value,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_stop(&self, _: &str, _: &str) -> Result<(), String> {
            Ok(())
        }
        // dispatch_regenerate 用 trait 默认实现（no-op）
    }
    let router = InboundRouter::new(Arc::new(NoRegenerate));
    let msg = serde_json::json!({
        "type": "regenerate",
        "thread_id": "thread-1",
        "user_message_id": "mc_x",
    });
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Handled, "默认实现应放行");
}

#[tokio::test]
async fn regenerate_dispatcher_failure_returns_error() {
    // route_regenerate 的 Err 分支：dispatcher 报错 → RouteOutcome::Error 透传
    struct FailingRegenerate;
    #[async_trait]
    impl PipelineDispatcher for FailingRegenerate {
        async fn dispatch_user_input(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: Option<&serde_json::Value>,
            _: Option<&serde_json::Value>,
            _: &str,
            _: Option<&str>,
            _: &str,
            _: PendingInputSource,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_interaction_response(
            &self,
            _: &str,
            _: &str,
            _: &serde_json::Value,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_stop(&self, _: &str, _: &str) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_regenerate(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: Option<&str>,
        ) -> Result<(), String> {
            Err("boom".into())
        }
    }
    let router = InboundRouter::new(Arc::new(FailingRegenerate));
    let msg = serde_json::json!({"type": "regenerate", "thread_id": "thread-1"});
    let outcome = router.route(&msg, "user-A").await;
    assert!(matches!(outcome, RouteOutcome::Error(_)));
}

#[tokio::test]
async fn heartbeat_returns_heartbeat_ack_outcome() {
    let (router, _dispatcher) = router();
    let msg = serde_json::json!({"type": "heartbeat"});
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Heartbeat);
}

#[tokio::test]
async fn unknown_type_returns_ignored() {
    let (router, _dispatcher) = router();
    let msg = serde_json::json!({"type": "mystery"});
    let outcome = router.route(&msg, "user-A").await;
    assert_eq!(outcome, RouteOutcome::Ignored);
}

#[tokio::test]
async fn user_input_missing_thread_id_returns_error() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "user_input",
        "data": {"content": "hello"},
    });
    let outcome = router.route(&msg, "user-A").await;
    assert!(
        matches!(outcome, RouteOutcome::Error(_)),
        "缺 thread_id 应返回 Error"
    );
    assert!(
        dispatcher.user_inputs.lock().unwrap().is_empty(),
        "缺 thread_id 不应分发"
    );
}

#[tokio::test]
async fn invalid_json_returns_error() {
    let (router, _dispatcher) = router();
    let outcome = router.route_raw("not json", "user-A").await;
    assert!(matches!(outcome, RouteOutcome::Error(_)));
}

#[tokio::test]
async fn dispatcher_failure_returns_error() {
    struct FailingDispatcher;
    #[async_trait]
    impl PipelineDispatcher for FailingDispatcher {
        async fn dispatch_user_input(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: Option<&serde_json::Value>,
            _: Option<&serde_json::Value>,
            _: &str,
            _: Option<&str>,
            _: &str,
            _: PendingInputSource,
        ) -> Result<(), String> {
            Err("boom".into())
        }
        async fn dispatch_interaction_response(
            &self,
            _: &str,
            _: &str,
            _: &serde_json::Value,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_stop(&self, _: &str, _: &str) -> Result<(), String> {
            Ok(())
        }
    }
    let router = InboundRouter::new(Arc::new(FailingDispatcher));
    let msg = serde_json::json!({
        "type": "user_input",
        "thread_id": "thread-1",
        "data": {"content": "hi"},
    });
    let outcome = router.route(&msg, "user-A").await;
    assert!(matches!(outcome, RouteOutcome::Error(_)));
}

// ── interaction_response：缺 thread_id 拒绝、response 整体透传、dispatcher 失败上抛 ──

#[tokio::test]
async fn interaction_response_missing_thread_id_returns_error() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "interaction_response",
        "data": {"request_id": "req-1"},
    });
    let outcome = router.route(&msg, "user-A").await;
    assert!(
        matches!(outcome, RouteOutcome::Error(_)),
        "缺 thread_id 应返回 Error"
    );
    assert!(
        dispatcher.interactions.lock().unwrap().is_empty(),
        "缺 thread_id 不应分发"
    );
}

/// response 体（response_type/selected_option/feedback）整体透传：
/// 交互插件按它唤醒 wait_for_choice，路由层不得裁剪字段。
#[tokio::test]
async fn interaction_response_passes_response_body_through() {
    struct ResponseCapturing;
    #[async_trait]
    impl PipelineDispatcher for ResponseCapturing {
        async fn dispatch_user_input(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: Option<&serde_json::Value>,
            _: Option<&serde_json::Value>,
            _: &str,
            _: Option<&str>,
            _: &str,
            _: PendingInputSource,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_interaction_response(
            &self,
            _: &str,
            _: &str,
            response: &serde_json::Value,
        ) -> Result<(), String> {
            assert_eq!(
                response["selected_option"], "opt-b",
                "selected_option 应原样透传"
            );
            assert_eq!(response["response_type"], "choice");
            Ok(())
        }
        async fn dispatch_stop(&self, _: &str, _: &str) -> Result<(), String> {
            Ok(())
        }
    }
    let router = InboundRouter::new(Arc::new(ResponseCapturing));
    let msg = serde_json::json!({
        "type": "interaction_response",
        "thread_id": "thread-1",
        "data": {
            "request_id": "req-9",
            "response": {"response_type": "choice", "selected_option": "opt-b"},
        },
    });
    assert_eq!(router.route(&msg, "user-A").await, RouteOutcome::Handled);
}

/// 缺 data.response → 空 response（Value::Null）仍分发：审批响应体可选。
#[tokio::test]
async fn interaction_response_without_body_dispatches_null() {
    struct NullBodyChecker;
    #[async_trait]
    impl PipelineDispatcher for NullBodyChecker {
        async fn dispatch_user_input(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: Option<&serde_json::Value>,
            _: Option<&serde_json::Value>,
            _: &str,
            _: Option<&str>,
            _: &str,
            _: PendingInputSource,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_interaction_response(
            &self,
            _: &str,
            _: &str,
            response: &serde_json::Value,
        ) -> Result<(), String> {
            assert!(response.is_null(), "缺响应体时透传 Null");
            Ok(())
        }
        async fn dispatch_stop(&self, _: &str, _: &str) -> Result<(), String> {
            Ok(())
        }
    }
    let router = InboundRouter::new(Arc::new(NullBodyChecker));
    let msg = serde_json::json!({
        "type": "interaction_response",
        "thread_id": "thread-1",
        "data": {"request_id": "req-1"},
    });
    assert_eq!(router.route(&msg, "user-A").await, RouteOutcome::Handled);
}

#[tokio::test]
async fn interaction_response_dispatcher_failure_returns_error() {
    struct Fail;
    #[async_trait]
    impl PipelineDispatcher for Fail {
        async fn dispatch_user_input(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: Option<&serde_json::Value>,
            _: Option<&serde_json::Value>,
            _: &str,
            _: Option<&str>,
            _: &str,
            _: PendingInputSource,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_interaction_response(
            &self,
            _: &str,
            _: &str,
            _: &serde_json::Value,
        ) -> Result<(), String> {
            Err("interaction 唤醒失败".into())
        }
        async fn dispatch_stop(&self, _: &str, _: &str) -> Result<(), String> {
            Ok(())
        }
    }
    let failing = InboundRouter::new(Arc::new(Fail));
    let msg = serde_json::json!({
        "type": "interaction_response",
        "thread_id": "thread-1",
        "data": {"request_id": "req-1"},
    });
    match failing.route(&msg, "user-A").await {
        RouteOutcome::Error(e) => assert_eq!(e, "interaction 唤醒失败"),
        other => panic!("应上抛 dispatcher 错误，实际 {other:?}"),
    }
}

// ── stop_generation：缺 thread_id 拒绝、pipeline_id 顶层/data 双位置、失败上抛 ──

#[tokio::test]
async fn stop_generation_missing_thread_id_returns_error() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({"type": "stop_generation"});
    let outcome = router.route(&msg, "user-A").await;
    assert!(matches!(outcome, RouteOutcome::Error(_)));
    assert!(dispatcher.stops.lock().unwrap().is_empty());
}

#[tokio::test]
async fn stop_generation_reads_pipeline_id_from_data_envelope() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "stop_generation",
        "thread_id": "thread-1",
        "data": {"pipeline_id": "p-from-data"},
    });
    assert_eq!(router.route(&msg, "user-A").await, RouteOutcome::Handled);
    let stops = dispatcher.stops.lock().unwrap();
    assert_eq!(stops[0].1, "p-from-data", "data 信封兜底");
}

/// 顶层 pipeline_id 与 data 信封并存 → 顶层优先（与 field_or_data 契约一致）。
#[tokio::test]
async fn stop_generation_top_level_pipeline_id_wins_over_data() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "stop_generation",
        "thread_id": "thread-1",
        "pipeline_id": "p-top",
        "data": {"pipeline_id": "p-data"},
    });
    assert_eq!(router.route(&msg, "user-A").await, RouteOutcome::Handled);
    assert_eq!(dispatcher.stops.lock().unwrap()[0].1, "p-top");
}

#[tokio::test]
async fn stop_generation_dispatcher_failure_returns_error() {
    struct Fail;
    #[async_trait]
    impl PipelineDispatcher for Fail {
        async fn dispatch_user_input(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: Option<&serde_json::Value>,
            _: Option<&serde_json::Value>,
            _: &str,
            _: Option<&str>,
            _: &str,
            _: PendingInputSource,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_interaction_response(
            &self,
            _: &str,
            _: &str,
            _: &serde_json::Value,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_stop(&self, _: &str, _: &str) -> Result<(), String> {
            Err("取消失败".into())
        }
    }
    let router = InboundRouter::new(Arc::new(Fail));
    let msg = serde_json::json!({"type": "stop_generation", "thread_id": "t1"});
    match router.route(&msg, "user-A").await {
        RouteOutcome::Error(e) => assert_eq!(e, "取消失败"),
        other => panic!("应上抛 dispatcher 错误，实际 {other:?}"),
    }
}

// ── active_thread_changed：排队优先级键更新（本路由全部分支此前未覆盖）──

/// 记录 active_thread 分发：(user_id, thread_id, pipeline_id)。
#[derive(Default)]
struct ActiveThreadRecorder {
    calls: Arc<Mutex<Vec<(String, String, String)>>>,
}

#[async_trait]
impl PipelineDispatcher for ActiveThreadRecorder {
    async fn dispatch_user_input(
        &self,
        _: &str,
        _: &str,
        _: &str,
        _: &str,
        _: &str,
        _: Option<&serde_json::Value>,
        _: Option<&serde_json::Value>,
        _: &str,
        _: Option<&str>,
        _: &str,
        _: PendingInputSource,
    ) -> Result<(), String> {
        Ok(())
    }
    async fn dispatch_interaction_response(
        &self,
        _: &str,
        _: &str,
        _: &serde_json::Value,
    ) -> Result<(), String> {
        Ok(())
    }
    async fn dispatch_stop(&self, _: &str, _: &str) -> Result<(), String> {
        Ok(())
    }
    async fn dispatch_active_thread(
        &self,
        user_id: &str,
        thread_id: &str,
        pipeline_id: &str,
    ) -> Result<(), String> {
        self.calls
            .lock()
            .unwrap()
            .push((user_id.into(), thread_id.into(), pipeline_id.into()));
        Ok(())
    }
}

fn active_router() -> (InboundRouter, Arc<ActiveThreadRecorder>) {
    let d = Arc::new(ActiveThreadRecorder::default());
    (InboundRouter::new(d.clone()), d)
}

#[tokio::test]
async fn active_thread_changed_routes_user_thread_and_pipeline() {
    let (router, rec) = active_router();
    let msg = serde_json::json!({
        "type": "active_thread_changed",
        "thread_id": "thread-7",
        "pipeline_id": "p-7",
    });
    assert_eq!(router.route(&msg, "user-Z").await, RouteOutcome::Handled);
    let calls = rec.calls.lock().unwrap();
    assert_eq!(
        calls.as_slice(),
        &[(
            "user-Z".to_string(),
            "thread-7".to_string(),
            "p-7".to_string()
        )]
    );
}

#[tokio::test]
async fn active_thread_changed_reads_pipeline_id_from_data_envelope() {
    let (router, rec) = active_router();
    let msg = serde_json::json!({
        "type": "active_thread_changed",
        "thread_id": "thread-8",
        "data": {"pipeline_id": "p-8"},
    });
    assert_eq!(router.route(&msg, "user-Z").await, RouteOutcome::Handled);
    assert_eq!(rec.calls.lock().unwrap()[0].2, "p-8");
}

/// pipeline_id 两处皆缺 → 空串（dispatcher 侧回退 thread 主管道）。
#[tokio::test]
async fn active_thread_changed_without_pipeline_id_defaults_empty() {
    let (router, rec) = active_router();
    let msg = serde_json::json!({
        "type": "active_thread_changed",
        "thread_id": "thread-9",
    });
    assert_eq!(router.route(&msg, "user-Z").await, RouteOutcome::Handled);
    assert_eq!(rec.calls.lock().unwrap()[0].2, "", "缺省空串");
}

#[tokio::test]
async fn active_thread_changed_missing_thread_id_returns_error() {
    let (router, rec) = active_router();
    let msg = serde_json::json!({
        "type": "active_thread_changed",
        "pipeline_id": "p",
    });
    match router.route(&msg, "user-Z").await {
        RouteOutcome::Error(e) => assert!(e.contains("thread_id"), "错误文案应指明缺字段: {e}"),
        other => panic!("缺 thread_id 应 Error，实际 {other:?}"),
    }
    assert!(rec.calls.lock().unwrap().is_empty(), "缺字段不应分发");
}

#[tokio::test]
async fn active_thread_changed_dispatcher_failure_returns_error() {
    struct Fail;
    #[async_trait]
    impl PipelineDispatcher for Fail {
        async fn dispatch_user_input(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: Option<&serde_json::Value>,
            _: Option<&serde_json::Value>,
            _: &str,
            _: Option<&str>,
            _: &str,
            _: PendingInputSource,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_interaction_response(
            &self,
            _: &str,
            _: &str,
            _: &serde_json::Value,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_stop(&self, _: &str, _: &str) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_active_thread(&self, _: &str, _: &str, _: &str) -> Result<(), String> {
            Err("活跃管道更新失败".into())
        }
    }
    let router = InboundRouter::new(Arc::new(Fail));
    let msg = serde_json::json!({"type": "active_thread_changed", "thread_id": "t"});
    match router.route(&msg, "user-A").await {
        RouteOutcome::Error(e) => assert_eq!(e, "活跃管道更新失败"),
        other => panic!("应上抛 dispatcher 错误，实际 {other:?}"),
    }
}

/// trait 默认实现（no-op）覆盖：未实现 dispatch_active_thread 的 dispatcher
/// 收到 active_thread_changed → Handled（旧实现不受新消息类型影响）。
#[tokio::test]
async fn active_thread_changed_default_noop_dispatcher_is_handled() {
    struct NoActiveThread;
    #[async_trait]
    impl PipelineDispatcher for NoActiveThread {
        async fn dispatch_user_input(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: Option<&serde_json::Value>,
            _: Option<&serde_json::Value>,
            _: &str,
            _: Option<&str>,
            _: &str,
            _: PendingInputSource,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_interaction_response(
            &self,
            _: &str,
            _: &str,
            _: &serde_json::Value,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_stop(&self, _: &str, _: &str) -> Result<(), String> {
            Ok(())
        }
        // dispatch_active_thread 用 trait 默认实现（no-op）
    }
    let router = InboundRouter::new(Arc::new(NoActiveThread));
    let msg = serde_json::json!({
        "type": "active_thread_changed",
        "thread_id": "thread-1",
    });
    assert_eq!(router.route(&msg, "user-A").await, RouteOutcome::Handled);
}

// ── 非字符串字段视为缺失（field_or_data 的 as_str 过滤）──

/// thread_id 存在但非字符串 → 与缺失同判（不得拼出一个 "null" 线程）。
/// 表驱动覆盖四类入站消息。
#[tokio::test]
async fn non_string_thread_id_is_treated_as_missing() {
    let cases: Vec<(&str, serde_json::Value)> = vec![
        ("user_input", serde_json::json!(123)),
        ("interaction_response", serde_json::json!({"a": 1})),
        ("stop_generation", serde_json::json!(true)),
        ("regenerate", serde_json::json!([])),
        ("active_thread_changed", serde_json::json!(7.5)),
    ];
    let (router, dispatcher) = router();
    for (msg_type, bad_thread) in cases {
        let msg = serde_json::json!({"type": msg_type, "thread_id": bad_thread});
        let outcome = router.route(&msg, "user-A").await;
        assert!(
            matches!(outcome, RouteOutcome::Error(_)),
            "{msg_type} 的非字符串 thread_id 应按缺失处理，实际 {outcome:?}"
        );
    }
    assert!(dispatcher.user_inputs.lock().unwrap().is_empty());
    assert!(dispatcher.interactions.lock().unwrap().is_empty());
    assert!(dispatcher.stops.lock().unwrap().is_empty());
    assert!(dispatcher.regenerates.lock().unwrap().is_empty());
}

/// 空串 thread_id 与缺失同判（避免建出无名线程）。
#[tokio::test]
async fn empty_thread_id_is_rejected() {
    let (router, dispatcher) = router();
    let msg = serde_json::json!({
        "type": "user_input",
        "thread_id": "",
        "data": {"content": "hi"},
    });
    assert!(matches!(
        router.route(&msg, "user-A").await,
        RouteOutcome::Error(_)
    ));
    assert!(dispatcher.user_inputs.lock().unwrap().is_empty());
}

/// user_input 的顶层 content 兜底（前端 GlobalWebSocket.sendUserInput 现状）
/// 与 data.content 优先（未来标准契约）——表驱动两位置 × 有/无值。
#[tokio::test]
async fn user_input_content_falls_back_between_top_level_and_data() {
    let cases: Vec<(serde_json::Value, &str, &str)> = vec![
        (
            serde_json::json!({"content": "top"}),
            "top",
            "仅顶层 content",
        ),
        (
            serde_json::json!({"data": {"content": "data"}}),
            "data",
            "仅 data.content",
        ),
        (
            serde_json::json!({"content": "top", "data": {"content": "data"}}),
            "data",
            "两者并存 → data 优先",
        ),
        (serde_json::json!({}), "", "两者皆缺 → 空串"),
    ];
    let (router, dispatcher) = router();
    for (extra, expected, why) in cases {
        let mut msg = serde_json::json!({"type": "user_input", "thread_id": "thread-1"});
        if let Some(obj) = extra.as_object() {
            for (k, v) in obj {
                msg[k] = v.clone();
            }
        }
        assert_eq!(
            router.route(&msg, "user-A").await,
            RouteOutcome::Handled,
            "{why}"
        );
        let inputs = dispatcher.user_inputs.lock().unwrap();
        assert_eq!(inputs.last().unwrap().2, expected, "{why}");
    }
}

/// user_input 的 pipeline_id：顶层优先、data 兜底（分发成功即证明该提取链
/// 各位置组合均正常；具体取值经 stop_generation 用例断言同一 field_or_data）。
#[tokio::test]
async fn user_input_pipeline_id_resolution_order() {
    let cases: Vec<(&str, serde_json::Value)> = vec![
        ("top", serde_json::json!("p-top")),
        ("data-only", serde_json::json!(null)),
        ("both", serde_json::json!("p-top")),
    ];
    let (router, dispatcher) = router();
    for (label, top) in cases {
        let mut msg = serde_json::json!({
            "type": "user_input",
            "thread_id": "thread-1",
            "data": {"content": "hi", "pipeline_id": "p-data"},
        });
        if !top.is_null() {
            msg["pipeline_id"] = top;
        }
        assert_eq!(
            router.route(&msg, "user-A").await,
            RouteOutcome::Handled,
            "{label} 位置组合应正常分发"
        );
        let inputs = dispatcher.user_inputs.lock().unwrap();
        assert_eq!(inputs.last().unwrap().0, "thread-1");
    }
}

/// 非对象 execution_context（数组/字符串/数字）按缺失处理：不得把非法结构
/// 注入引擎 initial_state。
#[tokio::test]
async fn non_object_execution_context_is_ignored() {
    struct EcRecorder(Arc<Mutex<usize>>);
    #[async_trait]
    impl PipelineDispatcher for EcRecorder {
        async fn dispatch_user_input(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            execution_context: Option<&serde_json::Value>,
            _: Option<&serde_json::Value>,
            _: &str,
            _: Option<&str>,
            _: &str,
            _: PendingInputSource,
        ) -> Result<(), String> {
            if execution_context.is_some() {
                *self.0.lock().unwrap() += 1;
            }
            Ok(())
        }
        async fn dispatch_interaction_response(
            &self,
            _: &str,
            _: &str,
            _: &serde_json::Value,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_stop(&self, _: &str, _: &str) -> Result<(), String> {
            Ok(())
        }
    }
    let seen = Arc::new(Mutex::new(0usize));
    let router = InboundRouter::new(Arc::new(EcRecorder(seen.clone())));
    for bad in [
        serde_json::json!([1, 2]),
        serde_json::json!("ec"),
        serde_json::json!(42),
    ] {
        let msg = serde_json::json!({
            "type": "user_input",
            "thread_id": "thread-1",
            "content": "hi",
            "execution_context": bad,
        });
        assert_eq!(router.route(&msg, "user-A").await, RouteOutcome::Handled);
    }
    assert_eq!(*seen.lock().unwrap(), 0, "非对象值不得作为执行上下文下发");
}

/// route_raw 合法 JSON 走通（对照 invalid_json 用例）。
#[tokio::test]
async fn route_raw_parses_valid_json() {
    let (router, dispatcher) = router();
    let outcome = router
        .route_raw(
            r#"{"type":"user_input","thread_id":"t-1","content":"raw"}"#,
            "user-A",
        )
        .await;
    assert_eq!(outcome, RouteOutcome::Handled);
    let inputs = dispatcher.user_inputs.lock().unwrap();
    assert_eq!(inputs[0].2, "raw");
}
