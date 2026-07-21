//! Axum HTTP/WebSocket API 服务器
//!
//! 提供 RESTful API 端点和 WebSocket 流式通信。
//! AC-06-3: /health 返回 200
//! AC-06-4: WebSocket 可连接收发消息
//! AC-06-5: Schema 聚合端点
//!
//! [来源: docs/tasks/task_07_llm_api.md]

use std::net::SocketAddr;

use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        State,
    },
    response::IntoResponse,
    routing::{get, post},
    Router,
};
use serde::{Deserialize, Serialize};
use tracing::info;

use crate::auth::{login_handler, logout_handler, me_handler, refresh_handler, register_handler};
use crate::error::ApiError;
use crate::routes::{
    agents_handler, health_handler, pipelines_handler, schema_handler, tools_handler, AppState,
};

/// WebSocket 消息请求体。
#[derive(Debug, Deserialize, Serialize)]
pub struct WsRequest {
    #[serde(default)]
    pub message: String,
    #[serde(default)]
    pub session_id: String,
}

/// WebSocket 消息响应体。
#[derive(Debug, Serialize)]
pub struct WsResponse {
    pub r#type: String,
    pub content: String,
    pub session_id: String,
    pub timestamp: String,
}

/// 构建 API 路由树。
pub fn build_router(state: AppState) -> Router {
    Router::new()
        // AC-06-3: 健康检查
        .route("/health", get(health_handler))
        // AC-06-5: Schema 聚合端点
        .route("/api/v1/schema", get(schema_handler))
        .route("/api/v1/agents", get(agents_handler))
        .route("/api/v1/pipelines", get(pipelines_handler))
        .route("/api/v1/tools", get(tools_handler))
        // AC-06-4: WebSocket 端点
        .route("/ws", get(ws_handler))
        // 消息发送端点（REST fallback for WS）
        .route("/api/v1/chat", post(chat_handler))
        // Auth 端点
        .route("/api/v1/auth/login", post(login_handler))
        .route("/api/v1/auth/me", get(me_handler))
        .route("/api/v1/auth/refresh", post(refresh_handler))
        .route("/api/v1/auth/logout", post(logout_handler))
        .route("/api/v1/auth/register", post(register_handler))
        .with_state(state)
}

/// WebSocket 连接处理器（AC-06-4）。
async fn ws_handler(ws: WebSocketUpgrade, State(state): State<AppState>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_ws_connection(socket, state))
}

/// 处理 WebSocket 连接——收发消息循环。
async fn handle_ws_connection(socket: WebSocket, _state: AppState) {
    let (mut sender, mut receiver) = socket.split();

    // 发送欢迎消息
    let welcome = WsResponse {
        r#type: "connected".to_string(),
        content: "WebSocket connected to Lingxi AgentOS 0.2".to_string(),
        session_id: Uuid::new_v4().to_string(),
        timestamp: chrono::Utc::now().to_rfc3339(),
    };
    let welcome_json = serde_json::to_string(&welcome).unwrap_or_default();
    let _ = sender.send(Message::Text(welcome_json.into())).await;

    info!("WebSocket connection established");

    // 收发循环
    while let Some(Ok(msg)) = receiver.next().await {
        match msg {
            Message::Text(text) => {
                // 解析客户端消息
                let req: WsRequest = serde_json::from_str(&text).unwrap_or(WsRequest {
                    message: text.to_string(),
                    session_id: String::new(),
                });

                // 构造响应（简化：echo + 时间戳）
                let response = WsResponse {
                    r#type: "message".to_string(),
                    content: format!("Echo: {}", req.message),
                    session_id: req.session_id,
                    timestamp: chrono::Utc::now().to_rfc3339(),
                };

                let response_json = serde_json::to_string(&response).unwrap_or_default();
                if sender
                    .send(Message::Text(response_json.into()))
                    .await
                    .is_err()
                {
                    break;
                }
            }
            Message::Binary(_) => {
                // 忽略二进制消息
            }
            Message::Close(_) => {
                info!("WebSocket connection closed");
                break;
            }
            _ => {}
        }
    }
}

/// /api/v1/chat POST 端点——非 WebSocket 消息发送。
async fn chat_handler(
    State(_state): State<AppState>,
    axum::Json(req): axum::Json<WsRequest>,
) -> Result<axum::Json<WsResponse>, ApiError> {
    let response = WsResponse {
        r#type: "message".to_string(),
        content: format!("Response to: {}", req.message),
        session_id: req.session_id,
        timestamp: chrono::Utc::now().to_rfc3339(),
    };
    Ok(axum::Json(response))
}

/// 启动 API 服务器。
pub async fn start_server(addr: SocketAddr, state: AppState) -> Result<(), ApiError> {
    let app = build_router(state);
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("Failed to bind {}: {}", addr, e),
        })?;
    info!("API server starting on {}", addr);
    axum::serve(listener, app)
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("Server error: {}", e),
        })?;
    Ok(())
}

use futures_util::{SinkExt, StreamExt};
use uuid::Uuid;

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use serde_json::json;
    use tower::ServiceExt;

    #[tokio::test]
    async fn test_health_returns_200() {
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/health")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn test_schema_returns_200() {
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/schema")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn test_agents_returns_200() {
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/agents")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn test_pipelines_returns_200() {
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/pipelines")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn test_tools_returns_200() {
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/tools")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn test_chat_post_returns_200() {
        let app = build_router(AppState::new());
        let body = serde_json::to_string(&WsRequest {
            message: "hello".to_string(),
            session_id: "s1".to_string(),
        })
        .unwrap();
        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/chat")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn test_health_response_body() {
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/health")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(json["status"], "ok");
        assert!(json["version"].is_string());
    }

    #[tokio::test]
    async fn test_schema_with_config() {
        let config = json!({
            "agents": [{"id": "agent1", "name": "Test Agent"}],
            "pipelines": [{"id": "default", "name": "Default Pipeline"}],
            "tools": [{"name": "search", "description": "Search tool"}],
            "routes": {"input": ["plugin1"], "output": ["plugin2"]}
        });
        let app = build_router(AppState::with_config(config));
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/schema")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(json["agents"].as_array().unwrap().len(), 1);
        assert_eq!(json["pipelines"].as_array().unwrap().len(), 1);
        assert_eq!(json["tools"].as_array().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn test_chat_response_content() {
        let app = build_router(AppState::new());
        let body = serde_json::to_string(&WsRequest {
            message: "hello world".to_string(),
            session_id: "test_session".to_string(),
        })
        .unwrap();
        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/chat")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(json["type"], "message");
        assert!(json["content"].as_str().unwrap().contains("hello world"));
        assert_eq!(json["session_id"], "test_session");
    }
}
