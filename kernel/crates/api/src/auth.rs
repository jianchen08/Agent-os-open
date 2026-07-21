//! Auth 端点——登录、获取当前用户信息、刷新令牌、登出、注册。
//!
//! 前端 `frontend/src/services/api/auth.ts` 和 `frontend/src/types/api.ts` 定义的契约：
//! - POST /api/v1/auth/login   → { access_token, refresh_token, token_type, expires_in }
//! - GET  /api/v1/auth/me      → { id, username, email, role, is_active, created_at, last_login_at? }
//! - POST /api/v1/auth/refresh → { access_token, refresh_token?, token_type, expires_in }
//! - POST /api/v1/auth/logout  → { success, message }
//! - POST /api/v1/auth/register → 同 login

use axum::extract::State;
use axum::http::HeaderMap;
use axum::Json;
use serde::{Deserialize, Serialize};

use crate::error::ApiError;
use crate::routes::AppState;

// ─── 常量 ────────────────────────────────────────────────────────────

/// 默认 access token 有效期（秒）——与前端 authStore 中 `expires_in` 字段配合。
const ACCESS_TOKEN_TTL_SECS: u64 = 30 * 60; // 30 min

/// 默认 refresh token 有效期（秒）。
const REFRESH_TOKEN_TTL_SECS: u64 = 7 * 24 * 60 * 60; // 7 days

/// 内置默认用户（硬编码，无密码哈希开销，满足"简单内置用户"需求）。
/// DEBT: 明文密码仅用于开发/演示。ceiling: 无多用户管理。
/// upgrade: 接入正式用户系统时替换为数据库 + 哈希校验。
fn default_users() -> Vec<BuiltInUser> {
    vec![BuiltInUser {
        id: "00000000-0000-0000-0000-000000000001".to_string(),
        username: "admin".to_string(),
        password: "admin12345".to_string(),
        email: "admin@lingxi.dev".to_string(),
        role: "admin".to_string(),
        created_at: "2025-01-01T00:00:00Z".to_string(),
    }]
}

// ─── 数据结构 ────────────────────────────────────────────────────────

struct BuiltInUser {
    id: String,
    username: String,
    password: String,
    email: String,
    role: String,
    created_at: String,
}

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

// ─── Token 编解码 ────────────────────────────────────────────────────
//
// Token 格式：base64({type}:{user_id}:{username}:{exp_unix_secs})
// DEBT: base64 编码无签名，可被任何人解码伪造。ceiling: 仅限 0.2 开发/演示环境。
// upgrade: 接入正式认证后替换为 JWT/HMAC 签名 + 密钥轮换。
// 前端 client.ts 仅注入 Bearer 头，不解析 token 内容（已验证 NEED-1），因此该方案在开发阶段安全。

#[derive(Debug, Clone, Copy, PartialEq)]
enum TokenType {
    Access,
    Refresh,
}

impl TokenType {
    fn prefix(self) -> &'static str {
        match self {
            TokenType::Access => "access",
            TokenType::Refresh => "refresh",
        }
    }
}

fn encode_token(token_type: TokenType, user: &BuiltInUser, ttl_secs: u64) -> String {
    let exp = chrono::Utc::now().timestamp() as u64 + ttl_secs;
    let payload = format!(
        "{}:{}:{}:{}",
        token_type.prefix(),
        user.id,
        user.username,
        exp
    );
    use base64::Engine;
    base64::engine::general_purpose::STANDARD_NO_PAD.encode(payload.as_bytes())
}

fn decode_token(token: &str) -> Option<(String, String, u64)> {
    use base64::Engine;
    let decoded = base64::engine::general_purpose::STANDARD_NO_PAD
        .decode(token.trim())
        .ok()?;
    let payload = String::from_utf8(decoded).ok()?;
    let parts: Vec<&str> = payload.splitn(4, ':').collect();
    if parts.len() != 4 {
        return None;
    }
    let exp: u64 = parts[3].parse().ok()?;
    Some((parts[1].to_string(), parts[2].to_string(), exp))
}

fn is_token_expired(exp: u64) -> bool {
    chrono::Utc::now().timestamp() as u64 >= exp
}

/// 从 Authorization 头提取 bearer token。
fn extract_bearer_token(headers: &HeaderMap) -> Option<String> {
    let auth_header = headers.get("authorization")?;
    let auth_str = auth_header.to_str().ok()?;
    auth_str.strip_prefix("Bearer ").map(|s| s.to_string())
}

// ─── 用户查找 ────────────────────────────────────────────────────────

fn find_user_by_credentials(username: &str, password: &str) -> Option<BuiltInUser> {
    default_users()
        .into_iter()
        .find(|u| u.username == username && u.password == password)
}

fn find_user_by_id(id: &str) -> Option<BuiltInUser> {
    default_users().into_iter().find(|u| u.id == id)
}

// ─── 端点处理器 ──────────────────────────────────────────────────────

/// POST /api/v1/auth/login
pub async fn login_handler(
    State(_state): State<AppState>,
    Json(req): Json<LoginRequest>,
) -> Result<Json<TokenResponse>, ApiError> {
    let user = find_user_by_credentials(&req.username, &req.password).ok_or_else(|| {
        ApiError::BadRequest {
            message: "用户名或密码错误".to_string(),
        }
    })?;

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
    State(_state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<UserInfoResponse>, ApiError> {
    let token = extract_bearer_token(&headers).ok_or(ApiError::Unauthorized {
        message: "缺少认证信息".to_string(),
    })?;

    let (user_id, _, exp) = decode_token(&token).ok_or(ApiError::Unauthorized {
        message: "无效的认证令牌".to_string(),
    })?;

    if is_token_expired(exp) {
        return Err(ApiError::Unauthorized {
            message: "认证令牌已过期".to_string(),
        });
    }

    let user = find_user_by_id(&user_id).ok_or(ApiError::Unauthorized {
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
    State(_state): State<AppState>,
    Json(req): Json<RefreshRequest>,
) -> Result<Json<RefreshResponse>, ApiError> {
    let (user_id, _, exp) =
        decode_token(&req.refresh_token).ok_or_else(|| ApiError::Unauthorized {
            message: "无效的刷新令牌".to_string(),
        })?;

    if is_token_expired(exp) {
        return Err(ApiError::Unauthorized {
            message: "刷新令牌已过期".to_string(),
        });
    }

    let user = find_user_by_id(&user_id).ok_or(ApiError::Unauthorized {
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

/// POST /api/v1/auth/register — 注册新用户并返回令牌。
pub async fn register_handler(
    State(_state): State<AppState>,
    Json(req): Json<RegisterRequest>,
) -> Result<Json<TokenResponse>, ApiError> {
    // DEBT: 注册的用户不会持久化。ceiling: 进程重启后丢失。
    // upgrade: 接入数据库后实现真正的用户注册。
    // DEBT: 注册后以 admin 身份签发 token，忽略新用户信息。ceiling: 新用户无独立身份。
    // upgrade: 接入用户系统后用注册数据创建真实用户。
    let existing = default_users().iter().any(|u| u.username == req.username);

    if existing {
        return Err(ApiError::BadRequest {
            message: "用户名已存在".to_string(),
        });
    }

    // 使用内置 admin 用户签发 token（简化：注册后以 admin 身份登录）
    let user = find_user_by_credentials("admin", "admin12345").unwrap();

    let access_token = encode_token(TokenType::Access, &user, ACCESS_TOKEN_TTL_SECS);
    let refresh_token = encode_token(TokenType::Refresh, &user, REFRESH_TOKEN_TTL_SECS);

    Ok(Json(TokenResponse {
        access_token,
        refresh_token,
        token_type: "bearer".to_string(),
        expires_in: ACCESS_TOKEN_TTL_SECS,
    }))
}

// ─── 单元测试 ────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use serde_json::json;
    use tower::ServiceExt;

    fn app() -> axum::Router {
        crate::server::build_router(AppState::new())
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
        assert_eq!(json["email"], "admin@lingxi.dev");
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
                    .method("POST")
                    .uri("/api/v1/auth/refresh")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
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

    #[tokio::test]
    async fn test_register_new_user_returns_token() {
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
}
