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

use self::auth_surface::cors_middleware;
use self::auth_surface::write_surface_auth;
pub(crate) use self::engine_dispatch::EngineOutcome;
pub(crate) use self::ws_entry::request_tenant_ctx;
use self::ws_entry::ws_handler;
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
    plugins_set_enabled_handler, plugins_status_handler, put_pipeline_config_handler,
    put_plugin_config_handler, put_user_env_handler, schema_handler, serve_upload_handler,
    system_memstats_handler, system_restart_handler, tools_handler, validate_all_plugins_handler,
    AppState,
};
use crate::session_routes::{
    create_session_handler, delete_session_handler, list_session_messages_handler,
    list_sessions_handler, sessions_schema_handler, update_session_agent_handler,
    update_session_handler,
};
use agentos_http::auth::resolve_request_tenant_id;
use agentos_http::error::ApiError;
use agentos_session::router::PipelineDispatcher;
#[allow(unused_imports)] // 路径解析面：crate::server::X 经此供给（glob 非直接消费）
pub(crate) use auth_surface::*;
pub use compile_cache::load_and_compile_by_name;
pub(crate) use compile_cache::pipeline_cache_invalidate;
pub(crate) use compile_cache::pipeline_compiled_cache_stats;
#[allow(unused_imports)] // 路径解析面：crate::server::X 经此供给（glob 非直接消费）
pub(crate) use compile_cache::*;
pub use compile_cache::{load_and_compile, pipeline_cache_seed};
pub(crate) use engine_dispatch::process_via_engine;
#[allow(unused_imports)] // 路径解析面：crate::server::X 经此供给（glob 非直接消费）
pub(crate) use engine_dispatch::*;
#[allow(unused_imports)] // 路径解析面：crate::server::X 经此供给（glob 非直接消费）
pub(crate) use history_recover::*;
#[allow(unused_imports)] // 路径解析面：crate::server::X 经此供给（glob 非直接消费）
pub(crate) use stage_exec::*;
#[allow(unused_imports)] // 路径解析面：crate::server::X 经此供给（glob 非直接消费）
pub(crate) use stages::*;
#[allow(unused_imports)] // 路径解析面：crate::server::X 经此供给（glob 非直接消费）
pub(crate) use ws_entry::*;

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
        // 启动装载清单（分级单副本可观察面：每插件路径/版本/级别/来源根；
        // 鉴权同 memstats 前缀读面）
        .route(
            "/api/v1/system/plugin-load-manifest",
            get(crate::system_diagnostics::system_plugin_load_manifest_handler),
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
    // 执行 agent（可选）：入口统一下压为 state agent.id overlay（ADR-2026-11-17
    // 契约键）——run 执行身份只认管道 state，派发面无独立形参。值守卫与
    // WS router 帧字段同款（valid_frame_field_value：agent.id 直落持久 state
    // 键，值形态归声明方），非法值静默丢弃该键并 WARN（可选提示性字段
    // 不得让请求失败）。
    let state_overlay = if req.agent_id.is_empty() {
        None
    } else if agentos_session::router::valid_frame_field_value(&req.agent_id) {
        Some(serde_json::json!({ "agent.id": req.agent_id }))
    } else {
        warn!(
            session = %req.session_id,
            value_chars = req.agent_id.chars().count(),
            "chat agent_id 形态非法（空/超长/含控制字符），丢弃身份键不入 overlay"
        );
        None
    };
    let dispatcher = crate::ws_session::EngineDispatcher::new(state.clone());
    let cmid = format!("http_{}", &uuid::Uuid::new_v4().simple().to_string()[..16]);
    let (tx, rx) = tokio::sync::oneshot::channel::<EngineOutcome>();
    crate::ws_session::register_outcome_waiter(cmid.clone(), tx);
    // tenant_ctx 仅供前段读 session；dispatcher 内部按 user_id 重新解析真实
    // 租户（同源函数），不从此处传递。
    let request = agentos_core::types::DispatchRequest {
        thread_id: req.session_id.clone(),
        user_id,
        pipeline_id,
        content: req.message.clone(),
        client_message_id: cmid.clone(),
        source: agentos_core::types::PendingInputSource::Http,
        execution_context: None,
        state_overlay,
        pipeline_config_id: None,
    };
    if let Err(e) = dispatcher.dispatch_user_input(request).await {
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
pub mod tests;

pub mod auth_surface;
pub mod compile_cache;
pub mod engine_dispatch;
pub mod history_recover;
pub mod stage_exec;
pub mod stages;
pub mod ws_entry;
