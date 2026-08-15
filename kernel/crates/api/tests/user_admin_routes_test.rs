// @feature: boot-plugin 第二刀 用户管理策略面 | @ci: rust-test
//! user-admin capability handler（用户管理策略面）行为回归测试。
//!
//! boot-plugin 第二刀（docs/working/重要设计/boot-plugin内核能力插件化立项.md
//! §四/§五）：§9.6 精确拆分——auth 执行门（/api/v1/auth/login|logout|me|
//! register|refresh）永留内核（auth.rs 一行不动）；用户管理策略面（用户列表/
//! 改角色/改租户/删用户——拆分前内核无这些端点）以 `user-admin` namespace 的
//! CapabilityHandler（策略层留内核）+ plugins/shared/user_admin 插件（HTTP 面
//! /ext/user_admin/**）新建。本文件对齐 db_admin_routes_test.rs 的测试模式，
//! handler 直调——鉴权（401/403）/CRUD 语义/self-service 防护/响应形状（password
//! 剥离）全部保真验证；HTTP 面（通配分发→插件→反调）的端到端验证在真机覆盖。
//!
//! 鉴权：handler 从 params `_authorization`（HTTP 面插件透传的原始头）重建
//! HeaderMap 后走 resolve_request_user + admin 角色校验（与执行门同一实现）；
//! token 经 api build_router 的真实 login/register 端点签发。

use std::sync::Arc;

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_core::traits::StorageBackend as _;
use agentos_mcp::CapabilityHandler;
use agentos_user_admin::UserAdminCapabilityHandler;
use axum::body::Body;
use axum::http::Request;
use axum::Router;
use serde_json::{json, Value};
use tower::ServiceExt;

/// 内存库 + handler + 可登录的 router（token 签发用）。
/// handler 与 router 共享同一 store 实例（对齐生产装配）。
/// 播种 admin（与生产 seed_admin_user 一致）——不播种时 login 走内置
/// admin 回退，admin 不会出现在 users 表，list_users 断言会失真。
async fn handler_setup() -> (
    UserAdminCapabilityHandler,
    Router,
    Arc<agentos_engine::SqliteStore>,
) {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let now = chrono::Utc::now().to_rfc3339();
    store
        .create_user(&agentos_core::types::UserRecord {
            user_id: "00000000-0000-0000-0000-000000000001".to_string(),
            username: "admin".to_string(),
            password: "admin12345".to_string(),
            email: Some("admin@agentos.dev".to_string()),
            role: "admin".to_string(),
            tenant_id: "default".to_string(),
            created_at: now,
            last_login_at: None,
        })
        .await
        .unwrap();
    let handler = UserAdminCapabilityHandler::new(Some(store.clone()), Some(store.clone()));
    let mut state = AppState::new();
    state.store = Some(store.clone());
    state.db = Some(store.clone());
    (handler, build_router(state), store)
}

async fn admin_token(router: &Router) -> String {
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

/// 注册一个普通用户并返回 (user_id, access_token)。
async fn register_user(router: &Router, username: &str) -> (String, String) {
    let resp = router
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/auth/register")
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({
                        "username": username,
                        "password": "pass12345",
                        "email": format!("{username}@example.com"),
                    })
                    .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    assert!(status.is_success(), "注册失败: status={status} body={json}");
    let token = json["access_token"].as_str().unwrap().to_string();
    // token 是无分隔符的整段 base64，载荷 {type}:{user_id}:{username}:{exp}
    //——解出 user_id（与内核 resolve_request_user 同一格式）。
    use base64::Engine;
    let payload = base64::engine::general_purpose::STANDARD_NO_PAD
        .decode(&token)
        .unwrap();
    let text = String::from_utf8(payload).unwrap();
    let user_id = text.split(':').nth(1).unwrap().to_string();
    (user_id, token)
}

/// 直调 handler。返回 (status, body_or_error)：
/// 成功 → (status, body)；失败 → (status, error json)。
async fn call(handler: &UserAdminCapabilityHandler, method: &str, params: Value) -> (u16, Value) {
    let resp = handler
        .handle(method, params)
        .await
        .expect("handler 不应返回协议错误");
    let status = resp["status"].as_u64().unwrap_or(500) as u16;
    let payload = if resp.get("body").is_some() {
        resp["body"].clone()
    } else {
        resp["error"].clone()
    };
    if status >= 400 {
        eprintln!("[call] method={method} status={status} payload={payload}");
    }
    (status, payload)
}

/// 带 token 的参数（_authorization = HTTP 面插件透传的原始头）。
fn with_auth(params: Value, token: &str) -> Value {
    let mut p = params;
    p["_authorization"] = json!(format!("Bearer {token}"));
    p
}

/// auth 执行门永留内核（§9.6 判据回归）：拆分后 login/register 端点必须原样可用
/// ——本测试面的一切 token 都经它们签发，此处再显式断言端点存在。
#[tokio::test]
async fn test_auth_execution_gate_stays_in_kernel() {
    let (_handler, router, _store) = handler_setup().await;
    let token = admin_token(&router).await;
    assert!(!token.is_empty(), "login 端点（执行门）必须留在内核可用");

    let resp = router
        .oneshot(
            Request::builder()
                .method("GET")
                .uri("/api/v1/auth/me")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), axum::http::StatusCode::OK);
}

#[tokio::test]
async fn test_list_users_requires_admin() {
    let (handler, router, _store) = handler_setup().await;

    // 无 _authorization → 401
    let (status, _json) = call(&handler, "list_users", json!({})).await;
    assert_eq!(status, 401);

    // 普通用户 → 403（管理面无只读豁免，viewer/user 均拒）
    let (_uid, user_tok) = register_user(&router, "alice").await;
    let (status, json) = call(&handler, "list_users", with_auth(json!({}), &user_tok)).await;
    assert_eq!(status, 403, "普通用户列用户应 403: {json}");
}

#[tokio::test]
async fn test_list_users_returns_sanitized_users() {
    let (handler, router, _store) = handler_setup().await;
    let (_uid, _tok) = register_user(&router, "bob").await;
    let token = admin_token(&router).await;

    let (status, json) = call(&handler, "list_users", with_auth(json!({}), &token)).await;
    assert_eq!(status, 200, "列用户失败: {json}");
    let users = json["users"].as_array().expect("users 数组");
    assert_eq!(json["total"], users.len() as u64);
    // admin（种子）+ bob
    assert_eq!(users.len(), 2, "应含种子 admin 与 bob: {users:?}");
    let usernames: Vec<&str> = users
        .iter()
        .map(|u| u["username"].as_str().unwrap())
        .collect();
    assert!(usernames.contains(&"admin") && usernames.contains(&"bob"));
    // password 剥离（明文密码不得经管理面外泄）
    for u in users {
        assert!(u.get("password").is_none(), "响应不得含 password: {u}");
        assert!(u["id"].is_string());
        assert!(u["role"].is_string());
        assert!(u["tenant_id"].is_string());
    }
}

#[tokio::test]
async fn test_update_role_roundtrip_and_validation() {
    let (handler, router, store) = handler_setup().await;
    let (alice, _tok) = register_user(&router, "alice").await;
    let token = admin_token(&router).await;

    // user → admin
    let (status, json) = call(
        &handler,
        "update_role",
        with_auth(json!({ "user_id": alice, "role": "admin" }), &token),
    )
    .await;
    assert_eq!(status, 200, "改角色失败: {json}");
    assert_eq!(json["user"]["role"], "admin");
    assert!(json["user"].get("password").is_none());
    let updated = store.get_user_by_id(&alice).await.unwrap().unwrap();
    assert_eq!(updated.role, "admin");

    // admin → user（改回来，顺便验证双向）
    let (status, json) = call(
        &handler,
        "update_role",
        with_auth(json!({ "user_id": alice, "role": "user" }), &token),
    )
    .await;
    assert_eq!(status, 200, "{json}");
    assert_eq!(json["user"]["role"], "user");

    // 非法角色 → 400
    let (status, _json) = call(
        &handler,
        "update_role",
        with_auth(json!({ "user_id": alice, "role": "superadmin" }), &token),
    )
    .await;
    assert_eq!(status, 400);

    // 不存在的用户 → 404
    let (status, _json) = call(
        &handler,
        "update_role",
        with_auth(json!({ "user_id": "u-nope", "role": "admin" }), &token),
    )
    .await;
    assert_eq!(status, 404);

    // 缺 role 参数 → 400
    let (status, _json) = call(
        &handler,
        "update_role",
        with_auth(json!({ "user_id": alice }), &token),
    )
    .await;
    assert_eq!(status, 400);

    // 普通用户改他人角色 → 403
    let (_uid, mallory_tok) = register_user(&router, "mallory").await;
    let (status, _json) = call(
        &handler,
        "update_role",
        with_auth(json!({ "user_id": alice, "role": "admin" }), &mallory_tok),
    )
    .await;
    assert_eq!(status, 403, "普通用户改角色应 403");
}

#[tokio::test]
async fn test_update_tenant_moves_membership() {
    let (handler, router, store) = handler_setup().await;
    let (carol, _tok) = register_user(&router, "carol").await;
    let token = admin_token(&router).await;

    let (status, json) = call(
        &handler,
        "update_tenant",
        with_auth(json!({ "user_id": carol, "tenant_id": "team-b" }), &token),
    )
    .await;
    assert_eq!(status, 200, "改租户失败: {json}");
    assert_eq!(json["user"]["tenant_id"], "team-b");
    let updated = store.get_user_by_id(&carol).await.unwrap().unwrap();
    assert_eq!(updated.tenant_id, "team-b");

    // 空租户 → 400；不存在用户 → 404
    let (status, _json) = call(
        &handler,
        "update_tenant",
        with_auth(json!({ "user_id": carol, "tenant_id": "  " }), &token),
    )
    .await;
    assert_eq!(status, 400);
    let (status, _json) = call(
        &handler,
        "update_tenant",
        with_auth(json!({ "user_id": "u-nope", "tenant_id": "t" }), &token),
    )
    .await;
    assert_eq!(status, 404);
}

#[tokio::test]
async fn test_delete_user_removes_membership() {
    let (handler, router, store) = handler_setup().await;
    let (dave, _tok) = register_user(&router, "dave").await;
    let token = admin_token(&router).await;

    let (status, json) = call(
        &handler,
        "delete_user",
        with_auth(json!({ "user_id": dave }), &token),
    )
    .await;
    assert_eq!(status, 200, "删用户失败: {json}");
    assert_eq!(json["deleted"], true);
    assert_eq!(json["user_id"], dave);
    assert!(
        store.get_user_by_id(&dave).await.unwrap().is_none(),
        "应已删除"
    );

    // 再删 → 404
    let (status, _json) = call(
        &handler,
        "delete_user",
        with_auth(json!({ "user_id": dave }), &token),
    )
    .await;
    assert_eq!(status, 404);

    // 普通用户删他人 → 403
    let (_uid, mallory_tok) = register_user(&router, "mallory2").await;
    let (eve, _tok) = register_user(&router, "eve").await;
    let (status, _json) = call(
        &handler,
        "delete_user",
        with_auth(json!({ "user_id": eve }), &mallory_tok),
    )
    .await;
    assert_eq!(status, 403, "普通用户删用户应 403");
}

#[tokio::test]
async fn test_self_protection_blocks_delete_role_tenant_on_self() {
    let (handler, router, _store) = handler_setup().await;
    let token = admin_token(&router).await;
    // 从 token 解出 admin 自己的 user_id（与生产 _authorization 解析路径同源）
    use base64::Engine;
    let payload = base64::engine::general_purpose::STANDARD_NO_PAD
        .decode(&token)
        .unwrap();
    let text = String::from_utf8(payload).unwrap();
    let admin_id = text.split(':').nth(1).unwrap().to_string();

    // 删自己 → 403（防锁死系统）
    let (status, json) = call(
        &handler,
        "delete_user",
        with_auth(json!({ "user_id": admin_id }), &token),
    )
    .await;
    assert_eq!(status, 403, "admin 删自己应 403: {json}");

    // 降自己角色 → 403
    let (status, json) = call(
        &handler,
        "update_role",
        with_auth(json!({ "user_id": admin_id, "role": "user" }), &token),
    )
    .await;
    assert_eq!(status, 403, "admin 降自己角色应 403: {json}");

    // 改自己租户 → 403
    let (status, json) = call(
        &handler,
        "update_tenant",
        with_auth(json!({ "user_id": admin_id, "tenant_id": "other" }), &token),
    )
    .await;
    assert_eq!(status, 403, "admin 改自己租户应 403: {json}");
}

#[tokio::test]
async fn test_unknown_method_rejected() {
    let (handler, router, _store) = handler_setup().await;
    let token = admin_token(&router).await;
    let result = handler
        .handle("create_user", with_auth(json!({}), &token))
        .await;
    // 未在清单的 method → McpError::Protocol（协议层拒绝，非业务 4xx 信封）
    assert!(result.is_err(), "不在清单的 method 应协议层拒绝");
    assert!(format!("{}", result.unwrap_err()).contains("not implemented"));
}

#[tokio::test]
async fn test_registry_route_via_trait_roundtrip() {
    // 经 CapabilityHandlerRegistry（生产 reader loop 的真实路由路径）验证注册即路由。
    use agentos_mcp::CapabilityRouter;
    let (handler, router, _store) = handler_setup().await;
    let token = admin_token(&router).await;
    let registry = Arc::new(agentos_mcp::CapabilityHandlerRegistry::new());
    registry.register(Arc::new(handler));
    assert!(registry.has_namespace(agentos_user_admin::NAMESPACE));
    let dyn_router: Arc<dyn CapabilityRouter> = registry;
    assert!(dyn_router
        .known_namespaces()
        .contains(&agentos_user_admin::NAMESPACE.to_string()));
    let envelope = dyn_router
        .handle(
            agentos_user_admin::NAMESPACE,
            "list_users",
            with_auth(json!({}), &token),
        )
        .await
        .unwrap();
    assert_eq!(envelope["status"], 200);
}
