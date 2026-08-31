//! HTTP 路由处理器
//!
//! 提供健康检查、Schema 聚合、能力清单等 RESTful 端点。
//!

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
use crate::metrics::plugin_widget_broadcast::{remove_plugin_bindings, WidgetBinding};
use crate::metrics::{export_prometheus, MetricsAggregator};
use agentos_http::error::ApiError;

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
    /// 内核基础设施能力契约聚合（config/kernel_capabilities/*.json 透出）。
    /// 与插件侧 plugin.json 契约同构：前端/调用方/入口校验消费同一份定义，
    /// 不读代码副本（单一真值源，消除双轨漂移）。
    pub kernel_capabilities: Vec<serde_json::Value>,
}

/// 应用状态——通过 Axum State 共享。
///
/// 集成插件系统后，持有能力注册表、管道引擎以及 0.2 引擎所需的运行期资源
/// （pipeline_config / step_library / invoker / store / project_root）。已知插件
/// id 集合不在此快照——经 `manifests` 共享存储现读（watcher 热发现即时可见）。
#[derive(Clone)]
pub struct AppState {
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
    /// 项目根目录（`{{path:...}}` 模板解析基准 + agent 配置加载基准）
    pub project_root: Option<PathBuf>,
    /// P2：会话协调器（连接注册表 / 事件总线 / 重放缓冲）。None = WS 入口 503 拒连。
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
    /// 插件根目录映射（plugin_id → 插件根目录绝对路径）。
    ///
    /// 由启动期 loader 扫描结果填充（或测试直接构造）。HTTP dispatcher 据此把
    /// `/ext/{plugin_id}/assets/{*path}` 解析到 `<plugin_root>/web/<path>`，
    /// 直接读文件返回，免去为每个子资源单独声明 http_endpoints。
    /// 空 map = 无插件托管静态资源（仅静态路由 + dispatcher）。
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
    /// 闸2·观测：插件契约状态账本（boot/热发现/reenable/validate-all 收口写入）。
    /// 只读端点 `GET /api/v1/plugins/contract-status` 消费；未接线 = 空账本
    /// （端点照常返回 manifest 派生的 not_covered 缺省）。
    pub contract_states: Arc<crate::contract::ContractLedger>,
    /// 能力命名空间注册表（provides 声明驱动装配）：内核按**服务角色**
    /// （namespace）调用插件服务的出口——如交互应答 route("human-interaction",
    /// "respond")，内核不点名插件 id/工具名（ADR 2026-08-28 服务角色解析）。
    /// None = 未装配（兼容旧装配/测试，调用点显式降级报错）。
    pub capability_handlers: Option<Arc<agentos_mcp::CapabilityHandlerRegistry>>,
    /// 内核能力契约（config/kernel_capabilities/*.json）——schema 聚合透出用。
    /// None = 未装配（契约目录缺失或测试装配；入口校验由 router 侧独立持有）。
    pub kernel_capability_contracts:
        Option<Arc<Vec<crate::kernel_capabilities::KernelCapabilityContract>>>,
}

impl AppState {
    pub fn new() -> Self {
        Self {
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
            contract_states: Arc::new(crate::contract::ContractLedger::new()),
            capability_handlers: None,
            kernel_capability_contracts: None,
        }
    }

    /// 构建集成了插件系统的 AppState（生产装配入口）。
    ///
    /// 多参签名保证能力面非空：registry / invoker / store / project_root 均为
    /// 必传，chat 端点据 `state` 字段直接构造 [`agentos_engine::PipelineExecutor`]。
    #[allow(clippy::too_many_arguments)]
    pub fn with_plugins(
        manifests: Vec<PluginManifest>,
        registry: Arc<CapabilityRegistryImpl>,
        pipeline_config: Arc<PipelineConfig>,
        step_library: Arc<StepLibrary>,
        invoker: Arc<dyn PluginInvoker>,
        store: Arc<dyn StorageBackend>,
        project_root: PathBuf,
        enabled_plugin_ids: std::collections::HashSet<String>,
    ) -> Self {
        // 注入面字段覆盖基底默认；其余（session/http_handler/metrics/plugin_dirs/
        // config_center 等）经 `..Self::new()` 收敛，加字段只改 [`AppState::new`] 一处。
        Self {
            manifests: Arc::new(RwLock::new(manifests)),
            capability_registry: Some(registry),
            pipeline_config,
            step_library,
            invoker: Some(invoker),
            store: Some(store),
            project_root: Some(project_root),
            enabled_plugin_ids: Arc::new(RwLock::new(enabled_plugin_ids)),
            ..Self::new()
        }
    }

    /// 注入内核能力契约（config/kernel_capabilities/*.json 加载物）：
    /// /api/v1/schema 聚合透出（前端/调用方与入口校验同一真值源）。
    pub fn with_kernel_capability_contracts(
        mut self,
        contracts: Arc<Vec<crate::kernel_capabilities::KernelCapabilityContract>>,
    ) -> Self {
        self.kernel_capability_contracts = Some(contracts);
        self
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

    /// 注入闸2·观测的插件契约状态账本（boot 填健康度后注入）。
    pub fn with_contract_states(
        mut self,
        contract_states: Arc<crate::contract::ContractLedger>,
    ) -> Self {
        self.contract_states = contract_states;
        self
    }

    /// 注入能力命名空间注册表（服务角色解析出口，见字段 doc）。
    pub fn with_capability_handlers(
        mut self,
        registry: Arc<agentos_mcp::CapabilityHandlerRegistry>,
    ) -> Self {
        self.capability_handlers = Some(registry);
        self
    }

    /// 启用会话内核（连接注册表 / 事件总线 / 重放缓冲 + 入站路由）。
    ///
    /// 在 `with_plugins` 后调用，注入 SessionCoordinator 与入站分发器
    /// （EngineDispatcher）。ws_handler 据此承载真实 WS 会话；未调用时
    /// WS 连接在握手前被拒（503，见 server.rs ws_handler）。
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

    /// 注入插件根目录映射（plugin_id → 插件目录绝对路径）。
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
    // 常见媒体扩展名 → content-type（未知回退 octet-stream）。
    // 单一来源：http_dispatcher::mime_for_extension（/ext 静态资源共用）。
    let content_type = crate::http_dispatcher::mime_for_extension(
        file_path.extension().and_then(|e| e.to_str()).unwrap_or(""),
    );
    let mut response = axum::response::Response::new(Body::from(bytes));
    response.headers_mut().insert(
        axum::http::header::CONTENT_TYPE,
        HeaderValue::from_static(content_type),
    );
    if let Ok(cache) = HeaderValue::from_str("public, max-age=31536000, immutable") {
        response
            .headers_mut()
            .insert(axum::http::header::CACHE_CONTROL, cache);
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
        // registry 未装配（测试装配路径）：schema 无工具面（config 树已删，
        // 不再有回退数据源——生产装配必有 registry，缺装配是测试态）。
        Vec::new()
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

    // routes 面：插件 HTTP 端点登记（注册表数据驱动——热发现/热重载/reenable
    // 即时反映；config 树已删，不再有静态 routes 回退）。结构保持对象
    // `{plugin_id: [route…]}`（前端 SchemaResponse.routes 类型为 object，
    // 生产环境原值恒为 {} 占位——现在带真实注册数据，形状不变）。
    // 排序保确定性：注册表是 HashMap，直接序列化会让相同内容产出不同字节，
    // ETag 内容寻址会误变（schema 304 协商失效）；plugin_id 与路由均排序。
    let routes = state
        .capability_registry
        .as_ref()
        .map(|registry| {
            let mut by_plugin: serde_json::Map<String, serde_json::Value> = serde_json::Map::new();
            for r in registry.list_http_routes() {
                by_plugin
                    .entry(r.plugin_id.clone())
                    .or_insert_with(|| serde_json::Value::Array(Vec::new()))
                    .as_array_mut()
                    .expect("just inserted array")
                    .push(serde_json::json!({
                        "route_id": r.endpoint.route_id,
                        "method": r.endpoint.method,
                        "path": r.endpoint.path,
                        "auth": r.endpoint.auth,
                        "handler_capability": r.endpoint.handler_capability,
                        "timeout_ms": r.endpoint.timeout_ms,
                        "max_concurrency": r.endpoint.max_concurrency,
                        "description": r.endpoint.description,
                    }));
            }
            for entry in by_plugin.values_mut() {
                if let Some(arr) = entry.as_array_mut() {
                    arr.sort_by(|a, b| {
                        let ka = format!("{}|{}|{}", a["route_id"], a["method"], a["path"]);
                        let kb = format!("{}|{}|{}", b["route_id"], b["method"], b["path"]);
                        ka.cmp(&kb)
                    });
                }
            }
            serde_json::Value::Object(by_plugin)
        })
        .unwrap_or_else(|| json!({}));

    // P1-4：聚合各插件的 config_files（仅含声明 config_files 的插件）。
    // 前端据此构建"插件 → 多配置子项"配置树（ADR §4.6）。
    // settings:false 条目是注入专用（sidecar 收文件内容），不出口为配置面板。
    let plugin_configs: Vec<serde_json::Value> = state
        .manifests
        .read()
        .await
        .iter()
        .filter_map(|m| {
            let visible: Vec<_> = m
                .config_files
                .iter()
                .filter(|c| c.settings.unwrap_or(true))
                .collect();
            if visible.is_empty() {
                return None;
            }
            Some(json!({
                "plugin_id": m.id,
                "plugin_name": m.name,
                "config_files": visible,
            }))
        })
        .collect();

    // P4/P5：聚合各插件的 contributes（仅含声明 contributes/ui_schema 且 enabled 的插件）。
    // 内核不解释结构，透传给前端 ContributionRegistry（ADR §3.4/§六）。
    // ui_schema 随同一通道出口：agents/pipelines 数组只收 System/Pipeline 类型清单，
    // tool 等其余类型插件的 ui_schema.widgets 只有这里能到达前端。
    // 安装触发模型 L1：disabled 插件的不出口（tools/http 已过滤，UI 也不过来）。
    let enabled_ids = state.enabled_plugin_ids.read().await;
    let plugin_contributes: Vec<serde_json::Value> = state
        .manifests
        .read()
        .await
        .iter()
        .filter(|m| {
            (m.contributes.is_some() || m.ui_schema.is_some()) && enabled_ids.contains(&m.id)
        })
        .map(|m| {
            json!({
                "plugin_id": m.id,
                "plugin_name": m.name,
                "contributes": m.contributes,
                "ui_schema": m.ui_schema,
            })
        })
        .collect();

    // 内核基础设施能力契约透出（Part B：与插件 contributes 同构的聚合位）。
    // 契约结构原样序列化（namespace/description/capabilities[].method +
    // input_schema/output_schema）——前端/调用方与入口校验消费同一份定义。
    let kernel_capabilities: Vec<serde_json::Value> = state
        .kernel_capability_contracts
        .as_ref()
        .map(|contracts| {
            contracts
                .iter()
                .map(|c| serde_json::to_value(c).unwrap_or_default())
                .collect()
        })
        .unwrap_or_default();

    SchemaResponse {
        agents,
        pipelines,
        tools,
        routes,
        plugin_configs,
        plugin_contributes,
        kernel_capabilities,
    }
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
/// （sidecar 汇总写入，可为空）。无消息槽的 run 被过滤——run→pipeline 映射
/// 依赖 message_slots，无槽记录无法归属管道。
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

// ── pending 输入队列端点（ADR-2026-08-26）─────────────────────────
// 等待窗口内（入队→激活）的管道消息可经此处查询/修改/删除/清空。
// 全部按租户隔离；不存在条目返回 404（显示性错误，不静默）。

/// GET /api/v1/pipelines/{pipeline_id}/pending-inputs——队列列表（FIFO 序）。
pub async fn pending_inputs_list_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path(pipeline_id): axum::extract::Path<String>,
    headers: axum::http::HeaderMap,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let store = state.store.as_ref().ok_or_else(|| ApiError::NotFound {
        message: "store not injected".to_string(),
    })?;
    let tenant_ctx = crate::server::request_tenant_ctx(state.store.as_ref(), &headers, "").await;
    let rows = store
        .list_pending_inputs(&tenant_ctx.tenant_id, &pipeline_id)
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("pending-inputs 查询失败: {e}"),
        })?;
    let items: Vec<serde_json::Value> = rows
        .into_iter()
        .map(|r| {
            serde_json::json!({
                "id": r.id,
                "pipeline_id": r.pipeline_id,
                "content": r.content,
                "source": r.source,
                "created_at": r.created_at,
            })
        })
        .collect();
    Ok(axum::Json(json!({ "items": items })))
}

/// PUT /api/v1/pipelines/{pipeline_id}/pending-inputs/{input_id}——修改 content。
pub async fn pending_inputs_update_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path((pipeline_id, input_id)): axum::extract::Path<(String, String)>,
    headers: axum::http::HeaderMap,
    body: axum::extract::Json<serde_json::Value>,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let store = state.store.as_ref().ok_or_else(|| ApiError::NotFound {
        message: "store not injected".to_string(),
    })?;
    let content = body
        .get("content")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| ApiError::BadRequest {
            message: "content 必须为非空字符串".to_string(),
        })?;
    let tenant_ctx = crate::server::request_tenant_ctx(state.store.as_ref(), &headers, "").await;
    let updated = store
        .update_pending_input_content(&tenant_ctx.tenant_id, &pipeline_id, &input_id, content)
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("pending-inputs 修改失败: {e}"),
        })?;
    if !updated {
        return Err(ApiError::NotFound {
            message: format!("pending-inputs 条目不存在: {input_id}"),
        });
    }
    emit_pending_inputs_changed_endpoint(&state, &pipeline_id, &tenant_ctx.tenant_id, "updated")
        .await;
    Ok(axum::Json(json!({ "status": "updated" })))
}

/// DELETE /api/v1/pipelines/{pipeline_id}/pending-inputs/{input_id}——删除单条。
pub async fn pending_inputs_delete_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path((pipeline_id, input_id)): axum::extract::Path<(String, String)>,
    headers: axum::http::HeaderMap,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let store = state.store.as_ref().ok_or_else(|| ApiError::NotFound {
        message: "store not injected".to_string(),
    })?;
    let tenant_ctx = crate::server::request_tenant_ctx(state.store.as_ref(), &headers, "").await;
    // 删除前读取条目 cmid：排队中的 REST chat 请求（http_ 前缀 cmid）据此收到
    // 失败 outcome 解除挂起；条目已被消费循环 pop 时 list 查不到，不通知。
    // fail-closed：列表读取失败 = 无法识别待通知 cmid → 跳过删除并报 503，
    // 绝不带病删除（那会丢 cmid 让 waiter 挂死至超时）；条目保留则消费循环
    // 稍后仍能正常 pop 执行并回传 outcome。
    let records = store
        .list_pending_inputs(&tenant_ctx.tenant_id, &pipeline_id)
        .await
        .map_err(|e| {
            tracing::warn!(
                pipeline = %pipeline_id,
                error = %e,
                "pending-inputs 删除前置列表读取失败，跳过删除（防 REST chat waiter 挂死）"
            );
            ApiError::ServiceUnavailable {
                message: "pending-inputs 暂时不可用（存储异常），请稍后重试".to_string(),
            }
        })?;
    let cmid = records
        .into_iter()
        .find(|r| r.id == input_id)
        .map(|r| r.client_message_id);
    let deleted = store
        .delete_pending_input(&tenant_ctx.tenant_id, &pipeline_id, &input_id)
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("pending-inputs 删除失败: {e}"),
        })?;
    if !deleted {
        return Err(ApiError::NotFound {
            message: format!("pending-inputs 条目不存在: {input_id}"),
        });
    }
    if let Some(cmid) = cmid {
        crate::ws_session::notify_outcome_waiter(
            &cmid,
            crate::server::EngineOutcome {
                content: format!("pending-inputs 条目 {input_id} 已在排队中被删除"),
                final_assistant: None,
                failed: true,
                degraded: false,
                plugin_errors: Vec::new(),
            },
        );
    }
    emit_pending_inputs_changed_endpoint(&state, &pipeline_id, &tenant_ctx.tenant_id, "deleted")
        .await;
    Ok(axum::Json(json!({ "status": "deleted" })))
}

/// DELETE /api/v1/pipelines/{pipeline_id}/pending-inputs——清空队列。
pub async fn pending_inputs_clear_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path(pipeline_id): axum::extract::Path<String>,
    headers: axum::http::HeaderMap,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let store = state.store.as_ref().ok_or_else(|| ApiError::NotFound {
        message: "store not injected".to_string(),
    })?;
    let tenant_ctx = crate::server::request_tenant_ctx(state.store.as_ref(), &headers, "").await;
    // 清空前读取全部 cmid（同 delete：解除排队中 REST chat 请求的挂起）。
    // fail-closed 同删除分支：读取失败跳过清空并报 503，不丢 cmid 挂死 waiter。
    let cmids: Vec<String> = store
        .list_pending_inputs(&tenant_ctx.tenant_id, &pipeline_id)
        .await
        .map_err(|e| {
            tracing::warn!(
                pipeline = %pipeline_id,
                error = %e,
                "pending-inputs 清空前列表读取失败，跳过清空（防 REST chat waiter 挂死）"
            );
            ApiError::ServiceUnavailable {
                message: "pending-inputs 暂时不可用（存储异常），请稍后重试".to_string(),
            }
        })?
        .into_iter()
        .map(|r| r.client_message_id)
        .collect();
    let deleted = store
        .clear_pending_inputs(&tenant_ctx.tenant_id, &pipeline_id)
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("pending-inputs 清空失败: {e}"),
        })?;
    for cmid in cmids {
        crate::ws_session::notify_outcome_waiter(
            &cmid,
            crate::server::EngineOutcome {
                content: format!("pending-inputs 队列已清空（{pipeline_id}）"),
                final_assistant: None,
                failed: true,
                degraded: false,
                plugin_errors: Vec::new(),
            },
        );
    }
    emit_pending_inputs_changed_endpoint(&state, &pipeline_id, &tenant_ctx.tenant_id, "cleared")
        .await;
    Ok(axum::Json(
        json!({ "status": "cleared", "deleted": deleted }),
    ))
}

/// 端点变更后的 WS 事件推送（PUT/DELETE/clear 共用）：反射到该管道的会话
/// thread 单播 `pending_inputs_changed`（与 ws_session.rs 同款 payload）。
/// 会话未接线时静默跳过。
async fn emit_pending_inputs_changed_endpoint(
    state: &AppState,
    pipeline_id: &str,
    tenant_id: &str,
    action: &str,
) {
    let Some(session) = state.session.as_ref() else {
        return;
    };
    let Some(store) = state.store.as_ref() else {
        return;
    };
    let items = match store.list_pending_inputs(tenant_id, pipeline_id).await {
        Ok(rows) => rows
            .into_iter()
            .map(|r| {
                serde_json::json!({
                    "id": r.id,
                    "pipeline_id": r.pipeline_id,
                    "content": r.content,
                    "source": r.source,
                    "created_at": r.created_at,
                })
            })
            .collect::<Vec<_>>(),
        Err(e) => {
            tracing::warn!(
                pipeline = %pipeline_id,
                error = %e,
                "pending_inputs_changed 列表读取失败（事件跳过）"
            );
            Vec::new()
        }
    };
    // thread 坐标：pipeline_sessions 反查（无会话则跳过——端点变更无坐标可推）。
    // 存储故障（Err）与"无会话"（None）分开处理：Err 静默丢弃会让事件坐标
    // 反查失败无从排查。
    let thread_id = match store.get_thread_id_by_pipeline(pipeline_id).await {
        Ok(t) => t,
        Err(e) => {
            tracing::warn!(
                pipeline = %pipeline_id,
                error = %e,
                "pending_inputs_changed thread 坐标反查失败（事件跳过）"
            );
            return;
        }
    };
    let Some(thread_id) = thread_id else {
        return;
    };
    let _ = session
        .emit_event(
            &thread_id,
            "pending_inputs_changed",
            serde_json::json!({
                "pipeline_id": pipeline_id,
                "thread_id": thread_id,
                "action": action,
                "items": items,
            }),
        )
        .await;
}
/// 内核运行域结构基线：内核写或引擎写的自有键（执行坐标/引擎循环/引擎输出
/// 投影），随摘要直接出口。插件域字段（task.*/track.*/evaluation.*/workspace
/// 等）不在此列——由写入方插件经 manifest `export_fields` 声明出口（见
/// [`ExportFields`]），未声明 = 不出口（默认拒绝）。agent_id 亦不在基线：
/// agent 是管道插件的服务者（执行上下文键仅作派发路由坐标），行级 agent_id
/// 由 registry listing 注入，state 出口随消费需要由插件声明。
const STATE_BASELINE_KEYS: &[&str] = &[
    "current_phase",
    "ended",
    "session_id",
    "thread_id",
    "pipeline_id",
    "max_iterations",
    "ckpt_max_seq",
    "suspended",
    "metadata",
    "input",
    "raw_result",
    "raw_error",
];

/// 插件出口声明（各 manifest `export_fields` 的并集）：精确键 + `前缀.*`
/// 通配前缀。两个消费面（GET /pipelines/state 与 pipeline-state.list 能力）
/// 共用——插件新增出口字段改自己的 plugin.json，内核零改动；热重载刷新
/// manifest 集合后下一请求即生效。
#[derive(Default, Clone)]
pub struct ExportFields {
    exact: std::collections::HashSet<String>,
    prefixes: Vec<String>,
}

impl ExportFields {
    /// 从 manifest 集合收集 export_fields 声明并集。
    pub fn from_manifests<'a>(
        manifests: impl IntoIterator<Item = &'a agentos_core::traits::PluginManifest>,
    ) -> Self {
        let mut out = Self::default();
        for m in manifests {
            for f in &m.export_fields {
                if let Some(prefix) = f.strip_suffix(".*") {
                    out.prefixes.push(prefix.to_string());
                } else {
                    out.exact.insert(f.clone());
                }
            }
        }
        out
    }
}

/// 从一份管道 state 提取摘要（内核基线 + 插件声明出口 + messages 条数）。
pub(crate) fn summarize_state(
    state: &serde_json::Value,
    export: &ExportFields,
) -> serde_json::Value {
    let mut out = serde_json::Map::new();
    if let Some(obj) = state.as_object() {
        for k in STATE_BASELINE_KEYS {
            if let Some(v) = obj.get(*k) {
                out.insert(k.to_string(), v.clone());
            }
        }
        for k in &export.exact {
            if let Some(v) = obj.get(k.as_str()) {
                out.insert(k.clone(), v.clone());
            }
        }
        // 动态前缀键（如 task.owned.<id>.<field>）整段出口
        for (k, v) in obj {
            if export.prefixes.iter().any(|p| k.starts_with(p.as_str())) {
                out.insert(k.clone(), v.clone());
            }
        }
        // messages 只出口条数（迭代/轮次规模），不出口全文
        if let Some(msgs) = obj.get("messages").and_then(|v| v.as_array()) {
            out.insert("message_count".to_string(), json!(msgs.len()));
        }
    }
    serde_json::Value::Object(out)
}

/// 冷读兜底：取最新 checkpoint，并把 `pipeline_state` 表最新标量覆盖上去。
///
/// 顺序对齐 `stage_recover_history` 的冷恢复（checkpoint 标量 → pipeline_state 表
/// 补充，表的最新值覆盖 checkpoint 里的出生/过期值，如 `task.status` pending →
/// completed）。registry 未命中（重启后未再轮）时 `/pipelines/state` 与
/// `pipeline-state.list` 的 DB 兜底共用；无 checkpoint 返回 None。读取失败
/// 同样返回 None（任务树读面降级不崩，调用方跳过该行），但两类失败各留
/// warn 痕迹——与「确实无档」的 Ok(None) 可区分，静默缺行可从日志定位。
///
/// 无 checkpoint 但 `pipeline_state` 表有行时以表行为基线：running 中任务
/// interval 未到不会有 checkpoint，整行丢弃会看不到刚提交的任务（出生字段
/// 创建即落表，见 chat_send_handler 创建分支）。
pub(crate) async fn cold_state_row(
    store: &std::sync::Arc<dyn agentos_core::traits::StorageBackend>,
    pipeline_id: &str,
    tenant_id: &str,
) -> Option<serde_json::Value> {
    let ckpt = match store.load_latest_checkpoint(pipeline_id, tenant_id).await {
        Ok(Some((_step, c))) => Some(c),
        Ok(None) => None,
        Err(e) => {
            tracing::warn!(
                pipeline_id = %pipeline_id,
                error = %e,
                "load_latest_checkpoint 冷读失败，按无 checkpoint 处理（仅表行基线）"
            );
            None
        }
    };
    let fields = match store.load_pipeline_state(pipeline_id, tenant_id).await {
        Ok(fields) => fields,
        Err(e) => {
            tracing::warn!(
                pipeline_id = %pipeline_id,
                error = %e,
                "load_pipeline_state 冷读失败，跳过该行"
            );
            return None;
        }
    };
    let mut merged = ckpt.unwrap_or_else(|| serde_json::json!({}));
    if let Some(obj) = merged.as_object_mut() {
        for (k, v) in fields {
            obj.insert(k, v);
        }
    }
    // checkpoint 与表行双空 = 真孤儿（无任何持久痕迹），不出口
    if merged.as_object().is_none_or(|o| o.is_empty()) {
        return None;
    }
    Some(merged)
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

    // 出口声明按当前 manifest 集合实时收集（热重载刷新后下一请求即生效）
    let export = ExportFields::from_manifests(state.manifests.read().await.iter());

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
            summarize_state(&e.state, &export)
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
            // runs 清单每管道可有多个 run——首个命中后登记去重（防止同管道按
            // run 数重复出口）。
            seen.insert(pid.clone());
            // 冷兜底行 = 最新 checkpoint + pipeline_state 表最新标量覆盖（复用
            // cold_state_row：checkpoint 拍在终态回写前 → task.status 等完成态以
            // pipeline_state 表为准，重启后不再倒退回 pending）。
            let store: std::sync::Arc<dyn agentos_core::traits::StorageBackend> =
                state.db.as_ref().expect("db checked above").clone();
            let summary = match cold_state_row(&store, &pid, &tenant_id).await {
                Some(st) => summarize_state(&st, &export),
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
/// 从 CapabilityRegistry 返回已注册的工具列表；registry 未装配时返回空列表
/// （config 树已删——生产装配必有 registry，空面是测试装配态的真实反映）。
/// 响应信封统一为 `{ "items": [...], "total": n }`
/// （消费方为插件管理页能力浏览/调试数据面）。
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
        // registry 未装配（测试装配路径）→ 空工具面，响应信封仍为 {items, total}。
        Vec::new()
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
fn masked_env_fields(mapping: &ConfigFileMapping, env_path: &std::path::Path) -> serde_json::Value {
    let text = std::fs::read_to_string(env_path).unwrap_or_default();
    let from_file = agentos_mcp::env_file::parse_env_text_for_read(&text);
    let mut out = serde_json::Map::new();
    for f in &mapping.fields {
        let set = std::env::var(&f.name).ok().is_some() || from_file.contains_key(&f.name);
        out.insert(
            f.name.clone(),
            serde_json::Value::String(if set {
                "***".to_string()
            } else {
                String::new()
            }),
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
        let current_etag = compute_etag(
            serde_json::to_string(&current)
                .unwrap_or_default()
                .as_bytes(),
        );
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
        let declared: std::collections::HashSet<&str> =
            mapping.fields.iter().map(|f| f.name.as_str()).collect();
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
        let new_etag = compute_etag(serde_json::to_string(&data).unwrap_or_default().as_bytes());
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
// 现存端点：GET /api/v1/plugins（状态清单）与
// PUT /api/v1/plugins/{id}/enabled（启停，写 default_profile.yaml + 热更新）。

/// G8 排空 + 自退出共享实现（`system_restart_handler` 与 plugin_watcher 的
/// cdylib 变更自动重启共用，watcher 经注入的回调调用，不 import axum handler）。
///
/// 流程：排空（在途 `running` runs → `suspended`，重启后 resume 续跑）→ 记日志
/// → 延迟 200ms 退出（让触发方的响应/日志先送达）→ **exit 前 best-effort 调
/// `invoker.shutdown_all()` 杀掉全部缓存 sidecar**（0.2 收尾 §3.3a：重启换进程
/// 后旧 sidecar 成孤儿，e2e G4；带 2s 总预算，不阻塞退出超过秒级）。退出码
/// **75** = "restart requested"，监督者（启动脚本循环 / Service 重启策略）据码
/// 拉起新进程。
///
/// 测试逃生门：设 `AGENTOS_DISABLE_SELF_EXIT=1` 时只排空不退出（嵌入/测试场景，
/// 也不杀 sidecar——进程不退出，懒 spawn 的 sidecar 交由 idle GC 管理）。
///
/// 返回被排空的 run 数（触发方记入日志/响应）。
pub async fn drain_and_exit75(
    db: Option<&Arc<agentos_engine::SqliteStore>>,
    invoker: Option<Arc<dyn PluginInvoker>>,
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
        tokio::spawn(async move {
            // 让触发方的响应/日志先 flush 再清理退出。
            tokio::time::sleep(std::time::Duration::from_millis(200)).await;
            // §3.3a：exit 前 best-effort 杀全部缓存 sidecar（总预算 2s，
            // invoker 内部逐 kill 另有各自超时——重启不留孤儿，也不被卡死
            // 的 kill 拖住退出）。
            if let Some(invoker) = invoker.as_ref() {
                let _ =
                    tokio::time::timeout(std::time::Duration::from_secs(2), invoker.shutdown_all())
                        .await;
            }
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
    let suspended_runs = drain_and_exit75(
        state.db.as_ref(),
        state.invoker.clone(),
        "POST /api/v1/system/restart",
    )
    .await;
    axum::Json(json!({
        "success": true,
        "message": "内核排空完成，即将退出（exit 75）；监督者将重启进程",
        "exit_code": 75,
        "suspended_runs": suspended_runs,
    }))
}

/// 读磁盘 plugin.json 并与注册表 manifest 做工具集/schema 差异比对
/// （ADR 2026-08-28 决策3，[`crate::contract::registry_disk_diffs`] 纯函数消费）。
/// 返回（`Some(diffs)` = 已检出（空 vec = 一致），`None` = 磁盘不可读）+
/// 不可读原因。磁盘不可读不是"一致"——不可静默当绿。
fn read_disk_manifest_diffs(
    state: &AppState,
    m: &PluginManifest,
) -> (
    Option<Vec<crate::contract::RegistryDiskDiff>>,
    Option<String>,
) {
    let Some(root) = state.plugin_dirs.get(&m.id) else {
        return (
            None,
            Some("无插件目录映射（plugin_dirs 未登记），无法读取磁盘 manifest".to_string()),
        );
    };
    let raw = match std::fs::read_to_string(root.join("plugin.json")) {
        Ok(r) => r,
        Err(e) => return (None, Some(format!("磁盘 plugin.json 读取失败: {e}"))),
    };
    let disk: PluginManifest = match serde_json::from_str(&raw) {
        Ok(d) => d,
        Err(e) => return (None, Some(format!("磁盘 plugin.json 解析失败: {e}"))),
    };
    (Some(crate::contract::registry_disk_diffs(m, &disk)), None)
}

/// POST /api/v1/plugins/validate-all — G2 双写一致性全量巡检。
///
/// 对照每个 tool 插件的 manifest 声明（`capabilities.tools`）与 sidecar 实际上报
/// （MCP `tools/list`）——工具名集合 + 参数 schema。漂移分类：
/// `missing`（声明有实际无）/ `undeclared`（实际有声明无）/ `schema_mismatch`。
///
/// 语义：spawn → 校验 → 回收（新 spawn 的连接校验后 kill，不破坏懒加载）；
/// 校验失败（spawn 失败 / host 不支持）不阻断，插件标记 `error` 并继续。
/// 结果报告 + **写闸2·观测账本**（`state.contract_states`，处置=安装路径的
/// plugin_watcher 拒绝注册已做；此处为人工巡检的收口，见契约闸门方案 Phase2）。
/// 另做注册表 manifest ↔ 磁盘 manifest 一致性检出（ADR 2026-08-28 决策3）：
/// 差异为独立 `consistency_reports` 报告项 + 账本留痕。
pub async fn validate_all_plugins_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<serde_json::Value> {
    let Some(invoker) = state.invoker.clone() else {
        return axum::Json(json!({
            "checked": 0, "clean": 0, "drifted": 0, "errors": 1,
            "registry_disk_mismatches": 0,
            "message": "invoker 未接线（validate-all 不可用）",
            "reports": [],
            "consistency_reports": [],
        }));
    };
    let enabled_ids = state.enabled_plugin_ids.read().await.clone();
    let ledger = state.contract_states.clone();
    let mut reports: Vec<serde_json::Value> = Vec::new();
    let mut consistency_reports: Vec<serde_json::Value> = Vec::new();
    let mut clean = 0usize;
    let mut drifted = 0usize;
    let mut errors = 0usize;
    let mut registry_disk_mismatches = 0usize;
    for m in state.manifests.read().await.iter() {
        let enabled = enabled_ids.contains(&m.id);
        // ADR 决策3：注册表 manifest ↔ 磁盘 manifest 一致性检出——净化/热改
        // 导致的注册表静默降级机检，差异为独立报告项 + 账本留痕。
        let (disk_diffs, disk_unreadable) = read_disk_manifest_diffs(&state, m);
        match (&disk_diffs, &disk_unreadable) {
            (Some(diffs), _) if diffs.is_empty() => {
                ledger.record_registry_disk_diffs(&m.id, Vec::new());
            }
            (Some(diffs), _) => {
                registry_disk_mismatches += 1;
                ledger.record_registry_disk_diffs(&m.id, diffs.clone());
                consistency_reports.push(json!({
                    "plugin_id": m.id,
                    "status": "registry_disk_mismatch",
                    "diffs": diffs,
                }));
            }
            (None, Some(reason)) => {
                consistency_reports.push(json!({
                    "plugin_id": m.id,
                    "status": "disk_manifest_unreadable",
                    "reason": reason,
                }));
            }
            (None, None) => {}
        }
        if m.capabilities.tools.is_empty() {
            // 非 tool 插件无工具可对照：登记 not_covered 缺省（诚实标未覆盖）
            ledger.upsert(crate::contract::PluginContractState::not_covered(
                m, enabled,
            ));
            continue;
        }
        if m.host_type != agentos_core::traits::HostType::Sidecar {
            reports.push(json!({
                "plugin_id": m.id,
                "status": "skipped",
                "reason": format!("host_type {:?} 暂无 describe 通道（G2 渐进落地）", m.host_type),
                "mismatches": [],
            }));
            ledger.upsert(crate::contract::PluginContractState::not_covered(
                m, enabled,
            ));
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
                // 写健康度：被拒工具 = missing/schema_mismatch（undeclared 仅记录不拒）
                let rejected: Vec<String> = mismatches
                    .iter()
                    .filter_map(|mm| match mm {
                        agentos_invoker::verify::VerifyMismatch::Missing { name } => {
                            Some(name.clone())
                        }
                        agentos_invoker::verify::VerifyMismatch::SchemaMismatch {
                            name, ..
                        } => Some(name.clone()),
                        agentos_invoker::verify::VerifyMismatch::Undeclared { .. } => None,
                    })
                    .collect();
                let g2o = crate::plugin_watcher::G2VerifyOutcome {
                    manifest: m.clone(),
                    rejected_tools: rejected,
                    drift: !mismatches.is_empty(),
                    spawn_failed: false,
                    smoke_failed: false,
                };
                ledger.upsert(crate::contract::PluginContractState::derived(
                    m,
                    enabled,
                    Some(&g2o),
                ));
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
                // 写健康度：spawn/上报不可用 = not_covered（不是验出漂移），记 reason
                let mut st = crate::contract::PluginContractState::not_covered(m, enabled);
                st.gates.last_error = Some(e.message.clone());
                ledger.upsert(st);
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
        "registry_disk_mismatches": registry_disk_mismatches,
        "reports": reports,
        "consistency_reports": consistency_reports,
    }))
}

/// GET /api/v1/plugins/contract-status — 闸2·观测：每插件契约状态（只读）。
///
/// 只读账本（boot/热发现/reenable/validate-all 写入，`state.contract_states`）；
/// 请求时不重跑校验（"结果前置复用"，契约闸门方案 §1.7）。未登记插件补
/// `not_covered` 缺省（诚实标注未覆盖，不假装绿）。响应 `{ plugins, ... }`
/// 信封，前端 `parseContractStatus` 兼容。
pub async fn plugins_contract_status_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<serde_json::Value> {
    let enabled_ids = state.enabled_plugin_ids.read().await.clone();
    let manifests = state.manifests.read().await.clone();
    let items =
        crate::contract::contract_statuses(&state.contract_states, &manifests, &enabled_ids);
    axum::Json(json!({
        "plugins": items,
        "count": items.len(),
        "generated_at": crate::contract::now_ms(),
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
                "description": m.description,
                "config_type": config_type,
                "host_type": host_type,
                "version": m.version,
                "enabled": enabled,
                "activation": activation,
                "status": run_status,
                "config_files": m.config_files.iter().filter(|c| c.settings.unwrap_or(true)).map(|c| json!({
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

    let mut doc = load_profile_doc(&profile_path)?;
    apply_enabled_patch(&mut doc, &plugin_id, new_enabled);

    // 写回：序列化失败直接报错（K1：不得 unwrap_or_default() 把空串写盘，
    // 物理清空整个 profile——序列化对 Mapping 几乎不会失败，但兜底不得是破坏性写）。
    let new_raw = serde_yaml::to_string(&doc).map_err(|e| ApiError::Internal {
        message: format!("序列化 default_profile.yaml 失败：{e}"),
    })?;
    // A12：写盘失败 → 5xx 统一错误信封（不再 200 + success:false 混装，
    // 前端无法据状态码区分"已生效"与"根本没写进去"）。
    std::fs::write(&profile_path, new_raw).map_err(|e| {
        tracing::error!(
            target: "plugin-enablement",
            plugin_id = %plugin_id,
            error = %e,
            "写入 profile 失败"
        );
        ApiError::Internal {
            message: format!("写入 profile 失败: {e}"),
        }
    })?;

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
        if new_enabled {
            registered = reenable_hot_path(&state, registry, &plugin_id).await;
        } else {
            disable_hot_path(&state, registry, &plugin_id).await;
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

/// 读 default_profile.yaml 为 serde_yaml 文档。
///
/// 文件缺失/空白 → 全新 Mapping（首次落盘，无存量可破坏）；文件存在但解析
/// 失败/顶层非 Mapping → 422 拒绝写入（K1：不得用硬编码模板顶替并覆写，
/// profile 里其他插件的启停设置会被物理清空；损坏现场必须保留给运维排查，
/// 不得静默重建）。其余读失败（权限等）→ 500，同样不写。
fn load_profile_doc(profile_path: &std::path::Path) -> Result<serde_yaml::Value, ApiError> {
    match std::fs::read_to_string(profile_path) {
        Ok(raw) if raw.trim().is_empty() => {
            Ok(serde_yaml::Value::Mapping(serde_yaml::Mapping::new()))
        }
        Ok(raw) => match serde_yaml::from_str::<serde_yaml::Value>(&raw) {
            Ok(v @ serde_yaml::Value::Mapping(_)) => Ok(v),
            Ok(_) => {
                tracing::error!(
                    path = %profile_path.display(),
                    "default_profile.yaml 顶层非 Mapping，拒绝覆写（profile corrupted）"
                );
                Err(ApiError::UnprocessableEntity {
                    message: "profile corrupted, refusing to overwrite".to_string(),
                })
            }
            Err(e) => {
                tracing::error!(
                    path = %profile_path.display(),
                    error = %e,
                    "default_profile.yaml 解析失败，拒绝覆写（profile corrupted）"
                );
                Err(ApiError::UnprocessableEntity {
                    message: "profile corrupted, refusing to overwrite".to_string(),
                })
            }
        },
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            Ok(serde_yaml::Value::Mapping(serde_yaml::Mapping::new()))
        }
        Err(e) => Err(ApiError::Internal {
            message: format!("读取 default_profile.yaml 失败：{e}"),
        }),
    }
}

/// 在 profile 文档 `plugins.<plugin_id>.enabled` 处打启用补丁。
///
/// plugins 键 / 插件条目不存在则逐级创建（手动操作 serde_yaml Mapping）。
fn apply_enabled_patch(doc: &mut serde_yaml::Value, plugin_id: &str, new_enabled: bool) {
    if let serde_yaml::Value::Mapping(ref mut top) = *doc {
        // 确保 plugins 键存在且是 Mapping
        let plugins_key = serde_yaml::Value::String("plugins".into());
        if !top.contains_key(&plugins_key) {
            top.insert(
                plugins_key.clone(),
                serde_yaml::Value::Mapping(serde_yaml::Mapping::new()),
            );
        }
        if let Some(serde_yaml::Value::Mapping(ref mut plugins_map)) = top.get_mut(&plugins_key) {
            let pid_key = serde_yaml::Value::String(plugin_id.to_owned());
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
}

/// 启用热路径：注册闸 G2 复核后按（净化后）manifest 重注册 tools/http_routes。
///
/// 判定失败（漂移）→ 用净化后 manifest 注册，禁止把"声明与实现不服"的能力在
/// 启用时带进来；观测失败（重试后仍 spawn/list 失败）≠ 判定失败 → 按声明注册，
/// 账本标记校验未完成。返回 registered 账目 `{tools, http_routes}`
/// （manifest 未找到 → null 并告警，不重注册）。
async fn reenable_hot_path(
    state: &AppState,
    registry: &Arc<CapabilityRegistryImpl>,
    plugin_id: &str,
) -> serde_json::Value {
    let mut registered = serde_json::Value::Null;
    match state
        .manifests
        .read()
        .await
        .iter()
        .find(|m| m.id == plugin_id)
    {
        Some(m) => {
            let mut manifest_for_register = m.clone();
            let mut g2_outcome: Option<crate::plugin_watcher::G2VerifyOutcome> = None;
            // G2 适用闸（与 boot g2_applicable 同判）：无 tools+services 的 manifest
            // 无可验面——不跑复核。净化后 0 工具 manifest 的"复核通过"是假绿，会经
            // 复验清除口销毁 drift/sanitized 证据；此处不写 g2 结果，账本 upsert 走
            // not_covered 弱信号（既有证据粘滞保留，ADR 2026-08-28 决策1）。
            if !m.capabilities.tools.is_empty() || !m.capabilities.services.is_empty() {
                if let Some(invoker) = &state.invoker {
                    let outcome =
                        crate::plugin_watcher::g2_verify_and_sanitize(invoker.as_ref(), m.clone())
                            .await;
                    g2_outcome = Some(outcome.clone());
                    if outcome.drift {
                        tracing::warn!(
                            target: "plugin-enablement",
                            plugin = %plugin_id,
                            rejected = ?outcome.rejected_tools,
                            spawn_failed = outcome.spawn_failed,
                            "注册闸 G2：启用复核判定声明与实现不一致，按净化后能力注册（需修改插件）"
                        );
                        manifest_for_register = outcome.manifest;
                    } else if outcome.spawn_failed {
                        tracing::warn!(
                            target: "plugin-enablement",
                            plugin = %plugin_id,
                            "注册闸 G2：启用复核观测失败——按声明注册，账本标记校验未完成（待复验）"
                        );
                    }
                }
            }
            // 闸2·观测：启用复核结果收口（无 invoker = not_covered 缺省）
            state
                .contract_states
                .upsert(crate::contract::PluginContractState::derived(
                    m,
                    true,
                    g2_outcome.as_ref(),
                ));
            let (tools, http_routes) = crate::plugin_lifecycle::reenable_plugin_capabilities(
                &manifest_for_register,
                registry,
                &state.plugin_scopes,
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
    registered
}

/// 禁用热路径：M1 scope 收回全部登记（registry 四维 + broadcaster 绑定）+
/// clear_plugin 兜底（scope 无登记的直连注册路径仍被覆盖）+ sidecar 窄口击杀。
///
/// 闸2·观测：禁用收口登记 not_covered（账本旧值作废）。sidecar 走
/// kill_sidecar_if_any（只 kill 进程 + 移除缓存，不走 force_unload 的 OnUnload
/// 广播/loader.unload/指纹清理——"仅禁用"语义下插件仍在 loader 内、热发现不
/// 失效）；sidecar 按调用懒 spawn，reenable 后下次调用自然重生。
async fn disable_hot_path(
    state: &AppState,
    registry: &Arc<CapabilityRegistryImpl>,
    plugin_id: &str,
) {
    use agentos_core::traits::CapabilityRegistry;

    if let Some(m) = state
        .manifests
        .read()
        .await
        .iter()
        .find(|m| m.id == plugin_id)
    {
        state
            .contract_states
            .upsert(crate::contract::PluginContractState::not_covered(m, false));
    }
    state.plugin_scopes.revoke(plugin_id);
    if let Some(bindings) = &state.widget_bindings {
        remove_plugin_bindings(bindings, plugin_id);
    }
    registry.clear_plugin(plugin_id);
    // G3：动态注册随 scope/clear_plugin 结构性收回（动态注册是
    // state 域数据不落内核，re-enable 后插件经 on_load/运行时自行重建）。
    if let Some(invoker) = state.invoker.as_ref() {
        invoker.kill_sidecar_if_any(plugin_id).await;
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
    // G10 文件 DSL 结构校验：与启动加载同一条 PipelineFile 解析路径——旧形态键
    // （routes/exit_routes/体级 loop_config）、死形态（then:{next,set}/wait）、
    // 未知转移目标在写盘前拒绝；否则保存无告警，重启加载失败静默降级空管道。
    crate::pipeline_loader::validate_pipeline_config_data(&req.data).map_err(|e| {
        ApiError::BadRequest {
            message: format!("pipeline config validation failed: {e}"),
        }
    })?;
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
    //! 声明化出口（ADR 2026-08-28）：内核运行域基线 + manifest `export_fields`
    //! 声明并集。插件域字段（task.*/lineage.*/workspace/evaluation.* 等）经写入方
    //! 插件声明出口；未声明 = 不出口（默认拒绝），基线键恒出口。

    use super::*;

    /// 构造带指定 export_fields 声明的测试 manifest。
    fn export_manifest(fields: &[&str]) -> PluginManifest {
        PluginManifest {
            id: "test_plugin".to_string(),
            name: "test_plugin".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::System,
            pipeline_role: None,
            language: "python".to_string(),
            host_type: agentos_core::traits::HostType::Sidecar,
            host_group: None,
            entry: "python server.py".to_string(),
            capabilities: agentos_core::traits::ManifestCapabilities::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            granted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            persistent_fields: vec![],
            export_fields: fields.iter().map(|s| s.to_string()).collect(),
            provides: None,
        }
    }

    #[test]
    fn test_summarize_exports_declared_task_and_lineage_fields() {
        // 任务域/血缘键由写入方插件声明后出口（原内核白名单语义迁入声明）
        let export = ExportFields::from_manifests(&[export_manifest(&[
            "task.goal",
            "task.status",
            "task.id",
            "lineage.parent_pipeline_id",
            "lineage.origin_session_id",
            "lineage.root",
        ])]);
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
        let s = summarize_state(&state, &export);
        assert_eq!(s["pipeline_id"], "p1", "基线键恒出口");
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
    fn test_summarize_default_denies_undeclared_plugin_fields() {
        // 默认拒绝性质：无任何声明（ExportFields::default）时插件域字段一律裁掉，
        // 基线键不受影响——插件声明是出口的唯一扩权通道。
        let export = ExportFields::default();
        let s = summarize_state(
            &json!({
                "pipeline_id": "p2",
                "ended": true,
                "task.goal": "g",
                "lineage.root": true,
                "workspace": "D:/ws",
            }),
            &export,
        );
        assert_eq!(s["pipeline_id"], "p2");
        assert_eq!(s["ended"], true);
        for k in [
            "task.goal",
            "lineage.root",
            "workspace",
            "task.owned.x.title",
        ] {
            assert!(s.get(k).is_none(), "未声明键 {k} 不应出口");
        }
    }

    #[test]
    fn test_summarize_exports_task_owned_prefix_and_workspace() {
        // task.owned.<id>.<field>（提交者管道自持的任务登记，键含动态管道 id）
        // 经 `task.owned.*` 前缀声明整段出口；workspace/ws_meta 精确键声明出口。
        let export = ExportFields::from_manifests(&[export_manifest(&[
            "task.owned.*",
            "task.submitted_by",
            "workspace",
            "ws_meta",
        ])]);
        let state = json!({
            "pipeline_id": "p3",
            "task.owned.ae7b430f.title": "AI行业近月发展调研",
            "task.owned.ae7b430f.status": "running",
            "task.submitted_by": "u1",
            "workspace": "D:/ws/copy_1",
            "ws_meta": {"path": "D:/ws/copy_1", "mode": "worktree"},
            "messages": [{"role": "user"}],
        });
        let s = summarize_state(&state, &export);
        assert_eq!(s["task.owned.ae7b430f.title"], "AI行业近月发展调研");
        assert_eq!(s["task.owned.ae7b430f.status"], "running");
        assert_eq!(s["task.submitted_by"], "u1");
        assert_eq!(s["workspace"], "D:/ws/copy_1");
        assert_eq!(s["ws_meta"]["mode"], "worktree");
        // 杂键仍裁掉（默认拒绝语义不变，防越权大字段出口）
        assert!(s.get("secret_field").is_none());
    }

    #[test]
    fn test_summarize_omits_absent_declared_fields() {
        // 反向性质：无任务域字段的普通会话管道，摘要不得伪造 task.*/lineage.* 键
        let export = ExportFields::from_manifests(&[export_manifest(&[
            "task.goal",
            "task.status",
            "task.id",
            "lineage.parent_pipeline_id",
            "lineage.origin_session_id",
            "lineage.root",
        ])]);
        let s = summarize_state(&json!({"pipeline_id": "p2", "ended": true}), &export);
        assert_eq!(s["pipeline_id"], "p2");
        assert_eq!(s["ended"], true);
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
    fn test_summarize_exports_evaluation_fields() {
        // 评估域键声明出口——pipeline-state.list 同数据源，评估执行器据此轮询
        // 回收评估结论（ADR 2026-08-24-eval-pipeline-state-keys 语义由声明承载）。
        let export = ExportFields::from_manifests(&[export_manifest(&[
            "evaluation.of_task",
            "evaluation.metric_id",
            "evaluation.detected_result",
        ])]);
        let state = json!({
            "pipeline_id": "evalPipe",
            "evaluation.of_task": "taskPipe1",
            "evaluation.metric_id": "semantic_check",
            "evaluation.detected_result": {"passed": true, "score": 88, "feedback": "结构完整"},
            "secret_field": "must-not-leak",
        });
        let s = summarize_state(&state, &export);
        assert_eq!(s["evaluation.of_task"], "taskPipe1");
        assert_eq!(s["evaluation.metric_id"], "semantic_check");
        assert_eq!(s["evaluation.detected_result"]["passed"], true);
        assert_eq!(s["evaluation.detected_result"]["score"], 88);
        assert!(s.get("secret_field").is_none());
    }

    #[test]
    fn test_summarize_union_across_manifests() {
        // 并集语义：多个 manifest 各自声明，取并集出口。
        let export = ExportFields::from_manifests(&[
            export_manifest(&["task.goal"]),
            export_manifest(&["llm_model"]),
        ]);
        let s = summarize_state(
            &json!({"pipeline_id": "p7", "task.goal": "g", "llm_model": "k2"}),
            &export,
        );
        assert_eq!(s["task.goal"], "g");
        assert_eq!(s["llm_model"], "k2");
    }

    #[test]
    fn test_summarize_cuts_retired_task_submit_params() {
        // 参数退役守卫：task.priority/task.max_retries 已随 task_submit 参数瘦身
        // 移除（执行层零消费者，ADR 2026-08-24-task-submit-param-diet）——退役后
        // 无插件声明，即使任务域其他键已声明也不出口。
        let export = ExportFields::from_manifests(&[export_manifest(&["task.goal"])]);
        let s = summarize_state(
            &json!({
                "pipeline_id": "p6",
                "task.priority": "high",
                "task.max_retries": 2,
                "task.goal": "g",
            }),
            &export,
        );
        assert!(s.get("task.priority").is_none(), "退役参数不应出口");
        assert!(s.get("task.max_retries").is_none(), "退役参数不应出口");
        assert_eq!(s["task.goal"], "g");
    }

    #[test]
    fn test_summarize_still_cuts_non_whitelisted_fields() {
        // 默认拒绝机制本身不变：声明之外的新键不出口
        let export =
            ExportFields::from_manifests(&[export_manifest(&["task.goal", "task.ended_at"])]);
        let s = summarize_state(
            &json!({
                "pipeline_id": "p3",
                "secret_blob": "x",
                "task.goal": "g"
            }),
            &export,
        );
        assert!(s.get("secret_blob").is_none(), "未声明字段仍裁剪");
        assert_eq!(s["task.goal"], "g");
        // raw_result（最终输出）属内核运行域基线，恒出口——复盘报告提取/任务树展示依赖
        let s2 = summarize_state(
            &json!({"pipeline_id": "p4", "raw_result": "复盘结论：x"}),
            &ExportFields::default(),
        );
        assert_eq!(s2["raw_result"], "复盘结论：x");
        // 终态回写的 task.ended_at 经声明出口（任务树展示完成时间）
        let s3 = summarize_state(
            &json!({"pipeline_id": "p5", "task.ended_at": "2026-08-16T09:00:00Z"}),
            &export,
        );
        assert_eq!(s3["task.ended_at"], "2026-08-16T09:00:00Z");
    }
}

#[cfg(test)]
mod plugin_profile_write_tests {
    //! K1：default_profile.yaml 损坏时 PUT enabled 拒绝写（422），不再用
    //! 硬编码模板顶替并覆写（清空全部插件启停配置）；文件缺失 → 正常新建。

    use super::*;

    fn app_state_with_project_root(tmp: &tempfile::TempDir) -> AppState {
        let mut state = AppState::new();
        state.project_root = Some(tmp.path().to_path_buf());
        state
    }

    fn profile_path(tmp: &tempfile::TempDir) -> std::path::PathBuf {
        tmp.path()
            .join("config")
            .join("plugins")
            .join("default_profile.yaml")
    }

    #[tokio::test]
    async fn corrupted_profile_refuses_overwrite_with_422() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(profile_path(&tmp).parent().unwrap()).unwrap();
        let corrupted = "version: 1\nplugins: [broken\n";
        std::fs::write(profile_path(&tmp), corrupted).unwrap();

        let state = app_state_with_project_root(&tmp);
        let err = plugins_set_enabled_handler(
            axum::extract::Path("some_plugin".to_string()),
            axum::extract::State(state),
            axum::Json(EnabledBody { enabled: true }),
        )
        .await
        .unwrap_err();

        assert!(
            matches!(err, ApiError::UnprocessableEntity { .. }),
            "损坏 profile 应 422 拒写，实际 {err:?}"
        );
        assert_eq!(
            std::fs::read_to_string(profile_path(&tmp)).unwrap(),
            corrupted,
            "损坏现场必须原样保留（拒写不覆写）"
        );
    }

    #[tokio::test]
    async fn scalar_top_level_profile_refuses_overwrite_with_422() {
        // 解析成功但顶层非 Mapping（标量）：if-let Mapping 不命中则静默跳过补丁、
        // 写回仍会覆盖原文——同样按损坏拒写。
        let tmp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(profile_path(&tmp).parent().unwrap()).unwrap();
        std::fs::write(profile_path(&tmp), "just_a_string\n").unwrap();

        let state = app_state_with_project_root(&tmp);
        let err = plugins_set_enabled_handler(
            axum::extract::Path("some_plugin".to_string()),
            axum::extract::State(state),
            axum::Json(EnabledBody { enabled: false }),
        )
        .await
        .unwrap_err();

        assert!(matches!(err, ApiError::UnprocessableEntity { .. }));
        assert_eq!(
            std::fs::read_to_string(profile_path(&tmp)).unwrap(),
            "just_a_string\n"
        );
    }

    #[tokio::test]
    async fn missing_profile_creates_fresh_one_on_enable() {
        // 文件缺失（非损坏）：首次落盘合法——新建 Mapping 写入该插件开关，
        // 其他插件无存量可破坏。
        let tmp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(profile_path(&tmp).parent().unwrap()).unwrap();

        let state = app_state_with_project_root(&tmp);
        let resp = plugins_set_enabled_handler(
            axum::extract::Path("fresh_plugin".to_string()),
            axum::extract::State(state),
            axum::Json(EnabledBody { enabled: true }),
        )
        .await
        .unwrap();

        assert_eq!(resp.0["success"], true);
        let raw = std::fs::read_to_string(profile_path(&tmp)).unwrap();
        assert!(raw.contains("fresh_plugin"), "新 profile 应含该插件条目");
        assert!(raw.contains("enabled: true"));
    }

    #[tokio::test]
    async fn valid_profile_roundtrip_preserves_other_plugins() {
        // 回归：合法 profile 上切换某插件开关，其他插件启停设置必须保留
        // （K1 修复守护的正是这条不变量）。
        let tmp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(profile_path(&tmp).parent().unwrap()).unwrap();
        std::fs::write(
            profile_path(&tmp),
            "version: 1\nplugins:\n  other_plugin:\n    enabled: false\ndefaults:\n  enabled: true\n",
        )
        .unwrap();

        let state = app_state_with_project_root(&tmp);
        let _resp = plugins_set_enabled_handler(
            axum::extract::Path("target_plugin".to_string()),
            axum::extract::State(state),
            axum::Json(EnabledBody { enabled: true }),
        )
        .await
        .unwrap();

        let doc: serde_yaml::Value =
            serde_yaml::from_str(&std::fs::read_to_string(profile_path(&tmp)).unwrap()).unwrap();
        assert_eq!(
            doc["plugins"]["other_plugin"]["enabled"], false,
            "其他插件启停设置必须保留"
        );
        assert_eq!(doc["plugins"]["target_plugin"]["enabled"], true);
    }
}

#[cfg(test)]
mod pending_inputs_failure_tests {
    //! 扫描 2026-08-27 辖区一 Should#18：pending-inputs 删除/清空前
    //! `list_pending_inputs(...).unwrap_or_default()` 吞 DB 错 → cmid 丢 →
    //! 排队中的 REST chat waiter 永不通知（挂死至超时）。
    //! 修复契约：列表读取失败 = 无法安全逐出 → 跳过删除/清空并返回 503，
    //! 排队条目保留（消费循环稍后仍会正常 pop 执行并回传 outcome），不再丢 cmid。

    use super::*;
    use agentos_core::traits::StorageBackend;
    use agentos_core::types::{PendingInputRecord, PendingInputSource};
    use axum::http::HeaderMap;

    /// 委托真实 SqliteStore 的探针 store：仅 list_pending_inputs 可注入故障，
    /// 其余方法全部转发真实实现（删除/清空走真库，非全 mock）。
    struct PendingProbeStore {
        inner: std::sync::Arc<agentos_engine::SqliteStore>,
        fail_list: std::sync::atomic::AtomicBool,
    }

    impl PendingProbeStore {
        fn new() -> Self {
            Self {
                inner: std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()),
                fail_list: std::sync::atomic::AtomicBool::new(false),
            }
        }

        async fn seed(&self, pipeline_id: &str, id: &str, cmid: &str) {
            StorageBackend::enqueue_pending_input(
                self.inner.as_ref(),
                "default",
                pipeline_id,
                &PendingInputRecord {
                    id: id.to_string(),
                    pipeline_id: pipeline_id.to_string(),
                    tenant_id: "default".to_string(),
                    user_id: "u1".to_string(),
                    content: "排队输入".to_string(),
                    thread: "thread-probe".to_string(),
                    source: PendingInputSource::Trigger,
                    agent_id: "agentos".to_string(),
                    route_id: pipeline_id.to_string(),
                    thinking_strength: String::new(),
                    client_message_id: cmid.to_string(),
                    execution_context: None,
                    state_overlay: None,
                    created_at: chrono::Utc::now().to_rfc3339(),
                },
            )
            .await
            .unwrap();
        }

        async fn listed(&self, pipeline_id: &str) -> Vec<PendingInputRecord> {
            StorageBackend::list_pending_inputs(self.inner.as_ref(), "default", pipeline_id)
                .await
                .unwrap()
        }
    }

    #[async_trait::async_trait]
    impl StorageBackend for PendingProbeStore {
        async fn list_pending_inputs(
            &self,
            tenant_id: &str,
            pipeline_id: &str,
        ) -> Result<Vec<PendingInputRecord>, agentos_core::types::StorageError> {
            if self.fail_list.load(std::sync::atomic::Ordering::SeqCst) {
                return Err(agentos_core::types::StorageError::Database(
                    "injected list failure".to_string(),
                ));
            }
            StorageBackend::list_pending_inputs(self.inner.as_ref(), tenant_id, pipeline_id).await
        }
        async fn delete_pending_input(
            &self,
            tenant_id: &str,
            pipeline_id: &str,
            input_id: &str,
        ) -> Result<bool, agentos_core::types::StorageError> {
            StorageBackend::delete_pending_input(
                self.inner.as_ref(),
                tenant_id,
                pipeline_id,
                input_id,
            )
            .await
        }
        async fn clear_pending_inputs(
            &self,
            tenant_id: &str,
            pipeline_id: &str,
        ) -> Result<usize, agentos_core::types::StorageError> {
            StorageBackend::clear_pending_inputs(self.inner.as_ref(), tenant_id, pipeline_id).await
        }
        async fn get_thread_id_by_pipeline(
            &self,
            pipeline_id: &str,
        ) -> Result<Option<String>, agentos_core::types::StorageError> {
            StorageBackend::get_thread_id_by_pipeline(self.inner.as_ref(), pipeline_id).await
        }

        // ── trait 必需（无默认）方法：转发真实 store，保持行为真实性 ──
        async fn get_run(
            &self,
            run_id: &str,
        ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
            self.inner.get_run(run_id).await
        }
        async fn get_messages_by_pipeline(
            &self,
            pipeline_id: &str,
            opts: agentos_core::traits::MessageQueryOpts,
        ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError>
        {
            self.inner.get_messages_by_pipeline(pipeline_id, opts).await
        }
        async fn get_blob(
            &self,
            blob_id: &str,
        ) -> Result<Vec<u8>, agentos_core::types::StorageError> {
            self.inner.get_blob(blob_id).await
        }
        async fn append_trace(
            &self,
            entry: agentos_core::types::TraceEntry,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner.append_trace(entry).await
        }
        async fn create_branch(
            &self,
            branch: agentos_core::types::Branch,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner.create_branch(branch).await
        }
        async fn update_run_status(
            &self,
            run_id: &str,
            status: agentos_core::types::RunStatus,
            branch: Option<&str>,
            seq: Option<u32>,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner
                .update_run_status(run_id, status, branch, seq)
                .await
        }
        async fn create_run(
            &self,
            run_id: &str,
            config_hash: &str,
            tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            StorageBackend::create_run(self.inner.as_ref(), run_id, config_hash, tenant_id).await
        }
        async fn store_blob(
            &self,
            data: &[u8],
            mime_type: &str,
        ) -> Result<String, agentos_core::types::StorageError> {
            StorageBackend::store_blob(self.inner.as_ref(), data, mime_type).await
        }
        async fn create_session(
            &self,
            session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner.create_session(session).await
        }
        async fn get_session(
            &self,
            thread_id: &str,
        ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            self.inner.get_session(thread_id).await
        }
        async fn list_sessions(
            &self,
            filter: agentos_core::traits::SessionListFilter,
        ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            self.inner.list_sessions(filter).await
        }
        async fn update_session(
            &self,
            session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner.update_session(session).await
        }
        async fn delete_session(
            &self,
            thread_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            self.inner.delete_session(thread_id).await
        }
        async fn link_pipeline_session(
            &self,
            pipeline_id: &str,
            thread_id: &str,
            tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner
                .link_pipeline_session(pipeline_id, thread_id, tenant_id)
                .await
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            thread_id: &str,
            tenant_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            self.inner
                .list_pipeline_ids_by_thread(thread_id, tenant_id)
                .await
        }
        async fn get_step_traces_by_thread(
            &self,
            thread_id: &str,
            tenant_id: &str,
        ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError>
        {
            self.inner
                .get_step_traces_by_thread(thread_id, tenant_id)
                .await
        }
        async fn create_user(
            &self,
            user: &agentos_core::types::UserRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner.create_user(user).await
        }
        async fn get_user_by_id(
            &self,
            user_id: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            self.inner.get_user_by_id(user_id).await
        }
        async fn get_user_by_username(
            &self,
            username: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            self.inner.get_user_by_username(username).await
        }
        async fn list_users(
            &self,
        ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            self.inner.list_users().await
        }
        async fn update_last_login(
            &self,
            user_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner.update_last_login(user_id).await
        }
        async fn delete_user(
            &self,
            user_id: &str,
        ) -> Result<bool, agentos_core::types::StorageError> {
            self.inner.delete_user(user_id).await
        }
    }

    fn probe_state(store: std::sync::Arc<PendingProbeStore>) -> AppState {
        let mut state = AppState::new();
        let dyn_store: std::sync::Arc<dyn StorageBackend> = store;
        state.store = Some(dyn_store);
        state
    }

    fn probe_record(id: &str) -> PendingInputRecord {
        PendingInputRecord {
            id: id.to_string(),
            pipeline_id: "pipe-fail-del".to_string(),
            tenant_id: "default".to_string(),
            user_id: "u1".to_string(),
            content: "x".to_string(),
            thread: "thread-probe".to_string(),
            source: PendingInputSource::Trigger,
            agent_id: "agentos".to_string(),
            route_id: "pipe-fail-del".to_string(),
            thinking_strength: String::new(),
            client_message_id: String::new(),
            execution_context: None,
            state_overlay: None,
            created_at: chrono::Utc::now().to_rfc3339(),
        }
    }

    #[tokio::test]
    async fn delete_with_failing_list_returns_503_and_keeps_entry() {
        let probe = std::sync::Arc::new(PendingProbeStore::new());
        probe.seed("pipe-fail-del", "pi_del_1", "").await;
        probe
            .fail_list
            .store(true, std::sync::atomic::Ordering::SeqCst);
        let state = probe_state(probe.clone());

        let resp = pending_inputs_delete_handler(
            axum::extract::State(state),
            axum::extract::Path(("pipe-fail-del".to_string(), "pi_del_1".to_string())),
            HeaderMap::new(),
        )
        .await;

        match resp {
            Ok(body) => panic!("list 故障时不得照常删除伪装成功，实际 {:?}", body.0),
            Err(e) => assert!(
                matches!(e, ApiError::ServiceUnavailable { .. }),
                "应返回 503，实际 {e:?}"
            ),
        }
        probe
            .fail_list
            .store(false, std::sync::atomic::Ordering::SeqCst);
        assert_eq!(
            probe.listed("pipe-fail-del").await.len(),
            1,
            "队列条目必须保留——消费循环稍后仍可正常执行并回传 outcome"
        );
    }

    #[tokio::test]
    async fn clear_with_failing_list_returns_503_and_keeps_entries() {
        let probe = std::sync::Arc::new(PendingProbeStore::new());
        StorageBackend::enqueue_pending_input(
            probe.inner.as_ref(),
            "default",
            "pipe-fail-clr",
            &probe_record("pi_c_1"),
        )
        .await
        .unwrap();
        StorageBackend::enqueue_pending_input(
            probe.inner.as_ref(),
            "default",
            "pipe-fail-clr",
            &probe_record("pi_c_2"),
        )
        .await
        .unwrap();
        probe
            .fail_list
            .store(true, std::sync::atomic::Ordering::SeqCst);
        let state = probe_state(probe.clone());

        let resp = pending_inputs_clear_handler(
            axum::extract::State(state),
            axum::extract::Path("pipe-fail-clr".to_string()),
            HeaderMap::new(),
        )
        .await;

        match resp {
            Ok(body) => panic!("list 故障时不得照常清空伪装成功，实际 {:?}", body.0),
            Err(e) => assert!(
                matches!(e, ApiError::ServiceUnavailable { .. }),
                "应返回 503，实际 {e:?}"
            ),
        }
        probe
            .fail_list
            .store(false, std::sync::atomic::Ordering::SeqCst);
        assert_eq!(
            probe.listed("pipe-fail-clr").await.len(),
            2,
            "清空失败不得破坏排队条目"
        );
    }

    #[tokio::test]
    async fn delete_with_healthy_list_notifies_rest_waiter() {
        // 正向对照：list 正常 → 删除成功 + REST chat waiter 收到失败 outcome
        // （这正是故障分支要保护的那条通知链）。
        let probe = std::sync::Arc::new(PendingProbeStore::new());
        probe
            .seed("pipe-fail-del", "pi_del_ok", "http_probe_notify_1")
            .await;
        let state = probe_state(probe);

        let (tx, rx) = tokio::sync::oneshot::channel();
        crate::ws_session::register_outcome_waiter("http_probe_notify_1".to_string(), tx);

        let resp = pending_inputs_delete_handler(
            axum::extract::State(state),
            axum::extract::Path(("pipe-fail-del".to_string(), "pi_del_ok".to_string())),
            HeaderMap::new(),
        )
        .await;

        assert!(resp.is_ok(), "list 正常时删除应成功");
        let outcome = tokio::time::timeout(std::time::Duration::from_secs(2), rx)
            .await
            .expect("waiter 应被通知")
            .expect("outcome 通道不应关闭");
        assert!(outcome.failed, "逐出的排队条目应对 REST 请求回失败 outcome");
    }
}

#[cfg(test)]
mod cold_state_row_tests {
    //! cold_state_row 对外契约锁（Should#7 重构后防漂移）：checkpoint 基线 +
    //! pipeline_state 表行覆盖的合并顺序、表行-only 基线、双空真孤儿不出口。
    //! 两类 DB 故障路径维持返回 None 不变（调用方跳行），仅新增 warn 留痕。

    use super::*;
    use agentos_core::traits::StorageBackend;
    use std::sync::Arc;

    fn sqlite() -> Arc<agentos_engine::SqliteStore> {
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap())
    }

    #[tokio::test]
    async fn table_fields_override_stale_checkpoint_scalars() {
        let store = sqlite();
        let dyn_store: Arc<dyn StorageBackend> = store.clone();
        let ckpt = serde_json::json!({ "task.status": "pending", "agent.id": "agentos" });
        StorageBackend::save_checkpoint(dyn_store.as_ref(), "pipe-cold", "default", 3, &ckpt)
            .await
            .unwrap();
        // 出生字段已过期：表行最新值必须覆盖（completed 赢过 pending）
        StorageBackend::upsert_state_field(
            dyn_store.as_ref(),
            "pipe-cold",
            "default",
            "task.status",
            &serde_json::json!("completed"),
        )
        .await
        .unwrap();

        let row = cold_state_row(&dyn_store, "pipe-cold", "default")
            .await
            .expect("有持久痕迹应出口");
        assert_eq!(
            row["task.status"], "completed",
            "表行最新值覆盖 checkpoint 过期值"
        );
        assert_eq!(
            row["agent.id"], "agentos",
            "checkpoint 独有字段保留（表未写不覆盖）"
        );
    }

    #[tokio::test]
    async fn fields_only_without_checkpoint_still_emits_row() {
        // running 中任务 interval 未到无 checkpoint：只靠表行基线可见
        let store = sqlite();
        let dyn_store: Arc<dyn StorageBackend> = store.clone();
        StorageBackend::upsert_state_field(
            dyn_store.as_ref(),
            "pipe-cold-b",
            "default",
            "title",
            &serde_json::json!("刚提交的任务"),
        )
        .await
        .unwrap();

        let row = cold_state_row(&dyn_store, "pipe-cold-b", "default")
            .await
            .expect("表行单独在场即出口");
        assert_eq!(row["title"], "刚提交的任务");
    }

    #[tokio::test]
    async fn orphan_without_any_persisted_trace_returns_none() {
        let dyn_store: Arc<dyn StorageBackend> = sqlite();
        assert!(
            cold_state_row(&dyn_store, "pipe-orphan", "default")
                .await
                .is_none(),
            "checkpoint 与表行双空 = 真孤儿不出口"
        );
    }
}
