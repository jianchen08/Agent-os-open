//! Axum HTTP/WebSocket API 服务器
//!
//! 提供 RESTful API 端点和 WebSocket 流式通信。
//! AC-06-3: /health 返回 200
//! AC-06-4: WebSocket 可连接收发消息
//! AC-06-5: Schema 聚合端点
//!
//! [来源: docs/tasks/task_07_llm_api.md]

use std::net::SocketAddr;
use std::sync::Arc;

use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        State,
    },
    http::HeaderMap,
    response::IntoResponse,
    routing::{get, post},
    Router,
};
use agentos_core::types::TenantContext;
use serde::{Deserialize, Serialize};
use tracing::{info, warn};

use crate::auth::{
    login_handler, logout_handler, me_handler, refresh_handler, register_handler,
    resolve_request_tenant_id,
};
use crate::error::ApiError;
use crate::routes::{
    agents_handler, get_plugin_config_with_etag, health_handler, pipelines_handler,
    put_plugin_config_handler, schema_handler, tools_handler, AppState,
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
        // P1-4: 插件配置读写端点（manifest config_files 映射）
        .route(
            "/api/v1/plugins/{id}/config/{file_id}",
            get(get_plugin_config_with_etag).put(put_plugin_config_handler),
        )
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
///
/// P2：若 AppState 启用了 session（`enable_session`），走内核化会话路径
/// （握手鉴权 + 连接注册 + 入站路由）；否则降级为旧 echo/engine 路径（兼容）。
async fn ws_handler(
    ws: WebSocketUpgrade,
    headers: HeaderMap,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
    State(state): State<AppState>,
) -> impl IntoResponse {
    // P2 内核化路径
    if let (Some(session), Some(router)) = (state.session.clone(), state.inbound_router.clone()) {
        let token = params.get("token").cloned();
        return ws
            .on_upgrade(move |socket| run_p2_ws_session(socket, session, router, token));
    }
    // 降级路径（旧 echo/engine，未启用 session 时）
    ws.on_upgrade(move |socket| handle_ws_connection(socket, state, headers))
}

/// P2 内核化 WS 会话包装：握手鉴权 + 会话运行 + 拒绝时 accept+close。
async fn run_p2_ws_session(
    socket: WebSocket,
    session: Arc<agentos_session::SessionCoordinator>,
    router: Arc<agentos_session::router::InboundRouter>,
    token: Option<String>,
) {
    let mut user_id = None;
    let (code, reason) = crate::ws_session::run_ws_session(
        socket,
        session,
        router,
        token.as_deref(),
        &mut user_id,
    )
    .await;
    if code != 1000 {
        info!(code, reason = %reason, "WS 握手拒绝（P2 内核化路径）");
    }
    // 握手拒绝时 run_ws_session 已提前返回，socket 尚未 accept；
    // axum WebSocketUpgrade 在 on_upgrade 回调里已 accept，拒绝码仅作日志。
}

/// 根据请求头解析当前租户上下文。
///
/// 多租户 P0-4：从 Authorization token 解析（或回退到默认租户），
/// 注入到 [`agentos_tenant::scope`] 后，engine/store 通过 task_local 读取。
pub(crate) fn request_tenant_ctx(headers: &HeaderMap, session_id: &str) -> TenantContext {
    TenantContext::new(resolve_request_tenant_id(headers), session_id)
}

/// 通过 0.2 配置驱动管道引擎处理消息。
///
/// 替代旧的"遍历全部 pipeline 插件"placeholder：改为构造
/// [`agentos_engine::PipelineExecutor`]，读取 AppState 中的 `pipeline_config`
/// + `step_library`，按 YAML 定义的 step 顺序执行（三级命中规则）。
///
/// 流程：
/// 1. 构造初始 state（含 `message` / 默认 `agent_id` / `core_type` 等）
/// 2. 加载 Agent 配置注入 state（system_prompt / tool_ids / model_tier / max_iterations）
/// 3. 构造 PipelineExecutor 并执行 `run`
/// 4. 从最终 state 提取响应（优先 `raw_result`，回退 `message`，再回退原消息）
///
/// 降级条件：AppState 缺少 invoker / store / project_root（典型为测试或老式构造）
/// 时走 echo-fallback，标注降级原因。
pub(crate) async fn process_via_engine(state: &AppState, message: &str, agent_id: &str) -> String {
    let invoker = match state.invoker.clone() {
        Some(i) => i,
        None => {
            return format!(
                "[echo-fallback: engine not available] {}",
                message
            );
        }
    };
    let store = match state.store.clone() {
        Some(s) => s,
        None => {
            return format!(
                "[echo-fallback: store not available] {}",
                message
            );
        }
    };
    let project_root = match state.project_root.clone() {
        Some(p) => p,
        None => {
            return format!(
                "[echo-fallback: project_root not configured] {}",
                message
            );
        }
    };

    // 1. 构造初始 state
    let mut initial_state = serde_json::json!({
        "message": message,
        "input": message,
        "agent_id": agent_id,
        "core_type": "llm_call",
        "core_plugin": "pipeline_llm_core", // 初始调 LLM（插件 id 带 pipeline_ 前缀）
        "ended": false,
        "suspended": false,
    });

    // 2. 加载 Agent 配置注入 state（读 config/agents/<agent_id>.yaml，不存在跳过）
    load_agent_config_into_state(&mut initial_state, agent_id, &project_root);

    // 3. 构造 PipelineExecutor 并执行
    //    run_id / branch_id 用 uuid 保证多请求隔离；租户上下文从 task_local 读取
    //    （多租户 P0-4：本函数已在 agentos_tenant::scope 内调用）。
    let tenant =
        agentos_tenant::current().unwrap_or_else(|| TenantContext::new("default", "kernel"));
    let run_id = uuid::Uuid::new_v4().to_string();
    let branch_id = "main".to_string();
    let executor = agentos_engine::PipelineExecutor::new(
        invoker,
        project_root,
        tenant,
        state.plugin_ids.iter().cloned(),
        store,
        run_id.clone(),
        branch_id,
    );

    info!(run_id = %run_id, agent_id = %agent_id, "Pipeline run started");

    let final_state = match executor
        .run(&state.pipeline_config, &state.step_library, initial_state)
        .await
    {
        Ok(s) => s,
        Err(e) => {
            warn!(run_id = %run_id, error = %e, "PipelineExecutor run failed");
            return format!("[engine-run-failed] {}", message);
        }
    };

    // 4. 提取响应：优先 raw_result，回退 state.message，再回退原消息
    if let Some(raw) = final_state.get("raw_result").and_then(|v| v.as_str()) {
        return raw.to_string();
    }
    if let Some(msg) = final_state.get("message").and_then(|v| v.as_str()) {
        return msg.to_string();
    }
    // 没有 raw_result / message 字段：pretty-print 整个 state（便于调试）
    serde_json::to_string_pretty(&final_state).unwrap_or_else(|_| message.to_string())
}

/// 加载 Agent 配置注入到管道 state。
///
/// 简化语义（[来源: 任务 §load_agent_config_into_state]）：只读 `system_prompt`
/// / `tool_ids` / `model_tier` / `max_iterations` 几个字段，不解析复杂结构。
/// 文件不存在跳过（用 state 已有的默认值）。
///
/// 设计取舍：字段冲突时不覆盖 state 中已有的值（agent 调用方注入优先级高于配置默认），
/// 仅在缺失时补。`max_iterations` 同时覆写 `pipeline_config.loop_config.max_iterations`
/// 的运行期语义（由 PipelineExecutor 在每次 run 时读取 state，而非 config）。
fn load_agent_config_into_state(
    state: &mut serde_json::Value,
    agent_id: &str,
    config_root: &std::path::Path,
) {
    let path = config_root.join("agents").join(format!("{}.yaml", agent_id));
    let raw = match std::fs::read_to_string(&path) {
        Ok(s) => s,
        Err(_) => {
            // 文件不存在 → 跳过（用默认 state）
            tracing::debug!(
                agent_id = %agent_id,
                "Agent config not found at {}, using defaults",
                path.display()
            );
            return;
        }
    };
    let parsed: serde_yaml::Value = match serde_yaml::from_str(&raw) {
        Ok(v) => v,
        Err(e) => {
            warn!(
                agent_id = %agent_id,
                error = %e,
                "Failed to parse agent config, using defaults"
            );
            return;
        }
    };

    let obj = match state.as_object_mut() {
        Some(o) => o,
        None => return,
    };
    let entry = |key: &str| -> Option<serde_json::Value> {
        parsed
            .get(key)
            .cloned()
            .and_then(|v| serde_yaml::from_value(v).ok())
    };

    if let Some(v) = entry("system_prompt") {
        obj.entry("system_prompt").or_insert(v);
    }
    if let Some(v) = entry("tool_ids") {
        obj.entry("tool_ids").or_insert(v);
    }
    if let Some(v) = entry("model_tier") {
        obj.entry("model_tier").or_insert(v);
    }
    if let Some(v) = entry("max_iterations") {
        obj.entry("max_iterations").or_insert(v);
    }
}

/// 处理 WebSocket 连接——收发消息循环。
async fn handle_ws_connection(socket: WebSocket, state: AppState, headers: HeaderMap) {
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

                // 在租户上下文内通过管道引擎处理消息（多租户 P0-4）
                // TODO: agent_id 暂用默认（chat 协议暂未携带；后续从请求体取）
                let tenant_ctx = request_tenant_ctx(&headers, &req.session_id);
                let content =
                    agentos_tenant::scope(tenant_ctx, process_via_engine(&state, &req.message, "agentos"))
                        .await;

                // 构造响应
                let response = WsResponse {
                    r#type: "message".to_string(),
                    content,
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

/// /api/v1/chat POST 端点——通过管道引擎处理消息。
async fn chat_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    axum::Json(req): axum::Json<WsRequest>,
) -> Result<axum::Json<WsResponse>, ApiError> {
    // 在租户上下文内通过管道引擎处理消息（多租户 P0-4）
    // TODO: agent_id 暂用默认（chat 协议暂未携带；后续从请求体取）
    let tenant_ctx = request_tenant_ctx(&headers, &req.session_id);
    let content =
        agentos_tenant::scope(tenant_ctx, process_via_engine(&state, &req.message, "agentos"))
            .await;

    let response = WsResponse {
        r#type: "message".to_string(),
        content,
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
    async fn test_chat_uses_engine_not_echo() {
        // 验证 chat 响应不再是简单的 "Response to: xxx"
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
        let body = axum::body::to_bytes(response.into_body(), 8192)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(json["type"], "message");
        // 响应内容不应再是 "Response to: hello world"（echo 模式）
        let content = json["content"].as_str().unwrap();
        assert!(
            !content.starts_with("Response to:"),
            "Chat should not be in echo mode, got: {}",
            content
        );
        assert_eq!(json["session_id"], "test_session");
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
        // config-based 模式下 agents 为空（因为 manifests 为空）
        assert_eq!(json["agents"].as_array().unwrap().len(), 0);
        // tools 来自 config（capability_registry 为 None 时 fallback 到 config）
        assert_eq!(json["tools"].as_array().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn test_tools_handler_returns_tools_list() {
        // 验证 tools handler 从 config 返回工具列表（无 registry 时）
        let config = json!({
            "tools": [{"name": "calculator", "description": "A calculator"}],
        });
        let app = build_router(AppState::with_config(config));
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/tools")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let tools = json.as_array().unwrap();
        assert_eq!(tools.len(), 1);
        assert_eq!(tools[0]["name"], "calculator");
    }
}
