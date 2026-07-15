//! HTTP 路由处理器
//!
//! 提供健康检查、Schema 聚合、能力清单等 RESTful 端点。
//!
//! [来源: docs/tasks/task_07_llm_api.md AC-06-3/AC-06-5]

use serde::Serialize;
use serde_json::json;

/// 健康检查响应。
#[derive(Debug, Serialize)]
pub struct HealthResponse {
    pub status: String,
    pub version: String,
    pub timestamp: String,
}

/// Schema 聚合响应（AC-06-5）。
///
/// 聚合插件能力清单和 UI Schema，供前端渲染使用。
#[derive(Debug, Serialize)]
pub struct SchemaResponse {
    pub agents: Vec<serde_json::Value>,
    pub pipelines: Vec<serde_json::Value>,
    pub tools: Vec<serde_json::Value>,
    pub routes: serde_json::Value,
}

/// 应用状态——通过 Axum State 共享。
#[derive(Clone)]
pub struct AppState {
    pub config: serde_json::Value,
}

impl AppState {
    pub fn new() -> Self {
        Self { config: json!({}) }
    }

    pub fn with_config(config: serde_json::Value) -> Self {
        Self { config }
    }
}

impl Default for AppState {
    fn default() -> Self {
        Self::new()
    }
}

/// /health 端点处理器（AC-06-3）。
pub async fn health_handler() -> axum::Json<HealthResponse> {
    axum::Json(HealthResponse {
        status: "ok".to_string(),
        version: env!("CARGO_PKG_VERSION").to_string(),
        timestamp: chrono::Utc::now().to_rfc3339(),
    })
}

/// /api/v1/schema 端点处理器（AC-06-5）。
pub async fn schema_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<SchemaResponse> {
    // 从 config 聚合能力清单
    let agents = state
        .config
        .get("agents")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let pipelines = state
        .config
        .get("pipelines")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let tools = state
        .config
        .get("tools")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let routes = state.config.get("routes").cloned().unwrap_or(json!({}));

    axum::Json(SchemaResponse {
        agents,
        pipelines,
        tools,
        routes,
    })
}

/// /api/v1/agents 端点处理器。
pub async fn agents_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<Vec<serde_json::Value>> {
    let agents = state
        .config
        .get("agents")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    axum::Json(agents)
}

/// /api/v1/pipelines 端点处理器。
pub async fn pipelines_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<Vec<serde_json::Value>> {
    let pipelines = state
        .config
        .get("pipelines")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    axum::Json(pipelines)
}

/// /api/v1/tools 端点处理器。
pub async fn tools_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<Vec<serde_json::Value>> {
    let tools = state
        .config
        .get("tools")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    axum::Json(tools)
}
