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
    let Some(registry) = state.capability_registry.clone() else {
        return static_router;
    };
    let Some(handler) = state.http_handler.clone() else {
        // 无 handler：不挂载插件端点（降级，仅内核静态路由）
        return static_router;
    };

    let dispatcher = Arc::new(HttpDispatcher::new(registry.clone(), handler));
    let routes = registry.list_http_routes();

    let mut router = static_router;
    for route in routes {
        let dispatcher = dispatcher.clone();
        let path = route.endpoint.path.clone();
        let method = route.endpoint.method.clone();
        // manifest 用 {param}/{param:path} 约定（对齐 FastAPI/OpenAPI）；
        // axum 0.8 路径参数语法是 :param（单段）与 *param（多段通配）。注册前转换。
        let axum_path = manifest_path_to_axum(&path);
        // build_dynamic_route_handler 内部按 method 选 axum 方法路由（get/post/...），
        // 返回 MethodRouter 后直接挂到 axum_path（axum .route 接受 MethodRouter）。
        let method_handler = build_dynamic_route_handler(dispatcher, path.clone(), method.clone());
        router = router.route(&axum_path, method_handler);
    }
    router
}

/// 把 manifest 路径约定（{param}/{param:path}）转成 axum 0.8 路径参数语法（:param/*param）。
///
/// - `{name}` → `:name`（单段捕获）
/// - `{name:path}` → `*name`（剩余多段通配，axum 0.8 用 `*` 前缀）
/// - 其他段原样保留。
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

/// 构造一个动态端点的 axum handler：捕获 path/method → 调 dispatch_http → 转 HTTP 响应。
fn build_dynamic_route_handler(
    dispatcher: Arc<HttpDispatcher>,
    path: String,
    method: String,
) -> axum::routing::MethodRouter<crate::routes::AppState> {
    use axum::body::Body;
    use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
    use axum::response::IntoResponse;

    // 先确定 method 大写形式（供下方 match 选择 axum 方法路由；闭包会 move 原 method）。
    let method_upper = method.to_ascii_uppercase();

    let handler = move |uri: axum::http::Uri,
                        headers: HeaderMap,
                        query: axum::extract::Query<HashMap<String, String>>,
                        body: axum::body::Bytes| {
        let dispatcher = dispatcher.clone();
        let registered_path = path.clone();
        let method = method.clone();
        async move {
            let headers_map = header_map_to_hashmap(&headers);
            let raw_body = body.to_vec();
            // 用传入请求的**真实 path**（含 param 实际值，如 /models/gpt-4），
            // 而非注册模板 path（/models/{model_id}）。这样插件的 http.handle 能拿到真实 param。
            // uri.path() 已做百分号解码；query 部分由 axum::extract::Query 单独解析。
            let incoming_path = uri.path().to_string();
            // dispatch_http 内部 find_http_route 会用 incoming_path 做模板匹配，
            // 找到对应注册路由（registered_path 仅作调试用，不参与分发）。
            let _ = &registered_path;
            let outcome =
                dispatch_http(&dispatcher, &incoming_path, &method, raw_body, headers_map, query.0)
                    .await;
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
                    (StatusCode::SERVICE_UNAVAILABLE, "concurrency limit reached").into_response()
                }
                DispatchOutcome::HandlerError(_) => {
                    (StatusCode::BAD_GATEWAY, "endpoint handler error").into_response()
                }
            }
        }
    };

    // 按 method 选择 axum 方法路由（method_upper 已在闭包前计算，避免 borrow-after-move）。
    match method_upper.as_str() {
        "GET" => axum::routing::get(handler),
        "POST" => axum::routing::post(handler),
        "PUT" => axum::routing::put(handler),
        "DELETE" => axum::routing::delete(handler),
        "PATCH" => axum::routing::patch(handler),
        _ => axum::routing::get(handler),
    }
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
