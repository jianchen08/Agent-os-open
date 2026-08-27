// @feature: FP-0.2.七 路由收敛 | @ci: rust-test
//! WS 握手认证拒绝关闭码测试：无效/缺失 token 的握手必须收到带 4001 状态码的
//! Close 帧，而不是被直接 drop（浏览器 onclose 只能看到 1006）。
//!
//! 背景：`run_ws_session` 拒绝分支直接 return 时 socket 被 drop 且无 Close 帧
//! → 浏览器 `event.code === 1006` → 前端
//! GlobalWebSocket 的「4001 → refreshToken → 重连」自愈路径永远不触发；
//! 叠加 localStorage 缺 refresh_token 时被当瞬时故障无限重试，表现为
//! 「未连接」常驻、发消息无响应、永不弹登录。

use std::sync::Arc;
use std::time::Duration;

use agentos_core::types::PendingInputSource;
use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_session::auth::{REJECT_CODE_INVALID_TOKEN, REJECT_CODE_NO_TOKEN};
use agentos_session::router::{InboundRouter, PipelineDispatcher};
use agentos_session::SessionCoordinator;
use futures_util::StreamExt;
use tokio_tungstenite::tungstenite::Message;

struct NoopDispatcher;

#[async_trait::async_trait]
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
        _cmid: &str,
        _source: PendingInputSource,
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

async fn spawn_ws_server() -> std::net::SocketAddr {
    let mut state = AppState::new();
    state.session = Some(Arc::new(SessionCoordinator::new()));
    state.inbound_router = Some(Arc::new(InboundRouter::new(Arc::new(NoopDispatcher))));
    let app = build_router(state.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    addr
}

/// 无效 token：握手 accept 后必须收到 Close(4001) 帧（不能裸 drop 出 1006）。
#[tokio::test]
async fn ws_invalid_token_rejected_with_coded_close() {
    let addr = spawn_ws_server().await;
    let (mut ws, _resp) =
        tokio_tungstenite::connect_async(format!("ws://{addr}/ws/chat?token=garbage-token"))
            .await
            .expect("握手应被 accept（拒绝在帧层表达）");

    let frame = tokio::time::timeout(Duration::from_secs(5), ws.next())
        .await
        .expect("应在 5s 内收到拒绝帧")
        .expect("流未关闭")
        .expect("帧非错误");

    match frame {
        Message::Close(Some(close)) => {
            assert_eq!(
                u16::from(close.code),
                REJECT_CODE_INVALID_TOKEN,
                "无效 token 拒绝必须带 4001（前端据 4001 触发 refreshToken 自愈）"
            );
        }
        other => panic!("应收到 Close(4001) 帧，实际: {other:?}"),
    }
}

/// 缺失 token：同上，必须 Close(4001) 而非裸 drop。
#[tokio::test]
async fn ws_missing_token_rejected_with_coded_close() {
    let addr = spawn_ws_server().await;
    let (mut ws, _resp) = tokio_tungstenite::connect_async(format!("ws://{addr}/ws/chat"))
        .await
        .expect("握手应被 accept（拒绝在帧层表达）");

    let frame = tokio::time::timeout(Duration::from_secs(5), ws.next())
        .await
        .expect("应在 5s 内收到拒绝帧")
        .expect("流未关闭")
        .expect("帧非错误");

    match frame {
        Message::Close(Some(close)) => {
            assert_eq!(
                u16::from(close.code),
                REJECT_CODE_NO_TOKEN,
                "缺失 token 拒绝必须带 4001（前端据 4001 触发 refreshToken 自愈）"
            );
        }
        other => panic!("应收到 Close(4001) 帧，实际: {other:?}"),
    }
}
