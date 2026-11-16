// @feature: FP-0.2.七 路由收敛 | @vision: V6 可即用 | @ci: rust-test
//! 阶段3 遗留：后端静态资源托管（插件可带完整 SPA）—— TDD RED。
//!
//! 验证：插件可声明一个 web/ 子目录，内核自动托管在
//! `/ext/{pluginId}/assets/{*path}`，免去为每个子资源单独声明 http_endpoints。
//!
//! 设计：
//! - 不引入 tower_http（全仓零依赖、编译开销最小）；
//! - 在 `/ext/{*rest}` 通配 dispatcher 内加静态文件直读分支，先于路由分发尝试读文件；
//! - Content-Type 由扩展名映射表推断（text/html、application/javascript、text/css …）；
//! - 路径安全：拒绝 `..` 逃逸、canonicalize 后必须仍在插件 web/ 子树内。

use std::collections::HashMap;
use std::sync::Arc;

use agentos_api::http_dispatcher::{dispatch_http, DispatchOutcome, HttpDispatcher};
use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_core::traits::{HttpHandleCapability, HttpHandleRequest, HttpHandleResponse};
use agentos_plugin_loader::CapabilityRegistryImpl;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

/// 简单的 in-process handler：用于让 dispatcher 资源齐备（capability_registry + http_handler）。
/// 静态资源分支在它之前生效，所以本测试不会真正调用它。
struct NopHandler;

#[async_trait::async_trait]
impl HttpHandleCapability for NopHandler {
    async fn handle(&self, _req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        Ok(HttpHandleResponse {
            status: 200,
            headers: HashMap::new(),
            body: String::new(),
            body_encoding: "base64".to_string(),
        })
    }
}

/// 构造插件根 fixture：`<root>/<PLUGIN_ID>/{web/,plugin.json}`（装机布局形态，
/// loader discover 可发现）。返回根目录（供 loader 扫描）。
fn make_plugin_with_web() -> tempfile::TempDir {
    let dir = tempfile::tempdir().expect("tempdir");
    let pkg = dir.path().join(PLUGIN_ID);
    std::fs::create_dir_all(&pkg).unwrap();
    std::fs::write(
        pkg.join("plugin.json"),
        format!(
            r#"{{"id":"{PLUGIN_ID}","name":"{PLUGIN_ID}","version":"1.0.0",
                "plugin_type":"tool","language":"python","host_type":"sidecar",
                "entry":"python server.py","capabilities":{{}}}}"#
        ),
    )
    .unwrap();
    let web = pkg.join("web");
    std::fs::create_dir_all(&web).unwrap();
    std::fs::write(
        web.join("index.html"),
        "<!doctype html><html><body><h1>SPA Root</h1></body></html>",
    )
    .unwrap();
    std::fs::write(
        web.join("app.js"),
        "console.log('app loaded'); export default {};",
    )
    .unwrap();
    std::fs::write(web.join("style.css"), "body { font-family: sans-serif; }").unwrap();
    // 子目录文件（验证多段路径 /ext/{plugin}/assets/sub/deep.json）
    std::fs::create_dir_all(web.join("sub")).unwrap();
    std::fs::write(web.join("sub").join("deep.json"), "{\"k\":\"v\"}").unwrap();
    dir
}

/// 构造带插件目录活真源（loader 活对象）的 AppState。
async fn make_state(plugin_id: &str, plugins_root: &std::path::Path) -> AppState {
    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(NopHandler));
    let loader = agentos_api::test_env::discovered_plugin_loader(plugins_root).await;
    assert!(
        loader.plugin_dir(plugin_id).is_some(),
        "fixture 插件必须被 loader 发现"
    );
    state.plugin_loader = Some(loader);
    state
}

const PLUGIN_ID: &str = "demo_spa_plugin";

// ── 200 + text/html + 正确内容 ───────────────────────────────

#[tokio::test]
async fn test_static_asset_serves_index_html() {
    let tmp = make_plugin_with_web();
    let state = make_state(PLUGIN_ID, tmp.path()).await;
    let app = build_router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .uri(format!("/ext/{PLUGIN_ID}/assets/index.html"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let ct = resp
        .headers()
        .get("content-type")
        .expect("content-type header")
        .to_str()
        .unwrap()
        .to_string();
    assert!(ct.starts_with("text/html"), "expected text/html, got {ct}");
    let body = axum::body::to_bytes(resp.into_body(), 65536).await.unwrap();
    let text = String::from_utf8(body.to_vec()).unwrap();
    assert!(
        text.contains("SPA Root"),
        "body should contain SPA Root: {text}"
    );
}

// ── 200 + application/javascript ─────────────────────────────

#[tokio::test]
async fn test_static_asset_serves_js_with_correct_mime() {
    let tmp = make_plugin_with_web();
    let state = make_state(PLUGIN_ID, tmp.path()).await;
    let app = build_router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .uri(format!("/ext/{PLUGIN_ID}/assets/app.js"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let ct = resp
        .headers()
        .get("content-type")
        .expect("content-type header")
        .to_str()
        .unwrap()
        .to_string();
    assert!(
        ct.starts_with("application/javascript") || ct.starts_with("text/javascript"),
        "expected javascript mime, got {ct}"
    );
}

// ── 200 + text/css ───────────────────────────────────────────

#[tokio::test]
async fn test_static_asset_serves_css_with_correct_mime() {
    let tmp = make_plugin_with_web();
    let state = make_state(PLUGIN_ID, tmp.path()).await;
    let app = build_router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .uri(format!("/ext/{PLUGIN_ID}/assets/style.css"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let ct = resp
        .headers()
        .get("content-type")
        .expect("content-type header")
        .to_str()
        .unwrap()
        .to_string();
    assert!(ct.starts_with("text/css"), "expected text/css, got {ct}");
}

// ── 多段路径（sub/deep.json）────────────────────────────────

#[tokio::test]
async fn test_static_asset_serves_nested_path() {
    let tmp = make_plugin_with_web();
    let state = make_state(PLUGIN_ID, tmp.path()).await;
    let app = build_router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .uri(format!("/ext/{PLUGIN_ID}/assets/sub/deep.json"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let ct = resp
        .headers()
        .get("content-type")
        .expect("content-type header")
        .to_str()
        .unwrap()
        .to_string();
    assert!(
        ct.starts_with("application/json"),
        "expected application/json, got {ct}"
    );
}

// ── 404 文件不存在 ───────────────────────────────────────────

#[tokio::test]
async fn test_static_asset_nonexistent_returns_404() {
    let tmp = make_plugin_with_web();
    let state = make_state(PLUGIN_ID, tmp.path()).await;
    let app = build_router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .uri(format!("/ext/{PLUGIN_ID}/assets/nonexistent.html"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

// ── 路径逃逸防护：.. 应被拒绝 ───────────────────────────────

#[tokio::test]
async fn test_static_asset_rejects_path_traversal() {
    let tmp = make_plugin_with_web();
    let state = make_state(PLUGIN_ID, tmp.path()).await;
    let app = build_router(state);

    // /ext/{plugin}/assets/../../plugin.json —— 试图逃出 web/ 读 plugin.json
    let resp = app
        .oneshot(
            Request::builder()
                .uri(format!("/ext/{PLUGIN_ID}/assets/../../../../etc/passwd"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    // 必须 404，绝不能 200 + /etc/passwd 内容
    assert_eq!(
        resp.status(),
        StatusCode::NOT_FOUND,
        "path traversal must be rejected with 404"
    );
}

// ── 未登记插件不影响 dispatcher 行为 ─────────────────

#[tokio::test]
async fn test_static_asset_unknown_plugin_falls_through_to_dispatcher() {
    // loader 路径表未登记的插件 → 静态分支不命中 → 走 dispatcher → 404
    let tmp = make_plugin_with_web();
    let state = make_state(PLUGIN_ID, tmp.path()).await;
    let app = build_router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ext/unknown_plugin/assets/index.html")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

// ── 非 assets 路径不被静态分支拦截 ──────────────────────────

#[tokio::test]
async fn test_static_asset_only_intercepts_assets_subpath() {
    // /ext/{plugin}/foo（非 assets）不应被静态分支处理，应走 dispatcher（404）
    let tmp = make_plugin_with_web();
    let state = make_state(PLUGIN_ID, tmp.path()).await;
    let app = build_router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .uri(format!("/ext/{PLUGIN_ID}/not-assets/index.html"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

// ── dispatch_http 不受影响（回归保障：现有契约不变）────────

#[tokio::test]
async fn test_dispatch_http_still_works_alongside_static_assets() {
    // 验证引入静态分支后，原有 dispatch_http（无路由 → NotFound）行为未回归。
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let dispatcher = HttpDispatcher::new(registry, Arc::new(NopHandler));
    let outcome = dispatch_http(
        &dispatcher,
        "/ext/whatever/x",
        "GET",
        vec![],
        HashMap::new(),
        HashMap::new(),
    )
    .await;
    assert!(
        matches!(outcome, DispatchOutcome::NotFound),
        "dispatch_http should still return NotFound for unknown route, got {outcome:?}"
    );
}
