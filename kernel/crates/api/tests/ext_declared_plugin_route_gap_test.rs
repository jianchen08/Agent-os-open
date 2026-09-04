// @feature: FP-0.2.七 路由收敛 | @ci: rust-test
//! /ext 通配分发「声明面在册但路由缺席」语义测试（TDD RED）。
//!
//! 插件生命周期存在路由空窗：G2 复验重注册（scopes.revoke → guarded 重注册，
//! 静默无日志）、enable/disable 切换、空闲卸载后的再注册间隙。窗口内
//! `find_http_route` 落空，分发器回裸 404——前端把 404 当「路由不存在」
//! 常态化报错（2026-09-04 实测 task_service 任务页反复 404）。
//!
//! 契约：
//! - manifest 在册 **且** 插件启用，但路由缺席 → 503 + Retry-After（瞬态不可用，
//!   前端按可重试处理），并以 warn 留痕（窗口此前零观测）；
//! - manifest 不在册（未知插件）或已禁用 → 404（语义不变）；
//! - 路由在册 → 正常分发（既有测试覆盖，此处不重复）。

use std::collections::HashMap;
use std::sync::Arc;

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_core::traits::{
    CapabilityRegistry, HostType, HttpEndpoint, HttpHandleCapability, HttpHandleRequest,
    HttpHandleResponse, PluginManifest, PluginType,
};
use agentos_plugin_loader::CapabilityRegistryImpl;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

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

fn endpoint(method: &str, path: &str) -> HttpEndpoint {
    HttpEndpoint {
        route_id: "r".to_string(),
        method: method.to_string(),
        path: path.to_string(),
        auth: "none".to_string(),
        handler_capability: "http.handle".to_string(),
        timeout_ms: None,
        max_concurrency: None,
        description: None,
    }
}

fn manifest_with_endpoint(plugin_id: &str, method: &str, path: &str) -> PluginManifest {
    PluginManifest {
        id: plugin_id.to_string(),
        name: plugin_id.to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::System,
        pipeline_role: None,
        language: "python".to_string(),
        host_type: HostType::Sidecar,
        host_group: None,
        entry: "python server.py".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        granted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![endpoint(method, path)],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        export_fields: vec![],
        provides: None,
    }
}

/// AppState：dispatcher 资源齐备；manifests/enabled 由调用方注入。
/// `register_route`：false 模拟「声明面在册但路由被摘」（生命周期空窗）。
fn make_state(manifests: Vec<PluginManifest>, enabled: Vec<&str>, register_route: bool) -> AppState {
    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    if register_route {
        for m in &manifests {
            for ep in &m.http_endpoints {
                registry
                    .register_http_route(&m.id, ep.clone())
                    .expect("路由注册应成功");
            }
        }
    }
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(NopHandler));
    state.manifests = Arc::new(tokio::sync::RwLock::new(manifests));
    state.enabled_plugin_ids = Arc::new(tokio::sync::RwLock::new(
        enabled.into_iter().map(String::from).collect(),
    ));
    state
}

async fn get_status(app: axum::Router, path: &str) -> (StatusCode, Option<String>) {
    let resp = app
        .oneshot(
            Request::builder()
                .uri(path)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let retry_after = resp
        .headers()
        .get("retry-after")
        .and_then(|v| v.to_str().ok())
        .map(String::from);
    (resp.status(), retry_after)
}

/// 声明面在册 + 启用，但路由缺席（生命周期空窗）→ 503 + Retry-After，不是裸 404。
#[tokio::test]
async fn declared_enabled_plugin_missing_route_returns_503() {
    let state = make_state(
        vec![manifest_with_endpoint("svc", "GET", "/ext/svc/ping")],
        vec!["svc"],
        false,
    );
    let (status, retry_after) = get_status(build_router(state), "/ext/svc/ping").await;
    assert_eq!(
        status,
        StatusCode::SERVICE_UNAVAILABLE,
        "声明面在册且启用的插件路由缺席是瞬态不可用（可重试），不得回裸 404"
    );
    assert!(
        retry_after.is_some(),
        "503 必须携带 Retry-After（前端/代理按可重试语义处理）"
    );
}

/// 未知插件（manifest 不在册）→ 404 语义不变。
#[tokio::test]
async fn undeclared_plugin_missing_route_returns_404() {
    let state = make_state(
        vec![manifest_with_endpoint("svc", "GET", "/ext/svc/ping")],
        vec!["svc"],
        false,
    );
    let (status, _) = get_status(build_router(state), "/ext/ghost/ping").await;
    assert_eq!(status, StatusCode::NOT_FOUND, "未知插件保持 404");
}

/// 已禁用插件（manifest 在册但不在启用集）→ 404 语义不变。
#[tokio::test]
async fn disabled_plugin_missing_route_returns_404() {
    let state = make_state(
        vec![manifest_with_endpoint("svc", "GET", "/ext/svc/ping")],
        vec![], // svc 未启用
        false,
    );
    let (status, _) = get_status(build_router(state), "/ext/svc/ping").await;
    assert_eq!(status, StatusCode::NOT_FOUND, "禁用插件保持 404");
}

/// 声明面在册且启用，路由在册（非空窗）→ 正常分发（NopHandler 200），
/// 503 分支不得误伤正常路径。
#[tokio::test]
async fn registered_route_still_dispatches_normally() {
    let state = make_state(
        vec![manifest_with_endpoint("svc", "GET", "/ext/svc/ping")],
        vec!["svc"],
        true,
    );
    let (status, _) = get_status(build_router(state), "/ext/svc/ping").await;
    assert_eq!(status, StatusCode::OK, "路由在册时正常分发，不受 503 分支影响");
}
