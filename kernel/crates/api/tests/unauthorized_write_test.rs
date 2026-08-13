// @feature: FP-0.2.八 多租户核心系统 | @vision: V4 多用户 | @ci: rust-test
//! F-API-1（映射 FP-0.2.八 多租户核心系统 / V4 多用户 + FP-DB）：api 配置写入面零鉴权 → 鉴权修复测试。
//!
//! 修复前：以下端点无任何鉴权（对照 db_routes.rs 的 require_read_role/require_admin_role 样板）：
//! - routes.rs 三个 PUT config handler（agent / plugin / pipeline 配置写盘）
//! - routes.rs actions_execute_handler（可触发插件命令执行）
//! - server.rs interaction_response_handler（可提交人类交互响应）
//! - compat_routes.rs 全部端点（threads CRUD、plugins reload/status/enabled）
//!
//! 意图（WHY）：这些端点要么写磁盘配置文件、要么触发插件命令/交互响应、要么读写
//! 会话与插件生命周期。匿名可调用 = 任意人可篡改配置、操纵插件、读取会话——多用户
//! （V4）场景下必须由内核强制鉴权：无 token → 401；普通用户（role=user）→ 403
//! （写面仅 admin；读面 admin/viewer）；admin → 放行。
//!
//! 鉴权实现复用 db_routes.rs 的 require_read_role / require_admin_role（同一套
//! resolve_request_user + 角色校验），与 /api/v1/db/* 保持风格一致。

use std::fs;
use std::sync::Arc;

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_core::traits::{ConfigFileMapping, HostType, PluginManifest, PluginType};
use axum::body::Body;
use axum::http::{Method, Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

/// 构造带内存 store + project_root（agent/pipeline/plugin 配置齐全）的测试 app。
///
/// 布局对齐各端点既有测试夹具：
/// - config/agents/main/test_agent.yaml（agent config PUT 正例）
/// - config/pipelines/default.yaml（pipeline config PUT 正例）
/// - config/models/llm.yaml（经 manifest config_files 映射，plugin config PUT 正例）
fn app_with_deps() -> (tempfile::TempDir, axum::Router) {
    let tmp = tempfile::tempdir().unwrap();

    let agent_dir = tmp.path().join("config").join("agents").join("main");
    fs::create_dir_all(&agent_dir).unwrap();
    fs::write(agent_dir.join("test_agent.yaml"), "config_id: test_agent\nname: t\n").unwrap();

    let pipe_dir = tmp.path().join("config").join("pipelines");
    fs::create_dir_all(&pipe_dir).unwrap();
    fs::write(pipe_dir.join("default.yaml"), "name: default\n").unwrap();

    let model_dir = tmp.path().join("config").join("models");
    fs::create_dir_all(&model_dir).unwrap();
    fs::write(model_dir.join("llm.yaml"), "name: glm\napi_key: ${ENV_KEY}\n").unwrap();

    let manifest = PluginManifest {
        id: "llm_service".to_string(),
        name: "llm_service".to_string(),
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
        config_files: vec![ConfigFileMapping {
            id: "llm".to_string(),
            path: "config/models/llm.yaml".to_string(),
            label: "LLM".to_string(),
        }],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        provides: None,
    };

    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let mut state = AppState::new();
    state.store = Some(store.clone());
    state.manifests = Arc::new(vec![manifest]);
    state.project_root = Some(tmp.path().to_path_buf());
    (tmp, build_router(state))
}

/// 登录并返回 access_token（内置 admin；store 无该用户时回退内置用户表）。
async fn admin_token(app: &axum::Router) -> String {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(Method::POST)
                .uri("/api/v1/auth/login")
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({"username": "admin", "password": "admin12345"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK, "admin 登录应成功");
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    json["access_token"].as_str().unwrap().to_string()
}

/// 注册新用户并返回其 access_token（role=user，一用户一租户）。
async fn user_token(app: &axum::Router) -> String {
    let username = format!(
        "alice{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(Method::POST)
                .uri("/api/v1/auth/register")
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({
                        "username": username,
                        "password": "pass12345",
                        "email": "alice@test.dev"
                    })
                    .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK, "注册应成功");
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    json["access_token"].as_str().unwrap().to_string()
}

/// 发一个请求，返回状态码。payload 为 Some 时带 JSON body。
async fn send(
    app: &axum::Router,
    method: Method,
    uri: &str,
    token: Option<&str>,
    payload: Option<Value>,
) -> StatusCode {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(t) = token {
        builder = builder.header("authorization", format!("Bearer {t}"));
    }
    let body = match payload {
        Some(v) => {
            builder = builder.header("content-type", "application/json");
            Body::from(v.to_string())
        }
        None => Body::empty(),
    };
    app.clone()
        .oneshot(builder.body(body).unwrap())
        .await
        .unwrap()
        .status()
}

/// 写面端点清单（方法 / 路径 / 请求体）。
fn write_surface_cases() -> Vec<(Method, &'static str, Option<Value>)> {
    vec![
        // routes.rs 三个 PUT config handler
        (
            Method::PUT,
            "/api/v1/agents/test_agent/config",
            Some(json!({"yaml": "config_id: test_agent\nname: x\n"})),
        ),
        (
            Method::PUT,
            "/api/v1/plugins/llm_service/config/llm",
            Some(json!({"data": {"name": "glm"}})),
        ),
        (
            Method::PUT,
            "/api/v1/config/pipelines/default",
            Some(json!({"data": {"name": "default"}})),
        ),
        // actions_execute：可触发插件命令执行
        (
            Method::POST,
            "/api/v1/actions/execute",
            Some(json!({"action": "x.cmd", "args": {}})),
        ),
        // interaction/response：可提交人类交互响应
        (
            Method::POST,
            "/api/v1/interaction/response",
            Some(json!({"request_id": "r1"})),
        ),
        // compat_routes：threads 写面（create/update/update-agent/delete）
        (
            Method::POST,
            "/api/v1/threads",
            Some(json!({"title": "t"})),
        ),
        (
            Method::PATCH,
            "/api/v1/threads/thr-1",
            Some(json!({"intent": "x"})),
        ),
        (
            Method::PATCH,
            "/api/v1/threads/thr-1/agent",
            Some(json!({"agent_id": "a"})),
        ),
        (Method::DELETE, "/api/v1/threads/thr-1", None),
        // compat_routes：plugins 写面（reload / reload-all / reload-by-id / enabled）
        (Method::POST, "/api/v1/plugins/reload", None),
        (Method::POST, "/api/v1/plugins/reload-all", None),
        (Method::POST, "/api/v1/plugins/llm_service/reload", None),
        (
            Method::PUT,
            "/api/v1/plugins/llm_service/enabled",
            Some(json!({"enabled": false})),
        ),
    ]
}

/// 写面端点：无 token → 401（匿名不得写配置/触发命令/提交交互响应/操纵插件）。
#[tokio::test]
async fn test_write_surface_rejects_anonymous_401() {
    let (_tmp, app) = app_with_deps();
    for (method, uri, payload) in write_surface_cases() {
        let status = send(&app, method, uri, None, payload).await;
        assert_eq!(
            status,
            StatusCode::UNAUTHORIZED,
            "匿名访问写面 {uri} 应 401（当前 {status}）"
        );
    }
}

/// 写面端点：普通用户（role=user）→ 403（写面仅 admin）。
#[tokio::test]
async fn test_write_surface_rejects_non_admin_403() {
    let (_tmp, app) = app_with_deps();
    let user = user_token(&app).await;
    for (method, uri, payload) in write_surface_cases() {
        let status = send(&app, method, uri, Some(&user), payload).await;
        assert_eq!(
            status,
            StatusCode::FORBIDDEN,
            "普通用户访问写面 {uri} 应 403（当前 {status}）"
        );
    }
}

/// 读面端点（compat threads 读 + plugins status/history）：
/// 无 token → 401；普通用户 → 403（仅 admin/viewer，对齐 db_routes 只读角色）。
#[tokio::test]
async fn test_read_surface_requires_auth_401_and_403() {
    let (_tmp, app) = app_with_deps();
    let user = user_token(&app).await;
    for (method, uri) in [
        (Method::GET, "/api/v1/threads"),
        (Method::GET, "/api/v1/threads/thr-1"),
        (Method::GET, "/api/v1/threads/thr-1/messages"),
        (Method::GET, "/api/v1/plugins/status"),
        (Method::GET, "/api/v1/plugins/history"),
    ] {
        let status = send(&app, method.clone(), uri, None, None).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED, "匿名读 {uri} 应 401（当前 {status}）");
        let status = send(&app, method, uri, Some(&user), None).await;
        assert_eq!(
            status,
            StatusCode::FORBIDDEN,
            "普通用户读 {uri} 应 403（仅 admin/viewer，当前 {status}）"
        );
    }
}

/// admin token → 写面/读面全部放行（正常流程不回归）。
#[tokio::test]
async fn test_admin_token_passes_write_and_read() {
    let (_tmp, app) = app_with_deps();
    let admin = admin_token(&app).await;

    // PUT agent config → 200（文件写回）
    let status = send(
        &app,
        Method::PUT,
        "/api/v1/agents/test_agent/config",
        Some(&admin),
        Some(json!({"yaml": "config_id: test_agent\nname: 新名\n"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "admin PUT agent config 应通过（当前 {status}）");

    // PUT pipeline config → 200（原子写回）
    let status = send(
        &app,
        Method::PUT,
        "/api/v1/config/pipelines/default",
        Some(&admin),
        Some(json!({"data": {"name": "default"}})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "admin PUT pipeline config 应通过（当前 {status}）");

    // PUT plugin config（先 GET 拿 ETag 满足 If-Match 乐观锁）→ 200
    let get_resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/v1/plugins/llm_service/config/llm")
                .header("authorization", format!("Bearer {admin}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(get_resp.status(), StatusCode::OK);
    let etag = get_resp
        .headers()
        .get("etag")
        .expect("GET plugin config 应带 etag")
        .to_str()
        .unwrap()
        .to_string();
    let status = send(
        &app,
        Method::PUT,
        "/api/v1/plugins/llm_service/config/llm",
        Some(&admin),
        Some(json!({"data": {"name": "glm", "limit": 200}, "if_match": etag})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "admin PUT plugin config 应通过（当前 {status}）");

    // POST /api/v1/threads → 200（创建会话）
    let status = send(
        &app,
        Method::POST,
        "/api/v1/threads",
        Some(&admin),
        Some(json!({"title": "t"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "admin POST threads 应通过（当前 {status}）");

    // GET /api/v1/threads → 200
    let status = send(&app, Method::GET, "/api/v1/threads", Some(&admin), None).await;
    assert_eq!(status, StatusCode::OK, "admin GET threads 应通过（当前 {status}）");

    // GET /api/v1/plugins/status → 200
    let status = send(&app, Method::GET, "/api/v1/plugins/status", Some(&admin), None).await;
    assert_eq!(status, StatusCode::OK, "admin GET plugins/status 应通过（当前 {status}）");
}
