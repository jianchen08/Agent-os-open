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
    HttpRouteDescriptor, PluginManifest,
};
use agentos_plugin_loader::CapabilityRegistryImpl;
use base64::Engine;
use tracing::warn;

/// per-endpoint 并发许可信号量（max_concurrency 上限保护）。
type Semaphore = tokio::sync::Semaphore;

/// HTTP dispatcher：路由分发 + timeout + 并发上限。
///
/// 持有能力注册表（查路由）与一个 [`HttpHandleCapability`]（调插件 http.handle）。
/// 生产实现走 sidecar MCP；测试用进程内实现验证透传链路。
pub struct HttpDispatcher {
    registry: Arc<CapabilityRegistryImpl>,
    handler: Arc<dyn HttpHandleCapability>,
    /// RouteKey(path+method) → 并发信号量，惰性创建。
    semaphores: tokio::sync::Mutex<HashMap<(String, String), Arc<Semaphore>>>,
}

impl HttpDispatcher {
    /// 创建 dispatcher。
    pub fn new(
        registry: Arc<CapabilityRegistryImpl>,
        handler: Arc<dyn HttpHandleCapability>,
    ) -> Self {
        Self {
            registry,
            handler,
            semaphores: tokio::sync::Mutex::new(HashMap::new()),
        }
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

    // per-endpoint timeout（默认 30000ms）。
    let timeout = std::time::Duration::from_millis(route.timeout_ms());
    let handle_fut = dispatcher.handler.handle(req);
    match tokio::time::timeout(timeout, handle_fut).await {
        Ok(Ok(resp)) => DispatchOutcome::Handled(resp),
        Ok(Err(e)) => {
            warn!(
                plugin = %route.plugin_id,
                error = %e,
                "http endpoint handler error (502)"
            );
            DispatchOutcome::HandlerError(e)
        }
        Err(_) => {
            warn!(
                plugin = %route.plugin_id,
                timeout_ms = route.timeout_ms(),
                "http endpoint timeout (504)"
            );
            DispatchOutcome::Timeout
        }
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
    let plugin_dirs = state.plugin_dirs.clone();

    // 至少要有 dispatcher 资源（registry + handler）或 plugin_dirs（静态资源）才挂通配路由。
    // 否则保留内核静态路由（兼容 AppState::new() 的旧测试）。
    let dispatcher: Option<Arc<HttpDispatcher>> = match (registry, handler) {
        (Some(r), Some(h)) => Some(Arc::new(HttpDispatcher::new(r, h))),
        _ => None,
    };
    let has_static = !plugin_dirs.is_empty();
    if dispatcher.is_none() && !has_static {
        return static_router;
    }

    let mut router = static_router;
    // 注册一条 /ext/{*rest} 通配路由，所有 /ext/** 请求统一进 handler。
    // handler 先尝试静态资源（plugin_dirs 命中 + 文件存在）→ 命中则直接返回；
    // 否则若有 dispatcher，走 find_http_route 模板匹配插件 http_endpoints；
    // 都不命中：manifest 在册且插件启用（生命周期空窗）→ 503，其余 → 404。
    let wildcard_handler =
        build_wildcard_handler(state.clone(), dispatcher.clone(), plugin_dirs.clone());
    router = router.route("/ext/{*rest}", wildcard_handler);
    // G6-a：/api/v1/datasource/{*rest} 数据源代理——改写 /ext/{rest} 复用同一分发。
    // 前端 fetchDatasourceOptions 对非绝对 URI 走此前缀。
    let datasource_handler = build_datasource_handler(state, dispatcher, plugin_dirs);
    router = router.route("/api/v1/datasource/{*rest}", datasource_handler);
    router
}

/// 构造 /ext/{*rest} 的 axum handler：先尝试静态资源直读（plugin_dirs 命中），
/// 否则走 dispatcher（find_http_route 模板匹配 → 插件 http.handle）。
///
/// 静态资源优先：`/ext/{plugin_id}/assets/{*rest}` 命中时由内核直接读文件返回，
/// 不进入 dispatcher。这让插件带完整 SPA（分离的 JS/CSS/图片）无需为每个子资源
/// 单独声明 http_endpoints。
/// 执行一个 /ext 风格请求（插件 http_endpoints 分发 / 静态资源），供
/// - `/ext/{*rest}` 通配路由
/// - `/api/v1/datasource/{*rest}` 数据源代理（见 build_datasource_handler）
///   共用。path 形如 `/ext/{plugin_id}/...`。
#[allow(clippy::too_many_arguments)]
async fn exec_ext_request(
    state: crate::routes::AppState,
    dispatcher: Option<Arc<HttpDispatcher>>,
    plugin_dirs: Arc<HashMap<String, std::path::PathBuf>>,
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
    if let Some(resp) = try_serve_static_asset(&path, &plugin_dirs).await {
        return resp;
    }

    // 2) 否则走 dispatcher（若有）。无 dispatcher → 404。
    let Some(dispatcher) = dispatcher else {
        return (StatusCode::NOT_FOUND, "route not found").into_response();
    };

    let method_str = method.as_str().to_string();
    let mut headers_map = header_map_to_hashmap(&headers);

    // 3) 鉴权闸（D2 两刀均落地）：执行 manifest `http_endpoints[].auth` 声明。
    //    静态资源（/assets/**）不声明 http_endpoints，不受此闸约束。
    //    已认证（user/admin 声明）时注入身份头（X-AgentOS-Tenant / User / Role，
    //    值来自已验签 token 解析的用户记录）——插件侧租户过滤/归属校验只信
    //    这组内核注入头，不自行解析 token（HMAC 密钥不出内核）。
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
                }
                ExtAuthOutcome::Authenticated {
                    user_id,
                    role,
                    tenant_id,
                } => {
                    headers_map.insert("x-agentos-tenant".to_string(), tenant_id);
                    headers_map.insert("x-agentos-user".to_string(), user_id);
                    headers_map.insert("x-agentos-role".to_string(), role);
                }
                ExtAuthOutcome::Deny(resp) => return resp,
            }
        }
    }

    let raw_body = body.to_vec();
    let outcome = dispatch_http(
        &dispatcher,
        &path,
        &method_str,
        raw_body,
        headers_map,
        query_multi,
    )
    .await;
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
            // 声明面在册（manifest 已知且插件启用）但路由缺席 = 插件生命周期空窗
            // （G2 复验重注册 / enable-disable 切换 / 空闲卸载后的再注册间隙，
            // scopes.revoke 与重注册之间静默无日志）——瞬态不可用而非路由不存在：
            // 503+Retry-After 让前端按可重试处理（裸 404 会被前端当「路由没了」
            // 常态化报错，2026-09-04 任务页实测反复 404）；warn 补上空窗观测。
            let plugin_id = ext_path_plugin_id(&path);
            let declared = match plugin_id {
                Some(pid) => {
                    state.manifests.read().await.iter().any(|m| m.id == pid)
                        && state.enabled_plugin_ids.read().await.contains(pid)
                }
                None => false,
            };
            if declared {
                warn!(
                    path = %path,
                    plugin = plugin_id.unwrap_or_default(),
                    "http route missing for declared plugin (503, transient lifecycle window)"
                );
                return (
                    StatusCode::SERVICE_UNAVAILABLE,
                    [("retry-after", "1")],
                    "plugin route temporarily unavailable (transient)",
                )
                    .into_response();
            }
            (StatusCode::NOT_FOUND, "route not found").into_response()
        }
        DispatchOutcome::Timeout => {
            (StatusCode::GATEWAY_TIMEOUT, "endpoint timeout").into_response()
        }
        DispatchOutcome::ConcurrencyLimited => {
            (StatusCode::SERVICE_UNAVAILABLE, "concurrency limit").into_response()
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
    plugin_dirs: Arc<HashMap<String, std::path::PathBuf>>,
) -> axum::routing::MethodRouter<crate::routes::AppState> {
    use axum::http::{HeaderMap, Method};

    // axum::routing::any 注册——任何 method 都走同一 handler。
    // query 多值解析统一走 [`parse_query_multi`]（重复 key 不塌缩，A1）。
    let handler =
        move |method: Method, uri: axum::http::Uri, headers: HeaderMap, body: axum::body::Bytes| {
            let state = state.clone();
            let dispatcher = dispatcher.clone();
            let plugin_dirs = plugin_dirs.clone();
            async move {
                let path = uri.path().to_string();
                let query_multi = parse_query_multi(&uri);
                exec_ext_request(
                    state,
                    dispatcher,
                    plugin_dirs,
                    method,
                    path,
                    query_multi,
                    headers,
                    body,
                )
                .await
            }
        };

    axum::routing::any(handler)
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
    plugin_dirs: Arc<HashMap<String, std::path::PathBuf>>,
) -> axum::routing::MethodRouter<crate::routes::AppState> {
    use axum::http::{HeaderMap, Method, Uri};

    let handler = move |method: Method, uri: Uri, headers: HeaderMap, body: axum::body::Bytes| {
        let state = state.clone();
        let dispatcher = dispatcher.clone();
        let plugin_dirs = plugin_dirs.clone();
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
                plugin_dirs,
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
}

/// 静态资源单文件大小上限（字节）：超限直接 413 拒绝，不整读入内存。
/// 插件前端资源（JS/CSS/图片）正常远小于此；上限只挡异常巨物（误放文件/
/// 恶意构造），同时约束单请求读盘的内存峰值。
const STATIC_ASSET_MAX_BYTES: u64 = 8 * 1024 * 1024;

/// 尝试把 `/ext/{plugin_id}/assets/{*rest}` 解析为插件 `web/` 子目录下的文件并直读返回。
///
/// 返回：
/// - `Some(response)` —— 路径形态匹配静态资源（无论命中/未命中/被拒），由调用方直接返回；
/// - `None` —— 不是静态资源路径形态，或该插件未声明目录，调用方应继续走 dispatcher。
///
/// 路径安全：
/// - 拒绝 `..` 段；
/// - canonicalize 后必须仍在插件 `web/` 子树内（防 symlink 逃逸）。
///
/// 文件读取契约：整读移出 async 执行器（`spawn_blocking`），且先 stat 校验
/// [`STATIC_ASSET_MAX_BYTES`] 上限——超限 413 拒绝，不整读超限文件。
async fn try_serve_static_asset(
    path: &str,
    plugin_dirs: &HashMap<String, std::path::PathBuf>,
) -> Option<axum::response::Response> {
    use axum::body::Body;
    use axum::http::{HeaderName, HeaderValue, StatusCode};
    use axum::response::IntoResponse;

    // 解析 /ext/{plugin_id}/assets/{rest}。
    let stripped = path.strip_prefix("/ext/")?;
    let mut iter = stripped.splitn(3, '/');
    let plugin_id = iter.next()?;
    let mid = iter.next()?;
    if mid != "assets" {
        return None; // 非 assets 子路径：交回 dispatcher
    }
    let rest = iter.next().unwrap_or("");
    // 空路径（/ext/{plugin}/assets 或 /ext/{plugin}/assets/）：交回 dispatcher，
    // 让插件自己决定要不要给一个 http_endpoint 处理目录索引。
    if rest.is_empty() {
        return None;
    }

    let plugin_dir = plugin_dirs.get(plugin_id)?;
    // 路径安全：先拒 .. 段（canonicalize 之前），防止 ./.. 逃逸。
    for seg in rest.split('/') {
        if seg == ".." {
            return Some((StatusCode::NOT_FOUND, "not found").into_response());
        }
    }
    let web_root = plugin_dir.join("web");
    // web/ 不存在 → 此插件不托管静态资源，交回 dispatcher。
    if !web_root.exists() {
        return None;
    }

    let file_path = web_root.join(rest);

    // canonicalize 防 symlink 逃逸：file 必须在 web_root 子树内。
    let canonical_web = match std::fs::canonicalize(&web_root) {
        Ok(p) => p,
        Err(_) => return None,
    };
    let canonical_file = match std::fs::canonicalize(&file_path) {
        Ok(p) => p,
        Err(_) => {
            // 文件不存在 → 此路径形态确实归静态资源所有，直接 404（不交回 dispatcher，
            // 否则 dispatcher 也会 404，但语义上 /assets/** 是静态资源命名空间）。
            return Some((StatusCode::NOT_FOUND, "not found").into_response());
        }
    };
    if !canonical_file.starts_with(&canonical_web) || !canonical_file.is_file() {
        return Some((StatusCode::NOT_FOUND, "not found").into_response());
    }

    // 整读在阻塞线程执行（async 执行器上不做同步磁盘 IO），先 stat 校验
    // 上限——超限 413 拒绝，不整读超限文件。
    let read_path = canonical_file.clone();
    let read = tokio::task::spawn_blocking(move || read_static_asset_capped(&read_path)).await;
    let bytes = match read {
        Ok(Ok(bytes)) => bytes,
        Ok(Err(StaticAssetReadError::TooLarge)) => {
            return Some(
                (
                    StatusCode::PAYLOAD_TOO_LARGE,
                    "static asset exceeds size limit",
                )
                    .into_response(),
            );
        }
        Ok(Err(StaticAssetReadError::Io)) => {
            return Some((StatusCode::NOT_FOUND, "not found").into_response());
        }
        Err(_) => {
            return Some(
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "static asset read task failed",
                )
                    .into_response(),
            );
        }
    };

    let mime = mime_for_extension(
        canonical_file
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or(""),
    );

    let mut builder = axum::response::Response::builder().status(StatusCode::OK);
    if let (Ok(name), Ok(val)) = (
        HeaderName::try_from("content-type"),
        HeaderValue::try_from(mime),
    ) {
        builder = builder.header(name, val);
    }
    // 静态资源通常可缓存；这里给一个保守的 no-cache（开发期 SPA 热更新友好）。
    if let (Ok(name), Ok(val)) = (
        HeaderName::try_from("cache-control"),
        HeaderValue::try_from("no-cache"),
    ) {
        builder = builder.header(name, val);
    }
    Some(builder.body(Body::from(bytes)).unwrap_or_else(|_| {
        (StatusCode::INTERNAL_SERVER_ERROR, "response build failed").into_response()
    }))
}

/// 单文件读取结果：超限 / IO 失败分立（分别映射 413 / 404；IO 细节按既有
/// 契约不外泄——读失败统一 404）。
enum StaticAssetReadError {
    TooLarge,
    Io,
}

/// 阻塞线程内的静态资源读取：先 stat 校验大小上限，再整读。
fn read_static_asset_capped(path: &std::path::Path) -> Result<Vec<u8>, StaticAssetReadError> {
    let len = match std::fs::metadata(path) {
        Ok(m) => m.len(),
        Err(_) => return Err(StaticAssetReadError::Io),
    };
    if len > STATIC_ASSET_MAX_BYTES {
        return Err(StaticAssetReadError::TooLarge);
    }
    std::fs::read(path).map_err(|_| StaticAssetReadError::Io)
}

/// 扩展名 → Content-Type 映射表（覆盖常见 web 资源类型）。
///
/// 未命中扩展名统一回退到 `application/octet-stream`（浏览器嗅探可识别大部分文本）。
/// 不引入 mime crate —— 任务范围控制依赖膨胀，常见 web 类型手写映射足够。
/// 单一来源（2026-08-24 合并）：`/uploads`（routes.rs）与 `/ext/{plugin}/assets`
/// 两处静态资源出口共用本映射，扩展名集合取两处并集。
pub(crate) fn mime_for_extension(ext: &str) -> &'static str {
    match ext.to_ascii_lowercase().as_str() {
        "html" | "htm" => "text/html; charset=utf-8",
        "js" | "mjs" => "application/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" => "application/json; charset=utf-8",
        "map" => "application/json",
        "txt" | "md" => "text/plain; charset=utf-8",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "avif" => "image/avif",
        "ico" => "image/x-icon",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "ttf" => "font/ttf",
        "otf" => "font/otf",
        "eot" => "application/vnd.ms-fontobject",
        "wasm" => "application/wasm",
        "pdf" => "application/pdf",
        "xml" => "application/xml; charset=utf-8",
        "mp4" => "video/mp4",
        "webm" => "video/webm",
        "mp3" => "audio/mpeg",
        "wav" => "audio/wav",
        "m4a" => "audio/mp4",
        "ogg" => "audio/ogg",
        _ => "application/octet-stream",
    }
}

/// 把 manifest 路径约定（{param}/{param:path}）转成 axum 0.8 路径参数语法（:param/*param）。
///
/// - `{name}` → `:name`（单段捕获）
/// - `{name:path}` → `*name`（剩余多段通配，axum 0.8 用 `*` 前缀）
/// - 其他段原样保留。
#[cfg(test)]
fn manifest_path_to_axum(manifest_path: &str) -> String {
    manifest_path
        .split('/')
        .map(|seg| {
            if seg.starts_with('{') && seg.ends_with('}') {
                let inner = &seg[1..seg.len() - 1]; // 去掉 { }
                if let Some(name) = inner.strip_suffix(":path") {
                    format!("{{*{name}}}") // axum 0.8 多段通配语法 {*name}
                } else {
                    format!("{{{inner}}}") // axum 0.8 单段捕获语法 {name}
                }
            } else {
                seg.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join("/")
}

/// HeaderMap → HashMap<String,String>（多值取首个，key 转小写）。
fn header_map_to_hashmap(headers: &axum::http::HeaderMap) -> HashMap<String, String> {
    let mut map = HashMap::new();
    for (k, v) in headers.iter() {
        let key = k.as_str().to_lowercase();
        if let Ok(val) = v.to_str() {
            map.entry(key).or_insert_with(|| val.to_string());
        }
    }
    map
}

// ── 生产实现：经 PluginInvoker 调插件 http.handle（sidecar MCP / InProcess 透明分发）──

/// 生产环境 HTTP 处理能力实现：经 [`PluginInvoker`] 把入站请求交给插件 `http.handle`。
///
/// dispatcher 据路由查到 `plugin_id` 后填入 [`HttpHandleRequest::plugin_id`]，
/// 本实现据此调 `invoker.invoke_tool(plugin_id, "http.handle", ...)`（透明走
/// InProcess 或 sidecar MCP），把返回的 `{status,headers,body,body_encoding}` 解析为
/// [`HttpHandleResponse`]。
///
/// 企微真实回调（真实 corp_id/AES 回包）需插件代码配合 + 部署后验证；本实现只负责
/// 把 raw body/headers/query 字节级透传到插件，验签/解密/加密由插件（Python）完成。
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
mod tests {
    use super::*;

    #[test]
    fn test_manifest_path_to_axum_static() {
        assert_eq!(manifest_path_to_axum("/ext/p/llm"), "/ext/p/llm");
        assert_eq!(
            manifest_path_to_axum("/ext/p/llm/defaults"),
            "/ext/p/llm/defaults"
        );
    }

    #[test]
    fn test_manifest_path_to_axum_single_param() {
        // {model_id} → {model_id}（axum 0.8 单段捕获，同名语法）
        assert_eq!(
            manifest_path_to_axum("/ext/p/models/{model_id}"),
            "/ext/p/models/{model_id}"
        );
        assert_eq!(
            manifest_path_to_axum("/ext/p/providers/{provider_id}"),
            "/ext/p/providers/{provider_id}"
        );
    }

    #[test]
    fn test_manifest_path_to_axum_catchall_param() {
        // {config_path:path} → {*config_path}（axum 0.8 多段通配语法）
        assert_eq!(
            manifest_path_to_axum("/ext/p/generic/{config_path:path}"),
            "/ext/p/generic/{*config_path}"
        );
    }

    /// 建一个带 web/ 子目录的插件目录桩：plugin_id → 插件目录。
    fn plugin_dirs_with_web(dir: &tempfile::TempDir) -> HashMap<String, std::path::PathBuf> {
        let web = dir.path().join("web");
        std::fs::create_dir_all(&web).expect("create web dir");
        let mut map = HashMap::new();
        map.insert("p".to_string(), dir.path().to_path_buf());
        map
    }

    fn response_status(resp: &axum::response::Response) -> axum::http::StatusCode {
        resp.status()
    }

    #[tokio::test]
    async fn static_asset_serves_small_file_with_mime() {
        let dir = tempfile::tempdir().expect("tempdir");
        let dirs = plugin_dirs_with_web(&dir);
        std::fs::write(dir.path().join("web/app.js"), b"console.log(1);").unwrap();

        let resp = try_serve_static_asset("/ext/p/assets/app.js", &dirs)
            .await
            .expect("路径形态匹配必须返回响应");
        assert_eq!(response_status(&resp), axum::http::StatusCode::OK);
        let headers = resp.headers();
        assert_eq!(
            headers.get("content-type").unwrap(),
            "application/javascript; charset=utf-8"
        );
        let body = axum::body::to_bytes(resp.into_body(), 1 << 20)
            .await
            .unwrap();
        assert_eq!(&body[..], b"console.log(1);");
    }

    #[tokio::test]
    async fn static_asset_oversize_file_rejected_with_413() {
        let dir = tempfile::tempdir().expect("tempdir");
        let dirs = plugin_dirs_with_web(&dir);
        // 上限 + 1 字节：超限文件必须被拒绝（不整读入内存）。
        let oversize = vec![b'x'; STATIC_ASSET_MAX_BYTES as usize + 1];
        std::fs::write(dir.path().join("web/big.bin"), oversize).unwrap();

        let resp = try_serve_static_asset("/ext/p/assets/big.bin", &dirs)
            .await
            .expect("路径形态匹配必须返回响应");
        assert_eq!(
            response_status(&resp),
            axum::http::StatusCode::PAYLOAD_TOO_LARGE,
            "超限静态资源必须 413 拒绝"
        );
    }

    #[tokio::test]
    async fn static_asset_file_at_limit_is_served() {
        // 边界区分度：恰好等于上限（≤ 上限）必须正常返回，与超限用例形成
        // 边界两侧断言。
        let dir = tempfile::tempdir().expect("tempdir");
        let dirs = plugin_dirs_with_web(&dir);
        let at_limit = vec![b'a'; STATIC_ASSET_MAX_BYTES as usize];
        std::fs::write(dir.path().join("web/edge.bin"), at_limit).unwrap();

        let resp = try_serve_static_asset("/ext/p/assets/edge.bin", &dirs)
            .await
            .expect("路径形态匹配必须返回响应");
        assert_eq!(
            response_status(&resp),
            axum::http::StatusCode::OK,
            "恰好等于上限的文件必须正常返回（上限为闭区间）"
        );
    }
}
