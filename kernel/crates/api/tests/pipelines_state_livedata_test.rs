// @feature: FP-0.2.七 路由收敛 | @ci: rust-test
//! /api/v1/pipelines/state 运行期数据补齐集成测试。
//!
//! 契约：run 期间引擎只在本地内存推进 state，registry 快照拍在 stage_finalize
//! （run 收尾）——运行中内存行停留在出生/上次终态，llm_model / track.llm_usage /
//! context_window 等每轮投影键全部缺失。读面必须从 pipeline_state 表补齐内存行
//! 缺失键（内存已有键不覆盖——pipeline-state.update 热路径双写内存+表）。
use std::sync::Arc;

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

/// 播种内置 admin（带 store 时登录查 users 表，不播种则 login 必败）。
async fn seed_admin(store: &agentos_engine::SqliteStore) {
    use agentos_core::traits::StorageBackend;
    let now = chrono::Utc::now().to_rfc3339();
    let admin = agentos_core::types::UserRecord {
        user_id: "00000000-0000-0000-0000-000000000001".to_string(),
        username: "admin".to_string(),
        password: "admin12345".to_string(),
        email: Some("admin@agentos.dev".to_string()),
        role: "admin".to_string(),
        tenant_id: agentos_http::auth::DEFAULT_TENANT_ID.to_string(),
        created_at: now,
        last_login_at: None,
    };
    let _ = store.create_user(&admin).await; // 已有则忽略错误
}

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

/// 声明 llm_model / context_window / track.llm_usage 出口的 manifest（模拟 llm_core/track）。
fn llm_manifest() -> agentos_core::traits::PluginManifest {
    use agentos_core::traits::{HostType, PluginManifest, PluginType};
    PluginManifest {
        id: "pipeline_llm_core".to_string(),
        name: "pipeline_llm_core".to_string(),
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
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        export_fields: vec![
            "llm_model".to_string(),
            "context_window".to_string(),
            "track.llm_usage".to_string(),
        ],
        provides: None,
    }
}

#[tokio::test]
async fn test_running_pipeline_memory_row_filled_from_state_table() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let tenant = "default";
    let pid = format!("state_fill_{}", std::process::id());

    // registry 出生种子（run 开始时的快照）：无任何 LLM 观测键
    let birth = json!({
        "pipeline_id": pid,
        "current_phase": "core",
        "messages": [{"role": "user", "content": "出生唯一消息"}],
    });
    agentos_session::pipeline_state_registry::global_registry()
        .get_or_init(tenant, &pid, "thread-x", "agent-x", birth);

    // DB 表投影（引擎每轮 merge 时同步落库）：run 期唯一活面
    store
        .upsert_state_field(&pid, tenant, "llm_model", &json!("MiniMax-M3"))
        .unwrap();
    store
        .upsert_state_field(&pid, tenant, "context_window", &json!(1_000_000))
        .unwrap();
    store
        .upsert_state_field(
            &pid,
            tenant,
            "track.llm_usage",
            &json!({"last_input_tokens": 63260}),
        )
        .unwrap();

    seed_admin(&store).await;
    let mut state = AppState::new();
    state.store = Some(store.clone());
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![llm_manifest()]));
    let app = build_router(state);
    let token = admin_token(&app).await;

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/pipelines/state")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .unwrap();
    let v: Value = serde_json::from_slice(&body).unwrap();
    let item = v["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|i| i["pipeline_id"] == pid.as_str())
        .expect("registry 行必须出口")
        .clone();
    assert_eq!(item["source"], "memory");
    let st = &item["state"];
    // 表投影键补齐（原缺陷：run 期全缺，前端用量指示器拿不到模型信息）
    assert_eq!(st["llm_model"], "MiniMax-M3");
    assert_eq!(st["context_window"], 1_000_000);
    assert_eq!(st["track.llm_usage"]["last_input_tokens"], 63260);
    // 内存自有键保留（registry 快照不被动覆盖）
    assert_eq!(st["current_phase"], "core");
    assert_eq!(st["message_count"], 1);
}

#[tokio::test]
async fn test_memory_key_not_overwritten_by_table() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let tenant = "default";
    let pid = format!("state_no_ovr_{}", std::process::id());

    // registry 行已有 context_window=111（旧快照/热路径双写），表里是 222
    let birth = json!({
        "pipeline_id": pid,
        "context_window": 111,
        "messages": [],
    });
    agentos_session::pipeline_state_registry::global_registry()
        .get_or_init(tenant, &pid, "thread-y", "agent-y", birth);
    store
        .upsert_state_field(&pid, tenant, "context_window", &json!(222))
        .unwrap();

    seed_admin(&store).await;
    let mut state = AppState::new();
    state.store = Some(store.clone());
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![llm_manifest()]));
    let app = build_router(state);
    let token = admin_token(&app).await;

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/pipelines/state")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .unwrap();
    let v: Value = serde_json::from_slice(&body).unwrap();
    let item = v["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|i| i["pipeline_id"] == pid.as_str())
        .expect("registry 行必须出口")
        .clone();
    // 内存已有键不覆盖（运行时态权威归属内存）
    assert_eq!(item["state"]["context_window"], 111);
}
