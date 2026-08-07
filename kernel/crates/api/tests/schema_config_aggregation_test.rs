//! P1-4: /api/v1/schema 聚合各插件 config_files（TDD）。
//!
//! 前端据此构建"按插件展示"的配置树（ADR §4.6）。

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_core::traits::{ConfigFileMapping, HostType, PluginManifest, PluginType};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::Value;
use tower::ServiceExt;

fn manifest(plugin_id: &str, files: Vec<ConfigFileMapping>) -> PluginManifest {
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
        config_files: files,
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
    }
}

#[tokio::test]
async fn test_schema_aggregates_plugin_config_files() {
    let manifests = vec![
        manifest(
            "connectors_service",
            vec![
                ConfigFileMapping {
                    id: "godot".to_string(),
                    path: "config/external_tools/godot.yaml".to_string(),
                    label: "Godot 工具配置".to_string(),
                },
                ConfigFileMapping {
                    id: "vscode".to_string(),
                    path: "config/external_tools/vscode.yaml".to_string(),
                    label: "VS Code 工具配置".to_string(),
                },
            ],
        ),
        manifest(
            "llm_service",
            vec![ConfigFileMapping {
                id: "llm".to_string(),
                path: "config/models/llm.yaml".to_string(),
                label: "LLM 模型配置".to_string(),
            }],
        ),
        // 无 config_files 的插件不应出现在配置聚合里
        manifest("memory", vec![]),
    ];

    let mut state = AppState::new();
    state.manifests = std::sync::Arc::new(manifests);

    let app = build_router(state);
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/schema")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    let body = axum::body::to_bytes(response.into_body(), 16384).await.unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();

    // schema 应含 plugin_configs 数组，聚合各插件的 config_files
    let plugin_configs = json["plugin_configs"]
        .as_array()
        .expect("plugin_configs array missing");
    assert_eq!(
        plugin_configs.len(),
        2,
        "only plugins with config_files appear, got: {plugin_configs:?}"
    );

    let by_id: std::collections::HashMap<&str, &Value> = plugin_configs
        .iter()
        .map(|p| (p["plugin_id"].as_str().unwrap(), p))
        .collect();
    let connectors = by_id.get("connectors_service").unwrap();
    assert_eq!(connectors["config_files"].as_array().unwrap().len(), 2);
    assert_eq!(connectors["config_files"][0]["id"], "godot");
    assert_eq!(connectors["config_files"][0]["label"], "Godot 工具配置");
}
