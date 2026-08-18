// @feature: FP-0.2.七 路由收敛 | @vision: V6 可即用 | @ci: rust-test
//! P3-2/P3-3: HTTP dispatcher 测试（TDD RED）。
//!
//! 验证 ADR §3.3 / 附录 E.1.2 / E.1.3：
//! - raw body(base64) + 全量 headers + query 透传给插件 http.handle（不反序列化）
//! - query 多值形态（query_multi，重复 key 如 filter=a&filter=b 全量到达插件——A1 修复）
//! - 插件自定义响应（status/headers/body）原样回写 HTTP
//! - per-endpoint timeout（慢插件 → 504）
//! - per-endpoint max_concurrency（超限 → 503）
//! - build_router 动态挂载插件端点（内核静态路由 + 扫描 http_routes）
//!
//! "真实可运行不 Mock"：测试用一个真实的进程内 HttpHandleCapability 实现
//! （EchoHandler 原样回显收到的 raw_body/headers），验证 dispatcher 透传链路。
//! 生产实现走 sidecar MCP（见 InProcessDispatcher 的 SidecarHttpHandler）。

use std::collections::HashMap;
use std::sync::Arc;

use agentos_core::traits::{
    CapabilityRegistry, HttpEndpoint, HttpHandleCapability, HttpHandleRequest, HttpHandleResponse,
};
use agentos_plugin_loader::CapabilityRegistryImpl;
use base64::Engine;

use agentos_api::http_dispatcher::{
    dispatch_http, register_manifest_http_routes, DispatchOutcome, HttpDispatcher,
};

/// 构造一个 http_endpoints。
fn endpoint(route_id: &str, method: &str, path: &str) -> HttpEndpoint {
    HttpEndpoint {
        route_id: route_id.to_string(),
        method: method.to_string(),
        path: path.to_string(),
        auth: "none".to_string(),
        handler_capability: "http.handle".to_string(),
        timeout_ms: None,
        max_concurrency: None,
        description: None,
    }
}

/// 真实的进程内 Echo handler：把收到的 raw_body 解码后原样作为响应 body 返回，
/// 并把收到的 headers 透传回响应 headers（加前缀 x-echo-）。
/// 用于验证 dispatcher 不反序列化 body、headers 全量透传。
struct EchoHandler;

#[async_trait::async_trait]
impl HttpHandleCapability for EchoHandler {
    async fn handle(&self, req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        // 把收到的 raw_body(base64) 解码后原样回（验证字节级透传）
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(&req.raw_body)
            .map_err(|e| e.to_string())?;
        let mut headers = HashMap::new();
        for (k, v) in &req.headers {
            headers.insert(format!("x-echo-{k}"), v.clone());
        }
        Ok(HttpHandleResponse {
            status: 200,
            headers,
            body: base64::engine::general_purpose::STANDARD.encode(&decoded),
            body_encoding: "base64".to_string(),
        })
    }
}

// ── raw body 透传 ──────────────────────────────────────────

/// 构造一个非 UTF-8 友好的二进制 raw body（含 0x00/0xFF），验证字节级透传：
/// dispatcher 绝不反序列化 body，EchoHandler 收到的 raw_body 解码后必须与原字节一致。
#[tokio::test]
async fn test_dispatcher_passes_raw_body_bytes_untouched() {
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry
        .register_http_route("p1", endpoint("r", "POST", "/ext/p1/cb"))
        .unwrap();
    let dispatcher = HttpDispatcher::new(registry, Arc::new(EchoHandler));

    // 含非 UTF-8 字节的 raw body（企微加密 XML 类似场景）
    let raw = vec![0x00u8, 0xFF, 0xAB, b'<', b'x', b'>', 0xCD, 0x01];
    let outcome = dispatch_http(
        &dispatcher,
        "/ext/p1/cb",
        "POST",
        raw.clone(),
        HashMap::from([("x-wecom-signature".to_string(), "abc".to_string())]),
        HashMap::new(),
    )
    .await;

    match outcome {
        DispatchOutcome::Handled(resp) => {
            assert_eq!(resp.status, 200);
            // body 解码后必须与原字节完全一致（字节级透传）
            let decoded = base64::engine::general_purpose::STANDARD
                .decode(&resp.body)
                .unwrap();
            assert_eq!(decoded, raw);
            // headers 全量透传（带 x-echo- 前缀）
            assert_eq!(
                resp.headers.get("x-echo-x-wecom-signature"),
                Some(&"abc".to_string())
            );
        }
        other => panic!("expected Handled, got {other:?}"),
    }
}

// ── 多值 query 透传（A1：重复 key 不塌缩） ─────────────────────

/// 捕获型 handler：把收到的 query_multi 序列化为 JSON 塞进响应 body（回显验证）。
struct QueryEchoHandler;

#[async_trait::async_trait]
impl HttpHandleCapability for QueryEchoHandler {
    async fn handle(&self, req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        let payload = serde_json::json!({
            "query": req.query,
            "query_multi": req.query_multi,
        });
        Ok(HttpHandleResponse {
            status: 200,
            headers: HashMap::new(),
            body: base64::engine::general_purpose::STANDARD.encode(payload.to_string().as_bytes()),
            body_encoding: "base64".to_string(),
        })
    }
}

/// 直接调 dispatch_http：重复 key（filter=a&filter=b&filter=c 的多值 map）全量
/// 到达插件——query_multi 保序全量，单值 query 取最后一个（last-wins 旧语义）。
#[tokio::test]
async fn test_dispatcher_passes_multi_value_query_untouched() {
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry
        .register_http_route("p1", endpoint("r", "GET", "/ext/p1/list"))
        .unwrap();
    let dispatcher = HttpDispatcher::new(registry, Arc::new(QueryEchoHandler));

    let query_multi = HashMap::from([(
        "filter".to_string(),
        vec![
            "memory_type:eq:episode".to_string(),
            "score:gt:3".to_string(),
            "status:eq:active".to_string(),
        ],
    )]);
    let outcome = dispatch_http(
        &dispatcher,
        "/ext/p1/list",
        "GET",
        vec![],
        HashMap::new(),
        query_multi,
    )
    .await;

    match outcome {
        DispatchOutcome::Handled(resp) => {
            let body = base64::engine::general_purpose::STANDARD
                .decode(&resp.body)
                .unwrap();
            let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
            // query_multi：filter 是全量数组，保序（db_admin 多条件 AND 的输入）
            let filter = json["query_multi"]["filter"].as_array().unwrap();
            assert_eq!(
                filter,
                &[
                    serde_json::json!("memory_type:eq:episode"),
                    serde_json::json!("score:gt:3"),
                    serde_json::json!("status:eq:active"),
                ],
                "重复 filter key 应全量保序到达插件"
            );
            // 单值 query：last-wins（与旧 HashMap 覆盖语义一致，旧插件不受影响）
            assert_eq!(json["query"]["filter"], "status:eq:active");
        }
        other => panic!("expected Handled, got {other:?}"),
    }
}

/// 经 build_router 的 /ext 通配端点全链路：axum 提取（serde_urlencoded 多值收集）
/// → dispatch_http → 插件收到的 query_multi 里 filter 是全量数组。
#[tokio::test]
async fn test_router_multi_value_query_reaches_plugin() {
    use agentos_api::routes::AppState;
    use agentos_api::server::build_router;
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry
        .register_http_route(
            "db_admin",
            endpoint("r", "GET", "/ext/db_admin/table/memory"),
        )
        .unwrap();
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(QueryEchoHandler));

    let app = build_router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .method("GET")
                .uri("/ext/db_admin/table/memory?filter=memory_type:eq:episode&filter=score:gt:3&limit=10")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .unwrap();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
    let filter = json["query_multi"]["filter"].as_array().unwrap();
    assert_eq!(
        filter,
        &[
            serde_json::json!("memory_type:eq:episode"),
            serde_json::json!("score:gt:3"),
        ],
        "经 axum 通配端点的重复 filter key 应全量到达插件（A1 修复）"
    );
    // 其他 key 不受影响
    assert_eq!(json["query_multi"]["limit"], serde_json::json!(["10"]));
    assert_eq!(json["query"]["limit"], "10");
}

/// 向后兼容：旧负载（无 query_multi 字段）反序列化得空 map，不报错。
#[test]
fn test_http_handle_request_deserializes_legacy_payload() {
    let legacy = serde_json::json!({
        "method": "GET",
        "path": "/ext/p/cb",
        "plugin_id": "p",
        "raw_body": "",
        "headers": {},
        "query": {"a": "1"},
    });
    let req: HttpHandleRequest = serde_json::from_value(legacy).expect("旧负载应可反序列化");
    assert!(req.query_multi.is_empty());
    assert_eq!(req.query.get("a").map(|s| s.as_str()), Some("1"));
    // 新负载 roundtrip 保序
    let full = serde_json::json!({
        "method": "GET",
        "path": "/ext/p/cb",
        "plugin_id": "p",
        "raw_body": "",
        "headers": {},
        "query": {"a": "2"},
        "query_multi": {"a": ["1", "2"]},
    });
    let req: HttpHandleRequest = serde_json::from_value(full).unwrap();
    assert_eq!(req.query_multi.get("a").unwrap(), &vec!["1", "2"]);
}

// ── 插件自定义响应（status/headers/body） ────────────────────

/// 插件返回非 200 status + 自定义 content-type + 任意 body —— dispatcher 原样回写。
struct CustomResponseHandler;

#[async_trait::async_trait]
impl HttpHandleCapability for CustomResponseHandler {
    async fn handle(&self, _req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        Ok(HttpHandleResponse {
            status: 201,
            headers: HashMap::from([("content-type".to_string(), "text/xml".to_string())]),
            body: base64::engine::general_purpose::STANDARD.encode(b"<xml>ok</xml>"),
            body_encoding: "base64".to_string(),
        })
    }
}

#[tokio::test]
async fn test_dispatcher_passes_plugin_custom_response() {
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry
        .register_http_route("p1", endpoint("r", "POST", "/ext/p1/cb"))
        .unwrap();
    let dispatcher = HttpDispatcher::new(registry, Arc::new(CustomResponseHandler));

    let outcome = dispatch_http(
        &dispatcher,
        "/ext/p1/cb",
        "POST",
        vec![],
        HashMap::new(),
        HashMap::new(),
    )
    .await;

    match outcome {
        DispatchOutcome::Handled(resp) => {
            assert_eq!(resp.status, 201);
            assert_eq!(
                resp.headers.get("content-type"),
                Some(&"text/xml".to_string())
            );
        }
        other => panic!("expected Handled, got {other:?}"),
    }
}

// ── per-endpoint timeout（慢插件 → 504） ─────────────────────

struct SlowHandler;

#[async_trait::async_trait]
impl HttpHandleCapability for SlowHandler {
    async fn handle(&self, _req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        // 睡 2s，超过测试用的 100ms timeout
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
        Ok(HttpHandleResponse {
            status: 200,
            headers: HashMap::new(),
            body: String::new(),
            body_encoding: "base64".to_string(),
        })
    }
}

#[tokio::test]
async fn test_dispatcher_timeout_returns_504() {
    let registry = Arc::new(CapabilityRegistryImpl::new());
    // 声明 100ms timeout
    let mut ep = endpoint("r", "POST", "/ext/p1/cb");
    ep.timeout_ms = Some(100);
    registry.register_http_route("p1", ep).unwrap();
    let dispatcher = HttpDispatcher::new(registry, Arc::new(SlowHandler));

    let outcome = dispatch_http(
        &dispatcher,
        "/ext/p1/cb",
        "POST",
        vec![],
        HashMap::new(),
        HashMap::new(),
    )
    .await;

    assert!(
        matches!(outcome, DispatchOutcome::Timeout),
        "slow plugin should time out, got {outcome:?}"
    );
}

// ── per-endpoint max_concurrency（超限 → 503） ───────────────

struct ConcurrencyLimitedHandler;

#[async_trait::async_trait]
impl HttpHandleCapability for ConcurrencyLimitedHandler {
    async fn handle(&self, _req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        // 占住一个槽位 500ms
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        Ok(HttpHandleResponse {
            status: 200,
            headers: HashMap::new(),
            body: String::new(),
            body_encoding: "base64".to_string(),
        })
    }
}

#[tokio::test]
async fn test_dispatcher_max_concurrency_returns_503() {
    let registry = Arc::new(CapabilityRegistryImpl::new());
    // max_concurrency = 1
    let mut ep = endpoint("r", "POST", "/ext/p1/cb");
    ep.max_concurrency = Some(1);
    ep.timeout_ms = Some(5000); // 不会被 timeout 干扰
    registry.register_http_route("p1", ep).unwrap();
    let dispatcher = Arc::new(HttpDispatcher::new(
        registry,
        Arc::new(ConcurrencyLimitedHandler),
    ));

    // 第一个请求占住唯一槽位
    let d1 = dispatcher.clone();
    let h1 = tokio::spawn(async move {
        dispatch_http(
            &d1,
            "/ext/p1/cb",
            "POST",
            vec![],
            HashMap::new(),
            HashMap::new(),
        )
        .await
    });
    // 让出调度，保证 h1 先拿到信号量
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;

    // 第二个请求应被拒（503）
    let outcome2 = dispatch_http(
        &dispatcher,
        "/ext/p1/cb",
        "POST",
        vec![],
        HashMap::new(),
        HashMap::new(),
    )
    .await;
    assert!(
        matches!(outcome2, DispatchOutcome::ConcurrencyLimited),
        "second concurrent request should be 503, got {outcome2:?}"
    );

    // 第一个请求最终成功
    let _ = h1.await.unwrap();
}

// ── 未知路由（dispatcher 找不到路由） ────────────────────────

#[tokio::test]
async fn test_dispatcher_unknown_route() {
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry
        .register_http_route("p1", endpoint("r", "POST", "/ext/p1/cb"))
        .unwrap();
    let dispatcher = HttpDispatcher::new(registry, Arc::new(EchoHandler));

    let outcome = dispatch_http(
        &dispatcher,
        "/ext/p1/nonexistent",
        "POST",
        vec![],
        HashMap::new(),
        HashMap::new(),
    )
    .await;
    assert!(
        matches!(outcome, DispatchOutcome::NotFound),
        "unknown route should be NotFound, got {outcome:?}"
    );
}

// ── build_router 动态挂载（内核静态 + 插件端点） ─────────────

/// 验证 build_router 把插件 http_routes 动态挂到 axum 路由树上，
/// 且内核静态路由（/health）不受影响。
#[tokio::test]
async fn test_build_router_mounts_plugin_routes() {
    use agentos_api::routes::AppState;
    use agentos_api::server::build_router;
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    // 带一个插件端点的 state
    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry
        .register_http_route("p1", endpoint("r", "POST", "/ext/p1/cb"))
        .unwrap();
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(EchoHandler));

    let app = build_router(state);

    // 内核静态路由仍在
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/health")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    // 插件端点已挂载：POST /ext/p1/cb 应能打到 dispatcher（404 vs 200/其他都说明挂载成功，区别于"路由不存在"）
    // axum 对未挂载路由返回 404；已挂载但 handler 内部找不到会走 dispatcher 的 NotFound(404)。
    // 这里验证请求被接受（非 axum 静态 405 Method Not Allowed 即说明路由存在）。
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/ext/p1/cb")
                .body(Body::from(vec![1u8, 2u8, 3u8]))
                .unwrap(),
        )
        .await
        .unwrap();
    // dispatcher 走 EchoHandler 应返回 200
    assert_eq!(resp.status(), StatusCode::OK);
}

// ── register_manifest_http_routes：聚合报错（不逐个 panic） ────

/// 多个 manifest 注册时，若部分路由冲突/越界，应聚合所有错误返回，
/// 而不是第一个就 panic（ADR 命名陷阱治理 D.4 / E.1.3 fail-closed 聚合）。
#[tokio::test]
async fn test_register_manifest_http_routes_aggregates_errors() {
    use agentos_core::traits::{HostType, PluginManifest, PluginType};

    let registry = Arc::new(CapabilityRegistryImpl::new());

    // 两个 manifest：一个合法，一个越界（path 不在命名空间）
    let good = PluginManifest {
        id: "good".to_string(),
        name: "Good".to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::System,
        pipeline_role: None,
        language: "python".to_string(),
        host_type: HostType::Sidecar,
        entry: "python server.py".to_string(),
        capabilities: Default::default(),
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
        http_endpoints: vec![endpoint("r", "POST", "/ext/good/cb")],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        provides: None,
    };
    let bad = PluginManifest {
        id: "bad".to_string(),
        name: "Bad".to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::System,
        pipeline_role: None,
        language: "python".to_string(),
        host_type: HostType::Sidecar,
        entry: "python server.py".to_string(),
        capabilities: Default::default(),
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
        // 越界：不在 /ext/bad/ 命名空间
        http_endpoints: vec![endpoint("r", "POST", "/wecom/cb")],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        provides: None,
    };

    let errors = register_manifest_http_routes(&registry, &[good, bad], None);
    // 合法的注册成功，越界的报错——聚合返回 1 个错误（不 panic）
    assert_eq!(
        errors.len(),
        1,
        "should aggregate 1 error for the bad route, got {errors:?}"
    );
    assert!(errors[0].contains("namespace") || errors[0].contains("命名空间"));
    // 合法路由确实注册了
    assert_eq!(registry.list_http_routes().len(), 1);
}

// ── /api/v1/datasource/{*rest} 数据源代理（G6-a：占位转真实路由） ─────────

/// 验证 datasource 代理把 `{rest}` 改写为 /ext/{rest} 复用同一分发：
/// - 短形式 `/api/v1/datasource/{plugin_id}/{route_id}` → `/ext/{plugin_id}/{route_id}`
/// - 显式 ext 长形式 `/api/v1/datasource/ext/{plugin_id}/{route_id}` 也命中
/// - query 透传（多值全量）；未注册 route → 404（真实路由接管，前端占位护栏已移）
#[tokio::test]
async fn test_datasource_proxy_forwards_to_ext_routes() {
    use agentos_api::routes::AppState;
    use agentos_api::server::build_router;
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry
        .register_http_route(
            "db_admin",
            endpoint("r", "GET", "/ext/db_admin/table/memory"),
        )
        .unwrap();
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(QueryEchoHandler));
    let app = build_router(state);

    // 短形式（前端 fetchDatasourceOptions 非绝对 URI 的调用形态）
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("GET")
                .uri("/api/v1/datasource/db_admin/table/memory?limit=5&filter=a&filter=b")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let text = String::from_utf8(body.to_vec()).unwrap();
    assert!(text.contains(r#""limit":"5""#), "query 应透传: {text}");
    assert!(text.contains(r#""filter":["a","b"]"#), "多值 query 应全量: {text}");

    // 显式 ext 长形式
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("GET")
                .uri("/api/v1/datasource/ext/db_admin/table/memory")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    // 未注册 route → 404
    let resp = app
        .oneshot(
            Request::builder()
                .method("GET")
                .uri("/api/v1/datasource/nonexistent/foo")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}
