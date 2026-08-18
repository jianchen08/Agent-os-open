// @feature: FP-0.2.七 路由收敛 | @vision: V6 可即用 | @ci: rust-test
//! 阶段3(后端):POST /api/v1/actions/execute 端点集成测试(TDD)。
//!
//! 前端 GrowthLoop.ts 已注入 transport,把命令面板/快捷键/菜单触发统一 POST 到
//! `/api/v1/actions/execute`,body 为 `{ action: <commandId>, args: {...} }`。
//! 本测试验证端点存在并闭合链路:
//! - 未知 command → 404
//! - 已知 command(某 manifest contributes.commands 声明)→ 200 + success
//! - 请求体缺 action 字段 → 400

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_core::traits::{HostType, PluginManifest, PluginType};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

/// 登录内置 admin（无 store 时回退内置用户表）返回 access_token。
async fn admin_token(app: &axum::Router) -> String {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(axum::http::Method::POST)
                .uri("/api/v1/auth/login")
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({"username": "admin", "password": "admin12345"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let v: Value = serde_json::from_slice(&body).unwrap();
    v["access_token"].as_str().unwrap().to_string()
}

/// 构造一个最小 PluginManifest 字面量(基线 requires provides: None)。
fn manifest_with_commands(plugin_id: &str, commands: Vec<Value>) -> PluginManifest {
    PluginManifest {
        id: plugin_id.to_string(),
        name: plugin_id.to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::System,
        pipeline_role: None,
        language: "python".to_string(),
        host_type: HostType::Sidecar,
        entry: "python server.py".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
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
        http_endpoints: vec![],
        ui_schema: None,
        contributes: Some(json!({ "commands": commands })),
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        provides: None,
    }
}

/// POST /api/v1/actions/execute 对未知 command(无任何 manifest 声明)返回 404。
#[tokio::test]
async fn test_actions_execute_unknown_command_returns_404() {
    let app = build_router(AppState::new());
    let token = admin_token(&app).await;
    let body = serde_json::to_string(&json!({
        "action": "nonexistent.cmd",
        "args": {},
    }))
    .unwrap();
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/actions/execute")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        response.status(),
        StatusCode::NOT_FOUND,
        "未知 command 应返回 404"
    );
}

/// POST /api/v1/actions/execute 对已声明 command(某 manifest contributes.commands
/// 含匹配 id)返回 200 + success=true,闭合前端链路。
#[tokio::test]
async fn test_actions_execute_known_command_returns_success() {
    let manifests = vec![manifest_with_commands(
        "demo_plugin",
        vec![json!({ "id": "test.cmd", "title": "Test Command" })],
    )];
    let mut state = AppState::new();
    state.manifests = std::sync::Arc::new(tokio::sync::RwLock::new(manifests));

    let app = build_router(state);
    let token = admin_token(&app).await;
    let body = serde_json::to_string(&json!({
        "action": "test.cmd",
        "args": { "foo": "bar" },
    }))
    .unwrap();
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/actions/execute")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK, "已知 command 应返回 200");

    let body = axum::body::to_bytes(response.into_body(), 64 * 1024)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        json["success"], true,
        "已知 command 应返回 success=true: {json}"
    );
}

/// POST /api/v1/actions/execute 请求体缺 action 字段返回 400。
#[tokio::test]
async fn test_actions_execute_missing_action_field_returns_400() {
    let app = build_router(AppState::new());
    let token = admin_token(&app).await;
    // 缺 action 字段,只有 args
    let body = serde_json::to_string(&json!({ "args": {} })).unwrap();
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/actions/execute")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        response.status(),
        StatusCode::BAD_REQUEST,
        "缺 action 字段应返回 400"
    );
}

/// POST /api/v1/actions/execute:command 声明 `tool` 字段 + invoker 不可用(None)
/// → 返回明确失败(success:false + "工具执行器不可用"),不静默假成功。
#[tokio::test]
async fn test_actions_execute_tool_declared_but_no_invoker_returns_failure() {
    let manifests = vec![manifest_with_commands(
        "tool_plugin",
        vec![json!({
            "id": "routed.cmd",
            "title": "Routed Command",
            "tool": "some_tool"
        })],
    )];
    let mut state = AppState::new();
    state.manifests = std::sync::Arc::new(tokio::sync::RwLock::new(manifests));
    // state.invoker 保持 None(AppState::new 默认)——模拟执行器缺席装配

    let app = build_router(state);
    let token = admin_token(&app).await;
    let body = serde_json::to_string(&json!({
        "action": "routed.cmd",
        "args": { "foo": "bar" },
    }))
    .unwrap();
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/actions/execute")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK, "降级失败走业务信封 200");

    let body = axum::body::to_bytes(response.into_body(), 64 * 1024)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        json["success"], false,
        "tool 声明 + invoker=None 必须返回 success=false: {json}"
    );
    let err = json["error"].as_str().unwrap_or_default();
    assert!(
        err.contains("工具执行器不可用"),
        "error 字段应说明工具执行器不可用: {json}"
    );
    assert_eq!(json["plugin_id"], "tool_plugin");
}
