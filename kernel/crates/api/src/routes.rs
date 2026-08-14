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
use agentos_plugin_loader::CapabilityRegistryImpl;
use axum::response::IntoResponse;
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::config_service::{
    apply_put_masked_sentinels, atomic_write_yaml, compute_etag, mask_secrets,
    validate_config_path,
};
use crate::error::ApiError;
use crate::metrics::{export_prometheus, Labels, MetricsAggregator};

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
}

impl AppState {
    pub fn new() -> Self {
        Self {
            config: json!({}),
            manifests: Arc::new(Vec::new()),
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
        }
    }

    pub fn with_config(config: serde_json::Value) -> Self {
        Self {
            config,
            manifests: Arc::new(Vec::new()),
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
            manifests: Arc::new(manifests),
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

    /// P2：启用会话内核（连接注册表 / 事件总线 / 重放缓冲 + 入站路由）。
    ///
    /// 在 `with_plugins` 后调用，注入 SessionCoordinator 与基于引擎的
    /// 入站分发器。ws_handler 据此承载真实 WS 会话；未调用时 ws_handler
    /// 降级为旧 echo/engine 路径（兼容）。
    pub fn enable_session(self) -> Self {
        let session = Arc::new(agentos_session::SessionCoordinator::new());
        // 管道 state 常驻注册表（与 session 同生命期，一起启用）。
        // 关键：先把 session 注入 self，再 clone 给 dispatcher。
        // 否则 dispatcher 持有的 state.session 永远是 None（旧 bug：dispatcher
        // 用了设置 session 字段之前的 clone，导致引擎结果无法推回前端）。
        let self_with_session = Self {
            session: Some(session.clone()),
            inbound_router: None,
            ..self
        };
        let dispatcher = Arc::new(crate::ws_session::EngineDispatcher::new(self_with_session.clone()));
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
        let dispatcher = Arc::new(crate::ws_session::EngineDispatcher::new(self_with_session.clone()));
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

    /// 监控 M1：注入指标聚合器（启用 `/api/v1/metrics` 与 `/metrics` 端点）。
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

    // P4/P5：聚合各插件的 contributes（仅含声明 contributes 且 enabled 的插件）。
    // 内核不解释结构，透传给前端 ContributionRegistry（ADR §3.4/§六）。
    // 安装触发模型 L1：disabled 插件的 contributes 不出口（tools/http 已过滤，UI 也不过来）。
    let enabled_ids = state.enabled_plugin_ids.read().await;
    let plugin_contributes: Vec<serde_json::Value> = state
        .manifests
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

/// GET /api/v1/agents/{id}/config——读取指定 agent 的 yaml 文件内容。
///
/// 按 `resolve_agent_yaml_path` 定位文件（顶层 + 递归分类子目录），返回
/// `{ config_id, yaml }`（yaml 为文件原文）。id 不存在 → 404。
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
    Ok(axum::Json(json!({ "config_id": agent_id, "yaml": raw })))
}

/// PUT /api/v1/agents/{id}/config 请求体。
#[derive(Debug, Deserialize)]
pub struct AgentConfigUpdateRequest {
    /// 新的 yaml 文件内容（原文写回）。
    pub yaml: String,
}

/// PUT /api/v1/agents/{id}/config——写回 agent 的 yaml 文件。
///
/// 流程：定位文件（不存在 → 404）→ 先备份原文件为 `<file>.yaml.bak` →
/// 写新内容 → 返回 `{ config_id, success, backup }`。
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

    // 先备份原文件（同目录 <file>.yaml.bak），再写新内容
    let backup = path.with_extension("yaml.bak");
    std::fs::copy(&path, &backup).map_err(|e| ApiError::Internal {
        message: format!("backup agent config {}: {e}", path.display()),
    })?;
    std::fs::write(&path, &req.yaml).map_err(|e| ApiError::Internal {
        message: format!("write agent config {}: {e}", path.display()),
    })?;

    Ok(axum::Json(json!({
        "config_id": agent_id,
        "success": true,
        "backup": backup.file_name().map(|n| n.to_string_lossy().to_string()),
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
/// 3. 命中后,若 command 声明了显式路由(条目里的 `tool` 字段指向工具名)且 invoker 可用,
///    经 `invoker.invoke_tool(plugin_id, tool, args)` 调插件 sidecar(参考 capability_router
///    的 tool-executor.invoke 模式);否则返回 success 占位(后续再细化路由)。
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
    for m in state.manifests.iter() {
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
    // 缺省(无 tool 字段 / invoker 不可用)→ success 占位,保证前端链路通。
    if let Some(tool_name) = command_entry
        .get("tool")
        .and_then(|v| v.as_str())
        .map(str::to_string)
    {
        if let Some(invoker) = state.invoker.clone() {
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
    }

    // 占位成功:command 已声明但无显式执行路由(或测试环境 invoker=None)。
    // 后续可扩展为按 command 声明的 handler_capability/方法名路由到具体 sidecar。
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

fn collect_yaml_files_inner(dir: &std::path::Path, files: &mut Vec<std::path::PathBuf>) -> std::io::Result<()> {
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
    let status = params
        .get("status")
        .filter(|s| !s.is_empty())
        .cloned();
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

// ── 插件管理端点（/api/v1/plugins）——loader 监管能力（内核职责）──
//
// 转正说明（task_kernel_cleanup_and_split 任务 2）：由 compat_routes.rs 平移而来，
// 实现深度绑定 0.2 运行态（manifests / enabled_plugin_ids / default_profile.yaml），
// 非空 stub。GET /api/v1/plugins（原 /api/v1/plugins/status）与
// PUT /api/v1/plugins/{id}/enabled 保留转正；history/reload* 4 个死端点已删除
// （无任何前端/客户端消费者，见任务文档死代码清单）。

/// GET /api/v1/plugins — 从 manifests 派生插件状态列表。
pub async fn plugins_status_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<serde_json::Value> {
    let enabled_ids = state.enabled_plugin_ids.read().await;
    let items: Vec<serde_json::Value> = state
        .manifests
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
                agentos_core::traits::HostType::Wasm => "wasm",
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
) -> axum::Json<serde_json::Value> {
    let new_enabled = body.enabled;
    let project_root = match &state.project_root {
        Some(p) => p,
        None => {
            return axum::Json(json!({
                "success": false,
                "error": "project_root not available",
            }))
        }
    };
    let profile_path = project_root.join("config").join("plugins").join("default_profile.yaml");

    // 读现有 profile（不存在则用空结构）
    let raw = std::fs::read_to_string(&profile_path).unwrap_or_default();
    let mut doc: serde_yaml::Value = serde_yaml::from_str(&raw).unwrap_or_else(|_| {
        serde_yaml::from_str("version: 1\nplugins:\ndefaults:\n  enabled: true\n  activation: lazy\n")
            .unwrap()
    });

    // 改 plugins.<id>.enabled（手动操作 serde_yaml Mapping）
    if let serde_yaml::Value::Mapping(ref mut top) = doc {
        // 确保 plugins 键存在且是 Mapping
        let plugins_key = serde_yaml::Value::String("plugins".into());
        if !top.contains_key(&plugins_key) {
            top.insert(plugins_key.clone(), serde_yaml::Value::Mapping(serde_yaml::Mapping::new()));
        }
        if let Some(serde_yaml::Value::Mapping(ref mut plugins_map)) = top.get_mut(&plugins_key) {
            let pid_key = serde_yaml::Value::String(plugin_id.clone());
            // 确保该插件条目存在
            if !plugins_map.contains_key(&pid_key) {
                plugins_map.insert(pid_key.clone(), serde_yaml::Value::Mapping(serde_yaml::Mapping::new()));
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
            // 2) 禁用时立即从 CapabilityRegistry 摘掉（tools/http_routes 立即不可用）
            //    启用时重新注册需要重启（axum 路由树在启动期固定，运行时无法动态加路由）
            if !new_enabled {
                if let Some(registry) = &state.capability_registry {
                    use agentos_core::traits::CapabilityRegistry;
                    registry.clear_plugin(&plugin_id);
                }
            }
            let restart_needed = new_enabled; // 启用需重启（路由重建），禁用立即生效
            tracing::info!(
                target: "plugin-enablement",
                "plugin {} enabled={} (hot-reloaded: contributes + registry updated, restart={})",
                plugin_id, new_enabled, restart_needed
            );
            axum::Json(json!({
                "success": true,
                "plugin_id": plugin_id,
                "enabled": new_enabled,
                "restart_required": restart_needed,
                "message": if restart_needed {
                    format!("已启用插件 {}（重启后完全生效，contributes 已立即更新）", plugin_id)
                } else {
                    format!("已禁用插件 {}（立即生效）", plugin_id)
                },
            }))
        }
        Err(e) => axum::Json(json!({
            "success": false,
            "error": format!("写入 profile 失败: {}", e),
        })),
    }
}

/// PUT /api/v1/plugins/{id}/enabled 请求体。
#[derive(Debug, Deserialize)]
pub struct EnabledBody {
    pub enabled: bool,
}

// ── 监控 M5/M5b：指标查询与导出端点（监控设计 §五）──

/// `/api/v1/metrics` 查询参数。
#[derive(Debug, Default, Deserialize)]
pub struct MetricsQueryParams {
    /// 插件 id 过滤（缺省=all）。
    pub plugin: Option<String>,
    /// 指标名过滤（缺省=all）。
    pub metric: Option<String>,
    /// 时间窗（5m/1h/24h，缺省=1h）。
    pub window: Option<String>,
    /// labels 过滤，格式 key:value，多个用逗号。
    pub labels: Option<String>,
}

/// 单条指标查询结果（监控设计 §五 响应）。
#[derive(Debug, Serialize)]
pub struct MetricSeriesResponse {
    pub plugin_id: String,
    pub name: String,
    #[serde(rename = "type")]
    pub metric_type: String,
    pub labels: serde_json::Value,
    pub samples: Vec<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub unit: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub latest: Option<f64>,
}

/// `/api/v1/metrics` 响应。
#[derive(Debug, Serialize)]
pub struct MetricsResponse {
    pub metrics: Vec<MetricSeriesResponse>,
}

/// 解析 window 字符串为 Duration。
fn parse_window(s: &str) -> std::time::Duration {
    match s.trim() {
        "5m" => std::time::Duration::from_secs(5 * 60),
        "1h" => std::time::Duration::from_secs(60 * 60),
        "24h" => std::time::Duration::from_secs(24 * 60 * 60),
        _ => std::time::Duration::from_secs(60 * 60), // 默认 1h
    }
}

/// 解析 labels 查询串（"model:deepseek,region:us"）。
fn parse_labels_query(s: &str) -> Labels {
    let mut out = Labels::new();
    for pair in s.split(',') {
        let pair = pair.trim();
        if let Some((k, v)) = pair.split_once(':') {
            let k = k.trim();
            let v = v.trim();
            if !k.is_empty() {
                out.insert(k.to_string(), v.to_string());
            }
        }
    }
    out
}

/// GET /api/v1/metrics（监控设计 §五）。
///
/// 支持 ?plugin=&metric=&window=5m/1h/24h&labels=key:value 过滤。
pub async fn metrics_query_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Query(params): axum::extract::Query<MetricsQueryParams>,
) -> Result<axum::Json<MetricsResponse>, ApiError> {
    let agg = state.metrics.as_ref().ok_or_else(|| ApiError::NotFound {
        message: "metrics aggregator not enabled".to_string(),
    })?;
    let window = params
        .window
        .as_deref()
        .map(parse_window)
        .unwrap_or_else(|| std::time::Duration::from_secs(60 * 60));
    let labels_filter = params
        .labels
        .as_deref()
        .map(parse_labels_query)
        .unwrap_or_default();
    let views = agg.query(
        params.plugin.as_deref(),
        params.metric.as_deref(),
        Some(window),
        &labels_filter,
    );
    let metrics = views
        .into_iter()
        .map(|v| MetricSeriesResponse {
            plugin_id: v.plugin_id,
            name: v.name,
            metric_type: v.metric_type.as_str().to_string(),
            labels: serde_json::to_value(&v.labels).unwrap_or(serde_json::json!({})),
            samples: v
                .samples
                .into_iter()
                .map(|s| serde_json::json!({"ts": s.ts, "value": s.value}))
                .collect(),
            unit: v.unit,
            latest: v.latest,
        })
        .collect();
    Ok(axum::Json(MetricsResponse { metrics }))
}

/// GET /metrics（Prometheus exposition format，监控设计 §十一 决策3）。
///
/// 返回纯文本 Prometheus exposition 格式，供 Prometheus/Grafana 抓取。
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
}

/// 解析管道配置文件路径：`config/pipelines/{name}.yaml`。
fn pipeline_config_path(project_root: &std::path::Path, name: &str) -> std::path::PathBuf {
    project_root.join("config").join("pipelines").join(format!("{name}.yaml"))
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
    let raw = std::fs::read_to_string(&path).map_err(|_| ApiError::NotFound {
        message: format!("pipeline config not found: {name}"),
    })?;
    let etag = compute_etag(raw.as_bytes());
    let data: serde_json::Value = serde_yaml::from_str(&raw).map_err(|e| ApiError::Internal {
        message: format!("pipeline config yaml parse error: {e}"),
    })?;
    Ok(axum::Json(PipelineConfigResponse { name, data, etag }))
}

/// PUT /api/v1/config/pipelines/{name}（P7）。
///
/// 原子写回 config/pipelines/{name}.yaml（tmp + rename + round-trip 校验），
/// 返回新 ETag。非法 name → 400；父目录缺失自动创建。
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
    let project_root = state.project_root.ok_or_else(|| ApiError::Internal {
        message: "project_root not configured".to_string(),
    })?;
    let path = pipeline_config_path(&project_root, &name);

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
    let axum::Json(resp) = get_pipeline_config_handler(
        axum::extract::State(state),
        axum::extract::Path(name),
    )
    .await?;
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
