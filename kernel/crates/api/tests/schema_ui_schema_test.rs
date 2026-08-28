// @feature: FP-0.2.四 前端Schema | @vision: V6 可即用 | @ci: rust-test
//! P2-3: /api/v1/schema 聚合各插件 ui_schema（TDD）。
//!
//! 前端据此自动渲染插件界面（ADR §3.4 / §8.3 P2-3）。

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_core::traits::{HostType, PluginManifest, PluginType};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

fn manifest_with_ui(
    plugin_id: &str,
    plugin_type: PluginType,
    ui_schema: Option<Value>,
) -> PluginManifest {
    PluginManifest {
        id: plugin_id.to_string(),
        name: plugin_id.to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type,
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
        ui_schema,
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        export_fields: vec![],
        provides: None,
    }
}

async fn fetch_schema(manifests: Vec<PluginManifest>) -> Value {
    let state = AppState::with_config(json!({}));
    // 注入 manifests：用 with_plugins 太重，直接构造带 manifests 的 state
    let state = AppState {
        manifests: std::sync::Arc::new(tokio::sync::RwLock::new(manifests)),
        ..state
    };
    let app = build_router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/schema")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    serde_json::from_slice(&body).unwrap()
}

#[tokio::test]
async fn schema_aggregates_ui_schema_for_system_plugin() {
    let ui = json!({
        "widgets": [{"type": "status_bar", "id": "approval_status"}],
    });
    let manifests = vec![manifest_with_ui(
        "approval",
        PluginType::System,
        Some(ui.clone()),
    )];
    let schema = fetch_schema(manifests).await;

    let agents = schema["agents"].as_array().unwrap();
    assert_eq!(agents.len(), 1);
    assert_eq!(agents[0]["ui_schema"], ui, "system 插件 ui_schema 应聚合");
}

#[tokio::test]
async fn schema_aggregates_ui_schema_for_pipeline_plugin() {
    let ui = json!({"widgets": [{"type": "panel", "id": "p1"}]});
    let manifests = vec![manifest_with_ui(
        "my_pipeline",
        PluginType::Pipeline,
        Some(ui.clone()),
    )];
    let schema = fetch_schema(manifests).await;

    let pipelines = schema["pipelines"].as_array().unwrap();
    assert_eq!(pipelines.len(), 1);
    assert_eq!(
        pipelines[0]["ui_schema"], ui,
        "pipeline 插件 ui_schema 应聚合"
    );
}

#[tokio::test]
async fn schema_omits_ui_schema_when_absent() {
    let manifests = vec![manifest_with_ui("bare", PluginType::System, None)];
    let schema = fetch_schema(manifests).await;
    let agents = schema["agents"].as_array().unwrap();
    assert_eq!(agents.len(), 1);
    // ui_schema 缺失时为 null（serde Option::None → null）
    assert!(agents[0]["ui_schema"].is_null());
}
