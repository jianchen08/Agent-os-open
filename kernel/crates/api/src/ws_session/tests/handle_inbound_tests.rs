use super::*;

use agentos_session::router::InboundRouter;

use super::dispatch_guards::frame_types;

use super::super::handle_inbound;
use super::support::ErrUserInputDispatcher;
use super::CapturingSink;

// ── 补测：handle_inbound 入站帧处理（解析/归属/心跳/路由错误）─────────

struct NoopDispatcher;

#[async_trait]
impl PipelineDispatcher for NoopDispatcher {
    async fn dispatch_user_input(
        &self,
        _thread_id: &str,
        _user_id: &str,
        _content: &str,
        _pipeline_id: &str,
        _thinking_strength: &str,
        _execution_context: Option<&serde_json::Value>,
        _state_overlay: Option<&serde_json::Value>,
        _agent_id: &str,
        _pipeline_config_id: Option<&str>,
        _cmid: &str,
        _source: agentos_core::types::PendingInputSource,
    ) -> Result<(), String> {
        Ok(())
    }
    async fn dispatch_interaction_response(
        &self,
        _thread_id: &str,
        _request_id: &str,
        _response: &serde_json::Value,
    ) -> Result<(), String> {
        Ok(())
    }
    async fn dispatch_stop(&self, _thread_id: &str, _pipeline_id: &str) -> Result<(), String> {
        Ok(())
    }
}

async fn inbound_frames_after(
    text: &str,
    store: Option<&Arc<dyn StorageBackend>>,
    last_sequence: Option<u64>,
) -> Vec<(String, serde_json::Value)> {
    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    handle_inbound(
        text,
        "u1",
        &coord,
        &router,
        last_sequence,
        &std::sync::atomic::AtomicBool::new(false),
        store,
    )
    .await;
    frame_types(&frames).await
}

#[tokio::test]
async fn inbound_heartbeat_replies_ack_without_thread_registration() {
    // 心跳必须回 ack（前端连续收不到 ack 判死断连）；无有效 thread_id 不注册
    let frames = inbound_frames_after(
        json!({"type": "heartbeat"}).to_string().as_str(),
        None,
        None,
    )
    .await;
    let ack = frames
        .iter()
        .find(|(t, _)| t == "heartbeat_ack")
        .map(|(_, v)| v.clone())
        .expect("心跳必须回 heartbeat_ack");
    assert!(
        ack["data"]["timestamp"].as_str().is_some(),
        "ack 携带时间戳: {ack}"
    );
}

#[tokio::test]
async fn inbound_empty_thread_id_skips_registration_but_still_ack() {
    // thread_id 空串不构成注册坐标（注册表不收空键），心跳语义照常
    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    handle_inbound(
        json!({"type": "heartbeat", "thread_id": ""})
            .to_string()
            .as_str(),
        "u1",
        &coord,
        &router,
        None,
        &std::sync::atomic::AtomicBool::new(false),
        None,
    )
    .await;
    assert!(
        coord.registry().get_user_for_thread("").is_none(),
        "空 thread_id 不得进注册表"
    );
    let all = frame_types(&frames).await;
    assert!(all.iter().any(|(t, _)| t == "heartbeat_ack"));
}

#[tokio::test]
async fn inbound_malformed_json_is_dropped_silently() {
    // 畸形帧丢弃（限频 warn 可观测）：不 panic、无出站帧、无注册
    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    handle_inbound(
        "not-json{{{",
        "u1",
        &coord,
        &router,
        None,
        &std::sync::atomic::AtomicBool::new(false),
        None,
    )
    .await;
    assert!(frames.lock().unwrap().is_empty(), "畸形帧不得产生出站帧");
    assert!(coord.registry().get_user_for_thread("T1").is_none());
}

#[tokio::test]
async fn inbound_registers_owned_thread_and_replays_once_per_connection() {
    // 归属校验放行 → 注册 thread→user；B3 回放每连接仅一次（首个带 thread_id
    // 的入站消息触发，此后不再重放）。
    let owned =
        Arc::new(ResolveMock::new(Ok(vec!["P-1".to_string()]), None)) as Arc<dyn StorageBackend>;

    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    let replayed = std::sync::atomic::AtomicBool::new(false);
    let frame = json!({"type": "user_input", "thread_id": "T-owned", "content": "hi"});

    // 首帧：注册 + （last_sequence>0 时）回放缺失误差
    handle_inbound(
        frame.to_string().as_str(),
        "u1",
        &coord,
        &router,
        Some(7),
        &replayed,
        Some(&owned),
    )
    .await;
    assert_eq!(
        coord.registry().get_user_for_thread("T-owned").as_deref(),
        Some("u1"),
        "本租户 thread 注册放行"
    );

    // 二帧：注册幂等，回放不二次触发（无重复帧、无 panic）
    handle_inbound(
        frame.to_string().as_str(),
        "u1",
        &coord,
        &router,
        Some(7),
        &replayed,
        Some(&owned),
    )
    .await;
    assert_eq!(
        coord.registry().get_user_for_thread("T-owned").as_deref(),
        Some("u1")
    );
}

#[tokio::test]
async fn inbound_rejects_unowned_thread_registration() {
    // 无管道关联的 thread：不注册（事件单播/回放路由不被外来 thread 劫持）
    let foreign = Arc::new(ResolveMock::new(Ok(vec![]), None)) as Arc<dyn StorageBackend>;
    let coord = agentos_session::SessionCoordinator::new();
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    handle_inbound(
        json!({"type": "user_input", "thread_id": "T-foreign", "content": "hi"})
            .to_string()
            .as_str(),
        "u1",
        &coord,
        &router,
        None,
        &std::sync::atomic::AtomicBool::new(false),
        Some(&foreign),
    )
    .await;
    assert!(
        coord.registry().get_user_for_thread("T-foreign").is_none(),
        "无关联 thread 必须拒绝注册"
    );
}

#[tokio::test]
async fn inbound_route_error_replies_user_input_ack_failure_frame() {
    // 路由错误（分发失败）必须对客户端可见：回 user_input_ack(ok:false) 错误
    // 帧（带 client_message_id/thread_id/error 原文），warn 仅留痕不再静默吞。
    // （2026-09-29 装机 R302 实证：旧实现只写 WARN，前端「思考中」8 分钟无声。）
    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(ErrUserInputDispatcher));
    handle_inbound(
        json!({
            "type": "user_input",
            "thread_id": "T1",
            "content": "hi",
            "client_message_id": "cm-err-1",
        })
        .to_string()
        .as_str(),
        "u1",
        &coord,
        &router,
        None,
        &std::sync::atomic::AtomicBool::new(false),
        None,
    )
    .await;
    let all = frame_types(&frames).await;
    let ack = all
        .iter()
        .find(|(t, _)| t == "user_input_ack")
        .map(|(_, v)| v.clone())
        .expect("派发失败必须回 user_input_ack(ok:false) 错误帧");
    assert_eq!(ack["ok"], false, "失败回执 ok 必须 false: {ack}");
    assert_eq!(ack["client_message_id"], "cm-err-1", "回执带幂等键: {ack}");
    assert_eq!(ack["thread_id"], "T1", "回执带线程坐标: {ack}");
    let err_text = ack["error"].as_str().unwrap_or_default();
    assert!(
        err_text.contains("injected dispatch failure"),
        "error 携带失败原文: {ack}"
    );
}

#[tokio::test]
async fn inbound_user_input_success_replies_ack_ok_frame() {
    // 派发受理（含直跑/入队两态）即回 user_input_ack(ok:true)——前端恒挂
    // 超时据此撤销；无回执则「已送达」不可知，与失败路径同为盲区。
    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    handle_inbound(
        json!({
            "type": "user_input",
            "thread_id": "T1",
            "content": "hi",
            "client_message_id": "cm-ok-1",
        })
        .to_string()
        .as_str(),
        "u1",
        &coord,
        &router,
        None,
        &std::sync::atomic::AtomicBool::new(false),
        None,
    )
    .await;
    let all = frame_types(&frames).await;
    let ack = all
        .iter()
        .find(|(t, _)| t == "user_input_ack")
        .map(|(_, v)| v.clone())
        .expect("派发受理必须回 user_input_ack(ok:true)");
    assert_eq!(ack["ok"], true, "成功回执 ok 必须 true: {ack}");
    assert_eq!(ack["client_message_id"], "cm-ok-1", "回执带幂等键: {ack}");
    assert_eq!(ack["thread_id"], "T1");
    assert!(ack.get("error").is_none(), "成功回执不携带 error 键: {ack}");
}

#[tokio::test]
async fn inbound_ack_reads_client_message_id_from_data_envelope() {
    // cmid 顶层优先、data 信封兜底（与 pipeline_id 同法，契约只增不删）
    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    handle_inbound(
        json!({
            "type": "user_input",
            "thread_id": "T1",
            "data": {"content": "hi", "client_message_id": "cm-env-1"},
        })
        .to_string()
        .as_str(),
        "u1",
        &coord,
        &router,
        None,
        &std::sync::atomic::AtomicBool::new(false),
        None,
    )
    .await;
    let all = frame_types(&frames).await;
    let ack = all
        .iter()
        .find(|(t, _)| t == "user_input_ack")
        .map(|(_, v)| v.clone())
        .expect("data 信封 cmid 同样回执");
    assert_eq!(ack["client_message_id"], "cm-env-1", "信封兜底提取: {ack}");
}

#[tokio::test]
async fn inbound_non_user_input_route_error_replies_route_error_frame() {
    // 非 user_input 帧的路由错误回 route_error 通用错误帧（message_type 标明
    // 来源类型），不冒用 user_input_ack 语义；帧字段尽力提取（缺为空串）。
    let coord = agentos_session::SessionCoordinator::new();
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register("u1", Arc::new(CapturingSink::with_frames(frames.clone())));
    let router = InboundRouter::new(Arc::new(NoopDispatcher));
    handle_inbound(
        // interaction_response 缺 thread_id → RouteOutcome::Error（不经 dispatcher）
        json!({"type": "interaction_response", "data": {"request_id": "r-1"}})
            .to_string()
            .as_str(),
        "u1",
        &coord,
        &router,
        None,
        &std::sync::atomic::AtomicBool::new(false),
        None,
    )
    .await;
    let all = frame_types(&frames).await;
    let err_frame = all
        .iter()
        .find(|(t, _)| t == "route_error")
        .map(|(_, v)| v.clone())
        .expect("非 user_input 路由错误必须回 route_error 帧");
    assert_eq!(
        err_frame["data"]["message_type"], "interaction_response",
        "标明来源消息类型: {err_frame}"
    );
    assert!(
        err_frame["data"]["error"]
            .as_str()
            .unwrap_or_default()
            .contains("interaction_response 缺少 thread_id"),
        "error 携带失败原文: {err_frame}"
    );
    assert!(
        all.iter().all(|(t, _)| t != "user_input_ack"),
        "非 user_input 错误不得冒用 user_input_ack"
    );
}
