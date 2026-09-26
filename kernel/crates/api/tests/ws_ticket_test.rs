// @feature: FP-0.2.七 路由收敛 | @vision: V4 多用户 | @ci: rust-test
//! WS 一次性票据（POST /api/v1/ws-ticket → ?ticket= 握手）行为测试。
//!
//! 契约：
//! - 签发：任意已认证用户 POST /api/v1/ws-ticket → 200 {"ticket","expires_in":60}；
//! - 消费：?ticket= 握手成功一次；复用 / 过期 / 未知票据 → Close(4001)；
//! - ?token= 路径并存保留（外部脚本既有消费方）；无票据无 token → 4001。

use std::sync::Arc;
use std::time::Duration;

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_core::traits::StorageBackend;
use agentos_core::types::PendingInputSource;
use agentos_session::auth::REJECT_CODE_INVALID_TOKEN;
use agentos_session::router::{InboundRouter, PipelineDispatcher};
use agentos_session::SessionCoordinator;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use futures_util::StreamExt;
use serde_json::Value;
use tokio_tungstenite::tungstenite::Message;
use tower::ServiceExt;

const SEED_ADMIN_PW: &str = "test-admin-pw-2026";

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
        _pipeline_config_id: Option<&str>,
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

/// 构造带内存 store（播种 admin）+ session 装配的 app，起真实 TCP 服务。
/// 返回 (addr, 同源 app)——addr 供 WS 真实连接，app 供 HTTP oneshot（登录/签发）。
/// `ticket_ttl` 可注入短 TTL 供过期路径测试（无需等待真实 60s）。
async fn spawn_ws_server(ticket_ttl: Option<Duration>) -> (std::net::SocketAddr, axum::Router) {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    store
        .create_user(&agentos_core::types::UserRecord {
            user_id: "00000000-0000-0000-0000-000000000001".to_string(),
            username: "admin".to_string(),
            password: agentos_http::auth::hash_password(SEED_ADMIN_PW).unwrap(),
            email: Some("admin@agentos.dev".to_string()),
            role: "admin".to_string(),
            tenant_id: "default".to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_login_at: None,
            must_change_password: false,
        })
        .await
        .unwrap();
    let mut state = AppState::new();
    state.store = Some(store);
    if let Some(ttl) = ticket_ttl {
        state.ws_tickets = Arc::new(agentos_api::ws_ticket::WsTicketStore::with_ttl(ttl));
    }
    state.session = Some(Arc::new(SessionCoordinator::new()));
    state.inbound_router = Some(Arc::new(InboundRouter::new(Arc::new(NoopDispatcher))));
    let http_app = build_router(state.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, build_router(state)).await.unwrap();
    });
    (addr, http_app)
}

/// 走同源 app oneshot 登录，返回 admin access token。
async fn login_token(app: &axum::Router) -> String {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/auth/login")
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"username": "admin", "password": SEED_ADMIN_PW}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK, "播种 admin 必须可登录");
    let bytes = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let v: Value = serde_json::from_slice(&bytes).unwrap();
    v["access_token"].as_str().unwrap().to_string()
}

/// 走同源 app oneshot 签发票据，返回 (状态码, 响应 JSON)。
async fn issue_ticket(app: &axum::Router, token: &str) -> (StatusCode, Value) {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/ws-ticket")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let v: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, v)
}

/// 收到的前一帧（None = 500ms 内无帧 = 会话存活）。
async fn next_frame_or_alive(
    ws: &mut tokio_tungstenite::WebSocketStream<
        tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
    >,
) -> Option<u16> {
    match tokio::time::timeout(Duration::from_millis(500), ws.next()).await {
        Ok(Some(Ok(Message::Close(Some(close))))) => Some(u16::from(close.code)),
        Ok(Some(Ok(other))) => panic!("意外帧: {other:?}"),
        Ok(Some(Err(e))) => panic!("流错误: {e}"),
        Ok(None) => panic!("连接被对端关闭"),
        Err(_) => None, // 超时无帧 = 握手通过、会话存活
    }
}

#[tokio::test]
async fn issued_ticket_authenticates_ws_handshake_once() {
    let (addr, app) = spawn_ws_server(None).await;
    let token = login_token(&app).await;
    let (status, body) = issue_ticket(&app, &token).await;
    assert_eq!(status, StatusCode::OK, "已认证用户签发票据必须 200");
    let ticket = body["ticket"].as_str().expect("响应必须携带 ticket");
    assert_eq!(body["expires_in"], 60, "契约：expires_in=60");
    assert_eq!(ticket.len(), 43, "契约：43 位 url-safe 随机票据");

    // 签发 → 握手成功：首帧为 connection_confirmation 文本（非 Close 拒绝帧）
    let (mut ws, _resp) =
        tokio_tungstenite::connect_async(format!("ws://{addr}/ws/chat?ticket={ticket}"))
            .await
            .expect("握手应被 accept");
    match tokio::time::timeout(Duration::from_secs(5), ws.next()).await {
        Ok(Some(Ok(Message::Text(text)))) => {
            let v: Value = serde_json::from_str(&text).unwrap();
            assert_eq!(
                v["type"], "connection_confirmation",
                "有效票据握手必须收到连接确认而非拒绝"
            );
            assert_eq!(v["data"]["user_id"], "00000000-0000-0000-0000-000000000001");
        }
        other => panic!("有效票据握手应收到确认帧，实际: {other:?}"),
    }
    ws.close(None).await.unwrap();

    // 复用同一票据 → 4001
    let (mut ws, _resp) =
        tokio_tungstenite::connect_async(format!("ws://{addr}/ws/chat?ticket={ticket}"))
            .await
            .expect("复用握手同样被 accept（拒绝在帧层表达）");
    assert_eq!(
        next_frame_or_alive(&mut ws).await,
        Some(REJECT_CODE_INVALID_TOKEN),
        "票据单次消费：复用必须 4001"
    );
}

#[tokio::test]
async fn expired_ticket_rejected_with_4001() {
    // 短 TTL（100ms）：签发后等过期再握手
    let (addr, app) = spawn_ws_server(Some(Duration::from_millis(100))).await;
    let token = login_token(&app).await;
    let (_, body) = issue_ticket(&app, &token).await;
    let ticket = body["ticket"].as_str().unwrap().to_string();
    tokio::time::sleep(Duration::from_millis(200)).await;

    let (mut ws, _resp) =
        tokio_tungstenite::connect_async(format!("ws://{addr}/ws/chat?ticket={ticket}"))
            .await
            .expect("过期票据握手同样被 accept（拒绝在帧层表达）");
    assert_eq!(
        next_frame_or_alive(&mut ws).await,
        Some(REJECT_CODE_INVALID_TOKEN),
        "过期票据必须 4001"
    );
}

#[tokio::test]
async fn unknown_or_missing_credentials_rejected_with_4001() {
    let (addr, _app) = spawn_ws_server(None).await;
    // 未知票据
    let (mut ws, _resp) =
        tokio_tungstenite::connect_async(format!("ws://{addr}/ws/chat?ticket=forged-ticket"))
            .await
            .expect("未知票据握手同样被 accept（拒绝在帧层表达）");
    assert_eq!(
        next_frame_or_alive(&mut ws).await,
        Some(REJECT_CODE_INVALID_TOKEN),
        "未知票据必须 4001"
    );

    // 无票据无 token（?token= 既有路径并存：同样缺失 → 4001）
    let (mut ws, _resp) = tokio_tungstenite::connect_async(format!("ws://{addr}/ws/chat"))
        .await
        .expect("无凭据握手同样被 accept（拒绝在帧层表达）");
    assert_eq!(
        next_frame_or_alive(&mut ws).await,
        Some(REJECT_CODE_INVALID_TOKEN),
        "无票据无 token 必须 4001"
    );
}

#[tokio::test]
async fn ticket_issue_requires_authentication() {
    let (_addr, app) = spawn_ws_server(None).await;
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/ws-ticket")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::UNAUTHORIZED,
        "匿名签发票据必须 401"
    );
}

/// oneshot 形态直测签发端点契约（不走真实 TCP，快路径锁响应形状）。
#[tokio::test]
async fn ticket_endpoint_response_shape() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    store
        .create_user(&agentos_core::types::UserRecord {
            user_id: "00000000-0000-0000-0000-000000000001".to_string(),
            username: "admin".to_string(),
            password: agentos_http::auth::hash_password(SEED_ADMIN_PW).unwrap(),
            email: None,
            role: "admin".to_string(),
            tenant_id: "default".to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_login_at: None,
            must_change_password: false,
        })
        .await
        .unwrap();
    let mut state = AppState::new();
    state.store = Some(store);
    let app = build_router(state);

    let login = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/auth/login")
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"username": "admin", "password": SEED_ADMIN_PW}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(login.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(login.into_body(), 8192).await.unwrap();
    let v: Value = serde_json::from_slice(&bytes).unwrap();
    let token = v["access_token"].as_str().unwrap();

    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/ws-ticket")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let v: Value = serde_json::from_slice(&bytes).unwrap();
    assert!(v["ticket"].as_str().is_some());
    assert_eq!(v["expires_in"], 60);
}
