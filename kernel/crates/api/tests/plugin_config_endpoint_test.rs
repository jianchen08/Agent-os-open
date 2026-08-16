// @feature: FP-0.2.CFG 配置注入 | @vision: V6 可即用 | @ci: rust-test
//! P1-4: /api/v1/plugins/{id}/config/{file_id} GET/PUT 端点集成测试（TDD）。
//!
//! 验证端点用真实文件系统 + 真实 manifest 跑通：
//! - GET 返回文件内容（B2 掩码 + B4 ETag）
//! - PUT 写回 config/ 现有文件（B2 *** 保留原值 + B4 原子写 + B6 round-trip）
//! - B2 回归：含 ${ENV_VAR} 占位符的文件，保存其他字段后占位符不破坏

use std::fs;

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_core::traits::{ConfigFileMapping, HostType, PluginManifest, PluginType};
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

/// 构造一个含 config_files 的 manifest，映射到临时 config 目录的真实文件。
fn manifest_with_files(plugin_id: &str, files: Vec<ConfigFileMapping>) -> PluginManifest {
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
        lifecycle: None,
        native: None,
        granted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: files,
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        provides: None,
    }
}

/// GET 返回 config_files[].path 映射的文件内容（带 ETag 头）。
#[tokio::test]
async fn test_get_plugin_config_returns_file_content_with_etag() {
    let tmp = tempfile::tempdir().unwrap();
    let config_dir = tmp.path().join("config").join("models");
    fs::create_dir_all(&config_dir).unwrap();
    let llm_path = config_dir.join("llm.yaml");
    fs::write(&llm_path, "name: glm\napi_key: ${DEEPSEEK_API_KEY}\n").unwrap();

    let manifest = manifest_with_files(
        "llm_service",
        vec![ConfigFileMapping {
            id: "llm".to_string(),
            path: "config/models/llm.yaml".to_string(),
            label: "LLM".to_string(),
        }],
    );

    let mut state = AppState::new();
    state.manifests = std::sync::Arc::new(tokio::sync::RwLock::new(vec![manifest]));
    state.project_root = Some(tmp.path().to_path_buf());

    let app = build_router(state);
    let token = admin_token(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/plugins/llm_service/config/llm")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    // ETag 头存在
    assert!(
        response.headers().get("etag").is_some(),
        "ETag header missing"
    );

    let body = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    // B2：${ENV_VAR} 占位符原样显示
    assert_eq!(json["data"]["api_key"], "${DEEPSEEK_API_KEY}");
    assert_eq!(json["data"]["name"], "glm");
}

/// GET 掩码真实明文 secret（文件直接写了明文 key）。
#[tokio::test]
async fn test_get_plugin_config_masks_plaintext_secret() {
    let tmp = tempfile::tempdir().unwrap();
    let config_dir = tmp.path().join("config").join("external_tools");
    fs::create_dir_all(&config_dir).unwrap();
    fs::write(
        config_dir.join("godot.yaml"),
        "name: godot\napi_key: sk-realtoken123\n",
    )
    .unwrap();

    let manifest = manifest_with_files(
        "connectors_service",
        vec![ConfigFileMapping {
            id: "godot".to_string(),
            path: "config/external_tools/godot.yaml".to_string(),
            label: "Godot".to_string(),
        }],
    );

    let mut state = AppState::new();
    state.manifests = std::sync::Arc::new(tokio::sync::RwLock::new(vec![manifest]));
    state.project_root = Some(tmp.path().to_path_buf());

    let app = build_router(state);
    let token = admin_token(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/plugins/connectors_service/config/godot")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    let body = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    let masked = json["data"]["api_key"].as_str().unwrap();
    assert!(masked.contains("***"), "plaintext secret masked: {masked}");
    assert!(!masked.contains("realtoken123"), "must not leak plaintext");
}

/// B2 回归：PUT 时 *** 哨兵保留磁盘原值（含 ${ENV} 占位符不被破坏）。
#[tokio::test]
async fn test_put_plugin_config_preserves_env_placeholder_via_sentinel() {
    let tmp = tempfile::tempdir().unwrap();
    let config_dir = tmp.path().join("config").join("models");
    fs::create_dir_all(&config_dir).unwrap();
    let llm_path = config_dir.join("llm.yaml");
    let original = "name: glm\napi_key: ${DEEPSEEK_API_KEY}\nlimit: 100\n";
    fs::write(&llm_path, original).unwrap();

    let manifest = manifest_with_files(
        "llm_service",
        vec![ConfigFileMapping {
            id: "llm".to_string(),
            path: "config/models/llm.yaml".to_string(),
            label: "LLM".to_string(),
        }],
    );

    let mut state = AppState::new();
    state.manifests = std::sync::Arc::new(tokio::sync::RwLock::new(vec![manifest]));
    state.project_root = Some(tmp.path().to_path_buf());

    // 先 GET 拿 ETag
    let app = build_router(state.clone());
    let token = admin_token(&app).await;
    let get_resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/plugins/llm_service/config/llm")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let etag = get_resp
        .headers()
        .get("etag")
        .unwrap()
        .to_str()
        .unwrap()
        .to_string();

    // PUT：改 limit，api_key 传 ***（前端 GET 时拿到明文掩码后的 ****，
    // 但本场景文件存的是占位符，PUT *** 应回退为磁盘原值 ${DEEPSEEK_API_KEY}）
    let put_body = serde_json::to_string(&json!({
        "data": {"name": "glm", "api_key": "***", "limit": 200},
        "if_match": etag,
    }))
    .unwrap();
    let app = build_router(state);
    let response = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/api/v1/plugins/llm_service/config/llm")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(put_body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    // 磁盘文件：limit 改了，api_key 占位符保留（不被 *** 破坏）
    let after = fs::read_to_string(&llm_path).unwrap();
    assert!(
        after.contains("${DEEPSEEK_API_KEY}"),
        "ENV placeholder must survive PUT, got: {after}"
    );
    assert!(after.contains("200"), "limit must be updated, got: {after}");
}

/// PUT 缺失 If-Match → 409（B4 乐观锁）。
#[tokio::test]
async fn test_put_plugin_config_without_if_match_returns_409() {
    let tmp = tempfile::tempdir().unwrap();
    let config_dir = tmp.path().join("config").join("models");
    fs::create_dir_all(&config_dir).unwrap();
    fs::write(config_dir.join("llm.yaml"), "name: glm\n").unwrap();

    let manifest = manifest_with_files(
        "llm_service",
        vec![ConfigFileMapping {
            id: "llm".to_string(),
            path: "config/models/llm.yaml".to_string(),
            label: "LLM".to_string(),
        }],
    );

    let mut state = AppState::new();
    state.manifests = std::sync::Arc::new(tokio::sync::RwLock::new(vec![manifest]));
    state.project_root = Some(tmp.path().to_path_buf());

    let put_body = serde_json::to_string(&json!({
        "data": {"name": "glm"},
    }))
    .unwrap();
    let app = build_router(state);
    let token = admin_token(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/api/v1/plugins/llm_service/config/llm")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(put_body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::CONFLICT);
}

/// GET 不存在的 file_id → 404。
#[tokio::test]
async fn test_get_plugin_config_unknown_file_id_returns_404() {
    let tmp = tempfile::tempdir().unwrap();
    fs::create_dir_all(tmp.path().join("config")).unwrap();

    let manifest = manifest_with_files(
        "llm_service",
        vec![ConfigFileMapping {
            id: "llm".to_string(),
            path: "config/models/llm.yaml".to_string(),
            label: "LLM".to_string(),
        }],
    );

    let mut state = AppState::new();
    state.manifests = std::sync::Arc::new(tokio::sync::RwLock::new(vec![manifest]));
    state.project_root = Some(tmp.path().to_path_buf());

    let app = build_router(state);
    let token = admin_token(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/plugins/llm_service/config/nonexistent")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}
