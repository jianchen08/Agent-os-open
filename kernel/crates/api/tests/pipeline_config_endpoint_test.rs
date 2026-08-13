// @feature: FP-0.2.CFG 配置注入 | @vision: V6 可即用 | @ci: rust-test
//! P7: 管道配置查询/更新接口集成测试（TDD）。
//!
//! 验证 `/api/v1/config/pipelines/{name}` GET/PUT 端点：
//! - GET 返回 config/pipelines/{name}.yaml 内容（含 etag）
//! - GET 未知管道 → 404；非法 name（路径穿越）→ 400
//! - PUT 原子写回 + round-trip 校验；坏 YAML → 400

use std::fs;

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
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


/// 在临时 config/pipelines/ 下写一份 default.yaml。
/// 注意：project_root 语义 = 项目根（config/ 的父目录），
/// handler 读取 `project_root/config/pipelines/{name}.yaml`（对齐 0.1 白名单）。
fn write_pipeline(project_root: &std::path::Path) {
    let dir = project_root.join("config").join("pipelines");
    fs::create_dir_all(&dir).unwrap();
    fs::write(
        dir.join("default.yaml"),
        "name: agentos_agent\ninput_routes:\n  - name: default\n    target: core\n    plugins: [tool_schema]\n    priority: 30\n",
    )
    .unwrap();
}

/// 构造带 project_root 的 AppState + router。
fn make_router(tmp: &tempfile::TempDir) -> axum::Router {
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    build_router(state)
}

/// GET 存在的管道配置 → 200，返回 data（含 name）+ etag 头。
#[tokio::test]
async fn test_get_pipeline_config_returns_yaml_content() {
    let tmp = tempfile::tempdir().unwrap();
    write_pipeline(tmp.path());

    let app = make_router(&tmp);
    let token = admin_token(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/config/pipelines/default")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    assert!(response.headers().get("etag").is_some(), "ETag header missing");

    let body = axum::body::to_bytes(response.into_body(), 8192).await.unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(json["data"]["name"], "agentos_agent");
    assert_eq!(json["name"], "default");
}

/// GET 未知管道 → 404。
#[tokio::test]
async fn test_get_pipeline_config_missing_returns_404() {
    let tmp = tempfile::tempdir().unwrap();
    write_pipeline(tmp.path());

    let app = make_router(&tmp);
    let token = admin_token(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/config/pipelines/nonexistent")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

/// GET 非法 name（路径穿越）→ 400。
#[tokio::test]
async fn test_get_pipeline_config_invalid_name_returns_400() {
    let tmp = tempfile::tempdir().unwrap();
    write_pipeline(tmp.path());

    let app = make_router(&tmp);
    let token = admin_token(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/config/pipelines/..%2F..%2Fetc%2Fpasswd")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

/// PUT 更新管道配置 → 200，文件内容被原子写回。
#[tokio::test]
async fn test_put_pipeline_config_writes_atomically() {
    let tmp = tempfile::tempdir().unwrap();
    write_pipeline(tmp.path());

    let app = make_router(&tmp);
    let token = admin_token(&app).await;
    let body = json!({
        "data": {
            "name": "agentos_agent",
            "input_routes": [
                { "name": "default", "target": "core", "plugins": ["tool_schema", "security_check"], "priority": 30 }
            ]
        }
    });
    let response = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/api/v1/config/pipelines/default")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);

    // 磁盘文件已更新（含新插件）
    let raw = fs::read_to_string(tmp.path().join("config/pipelines/default.yaml")).unwrap();
    assert!(raw.contains("security_check"), "disk content should be updated: {raw}");
}

/// PUT 非法 name（路径穿越）→ 400。
#[tokio::test]
async fn test_put_pipeline_config_invalid_name_returns_400() {
    let tmp = tempfile::tempdir().unwrap();
    write_pipeline(tmp.path());

    let app = make_router(&tmp);
    let token = admin_token(&app).await;
    let body = json!({ "data": { "name": "x" } });
    let response = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/api/v1/config/pipelines/..%2F..%2Fetc%2Fpasswd")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}
