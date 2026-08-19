// @feature: FP-0.2.八 多租户核心系统 | @vision: V4 多用户 | @ci: rust-test
//! F-API-1（映射 FP-0.2.八 多租户核心系统 / V4 多用户 + FP-DB）：api 配置写入面零鉴权 → 鉴权修复测试。
//!
//! 修复前：以下端点无任何鉴权（对照 db_routes.rs 的 require_read_role/require_admin_role 样板）：
//! - routes.rs 两个 PUT config handler（plugin / pipeline 配置写盘；agent PUT 已
//!   插件化 /ext/agent_manager，admin 闸由插件自持——2026-08-20 ADR）
//! - routes.rs actions_execute_handler（可触发插件命令执行）
//! - server.rs interaction_response_handler（可提交人类交互响应）
//! - session_routes.rs 会话 CRUD + routes.rs 插件 enabled（原 compat_routes.rs 转正）
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
use agentos_core::traits::{
    ConfigFileMapping, HostType, PluginManifest, PluginType, StorageBackend,
};
use axum::body::Body;
use axum::http::{Method, Request, StatusCode};
use serde_json::{json, Value};
use tokio::sync::RwLock;
use tower::ServiceExt;

/// 构造带内存 store + project_root（pipeline/plugin 配置齐全）的测试 app。
///
/// 布局对齐各端点既有测试夹具：
/// - config/pipelines/default.yaml（pipeline config PUT 正例）
/// - config/models/llm.yaml（经 manifest config_files 映射，plugin config PUT 正例）
async fn app_with_deps() -> (tempfile::TempDir, axum::Router) {
    let tmp = tempfile::tempdir().unwrap();

    let pipe_dir = tmp.path().join("config").join("pipelines");
    fs::create_dir_all(&pipe_dir).unwrap();
    fs::write(pipe_dir.join("default.yaml"), "name: default\n").unwrap();

    let model_dir = tmp.path().join("config").join("models");
    fs::create_dir_all(&model_dir).unwrap();
    fs::write(
        model_dir.join("llm.yaml"),
        "name: glm\napi_key: ${ENV_KEY}\n",
    )
    .unwrap();

    let manifest = PluginManifest {
        id: "llm_service".to_string(),
        name: "llm_service".to_string(),
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
        config_files: vec![ConfigFileMapping {
            id: "llm".to_string(),
            path: "config/models/llm.yaml".to_string(),
            label: "LLM".to_string(),
            target: None,
            fields: vec![],
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
    // 播种 admin 对齐生产启动行为（seed_admin_user）：auth 加固后
    // store 存在但用户名未命中不再回退内置硬编码凭据
    store
        .create_user(&agentos_core::types::UserRecord {
            user_id: "00000000-0000-0000-0000-000000000001".to_string(),
            username: "admin".to_string(),
            password: "admin12345".to_string(),
            email: Some("admin@agentos.dev".to_string()),
            role: "admin".to_string(),
            tenant_id: "default".to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_login_at: None,
        })
        .await
        .unwrap();
    let mut state = AppState::new();
    state.store = Some(store.clone());
    state.manifests = Arc::new(RwLock::new(vec![manifest]));
    state.project_root = Some(tmp.path().to_path_buf());
    (tmp, build_router(state))
}

/// 登录并返回 access_token（admin 由 app_with_deps 按生产行为播种入库）。
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
        // routes.rs 两个 PUT config handler（agents PUT 已插件化 /ext/agent_manager，
        // admin 闸由插件自持——2026-08-20 ADR）
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
        // 会话管理（session_routes）：写面（create/update/update-agent/delete）
        (
            Method::POST,
            "/api/v1/sessions",
            Some(json!({"title": "t"})),
        ),
        (
            Method::PATCH,
            "/api/v1/sessions/thr-1",
            Some(json!({"intent": "x"})),
        ),
        (
            Method::PATCH,
            "/api/v1/sessions/thr-1/agent",
            Some(json!({"agent_id": "a"})),
        ),
        (Method::DELETE, "/api/v1/sessions/thr-1", None),
        // 插件监管：写面（enabled；reload* 死端点已删除）
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
    let (_tmp, app) = app_with_deps().await;
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
    let (_tmp, app) = app_with_deps().await;
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

/// 读面端点（sessions 读 + plugins 状态）：
/// 无 token → 401；普通用户 → 403（仅 admin/viewer，对齐 db_routes 只读角色）。
#[tokio::test]
async fn test_read_surface_requires_auth_401_and_403() {
    let (_tmp, app) = app_with_deps().await;
    let user = user_token(&app).await;
    for (method, uri) in [
        (Method::GET, "/api/v1/sessions"),
        (Method::GET, "/api/v1/sessions/thr-1/messages"),
        (Method::GET, "/api/v1/plugins"),
    ] {
        let status = send(&app, method.clone(), uri, None, None).await;
        assert_eq!(
            status,
            StatusCode::UNAUTHORIZED,
            "匿名读 {uri} 应 401（当前 {status}）"
        );
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
    let (_tmp, app) = app_with_deps().await;
    let admin = admin_token(&app).await;

    // PUT agent config 闸已插件化（/ext/agent_manager，插件自持 admin 检查——
    // 2026-08-20 ADR）；此处保留内核侧两个 PUT config 闸的正路径。

    // PUT pipeline config（先 GET 拿 etag 满足 A13 If-Match 乐观锁）→ 200
    let pipe_etag = {
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/v1/config/pipelines/default")
                    .header("authorization", format!("Bearer {admin}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        resp.headers()
            .get("etag")
            .expect("GET pipeline config 应带 etag 头")
            .to_str()
            .unwrap()
            .to_string()
    };
    let status = send(
        &app,
        Method::PUT,
        "/api/v1/config/pipelines/default",
        Some(&admin),
        Some(json!({"data": {"name": "default"}, "if_match": pipe_etag})),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "admin PUT pipeline config 应通过（当前 {status}）"
    );

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
    assert_eq!(
        status,
        StatusCode::OK,
        "admin PUT plugin config 应通过（当前 {status}）"
    );

    // POST /api/v1/sessions → 200（创建会话）
    let status = send(
        &app,
        Method::POST,
        "/api/v1/sessions",
        Some(&admin),
        Some(json!({"title": "t"})),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "admin POST sessions 应通过（当前 {status}）"
    );

    // GET /api/v1/sessions → 200
    let status = send(&app, Method::GET, "/api/v1/sessions", Some(&admin), None).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "admin GET sessions 应通过（当前 {status}）"
    );

    // GET /api/v1/plugins → 200
    let status = send(&app, Method::GET, "/api/v1/plugins", Some(&admin), None).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "admin GET plugins 应通过（当前 {status}）"
    );
}

/// A14：create_session 的 user_id 以 token（resolve_request_user）解析为准——
/// body 伪造 user_id ≠ token 用户时，事件路由（registry 的 thread → user 注册，
/// WS 流式推送据此定位连接）按 token 用户登记。
#[tokio::test]
async fn test_create_session_routes_events_by_token_user_not_body() {
    let tmp = tempfile::tempdir().unwrap();
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    // 播种 admin（对齐生产 seed_admin_user；auth 加固后无硬编码回退）
    store
        .create_user(&agentos_core::types::UserRecord {
            user_id: "00000000-0000-0000-0000-000000000001".to_string(),
            username: "admin".to_string(),
            password: "admin12345".to_string(),
            email: Some("admin@agentos.dev".to_string()),
            role: "admin".to_string(),
            tenant_id: "default".to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_login_at: None,
        })
        .await
        .unwrap();
    let session = Arc::new(agentos_session::SessionCoordinator::new());

    let mut state = AppState::new();
    state.store = Some(store);
    state.session = Some(session.clone());
    state.project_root = Some(tmp.path().to_path_buf());
    let app = build_router(state);

    let token = admin_token(&app).await;
    // body 伪造 user_id（≠ token 用户）
    let resp = app
        .oneshot(
            Request::builder()
                .method(Method::POST)
                .uri("/api/v1/sessions")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({"title": "t", "user_id": "forged-user-999"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK, "admin 创建会话应 200");
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let v: Value = serde_json::from_slice(&body).unwrap();
    let thread_id = v["thread_id"].as_str().unwrap().to_string();

    // registry 的 thread → user 映射必须是 token 用户（内置 admin 的固定 uuid），
    // 而非 body 伪造的 "forged-user-999"。
    let threads = session.list_threads();
    let bound_user = threads
        .iter()
        .find(|(tid, _)| *tid == thread_id)
        .map(|(_, uid)| uid.clone())
        .expect("thread 应已注册到事件路由表");
    assert_eq!(
        bound_user, "00000000-0000-0000-0000-000000000001",
        "事件路由应按 token 用户登记，而非 body 伪造值"
    );
}
