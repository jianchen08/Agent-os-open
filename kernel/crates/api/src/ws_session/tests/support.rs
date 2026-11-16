//! WS 测试共享夹具（tests 子模块）：鉴权 token 签发、真实 axum+tungstenite
//! 服务/客户端封装、静默 dispatcher、帧读取 helper、捕获型 EventSink 与
//! 3 槽位历史预置。由 tests `mod support` 挂载，兄弟测试模块经
//! `super::support::*` 或 tests 显式清单消费；`session_record` 等夹具仍归
//! tests 序窗，经 `super::` 现取。
use std::sync::{Arc, Mutex};

use agentos_core::traits::StorageBackend;
use agentos_session::{
    router::{InboundRouter, PipelineDispatcher},
    EventSink,
};
use async_trait::async_trait;
use futures_util::StreamExt as _;
use serde_json::json;
use tokio_tungstenite::tungstenite::Message as WsMessage;

use super::session_record;

/// 静默分发器：全部 Ok（聚焦会话收发循环而非派发语义）。
#[cfg(test)]
struct SilentDispatcher;

#[cfg(test)]
#[async_trait]
impl PipelineDispatcher for SilentDispatcher {
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

/// 失败注入分发器：dispatch_user_input 恒 Err（回执可见性 / 忙链入队等
/// 失败路径测试共用，"injected dispatch failure" 原文经 user_input_ack 透出）。
#[cfg(test)]
pub(super) struct ErrUserInputDispatcher;

#[cfg(test)]
#[async_trait]
impl PipelineDispatcher for ErrUserInputDispatcher {
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
        Err("injected dispatch failure".to_string())
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

/// 捕获型 EventSink：记录收到的全部文本帧（与 chat_send_handler 测试同款）。
#[cfg(test)]
pub(super) struct CapturingSink {
    frames: Arc<Mutex<Vec<String>>>,
}

#[cfg(test)]
impl CapturingSink {
    /// 外持 frames 桥构造：调用方保留 Vec 句柄供断言。
    pub(super) fn with_frames(frames: Arc<Mutex<Vec<String>>>) -> Self {
        Self { frames }
    }
}

#[cfg(test)]
#[async_trait]
impl EventSink for CapturingSink {
    async fn send_text(&self, text: &str) -> bool {
        self.frames.lock().unwrap().push(text.to_string());
        true
    }
    fn id(&self) -> u64 {
        7
    }
}

/// 签发合法 access token（HMAC 签名链路与生产同源）。
#[cfg(test)]
pub(super) fn mint_access_token(user_id: &str, username: &str) -> String {
    let user = agentos_http::auth::BuiltInUser {
        id: user_id.to_string(),
        username: username.to_string(),
        password: String::new(),
        email: String::new(),
        role: "user".to_string(),
        tenant_id: agentos_http::auth::DEFAULT_TENANT_ID.to_string(),
        created_at: "2026-09-13T00:00:00Z".to_string(),
        must_change_password: false,
    };
    agentos_http::auth::encode_token(agentos_http::auth::TokenType::Access, &user, 3600)
}

/// e2e 会话态：真实协调器 + 静默 dispatcher 的入站路由。
#[cfg(test)]
pub(super) fn ws_e2e_state(
    session: Arc<agentos_session::SessionCoordinator>,
) -> crate::routes::AppState {
    let mut state = crate::routes::AppState::new();
    state.session = Some(session);
    state.inbound_router = Some(Arc::new(InboundRouter::new(Arc::new(SilentDispatcher))));
    state
}

/// 起真实 axum 服务（随机端口），返回监听地址。
#[cfg(test)]
pub(super) async fn spawn_ws_server(state: crate::routes::AppState) -> std::net::SocketAddr {
    let app = crate::server::build_router(state);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    addr
}

/// tungstenite 客户端连接（握手失败即 panic）。
#[cfg(test)]
pub(super) async fn ws_connect(url: String) -> ClientWs {
    let (ws, _resp) = tokio_tungstenite::connect_async(url)
        .await
        .expect("WS 握手应成功");
    ws
}

/// e2e 客户端流类型。
#[cfg(test)]
pub(super) type ClientWs =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

/// 读下一文本帧（JSON）；Close/错误即 panic（label 标注等待目标）。
#[cfg(test)]
pub(super) async fn next_text_frame(ws: &mut ClientWs, label: &str) -> serde_json::Value {
    loop {
        let frame = tokio::time::timeout(std::time::Duration::from_secs(5), ws.next())
            .await
            .unwrap_or_else(|_| panic!("5s 内未收到 {label} 帧"))
            .expect("流未关闭")
            .expect("帧非错误");
        match frame {
            WsMessage::Text(t) => {
                return serde_json::from_str(t.as_str()).expect("文本帧应为 JSON")
            }
            WsMessage::Close(cf) => panic!("{label}: 意外收到 Close {cf:?}"),
            _ => continue,
        }
    }
}

/// 读下一 Close 帧的状态码（跳过前置文本帧）。
#[cfg(test)]
pub(super) async fn next_close_code(ws: &mut ClientWs, label: &str) -> u16 {
    loop {
        let frame = tokio::time::timeout(std::time::Duration::from_secs(5), ws.next())
            .await
            .unwrap_or_else(|_| panic!("5s 内未收到 {label} Close 帧"))
            .expect("流未关闭")
            .expect("帧非错误");
        if let WsMessage::Close(cf) = frame {
            return cf.map(|c| u16::from(c.code)).unwrap_or(1005);
        }
    }
}

/// 预置 3 槽位历史（user/assistant/tool 配对完整）：seq 0 user、1 assistant
/// （带 tool_calls）、2 tool（配 seq1 的调用）。截断目标 = seq 0。
/// pipeline_id 由调用方传入（测试进程并行共享全局链注册表，同 id 并行测试
/// 会互相占链，各自用专名隔离）。
#[cfg(test)]
pub(super) async fn seed_three_slot_history(store: &Arc<dyn StorageBackend>, pipeline_id: &str) {
    let user = json!({"role": "user", "content": "第一问"});
    let assistant = json!({
        "role": "assistant", "content": "旧回复",
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "bash_execute", "arguments": "{}"}}]
    });
    let tool = json!({
        "role": "tool", "tool_call_id": "call_1",
        "content": "{\"success\":true,\"data\":\"ok\"}", "tool_result": {"success": true}
    });
    // 引擎真实链路 merge_and_project 会给每个 op 注入 _run_id（表侧
    // write_slot_to_table_locked 写入 message_slots.run_id）——预置模拟同款，
    // get_step_traces_by_thread 才能经消息反查到 run 集合。
    store
        .apply_messages_ops_to_table(
            pipeline_id,
            "default",
            &[
                json!({"op": "set", "seq": 0, "msg": user, "_run_id": format!("run-{pipeline_id}")}),
                json!({"op": "set", "seq": 1, "msg": assistant, "_run_id": format!("run-{pipeline_id}")}),
                json!({"op": "set", "seq": 2, "msg": tool, "_run_id": format!("run-{pipeline_id}")}),
            ],
        )
        .await
        .unwrap();
    store
        .record_run_start(
            pipeline_id,
            "default",
            format!("run-{pipeline_id}").as_str(),
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .await
        .unwrap();
    store
        .create_session(&session_record(Some(pipeline_id)))
        .await
        .unwrap();
    // get_step_traces_by_thread 经 pipeline_sessions 映射反查 pipeline_ids
    store
        .link_pipeline_session(pipeline_id, "T1", "default")
        .await
        .unwrap();
}
