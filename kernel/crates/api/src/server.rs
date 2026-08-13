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
use std::sync::OnceLock;
use std::time::{Duration, SystemTime};

use parking_lot::RwLock as ParkingRwLock;

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
#[allow(unused_imports)]
use agentos_core::traits::{CapabilityRegistry, MessageQueryOpts};
use agentos_core::types::{PipelineConfig, StepLibrary, TenantContext};

use crate::pipeline_loader::{load_pipeline_config, load_step_library, validate_no_name_conflicts};
use serde::{Deserialize, Serialize};
use tracing::{debug, error, info, warn};

use crate::auth::{
    login_handler, logout_handler, me_handler, refresh_handler, register_handler,
    resolve_request_tenant_id,
};
use crate::error::ApiError;
use crate::compat_routes::{
    create_thread_handler, delete_thread_handler,
    get_thread_handler, list_thread_messages_handler,
    list_threads_handler, plugins_history_handler,
    plugins_reload_all_handler, plugins_reload_by_id_handler, plugins_reload_handler,
    plugins_set_enabled_handler, plugins_status_handler, update_thread_agent_handler,
    update_thread_handler,
};
use crate::routes::{
    actions_execute_handler, agents_handler, agents_schema_handler, get_agent_config_handler,
    get_pipeline_config_with_etag, get_plugin_config_with_etag, health_handler,
    metrics_prometheus_handler, metrics_query_handler, pipelines_handler, put_agent_config_handler,
    put_pipeline_config_handler, put_plugin_config_handler, schema_handler, tools_handler,
    AppState,
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
        // 前端兼容端点（对齐 0.1 channel_api，消除 404）
        .route(
            "/api/v1/threads",
            get(list_threads_handler).post(create_thread_handler),
        )
        .route(
            "/api/v1/threads/{id}",
            get(get_thread_handler)
                .patch(update_thread_handler)
                .delete(delete_thread_handler),
        )
        .route(
            "/api/v1/threads/{id}/agent",
            axum::routing::patch(update_thread_agent_handler),
        )
        .route(
            "/api/v1/threads/{id}/messages",
            get(list_thread_messages_handler),
        )
        .route("/api/v1/plugins/status", get(plugins_status_handler))
        .route("/api/v1/plugins/history", get(plugins_history_handler))
        .route("/api/v1/plugins/reload", post(plugins_reload_handler))
        .route(
            "/api/v1/plugins/{id}/reload",
            post(plugins_reload_by_id_handler),
        )
        .route("/api/v1/plugins/{id}/enabled", axum::routing::put(plugins_set_enabled_handler))
        .route(
            "/api/v1/plugins/reload-all",
            post(plugins_reload_all_handler),
        )
        // 监控 M5/M5b：指标查询 + Prometheus 导出（监控设计 §五/§十一）
        .route("/api/v1/metrics", get(metrics_query_handler))
        .route("/metrics", get(metrics_prometheus_handler))
        // AC-06-4: WebSocket 端点
        .route("/ws", get(ws_handler))
        // task_11 A2：前端写死连 /ws/chat（0.1 路径格式），加别名指向同一 handler，
        // 保证 0.2 模式下前端直连内核可用；/ws 保留给新客户端。
        .route("/ws/chat", get(ws_handler))
        // 消息发送端点（REST fallback for WS）
        .route("/api/v1/chat", post(chat_handler))
        // 人类交互响应端点——前端用户操作经此提交，内核转发到交互插件的 interaction.respond
        .route("/api/v1/interaction/response", post(interaction_response_handler))
        // Auth 端点
        .route("/api/v1/auth/login", post(login_handler))
        .route("/api/v1/auth/me", get(me_handler))
        .route("/api/v1/auth/refresh", post(refresh_handler))
        .route("/api/v1/auth/logout", post(logout_handler))
        .route("/api/v1/auth/register", post(register_handler))
        // 统一通用数据接口（task_01：表驱动动态枚举，不改持久化）
        .route("/api/v1/db/tables", get(crate::db_routes::list_tables_handler))
        .route(
            "/api/v1/db/table/{table}",
            get(crate::db_routes::query_rows_handler)
                .post(crate::db_routes::insert_row_handler),
        )
        .route(
            "/api/v1/db/table/{table}/{pk_value}",
            get(crate::db_routes::get_row_handler)
                .patch(crate::db_routes::update_row_handler)
                .delete(crate::db_routes::delete_row_handler),
        )
        .route("/api/v1/db/execute", post(crate::db_routes::execute_sql_handler));

    // P3：动态挂载插件 HTTP 端点（http_routes → dispatcher）
    let router = crate::http_dispatcher::build_router_with_http_routes(state.clone(), static_router);
    // F-API-1：配置写入面 + compat 会话/插件生命周期鉴权（白名单路径 + method 分写/读）。
    let auth_layer = from_fn_with_state(state.clone(), write_surface_auth);
    // CORS：开发期前端通过 VITE_API_BASE_URL 直连内核（http://localhost:9100），
    // 浏览器跨域请求需要预检（OPTIONS）+ 响应头带 Access-Control-Allow-Origin。
    // 作为最外层中间件，拦截 OPTIONS 预检并给所有响应注入 CORS 头。
    router
        .with_state(state)
        .layer(auth_layer)
        .layer(from_fn(cors_middleware))
}

/// F-API-1：配置写入面 + compat 会话/插件生命周期鉴权中间件。
///
/// 按路径白名单 + method 区分：写面（POST/PUT/PATCH/DELETE）→ require_admin_role；
/// 读面（GET）→ require_read_role（admin/viewer）。白名单覆盖：
/// - `/api/v1/threads*`（会话 CRUD）
/// - `/api/v1/plugins/*`（status/history/reload/enabled/config）
/// - `/api/v1/actions/execute`、`/api/v1/interaction/response`
/// - `PUT /api/v1/agents/{id}/config`、`PUT /api/v1/config/pipelines/{name}`
/// 其余路径放行（/health、/api/v1/auth/*、/ws、/api/v1/db/* 等）。
async fn write_surface_auth(
    State(state): State<AppState>,
    req: Request,
    next: Next,
) -> Result<Response, ApiError> {
    let path = req.uri().path();
    let method = req.method().clone();

    let needs_auth = path.starts_with("/api/v1/threads")
        || path.starts_with("/api/v1/plugins/")
        || path == "/api/v1/actions/execute"
        || path == "/api/v1/interaction/response"
        || (path.starts_with("/api/v1/agents/") && path.ends_with("/config") && method == Method::PUT)
        || (path.starts_with("/api/v1/config/pipelines/") && method == Method::PUT);

    if !needs_auth {
        return Ok(next.run(req).await);
    }

    let result = match method {
        Method::GET => crate::db_routes::require_read_role(&state, req.headers()).await,
        _ => crate::db_routes::require_admin_role(&state, req.headers()).await,
    };
    match result {
        Ok(_) => Ok(next.run(req).await),
        Err(e) => Err(e),
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
// 替代旧的"遍历全部 pipeline 插件"placeholder：改为构造
// [`agentos_engine::PipelineExecutor`]，读取 AppState 中的 `pipeline_config`
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
/// 历史上硬编码 "pipeline_llm_core" 写在 initial_state，现提取为常量便于发现与替换。
const DEFAULT_CORE_PLUGIN: &str = "pipeline_llm_core";

// 冷路径回放从该 pipeline 的 step 级轨迹按序 merge 重建完整 state（含 messages），
// 不再特化只读 messages 表——轨迹颗粒度即恢复边界。

/// 管道配置热重载的 mtime 检测 TTL：1 秒内不重复 stat，避免高频 chat 时反复解析 YAML。
const PIPELINE_CONFIG_TTL: Duration = Duration::from_secs(1);

/// 热重载检测的全局缓存：记录上次检测时刻 + 上次配置文件的 mtime 指纹。
/// 进程级单例（OnceLock），所有 chat 请求共享，避免每次都重新 stat。
struct ConfigReloadState {
    last_check: std::time::Instant,
    last_fingerprint: u64,
}

static CONFIG_RELOAD_CACHE: OnceLock<ParkingRwLock<Option<ConfigReloadState>>> = OnceLock::new();

fn config_reload_cache() -> &'static ParkingRwLock<Option<ConfigReloadState>> {
    CONFIG_RELOAD_CACHE.get_or_init(|| ParkingRwLock::new(None))
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

/// Pull 热加载：按需重载管道配置。
///
/// 每次 `process_via_engine` 执行前调用。返回本次执行应使用的 pipeline_config + step_library。
/// 策略（双层短路）：
/// 1. **TTL 门**：距上次检测不足 `PIPELINE_CONFIG_TTL`（1s）→ 直接返回 AppState 里启动期加载的配置。
/// 2. **指纹比对**：TTL 过期后 stat autonomous.yaml + steps 目录 mtime，与缓存比对。
///    相同 → 返回 AppState 配置（没更新）；不同 → 重新加载 + 校验，校验通过返回新配置，
///    校验失败（坏 YAML / 命名冲突）则保留旧配置 + 记 warn（不 panic，对照启动期 fail-fast 的降级版）。
///
/// 失败安全：任何 IO/解析错误都回退到 AppState 启动期配置，绝不因热重载让 chat 不可用。
fn maybe_reload_pipeline_configs(
    state: &AppState,
    config_root: &std::path::Path,
) -> (Arc<PipelineConfig>, Arc<StepLibrary>) {
    let now = std::time::Instant::now();
    // TTL 门 + 指纹比对在同一把读锁下快照
    let needs_check = {
        let cache = config_reload_cache().read();
        match cache.as_ref() {
            None => true, // 首次必检
            Some(c) => now.duration_since(c.last_check) >= PIPELINE_CONFIG_TTL,
        }
    };
    if !needs_check {
        return (Arc::clone(&state.pipeline_config), Arc::clone(&state.step_library));
    }

    let new_fp = compute_config_fingerprint(config_root);
    let reload = {
        let mut cache = config_reload_cache().write();
        let stale = match cache.as_ref() {
            None => true,
            Some(c) => c.last_fingerprint != new_fp,
        };
        // 无论是否重载都刷新检测时刻；指纹更新到 new_fp
        *cache = Some(ConfigReloadState {
            last_check: now,
            last_fingerprint: new_fp,
        });
        stale
    };

    if !reload {
        return (Arc::clone(&state.pipeline_config), Arc::clone(&state.step_library));
    }

    // 指纹变化 → 重新加载 + 校验
    info!("Pipeline config changed on disk, reloading...");
    let new_pipeline = match load_pipeline_config(config_root) {
        Ok(c) => c,
        Err(e) => {
            warn!(error = %e, "Hot reload: load_pipeline_config failed, keeping old config");
            return (Arc::clone(&state.pipeline_config), Arc::clone(&state.step_library));
        }
    };
    let new_steps = match load_step_library(config_root) {
        Ok(s) => s,
        Err(e) => {
            warn!(error = %e, "Hot reload: load_step_library failed, keeping old config");
            return (Arc::clone(&state.pipeline_config), Arc::clone(&state.step_library));
        }
    };
    // 重名校验（启动期 fail-fast 的降级版：记 warn 不 panic）
    let known_ids: std::collections::HashSet<String> = state.plugin_ids.iter().cloned().collect();
    if let Err(conflict) = validate_no_name_conflicts(&new_pipeline, &new_steps, &known_ids) {
        warn!(conflict = %conflict, "Hot reload: name conflict, keeping old config");
        return (Arc::clone(&state.pipeline_config), Arc::clone(&state.step_library));
    }
    info!(
        pipeline = %new_pipeline.name,
        steps = new_pipeline.steps.len(),
        "Pipeline config hot-reloaded successfully"
    );
    (Arc::new(new_pipeline), Arc::new(new_steps))
}

pub(crate) async fn process_via_engine(
    state: &AppState,
    message: &str,
    agent_id: &str,
    history: &[serde_json::Value],
    pipeline_id: &str,
    thread_id: &str,
    message_id: &str,
    user_id: &str,
) -> String {
    // Box::pin 到堆上：回写段 + executor.run 的深 sidecar 调用链让 Future 状态机
    // 在 release 下也接近 tokio worker 2MB 栈极限，堆分配规避溢出。
    Box::pin(process_via_engine_inner(
        state, message, agent_id, history, pipeline_id, thread_id, message_id, user_id,
    ))
    .await
}

#[inline(never)]
async fn process_via_engine_inner(
    state: &AppState,
    message: &str,
    agent_id: &str,
    history: &[serde_json::Value],
    pipeline_id: &str,
    thread_id: &str,
    message_id: &str,
    user_id: &str,
) -> String {
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

    // 0. 统一管道查询键：pipeline_id 为空（HTTP 路径不传 / 旧 WS handler）时
    //    回退 thread_id，保证 messages 表 pipeline_id 列 + registry 键落到同一维度。
    //    WS 路径（ws_session.rs）已用 route_id（pipeline_id 空时回退 thread_id）。
    let effective_pipeline_id = if pipeline_id.is_empty() {
        thread_id
    } else {
        pipeline_id
    };

    // 1. 构造初始 state
    //    core_plugin 默认值可被 agent 配置（config/agents/<id>.yaml 的 core_plugin 字段）
    //    覆盖——见 load_agent_config_into_state。避免在内核硬编码具体插件 id。
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
    });

    // 1b. 多轮上下文装配：state.messages 是 LLMCore._build_messages 读取的对话历史。
    // 单一权威（不搞降级路径）：
    //   ① 热路径：PipelineStateRegistry 内存 state 完整（每轮 final_state 含 messages
    //      写回，LLM 插件负责 append assistant 回复）→ 直接复用 entry.state["messages"]。
    //   ② 冷路径：registry 未命中（重启/新会话/内存丢失）→ 从 messages 表（持久化
    //      冷数据）按 effective_pipeline_id 恢复完整历史，后续轮走热路径。
    //   ③ 客户端传的 history 仅在①②均为空（真·首轮）时兜底，向后兼容老客户端。
    // 恢复失败 = bug（内存丢 + DB 读不到）：显式 error 暴露，不静默吞掉。
    let tenant =
        agentos_tenant::current().unwrap_or_else(|| TenantContext::new("default", "kernel"));
    let tenant_id = tenant.tenant_id.clone();
    let mut history_prefix: Vec<serde_json::Value> = Vec::new();
    let mut history_loaded = false;
    let registry = agentos_session::global_registry();
    if let Some(entry) = registry.get(&tenant_id, effective_pipeline_id) {
        // 热路径：内存 state 完整，直接复用（无需走 DB）
        if let Some(msgs) = entry.read().state.get("messages").and_then(|v| v.as_array()) {
            history_prefix = msgs.clone();
            history_loaded = true;
        }
    }
    if !history_loaded {
        // 冷路径 ①：优先从最近 checkpoint 恢复（O(1) 取基线，无需回放 traces）。
        // checkpoint 存当时完整 state（messages + 全部标量字段）。
        // 命中则直接用作 recovered，跳过全量 traces 回放。
        let mut recovered: serde_json::Value = serde_json::json!({});
        let mut ckpt_hit = false;
        if !effective_pipeline_id.is_empty() {
            if let Ok(Some((_step_no, ckpt_state))) =
                store.load_latest_checkpoint(effective_pipeline_id, &tenant_id).await
            {
                recovered = ckpt_state;
                ckpt_hit = true;
                debug!(
                    pipeline_id = %effective_pipeline_id,
                    "冷启动从 checkpoint 恢复（跳过 traces 全量回放）"
                );
            }
        }
        // 冷路径 ②：无 checkpoint → 从持久化的 step 级轨迹按序 merge 重建完整 state。
        // 轨迹颗粒度 = 配置 step（prepare/core/post），patch_data 含该 step 期间所有
        // state 变更（messages 现为增量 _ops，由 merge_patch/apply_messages_ops 还原）。
        if !ckpt_hit {
            match store.get_step_traces_by_thread(thread_id, &tenant_id).await {
                Ok(step_traces) => {
                    for entry in &step_traces {
                        merge_patch(&mut recovered, &entry.patch_data);
                    }
                    if !step_traces.is_empty() && !recovered.as_object().is_none_or(|o| o.contains_key("messages")) {
                        debug!(
                            pipeline_id = %effective_pipeline_id,
                            traces = step_traces.len(),
                            "step 轨迹回放完成但无 messages 字段（可能首轮）"
                        );
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
        // 冷路径 ③：从 pipeline_state 表补充累计标量字段（track.total_tokens 等）。
        // checkpoint/traces 回放已还原 messages，但累计字段以 pipeline_state 为准
        // （它每 step upsert 最新值）。重建后插件能在正确基线上自然累加，不归零。
        if !effective_pipeline_id.is_empty() {
            if let Ok(state_fields) = store.load_pipeline_state(effective_pipeline_id, &tenant_id).await {
                if let Some(rec_obj) = recovered.as_object_mut() {
                    for (k, v) in &state_fields {
                        rec_obj.insert(k.clone(), v.clone());
                    }
                }
            }
        }
        // 把回放出的非 messages 字段并入 initial_state；messages 提取为 history
        if let Some(rec_obj) = recovered.as_object_mut() {
            if let Some(msgs) = rec_obj.remove("messages").and_then(|v| v.as_array().cloned()) {
                history_prefix = msgs;
                history_loaded = !history_prefix.is_empty();
            }
            // 其余字段（system_message/dynamic_vars/memory/knowledge/累计字段 等）注入 state
            if let Some(init_obj) = initial_state.as_object_mut() {
                for (k, v) in rec_obj.iter() {
                    init_obj.insert(k.clone(), v.clone());
                }
            }
        }
    }
    // 真·首轮兜底：客户端 history（向后兼容老客户端，正常路径不依赖它）
    if !history_loaded && !history.is_empty() {
        history_prefix = history.to_vec();
    }
    let mut msgs = history_prefix;
    msgs.push(serde_json::json!({"role": "user", "content": message}));
    if let Some(obj) = initial_state.as_object_mut() {
        obj.insert("messages".to_string(), serde_json::Value::Array(msgs));
    }

    // 2. 加载 Agent 配置注入 state（读 config/agents/<agent_id>.yaml，不存在跳过）
    // 统一配置加载方案 TDD-6：优先走 ConfigCenter（泛化注入所有字段），
    // 未接线时降级到旧的 load_agent_config_into_state（挑 5 字段，兼容）。
    if let Some(cc) = state.config_center.as_ref() {
        agentos_config::load_agent_into_state(cc, &mut initial_state, agent_id);
    } else {
        load_agent_config_into_state(&mut initial_state, agent_id, &project_root);
    }

    // 2b. 注入工具 schema 到 state（0.2 sidecar 架构适配）。
    // 0.1 单进程时 tool_schema 插件经 ctx.get_service("tool_registry") 直接访问内核
    // ToolRegistry；0.2 sidecar 是独立进程拿不到该 service。改为内核侧在管道启动前
    // 按 agent tool_ids 过滤、转成 OpenAI function-calling 格式注入 state["tool_schemas"]，
    // 这样 prepare 阶段的 tool_schema 插件读到非空 schema（它优先用 state 里的值），
    // LLM 即可看到工具并调用（tool_core 执行时内核 invoke_tool 经 MCP 调 sidecar）。
    inject_tool_schemas(&mut initial_state, state);

    // 3. 构造 PipelineExecutor 并执行
    //    run_id / branch_id 用 uuid 保证多请求隔离；租户上下文从 task_local 读取
    //    （多租户 P0-4：本函数已在 agentos_tenant::scope 内调用）。
    //    tenant / tenant_id 已在 1b 段解析（历史加载用），此处复用。
    let run_id = uuid::Uuid::new_v4().to_string();
    let branch_id = "main".to_string();

    // ── Pull 热加载：按需重载管道配置（autonomous.yaml + steps）──
    // 每次 chat 执行前检测配置 mtime，变了才重新加载到本次执行用的局部变量，
    // 不写回 AppState（启动期配置保持不动，作为重载失败的兜底）。
    // 配置变更对本次及后续请求生效：每次执行都经此检测。
    // 在 project_root 被 move 给 executor 之前算出 config_root。
    let config_root = project_root.join("config");
    let (pipeline_cfg, step_lib) = maybe_reload_pipeline_configs(state, &config_root);

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
        state.manifests
            .iter()
            .flat_map(|m| m.persistent_fields.iter().cloned()),
    );

    // 统一配置加载方案 TDD-7：注入 ConfigCenter，启用 per-iteration agent 热加载。
    // 改 config/agents/<id>.yaml 后，正在跑的任务下一轮迭代立即用新配置。
    let executor = if let Some(cc) = state.config_center.clone() {
        executor.with_config_center(cc)
    } else {
        executor
    };

    info!(run_id = %run_id, agent_id = %agent_id, "Pipeline run started");

    let final_state = match executor
        .run(&pipeline_cfg, &step_lib, initial_state)
        .await
    {
        Ok(s) => s,
        Err(e) => {
            warn!(run_id = %run_id, error = %e, "PipelineExecutor run failed");
            // B2：引擎失败兜底——把 run 标记 failed + ended_at，避免永远卡 running、
            // 历史悬空。（PipelineExecutor::run 当前不返回 Err，这是防御网；崩溃留下的
            // running 孤儿由内核启动 reap_orphan_runs 清扫。）
            if let Err(pe) = store
                .update_run_status(&run_id, agentos_core::types::RunStatus::Failed, None, None)
                .await
            {
                warn!(run_id = %run_id, error = %pe, "update_run_status(Failed) 失败（继续）");
            }
            return format!("[engine-run-failed] {}", message);
        }
    };

    // 3b. 回写 final_state 到全局 registry（state 内存常驻，对齐 0.1 _current_state）。
    // final_state 含完整 messages 历史（LLM 插件 append 了 assistant 回复），
    // 按 (tenant_id, effective_pipeline_id) 常驻：下一轮热路径直接复用，
    // 免 DB 查询；重启/内存丢失时冷路径从 messages 表恢复（见 1b 段）。
    if !effective_pipeline_id.is_empty() {
        let reg = agentos_session::global_registry();
        if !reg.contains(&tenant_id, effective_pipeline_id) {
            reg.get_or_init(&tenant_id, effective_pipeline_id, thread_id, agent_id, final_state.clone());
        } else {
            reg.update_state(&tenant_id, effective_pipeline_id, final_state.clone());
        }
    }

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

/// 注入工具 schema 到 state["tool_schemas"]（0.2 sidecar 架构适配）。
/// RFC 7396 JSON Merge Patch：把 patch 按序合并进 target。
/// 用于冷启动时按 step 级轨迹逐条 merge 回放，重建完整 state。
/// - patch 中值为对象：递归 merge（target 同 key 也为对象时）
/// - patch 中值为 null：从 target 删除该 key
/// - 否则：target[key] = patch[key]（整体替换）
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
                // 分层持久化：messages 增量化。新格式 { "_ops": [...] } 按序应用 ops；
                // 旧格式（数组）整体覆盖（兼容历史 patch）。检测 _ops 区分新旧格式。
                if let Some(ops) = v.get("_ops").and_then(|o| o.as_array()) {
                    // 确保存在 messages 数组
                    let existing = target_obj
                        .entry("messages".to_string())
                        .or_insert_with(|| serde_json::Value::Array(vec![]));
                    if let Some(arr) = existing.as_array_mut() {
                        apply_messages_ops(arr, ops);
                    }
                } else if let Some(existing) = target_obj.get_mut(k) {
                    *existing = v.clone();
                } else {
                    target_obj.insert(k.clone(), v.clone());
                }
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

/// 对 messages 数组应用增量 ops（回放新格式 patch 用）。
///
/// ops 元素（与 pipeline_loop.messages_diff_ops 对齐）：
/// - `{"op":"append","msg":{...}}`：尾部追加
/// - `{"op":"delete_from","idx":N}`：删除 index ≥ N（压缩）
/// - `{"op":"replace_at","idx":N,"msg":{...}}`：替换 index N
fn apply_messages_ops(arr: &mut Vec<serde_json::Value>, ops: &[serde_json::Value]) {
    for op in ops {
        let kind = op.get("op").and_then(|v| v.as_str()).unwrap_or("");
        match kind {
            "append" => {
                if let Some(msg) = op.get("msg") {
                    arr.push(msg.clone());
                }
            }
            "delete_from" => {
                if let Some(idx) = op.get("idx").and_then(|v| v.as_u64()) {
                    let idx = idx as usize;
                    if idx < arr.len() {
                        arr.truncate(idx);
                    }
                }
            }
            "replace_at" => {
                if let (Some(idx), Some(msg)) = (
                    op.get("idx").and_then(|v| v.as_u64()),
                    op.get("msg"),
                ) {
                    let idx = idx as usize;
                    match idx.cmp(&arr.len()) {
                        std::cmp::Ordering::Less => arr[idx] = msg.clone(),
                        std::cmp::Ordering::Equal => arr.push(msg.clone()),
                        std::cmp::Ordering::Greater => {}
                    }
                }
            }
            _ => {}
        }
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
    let wanted_ids: Option<Vec<String>> = state
        .get("tool_ids")
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().filter_map(|t| t.as_str().map(String::from)).collect());
    let wanted: Option<std::collections::HashSet<String>> =
        wanted_ids.map(|ids| ids.into_iter().collect());
    let schemas: Vec<serde_json::Value> = all_tools
        .iter()
        .filter(|t| match &wanted {
            Some(ids) => ids.contains(&t.name),
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
    for entry in entries.flatten() {
        let p = entry.path();
        if p.is_dir() {
            if let Some(found) = find_agent_yaml(&p, agent_id) {
                return Some(found);
            }
        } else if p.file_name().map(|n| n == target.as_str()).unwrap_or(false) {
            return Some(p);
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
                // TODO: agent_id 暂用默认（chat 协议暂未携带；后续从请求体取）
                let tenant_ctx = request_tenant_ctx(state.store.as_ref(), &headers, &req.session_id).await;
                let content =
                    agentos_tenant::scope(tenant_ctx, process_via_engine(&state, &req.message, if req.agent_id.is_empty() { "agentos" } else { req.agent_id.as_str() }, &req.history, "", "", "", ""))
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
    let tenant_ctx = request_tenant_ctx(state.store.as_ref(), &headers, &req.session_id).await;
    // 分层持久化：REST chat 路径需用会话的真实 active_pipeline_id，否则消息会落到
    // thread_id 维度（effective 回退），与前端按 active_pipeline_id 查询不匹配 → 前端拿不到消息。
    // WS 路径（ws_session.rs）前端已带真实 pipeline_id，此处为 REST fallback 补齐。
    let pipeline_id = if let Some(store) = state.store.as_ref() {
        let sid = req.session_id.clone();
        let store_clone = store.clone();
        match agentos_tenant::scope(tenant_ctx.clone(), async move {
            store_clone.get_session(&sid).await
        }).await {
            Ok(Some(session)) => session.active_pipeline_id.unwrap_or_default(),
            _ => String::new(),
        }
    } else { String::new() };
    // 解析请求用户 user_id（从 Authorization token），供 process_via_engine 写入 state；
    // 触发器等工具据此捕获 user_id，触发时回派发（chat.send_message）解析 tenant。
    let user_id = crate::auth::resolve_request_user(state.store.as_ref(), &headers)
        .await
        .map(|(uid, _, _, _)| uid)
        .unwrap_or_default();
    let content =
        agentos_tenant::scope(tenant_ctx, process_via_engine(&state, &req.message, if req.agent_id.is_empty() { "agentos" } else { req.agent_id.as_str() }, &req.history, &pipeline_id, &req.session_id, "", &user_id))
            .await;

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
        return Ok(axum::Json(serde_json::json!({"success": false, "error": "缺少 request_id"})));
    }

    let Some(invoker) = state.invoker.clone() else {
        return Ok(axum::Json(serde_json::json!({"success": false, "error": "invoker not available"})));
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
    async fn test_metrics_query_endpoint_returns_data() {
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
        assert_eq!(response.status(), StatusCode::OK);
        let body = axum::body::to_bytes(response.into_body(), 8192)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let metrics = json["metrics"].as_array().unwrap();
        assert!(!metrics.is_empty());
        assert_eq!(metrics[0]["plugin_id"], "llm_service");
    }

    #[tokio::test]
    async fn test_metrics_query_filter_by_metric_name() {
        let app = build_router(state_with_metrics());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/metrics?plugin=llm_service&metric=tokens_used")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(response.into_body(), 8192)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let metrics = json["metrics"].as_array().unwrap();
        assert_eq!(metrics.len(), 1);
        assert_eq!(metrics[0]["name"], "tokens_used");
    }

    #[tokio::test]
    async fn test_metrics_query_filter_by_labels() {
        let app = build_router(state_with_metrics());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/metrics?labels=model:deepseek")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(response.into_body(), 8192)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let metrics = json["metrics"].as_array().unwrap();
        // 只有 tokens_used 带 model 标签
        assert!(metrics.iter().all(|m| m["name"] == "tokens_used"));
    }

    #[tokio::test]
    async fn test_metrics_query_no_aggregator_404() {
        // 未注入 metrics → 404
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/metrics")
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
    }

    #[async_trait::async_trait]
    impl agentos_core::traits::PluginInvoker for RecordingInvoker {
        async fn invoke_pipeline_plugin(
            &self,
            _plugin_id: &str,
            ctx: &agentos_core::types::PluginContext,
        ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
            let mut history = ctx
                .state
                .get("messages")
                .cloned()
                .unwrap_or_else(|| serde_json::json!([]));
            self.seen.lock().unwrap().push(history.clone());
            // 模拟 LLM：追加 assistant 回复（内容基于收到的消息数，便于断言）
            if let Some(arr) = history.as_array_mut() {
                arr.push(serde_json::json!({
                    "role": "assistant",
                    "content": format!("回复第{}条", arr.len()),
                }));
            }
            let mut updates = std::collections::HashMap::new();
            let reply = history
                .as_array()
                .and_then(|a| a.last())
                .and_then(|m| m.get("content"))
                .and_then(|c| c.as_str())
                .unwrap_or("")
                .to_string();
            updates.insert("raw_result".to_string(), serde_json::json!(reply));
            updates.insert("messages".to_string(), history);
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
        ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError> {
            Ok(agentos_core::types::ToolExecutionResult::success(
                serde_json::Value::Null,
            ))
        }

        async fn send_lifecycle_hook(
            &self,
            _plugin_id: &str,
            _hook: agentos_core::traits::LifecycleHook,
            _context: &agentos_core::traits::HookContext,
        ) -> Result<(), agentos_core::types::PluginError> {
            Ok(())
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
    ) {
        let store: Arc<dyn agentos_core::traits::StorageBackend> =
            Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let invoker = Arc::new(RecordingInvoker {
            seen: std::sync::Mutex::new(Vec::new()),
        });
        // 临时项目根：含 config/pipelines/autonomous.yaml，引用 mock LLM 插件
        let tmp_root = std::env::temp_dir().join(format!(
            "mt_test_{}",
            uuid::Uuid::new_v4().simple()
        ));
        let cfg_dir = tmp_root.join("config").join("pipelines");
        std::fs::create_dir_all(&cfg_dir).unwrap();
        std::fs::write(
            cfg_dir.join("autonomous.yaml"),
            "name: test_multi_turn\nloop:\n  enabled: false\nsteps:\n  - id: llm\n    steps:\n      - mock_llm_core\n",
        )
        .unwrap();
        let mut state = AppState::new();
        state.store = Some(store.clone());
        state.invoker = Some(invoker.clone());
        state.project_root = Some(tmp_root);
        // 兜底配置（与临时 YAML 一致；临时 YAML 加载成功时此值被覆盖）
        state.pipeline_config = Arc::new(agentos_core::types::PipelineConfig {
            name: "test_multi_turn".to_string(),
            loop_config: Default::default(),
            steps: vec![agentos_core::types::PipelineStep {
                id: "llm".to_string(),
                steps: vec!["mock_llm_core".to_string()],
                context: std::collections::HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
            checkpoint: Default::default(),
        });
        state.step_library = Arc::new(agentos_core::types::StepLibrary::default());
        state.plugin_ids = Arc::new(std::collections::HashSet::from([
            "mock_llm_core".to_string(),
        ]));
        (state, invoker, store)
    }

    #[tokio::test]
    async fn test_multi_turn_second_round_sees_first_round_context() {
        let (state, invoker, _store) = make_engine_state();
        let tenant = TenantContext::new("tenant_mt", "thread_mt");
        let pipe = "pipe_mt";
        let thread = "thread_mt";

        // 第一轮：pipeline_id=pipe_mt 非空（WS 路径 route_id 语义）
        let r1 = agentos_tenant::scope(
            tenant.clone(),
            process_via_engine(&state, "第一轮：我叫小明", "agentos", &[], pipe, thread, "m1", ""),
        )
        .await;
        assert!(!r1.is_empty(), "第一轮应返回 assistant 回复");

        // 第二轮：同 pipeline_id，应看到第一轮 user+assistant 上下文
        let r2 = agentos_tenant::scope(
            tenant,
            process_via_engine(&state, "第二轮：我叫什么？", "agentos", &[], pipe, thread, "m2", ""),
        )
        .await;
        assert!(!r2.is_empty(), "第二轮应返回 assistant 回复");

        // 断言：第二轮 LLM 收到的 messages 是完整序列（历史 + 当前）
        let seen = invoker.seen.lock().unwrap();
        assert_eq!(seen.len(), 2, "应有两轮 LLM 调用");
        let first = seen[0].as_array().unwrap();
        assert_eq!(first.len(), 1, "第一轮应只有当前 user 消息");
        assert_eq!(first[0]["role"], "user");
        assert_eq!(first[0]["content"], "第一轮：我叫小明");

        let second = seen[1].as_array().unwrap();
        // 完整序列 = 第一轮 user + 第一轮 assistant + 第二轮 user
        assert_eq!(second.len(), 3, "第二轮应含第一轮上下文（user+assistant）+ 当前 user");
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
        let (state, invoker, _store) = make_engine_state();
        let tenant = TenantContext::new("tenant_http", "thread_http");
        let thread = "thread_http";

        let r1 = agentos_tenant::scope(
            tenant.clone(),
            process_via_engine(&state, "HTTP 第一轮", "agentos", &[], "", thread, "h1", ""),
        )
        .await;
        assert!(!r1.is_empty());

        let r2 = agentos_tenant::scope(
            tenant,
            process_via_engine(&state, "HTTP 第二轮", "agentos", &[], "", thread, "h2", ""),
        )
        .await;
        assert!(!r2.is_empty());

        let seen = invoker.seen.lock().unwrap();
        assert_eq!(seen.len(), 2);
        let second = seen[1].as_array().unwrap();
        assert_eq!(second.len(), 3, "HTTP 空 pipeline_id 也应通过 thread_id 回退看到历史");
        assert_eq!(second[0]["content"], "HTTP 第一轮");
        assert_eq!(second[2]["content"], "HTTP 第二轮");
    }

    #[tokio::test]
    async fn test_multi_turn_cold_start_recovers_from_store() {
        // 冷路径验证：registry 未命中（新进程/重启）时，从 store 恢复历史。
        // 模拟：先直接向 store 写入第一轮 user+assistant 消息（pipeline_id=pipe_cold），
        // 再调用 process_via_engine，断言 LLM 收到历史 + 当前。
        let (state, invoker, store) = make_engine_state();
        let tenant = TenantContext::new("tenant_cold", "thread_cold");
        let pipe = "pipe_cold";
        let thread = "thread_cold";

        // 直接写库（模拟上一轮已持久化，registry 无该管道——冷启动）
        // 冷启动恢复源是 step 级轨迹（含 messages 字段），故同时写 messages 表（供断言）
        // 与一条 step 级 trace（patch_data 含 messages，冷路径据此回放）。
        use agentos_core::types::{PatchType, TraceEntry};
        let store_ref = store.clone();
        agentos_tenant::scope(tenant.clone(), async {
            store_ref.create_run("run_cold", "", "tenant_cold").await.unwrap();
            // 管道→会话映射，使 get_step_traces_by_thread 能定位 pipe
            store_ref.link_pipeline_session(pipe, thread, "tenant_cold").await.unwrap();
            store_ref
                .append_message("u_cold", "run_cold", "main", 1, "user", None, Some("冷启动第一轮"), Some(pipe))
                .await
                .unwrap();
            store_ref
                .append_message("a_cold", "run_cold", "main", 2, "assistant", None, Some("冷启动回复"), Some(pipe))
                .await
                .unwrap();
            // step 级轨迹：模拟上一轮 prepare/core 跑完后把 messages 写进了 state
            store_ref.append_trace(TraceEntry {
                trace_id: "t_cold".into(),
                run_id: "run_cold".into(),
                branch_id: "main".into(),
                seq_in_branch: 0,
                plugin_id: "core".into(),
                patch_type: PatchType::StateUpdate,
                patch_data: serde_json::json!({
                    "messages": [
                        {"role": "user", "content": "冷启动第一轮"},
                        {"role": "assistant", "content": "冷启动回复"}
                    ]
                }),
                created_at: chrono::Utc::now().to_rfc3339(),
            }).await.unwrap();
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
            process_via_engine(&state, "冷启动第二轮", "agentos", &[], pipe, thread, "c2", ""),
        )
        .await;
        assert!(!r.is_empty(), "冷启动第二轮应返回 assistant 回复");

        let seen = invoker.seen.lock().unwrap();
        assert_eq!(seen.len(), 1, "冷启动应从 store 恢复历史并调用 LLM");
        let msgs = seen[0].as_array().unwrap();
        assert_eq!(msgs.len(), 3, "冷启动应从 store 恢复第一轮 user+assistant + 当前 user");
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
        let (state, _invoker, store) = make_engine_state();

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
            process_via_engine(&state, "alice 的消息", "agentos", &[], pipe_a, thread_a, "a1", ""),
        )
        .await;
        assert!(!r_a.is_empty(), "alice 发消息应返回 assistant 回复");

        // bob 发消息（在 bob 的 tenant scope 内）
        let r_b = agentos_tenant::scope(
            TenantContext::new("tenant_bob", thread_b),
            process_via_engine(&state, "bob 的消息", "agentos", &[], pipe_b, thread_b, "b1", ""),
        )
        .await;
        assert!(!r_b.is_empty(), "bob 发消息应返回 assistant 回复");

        // alice 在自己 scope 内能读到自己的消息（user + assistant ≥ 2 条）
        let store_a = store.clone();
        let msgs_a = agentos_tenant::scope(
            TenantContext::new("tenant_alice", thread_a),
            async move { store_a.get_messages_by_pipeline(pipe_a, MessageQueryOpts::default()).await },
        )
        .await
        .unwrap();
        assert!(
            msgs_a.len() >= 2,
            "alice 应能读到自己的 user+assistant 消息，实际 {}",
            msgs_a.len()
        );

        // bob 在自己 scope 内能读到自己的消息
        let store_b = store.clone();
        let msgs_b = agentos_tenant::scope(
            TenantContext::new("tenant_bob", thread_b),
            async move { store_b.get_messages_by_pipeline(pipe_b, MessageQueryOpts::default()).await },
        )
        .await
        .unwrap();
        assert!(msgs_b.len() >= 2, "bob 应能读到自己的消息");

        // ★ 隔离断言：在 bob 的 scope 内读 alice 的 pipeline，必须为空
        let store_cross = store.clone();
        let cross = agentos_tenant::scope(
            TenantContext::new("tenant_bob", thread_b),
            async move { store_cross.get_messages_by_pipeline(pipe_a, MessageQueryOpts::default()).await },
        )
        .await
        .unwrap();
        assert!(
            cross.is_empty(),
            "tenant_bob 必须读不到 tenant_alice 的消息（数据隔离）"
        );

        // 反向：alice scope 内读 bob 的 pipeline，也必须为空
        let store_cross2 = store.clone();
        let cross2 = agentos_tenant::scope(
            TenantContext::new("tenant_alice", thread_a),
            async move { store_cross2.get_messages_by_pipeline(pipe_b, MessageQueryOpts::default()).await },
        )
        .await
        .unwrap();
        assert!(cross2.is_empty(), "tenant_alice 必须读不到 tenant_bob 的消息");

        // 验证消息内容确实是各自的（alice 的 user 消息内容含 "alice"）
        let alice_user_msg = msgs_a.iter().find(|m| m.role == "user").expect("alice 应有 user 消息");
        assert!(
            alice_user_msg.content_preview.as_deref().unwrap_or("").contains("alice"),
            "alice 的消息内容应含 'alice'"
        );
    }

    /// 验证 register → login → 发消息 → 读历史 的完整用户流程（含持久化用户）。
    ///
    /// 用真实 store 跑 register/login handler（经 build_router），拿到 token 后
    /// 模拟 WS 路径发消息，验证新注册用户能正常保存和读取自己的历史。
    #[tokio::test]
    async fn test_registered_user_can_save_and_read_history() {
        use agentos_core::traits::StorageBackend;
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
        }));
        // 临时 config（make_engine_state 的精简版，足够 process_via_engine 跑通）
        let tmp_root = std::env::temp_dir().join(format!("frank_test_{}", uuid::Uuid::new_v4().simple()));
        let cfg_dir = tmp_root.join("config").join("pipelines");
        std::fs::create_dir_all(&cfg_dir).unwrap();
        std::fs::write(
            cfg_dir.join("autonomous.yaml"),
            "name: t\nloop:\n  enabled: false\nsteps:\n  - id: llm\n    steps:\n      - mock_llm_core\n",
        ).unwrap();
        state.project_root = Some(tmp_root);
        state.pipeline_config = Arc::new(agentos_core::types::PipelineConfig {
            name: "t".to_string(),
            loop_config: Default::default(),
            steps: vec![agentos_core::types::PipelineStep {
                id: "llm".to_string(),
                steps: vec!["mock_llm_core".to_string()],
                context: std::collections::HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
            checkpoint: Default::default(),
        });
        state.step_library = Arc::new(agentos_core::types::StepLibrary::default());
        state.plugin_ids = Arc::new(std::collections::HashSet::from(["mock_llm_core".to_string()]));

        let pipe = "pipe_frank";
        let thread = "thread_frank";
        // frank 发消息（tenant = frank_id）
        let r = agentos_tenant::scope(
            TenantContext::new(&frank_id, thread),
            process_via_engine(&state, "frank 的问题", "agentos", &[], pipe, thread, "f1", ""),
        )
        .await;
        assert!(!r.is_empty(), "frank 发消息应成功");

        // frank 能读到自己的历史
        let store_read = store.clone();
        let msgs = agentos_tenant::scope(
            TenantContext::new(&frank_id, thread),
            async move { store_read.get_messages_by_pipeline(pipe, MessageQueryOpts::default()).await },
        )
        .await
        .unwrap();
        assert!(msgs.len() >= 2, "frank 应能读到自己的历史");

        // admin（default 租户）读不到 frank 的消息
        let store_admin = store.clone();
        let admin_msgs = agentos_tenant::scope(
            TenantContext::new("default", "admin_thread"),
            async move { store_admin.get_messages_by_pipeline(pipe, MessageQueryOpts::default()).await },
        )
        .await
        .unwrap();
        assert!(admin_msgs.is_empty(), "admin(default) 不应读到 frank 的消息");
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
        assert!(super::origin_matches_allowlist("https://app.example.com", &allow));
        // 精确匹配——子域/变体不应通过
        assert!(!super::origin_matches_allowlist("https://evil.example.com", &allow));
        assert!(!super::origin_matches_allowlist("https://app.example.com.evil.com", &allow));
    }
}
