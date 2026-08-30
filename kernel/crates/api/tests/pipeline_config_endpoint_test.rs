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

/// 在临时 config/pipelines/ 下写一份 default.yaml（G10 文件 DSL 格式）。
/// 注意：project_root 语义 = 项目根（config/ 的父目录），
/// handler 读取 `project_root/config/pipelines/{name}.yaml`（对齐 0.1 白名单）。
fn write_pipeline(project_root: &std::path::Path) {
    let dir = project_root.join("config").join("pipelines");
    fs::create_dir_all(&dir).unwrap();
    fs::write(
        dir.join("default.yaml"),
        "name: agentos_agent\nloop_bodies:\n  - id: main\n    while: \"True\"\n    steps:\n      - id: core\n        steps: [tool_schema]\n",
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
    assert!(
        response.headers().get("etag").is_some(),
        "ETag header missing"
    );

    let body = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
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

    // A13：先 GET 拿 etag（If-Match 乐观锁）
    let app = make_router(&tmp);
    let token = admin_token(&app).await;
    let get_resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/v1/config/pipelines/default")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(get_resp.status(), StatusCode::OK);
    let etag = get_resp
        .headers()
        .get("etag")
        .expect("GET 应带 etag 头")
        .to_str()
        .unwrap()
        .to_string();
    let body = json!({
        "data": {
            "name": "agentos_agent",
            "loop_bodies": [
                {
                    "id": "main",
                    "while": "True",
                    "steps": [
                        { "id": "core", "steps": ["tool_schema", "security_check"] }
                    ],
                    "next": [{ "when": "suspended == True", "then": "end" }]
                }
            ]
        },
        "if_match": etag
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
    assert!(
        raw.contains("security_check"),
        "disk content should be updated: {raw}"
    );
}

/// PUT 非映射 data（标量/序列无法承载管道字段）→ 400 且磁盘保持原值（T2）。
#[tokio::test]
async fn test_put_pipeline_config_non_mapping_returns_400() {
    let tmp = tempfile::tempdir().unwrap();
    write_pipeline(tmp.path());

    let app = make_router(&tmp);
    let token = admin_token(&app).await;
    // 先 GET 拿 etag，排除 409 干扰（结构校验先于乐观锁）
    let get_resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/v1/config/pipelines/default")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let etag = get_resp
        .headers()
        .get("etag")
        .expect("GET 应带 etag 头")
        .to_str()
        .unwrap()
        .to_string();

    let before = fs::read_to_string(tmp.path().join("config/pipelines/default.yaml")).unwrap();
    for bad in [json!(42), json!("scalar"), json!([1, 2, 3])] {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("PUT")
                    .uri("/api/v1/config/pipelines/default")
                    .header("authorization", format!("Bearer {token}"))
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({ "data": bad, "if_match": etag }).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            response.status(),
            StatusCode::BAD_REQUEST,
            "非映射 data({bad}) 应 400"
        );
    }
    let after = fs::read_to_string(tmp.path().join("config/pipelines/default.yaml")).unwrap();
    assert_eq!(after, before, "400 时磁盘应保持原值");
}

/// PUT 缺 If-Match → 409（A13，对齐 plugin config PUT）。
#[tokio::test]
async fn test_put_pipeline_config_without_if_match_returns_409() {
    let tmp = tempfile::tempdir().unwrap();
    write_pipeline(tmp.path());

    let app = make_router(&tmp);
    let token = admin_token(&app).await;
    let before = fs::read_to_string(tmp.path().join("config/pipelines/default.yaml")).unwrap();
    let response = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/api/v1/config/pipelines/default")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({ "data": { "name": "agentos_agent" } }).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        response.status(),
        StatusCode::CONFLICT,
        "缺 If-Match 应 409"
    );
    let after = fs::read_to_string(tmp.path().join("config/pipelines/default.yaml")).unwrap();
    assert_eq!(after, before, "409 时磁盘应保持原值");
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

/// PUT 请求构造（带合法 If-Match）。
async fn put_with_valid_etag(
    app: &axum::Router,
    token: &str,
    data: Value,
) -> axum::http::Response<axum::body::Body> {
    let get_resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/v1/config/pipelines/default")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let etag = get_resp
        .headers()
        .get("etag")
        .expect("GET 应带 etag 头")
        .to_str()
        .unwrap()
        .to_string();
    let body = json!({ "data": data, "if_match": etag });
    app.clone()
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
        .unwrap()
}

/// PUT 旧形态键（routes / exit_routes / 体级 loop_config）→ 400 且磁盘保持原值。
///
/// 这些键在 G10 单轨化后已退役（*File 结构 deny_unknown_fields，加载即报错）——
/// 写盘前校验把「保存无告警 → 加载失败静默降级空管道」提前到「保存即报错」。
#[tokio::test]
async fn test_put_pipeline_config_legacy_shape_rejected_400() {
    let tmp = tempfile::tempdir().unwrap();
    write_pipeline(tmp.path());

    let app = make_router(&tmp);
    let token = admin_token(&app).await;
    let before = fs::read_to_string(tmp.path().join("config/pipelines/default.yaml")).unwrap();

    let legacy_bodies = vec![
        // step 级旧路由键 routes（现役键为 next，且 then 必须是目标字符串）
        json!({
            "name": "agentos_agent",
            "loop_bodies": [
                { "id": "main", "steps": [
                    { "id": "post", "steps": [], "routes": [
                        { "when": "True", "then": { "next": "end" } }
                    ] }
                ] }
            ]
        }),
        // 体级旧出口键 exit_routes（现役键为 next）
        json!({
            "name": "agentos_agent",
            "loop_bodies": [
                { "id": "main", "steps": [], "exit_routes": [
                    { "when": "True", "then": { "next": "end" } }
                ] }
            ]
        }),
        // 体级 loop_config（体级循环现役形态为 while）
        json!({
            "name": "agentos_agent",
            "loop_bodies": [
                { "id": "main", "steps": [], "loop_config": { "enabled": true } }
            ]
        }),
    ];

    for bad in &legacy_bodies {
        let response = put_with_valid_etag(&app, &token, bad.clone()).await;
        assert_eq!(
            response.status(),
            StatusCode::BAD_REQUEST,
            "旧形态 data 应 400: {bad}"
        );
    }
    let after = fs::read_to_string(tmp.path().join("config/pipelines/default.yaml")).unwrap();
    assert_eq!(after, before, "400 时磁盘应保持原值");
}

/// PUT 死形态转移目标（then: wait / then: {next,set} / 未知目标）→ 400。
#[tokio::test]
async fn test_put_pipeline_config_dead_then_forms_rejected_400() {
    let tmp = tempfile::tempdir().unwrap();
    write_pipeline(tmp.path());

    let app = make_router(&tmp);
    let token = admin_token(&app).await;
    let before = fs::read_to_string(tmp.path().join("config/pipelines/default.yaml")).unwrap();

    let dead_forms = vec![
        // then: wait 已退役（挂起由 state.suspended 表达）
        json!({
            "name": "agentos_agent",
            "loop_bodies": [
                { "id": "main", "steps": [
                    { "id": "post", "steps": [], "next": [{ "when": "True", "then": "wait" }] }
                ] }
            ]
        }),
        // then: {next,set} 旧对象形态（现役形态 then 为目标字符串、set 平级）
        json!({
            "name": "agentos_agent",
            "loop_bodies": [
                { "id": "main", "steps": [
                    { "id": "post", "steps": [], "next": [
                        { "when": "True", "then": { "next": "end", "set": {} } }
                    ] }
                ] }
            ]
        }),
        // 未知转移目标（加载期语义校验前移到保存期）
        json!({
            "name": "agentos_agent",
            "loop_bodies": [
                { "id": "main", "steps": [
                    { "id": "post", "steps": [], "next": [{ "when": "True", "then": "nowhere" }] }
                ] }
            ]
        }),
    ];

    for bad in &dead_forms {
        let response = put_with_valid_etag(&app, &token, bad.clone()).await;
        assert_eq!(
            response.status(),
            StatusCode::BAD_REQUEST,
            "死形态 data 应 400: {bad}"
        );
    }
    let after = fs::read_to_string(tmp.path().join("config/pipelines/default.yaml")).unwrap();
    assert_eq!(after, before, "400 时磁盘应保持原值");
}

/// PUT 合法 G10 DSL（next/while/set 平级）→ 200 正常写盘（校验不放行漏拦）。
#[tokio::test]
async fn test_put_pipeline_config_valid_dsl_accepted() {
    let tmp = tempfile::tempdir().unwrap();
    write_pipeline(tmp.path());

    let app = make_router(&tmp);
    let token = admin_token(&app).await;
    let data = json!({
        "name": "agentos_agent",
        "loop_bodies": [
            {
                "id": "main",
                "while": "True",
                "steps": [
                    { "id": "post", "steps": [], "next": [
                        { "when": "raw_tool_calls != []", "then": "loop",
                          "set": { "core_type": "tool_execute" } },
                        { "then": "end" }
                    ] }
                ]
            }
        ]
    });
    let response = put_with_valid_etag(&app, &token, data).await;
    assert_eq!(response.status(), StatusCode::OK);

    let raw = fs::read_to_string(tmp.path().join("config/pipelines/default.yaml")).unwrap();
    assert!(
        raw.contains("core_type"),
        "disk content should be updated: {raw}"
    );
}
