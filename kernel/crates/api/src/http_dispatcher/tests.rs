//! http_dispatcher 测试（原内联 mod tests 平移，file-length 触线拆分：
//! 存量 2500 触线闸红，capability_router 冻结件同刀拆先例）。
//! 子模块保留对父模块私有项（exec_ext_request/endpoint 助手等）的访问。

use super::*;

/// /ext 请求体上限行为钉死（2026-10-25 显式化）：超限 413 在 handler 抽取
/// 前拒绝，限内请求穿透到路由裁决不受影响。
#[tokio::test]
async fn ext_body_over_limit_rejected_413_and_under_limit_passes() {
    use tower::ServiceExt;
    let app = axum::Router::new()
        .route(
            "/ext/{*rest}",
            build_wildcard_handler(crate::routes::AppState::new(), None),
        )
        .with_state(crate::routes::AppState::new());
    let over = vec![b'x'; EXT_BODY_LIMIT_BYTES + 1];
    let req = axum::http::Request::builder()
        .method("POST")
        .uri("/ext/sync_relay/events")
        .body(axum::body::Body::from(over))
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    assert_eq!(
        resp.status(),
        axum::http::StatusCode::PAYLOAD_TOO_LARGE,
        "超限请求体必须在 handler 前被 413 拒绝"
    );

    // 限内：穿透到分发裁决（无 dispatcher/静态资源 → 404），证明层不误伤。
    let req = axum::http::Request::builder()
        .method("POST")
        .uri("/ext/sync_relay/events")
        .body(axum::body::Body::from(b"{\"events\":[]}".to_vec()))
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::NOT_FOUND);
}

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

// ── 静态资源路径形态判定（None = 交回 dispatcher 的分支面）──

/// `try_serve_static_asset` 的非静态形态：非 /ext 前缀 / 缺 plugin 段 /
/// 缺 mid 段 / 非 assets / 空 rest / 未声明目录 / 无 web 目录 → 一律 None
///（交回 dispatcher，不吞请求）。
#[tokio::test]
async fn static_asset_shape_mismatches_return_none() {
    let dir = tempfile::tempdir().expect("tempdir");
    let dirs = plugin_dirs_with_web(&dir);
    // 无 web/ 子目录的插件根
    let bare = tempfile::tempdir().expect("tempdir");
    let mut with_bare = dirs.clone();
    with_bare.insert("bare".to_string(), bare.path().to_path_buf());

    let cases = [
        ("/api/v1/other", "非 /ext 前缀"),
        ("/ext/", "缺 plugin 段"),
        ("/ext/p", "缺 mid 段（splitn 只两段）"),
        ("/ext/p/not-assets/x.js", "非 assets 子路径"),
        ("/ext/p/assets", "空 rest（目录索引交插件）"),
        ("/ext/p/assets/", "rest 仅斜杠也视为空"),
        ("/ext/unknown/assets/x.js", "插件未声明目录"),
        ("/ext/bare/assets/x.js", "插件无 web/ 子目录"),
    ];
    for (path, why) in cases {
        assert!(
            try_serve_static_asset(path, &with_bare).await.is_none(),
            "{why}：应返回 None 交回 dispatcher，path={path}"
        );
    }
}

/// `..` 段逃逸与文件不存在：路径形态匹配即由静态分支裁决（Some + 404，
/// 不交回 dispatcher——/assets/** 是静态资源命名空间）。
#[tokio::test]
async fn static_asset_rejects_traversal_and_missing_file() {
    let dir = tempfile::tempdir().expect("tempdir");
    let dirs = plugin_dirs_with_web(&dir);
    // plugin.json 在插件根（web/ 之外），逃逸尝试若成功会读到它
    std::fs::write(dir.path().join("plugin.json"), b"{\"id\":\"p\"}").unwrap();

    let traversal = try_serve_static_asset("/ext/p/assets/../../plugin.json", &dirs)
        .await
        .expect("路径形态匹配（含 .. 段）仍归静态分支裁决");
    assert_eq!(
        response_status(&traversal),
        axum::http::StatusCode::NOT_FOUND,
        ".. 段必须 404，不得逃出 web/ 子树"
    );

    let missing = try_serve_static_asset("/ext/p/assets/nope.js", &dirs)
        .await
        .expect("不存在文件的路径形态仍归静态分支");
    assert_eq!(response_status(&missing), axum::http::StatusCode::NOT_FOUND);
}

/// `read_static_asset_capped` 的 IO 失败分支（文件被删/不可读）→ Io 错误。
#[test]
fn read_static_asset_capped_io_failure() {
    let dir = tempfile::tempdir().expect("tempdir");
    let missing = dir.path().join("gone.bin");
    assert!(matches!(
        read_static_asset_capped(&missing),
        Err(StaticAssetReadError::Io)
    ));
    // 超限优先于 IO：先 stat 再读，目录本身 stat 成功但 read 失败 → Io
    assert!(matches!(
        read_static_asset_capped(dir.path()),
        Err(StaticAssetReadError::Io)
    ));
}

/// `mime_for_extension` 全表 + 大小写不敏感 + 未知回退 octet-stream。
#[test]
fn mime_for_extension_table_and_fallback() {
    for (ext, want) in [
        ("html", "text/html; charset=utf-8"),
        ("htm", "text/html; charset=utf-8"),
        ("js", "application/javascript; charset=utf-8"),
        ("mjs", "application/javascript; charset=utf-8"),
        ("css", "text/css; charset=utf-8"),
        ("json", "application/json; charset=utf-8"),
        ("map", "application/json"),
        ("txt", "text/plain; charset=utf-8"),
        ("svg", "image/svg+xml"),
        ("png", "image/png"),
        ("woff2", "font/woff2"),
        ("wasm", "application/wasm"),
        ("XML", "application/xml; charset=utf-8"),
    ] {
        assert_eq!(mime_for_extension(ext), want, "ext={ext}");
    }
    for unknown in ["bin", "", "tar.gz", "exe"] {
        assert_eq!(
            mime_for_extension(unknown),
            "application/octet-stream",
            "未知扩展名 {unknown:?} 回退 octet-stream"
        );
    }
}

/// `ext_path_plugin_id`：/ext 前缀提取首段；非 /ext、空段 → None。
#[test]
fn ext_path_plugin_id_extraction() {
    assert_eq!(ext_path_plugin_id("/ext/svc/ping"), Some("svc"));
    assert_eq!(ext_path_plugin_id("/ext/svc"), Some("svc"));
    assert_eq!(ext_path_plugin_id("/api/v1/ext/svc"), None);
    assert_eq!(ext_path_plugin_id("/ext/"), None);
    assert_eq!(ext_path_plugin_id("/ext//ping"), None);
    assert_eq!(ext_path_plugin_id("/ext"), None);
}

/// `parse_query_multi`：重复 key 保序全量、无 query → 空表。
#[test]
fn parse_query_multi_preserves_duplicates() {
    let uri: axum::http::Uri = "/x?a=1&b=2&a=3".parse().unwrap();
    let m = parse_query_multi(&uri);
    assert_eq!(m.get("a").unwrap(), &vec!["1".to_string(), "3".to_string()]);
    assert_eq!(m.get("b").unwrap(), &vec!["2".to_string()]);
    let bare: axum::http::Uri = "/x".parse().unwrap();
    assert!(parse_query_multi(&bare).is_empty());
}

/// `header_map_to_hashmap`：key 小写化、多值取首个、非 ASCII 值跳过。
#[test]
fn header_map_to_hashmap_lowercases_and_first_wins() {
    use axum::http::{HeaderMap, HeaderValue};
    let mut headers = HeaderMap::new();
    headers.insert("X-Custom", HeaderValue::from_static("first"));
    headers.append("X-Custom", HeaderValue::from_static("second"));
    let map = header_map_to_hashmap(&headers);
    assert_eq!(map.get("x-custom").map(String::as_str), Some("first"));
    assert!(header_map_to_hashmap(&HeaderMap::new()).is_empty());
}

// ── 分发结果 → HTTP 响应的映射（exec_ext_request 的出口面）──

/// 构造测试用 http_endpoints 声明（auth 显式 none，除用例另行覆写）。
fn endpoint(route_id: &str, method: &str, path: &str) -> agentos_core::traits::HttpEndpoint {
    agentos_core::traits::HttpEndpoint {
        route_id: route_id.to_string(),
        method: method.to_string(),
        path: path.to_string(),
        auth: Some("none".to_string()),
        handler_capability: "http.handle".to_string(),
        timeout_ms: None,
        max_concurrency: None,
        description: None,
    }
}

/// 插件回包状态码越界 / body 非 base64 → 502（上游故障不得静默改 200）。
struct BadResponseHandler {
    bad_status: bool,
}

#[async_trait::async_trait]
impl HttpHandleCapability for BadResponseHandler {
    async fn handle(&self, _req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        Ok(HttpHandleResponse {
            status: if self.bad_status { 0 } else { 200 },
            headers: HashMap::new(),
            body: if self.bad_status {
                String::new()
            } else {
                "not-base64!!".to_string()
            },
            body_encoding: "base64".to_string(),
        })
    }
}

/// 坏状态码 + 坏 body 两组区分度输入：都必须 502 且文案点明原因。
#[tokio::test]
async fn exec_ext_request_maps_bad_plugin_response_to_502() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    for (bad_status, why) in [(true, "状态码越界"), (false, "body 非 base64")] {
        let mut state = AppState::new();
        let registry = Arc::new(CapabilityRegistryImpl::new());
        registry
            .register_http_route("p1", endpoint("r", "GET", "/ext/p1/cb"))
            .unwrap();
        state.capability_registry = Some(registry);
        state.http_handler = Some(Arc::new(BadResponseHandler { bad_status }));

        let app = crate::server::build_router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/ext/p1/cb")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            axum::http::StatusCode::BAD_GATEWAY,
            "{why} 必须 502"
        );
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let text = String::from_utf8_lossy(&body);
        let expect = if bad_status {
            "invalid status"
        } else {
            "decode failed"
        };
        assert!(text.contains(expect), "{why} 文案应含 {expect}: {text}");
    }
}

/// 无 dispatcher 资源且无插件目录活真源 → build_router_with_http_routes 原样
/// 返回静态路由（不挂 /ext 通配），请求 404 由内核路由树裁决。
#[tokio::test]
async fn build_router_without_dispatcher_resources_keeps_static_tree() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    let state = AppState::new();
    assert!(state.capability_registry.is_none() && state.plugin_loader.is_none());
    // 传入的静态路由树已带 state（生产由 build_router 先 with_state 之前调用，
    // 此处直接验证返回值语义：资源缺失时原样返回入参路由器）
    let inner: axum::Router<crate::routes::AppState> = axum::Router::new().route(
        "/ping",
        axum::routing::get(|| async { axum::http::StatusCode::OK }),
    );
    let app = build_router_with_http_routes(state, inner);
    let app = app.with_state(crate::routes::AppState::new());
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/anything")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        axum::http::StatusCode::NOT_FOUND,
        "未挂 /ext 通配时由内核路由树 404"
    );
}

/// 并发超限 → handler 出口 503（DispatchOutcome::ConcurrencyLimited 映射）。
struct HangHandler;

#[async_trait::async_trait]
impl HttpHandleCapability for HangHandler {
    async fn handle(&self, _req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        tokio::time::sleep(std::time::Duration::from_millis(600)).await;
        Ok(HttpHandleResponse {
            status: 200,
            headers: HashMap::new(),
            body: String::new(),
            body_encoding: "base64".to_string(),
        })
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn exec_ext_request_concurrency_limited_maps_to_503() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let mut ep = endpoint("r", "GET", "/ext/p1/slow");
    ep.max_concurrency = Some(1);
    ep.timeout_ms = Some(5000);
    registry.register_http_route("p1", ep).unwrap();
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(HangHandler));
    let app = crate::server::build_router(state);

    let first = app.clone().oneshot(
        Request::builder()
            .uri("/ext/p1/slow")
            .body(Body::empty())
            .unwrap(),
    );
    let second = app.clone().oneshot(
        Request::builder()
            .uri("/ext/p1/slow")
            .body(Body::empty())
            .unwrap(),
    );
    let (a, b) = tokio::join!(first, second);
    let statuses = [a.unwrap().status(), b.unwrap().status()];
    assert!(
        statuses.contains(&axum::http::StatusCode::OK)
            && statuses.contains(&axum::http::StatusCode::SERVICE_UNAVAILABLE),
        "并发上限 1 时两请求应一放行一 503，实际 {statuses:?}"
    );
}

/// 超时 → handler 出口 504（DispatchOutcome::Timeout 映射）。
struct SlowThenOkHandler;

#[async_trait::async_trait]
impl HttpHandleCapability for SlowThenOkHandler {
    async fn handle(&self, _req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
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
async fn exec_ext_request_timeout_maps_to_504() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let mut ep = endpoint("r", "GET", "/ext/p1/slow");
    ep.timeout_ms = Some(50); // 远小于 handler 的 500ms
    registry.register_http_route("p1", ep).unwrap();
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(SlowThenOkHandler));
    let app = crate::server::build_router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/p1/slow")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::GATEWAY_TIMEOUT);
}

// ── D1 宽限面：宿主 starting 期间端点超时预算暂停（等待就绪而非 504）──

/// D1 探针桩：只回答 is_host_starting，其余 trait 面不可达/空实现。
struct StartingProbeInvoker {
    starting: std::sync::atomic::AtomicBool,
}

/// trait 默认实现语义：未覆写 is_host_starting 的实现方（测试桩等）
/// 恒不在启动窗口——dispatcher 行为与无探针时完全一致。
#[test]
fn invoker_default_is_host_starting_is_false() {
    let invoker = ScriptedInvoker {
        result: parking_lot::Mutex::new(Err("unused".to_string())),
    };
    assert!(
        !agentos_core::traits::PluginInvoker::is_host_starting(&invoker, "p1"),
        "默认实现必须返回 false（不在启动窗口）"
    );
}

#[async_trait::async_trait]
impl agentos_core::traits::PluginInvoker for StartingProbeInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        _ctx: &agentos_core::types::PluginContext<'a>,
    ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
        unreachable!("本测试不触达管道插件调用")
    }
    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &serde_json::Value,
    ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError> {
        unreachable!("本测试不触达工具调用")
    }
    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: agentos_core::traits::LifecycleHook,
        _context: &agentos_core::traits::HookContext,
    ) -> Result<(), agentos_core::types::PluginError> {
        Ok(())
    }
    fn is_host_starting(&self, _plugin_id: &str) -> bool {
        self.starting.load(std::sync::atomic::Ordering::SeqCst)
    }
}

/// D1：宿主 starting 期间预算暂停——handler 在 base timeout 之后才完成
/// 仍被等待就绪（respawn 冷启窗内不 504），无探针同型请求则照旧 504。
#[tokio::test]
async fn dispatch_waits_for_starting_host_beyond_base_timeout() {
    struct DelayedOkHandler;
    #[async_trait::async_trait]
    impl HttpHandleCapability for DelayedOkHandler {
        async fn handle(&self, _req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
            tokio::time::sleep(std::time::Duration::from_millis(400)).await;
            Ok(HttpHandleResponse {
                status: 200,
                headers: HashMap::new(),
                body: String::new(),
                body_encoding: "base64".to_string(),
            })
        }
    }

    let registry = Arc::new(CapabilityRegistryImpl::new());
    let mut ep = endpoint("r", "GET", "/ext/p1/starting");
    ep.timeout_ms = Some(100); // 远小于 handler 的 400ms
    registry.register_http_route("p1", ep).unwrap();

    // 有探针且 starting：预算暂停 → 等 handler 完成而非 100ms 处 504
    let dispatcher = HttpDispatcher::new(registry.clone(), Arc::new(DelayedOkHandler))
        .with_startup_probe(Some(Arc::new(StartingProbeInvoker {
            starting: std::sync::atomic::AtomicBool::new(true),
        })));
    let started = std::time::Instant::now();
    let outcome = dispatch_http(
        &dispatcher,
        "/ext/p1/starting",
        "GET",
        vec![],
        HashMap::new(),
        HashMap::new(),
    )
    .await;
    assert!(
        matches!(outcome, DispatchOutcome::Handled(_)),
        "starting 窗口内必须等待就绪而非 504: {outcome:?}"
    );
    assert!(
        started.elapsed() >= std::time::Duration::from_millis(350),
        "完成必须落在 base timeout 之后的宽限窗内（等待了 {:?}）",
        started.elapsed()
    );

    // 无探针：同型请求在 base timeout 处照旧 504（既有语义逐毫秒等价）
    let dispatcher = HttpDispatcher::new(registry, Arc::new(DelayedOkHandler));
    let outcome = dispatch_http(
        &dispatcher,
        "/ext/p1/starting",
        "GET",
        vec![],
        HashMap::new(),
        HashMap::new(),
    )
    .await;
    assert!(
        matches!(outcome, DispatchOutcome::Timeout),
        "无探针时不得被宽限: {outcome:?}"
    );
}

/// D1：宿主退出 starting 窗口后预算恢复消耗——不能被宽限无限拖住。
#[tokio::test]
async fn dispatch_budget_resumes_when_host_leaves_starting_window() {
    struct SlowHandler;
    #[async_trait::async_trait]
    impl HttpHandleCapability for SlowHandler {
        async fn handle(&self, _req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
            tokio::time::sleep(std::time::Duration::from_millis(900)).await;
            Ok(HttpHandleResponse {
                status: 200,
                headers: HashMap::new(),
                body: String::new(),
                body_encoding: "base64".to_string(),
            })
        }
    }

    let probe = Arc::new(StartingProbeInvoker {
        starting: std::sync::atomic::AtomicBool::new(true),
    });
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let mut ep = endpoint("r", "GET", "/ext/p1/flap");
    ep.timeout_ms = Some(100);
    registry.register_http_route("p1", ep).unwrap();
    let dispatcher = HttpDispatcher::new(registry, Arc::new(SlowHandler))
        .with_startup_probe(Some(probe.clone() as Arc<dyn PluginInvoker>));

    // 300ms 后探针翻 false：预算恢复消耗，100ms 预算在 ~400ms 处耗尽 → 504
    //（handler 900ms 才完成，必然赶不上）；elapsed < 900ms 证明预算确实恢复。
    let p = probe.clone();
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(300)).await;
        p.starting.store(false, std::sync::atomic::Ordering::SeqCst);
    });

    let started = std::time::Instant::now();
    let outcome = dispatch_http(
        &dispatcher,
        "/ext/p1/flap",
        "GET",
        vec![],
        HashMap::new(),
        HashMap::new(),
    )
    .await;
    assert!(
        matches!(outcome, DispatchOutcome::Timeout),
        "退出 starting 窗口后预算必须恢复消耗: {outcome:?}"
    );
    let elapsed = started.elapsed();
    assert!(
        elapsed >= std::time::Duration::from_millis(350),
        "starting 期间预算必须暂停（400ms 完成前不 504）: {elapsed:?}"
    );
    assert!(
        elapsed < std::time::Duration::from_millis(800),
        "预算恢复后必须按剩余预算 504（不能被宽限无限拖住）: {elapsed:?}"
    );
}

/// handler 返回 Err → 502（DispatchOutcome::HandlerError 映射）。
struct ErrHandler;

#[async_trait::async_trait]
impl HttpHandleCapability for ErrHandler {
    async fn handle(&self, _req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        Err("plugin blew up".to_string())
    }
}

#[tokio::test]
async fn exec_ext_request_handler_error_maps_to_502() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry
        .register_http_route("p1", endpoint("r", "GET", "/ext/p1/boom"))
        .unwrap();
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(ErrHandler));
    let app = crate::server::build_router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/p1/boom")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::BAD_GATEWAY);
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    assert!(String::from_utf8_lossy(&body).contains("plugin blew up"));
}

/// 未知路由（无 dispatcher 资源但有 plugin_dirs）：静态分支未命中
/// → dispatcher 缺席 → 404 "route not found"。
#[tokio::test]
async fn exec_ext_request_without_dispatcher_returns_404() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    let dir = tempfile::tempdir().expect("tempdir");
    // 只有插件目录活真源（静态资源面），无 capability_registry/http_handler
    let pkg = dir.path().join("p");
    std::fs::create_dir_all(&pkg).unwrap();
    std::fs::write(
        pkg.join("plugin.json"),
        r#"{"id":"p","name":"p","version":"1.0.0","plugin_type":"tool",
                "language":"python","host_type":"sidecar","entry":"python server.py",
                "capabilities":{}}"#,
    )
    .unwrap();
    let mut state = AppState::new();
    state.plugin_loader = Some(crate::test_env::discovered_plugin_loader(dir.path()).await);
    let app = crate::server::build_router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/p/no-route")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::NOT_FOUND);
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    assert!(String::from_utf8_lossy(&body).contains("route not found"));
}

/// `build_datasource_handler`：`/api/v1/datasource/ext/{plugin}/{route}` 改写
/// 复用同一分发；短形式 `/api/v1/datasource/{route}` 按 /ext/{route} 处理。
#[tokio::test]
async fn datasource_handler_rewrites_both_forms() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    struct EchoPath;
    #[async_trait::async_trait]
    impl HttpHandleCapability for EchoPath {
        async fn handle(&self, req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
            Ok(HttpHandleResponse {
                status: 200,
                headers: HashMap::new(),
                body: base64::engine::general_purpose::STANDARD.encode(req.path.as_bytes()),
                body_encoding: "base64".to_string(),
            })
        }
    }

    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry
        .register_http_route("p1", endpoint("r", "GET", "/ext/p1/opts"))
        .unwrap();
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(EchoPath));
    let app = crate::server::build_router(state);
    // /api/v1/datasource 代理面受 /api/v1/ 前缀 default-deny 保护
    // （ADR 2026-10-10）——store=None 回退内置脚手架 admin。
    let bearer = format!(
        "Bearer {}",
        agentos_http::auth::encode_token(
            agentos_http::auth::TokenType::Access,
            &agentos_http::auth::BuiltInUser {
                id: "00000000-0000-0000-0000-000000000001".to_string(),
                username: "admin".to_string(),
                password: String::new(),
                email: String::new(),
                role: "admin".to_string(),
                tenant_id: "default".to_string(),
                created_at: String::new(),
                must_change_password: false,
            },
            3600,
        )
    );

    // 带 ext/ 前缀 → 改写为 /ext/p1/opts 命中插件路由
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/v1/datasource/ext/p1/opts")
                .header("authorization", &bearer)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    assert_eq!(
        String::from_utf8_lossy(&body),
        "/ext/p1/opts",
        "带 ext/ 前缀应改写为 /ext/ 原样（透传）"
    );

    // 短形式 → 按 /ext/{route} 处理（未命中注册路由 → 404）
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/datasource/unknown")
                .header("authorization", &bearer)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::NOT_FOUND);
}

/// 鉴权声明为 `user` 且带合法 token → 注入身份头转发给插件（三头齐全）。
#[tokio::test]
async fn exec_ext_request_injects_identity_headers_for_authed_route() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    struct HeaderEcho;
    #[async_trait::async_trait]
    impl HttpHandleCapability for HeaderEcho {
        async fn handle(&self, req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
            let payload = serde_json::json!({
                "tenant": req.headers.get("x-agentos-tenant"),
                "user": req.headers.get("x-agentos-user"),
                "role": req.headers.get("x-agentos-role"),
            });
            Ok(HttpHandleResponse {
                status: 200,
                headers: HashMap::new(),
                body: base64::engine::general_purpose::STANDARD
                    .encode(payload.to_string().as_bytes()),
                body_encoding: "base64".to_string(),
            })
        }
    }

    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let password = agentos_http::auth::hash_password("ext-pw").unwrap();
    let record = agentos_core::types::UserRecord {
        user_id: "u-ext-1".to_string(),
        username: "extuser".to_string(),
        password: password.clone(),
        email: None,
        role: "admin".to_string(),
        tenant_id: "tenant-ext".to_string(),
        created_at: chrono::Utc::now().to_rfc3339(),
        last_login_at: None,
        must_change_password: false,
    };
    agentos_core::traits::StorageBackend::create_user(store.as_ref(), &record)
        .await
        .unwrap();
    let built = agentos_http::auth::BuiltInUser {
        id: record.user_id.clone(),
        username: record.username.clone(),
        password,
        email: String::new(),
        role: "admin".to_string(),
        tenant_id: "tenant-ext".to_string(),
        created_at: String::new(),
        must_change_password: false,
    };
    let token =
        agentos_http::auth::encode_token(agentos_http::auth::TokenType::Access, &built, 3600);

    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let mut ep = endpoint("r", "GET", "/ext/p1/whoami");
    ep.auth = Some("user".to_string());
    registry.register_http_route("p1", ep).unwrap();
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(HeaderEcho));
    state.store = Some(store);

    let app = crate::server::build_router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/p1/whoami")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(v["tenant"], "tenant-ext", "租户头必须为 token 解析值: {v}");
    assert_eq!(v["user"], "u-ext-1");
    assert_eq!(v["role"], "admin");
}

/// 认证端点派发建立 task-local 身份 scope（反调链第三分支收口）：插件
/// http.handle 内 `agentos_tenant::current()` 必须读到鉴权身份——这是插件
/// capability 反调回带 `_identity`、内核派发面（resume_pipeline 等）身份
/// 守卫放行的前提（装机 2026-11-06 resume 全堵实证：只落转发头时 task-local
/// 空，插件反调恒被拒）。匿名端点保持无 scope（fail-closed）。
#[tokio::test]
async fn authed_route_establishes_task_local_identity_scope() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    struct ScopeEcho;
    #[async_trait::async_trait]
    impl HttpHandleCapability for ScopeEcho {
        async fn handle(&self, _req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
            let ctx = agentos_tenant::current();
            let payload = serde_json::json!({
                "tenant": ctx.as_ref().map(|c| c.tenant_id.clone()),
                "user": ctx.as_ref().and_then(|c| c.user_id.clone()),
                "role": ctx.as_ref().and_then(|c| c.role.clone()),
            });
            Ok(HttpHandleResponse {
                status: 200,
                headers: HashMap::new(),
                body: base64::engine::general_purpose::STANDARD
                    .encode(payload.to_string().as_bytes()),
                body_encoding: "base64".to_string(),
            })
        }
    }

    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let password = agentos_http::auth::hash_password("ext-pw").unwrap();
    let record = agentos_core::types::UserRecord {
        user_id: "u-ext-scope".to_string(),
        username: "extscope".to_string(),
        password: password.clone(),
        email: None,
        role: "admin".to_string(),
        tenant_id: "tenant-scope".to_string(),
        created_at: chrono::Utc::now().to_rfc3339(),
        last_login_at: None,
        must_change_password: false,
    };
    agentos_core::traits::StorageBackend::create_user(store.as_ref(), &record)
        .await
        .unwrap();
    let built = agentos_http::auth::BuiltInUser {
        id: record.user_id.clone(),
        username: record.username.clone(),
        password,
        email: String::new(),
        role: "admin".to_string(),
        tenant_id: "tenant-scope".to_string(),
        created_at: String::new(),
        must_change_password: false,
    };
    let token =
        agentos_http::auth::encode_token(agentos_http::auth::TokenType::Access, &built, 3600);

    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let mut ep = endpoint("p1", "GET", "/ext/p1/scope-check");
    ep.auth = Some("user".to_string());
    registry.register_http_route("p1", ep).unwrap();
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(ScopeEcho));
    state.store = Some(store);

    let app = crate::server::build_router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/p1/scope-check")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        v["tenant"], "tenant-scope",
        "task-local 租户必须为鉴权值: {v}"
    );
    assert_eq!(
        v["user"], "u-ext-scope",
        "task-local 用户必须为鉴权值（反调身份前提）"
    );
    assert_eq!(v["role"], "admin");
}

/// 匿名端点（auth:"none"）剥离客户端伪造的身份头（A2：匿名放行 ≠ 信任客户端）。
#[tokio::test]
async fn anonymous_route_strips_spoofed_identity_headers() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    struct HeaderEcho;
    #[async_trait::async_trait]
    impl HttpHandleCapability for HeaderEcho {
        async fn handle(&self, req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
            let payload = serde_json::json!({
                "tenant": req.headers.get("x-agentos-tenant"),
                "user": req.headers.get("x-agentos-user"),
                "role": req.headers.get("x-agentos-role"),
            });
            Ok(HttpHandleResponse {
                status: 200,
                headers: HashMap::new(),
                body: base64::engine::general_purpose::STANDARD
                    .encode(payload.to_string().as_bytes()),
                body_encoding: "base64".to_string(),
            })
        }
    }

    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry
        .register_http_route("p1", endpoint("r", "POST", "/ext/p1/hook"))
        .unwrap();
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(HeaderEcho));
    let app = crate::server::build_router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/ext/p1/hook")
                .header("x-agentos-tenant", "victim-tenant")
                .header("x-agentos-user", "victim-user")
                .header("x-agentos-role", "admin")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert!(
        v["tenant"].is_null() && v["user"].is_null() && v["role"].is_null(),
        "匿名端点必须剥离客户端伪造的身份头: {v}"
    );
}

/// 无 auth 声明 → 401 fail-closed（读写同规）；admin 声明 + 非 admin 角色 → 403。
#[tokio::test]
async fn undeclared_auth_denied_and_admin_role_enforced() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    // 无声明
    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let mut ep = endpoint("r", "GET", "/ext/p1/undeclared");
    ep.auth = None;
    registry.register_http_route("p1", ep).unwrap();
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(NopHandlerForTests));
    let app = crate::server::build_router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/p1/undeclared")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        axum::http::StatusCode::UNAUTHORIZED,
        "无 auth 声明必须 401 fail-closed"
    );

    // admin 声明 + viewer 角色
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let password = agentos_http::auth::hash_password("role-pw").unwrap();
    let record = agentos_core::types::UserRecord {
        user_id: "u-viewer-1".to_string(),
        username: "viewer_x".to_string(),
        password: password.clone(),
        email: None,
        role: "viewer".to_string(),
        tenant_id: agentos_http::auth::DEFAULT_TENANT_ID.to_string(),
        created_at: chrono::Utc::now().to_rfc3339(),
        last_login_at: None,
        must_change_password: false,
    };
    agentos_core::traits::StorageBackend::create_user(store.as_ref(), &record)
        .await
        .unwrap();
    let built = agentos_http::auth::BuiltInUser {
        id: record.user_id.clone(),
        username: record.username.clone(),
        password,
        email: String::new(),
        role: "viewer".to_string(),
        tenant_id: record.tenant_id.clone(),
        created_at: String::new(),
        must_change_password: false,
    };
    let token =
        agentos_http::auth::encode_token(agentos_http::auth::TokenType::Access, &built, 3600);

    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let mut ep = endpoint("r", "GET", "/ext/p1/adminonly");
    ep.auth = Some("admin".to_string());
    registry.register_http_route("p1", ep).unwrap();
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(NopHandlerForTests));
    state.store = Some(store);
    let app = crate::server::build_router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/p1/adminonly")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        axum::http::StatusCode::FORBIDDEN,
        "admin 端点 + viewer 角色必须 403"
    );
}

struct NopHandlerForTests;

#[async_trait::async_trait]
impl HttpHandleCapability for NopHandlerForTests {
    async fn handle(&self, _req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        Ok(HttpHandleResponse {
            status: 200,
            headers: HashMap::new(),
            body: String::new(),
            body_encoding: "base64".to_string(),
        })
    }
}

// ── BUG-58：路由缺席自愈（declared+enabled 注册面静默丢失 → 调用路径恢复）──

/// 构造带 http_endpoints 的 manifest（走 JSON 反序列化，省去手写字段）。
fn manifest_with_endpoints(
    id: &str,
    endpoints: &[(&str, &str, &str, Option<&str>)],
) -> PluginManifest {
    let eps: Vec<serde_json::Value> = endpoints
        .iter()
        .map(|(route_id, method, path, auth)| {
            serde_json::json!({
                "route_id": route_id, "method": method, "path": path,
                "auth": auth, "handler_capability": "http.handle"
            })
        })
        .collect();
    serde_json::from_value(serde_json::json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "system", "language": "python",
        "host_type": "sidecar", "entry": "python server.py",
        "capabilities": {},
        "http_endpoints": eps
    }))
    .expect("valid manifest")
}

/// BUG-58 核心回归：声明面在册（store manifest + 启用集）但注册表条目静默
/// 丢失 → 请求触发调用路径自愈 → 路由恢复并当次分发成功（200 而非 503），
/// 且注册表可查、guard 入 scope（幂等基线，不残留悬空注册）。
#[tokio::test]
async fn exec_ext_request_heals_missing_route_for_declared_plugin() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    // 关键前置：注册表无任何路由（模拟装机版静默丢失后的现场）
    state.capability_registry = Some(registry.clone());
    state.http_handler = Some(Arc::new(NopHandlerForTests));
    let manifest = manifest_with_endpoints(
        "mode_x",
        &[
            ("p1", "GET", "/ext/mode_x/page/panel", Some("none")),
            ("d1", "GET", "/ext/mode_x/data/bootstrap", Some("none")),
        ],
    );
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest]));
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("mode_x".to_string());
    let scopes_keepalive = state.plugin_scopes.clone();
    let app = crate::server::build_router(state);
    // scope 账本须在断言期保活：oneshot 消费 Router → AppState 释放 →
    // PluginScopeRegistry drop = M1 结构性收回（guard 全撤）。生产态 AppState
    // 常驻，此析构只在测试里发生；此处持 Arc 模拟生产态。
    let scopes_for_assert = scopes_keepalive.clone();
    let registry_for_assert = registry;

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/mode_x/data/bootstrap")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        axum::http::StatusCode::OK,
        "自愈恢复路由后当次请求必须分发成功，而非 503"
    );
    assert!(
        registry_for_assert
            .find_http_route("/ext/mode_x/data/bootstrap", "GET")
            .is_some(),
        "自愈后路由必须可查（后续请求直接命中）"
    );
    assert!(
        registry_for_assert
            .find_http_route("/ext/mode_x/page/panel", "GET")
            .is_some(),
        "声明内其余端点一并恢复"
    );
    assert_eq!(
        registry_for_assert.list_http_routes().len(),
        2,
        "恢复条数 = 声明端点数（scope 账本 = {} 条 guard）",
        scopes_for_assert.scope_of("mode_x").len()
    );
}

/// fail-closed 不变：插件未启用（不在启用集）→ 不自愈、仍 404
/// （自愈不得复活被禁用插件的摘除语义）。
#[tokio::test]
async fn exec_ext_request_does_not_heal_disabled_plugin() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    let mut state = AppState::new();
    state.capability_registry = Some(Arc::new(CapabilityRegistryImpl::new()));
    state.http_handler = Some(Arc::new(NopHandlerForTests));
    let manifest =
        manifest_with_endpoints("mode_y", &[("d1", "GET", "/ext/mode_y/data", Some("none"))]);
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest]));
    // enabled_plugin_ids 留空 = 插件已禁用
    let app = crate::server::build_router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/mode_y/data")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::NOT_FOUND);
}

/// 声明无 http_endpoints：声明面零端点 = 平凡完整，miss 属请求与声明面
/// 不匹配 → 404（旧语义 503 会把永不可达的路径标成「瞬态可重试」）。
#[tokio::test]
async fn exec_ext_request_404_when_declaration_has_no_endpoints() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    let mut state = AppState::new();
    state.capability_registry = Some(Arc::new(CapabilityRegistryImpl::new()));
    state.http_handler = Some(Arc::new(NopHandlerForTests));
    let manifest = manifest_with_endpoints("mode_z", &[]);
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest]));
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("mode_z".to_string());
    let app = crate::server::build_router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/mode_z/data")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        axum::http::StatusCode::NOT_FOUND,
        "零声明端点 = 注册面平凡完整，miss 必须回 404 而非可重试 503"
    );
}

/// 注册面完整但请求路径不匹配任何声明端点 → 404（旧语义 503 永续：
/// 装机 R297 resume/cancel 实证——heal 27/27 幂等命中 = 注册面从未缺失，
/// 503+Retry-After 让坏调用方 URL 被当瞬态无限重试且重启不自愈）；
/// 且声明面内健康路由不受影响（同插件相邻请求照常分发）。
#[tokio::test]
async fn exec_ext_request_404_when_registry_intact_and_path_unmatched() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    // 关键前置：声明面**全量在册**（模拟装机 503 时 heal 27/27 幂等命中的现场）
    let manifest = manifest_with_endpoints(
        "task_like",
        &[
            (
                "cancel",
                "POST",
                "/ext/task_like/tasks/{task_id}/cancel",
                Some("none"),
            ),
            (
                "resume",
                "POST",
                "/ext/task_like/tasks/{task_id}/resume",
                Some("none"),
            ),
        ],
    );
    for ep in &manifest.http_endpoints {
        registry
            .register_http_route("task_like", ep.clone())
            .unwrap();
    }
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(NopHandlerForTests));
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest]));
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("task_like".to_string());
    let app = crate::server::build_router(state);

    // 1) 声明面外的路径（多一段，匹配任何模板）→ 404 而非 503
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/ext/task_like/tasks/t1/resume/now")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        axum::http::StatusCode::NOT_FOUND,
        "注册面完整时路径级 miss 必须 404（重试与重启都不可恢复，不得标瞬态 503）"
    );

    // 2) 同插件声明面内的健康路由不受影响 → 200
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/ext/task_like/tasks/t1/resume")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        axum::http::StatusCode::OK,
        "声明面内健康路由必须照常分发"
    );
}

/// 声明面**真缺**（有声明端点无法在册：路径越界被注册期校验拒绝）→ 维持
/// 503+Retry-After 瞬态语义（真生命周期窗与路径级 miss 的分界）。
#[tokio::test]
async fn exec_ext_request_503_persists_when_declared_surface_incomplete() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    let mut state = AppState::new();
    state.capability_registry = Some(Arc::new(CapabilityRegistryImpl::new()));
    state.http_handler = Some(Arc::new(NopHandlerForTests));
    // bad 路径越出 /ext/{plugin_id}/** 命名空间 → heal 注册期校验拒绝，
    // 声明面永远无法完整在册
    let manifest = manifest_with_endpoints(
        "mode_w",
        &[
            ("d1", "GET", "/ext/mode_w/data", Some("none")),
            ("bad", "GET", "/elsewhere/bad", Some("none")),
        ],
    );
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest]));
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("mode_w".to_string());
    let app = crate::server::build_router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/mode_w/unmatched")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        axum::http::StatusCode::SERVICE_UNAVAILABLE,
        "声明面有缺（端点无法在册）必须维持 503 瞬态语义"
    );
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    assert!(String::from_utf8_lossy(&body).contains("temporarily unavailable"));
}

/// 自愈后鉴权闸必须重跑：声明 auth="user" 的路由经自愈恢复后，无 token
/// 请求 → 401（而非借自愈绕过鉴权 200，也不是 503）。
#[tokio::test]
async fn exec_ext_request_reapplies_auth_after_heal() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    let mut state = AppState::new();
    state.capability_registry = Some(Arc::new(CapabilityRegistryImpl::new()));
    state.http_handler = Some(Arc::new(NopHandlerForTests));
    let manifest =
        manifest_with_endpoints("mode_a", &[("d1", "GET", "/ext/mode_a/data", Some("user"))]);
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest]));
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("mode_a".to_string());
    // store 缺席（401 需要验 token 的用户存储）→ resolve_request_user 走
    // 存储缺失路径；此处只断言"不是 200"，即自愈后鉴权确实执行。
    let app = crate::server::build_router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/mode_a/data")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        axum::http::StatusCode::UNAUTHORIZED,
        "自愈恢复的路由必须重过鉴权闸"
    );
}

/// 自愈分支覆盖：启用集命中但 store 无 manifest（manifest 缺席）→ 不动作。
#[tokio::test]
async fn heal_noops_when_manifest_absent_from_store() {
    use crate::routes::AppState;
    let mut state = AppState::new();
    state.capability_registry = Some(Arc::new(CapabilityRegistryImpl::new()));
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("ghost".to_string());
    let healed = crate::plugin_lifecycle::heal_plugin_http_routes(
        state.capability_registry.as_ref().unwrap(),
        &state.plugin_scopes,
        &state.manifests,
        &state.enabled_plugin_ids,
        "ghost",
    )
    .await;
    assert!(!healed, "manifest 缺席时自愈必须不动作");
}

// ── 波尾巡检 + 未在册 404 分支日志（2026-10-15 酒馆面板 404 消缺）──
// 日志断言统一走 [`crate::test_env::capture_logs`]（进程级订阅）：线程级
// set_default 的调用点兴趣缓存是进程级的，并行测试下他线程注册新调用点
// 触发重算会把本线程订阅翻成"无兴趣"短路（G2 门禁新调用点加入后实测
// 必现）——与 test_env::capture_logs 文档化的既有裁定同一口径。

/// 未在册 404 分支必须留痕（曾是零日志排障盲区）：warn 带 message、path、
/// method、解析出的插件 id 与 heal 尝试痕迹。
#[tokio::test]
async fn ext_route_404_for_unknown_plugin_logs_warn_with_path_and_plugin() {
    use crate::routes::AppState;
    use crate::test_env::capture_logs;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    let mut state = AppState::new();
    state.capability_registry = Some(Arc::new(CapabilityRegistryImpl::new()));
    state.http_handler = Some(Arc::new(NopHandlerForTests));
    state.manifests = Arc::new(tokio::sync::RwLock::new(Vec::new()));
    let (_guard, logs) = capture_logs();
    let app = crate::server::build_router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/ghost_mode/page/panel")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::NOT_FOUND);
    let text = logs.text();
    assert!(
        text.contains("plugin not in manifest store or not enabled"),
        "未在册 404 分支必须留下 warn 日志: {text}"
    );
    assert!(
        text.contains("ghost_mode"),
        "日志须带解析出的插件 id: {text}"
    );
    assert!(
        text.contains("/ext/ghost_mode/page/panel") && text.contains("GET"),
        "日志须带 path 与 method（排障最小集）: {text}"
    );
}

/// 波尾巡检状态机锁：波次收回路由后巡检一轮 → 路由集合非空，立即请求
/// 直接分发成功（不再依赖请求路径惰性 heal）。
#[tokio::test]
async fn wave_tail_sweep_restores_routes_and_request_dispatches() {
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    state.capability_registry = Some(registry.clone());
    state.http_handler = Some(Arc::new(NopHandlerForTests));
    let manifest = manifest_with_endpoints(
        "mode_sweep",
        &[("d1", "GET", "/ext/mode_sweep/page/panel", Some("none"))],
    );
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest.clone()]));
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("mode_sweep".to_string());
    // boot 注册 → 波次收回（scope revoke = uninstall/复验语义）。
    crate::plugin_lifecycle::reenable_plugin_capabilities(
        &manifest,
        &registry,
        &state.plugin_scopes,
    );
    state.plugin_scopes.revoke("mode_sweep");
    assert!(
        registry
            .find_http_route("/ext/mode_sweep/page/panel", "GET")
            .is_none(),
        "前置：波次收回后路由缺失"
    );
    // 波尾巡检（watcher sync 收尾同款调用）。
    let (plugins, routes) = crate::plugin_lifecycle::sweep_declared_http_routes(
        &registry,
        &state.plugin_scopes,
        &state.manifests,
        &state.enabled_plugin_ids,
    )
    .await;
    assert_eq!((plugins, routes), (1, 1), "巡检应补回波次摘掉的路由");
    // scope 账本保活：oneshot 消费 Router 后 AppState 析构，guard 全撤会
    // 连带摘路由（既有测试同款保活模式）。
    let _scopes_keepalive = state.plugin_scopes.clone();
    let app = crate::server::build_router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/mode_sweep/page/panel")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        axum::http::StatusCode::OK,
        "波尾巡检后立即请求必须直接分发成功（惰性 heal 窗消除）"
    );
}

/// `register_manifest_http_routes`：scopes 为 Some 时走 guarded 注册并入
/// scope（disable 可撤销）；冲突路由聚合报错而非 panic。
#[test]
fn register_manifest_http_routes_guarded_and_conflicts_aggregate() {
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let scopes = agentos_plugin_loader::PluginScopeRegistry::new();
    // 用 JSON 反序列化构造 manifest（省去手写全部字段，未给字段走 serde default）
    let manifest = |id: &str, path: &str| -> PluginManifest {
        serde_json::from_value(serde_json::json!({
            "id": id, "name": id, "version": "1.0.0",
            "plugin_type": "tool", "language": "python",
            "host_type": "sidecar", "entry": "x",
            "capabilities": {},
            "http_endpoints": [{
                "route_id": "r", "method": "GET", "path": path,
                "auth": "none", "handler_capability": "http.handle"
            }]
        }))
        .expect("valid manifest")
    };

    let errors =
        register_manifest_http_routes(&registry, &[manifest("p1", "/ext/p1/a")], Some(&scopes));
    assert!(errors.is_empty(), "首个注册不得报错: {errors:?}");
    assert!(
        !scopes.scope_of("p1").is_empty(),
        "guarded 注册必须把撤销 guard 登记进 scope"
    );

    // 冲突：同 path+method 第二个插件 → 聚合错误（不 panic）
    let errors =
        register_manifest_http_routes(&registry, &[manifest("p2", "/ext/p1/a")], Some(&scopes));
    assert_eq!(errors.len(), 1, "冲突必须被聚合上报: {errors:?}");
    assert!(errors[0].contains("p2"), "错误须点名插件: {errors:?}");

    // None scopes：走非 guarded 注册，同样返回错误列表
    let registry2 = Arc::new(CapabilityRegistryImpl::new());
    let errors = register_manifest_http_routes(&registry2, &[manifest("p3", "/ext/p3/ok")], None);
    assert!(errors.is_empty());
}

/// `SidecarHttpHandler`：经 invoker 转发 http.handle，解析响应；
/// 调用失败 / success=false / 响应形状非法三档各自报错。
struct ScriptedInvoker {
    result: parking_lot::Mutex<Result<agentos_core::types::ToolExecutionResult, String>>,
}

#[async_trait::async_trait]
impl agentos_core::traits::PluginInvoker for ScriptedInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        _ctx: &agentos_core::types::PluginContext<'a>,
    ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
        unreachable!("本测试不触达管道插件调用")
    }
    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &serde_json::Value,
    ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError> {
        match self.result.lock().clone() {
            Ok(r) => Ok(r),
            Err(msg) => Err(agentos_core::types::PluginError {
                message: msg,
                code: None,
                source: None,
            }),
        }
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

#[tokio::test]
async fn sidecar_http_handler_forwards_and_validates_shape() {
    use agentos_core::types::ToolExecutionResult;

    let req = HttpHandleRequest {
        method: "POST".to_string(),
        path: "/ext/p1/cb".to_string(),
        plugin_id: "p1".to_string(),
        raw_body: String::new(),
        headers: HashMap::new(),
        query: HashMap::new(),
        query_multi: HashMap::new(),
    };

    // 成功路径：{status,headers,body,body_encoding} 原样解析
    let ok = ToolExecutionResult {
        success: true,
        data: serde_json::json!({
            "status": 201,
            "headers": {"content-type": "text/plain"},
            "body": "",
            "body_encoding": "base64"
        }),
        error: None,
        metadata: None,
        duration_ms: None,
    };
    let handler = SidecarHttpHandler::new(Arc::new(ScriptedInvoker {
        result: parking_lot::Mutex::new(Ok(ok)),
    }));
    let resp = handler.handle(req.clone()).await.expect("成功路径应 Ok");
    assert_eq!(resp.status, 201);
    assert_eq!(resp.headers.get("content-type").unwrap(), "text/plain");

    // invoker 调用失败 → Err 透传错误消息
    let handler = SidecarHttpHandler::new(Arc::new(ScriptedInvoker {
        result: parking_lot::Mutex::new(Err("transport down".to_string())),
    }));
    let err = handler
        .handle(req.clone())
        .await
        .expect_err("调用失败应 Err");
    assert_eq!(err, "transport down");

    // success=false → Err 取 error 文案
    let failed = ToolExecutionResult {
        success: false,
        data: serde_json::json!({}),
        error: Some("plugin rejected".to_string()),
        metadata: None,
        duration_ms: None,
    };
    let handler = SidecarHttpHandler::new(Arc::new(ScriptedInvoker {
        result: parking_lot::Mutex::new(Ok(failed.clone())),
    }));
    let err = handler
        .handle(req.clone())
        .await
        .expect_err("success=false 应 Err");
    assert_eq!(err, "plugin rejected");

    // success=false 且无 error 文案 → unknown error 兜底
    let mut no_msg = failed;
    no_msg.error = None;
    let handler = SidecarHttpHandler::new(Arc::new(ScriptedInvoker {
        result: parking_lot::Mutex::new(Ok(no_msg)),
    }));
    let err = handler
        .handle(req.clone())
        .await
        .expect_err("无 error 文案应兜底");
    assert_eq!(err, "unknown error");

    // 响应形状非法（状态码缺失）→ 形状错误文案
    let bad = ToolExecutionResult {
        success: true,
        data: serde_json::json!({"unexpected": true}),
        error: None,
        metadata: None,
        duration_ms: None,
    };
    let handler = SidecarHttpHandler::new(Arc::new(ScriptedInvoker {
        result: parking_lot::Mutex::new(Ok(bad)),
    }));
    let err = handler.handle(req).await.expect_err("形状非法应 Err");
    assert!(
        err.contains("invalid http.handle response shape"),
        "实际: {err}"
    );
}

// ── G2 注册门禁全能力面通用化：插件 HTTP 端点（http_endpoints）同源拦截 ──
// 契约：账本 verify_incomplete/verify_failed（名单现值）内插件的端点
// 一并拒绝（用户裁定 2026-10-26：校验没通过 = 整个插件不许上岗），响应
// 与既有服务不可用类一致（503）；复验通过（账本 ok）自动恢复。

/// 构造挂了遮挡名单查询的 dispatcher（单插件单端点 /ext/p1/x）。
fn gate_test_dispatcher(shadowed: Vec<String>) -> HttpDispatcher {
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry
        .register_http_route("p1", endpoint("r", "GET", "/ext/p1/x"))
        .unwrap();
    let lookup: Arc<dyn Fn() -> Vec<String> + Send + Sync> = Arc::new(move || shadowed.clone());
    HttpDispatcher::new(registry, Arc::new(NopHandlerForTests))
        .with_shadowed_plugin_ids_lookup(Some(lookup))
}

#[tokio::test]
async fn dispatch_http_blocks_verify_pending_plugin() {
    let dispatcher = gate_test_dispatcher(vec!["p1".to_string()]);
    let outcome = dispatch_http(
        &dispatcher,
        "/ext/p1/x",
        "GET",
        vec![],
        HashMap::new(),
        HashMap::new(),
    )
    .await;
    // 拦截判别在此；原因文案（点名插件 + 明确措辞）由 exec 级测试沿
    // 503 body 断言（见 exec_ext_request_maps_verification_blocked_to_503）。
    assert!(
        matches!(outcome, DispatchOutcome::VerificationBlocked(_)),
        "必败端点必须被门禁拦截: {outcome:?}"
    );
}

#[tokio::test]
async fn dispatch_http_reopens_endpoint_after_ledger_ok() {
    // 转正：复验通过（账本 ok，名单清除）后同一端点自动恢复，无需重启。
    let pending = Arc::new(std::sync::Mutex::new(vec!["p1".to_string()]));
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry
        .register_http_route("p1", endpoint("r", "GET", "/ext/p1/x"))
        .unwrap();
    let pending_for_lookup = pending.clone();
    let lookup: Arc<dyn Fn() -> Vec<String> + Send + Sync> =
        Arc::new(move || pending_for_lookup.lock().unwrap().clone());
    let dispatcher = HttpDispatcher::new(registry, Arc::new(NopHandlerForTests))
        .with_shadowed_plugin_ids_lookup(Some(lookup));

    let blocked = dispatch_http(
        &dispatcher,
        "/ext/p1/x",
        "GET",
        vec![],
        HashMap::new(),
        HashMap::new(),
    )
    .await;
    assert!(
        matches!(blocked, DispatchOutcome::VerificationBlocked(_)),
        "待复验期间端点必须被拦: {blocked:?}"
    );

    pending.lock().unwrap().clear();
    let ok = dispatch_http(
        &dispatcher,
        "/ext/p1/x",
        "GET",
        vec![],
        HashMap::new(),
        HashMap::new(),
    )
    .await;
    assert!(
        matches!(ok, DispatchOutcome::Handled(_)),
        "账本 ok 后端点自动恢复: {ok:?}"
    );
}

#[tokio::test]
async fn dispatch_http_unaffected_for_healthy_plugin() {
    // 区分度：名单只含他插件时，健康插件端点照常分发。
    let dispatcher = gate_test_dispatcher(vec!["some_other_plugin".to_string()]);
    let outcome = dispatch_http(
        &dispatcher,
        "/ext/p1/x",
        "GET",
        vec![],
        HashMap::new(),
        HashMap::new(),
    )
    .await;
    assert!(
        matches!(outcome, DispatchOutcome::Handled(_)),
        "健康插件不受名单影响: {outcome:?}"
    );
}

/// 账本条目构造（直接写 g2_consistency，不经 G2 观测链——观测链语义在
/// contract.rs 测试覆盖，此处聚焦门禁消费面）。
fn ledger_entry(id: &str, g2_consistency: &str) -> crate::contract::PluginContractState {
    crate::contract::PluginContractState {
        plugin_id: id.to_string(),
        enabled: true,
        gates: crate::contract::ContractGates {
            manifest_schema_valid: true,
            dep_ok: true,
            g2_consistency: g2_consistency.to_string(),
            smoke_result: "not_covered".to_string(),
            render_decl_valid: "n/a".to_string(),
            runtime_input_violations: 0,
            runtime_output_violations: 0,
            last_error: None,
            rejected_tools: Vec::new(),
            sanitized: None,
            reverified_ts: None,
            registry_disk_diffs: None,
        },
        last_scan_ts: 0,
    }
}

#[tokio::test]
async fn exec_ext_request_maps_verification_blocked_to_503() {
    // 真实账本驱动（AppState 同源装配）：verify_incomplete 与 verify_failed
    // 两组状态都拦（503 + 原因文案），ok 插件端点照常 200。
    use crate::routes::AppState;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry
        .register_http_route("p_inc", endpoint("r", "GET", "/ext/p_inc/x"))
        .unwrap();
    registry
        .register_http_route("p_fail", endpoint("r", "GET", "/ext/p_fail/x"))
        .unwrap();
    registry
        .register_http_route("p_ok", endpoint("r", "GET", "/ext/p_ok/x"))
        .unwrap();
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(NopHandlerForTests));
    let ledger = Arc::new(crate::contract::ContractLedger::new());
    ledger.upsert(ledger_entry("p_inc", "verify_incomplete"));
    ledger.upsert(ledger_entry("p_fail", "verify_failed"));
    ledger.upsert(ledger_entry("p_ok", "ok"));
    let state = state.with_contract_states(ledger);
    let app = crate::server::build_router(state);

    for (path, id, why) in [
        ("/ext/p_inc/x", "p_inc", "verify_incomplete"),
        ("/ext/p_fail/x", "p_fail", "verify_failed"),
    ] {
        let resp = app
            .clone()
            .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            axum::http::StatusCode::SERVICE_UNAVAILABLE,
            "{why} 端点必须 503（服务不可用类）"
        );
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let text = String::from_utf8_lossy(&body);
        assert!(
            text.contains("校验未完成或校验失败") && text.contains("已拦截"),
            "{why} 503 必须带明确原因: {text}"
        );
        assert!(text.contains(id), "原因必须点名插件 {id}: {text}");
    }

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/p_ok/x")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::OK, "ok 插件照常服务");
}
