//! Axum HTTP/WebSocket API 服务器
//!
//! 提供 RESTful API 端点和 WebSocket 流式通信。
//! AC-06-3: /health 返回 200
//! AC-06-4: WebSocket 可连接收发消息
//! AC-06-5: Schema 聚合端点
//!
//! [来源: docs/tasks/task_07_llm_api.md]

use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::OnceLock;
use std::time::{Duration, SystemTime};

use parking_lot::RwLock as ParkingRwLock;

use agentos_core::traits::{CapabilityRegistry, StorageBackend};
use agentos_core::types::{PipelineConfig, StepLibrary, TenantContext};
use axum::{
    body::Body,
    extract::{
        ws::{WebSocket, WebSocketUpgrade},
        Request, State,
    },
    http::{header, HeaderMap, HeaderValue, Method, StatusCode},
    middleware::{from_fn, from_fn_with_state, Next},
    response::{IntoResponse, Response},
    routing::{get, post, put},
    Router,
};

use crate::pipeline_loader::{load_pipeline_config, load_step_library, validate_no_name_conflicts};
use serde::{Deserialize, Serialize};
use tracing::{debug, error, info, warn};

use crate::auth::{login_handler, logout_handler, me_handler, refresh_handler, register_handler};
use crate::routes::{
    actions_execute_handler, get_pipeline_config_with_etag, get_plugin_config_with_etag,
    health_handler, metrics_prometheus_handler, pending_inputs_clear_handler,
    pending_inputs_delete_handler, pending_inputs_list_handler, pending_inputs_update_handler,
    pipelines_handler, pipelines_runs_handler, pipelines_state_handler,
    plugins_contract_status_handler, plugins_set_enabled_handler, plugins_status_handler,
    put_pipeline_config_handler, put_plugin_config_handler, schema_handler, serve_upload_handler,
    system_restart_handler, tools_handler, validate_all_plugins_handler, AppState,
};
use crate::session_routes::{
    create_session_handler, delete_session_handler, list_session_messages_handler,
    list_sessions_handler, sessions_schema_handler, update_session_agent_handler,
    update_session_handler,
};
use agentos_http::auth::resolve_request_tenant_id;
use agentos_http::error::ApiError;
use agentos_session::router::PipelineDispatcher;

/// WebSocket 消息请求体。
#[derive(Debug, Deserialize, Serialize)]
pub struct WsRequest {
    #[serde(default)]
    pub message: String,
    #[serde(default)]
    pub session_id: String,
    /// 可选对话历史（多轮上下文）。客户端传入前几轮的 messages（OpenAI 格式），
    /// 内核注入 state.messages 供 LLM 看到上下文。0.2 内核暂不自动持久化历史，
    /// 由客户端维护会话历史并每轮带上（与 0.1 文件存储的按 session 加载等价）。
    #[serde(default)]
    pub history: Vec<serde_json::Value>,
    /// 可选 agent_id（默认 agentos）。指定执行 agent（如 general_agent 触发 bash 隔离）。
    #[serde(default)]
    pub agent_id: String,
}

/// WebSocket 消息响应体。
#[derive(Debug, Serialize)]
pub struct WsResponse {
    pub r#type: String,
    pub content: String,
    pub session_id: String,
    pub timestamp: String,
    /// 降级应答标记（echo_fallback；统一错误模型：假成功显式化）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub degraded: Option<bool>,
}

/// 构建 API 路由树。
///
/// P3（ADR §3.3）：内核静态路由 + 扫描 `http_routes` 动态挂载插件端点。
/// 动态端点统一走 `dispatch_http`（raw body/headers 透传 + 插件自定义响应 +
/// per-endpoint timeout/concurrency）。
pub fn build_router(state: AppState) -> Router {
    let static_router = Router::new()
        // AC-06-3: 健康检查
        .route("/health", get(health_handler))
        // 上传文件静态服务（channel_api artifacts 上传的媒体；前端附件预览/
        // 背景图引用 /uploads/... URL）
        .route("/uploads/{filename}", get(serve_upload_handler))
        // AC-06-5: Schema 聚合端点
        .route("/api/v1/schema", get(schema_handler))
        // agent 管理面已插件化：/api/v1/agents* 4 路由迁至
        // agent_manager 插件 /ext/agent_manager/agents*（http_endpoints 承载，
        // 掩码/etag/乐观锁/.bak 语义一项不丢，见 2026-08-20 ADR）。
        // 阶段3:命令执行统一出口(前端 GrowthLoop.ts commandDispatcher transport 注入此端点)
        // 命令面板/快捷键/菜单触发 → 查找声明该 command 的插件 → 执行或占位
        .route("/api/v1/actions/execute", post(actions_execute_handler))
        .route("/api/v1/pipelines", get(pipelines_handler))
        .route("/api/v1/pipelines/runs", get(pipelines_runs_handler))
        .route("/api/v1/pipelines/state", get(pipelines_state_handler))
        // pending 输入队列（ADR-2026-08-26）：等待窗口内查询/修改/删除/清空
        .route(
            "/api/v1/pipelines/{pipeline_id}/pending-inputs",
            get(pending_inputs_list_handler).delete(pending_inputs_clear_handler),
        )
        .route(
            "/api/v1/pipelines/{pipeline_id}/pending-inputs/{input_id}",
            put(pending_inputs_update_handler).delete(pending_inputs_delete_handler),
        )
        .route("/api/v1/tools", get(tools_handler))
        // P7: 管道配置查询/更新（内核承载 config/pipelines/*.yaml）
        .route(
            "/api/v1/config/pipelines/{name}",
            get(get_pipeline_config_with_etag).put(put_pipeline_config_handler),
        )
        // P1-4: 插件配置读写端点（manifest config_files 映射）
        .route(
            "/api/v1/plugins/{id}/config/{file_id}",
            get(get_plugin_config_with_etag).put(put_plugin_config_handler),
        )
        // 会话管理原生端点（task_kernel_cleanup_and_split 任务 2：compat threads 转正
        // 为 /api/v1/sessions*，实现与响应形状不变——前端 mappers 契约无感知）
        .route(
            "/api/v1/sessions",
            get(list_sessions_handler).post(create_session_handler),
        )
        .route("/api/v1/sessions/schema", get(sessions_schema_handler))
        .route(
            "/api/v1/sessions/{id}",
            axum::routing::patch(update_session_handler).delete(delete_session_handler),
        )
        .route(
            "/api/v1/sessions/{id}/agent",
            axum::routing::patch(update_session_agent_handler),
        )
        .route(
            "/api/v1/sessions/{id}/messages",
            get(list_session_messages_handler),
        )
        // 插件监管端点（compat plugins/status 转正；history/reload* 死端点已删除）
        .route("/api/v1/plugins", get(plugins_status_handler))
        // G2：双写一致性全量巡检（manifest 声明 vs sidecar 实际暴露）
        .route(
            "/api/v1/plugins/validate-all",
            axum::routing::post(validate_all_plugins_handler),
        )
        // 闸2·观测：每插件契约状态（只读账本；三线会师——契约校验 × 三通道 × 面板）
        .route(
            "/api/v1/plugins/contract-status",
            get(plugins_contract_status_handler),
        )
        // G8 优雅重启（admin）：排空 running runs → exit 75，监督者拉起新进程
        .route(
            "/api/v1/system/restart",
            axum::routing::post(system_restart_handler),
        )
        .route(
            "/api/v1/plugins/{id}/enabled",
            axum::routing::put(plugins_set_enabled_handler),
        )
        // 监控 M5b：Prometheus 导出端点保留内核（运维契约：抓取方通常不鉴权且
        // URL 稳定优先，boot-plugin 立项 §二/第三刀决策）。查询面 /api/v1/metrics
        // 已迁 boot-plugin 第三刀：HTTP 面在 plugins/shared/metrics_admin 插件
        // （/ext/metrics_admin/query），能力层 metrics-admin handler 在
        // metrics/capability.rs（agentos-kernel.rs 启动期注册）。
        .route("/metrics", get(metrics_prometheus_handler))
        // AC-06-4: WebSocket 端点（前端写死连 /ws/chat，0.1 路径格式）。
        .route("/ws/chat", get(ws_handler))
        // 消息发送端点（REST fallback for WS）
        .route("/api/v1/chat", post(chat_handler))
        // 人类交互响应端点——前端用户操作经此提交，内核转发到交互插件的 interaction.respond
        .route(
            "/api/v1/interaction/response",
            post(interaction_response_handler),
        )
        // Auth 端点
        .route("/api/v1/auth/login", post(login_handler))
        .route("/api/v1/auth/me", get(me_handler))
        .route("/api/v1/auth/refresh", post(refresh_handler))
        .route("/api/v1/auth/logout", post(logout_handler))
        .route("/api/v1/auth/register", post(register_handler));
    // 统一通用数据接口（task_01）已迁移为 boot-plugin（第一刀）：
    // /api/v1/db/* 的 axum nest 已摘除——SQL 能力层在 agentos-db-admin crate 的
    // capability handler（agentos-kernel.rs 注册 db-admin namespace），HTTP 面在
    // plugins/shared/db_admin 插件（/ext/db_admin/** 经 dispatcher 通配分发）。

    // P3：动态挂载插件 HTTP 端点（http_routes → dispatcher）
    let router =
        crate::http_dispatcher::build_router_with_http_routes(state.clone(), static_router);
    // F-API-1：配置写入面 + 会话/插件生命周期鉴权（白名单路径 + method 分写/读）。
    let auth_layer = from_fn_with_state(state.clone(), write_surface_auth);
    // CORS：开发期前端通过 VITE_API_BASE_URL 直连内核（http://localhost:9100），
    // 浏览器跨域请求需要预检（OPTIONS）+ 响应头带 Access-Control-Allow-Origin。
    // 作为最外层中间件，拦截 OPTIONS 预检并给所有响应注入 CORS 头。
    router
        .with_state(state)
        .layer(auth_layer)
        .layer(from_fn(cors_middleware))
}

/// F-API-1：配置写入面 + 会话/插件生命周期鉴权中间件。
///
/// 按路径白名单 + method 区分：写面（POST/PUT/PATCH/DELETE）→ require_surface_role
/// （admin）；读面（GET）→ require_surface_role（admin/viewer）。白名单覆盖：
/// - `/api/v1/sessions*`（会话 CRUD）
/// - `/api/v1/plugins*`（status/enabled/config；注意 /api/v1/plugins 裸路径也需鉴权）
/// - `/api/v1/actions/execute`、`/api/v1/interaction/response`
/// - `PUT /api/v1/config/pipelines/{name}`
/// - `POST /api/v1/chat`（0.2 收紧：原来放行匿名——消息触发管道执行/落库，
///   属写面语义，未鉴权等于任意人可驱动执行与消耗算力）
///
/// 其余路径放行（/health、/api/v1/auth/*、/ws、/api/v1/db/* 等；agent 配置写面
/// 已插件化——/ext/agent_manager 的 PUT admin 闸由插件自持，见 2026-08-20 ADR）。
async fn write_surface_auth(
    State(state): State<AppState>,
    req: Request,
    next: Next,
) -> Result<Response, ApiError> {
    let path = req.uri().path();
    let method = req.method().clone();

    let needs_auth = path.starts_with("/api/v1/sessions")
        || path.starts_with("/api/v1/plugins")
        || path == "/api/v1/system/restart"
        || path == "/api/v1/actions/execute"
        || path == "/api/v1/interaction/response"
        || path == "/api/v1/chat"
        || (path.starts_with("/api/v1/config/pipelines/") && method == Method::PUT);

    if !needs_auth {
        return Ok(next.run(req).await);
    }

    let result = match method {
        Method::GET => require_surface_role(&state, req.headers(), false).await,
        _ => require_surface_role(&state, req.headers(), true).await,
    };
    match result {
        Ok(_) => Ok(next.run(req).await),
        Err(e) => Err(e),
    }
}

/// 会话/插件生命周期白名单面的角色校验（原 db_routes::require_read_role /
/// require_admin_role 的 api 侧等价实现——db_routes 已拆至 db-admin crate，
/// 此处用 api 自身的 resolve_request_user（agentos-http 单一实现），
/// 语义与错误消息保持与拆分前一致）。
async fn require_surface_role(
    state: &AppState,
    headers: &HeaderMap,
    write: bool,
) -> Result<(), ApiError> {
    let (_, _, role, _) =
        agentos_http::auth::resolve_request_user(state.store.as_ref(), headers).await?;
    let ok = if write {
        role == "admin"
    } else {
        role == "admin" || role == "viewer"
    };
    if ok {
        Ok(())
    } else if write {
        Err(ApiError::Forbidden {
            message: "写操作需要 admin 角色".to_string(),
        })
    } else {
        Err(ApiError::Forbidden {
            message: "需要 admin 或 viewer 角色".to_string(),
        })
    }
}

/// CORS 中间件：拦截 OPTIONS 预检（返回 204 + CORS 头），并给所有响应注入 CORS 头。
/// 反射请求 Origin（开发友好，支持任意 localhost/自定义前端源），允许凭据。
async fn cors_middleware(req: Request, next: Next) -> Response {
    let origin = req
        .headers()
        .get(header::ORIGIN)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string());

    // 预检请求：直接回 204 + CORS 头，不进入业务路由（业务路由未注册 OPTIONS 会 405）
    if req.method() == Method::OPTIONS {
        let mut resp = Response::builder()
            .status(StatusCode::NO_CONTENT)
            .body(Body::empty())
            .expect("empty cors preflight body");
        apply_cors_headers(resp.headers_mut(), origin.as_deref());
        return resp;
    }

    let mut resp = next.run(req).await;
    apply_cors_headers(resp.headers_mut(), origin.as_deref());
    resp
}

/// 判断 origin 是否为本地开发源（localhost / 127.0.0.1 / [::1]，任意端口）。
///
/// base 后必须紧跟 `:`（端口分隔），防止 `localhost.evil.com` 这类前缀绕过。
fn is_local_origin(origin: &str) -> bool {
    const LOCAL_BASES: [&str; 6] = [
        "http://localhost",
        "https://localhost",
        "http://127.0.0.1",
        "https://127.0.0.1",
        "http://[::1]",
        "https://[::1]",
    ];
    for base in LOCAL_BASES {
        if origin == base {
            return true;
        }
        if let Some(rest) = origin.strip_prefix(base) {
            if rest.starts_with(':') {
                return true;
            }
        }
    }
    false
}

/// 精确匹配 origin 是否在生产白名单中（不做子域/前缀模糊匹配）。
fn origin_matches_allowlist(origin: &str, allowlist: &[&str]) -> bool {
    allowlist.iter().any(|o| *o == origin)
}

/// 判断 origin 是否被放行：本地源任意端口，或命中 `AGENTOS_CORS_ORIGINS` 白名单。
///
/// 生产白名单由环境变量 `AGENTOS_CORS_ORIGINS` 提供（逗号分隔的完整 origin，
/// 如 `https://app.example.com,https://www.example.com`）。未配置时仅放行本地源。
fn is_origin_allowed(origin: &str) -> bool {
    if is_local_origin(origin) {
        return true;
    }
    if let Ok(raw) = std::env::var("AGENTOS_CORS_ORIGINS") {
        let entries: Vec<&str> = raw
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .collect();
        return origin_matches_allowlist(origin, &entries);
    }
    false
}

/// 注入 CORS 响应头。仅当 origin 被白名单放行时才反射 Origin 并允许凭据——
/// 反射任意 Origin + Allow-Credentials 会让任意站点带凭据跨域调用内核 API。
/// 其余头（Methods/Headers/Max-Age）固定。
fn apply_cors_headers(headers: &mut HeaderMap, origin: Option<&str>) {
    if let Some(o) = origin {
        if is_origin_allowed(o) {
            if let Ok(v) = HeaderValue::from_str(o) {
                headers.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, v);
            }
            headers.insert(
                header::ACCESS_CONTROL_ALLOW_CREDENTIALS,
                HeaderValue::from_static("true"),
            );
        }
    }
    headers.insert(
        header::ACCESS_CONTROL_ALLOW_METHODS,
        HeaderValue::from_static("GET, POST, PUT, PATCH, DELETE, OPTIONS"),
    );
    headers.insert(
        header::ACCESS_CONTROL_ALLOW_HEADERS,
        HeaderValue::from_static(
            "Content-Type, Authorization, X-Requested-With, X-Auth-Token, Accept, Origin",
        ),
    );
    headers.insert(
        header::ACCESS_CONTROL_MAX_AGE,
        HeaderValue::from_static("86400"),
    );
}

/// WebSocket 连接处理器（AC-06-4）。
///
/// 生产恒走 P2 内核化会话路径（握手鉴权 + 连接注册 + 入站路由）；
/// session/路由未装配（仅测试构造场景）时显式 503，不静默降级 echo。
async fn ws_handler(
    ws: WebSocketUpgrade,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
    State(state): State<AppState>,
) -> impl IntoResponse {
    let Some(session) = state.session else {
        warn!("WS 连接被拒：AppState 未装配 session（仅测试构造会走到）");
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    let Some(router) = state.inbound_router else {
        warn!("WS 连接被拒：AppState 未装配 inbound_router（仅测试构造会走到）");
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    let token = params.get("token").cloned();
    // B3：前端重连时上报 last_sequence（全局 watermark），用于首个 thread 注册时回放断线期间事件。
    let last_sequence = params
        .get("last_sequence")
        .and_then(|s| s.parse::<u64>().ok());
    ws.on_upgrade(move |socket| run_p2_ws_session(socket, session, router, token, last_sequence))
}

/// P2 内核化 WS 会话包装：握手鉴权 + 会话运行 + 拒绝时 accept+close。
async fn run_p2_ws_session(
    socket: WebSocket,
    session: Arc<agentos_session::SessionCoordinator>,
    router: Arc<agentos_session::router::InboundRouter>,
    token: Option<String>,
    last_sequence: Option<u64>,
) {
    let mut user_id = None;
    let (code, reason) = crate::ws_session::run_ws_session(
        socket,
        session,
        router,
        token.as_deref(),
        &mut user_id,
        last_sequence,
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
/// async：tenant 解析需查 store（持久化用户的一用户一租户映射）。
pub(crate) async fn request_tenant_ctx(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    headers: &HeaderMap,
    session_id: &str,
) -> TenantContext {
    TenantContext::new(resolve_request_tenant_id(store, headers).await, session_id)
}

// 通过 0.2 配置驱动管道引擎处理消息。
//
// 构造 [`agentos_engine::PipelineExecutor`]，读取 AppState 中的 `pipeline_config`
// + `step_library`，按 YAML 定义的 step 顺序执行（三级命中规则）。
//
// 流程：
// 1. 构造初始 state（含 `message` / 默认 `agent_id` / `core_type` 等）
// 2. 注入工具 schema（按 state.tool_ids 或 agent yaml 的 tool_ids 过滤；
//    agent 全量配置由 context_build 插件在管道内自持加载）
// 3. 构造 PipelineExecutor 并执行 `run`
// 4. 从最终 state 提取响应（优先 `raw_result`，回退 `message`，再回退原消息）
//
// 降级条件：AppState 缺少 invoker / store / project_root（典型为测试或老式构造）
// 时走 echo-fallback，标注降级原因。

/// 默认核心管道插件 id（可被 agent 配置 config/agents/<id>.yaml 的 core_plugin 覆盖）。
/// 提取为常量，便于发现与替换。
const DEFAULT_CORE_PLUGIN: &str = "pipeline_llm_core";

// 冷路径回放从该 pipeline 的 step 级轨迹按序 merge 重建完整 state（含 messages），
// 不再特化只读 messages 表——轨迹颗粒度即恢复边界。

/// 管道配置热重载的 mtime 检测 TTL：1 秒内不重复 stat，避免高频 chat 时反复解析 YAML。
const PIPELINE_CONFIG_TTL: Duration = Duration::from_secs(1);

/// 热重载检测的全局缓存：记录上次检测时刻 + 上次配置文件的 mtime 指纹 +
/// 上次编译产物（G10：指纹不变时直接复用，零重编译）。
/// 进程级单例（OnceLock），所有 chat 请求共享，避免每次都重新 stat/解析/编译。
#[derive(Clone)]
struct ConfigReloadState {
    last_check: std::time::Instant,
    last_fingerprint: u64,
    compiled: Option<Arc<agentos_engine::compiler::CompiledPipeline>>,
}

/// 进程级单例缓存：按 config_root 隔离（测试多根并发、生产单根）。
/// 键 = 配置根目录绝对路径——不同 config_root（如各测试的临时目录）互不污染。
static CONFIG_RELOAD_CACHE: OnceLock<ParkingRwLock<HashMap<PathBuf, ConfigReloadState>>> =
    OnceLock::new();

fn config_reload_cache() -> &'static ParkingRwLock<HashMap<PathBuf, ConfigReloadState>> {
    CONFIG_RELOAD_CACHE.get_or_init(|| ParkingRwLock::new(HashMap::new()))
}

/// 计算管道配置的 mtime 指纹：autonomous.yaml + config/steps/ 目录下所有 .yaml。
///
/// 用 mtime 而非内容 hash：stat 是微秒级，热路径可接受；mtime 精度足够捕获配置修改。
/// 任一配置文件 mtime 变化 → 指纹变化 → 触发重载。
fn compute_config_fingerprint(config_root: &std::path::Path) -> u64 {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::Hasher;
    let mut hasher = DefaultHasher::new();

    let mtime_secs = |p: &std::path::Path| -> u64 {
        std::fs::metadata(p)
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(SystemTime::UNIX_EPOCH).ok())
            .map(|d| d.as_secs())
            .unwrap_or(0)
    };

    // autonomous.yaml（核心管道配置）
    let pipeline_yaml = config_root.join("pipelines").join("autonomous.yaml");
    hasher.write(b"autonomous:");
    hasher.write_u64(mtime_secs(&pipeline_yaml));

    // config/steps/ 目录（公共 step 库）
    let steps_dir = config_root.join("steps");
    if let Ok(entries) = std::fs::read_dir(&steps_dir) {
        let mut paths: Vec<_> = entries
            .filter_map(|e| e.ok())
            .filter_map(|e| {
                let p = e.path();
                if p.is_file()
                    && (p.extension().and_then(|x| x.to_str()) == Some("yaml")
                        || p.extension().and_then(|x| x.to_str()) == Some("yml"))
                {
                    Some((p.file_name()?.to_string_lossy().to_string(), mtime_secs(&p)))
                } else {
                    None
                }
            })
            .collect();
        paths.sort_by(|a, b| a.0.cmp(&b.0));
        for (name, mtime) in paths {
            hasher.write(name.as_bytes());
            hasher.write(b":");
            hasher.write_u64(mtime);
        }
    }

    hasher.finish()
}

/// 已知插件 id 的**活集合**：从共享 manifests store 现读（watcher 热发现的
/// 增量合并即时可见），供管道编译的命中规则③与 executor 的 step 解析使用——
/// 新插件热注册后，管道 YAML 引用其 id 即可编译解析，无需重启内核。
async fn live_plugin_ids(state: &AppState) -> std::collections::HashSet<String> {
    state
        .manifests
        .read()
        .await
        .iter()
        .map(|m| m.id.clone())
        .collect()
}

/// Pull 热加载：按需重载并**编译**管道配置（G10 生产路径）。
///
/// 每次 `process_via_engine` 执行前调用。返回本次执行应使用的
/// [`CompiledPipeline`]（启动期编译的语义，运行时零解析）。
/// 策略（双层短路 + fail-safe）：
/// 1. **TTL 门**：距上次检测不足 `PIPELINE_CONFIG_TTL`（1s）→ 直接返回缓存编译产物。
/// 2. **指纹比对**：TTL 过期后 stat autonomous.yaml + steps 目录 mtime，与缓存比对。
///    相同 → 返回缓存产物（没更新）；不同 → 重新加载 + 校验 + 编译，
///    成功返回新产物并更新缓存，失败（坏 YAML / 命名冲突 / 编译错误）则保留旧
///    产物 + 记 warn（不 panic，对照启动期 fail-fast 的降级版）。
///
/// 快照语义：在途 run 持调用时拿到的 `Arc` 跑完，新 run 取新产物——热重载对
/// 正在执行的管道零影响。失败安全：任何 IO/解析/编译错误都回退到旧产物，
/// 绝不因热重载让 chat 不可用。
async fn maybe_reload_compiled_pipeline(
    state: &AppState,
    config_root: &std::path::Path,
) -> Arc<agentos_engine::compiler::CompiledPipeline> {
    let now = std::time::Instant::now();
    // TTL 门 + 指纹比对在同一把读锁下快照（按 config_root 取，测试多根并发隔离）
    let (needs_check, cached) = {
        let cache = config_reload_cache().read();
        match cache.get(config_root) {
            None => (true, None),
            Some(c) => (
                now.duration_since(c.last_check) >= PIPELINE_CONFIG_TTL,
                c.compiled.clone(),
            ),
        }
    };
    if !needs_check {
        return cached.unwrap_or_else(empty_compiled);
    }

    let new_fp = compute_config_fingerprint(config_root);
    let (reload, cached_compiled) = {
        let mut cache = config_reload_cache().write();
        let (stale, compiled) = match cache.get(config_root) {
            None => (true, None),
            Some(c) => (c.last_fingerprint != new_fp, c.compiled.clone()),
        };
        // 无论是否重载都刷新检测时刻；指纹更新到 new_fp
        cache.insert(
            config_root.to_path_buf(),
            ConfigReloadState {
                last_check: now,
                last_fingerprint: new_fp,
                compiled: compiled.clone(),
            },
        );
        (stale, compiled)
    };

    if !reload {
        return cached_compiled.unwrap_or_else(empty_compiled);
    }

    // 指纹变化 → 重新加载 + 校验 + 编译（G10：when AST / 引用 / 环在加载期暴露）
    info!("Pipeline config changed on disk, reloading + recompiling...");
    let known_ids = live_plugin_ids(state).await;
    let recompiled = load_and_compile(config_root, &known_ids);
    let new_compiled = match recompiled {
        Ok(c) => {
            info!(
                pipeline = %c.name,
                bodies = c.bodies.len(),
                "Pipeline config hot-reloaded and compiled successfully"
            );
            Arc::new(c)
        }
        Err(e) => {
            warn!(error = %e, "Hot reload: pipeline recompile failed, keeping old compiled config");
            return cached_compiled.unwrap_or_else(empty_compiled);
        }
    };
    // 编译成功 → 原子更新缓存（后续请求零重编译）。
    // 注意：读锁快照必须先落变量再释放——`if let Some(x) = lock.read().clone()` 的
    // scrutinee 临时值（读锁 guard）生命周期会延长到 if 块结束，块内拿写锁即
    // 同线程死锁（读锁未释放等写锁）。先求值再拿写锁，锁严格分离。
    let cached_snapshot = config_reload_cache().read().get(config_root).cloned();
    if let Some(mut c) = cached_snapshot {
        c.compiled = Some(Arc::clone(&new_compiled));
        config_reload_cache()
            .write()
            .insert(config_root.to_path_buf(), c);
    }
    new_compiled
}

/// 加载 + 校验 + 编译管道（供热重载与启动期 fail-fast 复用）。
///
/// 任一步失败返回 Err（含上下文）——启动期调用方据此 panic 拒绝启动，
/// 热重载调用方据此保留旧产物。
pub fn load_and_compile(
    config_root: &std::path::Path,
    plugin_ids: &std::collections::HashSet<String>,
) -> Result<agentos_engine::compiler::CompiledPipeline, String> {
    use agentos_engine::compiler::compile_pipeline;
    let pipeline =
        load_pipeline_config(config_root).map_err(|e| format!("加载管道配置失败: {e}"))?;
    let steps = load_step_library(config_root).map_err(|e| format!("加载公共 step 库失败: {e}"))?;
    validate_no_name_conflicts(&pipeline, &steps, plugin_ids)
        .map_err(|conflict| format!("命名冲突: {conflict}"))?;
    compile_pipeline(&pipeline, &steps, plugin_ids).map_err(|e| e.to_string())
}

/// 空编译产物（配置缺失/首次编译失败时的安全降级：空管道执行，语义与
/// "缺省配置下内核可启动"一致）。
fn empty_compiled() -> Arc<agentos_engine::compiler::CompiledPipeline> {
    use agentos_engine::compiler::compile_pipeline;
    let empty = PipelineConfig::default();
    Arc::new(
        compile_pipeline(
            &empty,
            &StepLibrary::default(),
            &std::collections::HashSet::new(),
        )
        .expect("空管道编译必然成功"),
    )
}

/// 引擎执行结果。
///
/// - `content`：纯文本回复（raw_result/message 提取），HTTP/回退路径直接消费；
/// - `final_assistant`：本轮最终 assistant 消息的完整持久形态（含
///   `tool_calls`/`reasoning_content`/`seq`），WS 路径 new_message 携带它，
///   前端用与 DB 加载相同的 mapper 生成 parts——流式事件与 DB 冷热同构；
/// - `final_user`：本轮 user 消息的完整持久形态（含 `seq`/`metadata`，
///   record_id=compute_message_id 指纹，与表侧一致），WS 路径 new_message 携带
///   它做乐观 user 认领回传（ADR 2026-08-22 双字段范式）；
/// - `failed`：executor.run 返回 Err（WS 路径据此推 stream_error 收尾）。
pub(crate) struct EngineOutcome {
    pub content: String,
    pub final_assistant: Option<serde_json::Value>,
    pub final_user: Option<serde_json::Value>,
    pub failed: bool,
    /// 降级应答标记（前置依赖缺失 echo_fallback）：调用方据此与正常回复区分，
    /// 不再把降级应答当成功处理（2026-08-26 统一错误模型：假成功显式化）。
    pub degraded: bool,
}

// 技术债（同 ROADMAP 已知技术债表 PLR091x 治理方式）：多参转发函数，
// 收尾时统一收敛参数结构体。
#[allow(clippy::too_many_arguments)]
pub(crate) async fn process_via_engine(
    state: &AppState,
    message: &str,
    agent_id: &str,
    history: &[serde_json::Value],
    pipeline_id: &str,
    thread_id: &str,
    message_id: &str,
    user_id: &str,
    thinking_strength: &str,
    execution_context: Option<&serde_json::Value>,
    state_overlay: Option<&serde_json::Value>,
    client_message_id: &str,
) -> EngineOutcome {
    // Box::pin 到堆上：回写段 + executor.run 的深 sidecar 调用链让 Future 状态机
    // 在 release 下也接近 tokio worker 2MB 栈极限，堆分配规避溢出。
    Box::pin(process_via_engine_inner(
        state,
        message,
        agent_id,
        history,
        pipeline_id,
        thread_id,
        message_id,
        user_id,
        thinking_strength,
        execution_context,
        state_overlay,
        client_message_id,
    ))
    .await
}

#[inline(never)]
// 技术债（同上）：内部转发实现，随外层一并收敛。
#[allow(clippy::too_many_arguments)]
async fn process_via_engine_inner(
    state: &AppState,
    message: &str,
    agent_id: &str,
    _history: &[serde_json::Value],
    pipeline_id: &str,
    thread_id: &str,
    message_id: &str,
    user_id: &str,
    thinking_strength: &str,
    execution_context: Option<&serde_json::Value>,
    state_overlay: Option<&serde_json::Value>,
    client_message_id: &str,
) -> EngineOutcome {
    // ── 前置依赖：invoker / store / project_root 任一缺席 → echo 降级 ──
    let Some(invoker) = state.invoker.clone() else {
        return echo_fallback("engine not available", message);
    };
    let Some(store) = state.store.clone() else {
        return echo_fallback("store not available", message);
    };
    let Some(project_root) = state.project_root.clone() else {
        return echo_fallback("project_root not configured", message);
    };

    // 0. 统一管道查询键：pipeline_id 为空（HTTP 路径不传 / 旧 WS handler）时
    //    回退 thread_id，保证 messages 表 pipeline_id 列 + registry 键落到同一维度。
    //    WS 路径（ws_session.rs）已用 route_id（pipeline_id 空时回退 thread_id）。
    let effective_pipeline_id = if pipeline_id.is_empty() {
        thread_id
    } else {
        pipeline_id
    };
    let tenant =
        agentos_tenant::current().unwrap_or_else(|| TenantContext::new("default", "kernel"));
    let tenant_id = tenant.tenant_id.clone();

    // run_id 权威生成点：注入 initial_state（插件侧 llm_core 取消轮询的定位锚，
    // 见 聊天中断保留与重新生成回退方案 §四.1），并贯穿到 executor 构造——同一
    // run 的两个消费面（state 轮询锚 vs runs 表落库）共享一个 uuid。
    let run_id = uuid::Uuid::new_v4().to_string();

    // 1/1a/1a2/1a3. 初始 state 构造（含会话级/任务级 execution_context 注入 +
    // 自由 state overlay）。
    let initial_state = stage_build_initial_state(
        &store,
        message,
        agent_id,
        effective_pipeline_id,
        thread_id,
        message_id,
        user_id,
        thinking_strength,
        execution_context,
        state_overlay,
        &run_id,
    )
    .await;

    // 1b. 多轮上下文装配（热路径 registry / 冷路径 checkpoint+traces+state+messages）
    //     + 本轮 user 消息入账（metadata 携带 client_message_id 幂等键，
    //     ADR 2026-08-21：随消息落库回显，前端据此对账去重乐观消息）。
    //     skip_user_append：regenerate 重跑标志（批次 D，overlay 顶层键），
    //     目标 user 消息已在截断后历史中，本轮不重复 append。
    let skip_user_append = state_overlay
        .and_then(|o| o.get("_skip_user_append"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let mut initial_state = stage_recover_history(
        initial_state,
        &store,
        message,
        thread_id,
        effective_pipeline_id,
        &tenant_id,
        client_message_id,
        skip_user_append,
    )
    .await;

    // 2/2b. agent 配置加载 + 工具 schema 注入。
    stage_inject_agent_and_tools(state, &mut initial_state, agent_id, &project_root);

    // 3. 构造 PipelineExecutor 并执行；失败（含 run 标记 Failed 的兜底）直接
    //    以失败 outcome 出口。
    let final_state = match stage_execute(
        state,
        invoker,
        project_root,
        tenant,
        store,
        initial_state,
        agent_id,
        message,
        &run_id,
    )
    .await
    {
        Ok(s) => s,
        Err(outcome) => return outcome,
    };

    // 3b/4. registry 回写 + 响应提取。
    let outcome = stage_finalize(
        &final_state,
        &tenant_id,
        effective_pipeline_id,
        thread_id,
        agent_id,
    );
    // GAP-2：run 终态域事件（completed/suspended）——state 带 task.* 时派生
    // task_completed（EVENT 触发器输入源）。fire-and-forget，不影响响应。
    // 注：任务状态（task.status/ended_at）由任务域插件裁决写入（task_evaluate
    // 评估终态经 pipeline-state.update 落 state），内核只广播 run 终态事件，
    // 不写任务状态（职责边界：内核只管管道运行域，任务状态由任务域插件裁决）。
    emit_run_terminal_domain_events(state, &final_state, outcome.failed).await;
    outcome
}

/// GAP-2：从 run 终态 state 派生域事件（run.* 终态 + 任务域派生）。
///
/// - `failed=true` → `run.failed`（state 带 `task.*` 字段时追加 `task_failed`）
/// - `suspended` 标志（RouteNext::Wait 落档）→ `run.suspended`（等待人工交互，
///   无任务派生——收口把关是 child_task_guard 的结构性职责）
/// - 否则 → `run.completed`（state 带 `task.*` 字段时追加 `task_completed`）
///
/// 任务派生判据：state 存在 `task.` 前缀键（task = pipeline 单一真值，
/// 任务管道的 state 出生即带 task.* 字段；`task.owned.*` 是父管道登记子任务的
/// 扁平键，不算任务管道自身标记——仅登记过子任务的聊天管道不得派生任务事件）。
/// 事件经 [`broadcast_domain_event`] 双通道投递：观察总线 + 点对点推给声明
/// domain_event hook 的订阅插件（triggers_ext → evaluate_event——EVENT 触发器
/// 的输入源）。
///
/// 子任务通知（GAP-1）：task_completed/task_failed 事件额外携带
/// `parent_pipeline_id` 标签（state 的 `lineage.parent_pipeline_id` 扁平键，
/// 无父/根形式时为空串）——triggers_ext 据此把子任务完成通知注入父管道，
/// 兑现 task_submit "子任务完成后自动通知上级"的承诺（等效自动注册的触发器，
/// 注册逻辑收敛在统一触发服务，任务系统零触发代码）。
fn derive_run_terminal_events(
    final_state: &serde_json::Value,
    failed: bool,
) -> Vec<(&'static str, Vec<(&'static str, serde_json::Value)>)> {
    let v = |k: &str| {
        final_state
            .get(k)
            .cloned()
            .unwrap_or(serde_json::Value::Null)
    };
    let pipeline_id = v("pipeline_id");
    let thread_id = v("session_id");
    let has_task = has_task_marker(final_state);
    let task_id = v("task.id");
    // 子任务通知锚点：lineage.parent_pipeline_id 扁平键（有父形式出生写入）
    let parent_pipeline_id = v("lineage.parent_pipeline_id");
    // 子任务完成通知注入 chat.send_message 需要 user_id：task_submit 创建子任务
    // 时把提交者写进初始 state（task.submitted_by），随 final_state 带出给
    // triggers_ext 注入器——否则注入器传空串被内核拒绝（-32603 缺少 user_id）。
    let task_user_id = v("task.submitted_by");

    let mut events: Vec<(&'static str, Vec<(&'static str, serde_json::Value)>)> = Vec::new();
    let run_tags = vec![
        ("pipeline_id", pipeline_id.clone()),
        ("thread_id", thread_id.clone()),
    ];
    if failed {
        events.push(("run.failed", run_tags));
        if has_task {
            events.push((
                "task_failed",
                vec![
                    ("pipeline_id", pipeline_id),
                    ("thread_id", thread_id),
                    ("task_id", task_id),
                    ("parent_pipeline_id", parent_pipeline_id),
                    ("user_id", task_user_id),
                ],
            ));
        }
        return events;
    }
    let suspended = final_state
        .get("suspended")
        .and_then(|s| s.as_bool())
        .unwrap_or(false);
    if suspended {
        events.push(("run.suspended", run_tags));
        return events;
    }
    events.push(("run.completed", run_tags));
    if has_task {
        events.push((
            "task_completed",
            vec![
                ("pipeline_id", pipeline_id),
                ("thread_id", thread_id),
                ("task_id", task_id),
                ("parent_pipeline_id", parent_pipeline_id),
                ("user_id", task_user_id),
            ],
        ));
    }
    events
}

/// 广播 run 终态域事件（GAP-2：fire-and-forget，不阻塞引擎出口）。
async fn emit_run_terminal_domain_events(
    state: &AppState,
    terminal_state: &serde_json::Value,
    failed: bool,
) {
    for (name, tags) in derive_run_terminal_events(terminal_state, failed) {
        crate::plugin_lifecycle::broadcast_domain_event(state, name, tags).await;
    }
}

/// 判定一份 state 是否属于任务管道（task = pipeline state 单一真值）。
///
/// 口径与插件侧聚合 `_list_tasks_from_state` 第一趟一致：含 `task.` 前缀键
/// 且**不含** `task.owned.` 前缀键。`task.owned.*` 是父管道登记子任务的扁平键
/// （任务声明在子管道自己的 `task.*` 上），仅登记过子任务的聊天主管道不得被
/// 误判为任务管道——否则 run 终态会被回写 `task.status`/`task.ended_at`，
/// 在任务聚合出口变成无标题无 task.id 的幽灵任务行。
fn has_task_marker(state: &serde_json::Value) -> bool {
    state
        .as_object()
        .map(|o| {
            o.keys()
                .any(|k| k.starts_with("task.") && !k.starts_with("task.owned."))
        })
        .unwrap_or(false)
}

/// 前置依赖缺失时的 echo 降级应答（invoker/store/project_root 任一缺席）。
/// degraded 标记供调用方（REST chat / 插件 send_message）识别降级应答，
/// 与正常回复区分（2026-08-26 统一错误模型：假成功显式化）。
fn echo_fallback(missing: &str, message: &str) -> EngineOutcome {
    EngineOutcome {
        content: format!("[echo-fallback: {missing}] {message}"),
        final_assistant: None,
        final_user: None,
        failed: false,
        degraded: true,
    }
}

/// 阶段 1/1a/1a2/1a3：构造初始 state（含会话级/任务级 execution_context 注入 +
/// 自由 state overlay 合并）。
///
/// core_plugin 默认值可被 agent 配置（config/agents/<id>.yaml 的 core_plugin 字段）
/// 覆盖——见 load_agent_config_into_state。避免在内核硬编码具体插件 id。
// 技术债（同上）：多参阶段函数，收尾时统一收敛参数结构体。
#[allow(clippy::too_many_arguments)]
async fn stage_build_initial_state(
    store: &Arc<dyn StorageBackend>,
    message: &str,
    agent_id: &str,
    effective_pipeline_id: &str,
    thread_id: &str,
    message_id: &str,
    user_id: &str,
    thinking_strength: &str,
    execution_context: Option<&serde_json::Value>,
    state_overlay: Option<&serde_json::Value>,
    run_id: &str,
) -> serde_json::Value {
    let mut initial_state = serde_json::json!({
        "message": message,
        "input": message,
        "agent_id": agent_id,
        "core_type": "llm_call",
        "core_plugin": DEFAULT_CORE_PLUGIN,
        "ended": false,
        "suspended": false,
        // run_id：本轮 run 的轮询定位锚（批次 C 注入，llm_core 取消轮询经
        // pipeline-executor.get_run_status 查询它；跨轮被下一轮覆盖，属 per-run 键）。
        "run_id": run_id,
        // 流式推送路由键：
        // - pipeline_id：前端消息路由键（前端 handleStreamStart 据此匹配占位气泡）
        // - session_id：WS 连接路由键（内核 session.emit_stream 据此定位前端 WS 连接）
        //   必须是真实 thread_id（WS 握手时注册的），否则 chunk 推不到前端。
        // pipeline_id 用 effective 值覆盖：persist_run_start/end 从 state 读
        // pipeline_id 落库，统一语义后写入与查询键一致，空值不再产生孤儿数据。
        "pipeline_id": effective_pipeline_id,
        "session_id": thread_id,
        // user_id：触发器（trigger_setup）等工具在 0.2 需它做上下文绑定（触发时
        // 经 chat.send_message 回派发需要 user_id 解析 tenant）。param_inject 据此
        // 注入到工具 args；空串时 param_inject 自动跳过注入，不影响既有行为。
        "user_id": user_id,
        // assistant message_id：内核权威生成，sidecar 流式 chunk 携带它，
        // 前端 handleStreamChunk 据此把 chunk 路由到 stream_start 建立的占位气泡。
        "message_id": message_id,
        // 思考强度（off/low/medium/high；空串=未指定）：透传给 llm_core，
        // 由插件在请求构造时路由到具体模型参数（temperature/max_tokens/reasoning_effort）。
        "thinking_strength": thinking_strength,
    });

    // 1a. 会话级 execution_context 注入：thread metadata 的 workspace /
    // workspace_mode / isolation_mode（会话创建时由前端写入 metadata）组装为
    // 结构化 execution_context，随 initial_state 进入管道——init 体的
    // workspace_lifecycle / environment_lifecycle 插件据此执行（工作空间解析 +
    // 环境基线），checkpoint 自动持久化。
    //
    // 拓扑/隔离不依赖 workspace 填写：未填 source_path 时插件按默认目录自动
    // 生成，mode/level 仍然生效（拓扑默认 worktree，对齐 task_submit）。
    //
    // 任务级 execution_context（task_submit 提交的 workspace_mode/isolation_level）
    // 经任务管道执行入口透传（chat.send_message params），优先级高于此会话级来源；
    // 两者结构一致：{"workspace": {source_path, mode}, "isolation": {level}}。
    match store.get_session(thread_id).await {
        Ok(Some(sess)) => {
            if let Some(meta) = sess.metadata {
                let mut ec = serde_json::Map::new();
                let ws_path = meta
                    .get("workspace")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty());
                let ws_mode = meta
                    .get("workspace_mode")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty())
                    .unwrap_or("worktree");
                if ws_path.is_some() || meta.get("workspace_mode").is_some() {
                    let mut ws_obj = serde_json::Map::new();
                    if let Some(p) = ws_path {
                        ws_obj.insert("source_path".to_string(), serde_json::json!(p));
                    }
                    ws_obj.insert("mode".to_string(), serde_json::json!(ws_mode));
                    ec.insert("workspace".to_string(), serde_json::Value::Object(ws_obj));
                }
                if let Some(iso) = meta
                    .get("isolation_mode")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty())
                {
                    ec.insert("isolation".to_string(), serde_json::json!({"level": iso}));
                }
                if !ec.is_empty() {
                    if let Some(obj) = initial_state.as_object_mut() {
                        obj.insert(
                            "execution_context".to_string(),
                            serde_json::Value::Object(ec),
                        );
                    }
                }
            }
        }
        Ok(None) => {}
        Err(e) => {
            warn!(
                thread_id = %thread_id,
                error = %e,
                "execution_context 会话级注入失败：get_session 读不到会话（沿用默认）"
            );
        }
    }

    // 1a2. 任务级 execution_context 覆盖（经 chat.send_message params 透传，
    // 任务执行器从 task.metadata 组装；优先级高于会话级 thread metadata）。
    if let Some(ec) = execution_context {
        if ec.is_object() && !ec.as_object().map(|o| o.is_empty()).unwrap_or(true) {
            if let Some(obj) = initial_state.as_object_mut() {
                obj.insert("execution_context".to_string(), ec.clone());
            }
        }
    }

    // 1a3. 自由 state overlay（GAP-1：chat.send_message 的 state 参数 + 引擎
    // 写入的 lineage 扁平键）：在 execution_context 合并点（1a/1a2）之后并入
    // 顶层扁平键——任务域 task.* 出生即入 state，消费方（task_evaluate /
    // child_task_guard / 任务树聚合）从 state 直读。
    if let Some(overlay) = state_overlay {
        apply_state_overlay(&mut initial_state, overlay);
    }

    initial_state
}

/// 把自由 state overlay 并入 initial_state 顶层扁平键（GAP-1 阶段 1 合并点）。
///
/// 防御语义（调用方 chat_send_handler 已做保留字校验，此处纵深防御）：
/// - 引擎系统保留字（[`crate::kernel_capabilities::RESERVED_STATE_KEYS`]，
///   契约文件锚定的单一真值源清单）跳过，非 chat 入口的理论调用者无法覆写
///   message/pipeline_id 等系统字段；
/// - `lineage.*` 已存在时跳过——lineage 是引擎出生写入的保护字段（与 messages
///   同级），后续轮次 overlay 不可覆写（防伪造父/根）。
fn apply_state_overlay(initial_state: &mut serde_json::Value, overlay: &serde_json::Value) {
    let Some(src) = overlay.as_object() else {
        return;
    };
    let Some(obj) = initial_state.as_object_mut() else {
        return;
    };
    for (k, v) in src {
        if crate::kernel_capabilities::RESERVED_STATE_KEYS.contains(&k.as_str()) {
            continue;
        }
        if k.starts_with("lineage.") && obj.contains_key(k) {
            continue;
        }
        obj.insert(k.clone(), v.clone());
    }
}

/// 阶段 1b：多轮上下文装配 + 本轮 user 消息入账，返回补全后的 initial_state。
///
/// state.messages 是 LLMCore._build_messages 读取的对话历史。单一权威（不搞降级路径）：
///   ① 热路径：PipelineStateRegistry 内存 state 完整（每轮 final_state 含 messages
///      写回，LLM 插件负责 append assistant 回复）→ 直接复用 entry.state["messages"]。
///   ② 冷路径：registry 未命中（重启/新会话/内存丢失）→ 从 messages 表（持久化
///      冷数据）按 effective_pipeline_id 恢复完整历史，后续轮走热路径。
///   ③ 客户端传的 history 仅在①②均为空（真·首轮）时兜底，向后兼容老客户端。
/// 恢复失败 = bug（内存丢 + DB 读不到）：显式 error 暴露，不静默吞掉。
///
/// `skip_user_append`（批次 D 显式重跑标志，经 state_overlay 注入）：regenerate
/// 重跑时目标 user 消息已在截断后历史中，跳过本轮 append——不借用 interrupted_tail
/// 启发式（同文/同 cmid 判定只服务崩溃重放场景）。
#[allow(clippy::too_many_arguments)]
#[allow(clippy::too_many_arguments)]
async fn stage_recover_history(
    mut initial_state: serde_json::Value,
    store: &Arc<dyn StorageBackend>,
    message: &str,
    thread_id: &str,
    effective_pipeline_id: &str,
    tenant_id: &str,
    client_message_id: &str,
    skip_user_append: bool,
) -> serde_json::Value {
    let mut history_prefix: Vec<serde_json::Value> = Vec::new();
    let mut history_loaded = false;
    let registry = agentos_session::global_registry();
    if let Some(entry) = registry.get(tenant_id, effective_pipeline_id) {
        // 热路径：内存 state 完整，直接复用（无需走 DB）
        if let Some(msgs) = entry
            .read()
            .state
            .get("messages")
            .and_then(|v| v.as_array())
        {
            history_prefix = msgs.clone();
            history_loaded = true;
        }
    }
    if !history_loaded {
        // 冷路径（零兼容重排）：
        // ① checkpoint 只提供**标量基线**——messages 一律不消费（load 后剥离丢弃），
        //    无论新旧格式（任务 5 瘦身后新 checkpoint 本就不含 messages）。
        // ② 无 checkpoint → traces 回放**标量字段**（merge_patch 跳过 messages）。
        // ③ pipeline_state 表补充累计标量（每 step upsert 最新值）。
        // ④ messages 从 message_slots 表直读（消息队列持久真值，零回放）。
        let mut recovered: serde_json::Value = serde_json::json!({});
        let mut ckpt_hit = false;
        if !effective_pipeline_id.is_empty() {
            match store
                .load_latest_checkpoint(effective_pipeline_id, tenant_id)
                .await
            {
                Ok(Some((_step_no, ckpt_state))) => {
                    recovered = ckpt_state;
                    // 队列真值在表：checkpoint 的 messages 剥离丢弃（新旧格式一律）
                    if let Some(rec_obj) = recovered.as_object_mut() {
                        rec_obj.remove("messages");
                    }
                    ckpt_hit = true;
                    debug!(
                        pipeline_id = %effective_pipeline_id,
                        "冷启动从 checkpoint 恢复标量基线（messages 走表读）"
                    );
                }
                Ok(None) => {
                    // 无 checkpoint：正常冷启动路径，走 traces 回放
                }
                Err(e) => {
                    // checkpoint 读失败不等同无 checkpoint：降级走 traces 回放时留痕，
                    // 避免静默丢标量基线（错误是数据读失败，不是记录不存在）。
                    error!(
                        pipeline_id = %effective_pipeline_id,
                        error = %e,
                        "load_latest_checkpoint 冷恢复失败，降级 traces 回放"
                    );
                }
            }
        }
        if !ckpt_hit {
            match store.get_step_traces_by_thread(thread_id, tenant_id).await {
                Ok(step_traces) => {
                    for entry in &step_traces {
                        merge_patch(&mut recovered, &entry.patch_data);
                    }
                }
                Err(e) => {
                    // 持久化恢复失败：bug 信号（内存丢 + DB 也读不到），显式 error 暴露。
                    error!(
                        thread_id = %thread_id,
                        error = %e,
                        "get_step_traces_by_thread 回放失败"
                    );
                }
            }
        }
        // 累计标量字段以 pipeline_state 为准（每 step upsert 最新值），
        // 重建后插件能在正确基线上自然累加，不归零。
        if !effective_pipeline_id.is_empty() {
            match store
                .load_pipeline_state(effective_pipeline_id, tenant_id)
                .await
            {
                Ok(state_fields) => {
                    if let Some(rec_obj) = recovered.as_object_mut() {
                        for (k, v) in &state_fields {
                            rec_obj.insert(k.clone(), v.clone());
                        }
                    }
                }
                Err(e) => {
                    error!(
                        pipeline_id = %effective_pipeline_id,
                        error = %e,
                        "load_pipeline_state 冷恢复失败（标量基线可能不完整）"
                    );
                }
            }
        }
        // messages 直读 message_slots（零回放；元素自带稳定 seq）
        if !effective_pipeline_id.is_empty() {
            match store
                .load_message_history(effective_pipeline_id, tenant_id)
                .await
            {
                Ok(msgs) => history_prefix = msgs,
                Err(e) => {
                    error!(
                        pipeline_id = %effective_pipeline_id,
                        error = %e,
                        "load_message_history 冷恢复失败（对话历史可能不完整）"
                    );
                }
            }
        }
        // 标量字段注入 state（GAP-3：跳过易变 per-run 键——修复前已写的旧
        // checkpoint 仍携带 message/input/suspended 等，覆盖会顶掉本轮新输入，
        // 导致重启后旧 user 消息被重放消费。键集与 checkpoint 瘦身同源）。
        // 内部下划线键（_skip_user_append 等）同为 per-run 指令，不跨轮恢复——
        // 上一轮的重跑标志泄漏到本轮会把正常消息吞掉（批次 D 显式重跑标志）。
        if let Some(rec_obj) = recovered.as_object_mut() {
            rec_obj.remove("messages");
            if let Some(init_obj) = initial_state.as_object_mut() {
                for (k, v) in rec_obj.iter() {
                    if k.starts_with('_') {
                        continue;
                    }
                    if agentos_engine::VOLATILE_RUN_KEYS.contains(&k.as_str()) {
                        continue;
                    }
                    init_obj.insert(k.clone(), v.clone());
                }
            }
        }
    }
    // messages 塞回 state（热路径元素自带 seq；冷路径表读也已带 seq——零兼容，
    // 不做缺 seq 补位、不做客户端 history 兜底）
    if let Some(obj) = initial_state.as_object_mut() {
        obj.insert(
            "messages".to_string(),
            serde_json::Value::Array(history_prefix),
        );
    }
    // A1：复位"本轮首个 assistant 已注入 id"标志（checkpoint/registry 恢复可能带旧值，
    // 必须在恢复合并之后、管道执行之前强制复位，保证每 run 恰好注入一次 message_id）。
    if let Some(obj) = initial_state.as_object_mut() {
        obj.insert(
            "_assistant_id_assigned".to_string(),
            serde_json::json!(false),
        );
    }
    // GAP-3：中断重放幂等——上一次尝试若已把本轮 user 消息落槽（重启/崩溃
    // 截断 run，无 assistant 跟随），恢复出的 history 尾部就是它；再 append 会
    // 重复落槽+重复消费。判定按幂等键裁决
    // （ADR 2026-08-21）：cmid 在场时只有「同 cmid」才算同一次发送的重派（吞）；
    // cmid 不同 = 真发了两条（绝不吞）。无 cmid
    // 的路径（触发器注入/旧客户端）维持同文判定。
    let interrupted_tail = initial_state
        .get("messages")
        .and_then(|v| v.as_array())
        .and_then(|msgs| msgs.last())
        .is_some_and(|last| {
            let same_user_content = last.get("role").and_then(|r| r.as_str()) == Some("user")
                && last.get("content").and_then(|c| c.as_str()) == Some(message);
            if !same_user_content {
                return false;
            }
            if client_message_id.is_empty() {
                return true;
            }
            last.get("metadata")
                .and_then(|m| m.get("client_message_id"))
                .and_then(|v| v.as_str())
                == Some(client_message_id)
        });
    // user 经 append op(无 seq → 引擎分配 next seq)+ 落 message_slots。
    // 指纹实录塞 _pending_message_ops（内部字段）：executor.persist_run_start 落一条
    // user_input 轨迹后移除——首轮 user 由此进入审计/回放范围（ops 即轨迹）。
    // cmid 非空时随 metadata 落库（compute_message_id 纳入 metadata 参与 hash：
    // 同内容多次发送 record_id 各自唯一，顺带消除重复内容同 id 碰撞）。
    if !interrupted_tail && !skip_user_append {
        let mut user_msg = serde_json::json!({"role":"user","content":message});
        if !client_message_id.is_empty() {
            user_msg["metadata"] = serde_json::json!({"client_message_id": client_message_id});
        }
        if let Ok(user_ledger) = agentos_engine::apply_messages_op_update(
            &mut initial_state,
            store.as_ref(),
            tenant_id,
            &[serde_json::json!({"op":"set","msg":user_msg})],
        )
        .await
        {
            if !user_ledger.is_empty() {
                if let Some(obj) = initial_state.as_object_mut() {
                    obj.insert(
                        "_pending_message_ops".to_string(),
                        serde_json::Value::Array(user_ledger),
                    );
                }
            }
        }
    }
    initial_state
}

/// 阶段 2b：注入工具 schema。
///
/// agent 配置（system_prompt/persona 等 yaml 字段）已从内核解耦：内核只负责
/// 把 agent_id 放进 initial_state（stage_build_initial_state），yaml 加载归
/// sidecar 的 context_build 插件（按 state.agent_id 读
/// AGENTOS_CONFIG_ROOT/agents/**）。工具 schema 注入留在内核——ToolRegistry
/// 在内核，这是工具面契约（按 agent tool_ids 过滤下发）而非 agent 配置。
fn stage_inject_agent_and_tools(
    state: &AppState,
    initial_state: &mut serde_json::Value,
    _agent_id: &str,
    _project_root: &std::path::Path,
) {
    // 2b. 注入工具 schema 到 state（0.2 sidecar 架构适配）。
    // 0.1 单进程时 tool_schema 插件经 ctx.get_service("tool_registry") 直接访问内核
    // ToolRegistry；0.2 sidecar 是独立进程拿不到该 service。改为内核侧在管道启动前
    // 按 agent tool_ids 过滤、转成 OpenAI function-calling 格式注入 state["tool_schemas"]，
    // 这样 prepare 阶段的 tool_schema 插件读到非空 schema（它优先用 state 里的值），
    // LLM 即可看到工具并调用（tool_core 执行时内核 invoke_tool 经 MCP 调 sidecar）。
    inject_tool_schemas(initial_state, state);
}

/// 阶段 3：构造 PipelineExecutor 并执行 initial_state，返回 final_state。
///
/// branch_id 用固定 "main"；run_id 由调用方生成（process_via_engine_inner 权威
/// 生成点，已注入 initial_state 供插件轮询），本函数只消费不再生成——保证
/// state 里的轮询锚与 runs 表落库 id 是同一个 uuid。租户上下文从 task_local 读取
/// （多租户 P0-4：调用方已在 agentos_tenant::scope 内）。
/// Pull 热加载：按需重载管道配置（autonomous.yaml + steps）——每次 chat 执行前
/// 检测配置 mtime，变了才重新加载到本次执行用的局部变量，不写回 AppState
/// （启动期配置保持不动，作为重载失败的兜底）。
/// 执行失败（executor.run 返回 Err）：把 run 标记 failed + ended_at（避免永远卡
/// running、历史悬空）并以 failed outcome 出口（Err 携带）。
#[allow(clippy::too_many_arguments)]
async fn stage_execute(
    state: &AppState,
    invoker: Arc<dyn agentos_core::traits::PluginInvoker>,
    project_root: PathBuf,
    tenant: TenantContext,
    store: Arc<dyn StorageBackend>,
    initial_state: serde_json::Value,
    agent_id: &str,
    message: &str,
    run_id: &str,
) -> Result<serde_json::Value, EngineOutcome> {
    let branch_id = "main".to_string();

    // ── Pull 热加载（在 project_root 被 move 给 executor 之前算出 config_root）──
    let config_root = project_root.join("config");
    let compiled = maybe_reload_compiled_pipeline(state, &config_root).await;
    let known_ids = live_plugin_ids(state).await;
    let executor = agentos_engine::PipelineExecutor::new(
        invoker,
        project_root,
        tenant,
        known_ids.iter().cloned(),
        store.clone(),
        run_id.to_string(),
        branch_id,
    )
    // 分层持久化：从所有插件 manifest 的 persistent_fields 声明收集并集。
    // 这些是需跨轮持久化的累计标量字段（如 track.total_tokens）。
    // messages 是系统字段（引擎固定投影），不依赖此集合。
    .with_persistent_fields(
        state
            .manifests
            .read()
            .await
            .iter()
            .flat_map(|m| m.persistent_fields.iter().cloned()),
    )
    // on_pipeline_end 钩子插件收集（spill_guard 清理通道）：manifest 声明了该
    // 钩子的插件在 run 结束时收到 OnPipelineEnd（best-effort，见 executor）。
    .with_pipeline_end_hook_plugins(
        state
            .manifests
            .read()
            .await
            .iter()
            .filter(|m| {
                m.capabilities
                    .lifecycle_hooks
                    .contains(&agentos_core::traits::LifecycleHook::OnPipelineEnd)
            })
            .map(|m| m.id.clone()),
    );

    // 双轨收敛（审计变更#1）：agent 全量配置唯一事实源 = context_build 插件，
    // 引擎不再 per-iteration 注入 agent yaml；内核只经 resolve_agent_tool_ids
    // 读 tool_ids 做工具面过滤（K10 窄接口）。
    info!(run_id = %run_id, agent_id = %agent_id, "Pipeline run started");

    // GAP-2：防御网路径的失败事件标签预捕获（run_compiled 会 move initial_state）。
    let failed_emit_state = serde_json::json!({
        "pipeline_id": initial_state.get("pipeline_id").cloned().unwrap_or(serde_json::Value::Null),
        "session_id": initial_state.get("session_id").cloned().unwrap_or(serde_json::Value::Null),
        "task.id": initial_state.get("task.id").cloned().unwrap_or(serde_json::Value::Null),
    });

    // run 启动域事件：回合开始时广播 run.started，触发器（EVENT）与生命周期
    // 订阅者据此感知回合边界。标签与 run 终态事件同构（run_id/pipeline_id/
    // thread_id/session_id）。
    crate::plugin_lifecycle::broadcast_domain_event(
        state,
        "run.started",
        vec![
            ("run_id", serde_json::json!(run_id)),
            (
                "pipeline_id",
                initial_state
                    .get("pipeline_id")
                    .cloned()
                    .unwrap_or(serde_json::Value::Null),
            ),
            (
                "thread_id",
                initial_state
                    .get("session_id")
                    .cloned()
                    .unwrap_or(serde_json::Value::Null),
            ),
        ],
    )
    .await;

    match executor.run_compiled(&compiled, initial_state).await {
        Ok(s) => Ok(s),
        Err(e) => {
            warn!(run_id = %run_id, error = %e, "PipelineExecutor run failed");
            // GAP-2：run.failed 终态事件（引擎 Err 防御网；task.* 派生经预捕获标签）
            emit_run_terminal_domain_events(state, &failed_emit_state, true).await;
            // B2：引擎失败兜底——把 run 标记 failed + ended_at，避免永远卡 running、
            // 历史悬空。（PipelineExecutor::run 当前不返回 Err，这是防御网；崩溃留下的
            // running 孤儿由内核启动 reap_orphan_runs 清扫。）
            if let Err(pe) = store
                .update_run_status(run_id, agentos_core::types::RunStatus::Failed, None, None)
                .await
            {
                warn!(run_id = %run_id, error = %pe, "update_run_status(Failed) 失败（继续）");
            }
            Err(EngineOutcome {
                content: format!("[engine-run-failed] {message}"),
                final_assistant: None,
                final_user: None,
                failed: true,
                degraded: false,
            })
        }
    }
}

/// 阶段 3b/4：final_state 回写全局 registry + 提取响应。
///
/// 回写：final_state 含完整 messages 历史（LLM 插件 append 了 assistant 回复），
/// 按 (tenant_id, effective_pipeline_id) 常驻：下一轮热路径直接复用，
/// 免 DB 查询；重启/内存丢失时冷路径从 messages 表恢复（见 stage_recover_history）。
fn stage_finalize(
    final_state: &serde_json::Value,
    tenant_id: &str,
    effective_pipeline_id: &str,
    thread_id: &str,
    agent_id: &str,
) -> EngineOutcome {
    if !effective_pipeline_id.is_empty() {
        let reg = agentos_session::global_registry();
        if !reg.contains(tenant_id, effective_pipeline_id) {
            reg.get_or_init(
                tenant_id,
                effective_pipeline_id,
                thread_id,
                agent_id,
                final_state.clone(),
            );
        } else {
            reg.update_state(tenant_id, effective_pipeline_id, final_state.clone());
        }
    }

    let content = extract_response_content(final_state);
    // A2：本轮最终 assistant 消息（完整持久形态，含 tool_calls/reasoning_content/seq），
    // WS 路径 new_message 携带它——前端与 DB 加载共用 mapper，冷热路径同构。
    let final_assistant = final_state
        .get("messages")
        .and_then(|v| v.as_array())
        .and_then(|msgs| {
            msgs.iter()
                .rev()
                .find(|m| m.get("role").and_then(|v| v.as_str()) == Some("assistant"))
                .cloned()
        });
    // A2：本轮 user 消息（完整持久形态，含引擎分配的 seq + metadata.client_message_id）。
    // record_id 由 compute_message_id 对整条消息（剔除 seq/_ 内部字段）取指纹，
    // 与表侧 write_slot_to_table_locked 落库 id 一致——认领回传的权威 id/seq
    // 即 DB 真值，前端据此补 recordId + 排序键。
    let final_user = final_state
        .get("messages")
        .and_then(|v| v.as_array())
        .and_then(|msgs| {
            msgs.iter()
                .rev()
                .find(|m| m.get("role").and_then(|v| v.as_str()) == Some("user"))
                .cloned()
        });
    EngineOutcome {
        content,
        final_assistant,
        final_user,
        failed: false,
        degraded: false,
    }
}

/// 响应内容提取：只认 raw_result（LLM 本轮产出）。
///
/// A12：兜底不再把整份内部 state pretty-print 给客户端（内部结构/工具细节泄漏），
/// 固定返回 "pipeline finished"；完整 state 保留在服务端 tracing（debug 级）。
/// 不回退 state.message——那是用户输入原文，回退即把用户消息当回复回发
/// （前端表现为 assistant 气泡回显用户消息）。无回复轮次由调用方
/// （ws_session run_pipeline_round）按 final_assistant 缺失走 stream_error。
fn extract_response_content(final_state: &serde_json::Value) -> String {
    if let Some(raw) = final_state
        .get("raw_result")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
    {
        raw.to_string()
    } else {
        debug!(
            target: "chat-response",
            state = ?final_state,
            "pipeline finished without raw_result（完整 state 仅留服务端日志）"
        );
        "pipeline finished".to_string()
    }
}

/// 框架级强制工具：无视 agent `tool_ids`，只要 registry 里存在就注入。
///
/// 这些工具的存在是**无害**的（LLM 不会无缘无故调——需要具体参数引导），但对
/// 框架兜底闭环**必需**。例如 `spill_retrieve`：spill_guard 把大输出替换为
/// 摘要 + "调用 spill_retrieve(tool_call_id=...) 取回" 引导，若 agent 的
/// tool_ids 未列此工具，LLM 工具列表里没有它的 schema，原文就永远取不回——
/// 兜底链路断裂。它与 spill_guard 配套安装（要么都装要么都不装），故"registry
/// 存在 = 已安装"即可作为注入判据，无需读 pipeline 配置做条件联动。
const FRAMEWORK_ALWAYS_INCLUDE_TOOLS: &[&str] = &["spill_retrieve"];

/// state 无 tool_ids 时按 `state.agent_id` 从 ConfigCenter 解析 agent yaml 的
/// tool_ids（K10：内核侧工具面过滤的配置解析点，窄接口——只读 tool_ids，不
/// 注入 agent 全量配置；agent 配置唯一事实源在 sidecar context_build）。
///
/// 返回：
/// - `Some(集合)`：yaml 带 `tool_ids` 键（含显式空表 = agent 声明零工具）；
/// - `None`：配置断链（无 config_center / 无 agent_id / yaml 缺失或损坏 /
///   yaml 无 tool_ids 键）。yaml 缺失或损坏时顺带把 `_agent_config_missing`
///   标记写进真实 state（与 K5 同键，诊断出口统一）。
fn resolve_agent_tool_ids(
    state: &mut serde_json::Value,
    app_state: &AppState,
) -> Option<std::collections::HashSet<String>> {
    let cc = app_state.config_center.as_ref()?;
    let agent_id = state
        .get("agent_id")
        .and_then(|v| v.as_str())
        .map(str::to_string)?;
    match agentos_config::resolve_agent_tool_ids(cc, &agent_id) {
        Ok(Some(ids)) => Some(ids.into_iter().collect()),
        Ok(None) => None,
        Err(_) => {
            if let Some(obj) = state.as_object_mut() {
                obj.insert(
                    "_agent_config_missing".to_string(),
                    serde_json::Value::Bool(true),
                );
            }
            None
        }
    }
}

/// 注入工具 schema 到 state["tool_schemas"]（0.2 sidecar 架构适配）。
/// RFC 7396 JSON Merge Patch：把 patch 按序合并进 target。
/// 用于冷启动时按 step 级轨迹逐条 merge 回放，重建完整 state（**仅标量字段**）。
/// - patch 中值为对象：递归 merge（target 同 key 也为对象时）
/// - patch 中值为 null：从 target 删除该 key
/// - 否则：target[key] = patch[key]（整体替换）
///
/// messages **不参与回放**（零兼容）：消息队列真值在 message_slots 表（冷路径直读），
/// 轨迹里的 messages 实录只做审计/回退重建（任务 6），不走本函数。
fn merge_patch(target: &mut serde_json::Value, patch: &serde_json::Value) {
    if let serde_json::Value::Object(patch_map) = patch {
        if !target.is_object() {
            *target = serde_json::Value::Object(serde_json::Map::new());
        }
        let target_obj = target.as_object_mut().expect("ensured object above");
        for (k, v) in patch_map {
            if v.is_null() {
                target_obj.remove(k);
            } else if k == "messages" {
                continue; // 队列真值在表，标量回放跳过
            } else if let Some(existing) = target_obj.get_mut(k) {
                if existing.is_object() && v.is_object() {
                    merge_patch(existing, v);
                } else {
                    *existing = v.clone();
                }
            } else {
                target_obj.insert(k.clone(), v.clone());
            }
        }
    } else {
        *target = patch.clone();
    }
}

///
/// 按 state["tool_ids"] 过滤 capability_registry 的工具，转成 OpenAI function-calling
/// 格式（`{type:"function", function:{name, description, parameters}}`）。
///
/// tool_ids 解析链（K10）：state 显式 tool_ids（overlay/上游注入）优先；缺失时按
/// state.agent_id 从 ConfigCenter 解析 agent yaml 的 tool_ids（0.2 契约：agentos.yaml
/// tool_ids 白名单控制 LLM 工具面）。两层都解析不出 = 配置断链 → **空工具面**
/// （仅保留 FRAMEWORK_ALWAYS_INCLUDE_TOOLS 框架强制工具）+ warn 报警，禁止
/// 静默全量（agent 配置断链时权限边界消失、配置错误伪装成"全工具可用"）。
/// registry 不可用时注入空列表（LLM 无工具可用）。
fn inject_tool_schemas(state: &mut serde_json::Value, app_state: &AppState) {
    let Some(registry) = app_state.capability_registry.as_ref() else {
        return;
    };
    let all_tools = registry.list_tools();

    // 按 agent 的 tool_ids 过滤；state 未带时按 agent_id 从 agent yaml 解析（K10）
    let wanted: Option<std::collections::HashSet<String>> =
        match state.get("tool_ids").and_then(|v| v.as_array()) {
            Some(arr) => Some(
                arr.iter()
                    .filter_map(|t| t.as_str().map(String::from))
                    .collect(),
            ),
            None => resolve_agent_tool_ids(state, app_state),
        };
    if wanted.is_none() {
        tracing::warn!(
            target: "tool-surface",
            agent_id = state.get("agent_id").and_then(|v| v.as_str()).unwrap_or(""),
            "state 无 tool_ids 且按 agent yaml 解析不出（配置断链，K10）：工具面置空（仅保留框架强制工具），拒绝兜底全量"
        );
    }
    let schemas: Vec<serde_json::Value> = all_tools
        .iter()
        .filter(|t| match &wanted {
            Some(ids) => {
                // tool_ids 命中 或 框架级强制工具（spill_retrieve 等，无视 agent 配置）
                ids.contains(&t.name) || FRAMEWORK_ALWAYS_INCLUDE_TOOLS.contains(&t.name.as_str())
            }
            // 配置断链：不再全量兜底（K10）。FRAMEWORK_ALWAYS_INCLUDE_TOOLS 是
            // 文档化框架机制（spill_retrieve 配套），保留。
            None => FRAMEWORK_ALWAYS_INCLUDE_TOOLS.contains(&t.name.as_str()),
        })
        .filter(|t| {
            // LLM 严格校验工具 schema:parameters 必须是 type:object 的 JSON Schema。
            // 注意（K9 勘误）：注册路径（plugin_lifecycle / agentos-kernel 启动循环）
            // 对缺 input_schema 的 manifest 工具按 {} 补注册，{} 是 object——本过滤
            // 对这些工具**恒不触发**（注册路径对缺 input_schema 的 manifest
            // 工具按 {} 补注册，{} 是 object），
            // 真正的防线在注册期的 warn + 启动报告计数，此处过滤只拦"注册后 schema
            // 被改写成非 object"的极端形态。
            t.input_schema.is_object()
        })
        .map(|t| {
            serde_json::json!({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                }
            })
        })
        .collect();

    if let Some(obj) = state.as_object_mut() {
        obj.insert(
            "tool_schemas".to_string(),
            serde_json::Value::Array(schemas),
        );
    }

    // 注入工具输出契约（task_dsh_plugin_adapter 任务 1）：tool_name →
    // {schema, render}。tool_core 执行后按 `schema` 校验返回值（fail-closed），
    // 前端按 `render` 意图路由渲染。只收集声明了 output_schema/render 的工具，
    // 未声明者不产生条目（41 个存量工具零负担）。
    let contracts: serde_json::Map<String, serde_json::Value> = all_tools
        .iter()
        .filter(|t| t.output_schema.is_some() || t.render.is_some())
        .map(|t| {
            (
                t.name.clone(),
                serde_json::json!({
                    "schema": t.output_schema.clone().unwrap_or(serde_json::Value::Null),
                    "render": t.render.clone().unwrap_or(serde_json::Value::Null),
                }),
            )
        })
        .collect();
    if let Some(obj) = state.as_object_mut() {
        obj.insert(
            "tool_output_contracts".to_string(),
            serde_json::Value::Object(contracts),
        );
    }
}

/// /api/v1/chat POST 端点——通过管道引擎处理消息。
async fn chat_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    axum::Json(req): axum::Json<WsRequest>,
) -> Result<axum::Json<WsResponse>, ApiError> {
    // 在租户上下文内通过管道引擎处理消息（多租户 P0-4）
    let tenant_ctx = request_tenant_ctx(state.store.as_ref(), &headers, &req.session_id).await;
    // 分层持久化：REST chat 路径需用会话的真实 active_pipeline_id，否则消息会落到
    // thread_id 维度（effective 回退），与前端按 active_pipeline_id 查询不匹配 → 前端拿不到消息。
    // WS 路径（ws_session.rs）前端已带真实 pipeline_id，此处为 REST fallback 补齐。
    let pipeline_id = if let Some(store) = state.store.as_ref() {
        let sid = req.session_id.clone();
        let store_clone = store.clone();
        match agentos_tenant::scope(tenant_ctx.clone(), async move {
            store_clone.get_session(&sid).await
        })
        .await
        {
            Ok(Some(session)) => session.active_pipeline_id.unwrap_or_default(),
            // 会话不存在（Ok(None)）→ 空 pipeline_id，chain_key 回退 session_id；
            // 存储故障（Err）单独报错可见，不与"无会话"混同。
            Ok(None) => String::new(),
            Err(e) => {
                warn!(session = %req.session_id, error = %e, "chat_handler: get_session 失败，chain_key 回退 session_id");
                String::new()
            }
        }
    } else {
        String::new()
    };
    // 解析请求用户 user_id（从 Authorization token），供 process_via_engine 写入 state；
    // 触发器等工具据此捕获 user_id，触发时回派发（chat.send_message）解析 tenant。
    let user_id = agentos_http::auth::resolve_request_user(state.store.as_ref(), &headers)
        .await
        .map(|(uid, _, _, _)| uid)
        .unwrap_or_default();
    // HTTP 路径同步返回 outcome，执行与 WS / chat.send_message 走同一
    // pending 队列消费路径（ADR-2026-08-26：入队先于激活，等待窗口可管），
    // 消除 HTTP 与 WS 同会话并发 run 的竞态（ADR-2026-08-15 的 FIFO 语义不变）。
    // 同步响应经 waiter 桥回传：cmid 为 `http_` 前缀（与前端 uuid cmid 空间
    // 区分），消费完成或排队中被 DELETE/清空时发送。
    let exec_agent = if req.agent_id.is_empty() {
        "agentos".to_string()
    } else {
        req.agent_id.clone()
    };
    let dispatcher =
        crate::ws_session::EngineDispatcher::new(state.clone());
    let cmid = format!("http_{}", &uuid::Uuid::new_v4().simple().to_string()[..16]);
    let (tx, rx) = tokio::sync::oneshot::channel::<EngineOutcome>();
    crate::ws_session::register_outcome_waiter(cmid.clone(), tx);
    // tenant_ctx 仅供前段读 session；dispatcher 内部按 user_id 重新解析真实
    // 租户（同源函数），不从此处传递。
    if let Err(e) = dispatcher
        .dispatch_user_input(
            &req.session_id,
            &user_id,
            &req.message,
            &pipeline_id,
            "",
            None,
            None,
            &exec_agent,
            &cmid,
            agentos_core::types::PendingInputSource::Http,
        )
        .await
    {
        return Err(ApiError::Internal {
            message: format!("chat 派发失败: {e}"),
        });
    }
    // 消费任务 panic（rx 关闭）时给出明确错误而非挂死等待。
    let outcome = rx.await.unwrap_or_else(|_| EngineOutcome {
        content: "[engine task terminated unexpectedly]".to_string(),
        final_assistant: None,
        final_user: None,
        failed: true,
        degraded: false,
    });

    let response = WsResponse {
        r#type: "message".to_string(),
        content: outcome.content,
        session_id: req.session_id,
        timestamp: chrono::Utc::now().to_rfc3339(),
        // 降级应答显式化（2026-08-26 统一错误模型）：echo_fallback 时标记，
        // 调用方（插件 send_message 等）据此与正常回复区分，不把降级当成功。
        degraded: Some(outcome.degraded),
    };
    Ok(axum::Json(response))
}

/// 人类交互响应端点——前端用户操作（选择选项/拒绝/取消）经此提交。
///
/// 内核转发到交互插件（human_interaction_tool）的 interaction.respond 工具，
/// 唤醒正在 wait_for_choice 阻塞的请求。这构成 choice/conversation 模式的
/// 响应回路：前端 → 内核 REST → invoker.invoke_tool → 交互插件 service.submit_response。
async fn interaction_response_handler(
    State(state): State<AppState>,
    axum::Json(body): axum::Json<serde_json::Value>,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let request_id = body
        .get("request_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if request_id.is_empty() {
        return Ok(axum::Json(
            serde_json::json!({"success": false, "error": "缺少 request_id"}),
        ));
    }

    let Some(invoker) = state.invoker.clone() else {
        return Ok(axum::Json(
            serde_json::json!({"success": false, "error": "invoker not available"}),
        ));
    };

    // 转发到交互插件的 interaction.respond 工具
    let inputs = serde_json::json!({
        "request_id": request_id,
        "response_type": body.get("response_type").and_then(|v| v.as_str()).unwrap_or("answered"),
        "selected_option": body.get("selected_option").and_then(|v| v.as_str()),
        "answers": body.get("answers"),
        "feedback": body.get("feedback").and_then(|v| v.as_str()),
    });

    match invoker
        .invoke_tool("human_interaction_tool", "interaction.respond", &inputs)
        .await
    {
        Ok(result) => Ok(axum::Json(serde_json::json!({
            "success": result.success,
            "request_id": request_id,
            "data": result.data,
            "error": result.error,
        }))),
        Err(e) => Ok(axum::Json(serde_json::json!({
            "success": false,
            "request_id": request_id,
            "error": e.message,
        }))),
    }
}

/// 启动 API 服务器。
///
/// `with_graceful_shutdown` 接线——Ctrl-C（Unix 另含 SIGTERM）
/// 触发后先 best-effort 杀掉全部缓存 sidecar（`shutdown_all`，防孤儿进程），
/// 再让 axum 收尾退出（裸 `axum::serve` 会让内核进程死后 sidecar 子进程
/// 全部变孤儿）。
pub async fn start_server(addr: SocketAddr, state: AppState) -> Result<(), ApiError> {
    // invoker 句柄先 clone 进 shutdown future（build_router 消费 state）。
    let shutdown_invoker = state.invoker.clone();
    let app = build_router(state);
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("Failed to bind {}: {}", addr, e),
        })?;
    info!("API server starting on {}", addr);
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal(shutdown_invoker))
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("Server error: {}", e),
        })?;
    Ok(())
}

/// 停机信号等待 + sidecar 清理（`start_server` 的 graceful shutdown future）。
///
/// 信号面：Ctrl-C 全平台；Unix 额外含 SIGTERM（容器/服务管理器标准停机信号）。
/// 触发后调用 `invoker.shutdown_all()`（drain + 逐 kill，各自带 2s 超时保护，
/// 见 invoker 实现）——best-effort，不阻塞退出超过秒级。
async fn shutdown_signal(invoker: Option<Arc<dyn agentos_core::traits::PluginInvoker>>) {
    #[cfg(unix)]
    {
        let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("install SIGTERM handler");
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {},
            _ = sigterm.recv() => {},
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
    info!(target: "api-server", "shutdown signal received; killing cached sidecars before exit");
    if let Some(invoker) = invoker.as_ref() {
        // 总预算 2s：shutdown_all 内部逐 kill 已有各自超时，这里再兜一层，
        // 保证停机信号到进程退出不被卡死的 kill 拖延（best-effort 清理）。
        let _ = tokio::time::timeout(Duration::from_secs(2), invoker.shutdown_all()).await;
    }
}

#[cfg(test)]
mod tests;
