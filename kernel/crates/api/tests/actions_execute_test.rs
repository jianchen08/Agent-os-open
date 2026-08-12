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

/// 构造一个最小 PluginManifest 字面量(基线 requires provides: None)。
fn manifest_with_commands(plugin_id: &str, commands: Vec<Value>) -> PluginManifest {
    PluginManifest {
        id: plugin_id.to_string(),
        name: plugin_id.to_string(),
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
        native: None,
        wasm: None,
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
    state.manifests = std::sync::Arc::new(manifests);

    let app = build_router(state);
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
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        response.status(),
        StatusCode::OK,
        "已知 command 应返回 200"
    );

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
    // 缺 action 字段,只有 args
    let body = serde_json::to_string(&json!({ "args": {} })).unwrap();
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/actions/execute")
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
