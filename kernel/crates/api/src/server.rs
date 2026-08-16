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

#[allow(unused_imports)]
use agentos_core::traits::{CapabilityRegistry, MessageQueryOpts, StorageBackend};
use agentos_core::types::{PipelineConfig, StepLibrary, TenantContext};
use axum::{
    body::Body,
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        Request, State,
    },
    http::{header, HeaderMap, HeaderValue, Method, StatusCode},
    middleware::{from_fn, from_fn_with_state, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Router,
};

use crate::pipeline_loader::{load_pipeline_config, load_step_library, validate_no_name_conflicts};
use serde::{Deserialize, Serialize};
use tracing::{debug, error, info, warn};

use crate::auth::{
    login_handler, logout_handler, me_handler, refresh_handler, register_handler,
    resolve_request_tenant_id,
};
use crate::error::ApiError;
use crate::routes::{
    actions_execute_handler, agents_handler, agents_schema_handler, get_agent_config_handler,
    get_pipeline_config_with_etag, get_plugin_config_with_etag, health_handler,
    metrics_prometheus_handler, pipelines_handler, pipelines_runs_handler, pipelines_state_handler,
    plugins_set_enabled_handler, plugins_status_handler, put_agent_config_handler,
    put_pipeline_config_handler, put_plugin_config_handler, schema_handler, serve_upload_handler,
    system_restart_handler, tools_handler, validate_all_plugins_handler, AppState,
};
use crate::session_routes::{
    create_session_handler, delete_session_handler, list_session_messages_handler,
    list_sessions_handler, sessions_schema_handler, update_session_agent_handler,
    update_session_handler,
};

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
        .route("/api/v1/agents", get(agents_handler))
        // 阶段1:agent schema(前端表单驱动)+ agent config 读写(真相源 config/agents/**/*.yaml)
        .route("/api/v1/agents/schema", get(agents_schema_handler))
        .route(
            "/api/v1/agents/{id}/config",
            get(get_agent_config_handler).put(put_agent_config_handler),
        )
        // 阶段3:命令执行统一出口(前端 GrowthLoop.ts commandDispatcher transport 注入此端点)
        // 命令面板/快捷键/菜单触发 → 查找声明该 command 的插件 → 执行或占位
        .route("/api/v1/actions/execute", post(actions_execute_handler))
        .route("/api/v1/pipelines", get(pipelines_handler))
        .route("/api/v1/pipelines/runs", get(pipelines_runs_handler))
        .route("/api/v1/pipelines/state", get(pipelines_state_handler))
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
        // AC-06-4: WebSocket 端点
        .route("/ws", get(ws_handler))
        // task_11 A2：前端写死连 /ws/chat（0.1 路径格式），加别名指向同一 handler，
        // 保证 0.2 模式下前端直连内核可用；/ws 保留给新客户端。
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
/// - `PUT /api/v1/agents/{id}/config`、`PUT /api/v1/config/pipelines/{name}`
/// - `POST /api/v1/chat`（0.2 收紧：原来放行匿名——消息触发管道执行/落库，
///   属写面语义，未鉴权等于任意人可驱动执行与消耗算力）
///
/// 其余路径放行（/health、/api/v1/auth/*、/ws、/api/v1/db/* 等）。
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
        || (path.starts_with("/api/v1/agents/")
            && path.ends_with("/config")
            && method == Method::PUT)
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
    let (_, _, role, _) = crate::auth::resolve_request_user(state.store.as_ref(), headers).await?;
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
        // B3：前端重连时上报 last_sequence（全局 watermark），用于首个 thread 注册时回放断线期间事件。
        let last_sequence = params
            .get("last_sequence")
            .and_then(|s| s.parse::<u64>().ok());
        return ws.on_upgrade(move |socket| {
            run_p2_ws_session(socket, session, router, token, last_sequence)
        });
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
// 2. 加载 Agent 配置注入 state（system_prompt / tool_ids / model_tier / max_iterations）
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
fn maybe_reload_compiled_pipeline(
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
    let known_ids: std::collections::HashSet<String> = state.plugin_ids.iter().cloned().collect();
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
/// - `failed`：executor.run 返回 Err（WS 路径据此推 stream_error 收尾）。
pub(crate) struct EngineOutcome {
    pub content: String,
    pub final_assistant: Option<serde_json::Value>,
    pub failed: bool,
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
    )
    .await;

    // 1b. 多轮上下文装配（热路径 registry / 冷路径 checkpoint+traces+state+messages）
    //     + 本轮 user 消息入账。
    let mut initial_state = stage_recover_history(
        initial_state,
        &store,
        message,
        thread_id,
        effective_pipeline_id,
        &tenant_id,
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
    // GAP-1：run 终态即任务终态——回写 task.status（热路径 registry + 冷路径
    // pipeline_state 标量表；suspended 不回写）。fire-and-forget 语义：失败仅
    // 告警，不影响响应。
    writeback_task_terminal_status(
        state,
        &final_state,
        outcome.failed,
        &tenant_id,
        effective_pipeline_id,
    )
    .await;
    // GAP-2：run 终态域事件（completed/suspended）——state 带 task.* 时派生
    // task_completed（EVENT 触发器输入源）。fire-and-forget，不影响响应。
    emit_run_terminal_domain_events(state, &final_state, outcome.failed).await;
    // GAP-1 统一：run 终态即任务终态——task.status/ended_at 回写 state 真值
    // （registry + pipeline_state 表），任务树/评估从 state 直读。
    finalize_task_status_in_state(
        state,
        &final_state,
        outcome.failed,
        &tenant_id,
        effective_pipeline_id,
    )
    .await;
    outcome
}

/// GAP-2：从 run 终态 state 派生域事件（run.* 终态 + 任务域派生）。
///
/// - `failed=true` → `run.failed`（state 带 `task.*` 字段时追加 `task_failed`）
/// - `suspended` 标志（RouteNext::Wait 落档）→ `run.suspended`（等待人工交互，
///   无任务派生——收口把关是 child_task_guard 的结构性职责）
/// - 否则 → `run.completed`（state 带 `task.*` 字段时追加 `task_completed`）
///
/// 任务派生判据：state 存在任意 `task.` 前缀键（task = pipeline 单一真值，
/// 任务管道的 state 出生即带 task.* 字段）。事件经 [`broadcast_domain_event`]
/// 双通道投递：观察总线 + 点对点推给声明 domain_event hook 的订阅插件
/// （triggers_ext → evaluate_event——EVENT 触发器的输入源）。
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
    let v = |k: &str| final_state.get(k).cloned().unwrap_or(serde_json::Value::Null);
    let pipeline_id = v("pipeline_id");
    let thread_id = v("session_id");
    let has_task = final_state
        .as_object()
        .map(|o| o.keys().any(|k| k.starts_with("task.")))
        .unwrap_or(false);
    let task_id = v("task.id");
    // 子任务通知锚点：lineage.parent_pipeline_id 扁平键（有父形式出生写入）
    let parent_pipeline_id = v("lineage.parent_pipeline_id");

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

/// GAP-1 统一：run 终态即任务终态——state 带 `task.*` 字段的管道，把
/// `task.status` / `task.ended_at` 回写 state 单一真值。
///
/// 双写目标：
/// 1. **registry 常驻 state**（热路径：/api/v1/pipelines/state 内存行直接读）；
/// 2. **pipeline_state 表**（冷路径：重启后 checkpoint 冷恢复按
///    `checkpoint 标量 → pipeline_state 表补充` 的顺序合并，表的最新值覆盖
///    checkpoint 里的出生值 pending——冷恢复也拿到终态）。
///
/// 任务树/评估/门控全部从 state 直读后，YAML 镜像不再被任何写路径触碰。
async fn finalize_task_status_in_state(
    state: &AppState,
    final_state: &serde_json::Value,
    failed: bool,
    tenant_id: &str,
    effective_pipeline_id: &str,
) {
    let has_task = final_state
        .as_object()
        .map(|o| o.keys().any(|k| k.starts_with("task.")))
        .unwrap_or(false);
    if !has_task || effective_pipeline_id.is_empty() {
        return;
    }
    let status = if failed {
        "failed"
    } else if final_state
        .get("suspended")
        .and_then(|s| s.as_bool())
        .unwrap_or(false)
    {
        "suspended"
    } else {
        "completed"
    };
    use serde_json::json;

    let ended_at = chrono::Utc::now().to_rfc3339();
    let mut merged = final_state.clone();
    if let Some(obj) = merged.as_object_mut() {
        obj.insert("task.status".to_string(), json!(status));
        obj.insert("task.ended_at".to_string(), json!(ended_at));
    }
    // 热路径：registry 常驻 state 回写
    let reg = agentos_session::global_registry();
    if reg.contains(tenant_id, effective_pipeline_id) {
        reg.update_state(tenant_id, effective_pipeline_id, merged.clone());
    }
    // 冷路径：pipeline_state 表（upsert_state_field 现成，O(1) 单键写）
    if let Some(store) = state.store.as_ref() {
        let _ = store
            .upsert_state_field(effective_pipeline_id, tenant_id, "task.status", &json!(status))
            .await;
        let _ = store
            .upsert_state_field(effective_pipeline_id, tenant_id, "task.ended_at", &json!(ended_at))
            .await;
    }
    info!(
        target: "task-state",
        pipeline = %effective_pipeline_id,
        status,
        "任务终态已回写 state 单一真值（registry + pipeline_state 表）"
    );
}

/// 派生任务管道的终态 `task.status`（GAP-1：task = pipeline state 单一真值）。
///
/// 任务管道的 state 出生即带 `task.*` 字段；run 终态即任务终态。返回
/// `Some(status)` 时调用方应把 `task.status` 推进为终态；`None` = 不回写：
/// - 非任务管道（无 `task.` 前缀键——精确前缀，`taskx` 不误判）；
/// - suspended（等待人工交互，任务状态保持 running，不写终态）。
fn derive_task_terminal_status(
    final_state: &serde_json::Value,
    failed: bool,
) -> Option<&'static str> {
    let has_task = final_state
        .as_object()
        .map(|o| o.keys().any(|k| k.starts_with("task.")))
        .unwrap_or(false);
    if !has_task {
        return None;
    }
    let suspended = final_state
        .get("suspended")
        .and_then(|s| s.as_bool())
        .unwrap_or(false);
    if suspended {
        return None;
    }
    Some(if failed { "failed" } else { "completed" })
}

/// GAP-1：run 终态回写 `task.status` 到 state（双落点，失败仅告警不阻断）。
///
/// task = pipeline state 单一真值：任务管道的终态由 run 终态决定（completed/
/// failed；suspended 不回写）。回写双落点保证热冷路径一致：
/// ① 内存 registry（热路径，`/api/v1/pipelines/state` 实时数据源）；
/// ② pipeline_state 标量表（冷路径，重启后 checkpoint 之上的覆盖层——
///    冷恢复顺序为 checkpoint → pipeline_state 补充，见 stage_recover_history
///    第③步，因此标量表回写即可让冷启动读到终态）。
async fn writeback_task_terminal_status(
    state: &AppState,
    terminal_state: &serde_json::Value,
    failed: bool,
    tenant_id: &str,
    effective_pipeline_id: &str,
) {
    let Some(status) = derive_task_terminal_status(terminal_state, failed) else {
        return;
    };
    if effective_pipeline_id.is_empty() {
        return;
    }
    let status_val = serde_json::json!(status);

    // ① 热路径：内存 registry（registry 回写发生在 stage_finalize，此处覆盖 task.status）
    let reg = agentos_session::global_registry();
    if let Some(entry) = reg.get(tenant_id, effective_pipeline_id) {
        let mut new_state = entry.read().state.clone();
        new_state["task.status"] = status_val.clone();
        reg.update_state(tenant_id, effective_pipeline_id, new_state);
    }

    // ② 冷路径：pipeline_state 标量表（重启后覆盖 checkpoint 的旧 task.status）
    if let Some(db) = state.db.as_ref() {
        let db = db.clone();
        let pid = effective_pipeline_id.to_string();
        let tid = tenant_id.to_string();
        let value = status_val.clone();
        let write_pid = pid.clone();
        if let Err(e) = tokio::task::spawn_blocking(move || {
            db.upsert_state_field(&pid, &tid, "task.status", &value)
        })
        .await
        {
            tracing::warn!(
                pipeline_id = %write_pid,
                error = %e,
                "task.status 终态回写失败（继续执行）"
            );
        }
    }
    tracing::info!(
        pipeline_id = %effective_pipeline_id,
        task_status = status,
        "run 终态回写 task.status"
    );
}

/// 前置依赖缺失时的 echo 降级应答（invoker/store/project_root 任一缺席）。
fn echo_fallback(missing: &str, message: &str) -> EngineOutcome {
    EngineOutcome {
        content: format!("[echo-fallback: {missing}] {message}"),
        final_assistant: None,
        failed: false,
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
) -> serde_json::Value {
    let mut initial_state = serde_json::json!({
        "message": message,
        "input": message,
        "agent_id": agent_id,
        "core_type": "llm_call",
        "core_plugin": DEFAULT_CORE_PLUGIN,
        "ended": false,
        "suspended": false,
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
    if let Ok(Some(sess)) = store.get_session(thread_id).await {
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
/// 防御语义（调用方 chat.send_handler 已做保留字校验，此处纵深防御）：
/// - 引擎系统保留字（[`crate::chat_send_handler::RESERVED_STATE_KEYS`]）跳过，
///   非 chat 入口的理论调用者无法覆写 message/pipeline_id 等系统字段；
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
        if crate::chat_send_handler::RESERVED_STATE_KEYS.contains(&k.as_str()) {
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
#[allow(clippy::too_many_arguments)]
async fn stage_recover_history(
    mut initial_state: serde_json::Value,
    store: &Arc<dyn StorageBackend>,
    message: &str,
    thread_id: &str,
    effective_pipeline_id: &str,
    tenant_id: &str,
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
            if let Ok(Some((_step_no, ckpt_state))) = store
                .load_latest_checkpoint(effective_pipeline_id, tenant_id)
                .await
            {
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
            if let Ok(state_fields) = store
                .load_pipeline_state(effective_pipeline_id, tenant_id)
                .await
            {
                if let Some(rec_obj) = recovered.as_object_mut() {
                    for (k, v) in &state_fields {
                        rec_obj.insert(k.clone(), v.clone());
                    }
                }
            }
        }
        // messages 直读 message_slots（零回放；元素自带稳定 seq）
        if !effective_pipeline_id.is_empty() {
            if let Ok(msgs) = store
                .load_message_history(effective_pipeline_id, tenant_id)
                .await
            {
                history_prefix = msgs;
            }
        }
        // 标量字段注入 state（GAP-3：跳过易变 per-run 键——修复前已写的旧
        // checkpoint 仍携带 message/input/suspended 等，覆盖会顶掉本轮新输入，
        // 导致重启后旧 user 消息被重放消费。键集与 checkpoint 瘦身同源）。
        if let Some(rec_obj) = recovered.as_object_mut() {
            rec_obj.remove("messages");
            if let Some(init_obj) = initial_state.as_object_mut() {
                for (k, v) in rec_obj.iter() {
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
    // 重复落槽+重复消费（e2e 重复 run/陈旧回复病根②）。tail 为同文 user →
    // 跳过 append，引擎基于既有历史继续执行并产出回复。
    let interrupted_tail = initial_state
        .get("messages")
        .and_then(|v| v.as_array())
        .and_then(|msgs| msgs.last())
        .is_some_and(|last| {
            last.get("role").and_then(|r| r.as_str()) == Some("user")
                && last.get("content").and_then(|c| c.as_str()) == Some(message)
        });
    // user 经 append op(无 seq → 引擎分配 next seq)+ 落 message_slots。
    // 指纹实录塞 _pending_message_ops（内部字段）：executor.persist_run_start 落一条
    // user_input 轨迹后移除——首轮 user 由此进入审计/回放范围（ops 即轨迹）。
    if !interrupted_tail {
    if let Ok(user_ledger) = agentos_engine::apply_messages_op_update(
        &mut initial_state,
        store.as_ref(),
        tenant_id,
        &[serde_json::json!({"op":"set","msg":{"role":"user","content":message}})],
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

/// 阶段 2/2b：加载 Agent 配置注入 state（读 config/agents/<agent_id>.yaml，不存在跳过）
/// + 注入工具 schema。
///
/// 统一配置加载方案 TDD-6：优先走 ConfigCenter（泛化注入所有字段），
/// 未接线时降级到旧的 load_agent_config_into_state（挑 5 字段，兼容）。
fn stage_inject_agent_and_tools(
    state: &AppState,
    initial_state: &mut serde_json::Value,
    agent_id: &str,
    project_root: &std::path::Path,
) {
    if let Some(cc) = state.config_center.as_ref() {
        agentos_config::load_agent_into_state(cc, initial_state, agent_id);
    } else {
        load_agent_config_into_state(initial_state, agent_id, project_root);
    }

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
/// run_id / branch_id 用 uuid 保证多请求隔离；租户上下文从 task_local 读取
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
) -> Result<serde_json::Value, EngineOutcome> {
    let run_id = uuid::Uuid::new_v4().to_string();
    let branch_id = "main".to_string();

    // ── Pull 热加载（在 project_root 被 move 给 executor 之前算出 config_root）──
    let config_root = project_root.join("config");
    let compiled = maybe_reload_compiled_pipeline(state, &config_root);
    let executor = agentos_engine::PipelineExecutor::new(
        invoker,
        project_root,
        tenant,
        state.plugin_ids.iter().cloned(),
        store.clone(),
        run_id.clone(),
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

    // 统一配置加载方案 TDD-7：注入 ConfigCenter，启用 per-iteration agent 热加载。
    // 改 config/agents/<id>.yaml 后，正在跑的任务下一轮迭代立即用新配置。
    let executor = if let Some(cc) = state.config_center.clone() {
        executor.with_config_center(cc)
    } else {
        executor
    };

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
            ("pipeline_id", initial_state.get("pipeline_id").cloned().unwrap_or(serde_json::Value::Null)),
            ("thread_id", initial_state.get("session_id").cloned().unwrap_or(serde_json::Value::Null)),
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
                .update_run_status(&run_id, agentos_core::types::RunStatus::Failed, None, None)
                .await
            {
                warn!(run_id = %run_id, error = %pe, "update_run_status(Failed) 失败（继续）");
            }
            Err(EngineOutcome {
                content: format!("[engine-run-failed] {message}"),
                final_assistant: None,
                failed: true,
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
    EngineOutcome {
        content,
        final_assistant,
        failed: false,
    }
}

/// 响应内容提取：优先 raw_result，回退 state.message，再回退固定文案。
///
/// A12：兜底不再把整份内部 state pretty-print 给客户端（内部结构/工具细节泄漏），
/// 固定返回 "pipeline finished"；完整 state 保留在服务端 tracing（debug 级）。
fn extract_response_content(final_state: &serde_json::Value) -> String {
    if let Some(raw) = final_state.get("raw_result").and_then(|v| v.as_str()) {
        raw.to_string()
    } else if let Some(msg) = final_state.get("message").and_then(|v| v.as_str()) {
        msg.to_string()
    } else {
        debug!(
            target: "chat-response",
            state = ?final_state,
            "pipeline finished without raw_result/message（完整 state 仅留服务端日志）"
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
/// 格式（`{type:"function", function:{name, description, parameters}}`）。tool_ids
/// 缺失时注入全部工具（兜底）。registry 不可用时注入空列表（LLM 无工具可用）。
fn inject_tool_schemas(state: &mut serde_json::Value, app_state: &AppState) {
    let Some(registry) = app_state.capability_registry.as_ref() else {
        return;
    };
    let all_tools = registry.list_tools();

    // 按 agent 的 tool_ids 过滤；缺失则用全部（兜底，避免无工具可用）
    let wanted_ids: Option<Vec<String>> =
        state.get("tool_ids").and_then(|v| v.as_array()).map(|arr| {
            arr.iter()
                .filter_map(|t| t.as_str().map(String::from))
                .collect()
        });
    let wanted: Option<std::collections::HashSet<String>> =
        wanted_ids.map(|ids| ids.into_iter().collect());
    let schemas: Vec<serde_json::Value> = all_tools
        .iter()
        .filter(|t| match &wanted {
            Some(ids) => {
                // tool_ids 命中 或 框架级强制工具（spill_retrieve 等，无视 agent 配置）
                ids.contains(&t.name) || FRAMEWORK_ALWAYS_INCLUDE_TOOLS.contains(&t.name.as_str())
            }
            None => true,
        })
        .filter(|t| {
            // LLM 严格校验工具 schema:parameters 必须是 type:object 的 JSON Schema。
            // 过滤掉 input_schema 缺失/非 object 的工具(如 simple_tools 的部分工具
            // manifest 未声明 input_schema),否则 DeepSeek/OpenAI 拒绝整个请求
            // (BadRequest: schema must be a JSON Schema of 'type: "object"')。
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

/// 在 agents 目录（含分类子目录）递归查找 `<agent_id>.yaml`。
///
/// agents/ 按分类组织为 `agents/<category>/<id>.yaml`（main/orchestrator/
/// executor/system/task/test），顶层不再放单文件。返回首个匹配路径。
/// pub(crate)：routes.rs 的 agent config 读写端点复用同一套定位逻辑。
pub(crate) fn find_agent_yaml(dir: &std::path::Path, agent_id: &str) -> Option<std::path::PathBuf> {
    let target = format!("{}.yaml", agent_id);
    let Ok(entries) = std::fs::read_dir(dir) else {
        return None;
    };
    // 两轮：先按文件名精确匹配（快路径），未命中再按 yaml 内 config_id 匹配——
    // 大量执行 agent 的文件名与 config_id 不同（如 code_writer.yaml 的
    // config_id=code_writer_agent，LLM 派发用的是 config_id）。
    let mut fallback: Vec<std::path::PathBuf> = Vec::new();
    for entry in entries.flatten() {
        let p = entry.path();
        if p.is_dir() {
            if let Some(found) = find_agent_yaml(&p, agent_id) {
                return Some(found);
            }
        } else if p.file_name().map(|n| n == target.as_str()).unwrap_or(false) {
            return Some(p);
        } else if p.extension().map(|e| e == "yaml").unwrap_or(false) {
            fallback.push(p);
        }
    }
    for p in fallback {
        if let Ok(raw) = std::fs::read_to_string(&p) {
            if let Ok(cfg) = serde_yaml::from_str::<serde_yaml::Value>(&raw) {
                if cfg.get("config_id").and_then(|v| v.as_str()) == Some(agent_id) {
                    return Some(p);
                }
            }
        }
    }
    None
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
    project_root: &std::path::Path,
) {
    // Agent 配置在 config/agents/ 下（按分类子目录 main/orchestrator/executor/…）。
    // project_root 是项目根（config/ 的父目录），拼 config/agents。
    let agents_dir = project_root.join("config").join("agents");
    let top = agents_dir.join(format!("{}.yaml", agent_id));
    let path = if top.is_file() {
        top
    } else {
        match find_agent_yaml(&agents_dir, agent_id) {
            Some(p) => p,
            None => {
                tracing::debug!(
                    agent_id = %agent_id,
                    "Agent config not found under {}, using defaults",
                    agents_dir.display()
                );
                return;
            }
        }
    };
    let raw = match std::fs::read_to_string(&path) {
        Ok(s) => s,
        Err(_) => {
            tracing::debug!(
                agent_id = %agent_id,
                "Agent config not readable at {}, using defaults",
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
    // core_plugin：agent 配置优先于内核默认值（DEFAULT_CORE_PLUGIN）。
    // 用 insert 直接覆盖，使 agent 能切换核心插件（如换 LLM 提供商）。
    if let Some(v) = entry("core_plugin") {
        obj.insert("core_plugin".to_string(), v);
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
    if let Err(e) = sender.send(Message::Text(welcome_json.into())).await {
        warn!("Failed to send WS welcome message: {e}");
    }

    info!("WebSocket connection established");

    // 收发循环
    while let Some(Ok(msg)) = receiver.next().await {
        match msg {
            Message::Text(text) => {
                // 解析客户端消息
                let req: WsRequest = serde_json::from_str(&text).unwrap_or(WsRequest {
                    message: text.to_string(),
                    session_id: String::new(),
                    history: Vec::new(),
                    agent_id: String::new(),
                });

                // 在租户上下文内通过管道引擎处理消息（多租户 P0-4）
                let tenant_ctx =
                    request_tenant_ctx(state.store.as_ref(), &headers, &req.session_id).await;
                let content = agentos_tenant::scope(
                    tenant_ctx,
                    process_via_engine(
                        &state,
                        &req.message,
                        if req.agent_id.is_empty() {
                            "agentos"
                        } else {
                            req.agent_id.as_str()
                        },
                        &req.history,
                        "",
                        "",
                        "",
                        "",
                        "",
                        None,
                        None,
                    ),
                )
                .await
                .content;

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
            _ => String::new(),
        }
    } else {
        String::new()
    };
    // 解析请求用户 user_id（从 Authorization token），供 process_via_engine 写入 state；
    // 触发器等工具据此捕获 user_id，触发时回派发（chat.send_message）解析 tenant。
    let user_id = crate::auth::resolve_request_user(state.store.as_ref(), &headers)
        .await
        .map(|(uid, _, _, _)| uid)
        .unwrap_or_default();
    // ADR-2026-08-15：HTTP 路径同步返回 outcome，但执行必须入管道链——与 WS /
    // chat.send_message 共用同一条 FIFO，消除 HTTP 与 WS 同会话并发 run 的竞态。
    // key 与 process_via_engine_inner 的 effective_pipeline_id 同式（空则回退
    // thread_id），保证跨入口落到同一条链。
    let chain_key = if pipeline_id.is_empty() {
        req.session_id.clone()
    } else {
        pipeline_id.clone()
    };
    let registry = crate::run_chain::RunChainRegistry::global();
    registry.note_user_pipeline(&user_id, &chain_key);
    let (tx, rx) = tokio::sync::oneshot::channel::<String>();
    let exec_state = state.clone();
    let exec_message = req.message.clone();
    let exec_agent = if req.agent_id.is_empty() {
        "agentos".to_string()
    } else {
        req.agent_id.clone()
    };
    let exec_history = req.history.clone();
    let exec_pipeline = pipeline_id.clone();
    let exec_session = req.session_id.clone();
    let exec_user = user_id.clone();
    registry.enqueue(&chain_key, &user_id, async move {
        let content = agentos_tenant::scope(
            tenant_ctx,
            process_via_engine(
                &exec_state,
                &exec_message,
                &exec_agent,
                &exec_history,
                &exec_pipeline,
                &exec_session,
                "",
                &exec_user,
                "",
                None,
                None,
            ),
        )
        .await
        .content;
        let _ = tx.send(content);
    });
    // 任务 panic（rx 关闭）时给出明确错误而非挂死等待。
    let content = rx
        .await
        .unwrap_or_else(|_| "[engine task terminated unexpectedly]".to_string());

    let response = WsResponse {
        r#type: "message".to_string(),
        content,
        session_id: req.session_id,
        timestamp: chrono::Utc::now().to_rfc3339(),
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

    /// 登录内置 admin（无 store 时回退内置用户表）返回 access_token。
    async fn admin_token(app: &axum::Router) -> String {
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"username": "admin", "password": "admin12345"}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        v["access_token"].as_str().unwrap().to_string()
    }

    /// G2：validate-all 全量巡检——声明 vs 实际对照，漂移分类报告。
    #[tokio::test]
    async fn test_validate_all_reports_drift_and_clean() {
        let mut state = AppState::new();
        // 两个 tool 插件：p_drift（声明 t1+ghost，上报只有 t1 → missing 漂移）、
        // p_clean（声明 t2，上报一致 → clean）
        let mk_manifest = |id: &str, tools: &[&str]| -> agentos_core::traits::PluginManifest {
            serde_json::from_value(json!({
                "id": id, "name": id, "version": "1.0.0",
                "plugin_type": "tool", "language": "python",
                "host_type": "sidecar", "entry": "python server.py",
                "capabilities": { "tools": tools.iter().map(|t| {
                    json!({"name": t, "description": t})
                }).collect::<Vec<_>>() },
            }))
            .expect("valid manifest")
        };
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
            mk_manifest("p_drift", &["t1", "ghost"]),
            mk_manifest("p_clean", &["t2"]),
        ]));
        let invoker = Arc::new(RecordingInvoker {
            seen: std::sync::Mutex::new(Vec::new()),
            seen_states: std::sync::Mutex::new(Vec::new()),
            hooks: std::sync::Mutex::new(Vec::new()),
            list_tools: std::collections::HashMap::from([
                (
                    "p_drift".to_string(),
                    json!({ "tools": [{"name": "t1", "description": "t1"}] }),
                ),
                (
                    "p_clean".to_string(),
                    json!({ "tools": [{"name": "t2", "description": "t2"}] }),
                ),
            ]),
        });
        state.invoker = Some(invoker);

        let resp = validate_all_plugins_handler(axum::extract::State(state)).await;
        let body = resp.0;
        assert_eq!(body["checked"], 2);
        assert_eq!(body["clean"], 1);
        assert_eq!(body["drifted"], 1);
        assert_eq!(body["errors"], 0);
        let drift_report = body["reports"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["plugin_id"] == "p_drift")
            .expect("p_drift 报告");
        assert_eq!(drift_report["status"], "drifted");
        let kinds: Vec<&str> = drift_report["mismatches"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| m["kind"].as_str().unwrap())
            .collect();
        assert_eq!(kinds, vec!["missing"], "ghost 声明有实际无 → missing");
        let clean_report = body["reports"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["plugin_id"] == "p_clean")
            .expect("p_clean 报告");
        assert_eq!(clean_report["status"], "clean");
        assert_eq!(clean_report["mismatches"].as_array().unwrap().len(), 0);
    }

    /// G2：validate-all 在 invoker 未接线时返回错误计数（不 panic）。
    #[tokio::test]
    async fn test_validate_all_without_invoker_reports_error() {
        let state = AppState::new(); // invoker = None
        let resp = validate_all_plugins_handler(axum::extract::State(state)).await;
        assert_eq!(resp.0["errors"], 1);
        assert!(resp.0["message"].as_str().is_some());
    }

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

    /// 剩余项清仓 D2：schema ETag——首次 200 带 ETag；If-None-Match 命中
    /// （含 *）返回 304 空体；未命中返回 200 新体。
    #[tokio::test]
    async fn test_schema_etag_if_none_match_304() {
        let app = build_router(AppState::new());

        // 首次：200 + ETag 响应头
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/v1/schema")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let etag = resp
            .headers()
            .get("etag")
            .and_then(|v| v.to_str().ok())
            .expect("ETag header")
            .to_string();

        // If-None-Match 命中 → 304 空体（ETag 仍带回，便于客户端续用）
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/v1/schema")
                    .header("If-None-Match", &etag)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_MODIFIED);
        assert_eq!(
            resp.headers().get("etag").and_then(|v| v.to_str().ok()),
            Some(etag.as_str())
        );
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        assert!(body.is_empty(), "304 必须空体");

        // If-None-Match: * → 304
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/v1/schema")
                    .header("If-None-Match", "*")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_MODIFIED);

        // 未命中的 ETag → 200 全量体
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/schema")
                    .header("If-None-Match", "\"stale-etag\"")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 65536).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        // 响应体形状不变（agents/pipelines/tools/routes/plugin_*）
        for key in ["agents", "pipelines", "tools", "routes"] {
            assert!(json.get(key).is_some(), "schema 响应应含 {key}");
        }
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
        // A11：chat 已纳入写面鉴权，先 login 拿 token
        let token = admin_token(&app).await;
        let body = serde_json::to_string(&WsRequest {
            message: "hello".to_string(),
            session_id: "s1".to_string(),
            history: Vec::new(),
            agent_id: String::new(),
        })
        .unwrap();
        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/chat")
                    .header("authorization", format!("Bearer {token}"))
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    /// A11：匿名 POST /api/v1/chat → 401（0.2 收紧：消息驱动管道执行属写面）。
    #[tokio::test]
    async fn test_chat_post_anonymous_returns_401() {
        let app = build_router(AppState::new());
        let body = serde_json::to_string(&WsRequest {
            message: "hello".to_string(),
            session_id: "s1".to_string(),
            history: Vec::new(),
            agent_id: String::new(),
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
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
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
        // A11：chat 已纳入写面鉴权，先 login 拿 token
        let token = admin_token(&app).await;
        let body = serde_json::to_string(&WsRequest {
            message: "hello world".to_string(),
            session_id: "test_session".to_string(),
            history: Vec::new(),
            agent_id: String::new(),
        })
        .unwrap();
        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/chat")
                    .header("authorization", format!("Bearer {token}"))
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
        // 验证 tools handler 从 config 返回工具列表（无 registry 时）。
        // W-C2：响应信封统一为 {items, total}（对齐 /api/v1/agents）。
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
        let items = json["items"].as_array().expect("应含 items 数组");
        assert_eq!(json["total"], 1);
        assert_eq!(items.len(), 1);
        assert_eq!(items[0]["name"], "calculator");
    }

    // ── 监控 M5/M5b：指标查询端点 + Prometheus 导出端点 ──

    fn state_with_metrics() -> AppState {
        use crate::metrics::{Labels, MetricType, MetricsAggregator};
        let agg = MetricsAggregator::new();
        let mut labels = Labels::new();
        labels.insert("model".to_string(), "deepseek".to_string());
        agg.record(
            "llm_service",
            "tokens_used",
            MetricType::Counter,
            12800.0,
            &labels,
            Some("tokens"),
            Some("Total tokens used"),
        );
        agg.record(
            "llm_service",
            "latency",
            MetricType::Histogram,
            0.02,
            &Labels::new(),
            Some("seconds"),
            Some("LLM latency"),
        );
        AppState::new().with_metrics(agg)
    }

    #[tokio::test]
    async fn test_metrics_query_endpoint_migrated_to_plugin() {
        // boot-plugin 第三刀：查询面迁 /ext/metrics_admin/query（metrics-admin
        // capability，语义测试在 metrics/capability.rs）；旧内核路由应已摘除。
        let app = build_router(state_with_metrics());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/metrics?plugin=llm_service")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn test_metrics_prometheus_endpoint() {
        let app = build_router(state_with_metrics());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/metrics")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = axum::body::to_bytes(response.into_body(), 8192)
            .await
            .unwrap();
        let text = String::from_utf8(body.to_vec()).unwrap();
        // counter 导出
        assert!(text.contains("# HELP llm_service_tokens_used Total tokens used"));
        assert!(text.contains("# TYPE llm_service_tokens_used counter"));
        assert!(text.contains("llm_service_tokens_used{model=\"deepseek\"}"));
        // histogram 导出
        assert!(text.contains("# TYPE llm_service_latency histogram"));
        assert!(text.contains("llm_service_latency_bucket{le=\"0.025\"}"));
        assert!(text.contains("llm_service_latency_bucket{le=\"+Inf\"}"));
        assert!(text.contains("llm_service_latency_count"));
    }

    #[tokio::test]
    async fn test_metrics_prometheus_no_aggregator_404() {
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/metrics")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }

    // ── 多轮对话上下文修复集成测试 ──────────────────────────────
    // 验证：process_via_engine_inner 从 store/registry 加载历史，state["messages"]
    // 组装为完整消息序列（历史 + 当前 user），第二轮能看到第一轮上下文。

    /// 模拟 LLM 插件：读取 state["messages"]，append assistant 回复后写回。
    /// 记录每次收到的 messages（按调用顺序），供测试断言。
    struct RecordingInvoker {
        seen: std::sync::Mutex<Vec<serde_json::Value>>,
        /// GAP-1：记录每次收到的完整 state 快照（断言 state overlay / lineage
        /// 是否进入插件可见 state）。
        seen_states: std::sync::Mutex<Vec<serde_json::Value>>,
        /// G2：list_plugin_tools 响应（plugin_id → tools/list JSON）。缺省空。
        list_tools: std::collections::HashMap<String, serde_json::Value>,
        /// GAP-2：记录收到的生命周期钩子 (plugin_id, hook 名, ctx JSON)。
        hooks: std::sync::Mutex<Vec<(String, String, serde_json::Value)>>,
    }

    #[async_trait::async_trait]
    impl agentos_core::traits::PluginInvoker for RecordingInvoker {
        async fn invoke_pipeline_plugin(
            &self,
            _plugin_id: &str,
            ctx: &agentos_core::types::PluginContext,
        ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
            let history = ctx
                .state
                .get("messages")
                .cloned()
                .unwrap_or_else(|| serde_json::json!([]));
            self.seen.lock().unwrap().push(history.clone());
            self.seen_states.lock().unwrap().push(ctx.state.clone());
            // 模拟 LLM：构造 assistant 回复（内容基于收到的消息数，便于断言），
            // 以增量 op emit（零兼容：所有插件一律 op 模型，无全量数组分支）
            let reply_msg = serde_json::json!({
                "role": "assistant",
                "content": format!("回复第{}条", history.as_array().map(|a| a.len()).unwrap_or(1)),
            });
            let reply = reply_msg["content"].as_str().unwrap_or("").to_string();
            let mut updates = std::collections::HashMap::new();
            updates.insert("raw_result".to_string(), serde_json::json!(reply));
            updates.insert(
                "messages".to_string(),
                serde_json::json!({ "_ops": [{ "op": "set", "msg": reply_msg }] }),
            );
            Ok(agentos_core::types::PluginResult {
                state_updates: updates,
                ..Default::default()
            })
        }

        async fn invoke_tool(
            &self,
            _plugin_id: &str,
            _tool_name: &str,
            _inputs: &serde_json::Value,
        ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError>
        {
            Ok(agentos_core::types::ToolExecutionResult::success(
                serde_json::Value::Null,
            ))
        }

        async fn send_lifecycle_hook(
            &self,
            plugin_id: &str,
            hook: agentos_core::traits::LifecycleHook,
            context: &agentos_core::traits::HookContext,
        ) -> Result<(), agentos_core::types::PluginError> {
            let tag = |k: &str| context.get(k).cloned().unwrap_or(serde_json::Value::Null);
            self.hooks.lock().unwrap().push((
                plugin_id.to_string(),
                format!("{hook:?}"),
                serde_json::json!({
                    "event": tag("event"),
                    "pipeline_id": tag("pipeline_id"),
                    "task_id": tag("task_id"),
                    "parent_pipeline_id": tag("parent_pipeline_id"),
                }),
            ));
            Ok(())
        }
        async fn list_plugin_tools(
            &self,
            plugin_id: &str,
        ) -> Result<serde_json::Value, agentos_core::types::PluginError> {
            Ok(self
                .list_tools
                .get(plugin_id)
                .cloned()
                .unwrap_or(serde_json::json!({ "tools": [] })))
        }
    }

    /// 构造带 store + mock invoker 的 AppState（enable_session 以启用 registry 路径）。
    /// 创建临时 config 目录 + autonomous.yaml（引用 mock LLM 插件），使
    /// maybe_reload_pipeline_configs 能加载真实配置（否则 load_pipeline_config
    /// 在文件缺失时返回空 steps 配置，executor 不会调用任何插件）。
    fn make_engine_state() -> (
        AppState,
        Arc<RecordingInvoker>,
        Arc<dyn agentos_core::traits::StorageBackend>,
        Arc<agentos_engine::SqliteStore>,
    ) {
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let store: Arc<dyn agentos_core::traits::StorageBackend> = sqlite.clone();
        let invoker = Arc::new(RecordingInvoker {
            seen: std::sync::Mutex::new(Vec::new()),
            seen_states: std::sync::Mutex::new(Vec::new()),
            hooks: std::sync::Mutex::new(Vec::new()),
            list_tools: std::collections::HashMap::new(),
        });
        // 临时项目根：含 config/pipelines/autonomous.yaml，引用 mock LLM 插件
        let tmp_root =
            std::env::temp_dir().join(format!("mt_test_{}", uuid::Uuid::new_v4().simple()));
        let cfg_dir = tmp_root.join("config").join("pipelines");
        std::fs::create_dir_all(&cfg_dir).unwrap();
        std::fs::write(
            cfg_dir.join("autonomous.yaml"),
            "name: test_multi_turn\nloop_bodies:\n  - id: main\n    steps:\n      - id: llm\n        steps:\n          - mock_llm_core\n",
        )
        .unwrap();
        let mut state = AppState::new();
        state.store = Some(store.clone());
        state.invoker = Some(invoker.clone());
        state.project_root = Some(tmp_root);
        // 注入统一数据接口句柄（writeback_task_terminal_status 冷路径回写依赖）
        state.db = Some(sqlite.clone());
        // 兜底配置（与临时 YAML 一致；临时 YAML 加载成功时此值被覆盖）
        state.pipeline_config = Arc::new(agentos_core::types::PipelineConfig {
            name: "test_multi_turn".to_string(),
            loop_bodies: vec![agentos_core::types::LoopBody {
                id: "llm".to_string(),
                steps: vec![agentos_core::types::PipelineStep {
                    id: "llm".to_string(),
                    steps: vec!["mock_llm_core".into()],
                    when: None,
                    context: std::collections::HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                }],
                loop_config: None,
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
        });
        state.step_library = Arc::new(agentos_core::types::StepLibrary::default());
        state.plugin_ids = Arc::new(std::collections::HashSet::from([
            "mock_llm_core".to_string()
        ]));
        (state, invoker, store, sqlite)
    }

    #[tokio::test]
    async fn test_multi_turn_second_round_sees_first_round_context() {
        let (state, invoker, _store, _sqlite) = make_engine_state();
        let tenant = TenantContext::new("tenant_mt", "thread_mt");
        let pipe = "pipe_mt";
        let thread = "thread_mt";

        // 第一轮：pipeline_id=pipe_mt 非空（WS 路径 route_id 语义）
        let r1 = agentos_tenant::scope(
            tenant.clone(),
            process_via_engine(
                &state,
                "第一轮：我叫小明",
                "agentos",
                &[],
                pipe,
                thread,
                "m1",
                "",
                "",
                None,
                None,
            ),
        )
        .await;
        assert!(!r1.content.is_empty(), "第一轮应返回 assistant 回复");

        // 第二轮：同 pipeline_id，应看到第一轮 user+assistant 上下文
        let r2 = agentos_tenant::scope(
            tenant,
            process_via_engine(
                &state,
                "第二轮：我叫什么？",
                "agentos",
                &[],
                pipe,
                thread,
                "m2",
                "",
                "",
                None,
                None,
            ),
        )
        .await;
        assert!(!r2.content.is_empty(), "第二轮应返回 assistant 回复");

        // 断言：第二轮 LLM 收到的 messages 是完整序列（历史 + 当前）
        let seen = invoker.seen.lock().unwrap();
        assert_eq!(seen.len(), 2, "应有两轮 LLM 调用");
        let first = seen[0].as_array().unwrap();
        assert_eq!(first.len(), 1, "第一轮应只有当前 user 消息");
        assert_eq!(first[0]["role"], "user");
        assert_eq!(first[0]["content"], "第一轮：我叫小明");

        let second = seen[1].as_array().unwrap();
        // 完整序列 = 第一轮 user + 第一轮 assistant + 第二轮 user
        assert_eq!(
            second.len(),
            3,
            "第二轮应含第一轮上下文（user+assistant）+ 当前 user"
        );
        assert_eq!(second[0]["role"], "user");
        assert_eq!(second[0]["content"], "第一轮：我叫小明");
        assert_eq!(second[1]["role"], "assistant");
        assert!(second[1]["content"].as_str().unwrap().contains("回复第1条"));
        assert_eq!(second[2]["role"], "user");
        assert_eq!(second[2]["content"], "第二轮：我叫什么？");
    }

    #[tokio::test]
    async fn test_multi_turn_http_empty_pipeline_id_falls_back_to_thread() {
        // HTTP 路径 pipeline_id=""，effective 应回退 thread_id，
        // 使 store 写入/查询键一致，第二轮仍能看到第一轮上下文。
        let (state, invoker, _store, _sqlite) = make_engine_state();
        let tenant = TenantContext::new("tenant_http", "thread_http");
        let thread = "thread_http";

        let r1 = agentos_tenant::scope(
            tenant.clone(),
            process_via_engine(
                &state,
                "HTTP 第一轮",
                "agentos",
                &[],
                "",
                thread,
                "h1",
                "",
                "",
                None,
                None,
            ),
        )
        .await;
        assert!(!r1.content.is_empty());

        let r2 = agentos_tenant::scope(
            tenant,
            process_via_engine(
                &state,
                "HTTP 第二轮",
                "agentos",
                &[],
                "",
                thread,
                "h2",
                "",
                "",
                None,
                None,
            ),
        )
        .await;
        assert!(!r2.content.is_empty());

        let seen = invoker.seen.lock().unwrap();
        assert_eq!(seen.len(), 2);
        let second = seen[1].as_array().unwrap();
        assert_eq!(
            second.len(),
            3,
            "HTTP 空 pipeline_id 也应通过 thread_id 回退看到历史"
        );
        assert_eq!(second[0]["content"], "HTTP 第一轮");
        assert_eq!(second[2]["content"], "HTTP 第二轮");
    }

    #[tokio::test]
    async fn test_multi_turn_cold_start_recovers_from_store() {
        // 冷路径验证：registry 未命中（新进程/重启）时，从 message_slots 表恢复历史
        // （零兼容重排：messages 持久真值 = slots 表，checkpoint/traces 只管标量）。
        // 模拟：直接向 slots 写入第一轮 user+assistant（pipeline_id=pipe_cold），
        // 再调用 process_via_engine，断言 LLM 收到历史 + 当前。
        let (state, invoker, store, sqlite) = make_engine_state();
        let tenant = TenantContext::new("tenant_cold", "thread_cold");
        let pipe = "pipe_cold";
        let thread = "thread_cold";

        // 直接写 slots（模拟上一轮已持久化，registry 无该管道——冷启动）
        let store_ref = store.clone();
        agentos_tenant::scope(tenant.clone(), async {
            store_ref.create_run("run_cold", "", "tenant_cold").await.unwrap();
            store_ref.link_pipeline_session(pipe, thread, "tenant_cold").await.unwrap();
            sqlite
                .apply_messages_ops_to_table(pipe, "tenant_cold", &[
                    serde_json::json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "冷启动第一轮"}}),
                    serde_json::json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "冷启动回复"}}),
                ])
                .unwrap();
        })
        .await;

        // 验证写库成功（恢复前置条件）
        let check_store = store.clone();
        let found = agentos_tenant::scope(tenant.clone(), async {
            check_store
                .get_messages_by_pipeline(pipe, MessageQueryOpts::default())
                .await
                .unwrap()
        })
        .await;
        assert_eq!(found.len(), 2, "冷启动写库应成功且 tenant 一致");

        let r = agentos_tenant::scope(
            tenant,
            process_via_engine(
                &state,
                "冷启动第二轮",
                "agentos",
                &[],
                pipe,
                thread,
                "c2",
                "",
                "",
                None,
                None,
            ),
        )
        .await;
        assert!(!r.content.is_empty(), "冷启动第二轮应返回 assistant 回复");

        let seen = invoker.seen.lock().unwrap();
        assert_eq!(seen.len(), 1, "冷启动应从 store 恢复历史并调用 LLM");
        let msgs = seen[0].as_array().unwrap();
        assert_eq!(
            msgs.len(),
            3,
            "冷启动应从 store 恢复第一轮 user+assistant + 当前 user"
        );
        assert_eq!(msgs[0]["content"], "冷启动第一轮");
        assert_eq!(msgs[1]["role"], "assistant");
        assert_eq!(msgs[2]["content"], "冷启动第二轮");
    }

    // ── 多用户持久化 + 数据隔离端到端测试（0.5.0 最小持久化地基）──
    //
    // 验证核心契约：两个不同用户（不同 tenant）各自发消息 → 各自能读到自己的历史
    // → 跨 tenant 读不到对方（隔离）。链路与生产一致：process_via_engine → 落库，
    // get_messages_by_pipeline 按 task_local tenant 过滤。

    /// 端到端：两用户各自发消息 + 读历史，验证数据隔离。
    ///
    /// 复用 make_engine_state 的 mock 引擎（RecordingInvoker），但注入两个真实
    /// 用户到 store（一用户一租户）。模拟 chat_handler / dispatch_user_input 的
    /// 核心链路：在各自 tenant scope 内调 process_via_engine（落库），再用
    /// get_messages_by_pipeline 在各自 scope 内读回。
    #[tokio::test]
    async fn test_multi_user_isolation_end_to_end() {
        let (state, _invoker, store, _sqlite) = make_engine_state();

        // 播种两个用户：alice → tenant_alice，bob → tenant_bob（一用户一租户）
        let now = chrono::Utc::now().to_rfc3339();
        let alice = agentos_core::types::UserRecord {
            user_id: "u-alice-001".to_string(),
            username: "alice".to_string(),
            password: "x".to_string(),
            email: None,
            role: "user".to_string(),
            tenant_id: "tenant_alice".to_string(),
            created_at: now.clone(),
            last_login_at: None,
        };
        let bob = agentos_core::types::UserRecord {
            user_id: "u-bob-002".to_string(),
            username: "bob".to_string(),
            password: "x".to_string(),
            email: None,
            role: "user".to_string(),
            tenant_id: "tenant_bob".to_string(),
            created_at: now,
            last_login_at: None,
        };
        store.create_user(&alice).await.unwrap();
        store.create_user(&bob).await.unwrap();

        let pipe_a = "pipe_alice";
        let pipe_b = "pipe_bob";
        let thread_a = "thread_alice";
        let thread_b = "thread_bob";

        // alice 发消息（在 alice 的 tenant scope 内，模拟 dispatch_user_input）
        let r_a = agentos_tenant::scope(
            TenantContext::new("tenant_alice", thread_a),
            process_via_engine(
                &state,
                "alice 的消息",
                "agentos",
                &[],
                pipe_a,
                thread_a,
                "a1",
                "",
                "",
                None,
                None,
            ),
        )
        .await;
        assert!(!r_a.content.is_empty(), "alice 发消息应返回 assistant 回复");

        // bob 发消息（在 bob 的 tenant scope 内）
        let r_b = agentos_tenant::scope(
            TenantContext::new("tenant_bob", thread_b),
            process_via_engine(
                &state,
                "bob 的消息",
                "agentos",
                &[],
                pipe_b,
                thread_b,
                "b1",
                "",
                "",
                None,
                None,
            ),
        )
        .await;
        assert!(!r_b.content.is_empty(), "bob 发消息应返回 assistant 回复");

        // alice 在自己 scope 内能读到自己的消息（user + assistant ≥ 2 条）
        let store_a = store.clone();
        let msgs_a =
            agentos_tenant::scope(TenantContext::new("tenant_alice", thread_a), async move {
                store_a
                    .get_messages_by_pipeline(pipe_a, MessageQueryOpts::default())
                    .await
            })
            .await
            .unwrap();
        assert!(
            msgs_a.len() >= 2,
            "alice 应能读到自己的 user+assistant 消息，实际 {}",
            msgs_a.len()
        );

        // bob 在自己 scope 内能读到自己的消息
        let store_b = store.clone();
        let msgs_b =
            agentos_tenant::scope(TenantContext::new("tenant_bob", thread_b), async move {
                store_b
                    .get_messages_by_pipeline(pipe_b, MessageQueryOpts::default())
                    .await
            })
            .await
            .unwrap();
        assert!(msgs_b.len() >= 2, "bob 应能读到自己的消息");

        // ★ 隔离断言：在 bob 的 scope 内读 alice 的 pipeline，必须为空
        let store_cross = store.clone();
        let cross = agentos_tenant::scope(TenantContext::new("tenant_bob", thread_b), async move {
            store_cross
                .get_messages_by_pipeline(pipe_a, MessageQueryOpts::default())
                .await
        })
        .await
        .unwrap();
        assert!(
            cross.is_empty(),
            "tenant_bob 必须读不到 tenant_alice 的消息（数据隔离）"
        );

        // 反向：alice scope 内读 bob 的 pipeline，也必须为空
        let store_cross2 = store.clone();
        let cross2 =
            agentos_tenant::scope(TenantContext::new("tenant_alice", thread_a), async move {
                store_cross2
                    .get_messages_by_pipeline(pipe_b, MessageQueryOpts::default())
                    .await
            })
            .await
            .unwrap();
        assert!(
            cross2.is_empty(),
            "tenant_alice 必须读不到 tenant_bob 的消息"
        );

        // 验证消息内容确实是各自的（alice 的 user 消息内容含 "alice"）
        let alice_user_msg = msgs_a
            .iter()
            .find(|m| m.role == "user")
            .expect("alice 应有 user 消息");
        assert!(
            alice_user_msg
                .content_preview
                .as_deref()
                .unwrap_or("")
                .contains("alice"),
            "alice 的消息内容应含 'alice'"
        );
    }

    /// 验证 register → login → 发消息 → 读历史 的完整用户流程（含持久化用户）。
    ///
    /// 用真实 store 跑 register/login handler（经 build_router），拿到 token 后
    /// 模拟 WS 路径发消息，验证新注册用户能正常保存和读取自己的历史。
    #[tokio::test]
    async fn test_registered_user_can_save_and_read_history() {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        // 播种 admin（login admin 兜底用）
        let now = chrono::Utc::now().to_rfc3339();
        let admin = agentos_core::types::UserRecord {
            user_id: "00000000-0000-0000-0000-000000000001".to_string(),
            username: "admin".to_string(),
            password: "admin12345".to_string(),
            email: None,
            role: "admin".to_string(),
            tenant_id: "default".to_string(),
            created_at: now.clone(),
            last_login_at: None,
        };
        store.create_user(&admin).await.unwrap();

        // 注册新用户 frank（一用户一租户）
        let frank_id = "u-frank-003".to_string();
        let frank = agentos_core::types::UserRecord {
            user_id: frank_id.clone(),
            username: "frank".to_string(),
            password: "frank123".to_string(),
            email: None,
            role: "user".to_string(),
            tenant_id: frank_id.clone(), // 一用户一租户
            created_at: now,
            last_login_at: None,
        };
        store.create_user(&frank).await.unwrap();

        // frank 的 tenant = frank_id（非 default），在自己的 scope 内发消息 + 读
        let mut state = AppState::new();
        state.store = Some(store.clone());
        state.invoker = Some(Arc::new(RecordingInvoker {
            seen: std::sync::Mutex::new(Vec::new()),
            seen_states: std::sync::Mutex::new(Vec::new()),
            hooks: std::sync::Mutex::new(Vec::new()),
            list_tools: std::collections::HashMap::new(),
        }));
        // 临时 config（make_engine_state 的精简版，足够 process_via_engine 跑通）
        let tmp_root =
            std::env::temp_dir().join(format!("frank_test_{}", uuid::Uuid::new_v4().simple()));
        let cfg_dir = tmp_root.join("config").join("pipelines");
        std::fs::create_dir_all(&cfg_dir).unwrap();
        std::fs::write(
            cfg_dir.join("autonomous.yaml"),
            "name: t\nloop_bodies:\n  - id: main\n    steps:\n      - id: llm\n        steps:\n          - mock_llm_core\n",
        ).unwrap();
        state.project_root = Some(tmp_root);
        state.pipeline_config = Arc::new(agentos_core::types::PipelineConfig {
            name: "t".to_string(),
            loop_bodies: vec![agentos_core::types::LoopBody {
                id: "llm".to_string(),
                steps: vec![agentos_core::types::PipelineStep {
                    id: "llm".to_string(),
                    steps: vec!["mock_llm_core".into()],
                    when: None,
                    context: std::collections::HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                }],
                loop_config: None,
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
        });
        state.step_library = Arc::new(agentos_core::types::StepLibrary::default());
        state.plugin_ids = Arc::new(std::collections::HashSet::from([
            "mock_llm_core".to_string()
        ]));

        let pipe = "pipe_frank";
        let thread = "thread_frank";
        // frank 发消息（tenant = frank_id）
        let r = agentos_tenant::scope(
            TenantContext::new(&frank_id, thread),
            process_via_engine(
                &state,
                "frank 的问题",
                "agentos",
                &[],
                pipe,
                thread,
                "f1",
                "",
                "",
                None,
                None,
            ),
        )
        .await;
        assert!(!r.content.is_empty(), "frank 发消息应成功");

        // frank 能读到自己的历史
        let store_read = store.clone();
        let msgs = agentos_tenant::scope(TenantContext::new(&frank_id, thread), async move {
            store_read
                .get_messages_by_pipeline(pipe, MessageQueryOpts::default())
                .await
        })
        .await
        .unwrap();
        assert!(msgs.len() >= 2, "frank 应能读到自己的历史");

        // admin（default 租户）读不到 frank 的消息
        let store_admin = store.clone();
        let admin_msgs =
            agentos_tenant::scope(TenantContext::new("default", "admin_thread"), async move {
                store_admin
                    .get_messages_by_pipeline(pipe, MessageQueryOpts::default())
                    .await
            })
            .await
            .unwrap();
        assert!(
            admin_msgs.is_empty(),
            "admin(default) 不应读到 frank 的消息"
        );
    }

    // ── CORS Origin 白名单（回归：反射任意 Origin + 凭据 = 跨域数据泄露）──

    #[test]
    fn local_origins_allowed_any_port() {
        assert!(super::is_local_origin("http://localhost:5173"));
        assert!(super::is_local_origin("https://localhost:9100"));
        assert!(super::is_local_origin("http://127.0.0.1:3000"));
        assert!(super::is_local_origin("https://127.0.0.1:443"));
        assert!(super::is_local_origin("http://[::1]:8080"));
    }

    #[test]
    fn nonlocal_origins_rejected_by_local_check() {
        assert!(!super::is_local_origin("https://evil.com"));
        // 边界：localhost.evil.com 不应冒充 localhost（防前缀绕过）
        assert!(!super::is_local_origin("http://localhost.evil.com"));
        assert!(!super::is_local_origin("http://127.0.0.1.evil.com"));
    }

    #[test]
    fn allowlist_exact_match_only() {
        let allow = ["https://app.example.com", "https://www.example.com"];
        assert!(super::origin_matches_allowlist(
            "https://app.example.com",
            &allow
        ));
        // 精确匹配——子域/变体不应通过
        assert!(!super::origin_matches_allowlist(
            "https://evil.example.com",
            &allow
        ));
        assert!(!super::origin_matches_allowlist(
            "https://app.example.com.evil.com",
            &allow
        ));
    }

    // ── spill_guard 配套：框架级强制工具注入 ──────────────────────

    /// 构造含 spill_retrieve + 普通工具的 registry，注入 AppState。
    fn app_state_with_tools(tool_names: &[&str]) -> AppState {
        use agentos_core::traits::ToolDescriptor;
        use agentos_core::types::{ToolCategory, ToolSource};
        use agentos_plugin_loader::CapabilityRegistryImpl;
        let registry = Arc::new(CapabilityRegistryImpl::new());
        for name in tool_names {
            registry.register_tool(
                "test_plugin",
                ToolDescriptor {
                    name: (*name).to_string(),
                    description: format!("test tool {name}"),
                    plugin_id: "test_plugin".to_string(),
                    input_schema: json!({"type": "object", "properties": {}}),
                    output_schema: None,
                    category: ToolCategory::System,
                    source: ToolSource::Builtin,
                    ui: None,
                    render: None,
                },
            );
        }
        let mut state = AppState::new();
        state.capability_registry = Some(registry);
        state
    }

    /// spill_retrieve 即使不在 agent tool_ids 里也必须注入——spill_guard 替换
    /// 文本引导 LLM 调它取回原文，若 schema 不可见就是死路。
    #[test]
    fn inject_tool_schemas_forces_spill_retrieve_regardless_of_tool_ids() {
        let app_state = app_state_with_tools(&["bash_execute", "spill_retrieve"]);
        let mut state = json!({
            "tool_ids": ["bash_execute"],  // 显式不含 spill_retrieve
        });
        inject_tool_schemas(&mut state, &app_state);
        let schemas = state["tool_schemas"].as_array().unwrap();
        let names: Vec<&str> = schemas
            .iter()
            .map(|s| s["function"]["name"].as_str().unwrap())
            .collect();
        assert!(
            names.contains(&"spill_retrieve"),
            "spill_retrieve 必须强制注入: {names:?}"
        );
        assert!(
            names.contains(&"bash_execute"),
            "tool_ids 命中的正常注入: {names:?}"
        );
    }

    /// 没有 tool_ids（兜底全量）时，spill_retrieve 自然在内。
    #[test]
    fn inject_tool_schemas_all_mode_includes_everything() {
        let app_state = app_state_with_tools(&["bash_execute", "spill_retrieve"]);
        let mut state = json!({}); // 无 tool_ids → 全量
        inject_tool_schemas(&mut state, &app_state);
        let names: Vec<&str> = state["tool_schemas"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| s["function"]["name"].as_str().unwrap())
            .collect();
        assert!(names.contains(&"spill_retrieve"));
        assert!(names.contains(&"bash_execute"));
    }

    /// registry 里没有 spill_retrieve（插件未安装）时不会凭空注入。
    #[test]
    fn inject_tool_schemas_no_spill_retrieve_when_not_installed() {
        let app_state = app_state_with_tools(&["bash_execute"]); // 无 spill_retrieve
        let mut state = json!({"tool_ids": ["bash_execute"]});
        inject_tool_schemas(&mut state, &app_state);
        let names: Vec<&str> = state["tool_schemas"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| s["function"]["name"].as_str().unwrap())
            .collect();
        assert!(!names.contains(&"spill_retrieve"), "未安装不应凭空出现");
    }

    /// task_dsh_plugin_adapter 任务 1：声明了 output_schema/render 的工具，
    /// 输出契约注入 state["tool_output_contracts"]（tool_core 校验 + 前端路由的
    /// 数据源）；未声明者不产生条目（存量工具零负担）。
    #[test]
    fn inject_tool_schemas_also_injects_output_contracts() {
        use agentos_core::traits::ToolDescriptor;
        use agentos_core::types::{ToolCategory, ToolSource};
        use agentos_plugin_loader::CapabilityRegistryImpl;
        let registry = Arc::new(CapabilityRegistryImpl::new());
        registry.register_tool(
            "test_plugin",
            ToolDescriptor {
                name: "dsh_read".to_string(),
                description: "read".to_string(),
                plugin_id: "test_plugin".to_string(),
                input_schema: json!({"type": "object", "properties": {}}),
                output_schema: Some(json!({"type": "object", "required": ["path"]})),
                category: ToolCategory::File,
                source: ToolSource::Builtin,
                ui: None,
                render: Some(json!({"card": "read"})),
            },
        );
        registry.register_tool(
            "test_plugin",
            ToolDescriptor {
                name: "legacy_tool".to_string(),
                description: "no contract".to_string(),
                plugin_id: "test_plugin".to_string(),
                input_schema: json!({"type": "object", "properties": {}}),
                output_schema: None,
                category: ToolCategory::System,
                source: ToolSource::Builtin,
                ui: None,
                render: None,
            },
        );
        let mut app_state = AppState::new();
        app_state.capability_registry = Some(registry);

        let mut state = json!({});
        inject_tool_schemas(&mut state, &app_state);

        let contracts = state["tool_output_contracts"].as_object().unwrap();
        assert_eq!(contracts.len(), 1, "只有声明契约的工具进入: {contracts:?}");
        assert_eq!(contracts["dsh_read"]["schema"]["required"][0], "path");
        assert_eq!(contracts["dsh_read"]["render"]["card"], "read");
        assert!(contracts.get("legacy_tool").is_none());
    }

    // ── GAP-1 阶段 1：自由 state overlay / lineage 并入 initial_state ──────
    // 契约：chat.send_message 的 state 注入在 execution_context 合并点
    // （1a/1a2）之后并入顶层扁平键；lineage.* 为引擎出生写入的保护字段。

    #[tokio::test]
    async fn test_stage_build_initial_state_merges_overlay_after_execution_context() {
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let store: Arc<dyn StorageBackend> = sqlite;
        let overlay = json!({
            "task.goal": "喝水提醒",
            "task.status": "pending",
            "task.id": "31bfdee19720",
            "lineage.parent_pipeline_id": "pipe_parent",
            "lineage.origin_session_id": "sess_root",
            "lineage.root": true,
        });
        let st = stage_build_initial_state(
            &store,
            "msg",
            "agentos",
            "pipe_new",
            "thread_new",
            "m1",
            "u1",
            "",
            Some(&json!({"workspace": {"mode": "worktree"}})),
            Some(&overlay),
        )
        .await;
        // execution_context 合并点（1a2）优先成立（overlay 不侵蚀其结构）
        assert_eq!(st["execution_context"]["workspace"]["mode"], "worktree");
        // overlay 顶层扁平键并入（与 track.total_tokens 同款约定）
        assert_eq!(st["task.goal"], "喝水提醒");
        assert_eq!(st["task.status"], "pending");
        assert_eq!(st["task.id"], "31bfdee19720");
        assert_eq!(st["lineage.parent_pipeline_id"], "pipe_parent");
        assert_eq!(st["lineage.origin_session_id"], "sess_root");
        assert_eq!(st["lineage.root"], true);
        // 引擎系统字段基线完好
        assert_eq!(st["message"], "msg");
        assert_eq!(st["pipeline_id"], "pipe_new");
        assert_eq!(st["session_id"], "thread_new");
        assert_eq!(st["user_id"], "u1");
    }

    #[test]
    fn test_apply_state_overlay_skips_engine_system_fields() {
        // 纵深防御：即使 overlay 携带保留字（handler 层已拦截，此处防内部旁路
        // 调用者），合并点也跳过引擎系统字段
        let mut st = json!({
            "message": "real",
            "pipeline_id": "pipe_real",
            "user_id": "u_real",
            "messages": [{"role": "user", "content": "real"}],
        });
        apply_state_overlay(
            &mut st,
            &json!({
                "message": "evil",
                "pipeline_id": "evil",
                "user_id": "evil",
                "messages": [],
                "execution_context": {"evil": true},
                "task.goal": "ok"
            }),
        );
        assert_eq!(st["message"], "real");
        assert_eq!(st["pipeline_id"], "pipe_real");
        assert_eq!(st["user_id"], "u_real");
        assert_eq!(st["messages"].as_array().unwrap().len(), 1);
        assert!(st.get("execution_context").is_none());
        assert_eq!(st["task.goal"], "ok", "非保留字自由键应并入");
    }

    #[test]
    fn test_apply_state_overlay_lineage_keys_not_overwritten_once_present() {
        // lineage 出生写入后为引擎保护字段：后续 overlay 同名键跳过（引擎值保留）
        let mut st = json!({});
        apply_state_overlay(
            &mut st,
            &json!({
                "lineage.root": true,
                "lineage.origin.kind": "channel",
                "task.status": "pending"
            }),
        );
        apply_state_overlay(
            &mut st,
            &json!({"lineage.root": false, "task.status": "running"}),
        );
        assert_eq!(st["lineage.root"], true, "lineage 已存在 → 引擎值保留");
        assert_eq!(st["lineage.origin.kind"], "channel");
        assert_eq!(st["task.status"], "running", "非保护键后续可更新");
    }

    #[tokio::test]
    async fn test_process_via_engine_state_overlay_reaches_plugin_context() {
        // 真实引擎路径（非 mock 合并点）：overlay 键进入插件可见 state——
        // task.* 消费契约（task_evaluate / child_task_guard 等读 state 直读）
        let (state, invoker, _store, _sqlite) = make_engine_state();
        let tenant = TenantContext::new("tenant_overlay", "thread_overlay");
        let overlay = json!({
            "task.goal": "写周报",
            "task.status": "pending",
            "lineage.parent_pipeline_id": "pipe_parent",
            "lineage.origin_session_id": "thread_human"
        });
        let r = agentos_tenant::scope(
            tenant,
            process_via_engine(
                &state,
                "开始执行任务",
                "agentos",
                &[],
                "pipe_overlay",
                "thread_overlay",
                "o1",
                "",
                "",
                None,
                Some(&overlay),
            ),
        )
        .await;
        assert!(!r.content.is_empty());
        let states = invoker.seen_states.lock().unwrap();
        assert!(!states.is_empty(), "引擎应至少调用一次 LLM 插件");
        assert_eq!(states[0]["task.goal"], "写周报");
        assert_eq!(states[0]["task.status"], "pending");
        assert_eq!(states[0]["lineage.parent_pipeline_id"], "pipe_parent");
        assert_eq!(states[0]["lineage.origin_session_id"], "thread_human");
    }

    // ── GAP-2：run 终态域事件（EVENT 触发器的输入源） ─────────────────────
    // 契约：run 结束（completed/suspended/failed）时内核广播 run.* 域事件；
    // state 带 task.* 字段时派生任务域事件（task_completed/task_failed）。

    #[test]
    fn test_derive_run_terminal_events_completed_with_task_fields() {
        let st = json!({
            "pipeline_id": "p1",
            "thread_id": "th1",
            "task.id": "t9",
            "task.goal": "喝水提醒",
            "lineage.parent_pipeline_id": "parent_p1",
        });
        let evs = derive_run_terminal_events(&st, false);
        let names: Vec<&str> = evs.iter().map(|(n, _)| *n).collect();
        assert_eq!(names, vec!["run.completed", "task_completed"]);
        // 标签携带溯源字段
        let (_, tags) = &evs[1];
        let tag = |k: &str| {
            tags.iter()
                .find(|(tk, _)| *tk == k)
                .map(|(_, v)| v.clone())
                .unwrap_or(serde_json::Value::Null)
        };
        assert_eq!(tag("pipeline_id"), json!("p1"));
        assert_eq!(tag("task_id"), json!("t9"));
        // 子任务通知锚点：parent_pipeline_id 从 state 的 lineage 扁平键带出
        assert_eq!(tag("parent_pipeline_id"), json!("parent_p1"));
    }

    #[test]
    fn test_derive_run_terminal_events_plain_and_suspended() {
        // 无 task.* 字段的普通会话管道：只发 run.*，不派生任务事件
        let plain = json!({"pipeline_id": "p2", "thread_id": "th2"});
        let names: Vec<&str> = derive_run_terminal_events(&plain, false)
            .iter()
            .map(|(n, _)| *n)
            .collect();
        assert_eq!(names, vec!["run.completed"]);

        // 挂起（RouteNext::Wait 落的 suspended 标志）：run.suspended
        let suspended = json!({"pipeline_id": "p3", "suspended": true, "task.id": "t3"});
        let names2: Vec<&str> = derive_run_terminal_events(&suspended, false)
            .iter()
            .map(|(n, _)| *n)
            .collect();
        assert_eq!(names2, vec!["run.suspended"]);
    }

    #[test]
    fn test_derive_run_terminal_events_failed_with_task_fields() {
        let st = json!({
            "pipeline_id": "p4",
            "task.id": "t4",
            "task.goal": "g",
            "lineage.parent_pipeline_id": "parent_p4",
        });
        let evs = derive_run_terminal_events(&st, true)
            .iter()
            .map(|(n, tags)| {
                let pp = tags
                    .iter()
                    .find(|(k, _)| *k == "parent_pipeline_id")
                    .map(|(_, v)| v.as_str().unwrap_or(""))
                    .unwrap_or("");
                (n.to_string(), pp.to_string())
            })
            .collect::<Vec<_>>();
        assert_eq!(evs[0], ("run.failed".to_string(), "".to_string()));
        assert_eq!(
            evs[1],
            ("task_failed".to_string(), "parent_p4".to_string())
        );
    }

    #[test]
    fn test_derive_run_terminal_events_task_prefix_is_dotted() {
        // 前缀必须精确为 "task."：taskx/execution_context.task_id 不得误派生
        let st = json!({"pipeline_id": "p5", "taskx": 1, "task_meta": "y"});
        let names: Vec<&str> = derive_run_terminal_events(&st, false)
            .iter()
            .map(|(n, _)| *n)
            .collect();
        assert_eq!(names, vec!["run.completed"]);
    }

    #[tokio::test]
    async fn test_process_via_engine_emits_run_terminal_domain_events() {
        // wiring：真实引擎跑一轮 → 声明 domain_event hook 的启用插件收到
        // run.completed + task_completed（state overlay 带 task.* 字段时）
        let (state, invoker, _store, _sqlite) = make_engine_state();
        // 订阅方插件：manifest 声明 DomainEvent hook 且启用
        {
            let mut manifests = state.manifests.write().await;
            manifests.push(agentos_core::traits::PluginManifest {
                id: "trigger_sub".to_string(),
                name: "trigger_sub".to_string(),
                version: "1.0.0".to_string(),
                plugin_type: agentos_core::traits::PluginType::System,
                pipeline_role: None,
                language: "python".to_string(),
                host_type: agentos_core::traits::HostType::Sidecar,
                entry: String::new(),
                capabilities: agentos_core::traits::ManifestCapabilities {
                    lifecycle_hooks: vec![agentos_core::traits::LifecycleHook::DomainEvent],
                    ..Default::default()
                },
                dependencies: vec![],
                permissions: Default::default(),
                error_policy: Default::default(),
                priority: 100,
                mcp: None,
                lifecycle: None,
                native: None,
                granted_capabilities: vec![],
                requires_content: None,
                invoke_entry: None,
                config_files: vec![],
                ui_schema: None,
                persistent_fields: vec![],
                http_endpoints: vec![],
                contributes: Default::default(),
                enabled: Some(true),
                activation: Default::default(),
                provides: Default::default(),
            });
        }
        state
            .enabled_plugin_ids
            .write()
            .await
            .insert("trigger_sub".to_string());

        let tenant = TenantContext::new("tenant_gap2_emit", "thread_gap2_emit");
        // 注：lineage.* 是保留字（引擎出生写入），overlay 不可携带；真实路径经
        // chat.send_message 的 lineage 参数写入 state，纯函数测试已覆盖 parent 标签透传。
        let overlay = json!({"task.id": "t77", "task.goal": "写周报"});
        let r = agentos_tenant::scope(
            tenant,
            process_via_engine(
                &state,
                "执行任务",
                "agentos",
                &[],
                "pipe_gap2_emit",
                "thread_gap2_emit",
                "o1",
                "",
                "",
                None,
                Some(&overlay),
            ),
        )
        .await;
        assert!(!r.content.is_empty());

        // 广播 spawn 是 fire-and-forget：轮询等待钩子抵达（上限 5s）
        let mut hooks = Vec::new();
        for _ in 0..50 {
            hooks = invoker.hooks.lock().unwrap().clone();
            if hooks.len() >= 2 {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        }
        let events: Vec<String> = hooks
            .iter()
            .map(|(pid, _, ctx)| {
                assert_eq!(pid, "trigger_sub");
                ctx["event"].as_str().unwrap_or("").to_string()
            })
            .collect();
        assert!(
            events.contains(&"run.completed".to_string()),
            "应广播 run.completed，实际 {events:?}"
        );
        assert!(
            events.contains(&"task_completed".to_string()),
            "state 带 task.* 应派生 task_completed，实际 {events:?}"
        );
        // task 事件携带任务标签
        let task_evt = hooks
            .iter()
            .find(|(_, _, ctx)| ctx["event"] == json!("task_completed"))
            .expect("task_completed 钩子");
        assert_eq!(task_evt.2["task_id"], json!("t77"));
        assert_eq!(task_evt.2["pipeline_id"], json!("pipe_gap2_emit"));
        // parent_pipeline_id 标签存在（无 lineage 时为空串——不缺失键）
        assert!(
            task_evt.2.get("parent_pipeline_id").is_some(),
            "task_completed 应携带 parent_pipeline_id 键（无父时为空串）"
        );
    }

    // ── GAP-1 续：run 终态回写 task.status（task = pipeline state 单一真值） ──

    #[test]
    fn test_derive_task_terminal_status_completed() {
        // 任务管道正常跑完（无 failed 标志）→ completed
        let st = json!({"pipeline_id": "p1", "task.id": "t9"});
        assert_eq!(derive_task_terminal_status(&st, false), Some("completed"));
    }

    #[test]
    fn test_derive_task_terminal_status_failed() {
        // 任务管道失败 → failed（优先于 completed）
        let st = json!({"pipeline_id": "p1", "task.id": "t9"});
        assert_eq!(derive_task_terminal_status(&st, true), Some("failed"));
    }

    #[test]
    fn test_derive_task_terminal_status_skips_non_task_pipeline() {
        // 非任务管道（无 task.* 前缀）→ None（不写回）
        let plain = json!({"pipeline_id": "p2"});
        assert_eq!(derive_task_terminal_status(&plain, false), None);
    }

    #[test]
    fn test_derive_task_terminal_status_skips_suspended() {
        // suspended（等待人工交互）→ None（任务状态保持 running，不写终态）
        let st = json!({"pipeline_id": "p3", "suspended": true, "task.id": "t3"});
        assert_eq!(derive_task_terminal_status(&st, false), None);
    }

    #[test]
    fn test_derive_task_terminal_status_task_prefix_is_dotted() {
        // 前缀必须精确 "task."：taskx 不得误判为任务管道
        let st = json!({"pipeline_id": "p5", "taskx": 1});
        assert_eq!(derive_task_terminal_status(&st, false), None);
    }

    // ── 集成：真实引擎跑完整链路 → 终态回写双落点（registry + pipeline_state 表） ──
    // 接口级行为断言（非白盒）：process_via_engine 是前端消息的完整入口，
    // 任务管道（state 带 task.*）跑完后 task.status 必须推进为 completed——
    // 这是监控页"任务已完成"与 task_reminder 判断的数据基础。

    #[tokio::test]
    async fn test_engine_run_writes_back_task_status_completed() {
        let (state, _invoker, store, _sqlite) = make_engine_state();
        let tenant = TenantContext::new("tenant_wb", "thread_wb");
        let tenant_id = tenant.tenant_id.clone();
        let pipe = "pipe_wb_task";
        let thread = "thread_wb";
        let overlay = json!({
            "task.id": "t_wb1",
            "task.goal": "写周报",
            "task.status": "pending",
            "task.agent_level": "L2",
        });
        let r = agentos_tenant::scope(
            tenant,
            process_via_engine(
                &state,
                "执行任务",
                "agentos",
                &[],
                pipe,
                thread,
                "o1",
                "",
                "",
                None,
                Some(&overlay),
            ),
        )
        .await;
        assert!(!r.content.is_empty(), "引擎应正常跑完一轮");

        // ① 热路径：内存 registry 的 state 里 task.status 已推进为 completed
        let reg = agentos_session::global_registry();
        let entry = reg.get(&tenant_id, pipe).expect("registry 应有该管道条目");
        let st = entry.read();
        assert_eq!(
            st.state.get("task.status").and_then(|v| v.as_str()),
            Some("completed"),
            "registry 热路径：task.status 应回写 completed，实际 {:?}",
            st.state.get("task.status")
        );
        drop(st);

        // ② 冷路径：pipeline_state 表同样落 completed（重启后 checkpoint 之上的覆盖层）
        let fields = store
            .load_pipeline_state(pipe, &tenant_id)
            .await
            .unwrap();
        assert_eq!(
            fields.get("task.status").and_then(|v| v.as_str()),
            Some("completed"),
            "pipeline_state 表：task.status 应落 completed，实际 {:?}",
            fields.get("task.status")
        );
    }

    #[tokio::test]
    async fn test_engine_run_does_not_write_task_status_for_session_pipeline() {
        // 非任务管道（无 task.* 字段）：run 终态不得误写 task.status
        let (state, _invoker, _store, _sqlite) = make_engine_state();
        let tenant = TenantContext::new("tenant_wb2", "thread_wb2");
        let tenant_id = tenant.tenant_id.clone();
        let pipe = "pipe_wb_session";
        let thread = "thread_wb2";
        let r = agentos_tenant::scope(
            tenant,
            process_via_engine(
                &state,
                "普通对话",
                "agentos",
                &[],
                pipe,
                thread,
                "o1",
                "",
                "",
                None,
                None,
            ),
        )
        .await;
        assert!(!r.content.is_empty());

        let reg = agentos_session::global_registry();
        if let Some(entry) = reg.get(&tenant_id, pipe) {
            let st = entry.read();
            assert!(
                st.state.get("task.status").is_none(),
                "会话管道不得出现 task.status，实际 {:?}",
                st.state.get("task.status")
            );
        }
    }

    // ── GAP-3 后半：resume 幂等（重启后 user 消息不重复消费） ────────────

    /// 中断签名：user 消息已落槽（上一次尝试被重启截断）且无 assistant 跟随
    /// → 冷启动重放同一消息时**不得再次落槽**（修复前无条件 append 导致
    /// 重复 run / 同消息双份 / 陈旧回复——e2e GAP-3 现象②）。
    #[tokio::test]
    async fn test_replay_after_interrupt_does_not_duplicate_user_message() {
        let (state, invoker, store, _sqlite) = make_engine_state();
        let tenant = TenantContext::new("tenant_gap3", "thread_gap3");

        // 模拟中断：user 消息已持久化（slot 落库）但 run 未产出 assistant
        let _ = store
            .apply_messages_ops_to_table(
                "pipe_gap3",
                "tenant_gap3",
                &[json!({"op":"set","seq":1,"msg":{"role":"user","content":"重启前的那条消息"}})],
            )
            .await;

        // 冷启动重放同一消息（registry 无条目 → 冷路径）
        let r = agentos_tenant::scope(
            tenant,
            process_via_engine(
                &state,
                "重启前的那条消息",
                "agentos",
                &[],
                "pipe_gap3",
                "thread_gap3",
                "o1",
                "",
                "",
                None,
                None,
            ),
        )
        .await;
        assert!(!r.content.is_empty());

        // 幂等断言：该内容的 user 消息在 message_slots 里恰好 1 条
        let msgs = store
            .load_message_history("pipe_gap3", "tenant_gap3")
            .await
            .unwrap();
        let dup = msgs
            .iter()
            .filter(|m| {
                m.get("role") == Some(&json!("user"))
                    && m.get("content") == Some(&json!("重启前的那条消息"))
            })
            .count();
        assert_eq!(dup, 1, "中断重放不得重复落槽：{msgs:?}");
        // 引擎基于既有历史正常跑完（assistant 已产出）
        assert!(
            msgs.iter().any(|m| m.get("role") == Some(&json!("assistant"))),
            "重放应继续执行产出回复：{msgs:?}"
        );
        let _ = invoker; // 引擎确实调用了 LLM 插件（seen_states 非空即跑过）
        assert!(!invoker.seen_states.lock().unwrap().is_empty());
    }

    /// 正常连续两轮同文消息不受幂等影响：第一轮已消费（assistant 跟随），
    /// 第二轮同文 user 是新输入 → 应正常 append（2 条 user）。
    #[tokio::test]
    async fn test_repeated_user_message_after_reply_still_appends() {
        let (state, _invoker, store, _sqlite) = make_engine_state();
        let tenant = TenantContext::new("tenant_gap3b", "thread_gap3b");
        for _ in 0..2 {
            let _ = agentos_tenant::scope(
                tenant.clone(),
                process_via_engine(
                    &state,
                    "再来一次",
                    "agentos",
                    &[],
                    "pipe_gap3b",
                    "thread_gap3b",
                    "o1",
                    "",
                    "",
                    None,
                    None,
                ),
            )
            .await;
        }
        let msgs = store
            .load_message_history("pipe_gap3b", "tenant_gap3b")
            .await
            .unwrap();
        let n = msgs
            .iter()
            .filter(|m| {
                m.get("role") == Some(&json!("user")) && m.get("content") == Some(&json!("再来一次"))
            })
            .count();
        assert_eq!(n, 2, "已消费后同文再发是合法新输入（应 2 条）：{msgs:?}");
    }

    /// 重启压力近似（GAP-3 验证标准的单进程版）：3 个会话（不同管道）并发
    /// 各跑一条消息 + 其中一个管道带中断重放 → 终态各管道 user 计数精确、
    /// 序列严格递增、无 NULL blob。同管道并发在生产入口必经 RunChain FIFO
    /// 串行（ws_session/HTTP handler），此处按会话维度并发与生产同构。
    #[tokio::test]
    async fn test_concurrent_chats_with_interrupted_replay_consistent() {
        let (state, _invoker, store, _sqlite) = make_engine_state();
        // 管道 A 预置中断消息（user 已落槽、run 未产出 assistant——重启截断签名）
        let _ = store
            .apply_messages_ops_to_table(
                "pipe_gap3c_a",
                "tenant_gap3c",
                &[json!({"op":"set","seq":1,"msg":{"role":"user","content":"被中断的并发消息"}})],
            )
            .await;

        let mk = |pipeline: &'static str, msg: &'static str| {
            let st = state.clone();
            async move {
                agentos_tenant::scope(
                    TenantContext::new("tenant_gap3c", "thread_gap3c"),
                    process_via_engine(
                        &st,
                        msg,
                        "agentos",
                        &[],
                        pipeline,
                        "thread_gap3c",
                        "o1",
                        "",
                        "",
                        None,
                        None,
                    ),
                )
                .await
            }
        };
        let (ra, rb, rc, rr) = tokio::join!(
            mk("pipe_gap3c_a", "被中断的并发消息"), // 中断重放（同文尾部）
            mk("pipe_gap3c_b", "会话B的消息"),
            mk("pipe_gap3c_c", "会话C的消息"),
            mk("pipe_gap3c_d", "会话D的消息"),
        );
        for r in [&ra, &rb, &rc, &rr] {
            assert!(!r.content.is_empty());
        }

        for (pid, expect_user_contents) in [
            ("pipe_gap3c_a", vec!["被中断的并发消息"]),
            ("pipe_gap3c_b", vec!["会话B的消息"]),
            ("pipe_gap3c_c", vec!["会话C的消息"]),
            ("pipe_gap3c_d", vec!["会话D的消息"]),
        ] {
            let msgs = store.load_message_history(pid, "tenant_gap3c").await.unwrap();
            let seqs: Vec<i64> = msgs
                .iter()
                .filter_map(|m| m.get("seq").and_then(|s| s.as_i64()))
                .collect();
            let uniq: std::collections::BTreeSet<i64> = seqs.iter().copied().collect();
            assert_eq!(seqs.len(), uniq.len(), "{pid} 序列应严格唯一：{msgs:?}");
            for content in &expect_user_contents {
                let n = msgs
                    .iter()
                    .filter(|m| {
                        m.get("role") == Some(&json!("user"))
                            && m.get("content") == Some(&json!(content))
                    })
                    .count();
                assert_eq!(n, 1, "{pid}「{content}」应恰好 1 条：{msgs:?}");
            }
            for m in &msgs {
                assert!(
                    m.get("role").is_some() && m.get("content").is_some(),
                    "{pid} 消息应可从 blob 完整重建：{m:?}"
                );
            }
        }
    }

    // ── GAP-1 统一：run 终态即任务终态（state 单一真值的完成回写） ─────────

    #[tokio::test]
    async fn test_run_terminal_writes_task_status_to_state() {
        // overlay 带 task.* 字段的管道跑完 → registry 常驻 state 与
        // pipeline_state 表（冷路径真值）都回写 task.status=completed +
        // task.ended_at——"run 终态即任务终态"，任务树数据源
        // （/api/v1/pipelines/state 聚合 + checkpoint 冷兜底）无需 YAML。
        let (state, _invoker, store, _sqlite) = make_engine_state();
        let tenant = TenantContext::new("tenant_unify", "thread_unify");
        let overlay = json!({"task.id": "t_unify", "task.goal": "统一验证", "task.status": "pending"});
        let r = agentos_tenant::scope(
            tenant,
            process_via_engine(
                &state,
                "执行任务",
                "agentos",
                &[],
                "pipe_unify",
                "thread_unify",
                "o1",
                "",
                "",
                None,
                Some(&overlay),
            ),
        )
        .await;
        assert!(!r.content.is_empty());

        // registry 热数据
        let reg = agentos_session::global_registry();
        let entry = reg
            .get("tenant_unify", "pipe_unify")
            .expect("registry 应有该管道");
        let st = entry.read();
        assert_eq!(st.state["task.status"], json!("completed"));
        assert!(st.state.get("task.ended_at").is_some(), "应写 ended_at");
        drop(st);

        // 冷路径表（checkpoint 冷恢复时以表为准覆盖旧 checkpoint 标量）
        let fields = store
            .load_pipeline_state("pipe_unify", "tenant_unify")
            .await
            .unwrap();
        assert_eq!(fields.get("task.status"), Some(&json!("completed")));
        assert!(fields.contains_key("task.ended_at"));

        // 普通会话管道（无 task.*）不受影响——不写任务字段
        let _ = agentos_tenant::scope(
            TenantContext::new("tenant_unify", "thread_unify"),
            process_via_engine(
                &state,
                "普通消息",
                "agentos",
                &[],
                "pipe_plain",
                "thread_unify",
                "o1",
                "",
                "",
                None,
                None,
            ),
        )
        .await;
        let fields2 = store
            .load_pipeline_state("pipe_plain", "tenant_unify")
            .await
            .unwrap();
        assert!(!fields2.contains_key("task.status"), "非任务管道不写任务字段");
    }

    // ── GAP-1 全流程数据流转：提交 → 管道创建 → run → 终态回写 → 聚合可见 ──

    #[tokio::test]
    async fn test_task_lifecycle_end_to_end_state_flow() {
        // 组合验证（各环节单测已绿，此处串全链）：
        // ① chat.send_message create 分支生成 pipeline_id（task.id 引擎注入）
        // ② 同一 overlay 派发 → run 完成
        // ③ 内核回写 task.status/ended_at（registry + pipeline_state 表）
        // ④ pipeline-state.list 聚合行完整（task.* + lineage.* + status）
        let (state, _invoker, store, _sqlite) = make_engine_state();

        // ① 创建契约（chat handler 侧独立测试覆盖；此处手工构造同参，
        // 聚焦引擎侧流转）：
        let overlay = json!({
            "task.goal": "全流程验证",
            "task.status": "pending",
            "task.scope": "non_container",
            "lineage.root": true,
            "lineage.origin.kind": "plugin",
            "lineage.origin.source": "task_submit",
        });
        let pipeline_id = "pipe_lifecycle_1";
        // 引擎注入 task.id（与 chat_send_handler create 分支同语义）
        let mut overlay = overlay;
        if let Some(obj) = overlay.as_object_mut() {
            obj.insert("task.id".to_string(), json!(pipeline_id));
        }

        // ② 派发 → run 完成
        let tenant = TenantContext::new("tenant_lifecycle", "thread_lifecycle");
        let r = agentos_tenant::scope(
            tenant,
            process_via_engine(
                &state,
                "执行全流程验证任务",
                "agentos",
                &[],
                pipeline_id,
                "thread_lifecycle",
                "o1",
                "",
                "",
                None,
                Some(&overlay),
            ),
        )
        .await;
        assert!(!r.content.is_empty());

        // ③ registry 热路径终态
        let reg = agentos_session::global_registry();
        let entry = reg.get("tenant_lifecycle", pipeline_id).expect("registry 应有管道");
        let st = entry.read();
        assert_eq!(st.state["task.status"], json!("completed"), "任务终态由 run 终态回写");
        assert!(st.state.get("task.ended_at").is_some());
        // 出生字段保留（goal/scope/lineage）
        assert_eq!(st.state["task.goal"], "全流程验证");
        assert_eq!(st.state["task.scope"], "non_container");
        assert_eq!(st.state["lineage.root"], true);
        drop(st);

        // ④ 聚合出口（pipeline-state.list 同源）行完整
        let fields = store.load_pipeline_state(pipeline_id, "tenant_lifecycle").await.unwrap();
        assert_eq!(fields.get("task.status"), Some(&json!("completed")), "冷路径表也回写");

    }
}
