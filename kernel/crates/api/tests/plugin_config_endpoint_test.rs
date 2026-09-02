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
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::System,
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
        config_files: files,
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        export_fields: vec![],
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
            target: None,
            settings: None,
            fields: vec![],
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
            target: None,
            settings: None,
            fields: vec![],
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
            target: None,
            settings: None,
            fields: vec![],
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
            target: None,
            settings: None,
            fields: vec![],
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
            target: None,
            settings: None,
            fields: vec![],
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

// ═══════════════════════════════════════════════════════════
// GAP-4：env target 条目（外部 MCP 源 key 的配置入口）
// ═══════════════════════════════════════════════════════════

fn env_mapping(file_id: &str) -> ConfigFileMapping {
    ConfigFileMapping {
        id: file_id.to_string(),
        path: ".env".to_string(),
        label: "搜索源密钥".to_string(),
        target: Some("env".to_string()),
        settings: None,
        fields: vec![
            agentos_core::traits::EnvConfigField {
                name: "SMITHERY_API_KEY".to_string(),
                label: "Smithery API Key".to_string(),
                field_type: "secret".to_string(),
                required: true,
                description: None,
                extra: None,
            },
            agentos_core::traits::EnvConfigField {
                name: "LANGSMITH_API_KEY".to_string(),
                label: "LangSmith API Key".to_string(),
                field_type: "secret".to_string(),
                required: false,
                description: None,
                extra: None,
            },
        ],
    }
}

/// 构造 app + 注入 manifest（复用上方 harness 的形态）。
async fn env_app() -> (axum::Router, tempfile::TempDir) {
    let tmp = tempfile::tempdir().unwrap();
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    let mut manifests = state.manifests.write().await;
    manifests.push(manifest_with_files(
        "smithery_search",
        vec![env_mapping("api_keys")],
    ));
    drop(manifests);
    (build_router(state), tmp)
}

#[tokio::test]
async fn test_env_target_get_masks_and_put_writes() {
    let (app, tmp) = env_app().await;
    let token = admin_token(&app).await;
    let uri = "/api/v1/plugins/smithery_search/config/api_keys";

    // GET（未设置）：两字段为空串
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(uri)
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let etag = resp
        .headers()
        .get("etag")
        .and_then(|h| h.to_str().ok())
        .expect("ETag 头")
        .to_string();
    let v: Value =
        serde_json::from_slice(&axum::body::to_bytes(resp.into_body(), 65536).await.unwrap())
            .unwrap();
    assert_eq!(v["path"], ".env");
    assert_eq!(v["data"]["SMITHERY_API_KEY"], "");
    assert_eq!(v["data"]["LANGSMITH_API_KEY"], "");

    // PUT：写入 smithery key（*** 哨兵跳过 langsmith）
    let put = app
        .clone()
        .oneshot(
            Request::builder()
                .method(axum::http::Method::PUT)
                .uri(uri)
                .header("content-type", "application/json")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::from(
                    json!({
                        "if_match": etag,
                        "data": {
                            "SMITHERY_API_KEY": "sk-secret-1",
                            "LANGSMITH_API_KEY": "***"
                        }
                    })
                    .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    let put_status = put.status();
    let put_body = axum::body::to_bytes(put.into_body(), 65536).await.unwrap();
    assert_eq!(
        put_status,
        StatusCode::OK,
        "PUT 应成功，body={}",
        String::from_utf8_lossy(&put_body)
    );

    // .env 落盘 + GET 掩码视图翻转
    let env_text = fs::read_to_string(tmp.path().join(".env")).unwrap();
    assert!(
        env_text.contains("SMITHERY_API_KEY=sk-secret-1"),
        "{env_text}"
    );
    assert!(
        !env_text.contains("LANGSMITH"),
        "哨兵字段不写入: {env_text}"
    );
    let resp2 = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(uri)
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let v2: Value = serde_json::from_slice(
        &axum::body::to_bytes(resp2.into_body(), 65536)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(v2["data"]["SMITHERY_API_KEY"], "***");
    assert_eq!(v2["data"]["LANGSMITH_API_KEY"], "");
}

#[tokio::test]
async fn test_env_target_etag_conflict_and_undeclared_rejected() {
    let (app, _tmp) = env_app().await;
    let token = admin_token(&app).await;
    let uri = "/api/v1/plugins/smithery_search/config/api_keys";

    // 旧 ETag → 409
    let stale = app
        .clone()
        .oneshot(
            Request::builder()
                .method(axum::http::Method::PUT)
                .uri(uri)
                .header("content-type", "application/json")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::from(
                    json!({"if_match": "stale-etag", "data": {"SMITHERY_API_KEY": "x"}})
                        .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(stale.status(), StatusCode::CONFLICT);

    // 未声明字段 → 400（声明驱动：fields 之外的名字不可写）
    // 先取合法 ETag
    let got = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(uri)
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let etag = got
        .headers()
        .get("etag")
        .and_then(|h| h.to_str().ok())
        .expect("ETag 头")
        .to_string();
    let bad = app
        .clone()
        .oneshot(
            Request::builder()
                .method(axum::http::Method::PUT)
                .uri(uri)
                .header("content-type", "application/json")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::from(
                    json!({"if_match": etag, "data": {"EVIL_KEY": "x"}}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(bad.status(), StatusCode::BAD_REQUEST, "未声明字段应 400");
}

/// manifest 内联默认（ADR 2026-09-02-context-window-config-inline-manifest）：
/// 配置文件缺失时 GET 回退 fields.default 组装（含点号路径展开）+ ETag，
/// PUT（带该 ETag）保存即创建用户覆盖文件（当前值 = 默认组装的 JSON 串）。
#[tokio::test]
async fn test_get_missing_file_falls_back_to_field_defaults_and_put_creates_file() {
    use agentos_core::traits::EnvConfigField;

    let tmp = tempfile::tempdir().unwrap();
    // config/system/context_window_config.yaml 不存在（值内联 manifest）

    let fields = vec![
        EnvConfigField {
            name: "compress_trigger_ratio".to_string(),
            label: "压缩触发比例".to_string(),
            field_type: "slider".to_string(),
            required: false,
            description: None,
            extra: Some(
                json!({"default": 0.55, "min": 0, "max": 1})
                    .as_object()
                    .unwrap()
                    .clone(),
            ),
        },
        EnvConfigField {
            name: "budgets.l1".to_string(),
            label: "预算·L1 记忆".to_string(),
            field_type: "slider".to_string(),
            required: false,
            description: None,
            extra: Some(json!({"default": 0.1}).as_object().unwrap().clone()),
        },
    ];
    let manifest = manifest_with_files(
        "pipeline_context_window_guard",
        vec![ConfigFileMapping {
            id: "context_window".to_string(),
            path: "config/system/context_window_config.yaml".to_string(),
            label: "上下文窗口配置".to_string(),
            target: None,
            settings: None,
            fields,
        }],
    );

    let mut state = AppState::new();
    state.manifests = std::sync::Arc::new(tokio::sync::RwLock::new(vec![manifest]));
    state.project_root = Some(tmp.path().to_path_buf());

    let app = build_router(state);
    let token = admin_token(&app).await;
    let uri = "/api/v1/plugins/pipeline_context_window_guard/config/context_window";

    // GET：文件缺失 → 200 + fields.default 组装（点号展开为嵌套 budgets）
    let got = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(uri)
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(got.status(), StatusCode::OK, "文件缺失应 200 而非 404");
    let etag = got
        .headers()
        .get("etag")
        .and_then(|h| h.to_str().ok())
        .expect("ETag 头")
        .to_string();
    let body = axum::body::to_bytes(got.into_body(), 8192).await.unwrap();
    let v: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(v["data"]["compress_trigger_ratio"], 0.55);
    assert_eq!(v["data"]["budgets"]["l1"], 0.1, "点号路径应展开为嵌套对象");

    // PUT：带 GET 返回的 etag → 200，并创建用户覆盖文件（当前值 = 默认组装）
    let put = app
        .clone()
        .oneshot(
            Request::builder()
                .method(axum::http::Method::PUT)
                .uri(uri)
                .header("content-type", "application/json")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::from(
                    json!({"if_match": etag, "data": {"compress_trigger_ratio": 0.3}}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(put.status(), StatusCode::OK, "文件缺失时保存应创建覆盖文件");

    let written = fs::read_to_string(tmp.path().join("config/system/context_window_config.yaml"))
        .expect("PUT 应创建磁盘覆盖文件");
    assert!(written.contains("0.3"), "覆盖值应落盘: {written}");
}
