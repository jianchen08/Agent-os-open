//! Axum HTTP/WebSocket API 服务器——RESTful API 端点与 WebSocket 流式通信。
//! AC-06-3: /health 返回 200；AC-06-4: WebSocket 可连接收发消息；AC-06-5:
//! Schema 聚合端点。
//!
//! [来源: docs/tasks/task_07_llm_api.md]

use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::OnceLock;
use std::time::{Duration, SystemTime};

use parking_lot::RwLock as ParkingRwLock;

use agentos_core::traits::StorageBackend;
use agentos_core::types::TenantContext;
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

use crate::pipeline_loader::{
    load_pipeline_with_checks, load_step_library, validate_no_name_conflicts,
};
use serde::{Deserialize, Serialize};
use tracing::{debug, error, info, warn};

use crate::auth::{
    change_password_handler, login_handler, logout_handler, me_handler, refresh_handler,
    register_handler,
};
use crate::routes::{
    actions_execute_handler, get_pipeline_config_with_etag, get_plugin_config_with_etag,
    get_user_env_handler, health_handler, message_segment_get_handler,
    message_segments_list_handler, metrics_prometheus_handler, pending_inputs_clear_handler,
    pending_inputs_delete_handler, pending_inputs_list_handler, pending_inputs_update_handler,
    pipelines_handler, pipelines_runs_handler, pipelines_state_handler,
    plugins_contract_status_handler, plugins_dependents_handler, plugins_hosts_handler,
    plugins_set_enabled_handler, plugins_source_arbitration_handler, plugins_status_handler,
    put_pipeline_config_handler, put_plugin_config_handler, put_user_env_handler, schema_handler,
    serve_upload_handler, system_memstats_handler, system_restart_handler, tools_handler,
    validate_all_plugins_handler, AppState,
};
use crate::session_routes::{
    create_session_handler, delete_session_handler, list_session_messages_handler,
    list_sessions_handler, sessions_schema_handler, update_session_agent_handler,
    update_session_handler,
};
use agentos_http::auth::resolve_request_tenant_id;
use agentos_http::error::ApiError;
use agentos_session::router::PipelineDispatcher;

mod anchor_liveness;
mod step_error_fold;

/// WebSocket 消息请求体。
#[derive(Debug, Deserialize, Serialize)]
pub struct WsRequest {
    #[serde(default)]
    pub message: String,
    #[serde(default)]
    pub session_id: String,
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
    /// 失败终态标记（自 run 控制状态键真实折算，2026-10-15 刀2）：失败 run 对
    /// HTTP 调用方显式呈现，不再以 200+文本虚报成功
    pub failed: Option<bool>,
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
        // 上传文件静态服务（artifacts 上传的媒体；需鉴权——前端统一走鉴权
        // 取流加载器，无裸直链消费面，ADR 2026-10-10）
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
        // 消息段（多代切换/压缩原文存档）：段清单按区间聚合；单段含成员全文解析
        .route(
            "/api/v1/pipelines/{pipeline_id}/message-segments",
            get(message_segments_list_handler),
        )
        .route(
            "/api/v1/message-segments/{id}",
            get(message_segment_get_handler),
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
        // 通用用户空间 .env 读写（2026-09-28 批次 A2：动态 provider key 等
        // 无法走 manifest env-target 静态声明制的变量面；保留名 422 拒绝）
        .route(
            "/api/v1/config/env",
            get(get_user_env_handler).put(put_user_env_handler),
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
        // 批次C 观测面：宿主盒子快照（只读；鉴权走 /api/v1 前缀读面）
        .route("/api/v1/plugins/hosts", get(plugins_hosts_handler))
        // D4（审查总册 P1-15）：多源仲裁「用户副本未生效」清单（只读告警面）
        .route(
            "/api/v1/plugins/source-arbitration",
            get(plugins_source_arbitration_handler),
        )
        // G8 优雅重启（admin）：排空 running runs → exit 75，监督者拉起新进程
        .route(
            "/api/v1/system/restart",
            axum::routing::post(system_restart_handler),
        )
        // M1 测量面：mimalloc 分配器统计快照（内核自有只读诊断端点，
        // 鉴权走 /api/v1 前缀读面）
        .route("/api/v1/system/memstats", get(system_memstats_handler))
        // 堆栈级诊断面：内核活内存按结构分解 + 任务级活动转储（只读，
        // 鉴权同 memstats 前缀读面）
        .route(
            "/api/v1/system/memory-breakdown",
            get(crate::system_diagnostics::system_memory_breakdown_handler),
        )
        .route(
            "/api/v1/system/task-dump",
            get(crate::system_diagnostics::system_task_dump_handler),
        )
        // mimalloc 快照差分：POST 记基线 / GET 读与基线的逐字段差值
        .route(
            "/api/v1/system/memstats/snapshot",
            axum::routing::post(crate::system_diagnostics::memstats_snapshot_record_handler)
                .get(crate::system_diagnostics::memstats_snapshot_diff_handler),
        )
        .route(
            "/api/v1/plugins/{id}/enabled",
            axum::routing::put(plugins_set_enabled_handler),
        )
        // §2.4 卸载/禁用事前提醒数据源：谁依赖我的服务（requires_services 反向边）
        .route(
            "/api/v1/plugins/{id}/dependents",
            axum::routing::get(plugins_dependents_handler),
        )
        // 监控 M5b：Prometheus 导出端点保留内核（运维契约：抓取方通常不鉴权且
        // URL 稳定优先，boot-plugin 立项 §二/第三刀决策）。查询面 /api/v1/metrics
        // 已迁 boot-plugin 第三刀：HTTP 面在 plugins/shared/metrics_admin 插件
        // （/ext/metrics_admin/query），能力层 metrics-admin handler 在
        // metrics/capability.rs（agentos-kernel.rs 启动期注册）。
        .route("/metrics", get(metrics_prometheus_handler))
        // AC-06-4: WebSocket 端点（前端写死连 /ws/chat，0.1 路径格式）。
        .route("/ws/chat", get(ws_handler))
        // WS 一次性票据签发（auth user）：前端先 POST 换 60s 单次消费 ticket，
        // 再以 ?ticket= 握手——token 不再进 URL query（?token= 兼容保留）。
        .route(
            "/api/v1/ws-ticket",
            post(crate::ws_ticket::issue_ticket_handler),
        )
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
        .route("/api/v1/auth/register", post(register_handler))
        .route(
            "/api/v1/auth/change-password",
            post(change_password_handler),
        );
    // 统一通用数据接口（task_01）已迁移为 boot-plugin（第一刀）：/api/v1/db/* 的 axum nest
    // 已摘除（能力层在 agentos-db-admin handler，HTTP 面在 db_admin 插件）。例外
    // /api/v1/db/read = db-read 读契约管理端点（C2 刀1，handler 在 db-admin read_faces）。
    let static_router = static_router.route(
        "/api/v1/db/read",
        post(agentos_db_admin::db_read_http_handler),
    );
    // P3：动态挂载插件 HTTP 端点（http_routes → dispatcher）
    let router =
        crate::http_dispatcher::build_router_with_http_routes(state.clone(), static_router);
    // 鉴权层（default-deny 枚举制）：受保护前缀 + 匿名豁免清单，声明见
    // [AUTH_PROTECTED_PREFIXES] 一组 pub 常量（对账枚举单一真值源）。
    let auth_layer = from_fn_with_state(state.clone(), write_surface_auth);
    // CORS：开发期前端通过 VITE_API_BASE_URL 直连内核（http://localhost:9100），
    // 浏览器跨域请求需要预检（OPTIONS）+ 响应头带 Access-Control-Allow-Origin。
    // 作为最外层中间件，拦截 OPTIONS 预检并给所有响应注入 CORS 头。
    router
        .with_state(state)
        .layer(auth_layer)
        .layer(from_fn(cors_middleware))
}

/// 鉴权面声明（P-2 default-deny 枚举制，ADR 2026-10-10；2026-09-11 未竟第二刀）。
///
/// 单一真值源：鉴权层与对账测试共用这组常量；scripts/gen_openapi.py 后续复用
/// 同一对账枚举（枚举键 = server.rs / http_dispatcher.rs 注册调用里的路径字面量
/// × 下列各清单，Python 侧同型扫描即可，先例 scripts/check_kernel_port_sync.py
/// 正则抽源码）。
pub const AUTH_PROTECTED_PREFIXES: &[&str] = &[
    // 内核静态 API 全域（含 /api/v1/datasource 数据源代理——前端 apiClient
    // 统一带 token，代理面默认鉴权）
    "/api/v1/",
    // 附件取流面：前端消费面统一走鉴权取流加载器（2026-09-28 文件加载器
    // 路由），裸直链已清零；svg 同源 XSS 匿名面随之闭合
    "/uploads/",
    // WebSocket 握手面（豁免清单内的握手端点票据/token 自鉴权）
    "/ws/",
];

/// 受保护路径中不落任何前缀的精确成员（"/metrics" 无子面）。
pub const AUTH_PROTECTED_EXACT: &[&str] = &["/metrics"];

/// 匿名豁免清单：受保护域内允许匿名到达的精确路径，每项注明为何匿名安全。
/// 条目必须存在于注册面（对账测试双向对账，过期条目即红）。
pub const AUTH_ANONYMOUS_ALLOW: &[&str] = &[
    // 不匿名无法登录（同面自带登录限流）
    "/api/v1/auth/login",
    // 注册是登录前唯一写入口（同面自带全局限频）
    "/api/v1/auth/register",
    // handler 自校验 token：无 token 即 401，匿名路由无害
    "/api/v1/auth/me",
    // handler 自校验 refresh token（单次轮换 + 口令绑定）
    "/api/v1/auth/refresh",
    // 仅消费请求体携带的 refresh token，无 token 即无副作用
    "/api/v1/auth/logout",
    // handler 自校验 access token + 口令绑定段（自服务面，与角色无关——
    // 落角色门会把普通用户改自己口令误拒为 403）
    "/api/v1/auth/change-password",
    // 握手自鉴权：?ticket= 一次性票据 / ?token= Bearer，无效 4001 拒绝
    "/ws/chat",
];

/// 任意已认证用户（无角色门）的受保护路径：ws-ticket 签发——票据只是本人
/// WS 握手凭证（单次消费 + 60s TTL），非管理面写操作。
pub const AUTH_ANY_USER_ALLOW: &[&str] = &["/api/v1/ws-ticket"];

/// 鉴权层域外（鉴权另有所属，对账清单）：
/// - /health：存活探测（release.yml 冒烟与装机探测），无数据面；
/// - /ext/{*rest}：插件声明制鉴权（manifest auth 字段缺省 401 fail-closed，
///   ADR 2026-09-11），由 http_dispatcher 自持。
pub const AUTH_OUTSIDE_MIDDLEWARE: &[&str] = &["/health", "/ext/{*rest}"];

/// 鉴权面分类（对账枚举的判定结果；`None` = 路径未落入任何一侧）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthSurfaceClass {
    /// 匿名豁免（handler 自鉴权或登录前入口）
    AnonymousAllow,
    /// 任意已认证用户，无角色门
    AnyUser,
    /// 受保护：GET → admin/viewer，其余 method → admin
    Protected,
    /// 鉴权层域外（/health 探测、/ext 声明制）
    OutsideMiddleware,
}

/// 路径 → 鉴权面分类。豁免/任意用户清单精确匹配优先于受保护前缀；未落入
/// 任何一侧返回 None——注册面出现此类路径时对账测试判红（新路由必须在
/// 受保护前缀内，或显式登记进某一清单并注明缘由）。
pub fn classify_auth_surface(path: &str) -> Option<AuthSurfaceClass> {
    if AUTH_ANONYMOUS_ALLOW.contains(&path) {
        return Some(AuthSurfaceClass::AnonymousAllow);
    }
    if AUTH_ANY_USER_ALLOW.contains(&path) {
        return Some(AuthSurfaceClass::AnyUser);
    }
    if AUTH_PROTECTED_PREFIXES.iter().any(|p| path.starts_with(p))
        || AUTH_PROTECTED_EXACT.contains(&path)
    {
        return Some(AuthSurfaceClass::Protected);
    }
    if AUTH_OUTSIDE_MIDDLEWARE.contains(&path) {
        return Some(AuthSurfaceClass::OutsideMiddleware);
    }
    None
}

/// 内核静态面鉴权中间件（default-deny 枚举制，ADR 2026-10-10）。
///
/// `needs_auth = 受保护路径 && !匿名豁免`：命中受保护前缀且不在豁免清单的
/// 路径按 method 分权（GET → admin/viewer，POST/PUT/PATCH/DELETE → admin，
/// require_surface_role）；受保护前缀内新注册的路由缺省即受保护（枚举制
/// 翻转的本意），前缀外新路由须登记进域外清单，否则对账测试红。豁免清单
/// 与域外清单逐项缘由见 [AUTH_ANONYMOUS_ALLOW] / [AUTH_OUTSIDE_MIDDLEWARE]。
async fn write_surface_auth(
    State(state): State<AppState>,
    req: Request,
    next: Next,
) -> Result<Response, ApiError> {
    let path = req.uri().path();
    let method = req.method().clone();

    // WS 票据签发：任意已认证用户（user/admin 均可），不做角色区分——
    // 票据只是本人 WS 握手凭证（单次消费 + 60s TTL），非管理面写操作。
    if AUTH_ANY_USER_ALLOW.contains(&path) {
        agentos_http::auth::resolve_request_user(state.store.as_ref(), req.headers()).await?;
        return Ok(next.run(req).await);
    }

    let needs_auth = matches!(
        classify_auth_surface(path),
        Some(AuthSurfaceClass::Protected)
    );

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

/// 受保护面角色校验（原 db_routes::require_read_role /
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
/// 反射请求 Origin（开发友好，支持任意 localhost/自定义前端源）。凭据策略：
/// 内核认证走 Authorization Bearer 头（非 cookie），本地开发源不回
/// Allow-Credentials；显式配置的生产白名单源保留凭据放行。
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
    allowlist.contains(&origin)
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

/// 注入 CORS 响应头。仅当 origin 被放行时才反射 Origin；Allow-Credentials 只给
/// 显式配置的生产白名单源（`AGENTOS_CORS_ORIGINS`）——本地开发源反射但不带凭据头。
/// 内核认证走 Authorization Bearer 头（前端 apiClient 注入，不依赖 cookie），
/// 本地源去掉凭据头不影响任何合法调用，同时消除「反射任意 localhost 源 +
/// Allow-Credentials」被恶意本地页面带凭据跨域调用的面。其余头固定。
fn apply_cors_headers(headers: &mut HeaderMap, origin: Option<&str>) {
    if let Some(o) = origin {
        if is_origin_allowed(o) {
            if let Ok(v) = HeaderValue::from_str(o) {
                headers.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, v);
            }
            // 凭据放行仅限显式生产白名单（本地源反射不附带）。
            let explicit_allowlisted = std::env::var("AGENTOS_CORS_ORIGINS")
                .map(|raw| raw.split(',').map(str::trim).any(|e| e == o))
                .unwrap_or(false);
            if explicit_allowlisted {
                headers.insert(
                    header::ACCESS_CONTROL_ALLOW_CREDENTIALS,
                    HeaderValue::from_static("true"),
                );
            }
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
    // 握手凭证二选一：?ticket=（一次性票据，前端主路径）或 ?token=
    // （外部脚本 send.py 等既有消费方，兼容保留）。票据优先——单次消费语义
    // 下先消费票据；无效/过期票据与无效 token 同以 4001 拒绝。
    let auth = match params.get("ticket").cloned() {
        Some(ticket) => match state.ws_tickets.consume(&ticket) {
            Some((user_id, username)) => {
                agentos_session::auth::HandshakeAuth::Ok { user_id, username }
            }
            None => agentos_session::auth::HandshakeAuth::Rejected {
                code: 4001,
                reason: "WS 票据无效、已使用或已过期".to_string(),
            },
        },
        None => {
            let token = params.get("token").cloned();
            crate::ws_session::authenticate(token.as_deref())
        }
    };
    // B3：前端重连时上报 last_sequence（全局 watermark），用于首个 thread 注册时回放断线期间事件。
    let last_sequence = params
        .get("last_sequence")
        .and_then(|s| s.parse::<u64>().ok());
    let ws_store = state.store.clone();
    ws.on_upgrade(move |socket| {
        run_p2_ws_session_authed(socket, session, router, auth, last_sequence, ws_store)
    })
}

/// P2 内核化 WS 会话包装：握手鉴权 + 会话运行 + 拒绝时 accept+close。
async fn run_p2_ws_session_authed(
    socket: WebSocket,
    session: Arc<agentos_session::SessionCoordinator>,
    router: Arc<agentos_session::router::InboundRouter>,
    auth: agentos_session::auth::HandshakeAuth,
    last_sequence: Option<u64>,
    store: Option<std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
) {
    let mut user_id = None;
    let (code, reason) = crate::ws_session::run_ws_session_with_auth(
        socket,
        session,
        router,
        auth,
        &mut user_id,
        last_sequence,
        store,
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
/// 多租户 P0-4：从 Authorization token 解析（无 token / 无效 / 过期回退默认
/// 租户），注入到 [`agentos_tenant::scope`] 后，engine/store 通过 task_local 读取。
/// async：tenant 解析需查 store（持久化用户的一用户一租户映射）。
/// token 合法但用户解析失败（已删除/存储故障）→ 错误传播（401/503，
/// K8 修订 ADR 2026-10-10），不再降级 default——写面降级即写串租户。
pub(crate) async fn request_tenant_ctx(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    headers: &HeaderMap,
    session_id: &str,
) -> Result<TenantContext, agentos_http::error::ApiError> {
    let tenant_id = resolve_request_tenant_id(store, headers).await?;
    Ok(TenantContext::new(tenant_id, session_id))
}

// 通过 0.2 配置驱动管道引擎处理消息：构造 [`agentos_engine::PipelineExecutor`]，
// 热路径经 `load_and_compile_by_name` 现读盘上 config（管道配置 + 公共 step 库）
// 按 YAML 定义的 step 顺序执行（三级命中规则）；初始 state 构造、agent 配置与
// 工具面均归插件域（agent_config_load / tool_schema 经 tool-surface capability），
// 从最终 state 只认 `raw_result` 提取响应；AppState 缺 invoker / store /
// project_root（测试或老式构造）时走 echo-fallback。冷路径回放从 step 级轨迹
// 按序 merge 重建完整 state（轨迹颗粒度即恢复边界）。

/// 管道配置热重载的 mtime 检测 TTL：1 秒内不重复 stat，避免高频 chat 时反复解析 YAML。
const PIPELINE_CONFIG_TTL: Duration = Duration::from_secs(1);

/// 热重载检测的全局缓存：上次检测时刻 + 上次编译指纹（配置 mtime ⊕ manifests
/// 摘要，B6 双维键域）+ 上次编译产物（G10：指纹不变直接复用）。进程级单例，
/// 所有 chat 请求共享，避免反复 stat/解析/编译。
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

/// 计算管道编译缓存指纹：配置面 mtime（autonomous.yaml + config/steps/*.yaml）
/// ⊕ manifests 摘要（B6，编译输入第二维——步骤服务索引与已知插件集合均出自
/// manifests）。配置面用 mtime（stat 微秒级，热路径可接受）；声明面是内存
/// 集合，mtime 无法表达，用按 id 排序的内容序列化 hash——内容不变（纯代码
/// 变更）命中缓存，任何声明变更必失效。任一维变化 → 指纹变化 → 重编译。
fn compute_config_fingerprint(
    config_root: &std::path::Path,
    manifests: &[agentos_core::traits::PluginManifest],
) -> u64 {
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

    // autonomous.yaml（核心管道配置）——取生效落点（用户空间优先）：用户在管道
    // 配置页保存后指纹必须跟着变，否则热重载永远看不到用户改动。
    let pipeline_yaml =
        crate::pipeline_loader::resolve_pipeline_config_path(config_root, "autonomous.yaml");
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

    // manifests 摘要（声明维，B6）：按 id 排序消除集合序噪声，逐个序列化哈希。
    let mut sorted: Vec<&agentos_core::traits::PluginManifest> = manifests.iter().collect();
    sorted.sort_by(|a, b| a.id.cmp(&b.id));
    hasher.write(b"manifests:");
    for m in sorted {
        let json = serde_json::to_string(m).expect("manifest 序列化不可失败");
        hasher.write(json.as_bytes());
        hasher.write(b"\n");
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

/// Pull 热加载：按需重载并**编译**管道配置（G10 生产路径）。每次
/// `process_via_engine` 执行前调用，返回本次执行应使用的
/// [`CompiledPipeline`]（启动期编译的语义，运行时零解析）；`None` = 尚无一次
/// 成功编译——调用方显式报错拒绝空跑（空管道轮次零步"完成" = 零回复零错误帧
/// 的假成功）。策略（双层短路）：①TTL 门——距上次检测不足 1s 直接返回缓存
/// 产物；②指纹比对——过期后计算指纹（配置 mtime ⊕ manifests 摘要）与缓存
/// 比对，相同 → 缓存产物，不同 → 重载 + 编译（失败保留旧产物 + warn）。快照
/// 语义：在途 run 持调用时的 `Arc` 跑完，新 run 取新产物。自愈：编译对
/// (配置, manifests) 确定性——同指纹必同结果，失败态不占位缓存；修复到达
/// （提供方热注册/能力变更/配置修复）必改指纹，下个 TTL 窗口自动重编译。
async fn maybe_reload_compiled_pipeline(
    state: &AppState,
    config_root: &std::path::Path,
) -> Option<Arc<agentos_engine::compiler::CompiledPipeline>> {
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
        return cached;
    }

    // 指纹双维（配置 stat ⊕ manifests 快照），仅 TTL 过期路径执行。
    let manifests_snapshot = state.manifests.read().await.clone();
    let new_fp = compute_config_fingerprint(config_root, &manifests_snapshot);
    let (reload, cached_compiled) = {
        let mut cache = config_reload_cache().write();
        let (stale, compiled) = match cache.get(config_root) {
            None => (true, None),
            Some(c) => (c.last_fingerprint != new_fp, c.compiled.clone()),
        };
        // 无论是否重载都刷新检测时刻与指纹基线（修复到达必改指纹，见函数头"自愈"）。
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
        return cached_compiled;
    }

    // 指纹变化 → 重新加载 + 校验 + 编译
    //（G10：when AST / 引用 / 环在加载期暴露）
    info!("Pipeline config changed on disk, reloading + recompiling...");
    let manifests_snapshot = state.manifests.read().await.clone();
    let recompiled = load_and_compile(config_root, &manifests_snapshot);
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
            // 失败态不入缓存：编译对 (配置, manifests) 确定性——同指纹必同结果；
            // 修复到达必改指纹，下个 TTL 窗口自动重编译（双维同权驱动自愈）。
            return cached_compiled;
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
    Some(new_compiled)
}

/// 加载 + 校验 + 编译管道（供热重载与启动期 fail-fast 复用）：任一步失败返回
/// Err（含上下文）——启动期调用方据此 panic 拒绝启动，热重载调用方据此保留旧产物。
/// 步骤服务③级已接线（ADR 2026-10-08-step-service-level3-wiring）：本入口恒构
/// [`build_step_service_index`] 传入 with_hooks 装配——③级命中生产可达（执行死层
/// ①接线分支，ADR 2026-09-09-kernel-dead-layer-adjudication）。兼容映射保证存量
/// 零破坏：纯步骤插件隐式默认注册、显式条目 name==插件 id 映射 method None（与④
/// 直引等价）；hooks 沿传空（无声明需求，hooks 链仍按原裁定保留，yaml hooks 字段
/// 在 `to_internal` 丢弃不变）。
pub fn load_and_compile(
    config_root: &std::path::Path,
    manifests: &[agentos_core::traits::PluginManifest],
) -> Result<agentos_engine::compiler::CompiledPipeline, String> {
    load_and_compile_by_name(config_root, "autonomous", manifests)
}

/// 按名加载 + 校验 + 编译管道（[`load_and_compile`] 的通用形态）。
///
/// 错误信息始终携带管道配置名（调用方透传给请求方定位用了哪个配置）。
pub fn load_and_compile_by_name(
    config_root: &std::path::Path,
    name: &str,
    manifests: &[agentos_core::traits::PluginManifest],
) -> Result<agentos_engine::compiler::CompiledPipeline, String> {
    use agentos_engine::compiler::{build_step_service_index, compile_pipeline_with_checks};
    let plugin_ids: std::collections::HashSet<String> =
        manifests.iter().map(|m| m.id.clone()).collect();
    let index = build_step_service_index(manifests)
        .map_err(|e| format!("步骤服务索引构建失败: {}", e.message))?;
    let (pipeline, checks) = load_pipeline_with_checks(config_root, name)
        .map_err(|e| format!("加载管道配置 '{name}' 失败: {e}"))?;
    let steps = load_step_library(config_root).map_err(|e| format!("加载公共 step 库失败: {e}"))?;
    validate_no_name_conflicts(&pipeline, &steps, &plugin_ids)
        .map_err(|conflict| format!("管道配置 '{name}' 命名冲突: {conflict}"))?;
    // L2 嵌套不变量装载期校验（ADR 2026-11-05-timeout-taxonomy-and-merge §5.2）：
    // 外层声明 ≥ 内层声明，违反拦截（指名哪层包不住哪层）——矛盾在装载时炸
    // 而非运行 600s 后炸；动态面（LLM 工具面）由 L1 让位机制承载，不在此拦。
    crate::timeout_nesting::validate_timeout_nesting(&pipeline, &steps, manifests, &index)
        .map_err(|e| format!("管道配置 '{name}' 超时嵌套校验失败: {e}"))?;
    compile_pipeline_with_checks(
        &pipeline,
        &steps,
        &plugin_ids,
        Some(&index),
        &checks.body_checks,
        &checks.safety_locked_steps,
    )
    .map_err(|e| format!("管道配置 '{name}' 编译失败: {}", e.message))
}

// ═════════════════════════════════════════════════════════════════
// 管道编译缓存（PipelineCache）：显式指定的管道配置按需加载编译
// ═════════════════════════════════════════════════════════════════

/// 单个管道配置名的编译产物版本集（D9 版本钉住，批 H）。`versions` 按
/// config_hash 双键保留全部历史版本产物——PUT 热更不删旧条目（已钉实例按
/// (name, config_hash) 恒取出生版本）；`latest` = 当前盘面最新版的 config_hash，
/// PUT 热更后置 None（下次未钉取用按盘面重编译产新版本入集）。版本集合进程内
/// 只增不删：每次 PUT 至多新增一个版本，产物为配置规模的小结构（无 messages 等
/// 大字段），进程生命周期内不做淘汰——淘汰会让已钉实例被迫走盘面重编译路径，
/// 配置已改时误触 fail-closed（旧产物必须保留）。
struct PipelineVersionSet {
    latest: Option<String>,
    versions: HashMap<String, Arc<agentos_engine::compiler::CompiledPipeline>>,
}

impl PipelineVersionSet {
    fn empty() -> Self {
        Self {
            latest: None,
            versions: HashMap::new(),
        }
    }

    /// 收录一个编译产物；`as_latest` = 同时设为当前盘面最新版。同 hash 已有
    /// 产物时保持原 Arc（身份稳定：盘面改回旧内容后与既有实例共享同产物）。
    fn upsert_version(
        &mut self,
        compiled: Arc<agentos_engine::compiler::CompiledPipeline>,
        as_latest: bool,
    ) -> Arc<agentos_engine::compiler::CompiledPipeline> {
        if as_latest {
            self.latest = Some(compiled.config_hash.clone());
        }
        self.versions
            .entry(compiled.config_hash.clone())
            .or_insert(compiled)
            .clone()
    }
}

static PIPELINE_COMPILED_CACHE: OnceLock<ParkingRwLock<HashMap<String, PipelineVersionSet>>> =
    OnceLock::new();

fn pipeline_compiled_cache() -> &'static ParkingRwLock<HashMap<String, PipelineVersionSet>> {
    PIPELINE_COMPILED_CACHE.get_or_init(|| ParkingRwLock::new(HashMap::new()))
}

/// 编译产物缓存驻留快照（堆栈级诊断面 memory-breakdown 消费）：
/// (管道配置数, 版本产物总数)。版本集随配置热更只增（D9 钉住契约要求
/// 保活）——观测项，条目数为实测计数。
pub fn pipeline_compiled_cache_stats() -> (usize, usize) {
    let cache = pipeline_compiled_cache().read();
    (
        cache.len(),
        cache.values().map(|set| set.versions.len()).sum(),
    )
}

/// 启动期预入编译产物（autonomous 单例；现状行为的缓存面）：入版本集并设为
/// 当前最新版。
pub fn pipeline_cache_seed(name: &str, compiled: Arc<agentos_engine::compiler::CompiledPipeline>) {
    let mut cache = pipeline_compiled_cache().write();
    let set = cache
        .entry(name.to_string())
        .or_insert_with(PipelineVersionSet::empty);
    set.upsert_version(compiled, true);
}

/// PUT 管道配置热更写盘后调用：清该配置的"当前盘面最新版"指针——下次未钉
/// 取用按盘面重编译产新版本入集；**不删任何历史版本产物**（D9：已钉实例继续
/// 按 (name, config_hash) 取出生版本，热更只对之后出生的实例生效）。不存在的
/// 键幂等。
pub fn pipeline_cache_invalidate(name: &str) {
    if let Some(set) = pipeline_compiled_cache().write().get_mut(name) {
        if set.latest.take().is_some() {
            info!(pipeline_config = %name, "Pipeline latest version invalidated (config updated; historical versions kept)");
        }
    }
}

/// 管道编译产物选择收敛点（CompiledPipeline 唯一运行时取用面）。
///
/// - `config_id = None` → autonomous 单例（[`maybe_reload_compiled_pipeline`]
///   热重载路径）——默认会话不参与版本钉住（D9 = 显式绑定与任务派生管道）；
/// - `Some(id)` 且未钉（`pinned_hash = None`）→ 当前最新版：缓存 latest 命中
///   直接返回，未命中按盘面加载+编译+入集返回（调用方据返回产物的
///   `config_hash` 落钉 = 出生即钉）；
/// - `Some(id)` 且已钉 → 恒取钉住版本：缓存按 (name, hash) 命中直接返回；
///   未命中（进程重启后缓存已空）按盘面重编译并比对 hash——一致复用（正常
///   恢复），不一致 → `Err` fail-closed（含期望/当前盘面 hash，提示新开会话
///   或重派任务，不静默换版本）；
/// - 任一步失败 → `Err`（带管道名）——**不回落 autonomous**：用哪个管道就是
///   哪个，静默回落 = 用错管道。`pinned_hash` 仅对显式配置生效（`None` 缺省
///   路径无视它，由调用方 [`resolve_config_pin`] 保证不出现在缺省轮）。
pub(crate) async fn compiled_for(
    state: &AppState,
    config_root: &std::path::Path,
    config_id: Option<&str>,
    pinned_hash: Option<&str>,
) -> Result<Arc<agentos_engine::compiler::CompiledPipeline>, String> {
    let Some(name) = config_id.filter(|s| !s.is_empty()) else {
        // autonomous 缺省路径：无可用编译产物显式报错拒绝空跑（零步"完成" =
        // 零回复零错误帧的假成功）；Err 经既有失败面可见，自愈靠指纹双维——
        // 修复到达必改指纹，下个 TTL 窗口自动重编译。
        return maybe_reload_compiled_pipeline(state, config_root).await.ok_or_else(|| {
            "管道配置 'autonomous' 暂不可用：编译失败且无历史产物，拒绝空管道执行（修复配置或补齐步骤提供方插件后自动恢复）"
                .to_string()
        });
    };
    if let Some(expected) = pinned_hash {
        return compiled_for_pinned(state, config_root, name, expected).await;
    }
    // 未钉（出生轮）：当前最新版命中零重编译（磁盘直改不经 PUT 不生效的
    // 既有契约不变）。
    let latest_hit = pipeline_compiled_cache().read().get(name).and_then(|set| {
        set.latest
            .as_ref()
            .and_then(|hash| set.versions.get(hash).cloned())
    });
    if let Some(hit) = latest_hit {
        return Ok(hit);
    }
    let compiled = compile_explicit_config(state, config_root, name).await?;
    let mut cache = pipeline_compiled_cache().write();
    let set = cache
        .entry(name.to_string())
        .or_insert_with(PipelineVersionSet::empty);
    Ok(set.upsert_version(compiled, true))
}

/// 钉住取用：恒按 (name, expected) 版本取产物；缓存未命中（重启后）按盘面
/// 重编译比对——hash 一致复用（正常恢复），不一致 fail-closed。
async fn compiled_for_pinned(
    state: &AppState,
    config_root: &std::path::Path,
    name: &str,
    expected: &str,
) -> Result<Arc<agentos_engine::compiler::CompiledPipeline>, String> {
    let pinned_hit = pipeline_compiled_cache()
        .read()
        .get(name)
        .and_then(|set| set.versions.get(expected).cloned());
    if let Some(hit) = pinned_hit {
        return Ok(hit);
    }
    // 重启恢复：缓存无该版本，按盘面重编译比对（与 compiled_for 缺配置
    // "不回落 autonomous、用错管道不如报错"同精神——盘面已无同内容时
    // fail-closed，不静默换版本）。
    let path =
        crate::pipeline_loader::resolve_pipeline_config_path(config_root, &format!("{name}.yaml"));
    if !path.exists() {
        return Err(format!(
            "管道配置 '{name}' 已不存在（{}），实例钉住版本（config_hash={expected}）\
             不可恢复——请新开会话或重派任务",
            path.display()
        ));
    }
    let compiled = compile_explicit_config(state, config_root, name).await?;
    if compiled.config_hash != expected {
        return Err(format!(
            "管道配置 '{name}' 已被修改，钉住版本不可恢复：期望 config_hash={expected}，\
             当前盘面 config_hash={}——请新开会话或重派任务（不静默换版本）",
            compiled.config_hash
        ));
    }
    info!(
        pipeline_config = %name,
        config_hash = %expected,
        "Pinned pipeline version recovered from disk (hash matched)"
    );
    let mut cache = pipeline_compiled_cache().write();
    let set = cache
        .entry(name.to_string())
        .or_insert_with(PipelineVersionSet::empty);
    Ok(set.upsert_version(compiled, true))
}

/// 按名加载 + 编译一个显式配置（未钉/钉住恢复共用底座）。
///
/// 显式指定的配置必须存在：缺文件 = Err（不降级空管道、不回落 autonomous
/// ——静默跑错管道比报错更糟）。缺文件降级仅属 autonomous 启动加载
/// （load_pipeline_config 的缺省启动契约）。
async fn compile_explicit_config(
    state: &AppState,
    config_root: &std::path::Path,
    name: &str,
) -> Result<Arc<agentos_engine::compiler::CompiledPipeline>, String> {
    let path =
        crate::pipeline_loader::resolve_pipeline_config_path(config_root, &format!("{name}.yaml"));
    if !path.exists() {
        return Err(format!("管道配置 '{name}' 不存在（{}）", path.display()));
    }
    let manifests_snapshot = state.manifests.read().await.clone();
    let compiled = Arc::new(load_and_compile_by_name(
        config_root,
        name,
        &manifests_snapshot,
    )?);
    info!(pipeline_config = %name, "Pipeline config lazily loaded and compiled");
    Ok(compiled)
}

// ── D9 版本钉住（实例级）：出生即钉的读写面 ──────────────────────────
//
// 机制选择（免跨层传 hash）：不扩 modeBinding 快照/发送链帧，kernel 侧按
// pipeline_id（管道实例唯一坐标）钉——实例首次解析显式配置时把
// (name, config_hash) 落管道 state 持久键，后续轮次恢复合并回 initial_state
// 后恒按钉住版本取产物。前端/插件零改动。

/// 管道实例版本钉住的 state 持久键（D9 出生即钉，批 H）。点号键，与 task.id
/// 同款约定；两键恒成对写入（出生轮一次），消费面 = stage_execute 逐轮读取。
pub(crate) const PIPELINE_CONFIG_PIN_NAME_KEY: &str = "pipeline_config_pin.name";
pub(crate) const PIPELINE_CONFIG_PIN_HASH_KEY: &str = "pipeline_config_pin.config_hash";

/// 从恢复后的 initial_state 解析实例钉住记录（热路径 registry 快照 / 冷路径
/// pipeline_state 表，两路恢复均进 state 顶层键）。任一键缺席/非字符串 →
/// None（未钉：出生轮或默认会话）。
fn pinned_config_from_state(initial_state: &serde_json::Value) -> Option<(String, String)> {
    let name = initial_state
        .get(PIPELINE_CONFIG_PIN_NAME_KEY)?
        .as_str()?
        .to_string();
    let hash = initial_state
        .get(PIPELINE_CONFIG_PIN_HASH_KEY)?
        .as_str()?
        .to_string();
    Some((name, hash))
}

/// D9 钉住裁定（出生即钉）：
/// - 显式配置 + 已钉同名 → `Ok(Some(hash))`：恒用钉住版本；
/// - 显式配置 + 未钉 → `Ok(None)`：出生轮，调用方取到产物后落钉；
/// - 显式配置 + 钉住异名 → `Err`：实例出生即钉，中途换绑配置名 = fail-closed
///   （换配置请新开会话/重派任务，与内容层 fail-closed 同精神）；
/// - 无显式配置 → `Ok(None)`：默认会话 autonomous 热重载，不参与钉住。
fn resolve_config_pin(
    explicit: Option<&str>,
    pinned: Option<(&str, &str)>,
) -> Result<Option<String>, String> {
    let Some(name) = explicit else {
        return Ok(None);
    };
    match pinned {
        Some((pinned_name, pinned_hash)) if pinned_name == name => {
            Ok(Some(pinned_hash.to_string()))
        }
        Some((pinned_name, _)) => Err(format!(
            "管道实例已钉住配置 '{pinned_name}'，拒绝中途改绑 '{name}'\
             （出生即钉：换配置请新开会话或重派任务）"
        )),
        None => Ok(None),
    }
}

/// 引擎执行结果。
///
/// - `content`：纯文本回复（raw_result/message 提取），HTTP/回退路径直接消费；
/// - `final_assistant`：本轮最终 assistant 消息的完整持久形态（含
///   `tool_calls`/`reasoning_content`/`seq`），WS 路径 new_message 携带它，
///   前端用与 DB 加载相同的 mapper 生成 parts——流式事件与 DB 冷热同构；
/// - `failed`：executor.run 返回 Err（WS 路径据此推 stream_error 收尾）。
/// - `plugin_errors`：本轮管道执行中插件失败（result.error / invoker Err）的
///   收集（引擎 warn+继续的假成功显式化）——WS 路径发射 plugin_error 事件
///   弹前端通知；HTTP 路径无消费面（REST chat 同步返回，错误进日志）。
#[derive(Debug, Clone)]
pub(crate) struct EngineOutcome {
    pub content: String,
    pub final_assistant: Option<serde_json::Value>,
    pub failed: bool,
    /// 降级应答标记（前置依赖缺失 echo_fallback）：调用方据此与正常回复区分，
    /// 不再把降级应答当成功处理（2026-08-26 统一错误模型：假成功显式化）。
    pub degraded: bool,
    /// 插件执行错误收集（`[{plugin_id, code, message}]`，引擎 `_plugin_errors` 键提取）。
    pub plugin_errors: Vec<serde_json::Value>,
    /// 轮边界注入消费的队列条目 cmid（引擎 `_consumed_pending_cmids` 键提取，
    /// 设计真值 docs/working/待输入队列轮边界注入_方案_20261025.md）：这些条目
    /// 不经链级 drain 独立成轮，run 收尾时调用方对每个 cmid 通知 REST /chat
    /// 同步桥等待者（同一 outcome），防等待者挂到超时层。
    pub consumed_pending_cmids: Vec<String>,
}

impl EngineOutcome {
    /// 终止型出口信封：无 assistant 产出、非降级、零插件错误的失败/拒绝构造
    /// （前置校验拒绝、引擎 Err 防御、落库重试耗尽等出口共用，消除同形字面量）。
    fn terminated(content: impl Into<String>) -> Self {
        Self {
            content: content.into(),
            final_assistant: None,
            failed: true,
            degraded: false,
            plugin_errors: Vec::new(),
            consumed_pending_cmids: Vec::new(),
        }
    }
}

// 技术债（同 ROADMAP 已知技术债表 PLR091x 治理方式）：多参转发函数，
// 收尾时统一收敛参数结构体。
#[allow(clippy::too_many_arguments)]
pub(crate) async fn process_via_engine(
    state: &AppState,
    message: &str,
    agent_id: &str,
    pipeline_id: &str,
    thread_id: &str,
    message_id: &str,
    user_id: &str,
    thinking_strength: &str,
    execution_context: Option<&serde_json::Value>,
    state_overlay: Option<&serde_json::Value>,
    pipeline_config_id: Option<&str>,
    client_message_id: &str,
) -> EngineOutcome {
    // Box::pin 到堆上：回写段 + executor.run 的深 sidecar 调用链让 Future 状态机
    // 在 release 下也接近 tokio worker 2MB 栈极限，堆分配规避溢出。
    Box::pin(process_via_engine_inner(
        state,
        message,
        agent_id,
        pipeline_id,
        thread_id,
        message_id,
        user_id,
        thinking_strength,
        execution_context,
        state_overlay,
        pipeline_config_id,
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
    pipeline_id: &str,
    thread_id: &str,
    message_id: &str,
    user_id: &str,
    thinking_strength: &str,
    execution_context: Option<&serde_json::Value>,
    state_overlay: Option<&serde_json::Value>,
    pipeline_config_id: Option<&str>,
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

    // 0. 统一管道查询键：pipeline_id 是执行态唯一坐标，缺失即拒绝——会话
    //    thread_id 是组织集合 id，不得充当执行坐标（2026-08-30 用户裁定：
    //    thread 回退曾让消息/state 落进以 thread id 为键的幽灵管道）。
    let effective_pipeline_id = if pipeline_id.is_empty() {
        return EngineOutcome::terminated(
            "[error] 缺少 pipeline_id，拒绝派发（会话 id 不得充当管道执行坐标）",
        );
    } else {
        pipeline_id
    };
    // 缺失租户上下文回退 default 是契约内兜底（非错误路径）；debug 留痕供
    // 排查"消息落错租户"类问题时定位缺失点。
    let tenant = agentos_tenant::current().unwrap_or_else(|| {
        tracing::debug!("process_via_engine 无租户上下文，回退 default/kernel");
        TenantContext::new("default", "kernel")
    });
    let tenant_id = tenant.tenant_id.clone();

    // run_id 权威生成点：注入 initial_state（插件侧 llm_core 取消轮询的定位锚，
    // 见 聊天中断保留与重新生成回退方案 §四.1），并贯穿到 executor 构造——同一
    // run 的两个消费面（state 轮询锚 vs runs 表落库）共享一个 uuid。
    let run_id = uuid::Uuid::new_v4().to_string();

    // 1/1a/1a2. 初始 state 构造（含会话级/任务级 execution_context 注入）。
    let initial_state = stage_build_initial_state(
        state,
        &store,
        message,
        effective_pipeline_id,
        thread_id,
        message_id,
        user_id,
        thinking_strength,
        execution_context,
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
    // P1-4 声明化：收集全部插件 manifest `state.volatile_keys` 声明并集，
    // 与内核自有键同语义参与恢复合并跳过（checkpoint 落档剥离侧由
    // stage_execute 将同一并集注入 store 消费）。
    let declared_volatile: std::collections::HashSet<String> = state
        .manifests
        .read()
        .await
        .iter()
        .filter_map(|m| m.state.as_ref())
        .flat_map(|s| s.volatile_keys.iter().cloned())
        .collect();
    // B3：user 消息落库失败（重试耗尽）在此上抛终止本轮发送——不进入引擎执行，
    // failed outcome 经既有 WS 错误通道（run_pipeline_round → stream_error）回显。
    let initial_state = match stage_recover_history(
        initial_state,
        &store,
        message,
        effective_pipeline_id,
        &tenant_id,
        client_message_id,
        skip_user_append,
        state_overlay,
        &declared_volatile,
        &run_id,
    )
    .await
    {
        Ok(s) => s,
        Err(outcome) => return outcome,
    };

    // 直跑早回执（聊天消息流权威化设计真值 docs/working/聊天消息流权威化重设计_20261027.md
    // 不变量 1）：触发 user 消息此刻已带权威 seq/message_id 落库，即时回传认领
    // 载荷——前端气泡只认引擎确认（claimUserMessage 同一条认领链），亚秒级上屏
    // 替代乐观气泡。regenerate 重跑（skip_user_append）目标消息已在场，不发。
    if !skip_user_append && !client_message_id.is_empty() {
        if let Some(session) = state.session.as_ref() {
            if let Some(first) = initial_state
                .get("_pending_message_ops")
                .and_then(|v| v.as_array())
                .and_then(|a| a.first())
            {
                let payload = serde_json::json!({
                    "pipeline_id": effective_pipeline_id,
                    "thread_id": thread_id,
                    "client_message_id": client_message_id,
                    "record": {
                        "id": first.get("message_id").cloned().unwrap_or_default(),
                        "sequence": first.get("seq").cloned().unwrap_or(serde_json::json!(0)),
                        "content": message,
                    },
                    "_threadId": thread_id,
                });
                let _ = session
                    .emit_event(thread_id, "user_message_confirmed", payload)
                    .await;
            }
        }
    }

    // 2a. 实际状态轮中可见（2026-09-03 双状态裁定）：上一轮 final_state 若滞留
    //     registry，轮中可观测性（/pipelines/state）会读到陈旧终态——「显示已
    //     结束但还在输出」。dispatch 在引擎执行前用带 run_status=running 的
    //     initial_state 覆写 registry（热路径本就自 registry 恢复，内容等价、
    //     控制键已刷新）。条目不存在（管道首轮）跳过——stage_finalize 收尾补建。
    stage_refresh_registry_snapshot(&tenant_id, effective_pipeline_id, &initial_state);

    // 2/2b. agent 配置加载与工具面注入均归插件域：agent_config_load 按 state 持久键
    //    agent.id 加载 agent yaml 并写 state.tool_ids，tool_schema 插件经内核
    //    tool-surface capability 取过滤后 schema（prepare 链每轮刷新）。

    // 3. 构造 PipelineExecutor 并执行；失败（含 run 标记 Failed 的兜底）直接
    //    以失败 outcome 出口。
    let final_state = match stage_execute(
        state,
        invoker,
        project_root,
        tenant,
        store.clone(),
        initial_state,
        agent_id,
        message,
        &run_id,
        user_id,
        pipeline_config_id,
    )
    .await
    {
        Ok(s) => s,
        Err(outcome) => return outcome,
    };

    // 3b/4. registry 回写 + 响应提取。
    let outcome = stage_finalize(
        &final_state,
        Some(store.as_ref()),
        &tenant_id,
        effective_pipeline_id,
        thread_id,
        agent_id,
        &run_id,
    )
    .await;
    // GAP-2：run 终态域事件——内核只派生 run.* 终态事件（failed/suspended/
    // cancelled/completed，见 derive_run_terminal_events）；task_completed/
    // task_failed 由 task_service 插件订阅 run.* 后按任务域语义派生（ADR
    // 2026-08-28 事件下沉）。fire-and-forget，不影响响应。
    // 注：任务状态（task.status/ended_at）由任务域插件裁决写入（task_evaluate
    // 评估终态经 pipeline-state.update 落 state），内核只广播 run 终态事件，
    // 不写任务状态（职责边界：内核只管管道运行域，任务状态由任务域插件裁决）。
    emit_run_terminal_domain_events(state, &final_state, &run_id, outcome.failed).await;
    outcome
}

/// GAP-2：从 run 终态 state 派生域事件（run.* 终态 + 任务域派生）。
///
/// run 终态域事件派生（内核运行域）：仅运行生命周期事件，与 runs 落库
/// 共用同一终态映射单点（`RunStatus::from_control_state`，控制状态键契约
/// ADR 2026-08-30——落库 Failed 的 run 不得对外广播 run.completed）——
/// - `failed=true`（引擎执行失败）→ `run.failed`
/// - 映射 Suspended → `run.suspended`
/// - 映射 Cancelled → `run.cancelled`（用户停止/任务取消/删除署名）
/// - 映射 Failed（署名：超线/评估失败/重复循环等）→ `run.failed`
/// - 映射 Completed → `run.completed`
///
/// 任务域事件（task_completed/task_failed）由 task_service 插件订阅本组
/// run.* 事件后按任务域语义派生（ADR 2026-08-28 事件下沉：裁决词汇
/// task.status 值与 task.* 键面归插件，内核零知识）。
///
/// 载荷（ADR 2026-09-11）：终态事件随带该管道的 state 对象（`state` 标签），
/// 订阅方据此零回查派生任务域语义。内核只转发不挑选——不理解 `task.*`/
/// `lineage.*` 的语义，与零知识约束不冲突（约束的是不得**判定**任务域词汇，
/// 非不得**转发**插件域数据）。大字段经 [`terminal_event_state_payload`] 剥离。
fn derive_run_terminal_events(
    final_state: &serde_json::Value,
    failed: bool,
) -> Vec<(&'static str, Vec<(String, serde_json::Value)>)> {
    let pipeline_id = final_state
        .get("pipeline_id")
        .cloned()
        .unwrap_or(serde_json::Value::Null);
    let thread_id = final_state
        .get("session_id")
        .cloned()
        .unwrap_or(serde_json::Value::Null);
    let run_tags = vec![
        ("pipeline_id".to_string(), pipeline_id),
        ("thread_id".to_string(), thread_id),
        (
            "state".to_string(),
            terminal_event_state_payload(final_state),
        ),
    ];
    let mut events: Vec<(&'static str, Vec<(String, serde_json::Value)>)> = Vec::new();
    if failed {
        events.push(("run.failed", run_tags));
        return events;
    }
    let event = match agentos_core::types::RunStatus::from_control_state(final_state) {
        agentos_core::types::RunStatus::Suspended => "run.suspended",
        agentos_core::types::RunStatus::Cancelled => "run.cancelled",
        agentos_core::types::RunStatus::Failed => "run.failed",
        agentos_core::types::RunStatus::Completed | agentos_core::types::RunStatus::Running => {
            "run.completed"
        }
    };
    events.push((event, run_tags));
    events
}

/// 终态事件 state 载荷的剥离集：订阅方零消费的引擎/内核大字段。
///
/// 剔除项与理由（ADR 2026-09-11）：
/// - `messages`：全历史，单管道最有分量的常驻结构（事件载荷会到 MB 级）；
/// - `tool_schemas`：每轮 prepare 链重新拉取的 LLM 工具面，跨轮无意义
///   （与 `stage_finalize` 常驻快照剥离同款）；
/// - `raw_result` / `raw_thinking` / `raw_tool_calls`：引擎原始投影，
///   订阅方消费的是插件域键而非引擎通道键；
/// - `_` 前缀：引擎私有中间键（`_executed_tool_calls` 等）。
///
/// 实测剥离后终态标量载荷约 1.8 KB（最大单键 642 字节）。
const TERMINAL_EVENT_STATE_STRIP: &[&str] = &[
    "messages",
    "tool_schemas",
    "raw_result",
    "raw_thinking",
    "raw_tool_calls",
];

/// 构造终态事件的 state 载荷（剥离大字段后的对象；非对象输入返回空对象）。
fn terminal_event_state_payload(final_state: &serde_json::Value) -> serde_json::Value {
    let Some(obj) = final_state.as_object() else {
        return serde_json::Value::Object(serde_json::Map::new());
    };
    let mut out = serde_json::Map::with_capacity(obj.len());
    for (k, v) in obj {
        if TERMINAL_EVENT_STATE_STRIP.contains(&k.as_str()) || k.starts_with('_') {
            continue;
        }
        out.insert(k.clone(), v.clone());
    }
    serde_json::Value::Object(out)
}

/// 广播 run 终态域事件（GAP-2：fire-and-forget，不阻塞引擎出口）。
///
/// run.failed 额外镜像一帧 `run_failed` 推前端（ADR 2026-09-28-run-failure-
/// frontend-notification）：域事件只达总线/订阅插件，前端不知情——署名失败
/// 路径（stop_reason 命中 FAILED_STOP_REASONS，DSL 正常收束 failed=false）与
/// 任务/注入管道的引擎失败此前对前端完全静默。completed/suspended/cancelled
/// 不镜像（每轮一帧 completed 是噪音，挂起/取消有既有通道）。
async fn emit_run_terminal_domain_events(
    state: &AppState,
    terminal_state: &serde_json::Value,
    run_id: &str,
    failed: bool,
) {
    let events = derive_run_terminal_events(terminal_state, failed);
    let run_failed = events.iter().any(|(name, _)| *name == "run.failed");
    for (name, tags) in events {
        crate::plugin_lifecycle::broadcast_domain_event(state, name, tags).await;
    }
    if run_failed {
        if let Some(session) = state.session.as_ref() {
            emit_run_failed_frontend_event(session, terminal_state, run_id).await;
        }
    }
    // 刀3（2026-09-09 内存驻留归因）：run 终态（非 suspended）注销该管道的
    // 热路径 state 缓存——final_state（含 messages 全历史）是单管道最有分量
    // 的常驻结构，且 remove() 此前在生产零调用，已终态管道的状态跨轮永久
    // 驻留（4788 条 pipeline_state 的内存镜像）。热缓存只为续跑服务：下轮
    // 冷启动经 persist_run_end 收尾 checkpoint + traces/message_slots 重建
    // （server.rs 冷路径，见 get_or_init 未命中分支）。suspended（停泊/
    // 审批挂起）保留热态，恢复路径零重建。
    let status = agentos_core::types::RunStatus::from_control_state(terminal_state);
    if status != agentos_core::types::RunStatus::Suspended {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let pipeline_id = terminal_state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if !pipeline_id.is_empty() {
            // 注销热缓存前把 run_status 落表（内核自有键，观测真值）：checkpoint
            // 落档按 VOLATILE 剥离 run_status，注销后内存副本也无——list 视图的
            // checkpoint 兜底行将永远读不到终态（评测判稳/前端观测断链，2026-09-10
            // 压测实证）。表行单字段 upsert，随兜底行合并出口；下轮起点注入
            // running 覆盖，无复活风险。
            let terminal_fields = terminal_run_fields(status, terminal_state);
            if let Some(store) = state.store.as_ref() {
                if let Err(e) = store
                    .upsert_state_fields(pipeline_id, &tenant_id, &terminal_fields)
                    .await
                {
                    warn!(
                        pipeline_id = %pipeline_id,
                        error = %e,
                        "终态 run_status/run_stop_reason 落表失败（观测兜底缺失，不影响执行）"
                    );
                }
            }
            agentos_session::pipeline_state_registry::global_registry()
                .remove(&tenant_id, pipeline_id);
        }
    }
}

/// run 收尾落表字段：`run_status` + `run_stop_reason`（同点、同生命周期、同事务）。
///
/// `run_status` 是内核持有的实际状态，checkpoint 落档按 VOLATILE 剥离，注销
/// 热缓存前必须旁路落表，否则 list 视图的 checkpoint 兜底行永远读不到终态。
///
/// `run_stop_reason` 与之同点：它是**失败归因的唯一真值载体**。`router.stop_reason`
/// 按设计在 `VOLATILE_RUN_KEYS` 内（防跨 run 残留，刻意不随 checkpoint 落档），
/// 代价是终态真因只活在 WS 事件与日志——插件侧对账面读不到，只能写死猜测文案
/// （事故 2026-10-17：墙钟超时被报成"启动清扫标记的进程异常终止"，与当日事实
/// 无关，把排查方向带偏）。
///
/// 未署名（键缺席 / 非字符串 / 空串）一律落**空串**而非跳过：每轮终态都写，
/// 杜绝上一轮署名残留被下一轮误读为本次归因。
fn terminal_run_fields(
    status: agentos_core::types::RunStatus,
    terminal_state: &serde_json::Value,
) -> serde_json::Map<String, serde_json::Value> {
    let stop_reason = terminal_state
        .get("router.stop_reason")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let mut fields = serde_json::Map::new();
    fields.insert("run_status".to_string(), serde_json::json!(status));
    fields.insert(
        "run_stop_reason".to_string(),
        serde_json::json!(stop_reason),
    );
    fields
}

/// run.failed 前端镜像帧：`run_failed` WS 事件（streaming.json 契约条目）。
///
/// 载荷坐标从终态 state 提取（session_id/pipeline_id 出生契约键；引擎 Err
/// 防御路径用预捕获的初始 state 剥离版，同样携带）。stop_reason 原值透传
/// （FAILED_STOP_REASONS 命中词；引擎 Err 无署名为 null）——内核只转发不
/// 解释。run_id 是前端通知幂等键（emit_event 进 thread 级重放缓冲，重放
/// 不重复弹卡）。会话坐标缺失 → 丢弃（无 WS 投递目标；域事件照发）。
///
/// `error`：首条插件错误原文（全量不截断，ADR 2026-10-27-pipeline-error-
/// notification-channel 错误透传裁定）。折叠失败轮（步失败零产出）stop_reason
/// 恒空串，通知卡此前只剩「管道异常退出」无归因——真实原因（限流/连接/
/// 认证等）随本字段出口，前端失败卡直接渲染。无插件错误的失败轮为 null。
async fn emit_run_failed_frontend_event(
    session: &agentos_session::SessionCoordinator,
    terminal_state: &serde_json::Value,
    run_id: &str,
) {
    let thread_id = terminal_state
        .get("session_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let pipeline_id = terminal_state
        .get("pipeline_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if thread_id.is_empty() || pipeline_id.is_empty() {
        return;
    }
    let first_plugin_error = terminal_state
        .get("_plugin_errors")
        .and_then(|v| v.as_array())
        .and_then(|errs| {
            errs.iter().find_map(|e| {
                let msg = e.get("message")?.as_str()?.trim();
                (!msg.is_empty()).then(|| msg.to_string())
            })
        });
    let _ = session
        .emit_event(
            thread_id,
            "run_failed",
            serde_json::json!({
                "pipeline_id": pipeline_id,
                "_threadId": thread_id,
                "status": "failed",
                "stop_reason": terminal_state.get("router.stop_reason")
                    .cloned()
                    .unwrap_or(serde_json::Value::Null),
                "error": first_plugin_error,
                "run_id": run_id,
            }),
        )
        .await;
}

/// 前置依赖缺失时的 echo 降级应答（invoker/store/project_root 任一缺席）。
/// degraded 标记供调用方（REST chat / 插件 send_message）识别降级应答，
/// 与正常回复区分（2026-08-26 统一错误模型：假成功显式化）。
fn echo_fallback(missing: &str, message: &str) -> EngineOutcome {
    EngineOutcome {
        content: format!("[echo-fallback: {missing}] {message}"),
        final_assistant: None,
        failed: false,
        degraded: true,
        plugin_errors: Vec::new(),
        consumed_pending_cmids: Vec::new(),
    }
}

/// 按 `a.b.c` 点号路径把值写入嵌套对象（中间节点非对象时覆盖为对象）。
/// 声明驱动 execution_context 组装的路径写入器（路径来自插件
/// thread_fields 的 x_execution_path 声明，内核不预知键面）。
fn set_execution_context_path(
    ec: &mut serde_json::Map<String, serde_json::Value>,
    path: &str,
    value: serde_json::Value,
) {
    let parts: Vec<&str> = path.split('.').collect();
    let mut cur = ec;
    for part in &parts[..parts.len() - 1] {
        let entry = cur
            .entry(part.to_string())
            .or_insert_with(|| serde_json::Value::Object(serde_json::Map::new()));
        if !entry.is_object() {
            *entry = serde_json::Value::Object(serde_json::Map::new());
        }
        cur = entry.as_object_mut().expect("中间节点已确保为对象");
    }
    cur.insert(parts[parts.len() - 1].to_string(), value);
}

/// 消息级 execution_context 逐键并入 base（1a2 合并点，消息级显式键优先）：
/// 两侧同键皆对象时按子键合并（消息级叶子覆盖，base 未携带子键保留），
/// 其余形态消息级值整体落位。execution_context 形态固定为两层对象
/// （mode 顶层标量 / isolation、workspace 为单层对象），两层足够。
fn merge_message_execution_context(base: &mut serde_json::Value, message: &serde_json::Value) {
    let Some(msg_obj) = message.as_object() else {
        return;
    };
    if !base.is_object() {
        *base = message.clone();
        return;
    }
    let base_obj = base.as_object_mut().expect("base 已确保为对象");
    for (k, mv) in msg_obj {
        match (base_obj.get_mut(k), mv) {
            (Some(serde_json::Value::Object(bm)), serde_json::Value::Object(mm)) => {
                for (bk, bv) in mm {
                    bm.insert(bk.clone(), bv.clone());
                }
            }
            _ => {
                base_obj.insert(k.clone(), mv.clone());
            }
        }
    }
}

/// 阶段 1/1a/1a2：构造初始 state（含会话级/任务级 execution_context 注入）。
///
/// core 器官执行态缺省（core_type 语义标志 + core_plugin 插件选择）不在本函数
/// 种入——P1-1 声明化后由管道配置 `initial_state:` 声明、stage_execute
/// 消费编译产物补种（内核零插件 id 知识）；agent 配置加载已移出内核
/// （内核不读 config/agents/**，语义由管道输入插件 agent_config_load 按执行上下文
/// 键自持），管道配置步骤经 state 写面按步覆盖 core_type/core_plugin
/// （config/pipelines/autonomous.yaml）。自由 state overlay 在恢复合并之后
/// 应用（stage_recover_history）。
// 技术债（同上）：多参阶段函数，收尾时统一收敛参数结构体。
#[allow(clippy::too_many_arguments)]
async fn stage_build_initial_state(
    state: &AppState,
    store: &Arc<dyn StorageBackend>,
    message: &str,
    effective_pipeline_id: &str,
    thread_id: &str,
    message_id: &str,
    user_id: &str,
    thinking_strength: &str,
    execution_context: Option<&serde_json::Value>,
    run_id: &str,
) -> serde_json::Value {
    let mut initial_state = serde_json::json!({
        "message": message,
        "input": message,
        // 执行身份不是 per-run 注入键：agent 身份是管道 state 持久键 `agent.id`
        // （出生方经 chat.send_message state 透传落库），随本函数之后的热恢复
        // （registry）/冷恢复（pipeline_state 表）进入 state，供 agent_config_load
        // 等插件消费面读取——缺省主 agent 由消费面自持，派发不再携带/注入身份。
        "ended": false,
        "suspended": false,
        // 实际状态（内核持有，2026-09-03 双状态裁定）：run 生命周期唯一真值，
        // 起点恒为 running；收束由引擎经终态映射单点改写。插件对该键的写入在
        // merge 处拦截、overlay 写入被保留字拦截，恢复合并经 VOLATILE_RUN_KEYS
        // 剥离——上轮终态不会复活成本轮起点。
        "run_status": "running",
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
        // 经 chat.send_message 回派发需要 user_id 解析 tenant）。tool_args_inject 据
        // 此注入到工具 args；空串时 tool_args_inject 自动跳过注入，不影响既有行为。
        "user_id": user_id,
        // assistant message_id：内核权威生成，sidecar 流式 chunk 携带它，
        // 前端 handleStreamChunk 据此把 chunk 路由到 stream_start 建立的占位气泡。
        "message_id": message_id,
        // 思考强度（off/low/medium/high；空串=未指定）：透传给 llm_core，
        // 由插件在请求构造时路由到具体模型参数（temperature/max_tokens/reasoning_effort）。
        "thinking_strength": thinking_strength,
    });

    // 1a. 会话级 execution_context 注入（声明驱动，ADR 2026-08-28）：遍历启用
    // 插件 contributes.thread_fields 的 x_metadata_key → x_execution_path 声明，
    // 把 thread metadata（会话创建时由前端按声明写入）的值按声明路径写入
    // execution_context。内核不认识 workspace/isolation 等具体键（归属
    // workspace_lifecycle / isolation 插件），缺省值同样归插件执行期兜底
    // （workspace_lifecycle：mode 未指定 → plain）——与前端表单组装
    // （SessionEditModal 按 x_execution_path 组装消息级上下文）同构。
    //
    // 任务级 execution_context（task_submit 提交）经消息级参数透传，整体覆盖
    // 会话级来源（1a2），优先级不变。
    match store.get_session(thread_id).await {
        Ok(Some(sess)) => {
            if let Some(meta) = sess.metadata {
                let enabled = state.enabled_plugin_ids.read().await.clone();
                let mut ec = serde_json::Map::new();
                for manifest in state.manifests.read().await.iter() {
                    if !enabled.contains(&manifest.id) {
                        continue;
                    }
                    let Some(fields) = manifest
                        .contributes
                        .as_ref()
                        .and_then(|c| c.get("thread_fields"))
                        .and_then(|v| v.as_array())
                    else {
                        continue;
                    };
                    for field in fields {
                        let (Some(meta_key), Some(path)) = (
                            field.get("x_metadata_key").and_then(|v| v.as_str()),
                            field.get("x_execution_path").and_then(|v| v.as_str()),
                        ) else {
                            continue;
                        };
                        // 只搬运非空字符串值（空值 = 未设置，默认语义归插件）
                        let Some(val) = meta
                            .get(meta_key)
                            .and_then(|v| v.as_str())
                            .filter(|s| !s.is_empty())
                        else {
                            continue;
                        };
                        set_execution_context_path(&mut ec, path, serde_json::json!(val));
                    }
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

    // 1a2. 消息级 execution_context 逐键并入（经 chat.send_message params 透传，
    // 任务执行器从 task.metadata 组装走同一通道）：消息级显式键 > 会话级同键，
    // 消息级未携带的键保留会话级种子（声明驱动兜底，整体替换会把会话级其余
    // 键丢掉）。非对象/空对象不注入。
    if let Some(ec) = execution_context {
        if ec.is_object() && !ec.as_object().map(|o| o.is_empty()).unwrap_or(true) {
            let base = initial_state
                .as_object_mut()
                .expect("initial_state 恒为对象")
                .entry("execution_context".to_string())
                .or_insert_with(|| serde_json::Value::Object(serde_json::Map::new()));
            merge_message_execution_context(base, ec);
        }
    }

    // 1a3（自由 state overlay）已移至 stage_recover_history 恢复合并之后应用
    // （B13① 域界定，ADR 2026-09-06）：恢复基线（registry 快照/checkpoint/
    // pipeline_state 表）合并会覆盖写已存在键，overlay 先应用会被残留值回写
    // 污染——显式派发指令必须优先于持久化基线。

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

/// 把恢复源（热路径 = registry 快照，冷路径 = checkpoint/traces/表重建）的标量键
/// 并入 initial_state 顶层扁平键，两条路径共用同一合并语义。
///
/// 跳过三类键：`messages`（队列真值由热/冷路径独立装配，不在此恢复）；内部
/// 下划线键、内核自有 per-run 键 [`agentos_engine::VOLATILE_RUN_KEYS`] 与插件
/// `state.volatile_keys` 声明键（per-run 指令与锚点/终止标志——快照残留会顶掉
/// 本轮新输入，旧 run_id 会顶掉取消轮询新锚，GAP-3 与批次 D 重跑标志同源）。
fn merge_recovered_scalars(
    initial_state: &mut serde_json::Value,
    source: &serde_json::Value,
    declared_volatile: &std::collections::HashSet<String>,
) {
    let Some(src_obj) = source.as_object() else {
        return;
    };
    let Some(init_obj) = initial_state.as_object_mut() else {
        return;
    };
    for (k, v) in src_obj {
        if k == "messages" || k.starts_with('_') {
            continue;
        }
        if agentos_engine::VOLATILE_RUN_KEYS.contains(&k.as_str()) || declared_volatile.contains(k)
        {
            continue;
        }
        // execution_context：每轮声明组装键（1a 会话级 + 1a2 消息级在本函数
        // 之前先行合并），恢复源残留是历史轮组装产物——本轮已注入（键存在）
        // 则跳过，防上轮值顶掉本轮显式声明（消息级显式键 > 会话级默认 >
        // 恢复兜底，mode/isolation/workspace 全键同序）；本轮两级皆无时照常
        // 恢复（续跑连续性兜底：无透传轮沿用上轮执行上下文）。
        if k == "execution_context" && init_obj.contains_key(k) {
            continue;
        }
        init_obj.insert(k.clone(), v.clone());
    }
}

#[cfg(test)]
mod merge_recovered_scalars_tests {
    use super::*;

    /// 2026-09-02 裁定：新消息到达 = 用户已响应交互，新一轮应从非对话态开始
    /// 直接运行——上轮挂起轮的对话/执行态（conversation_mode/core_type/
    /// core_plugin 及终止标志）不得经恢复合并复活。P1-4 后插件语义键经
    /// `state.volatile_keys` 声明进 declared_volatile（模拟 llm_core/human
    /// 插件声明的并集），内核自有键仍走 VOLATILE_RUN_KEYS。
    #[test]
    fn skips_volatile_run_keys_including_conversation_state() {
        let declared: std::collections::HashSet<String> = [
            "thinking_strength",
            "tool_schemas",
            "conversation_mode",
            "core_type",
            "core_plugin",
        ]
        .into_iter()
        .map(String::from)
        .collect();
        let mut initial = serde_json::json!({
            "message": "新消息",
            "run_id": "new-run",
            "suspended": false,
            "ended": false,
            "run_status": "running",
            "core_type": "llm_call",
            "core_plugin": "pipeline_llm_core",
        });
        // 恢复源 = 上一轮 final_state（交互挂起轮）：对话态 + 执行态 + 终止标志残留
        let source = serde_json::json!({
            "task.id": "t1",
            "workspace": "/ws",
            "conversation_mode": true,
            "core_type": "tool_execute",
            "core_plugin": "pipeline_tool_core",
            "suspended": true,
            "ended": true,
            "run_status": "completed",
            "run_id": "old-run",
        });
        merge_recovered_scalars(&mut initial, &source, &declared);
        // 持久键照常恢复
        assert_eq!(initial["task.id"], "t1");
        assert_eq!(initial["workspace"], "/ws");
        // 本轮执行态不被上轮覆盖（新 run 从 llm_call 开始；空转根因回归断言）
        assert!(
            initial.get("conversation_mode").is_none(),
            "对话等待态不得跨轮复活"
        );
        assert_eq!(initial["core_type"], "llm_call");
        assert_eq!(initial["core_plugin"], "pipeline_llm_core");
        // 终止标志、实际状态与新 run_id 保持本轮初始值
        assert_eq!(initial["suspended"], false);
        assert_eq!(initial["ended"], false);
        assert_eq!(initial["run_status"], "running");
        assert_eq!(initial["run_id"], "new-run");
    }

    /// 非易变标量照常合并（合并语义不破坏续跑不丢键）。
    #[test]
    fn merges_persistent_scalars() {
        let mut initial = serde_json::json!({"message": "m"});
        let source = serde_json::json!({"task.id": "t9", "track.total_tokens": 42});
        merge_recovered_scalars(&mut initial, &source, &Default::default());
        assert_eq!(initial["task.id"], "t9");
        assert_eq!(initial["track.total_tokens"], 42);
    }

    /// P1-4 声明化：恢复源残留的插件声明键同样不得复活（合并跳过声明键），
    /// 未声明键照常合并——跳过范围随声明增减。
    #[test]
    fn skips_declared_volatile_keys_from_plugins() {
        let declared: std::collections::HashSet<String> =
            ["tool_schemas"].into_iter().map(String::from).collect();
        let mut initial = serde_json::json!({"message": "m"});
        let source = serde_json::json!({"tool_schemas": [{"name": "x"}], "task.id": "t2"});
        merge_recovered_scalars(&mut initial, &source, &declared);
        assert!(
            initial.get("tool_schemas").is_none(),
            "声明键属 per-run 工具面，跨轮残留 = schema 滞留"
        );
        assert_eq!(initial["task.id"], "t2");
    }

    /// execution_context 是每轮声明组装键（1a 会话级 + 1a2 消息级先行注入），
    /// 恢复源残留是历史轮组装产物——本轮已注入时不得被快照整键顶掉
    /// （消息级显式键 > 会话级默认 > 恢复兜底，mode/isolation/workspace 全键同序）。
    #[test]
    fn execution_context_this_round_declaration_beats_recovered_snapshot() {
        let mut initial = serde_json::json!({
            "message": "m",
            "execution_context": {"mode": "coding"},
        });
        let source = serde_json::json!({
            "task.id": "t3",
            "execution_context": {"mode": "research"},
        });
        merge_recovered_scalars(&mut initial, &source, &Default::default());
        assert_eq!(
            initial["execution_context"]["mode"], "coding",
            "本轮显式 mode 不得被上轮残留覆盖"
        );
        assert_eq!(initial["task.id"], "t3", "其余持久键照常恢复");
    }

    /// 同序反例：本轮会话级种子也优先于恢复源（作用域是整键，不限消息级）。
    #[test]
    fn execution_context_session_seed_beats_recovered_snapshot() {
        let mut initial = serde_json::json!({
            "message": "m",
            "execution_context": {"workspace": {"source_path": "会话级目录"}},
        });
        let source = serde_json::json!({
            "execution_context": {
                "workspace": {"source_path": "项目目录", "mode": "worktree"},
                "isolation": {"level": "isolated"},
            },
        });
        merge_recovered_scalars(&mut initial, &source, &Default::default());
        assert_eq!(
            initial["execution_context"],
            serde_json::json!({"workspace": {"source_path": "会话级目录"}}),
            "本轮会话级种子原样保留，恢复源不得整键覆写"
        );
    }

    /// 本轮两级皆无（1a 无声明值且消息未携带）：恢复值照常生效——续跑
    /// 连续性兜底（无透传轮沿用上轮执行上下文，防工作区漂移）。
    #[test]
    fn execution_context_recovered_when_round_has_no_declaration() {
        let mut initial = serde_json::json!({"message": "m"});
        let source = serde_json::json!({
            "execution_context": {"isolation": {"level": "isolated"}},
        });
        merge_recovered_scalars(&mut initial, &source, &Default::default());
        assert_eq!(
            initial["execution_context"]["isolation"]["level"], "isolated",
            "本轮无声明时恢复值兜底生效"
        );
    }
}

#[cfg(test)]
mod stage_refresh_registry_snapshot_tests {
    use super::*;

    /// 实际状态轮中可见（2026-09-03 双状态裁定）：上一轮 final_state（ended=true
    /// / 上轮阶段）滞留 registry 时，dispatch 启动前刷新为带 run_status=running
    /// 的本轮 initial——轮中可观测性不得读到陈旧终态（「显示已结束但还在输出」
    /// 的显示侧根因）。
    #[test]
    fn overwrites_stale_terminal_snapshot_before_run() {
        let reg = agentos_session::pipeline_state_registry::global_registry();
        let tenant = "tenant_refresh_test";
        let pid = "pipe_refresh_test";
        let stale = serde_json::json!({
            "run_status": "completed",
            "ended": true,
            "current_phase": "exit",
            "messages": [{"role": "assistant", "content": "old"}],
        });
        if reg.contains(tenant, pid) {
            reg.update_state(tenant, pid, stale);
        } else {
            reg.get_or_init(tenant, pid, "thread_refresh_test", "agentos", stale);
        }

        let fresh = serde_json::json!({
            "run_status": "running",
            "ended": false,
            "message": "新消息",
            "messages": [{"role": "user", "content": "新消息"}],
        });
        stage_refresh_registry_snapshot(tenant, pid, &fresh);

        let entry = reg.get(tenant, pid).expect("registry 条目存在");
        let e = entry.read();
        assert_eq!(e.state["run_status"], "running");
        assert_eq!(e.state["ended"], false);
        assert!(
            e.state.get("current_phase").is_none(),
            "上轮阶段残留不得跨轮滞留快照"
        );
        assert_eq!(
            e.state["messages"],
            serde_json::json!([{"role": "user", "content": "新消息"}]),
            "快照整体覆写为本轮 initial（热路径本就自 registry 恢复）"
        );
    }

    /// 条目不存在的管道（首轮/重启后首跑）：刷新为 no-op，不创建条目——
    /// 收尾 stage_finalize 的 get_or_init 负责补建。
    #[test]
    fn missing_entry_is_noop() {
        let reg = agentos_session::pipeline_state_registry::global_registry();
        let tenant = "tenant_refresh_absent";
        let pid = "pipe_refresh_absent";
        stage_refresh_registry_snapshot(tenant, pid, &serde_json::json!({"run_status": "running"}));
        assert!(!reg.contains(tenant, pid), "不得为不存在的条目抢先建项");
    }
}

#[cfg(test)]
mod stage_finalize_residency_tests {
    use super::*;

    /// 常驻快照剥离 `_` 前缀引擎私有 staging 键（ADR 2026-09-29-trace-mirror-
    /// dedup）：`_full_tool_results` 等是 per-run 中间态（同 run 内被
    /// tool_cache_writer 消费，跨轮零消费——恢复链 merge_recovered_scalars 跳过
    /// `_` 键），不得随 final_state 滞留内存注册表。messages 驻留是设计正确
    /// （队列跨轮延续），不动；tool_schemas 剥离是既有契约。
    #[tokio::test]
    async fn residency_snapshot_strips_underscore_staging_keys_and_keeps_messages() {
        let reg = agentos_session::pipeline_state_registry::global_registry();
        let tenant = "tenant_finalize_us";
        let pid = "pipe_finalize_us";
        let final_state = serde_json::json!({
            "pipeline_id": pid,
            "run_status": "completed",
            "task.status": "done",
            "messages": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ],
            "_full_tool_results": [{"tool_name": "bash_execute", "data": "X".repeat(256)}],
            "_executed_tool_calls": [{"name": "bash_execute"}],
            "_plugin_errors": [],
            "tool_schemas": {"bash_execute": {"type": "object"}},
        });
        let _ = stage_finalize(
            &final_state,
            None,
            tenant,
            pid,
            "thread_fu",
            "agentos",
            "run_fu",
        )
        .await;

        let entry = reg.get(tenant, pid).expect("收尾应补建条目");
        let e = entry.read();
        assert!(
            e.state.get("_full_tool_results").is_none(),
            "全文 staging 键不得驻留"
        );
        assert!(
            e.state.get("_executed_tool_calls").is_none(),
            "执行记录 staging 键不得驻留"
        );
        assert!(
            e.state.get("_plugin_errors").is_none(),
            "引擎私有键不得驻留"
        );
        assert!(
            e.state.get("tool_schemas").is_none(),
            "tool_schemas 剥离是既有契约"
        );
        assert_eq!(e.state["task.status"], "done", "合法标量保留");
        assert_eq!(
            e.state["messages"].as_array().map(Vec::len),
            Some(2),
            "messages 队列驻留是设计正确，不动"
        );
        reg.remove(tenant, pid);
    }
}

#[cfg(test)]
mod apply_state_overlay_tests {
    use super::*;

    /// 实际状态内核持有（2026-09-03 双状态裁定）：overlay（chat.send_message
    /// state 参数）命中保留字 run_status 一律跳过——实际状态只出自引擎折算。
    #[test]
    fn overlay_cannot_write_kernel_held_run_status() {
        let mut initial = serde_json::json!({"run_status": "running", "pipeline_id": "p1"});
        let overlay = serde_json::json!({
            "run_status": "failed",
            "task.goal": "合法注入",
        });
        apply_state_overlay(&mut initial, &overlay);
        assert_eq!(initial["run_status"], "running", "保留字拦截 overlay 直写");
        assert_eq!(initial["task.goal"], "合法注入", "非保留键照常并入");
    }

    /// agent.id 消息级覆盖（WS agent_id 透传 2026-09-25）：overlay 在恢复合并
    /// 之后应用——帧显式携带的执行身份覆盖既有持久键（每条消息可切换人格），
    /// 原本无该键时照常写入（管道后续轮次沿用最后写入值）。
    #[test]
    fn overlay_agent_id_overrides_recovered_identity() {
        // 既有身份（热/冷恢复写入 initial_state 后的形态）被消息级覆盖。
        let mut initial = serde_json::json!({"agent.id": "old_persona", "run_status": "running"});
        apply_state_overlay(
            &mut initial,
            &serde_json::json!({"agent.id": "new_persona"}),
        );
        assert_eq!(
            initial["agent.id"], "new_persona",
            "消息级显式身份覆盖恢复基线"
        );
        // 管道原本无身份键（会话类管道）→ 写入生效。
        let mut fresh = serde_json::json!({"run_status": "running"});
        apply_state_overlay(&mut fresh, &serde_json::json!({"agent.id": "persona_a"}));
        assert_eq!(fresh["agent.id"], "persona_a");
    }

    /// 通道守卫：agent.id 不得进入 overlay 保留字与 per-run 挥发键——否则
    /// WS agent_id 透传（router 合成单键 overlay）与 task_birth 出生键会被
    /// 静默拦截，执行身份通道断裂（回归锚 2026-09-25）。
    #[test]
    fn agent_dot_id_is_neither_reserved_nor_volatile() {
        assert!(
            !crate::kernel_capabilities::RESERVED_STATE_KEYS.contains(&"agent.id"),
            "agent.id 是身份持久键，不得列为 overlay 保留字"
        );
        assert!(
            !agentos_engine::VOLATILE_RUN_KEYS.contains(&"agent.id"),
            "agent.id 跨轮沿用，不得列为 per-run 挥发键"
        );
    }
}

/// 阶段 2a：run 启动前刷新 registry 快照（实际状态轮中可见）。
fn stage_refresh_registry_snapshot(
    tenant_id: &str,
    effective_pipeline_id: &str,
    initial_state: &serde_json::Value,
) {
    let reg = agentos_session::pipeline_state_registry::global_registry();
    if reg.contains(tenant_id, effective_pipeline_id) {
        reg.update_state(tenant_id, effective_pipeline_id, initial_state.clone());
    }
}

/// 阶段 1b：多轮上下文装配 + 本轮 user 消息入账，返回补全后的 initial_state。
///
/// state.messages 是 LLMCore._build_messages 读取的对话历史。单一权威（不搞降级路径）：
///   ① 热路径：PipelineStateRegistry 内存快照即上轮 final_state → messages 直接复用，
///      标量键（task.id/workspace/lineage.*/execution_context…）经
///      merge_recovered_scalars 一并恢复——快照是管道合法数据全集，续跑不丢键。
///   ② 冷路径：registry 未命中（重启/新会话/内存丢失）→ checkpoint/traces/表恢复
///      标量基线 + messages 表（持久化冷数据）按 effective_pipeline_id 恢复完整
///      历史，后续轮走热路径。
/// 恢复失败 = bug（内存丢 + DB 读不到）：显式 error 暴露，不静默吞掉。
///
/// `skip_user_append`（批次 D 显式重跑标志，经 state_overlay 注入）：regenerate
/// 重跑时目标 user 消息已在截断后历史中，跳过本轮 append——不借用 interrupted_tail
/// 启发式（同文/同 cmid 判定只服务崩溃重放场景）。
#[allow(clippy::too_many_arguments)]
async fn stage_recover_history(
    mut initial_state: serde_json::Value,
    store: &Arc<dyn StorageBackend>,
    message: &str,
    effective_pipeline_id: &str,
    tenant_id: &str,
    client_message_id: &str,
    skip_user_append: bool,
    state_overlay: Option<&serde_json::Value>,
    declared_volatile: &std::collections::HashSet<String>,
    run_id: &str,
) -> Result<serde_json::Value, EngineOutcome> {
    let mut history_prefix: Vec<serde_json::Value> = Vec::new();
    let mut history_loaded = false;
    let registry = agentos_session::global_registry();
    if let Some(entry) = registry.get(tenant_id, effective_pipeline_id) {
        // 热路径：内存快照即上轮 final_state——messages 复用，标量键随
        // merge_recovered_scalars 全量恢复。快照是管道合法数据全集：标量
        // 不恢复会让下游按身份缺席误判（任务管道缺 task.id 被判成主会话，
        // 工作区漂移到会话共享目录，产物落错位置）。
        let entry = entry.read();
        if let Some(msgs) = entry.state.get("messages").and_then(|v| v.as_array()) {
            history_prefix = msgs.clone();
            history_loaded = true;
        }
        merge_recovered_scalars(&mut initial_state, &entry.state, declared_volatile);
    }
    if !history_loaded {
        let (state, prefix) = recover_cold_history(
            initial_state,
            store,
            effective_pipeline_id,
            tenant_id,
            declared_volatile,
        )
        .await;
        initial_state = state;
        history_prefix = prefix;
    }
    // 派发 overlay 在恢复合并之后应用（B13① 域界定，ADR 2026-09-06）：overlay
    // 是调用方的显式派发指令（如任务域 resume 链清 task.status 终态键），必须
    // 优先于恢复基线——先应用会被热/冷恢复源（registry 快照/checkpoint/
    // pipeline_state 表）的残留值覆盖写回。保留字与 lineage.* 保护不变
    // （apply_state_overlay 纵深防御）。
    if let Some(overlay) = state_overlay {
        apply_state_overlay(&mut initial_state, overlay);
    }
    seed_history_and_reset_assistant_flag(&mut initial_state, history_prefix);
    let interrupted_tail = is_interrupted_tail(&initial_state, message, client_message_id);
    if !interrupted_tail && !skip_user_append {
        append_user_message(
            &mut initial_state,
            store,
            tenant_id,
            effective_pipeline_id,
            message,
            client_message_id,
            run_id,
        )
        .await
        .map_err(|e| {
            // B3：重试耗尽仍失败 = 本轮 user 消息未受理（内存侧也不留——
            // 「内存已注入、实录缺席」的静默分叉窗口消灭在源头）。
            error!(
                pipeline_id = %effective_pipeline_id,
                tenant_id = %tenant_id,
                error = %e,
                "user 消息落库重试耗尽，终止本轮发送（不进入引擎执行）"
            );
            EngineOutcome::terminated(format!(
                "消息未受理：user 消息落库失败（已重试仍失败），本轮发送已终止，请稍候重试。原因: {e}"
            ))
        })?;
    }
    Ok(initial_state)
}

/// 冷路径恢复（registry 未命中时）：checkpoint/traces/pipeline_state/message_slots
/// 四源装配标量基线与消息历史，返回（合并标量后的 state, messages 历史前缀）。
/// ① checkpoint 只提供**标量基线**——messages 一律不消费（load 后剥离丢弃），
///    无论新旧格式（任务 5 瘦身后新 checkpoint 本就不含 messages）；
/// ② 无 checkpoint → traces 回放**标量字段**（merge_patch 跳过 messages）；
/// ③ pipeline_state 表补充累计标量（每 step upsert 最新值）；
/// ④ messages 从 message_slots 表直读（消息队列持久真值，零回放）。
async fn recover_cold_history(
    mut initial_state: serde_json::Value,
    store: &Arc<dyn StorageBackend>,
    effective_pipeline_id: &str,
    tenant_id: &str,
    declared_volatile: &std::collections::HashSet<String>,
) -> (serde_json::Value, Vec<serde_json::Value>) {
    let mut history_prefix: Vec<serde_json::Value> = Vec::new();
    // 冷路径（ADR 2026-09-18：state 唯一真值，checkpoint 降级纯备份/DR）：
    // ① pipeline_state 表是主真值——直接读累计标量（每 step upsert 最新值）。
    // ② messages 从 message_slots 表直读（消息队列持久真值，零回放）。
    // ③ DR 路径（仅当 ①+② 双双为空 = state 缺失/损坏）：checkpoint 标量基线 +
    //    traces 回放重建。checkpoint/traces 不再参与常规恢复主路径。
    let mut recovered: serde_json::Value = serde_json::json!({});
    let mut state_hit = false;
    if !effective_pipeline_id.is_empty() {
        match store
            .load_pipeline_state(effective_pipeline_id, tenant_id)
            .await
        {
            Ok(state_fields) => {
                if !state_fields.is_empty() {
                    state_hit = true;
                }
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
            Ok(msgs) if !msgs.is_empty() => {
                history_prefix = msgs;
                state_hit = true;
            }
            Ok(_) => {}
            Err(e) => {
                error!(
                    pipeline_id = %effective_pipeline_id,
                    error = %e,
                    "load_message_history 冷恢复失败（对话历史可能不完整）"
                );
            }
        }
    }
    // DR 路径：state 与消息双双缺席（state 丢失/损坏/首次恢复旧库）——
    // checkpoint 提供标量基线（messages 剥离丢弃），无 checkpoint 则 traces
    // 回放标量字段（merge_patch 跳过 messages）。
    if !state_hit && !effective_pipeline_id.is_empty() {
        warn!(
            pipeline_id = %effective_pipeline_id,
            "state 与消息双空：走 DR 重建（checkpoint 基线 + traces 回放）"
        );
        let mut ckpt_hit = false;
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
                    "DR 重建从 checkpoint 取标量基线"
                );
            }
            Ok(None) => {
                // 无 checkpoint：继续 traces 回放
            }
            Err(e) => {
                // checkpoint 读失败不等同无 checkpoint：降级走 traces 回放时留痕，
                // 避免静默丢标量基线（错误是数据读失败，不是记录不存在）。
                error!(
                    pipeline_id = %effective_pipeline_id,
                    error = %e,
                    "load_latest_checkpoint DR 重建失败，降级 traces 回放"
                );
            }
        }
        if !ckpt_hit {
            // 只回放本管道自己的轨迹。thread 全量回放会把同会话其它管道
            // （父会话/兄弟子任务）的控制态（conversation_mode/core_type/router.*）
            // 种进本管道初始 state——曾致新子任务第 0 轮即被对话挂起路由误挂
            // （2026-08-30 管道身份裁定：执行态恢复只认 pipeline_id）。
            match store
                .get_step_traces_by_pipeline(effective_pipeline_id, tenant_id)
                .await
            {
                Ok(step_traces) => {
                    for entry in &step_traces {
                        merge_patch(&mut recovered, &entry.patch_data);
                    }
                }
                Err(e) => {
                    // 持久化恢复失败：bug 信号（内存丢 + DB 也读不到），显式 error 暴露。
                    error!(
                        pipeline_id = %effective_pipeline_id,
                        error = %e,
                        "get_step_traces_by_pipeline 回放失败"
                    );
                }
            }
        }
    }
    // 标量字段注入 state——跳过规则见 merge_recovered_scalars（GAP-3：
    // message/input/suspended 等 per-run 键残留会顶掉本轮新输入，重启后
    // 旧 user 消息被重放消费；_skip_user_append 等下划线键泄漏会吞消息）。
    merge_recovered_scalars(&mut initial_state, &recovered, declared_volatile);
    (initial_state, history_prefix)
}

/// messages 塞回 state + A1 复位标志（行为契约注释随代码平移保留）。
fn seed_history_and_reset_assistant_flag(
    initial_state: &mut serde_json::Value,
    history_prefix: Vec<serde_json::Value>,
) {
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
}

/// GAP-3 中断重放幂等判定（ADR 2026-08-21 契约注释随代码平移保留，见函数体）。
fn is_interrupted_tail(
    initial_state: &serde_json::Value,
    message: &str,
    client_message_id: &str,
) -> bool {
    // GAP-3：中断重放幂等——上一次尝试若已把本轮 user 消息落槽（重启/崩溃
    // 截断 run，无 assistant 跟随），恢复出的 history 尾部就是它；再 append 会
    // 重复落槽+重复消费。判定按幂等键裁决
    // （ADR 2026-08-21）：cmid 在场时只有「同 cmid」才算同一次发送的重派（吞）；
    // cmid 不同 = 真发了两条（绝不吞）。无 cmid
    // 的路径（触发器注入/旧客户端）维持同文判定。
    initial_state
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
        })
}

/// user 消息落库重试退避序列（对齐引擎侧 PERSIST_RETRY_BACKOFF_MS 范式：
/// 失败后再试 2 次、真实退避 100/300ms，共 3 次尝试）。
const USER_APPEND_RETRY_BACKOFF_MS: &[u64] = &[100, 300];

/// 本轮 user 消息 append 落库（ADR 2026-08-21 / ops 即轨迹契约注释随代码平移保留）。
/// B3：落库失败先重试（瞬时 DB 抖动不打断对话），重试耗尽仍失败上抛给调用方
/// 终止本轮发送——会话面裁定"DB 故障不得假成功"，禁止"内存已注入、实录缺席"
/// 的静默分叉（重启后消息从历史消失且无对账锚点）。
async fn append_user_message(
    initial_state: &mut serde_json::Value,
    store: &Arc<dyn StorageBackend>,
    tenant_id: &str,
    effective_pipeline_id: &str,
    message: &str,
    client_message_id: &str,
    run_id: &str,
) -> Result<(), agentos_core::types::StorageError> {
    // user 经 append op(无 seq → 引擎分配 next seq)+ 落 message_slots。
    // 指纹实录塞 _pending_message_ops（内部字段）：executor.persist_run_start 落一条
    // user_input 轨迹后移除——首轮 user 由此进入审计/回放范围（ops 即轨迹）。
    // cmid 非空时随 metadata 落库（compute_message_id 纳入 metadata 参与 hash：
    // 同内容多次发送 record_id 各自唯一，顺带消除重复内容同 id 碰撞）。
    // _run_id（表侧内部字段，与 assistant 的 _message_id 同模式）：轮首 user 消息
    // 触发的 run 在 append 时已生成，随 op 落 message_slots.run_id——regenerate
    // 审计锚预检读它（NULL = 拒绝，轮首消息曾因此全部不可编辑重发/回退）。
    let mut user_msg = serde_json::json!({"role":"user","content":message});
    if !client_message_id.is_empty() {
        user_msg["metadata"] = serde_json::json!({"client_message_id": client_message_id});
    }
    let ops = [serde_json::json!({"op":"set","msg":user_msg,"_run_id": run_id})];
    // apply_messages_op_update 先改内存后落表：每次重试前按快照还原内存，
    // 防失败重试把同一条 user 消息双写进 messages。
    let messages_snapshot = initial_state.get("messages").cloned();

    let mut last_err: Option<agentos_core::types::StorageError> = None;
    for try_no in 0..=USER_APPEND_RETRY_BACKOFF_MS.len() {
        match agentos_engine::apply_messages_op_update(
            initial_state,
            store.as_ref(),
            tenant_id,
            &ops,
        )
        .await
        {
            Ok(user_ledger) if !user_ledger.is_empty() => {
                if let Some(obj) = initial_state.as_object_mut() {
                    obj.insert(
                        "_pending_message_ops".to_string(),
                        serde_json::Value::Array(user_ledger),
                    );
                }
                return Ok(());
            }
            Ok(_) => return Ok(()),
            Err(e) => {
                warn!(
                    target_op = "append_user_message",
                    attempt = try_no + 1,
                    pipeline_id = %effective_pipeline_id,
                    tenant_id = %tenant_id,
                    error = %e,
                    "user 消息落库失败"
                );
                // apply 先改内存后落表：每次失败（含最后一次）都按快照还原内存，
                // 保证出口处内存与落库一致——失败不留半条消息。
                if let Some(obj) = initial_state.as_object_mut() {
                    match &messages_snapshot {
                        Some(snapshot) => {
                            obj.insert("messages".to_string(), snapshot.clone());
                        }
                        None => {
                            obj.remove("messages");
                        }
                    }
                }
                if let Some(backoff_ms) = USER_APPEND_RETRY_BACKOFF_MS.get(try_no) {
                    tokio::time::sleep(Duration::from_millis(*backoff_ms)).await;
                } else {
                    last_err = Some(e);
                }
            }
        }
    }
    Err(last_err.expect("retry loop runs at least once"))
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
#[allow(clippy::too_many_lines)] // 存量吸收(2026-10-12 文件规模棘轮): chat 执行编排聚合,拆分随执行链批次
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
    user_id: &str,
    pipeline_config_id: Option<&str>,
) -> Result<serde_json::Value, EngineOutcome> {
    // runs 表退役（ADR 2026-09-18）：防御失败簿记所需坐标提前捕获（initial_state/
    // tenant 随后移入 executor）
    let defensive_bookkeeping: (String, String) = (
        initial_state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        tenant.tenant_id.clone(),
    );

    // 轮次观察点桥接（一轮 = 一条消息）：session 未接线的部署形态不挂（仅引擎执行）。
    // 线程/管道坐标从 initial_state 取（WS 路径与注入路径同一来源）。
    let round_events = state.session.as_ref().map(|session| {
        let thread_id = initial_state
            .get("session_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .or_else(|| initial_state.get("thread_id").and_then(|v| v.as_str()))
            .unwrap_or("")
            .to_string();
        let route_id = initial_state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| thread_id.clone());
        Arc::new(crate::ws_session::SessionRoundEvents::new(
            session.clone(),
            thread_id,
            route_id,
            user_id.to_string(),
            store.clone(),
            tenant.tenant_id.clone(),
        )) as Arc<dyn agentos_engine::RoundEvents>
    });

    // ── Pull 热加载（在 project_root 被 move 给 executor 之前算出 config_root）──
    // 管道编译产物选择收敛点：None = autonomous 热重载单例（默认会话现状路径，
    // D9 范围外不钉）；Some(id) = 版本化取用（D9 出生即钉：实例首次取用把
    // (name, config_hash) 钉进管道 state 持久键，后续轮次恒用钉住版本，PUT
    // 热更只对之后出生的实例生效）。
    let config_root = project_root.join("config");
    let pinned = pinned_config_from_state(&initial_state);
    let explicit_name = pipeline_config_id.filter(|s| !s.is_empty());
    let pinned_hash = match resolve_config_pin(
        explicit_name,
        pinned.as_ref().map(|(n, h)| (n.as_str(), h.as_str())),
    ) {
        Ok(h) => h,
        Err(e) => {
            warn!(pipeline_config = ?pipeline_config_id, error = %e, "Pipeline config pin rejected");
            return Err(EngineOutcome::terminated(format!(
                "[error] 管道配置加载失败: {e}"
            )));
        }
    };
    let compiled = match compiled_for(
        state,
        &config_root,
        pipeline_config_id,
        pinned_hash.as_deref(),
    )
    .await
    {
        Ok(c) => c,
        Err(e) => {
            warn!(pipeline_config = ?pipeline_config_id, error = %e, "Pipeline config select failed");
            return Err(EngineOutcome::terminated(format!(
                "[error] 管道配置加载失败: {e}"
            )));
        }
    };
    // P1-1 core 器官缺省声明化：管道配置 `initial_state:` 声明的键值在开轮前
    // 补种（缺省只补缺，已有值不覆盖——本轮/调用方值优先）。core_type 是纯
    // 语义迭代形态标志，未声明时以 llm_call 作语义缺省；core_plugin 缺省由
    // 配置声明（使用动态核心步骤的管道必须声明，内核零插件 id 知识）。
    let mut initial_state = initial_state;
    if let Some(obj) = initial_state.as_object_mut() {
        for (k, v) in &compiled.initial_state {
            if !obj.contains_key(k) {
                obj.insert(k.clone(), v.clone());
            }
        }
        if !obj.contains_key("core_type") {
            obj.insert("core_type".into(), serde_json::json!("llm_call"));
        }
    }
    // D9 出生即钉（写入面）：实例首次解析显式配置 → (name, config_hash) 落
    // 管道 state 持久键，两路写：
    // - pipeline_state 表直写（冷路径/重启恢复面）：project_state_snapshot 只
    //   投影 persistent_fields 声明键，内核自有簿记键走直写——record_run_start
    //   同通道；
    // - initial_state 点号键（热路径）：随 final_state 进 registry 快照，下轮
    //   merge_recovered_scalars 恢复。
    // 表写失败终止本轮（B3 同精神）：钉住簿记缺失 = 重启后版本钉住静默失效。
    if let (Some(name), None) = (explicit_name, pinned.as_ref()) {
        let mut pin_fields = serde_json::Map::new();
        pin_fields.insert(
            PIPELINE_CONFIG_PIN_NAME_KEY.to_string(),
            serde_json::json!(name),
        );
        pin_fields.insert(
            PIPELINE_CONFIG_PIN_HASH_KEY.to_string(),
            serde_json::json!(compiled.config_hash),
        );
        let pin_pipeline_id = initial_state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if pin_pipeline_id.is_empty() {
            warn!("版本钉住落库跳过：initial_state 缺 pipeline_id（不可达防御）");
        } else if let Err(e) = store
            .upsert_state_fields(pin_pipeline_id, &tenant.tenant_id, &pin_fields)
            .await
        {
            warn!(
                pipeline_id = %pin_pipeline_id,
                error = %e,
                "版本钉住落库失败，终止本轮（重发可恢复）"
            );
            return Err(EngineOutcome::terminated(format!(
                "[error] 管道版本钉住写入失败: {e}（本轮未执行，请重发消息重试）"
            )));
        }
        if let Some(obj) = initial_state.as_object_mut() {
            for (k, v) in &pin_fields {
                obj.insert(k.clone(), v.clone());
            }
        }
    }
    let known_ids = live_plugin_ids(state).await;
    // P1-4 声明化：收集全部插件 manifest `state.volatile_keys` 声明并集注入
    // store（StorageBackend::set_declared_volatile_keys），checkpoint 落档时
    // 与内核自有键在同一剥离点（save_checkpoint）剥离；恢复合并侧
    // （stage_build_initial_state）消费同一并集跳过。
    let declared_volatile: Vec<String> = state
        .manifests
        .read()
        .await
        .iter()
        .filter_map(|m| m.state.as_ref())
        .flat_map(|s| s.volatile_keys.iter().cloned())
        .collect();
    store.set_declared_volatile_keys(&declared_volatile);
    let executor = agentos_engine::PipelineExecutor::new(
        invoker,
        project_root,
        tenant,
        known_ids.iter().cloned(),
        store.clone(),
        run_id.to_string(),
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
    // 监控 M2 分类（整改 K2 声明化）：从全部插件 manifest 的
    // `capabilities.metrics_class` 声明收集注入（未声明 = Other 兜底；
    // 非法值引擎装配点 warn 后按 Other 计）。
    .with_metrics_classes(state.manifests.read().await.iter().filter_map(|m| {
        m.capabilities
            .metrics_class
            .clone()
            .map(|declared| (m.id.clone(), declared))
    }))
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
    )
    // 生命周期事件总线（观察路径）：run 开始/结束广播 OnPipelineStart/OnPipelineEnd
    // 给审计/指标订阅者。进程级单例（agentos-kernel 启动期 set_global 注册；
    // 未注册 None = 不发射，与域事件发射点同一降级语义）。
    .with_hook_bus(agentos_hooks::global());
    // 轮数硬上限接线（管道 YAML `max_rounds:` → 编译产物 → executor，ADR
    // 2026-09-11-engine-loop-cap）：None = 不注入（引擎缺省 200，旧 YAML 行为
    // 不变）；Some(0) 注入后由 run 入口 EngineError::Config 拒绝（零执行）。
    let executor = match compiled.max_rounds {
        Some(n) => executor.with_max_rounds(n),
        None => executor,
    };
    // 轮次观察点（None = 未接线）：chat 会话路径逐轮发流式事件。
    let executor = if let Some(ev) = round_events {
        executor.with_round_events(ev)
    } else {
        executor
    };

    // agent 配置唯一事实源 = agent_config_load 插件；工具面过滤 = tool_schema 插件
    // 经内核 tool-surface capability（K10 执行时契约）。引擎/内核零 agent 配置知识。
    info!(run_id = %run_id, agent_id = %agent_id, "Pipeline run started");

    // GAP-2：防御网路径的失败事件标签预捕获（run_compiled 会 move initial_state）。
    // 载荷复用终态剥离集——初始 state 携带出生契约键（task.*/lineage.*），
    // 订阅方据此派生失败通知；messages 等大字段已剥离，克隆成本 KB 级。
    let failed_emit_state = terminal_event_state_payload(&initial_state);

    // run 启动域事件：回合开始时广播 run.started，触发器（EVENT）与生命周期
    // 订阅者据此感知回合边界。标签与 run 终态事件同构（run_id/pipeline_id/
    // thread_id/session_id）。
    crate::plugin_lifecycle::broadcast_domain_event(
        state,
        "run.started",
        vec![
            ("run_id".to_string(), serde_json::json!(run_id)),
            (
                "pipeline_id".to_string(),
                initial_state
                    .get("pipeline_id")
                    .cloned()
                    .unwrap_or(serde_json::Value::Null),
            ),
            (
                "thread_id".to_string(),
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
            // error 级（run 失败是终态裁决事件；装机 2026-10-10 B74/B75/B76
            // 排障按 ERROR 检索不到本次级 WARN，失败原因等于没落日志）
            error!(run_id = %run_id, error = %e, "PipelineExecutor run failed");
            // GAP-2：run.failed 终态事件（引擎 Err 防御网；task.* 派生经预捕获标签）
            emit_run_terminal_domain_events(state, &failed_emit_state, run_id, true).await;
            // B2：引擎失败兜底——把 run 标记 failed + ended_at，避免永远卡 running、
            // 历史悬空。（PipelineExecutor::run 当前不返回 Err，这是防御网；崩溃留下的
            // running 孤儿由内核启动 reap_orphan_runs 清扫。）
            // runs 表退役（ADR 2026-09-18）：簿记 = state 运行键（按 pipeline_id 定位）
            let (failed_pipeline_id, bookkeeping_tenant) = &defensive_bookkeeping;
            if !failed_pipeline_id.is_empty() {
                let now = chrono::Utc::now().to_rfc3339();
                let mut fields = serde_json::Map::new();
                fields.insert("run_status".to_string(), serde_json::json!("failed"));
                fields.insert("run_ended_at".to_string(), serde_json::json!(now));
                if let Err(pe) = store
                    .upsert_state_fields(failed_pipeline_id, bookkeeping_tenant, &fields)
                    .await
                {
                    warn!(run_id = %run_id, error = %pe, "run_status(Failed) 簿记失败（继续）");
                }
            }
            Err(EngineOutcome::terminated(format!(
                "[engine-run-failed] {message}"
            )))
        }
    }
}

/// 阶段 3b/4：final_state 回写全局 registry + 提取响应。回写：final_state 含
/// 完整 messages 历史（LLM 插件 append 了 assistant 回复），按
/// (tenant_id, effective_pipeline_id) 常驻：下一轮热路径直接复用免 DB 查询，
/// 重启/内存丢失时冷路径从 messages 表恢复（见 stage_recover_history）。常驻
/// 副本剥离大对象：`tool_schemas`（每轮 prepare 链经 tool-surface capability
/// 重新拉取，跨轮无意义）与 `_` 前缀引擎私有 staging 键（`_full_tool_results`
/// 等 per-run 中间态，权威副本在工具缓存，见 ADR 2026-09-29-trace-mirror-dedup）；
/// messages 驻留是设计正确（管道持有队列省每轮拼装），不动。
async fn stage_finalize(
    final_state: &serde_json::Value,
    store: Option<&dyn StorageBackend>,
    tenant_id: &str,
    effective_pipeline_id: &str,
    thread_id: &str,
    agent_id: &str,
    run_id: &str,
) -> EngineOutcome {
    // 终态失败折算（控制状态键契约 ADR 2026-08-30，与引擎落库/域事件共用单点
    // RunStatus::from_control_state）：署名失败（dropped_tool_calls/timeout 等
    // FAILED_STOP_REASONS 命中，DSL 正常收束）曾硬编码 failed:false——失败 run
    // 以成功姿态外发，chat 面把虚报文本当真（BUG-45 机制根源）；挂起/取消非失败。
    let status = agentos_core::types::RunStatus::from_control_state(final_state);
    // 步骤失败聚合（ADR 2026-10-15-step-error-run-signature-aggregation）：插件
    // 步失败由引擎 warn+继续（ADR 2026-08-18），错误收集在 `_plugin_errors`——
    // 未署名完成轮零产出（raw_result 空，与响应提取同锚）折叠 failed；有产出
    // = 有痕完成（degraded）。控制状态署名优先，挂起/取消/署名失败轮不折叠。
    let plugin_errors = final_state
        .get("_plugin_errors")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    // 轮边界注入消费的队列 cmid（引擎 `_consumed_pending_cmids` 键，跨多次注入
    // 累积；下划线前缀键随 strip_non_resident_keys 剥离不进持久化）。
    let consumed_pending_cmids = final_state
        .get("_consumed_pending_cmids")
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|c| c.as_str().map(str::to_string))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let has_output = final_state
        .get("raw_result")
        .is_some_and(|v| v.as_str().is_some_and(|s| !s.is_empty()));
    let control_failed = matches!(status, agentos_core::types::RunStatus::Failed);
    let fold_failed = !control_failed
        && !has_output
        && !plugin_errors.is_empty()
        && status == agentos_core::types::RunStatus::Completed;
    let content = if control_failed {
        let reason = final_state
            .get("router.stop_reason")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown");
        format!("本轮 run 失败（stop_reason: {reason}）")
    } else if fold_failed {
        step_error_fold::zero_output_content(&plugin_errors)
    } else {
        extract_response_content(final_state)
    };
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
    let failed = control_failed || fold_failed;
    if !effective_pipeline_id.is_empty() {
        let reg = agentos_session::global_registry();
        let mut persisted = final_state.clone();
        strip_non_resident_keys(&mut persisted);
        // 失败不进消息流（ADR 2026-10-27-pipeline-error-notification-channel，
        // 用户裁定，替代缺陷 ④-A 的失败回复直写契约）：失败是管道域事实，不
        // 是对话内容——合成 assistant 错误消息（synthetic=true）曾直写
        // message_slots 并入常驻快照，聊天流被错误气泡污染且刷新后历史悬置
        // 错误回复。失败事实的常驻面 = run_status / run_stop_reason /
        // run.plugin_errors（下方镜像键，管道域读面）+ run_failed WS 事件
        // （error 字段携带首条插件错误原文，通知卡弹层）；REST content 与
        // WS stream_error 照常携带失败终态文本。消息队列零写入 = 零回灌
        // （llm_core 出站装配天然见不到失败原因，旧 synthetic 剔除逻辑随之
        // 退役）。
        // 插件级失败常驻镜像（ADR 2026-10-17-task-birth-workspace-inclusion）：
        // `_plugin_errors` 是引擎私有 staging 键（`_` 前缀），strip 时随快照
        // 剥离——重启后插件级失败彻底失忆，启动对账（tasks/reconcile）与面板
        // 读面均无从知悉。镜像到常驻键 run.plugin_errors 并直写 pipeline_state
        // 表，冷启动后仍可读。非空才写：绝大多数 run 零插件失败，不增常驻体积。
        // 不改 ADR 2026-10-15 的折叠语义（有产出 + 步失败 = degraded 仍成立），
        // 本键只负责让失败事实不丢。
        if !plugin_errors.is_empty() {
            if let Some(obj) = persisted.as_object_mut() {
                obj.insert(
                    "run.plugin_errors".to_string(),
                    serde_json::Value::Array(plugin_errors.clone()),
                );
            }
            if let Some(store) = store {
                let mut fields = serde_json::Map::new();
                fields.insert(
                    "run.plugin_errors".to_string(),
                    serde_json::Value::Array(plugin_errors.clone()),
                );
                if let Err(pe) = store
                    .upsert_state_fields(effective_pipeline_id, tenant_id, &fields)
                    .await
                {
                    warn!(
                        pipeline_id = %effective_pipeline_id,
                        tenant_id = %tenant_id,
                        run_id = %run_id,
                        error = %pe,
                        "插件级失败常驻落库失败（继续；重启后该失败将不可见）"
                    );
                }
            }
        }
        if !reg.contains(tenant_id, effective_pipeline_id) {
            reg.get_or_init(
                tenant_id,
                effective_pipeline_id,
                thread_id,
                agent_id,
                persisted,
            );
        } else {
            reg.update_state(tenant_id, effective_pipeline_id, persisted);
        }
    }
    EngineOutcome {
        content,
        final_assistant,
        failed,
        // 有痕完成（步失败后自救产出）：调用方据此与干净完成区分；失败/干净轮恒 false。
        degraded: !fold_failed
            && !plugin_errors.is_empty()
            && status == agentos_core::types::RunStatus::Completed,
        // 插件错误收集（引擎 `_plugin_errors` 键；下划线前缀键不参与插件
        // state_updates 合并，仅引擎内部写入）。WS 路径据此发射 plugin_error。
        plugin_errors,
        consumed_pending_cmids,
    }
}

/// 常驻快照剥离：`tool_schemas`（每轮 prepare 链经 tool-surface capability
/// 重新拉取，跨轮无意义）与 `_` 前缀引擎私有 staging 键（`_full_tool_results`
/// 等 per-run 中间态，权威副本在工具缓存，见 ADR 2026-09-29-trace-mirror-dedup）。
/// messages 驻留是设计正确（管道持有队列省每轮拼装），不动。
fn strip_non_resident_keys(state: &mut serde_json::Value) {
    if let Some(obj) = state.as_object_mut() {
        obj.remove("tool_schemas");
        let underscore_keys: Vec<String> =
            obj.keys().filter(|k| k.starts_with('_')).cloned().collect();
        for k in underscore_keys {
            obj.remove(&k);
        }
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

/// /api/v1/chat POST 端点——通过管道引擎处理消息。
async fn chat_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    axum::Json(req): axum::Json<WsRequest>,
) -> Result<axum::Json<WsResponse>, ApiError> {
    // 在租户上下文内通过管道引擎处理消息（多租户 P0-4）；租户解析失败即拒绝
    // （K8 修订 ADR 2026-10-10），不降级 default 写串租户。
    let tenant_ctx = request_tenant_ctx(state.store.as_ref(), &headers, &req.session_id).await?;
    // 分层持久化：REST chat 路径用会话真实 active_pipeline_id（锚点）经活性守卫
    // 解析（刀4 终态惰性切回，见 anchor_liveness）；WS 路径前端已带真实
    // pipeline_id，此处为 REST fallback 补齐。
    let pipeline_id = if let Some(store) = state.store.as_ref() {
        let sid = req.session_id.clone();
        let store_clone = store.clone();
        let session = match agentos_tenant::scope(tenant_ctx.clone(), async move {
            store_clone.get_session(&sid).await
        })
        .await
        {
            Ok(Some(session)) => Some(session),
            // 会话不存在（Ok(None)）→ 空 pipeline_id，chain_key 回退 session_id；
            // 存储故障（Err）单独报错可见，不与"无会话"混同。
            Ok(None) => None,
            Err(e) => {
                warn!(session = %req.session_id, error = %e, "chat_handler: get_session 失败，chain_key 回退 session_id");
                None
            }
        };
        anchor_liveness::resolve_chat_pipeline_id(store, &tenant_ctx, session).await
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
    // 执行 agent（派发簿记：registry 登记 / run 日志；run 执行身份 = 管道
    // state agent.id，不取此值）。
    let exec_agent = if req.agent_id.is_empty() {
        "agentos".to_string()
    } else {
        req.agent_id.clone()
    };
    let dispatcher = crate::ws_session::EngineDispatcher::new(state.clone());
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
            None,
            &cmid,
            agentos_core::types::PendingInputSource::Http,
        )
        .await
    {
        return Err(ApiError::Internal {
            message: format!("chat 派发失败: {e}"),
        });
    }
    // 消费任务 panic（rx 关闭）时给出明确错误而非挂死等待；等待整体带超时
    // 兜底：消费循环若因出队错误等场景未通知 waiter（残留队列通知也失败），
    // 无超时即 HTTP 请求无限挂死 + waiter 条目泄漏。上限取 600s——远大于
    // 健康轮次的生成窗口，只兜结构性挂死，不充当业务超时。
    const HTTP_CHAT_WAIT_TIMEOUT_SECS: u64 = 600;
    let outcome = match tokio::time::timeout(
        std::time::Duration::from_secs(HTTP_CHAT_WAIT_TIMEOUT_SECS),
        rx,
    )
    .await
    {
        Ok(outcome) => outcome
            .unwrap_or_else(|_| EngineOutcome::terminated("[engine task terminated unexpectedly]")),
        Err(_) => {
            // 超时自清理 waiter（防条目泄漏；事后到达的 notify 因条目已删
            // 变为 no-op），对客户端显式 500 而非无限挂死。
            crate::ws_session::remove_outcome_waiter(&cmid);
            return Err(ApiError::Internal {
                message: format!(
                    "chat 同步等待超时（{HTTP_CHAT_WAIT_TIMEOUT_SECS}s）：消费任务未在期限内回传结果"
                ),
            });
        }
    };

    let response = WsResponse {
        r#type: "message".to_string(),
        content: outcome.content,
        session_id: req.session_id,
        timestamp: chrono::Utc::now().to_rfc3339(),
        // 降级应答显式化（2026-08-26 统一错误模型）：echo_fallback 时标记，
        // 调用方（插件 send_message 等）据此与正常回复区分，不把降级当成功。
        degraded: Some(outcome.degraded),
        // 失败终态显式化（刀2）：外发失败语义随响应出走，非静默非虚报。
        failed: Some(outcome.failed),
    };
    Ok(axum::Json(response))
}

/// 交互应答提供者解析（P1-3 声明驱动）：遍历插件 manifests，找
/// `provides.capabilities[].protocol_roles` 含 `interaction-respond` 的
/// (namespace, method)。内核不点名交互插件 id——任意插件声明同角色即接管
/// 应答端点后端（与 capability namespace 的热替换语义一致）；无声明 → None
/// （调用方 fail-closed 显式失败，不猜测路由目标）。
pub(crate) fn resolve_interaction_responder(
    manifests: &[agentos_core::traits::PluginManifest],
) -> Option<(String, String)> {
    for m in manifests {
        let Some(provides) = &m.provides else {
            continue;
        };
        for cap in &provides.capabilities {
            for role in &cap.protocol_roles {
                if role.role == "interaction-respond" {
                    return Some((cap.namespace.clone(), role.method.clone()));
                }
            }
        }
    }
    None
}

/// 人类交互响应端点——前端用户操作（选择选项/拒绝/取消）经此提交。
///
/// 交互应答转发（P1-3 声明驱动）：路由目标 (namespace, method) 从插件
/// provides 声明的 `interaction-respond` 协议角色解析；应答载荷**原样透传**
/// （`{request_id, response: <body>}` 信封），载荷键契约（response_type 等）
/// 由交互插件自持——内核零载荷知识。
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

    let Some(registry) = state.capability_handlers.as_ref() else {
        return Ok(axum::Json(
            serde_json::json!({"success": false, "error": "capability registry not available"}),
        ));
    };

    let manifests = state.manifests.read().await.clone();
    let Some((namespace, method)) = resolve_interaction_responder(&manifests) else {
        return Ok(axum::Json(serde_json::json!({
            "success": false,
            "error": "no plugin declares the interaction-respond protocol role"
        })));
    };

    let inputs = serde_json::json!({
        "request_id": request_id,
        "response": body,
    });

    match registry.route(&namespace, &method, inputs).await {
        Ok(data) => Ok(axum::Json(serde_json::json!({
            "success": true,
            "request_id": request_id,
            "data": data,
        }))),
        Err(e) => Ok(axum::Json(serde_json::json!({
            "success": false,
            "request_id": request_id,
            "error": e.to_string(),
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
            message: format!("Failed to bind {addr}: {e}"),
        })?;
    info!("API server starting on {}", addr);
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal(shutdown_invoker))
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("Server error: {e}"),
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
mod finalize_visibility_tests;
#[cfg(test)]
mod pipeline_version_tests;
#[cfg(test)]
mod step_error_aggregation_tests;
#[cfg(test)]
mod tests;
