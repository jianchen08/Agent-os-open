// @feature: FP-0.2.〇 HTTP 面反调身份链 task-local 身份 scope | @ci: rust-test
//! HTTP 端点 dispatcher（ADR §3.3 / 附录 E.1.2 / E.1.3）。
//!
//! 把入站 HTTP 请求路由到插件贡献的端点：
//! 1. 按 path+method 查 [`CapabilityRegistry`] 的 `http_routes`；
//! 2. 经 [`HttpHandleCapability`] 把 raw body(base64) + 全量 headers + query
//!    （多值形态 `query_multi`，重复 key 不塌缩；单值 `query` 由其派生）透传给插件
//!    `http.handle`（**绝不反序列化 body 再转发**——企微 SHA1 验签 + AES 解密吃 raw body）；
//! 3. 插件返回 `{status, headers, body, body_encoding}`，dispatcher 原样回写（**插件全权控制响应**）；
//! 4. per-endpoint `timeout_ms`（默认 30000）超时返回 504；
//!    per-endpoint `max_concurrency`（默认 16）超限返回 503。
//! 5. 鉴权闸（D2 两刀均落地）：按 manifest `http_endpoints[].auth` 声明执行——
//!    `user`/`admin` 走 /api/v1 同一 token 校验；`none` 显式匿名放行；
//!    无声明一律 401 fail-closed（读写同规，见 [`enforce_ext_auth`]）；
//!    已认证请求向插件转发身份头 `X-AgentOS-Tenant` / `X-AgentOS-User` /
//!    `X-AgentOS-Role`（租户过滤等插件侧归属校验的信任锚）。
//!
//! `build_router_with_http_routes` 把内核静态路由 + 插件 http_routes 动态挂到 axum 树。

use std::collections::HashMap;
use std::sync::Arc;

use agentos_core::traits::{
    CapabilityRegistry, HttpHandleCapability, HttpHandleRequest, HttpHandleResponse,
    HttpRouteDescriptor, PluginInvoker, PluginManifest,
};
use agentos_plugin_loader::CapabilityRegistryImpl;
use base64::Engine;
use tracing::warn;

/// per-endpoint 并发许可信号量（max_concurrency 上限保护）。
type Semaphore = tokio::sync::Semaphore;

/// 插件面请求体上限：2 MiB（显式钉死，2026-10-25 用户裁定维持此值）。
///
/// 此前为 axum 框架隐式默认值（代码零配置、框架替拍）——2026-10-25 sync_agent
/// 上行 413 事故实证该墙存在且生效。不上调：请求体整读 + base64 转发内存
/// 放大约 2.67 倍，抬墙即内存尖峰（与 /uploads 限量读同源姿势）；发送端
/// 分批（sync_agent 413 拆批）绕大件，单件真超限走死信隔离留痕。
pub(crate) const EXT_BODY_LIMIT_BYTES: usize = 2 * 1024 * 1024;

/// HTTP dispatcher：路由分发 + timeout + 并发上限。
///
/// 持有能力注册表（查路由）与一个 [`HttpHandleCapability`]（调插件 http.handle）。
/// 生产实现走 sidecar MCP；测试用进程内实现验证透传链路。
pub struct HttpDispatcher {
    registry: Arc<CapabilityRegistryImpl>,
    handler: Arc<dyn HttpHandleCapability>,
    /// RouteKey(path+method) → 并发信号量，惰性创建。
    semaphores: tokio::sync::Mutex<HashMap<(String, String), Arc<Semaphore>>>,
    /// 宿主启动态探针（D1 宽限面）：目标插件所在宿主正处于 spawn/respawn 窗口
    /// 时，端点超时预算暂停消耗（等待就绪而非 504）。`None` = 不感知启动态，
    /// 分发行为与既有单次 timeout 完全一致（测试桩 / 未接线的构造路径）。
    startup_probe: Option<Arc<dyn PluginInvoker>>,
    /// 校验门禁名单查询（G2 注册门禁全能力面通用化）：返回当前校验未完成/
    /// 校验失败的插件 id 集（契约账本现值）。`None` = 门禁未接线（裸测试
    /// 构造），维持既有行为。名单命中即整插件端点拒绝（503）。
    shadowed_plugin_ids_lookup: Option<Arc<dyn Fn() -> Vec<String> + Send + Sync>>,
}

/// 目标宿主处于启动窗口时，端点超时预算之上允许的额外等待上限（D1）。
///
/// 覆盖装机版实测 respawn 冷启窗（组宿主约 1.5-2.5s；solo 宿主含插件 on_load
/// 排队实测可达 5s+，2026-09-25 kernel.log）并留余量；探针异常常开时由它兜底
/// 保证端点仍有界返回，不会无限悬挂。
const HOST_STARTING_GRACE: std::time::Duration = std::time::Duration::from_secs(15);

/// 分发等待循环的轮询切片（D1）：预算是否暂停消耗按切片粒度重估——调用方
/// 在等待期间宿主才进入启动窗口（排队等 spawn 锁）也能被宽限覆盖。
const DISPATCH_POLL_SLICE: std::time::Duration = std::time::Duration::from_millis(250);

impl HttpDispatcher {
    /// 创建 dispatcher（不感知宿主启动态）。
    pub fn new(
        registry: Arc<CapabilityRegistryImpl>,
        handler: Arc<dyn HttpHandleCapability>,
    ) -> Self {
        Self {
            registry,
            handler,
            semaphores: tokio::sync::Mutex::new(HashMap::new()),
            startup_probe: None,
            shadowed_plugin_ids_lookup: None,
        }
    }

    /// 挂载宿主启动态探针（D1），返回 self 供链式调用。
    pub fn with_startup_probe(mut self, probe: Option<Arc<dyn PluginInvoker>>) -> Self {
        self.startup_probe = probe;
        self
    }

    /// 挂载校验门禁名单查询（G2 注册门禁全能力面通用化），返回 self 供链式
    /// 调用。名单 = 契约账本 verify_incomplete/verify_failed 现值（生产装配
    /// 读 `ContractLedger::verify_pending_plugin_ids()`）：名单内插件的端点
    /// 一并拒绝，复验通过（账本 ok）自动恢复。
    pub fn with_shadowed_plugin_ids_lookup(
        mut self,
        lookup: Option<Arc<dyn Fn() -> Vec<String> + Send + Sync>>,
    ) -> Self {
        self.shadowed_plugin_ids_lookup = lookup;
        self
    }

    /// 获取（或创建）某路由的并发信号量。
    async fn semaphore_for(&self, route: &HttpRouteDescriptor) -> Arc<Semaphore> {
        let key = (route.endpoint.path.clone(), route.endpoint.method.clone());
        let mut sems = self.semaphores.lock().await;
        sems.entry(key)
            .or_insert_with(|| Arc::new(Semaphore::new(route.max_concurrency() as usize)))
            .clone()
    }
}

/// dispatcher 分发结果。
#[derive(Debug)]
pub enum DispatchOutcome {
    /// 插件处理完成，返回自定义响应。
    Handled(HttpHandleResponse),
    /// 路由未找到（404）。
    NotFound,
    /// 超时（504）。
    Timeout,
    /// 并发超限（503）。
    ConcurrencyLimited,
    /// 插件处理出错（502）。
    HandlerError(String),
    /// 校验门禁拦截（G2 注册门禁全能力面通用化）：目标插件校验未完成/校验
    /// 失败，端点整体拒绝（503 服务不可用类），载荷为用户可读原因。
    VerificationBlocked(String),
}

/// 分发一个入站 HTTP 请求到插件 http.handle。
///
/// 返回 [`DispatchOutcome`]，由 axum handler 据此构造 HTTP 响应。
///
/// # Arguments
/// * `dispatcher` - dispatcher 实例
/// * `path` / `method` - 请求路径与方法（路由查找键）
/// * `raw_body` - 原始请求字节（不反序列化）
/// * `headers` - 全量 headers
/// * `query_multi` - 查询参数多值形态（key → 全量 value 列表，保序）。单值
///   `query`（last-wins）由此派生，保证 `query[k] == query_multi[k].last()`——
///   重复 key（如 `filter=a&filter=b`）不塌缩，多条件 AND 筛选全量到达插件。
pub async fn dispatch_http(
    dispatcher: &HttpDispatcher,
    path: &str,
    method: &str,
    raw_body: Vec<u8>,
    headers: HashMap<String, String>,
    query_multi: HashMap<String, Vec<String>>,
) -> DispatchOutcome {
    let Some(route) = dispatcher.registry.find_http_route(path, method) else {
        return DispatchOutcome::NotFound;
    };

    // G2 注册门禁（全能力面通用化，用户裁定 2026-10-26）：校验未通过 = 整个
    // 插件不许上岗——其 http_endpoints 一并拒绝（503 服务不可用类），与 LLM
    // 工具面遮挡同源（契约账本现值查询）：复验通过（账本 ok）自动恢复。
    // 未接线（None）= 门禁未装配，维持既有行为。
    if let Some(lookup) = dispatcher.shadowed_plugin_ids_lookup.as_ref() {
        if lookup().iter().any(|id| id == &route.plugin_id) {
            let reason = format!(
                "插件 {} 校验未完成或校验失败（G2 注册门禁），已拦截；复验通过后自动恢复",
                route.plugin_id
            );
            warn!(
                plugin = %route.plugin_id,
                path = %route.endpoint.path,
                "http endpoint blocked by G2 verification gate (503)"
            );
            return DispatchOutcome::VerificationBlocked(reason);
        }
    }

    // 并发上限：try_acquire，拿不到立即 503（不排队，防慢插件拖垮）。
    let sem = dispatcher.semaphore_for(&route).await;
    let _permit = match sem.clone().try_acquire_owned() {
        Ok(p) => p,
        Err(_) => {
            warn!(
                plugin = %route.plugin_id,
                path = %route.endpoint.path,
                "http endpoint concurrency limit reached (503)"
            );
            return DispatchOutcome::ConcurrencyLimited;
        }
    };

    // 单值 query 从多值派生（last-wins，与旧 HashMap 覆盖语义一致）。
    let query: HashMap<String, String> = query_multi
        .iter()
        .filter_map(|(k, vs)| vs.last().map(|v| (k.clone(), v.clone())))
        .collect();

    // 构造 capability RPC 入参：raw_body base64 透传（绝不反序列化）。
    let req = HttpHandleRequest {
        method: method.to_string(),
        path: path.to_string(),
        plugin_id: route.plugin_id.clone(),
        raw_body: base64::engine::general_purpose::STANDARD.encode(&raw_body),
        headers,
        query,
        query_multi,
    };

    // per-endpoint timeout（默认 30000ms）。D1：目标宿主处于 spawn/respawn 窗口
    // 时预算暂停消耗（等待就绪而非 504）——按 [`DISPATCH_POLL_SLICE`] 轮询切片
    // 重估启动态，切片内 starting 则本切片不扣减预算；[`HOST_STARTING_GRACE`]
    // 为追加等待的硬上限（探针异常常开 / 宿主永不就绪也必须有界返回）。无探针
    // 或全程非 starting 时逐切片扣减，与既有单次 timeout 语义逐毫秒等价。
    let base_timeout = std::time::Duration::from_millis(route.timeout_ms());
    let hard_deadline = std::time::Instant::now() + base_timeout + HOST_STARTING_GRACE;
    let mut remaining = base_timeout;
    let mut handle_fut = std::pin::pin!(dispatcher.handler.handle(req));
    let outcome: Result<Result<HttpHandleResponse, String>, ()> = loop {
        let starting = dispatcher
            .startup_probe
            .as_ref()
            .is_some_and(|probe| probe.is_host_starting(&route.plugin_id));
        let slice = if starting {
            DISPATCH_POLL_SLICE
        } else {
            remaining.min(DISPATCH_POLL_SLICE)
        };
        match tokio::time::timeout(slice, handle_fut.as_mut()).await {
            Ok(result) => break Ok(result),
            Err(_elapsed) => {
                if !starting {
                    remaining = remaining.saturating_sub(slice);
                }
                if remaining.is_zero() || std::time::Instant::now() >= hard_deadline {
                    warn!(
                        plugin = %route.plugin_id,
                        path = %route.endpoint.path,
                        timeout_ms = route.timeout_ms(),
                        host_starting = starting,
                        "http endpoint timeout (504)"
                    );
                    break Err(());
                }
            }
        }
    };
    match outcome {
        Ok(Ok(resp)) => DispatchOutcome::Handled(resp),
        Ok(Err(e)) => {
            warn!(
                plugin = %route.plugin_id,
                error = %e,
                "http endpoint handler error (502)"
            );
            DispatchOutcome::HandlerError(e)
        }
        Err(()) => DispatchOutcome::Timeout,
    }
}

/// 把一组 manifest 的 http_endpoints 注册到 registry，聚合所有错误（fail-closed）。
///
/// 设计依据 ADR 命名陷阱治理 D.4 / 附录 E.1.3：冲突/越界 fail-closed，
/// 但**聚合报错而非逐个 panic**——收集所有失败项一次性返回，启动期据此决定是否中止。
///
/// M1：`scopes` 为 Some 时经 guarded 注册并把撤销 guard 登记进 PluginScope。
/// 返回的错误列表（空 = 全部注册成功）。
pub fn register_manifest_http_routes(
    registry: &std::sync::Arc<CapabilityRegistryImpl>,
    manifests: &[PluginManifest],
    scopes: Option<&agentos_plugin_loader::PluginScopeRegistry>,
) -> Vec<String> {
    let mut errors = Vec::new();
    for manifest in manifests {
        let scope = scopes.map(|s| s.scope_of(&manifest.id));
        for ep in &manifest.http_endpoints {
            let result = match &scope {
                Some(s) => registry
                    .register_http_route_guarded(&manifest.id, ep.clone())
                    .map(|(_d, guard)| s.track(guard)),
                None => registry
                    .register_http_route(&manifest.id, ep.clone())
                    .map(|_| ()),
            };
            if let Err(e) = result {
                errors.push(format!("plugin {}: {}", manifest.id, e));
            }
        }
    }
    errors
}

/// 构建带插件 HTTP 端点的 axum 路由树（内核静态路由 + 扫描 http_routes 动态挂载）。
///
/// 由 `build_router`（server.rs）调用。动态挂载的端点统一走 [`dispatch_http`]。
/// 不调用 `.with_state()`——由上层 `build_router` 统一附加 state。
pub fn build_router_with_http_routes(
    state: crate::routes::AppState,
    static_router: axum::Router<crate::routes::AppState>,
) -> axum::Router<crate::routes::AppState> {
    let registry = state.capability_registry.clone();
    let handler = state.http_handler.clone();

    // 至少要有 dispatcher 资源（registry + handler）或插件目录活真源（静态资源）才挂
    // 通配路由。否则保留内核静态路由（兼容 AppState::new() 的旧测试）。
    // G2 注册门禁（全能力面通用化）：遮挡名单查询与 LLM 工具面同源——契约
    // 账本（AppState.contract_states 同一 Arc 实例）现值，复验通过自动恢复。
    let dispatcher: Option<Arc<HttpDispatcher>> = match (registry, handler) {
        (Some(r), Some(h)) => {
            let gate_ledger = state.contract_states.clone();
            Some(Arc::new(
                // D1：注入宿主启动态探针——respawn 冷启窗内端点等待就绪而非 504。
                HttpDispatcher::new(r, h)
                    .with_startup_probe(state.invoker.clone())
                    .with_shadowed_plugin_ids_lookup(Some(Arc::new(move || {
                        gate_ledger.verify_pending_plugin_ids()
                    }))),
            ))
        }
        _ => None,
    };
    let has_static = state.plugin_loader.is_some();
    if dispatcher.is_none() && !has_static {
        return static_router;
    }

    let mut router = static_router;
    // 注册一条 /ext/{*rest} 通配路由，所有 /ext/** 请求统一进 handler。
    // handler 先尝试静态资源（插件目录活真源命中 + 文件存在）→ 命中则直接返回；
    // 否则若有 dispatcher，走 find_http_route 模板匹配插件 http_endpoints；
    // 都不命中：manifest 在册且插件启用（生命周期空窗）→ 503，其余 → 404。
    // 目录解析每请求现取 loader 活路径表（B1：不再闭包捕获 boot 快照）。
    let wildcard_handler = build_wildcard_handler(state.clone(), dispatcher.clone());
    router = router.route("/ext/{*rest}", wildcard_handler);
    // G6-a：/api/v1/datasource/{*rest} 数据源代理——改写 /ext/{rest} 复用同一分发。
    // 前端 fetchDatasourceOptions 对非绝对 URI 走此前缀。
    let datasource_handler = build_datasource_handler(state, dispatcher);
    router = router.route("/api/v1/datasource/{*rest}", datasource_handler);
    router
}

/// 构造 /ext/{*rest} 的 axum handler：先尝试静态资源直读（plugin_dirs 命中），
/// 否则走 dispatcher（find_http_route 模板匹配 → 插件 http.handle）。
///
/// 静态资源优先：`/ext/{plugin_id}/assets/{*rest}` 命中时由内核直接读文件返回，
/// 不进入 dispatcher。这让插件带完整 SPA（分离的 JS/CSS/图片）无需为每个子资源
/// 单独声明 http_endpoints。
/// 身份 scope 内派发（认证端点 task-local 建立，匿名 None 直派）。
///
/// 从 [`exec_ext_request`] 抽出：主函数行数闸（200）与身份链各自内聚。
#[allow(clippy::too_many_arguments)]
async fn dispatch_with_identity(
    auth_identity: Option<agentos_core::types::TenantContext>,
    dispatcher: &Arc<HttpDispatcher>,
    path: &str,
    method_str: &str,
    raw_body: Vec<u8>,
    headers_map: HashMap<String, String>,
    query_multi: HashMap<String, Vec<String>>,
) -> DispatchOutcome {
    let fut = dispatch_http(
        dispatcher,
        path,
        method_str,
        raw_body,
        headers_map,
        query_multi,
    );
    match auth_identity {
        Some(ctx) => agentos_tenant::scope(ctx, fut).await,
        None => fut.await,
    }
}

/// 执行一个 /ext 风格请求（插件 http_endpoints 分发 / 静态资源），供
/// - `/ext/{*rest}` 通配路由
/// - `/api/v1/datasource/{*rest}` 数据源代理（见 build_datasource_handler）
///   共用。path 形如 `/ext/{plugin_id}/...`。
#[allow(clippy::too_many_arguments)]
async fn exec_ext_request(
    state: crate::routes::AppState,
    dispatcher: Option<Arc<HttpDispatcher>>,
    method: axum::http::Method,
    path: String,
    query_multi: HashMap<String, Vec<String>>,
    headers: axum::http::HeaderMap,
    body: axum::body::Bytes,
) -> axum::response::Response {
    use axum::body::Body;
    use axum::http::{HeaderName, HeaderValue, StatusCode};
    use axum::response::IntoResponse;

    // 1) 静态资源直读：命中则直接返回（200 + mime / 404 / 超限 413）。
    //    目录映射每请求现取 loader 活路径表（B1：boot 快照已退役——热发现
    //    插件的静态资源不再有"重启才可见"窗口）。
    let plugin_dirs = state.plugin_dirs();
    if let Some(resp) = try_serve_static_asset(&path, &plugin_dirs).await {
        return resp;
    }

    // 2) 否则走 dispatcher（若有）。无 dispatcher → 404。
    let Some(dispatcher) = dispatcher else {
        return (StatusCode::NOT_FOUND, "route not found").into_response();
    };

    let method_str = method.as_str().to_string();

    // 3) 鉴权闸 + 4) 分发（BUG-58：路由缺席自愈重试；BUG-67 改 additive 语义）。
    //    首次分发 NotFound 且插件声明面在册（manifest 在共享 store + 在启用集）
    //    时，按声明 manifest 自愈补注册缺失路由（heal_plugin_http_routes：
    //    只增不毁——不收回 scope 在册面，防并发自愈/复验互相绞杀），再整体
    //    重试一次；重试时鉴权闸重跑——恢复出的路由携带 auth 声明，鉴权必须以
    //    恢复后的声明为准。重试仍 miss 时按注册面现况裁决：声明端点仍缺 →
    //    503 瞬态语义；声明面完整 → miss 属请求 path/method 与声明面不匹配
    //    → 404（见下方裁决分支）。插件未声明/未启用仍 404（fail-closed 不变）。
    let raw_body = body.to_vec();
    let mut route_healed = false;
    let mut declared = false;
    // HTTP 面身份 scope（反调链第三分支收口，2026-11-06 装机 resume 全堵实证）：
    // 鉴权身份此前只落转发头（x-agentos-*，caller 可读面），task-local 无人
    // 设置 → invoke_tool 的 `_identity` 注入单点（attempt_sidecar_tool 读
    // agentos_tenant::current()）空转 → 插件（task_service 等）内部 capability
    // 反调无身份回带 → 内核派发面身份守卫拒绝（resume_pipeline「调用链未
    // 携带用户身份」，装机 2026-11-02 TaskTool 三连拒同族第三分支）。
    // 认证端点在此建立 task-local scope：`_identity` 单一真值注入点自动携带，
    // 插件手写 user 不可指派的信任模型不变（身份原头=内核鉴权结果）。
    // 匿名端点（auth:none）保持无 scope = 反调无身份，fail-closed 不变。
    let mut auth_identity: Option<agentos_core::types::TenantContext> = None;
    let outcome = loop {
        let mut headers_map = header_map_to_hashmap(&headers);
        if let Some(registry) = state.capability_registry.as_ref() {
            if let Some(route) = registry.find_http_route(&path, &method_str) {
                match enforce_ext_auth(state.store.as_ref(), &headers, &route).await {
                    ExtAuthOutcome::AllowAnonymous => {
                        // A2：匿名放行 ≠ 信任客户端——剥离客户端伪造的身份头
                        //（键集与认证分支覆盖注入严格一致），否则匿名端点的插件
                        // 会把伪造租户/用户/角色当真（越权读写他人租户数据）。
                        for spoofed in ["x-agentos-tenant", "x-agentos-user", "x-agentos-role"] {
                            headers_map.remove(spoofed);
                        }
                        auth_identity = None;
                    }
                    ExtAuthOutcome::Authenticated {
                        user_id,
                        role,
                        tenant_id,
                    } => {
                        headers_map.insert("x-agentos-tenant".to_string(), tenant_id.clone());
                        headers_map.insert("x-agentos-user".to_string(), user_id.clone());
                        headers_map.insert("x-agentos-role".to_string(), role.clone());
                        auth_identity = {
                            let mut ctx =
                                agentos_core::types::TenantContext::new(tenant_id, String::new());
                            ctx.user_id = Some(user_id);
                            ctx.role = Some(role);
                            Some(ctx)
                        };
                    }
                    ExtAuthOutcome::Deny(resp) => return resp,
                }
            }
        }

        // 身份 scope 内派发（含 heal 重试轮——每轮重跑鉴权会刷新 auth_identity）。
        let outcome = dispatch_with_identity(
            auth_identity.clone(),
            &dispatcher,
            &path,
            &method_str,
            raw_body.clone(),
            headers_map,
            query_multi.clone(),
        )
        .await;
        match outcome {
            DispatchOutcome::NotFound => {
                let plugin_id = ext_path_plugin_id(&path);
                declared = match plugin_id {
                    Some(pid) => {
                        state.manifests.read().await.iter().any(|m| m.id == pid)
                            && state.enabled_plugin_ids.read().await.contains(pid)
                    }
                    None => false,
                };
                if declared && !route_healed {
                    route_healed = crate::plugin_lifecycle::heal_plugin_http_routes(
                        state
                            .capability_registry
                            .as_ref()
                            .expect("dispatcher 资源在场"),
                        &state.plugin_scopes,
                        &state.manifests,
                        &state.enabled_plugin_ids,
                        plugin_id.unwrap_or_default(),
                    )
                    .await;
                    if route_healed {
                        continue;
                    }
                }
                break DispatchOutcome::NotFound;
            }
            other => break other,
        }
    };
    match outcome {
        DispatchOutcome::Handled(resp) => {
            // 插件回包任一段落不合法（状态码越界 / body 非 base64）= 上游故障，
            // 回 502 让调用方感知，不得静默改写成 200 空载荷假装成功。
            let status = match StatusCode::from_u16(resp.status) {
                Ok(s) => s,
                Err(_) => {
                    return (
                        StatusCode::BAD_GATEWAY,
                        format!("plugin returned invalid status {}", resp.status),
                    )
                        .into_response()
                }
            };
            let body_bytes = if resp.body.is_empty() {
                Vec::new()
            } else {
                match base64::engine::general_purpose::STANDARD.decode(&resp.body) {
                    Ok(b) => b,
                    Err(e) => {
                        return (
                            StatusCode::BAD_GATEWAY,
                            format!("plugin response body decode failed: {e}"),
                        )
                            .into_response()
                    }
                }
            };
            let mut builder = axum::response::Response::builder().status(status);
            for (k, v) in &resp.headers {
                if let (Ok(name), Ok(val)) = (HeaderName::try_from(k), HeaderValue::try_from(v)) {
                    builder = builder.header(name, val);
                }
            }
            builder.body(Body::from(body_bytes)).unwrap_or_else(|_| {
                (StatusCode::INTERNAL_SERVER_ERROR, "response build failed").into_response()
            })
        }
        DispatchOutcome::NotFound => {
            if declared {
                // 503/404 裁决前复核注册面现况：miss 后自愈重试仍 miss 有两种
                // 成因，处置相反——
                // a) 声明面有缺（manifest 声明的端点仍不在册）= 真·生命周期
                //    空窗/注册面静默丢失（heal 不可达或恢复失败）→ 503+Retry-After
                //    让前端按可重试处理（裸 404 会被前端当「路由没了」常态化报错，
                //    2026-09-04 任务页实测）；
                // b) 声明面完整（全部声明端点都在册）= miss 属请求 path/method
                //    与声明面不匹配（坏调用方 URL/方法），heal 永远无物可补、
                //    重试同路径与重启都不可恢复 → 404。旧语义对这种路径级 miss
                //    也回 503，装机 R297 resume/cancel 永续 503「不自愈」实证
                //    （heal 27/27 幂等命中 = 注册面从未缺失，Restart-After 语义
                //    让调用方把永不恢复的 miss 当瞬态无限重试）。
                // 两分支日志都记 method——路径级 miss 缺 method 无法排障（本次
                // 调查实证盲点：warn 只有 path，调用方方法无从对证）。
                let plugin_id = ext_path_plugin_id(&path).unwrap_or_default();
                let surface_intact = match state
                    .manifests
                    .read()
                    .await
                    .iter()
                    .find(|m| m.id == plugin_id)
                {
                    Some(m) => match state.capability_registry.as_ref() {
                        Some(registry) => {
                            crate::plugin_lifecycle::declared_http_routes_all_present(registry, m)
                        }
                        None => false,
                    },
                    None => false,
                };
                if surface_intact {
                    tracing::info!(
                        path = %path,
                        method = %method_str,
                        plugin = %plugin_id,
                        "ext route 404: declared surface intact, path/method unmatched"
                    );
                    return (StatusCode::NOT_FOUND, "route not found").into_response();
                }
                warn!(
                    path = %path,
                    method = %method_str,
                    "http route missing for declared plugin even after heal attempt (503, transient lifecycle window)"
                );
                return (
                    StatusCode::SERVICE_UNAVAILABLE,
                    [("retry-after", "1")],
                    "plugin route temporarily unavailable (transient)",
                )
                    .into_response();
            }
            // 插件未在册（manifest store 查无 / 未启用）→ 404（fail-closed 不变）。
            // 此分支零日志曾是排障盲区：波次 manifest 面重建亚秒窗内撞上的请求
            // 连痕迹都不留（2026-10-15 酒馆面板 404 实证）——warn 带 path/method、
            // 解析出的插件 id 与 heal 尝试痕迹（heal_attempted=true = 请求时声明
            // 面曾在、heal 后 manifest 面又被摘的中间态）。
            warn!(
                path = %path,
                method = %method_str,
                plugin = %ext_path_plugin_id(&path).unwrap_or_default(),
                heal_attempted = route_healed,
                "ext route 404: plugin not in manifest store or not enabled"
            );
            (StatusCode::NOT_FOUND, "route not found").into_response()
        }
        DispatchOutcome::Timeout => {
            (StatusCode::GATEWAY_TIMEOUT, "endpoint timeout").into_response()
        }
        DispatchOutcome::ConcurrencyLimited => {
            (StatusCode::SERVICE_UNAVAILABLE, "concurrency limit").into_response()
        }
        // 校验门禁拦截（503 服务不可用类，与并发超限同形态）：原因文案随 body
        // 出走（点名插件 + 复验恢复路径），禁静默。
        DispatchOutcome::VerificationBlocked(reason) => {
            (StatusCode::SERVICE_UNAVAILABLE, reason).into_response()
        }
        DispatchOutcome::HandlerError(msg) => (StatusCode::BAD_GATEWAY, msg).into_response(),
    }
}

/// /ext 端点鉴权闸（D2 两刀均落地）的裁决结果。
///
/// - `AllowAnonymous`：manifest 显式声明 `auth:"none"`（webhook 验签自管等），全放行；
/// - `Authenticated`：声明 `user`/`admin` 且 token 校验通过，携带解析出的身份
///   （调用方注入转发头给插件）；
/// - `Deny`：拒绝（401 未认证 / 403 角色不足），响应直接回写。
enum ExtAuthOutcome {
    AllowAnonymous,
    Authenticated {
        user_id: String,
        role: String,
        tenant_id: String,
    },
    Deny(axum::response::Response),
}

/// /ext 端点鉴权闸（D2 两刀均落地）：执行 manifest `http_endpoints[].auth` 声明。
///
/// 语义：
/// - `user`：与 /api/v1 管理面同一 token 校验（`resolve_request_user` 单一实现：
///   HMAC 验签/过期/类型/口令绑定/用户存在性）；
/// - `admin`：同上 + admin 角色（不足 403）；
/// - `none`：显式匿名白名单（webhook 验签自管等），全方法放行；
/// - 无声明：一律 401 fail-closed（读写同规——读面数据（监控快照/trace/配置）
///   与写面同样敏感，缺省即拒绝，插件要匿名或鉴权必须显式声明）。
async fn enforce_ext_auth(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    headers: &axum::http::HeaderMap,
    route: &HttpRouteDescriptor,
) -> ExtAuthOutcome {
    use axum::response::IntoResponse;

    let declared = route.endpoint.auth.as_deref();
    match declared {
        Some("none") => ExtAuthOutcome::AllowAnonymous,
        Some(declared) => {
            let (user_id, _, role, tenant_id) =
                match agentos_http::auth::resolve_request_user(store, headers).await {
                    Ok(user) => user,
                    Err(e) => return ExtAuthOutcome::Deny(e.into_response()),
                };
            if declared == "admin" && role != "admin" {
                return ExtAuthOutcome::Deny(
                    agentos_http::error::ApiError::Forbidden {
                        message: "该端点需要 admin 角色".to_string(),
                    }
                    .into_response(),
                );
            }
            ExtAuthOutcome::Authenticated {
                user_id,
                role,
                tenant_id,
            }
        }
        None => ExtAuthOutcome::Deny(
            agentos_http::error::ApiError::Unauthorized {
                message: "端点未声明 auth，默认拒绝（fail-closed）".to_string(),
            }
            .into_response(),
        ),
    }
}

fn build_wildcard_handler(
    state: crate::routes::AppState,
    dispatcher: Option<Arc<HttpDispatcher>>,
) -> axum::routing::MethodRouter<crate::routes::AppState> {
    use axum::http::{HeaderMap, Method};

    // axum::routing::any 注册——任何 method 都走同一 handler。
    // query 多值解析统一走 [`parse_query_multi`]（重复 key 不塌缩，A1）。
    let handler =
        move |method: Method, uri: axum::http::Uri, headers: HeaderMap, body: axum::body::Bytes| {
            let state = state.clone();
            let dispatcher = dispatcher.clone();
            async move {
                let path = uri.path().to_string();
                let query_multi = parse_query_multi(&uri);
                exec_ext_request(state, dispatcher, method, path, query_multi, headers, body).await
            }
        };

    axum::routing::any(handler)
        // 请求体上限显式钉死（曾在 axum 隐式默认 2 MiB，事故实证后转正）。
        .layer(axum::extract::DefaultBodyLimit::max(EXT_BODY_LIMIT_BYTES))
}

/// 从 `/ext/{plugin_id}/...` 提取插件 id 段（非 /ext 前缀或空段返回 None）。
fn ext_path_plugin_id(path: &str) -> Option<&str> {
    path.strip_prefix("/ext/")
        .and_then(|rest| rest.split('/').next())
        .filter(|s| !s.is_empty())
}

/// 解析 URI query 为多值形态（重复 key 不塌缩）。
///
/// serde_urlencoded 的 Query 提取器对重复 key 直接报错（duplicate field → 400），
/// 不支持 `HashMap<String, Vec<String>>` 多值收集；form_urlencoded 逐对解析天然
/// 保序全量（filter=a&filter=b → filter: [a, b]，不塌缩）。单值 `query`（last-wins）
/// 由此派生，保证 `query[k] == query_multi[k].last()`。
fn parse_query_multi(uri: &axum::http::Uri) -> HashMap<String, Vec<String>> {
    uri.query()
        .map(|q| {
            let mut m: HashMap<String, Vec<String>> = HashMap::new();
            for (k, v) in form_urlencoded::parse(q.as_bytes()) {
                m.entry(k.to_string()).or_default().push(v.to_string());
            }
            m
        })
        .unwrap_or_default()
}

/// 构造 `/api/v1/datasource/{*rest}` 数据源代理（G6-a：datasource 占位转真实路由）。
///
/// 语义：`{rest}` 为插件数据源标识，形如 `/ext/{plugin_id}/{route_id}` 的短路径——
/// 把 `/api/v1/datasource/ext/{plugin_id}/{route_id}` 改写为 `/ext/{...}` 复用
/// [`exec_ext_request`]（插件 http_endpoints 分发，选项形状由插件决定）；
/// 兼容短形式 `/api/v1/datasource/{route_id}` 时按 `/ext/{route_id}` 处理。
/// 未命中 → 404（前端 datasource 占位护栏由真实路由接管后移除）。
fn build_datasource_handler(
    state: crate::routes::AppState,
    dispatcher: Option<Arc<HttpDispatcher>>,
) -> axum::routing::MethodRouter<crate::routes::AppState> {
    use axum::http::{HeaderMap, Method, Uri};

    let handler = move |method: Method, uri: Uri, headers: HeaderMap, body: axum::body::Bytes| {
        let state = state.clone();
        let dispatcher = dispatcher.clone();
        async move {
            let rest = uri.path().trim_start_matches("/api/v1/datasource");
            let rest = rest.trim_start_matches('/');
            let ext_path = if rest.starts_with("ext/") {
                format!("/{rest}")
            } else {
                format!("/ext/{rest}")
            };
            let query_multi = parse_query_multi(&uri);
            exec_ext_request(
                state,
                dispatcher,
                method,
                ext_path,
                query_multi,
                headers,
                body,
            )
            .await
        }
    };
    axum::routing::any(handler)
        // 请求体上限显式钉死（曾在 axum 隐式默认 2 MiB，事故实证后转正）。
        .layer(axum::extract::DefaultBodyLimit::max(EXT_BODY_LIMIT_BYTES))
}

pub mod static_assets;

pub(crate) use static_assets::mime_for_extension;
use static_assets::{header_map_to_hashmap, try_serve_static_asset};
#[cfg(test)]
use static_assets::{
    manifest_path_to_axum, read_static_asset_capped, StaticAssetReadError, STATIC_ASSET_MAX_BYTES,
};
/// 生产环境 HTTP 处理能力实现：经 [`PluginInvoker`] 把入站请求交给插件 `http.handle`。
///
/// dispatcher 据路由查到 `plugin_id` 后填入 [`HttpHandleRequest::plugin_id`]，
/// 本实现据此调 `invoker.invoke_tool(plugin_id, "http.handle", ...)`（透明走
/// InProcess 或 sidecar MCP），把返回的 `{status,headers,body,body_encoding}` 解析为
/// [`HttpHandleResponse`]。
///
/// 企微真实回调（真实 corp_id/AES 回包）需插件代码配合 + 部署后验证；本实现只负责
/// 把 raw body/headers/query 字节级透传到插件，验签/解密/加密由插件（Python）完成。
// ── 生产实现：经 PluginInvoker 调插件 http.handle（sidecar MCP / InProcess 透明分发）──
pub struct SidecarHttpHandler {
    invoker: Arc<dyn agentos_core::traits::PluginInvoker>,
}

impl SidecarHttpHandler {
    /// 创建生产 handler。
    pub fn new(invoker: Arc<dyn agentos_core::traits::PluginInvoker>) -> Self {
        Self { invoker }
    }
}

#[async_trait::async_trait]
impl HttpHandleCapability for SidecarHttpHandler {
    async fn handle(&self, req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        // 构造 capability RPC 入参（raw_body base64 透传，绝不反序列化）
        let inputs = serde_json::to_value(&req).map_err(|e| e.to_string())?;
        let result = self
            .invoker
            .invoke_tool(&req.plugin_id, "http.handle", &inputs)
            .await
            .map_err(|e| e.message)?;
        if !result.success {
            return Err(result.error.unwrap_or_else(|| "unknown error".to_string()));
        }
        // 插件返回 {status,headers,body,body_encoding}
        serde_json::from_value::<HttpHandleResponse>(result.data)
            .map_err(|e| format!("invalid http.handle response shape: {e}"))
    }
}

#[cfg(test)]
mod tests;
