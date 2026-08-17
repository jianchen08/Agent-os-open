// @feature: FP-0.2.七 路由收敛 | @ci: rust-test
//! WS 单连接踢旧（B10）关闭码测试：同一 user 第二连接注册时，第一连接必须
//! 收到带`CLOSE_CODE_KICKED`（4000）状态码的 Close 帧。
//!
//! 背景（2026-08-17 风暴复盘）：旧实现踢旧走空串哨兵 → `sender.close()` 发空
//! Close（浏览器 onclose=1000/1005）→ 前端 GlobalWebSocket 按普通掉线 4s 退避
//! 重连 → 双客户端（ZCode webview + Edge 标签页）互踢无限循环，实测每 ~4.5s
//! 一踢、发送按钮随连接闪烁、消息完全发不出去。前端对 4000 已有"被新连接替换
//! 跳过重连"分支（GlobalWebSocket.ts onclose），内核补齐带码关闭即可断根。

use std::sync::Arc;
use std::time::Duration;

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_session::router::{InboundRouter, PipelineDispatcher};
use agentos_session::SessionCoordinator;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use futures_util::StreamExt;
use tokio_tungstenite::tungstenite::Message;
use tower::ServiceExt;

/// 本测试只验证"连接注册/踢旧"，不发送业务消息——分发给 no-op。
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
    async fn dispatch_stop(&self, _thread_id: &str) -> Result<(), String> {
        Ok(())
    }
}

/// 内置 admin 登录拿 access_token（AppState 无 store 时回退内置用户表）。
async fn admin_token(app: &axum::Router) -> String {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(axum::http::Method::POST)
                .uri("/api/v1/auth/login")
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({"username": "admin", "password": "admin12345"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let v: Value = serde_json::from_slice(&body).unwrap();
    v["access_token"].as_str().unwrap().to_string()
}

#[tokio::test]
async fn ws_second_connection_kicks_old_with_coded_close() {
    let mut state = AppState::new();
    state.session = Some(Arc::new(SessionCoordinator::new()));
    state.inbound_router = Some(Arc::new(InboundRouter::new(Arc::new(NoopDispatcher))));
    let app = build_router(state.clone());

    // 登录拿 token（token 从 oneshot 的 router 取，无需依赖已监听实例）
    let token = admin_token(&app).await;

    // 起真实 TCP 服务（WS upgrade 必须走网络栈）
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });

    let url = |t: &str| format!("ws://{addr}/ws/chat?token={t}");

    // 连接 A：首个注册，无踢旧，应收到 connection_confirmation
    let (mut ws_a, _resp) = tokio_tungstenite::connect_async(url(&token))
        .await
        .expect("A 应能建立 WS");
    let first_a = tokio::time::timeout(Duration::from_secs(5), ws_a.next())
        .await
        .expect("A 应在 5s 内收到帧")
        .expect("A 流未关闭")
        .expect("A 帧非错误");
    assert!(
        matches!(first_a, Message::Text(ref t) if t.contains("connection_confirmation")),
        "A 首帧应为 connection_confirmation，实际: {first_a:?}"
    );

    // 连接 B：同一 user 注册 → 踢旧 A。A 必须收到带 CLOSE_CODE_KICKED 的 Close 帧。
    let (_ws_b, _) = tokio_tungstenite::connect_async(url(&token))
        .await
        .expect("B 应能建立 WS");

    let closed = tokio::time::timeout(Duration::from_secs(5), ws_a.next())
        .await
        .expect("A 应在 5s 内收到踢旧 Close")
        .expect("A 流未关闭")
        .expect("A 帧非错误");

    match closed {
        Message::Close(Some(frame)) => {
            assert_eq!(
                u16::from(frame.code),
                agentos_session::auth::CLOSE_CODE_KICKED,
                "踢旧必须带 CLOSE_CODE_KICKED 关闭码（前端据 4000 跳过重连，断互踢风暴）"
            );
        }
        other => panic!("A 应收到 Close 帧，实际: {other:?}"),
    }

    server.abort();
}