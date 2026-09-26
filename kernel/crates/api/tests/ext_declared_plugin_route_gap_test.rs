// @feature: FP-0.2.七 路由收敛 | @ci: rust-test
//! /ext 通配分发「声明面在册但路由缺席」语义测试。
//!
//! 插件生命周期存在路由空窗：G2 复验重注册（scopes.revoke → guarded 重注册）、
//! enable/disable 切换、装机版实测的注册面静默丢失（BUG-58：主城数据面路由
//! 缺席直至重启）。窗口内 `find_http_route` 落空。
//!
//! 契约（BUG-58 起，对齐 BUG-37 工具自愈）：
//! - manifest 在册 **且** 插件启用，但路由缺席 → 调用路径自愈：按声明 manifest
//!   重注册后当次分发成功（200）；路由进注册表，后续请求直接命中；
//! - manifest 在册且启用，但声明无 http_endpoints（自愈无物可恢复）→
//!   503 + Retry-After（瞬态不可用，前端按可重试处理），并以 warn 留痕；
//! - manifest 不在册（未知插件）或已禁用 → 404（fail-closed 语义不变）；
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
        auth: Some("none".to_string()),
        handler_capability: "http.handle".to_string(),
        timeout_ms: None,
        max_concurrency: None,
        description: None,
    }
}

fn manifest_with_endpoint(plugin_id: &str, method: &str, path: &str) -> PluginManifest {
    let mut m = manifest_without_endpoints(plugin_id);
    m.http_endpoints = vec![endpoint(method, path)];
    m
}

fn manifest_without_endpoints(plugin_id: &str) -> PluginManifest {
    PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
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
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        restricted_capabilities: Vec::new(),
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        export_fields: vec![],
        provides: None,
    }
}

/// AppState：dispatcher 资源齐备；manifests/enabled 由调用方注入。
/// `register_route`：false 模拟「声明面在册但路由被摘」（生命周期空窗）。
fn make_state(
    manifests: Vec<PluginManifest>,
    enabled: Vec<&str>,
    register_route: bool,
) -> AppState {
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
        .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let retry_after = resp
        .headers()
        .get("retry-after")
        .and_then(|v| v.to_str().ok())
        .map(String::from);
    (resp.status(), retry_after)
}

/// 声明面在册 + 启用，但路由缺席 → 调用路径自愈：按声明重注册后当次分发
/// 成功（BUG-58），不再是持续到重启的 503。
#[tokio::test]
async fn declared_enabled_plugin_missing_route_self_heals() {
    let state = make_state(
        vec![manifest_with_endpoint("svc", "GET", "/ext/svc/ping")],
        vec!["svc"],
        false,
    );
    let (status, retry_after) = get_status(build_router(state), "/ext/svc/ping").await;
    assert_eq!(
        status,
        StatusCode::OK,
        "声明面在册且启用的插件路由缺席必须自愈并当次分发（BUG-58），不得持续 503"
    );
    assert!(
        retry_after.is_none(),
        "自愈成功走正常分发，不得携带 Retry-After"
    );
}

/// 声明在册且启用，但声明本身无 http_endpoints（自愈无物可恢复）→
/// 503 + Retry-After（瞬态语义保留），不是裸 404。
#[tokio::test]
async fn declared_plugin_without_endpoints_still_returns_503() {
    let state = make_state(vec![manifest_without_endpoints("svc")], vec!["svc"], false);
    let (status, retry_after) = get_status(build_router(state), "/ext/svc/ping").await;
    assert_eq!(
        status,
        StatusCode::SERVICE_UNAVAILABLE,
        "声明无端点时自愈无物可恢复，维持瞬态 503"
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
    assert_eq!(
        status,
        StatusCode::OK,
        "路由在册时正常分发，不受 503 分支影响"
    );
}

// ── BUG-67：并发自愈不得互相绞杀 ──────────────────────────────────────────
//
// 装机版实证（R239/R244 部署窗 kernel.log 13:27:56~13:28:07Z）：模式面板
// 打开时并行发出多个 /ext 数据请求，各自触发调用路径自愈；heal 经
// reenable（全量 revoke + 重建）执行——并发 heal 互相摘除对方刚注册的
// 路由，同一请求 heal 后重试仍 miss → 503 持续。日志形态：每请求
// 「12 条 route registered → 自愈行 → even after heal attempt WARN」。

/// 12 端点 manifest（对齐装机版 mode_learning 的声明面规模）。
fn mode_pack_manifest(id: &str) -> PluginManifest {
    let paths: Vec<String> = (0..12)
        .map(|i| {
            if i % 3 == 2 {
                format!("/ext/{id}/data/actions/act{i}")
            } else {
                format!("/ext/{id}/data/view{i}")
            }
        })
        .collect();
    let eps: Vec<HttpEndpoint> = paths
        .iter()
        .map(|p| endpoint(if p.contains("actions") { "POST" } else { "GET" }, p))
        .collect();
    let mut m = manifest_without_endpoints(id);
    m.http_endpoints = eps;
    m
}

/// 并发 heal 风暴：R 轮 × N 个并行「请求单元」（find → miss → heal →
/// 重试 find），断言零次「自愈后重试仍 miss」（BUG-67 签名）且终态全量可查。
/// 修复语义 = 自愈 additive（只补缺失，不收回在册面）：任何一轮内，先恢复
/// 的路由不被后续并发自愈摘除，重试必然命中。
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_heals_do_not_defeat_each_other_retry() {
    const ROUNDS: usize = 8;
    const REQUESTS: usize = 6;
    let id = "mode_hstorm";
    let m = mode_pack_manifest(id);
    let paths: Vec<(String, String)> = m
        .http_endpoints
        .iter()
        .map(|ep| (ep.method.clone(), ep.path.clone()))
        .collect();

    let registry = Arc::new(CapabilityRegistryImpl::new());
    let scopes = Arc::new(agentos_plugin_loader::PluginScopeRegistry::new());
    let manifests: Arc<tokio::sync::RwLock<Vec<PluginManifest>>> =
        Arc::new(tokio::sync::RwLock::new(vec![m]));
    let enabled_ids = Arc::new(tokio::sync::RwLock::new(std::collections::HashSet::from([
        id.to_string(),
    ])));

    let mut self_defeats: Vec<String> = Vec::new();
    for _round in 0..ROUNDS {
        // 每轮重置回生命周期空窗（摘除上一轮 guard，路由全量缺席）。
        scopes.revoke(id);
        assert!(
            registry.find_http_route(&paths[0].1, &paths[0].0).is_none(),
            "前置：本轮路由应缺席"
        );
        let barrier = Arc::new(tokio::sync::Barrier::new(REQUESTS));
        let mut tasks = Vec::new();
        for req in 0..REQUESTS {
            let registry = Arc::clone(&registry);
            let scopes = Arc::clone(&scopes);
            let manifests = Arc::clone(&manifests);
            let enabled_ids = Arc::clone(&enabled_ids);
            let barrier = Arc::clone(&barrier);
            let (method, path) = paths[req % paths.len()].clone();
            tasks.push(tokio::spawn(async move {
                barrier.wait().await;
                // 请求单元：find → miss → heal → 重试 find。
                if registry.find_http_route(&path, &method).is_none() {
                    let healed = agentos_api::plugin_lifecycle::heal_plugin_http_routes(
                        &registry,
                        &scopes,
                        &manifests,
                        &enabled_ids,
                        id,
                    )
                    .await;
                    if registry.find_http_route(&path, &method).is_none() {
                        return format!(
                            "self-defeated: healed={healed} but retry missed {method} {path}"
                        );
                    }
                }
                String::new()
            }));
        }
        for t in tasks {
            let verdict = t.await.expect("请求单元任务不得 panic");
            if !verdict.is_empty() {
                self_defeats.push(verdict);
            }
        }
    }
    assert!(
        self_defeats.is_empty(),
        "并发自愈不得互相绞杀（heal 后重试必须命中），实反例 {}/{}: {self_defeats:?}",
        self_defeats.len(),
        ROUNDS * REQUESTS
    );
}
