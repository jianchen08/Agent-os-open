//! WebSocket 连接入口（握手鉴权 + P2 内核化会话路径）。

use super::*;
/// WebSocket 连接处理器（AC-06-4）。
///
/// 生产恒走 P2 内核化会话路径（握手鉴权 + 连接注册 + 入站路由）；
/// session/路由未装配（仅测试构造场景）时显式 503，不静默降级 echo。
pub(crate) async fn ws_handler(
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
pub(crate) async fn run_p2_ws_session_authed(
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
pub(super) const PIPELINE_CONFIG_TTL: Duration = Duration::from_secs(1);
