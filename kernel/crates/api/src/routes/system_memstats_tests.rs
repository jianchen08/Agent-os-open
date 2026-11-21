//! GET /api/v1/system/memstats（M1 测量面）：内核自有只读端点 + 白名单鉴权。
//! 测试构造与 auth.rs 同构（内存库播种 admin → 真实 login 换 access token）。

use super::*;
use agentos_core::traits::StorageBackend;
use agentos_http::auth::hash_password;
use axum::body::Body;
use axum::http::Request;
use serde_json::json;
use tower::ServiceExt;

const TEST_ADMIN_PW: &str = "test-admin-pw-2026";

/// 内存库 + 播种 admin 的完整路由 app（同 auth.rs 测试构造）。
async fn app_with_seeded_admin() -> axum::Router {
    let store =
        std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().expect("open_memory 失败"));
    let admin = agentos_core::types::UserRecord {
        user_id: "00000000-0000-0000-0000-000000000001".to_string(),
        username: "admin".to_string(),
        password: hash_password(TEST_ADMIN_PW).unwrap(),
        email: Some("admin@agentos.dev".to_string()),
        role: "admin".to_string(),
        tenant_id: agentos_http::auth::DEFAULT_TENANT_ID.to_string(),
        created_at: chrono::Utc::now().to_rfc3339(),
        last_login_at: None,
        must_change_password: false,
    };
    let _ = store.create_user(&admin).await; // 已有则忽略错误
    let mut state = AppState::new();
    state.store = Some(store);
    crate::server::build_router(state)
}

async fn login_token(app: &axum::Router) -> String {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/auth/login")
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({"username": "admin", "password": TEST_ADMIN_PW}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
    v["access_token"]
        .as_str()
        .expect("login 应签发 access_token")
        .to_string()
}

/// 未鉴权 GET → 401（白名单读面拒绝匿名：缺凭证即 Unauthorized，
/// 403 保留给"有凭证但角色不足"；与 /api/v1/sessions 同语义链）。
#[tokio::test]
async fn memstats_without_token_unauthorized() {
    let app = app_with_seeded_admin().await;
    let resp = app
        .oneshot(
            Request::builder()
                .method("GET")
                .uri("/api/v1/system/memstats")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::UNAUTHORIZED);
}

/// admin Bearer token GET → 200，JSON 快照字段齐备（数值或 null；
/// 值不判具体大小——测试进程构建开关与运行期负载都会影响数值）。
#[tokio::test]
async fn memstats_with_admin_token_returns_snapshot() {
    let app = app_with_seeded_admin().await;
    let token = login_token(&app).await;
    let resp = app
        .oneshot(
            Request::builder()
                .method("GET")
                .uri("/api/v1/system/memstats")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 65536).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
    for field in [
        "in_use_bytes",
        "in_use_drift_bytes",
        "requested_bytes",
        "committed_bytes",
        "reserved_bytes",
        "purged_bytes",
        "total_allocs",
        "freed_bytes",
        "abandoned_pages",
        "process_commit_bytes",
        "process_rss_bytes",
    ] {
        let val = &v[field];
        assert!(
            val.is_u64() || val.is_null(),
            "字段 {field} 应为数值或 null，实际 {val}"
        );
    }
    // 可信面（OS 口径 + arena 原子量）：有分配发生时必非零
    assert!(
        v["process_commit_bytes"].as_u64().unwrap_or(0) > 0,
        "process_commit_bytes 应 > 0（进程存活即有已提交内存）"
    );
}
