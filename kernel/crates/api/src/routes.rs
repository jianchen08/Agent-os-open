//! HTTP 路由处理器
//!
//! 提供健康检查、Schema 聚合、能力清单等 RESTful 端点。
//!
//! [来源: docs/tasks/task_07_llm_api.md AC-06-3/AC-06-5]

use std::path::PathBuf;
use std::sync::Arc;

use agentos_core::traits::{
    CapabilityRegistry, ConfigFileMapping, HttpHandleCapability, PluginInvoker, PluginManifest,
    PluginType, StorageBackend,
};
use agentos_core::types::{PipelineConfig, StepLibrary};
use agentos_engine::AdrEngineImpl;
use agentos_plugin_loader::CapabilityRegistryImpl;
use axum::http::HeaderMap;
use axum::response::IntoResponse;
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::config_service::{
    apply_put_masked_sentinels, atomic_write_yaml, compute_etag, mask_secrets,
    validate_config_path,
};
use crate::error::ApiError;

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
    /// P1-4：各插件的 config_files 聚合（仅含声明 config_files 的插件）。
    /// 前端据此构建"插件 → 多配置子项"的配置树（ADR §4.6）。
    pub plugin_configs: Vec<serde_json::Value>,
    /// P4/P5：各插件的 contributes 聚合（仅含声明 contributes 的插件）。
    /// 前端 ContributionRegistry 作为唯一真相源消费（ADR §3.4/§六）。
    pub plugin_contributes: Vec<serde_json::Value>,
}

/// 应用状态——通过 Axum State 共享。
///
/// 集成插件系统后，持有能力注册表、管道引擎以及 0.2 引擎所需的运行期资源
/// （pipeline_config / step_library / invoker / store / plugin_ids / project_root）。
#[derive(Clone)]
pub struct AppState {
    pub config: serde_json::Value,
    /// 已发现的插件 manifest 列表
    pub manifests: Arc<Vec<PluginManifest>>,
    /// 能力注册表（工具/资源/路由信号）
    pub capability_registry: Option<Arc<CapabilityRegistryImpl>>,
    /// 管道引擎（保留：旧的 AdrEngine 入口，schema/health 等查询用；chat 不再走它）
    pub engine: Option<Arc<AdrEngineImpl>>,
    /// ── 0.2 引擎接线所需资源（process_via_engine 用）──
    /// 管道配置（config/pipelines/autonomous.yaml 加载）
    pub pipeline_config: Arc<PipelineConfig>,
    /// 公共 step 库（config/steps/*.yaml 加载）
    pub step_library: Arc<StepLibrary>,
    /// 插件调用器（命中规则③调用原子插件）
    pub invoker: Option<Arc<dyn PluginInvoker>>,
    /// 存储后端（构造 ContentLoader）
    pub store: Option<Arc<dyn StorageBackend>>,
    /// 已知插件 id 集合（命中规则③判定 + 启动期重名检测）
    pub plugin_ids: Arc<std::collections::HashSet<String>>,
    /// 项目根目录（`{{path:...}}` 模板解析基准 + agent 配置加载基准）
    pub project_root: Option<PathBuf>,
    /// P2：会话协调器（连接注册表 / 事件总线 / 重放缓冲）。None = 降级 echo。
    pub session: Option<Arc<agentos_session::SessionCoordinator>>,
    /// P2：入站路由器（user_input/interaction/stop 分发）。
    pub inbound_router: Option<Arc<agentos_session::router::InboundRouter>>,
    /// P3：HTTP 端点 dispatcher 的插件处理能力（http.handle）。
    /// None = 不挂载插件 HTTP 端点（仅内核静态路由）。
    pub http_handler: Option<Arc<dyn HttpHandleCapability>>,
}

impl AppState {
    pub fn new() -> Self {
        Self {
            config: json!({}),
            manifests: Arc::new(Vec::new()),
            capability_registry: None,
            engine: None,
            pipeline_config: Arc::new(PipelineConfig {
                name: "default".to_string(),
                loop_config: Default::default(),
                steps: Vec::new(),
            }),
            step_library: Arc::new(StepLibrary::default()),
            invoker: None,
            store: None,
            plugin_ids: Arc::new(std::collections::HashSet::new()),
            project_root: None,
            session: None,
            inbound_router: None,
            http_handler: None,
        }
    }

    pub fn with_config(config: serde_json::Value) -> Self {
        Self {
            config,
            manifests: Arc::new(Vec::new()),
            capability_registry: None,
            engine: None,
            pipeline_config: Arc::new(PipelineConfig {
                name: "default".to_string(),
                loop_config: Default::default(),
                steps: Vec::new(),
            }),
            step_library: Arc::new(StepLibrary::default()),
            invoker: None,
            store: None,
            plugin_ids: Arc::new(std::collections::HashSet::new()),
            project_root: None,
            session: None,
            inbound_router: None,
            http_handler: None,
        }
    }

    /// 构建集成了插件系统的 AppState。
    ///
    /// 注意：旧的 `with_plugins(manifests, registry, engine)` 三参签名扩展为含
    /// pipeline_config / step_library / invoker / store / plugin_ids / project_root 的多参形式，
    /// 以便 chat 端点直接构造 [`agentos_engine::PipelineExecutor`]。
    #[allow(clippy::too_many_arguments)]
    pub fn with_plugins(
        manifests: Vec<PluginManifest>,
        registry: Arc<CapabilityRegistryImpl>,
        engine: Arc<AdrEngineImpl>,
        pipeline_config: Arc<PipelineConfig>,
        step_library: Arc<StepLibrary>,
        invoker: Arc<dyn PluginInvoker>,
        store: Arc<dyn StorageBackend>,
        plugin_ids: std::collections::HashSet<String>,
        project_root: PathBuf,
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
            pipeline_config,
            step_library,
            invoker: Some(invoker),
            store: Some(store),
            plugin_ids: Arc::new(plugin_ids),
            project_root: Some(project_root),
            session: None,
            inbound_router: None,
            http_handler: None,
        }
    }

    /// P2：启用会话内核（连接注册表 / 事件总线 / 重放缓冲 + 入站路由）。
    ///
    /// 在 `with_plugins` 后调用，注入 SessionCoordinator 与基于引擎的
    /// 入站分发器。ws_handler 据此承载真实 WS 会话；未调用时 ws_handler
    /// 降级为旧 echo/engine 路径（兼容）。
    pub fn enable_session(self) -> Self {
        let session = Arc::new(agentos_session::SessionCoordinator::new());
        let dispatcher = Arc::new(crate::ws_session::EngineDispatcher::new(self.clone()));
        let inbound_router = Arc::new(agentos_session::router::InboundRouter::new(dispatcher));
        Self {
            session: Some(session),
            inbound_router: Some(inbound_router),
            ..self
        }
    }

    /// P3：注入 HTTP 端点 dispatcher 的插件处理能力（`http.handle`）。
    ///
    /// 生产用 [`crate::http_dispatcher::SidecarHttpHandler`]（经 invoker 调插件）。
    /// 未注入时不挂载插件 HTTP 端点（`build_router` 仅保留内核静态路由）。
    pub fn with_http_handler(self, handler: Arc<dyn HttpHandleCapability>) -> Self {
        Self {
            http_handler: Some(handler),
            ..self
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
                "ui_schema": m.ui_schema.clone(),
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
                "ui_schema": m.ui_schema.clone(),
            })
        })
        .collect();

    let routes = state.config.get("routes").cloned().unwrap_or(json!({}));

    // P1-4：聚合各插件的 config_files（仅含声明 config_files 的插件）。
    // 前端据此构建"插件 → 多配置子项"配置树（ADR §4.6）。
    let plugin_configs: Vec<serde_json::Value> = state
        .manifests
        .iter()
        .filter(|m| !m.config_files.is_empty())
        .map(|m| {
            json!({
                "plugin_id": m.id,
                "plugin_name": m.name,
                "config_files": m.config_files,
            })
        })
        .collect();

    // P4/P5：聚合各插件的 contributes（仅含声明 contributes 的插件）。
    // 内核不解释结构，透传给前端 ContributionRegistry（ADR §3.4/§六）。
    let plugin_contributes: Vec<serde_json::Value> = state
        .manifests
        .iter()
        .filter(|m| m.contributes.is_some())
        .map(|m| {
            json!({
                "plugin_id": m.id,
                "plugin_name": m.name,
                "contributes": m.contributes,
            })
        })
        .collect();

    axum::Json(SchemaResponse {
        agents,
        pipelines,
        tools,
        routes,
        plugin_configs,
        plugin_contributes,
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

// ── P1-4/P1-5 插件配置端点（/api/v1/plugins/{id}/config/{file_id}）──

/// GET 配置响应体：返回掩码后的文件内容 + 元数据。
#[derive(Debug, Serialize)]
pub struct PluginConfigResponse {
    pub plugin_id: String,
    pub file_id: String,
    pub label: String,
    pub path: String,
    pub data: serde_json::Value,
    pub etag: String,
}

/// PUT 配置请求体：data 为完整文件内容，if_match 为 GET 返回的 ETag（B4 乐观锁）。
#[derive(Debug, Deserialize)]
pub struct PluginConfigUpdateRequest {
    pub data: serde_json::Value,
    pub if_match: Option<String>,
}

/// 在 manifests 中按 id 查找插件。
fn find_manifest<'a>(
    manifests: &'a [PluginManifest],
    plugin_id: &str,
) -> Option<&'a PluginManifest> {
    manifests.iter().find(|m| m.id == plugin_id)
}

/// 在插件 manifest 的 config_files 中按 file_id 查找映射项。
fn find_config_mapping<'a>(
    manifest: &'a PluginManifest,
    file_id: &str,
) -> Option<&'a ConfigFileMapping> {
    manifest.config_files.iter().find(|f| f.id == file_id)
}

/// GET /api/v1/plugins/{id}/config/{file_id}（ADR §4.3）。
///
/// 流程：查 manifest → 查 config_files[file_id] → B1 path 校验 → 读文件 →
/// B2 掩码 → 返回带 ETag（B4）的内容。
pub async fn get_plugin_config_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path((plugin_id, file_id)): axum::extract::Path<(String, String)>,
) -> Result<axum::Json<PluginConfigResponse>, ApiError> {
    let project_root = state.project_root.ok_or_else(|| ApiError::Internal {
        message: "project_root not configured".to_string(),
    })?;

    let manifest = find_manifest(&state.manifests, &plugin_id).ok_or_else(|| ApiError::NotFound {
        message: format!("plugin not found: {plugin_id}"),
    })?;

    let mapping = find_config_mapping(manifest, &file_id).ok_or_else(|| ApiError::NotFound {
        message: format!("config file_id not found: {file_id}"),
    })?;

    let resolved = validate_config_path(&project_root, &mapping.path).map_err(config_err_to_api)?;
    let raw = std::fs::read_to_string(&resolved).map_err(|_| ApiError::NotFound {
        message: format!("config file not found on disk: {}", mapping.path),
    })?;

    let etag = compute_etag(raw.as_bytes());
    let parsed: serde_json::Value = serde_yaml::from_str(&raw).map_err(|e| ApiError::Internal {
        message: format!("config file yaml parse error: {e}"),
    })?;
    let masked = mask_secrets(&parsed);

    Ok(axum::Json(PluginConfigResponse {
        plugin_id,
        file_id,
        label: mapping.label.clone(),
        path: mapping.path.clone(),
        data: masked,
        etag,
    }))
}

/// PUT /api/v1/plugins/{id}/config/{file_id}（ADR §4.3）。
///
/// 流程：查 manifest → 查 config_files[file_id] → B1 path 校验 → 读磁盘原值 →
/// B4 校验 If-Match（缺失/不匹配 → 409）→ B2 *** 哨兵合并 → B4/B6 原子写 →
/// 返回新 ETag。
pub async fn put_plugin_config_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path((plugin_id, file_id)): axum::extract::Path<(String, String)>,
    axum::Json(req): axum::Json<PluginConfigUpdateRequest>,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let project_root = state.project_root.ok_or_else(|| ApiError::Internal {
        message: "project_root not configured".to_string(),
    })?;

    let manifest = find_manifest(&state.manifests, &plugin_id).ok_or_else(|| ApiError::NotFound {
        message: format!("plugin not found: {plugin_id}"),
    })?;

    let mapping = find_config_mapping(manifest, &file_id).ok_or_else(|| ApiError::NotFound {
        message: format!("config file_id not found: {file_id}"),
    })?;

    let resolved = validate_config_path(&project_root, &mapping.path).map_err(config_err_to_api)?;

    let raw = std::fs::read_to_string(&resolved).map_err(|_| ApiError::NotFound {
        message: format!("config file not found on disk: {}", mapping.path),
    })?;
    let current_etag = compute_etag(raw.as_bytes());

    // B4 乐观锁：If-Match 必须匹配当前 ETag，否则 409
    match req.if_match.as_deref() {
        Some(given) if given == current_etag => {}
        _ => {
            return Err(ApiError::Conflict {
                message: format!(
                    "ETag mismatch: current={current_etag}, given={:?}",
                    req.if_match
                ),
            });
        }
    }

    let stored: serde_json::Value =
        serde_yaml::from_str(&raw).map_err(|e| ApiError::Internal {
            message: format!("stored config yaml parse error: {e}"),
        })?;
    // B2：*** 哨兵字段保留磁盘原值
    let merged = apply_put_masked_sentinels(&stored, &req.data);

    // B4/B6：原子写 + round-trip 校验
    atomic_write_yaml(&resolved, &merged).map_err(config_err_to_api)?;

    let new_etag = compute_etag(
        std::fs::read_to_string(&resolved)
            .map_err(|e| ApiError::Internal {
                message: format!("re-read after write failed: {e}"),
            })?
            .as_bytes(),
    );

    Ok(axum::Json(json!({
        "plugin_id": plugin_id,
        "file_id": file_id,
        "etag": new_etag,
    })))
}

/// 把 ConfigError 映射为 ApiError。
fn config_err_to_api(e: crate::config_service::ConfigError) -> ApiError {
    use crate::config_service::ConfigError as Ce;
    match e {
        Ce::PathOutsideConfigRoot { .. } | Ce::KernelReservedFile { .. } => ApiError::BadRequest {
            message: format!("invalid config path: {e}"),
        },
        Ce::NotFound { .. } => ApiError::NotFound {
            message: format!("config not found: {e}"),
        },
        Ce::YamlInvalid { .. } => ApiError::BadRequest {
            message: format!("config yaml invalid: {e}"),
        },
        Ce::Io { .. } => ApiError::Internal {
            message: format!("config io error: {e}"),
        },
    }
}

/// 返回带 ETag 头的 GET 响应（覆盖默认 JSON，附加 header）。
pub async fn get_plugin_config_with_etag(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path((plugin_id, file_id)): axum::extract::Path<(String, String)>,
) -> Result<axum::response::Response, ApiError> {
    let axum::Json(resp) = get_plugin_config_handler(
        axum::extract::State(state),
        axum::extract::Path((plugin_id, file_id)),
    )
    .await?;
    let etag = resp.etag.clone();
    Ok(([(axum::http::header::ETAG, etag)], axum::Json(resp)).into_response())
}

// 触发 HeaderMap/IntoResponse 的使用（headers 参数预留用于鉴权扩展）。
#[allow(dead_code)]
fn _headers_used(_h: HeaderMap) {}

/// 从请求头提取 If-Match（备用：支持 header 形式而非 body）。
#[allow(dead_code)]
fn extract_if_match_header(headers: &HeaderMap) -> Option<String> {
    headers
        .get("if-match")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.trim_matches('"').to_string())
}
