// @feature: FP-0.2.七 路由收敛 | @vision: V6 可即用 | @ci: rust-test
//! 会话列表读面权威性测试。
//!
//! 回归：清空执行数据（clear_execution_data）删空 sessions 表后，若回退内存
//! registry 会把"曾注册线程"（ConnectionRegistry.thread_user_map 只增不减）以
//! title=null 出口，前端 mappers 渲染成「未命名会话」幽灵。修复 = store 存在
//! 即权威：DB 空就是空列表；仅 store 未配置时才回退内存 registry（兼容路径）。

use std::sync::Arc;

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

fn make_state(store: Option<Arc<agentos_engine::SqliteStore>>) -> AppState {
    let mut state = AppState::new();
    state.store = store
        .clone()
        .map(|s| s as Arc<dyn agentos_core::traits::StorageBackend>);
    state.db = store;
    // 会话协调器（内存 thread 注册表承载 WS 路由）；None 场景 = store 与 session
    // 同时缺省（纯内存兼容模式）
    state.session = Some(Arc::new(agentos_session::SessionCoordinator::new()));
    state
}

/// 播种内置 admin（与生产 seed_admin_user 一致）：带 store 时登录查 users 表，
/// 不回退内置硬编码凭据，不播种则 login 必败。
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

/// 登录内置 admin 返回 access_token（带 store 时 users 表同样可用）。
async fn admin_token(router: &axum::Router) -> String {
    let resp = router
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
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
    let json: Value = serde_json::from_slice(&body).unwrap();
    json["access_token"].as_str().unwrap().to_string()
}

async fn list_sessions(router: &axum::Router, token: &str) -> (StatusCode, Value) {
    let resp = router
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/v1/sessions")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let body = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap();
    (status, serde_json::from_slice(&body).unwrap())
}

async fn create_session(router: &axum::Router, token: &str, title: &str) -> Value {
    let resp = router
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/sessions")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(json!({"title": title}).to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let body = axum::body::to_bytes(resp.into_body(), 16 * 1024)
        .await
        .unwrap();
    serde_json::from_slice(&body).unwrap()
}

/// 清空后 DB 空 + 内存"曾注册线程"残留 → 列表必须为空（不回退内存 registry
/// 出口 title=null 的未命名幽灵）。
#[tokio::test]
async fn db_empty_does_not_export_memory_ghosts() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_admin(&store).await;
    let state = make_state(Some(store));
    // 模拟清空执行数据后的内存残留：sessions 表已空，但 thread 注册表里仍有
    // 历史创建过的 thread（ConnectionRegistry 只增不减，仅进程重启清零）
    if let Some(session) = &state.session {
        session.registry().register_thread("ghost-1", "u1");
        session.registry().register_thread("ghost-2", "u1");
        session
            .registry()
            .register_thread_pipeline("ghost-1", "pipeline-ghost-1");
    }
    let router = build_router(state);
    let token = admin_token(&router).await;

    let (status, body) = list_sessions(&router, &token).await;
    assert_eq!(status, StatusCode::OK);
    let threads = body["threads"].as_array().expect("threads 数组");
    assert!(
        threads.is_empty(),
        "DB 空（清空后）不得回退内存 registry 出口幽灵线程: {threads:?}"
    );
}

/// store 未配置（无持久化数据源）→ 回退内存 registry 的兼容路径必须保留。
#[tokio::test]
async fn no_store_falls_back_to_memory_registry() {
    let state = make_state(None);
    if let Some(session) = &state.session {
        session.registry().register_thread("mem-thread-1", "u1");
    }
    let router = build_router(state);
    let token = admin_token(&router).await;

    let (status, body) = list_sessions(&router, &token).await;
    assert_eq!(status, StatusCode::OK);
    let threads = body["threads"].as_array().expect("threads 数组");
    assert_eq!(threads.len(), 1, "无 store 时应回退内存 registry: {body:?}");
    assert_eq!(threads[0]["thread_id"], "mem-thread-1");
}

/// 正常路径：DB 有会话 → 从 DB 出口（含标题），不被内存注册表干扰。
#[tokio::test]
async fn db_sessions_listed_with_title() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_admin(&store).await;
    let state = make_state(Some(store));
    let router = build_router(state);
    let token = admin_token(&router).await;

    let created = create_session(&router, &token, "我的会话").await;
    let thread_id = created["thread_id"].as_str().expect("thread_id");

    let (status, body) = list_sessions(&router, &token).await;
    assert_eq!(status, StatusCode::OK);
    let threads = body["threads"].as_array().expect("threads 数组");
    assert_eq!(threads.len(), 1, "DB 会话应原样出口: {body:?}");
    assert_eq!(threads[0]["thread_id"], thread_id);
    assert_eq!(threads[0]["title"], "我的会话");
}
