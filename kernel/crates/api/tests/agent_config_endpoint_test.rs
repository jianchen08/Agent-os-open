// @feature: FP-0.2.CFG 配置注入 | @vision: V6 可即用 | @ci: rust-test
//! 阶段1:agent schema 端点 + agent config 读写端点集成测试(TDD)。
//!
//! 验证端点用真实文件系统跑通:
//! - GET /api/v1/agents/schema 返回 200 且含 fields 数组
//! - GET /api/v1/agents/{id}/config 对存在的 agent 返回 200 + yaml
//! - GET /api/v1/agents/{id}/config 对不存在的 id 返回 404
//! - PUT /api/v1/agents/{id}/config 写入后 GET 能读回新内容
//! - PUT 会备份原文件(备份文件存在)

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


/// 在临时项目根构造 config/agents/main/<id>.yaml 的测试环境。
fn state_with_agent(id: &str, yaml_content: &str) -> (tempfile::TempDir, AppState) {
    let tmp = tempfile::tempdir().unwrap();
    let agent_dir = tmp.path().join("config").join("agents").join("main");
    fs::create_dir_all(&agent_dir).unwrap();
    fs::write(agent_dir.join(format!("{id}.yaml")), yaml_content).unwrap();

    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    (tmp, state)
}

/// GET /api/v1/agents/schema 返回 200 且有 fields 数组。
#[tokio::test]
async fn test_agent_schema_returns_fields_array() {
    let app = build_router(AppState::new());
    let token = admin_token(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/agents/schema")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    let body = axum::body::to_bytes(response.into_body(), 64 * 1024)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    let fields = json["fields"].as_array().expect("schema 应含 fields 数组");
    assert!(!fields.is_empty(), "fields 不应为空");

    // 关键字段存在
    let names: Vec<&str> = fields
        .iter()
        .filter_map(|f| f["name"].as_str())
        .collect();
    assert!(names.contains(&"config_id"), "缺少 config_id: {names:?}");
    assert!(names.contains(&"name"), "缺少 name: {names:?}");
    assert!(names.contains(&"agent_type"), "缺少 agent_type: {names:?}");

    // 类型集合覆盖 string/textarea/number/select/multiselect
    let types: Vec<&str> = fields.iter().filter_map(|f| f["type"].as_str()).collect();
    for t in ["string", "textarea", "number", "select", "multiselect"] {
        assert!(types.contains(&t), "缺少类型 {t}: {types:?}");
    }

    // 每个字段都有 label
    for f in fields {
        assert!(f["label"].is_string(), "字段缺 label: {f}");
    }
    // required 标记为布尔
    for f in fields {
        assert!(
            f["required"].is_null() || f["required"].is_boolean(),
            "required 应为布尔或缺省: {f}"
        );
    }
}

/// GET /api/v1/agents/{id}/config 对存在的 agent 返回 200 + yaml 原文。
#[tokio::test]
async fn test_get_agent_config_returns_yaml() {
    let yaml = "config_id: test_agent\nname: 测试Agent\nagent_type: main\nlevel: L1\n";
    let (_tmp, state) = state_with_agent("test_agent", yaml);

    let app = build_router(state);
    let token = admin_token(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/agents/test_agent/config")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    let body = axum::body::to_bytes(response.into_body(), 64 * 1024)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(json["config_id"], "test_agent");
    let yaml_back = json["yaml"].as_str().expect("应返回 yaml 字符串");
    assert!(yaml_back.contains("name: 测试Agent"), "yaml 内容不符: {yaml_back}");
    assert!(yaml_back.contains("agent_type: main"), "yaml 内容不符: {yaml_back}");
}

/// GET /api/v1/agents/{id}/config 对不存在的 id 返回 404。
#[tokio::test]
async fn test_get_agent_config_missing_returns_404() {
    let (_tmp, state) = state_with_agent("existing_agent", "config_id: existing_agent\nname: x\n");

    let app = build_router(state);
    let token = admin_token(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/agents/ghost_agent/config")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

/// PUT /api/v1/agents/{id}/config 写入后 GET 能读回新内容(round-trip)。
#[tokio::test]
async fn test_put_agent_config_then_get_reads_back_new_content() {
    let original = "config_id: round_trip_agent\nname: 原名\n";
    let (_tmp, state) = state_with_agent("round_trip_agent", original);

    let new_yaml = "config_id: round_trip_agent\nname: 新名\nlevel: L2\nmodel_tier: large\n";
    let put_body = serde_json::to_string(&json!({ "yaml": new_yaml })).unwrap();

    let app = build_router(state.clone());
    let token = admin_token(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/api/v1/agents/round_trip_agent/config")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(put_body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK, "PUT 应成功");

    // 磁盘文件已更新
    let disk =
        fs::read_to_string(state.project_root.as_ref().unwrap().join("config/agents/main/round_trip_agent.yaml"))
            .unwrap();
    assert!(disk.contains("新名"), "磁盘文件应写入新内容: {disk}");
    assert!(disk.contains("level: L2"), "磁盘文件应写入新内容: {disk}");

    // GET 读回新内容
    let app = build_router(state);
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/agents/round_trip_agent/config")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = axum::body::to_bytes(response.into_body(), 64 * 1024)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    let yaml_back = json["yaml"].as_str().unwrap();
    assert!(yaml_back.contains("新名"), "GET 应读回新内容: {yaml_back}");
}

/// PUT 会备份原文件(备份文件存在且内容为原内容)。
#[tokio::test]
async fn test_put_agent_config_creates_backup() {
    let original = "config_id: backup_agent\nname: 备份前\n";
    let (tmp, state) = state_with_agent("backup_agent", original);

    let put_body = serde_json::to_string(&json!({ "yaml": "config_id: backup_agent\nname: 备份后\n" }))
        .unwrap();
    let app = build_router(state);
    let token = admin_token(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/api/v1/agents/backup_agent/config")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(put_body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    // 备份文件存在(同目录 .bak 后缀),内容为原内容
    let backup = tmp
        .path()
        .join("config/agents/main/backup_agent.yaml.bak");
    assert!(backup.is_file(), "备份文件应存在: {}", backup.display());
    let backup_content = fs::read_to_string(&backup).unwrap();
    assert_eq!(backup_content, original, "备份内容应为原内容");
}

/// PUT 对不存在的 agent 返回 404。
#[tokio::test]
async fn test_put_agent_config_missing_returns_404() {
    let (_tmp, state) = state_with_agent("only_agent", "config_id: only_agent\nname: x\n");

    let put_body = serde_json::to_string(&json!({ "yaml": "config_id: ghost\n" })).unwrap();
    let app = build_router(state);
    let token = admin_token(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/api/v1/agents/ghost/config")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(put_body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

/// 支持顶层 config/agents/<id>.yaml(非分类子目录)的定位方式。
#[tokio::test]
async fn test_get_agent_config_top_level_file() {
    let tmp = tempfile::tempdir().unwrap();
    let agent_dir = tmp.path().join("config").join("agents");
    fs::create_dir_all(&agent_dir).unwrap();
    fs::write(
        agent_dir.join("top_level_agent.yaml"),
        "config_id: top_level_agent\nname: 顶层Agent\n",
    )
    .unwrap();

    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());

    let app = build_router(state);
    let token = admin_token(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/agents/top_level_agent/config")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
}
