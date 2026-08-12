// @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @audit: T5#16 | @ci: rust-test
//! P3-1/P3-3: CapabilityRegistry http_routes 维测试（TDD RED）。
//!
//! 验证：
//! - 注册/查询 http_routes
//! - path+method 冲突检测（fail-closed 聚合报错）
//! - 命名空间强制（/ext/{plugin_id}/** 前缀）+ denylist 越界拒绝
//!
//! 设计依据：ADR §3.3 + 附录 E.1.3（路由治理）。

use agentos_core::traits::{CapabilityRegistry, HttpEndpoint};
use agentos_plugin_loader::CapabilityRegistryImpl;

fn make_endpoint(route_id: &str, method: &str, path: &str) -> HttpEndpoint {
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

/// 注册单个 http route 后可查询出来（含 plugin_id 归属）。
#[test]
fn test_register_and_list_http_route() {
    let registry = CapabilityRegistryImpl::new();
    let ep = make_endpoint("wecom_callback", "POST", "/ext/channel_wecom/callback");
    let report = registry.register_http_route("channel_wecom", ep);

    assert!(report.is_ok(), "valid route should register");
    let routes = registry.list_http_routes();
    assert_eq!(routes.len(), 1);
    assert_eq!(routes[0].plugin_id, "channel_wecom");
    assert_eq!(routes[0].endpoint.path, "/ext/channel_wecom/callback");
    assert_eq!(routes[0].endpoint.method, "POST");
}

/// 同 path 不同 method 不冲突（企微 GET 验证 + POST 回调）。
#[test]
fn test_same_path_different_method_not_conflict() {
    let registry = CapabilityRegistryImpl::new();
    let post = make_endpoint("wecom_callback", "POST", "/ext/channel_wecom/callback");
    let get = make_endpoint("wecom_verify", "GET", "/ext/channel_wecom/callback");

    assert!(registry.register_http_route("channel_wecom", post).is_ok());
    assert!(registry.register_http_route("channel_wecom", get).is_ok());
    assert_eq!(registry.list_http_routes().len(), 2);
}

/// 同 path+method 冲突 → fail-closed：后者注册返回错误，不静默覆盖。
#[test]
fn test_conflict_same_path_and_method_rejected() {
    let registry = CapabilityRegistryImpl::new();
    let ep1 = make_endpoint("cb1", "POST", "/ext/channel_wecom/callback");
    let ep2 = make_endpoint("cb2", "POST", "/ext/channel_wecom/callback");

    assert!(registry.register_http_route("channel_wecom", ep1).is_ok());
    let result = registry.register_http_route("channel_wecom", ep2);
    assert!(result.is_err(), "conflicting route must be rejected");
    let err = result.unwrap_err();
    assert!(
        err.to_string().contains("conflict") || err.to_string().contains("冲突"),
        "error should mention conflict, got: {err}"
    );
}

/// 非 /ext/{plugin_id}/** 前缀 → 拒绝（命名空间强制）。
#[test]
fn test_namespace_violation_rejected() {
    let registry = CapabilityRegistryImpl::new();
    // path 不以 /ext/ 开头
    let ep = make_endpoint("r", "GET", "/wecom/callback");
    let result = registry.register_http_route("channel_wecom", ep);
    assert!(result.is_err(), "non-/ext/ path must be rejected");

    // path 是 /ext/ 但不跟 plugin_id
    let ep2 = make_endpoint("r", "GET", "/ext/other_plugin/x");
    let result2 = registry.register_http_route("channel_wecom", ep2);
    assert!(
        result2.is_err(),
        "path under different plugin_id namespace must be rejected"
    );
}

/// denylist 越界（/ws、/api/v1/*、/health）→ 拒绝。
#[test]
fn test_denylist_paths_rejected() {
    let registry = CapabilityRegistryImpl::new();

    // /ws
    let ep_ws = make_endpoint("r", "GET", "/ext/channel_wecom/ws");
    assert!(
        registry
            .register_http_route("channel_wecom", ep_ws)
            .is_err(),
        "/ws reserved path must be rejected"
    );

    // /api/v1/*
    let ep_api = make_endpoint("r", "GET", "/ext/channel_wecom/api/v1/x");
    assert!(
        registry
            .register_http_route("channel_wecom", ep_api)
            .is_err(),
        "/api/v1 reserved path must be rejected"
    );

    // /health
    let ep_health = make_endpoint("r", "GET", "/ext/channel_wecom/health");
    assert!(
        registry
            .register_http_route("channel_wecom", ep_health)
            .is_err(),
        "/health reserved path must be rejected"
    );
}

/// 注销插件后其 http routes 一并清除（clear_plugin）。
#[test]
fn test_clear_plugin_removes_http_routes() {
    let registry = CapabilityRegistryImpl::new();
    let ep = make_endpoint("cb", "POST", "/ext/channel_wecom/callback");
    registry.register_http_route("channel_wecom", ep).unwrap();
    assert_eq!(registry.list_http_routes().len(), 1);

    registry.clear_plugin("channel_wecom");
    assert_eq!(registry.list_http_routes().len(), 0);
}

/// 按 path+method 查询单个 route（dispatcher 用）。
#[test]
fn test_find_http_route_by_path_method() {
    let registry = CapabilityRegistryImpl::new();
    let post = make_endpoint("cb", "POST", "/ext/channel_wecom/callback");
    let get = make_endpoint("vf", "GET", "/ext/channel_wecom/callback");
    registry.register_http_route("channel_wecom", post).unwrap();
    registry.register_http_route("channel_wecom", get).unwrap();

    let found = registry.find_http_route("/ext/channel_wecom/callback", "POST");
    assert!(found.is_some());
    assert_eq!(found.unwrap().endpoint.route_id, "cb");

    let found_get = registry.find_http_route("/ext/channel_wecom/callback", "GET");
    assert_eq!(found_get.unwrap().endpoint.route_id, "vf");

    let none = registry.find_http_route("/ext/channel_wecom/callback", "DELETE");
    assert!(none.is_none());
}

/// timeout_ms/max_concurrency 默认值生效（None → 默认 30000/16）。
#[test]
fn test_http_route_defaults_applied() {
    let registry = CapabilityRegistryImpl::new();
    let ep = make_endpoint("cb", "POST", "/ext/channel_wecom/callback");
    // 声明时不带 timeout/concurrency
    registry.register_http_route("channel_wecom", ep).unwrap();

    let route = registry
        .find_http_route("/ext/channel_wecom/callback", "POST")
        .unwrap();
    // 解析后的 route 应带默认值
    assert_eq!(route.timeout_ms(), 30000);
    assert_eq!(route.max_concurrency(), 16);
}
