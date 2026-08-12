//! HTTP 端点 dispatcher（ADR §3.3 / 附录 E.1.2 / E.1.3）。
//!
//! 把入站 HTTP 请求路由到插件贡献的端点：
//! 1. 按 path+method 查 [`CapabilityRegistry`] 的 `http_routes`；
//! 2. 经 [`HttpHandleCapability`] 把 raw body(base64) + 全量 headers + query
//!    透传给插件 `http.handle`（**绝不反序列化 body 再转发**——企微 SHA1 验签 + AES 解密吃 raw body）；
//! 3. 插件返回 `{status, headers, body, body_encoding}`，dispatcher 原样回写（**插件全权控制响应**）；
//! 4. per-endpoint `timeout_ms`（默认 30000）超时返回 504；
//!    per-endpoint `max_concurrency`（默认 16）超限返回 503。
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
    pub fn new(registry: Arc<CapabilityRegistryImpl>, handler: Arc<dyn HttpHandleCapability>) -> Self {
        Self {
            registry,
            handler,
            semaphores: tokio::sync::Mutex::new(HashMap::new()),
        }
    }

    /// 获取（或创建）某路由的并发信号量。
    async fn semaphore_for(&self, route: &HttpRouteDescriptor) -> Arc<Semaphore> {
        let key = (
            route.endpoint.path.clone(),
            route.endpoint.method.clone(),
        );
        let mut sems = self.semaphores.lock().await;
        sems.entry(key)
            .or_insert_with(|| {
                Arc::new(Semaphore::new(route.max_concurrency() as usize))
            })
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
/// * `headers` / `query` - 全量 headers 与查询参数
pub async fn dispatch_http(
    dispatcher: &HttpDispatcher,
    path: &str,
    method: &str,
    raw_body: Vec<u8>,
    headers: HashMap<String, String>,
    query: HashMap<String, String>,
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

    // 构造 capability RPC 入参：raw_body base64 透传（绝不反序列化）。
    let req = HttpHandleRequest {
        method: method.to_string(),
        path: path.to_string(),
        plugin_id: route.plugin_id.clone(),
        raw_body: base64::engine::general_purpose::STANDARD.encode(&raw_body),
        headers,
        query,
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
/// 返回的错误列表（空 = 全部注册成功）。
pub fn register_manifest_http_routes(
    registry: &CapabilityRegistryImpl,
    manifests: &[PluginManifest],
) -> Vec<String> {
    let mut errors = Vec::new();
    for manifest in manifests {
        for ep in &manifest.http_endpoints {
            if let Err(e) = registry.register_http_route(&manifest.id, ep.clone()) {
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
    // 都不命中 → 404。
    let wildcard_handler = build_wildcard_handler(dispatcher, plugin_dirs);
    router = router.route("/ext/{*rest}", wildcard_handler);
    router
}

/// 构造 /ext/{*rest} 的 axum handler：先尝试静态资源直读（plugin_dirs 命中），
/// 否则走 dispatcher（find_http_route 模板匹配 → 插件 http.handle）。
///
/// 静态资源优先：`/ext/{plugin_id}/assets/{*rest}` 命中时由内核直接读文件返回，
/// 不进入 dispatcher。这让插件带完整 SPA（分离的 JS/CSS/图片）无需为每个子资源
/// 单独声明 http_endpoints。
fn build_wildcard_handler(
    dispatcher: Option<Arc<HttpDispatcher>>,
    plugin_dirs: Arc<HashMap<String, std::path::PathBuf>>,
) -> axum::routing::MethodRouter<crate::routes::AppState> {
    use axum::body::Body;
    use axum::http::{HeaderMap, HeaderName, HeaderValue, Method, StatusCode};
    use axum::response::IntoResponse;

    // axum::routing::any 注册——任何 method 都走同一 handler。
    let handler = move |method: Method,
                         uri: axum::http::Uri,
                         headers: HeaderMap,
                         query: axum::extract::Query<HashMap<String, String>>,
                         body: axum::body::Bytes| {
        let dispatcher = dispatcher.clone();
        let plugin_dirs = plugin_dirs.clone();
        async move {
            let path = uri.path().to_string();

            // 1) 静态资源直读：命中则直接返回（200 + mime / 404）。
            if let Some(resp) = try_serve_static_asset(&path, &plugin_dirs) {
                return resp;
            }

            // 2) 否则走 dispatcher（若有）。无 dispatcher → 404。
            let Some(dispatcher) = dispatcher else {
                return (StatusCode::NOT_FOUND, "route not found").into_response();
            };

            let method_str = method.as_str().to_string();
            let headers_map = header_map_to_hashmap(&headers);
            let raw_body = body.to_vec();
            let outcome =
                dispatch_http(&dispatcher, &path, &method_str, raw_body, headers_map, query.0).await;
            match outcome {
                DispatchOutcome::Handled(resp) => {
                    let mut builder = axum::response::Response::builder().status(
                        StatusCode::from_u16(resp.status).unwrap_or(StatusCode::OK),
                    );
                    for (k, v) in &resp.headers {
                        if let (Ok(name), Ok(val)) =
                            (HeaderName::try_from(k), HeaderValue::try_from(v))
                        {
                            builder = builder.header(name, val);
                        }
                    }
                    let body_bytes = if resp.body.is_empty() {
                        Vec::new()
                    } else {
                        base64::engine::general_purpose::STANDARD
                            .decode(&resp.body)
                            .unwrap_or_default()
                    };
                    builder.body(Body::from(body_bytes)).unwrap_or_else(|_| {
                        (StatusCode::INTERNAL_SERVER_ERROR, "response build failed")
                            .into_response()
                    })
                }
                DispatchOutcome::NotFound => {
                    (StatusCode::NOT_FOUND, "route not found").into_response()
                }
                DispatchOutcome::Timeout => {
                    (StatusCode::GATEWAY_TIMEOUT, "endpoint timeout").into_response()
                }
                DispatchOutcome::ConcurrencyLimited => {
                    (StatusCode::SERVICE_UNAVAILABLE, "concurrency limit").into_response()
                }
                DispatchOutcome::HandlerError(msg) => {
                    (StatusCode::BAD_GATEWAY, msg).into_response()
                }
            }
        }
    };

    axum::routing::any(handler)
}

/// 尝试把 `/ext/{plugin_id}/assets/{*rest}` 解析为插件 `web/` 子目录下的文件并直读返回。
///
/// 返回：
/// - `Some(response)` —— 路径形态匹配静态资源（无论命中/未命中/被拒），由调用方直接返回；
/// - `None` —— 不是静态资源路径形态，或该插件未声明目录，调用方应继续走 dispatcher。
///
/// 路径安全：
/// - 拒绝 `..` 段；
/// - canonicalize 后必须仍在插件 `web/` 子树内（防 symlink 逃逸）。
fn try_serve_static_asset(
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

    let bytes = match std::fs::read(&canonical_file) {
        Ok(b) => b,
        Err(_) => return Some((StatusCode::NOT_FOUND, "not found").into_response()),
    };

    let mime = mime_for_extension(
        canonical_file.extension().and_then(|s| s.to_str()).unwrap_or(""),
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

/// 扩展名 → Content-Type 映射表（覆盖常见 web 资源类型）。
///
/// 未命中扩展名统一回退到 `application/octet-stream`（浏览器嗅探可识别大部分文本）。
/// 不引入 mime crate —— 任务范围控制依赖膨胀，常见 web 类型手写映射足够。
fn mime_for_extension(ext: &str) -> &'static str {
    match ext.to_ascii_lowercase().as_str() {
        "html" | "htm" => "text/html; charset=utf-8",
        "js" | "mjs" => "application/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" => "application/json; charset=utf-8",
        "map" => "application/json",
        "txt" => "text/plain; charset=utf-8",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
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
                    format!("{{*{name}}}")  // axum 0.8 多段通配语法 {*name}
                } else {
                    format!("{{{inner}}}")  // axum 0.8 单段捕获语法 {name}
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
}
