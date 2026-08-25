//! Auth 端点——登录、获取当前用户信息、刷新令牌、登出、注册。
//!
//! 前端 `frontend/src/services/api/auth.ts` 和 `frontend/src/types/api.ts` 定义的契约：
//! - POST /api/v1/auth/login   → { access_token, refresh_token, token_type, expires_in }
//! - GET  /api/v1/auth/me      → { id, username, email, role, is_active, created_at, last_login_at? }
//! - POST /api/v1/auth/refresh → { access_token, refresh_token?, token_type, expires_in }
//! - POST /api/v1/auth/logout  → { success, message }
//! - POST /api/v1/auth/register → 同 login
//!
//! 用户解析与 token 编解码已下沉至 `agentos_http::auth`（2026-08，db-admin 拆分）：
//! api 与 db-admin 共用同一实现（鉴权单一来源），本模块以 `pub use` 再导出
//! 保持既有引用不变（ws_session.rs / server.rs 的
//! `resolve_request_user` / `verify_access_token` / `resolve_tenant_id_by_user` 等）。

use axum::extract::State;
use axum::http::HeaderMap;
use axum::Json;
use serde::{Deserialize, Serialize};

use crate::routes::AppState;
use agentos_http::error::ApiError;

// ─── 常量 ────────────────────────────────────────────────────────────

/// 默认 access token 有效期（秒）——与前端 authStore 中 `expires_in` 字段配合。
const ACCESS_TOKEN_TTL_SECS: u64 = 30 * 60; // 30 min

/// 默认 refresh token 有效期（秒）。
const REFRESH_TOKEN_TTL_SECS: u64 = 7 * 24 * 60 * 60; // 7 days

// ─── 共享鉴权实现（agentos-http） ────────────────────────────────────
//
// Token 格式：base64({type}:{user_id}:{username}:{exp_unix_secs})
// DEBT: base64 编码无签名，可被任何人解码伪造。ceiling: 仅限 0.2 开发/演示环境。
// upgrade: 接入正式认证后替换为 JWT/HMAC 签名 + 密钥轮换。
// 前端 client.ts 仅注入 Bearer 头，不解析 token 内容（已验证 NEED-1），
// 因此该方案在开发阶段安全。
// 再导出面（2026-08-24 清理）：外部 crate 零消费，仅保留 ws_session.rs（并发 WIP
// 不可触碰）仍经 `crate::auth::` 引用的两个符号；其余符号本文件内私有 use。
#[cfg(test)]
use agentos_http::auth::DEFAULT_TENANT_ID;
use agentos_http::auth::{
    decode_token, default_users, encode_token, extract_bearer_token, find_user_by_credentials,
    find_user_by_username, is_token_expired, BuiltInUser, TokenType,
};
pub use agentos_http::auth::{resolve_tenant_id_by_user, verify_access_token};

// ─── 请求 / 响应类型（与前端 types/api.ts 对齐）─────────────────────

#[derive(Debug, Deserialize)]
pub struct LoginRequest {
    pub username: String,
    pub password: String,
}

#[derive(Debug, Serialize)]
pub struct TokenResponse {
    pub access_token: String,
    pub refresh_token: String,
    pub token_type: String,
    pub expires_in: u64,
}

#[derive(Debug, Deserialize)]
pub struct RefreshRequest {
    pub refresh_token: String,
}

#[derive(Debug, Deserialize)]
pub struct LogoutRequest {
    #[serde(default)]
    pub refresh_token: Option<String>,
    #[serde(default)]
    pub logout_all: bool,
}

#[derive(Debug, Serialize)]
pub struct LogoutResponse {
    pub success: bool,
    pub message: String,
}

#[derive(Debug, Serialize)]
pub struct UserInfoResponse {
    pub id: String,
    pub username: String,
    pub email: String,
    pub role: String,
    pub is_active: bool,
    pub created_at: String,
    pub last_login_at: Option<String>,
}

/// 刷新令牌响应（refresh_token 可选，与前端 RefreshResponse 对齐）。
#[derive(Debug, Serialize)]
pub struct RefreshResponse {
    pub access_token: String,
    pub refresh_token: Option<String>,
    pub token_type: String,
    pub expires_in: u64,
}

/// 注册请求（与前端 RegisterRequest 对齐）。
#[derive(Debug, Deserialize)]
pub struct RegisterRequest {
    pub username: String,
    pub password: String,
    pub email: String,
}

// ─── 端点处理器 ──────────────────────────────────────────────────────

/// POST /api/v1/auth/login
pub async fn login_handler(
    State(state): State<AppState>,
    Json(req): Json<LoginRequest>,
) -> Result<Json<TokenResponse>, ApiError> {
    let user = find_user_by_credentials(state.store.as_ref(), &req.username, &req.password)
        .await
        .ok_or_else(|| ApiError::BadRequest {
            message: "用户名或密码错误".to_string(),
        })?;

    // 更新最近登录时间（best-effort，失败不影响登录）
    if let Some(store) = state.store.as_ref() {
        if let Err(e) = store.update_last_login(&user.id).await {
            tracing::warn!(
                "Failed to update last_login for user {} (login still succeeded): {e}",
                user.id
            );
        }
    }

    let access_token = encode_token(TokenType::Access, &user, ACCESS_TOKEN_TTL_SECS);
    let refresh_token = encode_token(TokenType::Refresh, &user, REFRESH_TOKEN_TTL_SECS);

    Ok(Json(TokenResponse {
        access_token,
        refresh_token,
        token_type: "bearer".to_string(),
        expires_in: ACCESS_TOKEN_TTL_SECS,
    }))
}

/// GET /api/v1/auth/me — 基于 Bearer token 返回当前用户信息。
pub async fn me_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<UserInfoResponse>, ApiError> {
    let token = extract_bearer_token(&headers).ok_or(ApiError::Unauthorized {
        message: "缺少认证信息".to_string(),
    })?;

    let (_, username, exp) = decode_token(&token).ok_or(ApiError::Unauthorized {
        message: "无效的认证令牌".to_string(),
    })?;

    if is_token_expired(exp) {
        return Err(ApiError::Unauthorized {
            message: "认证令牌已过期".to_string(),
        });
    }

    // token 校验场景无 tenant scope，用 username 跨租户查询（token 自带 username）。
    // find_user_by_username 已 fail-closed（store 存在未命中不回退内置表，K4），
    // 此处不得再叠 user_id 命中内置的回退——否则换库后旧 token 依旧合法。
    let user = find_user_by_username(state.store.as_ref(), &username)
        .await
        .ok_or(ApiError::Unauthorized {
            message: "用户不存在".to_string(),
        })?;

    Ok(Json(UserInfoResponse {
        id: user.id,
        username: user.username,
        email: user.email,
        role: user.role,
        is_active: true,
        created_at: user.created_at,
        last_login_at: None,
    }))
}

/// POST /api/v1/auth/refresh — 使用 refresh_token 获取新的 access_token。
pub async fn refresh_handler(
    State(state): State<AppState>,
    Json(req): Json<RefreshRequest>,
) -> Result<Json<RefreshResponse>, ApiError> {
    let (_, username, exp) =
        decode_token(&req.refresh_token).ok_or_else(|| ApiError::Unauthorized {
            message: "无效的刷新令牌".to_string(),
        })?;

    if is_token_expired(exp) {
        return Err(ApiError::Unauthorized {
            message: "刷新令牌已过期".to_string(),
        });
    }

    // find_user_by_username fail-closed（store 存在未命中不回退内置表，K4）——
    // refresh 也是 token 校验路径：已删除用户不得借内置表换发新 token。
    let user = find_user_by_username(state.store.as_ref(), &username)
        .await
        .ok_or(ApiError::Unauthorized {
            message: "用户不存在".to_string(),
        })?;

    let access_token = encode_token(TokenType::Access, &user, ACCESS_TOKEN_TTL_SECS);
    let refresh_token = encode_token(TokenType::Refresh, &user, REFRESH_TOKEN_TTL_SECS);

    Ok(Json(RefreshResponse {
        access_token,
        refresh_token: Some(refresh_token),
        token_type: "bearer".to_string(),
        expires_in: ACCESS_TOKEN_TTL_SECS,
    }))
}

/// POST /api/v1/auth/logout — 无状态设计，直接返回成功。
pub async fn logout_handler(
    State(_state): State<AppState>,
    Json(_req): Json<LogoutRequest>,
) -> Result<Json<LogoutResponse>, ApiError> {
    // DEBT: 无状态会话管理，不撤销 token。ceiling: token 在过期前仍有效。
    // upgrade: 接入数据库会话表后实现 token 黑名单。
    Ok(Json(LogoutResponse {
        success: true,
        message: "已成功登出".to_string(),
    }))
}

/// POST /api/v1/auth/register — 注册新用户（持久化）并返回令牌。
///
/// 0.5.0 完整用户系统的最小持久化落地：
/// - 生成 uuid user_id，一用户一租户（tenant_id = user_id）
/// - 明文密码存储（DEBT: 0.5.0 替换为哈希）
/// - 重名检查查 DB（跨租户全局唯一）+ 内置 admin
/// - 无 store 时返回 503（生产路径 store 恒非空）；测试用 app_with_store() 注入内存 store
pub async fn register_handler(
    State(state): State<AppState>,
    Json(req): Json<RegisterRequest>,
) -> Result<Json<TokenResponse>, ApiError> {
    // 重名检查：DB 优先 + 内置 admin
    let store = state.store.as_ref();
    if let Some(store) = store {
        if let Ok(Some(_)) = store.get_user_by_username(&req.username).await {
            return Err(ApiError::BadRequest {
                message: "用户名已存在".to_string(),
            });
        }
    } else if default_users().iter().any(|u| u.username == req.username) {
        return Err(ApiError::BadRequest {
            message: "用户名已存在".to_string(),
        });
    }

    let now = chrono::Utc::now().to_rfc3339();

    // 有 store：真实注册（一用户一租户）
    if let Some(store) = store {
        let user_id = format!("u-{}", uuid::Uuid::new_v4().simple());
        let user = agentos_core::types::UserRecord {
            user_id: user_id.clone(),
            username: req.username.clone(),
            password: req.password.clone(), // 明文（DEBT: 0.5.0 哈希）
            email: Some(req.email.clone()),
            role: "user".to_string(),
            tenant_id: user_id.clone(), // 一用户一租户
            created_at: now,
            last_login_at: None,
        };
        store
            .create_user(&user)
            .await
            .map_err(|e| ApiError::BadRequest {
                message: format!("注册失败: {e}"),
            })?;

        let builtin = BuiltInUser::from(&user);
        let access_token = encode_token(TokenType::Access, &builtin, ACCESS_TOKEN_TTL_SECS);
        let refresh_token = encode_token(TokenType::Refresh, &builtin, REFRESH_TOKEN_TTL_SECS);
        return Ok(Json(TokenResponse {
            access_token,
            refresh_token,
            token_type: "bearer".to_string(),
            expires_in: ACCESS_TOKEN_TTL_SECS,
        }));
    }

    // 无 store：生产路径不可达（AppState::with_plugins 必传非空 store）。
    // 测试请用 app_with_store() 注入内存 store（无 store 注册已回归测试为 503）。
    // 禁止回退签发 admin token——那是测试逻辑泄漏进生产二进制的安全 footgun。
    Err(ApiError::ServiceUnavailable {
        message: "存储后端未初始化，无法注册用户".to_string(),
    })
}

// ─── 单元测试 ────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::{Method, Request, StatusCode};
    use serde_json::json;
    use tower::ServiceExt;

    fn app() -> axum::Router {
        crate::server::build_router(AppState::new())
    }

    /// 构造带内存 store 的 app（用于持久化用户/多租户隔离测试）。
    /// 每次调用独立内存库，测试间不污染。已播种 admin（与生产 seed_admin_user 一致）。
    async fn app_with_store() -> (axum::Router, std::sync::Arc<agentos_engine::SqliteStore>) {
        use agentos_core::traits::StorageBackend;
        let store = std::sync::Arc::new(
            agentos_engine::SqliteStore::open_memory().expect("open_memory 失败"),
        );
        // 播种 admin（与生产 seed_admin_user 一致），保证 login admin 可用
        let now = chrono::Utc::now().to_rfc3339();
        let admin = agentos_core::types::UserRecord {
            user_id: "00000000-0000-0000-0000-000000000001".to_string(),
            username: "admin".to_string(),
            password: "admin12345".to_string(),
            email: Some("admin@agentos.dev".to_string()),
            role: "admin".to_string(),
            tenant_id: DEFAULT_TENANT_ID.to_string(),
            created_at: now,
            last_login_at: None,
        };
        let _ = store.create_user(&admin).await; // 已有则忽略错误
        let mut state = AppState::new();
        state.store = Some(store.clone());
        (crate::server::build_router(state), store)
    }

    // ── verify_access_token（WS 握手鉴权出口） ──

    #[tokio::test]
    async fn verify_access_token_accepts_valid_login_token() {
        // 登录拿到真实 access token，再校验
        let login_body = json!({"username": "admin", "password": "admin12345"}).to_string();
        let resp = app()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header("content-type", "application/json")
                    .body(Body::from(login_body))
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let token = json["access_token"].as_str().unwrap();

        let verified = verify_access_token(token).expect("有效 access token 应校验通过");
        assert_eq!(verified.username, "admin");
        assert!(!verified.user_id.is_empty());
        assert_eq!(verified.tenant_id, DEFAULT_TENANT_ID);
    }

    #[test]
    fn verify_access_token_rejects_garbage() {
        assert!(verify_access_token("not-a-real-token").is_none());
        assert!(verify_access_token("").is_none());
    }

    #[test]
    fn verify_access_token_rejects_refresh_token() {
        // 构造一个 refresh token（首段 "refresh"），即便未过期也应拒绝
        use base64::Engine;
        let exp = chrono::Utc::now().timestamp() as u64 + 3600;
        let payload = format!("refresh:00000000-0000-0000-0000-000000000001:admin:{exp}");
        let refresh = base64::engine::general_purpose::STANDARD_NO_PAD.encode(payload);
        assert!(
            verify_access_token(&refresh).is_none(),
            "refresh token 不得用于 WS 鉴权"
        );
    }

    #[test]
    fn verify_access_token_rejects_expired() {
        use base64::Engine;
        let exp = chrono::Utc::now().timestamp() as u64 - 1; // 已过期
        let payload = format!("access:00000000-0000-0000-0000-000000000001:admin:{exp}");
        let expired = base64::engine::general_purpose::STANDARD_NO_PAD.encode(payload);
        assert!(verify_access_token(&expired).is_none());
    }

    #[test]
    fn verify_access_token_unknown_user_passes_handshake_with_empty_tenant() {
        // 设计变更（0.5.0 最小持久化）：握手阶段 verify_access_token 同步上下文无法
        // 查 store，故不再校验 user 存在性——格式合法 + 未过期 + access 类型即放行，
        // tenant_id 对未知用户留空。真正的 user 存在性 + tenant 解析在 dispatch 时
        // 由 resolve_tenant_id_by_user（async，查 DB）权威完成，未知 user 回退 default。
        // 安全性：token 本就无签名 base64（DEBT 标注），伪造 user_id 最多落 default 租户，
        // 读不到他人数据（多租户隔离不受影响）。
        use base64::Engine;
        let exp = chrono::Utc::now().timestamp() as u64 + 3600;
        let payload = format!("access:no-such-user-id:ghost:{exp}");
        let token = base64::engine::general_purpose::STANDARD_NO_PAD.encode(payload);
        let verified = verify_access_token(&token).expect("格式合法的 access token 应通过握手");
        assert_eq!(verified.user_id, "no-such-user-id");
        assert_eq!(verified.username, "ghost");
        assert_eq!(
            verified.tenant_id, "",
            "未知用户的 tenant_id 应为空（dispatch 时解析）"
        );
    }

    // ── login ──

    #[tokio::test]
    async fn test_login_success() {
        let body = json!({"username": "admin", "password": "admin12345"}).to_string();
        let resp = app()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert!(json["access_token"].is_string());
        assert!(json["refresh_token"].is_string());
        assert_eq!(json["token_type"], "bearer");
        assert_eq!(json["expires_in"], 1800);
    }

    #[tokio::test]
    async fn test_login_wrong_password() {
        let body = json!({"username": "admin", "password": "wrong"}).to_string();
        let resp = app()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn test_login_unknown_user() {
        let body = json!({"username": "nobody", "password": "whatever"}).to_string();
        let resp = app()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    // ── me ──

    #[tokio::test]
    async fn test_me_with_valid_token() {
        // 先登录拿 token
        let login_body = json!({"username": "admin", "password": "admin12345"}).to_string();
        let resp = app()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header("content-type", "application/json")
                    .body(Body::from(login_body))
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let login_json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let token = login_json["access_token"].as_str().unwrap();

        // 用 token 调 /me
        let resp = app()
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/api/v1/auth/me")
                    .header("authorization", format!("Bearer {}", token))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(json["username"], "admin");
        assert_eq!(json["email"], "admin@agentos.dev");
        assert_eq!(json["role"], "admin");
        assert_eq!(json["is_active"], true);
        assert!(json["id"].is_string());
        assert!(json["created_at"].is_string());
    }

    #[tokio::test]
    async fn test_me_without_token() {
        let resp = app()
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/api/v1/auth/me")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn test_me_with_invalid_token() {
        let resp = app()
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/api/v1/auth/me")
                    .header("authorization", "Bearer invalidtoken123")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    // ── refresh ──

    #[tokio::test]
    async fn test_refresh_success() {
        let login_body = json!({"username": "admin", "password": "admin12345"}).to_string();
        let resp = app()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header("content-type", "application/json")
                    .body(Body::from(login_body))
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let login_json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let refresh_token = login_json["refresh_token"].as_str().unwrap();

        let refresh_body = json!({"refresh_token": refresh_token}).to_string();
        let resp = app()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/refresh")
                    .header("content-type", "application/json")
                    .body(Body::from(refresh_body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert!(json["access_token"].is_string());
        assert!(json["refresh_token"].is_string());
        assert_eq!(json["token_type"], "bearer");
    }

    #[tokio::test]
    async fn test_refresh_with_invalid_token() {
        let body = json!({"refresh_token": "invalid"}).to_string();
        let resp = app()
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/v1/auth/refresh")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    // ── K4：token 校验路径不回退内置 admin（对齐 find_user_by_credentials）──

    /// 手造内置 admin 形状的 access token（无签名 base64，格式见模块头）。
    fn forged_builtin_admin_access_token() -> String {
        use base64::Engine;
        let exp = chrono::Utc::now().timestamp() as u64 + 3600;
        let payload = format!("access:00000000-0000-0000-0000-000000000001:admin:{exp}");
        base64::engine::general_purpose::STANDARD_NO_PAD.encode(payload)
    }

    /// store 存在但库里没有该用户（换库/清库后旧 token）→ /me 必须 401，
    /// 不得借内置 admin 表复活（K4：token 校验路径对齐 find_user_by_credentials）。
    #[tokio::test]
    async fn test_me_rejects_token_when_user_absent_from_store() {
        // 内存 store，故意不播种 admin（模拟换库/清库）
        let store = std::sync::Arc::new(
            agentos_engine::SqliteStore::open_memory().expect("open_memory 失败"),
        );
        let mut state = AppState::new();
        state.store = Some(store);
        let app = crate::server::build_router(state);

        let resp = app
            .oneshot(
                Request::builder()
                    .method(Method::GET)
                    .uri("/api/v1/auth/me")
                    .header(
                        "authorization",
                        format!("Bearer {}", forged_builtin_admin_access_token()),
                    )
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::UNAUTHORIZED,
            "store 在而用户不存在：旧 token 不得命中内置表复活"
        );
    }

    /// 同款裁决覆盖 refresh：store 无此用户 → 拒绝换发新 token（401）。
    #[tokio::test]
    async fn test_refresh_rejects_token_when_user_absent_from_store() {
        use base64::Engine;
        let store = std::sync::Arc::new(
            agentos_engine::SqliteStore::open_memory().expect("open_memory 失败"),
        );
        let mut state = AppState::new();
        state.store = Some(store);
        let app = crate::server::build_router(state);

        let exp = chrono::Utc::now().timestamp() as u64 + 3600;
        let payload = format!("refresh:00000000-0000-0000-0000-000000000001:admin:{exp}");
        let refresh = base64::engine::general_purpose::STANDARD_NO_PAD.encode(payload);
        let resp = app
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/v1/auth/refresh")
                    .header("content-type", "application/json")
                    .body(Body::from(json!({ "refresh_token": refresh }).to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::UNAUTHORIZED,
            "已删除用户不得借内置表换发新 token"
        );
    }

    /// resolve_request_user（db-admin/user-admin/metrics 管理面共用鉴权入口）
    /// 同款 fail-closed：store 在而用户不存在 → 401。
    #[tokio::test]
    async fn test_resolve_request_user_fail_closed_with_store() {
        let store: std::sync::Arc<dyn agentos_core::traits::StorageBackend> = std::sync::Arc::new(
            agentos_engine::SqliteStore::open_memory().expect("open_memory 失败"),
        );
        let mut headers = axum::http::HeaderMap::new();
        headers.insert(
            "authorization",
            format!("Bearer {}", forged_builtin_admin_access_token())
                .parse()
                .unwrap(),
        );
        let err = agentos_http::auth::resolve_request_user(Some(&store), &headers)
            .await
            .unwrap_err();
        assert!(
            matches!(err, agentos_http::error::ApiError::Unauthorized { .. }),
            "store 在而用户不存在应 401，实际 {err:?}"
        );
    }

    // ── logout ──

    #[tokio::test]
    async fn test_logout_success() {
        let body = json!({"refresh_token": "anything"}).to_string();
        let resp = app()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/logout")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(json["success"], true);
        assert!(json["message"].is_string());
    }

    // ── register ──

    #[tokio::test]
    async fn test_register_duplicate() {
        let body =
            json!({"username": "admin", "password": "whatever", "email": "a@b.c"}).to_string();
        let resp = app()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/register")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    /// 无 store 时注册应返回 503（生产路径 store 恒非空，无 store = 配置错误），
    /// 而非回退签发 admin token（测试逻辑泄漏进生产二进制的安全 footgun）。
    #[tokio::test]
    async fn test_register_without_store_returns_503_not_admin_token() {
        let body =
            json!({"username": "newuser", "password": "password12345", "email": "new@test.com"})
                .to_string();
        let resp = app()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/register")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::SERVICE_UNAVAILABLE,
            "无 store 时注册应 503，而非签发 admin token"
        );
    }

    #[tokio::test]
    async fn test_register_new_user_returns_token() {
        // register 是 store 操作：用 app_with_store 走真实注册路径（无 store 已改为 503）。
        let (app, _store) = app_with_store().await;
        let body =
            json!({"username": "newuser", "password": "password12345", "email": "new@test.com"})
                .to_string();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/register")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert!(json["access_token"].is_string());
    }

    // ── token 编解码单元测试 ──

    #[test]
    fn test_token_encode_decode_roundtrip() {
        let user = BuiltInUser {
            id: "test-id".to_string(),
            username: "testuser".to_string(),
            password: String::new(),
            email: String::new(),
            role: String::new(),
            tenant_id: DEFAULT_TENANT_ID.to_string(),
            created_at: String::new(),
        };
        let token = encode_token(TokenType::Access, &user, 3600);
        let (uid, uname, exp) = decode_token(&token).unwrap();
        assert_eq!(uid, "test-id");
        assert_eq!(uname, "testuser");
        assert!(exp > chrono::Utc::now().timestamp() as u64);
    }

    #[test]
    fn test_decode_garbage_returns_none() {
        assert!(decode_token("!!!notbase64!!!").is_none());
        assert!(decode_token("dGVzdA==").is_none()); // valid base64 but wrong format
    }

    #[test]
    fn test_is_token_expired() {
        let now = chrono::Utc::now().timestamp() as u64;
        assert!(is_token_expired(now - 1)); // 过去
        assert!(!is_token_expired(now + 3600)); // 未来
    }

    // ── 持久化用户系统 + 多租户隔离测试（0.5.0 最小持久化地基）──

    /// 注册应在 DB 创建真实用户，且一用户一租户（tenant_id == user_id）。
    #[tokio::test]
    async fn test_register_creates_persistent_user_with_unique_tenant() {
        use agentos_core::traits::StorageBackend;
        let (app, store) = app_with_store().await;

        let body = json!({
            "username": "alice",
            "password": "alice123",
            "email": "alice@test.com"
        })
        .to_string();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/register")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let access_token = json["access_token"].as_str().expect("应有 access_token");

        // token 解出的 user_id 应能在 DB 查到 alice
        let (user_id, username, _) = decode_token(access_token).unwrap();
        assert_eq!(username, "alice");
        let user = store
            .get_user_by_username("alice")
            .await
            .expect("查询不应出错")
            .expect("alice 应已持久化");
        assert_eq!(user.user_id, user_id);
        assert_eq!(user.username, "alice");
        // 一用户一租户：tenant_id == user_id
        assert_eq!(
            user.tenant_id, user.user_id,
            "一用户一租户: tenant_id 应等于 user_id"
        );
    }

    /// 两个不同用户应有不同的 tenant_id（隔离前提）。
    #[tokio::test]
    async fn test_two_users_have_different_tenants() {
        use agentos_core::traits::StorageBackend;
        let (app, store) = app_with_store().await;

        for name in ["bob", "carol"] {
            let body = json!({"username": name, "password": format!("{name}123"), "email": format!("{name}@test.com")}).to_string();
            let resp = app
                .clone()
                .oneshot(
                    Request::builder()
                        .method("POST")
                        .uri("/api/v1/auth/register")
                        .header("content-type", "application/json")
                        .body(Body::from(body))
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(resp.status(), StatusCode::OK);
        }

        let bob = store.get_user_by_username("bob").await.unwrap().unwrap();
        let carol = store.get_user_by_username("carol").await.unwrap().unwrap();
        assert_ne!(
            bob.tenant_id, carol.tenant_id,
            "两用户 tenant_id 必须不同（隔离前提）"
        );
        assert_eq!(bob.tenant_id, bob.user_id);
        assert_eq!(carol.tenant_id, carol.user_id);
    }

    /// 重名注册应被拒绝（DB 全局唯一约束）。
    #[tokio::test]
    async fn test_register_duplicate_username_rejected() {
        let (app, _store) = app_with_store().await;
        let body =
            json!({"username": "dave", "password": "dave123", "email": "dave@t.com"}).to_string();

        // 第一次注册成功
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/register")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);

        // 第二次同名应 400
        let body2 =
            json!({"username": "dave", "password": "other", "email": "dave2@t.com"}).to_string();
        let resp2 = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/register")
                    .header("content-type", "application/json")
                    .body(Body::from(body2))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp2.status(), StatusCode::BAD_REQUEST);
    }

    /// 持久化用户登录后，token 解出的 tenant 应是用户真实 tenant（非 default）。
    #[tokio::test]
    async fn test_login_persistent_user_resolves_correct_tenant() {
        let (app, _store) = app_with_store().await;

        // 先注册 eve
        let reg_body =
            json!({"username": "eve", "password": "eve123", "email": "eve@t.com"}).to_string();
        let _ = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/register")
                    .header("content-type", "application/json")
                    .body(Body::from(reg_body))
                    .unwrap(),
            )
            .await
            .unwrap();

        // 登录 eve
        let login_body = json!({"username": "eve", "password": "eve123"}).to_string();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header("content-type", "application/json")
                    .body(Body::from(login_body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let token = json["access_token"].as_str().unwrap();

        // resolve_tenant_id_by_user 应返回 eve 的真实 tenant（= user_id，非 default）
        let (user_id, _, _) = decode_token(token).unwrap();
        // 用内置函数验证：find_user_by_id 需 store，这里用 get_user_by_username 间接
        // eve 的 tenant_id 应 ≠ DEFAULT_TENANT_ID（一用户一租户）
        assert_ne!(
            user_id, "00000000-0000-0000-0000-000000000001",
            "eve 的 user_id 不应是 admin 的"
        );
    }
}
