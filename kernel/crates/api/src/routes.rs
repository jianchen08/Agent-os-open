// @feature: FP-0.2.一 插件协议（api 路由装配） | @ci: rust-test
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
    CapabilityRegistry, HttpHandleCapability, PluginInvoker, PluginManifest, PluginType,
    StorageBackend,
};

use agentos_plugin_loader::{CapabilityRegistryImpl, PluginScopeRegistry};
use axum::response::IntoResponse;
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::config_service::{
    atomic_write_yaml, compute_etag, resolve_kernel_config_target, seed_user_config_from_factory,
    ConfigTargetMode,
};
use crate::metrics::plugin_widget_broadcast::WidgetBinding;
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
    /// 内核基础设施能力契约聚合（config/kernel/kernel_capabilities/*.json 透出）。
    /// 与插件侧 plugin.json 契约同构：前端/调用方/入口校验消费同一份定义，
    /// 不读代码副本（单一真值源，消除双轨漂移）。
    pub kernel_capabilities: Vec<serde_json::Value>,
    /// P2 模式包资源透出：模式私有 agent 条目（约定 `agents/<stem>.yaml`，
    /// 按需聚合现算，见 [`crate::mode_view`]）。结构 additive 只增：
    /// key/plugin_id/path。Python 消费方（task_submit 解析、面板导航）经
    /// HTTP 面取数，不读代码副本。
    pub mode_agents: Vec<serde_json::Value>,
    /// P2 模式包资源透出：模式内编排定义条目（约定 `pipelines/<stem>.yaml`，
    /// 编排键是管道定义，与运行实例 pipeline_id 概念分离）。
    /// 结构 additive 只增：key/plugin_id/path/task_kinds。
    pub mode_pipelines: Vec<serde_json::Value>,
}

/// 应用状态——通过 Axum State 共享。
///
/// 集成插件系统后，持有能力注册表、管道引擎以及 0.2 引擎所需的运行期资源
/// （invoker / store / project_root）。已知插件 id 集合不在此快照——经
/// `manifests` 共享存储现读（watcher 热发现即时可见）；管道配置与公共 step
/// 库同理不做 boot 快照——热路径 [`crate::server::load_and_compile_by_name`]
/// 每次现读盘（改 config 即生效，无需重启）。
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
    /// 插件目录活真源：loader 活对象句柄（其路径表每轮 discover 全量重建，
    /// watcher 热发现即时可见）。
    ///
    /// HTTP dispatcher 据此把 `/ext/{plugin_id}/assets/{*path}` 解析到
    /// `<plugin_root>/web/<path>` 直接读文件返回，免去为每个子资源单独声明
    /// http_endpoints；配置保存 / validate-all 据此定位磁盘 plugin.json。
    /// 消费点经 [`AppState::plugin_dir`]/[`AppState::plugin_dirs`] 现取
    /// （B1：boot 快照字段 `plugin_dirs` 已退役——热发现插件曾因此 404/500
    /// 直到重启）。None = 未装配（裸测试构造；目录解析按缺失处理）。
    pub plugin_loader: Option<Arc<agentos_plugin_loader::PluginLoaderImpl>>,
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
    /// （namespace）调用插件服务的出口——如交互应答按 interaction-respond
    /// 协议角色解析路由目标（P1-3 声明驱动），内核不点名 namespace/插件 id/工具名。
    /// None = 未装配（兼容旧装配/测试，调用点显式降级报错）。
    pub capability_handlers: Option<Arc<agentos_mcp::CapabilityHandlerRegistry>>,
    /// 内核能力契约（config/kernel/kernel_capabilities/*.json）——schema 聚合透出用。
    /// None = 未装配（契约目录缺失或测试装配；入口校验由 router 侧独立持有）。
    pub kernel_capability_contracts:
        Option<Arc<Vec<crate::kernel_capabilities::KernelCapabilityContract>>>,
    /// D12：已消费的 refresh token jti 注册表（单次轮换——旧值即废）。
    ///
    /// 生产装配（`with_db`）的持久真值在 store 的 `consumed_refresh_jtis` 表
    /// （跨重启判已消费，行保留 7 天对齐 refresh TTL 上限）；本进程内集合只在
    /// db 未接线的装配（裸 `AppState` 测试脚手架）下作兜底，语义 = 重启即清。
    pub consumed_refresh_jtis: Arc<parking_lot::Mutex<std::collections::HashSet<String>>>,
    /// 登录失败滑动窗口：username → 失败时刻队列（15 分钟窗口 ≥10 次 → 429）。
    /// 进程内存记录：同机单进程内核部署形态下等价于全量状态，重启即清零。
    pub login_failures: Arc<parking_lot::Mutex<HashMap<String, Vec<std::time::Instant>>>>,
    /// 注册限频滑动窗口：全局尝试时刻队列（15 分钟窗口限 20 次 → 429）。
    /// 进程内存记录，约束同上（单机单进程；多实例部署需外置限流）。
    pub register_attempts: Arc<parking_lot::Mutex<Vec<std::time::Instant>>>,
    /// WS 一次性握手票据表（POST /api/v1/ws-ticket 签发、?ticket= 单次消费）。
    pub ws_tickets: Arc<crate::ws_ticket::WsTicketStore>,
}

impl AppState {
    pub fn new() -> Self {
        Self {
            manifests: Arc::new(RwLock::new(Vec::new())),
            capability_registry: None,
            invoker: None,
            store: None,
            db: None,
            project_root: None,
            enabled_plugin_ids: Arc::new(RwLock::new(std::collections::HashSet::new())),
            session: None,
            inbound_router: None,
            http_handler: None,
            metrics: None,
            plugin_loader: None,
            config_center: None,
            plugin_scopes: Arc::new(PluginScopeRegistry::new()),
            widget_bindings: None,
            contract_states: Arc::new(crate::contract::ContractLedger::new()),
            capability_handlers: None,
            kernel_capability_contracts: None,
            consumed_refresh_jtis: Arc::new(parking_lot::Mutex::new(
                std::collections::HashSet::new(),
            )),
            login_failures: Arc::new(parking_lot::Mutex::new(HashMap::new())),
            register_attempts: Arc::new(parking_lot::Mutex::new(Vec::new())),
            ws_tickets: Arc::new(crate::ws_ticket::WsTicketStore::new()),
        }
    }

    /// 消费一个 refresh token jti（D12 单次轮换）。返回 true = 首次消费
    /// （token 有效）；false = 已被消费过（旧值复用，调用方拒绝）。
    ///
    /// db 已接线（生产装配）走 store 的 `consumed_refresh_jtis` 持久账本：
    /// 重启后旧 jti 仍判已消费，过期行由 store 侧按 refresh TTL 上限顺手清理。
    /// 账本故障按 fail-closed 处理（记 error 并返回 false 拒绝本次刷新），
    /// 不因存储异常放行旧值复用。db 未接线（裸 AppState 测试装配）退回进程内
    /// 集合。低频路径（每 token 至多一次），经 with_conn 同步执行不派 DB 线程池。
    pub fn consume_refresh_jti(&self, jti: &str) -> bool {
        if let Some(db) = self.db.as_ref() {
            return match db.consume_refresh_jti(jti) {
                Ok(first) => first,
                Err(e) => {
                    tracing::error!(
                        error = %e,
                        "refresh jti 消费账本写入失败，fail-closed 拒绝本次刷新"
                    );
                    false
                }
            };
        }
        self.consumed_refresh_jtis.lock().insert(jti.to_string())
    }

    /// 构建集成了插件系统的 AppState（生产装配入口）。
    ///
    /// 多参签名保证能力面非空：registry / invoker / store / project_root 均为
    /// 必传，chat 端点据 `state` 字段直接构造 [`agentos_engine::PipelineExecutor`]。
    /// 管道配置/公共 step 库不经此注入：热路径现读盘，boot 快照副本是死字段。
    pub fn with_plugins(
        manifests: Vec<PluginManifest>,
        registry: Arc<CapabilityRegistryImpl>,
        invoker: Arc<dyn PluginInvoker>,
        store: Arc<dyn StorageBackend>,
        project_root: PathBuf,
        enabled_plugin_ids: std::collections::HashSet<String>,
    ) -> Self {
        // 注入面字段覆盖基底默认；其余（session/http_handler/metrics/plugin_loader/
        // config_center 等）经 `..Self::new()` 收敛，加字段只改 [`AppState::new`] 一处。
        Self {
            manifests: Arc::new(RwLock::new(manifests)),
            capability_registry: Some(registry),
            invoker: Some(invoker),
            store: Some(store),
            project_root: Some(project_root),
            enabled_plugin_ids: Arc::new(RwLock::new(enabled_plugin_ids)),
            ..Self::new()
        }
    }

    /// 注入内核能力契约（config/kernel/kernel_capabilities/*.json 加载物）：
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

    /// 注入插件目录活真源（loader 活对象句柄）。
    ///
    /// 启用后，HTTP dispatcher 把 `/ext/{plugin_id}/assets/{*path}` 解析到
    /// `<plugin_dir>/web/<path>` 直读文件返回；配置保存 / validate-all 据此
    /// 定位磁盘 plugin.json。消费点经 [`Self::plugin_dir`]/[`Self::plugin_dirs`]
    /// 现取（路径表每轮 discover 全量重建，热发现即时可见）。
    pub fn with_plugin_loader(self, loader: Arc<agentos_plugin_loader::PluginLoaderImpl>) -> Self {
        Self {
            plugin_loader: Some(loader),
            ..self
        }
    }

    /// 单查插件根目录（活路径表现取；未装配/未登记 → None）。
    pub fn plugin_dir(&self, plugin_id: &str) -> Option<PathBuf> {
        self.plugin_loader.as_ref()?.plugin_dir(plugin_id)
    }

    /// 全量插件根目录映射（活路径表现取）。
    pub fn plugin_dirs(&self) -> HashMap<String, PathBuf> {
        self.plugin_loader
            .as_ref()
            .map(|l| l.get_plugin_dirs())
            .unwrap_or_default()
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

#[cfg(test)]
mod consume_refresh_jti_tests;

/// /health 端点处理器（AC-06-3）。
pub async fn health_handler() -> axum::Json<HealthResponse> {
    axum::Json(HealthResponse {
        status: "ok".to_string(),
        version: env!("CARGO_PKG_VERSION").to_string(),
        timestamp: chrono::Utc::now().to_rfc3339(),
    })
}

/// /uploads/{filename} 的媒体扩展名白名单（纵深防御：uploads 目录只承载媒体
/// artifacts 的类型契约；白名单外扩展名一律 404，防遍历读取上传目录内非媒体
/// 文件）。鉴权由 write_surface_auth 层持有（/uploads/ 受保护前缀），匿名请求
/// 在进入本 handler 前 401（ADR 2026-10-10）。
const UPLOAD_ALLOWED_EXTENSIONS: [&str; 12] = [
    "png", "jpg", "jpeg", "gif", "webp", "svg", "mp4", "mp3", "wav", "pdf", "webm", "ico",
];

/// /uploads 单文件大小上限默认值（字节）：64 MiB。媒体 artifacts（截图/录音/
/// 短视频）正常远小于此；上限只挡异常巨物（误传/恶意构造），约束认证用户
/// 单请求读盘的内存峰值（审查总册 P1-12：整读无上限 = 内存放大面）。
pub(crate) const DEFAULT_UPLOAD_MAX_BYTES: u64 = 64 * 1024 * 1024;

/// 上限配置环境变量（常量默认 + env 覆盖，同 AGENTOS_MAX_CONCURRENT_RUNS 先例）。
pub(crate) const UPLOAD_MAX_BYTES_ENV: &str = "AGENTOS_UPLOAD_MAX_BYTES";

/// 生效上限解析：未设 / 非法（非 u64）/ "0"（会使一切请求 413 的误配自瘫）
/// → 默认值；合法正值生效。
fn upload_max_bytes_from(raw: Option<&str>) -> u64 {
    match raw.map(str::parse::<u64>) {
        Some(Ok(v)) if v > 0 => v,
        _ => DEFAULT_UPLOAD_MAX_BYTES,
    }
}

/// 当前生效上限：每次请求现读环境（不 OnceLock 缓存——测试钉桩可即时生效，
/// 一次 env 查找相对一次读盘可忽略）。
fn upload_max_bytes() -> u64 {
    upload_max_bytes_from(std::env::var(UPLOAD_MAX_BYTES_ENV).ok().as_deref())
}

/// 限量读的块长（字节）：流式计数按块推进，上限远大于块长时读放大量以块为界。
const UPLOAD_READ_CHUNK: usize = 64 * 1024;

/// /uploads 单文件读取结果：超限 / IO 失败分立（分别映射 413 / 404；IO 细节
/// 按既有契约不外泄——读失败统一 404）。
#[derive(Debug)]
enum UploadReadError {
    TooLarge,
    Io,
}

/// /uploads 限量读：声明长度闸 + 流式字节计数双闸。`declared_len` 来自读取前
/// stat（None = 无长度声明，直接走流式计数）；读取途中计数超限同样拒绝——
/// stat 与读之间文件增长（TOCTOU）不放大内存。
fn read_upload_capped(
    path: &std::path::Path,
    declared_len: Option<u64>,
    max_bytes: u64,
) -> Result<Vec<u8>, UploadReadError> {
    use std::io::Read;
    if let Some(len) = declared_len {
        if len > max_bytes {
            return Err(UploadReadError::TooLarge);
        }
    }
    let mut file = std::fs::File::open(path).map_err(|_| UploadReadError::Io)?;
    let mut out: Vec<u8> = Vec::new();
    let mut chunk = [0u8; UPLOAD_READ_CHUNK];
    loop {
        let n = file.read(&mut chunk).map_err(|_| UploadReadError::Io)?;
        if n == 0 {
            return Ok(out);
        }
        if out.len() as u64 + n as u64 > max_bytes {
            return Err(UploadReadError::TooLarge);
        }
        out.extend_from_slice(&chunk[..n]);
    }
}

/// /uploads/{filename} 静态服务（上传文件读取，需鉴权——/uploads/ 受保护前缀，
/// ADR 2026-10-10）。
///
/// artifacts 上传落盘默认租户（default）上传目录并返回
/// `/uploads/{filename}` URL；前端消费面统一经鉴权取流加载器（2026-09-28
/// 文件加载器路由，apiClient 带 Authorization 经 artifacts_file_content 取流
/// 或本端点带 token 加载）。落盘位置随
/// ADR 2026-09-13-unified-user-root 迁入用户空间数据根（artifacts 插件
/// `_get_uploads_dir` 同源），本 handler 按序解析：用户空间
/// `<user_data>/default/uploads` 优先（迁移后唯一写点），仓库根
/// `data/default/uploads` 回退（迁移脚本显式调用，未迁移部署的存量文件）。
/// 路径安全：拒绝 `..` 与路径分隔符；扩展名白名单外 404（媒体类型契约，
/// 见 UPLOAD_ALLOWED_EXTENSIONS）。大小上限：stat 闸先行拒绝巨物（不 open
/// 不读）+ 限量读流式计数兜底（见 read_upload_capped），超限一律 413，
/// 上限见 DEFAULT_UPLOAD_MAX_BYTES / UPLOAD_MAX_BYTES_ENV。
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
    let ext_allowed = filename
        .rsplit('.')
        .next()
        .map(|ext| UPLOAD_ALLOWED_EXTENSIONS.contains(&ext.to_ascii_lowercase().as_str()))
        .unwrap_or(false);
    if !ext_allowed {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    }
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(data_dir) = agentos_core::user_space::user_data_dir() {
        candidates.push(data_dir.join("default").join("uploads"));
    }
    if let Some(project_root) = state.project_root.as_ref() {
        candidates.push(project_root.join("data").join("default").join("uploads"));
    }
    let mut resolved: Option<(PathBuf, u64)> = None;
    for dir in &candidates {
        let path = dir.join(&filename);
        let Ok(meta) = tokio::fs::metadata(&path).await else {
            continue;
        };
        if meta.is_file() {
            resolved = Some((path, meta.len()));
            break;
        }
    }
    let Some((file_path, file_len)) = resolved else {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    };
    let max_bytes = upload_max_bytes();
    if file_len > max_bytes {
        return (StatusCode::PAYLOAD_TOO_LARGE, "upload exceeds size limit").into_response();
    }
    // mime 扩展名在 move 进阻塞任务前提取（文件路径归闭包所有）。
    let ext = file_path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_string();
    // 整读移出 async 执行器（同 /ext 静态资源先例）：阻塞线程内做限量读。
    let read = tokio::task::spawn_blocking(move || {
        read_upload_capped(&file_path, Some(file_len), max_bytes)
    })
    .await;
    let bytes = match read {
        Ok(Ok(bytes)) => bytes,
        Ok(Err(UploadReadError::TooLarge)) => {
            return (StatusCode::PAYLOAD_TOO_LARGE, "upload exceeds size limit").into_response();
        }
        Ok(Err(UploadReadError::Io)) => {
            return (StatusCode::NOT_FOUND, "not found").into_response();
        }
        Err(_) => {
            return (StatusCode::INTERNAL_SERVER_ERROR, "upload read task failed").into_response();
        }
    };
    // 常见媒体扩展名 → content-type（未知回退 octet-stream）。
    // 单一来源：http_dispatcher::mime_for_extension（/ext 静态资源共用）。
    let content_type = crate::http_dispatcher::mime_for_extension(&ext);
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

    // P2 模式包资源透出：按需聚合（ADR 2026-10-15-mode-registry-retirement，
    // build_step_service_index 同型）——从活 manifests + 插件目录活真源现算，
    // 零注册状态（原 mode_registry 注册状态机已退役）。「禁用/卸载即同源消失」
    // 与热增删文件即时生效由数据流天然获得，无需刷新机器；列表按键字典序输出
    // 确定（HashMap 直序列化会让 ETag 内容寻址误变）；结构 additive 只增——
    // agent = {key, plugin_id, path}，pipeline 额外带 task_kinds（文件头路由
    // 标注，深语义由消费侧解释），路径统一字符串化。
    let (mode_agents, mode_pipelines) = {
        let manifests = state.manifests.read().await;
        let enabled_ids = state.enabled_plugin_ids.read().await;
        let dirs = state.plugin_dirs();
        crate::mode_view::aggregate_mode_resources(&manifests, &enabled_ids, &dirs)
    };

    SchemaResponse {
        agents,
        pipelines,
        tools,
        routes,
        plugin_configs,
        plugin_contributes,
        kernel_capabilities,
        mode_agents,
        mode_pipelines,
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
                "host_type": m.host_type,
            })
        })
        .collect();
    axum::Json(pipelines)
}

/// 管道域 REST 端点的租户上下文解析单点：统一「无 session_id」变体
/// （会话域端点携带 thread_id，在 session_routes 直接调用 request_tenant_ctx）。
/// async：tenant 解析需查 store（持久化用户的一用户一租户映射）；解析失败
/// 错误传播（K8 修订 ADR 2026-10-10），不降级 default。
async fn endpoint_tenant_ctx(
    state: &AppState,
    headers: &axum::http::HeaderMap,
) -> Result<agentos_core::types::TenantContext, agentos_http::error::ApiError> {
    crate::server::request_tenant_ctx(state.store.as_ref(), headers, "").await
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
    let tenant_ctx = endpoint_tenant_ctx(&state, &headers).await?;
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
    .map_err(ApiError::internal("list_pipelines join 失败"))?
    .map_err(ApiError::internal("list_pipelines 查询失败"))?;
    Ok(axum::Json(json!({ "items": rows })))
}

// ── 消息段端点（多代切换/压缩原文存档）─────────────────────────
// 段 = 替换事件的冻结内容（消息段模型 §5 读路径）：清单一次拉取（免解 blob），
// 展开/切换预览走单段端点解析成员全文。全部按租户隔离；不存在条目返回 404。

/// GET /api/v1/pipelines/{pipeline_id}/message-segments——段清单（无成员、含
/// preview，按 base_seq, created_at 升序）。
pub async fn message_segments_list_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path(pipeline_id): axum::extract::Path<String>,
    headers: axum::http::HeaderMap,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let store = state.store.as_ref().ok_or_else(|| ApiError::NotFound {
        message: "store not injected".to_string(),
    })?;
    let tenant_ctx = endpoint_tenant_ctx(&state, &headers).await?;
    let rows = store
        .list_message_segments(&pipeline_id, &tenant_ctx.tenant_id)
        .await
        .map_err(ApiError::internal("message-segments 查询失败"))?;
    Ok(axum::Json(json!({ "segments": rows })))
}

/// GET /api/v1/message-segments/{id}——单段（含成员全文解析）。
pub async fn message_segment_get_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path(id): axum::extract::Path<String>,
    headers: axum::http::HeaderMap,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let store = state.store.as_ref().ok_or_else(|| ApiError::NotFound {
        message: "store not injected".to_string(),
    })?;
    let tenant_ctx = endpoint_tenant_ctx(&state, &headers).await?;
    let segment = store
        .get_message_segment(&id, &tenant_ctx.tenant_id)
        .await
        .map_err(ApiError::internal("message-segment 查询失败"))?
        .ok_or_else(|| ApiError::NotFound {
            message: format!("message-segment 不存在: {id}"),
        })?;
    Ok(axum::Json(json!({ "segment": segment })))
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
    let tenant_ctx = endpoint_tenant_ctx(&state, &headers).await?;
    let rows = store
        .list_pending_inputs(&tenant_ctx.tenant_id, &pipeline_id)
        .await
        .map_err(ApiError::internal("pending-inputs 查询失败"))?;
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
    let tenant_ctx = endpoint_tenant_ctx(&state, &headers).await?;
    let updated = store
        .update_pending_input_content(&tenant_ctx.tenant_id, &pipeline_id, &input_id, content)
        .await
        .map_err(ApiError::internal("pending-inputs 修改失败"))?;
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
    let tenant_ctx = endpoint_tenant_ctx(&state, &headers).await?;
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
        .map_err(ApiError::internal("pending-inputs 删除失败"))?;
    if !deleted {
        return Err(ApiError::NotFound {
            message: format!("pending-inputs 条目不存在: {input_id}"),
        });
    }
    // 队列生命周期留痕（2026-10-21 装机排障）：此前删除成功零日志（仅失败路径
    // warn），条目蒸发不可归因、事件推送还造成"已发送"假象。剩余条数补读一次
    // 列表；读取失败记 -1（未知），不影响已成功的删除。
    let remaining = store
        .list_pending_inputs(&tenant_ctx.tenant_id, &pipeline_id)
        .await
        .map(|rows| rows.len() as i64)
        .unwrap_or(-1);
    tracing::info!(
        pipeline = %pipeline_id,
        input = %input_id,
        remaining = remaining,
        source = "REST",
        "pending-inputs 条目已删除"
    );
    if let Some(cmid) = cmid {
        crate::ws_session::notify_outcome_waiter(
            &cmid,
            crate::server::EngineOutcome {
                content: format!("pending-inputs 条目 {input_id} 已在排队中被删除"),
                final_assistant: None,
                failed: true,
                degraded: false,
                plugin_errors: Vec::new(),
                consumed_pending_cmids: Vec::new(),
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
    let tenant_ctx = endpoint_tenant_ctx(&state, &headers).await?;
    // 清空前读取全部条目（同 delete：解除排队中 REST chat 请求的挂起）。
    // fail-closed 同删除分支：读取失败跳过清空并报 503，不丢 cmid 挂死 waiter。
    let rows = store
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
        })?;
    let cmids: Vec<String> = rows.iter().map(|r| r.client_message_id.clone()).collect();
    let entry_ids: Vec<String> = rows.iter().map(|r| r.id.clone()).collect();
    let deleted = store
        .clear_pending_inputs(&tenant_ctx.tenant_id, &pipeline_id)
        .await
        .map_err(ApiError::internal("pending-inputs 清空失败"))?;
    // 队列生命周期留痕（同删除分支）：清空成功也必须有含 id 的日志痕迹。
    tracing::info!(
        pipeline = %pipeline_id,
        deleted = deleted,
        ids = %entry_ids.join(","),
        source = "REST",
        "pending-inputs 队列已清空"
    );
    for cmid in cmids {
        crate::ws_session::notify_outcome_waiter(
            &cmid,
            crate::server::EngineOutcome {
                content: format!("pending-inputs 队列已清空（{pipeline_id}）"),
                final_assistant: None,
                failed: true,
                degraded: false,
                plugin_errors: Vec::new(),
                consumed_pending_cmids: Vec::new(),
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
    // 统一管道标题（ADR 2026-10-27-pipeline-title-field）：启用方出生写入、
    // 会话域随标题变更刷新——内核零域知识，只透传落库与出口（运行域自有键，
    // 随基线出口、不依赖插件 export_fields 声明）。
    "title",
    // 实际状态内核持有（2026-09-03 双状态裁定）：可观测性的唯一运行状态
    // 真值，属内核运行域自有键，随基线出口、不依赖插件 export_fields 声明。
    "run_status",
    // 失败归因唯一真值载体（run 收尾 terminal_run_fields 落表）：不出口则
    // 面板只见 failed 无归因（2026-10-25 空回复耗尽排查实证）。未署名轮落
    // 空串防跨 run 残留，前端按非空渲染。
    "run_stop_reason",
    // 挂起归因唯一真值载体（G8 排空 suspend_running_runs 落表，P-23 片1）：
    // 写者是内核排空（无 manifest 可声明 export_fields），随基线出口——
    // 不出口则面板只见 suspended 无归因（内存自愈挂起用户不知道发生了
    // 什么）。非排空挂起（DSL 等待/审批挂起）不写该键，键缺席 = 无归因
    // 行可渲染；恢复（resume）即清。空串语义 = 未署名，前端按非空渲染。
    "suspended_reason",
    // 插件级失败的常驻镜像（ADR-2026-10-17-task-birth-workspace-inclusion）：
    // 引擎私有 `_plugin_errors` 是 staging 键、落库前被剥离，重启即失忆——
    // 镜像键随基线出口，启动对账（tasks/reconcile）与面板读面冷启动后仍可读。
    "run.plugin_errors",
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

    /// 从单个声明列表构造（`state.writes` 等按插件逐声明的校验面）。
    /// 条目语义同 `export_fields`：精确键 + `前缀.*` 通配。
    pub fn from_declarations<'a>(declarations: impl IntoIterator<Item = &'a String>) -> Self {
        let mut out = Self::default();
        for f in declarations {
            if let Some(prefix) = f.strip_suffix(".*") {
                out.prefixes.push(prefix.to_string());
            } else {
                out.exact.insert(f.clone());
            }
        }
        out
    }

    /// 键是否在出口声明内（精确命中或命中 `前缀.*` 通配）。
    pub fn contains(&self, key: &str) -> bool {
        self.exact.contains(key) || self.prefixes.iter().any(|p| key.starts_with(p.as_str()))
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
/// 冷读合并：最新 checkpoint 基线 + `pipeline_state` 表最新标量覆盖。
///
/// 顺序对齐 `stage_recover_history` 的冷恢复（checkpoint 标量 → pipeline_state
/// 表补充，表的最新值覆盖 checkpoint 里的出生/过期值，如 `task.status` pending →
/// completed）。registry 未命中（重启后未再轮）时 `/pipelines/state` 与
/// `pipeline-state.list` 的 DB 兜底共用；无 checkpoint 且表行为空返回 None。
/// 读取失败同样返回 None（任务树读面降级不崩，调用方跳过该行），但两类失败各留
/// warn 痕迹——与「确实无档」的 Ok(None) 可区分，静默缺行可从日志定位。
///
/// 无 checkpoint 但 `pipeline_state` 表有行时以表行为基线：running 中任务
/// interval 未到不会有 checkpoint，整行丢弃会看不到刚提交的任务（出生字段
/// 创建即落表，见 chat_send_handler 创建分支）。
pub(crate) async fn merge_cold_state(
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

/// 冷读行 = [`merge_cold_state`] 合并（checkpoint 基线 + pipeline_state 表最新
/// 标量覆盖）。运行状态 `run_status` 是表行簿记键，随合并行如实出口；投影缺失
/// 即如实缺键——读面不补写不伪造（谁终态谁随写，runs 退役收尾
/// ADR 2026-10-08-runs-retire-completion，旧 runs 表权威 overlay 已废）。
pub(crate) async fn cold_state_row(
    store: &std::sync::Arc<dyn agentos_core::traits::StorageBackend>,
    pipeline_id: &str,
    tenant_id: &str,
) -> Option<serde_json::Value> {
    merge_cold_state(store, pipeline_id, tenant_id).await
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
    let tenant_ctx = endpoint_tenant_ctx(&state, &headers).await?;
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
            "source": "memory",
            "state": summary,
        }));
    }

    // 1.5) DB 任务域镜像补齐（与 capability_router pipeline-state.list 同款语义）：
    // run 期间引擎只在本地内存推进 state，registry 快照拍在 stage_finalize（run
    // 收尾）——运行中的内存行停在出生/上次终态，llm_model / track.llm_usage /
    // context_window 等每轮投影键全部缺失，前端用量指示器拿不到任何模型信息。
    // 按 export 白名单从 pipeline_state 表补齐内存行缺失键（内存已有键不覆盖：
    // pipeline-state.update 热路径双写内存+表，两处一致的键无需仲裁）。
    if let Some(store) = state.store.as_ref() {
        for item in items.iter_mut() {
            if item.get("source").and_then(|v| v.as_str()) != Some("memory") {
                continue;
            }
            let Some(pid) = item.get("pipeline_id").and_then(|v| v.as_str()) else {
                continue;
            };
            let Ok(fields) = store.load_pipeline_state(pid, &tenant_id).await else {
                continue;
            };
            let Some(obj) = item.get_mut("state").and_then(|v| v.as_object_mut()) else {
                continue;
            };
            for (k, v) in fields {
                if export.contains(&k) && !obj.contains_key(&k) {
                    obj.insert(k, v);
                }
            }
        }
    }

    // 2) DB 冷数据兜底：运行簿记清单里 registry 未覆盖的管道读最新 checkpoint
    if let Some(db) = state.db.as_ref() {
        let store: std::sync::Arc<dyn agentos_core::traits::StorageBackend> = db.clone();
        let db = db.clone();
        let tid = tenant_id.clone();
        let rows = tokio::task::spawn_blocking(move || db.list_pipelines_inner(&tid, None, 200))
            .await
            .map_err(ApiError::internal("state list_pipelines join 失败"))?
            .map_err(ApiError::internal("state list_pipelines 查询失败"))?;
        for row in rows {
            let pid = match row.pipeline_id.as_deref() {
                Some(p) if !p.is_empty() => p.to_string(),
                _ => continue,
            };
            if seen.contains(&pid) {
                continue;
            }
            // 运行簿记每管道恰一条当前运行投影——首个命中后登记去重（防同管道
            // 重复出口）。
            seen.insert(pid.clone());
            // 冷兜底行 = 最新 checkpoint + pipeline_state 表最新标量覆盖（复用
            // cold_state_row：checkpoint 拍在终态回写前 → task.status 等完成态以
            // pipeline_state 表为准，重启后不再倒退回 pending）。运行状态
            // run_status 同源（表行簿记键）随合并行如实出口，投影缺失即如实
            // 缺键（谁终态谁随写，runs 退役收尾 ADR 2026-10-08）。
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
// （实现拆分：plugin_config.rs 配置读写 / plugin_lifecycle.rs 生命周期管理，
//   pub 项经 glob 再导出，`crate::routes::*` 引用路径不变）
mod plugin_config;
mod plugin_lifecycle;

pub use plugin_config::*;
pub use plugin_lifecycle::*;

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
        match db.suspend_running_runs(reason) {
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

/// GET /api/v1/system/memstats — mimalloc 分配器统计快照（M1 测量面）。
///
/// 内核自有只读端点：返回调用时刻的进程级 [`crate::allocator::MemStats`]。
/// 字段语义、release 构建下的可用性（malloc 计量面受 MI_STAT 门控恒 0，
/// 可信信号为 committed/purged/process 面）与 None 防御见 allocator 模块
/// 文档；分辨活增长 vs 分配器滞留 = 消费方对两次快照相减（mimalloc 无增量
/// delta API）。
pub async fn system_memstats_handler() -> axum::Json<crate::allocator::MemStats> {
    axum::Json(crate::allocator::snapshot_stats())
}

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

/// 解析管道配置文件落点：`pipelines/{name}.yaml`（用户空间优先）。
///
/// 本路由是**内核自有写面**（不经 manifest config_files）——故走
/// [`resolve_kernel_config_target`]：路径安全校验照旧，但不施加保留段 denylist
/// （`pipelines` 保留段的语义是"插件不得映射内核调度配置"，不是"用户不得经 UI
/// 编辑管道"）。落点解析仍走单一解析器：`Read` = 用户层文件存在即用，
/// `Write` = 一律写用户层——两侧各拼路径会让"保存了但加载的是另一份"。
fn pipeline_config_path(
    project_root: &std::path::Path,
    name: &str,
    mode: ConfigTargetMode,
) -> Result<std::path::PathBuf, ApiError> {
    resolve_kernel_config_target(project_root, &format!("pipelines/{name}.yaml"), mode)
        .map_err(config_err_to_api)
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
    let path = pipeline_config_path(&project_root, &name, ConfigTargetMode::Read)?;
    let raw = std::fs::read_to_string(&path).map_err(|e| ApiError::NotFound {
        message: format!("pipeline config read failed: {name}: {e}"),
    })?;
    let etag = compute_etag(raw.as_bytes());
    let data: serde_json::Value = serde_yaml::from_str(&raw)
        .map_err(ApiError::internal("pipeline config yaml parse error"))?;
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
    // 读落点＝用户空间优先（ETag 与 GET 同源——若此处用写落点，首次保存前
    // ETag 会基于"尚未存在的用户层文件"算空值，客户端拿到的 ETag 与所见内容不符）
    let read_path = pipeline_config_path(&project_root, &name, ConfigTargetMode::Read)?;

    // If-Match 乐观锁（A13）：必须匹配磁盘当前 ETag；文件不存在/不可读 → 404。
    let current_etag = match std::fs::read_to_string(&read_path) {
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

    // 写落点＝用户空间（ADR 2026-09-13-unified-user-root）：改动出仓，不再覆写
    // git 跟踪的 factory 文件。首次接管先按 factory 内容播种——用户拿到完整
    // 文件而非 diff 片段；不自动合并 factory 后续新增键（静默合并 = 两处存值）。
    let path = pipeline_config_path(&project_root, &name, ConfigTargetMode::Write)?;
    if let Err(e) = seed_user_config_from_factory(&project_root, &format!("pipelines/{name}.yaml"))
    {
        tracing::warn!(
            target: "pipeline-config",
            name = %name,
            error = %e,
            "播种 factory 管道配置失败，直接写用户层"
        );
    }

    // B4/B6：原子写 + round-trip 校验（复用 config_service）
    atomic_write_yaml(&path, &req.data).map_err(config_err_to_api)?;

    // 管道编译缓存失效（D9 版本钉住语义）：写盘成功即清该配置的"当前盘面
    // 最新版"指针——下次未钉取用（新实例出生轮）按盘面重编译新内容入缓存；
    // 历史版本产物保留不删（已钉实例恒用出生版本，PUT 热更只对之后出生的
    // 实例生效）。缓存无 mtime 校验，PUT 是唯一失效路径。
    crate::server::pipeline_cache_invalidate(&name);

    let new_etag = compute_etag(
        std::fs::read_to_string(&path)
            .map_err(ApiError::internal("re-read after write failed"))?
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
mod tdd4_config_center_tests;

#[cfg(test)]
mod state_summary_tests;

#[cfg(test)]
mod plugin_profile_write_tests;

#[cfg(test)]
mod pending_inputs_failure_tests;

#[cfg(test)]
mod cold_state_row_tests;

#[cfg(test)]
mod inline_field_updates_tests;

#[cfg(test)]
mod system_memstats_tests;

#[cfg(test)]
mod routes_http_handler_test;

#[cfg(test)]
mod app_state_builder_tests;
