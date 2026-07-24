//! P3-1: PluginManifest.http_endpoints 字段 serde 测试（TDD RED）。
//!
//! 验证 manifest 的 http_endpoints 声明可正确序列化/反序列化。
//! 设计依据：ADR §3.3 + 附录 E.1.2（http_endpoints: route_id/method/path/auth/
//! handler_capability/timeout_ms/max_concurrency/description）。

use agentos_core::traits::{HttpEndpoint, HostType, PluginManifest, PluginType};

/// 反序列化含 http_endpoints 的 manifest——应解析出企微双方法（GET 验证 + POST 回调）。
/// 设计依据：ADR §3.3 示例（同 path 不同 method 不冲突）。
#[test]
fn test_manifest_deserializes_http_endpoints() {
    let json = r#"{
        "id": "channel_wecom",
        "name": "WeCom Channel",
        "version": "1.0.0",
        "plugin_type": "system",
        "language": "python",
        "host_type": "sidecar",
        "entry": "python server.py",
        "capabilities": {},
        "http_endpoints": [
            {
                "route_id": "wecom_callback",
                "method": "POST",
                "path": "/ext/wecom/callback",
                "auth": "none",
                "handler_capability": "http.handle",
                "timeout_ms": 30000,
                "max_concurrency": 16,
                "description": "企微事件回调入口"
            },
            {
                "route_id": "wecom_verify",
                "method": "GET",
                "path": "/ext/wecom/callback",
                "auth": "none",
                "handler_capability": "http.handle",
                "description": "企微 URL 验证（回 echostr 明文）"
            }
        ]
    }"#;

    let manifest: PluginManifest = serde_json::from_str(json).expect("manifest must parse");

    assert_eq!(manifest.http_endpoints.len(), 2);
    let post = &manifest.http_endpoints[0];
    assert_eq!(post.route_id, "wecom_callback");
    assert_eq!(post.method, "POST");
    assert_eq!(post.path, "/ext/wecom/callback");
    assert_eq!(post.auth, "none");
    assert_eq!(post.handler_capability, "http.handle");
    assert_eq!(post.timeout_ms, Some(30000));
    assert_eq!(post.max_concurrency, Some(16));
    assert_eq!(post.description.as_deref(), Some("企微事件回调入口"));

    // GET 验证方法省略 timeout_ms/max_concurrency —— 应为 None
    let get = &manifest.http_endpoints[1];
    assert_eq!(get.method, "GET");
    assert_eq!(get.timeout_ms, None);
    assert_eq!(get.max_concurrency, None);
}

/// 未声明 http_endpoints 的旧 manifest 应向后兼容（http_endpoints 为空 vec）。
#[test]
fn test_manifest_without_http_endpoints_defaults_empty() {
    let json = r#"{
        "id": "memory",
        "name": "Memory",
        "version": "1.0.0",
        "plugin_type": "system",
        "language": "python",
        "host_type": "sidecar",
        "entry": "python server.py",
        "capabilities": {}
    }"#;

    let manifest: PluginManifest = serde_json::from_str(json).expect("manifest must parse");
    assert!(
        manifest.http_endpoints.is_empty(),
        "missing http_endpoints defaults to empty"
    );
}

/// 序列化时空 http_endpoints 应被省略（保持向后兼容的 wire 格式）。
#[test]
fn test_empty_http_endpoints_omitted_in_serialization() {
    let manifest = PluginManifest {
        id: "p".to_string(),
        name: "P".to_string(),
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
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
    };

    let serialized = serde_json::to_string(&manifest).expect("serialize");
    assert!(
        !serialized.contains("http_endpoints"),
        "empty http_endpoints should be omitted, got: {serialized}"
    );
}

/// auth 字段仅接受 none/user/admin（serde 校验）。
#[test]
fn test_http_endpoint_auth_invalid_value_rejected() {
    let json = r#"{
        "id": "p", "name": "P", "version": "1.0.0",
        "plugin_type": "system", "language": "python",
        "host_type": "sidecar", "entry": "python server.py", "capabilities": {},
        "http_endpoints": [
            {"route_id": "r", "method": "GET", "path": "/ext/p/x",
             "auth": "bogus", "handler_capability": "http.handle"}
        ]
    }"#;

    let result: Result<PluginManifest, _> = serde_json::from_str(json);
    assert!(
        result.is_err(),
        "invalid auth value must be rejected by serde"
    );
}

/// HttpEndpoint 可独立构造（验证字段可见性/Default）。
#[test]
fn test_http_endpoint_struct_constructible() {
    let ep = HttpEndpoint {
        route_id: "r".to_string(),
        method: "GET".to_string(),
        path: "/ext/p/x".to_string(),
        auth: "none".to_string(),
        handler_capability: "http.handle".to_string(),
        timeout_ms: None,
        max_concurrency: None,
        description: None,
    };
    assert_eq!(ep.route_id, "r");
    assert_eq!(ep.method, "GET");
}
