//! HTTP 路由处理器
//!
//! 提供健康检查、Schema 聚合、能力清单等 RESTful 端点。
//!
//! [来源: docs/tasks/task_07_llm_api.md AC-06-3/AC-06-5]

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use tokio::sync::RwLock;

use agentos_config::config_center::ConfigCenter;
use agentos_core::traits::{
    CapabilityRegistry, ConfigFileMapping, HttpHandleCapability, PluginInvoker, PluginManifest,
    PluginType, StorageBackend,
};
use agentos_core::types::{PipelineConfig, StepLibrary};
use agentos_invoker::verify::{compare_tools, declared_with_services, parse_actual_tools};
use agentos_plugin_loader::{CapabilityRegistryImpl, PluginScopeRegistry};
use axum::response::IntoResponse;
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::config_service::{
    apply_put_masked_sentinels, atomic_write_yaml, compute_etag, mask_secrets, validate_config_path,
};
use crate::error::ApiError;
use crate::metrics::plugin_widget_broadcast::{remove_plugin_bindings, WidgetBinding};
use crate::metrics::{export_prometheus, MetricsAggregator};

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
    /// 已发现的插件 manifest 列表。
    ///
    /// RwLock 共享存储：watcher 热发现的新插件经 [`crate::plugin_watcher`] 每轮
    /// sync 增量合并进来（按 id 去重），状态列表 / re-enable 重注册 / actions
    /// 命令查找等消费面与能力注册表保持一致，无需重启内核。
    pub manifests: Arc<RwLock<Vec<PluginManifest>>>,
    /// 能力注册表（工具/资源/路由信号）
    pub capability_registry: Option<Arc<CapabilityRegistryImpl>>,
    /// ── 0.2 引擎接线所需资源（process_via_engine 用）──
    /// 管道配置（config/pipelines/autonomous.yaml 加载）
    pub pipeline_config: Arc<PipelineConfig>,
    /// 公共 step 库（config/steps/*.yaml 加载）
    pub step_library: Arc<StepLibrary>,
    /// 插件调用器（命中规则③调用原子插件）
    pub invoker: Option<Arc<dyn PluginInvoker>>,
    /// 存储后端（构造 ContentLoader）
    pub store: Option<Arc<dyn StorageBackend>>,
    /// 统一数据接口专用：具体 SqliteStore 句柄（访问 sqlite_master / PRAGMA / 通用 CRUD）。
    /// 与 `store`（trait object）互补：`store` 走业务语义方法，`db` 走表驱动动态访问。
    /// 构造处由 `with_db` 注入；None = 统一数据接口不可用（返回 503 语义的 400）。
    pub db: Option<Arc<agentos_engine::SqliteStore>>,
    /// 已知插件 id 集合（命中规则③判定 + 启动期重名检测）
    pub plugin_ids: Arc<std::collections::HashSet<String>>,
    /// 项目根目录（`{{path:...}}` 模板解析基准 + agent 配置加载基准）
    pub project_root: Option<PathBuf>,
    /// P2：会话协调器（连接注册表 / 事件总线 / 重放缓冲）。None = 降级 echo。
    pub session: Option<Arc<agentos_session::SessionCoordinator>>,
    /// 管道 state 内存常驻注册表（对齐 0.1 EngineRegistry）。
    /// 按 (tenant_id, pipeline_id) 常驻 state，使多轮对话历史跨轮延续。
    /// 热路径走内存复用；冷启动（重启/新会话）走 DB 重建。None = 每轮重建（降级）。
    /// P2：入站路由器（user_input/interaction/stop 分发）。
    pub inbound_router: Option<Arc<agentos_session::router::InboundRouter>>,
    /// P3：HTTP 端点 dispatcher 的插件处理能力（http.handle）。
    /// None = 不挂载插件 HTTP 端点（仅内核静态路由）。
    pub http_handler: Option<Arc<dyn HttpHandleCapability>>,
    /// 监控 M1：指标聚合器（监控设计 §四）。None = 不启用指标端点。
    pub metrics: Option<MetricsAggregator>,
    /// 安装触发模型 L1：已启用插件 id 集合（schema 聚合据此过滤 contributes/configs）。
    /// disabled 插件的 manifest 仍在 manifests（用户能看到装了什么），但不出口 contributes。
    pub enabled_plugin_ids: Arc<RwLock<std::collections::HashSet<String>>>,
    /// 阶段3 遗留：插件根目录映射（plugin_id → 插件根目录绝对路径）。
    ///
    /// 由启动期 loader 扫描结果填充（或测试直接构造）。HTTP dispatcher 据此把
    /// `/ext/{plugin_id}/assets/{*path}` 解析到 `<plugin_root>/web/<path>`，
    /// 直接读文件返回，免去为每个子资源单独声明 http_endpoints。
    /// 空 map = 无插件托管静态资源（兼容旧行为，仅静态路由 + dispatcher）。
    pub plugin_dirs: Arc<HashMap<String, PathBuf>>,
    /// 统一配置中心（统一配置加载方案 TDD-1/2/3 的入口）。
    ///
    /// 提供 `load()` / `load_dir()` / `store()` 三个 pull 模式 API，
    /// 供 agent loader / pipeline loader / plugin config_files 统一走。
    /// None = 未接线（降级：各 loader 继续各自直读，行为不变）。
    pub config_center: Option<Arc<ConfigCenter>>,
    /// M1：per-plugin 注册账本（guard 化）。disable/unload 时经此结构性收回
    /// 该插件全部注册（registry 四维 + broadcaster 绑定）。空注册表 = 无登记
    /// （测试直接构造 AppState 时 disable 路径仍走 clear_plugin 兜底）。
    pub plugin_scopes: Arc<PluginScopeRegistry>,
    /// M1：widget 指标推送共享绑定表（禁用插件时移除其绑定，broadcaster 下 tick 生效）。
    /// None = 未启用 widget 指标推送。
    pub widget_bindings: Option<Arc<parking_lot::RwLock<Vec<WidgetBinding>>>>,
}

impl AppState {
    pub fn new() -> Self {
        Self {
            config: json!({}),
            manifests: Arc::new(RwLock::new(Vec::new())),
            capability_registry: None,
            pipeline_config: Arc::new(PipelineConfig {
                name: "default".to_string(),
                loop_bodies: Vec::new(),
                checkpoint: Default::default(),
            }),
            step_library: Arc::new(StepLibrary::default()),
            invoker: None,
            store: None,
            db: None,
            plugin_ids: Arc::new(std::collections::HashSet::new()),
            project_root: None,
            enabled_plugin_ids: Arc::new(RwLock::new(std::collections::HashSet::new())),
            session: None,
            inbound_router: None,
            http_handler: None,
            metrics: None,
            plugin_dirs: Arc::new(HashMap::new()),
            config_center: None,
            plugin_scopes: Arc::new(PluginScopeRegistry::new()),
            widget_bindings: None,
        }
    }

    pub fn with_config(config: serde_json::Value) -> Self {
        Self {
            config,
            manifests: Arc::new(RwLock::new(Vec::new())),
            capability_registry: None,
            pipeline_config: Arc::new(PipelineConfig {
                name: "default".to_string(),
                loop_bodies: Vec::new(),
                checkpoint: Default::default(),
            }),
            step_library: Arc::new(StepLibrary::default()),
            invoker: None,
            store: None,
            db: None,
            plugin_ids: Arc::new(std::collections::HashSet::new()),
            project_root: None,
            enabled_plugin_ids: Arc::new(RwLock::new(std::collections::HashSet::new())),
            session: None,
            inbound_router: None,
            http_handler: None,
            metrics: None,
            plugin_dirs: Arc::new(HashMap::new()),
            config_center: None,
            plugin_scopes: Arc::new(PluginScopeRegistry::new()),
            widget_bindings: None,
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
        pipeline_config: Arc<PipelineConfig>,
        step_library: Arc<StepLibrary>,
        invoker: Arc<dyn PluginInvoker>,
        store: Arc<dyn StorageBackend>,
        plugin_ids: std::collections::HashSet<String>,
        project_root: PathBuf,
        enabled_plugin_ids: std::collections::HashSet<String>,
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
            manifests: Arc::new(RwLock::new(manifests)),
            capability_registry: Some(registry),
            pipeline_config,
            step_library,
            invoker: Some(invoker),
            store: Some(store),
            db: None,
            plugin_ids: Arc::new(plugin_ids),
            project_root: Some(project_root),
            enabled_plugin_ids: Arc::new(RwLock::new(enabled_plugin_ids)),
            session: None,
            inbound_router: None,
            http_handler: None,
            metrics: None,
            plugin_dirs: Arc::new(HashMap::new()),
            config_center: None,
            plugin_scopes: Arc::new(PluginScopeRegistry::new()),
            widget_bindings: None,
        }
    }

    /// 注入统一数据接口专用 SqliteStore 句柄（`/api/v1/db/*` 用）。
    ///
    /// 与 `store`（trait object，业务语义方法）互补：`db` 提供表驱动动态访问
    /// （sqlite_master / PRAGMA table_info / 通用 CRUD / SQL 执行器）。
    pub fn with_db(mut self, db: Arc<agentos_engine::SqliteStore>) -> Self {
        self.db = Some(db);
        self
    }

    /// M1：注入插件注册账本（disable/unload 结构性收回的 guard 表）。
    pub fn with_plugin_scopes(mut self, scopes: Arc<PluginScopeRegistry>) -> Self {
        self.plugin_scopes = scopes;
        self
    }

    /// M1：注入 widget 指标推送共享绑定表（禁用插件时移除其绑定）。
    pub fn with_widget_bindings(
        mut self,
        bindings: Arc<parking_lot::RwLock<Vec<WidgetBinding>>>,
    ) -> Self {
        self.widget_bindings = Some(bindings);
        self
    }

    /// P2：启用会话内核（连接注册表 / 事件总线 / 重放缓冲 + 入站路由）。
    ///
    /// 在 `with_plugins` 后调用，注入 SessionCoordinator 与基于引擎的
    /// 入站分发器。ws_handler 据此承载真实 WS 会话；未调用时 ws_handler
    /// 降级为旧 echo/engine 路径（兼容）。
    pub fn enable_session(self) -> Self {
        let session = Arc::new(agentos_session::SessionCoordinator::new());
        // 管道 state 常驻注册表（与 session 同生命期，一起启用）。
        // 关键：先把 session 注入 self，再 clone 给 dispatcher。
        // 否则 dispatcher 持有的 state.session 永远是 None，引擎结果无法推回前端。
        let self_with_session = Self {
            session: Some(session.clone()),
            inbound_router: None,
            ..self
        };
        let dispatcher = Arc::new(crate::ws_session::EngineDispatcher::new(
            self_with_session.clone(),
        ));
        let inbound_router = Arc::new(agentos_session::router::InboundRouter::new(dispatcher));
        Self {
            inbound_router: Some(inbound_router),
            ..self_with_session
        }
    }

    /// 与 enable_session 相同，但接受外部已创建的 SessionCoordinator。
    ///
    /// 用于需要提前创建 session（如注入 capability router 的流式推送）的场景，
    /// 避免重复创建。enable_session = 自建 session 的便捷封装。
    pub fn enable_session_with(self, session: Arc<agentos_session::SessionCoordinator>) -> Self {
        // 管道 state 常驻注册表（与 session 同生命期，一起启用）。
        let self_with_session = Self {
            session: Some(session),
            inbound_router: None,
            ..self
        };
        let dispatcher = Arc::new(crate::ws_session::EngineDispatcher::new(
            self_with_session.clone(),
        ));
        let inbound_router = Arc::new(agentos_session::router::InboundRouter::new(dispatcher));
        Self {
            inbound_router: Some(inbound_router),
            ..self_with_session
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

    /// 监控 M1：注入指标聚合器（启用 `/metrics` 端点；查询面已迁
    /// metrics-admin capability，见 metrics/capability.rs）。
    pub fn with_metrics(self, metrics: MetricsAggregator) -> Self {
        Self {
            metrics: Some(metrics),
            ..self
        }
    }

    /// 阶段3 遗留：注入插件根目录映射（plugin_id → 插件目录绝对路径）。
    ///
    /// 启用后，HTTP dispatcher 把 `/ext/{plugin_id}/assets/{*path}` 解析到
    /// `<plugin_dir>/web/<path>` 直读文件返回。由启动期 loader 扫描结果填充。
    pub fn with_plugin_dirs(self, plugin_dirs: HashMap<String, PathBuf>) -> Self {
        Self {
            plugin_dirs: Arc::new(plugin_dirs),
            ..self
        }
    }

    /// 注入统一配置中心（统一配置加载方案 TDD-4）。
    ///
    /// 启动期由 `agentos-kernel.rs` 构造 `ConfigCenter` 后链式注入。
    /// 注入后所有 loader 可经 `state.config_center` 走统一 `load()` / `load_dir()` 路径。
    pub fn with_config_center(self, cc: Arc<ConfigCenter>) -> Self {
        Self {
            config_center: Some(cc),
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

/// /uploads/{filename} 静态服务（上传文件读取）。
///
/// channel_api artifacts 上传落盘 data/{tenant}/uploads 并返回
/// `/uploads/{filename}` URL（前端附件预览 / 主题背景图引用）。本 handler
/// 直读默认租户（default）上传目录；路径安全：拒绝 `..` 与路径分隔符。
pub async fn serve_upload_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path(filename): axum::extract::Path<String>,
) -> axum::response::Response {
    use axum::body::Body;
    use axum::http::{HeaderValue, StatusCode};
    use axum::response::IntoResponse;

    if filename.is_empty()
        || filename.contains('/')
        || filename.contains('\\')
        || filename.contains("..")
    {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    }
    let Some(project_root) = state.project_root.as_ref() else {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    };
    let uploads_dir = project_root.join("data").join("default").join("uploads");
    let file_path = uploads_dir.join(&filename);
    let Ok(meta) = tokio::fs::metadata(&file_path).await else {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    };
    if !meta.is_file() {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    }
    let Ok(bytes) = tokio::fs::read(&file_path).await else {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    };
    // 常见媒体扩展名 → content-type（未知回退 octet-stream）
    let content_type = match file_path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase()
        .as_str()
    {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "webp" => "image/webp",
        "gif" => "image/gif",
        "svg" => "image/svg+xml",
        "avif" => "image/avif",
        "mp3" => "audio/mpeg",
        "wav" => "audio/wav",
        "m4a" => "audio/mp4",
        "mp4" => "video/mp4",
        "webm" => "video/webm",
        "pdf" => "application/pdf",
        "json" => "application/json",
        "txt" | "md" => "text/plain",
        _ => "application/octet-stream",
    };
    let mut response = axum::response::Response::new(Body::from(bytes));
    response.headers_mut().insert(
        axum::http::header::CONTENT_TYPE,
        HeaderValue::from_static(content_type),
    );
    if let Ok(cache) = HeaderValue::from_str("public, max-age=31536000, immutable") {
        response.headers_mut().insert(axum::http::header::CACHE_CONTROL, cache);
    }
    response
}

/// /api/v1/schema 端点处理器（AC-06-5；剩余项清仓 D2：ETag 协商缓存）。
///
/// 聚合 JSON 规范化序列化（serde_json 对字段序固定的 struct 输出确定）后
/// sha256 作 ETag 响应头；请求带 `If-None-Match` 匹配（含 `*`）则返回 304
/// 空体（前端周期拉 schema 的未变更轮次走 304，省全量聚合响应带宽）。
/// 变更方（插件 enable/disable、G3 动态注册）另经 session 广播
/// widget_event {schema, changed} 推前端主动重拉（见 plugins_set_enabled_handler
/// / capability_router.rs）。
pub async fn schema_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    headers: axum::http::HeaderMap,
) -> axum::response::Response {
    let schema = build_schema(&state).await;
    // 规范化序列化：SchemaResponse 字段序固定，serde_json 输出确定——
    // 相同聚合内容 → 相同字节 → 相同 ETag（内容寻址，与 config_service 的
    // compute_etag 同一实现）。
    let body = serde_json::to_vec(&schema).unwrap_or_default();
    let etag = compute_etag(&body);

    // If-None-Match 协商：逗号分隔多候选（RFC 9110 §13.1.2），命中任一或 * → 304。
    if let Some(inm) = headers
        .get(axum::http::header::IF_NONE_MATCH)
        .and_then(|v| v.to_str().ok())
    {
        if inm.split(',').map(str::trim).any(|t| t == etag || t == "*") {
            return (
                axum::http::StatusCode::NOT_MODIFIED,
                [(axum::http::header::ETAG, etag)],
                axum::body::Body::empty(),
            )
                .into_response();
        }
    }

    (
        [(axum::http::header::ETAG, etag)],
        [(axum::http::header::CONTENT_TYPE, "application/json")],
        body,
    )
        .into_response()
}

/// 聚合 schema 响应体（schema_handler 的纯函数部分，便于复用与测试）。
async fn build_schema(state: &AppState) -> SchemaResponse {
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
        .read()
        .await
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
        .read()
        .await
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
        .read()
        .await
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

    // P4/P5：聚合各插件的 contributes（仅含声明 contributes 且 enabled 的插件）。
    // 内核不解释结构，透传给前端 ContributionRegistry（ADR §3.4/§六）。
    // 安装触发模型 L1：disabled 插件的 contributes 不出口（tools/http 已过滤，UI 也不过来）。
    let enabled_ids = state.enabled_plugin_ids.read().await;
    let plugin_contributes: Vec<serde_json::Value> = state
        .manifests
        .read()
        .await
        .iter()
        .filter(|m| m.contributes.is_some() && enabled_ids.contains(&m.id))
        .map(|m| {
            json!({
                "plugin_id": m.id,
                "plugin_name": m.name,
                "contributes": m.contributes,
            })
        })
        .collect();

    SchemaResponse {
        agents,
        pipelines,
        tools,
        routes,
        plugin_configs,
        plugin_contributes,
    }
}

/// /api/v1/agents 端点处理器。
///
/// 扫描 config/agents/**/*.yaml 返回 Agent 列表(对照 0.1 routes_agents.py)。
/// 支持 query 参数 agent_type 过滤(前端 agentStore 带 ?agent_type=main)。
/// 响应格式 { items: [...] } 兼容前端 agentStore.ts:113。
pub async fn agents_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> axum::Json<serde_json::Value> {
    let filter_type = params.get("agent_type").map(String::as_str);

    let agents_dir = state
        .project_root
        .as_deref()
        .map(|root| root.join("config").join("agents"))
        .unwrap_or_else(|| std::path::PathBuf::from("config/agents"));

    let mut items: Vec<serde_json::Value> = Vec::new();
    if let Ok(yaml_files) = collect_yaml_files(&agents_dir) {
        for path in yaml_files {
            if let Ok(content) = std::fs::read_to_string(&path) {
                if let Ok(parsed) = serde_yaml::from_str::<serde_json::Value>(&content) {
                    let agent_type = parsed
                        .get("agent_type")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    // agent_type 过滤(前端 ?agent_type=main)
                    if let Some(ft) = filter_type {
                        if agent_type != ft {
                            continue;
                        }
                    }
                    let config_id = parsed
                        .get("config_id")
                        .and_then(|v| v.as_str())
                        .or_else(|| parsed.get("id").and_then(|v| v.as_str()))
                        .unwrap_or("")
                        .to_string();
                    if config_id.is_empty() {
                        continue;
                    }
                    items.push(json!({
                        "id": config_id,
                        "config_id": config_id,
                        "name": parsed.get("name").and_then(|v| v.as_str()).unwrap_or(&config_id),
                        "description": parsed.get("description").and_then(|v| v.as_str()).unwrap_or(""),
                        "agent_type": agent_type,
                        "status": "active",
                        "model": parsed.get("model").and_then(|v| v.as_str())
                            .or_else(|| parsed.get("model_tier").and_then(|v| v.as_str()))
                            .unwrap_or(""),
                        "level": parsed.get("level").and_then(|v| v.as_str()).unwrap_or(""),
                        "model_tier": parsed.get("model_tier").and_then(|v| v.as_str()).unwrap_or(""),
                    }));
                }
            }
        }
    }

    let total = items.len();
    axum::Json(json!({ "items": items, "total": total }))
}

/// 解析 agent 配置文件路径（顶层 `config/agents/<id>.yaml` 优先，再递归分类子目录）。
///
/// 与 `load_agent_config_into_state` 的定位逻辑一致：agents/ 按分类组织为
/// `agents/<category>/<id>.yaml`（main/orchestrator/executor/...），顶层也可能放
/// 单文件（如 code_reviewer_agent.yaml）。返回首个匹配路径，不存在返回 None。
fn resolve_agent_yaml_path(
    project_root: &std::path::Path,
    agent_id: &str,
) -> Option<std::path::PathBuf> {
    let agents_dir = project_root.join("config").join("agents");
    let top = agents_dir.join(format!("{}.yaml", agent_id));
    if top.is_file() {
        Some(top)
    } else {
        crate::server::find_agent_yaml(&agents_dir, agent_id)
    }
}

/// /api/v1/agents/schema 端点——返回 agent 配置的字段级 schema（JSON Schema 子集）。
///
/// 供前端表单驱动渲染（createAgent/updateAgent 表单字段）。字段类型集合覆盖
/// string/textarea/number/select/multiselect；select 带 options 枚举。
/// [来源: config/templates/.agent_template_spec.yaml 字段规范]
pub async fn agents_schema_handler() -> axum::Json<serde_json::Value> {
    axum::Json(json!({
        "fields": [
            { "name": "config_id", "type": "string", "label": "配置ID", "required": true },
            { "name": "name", "type": "string", "label": "名称", "required": true },
            { "name": "display_name", "type": "string", "label": "显示名称" },
            { "name": "description", "type": "textarea", "label": "描述" },
            { "name": "agent_type", "type": "select", "label": "类型", "options": [
                {"label": "主控", "value": "main"},
                {"label": "编排", "value": "orchestrator"},
                {"label": "专用", "value": "specialized"},
                {"label": "原子", "value": "atomic"},
                {"label": "系统", "value": "system"}
            ]},
            { "name": "level", "type": "select", "label": "层级", "options": [
                {"label": "L1", "value": "L1"},
                {"label": "L2", "value": "L2"},
                {"label": "L3", "value": "L3"}
            ]},
            { "name": "model_tier", "type": "string", "label": "模型档位" },
            { "name": "system_prompt", "type": "textarea", "label": "系统提示词" },
            { "name": "tool_ids", "type": "multiselect", "label": "工具" },
            { "name": "max_iterations", "type": "number", "label": "最大迭代" },
            { "name": "timeout_seconds", "type": "number", "label": "超时秒" },
            { "name": "tags", "type": "multiselect", "label": "标签" }
        ]
    }))
}

/// GET /api/v1/agents/{id}/config——读取指定 agent 的 yaml 文件内容（掩码后）。
///
/// 按 `resolve_agent_yaml_path` 定位文件（顶层 + 递归分类子目录），返回
/// `{ config_id, yaml, etag }`。id 不存在 → 404。
///
/// 安全基线（对齐 plugin config GET，A13）：
/// - yaml 经 `mask_secrets` 掩码后返回（敏感字段值 → `****`；`${ENV}` 占位符保留）。
///   掩码走 parse → mask → serialize，字段顺序会归一化（yaml 键序无语义）；
/// - `etag` 为**磁盘原文**的内容哈希，供 PUT If-Match 乐观锁使用。
pub async fn get_agent_config_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path(agent_id): axum::extract::Path<String>,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let project_root = state.project_root.ok_or_else(|| ApiError::Internal {
        message: "project_root not configured".to_string(),
    })?;
    let path =
        resolve_agent_yaml_path(&project_root, &agent_id).ok_or_else(|| ApiError::NotFound {
            message: format!("agent config not found: {agent_id}"),
        })?;
    let raw = std::fs::read_to_string(&path).map_err(|e| ApiError::Internal {
        message: format!("read agent config {}: {e}", path.display()),
    })?;
    let etag = compute_etag(raw.as_bytes());
    let parsed: serde_json::Value = serde_yaml::from_str(&raw).map_err(|e| ApiError::Internal {
        message: format!("agent config yaml parse error: {e}"),
    })?;
    let masked = mask_secrets(&parsed);
    let yaml_text = serde_yaml::to_string(&masked).map_err(|e| ApiError::Internal {
        message: format!("agent config yaml serialize error: {e}"),
    })?;
    Ok(axum::Json(
        json!({ "config_id": agent_id, "yaml": yaml_text, "etag": etag }),
    ))
}

/// PUT /api/v1/agents/{id}/config 请求体。
#[derive(Debug, Deserialize)]
pub struct AgentConfigUpdateRequest {
    /// 新的 yaml 文件内容（原文写回）。
    pub yaml: String,
    /// GET 返回的 ETag（If-Match 乐观锁，对齐 plugin config PUT：缺失/不匹配 → 409）。
    pub if_match: Option<String>,
}

/// PUT /api/v1/agents/{id}/config——写回 agent 的 yaml 文件。
///
/// 流程：定位文件（不存在 → 404）→ **YAML 语法校验**（解析失败 → 400，不写盘）→
/// If-Match 校验（缺失/与磁盘 ETag 不匹配 → 409）→ 备份原文件为
/// `<file>.yaml.bak` → 写新内容 → 返回 `{ config_id, success, backup, etag }`。
pub async fn put_agent_config_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path(agent_id): axum::extract::Path<String>,
    axum::Json(req): axum::Json<AgentConfigUpdateRequest>,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let project_root = state.project_root.ok_or_else(|| ApiError::Internal {
        message: "project_root not configured".to_string(),
    })?;
    let path =
        resolve_agent_yaml_path(&project_root, &agent_id).ok_or_else(|| ApiError::NotFound {
            message: format!("agent config not found: {agent_id}"),
        })?;

    // 语法校验先行（T2）：解析失败的 yaml 一律 400 拒写——半结构化内容落盘会
    // 破坏下次启动的 agent 加载；此时磁盘保持原值。
    serde_yaml::from_str::<serde_yaml::Value>(&req.yaml).map_err(|e| ApiError::BadRequest {
        message: format!("agent config yaml invalid: {e}"),
    })?;

    // If-Match 乐观锁（A13，对齐 plugin config PUT）：必须匹配磁盘当前 ETag。
    let raw = std::fs::read_to_string(&path).map_err(|e| ApiError::Internal {
        message: format!("read agent config {}: {e}", path.display()),
    })?;
    let current_etag = compute_etag(raw.as_bytes());
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

    // 先备份原文件（同目录 <file>.yaml.bak），再写新内容
    let backup = path.with_extension("yaml.bak");
    std::fs::copy(&path, &backup).map_err(|e| ApiError::Internal {
        message: format!("backup agent config {}: {e}", path.display()),
    })?;
    std::fs::write(&path, &req.yaml).map_err(|e| ApiError::Internal {
        message: format!("write agent config {}: {e}", path.display()),
    })?;
    let new_etag = compute_etag(req.yaml.as_bytes());

    Ok(axum::Json(json!({
        "config_id": agent_id,
        "success": true,
        "backup": backup.file_name().map(|n| n.to_string_lossy().to_string()),
        "etag": new_etag,
    })))
}

/// POST /api/v1/actions/execute 请求体(对齐前端 GrowthLoop.ts transport 调用格式)。
///
/// `action` = commandId(对应某插件 contributes.commands[].id);
/// `args` = 命令参数对象(缺省空对象)。
/// `action` 标 `#[serde(default)]`:缺字段时反序列化为空串,由 handler 显式返回 400
/// (而非 axum Json 提取器默认的 422),对齐前端约定的错误语义。
#[derive(Debug, Deserialize)]
pub struct ActionsExecuteRequest {
    #[serde(default)]
    pub action: String,
    #[serde(default)]
    pub args: serde_json::Value,
}

/// POST /api/v1/actions/execute——前端命令面板/快捷键/菜单触发的统一出口。
///
/// 链路:前端 `commandDispatcher.setTransport` → 本端点 → 查找声明该 command 的插件 →
/// 执行(或占位)。重点是端点存在、不 404、链路闭合。
///
/// 处理:
/// 1. 请求体缺 `action`(或空串)→ 400
/// 2. 扫描 `state.manifests`,找 `contributes.commands[].id == action` 的插件 → 未命中 404
/// 3. 命中后,若 command 声明了显式路由(条目里的 `tool` 字段指向工具名):
///    invoker 可用 → 经 `invoker.invoke_tool(plugin_id, tool, args)` 调插件 sidecar
///    (参考 capability_router 的 tool-executor.invoke 模式);invoker 不可用(None)→
///    返回 success:false + "工具执行器不可用"错误(明确失败,不假成功)。
/// 4. 无 `tool` 字段的纯声明命令 → 返回 success 占位 ack(设计内契约)。
pub async fn actions_execute_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::Json(req): axum::Json<ActionsExecuteRequest>,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    if req.action.trim().is_empty() {
        return Err(ApiError::BadRequest {
            message: "missing required field: action".to_string(),
        });
    }

    // 扫描 manifests 找声明了该 command 的插件(同时取出 command 条目供路由判定)。
    let mut hit: Option<(&PluginManifest, &serde_json::Value)> = None;
    let manifests = state.manifests.read().await;
    for m in manifests.iter() {
        let Some(contributes) = m.contributes.as_ref() else {
            continue;
        };
        let Some(commands) = contributes.get("commands").and_then(|v| v.as_array()) else {
            continue;
        };
        if let Some(entry) = commands
            .iter()
            .find(|c| c.get("id").and_then(|v| v.as_str()) == Some(req.action.as_str()))
        {
            hit = Some((m, entry));
            break;
        }
    }

    let (manifest, command_entry) = hit.ok_or_else(|| ApiError::NotFound {
        message: format!("command not declared by any plugin: {}", req.action),
    })?;
    let plugin_id = manifest.id.clone();

    // 显式路由:command 条目声明 `tool` 字段 → 经 invoker 调对应工具 sidecar。
    // tool 已声明但 invoker 不可用(None)→ 返回明确失败(不再假成功):
    // 调用方声明了执行路由,执行器缺席意味着请求无法兑现,静默 success:true 会
    // 让前端把"没执行"当成"执行成功"。
    if let Some(tool_name) = command_entry
        .get("tool")
        .and_then(|v| v.as_str())
        .map(str::to_string)
    {
        let Some(invoker) = state.invoker.clone() else {
            return Ok(axum::Json(json!({
                "success": false,
                "error": "工具执行器不可用（invoker 未装配），无法执行声明的 tool 路由",
                "plugin_id": plugin_id,
            })));
        };
        match invoker.invoke_tool(&plugin_id, &tool_name, &req.args).await {
            Ok(result) => {
                return Ok(axum::Json(json!({
                    "success": result.success,
                    "result": result.data,
                    "error": result.error,
                    "plugin_id": plugin_id,
                })));
            }
            Err(e) => {
                return Ok(axum::Json(json!({
                    "success": false,
                    "error": e.message,
                    "plugin_id": plugin_id,
                })));
            }
        }
    }

    // 占位成功:command 已声明但无显式执行路由(纯声明命令的 ack 占位,设计内契约)。
    Ok(axum::Json(json!({
        "success": true,
        "result": { "acknowledged": true, "action": req.action },
        "plugin_id": plugin_id,
    })))
}

/// 递归收集目录下所有 .yaml/.yml 文件(按文件名排序保证稳定)。
fn collect_yaml_files(dir: &std::path::Path) -> std::io::Result<Vec<std::path::PathBuf>> {
    let mut files = Vec::new();
    collect_yaml_files_inner(dir, &mut files)?;
    files.sort();
    Ok(files)
}

fn collect_yaml_files_inner(
    dir: &std::path::Path,
    files: &mut Vec<std::path::PathBuf>,
) -> std::io::Result<()> {
    for entry in std::fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        if path.is_dir() {
            collect_yaml_files_inner(&path, files)?;
        } else if let Some(ext) = path.extension() {
            if ext == "yaml" || ext == "yml" {
                files.push(path);
            }
        }
    }
    Ok(())
}

/// /api/v1/pipelines 端点处理器。
pub async fn pipelines_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<Vec<serde_json::Value>> {
    let pipelines: Vec<serde_json::Value> = state
        .manifests
        .read()
        .await
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

/// GET /api/v1/pipelines/runs——管道运行快照（统一管道管理数据源）。
///
/// runs × message_slots × pipeline_sessions × pipeline_run_summaries 四表联结
/// （store.list_pipelines_inner）：run → pipeline 映射经 message_slots.run_id，
/// pipeline → 会话经 pipeline_sessions，消耗账本经 pipeline_run_summaries
/// （sidecar 汇总写入，可为空）。无消息槽的 run（旧引擎 start_run 占位）被过滤。
/// 返回按 started_at 倒序的运行列表，供前端管道管理面板初始化/兜底刷新。
///
/// 查询参数：`status`（可选：running/suspended/completed/failed）、
/// `limit`（可选，默认 100，上限 500）。
///
/// 与 `/api/v1/pipelines`（管道插件清单，配置级）路径区分，两者互不覆盖。
pub async fn pipelines_runs_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Query(params): axum::extract::Query<HashMap<String, String>>,
    headers: axum::http::HeaderMap,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let db = state.db.clone().ok_or_else(|| ApiError::NotFound {
        message: "db store not injected".to_string(),
    })?;
    let tenant_ctx = crate::server::request_tenant_ctx(state.store.as_ref(), &headers, "").await;
    let status = params.get("status").filter(|s| !s.is_empty()).cloned();
    let limit = params
        .get("limit")
        .and_then(|v| v.parse::<u32>().ok())
        .unwrap_or(100)
        .min(500);
    let rows = tokio::task::spawn_blocking(move || {
        db.list_pipelines_inner(&tenant_ctx.tenant_id, status.as_deref(), limit)
    })
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("list_pipelines join 失败: {e}"),
    })?
    .map_err(|e| ApiError::Internal {
        message: format!("list_pipelines 查询失败: {e}"),
    })?;
    Ok(axum::Json(json!({ "items": rows })))
}

/// state 摘要提取的字段白名单（phase/迭代/上下文——messages 等大字段不出口）。
///
/// GAP-1（task = pipeline）：任务域 `task.*`（任务树展示）与 `lineage.*`
/// （父子分组/溯源——任务树按 parent 分组聚合、根形式天然不进树的出口依赖）
/// 以扁平点号键出口，风格与 `track.total_tokens` 一致。
const STATE_SUMMARY_KEYS: &[&str] = &[
    "agent_id",
    "agent_type",
    "config_id",
    "current_phase",
    "ended",
    "status",
    "session_id",
    "thread_id",
    "pipeline_id",
    "max_iterations",
    "ckpt_max_seq",
    "track.total_tokens",
    "track.execution_stats",
    "track.llm_usage",
    "cost_control.total_tokens",
    "cost_control.usage_percent",
    "termination_advisor.status",
    "router.stop_reason",
    "stuck_detected",
    "suspended",
    "metadata",
    "display_name",
    "name",
    "tags",
    "input",
    "raw_result",
    "raw_error",
    // GAP-1 阶段 1：任务域字段（管道 state 是任务单一真值，聚合是任务树数据源）
    "task.goal",
    "task.status",
    "task.id",
    "task.ended_at",
    // GAP-1 阶段 1：血缘字段（出生写入，任务树分组与溯源的出口依赖）
    "lineage.parent_pipeline_id",
    "lineage.origin_session_id",
    "lineage.root",
];

/// 从一份管道 state 提取摘要（白名单字段 + messages 条数）。
pub(crate) fn summarize_state(state: &serde_json::Value) -> serde_json::Value {
    let mut out = serde_json::Map::new();
    if let Some(obj) = state.as_object() {
        for k in STATE_SUMMARY_KEYS {
            if let Some(v) = obj.get(*k) {
                out.insert(k.to_string(), v.clone());
            }
        }
        // messages 只出口条数（迭代/轮次规模），不出口全文
        if let Some(msgs) = obj.get("messages").and_then(|v| v.as_array()) {
            out.insert("message_count".to_string(), json!(msgs.len()));
        }
    }
    serde_json::Value::Object(out)
}

/// GET /api/v1/pipelines/state — 管道 state 摘要列表（前端任务树数据源）。
///
/// 数据分层（state 是会话/任务/迭代的运行时真值，直接供前端消费）：
/// - **内存热数据**：PipelineStateRegistry 全部常驻条目（当前会话管道的
///   final_state，含 current_phase / status / iteration 等实时字段）。
/// - **DB 冷数据兜底**：registry 未覆盖的管道（重启后未再轮）从
///   pipeline_checkpoints 最新一条提取同构摘要。
///
/// messages 全文不出口（只给 message_count）；大字段按白名单裁剪。
pub async fn pipelines_state_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    headers: axum::http::HeaderMap,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let tenant_ctx = crate::server::request_tenant_ctx(state.store.as_ref(), &headers, "").await;
    let tenant_id = tenant_ctx.tenant_id;

    // 1) 内存热数据：registry 全部条目（锁内取 state 快照提摘要）
    let registry = agentos_session::pipeline_state_registry::global_registry();
    let mut items: Vec<serde_json::Value> = Vec::new();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    for listing in registry.list() {
        if listing.tenant_id != tenant_id {
            continue;
        }
        let Some(entry) = registry.get(&listing.tenant_id, &listing.pipeline_id) else {
            continue;
        };
        seen.insert(listing.pipeline_id.clone());
        let summary = {
            let e = entry.read();
            summarize_state(&e.state)
        };
        items.push(json!({
            "pipeline_id": listing.pipeline_id,
            "thread_id": listing.thread_id,
            "agent_id": listing.agent_id,
            "msg_sequence": listing.msg_sequence,
            "source": "memory",
            "state": summary,
        }));
    }

    // 2) DB 冷数据兜底：runs 清单里 registry 未覆盖的管道读最新 checkpoint
    if let Some(db) = state.db.as_ref() {
        let db = db.clone();
        let tid = tenant_id.clone();
        let rows = tokio::task::spawn_blocking(move || db.list_pipelines_inner(&tid, None, 200))
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("state list_pipelines join 失败: {e}"),
            })?
            .map_err(|e| ApiError::Internal {
                message: format!("state list_pipelines 查询失败: {e}"),
            })?;
        for row in rows {
            let pid = match row.pipeline_id.as_deref() {
                Some(p) if !p.is_empty() => p.to_string(),
                _ => continue,
            };
            if seen.contains(&pid) {
                continue;
            }
            let db2 = state.db.as_ref().expect("db checked above").clone();
            let tid2 = tenant_id.clone();
            let pid2 = pid.clone();
            let ckpt =
                tokio::task::spawn_blocking(move || db2.load_latest_checkpoint(&pid2, &tid2))
                    .await
                    .map_err(|e| ApiError::Internal {
                        message: format!("checkpoint join 失败: {e}"),
                    })?
                    .unwrap_or(None);
            let summary = match ckpt {
                Some((_, st)) => summarize_state(&st),
                None => continue, // 无 checkpoint 的孤儿 run 不出口
            };
            items.push(json!({
                "pipeline_id": pid,
                "thread_id": row.thread_id.clone().unwrap_or_default(),
                "agent_id": serde_json::Value::Null,
                "source": "checkpoint",
                "state": summary,
            }));
        }
    }

    Ok(axum::Json(json!({ "items": items })))
}

/// /api/v1/tools 端点处理器。
///
/// 从 CapabilityRegistry 返回已注册的工具列表；registry 未装配时回退
/// state.config 的 tools 数组。响应信封统一为 `{ "items": [...], "total": n }`
/// （对齐 /api/v1/agents 列表端点的模式；前端 ToolsPage 期待该形状）。
pub async fn tools_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<serde_json::Value> {
    let tools: Vec<serde_json::Value> = if let Some(registry) = &state.capability_registry {
        registry
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
            .collect()
    } else {
        // fallback: 从 config 获取（兼容旧逻辑）
        state
            .config
            .get("tools")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default()
    };
    let total = tools.len();
    axum::Json(json!({ "items": tools, "total": total }))
}

/// serde_json::Value 的类型名（供 400 错误消息说明实际拿到的结构）。
fn json_value_type_name(v: &serde_json::Value) -> &'static str {
    match v {
        serde_json::Value::Null => "null",
        serde_json::Value::Bool(_) => "bool",
        serde_json::Value::Number(_) => "number",
        serde_json::Value::String(_) => "string",
        serde_json::Value::Array(_) => "array",
        serde_json::Value::Object(_) => "object",
    }
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

/// env target 条目的掩码视图：{字段名: "***"(已设置) | ""(未设置)}（GAP-4）。
///
/// 读取语义与内核侧解析一致：进程环境优先，缺失回退 .env（用户可能经系统
/// 环境变量注入而非设置页——掩码视图如实反映"内核能否解析到"）。
fn masked_env_fields(
    mapping: &ConfigFileMapping,
    env_path: &std::path::Path,
) -> serde_json::Value {
    let text = std::fs::read_to_string(env_path).unwrap_or_default();
    let from_file = agentos_mcp::env_file::parse_env_text_for_read(&text);
    let mut out = serde_json::Map::new();
    for f in &mapping.fields {
        let set = std::env::var(&f.name).ok().is_some() || from_file.contains_key(&f.name);
        out.insert(
            f.name.clone(),
            serde_json::Value::String(if set { "***".to_string() } else { String::new() }),
        );
    }
    serde_json::Value::Object(out)
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

    let manifests = state.manifests.read().await;
    let manifest = find_manifest(&manifests, &plugin_id).ok_or_else(|| ApiError::NotFound {
        message: format!("plugin not found: {plugin_id}"),
    })?;

    let mapping = find_config_mapping(manifest, &file_id).ok_or_else(|| ApiError::NotFound {
        message: format!("config file_id not found: {file_id}"),
    })?;

    // ── env target 分支（GAP-4：key/加密字段写 .env）──
    // GET 语义：data = {字段名: "***"(已设置) | ""(未设置)}——*** 哨兵即
    // has_key 语义，前端按掩码渲染密码框；ETag 由该掩码视图派生（B4 乐观锁
    // 对同一视图生效）。
    if mapping.target.as_deref() == Some("env") {
        let env_path = agentos_mcp::env_file::env_path_for_root(&project_root);
        let data = masked_env_fields(mapping, &env_path);
        let etag = compute_etag(serde_json::to_string(&data).unwrap_or_default().as_bytes());
        return Ok(axum::Json(PluginConfigResponse {
            plugin_id,
            file_id,
            label: mapping.label.clone(),
            path: ".env".to_string(),
            data,
            etag,
        }));
    }

    let resolved = validate_config_path(&project_root, &mapping.path).map_err(config_err_to_api)?;
    let raw = std::fs::read_to_string(&resolved).map_err(|e| ApiError::NotFound {
        message: format!("config file read failed: {}: {e}", mapping.path),
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

    let manifests = state.manifests.read().await;
    let manifest = find_manifest(&manifests, &plugin_id).ok_or_else(|| ApiError::NotFound {
        message: format!("plugin not found: {plugin_id}"),
    })?;

    let mapping = find_config_mapping(manifest, &file_id).ok_or_else(|| ApiError::NotFound {
        message: format!("config file_id not found: {file_id}"),
    })?;

    // ── env target 分支（GAP-4）：*** 哨兵跳过、空值清除、新值写入 .env ──
    // 生效语义：写入即生效（stdio spawn overlay + HTTP resolve_env_placeholders
    // 均回读 .env，配合 invoker 的 .env mtime 指纹触发客户端重建）。
    if mapping.target.as_deref() == Some("env") {
        let env_path = agentos_mcp::env_file::env_path_for_root(&project_root);
        let current = masked_env_fields(mapping, &env_path);
        let current_etag =
            compute_etag(serde_json::to_string(&current).unwrap_or_default().as_bytes());
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
        let declared: std::collections::HashSet<&str> = mapping
            .fields
            .iter()
            .map(|f| f.name.as_str())
            .collect();
        let mut updates: Vec<(String, String)> = Vec::new();
        if let Some(obj) = req.data.as_object() {
            for (name, value) in obj {
                if !declared.contains(name.as_str()) {
                    return Err(ApiError::BadRequest {
                        message: format!("undeclared env field: {name}"),
                    });
                }
                let v = value.as_str().unwrap_or("");
                if v == "***" {
                    continue; // 哨兵：保留现值
                }
                updates.push((name.clone(), v.to_string()));
            }
        }
        agentos_mcp::env_file::write_env_updates(&env_path, &updates).map_err(|e| {
            ApiError::Internal {
                message: format!("write .env: {e}"),
            }
        })?;
        let data = masked_env_fields(mapping, &env_path);
        let new_etag =
            compute_etag(serde_json::to_string(&data).unwrap_or_default().as_bytes());
        return Ok(axum::Json(json!({
            "ok": true,
            "plugin_id": plugin_id,
            "file_id": file_id,
            "etag": new_etag,
        })));
    }

    let resolved = validate_config_path(&project_root, &mapping.path).map_err(config_err_to_api)?;

    let raw = std::fs::read_to_string(&resolved).map_err(|e| ApiError::NotFound {
        message: format!("config file read failed: {}: {e}", mapping.path),
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

    let stored: serde_json::Value = serde_yaml::from_str(&raw).map_err(|e| ApiError::Internal {
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

// ── 插件管理端点（/api/v1/plugins）——loader 监管能力（内核职责）──
//
// 转正说明（task_kernel_cleanup_and_split 任务 2）：由 compat_routes.rs 平移而来，
// 实现深度绑定 0.2 运行态（manifests / enabled_plugin_ids / default_profile.yaml），
// 非空 stub。GET /api/v1/plugins（原 /api/v1/plugins/status）与
// PUT /api/v1/plugins/{id}/enabled 保留转正；history/reload* 4 个死端点已删除
// （无任何前端/客户端消费者，见任务文档死代码清单）。

/// G8 排空 + 自退出共享实现（`system_restart_handler` 与 plugin_watcher 的
/// cdylib 变更自动重启共用——剩余项清仓批次 A3 抽取，watcher 经注入的回调调用，
/// 不 import axum handler）。
///
/// 流程：排空（在途 `running` runs → `suspended`，重启后 resume 续跑）→ 记日志
/// → 延迟 200ms 退出（让触发方的响应/日志先送达）。退出码 **75** =
/// "restart requested"，监督者（启动脚本循环 / Service 重启策略）据码拉起新进程。
///
/// 测试逃生门：设 `AGENTOS_DISABLE_SELF_EXIT=1` 时只排空不退出（嵌入/测试场景）。
///
/// 返回被排空的 run 数（触发方记入日志/响应）。
pub async fn drain_and_exit75(
    db: Option<&Arc<agentos_engine::SqliteStore>>,
    reason: &str,
) -> usize {
    let mut suspended_runs = 0usize;
    if let Some(db) = db {
        match db.suspend_running_runs() {
            Ok(n) => suspended_runs = n as usize,
            Err(e) => {
                tracing::warn!(target: "system-restart", error = %e, "排空失败（继续重启流程）");
            }
        }
    }
    tracing::info!(
        target: "system-restart",
        suspended = suspended_runs,
        reason = reason,
        "G8 优雅重启：排空完成，即将以 exit 75 退出（监督者负责拉起新进程）"
    );
    if std::env::var("AGENTOS_DISABLE_SELF_EXIT").is_err() {
        tokio::spawn(async {
            // 让触发方的响应/日志先 flush 再退出。
            tokio::time::sleep(std::time::Duration::from_millis(200)).await;
            std::process::exit(75);
        });
    }
    suspended_runs
}

/// POST /api/v1/system/restart — G8 优雅重启（restart-as-unload，cdylib 死结终结方案）。
///
/// 流程：排空（在途 `running` runs → `suspended`，重启后 resume 续跑）→ 回响应
/// → 延迟 200ms 退出（让 HTTP 响应先送达）。退出码 **75** = "restart requested"，
/// 监督者（启动脚本循环 / Service 重启策略）据码拉起新进程；无监督者时进程
/// 即停止（诚实行为——`cargo run` / 直接启动下重启请求表现为停机）。
///
/// 触发场景：cdylib 插件集变更（装/卸/换——dlclose 死结使其无法热更新，重启即
/// 卸载）；配置/插件批量变更后想要干净状态。sidecar 变更走热路径无需重启。
/// cdylib 集合变更的另一条自动触发路径在 plugin_watcher（经 drain_and_exit75）。
///
/// 诚实限制（计划 §四 G8 已声明）：在途 LLM 流式那一步被切断（resume 后重试该步）；
/// 前端经 WS 断线重连 + resync_required 拿到新状态。
///
/// 测试逃生门：设 `AGENTOS_DISABLE_SELF_EXIT=1` 时只排空不退出（嵌入/测试场景）。
pub async fn system_restart_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<serde_json::Value> {
    let suspended_runs = drain_and_exit75(state.db.as_ref(), "POST /api/v1/system/restart").await;
    axum::Json(json!({
        "success": true,
        "message": "内核排空完成，即将退出（exit 75）；监督者将重启进程",
        "exit_code": 75,
        "suspended_runs": suspended_runs,
    }))
}

/// POST /api/v1/plugins/validate-all — G2 双写一致性全量巡检。
///
/// 对照每个 tool 插件的 manifest 声明（`capabilities.tools`）与 sidecar 实际上报
/// （MCP `tools/list`）——工具名集合 + 参数 schema。漂移分类：
/// `missing`（声明有实际无）/ `undeclared`（实际有声明无）/ `schema_mismatch`。
///
/// 语义：spawn → 校验 → 回收（新 spawn 的连接校验后 kill，不破坏懒加载）；
/// 校验失败（spawn 失败 / host 不支持）不阻断，插件标记 `error` 并继续。
/// 结果只报告不处置（安装路径的处置见 plugin_watcher 的拒绝注册）。
pub async fn validate_all_plugins_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<serde_json::Value> {
    let Some(invoker) = state.invoker.clone() else {
        return axum::Json(json!({
            "checked": 0, "clean": 0, "drifted": 0, "errors": 1,
            "message": "invoker 未接线（validate-all 不可用）",
            "reports": [],
        }));
    };
    let mut reports: Vec<serde_json::Value> = Vec::new();
    let mut clean = 0usize;
    let mut drifted = 0usize;
    let mut errors = 0usize;
    for m in state.manifests.read().await.iter() {
        if m.capabilities.tools.is_empty() {
            continue; // 非 tool 插件无工具可对照
        }
        if m.host_type != agentos_core::traits::HostType::Sidecar {
            reports.push(json!({
                "plugin_id": m.id,
                "status": "skipped",
                "reason": format!("host_type {:?} 暂无 describe 通道（G2 渐进落地）", m.host_type),
                "mismatches": [],
            }));
            continue;
        }
        match invoker.list_plugin_tools(&m.id).await {
            Ok(raw) => {
                let (actual, malformed) = parse_actual_tools(&raw);
                let mismatches = compare_tools(&declared_with_services(m), &actual);
                let items: Vec<serde_json::Value> = mismatches
                    .iter()
                    .map(|mm| match mm {
                        agentos_invoker::verify::VerifyMismatch::Missing { name } => {
                            json!({"kind": "missing", "tool": name})
                        }
                        agentos_invoker::verify::VerifyMismatch::Undeclared { name } => {
                            json!({"kind": "undeclared", "tool": name})
                        }
                        agentos_invoker::verify::VerifyMismatch::SchemaMismatch {
                            name,
                            declared,
                            actual,
                        } => {
                            json!({
                                "kind": "schema_mismatch",
                                "tool": name,
                                "declared_schema": declared,
                                "actual_schema": actual,
                            })
                        }
                    })
                    .collect();
                if mismatches.is_empty() {
                    clean += 1;
                } else {
                    drifted += 1;
                }
                reports.push(json!({
                    "plugin_id": m.id,
                    "status": if mismatches.is_empty() { "clean" } else { "drifted" },
                    "declared_tools": m.capabilities.tools.len(),
                    "actual_tools": actual.len(),
                    "malformed_items": malformed,
                    "mismatches": items,
                }));
            }
            Err(e) => {
                errors += 1;
                reports.push(json!({
                    "plugin_id": m.id,
                    "status": "error",
                    "reason": e.message,
                    "mismatches": [],
                }));
            }
        }
    }
    axum::Json(json!({
        "checked": reports.len(),
        "clean": clean,
        "drifted": drifted,
        "errors": errors,
        "reports": reports,
    }))
}

/// GET /api/v1/plugins — 从 manifests 派生插件状态列表。
pub async fn plugins_status_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<serde_json::Value> {
    let enabled_ids = state.enabled_plugin_ids.read().await;
    let items: Vec<serde_json::Value> = state
        .manifests
        .read()
        .await
        .iter()
        .map(|m| {
            let config_type = match m.plugin_type {
                agentos_core::traits::PluginType::System => "system",
                agentos_core::traits::PluginType::Pipeline => "pipeline",
                agentos_core::traits::PluginType::Tool => "tool",
                agentos_core::traits::PluginType::Composite => "composite",
            };
            let enabled = enabled_ids.contains(&m.id);
            // 运行态：enabled 且是 sidecar → Active（按需 lazy 时实为 Idle，但无法从静态状态区分，
            // 统一标 active）；enabled 非 sidecar → active；disabled → disabled
            let run_status = if enabled { "active" } else { "disabled" };
            let activation = match m.activation {
                Some(agentos_core::traits::ActivationPolicy::Eager) => "eager",
                Some(agentos_core::traits::ActivationPolicy::Manual) => "manual",
                _ => "lazy", // None = 走 default_profile 默认 lazy
            };
            let host_type = match m.host_type {
                agentos_core::traits::HostType::InProcess => "in_process",
                agentos_core::traits::HostType::Sidecar => "sidecar",
            };
            json!({
                "plugin_id": m.id,
                "name": m.name,
                "config_type": config_type,
                "host_type": host_type,
                "version": m.version,
                "enabled": enabled,
                "activation": activation,
                "status": run_status,
                "config_files": m.config_files.iter().map(|c| json!({
                    "id": c.id,
                    "label": c.label,
                    "path": c.path,
                })).collect::<Vec<_>>(),
                "has_contributes": m.contributes.is_some(),
                "has_http_endpoints": !m.http_endpoints.is_empty(),
                "error": null,
            })
        })
        .collect();
    axum::Json(json!(items))
}

/// PUT /api/v1/plugins/{id}/enabled — 切换插件启用状态（写 default_profile.yaml）。
///
/// 安装触发模型 L1：改 profile 文件后热更新内存状态；启用需重启内核完全生效
/// （axum 路由树启动期固定），禁用立即生效（摘除 capability registry）。
/// 返回 {success, enabled, restart_required}。
pub async fn plugins_set_enabled_handler(
    axum::extract::Path(plugin_id): axum::extract::Path<String>,
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::Json(body): axum::Json<EnabledBody>,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let new_enabled = body.enabled;
    let project_root = match &state.project_root {
        Some(p) => p,
        None => {
            return Err(ApiError::Internal {
                message: "project_root not available".to_string(),
            })
        }
    };
    let profile_path = project_root
        .join("config")
        .join("plugins")
        .join("default_profile.yaml");

    // 读现有 profile（不存在则用空结构）
    let raw = std::fs::read_to_string(&profile_path).unwrap_or_default();
    let mut doc: serde_yaml::Value = serde_yaml::from_str(&raw).unwrap_or_else(|_| {
        serde_yaml::from_str(
            "version: 1\nplugins:\ndefaults:\n  enabled: true\n  activation: lazy\n",
        )
        .unwrap()
    });

    // 改 plugins.<id>.enabled（手动操作 serde_yaml Mapping）
    if let serde_yaml::Value::Mapping(ref mut top) = doc {
        // 确保 plugins 键存在且是 Mapping
        let plugins_key = serde_yaml::Value::String("plugins".into());
        if !top.contains_key(&plugins_key) {
            top.insert(
                plugins_key.clone(),
                serde_yaml::Value::Mapping(serde_yaml::Mapping::new()),
            );
        }
        if let Some(serde_yaml::Value::Mapping(ref mut plugins_map)) = top.get_mut(&plugins_key) {
            let pid_key = serde_yaml::Value::String(plugin_id.clone());
            // 确保该插件条目存在
            if !plugins_map.contains_key(&pid_key) {
                plugins_map.insert(
                    pid_key.clone(),
                    serde_yaml::Value::Mapping(serde_yaml::Mapping::new()),
                );
            }
            if let Some(serde_yaml::Value::Mapping(ref mut entry)) = plugins_map.get_mut(&pid_key) {
                entry.insert(
                    serde_yaml::Value::String("enabled".into()),
                    serde_yaml::Value::Bool(new_enabled),
                );
            }
        }
    }

    // 写回
    let new_raw = serde_yaml::to_string(&doc).unwrap_or_default();
    match std::fs::write(&profile_path, new_raw) {
        Ok(_) => {
            // ── 热加载：立即改内存状态，不用重启 ──
            // 1) 改 enabled_plugin_ids（schema 出口的 contributes/configs 立即生效）
            {
                let mut ids = state.enabled_plugin_ids.write().await;
                if new_enabled {
                    ids.insert(plugin_id.clone());
                } else {
                    ids.remove(&plugin_id);
                }
            }
            // 2) 注册表对称热更新（G1 enable 对称化 + M1 scope 结构性收回）：
            //    禁用 → scope revoke（全部注册 guard 一次性收回）+ clear_plugin 兜底
            //           + broadcaster 绑定移除（零残留）；
            //    启用 → 立即重注册 tools/route_signals/http_routes（guarded，入新 scope）。
            //    /ext/{*rest} 通配分发是注册表数据驱动（http_dispatcher），路由树无需
            //    重启重建。
            let mut registered = serde_json::Value::Null;
            if let Some(registry) = &state.capability_registry {
                use agentos_core::traits::CapabilityRegistry;
                if new_enabled {
                    match state
                        .manifests
                        .read()
                        .await
                        .iter()
                        .find(|m| m.id == plugin_id)
                    {
                        Some(m) => {
                            let (tools, http_routes) =
                                crate::plugin_lifecycle::reenable_plugin_capabilities(
                                    m,
                                    registry,
                                    Some(&state.plugin_scopes),
                                );
                            tracing::info!(
                                target: "plugin-enablement",
                                "plugin {} re-enabled: re-registered tools={} http_routes={}",
                                plugin_id, tools, http_routes
                            );
                            registered = serde_json::json!({
                                "tools": tools, "http_routes": http_routes
                            });
                        }
                        None => {
                            tracing::warn!(
                                target: "plugin-enablement",
                                "plugin {} enabled but manifest not found; nothing re-registered",
                                plugin_id
                            );
                        }
                    }
                } else {
                    // M1：先经 scope 收回全部登记（registry 四维 + broadcaster 绑定），
                    // 再 clear_plugin 兜底（scope 无登记的直连注册路径仍被覆盖）。
                    state.plugin_scopes.revoke(&plugin_id);
                    if let Some(bindings) = &state.widget_bindings {
                        remove_plugin_bindings(bindings, &plugin_id);
                    }
                    registry.clear_plugin(&plugin_id);
                    // G3：动态注册的持久化记录随禁用删除（动态注册生命周期 =
                    // 插件启用周期；re-enable 后插件经 on_load/运行时自行重建）。
                    if let Some(db) = &state.db {
                        match db.delete_dynamic_tools_by_plugin(&plugin_id) {
                            Ok(n) if n > 0 => {
                                tracing::info!(
                                    target: "plugin-enablement",
                                    "plugin {} disabled: dropped {} dynamic tool registrations",
                                    plugin_id, n
                                );
                            }
                            Ok(_) => {}
                            Err(e) => {
                                tracing::warn!(
                                    target: "plugin-enablement",
                                    "plugin {} disabled but dynamic_tools cleanup failed: {}",
                                    plugin_id, e
                                );
                            }
                        }
                    }
                }
            }
            let restart_needed = false; // 双向即时生效（G1）
            tracing::info!(
                target: "plugin-enablement",
                "plugin {} enabled={} (hot-reloaded: contributes + registry updated, restart={})",
                plugin_id, new_enabled, restart_needed
            );
            // 剩余项清仓 D2：schema 变更推送——enable/disable 已改变 schema 聚合
            // （tools/contributes/configs），best-effort 广播 widget_event
            // {schema, changed} 让前端增量重载（前端消费见 resync.ts）。
            // 失败静默（观察层不拖垮主流程：session 未启用/无连接时 broadcast
            // 返回 0，不视为错误）。
            if let Some(session) = &state.session {
                let _ = session
                    .broadcast_widget(
                        "schema",
                        "changed",
                        json!({ "plugin_id": plugin_id, "enabled": new_enabled }),
                        "kernel",
                    )
                    .await;
            }
            Ok(axum::Json(json!({
                "success": true,
                "plugin_id": plugin_id,
                "enabled": new_enabled,
                "restart_required": restart_needed,
                "registered": registered,
                "message": if new_enabled {
                    format!("已启用插件 {}（立即生效）", plugin_id)
                } else {
                    format!("已禁用插件 {}（立即生效）", plugin_id)
                },
            })))
        }
        // A12：写盘失败 → 5xx 统一错误信封（不再 200 + success:false 混装，
        // 前端无法据状态码区分"已生效"与"根本没写进去"）。
        Err(e) => {
            tracing::error!(
                target: "plugin-enablement",
                plugin_id = %plugin_id,
                error = %e,
                "写入 profile 失败"
            );
            Err(ApiError::Internal {
                message: format!("写入 profile 失败: {e}"),
            })
        }
    }
}

/// PUT /api/v1/plugins/{id}/enabled 请求体。
#[derive(Debug, Deserialize)]
pub struct EnabledBody {
    pub enabled: bool,
}

// ── 监控 M5b：Prometheus 导出端点（监控设计 §十一）──
//
// boot-plugin 第三刀拆分（对齐 db-admin 模式）：查询面（原 GET /api/v1/metrics）
// 已迁 `metrics-admin` capability（metrics/capability.rs 的 query/list method，
// HTTP 面在 plugins/shared/metrics_admin 插件 /ext/metrics_admin/**）；
// /metrics 保留内核——Prometheus 抓取方通常不鉴权且是运维契约，URL 稳定优先。

/// GET /metrics（Prometheus exposition format，监控设计 §十一 决策3）。
///
/// 返回纯文本 Prometheus exposition 格式，供 Prometheus/Grafana 抓取。
/// 插件面副本：/ext/metrics_admin/prometheus（metrics-admin capability）。
pub async fn metrics_prometheus_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> Result<String, ApiError> {
    let agg = state.metrics.as_ref().ok_or_else(|| ApiError::NotFound {
        message: "metrics aggregator not enabled".to_string(),
    })?;
    let views = agg.snapshot();
    Ok(export_prometheus(&views))
}

// ── P7: 管道配置查询/更新端点（/api/v1/config/pipelines/{name}）──

/// 校验管道名白名单（防路径穿越）。
///
/// 只允许字母、数字、下划线、连字符；拒绝 `/`、`\`、`.`（含 `..`）、空串。
fn validate_pipeline_name(name: &str) -> bool {
    !name.is_empty()
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

/// 管道配置 GET 响应体：name + data（YAML 解析为 JSON）+ etag。
#[derive(Debug, Serialize)]
pub struct PipelineConfigResponse {
    pub name: String,
    pub data: serde_json::Value,
    pub etag: String,
}

/// 管道配置 PUT 请求体：data 为完整管道配置内容（对齐 GenericConfigUpdateRequest）。
#[derive(Debug, Deserialize)]
pub struct PipelineConfigUpdateRequest {
    pub data: serde_json::Value,
    /// GET 返回的 ETag（If-Match 乐观锁，对齐 plugin config PUT：缺失/不匹配 → 409）。
    pub if_match: Option<String>,
}

/// 解析管道配置文件路径：`config/pipelines/{name}.yaml`。
fn pipeline_config_path(project_root: &std::path::Path, name: &str) -> std::path::PathBuf {
    project_root
        .join("config")
        .join("pipelines")
        .join(format!("{name}.yaml"))
}

/// GET /api/v1/config/pipelines/{name}（P7）。
///
/// 返回 config/pipelines/{name}.yaml 的内容（YAML → JSON）+ ETag。
/// 未知管道 → 404；非法 name（路径穿越）→ 400。
pub async fn get_pipeline_config_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path(name): axum::extract::Path<String>,
) -> Result<axum::Json<PipelineConfigResponse>, ApiError> {
    if !validate_pipeline_name(&name) {
        return Err(ApiError::BadRequest {
            message: format!("invalid pipeline name: {name}"),
        });
    }
    let project_root = state.project_root.ok_or_else(|| ApiError::Internal {
        message: "project_root not configured".to_string(),
    })?;
    let path = pipeline_config_path(&project_root, &name);
    let raw = std::fs::read_to_string(&path).map_err(|e| ApiError::NotFound {
        message: format!("pipeline config read failed: {name}: {e}"),
    })?;
    let etag = compute_etag(raw.as_bytes());
    let data: serde_json::Value = serde_yaml::from_str(&raw).map_err(|e| ApiError::Internal {
        message: format!("pipeline config yaml parse error: {e}"),
    })?;
    Ok(axum::Json(PipelineConfigResponse { name, data, etag }))
}

/// PUT /api/v1/config/pipelines/{name}（P7）。
///
/// 校验（T2/A13，对齐 plugin config PUT）：data 必须是 YAML 映射（非映射文档
/// 无法表示管道配置）→ 400；If-Match 乐观锁（缺失/与磁盘 ETag 不匹配 → 409；
/// 文件不存在 → 404，不再支持 PUT 隐式创建）。通过后原子写回
/// config/pipelines/{name}.yaml（tmp + rename + round-trip 校验），返回新 ETag。
/// 非法 name → 400。
pub async fn put_pipeline_config_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path(name): axum::extract::Path<String>,
    axum::Json(req): axum::Json<PipelineConfigUpdateRequest>,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    if !validate_pipeline_name(&name) {
        return Err(ApiError::BadRequest {
            message: format!("invalid pipeline name: {name}"),
        });
    }
    // 结构校验（T2）：管道配置必须是映射——标量/序列无法承载管道字段，
    // 拒写保持磁盘原值。
    if !req.data.is_object() {
        return Err(ApiError::BadRequest {
            message: format!(
                "pipeline config must be a yaml mapping, got: {}",
                json_value_type_name(&req.data)
            ),
        });
    }
    let project_root = state.project_root.ok_or_else(|| ApiError::Internal {
        message: "project_root not configured".to_string(),
    })?;
    let path = pipeline_config_path(&project_root, &name);

    // If-Match 乐观锁（A13）：必须匹配磁盘当前 ETag；文件不存在/不可读 → 404。
    let current_etag = match std::fs::read_to_string(&path) {
        Ok(raw) => compute_etag(raw.as_bytes()),
        Err(e) => {
            return Err(ApiError::NotFound {
                message: format!("pipeline config read failed: {name}: {e}"),
            })
        }
    };
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

    // B4/B6：原子写 + round-trip 校验（复用 config_service）
    atomic_write_yaml(&path, &req.data).map_err(config_err_to_api)?;

    let new_etag = compute_etag(
        std::fs::read_to_string(&path)
            .map_err(|e| ApiError::Internal {
                message: format!("re-read after write failed: {e}"),
            })?
            .as_bytes(),
    );

    Ok(axum::Json(json!({
        "name": name,
        "etag": new_etag,
    })))
}

/// GET /api/v1/config/pipelines/{name}（带 ETag 头）。
///
/// 覆盖默认 JSON 响应，附加 `ETag` header（B4 乐观锁语义，供前端 If-Match 用）。
pub async fn get_pipeline_config_with_etag(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path(name): axum::extract::Path<String>,
) -> Result<axum::response::Response, ApiError> {
    let axum::Json(resp) =
        get_pipeline_config_handler(axum::extract::State(state), axum::extract::Path(name)).await?;
    let etag = resp.etag.clone();
    Ok(([(axum::http::header::ETAG, etag)], axum::Json(resp)).into_response())
}

#[cfg(test)]
mod tdd4_config_center_tests {
    //! TDD-4: AppState.config_center 字段 + with_config_center builder 测试。
    //! 设计依据：docs/working/重要设计/统一配置加载方案.md 阶段 1。

    use super::*;
    use agentos_config::config_center::ConfigCenter;

    #[test]
    fn test_new_app_state_has_no_config_center() {
        // 契约：默认构造的 AppState，config_center 为 None（未接线降级）
        let state = AppState::new();
        assert!(state.config_center.is_none());
    }

    #[test]
    fn test_with_config_center_injects_instance() {
        // 契约：with_config_center 注入后，config_center 为 Some
        let temp = tempfile::tempdir().unwrap();
        let cc = Arc::new(ConfigCenter::new(temp.path().to_path_buf()));

        let state = AppState::new().with_config_center(cc);

        assert!(state.config_center.is_some());
    }

    #[test]
    fn test_injected_config_center_can_load() {
        // 契约：注入的 ConfigCenter 能 load 配置文件（端到端可用性）
        let temp = tempfile::tempdir().unwrap();
        let config_dir = temp.path().join("config");
        std::fs::create_dir_all(&config_dir).unwrap();
        std::fs::write(config_dir.join("test.yaml"), "key: value\n").unwrap();

        let cc = Arc::new(ConfigCenter::new(config_dir));
        let state = AppState::new().with_config_center(cc);

        let cc = state.config_center.as_ref().expect("应已注入");
        let val = cc.load("test.yaml").expect("load 应成功");
        assert_eq!(val["key"], "value");
    }
}

#[cfg(test)]
mod state_summary_tests {
    //! GAP-1 阶段 1：任务域/血缘字段出得来——STATE_SUMMARY_KEYS 白名单扩展。
    //! task = pipeline 后，/api/v1/pipelines/state 聚合是任务树数据源，
    //! task.*（任务字段）与 lineage.*（父子分组/溯源）必须出口，不被 summarize 裁掉。

    use super::*;

    #[test]
    fn test_summarize_state_exports_task_and_lineage_fields() {
        let state = json!({
            "pipeline_id": "p1",
            "task.goal": "喝水提醒",
            "task.status": "running",
            "task.id": "t1",
            "lineage.parent_pipeline_id": "pipe_parent",
            "lineage.origin_session_id": "sess_root",
            "lineage.root": true,
            "messages": [{"role": "user"}, {"role": "assistant"}],
        });
        let s = summarize_state(&state);
        assert_eq!(s["task.goal"], "喝水提醒");
        assert_eq!(s["task.status"], "running");
        assert_eq!(s["task.id"], "t1");
        assert_eq!(s["lineage.parent_pipeline_id"], "pipe_parent");
        assert_eq!(s["lineage.origin_session_id"], "sess_root");
        assert_eq!(s["lineage.root"], true);
        // messages 仍只出口条数（大字段不出口的既有契约不变）
        assert!(s.get("messages").is_none());
        assert_eq!(s["message_count"], 2);
    }

    #[test]
    fn test_summarize_state_omits_absent_task_and_lineage_fields() {
        // 反向性质：无任务域字段的普通会话管道，摘要不得伪造 task.*/lineage.* 键
        let s = summarize_state(&json!({"pipeline_id": "p2", "status": "running"}));
        assert_eq!(s["pipeline_id"], "p2");
        assert_eq!(s["status"], "running");
        for k in [
            "task.goal",
            "task.status",
            "task.id",
            "lineage.parent_pipeline_id",
            "lineage.origin_session_id",
            "lineage.root",
        ] {
            assert!(s.get(k).is_none(), "{k} 不应被伪造");
        }
    }

    #[test]
    fn test_summarize_state_still_cuts_non_whitelisted_fields() {
        // 白名单机制本身不变：新增键不放开白名单外字段
        let s = summarize_state(&json!({
            "pipeline_id": "p3",
            "secret_blob": "x",
            "task.goal": "g"
        }));
        assert!(s.get("secret_blob").is_none(), "白名单外字段仍裁剪");
        assert_eq!(s["task.goal"], "g");
        // raw_result（最终输出）与 input 对称出口——复盘报告提取/任务树展示依赖
        let s2 = summarize_state(&json!({"pipeline_id": "p4", "raw_result": "复盘结论：x"}));
        assert_eq!(s2["raw_result"], "复盘结论：x");
        // 终态回写的 task.ended_at 出口（任务树展示完成时间）
        let s3 = summarize_state(&json!({"pipeline_id": "p5", "task.ended_at": "2026-08-16T09:00:00Z"}));
        assert_eq!(s3["task.ended_at"], "2026-08-16T09:00:00Z");
    }
}
