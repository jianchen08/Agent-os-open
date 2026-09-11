// @feature: FP-0.2.七 路由收敛 | @vision: V4 多用户 | @ci: rust-test
//! W3-2（D2 第一刀）：/ext 分发执行 manifest `http_endpoints[].auth` 声明的行为测试。
//!
//! 契约（D2 两刀均落地）：
//! - `auth: "user"` → 与 /api/v1 同一 token 校验（resolve_request_user：HMAC 验签/过期/类型/口令绑定/用户存在）
//! - `auth: "admin"` → 同上 + admin 角色（不足 403）
//! - `auth: "none"` → 显式匿名白名单：全部方法放行（webhook 验签自管等）
//! - 无声明（缺省）→ 一律 401 fail-closed（读写同规）
//! - 已认证（user/admin）分发 → 注入身份头 X-AgentOS-Tenant / X-AgentOS-User /
//!   X-AgentOS-Role（插件侧租户过滤/归属校验的信任锚）
//!
//! 测试断言可观察行为（HTTP 状态码），token 用真实 HMAC 签发链路构造
//! （agentos_http::auth::encode_token），不 mock 验证逻辑。

use std::collections::HashMap;
use std::sync::Arc;

use agentos_core::traits::{
    CapabilityRegistry, HttpEndpoint, HttpHandleCapability, HttpHandleRequest, HttpHandleResponse,
    StorageBackend,
};
use agentos_http::auth::{encode_token, hash_password, BuiltInUser, TokenType};
use agentos_plugin_loader::CapabilityRegistryImpl;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

use agentos_api::routes::AppState;
use agentos_api::server::build_router;

/// 命中即 200 的最小 handler（鉴权判定在 dispatcher，插件 handler 不参与）。
struct OkHandler;

#[async_trait::async_trait]
impl HttpHandleCapability for OkHandler {
    async fn handle(&self, _req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        Ok(HttpHandleResponse {
            status: 200,
            headers: HashMap::new(),
            body: String::new(),
            body_encoding: "base64".to_string(),
        })
    }
}

/// 构造端点声明；`auth: None` = manifest 缺省（无声明）。
fn endpoint(route_id: &str, method: &str, path: &str, auth: Option<&str>) -> HttpEndpoint {
    HttpEndpoint {
        route_id: route_id.to_string(),
        method: method.to_string(),
        path: path.to_string(),
        auth: auth.map(|s| s.to_string()),
        handler_capability: "http.handle".to_string(),
        timeout_ms: None,
        max_concurrency: None,
        description: None,
    }
}

/// 无 store 的 state：resolve 走内置脚手架表（仅内置 admin 可解析）。
fn state_with_routes(routes: &[(&str, &str, &str, Option<&str>)]) -> AppState {
    let mut state = AppState::new();
    let registry = Arc::new(CapabilityRegistryImpl::new());
    for (route_id, method, path, auth) in routes {
        registry
            .register_http_route("p1", endpoint(route_id, method, path, *auth))
            .unwrap();
    }
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(OkHandler));
    state
}

/// 为内置脚手架 admin 签发 access token（store=None 时可被 resolve 命中）。
fn admin_access_token(ttl_secs: u64) -> String {
    encode_token(
        TokenType::Access,
        &BuiltInUser {
            id: "00000000-0000-0000-0000-000000000001".to_string(),
            username: "admin".to_string(),
            password: String::new(),
            email: String::new(),
            role: "admin".to_string(),
            tenant_id: "default".to_string(),
            created_at: String::new(),
            must_change_password: false,
        },
        ttl_secs,
    )
}

fn bearer(token: &str) -> String {
    format!("Bearer {token}")
}

async fn send(
    app: axum::Router,
    method: &str,
    uri: &str,
    auth_header: Option<String>,
) -> StatusCode {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(h) = auth_header {
        builder = builder.header("authorization", h);
    }
    let resp = app
        .oneshot(builder.body(Body::empty()).unwrap())
        .await
        .unwrap();
    resp.status()
}

// ── 1) 声明 user：带 token 过 / 无 token·坏 token 401 ────────────────

#[tokio::test]
async fn declared_user_accepts_valid_bearer_token() {
    // 两组区分度输入：GET 读面与 POST 写面同持合法 token 均放行
    let app = build_router(state_with_routes(&[
        ("r-get", "GET", "/ext/p1/thing", Some("user")),
        ("r-post", "POST", "/ext/p1/thing", Some("user")),
    ]));
    let token = admin_access_token(3600);
    assert_eq!(
        send(app.clone(), "GET", "/ext/p1/thing", Some(bearer(&token))).await,
        StatusCode::OK,
        "声明 user 的读面持合法 token 应放行"
    );
    assert_eq!(
        send(app, "POST", "/ext/p1/thing", Some(bearer(&token))).await,
        StatusCode::OK,
        "声明 user 的写面持合法 token 应放行"
    );
}

#[tokio::test]
async fn declared_user_rejects_missing_invalid_or_expired_token() {
    // 三组区分度输入：缺失 / 篡改（签名不过）/ 过期 / refresh 类型——一律 401
    let app = build_router(state_with_routes(&[(
        "r",
        "POST",
        "/ext/p1/thing",
        Some("user"),
    )]));
    assert_eq!(
        send(app.clone(), "POST", "/ext/p1/thing", None).await,
        StatusCode::UNAUTHORIZED,
        "无 token 必须拒绝"
    );
    assert_eq!(
        send(
            app.clone(),
            "POST",
            "/ext/p1/thing",
            Some(bearer("forged.not-signature")),
        )
        .await,
        StatusCode::UNAUTHORIZED,
        "签名不过的 token 必须拒绝"
    );
    assert_eq!(
        send(
            app.clone(),
            "POST",
            "/ext/p1/thing",
            Some(bearer(&admin_access_token(0))),
        )
        .await,
        StatusCode::UNAUTHORIZED,
        "过期 token 必须拒绝（ttl=0 即刻过期）"
    );
    let refresh = encode_token(
        TokenType::Refresh,
        &BuiltInUser {
            id: "00000000-0000-0000-0000-000000000001".to_string(),
            username: "admin".to_string(),
            password: String::new(),
            email: String::new(),
            role: "admin".to_string(),
            tenant_id: "default".to_string(),
            created_at: String::new(),
            must_change_password: false,
        },
        3600,
    );
    assert_eq!(
        send(app, "POST", "/ext/p1/thing", Some(bearer(&refresh))).await,
        StatusCode::UNAUTHORIZED,
        "refresh token 不得过管理面鉴权"
    );
}

// ── 2) 无声明：写面 fail-closed 401 / 读面放行 + warn ────────────────

#[tokio::test]
async fn undeclared_write_surface_fails_closed() {
    // 两组区分度输入：POST 与 DELETE 无声明写面一律 401（fail-closed 默认拒绝）
    let app = build_router(state_with_routes(&[
        ("r-post", "POST", "/ext/p1/thing", None),
        ("r-del", "DELETE", "/ext/p1/thing", None),
    ]));
    assert_eq!(
        send(app.clone(), "POST", "/ext/p1/thing", None).await,
        StatusCode::UNAUTHORIZED,
        "无声明写面必须默认拒绝（fail-closed）"
    );
    assert_eq!(
        send(app, "DELETE", "/ext/p1/thing", None).await,
        StatusCode::UNAUTHORIZED,
        "无声明写面（DELETE）必须默认拒绝"
    );
}

#[tokio::test]
async fn undeclared_read_surface_fails_closed() {
    // 第二刀收紧：无声明 GET 与写面同规 401（读面数据——监控快照/trace/配置
    // ——与写面同样敏感，缺省即拒绝）
    let app = build_router(state_with_routes(&[("r", "GET", "/ext/p1/thing", None)]));
    assert_eq!(
        send(app, "GET", "/ext/p1/thing", None).await,
        StatusCode::UNAUTHORIZED,
        "无声明读面默认拒绝（与写面同规 fail-closed）"
    );
}

// ── 3) 显式 none：匿名写面放行（webhook 白名单语义） ─────────────────

#[tokio::test]
async fn explicit_none_allows_anonymous_writes() {
    // 两组区分度输入：POST 与 PUT 显式 none 均匿名放行
    let app = build_router(state_with_routes(&[
        ("r-post", "POST", "/ext/p1/cb", Some("none")),
        ("r-put", "PUT", "/ext/p1/cfg", Some("none")),
    ]));
    assert_eq!(
        send(app.clone(), "POST", "/ext/p1/cb", None).await,
        StatusCode::OK,
        "显式 none 的写面（webhook 类）匿名放行"
    );
    assert_eq!(
        send(app, "PUT", "/ext/p1/cfg", None).await,
        StatusCode::OK,
        "显式 none 的写面（PUT）匿名放行"
    );
}

// ── 4) 声明 admin：admin 角色 200 / 非 admin 角色 403 / 无 token 401 ──

async fn store_with_users(admin_pw: &str, bob_pw: &str) -> Arc<dyn StorageBackend> {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    for (id, name, pw, role) in [
        (
            "00000000-0000-0000-0000-000000000001",
            "admin",
            admin_pw,
            "admin",
        ),
        ("u-bob-0001", "bob", bob_pw, "user"),
    ] {
        store
            .create_user(&agentos_core::types::UserRecord {
                user_id: id.to_string(),
                username: name.to_string(),
                password: pw.to_string(),
                email: None,
                role: role.to_string(),
                tenant_id: "default".to_string(),
                created_at: chrono::Utc::now().to_rfc3339(),
                last_login_at: None,
                must_change_password: false,
            })
            .await
            .unwrap();
    }
    store as Arc<dyn StorageBackend>
}

fn token_for(username: &str, password_hash: &str) -> String {
    encode_token(
        TokenType::Access,
        &BuiltInUser {
            id: "u-x".to_string(),
            username: username.to_string(),
            password: password_hash.to_string(),
            email: String::new(),
            role: "user".to_string(),
            tenant_id: "default".to_string(),
            created_at: String::new(),
            must_change_password: false,
        },
        3600,
    )
}

#[tokio::test]
async fn declared_admin_endpoint_enforces_role() {
    // 哈希只算一次：口令绑定段 pwv = sha256(存储哈希)，store 与 token 必须同源
    let admin_hash = hash_password("admin-pw-2026").unwrap();
    let bob_hash = hash_password("bob-pw-2026").unwrap();
    let store = store_with_users(&admin_hash, &bob_hash).await;
    let mut state = state_with_routes(&[("r", "POST", "/ext/p1/cfg", Some("admin"))]);
    state.store = Some(store);
    let app = build_router(state);

    assert_eq!(
        send(
            app.clone(),
            "POST",
            "/ext/p1/cfg",
            Some(bearer(&token_for("admin", &admin_hash))),
        )
        .await,
        StatusCode::OK,
        "admin 角色持 token 应放行"
    );
    assert_eq!(
        send(
            app.clone(),
            "POST",
            "/ext/p1/cfg",
            Some(bearer(&token_for("bob", &bob_hash))),
        )
        .await,
        StatusCode::FORBIDDEN,
        "非 admin 角色持合法 token 应 403"
    );
    assert_eq!(
        send(app, "POST", "/ext/p1/cfg", None).await,
        StatusCode::UNAUTHORIZED,
        "无 token 访问 admin 端点应 401"
    );
}

// ── 5) datasource 代理与 /ext 同闸：不能借代理绕过鉴权 ───────────────

#[tokio::test]
async fn datasource_proxy_shares_ext_auth_gate() {
    let app = build_router(state_with_routes(&[(
        "r",
        "POST",
        "/ext/p1/thing",
        Some("user"),
    )]));
    assert_eq!(
        send(app.clone(), "POST", "/api/v1/datasource/p1/thing", None).await,
        StatusCode::UNAUTHORIZED,
        "datasource 代理改写后的 /ext 写面同样必须鉴权（无代理绕过）"
    );
    let token = admin_access_token(3600);
    assert_eq!(
        send(
            app,
            "POST",
            "/api/v1/datasource/p1/thing",
            Some(bearer(&token)),
        )
        .await,
        StatusCode::OK,
        "datasource 代理持合法 token 应放行"
    );
}

// ── 6) 身份头注入：已认证分发携带 X-AgentOS-Tenant/User/Role ─────────

/// 捕获到达插件的请求（断言身份头注入用）。
struct CapturingHandler {
    captured: std::sync::Mutex<Option<HttpHandleRequest>>,
}

#[async_trait::async_trait]
impl HttpHandleCapability for CapturingHandler {
    async fn handle(&self, req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        *self.captured.lock().unwrap() = Some(req);
        Ok(HttpHandleResponse {
            status: 200,
            headers: HashMap::new(),
            body: String::new(),
            body_encoding: "base64".to_string(),
        })
    }
}

#[tokio::test]
async fn authenticated_dispatch_injects_identity_headers() {
    // store 播种 admin（tenant=default）+ token 同源 → 分发到插件的请求
    // 必须携带内核注入的身份三头（值 = 已验签用户记录）。
    let admin_hash = hash_password("admin-pw-2026").unwrap();
    let store = store_with_users(&admin_hash, &hash_password("bob-pw-2026").unwrap()).await;
    let mut state = state_with_routes(&[
        ("r-user", "GET", "/ext/p1/data", Some("user")),
        ("r-none", "GET", "/ext/p1/anon", Some("none")),
    ]);
    state.store = Some(store);
    let captured = std::sync::Arc::new(CapturingHandler {
        captured: std::sync::Mutex::new(None),
    });
    state.http_handler = Some(captured.clone() as Arc<dyn HttpHandleCapability>);
    let app = build_router(state);

    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("GET")
                .uri("/ext/p1/data")
                .header("authorization", bearer(&token_for("admin", &admin_hash)))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let req = captured
        .captured
        .lock()
        .unwrap()
        .take()
        .expect("请求应到达插件");
    assert_eq!(
        req.headers.get("x-agentos-tenant").map(String::as_str),
        Some("default"),
        "已认证分发必须注入 X-AgentOS-Tenant（插件租户过滤的信任锚）"
    );
    assert_eq!(
        req.headers.get("x-agentos-role").map(String::as_str),
        Some("admin"),
        "已认证分发必须注入 X-AgentOS-Role"
    );
    assert!(
        req.headers.contains_key("x-agentos-user"),
        "已认证分发必须注入 X-AgentOS-User"
    );

    // 显式 none 路由：匿名放行且不注入身份（无身份可注入）
    let resp = app
        .oneshot(
            Request::builder()
                .method("GET")
                .uri("/ext/p1/anon")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let req = captured
        .captured
        .lock()
        .unwrap()
        .take()
        .expect("匿名请求也应到达插件");
    assert!(
        !req.headers.contains_key("x-agentos-tenant"),
        "匿名（none）分发不得注入身份头"
    );
}

/// A2：匿名（auth:"none"）端点剥离客户端伪造的身份头——透传给插件的请求
/// 不得携带客户端自带的三头（键集与认证分支覆盖注入严格一致），否则伪造
/// 租户/用户/角色会被插件侧租户过滤当真（越权读写他人租户数据）。
#[tokio::test]
async fn anonymous_dispatch_strips_client_spoofed_identity_headers() {
    let admin_hash = hash_password("admin-pw-2026").unwrap();
    let store = store_with_users(&admin_hash, &hash_password("bob-pw-2026").unwrap()).await;
    let mut state = state_with_routes(&[("r-none", "GET", "/ext/p1/anon", Some("none"))]);
    state.store = Some(store);
    let captured = std::sync::Arc::new(CapturingHandler {
        captured: std::sync::Mutex::new(None),
    });
    state.http_handler = Some(captured.clone() as Arc<dyn HttpHandleCapability>);
    let app = build_router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .method("GET")
                .uri("/ext/p1/anon")
                .header("x-agentos-tenant", "victim-tenant")
                .header("x-agentos-user", "victim-user")
                .header("x-agentos-role", "admin")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK, "匿名放行语义保持");

    let req = captured
        .captured
        .lock()
        .unwrap()
        .take()
        .expect("匿名请求应到达插件");
    for spoofed in ["x-agentos-tenant", "x-agentos-user", "x-agentos-role"] {
        assert!(
            !req.headers.contains_key(spoofed),
            "匿名分发必须剥离客户端伪造的 {spoofed} 头，实际: {:?}",
            req.headers.get(spoofed)
        );
    }
}
