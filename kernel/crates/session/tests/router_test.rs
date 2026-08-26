// @feature: FP-0.2.八 多租户核心系统 | @vision: V4 多用户 | @ci: rust-test

//! 入站路由测试——user_input/interaction_response/stop_generation/heartbeat 分发（ADR §7.2）。

use agentos_core::types::PendingInputSource;
use agentos_session::router::{InboundRouter, PipelineDispatcher, RouteOutcome};
use async_trait::async_trait;
use std::sync::Arc;
use std::sync::Mutex;

/// (thread_id, user_id, content, thinking_strength, client_message_id, agent_id)
type UserInputRecord = (String, String, String, String, String, String);

/// 记录型 mock dispatcher，捕获每次调用。
#[derive(Default)]
struct MockDispatcher {
    user_inputs: Arc<Mutex<Vec<UserInputRecord>>>,
    interactions: Arc<Mutex<Vec<(String, String)>>>, // (thread_id, request_id)
    stops: Arc<Mutex<Vec<String>>>,                  // thread_id
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
        _state_overlay: Option<&serde_json::Value>,
        agent_id: &str,
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
    async fn dispatch_stop(&self, thread_id: &str) -> Result<(), String> {
        self.stops.lock().unwrap().push(thread_id.into());
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
    // 2026-08-24 阶段1：router 不再硬编码 "agentos"——agent 解析归
    // dispatch_user_input 实现侧（线程绑定 registry → DB → agentos）。
    // 空串 = 未指定，由 EngineDispatcher 按绑定解析；任务派发等显式路径
    // 走 chat.send_message 的 agent_id 参数，不经此路由。
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
        "WS 主会话路径 agent_id 应传空串（未指定，由 dispatcher 解析）"
    );
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
    assert_eq!(stops[0], "thread-1");
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
        async fn dispatch_stop(&self, _: &str) -> Result<(), String> {
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
