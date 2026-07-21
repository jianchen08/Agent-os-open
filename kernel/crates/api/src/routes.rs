//! HTTP 路由处理器
//!
//! 提供健康检查、Schema 聚合、能力清单等 RESTful 端点。
//!
//! [来源: docs/tasks/task_07_llm_api.md AC-06-3/AC-06-5]

use std::sync::Arc;

use lingxi_core::traits::{
    CapabilityRegistry, PluginManifest, PluginType,
};
use lingxi_engine::AdrEngineImpl;
use lingxi_plugin_loader::CapabilityRegistryImpl;
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
///
/// 集成插件系统后，持有能力注册表和管道引擎引用。
#[derive(Clone)]
pub struct AppState {
    pub config: serde_json::Value,
    /// 已发现的插件 manifest 列表
    pub manifests: Arc<Vec<PluginManifest>>,
    /// 能力注册表（工具/资源/路由信号）
    pub capability_registry: Option<Arc<CapabilityRegistryImpl>>,
    /// 管道引擎（用于 chat/ws 端点）
    pub engine: Option<Arc<AdrEngineImpl>>,
}

impl AppState {
    pub fn new() -> Self {
        Self {
            config: json!({}),
            manifests: Arc::new(Vec::new()),
            capability_registry: None,
            engine: None,
        }
    }

    pub fn with_config(config: serde_json::Value) -> Self {
        Self {
            config,
            manifests: Arc::new(Vec::new()),
            capability_registry: None,
            engine: None,
        }
    }

    /// 构建集成了插件系统的 AppState。
    pub fn with_plugins(
        manifests: Vec<PluginManifest>,
        registry: Arc<CapabilityRegistryImpl>,
        engine: Arc<AdrEngineImpl>,
    ) -> Self {
        // 从 manifest 构建 config JSON（兼容旧的 config-based handler）
        let agents: Vec<serde_json::Value> = manifests
            .iter()
            .filter(|m| m.plugin_type == PluginType::System)
            .map(|m| serde_json::to_value(m).unwrap_or_default())
            .collect();
        let pipelines: Vec<serde_json::Value> = manifests
            .iter()
            .filter(|m| m.plugin_type == PluginType::Pipeline)
            .map(|m| serde_json::to_value(m).unwrap_or_default())
            .collect();
        let tools: Vec<serde_json::Value> = registry
            .list_tools()
            .iter()
            .map(|t| serde_json::to_value(t).unwrap_or_default())
            .collect();

        let config = json!({
            "agents": agents,
            "pipelines": pipelines,
            "tools": tools,
            "routes": {},
        });

        Self {
            config,
            manifests: Arc::new(manifests),
            capability_registry: Some(registry),
            engine: Some(engine),
        }
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
    // 优先从 capability_registry 获取工具列表
    let tools = if let Some(registry) = &state.capability_registry {
        registry
            .list_tools()
            .iter()
            .map(|t| serde_json::to_value(t).unwrap_or_default())
            .collect()
    } else {
        state
            .config
            .get("tools")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default()
    };

    // 从 manifest 构建 agents/pipelines
    let agents: Vec<serde_json::Value> = state
        .manifests
        .iter()
        .filter(|m| m.plugin_type == PluginType::System)
        .map(|m| {
            json!({
                "id": m.id,
                "name": m.name,
                "version": m.version,
            })
        })
        .collect();

    let pipelines: Vec<serde_json::Value> = state
        .manifests
        .iter()
        .filter(|m| m.plugin_type == PluginType::Pipeline)
        .map(|m| {
            json!({
                "id": m.id,
                "name": m.name,
                "version": m.version,
                "role": m.pipeline_role,
            })
        })
        .collect();

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
    let agents: Vec<serde_json::Value> = state
        .manifests
        .iter()
        .filter(|m| m.plugin_type == PluginType::System)
        .map(|m| {
            json!({
                "id": m.id,
                "name": m.name,
                "version": m.version,
            })
        })
        .collect();
    axum::Json(agents)
}

/// /api/v1/pipelines 端点处理器。
pub async fn pipelines_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<Vec<serde_json::Value>> {
    let pipelines: Vec<serde_json::Value> = state
        .manifests
        .iter()
        .filter(|m| m.plugin_type == PluginType::Pipeline)
        .map(|m| {
            json!({
                "id": m.id,
                "name": m.name,
                "version": m.version,
                "role": m.pipeline_role,
                "host_type": m.host_type,
            })
        })
        .collect();
    axum::Json(pipelines)
}

/// /api/v1/tools 端点处理器。
///
/// 从 CapabilityRegistry 返回已注册的工具列表。
pub async fn tools_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<Vec<serde_json::Value>> {
    if let Some(registry) = &state.capability_registry {
        let tools: Vec<serde_json::Value> = registry
            .list_tools()
            .iter()
            .map(|t| {
                json!({
                    "name": t.name,
                    "description": t.description,
                    "plugin_id": t.plugin_id,
                    "category": t.category,
                    "source": t.source,
                })
            })
            .collect();
        return axum::Json(tools);
    }
    // fallback: 从 config 获取（兼容旧逻辑）
    let tools = state
        .config
        .get("tools")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    axum::Json(tools)
}
