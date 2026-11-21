// @feature: FP-0.2.内核API面 | @vision: V3 可嵌入 | @ci: rust-test
use super::*;

fn plain_app() -> axum::Router {
    crate::server::build_router(AppState::new())
}

async fn send(
    app: &axum::Router,
    method: &str,
    uri: &str,
    headers: &[(&str, &str)],
    body: Option<String>,
) -> axum::response::Response {
    let mut builder = Request::builder().method(method).uri(uri);
    for (k, v) in headers {
        builder = builder.header(*k, *v);
    }
    let req = match body {
        Some(b) => builder
            .header("content-type", "application/json")
            .body(Body::from(b))
            .unwrap(),
        None => builder.body(Body::empty()).unwrap(),
    };
    app.clone().oneshot(req).await.unwrap()
}

// ── CORS 源判定（纯函数） ──────────────────────────────

#[test]
fn is_local_origin_matches_exact_and_port_forms_only() {
    for origin in [
        "http://localhost",
        "https://localhost",
        "http://127.0.0.1",
        "http://127.0.0.1:9100",
        "https://[::1]",
        "http://[::1]:5173",
    ] {
        assert!(is_local_origin(origin), "{origin} 应判本地源");
    }
    for origin in [
        "http://localhost.evil.com",
        "https://example.com",
        "localhost",
        "",
    ] {
        assert!(!is_local_origin(origin), "{origin} 不应判本地源");
    }
}

#[test]
fn origin_matches_allowlist_is_exact_match() {
    let allow = ["https://app.example.com", "https://b.example.com"];
    assert!(origin_matches_allowlist("https://app.example.com", &allow));
    assert!(!origin_matches_allowlist(
        "https://app.example.com.evil.com",
        &allow
    ));
    assert!(!origin_matches_allowlist("https://c.example.com", &allow));
    assert!(!origin_matches_allowlist("", &allow));
}

// ── CORS 中间件（oneshot 行为面） ──────────────────────

#[tokio::test]
async fn preflight_options_returns_204_with_cors_headers() {
    let app = plain_app();
    let resp = send(
        &app,
        "OPTIONS",
        "/api/v1/chat",
        &[("origin", "http://localhost:5173")],
        None,
    )
    .await;
    assert_eq!(resp.status(), StatusCode::NO_CONTENT, "预检拦截 204");
    let h = resp.headers();
    assert_eq!(h["access-control-allow-origin"], "http://localhost:5173");
    assert_eq!(
        h["access-control-allow-methods"],
        "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    );
    assert_eq!(h["access-control-max-age"], "86400");
    assert!(
        h.get("access-control-allow-credentials").is_none(),
        "本地源反射不带凭据头"
    );
}

#[tokio::test]
async fn responses_reflect_allowed_origin_and_deny_unknown() {
    let app = plain_app();
    let resp = send(
        &app,
        "GET",
        "/health",
        &[("origin", "http://127.0.0.1:5173")],
        None,
    )
    .await;
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(
        resp.headers()["access-control-allow-origin"],
        "http://127.0.0.1:5173"
    );

    // 非放行源：响应照常 200，但不反射 origin
    let resp = send(
        &app,
        "GET",
        "/health",
        &[("origin", "https://evil.com")],
        None,
    )
    .await;
    assert_eq!(resp.status(), StatusCode::OK);
    assert!(resp.headers().get("access-control-allow-origin").is_none());
}

#[tokio::test]
async fn explicit_allowlist_origin_gets_credentials() {
    // 本用例独占操作 AGENTOS_CORS_ORIGINS（进程全局），其余 CORS 用例
    // 只走本地源判定，不受影响。
    std::env::set_var("AGENTOS_CORS_ORIGINS", "https://app.example.com");
    let app = plain_app();
    let resp = send(
        &app,
        "OPTIONS",
        "/api/v1/chat",
        &[("origin", "https://app.example.com")],
        None,
    )
    .await;
    assert_eq!(resp.status(), StatusCode::NO_CONTENT);
    let h = resp.headers();
    assert_eq!(h["access-control-allow-origin"], "https://app.example.com");
    assert_eq!(
        h["access-control-allow-credentials"], "true",
        "显式白名单源带凭据放行"
    );
    assert!(is_origin_allowed("https://app.example.com"));
    assert!(!is_origin_allowed("https://c.example.com"));

    std::env::remove_var("AGENTOS_CORS_ORIGINS");
    let resp = send(
        &app,
        "OPTIONS",
        "/api/v1/chat",
        &[("origin", "https://app.example.com")],
        None,
    )
    .await;
    assert!(
        resp.headers().get("access-control-allow-origin").is_none(),
        "环境变量移除后白名单失效（仅本地源放行）"
    );
}

// ── write_surface_auth 鉴权面（default-deny 枚举制，ADR 2026-10-10）──

#[tokio::test]
async fn protected_paths_reject_anonymous_with_401() {
    let app = plain_app();
    for (method, uri) in [
        ("GET", "/api/v1/plugins"),
        ("GET", "/api/v1/sessions"),
        ("GET", "/api/v1/pipelines"),
        ("GET", "/metrics"),
        ("GET", "/api/v1/schema"),
        ("GET", "/api/v1/tools"),
        ("GET", "/api/v1/system/memstats"),
        // 翻转新增面：/uploads 收紧（ADR 2026-10-10，此前匿名）
        ("GET", "/uploads/pic.png"),
        // /api/v1/datasource 代理面默认鉴权（前端 apiClient 带 token）
        ("GET", "/api/v1/datasource/ext/a/b"),
        // /api/v1/config/env（用户空间 .env 读面，此前匿名可达）
        ("GET", "/api/v1/config/env"),
        ("PUT", "/api/v1/config/env"),
    ] {
        let resp = send(&app, method, uri, &[], None).await;
        assert_eq!(
            resp.status(),
            StatusCode::UNAUTHORIZED,
            "{method} {uri} 匿名应 401"
        );
    }
}

/// 前缀内假想路由（未注册、不在任何清单）匿名同样 401——受保护前缀
/// default-deny 的防拟合断言：保护来自前缀语义而非逐路由登记。
#[tokio::test]
async fn hypothetical_route_under_protected_prefix_rejects_anonymous() {
    let app = plain_app();
    let resp = send(&app, "GET", "/api/v1/brand-new-surface", &[], None).await;
    assert_eq!(
        resp.status(),
        StatusCode::UNAUTHORIZED,
        "受保护前缀内未注册路径匿名应 401（中间件先于 fallback）"
    );
    let resp = send(&app, "GET", "/uploads/nonexistent.png", &[], None).await;
    assert_eq!(
        resp.status(),
        StatusCode::UNAUTHORIZED,
        "文件不存在也应 401 而非 404（不泄露存在性）"
    );
}

#[tokio::test]
async fn exempted_paths_pass_anonymous_to_handler() {
    let app = plain_app();
    // /health 域外（release.yml 冒烟/装机探测）→ 匿名 200
    let resp = send(&app, "GET", "/health", &[], None).await;
    assert_eq!(resp.status(), StatusCode::OK);
    // 登录/注册豁免：缺 body → 422（提取器拒绝），而非中间件 401
    for uri in ["/api/v1/auth/login", "/api/v1/auth/register"] {
        let resp = send(&app, "POST", uri, &[], None).await;
        assert_ne!(
            resp.status(),
            StatusCode::UNAUTHORIZED,
            "{uri} 豁免内不挂鉴权"
        );
    }
    // 登录错误凭据 → handler 业务 400（区别于中间件拦截 401 的语义归属）
    let resp = send(
        &app,
        "POST",
        "/api/v1/auth/login",
        &[],
        Some(json!({"username": "admin", "password": "wrong"}).to_string()),
    )
    .await;
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST, "错误凭据是业务 400");
    // logout 仅消费请求体 refresh token → 匿名空体 200
    let resp = send(
        &app,
        "POST",
        "/api/v1/auth/logout",
        &[],
        Some(json!({}).to_string()),
    )
    .await;
    assert_eq!(resp.status(), StatusCode::OK);
    // refresh 垃圾 token → handler 自校验 401
    let resp = send(
        &app,
        "POST",
        "/api/v1/auth/refresh",
        &[],
        Some(json!({"refresh_token": "garbage"}).to_string()),
    )
    .await;
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    // me 无 token → handler 自校验 401；带合法 token → 200（路由贯通）
    let resp = send(&app, "GET", "/api/v1/auth/me", &[], None).await;
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    let resp = send(
        &app,
        "GET",
        "/api/v1/auth/me",
        &[("authorization", &scaffold_admin_bearer())],
        None,
    )
    .await;
    assert_eq!(resp.status(), StatusCode::OK);
    // change-password 自服务面：带 body 无 token → handler 自校验 401；
    // 豁免使其免受写面角色门（普通用户改自己口令不得 403）
    let resp = send(
        &app,
        "POST",
        "/api/v1/auth/change-password",
        &[],
        Some(json!({"old_password": "a", "new_password": "b"}).to_string()),
    )
    .await;
    assert_eq!(
        resp.status(),
        StatusCode::UNAUTHORIZED,
        "无 token 由 handler 401（自校验），非角色门 403"
    );
    // /ws/chat 豁免：无升级头 GET 由握手提取器自答，不得是中间件 401
    let resp = send(&app, "GET", "/ws/chat", &[], None).await;
    assert_ne!(
        resp.status(),
        StatusCode::UNAUTHORIZED,
        "/ws/chat 握手自鉴权（票据/token），中间件放行"
    );
}

/// 内存库播种 admin + viewer 的 app 与两枚 token（同一次哈希绑定）。
async fn app_with_admin_and_viewer() -> (axum::Router, String, String) {
    let store = std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let admin_hash = agentos_http::auth::hash_password(SEED_ADMIN_PW).unwrap();
    let viewer_hash = agentos_http::auth::hash_password("viewer-pw-2026").unwrap();
    for (uid, name, hash, role) in [
        (
            "00000000-0000-0000-0000-000000000001",
            "admin",
            admin_hash.clone(),
            "admin",
        ),
        (
            "00000000-0000-0000-0000-000000000002",
            "viewer",
            viewer_hash.clone(),
            "viewer",
        ),
    ] {
        let _ = StorageBackend::create_user(
            store.as_ref(),
            &agentos_core::types::UserRecord {
                user_id: uid.to_string(),
                username: name.to_string(),
                password: hash.clone(),
                email: None,
                role: role.to_string(),
                tenant_id: "default".to_string(),
                created_at: chrono::Utc::now().to_rfc3339(),
                last_login_at: None,
                must_change_password: false,
            },
        )
        .await;
    }
    let mint = |uid: &str, name: &str, hash: String, role: &str| {
        agentos_http::auth::encode_token(
            agentos_http::auth::TokenType::Access,
            &agentos_http::auth::BuiltInUser {
                id: uid.to_string(),
                username: name.to_string(),
                password: hash,
                email: String::new(),
                role: role.to_string(),
                tenant_id: "default".to_string(),
                created_at: String::new(),
                must_change_password: false,
            },
            3600,
        )
    };
    let admin_token = mint(
        "00000000-0000-0000-0000-000000000001",
        "admin",
        admin_hash,
        "admin",
    );
    let viewer_token = mint(
        "00000000-0000-0000-0000-000000000002",
        "viewer",
        viewer_hash,
        "viewer",
    );
    let mut state = AppState::new();
    state.store = Some(store);
    (
        crate::server::build_router(state),
        admin_token,
        viewer_token,
    )
}

#[tokio::test]
async fn viewer_can_read_but_write_needs_admin() {
    let (app, admin, viewer) = app_with_admin_and_viewer().await;
    let bearer = format!("Bearer {viewer}");

    // viewer 读面放行（GET /api/v1/plugins → 200 空清单）
    let resp = send(
        &app,
        "GET",
        "/api/v1/plugins",
        &[("authorization", &bearer)],
        None,
    )
    .await;
    assert_eq!(resp.status(), StatusCode::OK, "viewer 读面应放行");

    // viewer 写面拒绝（PUT enabled → 403）
    let resp = send(
        &app,
        "PUT",
        "/api/v1/plugins/some_plugin/enabled",
        &[("authorization", &bearer)],
        Some(json!({"enabled": true}).to_string()),
    )
    .await;
    assert_eq!(resp.status(), StatusCode::FORBIDDEN, "viewer 写面应 403");

    // admin 写面通过鉴权层（project_root 未接线由 handler 报 5xx，非 401/403）
    let resp = send(
        &app,
        "PUT",
        "/api/v1/plugins/some_plugin/enabled",
        &[("authorization", &format!("Bearer {admin}"))],
        Some(json!({"enabled": true}).to_string()),
    )
    .await;
    assert_ne!(
        resp.status(),
        StatusCode::UNAUTHORIZED,
        "admin 写面不应被鉴权层拒绝"
    );
    assert_ne!(resp.status(), StatusCode::FORBIDDEN, "admin 写面不应 403");
}

#[tokio::test]
async fn ws_ticket_any_authenticated_user_can_issue() {
    let app = plain_app();
    // 匿名 → 401
    let resp = send(&app, "POST", "/api/v1/ws-ticket", &[], None).await;
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    // 内置 admin（store None 回退内置表）→ 200 票据
    let resp = send(
        &app,
        "POST",
        "/api/v1/ws-ticket",
        &[("authorization", &scaffold_admin_bearer())],
        None,
    )
    .await;
    assert_eq!(resp.status(), StatusCode::OK, "已认证用户即可签发票据");
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert!(v["ticket"].as_str().is_some(), "响应应含 ticket");
    assert!(v["expires_in"].as_u64().unwrap_or(0) > 0);
}

// ── WS 入口：session 未装配显式 503 ────────────────────
// WebSocketUpgrade 提取器依赖 hyper 注入的 OnUpgrade extension，oneshot
// 无传输层不可达——起真实 TCP 服务 + tungstenite 客户端连接（同
// tests/ws_ticket_test.rs 构造）。

/// 起真实 TCP 服务，返回 (本地地址, shutdown 句柄)。
async fn spawn_server(state: AppState) -> (std::net::SocketAddr, tokio::task::JoinHandle<()>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = crate::server::build_router(state);
    let handle = tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    (addr, handle)
}

#[tokio::test]
async fn ws_handshake_without_session_coordinator_returns_503() {
    // session 未装配（仅测试构造场景）→ 握手前显式 503，不静默降级
    let (addr, handle) = spawn_server(AppState::new()).await;
    let err = tokio_tungstenite::connect_async(format!("ws://{addr}/ws/chat?token=x"))
        .await
        .expect_err("session 未装配握手必须被拒");
    match err {
        tokio_tungstenite::tungstenite::Error::Http(resp) => {
            assert_eq!(
                resp.status(),
                StatusCode::SERVICE_UNAVAILABLE,
                "握手拒绝应 503"
            );
        }
        other => panic!("应返回 HTTP 503 拒绝，实际 {other:?}"),
    }
    handle.abort();
}

#[tokio::test]
async fn ws_handshake_without_inbound_router_returns_503() {
    // session 已装配但 inbound_router 缺席（半装配态）→ 同样 503
    let state = AppState {
        session: Some(Arc::new(agentos_session::SessionCoordinator::new())),
        inbound_router: None,
        ..AppState::new()
    };
    let (addr, handle) = spawn_server(state).await;
    let err = tokio_tungstenite::connect_async(format!("ws://{addr}/ws/chat?token=x"))
        .await
        .expect_err("inbound_router 未装配握手必须被拒");
    match err {
        tokio_tungstenite::tungstenite::Error::Http(resp) => {
            assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
        }
        other => panic!("应返回 HTTP 503 拒绝，实际 {other:?}"),
    }
    handle.abort();
}

// ── 交互应答端点（P1-3 声明驱动路由） ──────────────────

fn interaction_manifest(namespace: &str) -> agentos_core::traits::PluginManifest {
    serde_json::from_value(json!({
        "id": "p_interact", "name": "p_interact", "version": "1.0.0",
        "plugin_type": "system", "language": "python",
        "host_type": "sidecar", "entry": "x",
        "provides": {"capabilities": [{
            "namespace": namespace,
            "methods": ["respond"],
            "protocol_roles": [{"role": "interaction-respond", "method": "respond"}],
        }]},
        "capabilities": {},
    }))
    .expect("valid manifest")
}

/// 可编程 CapabilityHandler 桩（interaction 路由面）。
struct StubCapabilityHandler {
    ns: String,
    result: std::sync::Mutex<Option<Result<serde_json::Value, agentos_mcp::McpError>>>,
}

#[async_trait::async_trait]
impl agentos_mcp::CapabilityHandler for StubCapabilityHandler {
    fn namespace(&self) -> &str {
        &self.ns
    }
    async fn handle(
        &self,
        _method: &str,
        _params: serde_json::Value,
    ) -> Result<serde_json::Value, agentos_mcp::McpError> {
        self.result
            .lock()
            .unwrap()
            .clone()
            .expect("handle 不应被调用")
    }
}

#[test]
fn resolve_interaction_responder_finds_declared_role() {
    let manifests = vec![interaction_manifest("itest")];
    let hit =
        resolve_interaction_responder(&manifests).expect("声明了 interaction-respond 角色应命中");
    assert_eq!(hit, ("itest".to_string(), "respond".to_string()));
    // 无 provides → None（fail-closed 不猜路由目标）
    let bare = vec![
        serde_json::from_value::<agentos_core::traits::PluginManifest>(json!({
            "id": "p_bare", "name": "p_bare", "version": "1.0.0",
            "plugin_type": "system", "language": "python",
            "host_type": "sidecar", "entry": "x",
            "capabilities": {},
        }))
        .expect("valid manifest"),
    ];
    assert!(resolve_interaction_responder(&bare).is_none());
}

#[tokio::test]
async fn interaction_response_guards_then_routes() {
    // 缺 request_id → 显式错误载荷
    let state = AppState::new();
    let resp = interaction_response_handler(axum::extract::State(state), axum::Json(json!({})))
        .await
        .unwrap();
    assert_eq!(resp.0["success"], false);
    assert_eq!(resp.0["error"], "缺少 request_id");

    // capability registry 未装配 → 显式错误
    let state = AppState::new();
    let resp = interaction_response_handler(
        axum::extract::State(state),
        axum::Json(json!({"request_id": "r1", "choice": 1})),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["success"], false);
    assert!(resp.0["error"]
        .as_str()
        .unwrap()
        .contains("registry not available"));

    // registry 在场但无插件声明角色 → 显式失败不猜路由
    let mut state = AppState::new();
    state.capability_handlers = Some(Arc::new(agentos_mcp::CapabilityHandlerRegistry::new()));
    let resp = interaction_response_handler(
        axum::extract::State(state),
        axum::Json(json!({"request_id": "r1"})),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["success"], false);
    assert!(resp.0["error"]
        .as_str()
        .unwrap()
        .contains("no plugin declares"));

    // 声明角色 + handler 成功 → success:true + data 透传
    let mut state = AppState::new();
    let registry = Arc::new(agentos_mcp::CapabilityHandlerRegistry::new());
    registry.register(Arc::new(StubCapabilityHandler {
        ns: "itest".to_string(),
        result: std::sync::Mutex::new(Some(Ok(json!({"answered": true})))),
    }));
    state.capability_handlers = Some(registry);
    *state.manifests.write().await = vec![interaction_manifest("itest")];
    let resp = interaction_response_handler(
        axum::extract::State(state),
        axum::Json(json!({"request_id": "r2", "response_type": "choice"})),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["success"], true);
    assert_eq!(resp.0["request_id"], "r2");
    assert_eq!(resp.0["data"]["answered"], true);

    // handler 失败 → success:false + 错误透传
    let mut state = AppState::new();
    let registry = Arc::new(agentos_mcp::CapabilityHandlerRegistry::new());
    registry.register(Arc::new(StubCapabilityHandler {
        ns: "itest".to_string(),
        result: std::sync::Mutex::new(Some(Err(agentos_mcp::McpError::Protocol {
            message: "interaction session gone".to_string(),
        }))),
    }));
    state.capability_handlers = Some(registry);
    *state.manifests.write().await = vec![interaction_manifest("itest")];
    let resp = interaction_response_handler(
        axum::extract::State(state),
        axum::Json(json!({"request_id": "r3"})),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["success"], false);
    assert_eq!(resp.0["request_id"], "r3");
    assert!(resp.0["error"]
        .as_str()
        .unwrap()
        .contains("interaction session gone"));
}

// ── 租户上下文解析（匿名回退 default） ─────────────────

#[tokio::test]
async fn request_tenant_ctx_falls_back_to_default_without_credentials() {
    let ctx = request_tenant_ctx(None, &HeaderMap::new(), "sess-1")
        .await
        .expect("无凭证是合法匿名态，不是解析失败");
    assert_eq!(ctx.tenant_id, "default", "无凭证回退默认租户");
    assert_eq!(ctx.session_id, "sess-1");
}
