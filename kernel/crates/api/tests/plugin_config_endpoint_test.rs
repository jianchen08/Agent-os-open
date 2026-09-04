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
use agentos_core::traits::{
    ConfigFileMapping, EnvConfigField, HostType, PluginManifest, PluginType,
};
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

/// 内联形态（path 空，单一真值裁定 2026-09-02）：GET 组装 fields.default
/// （点号路径展开）+ ETag；PUT（带该 ETag）直接写回 plugin.json 的
/// fields.default——不创建任何独立配置文件；再次 GET 反映新值。
#[tokio::test]
async fn test_inline_entry_get_defaults_and_put_writes_manifest() {
    use agentos_core::traits::EnvConfigField;

    let tmp = tempfile::tempdir().unwrap();
    // 内联条目不引用磁盘文件——config/system/ 下不应有任何覆盖文件产生

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
            path: String::new(),
            label: "上下文窗口配置".to_string(),
            target: None,
            settings: None,
            fields,
        }],
    );

    // 插件根目录（PUT 写回 plugin.json 的目标）
    let plugin_dir = tmp.path().join("plugins").join("context_window_guard");
    fs::create_dir_all(&plugin_dir).unwrap();
    fs::write(
        plugin_dir.join("plugin.json"),
        json!({
            "id": "pipeline_context_window_guard",
            "name": "Context Window Guard",
            "version": "1.0.0",
            "plugin_type": "pipeline",
            "language": "python",
            "host_type": "sidecar",
            "entry": "python server.py",
            "config_files": [{
                "id": "context_window",
                "label": "上下文窗口配置",
                "fields": [
                    {"name": "compress_trigger_ratio", "type": "slider", "default": 0.55},
                    {"name": "budgets.l1", "type": "slider", "default": 0.1}
                ]
            }]
        })
        .to_string(),
    )
    .unwrap();

    let mut state = AppState::new();
    state.manifests = std::sync::Arc::new(tokio::sync::RwLock::new(vec![manifest]));
    state.project_root = Some(tmp.path().to_path_buf());
    state.plugin_dirs = std::sync::Arc::new(
        [(
            "pipeline_context_window_guard".to_string(),
            plugin_dir.clone(),
        )]
        .into_iter()
        .collect(),
    );

    let app = build_router(state);
    let token = admin_token(&app).await;
    let uri = "/api/v1/plugins/pipeline_context_window_guard/config/context_window";

    // GET：内联形态 → 200 + fields.default 组装（点号展开为嵌套 budgets）
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
    assert_eq!(got.status(), StatusCode::OK, "内联条目应 200");
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

    // PUT：带 GET 返回的 etag → 200，值写回 plugin.json 的 fields.default
    let put = app
        .clone()
        .oneshot(
            Request::builder()
                .method(axum::http::Method::PUT)
                .uri(uri)
                .header("content-type", "application/json")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::from(
                    json!({"if_match": etag, "data": {"compress_trigger_ratio": 0.06}}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(put.status(), StatusCode::OK, "内联保存应写回 manifest");

    let written = fs::read_to_string(plugin_dir.join("plugin.json")).expect("plugin.json 应存在");
    assert!(written.contains("0.06"), "default 应更新为 0.06: {written}");
    assert!(
        !tmp.path()
            .join("config/system/context_window_config.yaml")
            .exists(),
        "内联形态不得生成独立配置文件"
    );

    // 再次 GET：内存 manifest 已同步，新值 + 新 ETag 即时可见
    let got2 = app
        .oneshot(
            Request::builder()
                .uri(uri)
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let body2 = axum::body::to_bytes(got2.into_body(), 8192).await.unwrap();
    let v2: Value = serde_json::from_slice(&body2).unwrap();
    assert_eq!(v2["data"]["compress_trigger_ratio"], 0.06, "保存应即时可见");
    let etag2 = v2["etag"].as_str().expect("新 ETag").to_string();
    assert_ne!(etag, etag2, "值变化应派生新 ETag");
}

/// 引用形态文件缺失：fields 禁声明 default（G2 双真值拦截），GET/PUT 当前值
/// 视图为空对象——保存即创建文件（引用形态语义保持）。
#[tokio::test]
async fn test_referenced_missing_file_yields_empty_view() {
    let tmp = tempfile::tempdir().unwrap();
    let manifest = manifest_with_files(
        "some_plugin",
        vec![ConfigFileMapping {
            id: "cfg".to_string(),
            path: "config/system/new_config.yaml".to_string(),
            label: "新配置".to_string(),
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
    let uri = "/api/v1/plugins/some_plugin/config/cfg";

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
    assert_eq!(got.status(), StatusCode::OK);
    let etag = got
        .headers()
        .get("etag")
        .and_then(|h| h.to_str().ok())
        .expect("ETag 头")
        .to_string();
    let body = axum::body::to_bytes(got.into_body(), 8192).await.unwrap();
    let v: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(v["data"], json!({}), "缺文件且无 default 应为空视图");

    // PUT 带该 etag → 创建文件（引用形态保存语义不变）
    let put = app
        .oneshot(
            Request::builder()
                .method(axum::http::Method::PUT)
                .uri(uri)
                .header("content-type", "application/json")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::from(
                    json!({"if_match": etag, "data": {"enabled": true}}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(put.status(), StatusCode::OK);
    let written =
        fs::read_to_string(tmp.path().join("config/system/new_config.yaml")).expect("应创建文件");
    assert!(written.contains("enabled"), "保存值应落盘: {written}");
}

/// 内联形态公共座（path 空）：同一份 fields JSON 同时喂内存 manifest 与磁盘
/// plugin.json，杜绝两份声明漂移；返回 (临时目录, AppState, plugin_dir)。
fn inline_state_with_fields(
    plugin_id: &str,
    file_id: &str,
    fields_json: Value,
) -> (tempfile::TempDir, AppState, std::path::PathBuf) {
    let tmp = tempfile::tempdir().unwrap();
    let fields: Vec<EnvConfigField> = fields_json
        .as_array()
        .unwrap()
        .iter()
        .map(|f| EnvConfigField {
            name: f["name"].as_str().unwrap().to_string(),
            label: f["name"].as_str().unwrap().to_string(),
            field_type: f
                .get("type")
                .and_then(|v| v.as_str())
                .unwrap_or("string")
                .to_string(),
            required: false,
            description: None,
            extra: Some(
                json!({"default": f.get("default").cloned().unwrap_or(Value::Null)})
                    .as_object()
                    .unwrap()
                    .clone(),
            ),
        })
        .collect();
    let manifest = manifest_with_files(
        plugin_id,
        vec![ConfigFileMapping {
            id: file_id.to_string(),
            path: String::new(),
            label: file_id.to_string(),
            target: None,
            settings: None,
            fields,
        }],
    );
    let plugin_dir = tmp.path().join("plugins").join(plugin_id);
    fs::create_dir_all(&plugin_dir).unwrap();
    fs::write(
        plugin_dir.join("plugin.json"),
        json!({
            "id": plugin_id,
            "name": plugin_id,
            "version": "1.0.0",
            "plugin_type": "pipeline",
            "language": "python",
            "host_type": "sidecar",
            "entry": "python server.py",
            "config_files": [{
                "id": file_id,
                "label": file_id,
                "fields": fields_json,
            }]
        })
        .to_string(),
    )
    .unwrap();
    let mut state = AppState::new();
    state.manifests = std::sync::Arc::new(tokio::sync::RwLock::new(vec![manifest]));
    state.project_root = Some(tmp.path().to_path_buf());
    state.plugin_dirs = std::sync::Arc::new(
        [(plugin_id.to_string(), plugin_dir.clone())]
            .into_iter()
            .collect(),
    );
    (tmp, state, plugin_dir)
}

/// 回归（2026-09-03 诊断）：GET 内联视图把点分字段名展开为嵌套对象，前端
/// 整树回传保存——PUT 必须接受与 GET 同形态的 data（往返对称）；扁平点分键
/// 部分更新（第二形态）同样接受。写回只动目标字段，其余字段不被触碰。
#[tokio::test]
async fn test_inline_put_accepts_nested_get_view_round_trip() {
    let (tmp, state, plugin_dir) = inline_state_with_fields(
        "pipeline_context_window_guard",
        "context_window",
        json!([
            {"name": "compress_trigger_ratio", "type": "slider", "default": 0.06},
            {"name": "budgets.recent", "type": "slider", "default": 0.18},
            {"name": "budgets.l1", "type": "slider", "default": 0.08},
            {"name": "compression.enabled", "type": "toggle", "default": true}
        ]),
    );
    let app = build_router(state);
    let token = admin_token(&app).await;
    let uri = "/api/v1/plugins/pipeline_context_window_guard/config/context_window";

    // GET：点分名展开为嵌套视图
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
    assert_eq!(got.status(), StatusCode::OK);
    let etag = got
        .headers()
        .get("etag")
        .and_then(|h| h.to_str().ok())
        .expect("ETag 头")
        .to_string();
    let body = axum::body::to_bytes(got.into_body(), 8192).await.unwrap();
    let v: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        v["data"]["budgets"]["recent"], 0.18,
        "点分名应展开为嵌套对象"
    );
    assert_eq!(v["data"]["compression"]["enabled"], true);

    // PUT：整树回传（改 budgets.recent，其余嵌套键原样携带）→ 200
    let mut data = v["data"].clone();
    data["budgets"]["recent"] = json!(0.25);
    let put = app
        .clone()
        .oneshot(
            Request::builder()
                .method(axum::http::Method::PUT)
                .uri(uri)
                .header("content-type", "application/json")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::from(
                    json!({"if_match": etag, "data": data}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        put.status(),
        StatusCode::OK,
        "嵌套视图整树回传应被接受: {:?}",
        axum::body::to_bytes(put.into_body(), 8192).await.unwrap()
    );

    // plugin.json：目标字段 default 已更新，其余字段不被触碰
    let written = fs::read_to_string(plugin_dir.join("plugin.json")).unwrap();
    let m: Value = serde_json::from_str(&written).unwrap();
    let fields = m["config_files"][0]["fields"].as_array().unwrap();
    let def =
        |name: &str| fields.iter().find(|f| f["name"] == json!(name)).unwrap()["default"].clone();
    assert_eq!(def("budgets.recent"), json!(0.25), "目标字段应写回");
    assert_eq!(
        def("compress_trigger_ratio"),
        json!(0.06),
        "未改的扁平字段不被触碰"
    );
    assert_eq!(
        def("compression.enabled"),
        json!(true),
        "未改的嵌套字段不被触碰"
    );
    assert!(
        !tmp.path()
            .join("config/system/context_window_config.yaml")
            .exists(),
        "内联形态不得产生独立配置文件"
    );

    // 再次 GET：新值即时可见 + ETag 轮转
    let got2 = app
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
    let body2 = axum::body::to_bytes(got2.into_body(), 8192).await.unwrap();
    let v2: Value = serde_json::from_slice(&body2).unwrap();
    assert_eq!(v2["data"]["budgets"]["recent"], 0.25, "保存应即时可见");
    assert_ne!(etag, v2["etag"].as_str().unwrap(), "值变化应派生新 ETag");

    // 形态二：扁平点分键部分更新同样接受
    let put2 = app
        .oneshot(
            Request::builder()
                .method(axum::http::Method::PUT)
                .uri(uri)
                .header("content-type", "application/json")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::from(
                    json!({"if_match": v2["etag"], "data": {"budgets.l1": 0.3}}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(put2.status(), StatusCode::OK, "扁平点分键部分更新应被接受");
    let written2 = fs::read_to_string(plugin_dir.join("plugin.json")).unwrap();
    assert!(written2.contains("0.3"), "扁平点分键应写回: {written2}");
}

/// fail-closed 保持（回归 2026-09-03 诊断）：嵌套形态下未声明的叶路径仍拒绝，
/// 报错点名违规叶路径（而非父键），且磁盘 manifest 不被部分写入、ETag 不轮转。
#[tokio::test]
async fn test_inline_put_rejects_undeclared_leaf_path() {
    let (_tmp, state, plugin_dir) = inline_state_with_fields(
        "pipeline_context_window_guard",
        "context_window",
        json!([
            {"name": "budgets.recent", "type": "slider", "default": 0.18}
        ]),
    );
    let app = build_router(state);
    let token = admin_token(&app).await;
    let uri = "/api/v1/plugins/pipeline_context_window_guard/config/context_window";

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

    // 两种区分形态：声明前缀下的未声明叶 / 顶层未声明标量
    let cases = vec![
        (json!({"budgets": {"nonsense": 0.5}}), "budgets.nonsense"),
        (json!({"custom": 1}), "custom"),
    ];
    for (data, offending) in cases {
        let put = app
            .clone()
            .oneshot(
                Request::builder()
                    .method(axum::http::Method::PUT)
                    .uri(uri)
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {token}"))
                    .body(Body::from(
                        json!({"if_match": etag, "data": data}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(put.status(), StatusCode::BAD_REQUEST, "未声明叶应 400");
        let body = axum::body::to_bytes(put.into_body(), 8192).await.unwrap();
        let text = String::from_utf8_lossy(&body).to_string();
        assert!(
            text.contains(offending),
            "报错应点名违规叶路径 {offending}: {text}"
        );
    }

    // 磁盘 manifest 不被部分写入，ETag 不轮转（状态未变）
    let written = fs::read_to_string(plugin_dir.join("plugin.json")).unwrap();
    assert!(
        !written.contains("nonsense") && !written.contains("custom"),
        "被拒 PUT 不得落盘: {written}"
    );
    let got2 = app
        .oneshot(
            Request::builder()
                .uri(uri)
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let etag2 = got2
        .headers()
        .get("etag")
        .and_then(|h| h.to_str().ok())
        .expect("ETag 头")
        .to_string();
    assert_eq!(etag, etag2, "被拒 PUT 不得改变状态视图");
}
