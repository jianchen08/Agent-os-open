//! Auth 端点——登录、获取当前用户信息、刷新令牌、登出、注册、改密。
//!
//! 前端 `frontend/src/services/api/auth.ts` 和 `frontend/src/types/api.ts` 定义的契约：
//! - POST /api/v1/auth/login   → { access_token, refresh_token, token_type, expires_in, must_change_password }
//! - GET  /api/v1/auth/me      → { id, username, email, role, is_active, created_at, last_login_at? }
//! - POST /api/v1/auth/refresh → { access_token, refresh_token, token_type, expires_in }
//! - POST /api/v1/auth/logout  → { success, message }
//! - POST /api/v1/auth/register → 同 login
//! - POST /api/v1/auth/change-password → 同 login（验旧口令→写新哈希→
//!   旧口令哈希下签发的全部旧 token 经口令绑定段失配而吊销；响携带新 token 对）
//!
//! token 为 HMAC 签名格式（见 agentos_http::auth 模块文档）：refresh 单次轮换
//! （每次刷新作废旧 jti），改密经口令绑定段（pwv）持久吊销全会话。
//!
//! 用户解析与 token 编解码已下沉至 `agentos_http::auth`（2026-08，db-admin 拆分）：
//! api 与 db-admin 共用同一实现（鉴权单一来源），消费方（ws_session.rs 等）
//! 直接从 `agentos_http::auth` 引入，本模块不再转发。

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

/// 新口令最小长度（与前端 validatePassword 的 8 字符下限对齐）。
const MIN_PASSWORD_LEN: usize = 8;

/// 登录失败计数滑动窗口宽度（秒）。
const LOGIN_FAILURE_WINDOW_SECS: u64 = 15 * 60;
/// 同一用户名窗口内允许的失败次数上限；≥上限后登录一律 429 直至窗口滑出。
const LOGIN_MAX_FAILURES: usize = 10;
/// 429 响应的 Retry-After 提示（秒）：按窗口宽度给保守值。
const RATE_LIMIT_RETRY_AFTER_SECS: u64 = LOGIN_FAILURE_WINDOW_SECS;
/// 注册限频滑动窗口宽度（秒）。
const REGISTER_WINDOW_SECS: u64 = 15 * 60;
/// 窗口内注册尝试次数上限（按用户名爆破注册探测防护取同一量级）。
const REGISTER_MAX_ATTEMPTS: usize = 20;
/// 登录限流表条目数上限（usernames 去重计数）：表免认证可达（POST /auth/login），
/// 随机用户名刷失败即无限增长内存——cleanup 顺带 retain + 新增时超限逐出最旧。
const LOGIN_FAILURES_MAX_ENTRIES: usize = 10_000;

// ─── 进程内限流（滑动窗口） ──────────────────────────────────────────
//
// 约束：单机单进程内核部署形态（config/kernel/storage.yaml 默认 SQLite 同构），
// 进程内计数即全量状态，重启清零。多实例/反代横向扩容部署需外置限流
// （网关或共享存储），本实现不覆盖——升级触发条件=部署形态变更。

/// 清理窗口外失败并判定：窗口内失败次数 ≥ 上限 → 429 + Retry-After。
/// 表级设界：顺带清空窗口全滑出的条目；条目数超上限（未认证 DoS 面：
/// 随机用户名刷失败）时逐出窗口起点最早的一条——限流对最旧用户名提前
/// 失忆，属可用性微损，换取内存有界。
fn login_rate_check(state: &AppState, username: &str) -> Result<(), ApiError> {
    let now = std::time::Instant::now();
    let cutoff = now - std::time::Duration::from_secs(LOGIN_FAILURE_WINDOW_SECS);
    let mut failures = state.login_failures.lock();
    failures.retain(|_, queue| {
        queue.retain(|t| *t > cutoff);
        !queue.is_empty()
    });
    while failures.len() >= LOGIN_FAILURES_MAX_ENTRIES && !failures.contains_key(username) {
        // 逐出窗口起点最早的一条（首个失败时间最小）
        let oldest = failures
            .iter()
            .min_by_key(|(_, q)| q.first().copied().unwrap_or(now))
            .map(|(k, _)| k.clone());
        match oldest {
            Some(k) => {
                failures.remove(&k);
            }
            None => break,
        }
    }
    let queue = failures.entry(username.to_string()).or_default();
    if queue.len() >= LOGIN_MAX_FAILURES {
        return Err(ApiError::TooManyRequests {
            message: format!("登录失败次数过多，请 {RATE_LIMIT_RETRY_AFTER_SECS} 秒后重试"),
            retry_after_secs: RATE_LIMIT_RETRY_AFTER_SECS,
        });
    }
    Ok(())
}

/// 记录一次登录失败（凭据校验未通过时调用）。
fn record_login_failure(state: &AppState, username: &str) {
    state
        .login_failures
        .lock()
        .entry(username.to_string())
        .or_default()
        .push(std::time::Instant::now());
}

/// 登录成功清零该用户名的失败窗口（成功凭据重置计数，防历史失败误伤）。
fn clear_login_failures(state: &AppState, username: &str) {
    state.login_failures.lock().remove(username);
}

/// 注册限频判定 + 占位：窗口内尝试 ≥ 上限 → 429 + Retry-After；否则记录本次。
fn register_rate_check(state: &AppState) -> Result<(), ApiError> {
    let now = std::time::Instant::now();
    let cutoff = now - std::time::Duration::from_secs(REGISTER_WINDOW_SECS);
    let mut attempts = state.register_attempts.lock();
    attempts.retain(|t| *t > cutoff);
    if attempts.len() >= REGISTER_MAX_ATTEMPTS {
        return Err(ApiError::TooManyRequests {
            message: format!("注册请求过于频繁，请 {} 秒后重试", REGISTER_WINDOW_SECS),
            retry_after_secs: REGISTER_WINDOW_SECS,
        });
    }
    attempts.push(now);
    Ok(())
}

// ─── 共享鉴权实现（agentos-http） ────────────────────────────────────
//
// token 编解码/口令哈希/验签单一实现下沉 agentos_http::auth（api 与 db-admin
// 共用）；引用面：本模块只登录/登出/改密端点所需符号；ws_session 经
// `agentos_http::auth` 直接引用 verify_access_token（WS 握手鉴权）与
// resolve_tenant_id_by_user（user→tenant 解析）。
#[cfg(test)]
use agentos_http::auth::DEFAULT_TENANT_ID;
use agentos_http::auth::{
    decode_token, default_users, encode_token, extract_bearer_token, find_user_by_credentials,
    find_user_by_username, is_token_expired, is_valid_username, BuiltInUser, TokenType,
};

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
    /// 首登强制改密标记（D1）：播种账号为 true，前端拦截强制改密后放行。
    pub must_change_password: bool,
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
    /// 首登改密标记（D1-4）：me 是会话恢复后前端刷新用户态的统一出口，
    /// 页面刷新经 refresh 恢复后据此继续拦截强制改密。
    pub must_change_password: bool,
}

/// 刷新令牌响应（refresh_token 可选，与前端 RefreshResponse 对齐）。
#[derive(Debug, Serialize)]
pub struct RefreshResponse {
    pub access_token: String,
    pub refresh_token: Option<String>,
    pub token_type: String,
    pub expires_in: u64,
    /// 首登改密标记随轮换透出：页面刷新经 refresh 恢复会话时，
    /// 前端据此继续拦截强制改密（D1-4）。
    pub must_change_password: bool,
}

/// 注册请求（与前端 RegisterRequest 对齐）。
#[derive(Debug, Deserialize)]
pub struct RegisterRequest {
    pub username: String,
    pub password: String,
    pub email: String,
}

/// 改密请求（与前端 ChangePasswordRequest 对齐）。
#[derive(Debug, Deserialize)]
pub struct ChangePasswordRequest {
    pub old_password: String,
    pub new_password: String,
}

/// 签发 access+refresh token 对（login / register / change-password 共用出口）。
fn issue_token_pair(user: &BuiltInUser, must_change_password: bool) -> TokenResponse {
    TokenResponse {
        access_token: encode_token(TokenType::Access, user, ACCESS_TOKEN_TTL_SECS),
        refresh_token: encode_token(TokenType::Refresh, user, REFRESH_TOKEN_TTL_SECS),
        token_type: "bearer".to_string(),
        expires_in: ACCESS_TOKEN_TTL_SECS,
        must_change_password,
    }
}

// ─── 端点处理器 ──────────────────────────────────────────────────────

/// POST /api/v1/auth/login
///
/// 限流：按用户名进程内滑动窗口（15 分钟 ≥10 次失败 → 429 + Retry-After，
/// 成功登录清零）。线上爆破防护第一道闸，计数约束见限流节注释。
pub async fn login_handler(
    State(state): State<AppState>,
    Json(req): Json<LoginRequest>,
) -> Result<Json<TokenResponse>, ApiError> {
    login_rate_check(&state, &req.username)?;

    let user =
        match find_user_by_credentials(state.store.as_ref(), &req.username, &req.password).await {
            Some(user) => user,
            None => {
                record_login_failure(&state, &req.username);
                return Err(ApiError::BadRequest {
                    message: "用户名或密码错误".to_string(),
                });
            }
        };
    clear_login_failures(&state, &req.username);

    // 更新最近登录时间（best-effort，失败不影响登录）
    if let Some(store) = state.store.as_ref() {
        if let Err(e) = store.update_last_login(&user.id).await {
            tracing::warn!(
                "Failed to update last_login for user {} (login still succeeded): {e}",
                user.id
            );
        }
    }

    // 凭据校验已要求 store 在场（无 store 恒不可登录），标记随记录带出
    Ok(Json(issue_token_pair(&user, user.must_change_password)))
}

/// GET /api/v1/auth/me — 基于 Bearer token 返回当前用户信息。
pub async fn me_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<UserInfoResponse>, ApiError> {
    // 认证链共享原语（提取/解码/过期/用户存在 K4/口令绑定）：本端点不限定
    // access 类型（refresh token 亦可观档），管理面版本带 is_access 检查。
    let (_t, user) =
        agentos_http::auth::authenticate_bearer_user(state.store.as_ref(), &headers, false).await?;

    Ok(Json(UserInfoResponse {
        id: user.id,
        username: user.username,
        email: user.email,
        role: user.role,
        is_active: true,
        created_at: user.created_at,
        must_change_password: user.must_change_password,
        last_login_at: None,
    }))
}

/// POST /api/v1/auth/refresh — 使用 refresh_token 换取新 token 对。
///
/// D12 单次轮换：每个 refresh token 的 jti 只能消费一次，轮换后旧值即废
/// （复用 → 401）。改密吊销经口令绑定段判定——换发前校验 pwv 与当前口令
/// 哈希匹配，改密后的旧 refresh token 一律 401。
pub async fn refresh_handler(
    State(state): State<AppState>,
    Json(req): Json<RefreshRequest>,
) -> Result<Json<RefreshResponse>, ApiError> {
    let t = decode_token(&req.refresh_token).ok_or_else(|| ApiError::Unauthorized {
        message: "无效的刷新令牌".to_string(),
    })?;

    // 类型闸（F2，2026-09-16 审查）：仅 refresh 前缀可换发。access token
    // （TTL 30min）提交本端点不得换取 refresh（TTL 7 天）——否则短 TTL
    // 凭证泄露被拉平为长效会话，破坏 access/refresh 分层 + 单次轮换契约。
    if !t.is_refresh() {
        return Err(ApiError::Unauthorized {
            message: "无效的刷新令牌".to_string(),
        });
    }

    if is_token_expired(t.exp) {
        return Err(ApiError::Unauthorized {
            message: "刷新令牌已过期".to_string(),
        });
    }
    // 单次轮换：已被消费的旧值直接拒绝（先于用户查询，防旧值复用试探）。
    if !state.consume_refresh_jti(&t.jti) {
        return Err(ApiError::Unauthorized {
            message: "刷新令牌已被使用".to_string(),
        });
    }

    // find_user_by_username fail-closed（store 存在未命中不回退内置表，K4）——
    // refresh 也是 token 校验路径：已删除用户不得借内置表换发新 token。
    let user = find_user_by_username(state.store.as_ref(), &t.username)
        .await
        .ok_or(ApiError::Unauthorized {
            message: "用户不存在".to_string(),
        })?;
    if !t.password_binding_matches(&user.password) {
        return Err(ApiError::Unauthorized {
            message: "刷新令牌已失效（口令已变更）".to_string(),
        });
    }

    Ok(Json(RefreshResponse {
        access_token: encode_token(TokenType::Access, &user, ACCESS_TOKEN_TTL_SECS),
        refresh_token: Some(encode_token(
            TokenType::Refresh,
            &user,
            REFRESH_TOKEN_TTL_SECS,
        )),
        token_type: "bearer".to_string(),
        expires_in: ACCESS_TOKEN_TTL_SECS,
        must_change_password: user.must_change_password,
    }))
}

/// POST /api/v1/auth/logout — 消费请求携带的 refresh token（服务端吊销），
/// access token 靠短 TTL（30 min）自然过期。
pub async fn logout_handler(
    State(state): State<AppState>,
    Json(req): Json<LogoutRequest>,
) -> Result<Json<LogoutResponse>, ApiError> {
    if let Some(refresh_token) = req.refresh_token.as_deref() {
        if let Some(t) = decode_token(refresh_token) {
            state.consume_refresh_jti(&t.jti);
        }
    }
    Ok(Json(LogoutResponse {
        success: true,
        message: "已成功登出".to_string(),
    }))
}

/// POST /api/v1/auth/register — 注册新用户（持久化）并返回令牌。
///
/// - 生成 uuid user_id，一用户一租户（tenant_id = user_id）
/// - 口令 argon2id 哈希落库（D1）
/// - 重名检查查 DB（跨租户全局唯一）+ 内置 admin
/// - 无 store 时返回 503（生产路径 store 恒非空）；测试用 app_with_store() 注入内存 store
pub async fn register_handler(
    State(state): State<AppState>,
    Json(req): Json<RegisterRequest>,
) -> Result<Json<TokenResponse>, ApiError> {
    // 注册限频（进程内滑动窗口，约束见限流节注释）：防批量探测/灌库。
    register_rate_check(&state)?;

    // username 字符集校验（用户创建唯一入口，域定义见 agentos_http::auth）：
    // username 原样进 token 载荷（`:` 分段），越界字符令载荷错位 → 注册即自锁。
    if !is_valid_username(&req.username) {
        return Err(ApiError::BadRequest {
            message: "用户名仅允许字母、数字、下划线、点、连字符和 @，长度 1-64".to_string(),
        });
    }

    // 重名检查：DB 优先 + 内置 admin。存储查询失败 ≠ 用户名不存在：
    // 吞掉 Err 会把基础设施故障伪装成"可注册"，落库阶段再以误导性错误失败
    // （错误翻译纪律：内部故障 500，客户端错误 4xx，不得互串）。
    let store = state.store.as_ref();
    if let Some(store) = store {
        match store.get_user_by_username(&req.username).await {
            Ok(Some(_)) => {
                return Err(ApiError::BadRequest {
                    message: "用户名已存在".to_string(),
                });
            }
            Ok(None) => {}
            Err(e) => {
                return Err(ApiError::Internal {
                    message: format!("注册失败：重名检查存储查询异常: {e}"),
                });
            }
        }
    } else if default_users().iter().any(|u| u.username == req.username) {
        return Err(ApiError::BadRequest {
            message: "用户名已存在".to_string(),
        });
    }

    let now = chrono::Utc::now().to_rfc3339();

    // 有 store：真实注册（一用户一租户）
    if let Some(store) = store {
        // argon2 哈希属服务端基础设施能力，失败是内部故障（500），非客户端 400
        let password_hash =
            agentos_http::auth::hash_password(&req.password).map_err(|e| ApiError::Internal {
                message: format!("注册失败：口令哈希失败: {e}"),
            })?;
        let user_id = format!("u-{}", uuid::Uuid::new_v4().simple());
        let user = agentos_core::types::UserRecord {
            user_id: user_id.clone(),
            username: req.username.clone(),
            password: password_hash,
            email: Some(req.email.clone()),
            role: "user".to_string(),
            tenant_id: user_id.clone(), // 一用户一租户
            created_at: now,
            last_login_at: None,
            must_change_password: false, // 注册者自设口令，无首登改密诉求
        };
        store
            .create_user(&user)
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("注册失败：用户落库异常: {e}"),
            })?;

        let builtin = BuiltInUser::from(&user);
        return Ok(Json(issue_token_pair(&builtin, user.must_change_password)));
    }

    // 无 store：生产路径不可达（AppState::with_plugins 必传非空 store）。
    // 测试请用 app_with_store() 注入内存 store（无 store 注册已回归测试为 503）。
    // 禁止回退签发 admin token——那是测试逻辑泄漏进生产二进制的安全 footgun。
    Err(ApiError::ServiceUnavailable {
        message: "存储后端未初始化，无法注册用户".to_string(),
    })
}

/// POST /api/v1/auth/change-password — 验旧口令 → 写新哈希 → 吊销其他会话。
///
/// D1-3 改密通道。其他会话的吊销持久且全自动：token 载荷携带口令绑定段
/// （pwv，见 agentos_http::auth），写新哈希后所有旧 token（含其他会话的
/// access/refresh）pwv 失配即 401。响应携带新 token 对——当前会话无感续期，
/// 其余会话被踢回登录。
pub async fn change_password_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<ChangePasswordRequest>,
) -> Result<Json<TokenResponse>, ApiError> {
    let token = extract_bearer_token(&headers).ok_or(ApiError::Unauthorized {
        message: "缺少认证信息".to_string(),
    })?;
    let t = decode_token(&token).ok_or(ApiError::Unauthorized {
        message: "无效的认证令牌".to_string(),
    })?;
    if is_token_expired(t.exp) {
        return Err(ApiError::Unauthorized {
            message: "认证令牌已过期".to_string(),
        });
    }
    if !t.is_access() {
        return Err(ApiError::Unauthorized {
            message: "无效的认证令牌".to_string(),
        });
    }
    let store = state.store.as_ref().ok_or(ApiError::ServiceUnavailable {
        message: "存储后端未初始化，无法修改口令".to_string(),
    })?;
    let record = store
        .get_user_by_username(&t.username)
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("查询用户失败: {e}"),
        })?
        .ok_or(ApiError::Unauthorized {
            message: "用户不存在".to_string(),
        })?;
    // 口令绑定失配 = token 签发后口令已改过（其他会话已改密），拒绝旧 token 再改
    if !t.password_binding_matches(&record.password) {
        return Err(ApiError::Unauthorized {
            message: "认证令牌已失效（口令已变更）".to_string(),
        });
    }
    // 验旧：旧口令不符 → 400（不泄露用户存在性以外的信息）
    if !agentos_http::auth::verify_password(&req.old_password, &record.password) {
        return Err(ApiError::BadRequest {
            message: "旧口令错误".to_string(),
        });
    }
    // 新口令下限与前端 validatePassword 对齐（≥8 字符）
    if req.new_password.len() < MIN_PASSWORD_LEN {
        return Err(ApiError::BadRequest {
            message: format!("新口令长度至少 {MIN_PASSWORD_LEN} 个字符"),
        });
    }
    let new_hash = agentos_http::auth::hash_password(&req.new_password)
        .map_err(|e| ApiError::Internal { message: e })?;
    let updated = store
        .update_user_password(&record.user_id, &new_hash, false)
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("写入新口令失败: {e}"),
        })?;
    if !updated {
        return Err(ApiError::NotFound {
            message: "用户不存在".to_string(),
        });
    }
    let mut user = BuiltInUser::from(&record);
    user.password = new_hash;
    Ok(Json(issue_token_pair(&user, false)))
}

// ─── 单元测试 ────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_http::auth::{hash_password, verify_access_token, verify_password};
    use axum::body::Body;
    use axum::http::{Method, Request, StatusCode};
    use serde_json::json;
    use tower::ServiceExt;

    /// 测试专用 admin 口令（经 argon2 哈希播种；不再存在硬编码默认口令）。
    const TEST_ADMIN_PW: &str = "test-admin-pw-2026";

    fn app() -> axum::Router {
        crate::server::build_router(AppState::new())
    }

    /// 构造带内存 store 的 app（用于持久化用户/多租户隔离测试）。
    /// 每次调用独立内存库，测试间不污染。已播种哈希口令的 admin（与生产
    /// seed_admin_user 同构：argon2 哈希 + must_change_password 可控）。
    async fn app_with_store() -> (axum::Router, std::sync::Arc<agentos_engine::SqliteStore>) {
        app_with_seeded_admin(true).await
    }

    /// 同上，但可控制播种账号的首登改密标记。
    async fn app_with_seeded_admin(
        must_change: bool,
    ) -> (axum::Router, std::sync::Arc<agentos_engine::SqliteStore>) {
        use agentos_core::traits::StorageBackend;
        let store = std::sync::Arc::new(
            agentos_engine::SqliteStore::open_memory().expect("open_memory 失败"),
        );
        let now = chrono::Utc::now().to_rfc3339();
        let admin = agentos_core::types::UserRecord {
            user_id: "00000000-0000-0000-0000-000000000001".to_string(),
            username: "admin".to_string(),
            password: hash_password(TEST_ADMIN_PW).unwrap(),
            email: Some("admin@agentos.dev".to_string()),
            role: "admin".to_string(),
            tenant_id: DEFAULT_TENANT_ID.to_string(),
            created_at: now,
            last_login_at: None,
            must_change_password: must_change,
        };
        let _ = store.create_user(&admin).await; // 已有则忽略错误
        let mut state = AppState::new();
        state.store = Some(store.clone());
        (crate::server::build_router(state), store)
    }

    /// 走真实 login 端点登入，返回完整响应 JSON（token 对 + 标记）。
    async fn login(app: &axum::Router, username: &str, password: &str) -> serde_json::Value {
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"username": username, "password": password}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        serde_json::from_slice(&body).unwrap()
    }

    async fn login_status(app: &axum::Router, username: &str, password: &str) -> StatusCode {
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"username": username, "password": password}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        resp.status()
    }

    // ── 登录（D1-2：哈希校验，明文拒绝） ──

    #[tokio::test]
    async fn hash_password_login_succeeds_and_reports_flag() {
        // 播种账号 must_change_password=true → login 响应携带标记（D1-4 验收）
        let (app, _store) = app_with_seeded_admin(true).await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        assert!(v["access_token"].is_string(), "{v}");
        assert!(v["refresh_token"].is_string());
        assert_eq!(v["token_type"], "bearer");
        assert_eq!(v["expires_in"], 1800);
        assert_eq!(
            v["must_change_password"], true,
            "未改密账号 login 响应必须携带标记"
        );
    }

    #[tokio::test]
    async fn changed_account_login_reports_no_flag() {
        let (app, _store) = app_with_seeded_admin(false).await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        assert_eq!(v["must_change_password"], false);
    }

    #[tokio::test]
    async fn plaintext_stored_row_cannot_login() {
        // 存量明文行（迁移前形态）：口令校验 fail-closed，明文登录拒绝
        use agentos_core::traits::StorageBackend;
        let (app, store) = app_with_seeded_admin(false).await;
        store
            .create_user(&agentos_core::types::UserRecord {
                user_id: "u-plain".to_string(),
                username: "plainuser".to_string(),
                password: "plaintext-pw".to_string(), // 明文（未迁移）
                email: None,
                role: "user".to_string(),
                tenant_id: "u-plain".to_string(),
                created_at: chrono::Utc::now().to_rfc3339(),
                last_login_at: None,
                must_change_password: false,
            })
            .await
            .unwrap();
        assert_eq!(
            login_status(&app, "plainuser", "plaintext-pw").await,
            StatusCode::BAD_REQUEST,
            "明文存储行即使用对口令也必须拒绝登录（fail-closed）"
        );
    }

    #[tokio::test]
    async fn login_without_store_fails_closed() {
        // 无持久化即无凭据校验依据：脚手架表不可登录
        assert_eq!(
            login_status(&app(), "admin", TEST_ADMIN_PW).await,
            StatusCode::BAD_REQUEST
        );
    }

    #[tokio::test]
    async fn login_wrong_password_rejected() {
        let (app, _store) = app_with_store().await;
        assert_eq!(
            login_status(&app, "admin", "not-the-password").await,
            StatusCode::BAD_REQUEST
        );
        assert_eq!(
            login_status(&app, "nobody", "whatever").await,
            StatusCode::BAD_REQUEST
        );
    }

    // ── 登录限流（进程内滑动窗口） ──

    /// 完整登录响应（状态码 + Retry-After 头）。
    async fn login_response(
        app: &axum::Router,
        username: &str,
        password: &str,
    ) -> (StatusCode, Option<String>) {
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"username": username, "password": password}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        let retry_after = resp
            .headers()
            .get("retry-after")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_string());
        (resp.status(), retry_after)
    }

    #[tokio::test]
    async fn login_locked_after_ten_failures_with_retry_after() {
        let (app, _store) = app_with_store().await;
        // 窗口内 10 次失败：第 1-10 次均为 400（凭据错误语义）
        for _ in 0..10 {
            assert_eq!(
                login_status(&app, "admin", "wrong-pw").await,
                StatusCode::BAD_REQUEST
            );
        }
        // 第 11 次：即使携带正确凭据也 429 + Retry-After（锁定按失败计数，
        // 不因凭据正确而豁免——爆破者持有正确口令的场景同样受限）
        let (status, retry_after) = login_response(&app, "admin", TEST_ADMIN_PW).await;
        assert_eq!(
            status,
            StatusCode::TOO_MANY_REQUESTS,
            "窗口内 ≥10 次失败后必须 429"
        );
        assert_eq!(
            retry_after.as_deref(),
            Some("900"),
            "429 必须携带 Retry-After 头（15 分钟窗口）"
        );
        // 其他用户名不受同一计数影响（按用户名隔离窗口）
        let (other_status, _) = login_response(&app, "admin2", "wrong-pw").await;
        assert_eq!(
            other_status,
            StatusCode::BAD_REQUEST,
            "不同用户名窗口互不影响"
        );
    }

    #[tokio::test]
    async fn login_within_threshold_correct_credentials_succeed() {
        let (app, _store) = app_with_store().await;
        // 阈值内（3 次 < 10）失败后，正确凭据仍可登录
        for _ in 0..3 {
            assert_eq!(
                login_status(&app, "admin", "wrong-pw").await,
                StatusCode::BAD_REQUEST
            );
        }
        assert_eq!(
            login_status(&app, "admin", TEST_ADMIN_PW).await,
            StatusCode::OK,
            "阈值内失败不得影响正确凭据登录"
        );
    }

    #[tokio::test]
    async fn login_success_resets_failure_window() {
        let (app, _store) = app_with_store().await;
        // 5 次失败 → 成功登录清零 → 再 10 次失败仍不触发 429（计数已重置）
        for _ in 0..5 {
            let _ = login_status(&app, "admin", "wrong-pw").await;
        }
        assert_eq!(
            login_status(&app, "admin", TEST_ADMIN_PW).await,
            StatusCode::OK
        );
        for i in 0..10 {
            let (status, _) = login_response(&app, "admin", "wrong-pw").await;
            assert_eq!(
                status,
                StatusCode::BAD_REQUEST,
                "成功清零后第 {} 次失败仍应是凭据错误而非 429",
                i + 1
            );
        }
        // 窗口内累计 10 次失败（清零后重新计满）→ 429
        let (status, _) = login_response(&app, "admin", TEST_ADMIN_PW).await;
        assert_eq!(
            status,
            StatusCode::TOO_MANY_REQUESTS,
            "清零后重新计满仍要锁定"
        );
    }

    // ── me ──

    #[tokio::test]
    async fn me_with_valid_token_returns_user() {
        let (app, _store) = app_with_store().await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let token = v["access_token"].as_str().unwrap();
        let resp = app
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
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(json["username"], "admin");
        assert_eq!(json["role"], "admin");
        assert_eq!(json["is_active"], true);
    }

    #[tokio::test]
    async fn me_rejects_missing_and_invalid_tokens() {
        let (app, _store) = app_with_store().await;
        let resp = app
            .clone()
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

        let resp = app
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

    /// D12-5：无签名旧格式 token（base64 四段载荷）经 /me 一律 401。
    #[tokio::test]
    async fn me_rejects_unsigned_legacy_token() {
        use base64::Engine;
        let (app, _store) = app_with_store().await;
        let exp = chrono::Utc::now().timestamp() as u64 + 3600;
        let legacy = base64::engine::general_purpose::STANDARD_NO_PAD.encode(format!(
            "access:00000000-0000-0000-0000-000000000001:admin:{exp}"
        ));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/api/v1/auth/me")
                    .header("authorization", format!("Bearer {legacy}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::UNAUTHORIZED,
            "无签名旧 token 一律拒绝"
        );
    }

    /// D12-5：篡改载荷（换用户名提权）的签名 token → 401。
    #[tokio::test]
    async fn me_rejects_tampered_token() {
        use base64::Engine;
        let engine = base64::engine::general_purpose::URL_SAFE_NO_PAD;
        let (app, _store) = app_with_store().await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let token = v["access_token"].as_str().unwrap();
        let (payload_b64, sig) = token.split_once('.').unwrap();
        let payload = String::from_utf8(engine.decode(payload_b64).unwrap()).unwrap();
        // 把载荷中用户名改为 root（载荷必变、签名沿用）→ 验签必须拒绝
        let tampered = payload.replacen("admin", "root", 1);
        assert_ne!(tampered, payload, "前置：载荷确实被改");
        let forged = format!("{}.{sig}", engine.encode(tampered.as_bytes()));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/api/v1/auth/me")
                    .header("authorization", format!("Bearer {forged}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::UNAUTHORIZED,
            "篡改载荷必须验签拒绝"
        );
    }

    /// 改密后旧 token（未轮换的 access）访问 /me → 401（口令绑定吊销）。
    #[tokio::test]
    async fn me_rejects_old_token_after_password_change() {
        let (app, _store) = app_with_store().await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let old_access = v["access_token"].as_str().unwrap().to_string();
        let old_refresh = v["refresh_token"].as_str().unwrap().to_string();

        // 改密（带旧 access token）
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/change-password")
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {old_access}"))
                    .body(Body::from(
                        json!({"old_password": TEST_ADMIN_PW, "new_password": "brand-new-pw-9"})
                            .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let changed: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(changed["must_change_password"], false);
        assert!(changed["access_token"].as_str().is_some());

        // 旧 access token → 401（pwv 失配）
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/api/v1/auth/me")
                    .header("authorization", format!("Bearer {old_access}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::UNAUTHORIZED,
            "改密后旧 access token 必须失效"
        );

        // 旧 refresh token → 401（pwv 失配）
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/refresh")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"refresh_token": old_refresh}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::UNAUTHORIZED,
            "改密后旧 refresh token 必须失效"
        );
    }

    // ── 改密（D1-3） ──

    #[tokio::test]
    async fn change_password_rejects_wrong_old_password() {
        let (app, _store) = app_with_store().await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let token = v["access_token"].as_str().unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/change-password")
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {token}"))
                    .body(Body::from(
                        json!({"old_password": "wrong-old", "new_password": "brand-new-pw-9"})
                            .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST, "旧口令错误必须 400");
    }

    #[tokio::test]
    async fn change_password_requires_auth_and_min_length() {
        let (app, _store) = app_with_store().await;
        // 无 token → 401
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/change-password")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"old_password": "x", "new_password": "brand-new-pw-9"}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);

        // 新口令过短 → 400
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let token = v["access_token"].as_str().unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/change-password")
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {token}"))
                    .body(Body::from(
                        json!({"old_password": TEST_ADMIN_PW, "new_password": "short"}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::BAD_REQUEST,
            "新口令低于下限必须 400"
        );
    }

    /// 改密全链路：新口令可登录 + 旧口令拒绝 + 标记清除 + 落库为哈希。
    #[tokio::test]
    async fn change_password_full_flow_rotates_credentials() {
        let (app, store) = app_with_seeded_admin(true).await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let token = v["access_token"].as_str().unwrap();

        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/change-password")
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {token}"))
                    .body(Body::from(
                        json!({"old_password": TEST_ADMIN_PW, "new_password": "brand-new-pw-9"})
                            .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);

        // 旧口令登录拒绝（改密后旧口令拒绝——D1 验收）
        assert_eq!(
            login_status(&app, "admin", TEST_ADMIN_PW).await,
            StatusCode::BAD_REQUEST
        );
        // 新口令登录通过，且标记已清除
        let v2 = login(&app, "admin", "brand-new-pw-9").await;
        assert_eq!(v2["must_change_password"], false, "改密成功后标记必须清除");

        // 落库形态：argon2 哈希（非明文）
        let record =
            agentos_core::traits::StorageBackend::get_user_by_username(store.as_ref(), "admin")
                .await
                .unwrap()
                .unwrap();
        assert!(
            agentos_http::auth::is_password_hash(&record.password),
            "改密后落库必须是哈希形态"
        );
        assert!(verify_password("brand-new-pw-9", &record.password));
        assert!(!record.must_change_password);
    }

    // ── refresh（D12-7：单次轮换） ──

    #[tokio::test]
    async fn refresh_rotation_old_value_rejected() {
        let (app, _store) = app_with_store().await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let refresh1 = v["refresh_token"].as_str().unwrap().to_string();

        // 第一次刷新：成功，返回新 token 对
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/refresh")
                    .header("content-type", "application/json")
                    .body(Body::from(json!({"refresh_token": refresh1}).to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let r1: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert!(r1["access_token"].is_string());
        assert!(
            r1["refresh_token"].is_string(),
            "轮换必须返回新 refresh token"
        );
        assert_ne!(
            r1["refresh_token"].as_str().unwrap(),
            refresh1,
            "新 refresh token 必须与旧值不同"
        );

        // 旧值复用 → 401（D12-7 验收：refresh 轮换后旧值 401）
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/refresh")
                    .header("content-type", "application/json")
                    .body(Body::from(json!({"refresh_token": refresh1}).to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::UNAUTHORIZED,
            "轮换后旧 refresh token 必须 401"
        );

        // 新值可继续轮换（链路存续）
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/refresh")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"refresh_token": r1["refresh_token"].as_str().unwrap()}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK, "轮换链新值必须可用");
    }

    #[tokio::test]
    async fn refresh_with_invalid_token_rejected() {
        let (app, _store) = app_with_store().await;
        let resp = app
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/v1/auth/refresh")
                    .header("content-type", "application/json")
                    .body(Body::from(json!({"refresh_token": "invalid"}).to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    /// F2（2026-09-16 审查）：access token 提交 refresh 端点不得换发——
    /// 否则 30min 短 TTL 凭证泄露被拉平为 7 天长效会话。
    #[tokio::test]
    async fn refresh_rejects_access_token_type_confusion() {
        let (app, _store) = app_with_store().await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let access = v["access_token"].as_str().unwrap().to_string();
        let refresh = v["refresh_token"].as_str().unwrap().to_string();

        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/v1/auth/refresh")
                    .header("content-type", "application/json")
                    .body(Body::from(json!({"refresh_token": access}).to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::UNAUTHORIZED,
            "access token 不得经 refresh 端点换发长效凭证"
        );

        // 对照：真正的 refresh token 同链路可用（类型闸不误伤正常轮换）
        let resp = app
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/v1/auth/refresh")
                    .header("content-type", "application/json")
                    .body(Body::from(json!({"refresh_token": refresh}).to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::OK,
            "正常 refresh 轮换不受类型闸影响"
        );
    }

    /// store 在而用户不存在：签名合法的 token 也不得换发新 token（K4）。
    #[tokio::test]
    async fn refresh_rejects_token_when_user_absent_from_store() {
        use agentos_http::auth::{encode_token, TokenType};
        let store = std::sync::Arc::new(
            agentos_engine::SqliteStore::open_memory().expect("open_memory 失败"),
        );
        let mut state = AppState::new();
        state.store = Some(store);
        let app = crate::server::build_router(state);

        let ghost = scaffold_user("00000000-0000-0000-0000-000000000001", "admin");
        let refresh = encode_token(TokenType::Refresh, &ghost, 3600);
        let resp = app
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/v1/auth/refresh")
                    .header("content-type", "application/json")
                    .body(Body::from(json!({"refresh_token": refresh}).to_string()))
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

    // ── logout ──

    #[tokio::test]
    async fn logout_consumes_refresh_token_server_side() {
        let (app, _store) = app_with_store().await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let refresh = v["refresh_token"].as_str().unwrap().to_string();

        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/logout")
                    .header("content-type", "application/json")
                    .body(Body::from(json!({"refresh_token": refresh}).to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(json["success"], true);

        // 已登出的 refresh token → 401（服务端吊销）
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/refresh")
                    .header("content-type", "application/json")
                    .body(Body::from(json!({"refresh_token": refresh}).to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::UNAUTHORIZED,
            "登出必须吊销 refresh token"
        );
    }

    // ── register ──

    #[tokio::test]
    async fn test_register_duplicate() {
        let (app, _store) = app_with_store().await;
        let body =
            json!({"username": "admin", "password": "whatever-pw", "email": "a@b.c"}).to_string();
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
        assert_eq!(
            json["must_change_password"], false,
            "注册用户无首登改密诉求"
        );
    }

    // ── WS 握手鉴权出口（verify_access_token） ──

    /// 无凭据意义的脚手架用户（口令空串 = 不可登录，仅供 token 签发形状）。
    fn scaffold_user(id: &str, username: &str) -> agentos_http::auth::BuiltInUser {
        agentos_http::auth::BuiltInUser {
            id: id.to_string(),
            username: username.to_string(),
            password: String::new(),
            email: String::new(),
            role: "user".to_string(),
            tenant_id: DEFAULT_TENANT_ID.to_string(),
            created_at: String::new(),
            must_change_password: false,
        }
    }

    #[tokio::test]
    async fn verify_access_token_accepts_valid_login_token() {
        let (app, _store) = app_with_store().await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let token = v["access_token"].as_str().unwrap();

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
        let user = scaffold_user("u-1", "alice");
        let refresh =
            agentos_http::auth::encode_token(agentos_http::auth::TokenType::Refresh, &user, 3600);
        assert!(
            verify_access_token(&refresh).is_none(),
            "refresh token 不得用于 WS 鉴权"
        );
    }

    #[test]
    fn verify_access_token_rejects_expired() {
        let user = scaffold_user("u-1", "alice");
        // ttl=0 → exp=签发时刻，now >= exp 判过期
        let token =
            agentos_http::auth::encode_token(agentos_http::auth::TokenType::Access, &user, 0);
        assert!(verify_access_token(&token).is_none(), "过期 token 必须拒绝");
    }

    #[test]
    fn verify_access_token_unknown_user_passes_handshake_with_empty_tenant() {
        // 握手阶段 verify_access_token 同步上下文无法查 store，故不校验用户
        // 存在性——签名合法 + 未过期 + access 类型即放行，tenant_id 对未知用户
        // 留空。真正的用户存在性 + tenant 解析在 dispatch 时由
        // resolve_tenant_id_by_user（async，查 DB）权威完成。
        let ghost = scaffold_user("no-such-user-id", "ghost");
        let token =
            agentos_http::auth::encode_token(agentos_http::auth::TokenType::Access, &ghost, 3600);
        let verified = verify_access_token(&token).expect("格式合法的 access token 应通过握手");
        assert_eq!(verified.user_id, "no-such-user-id");
        assert_eq!(verified.username, "ghost");
        assert_eq!(
            verified.tenant_id, "",
            "未知用户的 tenant_id 应为空（dispatch 时解析）"
        );
    }

    // ── 持久化用户系统 + 多租户隔离 ──

    /// 注册应在 DB 创建真实用户，且一用户一租户（tenant_id == user_id），
    /// 口令落库为哈希。
    #[tokio::test]
    async fn test_register_creates_persistent_user_with_unique_tenant() {
        use agentos_core::traits::StorageBackend;
        let (app, store) = app_with_store().await;

        let body = json!({
            "username": "alice",
            "password": "alice12345",
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
        let t = decode_token(access_token).unwrap();
        assert_eq!(t.username, "alice");
        let user = store
            .get_user_by_username("alice")
            .await
            .expect("查询不应出错")
            .expect("alice 应已持久化");
        assert_eq!(user.user_id, t.user_id);
        assert_eq!(
            user.tenant_id, user.user_id,
            "一用户一租户: tenant_id 应等于 user_id"
        );
        assert!(
            agentos_http::auth::is_password_hash(&user.password),
            "注册口令必须哈希落库"
        );
        assert!(verify_password("alice12345", &user.password));
    }

    /// 两个不同用户应有不同的 tenant_id（隔离前提）。
    #[tokio::test]
    async fn test_two_users_have_different_tenants() {
        use agentos_core::traits::StorageBackend;
        let (app, store) = app_with_store().await;

        for name in ["bob", "carol"] {
            let body = json!({"username": name, "password": format!("{name}12345"), "email": format!("{name}@test.com")}).to_string();
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
            json!({"username": "dave", "password": "dave12345", "email": "dave@t.com"}).to_string();

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

        let body2 = json!({"username": "dave", "password": "other12345", "email": "dave2@t.com"})
            .to_string();
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

    /// 走真实 register 端点，返回完整响应（username 字符集测试共用出口）。
    async fn register_resp(
        app: &axum::Router,
        username: &str,
    ) -> axum::http::Response<axum::body::Body> {
        let body =
            json!({"username": username, "password": "pw-123456", "email": "x@t.com"}).to_string();
        app.clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/register")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap()
    }

    /// username 字符集白名单（token 载荷分隔符安全）：username 原样进入
    /// `:` 分隔的 token 载荷六段（agentos_http::auth），混入 `:` 令 exp 段
    /// 错位 → 该账户签发的所有 token 恒解码失败（注册即自锁）。越界输入
    /// 必须 400 且不落库。
    #[tokio::test]
    async fn register_rejects_username_outside_charset_whitelist() {
        use agentos_core::traits::StorageBackend;
        let (app, store) = app_with_store().await;
        let invalid = [
            "a:b",         // 载荷分隔符（自锁案例本体）
            "has space",   // 空白
            "用户名",      // 非 ASCII
            "line\nbreak", // 控制字符
            "",            // 空用户名
        ];
        for name in invalid {
            let resp = register_resp(&app, name).await;
            assert_eq!(
                resp.status(),
                StatusCode::BAD_REQUEST,
                "越界用户名 {name:?} 必须 400"
            );
        }
        let users = store.list_users().await.unwrap();
        assert_eq!(users.len(), 1, "只应有播种的 admin，非法输入一个不落库");
    }

    /// 白名单内用户名放行（正常字符集 ≥2 组 + 长度上界），且签发 token 可
    /// 解码、username 保真（分隔符无歧义的性质断言）。
    #[tokio::test]
    async fn register_accepts_username_within_charset_whitelist() {
        let (app, _store) = app_with_store().await;
        let valid = [
            "alice",
            "user.name@x-y_01", // 覆盖全部允许字符类
            &"a".repeat(64),    // 长度上界
        ];
        for name in valid {
            let resp = register_resp(&app, name).await;
            assert_eq!(resp.status(), StatusCode::OK, "白名单用户名 {name} 应放行");
            let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
            let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
            let t = decode_token(v["access_token"].as_str().unwrap()).unwrap();
            assert_eq!(t.username, name, "token 内 username 必须保真");
        }
    }

    // ── token 编解码单元测试（签名格式） ──

    #[test]
    fn test_token_encode_decode_roundtrip() {
        let user = scaffold_user("test-id", "testuser");
        let token = encode_token(TokenType::Access, &user, 3600);
        let t = decode_token(&token).unwrap();
        assert_eq!(t.token_type, "access");
        assert_eq!(t.user_id, "test-id");
        assert_eq!(t.username, "testuser");
        assert!(t.exp > chrono::Utc::now().timestamp() as u64);
        assert!(!t.jti.is_empty());
    }

    #[test]
    fn test_decode_garbage_returns_none() {
        assert!(decode_token("!!!notbase64!!!").is_none());
        assert!(decode_token("dGVzdA==").is_none()); // valid base64 but unsigned/legacy
    }

    #[test]
    fn test_is_token_expired() {
        let now = chrono::Utc::now().timestamp() as u64;
        assert!(is_token_expired(now - 1)); // 过去
        assert!(!is_token_expired(now + 3600)); // 未来
    }

    // ── 登录限流表设界（S1）：条目数上限 + 空窗口清理 ──

    #[test]
    fn login_rate_table_bounded_under_flood() {
        // 随机用户名刷失败（免认证面）不得让限流表无限增长：
        // 新用户名在满表时逐出最旧条目完成占位，条目数恒 ≤ 上限。
        let state = AppState::new();
        {
            let mut failures = state.login_failures.lock();
            for i in 0..LOGIN_FAILURES_MAX_ENTRIES {
                failures.insert(format!("flood-user-{i}"), vec![std::time::Instant::now()]);
            }
        }
        login_rate_check(&state, "fresh-user").expect("满表逐出后新用户名必须可判定");
        let failures = state.login_failures.lock();
        assert!(
            failures.len() <= LOGIN_FAILURES_MAX_ENTRIES,
            "条目数必须钉在 {} 以内，实际 {}",
            LOGIN_FAILURES_MAX_ENTRIES,
            failures.len()
        );
        assert!(failures.contains_key("fresh-user"), "新用户名必须完成占位");
    }

    #[test]
    fn login_rate_table_cleans_empty_queues() {
        // 空窗口条目（历史清零残留）在判定顺带清除，不让僵尸键常驻
        let state = AppState::new();
        state
            .login_failures
            .lock()
            .insert("ghost-user".to_string(), vec![]);
        login_rate_check(&state, "other-user").expect("空窗口不构成限流");
        let failures = state.login_failures.lock();
        assert!(
            !failures.contains_key("ghost-user"),
            "空窗口条目必须被顺带清理"
        );
        assert_eq!(
            failures.len(),
            1,
            "只剩本次判定的用户名条目（判定本身会为当前用户名占位）"
        );
    }

    // ── 注册错误翻译（S9）：存储故障 500，不伪装 4xx ──

    struct FailingStore;

    impl FailingStore {
        fn outage() -> agentos_core::types::StorageError {
            agentos_core::types::StorageError::NotFound("injected storage outage".to_string())
        }
    }

    #[async_trait::async_trait]
    impl agentos_core::traits::StorageBackend for FailingStore {
        async fn get_run(
            &self,
            _run_id: &str,
        ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
            unreachable!("注册路径不调用")
        }
        async fn get_messages_by_pipeline(
            &self,
            _pipeline_id: &str,
            _opts: agentos_core::traits::MessageQueryOpts,
        ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError>
        {
            unreachable!("注册路径不调用")
        }
        async fn get_blob(
            &self,
            _blob_id: &str,
        ) -> Result<Vec<u8>, agentos_core::types::StorageError> {
            unreachable!("注册路径不调用")
        }
        async fn append_trace(
            &self,
            _entry: agentos_core::types::TraceEntry,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("注册路径不调用")
        }
        async fn store_blob(
            &self,
            _data: &[u8],
            _mime_type: &str,
        ) -> Result<String, agentos_core::types::StorageError> {
            unreachable!("注册路径不调用")
        }
        async fn create_session(
            &self,
            _session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("注册路径不调用")
        }
        async fn get_session(
            &self,
            _thread_id: &str,
        ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            unreachable!("注册路径不调用")
        }
        async fn list_sessions(
            &self,
            _filter: agentos_core::traits::SessionListFilter,
        ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            unreachable!("注册路径不调用")
        }
        async fn update_session(
            &self,
            _session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("注册路径不调用")
        }
        async fn delete_session(
            &self,
            _thread_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            unreachable!("注册路径不调用")
        }
        async fn link_pipeline_session(
            &self,
            _pipeline_id: &str,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("注册路径不调用")
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            unreachable!("注册路径不调用")
        }
        async fn get_step_traces_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError>
        {
            unreachable!("注册路径不调用")
        }
        async fn create_user(
            &self,
            _user: &agentos_core::types::UserRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("重名检查已失败，不应触达创建")
        }
        async fn get_user_by_id(
            &self,
            _user_id: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            unreachable!("注册路径不调用")
        }
        async fn get_user_by_username(
            &self,
            _username: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            Err(Self::outage())
        }
        async fn list_users(
            &self,
        ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            unreachable!("注册路径不调用")
        }
        async fn update_last_login(
            &self,
            _user_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("注册路径不调用")
        }
        async fn update_user_password(
            &self,
            _user_id: &str,
            _password_hash: &str,
            _must_change_password: bool,
        ) -> Result<bool, agentos_core::types::StorageError> {
            unreachable!("注册路径不调用")
        }
        async fn delete_user(
            &self,
            _user_id: &str,
        ) -> Result<bool, agentos_core::types::StorageError> {
            unreachable!("注册路径不调用")
        }
    }

    #[tokio::test]
    async fn register_storage_outage_is_internal_error_not_bad_request() {
        // 重名检查查询失败 ≠ 用户名不存在：吞掉 Err 会把基础设施故障伪装成
        // "可注册"，落库阶段再以误导性错误失败（错误翻译纪律）。
        let mut state = AppState::new();
        state.store = Some(std::sync::Arc::new(FailingStore));
        let app = crate::server::build_router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/register")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({
                            "username": "outage-probe",
                            "password": "Str0ng-Passw0rd!",
                            "email": "probe@agentos.dev",
                        })
                        .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::INTERNAL_SERVER_ERROR,
            "存储故障必须 500，不得伪装成 4xx"
        );
    }

    // ── 进程内限流：注册限频 / 登录窗口滑动与表级逐出 ──

    async fn register_status(app: &axum::Router, username: &str) -> StatusCode {
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/register")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"username": username, "password": "reg-pw-12345", "email": "u@test.dev"})
                            .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        resp.status()
    }

    #[tokio::test]
    async fn register_rate_limited_after_threshold_returns_429() {
        let state = AppState::new();
        {
            let mut attempts = state.register_attempts.lock();
            for _ in 0..REGISTER_MAX_ATTEMPTS {
                attempts.push(std::time::Instant::now());
            }
        }
        let app = crate::server::build_router(state);
        assert_eq!(
            register_status(&app, "new_user_1").await,
            StatusCode::TOO_MANY_REQUESTS,
            "窗口内尝试达上限后注册必须 429"
        );
    }

    #[tokio::test]
    async fn register_attempts_below_threshold_pass_rate_gate() {
        // 上限-1 次：限频放行（本次尝试记入窗口），进入后续校验——
        // 用越界字符集 username 触发 400 而非 429，证明闸门通过。
        let state = AppState::new();
        {
            let mut attempts = state.register_attempts.lock();
            for _ in 0..REGISTER_MAX_ATTEMPTS - 1 {
                attempts.push(std::time::Instant::now());
            }
        }
        let app = crate::server::build_router(state);
        assert_eq!(
            register_status(&app, "bad charset!").await,
            StatusCode::BAD_REQUEST,
            "未达上限应放行限频闸（400 = 字符集校验拒绝）"
        );
    }

    #[tokio::test]
    async fn login_aged_failures_outside_window_do_not_lock() {
        // 窗口全滑出的历史失败不锁号：≥LOGIN_MAX_FAILURES 条但全部早于
        // 窗口宽度 → login_rate_check 清空后放行（400 = 凭据错误，非 429）。
        // 直接经 handler 注入预置状态（build_router 会消费 AppState，无法后注）。
        let state = AppState::new();
        let old = std::time::Instant::now()
            - std::time::Duration::from_secs(LOGIN_FAILURE_WINDOW_SECS + 60);
        {
            let mut failures = state.login_failures.lock();
            failures.insert(
                "aged_user".to_string(),
                (0..LOGIN_MAX_FAILURES).map(|_| old).collect(),
            );
        }
        let err = login_handler(
            axum::extract::State(state.clone()),
            Json(LoginRequest {
                username: "aged_user".to_string(),
                password: "wrong-pw".to_string(),
            }),
        )
        .await
        .expect_err("错误口令应登录失败");
        assert!(
            matches!(err, ApiError::BadRequest { .. }),
            "窗口外旧失败应放行限流闸（400 = 凭据错误），实际 {err:?}"
        );
    }

    #[tokio::test]
    async fn login_failures_table_eviction_keeps_bounded() {
        // 表级有界：条目数达上限后，新用户名登录尝试逐出窗口起点最早的一条
        //（免认证 DoS 面的内存有界契约）。全部条目取窗口内时间戳 → 清理分支
        // 不代劳，逐出分支（逐出最旧 + 新用户名占位）被真实命中。
        let state = AppState::new();
        {
            let mut failures = state.login_failures.lock();
            // u9999 首个失败时刻最早：其余条目用单调时钟向后毫秒偏移构造
            // 「更晚」。不用 now-大回退构造年龄——Windows 上 Instant 自开机
            // 起算，开机未满窗口秒数时减法下溢 panic（环境脆断）。
            let now = std::time::Instant::now();
            failures.insert("u9999".to_string(), vec![now]);
            for i in 0..LOGIN_FAILURES_MAX_ENTRIES - 1 {
                failures.insert(
                    format!("u{i}"),
                    vec![now + std::time::Duration::from_millis(i as u64 + 1)],
                );
            }
        }
        let oldest_key = "u9999"; // 首个失败时刻最早
        let err = login_handler(
            axum::extract::State(state.clone()),
            Json(LoginRequest {
                username: "fresh_user".to_string(),
                password: "wrong-pw".to_string(),
            }),
        )
        .await
        .expect_err("错误口令应登录失败");
        assert!(matches!(err, ApiError::BadRequest { .. }));
        let failures = state.login_failures.lock();
        assert!(
            !failures.contains_key(oldest_key),
            "最旧条目应被逐出以保持表有界"
        );
        assert!(failures.contains_key("fresh_user"), "新用户名必须完成占位");
        // 有界不变量：满表逐出一进一出后恰好钉在上限（原断言 len < MAX 只在
        // 窗口清理先清掉大部分种子行时成立，并非逐出分支的契约）
        assert_eq!(
            failures.len(),
            LOGIN_FAILURES_MAX_ENTRIES,
            "条目数必须钉在上限以内: {}",
            failures.len()
        );
    }

    // ── 故障路径翻译：内部故障 500 / 未命中 404 / 口令绑定失配 401 ──────────
    //
    // 这些分支此前零命中：它们要求存储层在特定方法上报错或在关键步骤返回
    // 「未更新」，正常 SqliteStore 不会走到。用「只故障目标方法、其余转发真库」
    // 的包装 store 逐条取证，避免为造故障而伪造整个存储面。

    /// 包装真实 SqliteStore，可注入单方法故障/负结果。
    struct FaultStore {
        inner: std::sync::Arc<agentos_engine::SqliteStore>,
        fail_last_login: std::sync::atomic::AtomicBool,
        fail_get_user_by_username: std::sync::atomic::AtomicBool,
        fail_update_password: std::sync::atomic::AtomicBool,
        password_update_reports_missing: std::sync::atomic::AtomicBool,
    }

    impl FaultStore {
        fn new(inner: std::sync::Arc<agentos_engine::SqliteStore>) -> Self {
            Self {
                inner,
                fail_last_login: std::sync::atomic::AtomicBool::new(false),
                fail_get_user_by_username: std::sync::atomic::AtomicBool::new(false),
                fail_update_password: std::sync::atomic::AtomicBool::new(false),
                password_update_reports_missing: std::sync::atomic::AtomicBool::new(false),
            }
        }
        fn set(&self, which: &str, on: bool) {
            use std::sync::atomic::Ordering;
            let flag = match which {
                "last_login" => &self.fail_last_login,
                "get_user" => &self.fail_get_user_by_username,
                "update_password" => &self.fail_update_password,
                "password_missing" => &self.password_update_reports_missing,
                other => panic!("未知注入点: {other}"),
            };
            flag.store(on, Ordering::Relaxed);
        }
        fn outage() -> agentos_core::types::StorageError {
            agentos_core::types::StorageError::Database("injected fault".to_string())
        }
    }

    #[async_trait::async_trait]
    impl agentos_core::traits::StorageBackend for FaultStore {
        async fn get_run(
            &self,
            run_id: &str,
        ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
            self.inner.get_run(run_id).await
        }
        async fn get_messages_by_pipeline(
            &self,
            pipeline_id: &str,
            opts: agentos_core::traits::MessageQueryOpts,
        ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError>
        {
            self.inner.get_messages_by_pipeline(pipeline_id, opts).await
        }
        async fn get_blob(
            &self,
            blob_id: &str,
        ) -> Result<Vec<u8>, agentos_core::types::StorageError> {
            self.inner.get_blob(blob_id).await
        }
        async fn append_trace(
            &self,
            entry: agentos_core::types::TraceEntry,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner.append_trace(entry).await
        }
        async fn record_run_start(
            &self,
            pipeline_id: &str,
            tenant_id: &str,
            run_id: &str,
            config_hash: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner
                .record_run_start(pipeline_id, tenant_id, run_id, config_hash)
        }
        async fn store_blob(
            &self,
            data: &[u8],
            mime_type: &str,
        ) -> Result<String, agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::store_blob(self.inner.as_ref(), data, mime_type)
                .await
        }
        async fn create_session(
            &self,
            session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner.create_session(session).await
        }
        async fn get_session(
            &self,
            thread_id: &str,
        ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            self.inner.get_session(thread_id).await
        }
        async fn list_sessions(
            &self,
            filter: agentos_core::traits::SessionListFilter,
        ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            self.inner.list_sessions(filter).await
        }
        async fn update_session(
            &self,
            session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner.update_session(session).await
        }
        async fn delete_session(
            &self,
            thread_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            self.inner.delete_session(thread_id).await
        }
        async fn link_pipeline_session(
            &self,
            pipeline_id: &str,
            thread_id: &str,
            tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner
                .link_pipeline_session(pipeline_id, thread_id, tenant_id)
                .await
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            thread_id: &str,
            tenant_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            self.inner
                .list_pipeline_ids_by_thread(thread_id, tenant_id)
                .await
        }
        async fn get_step_traces_by_thread(
            &self,
            thread_id: &str,
            tenant_id: &str,
        ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError>
        {
            self.inner
                .get_step_traces_by_thread(thread_id, tenant_id)
                .await
        }
        async fn create_user(
            &self,
            user: &agentos_core::types::UserRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner.create_user(user).await
        }
        async fn get_user_by_id(
            &self,
            user_id: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            self.inner.get_user_by_id(user_id).await
        }
        async fn get_user_by_username(
            &self,
            username: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            if self
                .fail_get_user_by_username
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                return Err(Self::outage());
            }
            self.inner.get_user_by_username(username).await
        }
        async fn list_users(
            &self,
        ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            self.inner.list_users().await
        }
        async fn update_last_login(
            &self,
            user_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            if self
                .fail_last_login
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                return Err(Self::outage());
            }
            self.inner.update_last_login(user_id).await
        }
        async fn update_user_password(
            &self,
            user_id: &str,
            password_hash: &str,
            must_change_password: bool,
        ) -> Result<bool, agentos_core::types::StorageError> {
            if self
                .fail_update_password
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                return Err(Self::outage());
            }
            if self
                .password_update_reports_missing
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                return Ok(false);
            }
            self.inner
                .update_user_password(user_id, password_hash, must_change_password)
                .await
        }
        async fn delete_user(
            &self,
            user_id: &str,
        ) -> Result<bool, agentos_core::types::StorageError> {
            self.inner.delete_user(user_id).await
        }
    }

    /// 播种 admin 并返回（router, 故障可注入 store 句柄）。
    async fn app_with_fault_store() -> (axum::Router, std::sync::Arc<FaultStore>) {
        use agentos_core::traits::StorageBackend as _;
        let inner = std::sync::Arc::new(
            agentos_engine::SqliteStore::open_memory().expect("open_memory 失败"),
        );
        inner
            .create_user(&agentos_core::types::UserRecord {
                user_id: "u-fault-admin".to_string(),
                username: "admin".to_string(),
                password: hash_password(TEST_ADMIN_PW).unwrap(),
                email: Some("admin@agentos.dev".to_string()),
                role: "admin".to_string(),
                tenant_id: DEFAULT_TENANT_ID.to_string(),
                created_at: chrono::Utc::now().to_rfc3339(),
                last_login_at: None,
                must_change_password: false,
            })
            .await
            .expect("播种 admin");
        let store = std::sync::Arc::new(FaultStore::new(inner));
        let mut state = AppState::new();
        state.store =
            Some(store.clone() as std::sync::Arc<dyn agentos_core::traits::StorageBackend>);
        (crate::server::build_router(state), store)
    }

    /// last_login 更新失败是 best-effort：登录仍成功，但必须 warn 留痕
    /// （静默吞掉会让"最近登录时间长期不变"无从归因）。
    #[tokio::test]
    async fn login_succeeds_when_last_login_update_fails_and_warns() {
        let (_guard, logs) = crate::test_env::capture_logs();
        let (app, store) = app_with_fault_store().await;
        store.set("last_login", true);

        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let text = logs.text();

        assert!(v["access_token"].is_string(), "last_login 失败不得影响登录");
        assert!(
            text.contains("Failed to update last_login"),
            "best-effort 失败必须 warn 留痕: {text}"
        );
    }

    /// refresh：口令绑定失配（签发后口令已改）→ 401 且文案指明失配原因
    /// （与"用户不存在"区分，便于前端判别是否需要重新登录）。
    #[tokio::test]
    async fn refresh_rejects_token_when_password_binding_stale() {
        let (app, store) = app_with_fault_store().await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let refresh_token = v["refresh_token"].as_str().unwrap().to_string();

        // 直接改库口令（模拟其他会话改密）→ 旧 refresh token 的 pwv 失配
        use agentos_core::traits::StorageBackend;
        store
            .update_user_password(
                "u-fault-admin",
                &hash_password("new-pw-2026").unwrap(),
                false,
            )
            .await
            .unwrap();

        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/refresh")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"refresh_token": refresh_token}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let msg = v["error"]["message"].as_str().unwrap_or_default();
        assert!(msg.contains("口令已变更"), "改密吊销须给出可判别文案: {v}");
    }

    /// 改密：store 查询用户报错 → 500（内部故障，不伪装成 401）；
    /// 写新口令报错 → 500；写回「未更新」→ 404（用户不存在）。
    #[tokio::test]
    async fn change_password_translates_storage_faults_distinctly() {
        let (app, store) = app_with_fault_store().await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let token = v["access_token"].as_str().unwrap().to_string();

        let change = |token: String| {
            let app = app.clone();
            async move {
                app.oneshot(
                    Request::builder()
                        .method("POST")
                        .uri("/api/v1/auth/change-password")
                        .header("content-type", "application/json")
                        .header("authorization", format!("Bearer {token}"))
                        .body(Body::from(
                            json!({"old_password": TEST_ADMIN_PW, "new_password": "brand-new-pw-2026"})
                                .to_string(),
                        ))
                        .unwrap(),
                )
                .await
                .unwrap()
                .status()
            }
        };

        store.set("get_user", true);
        assert_eq!(
            change(token.clone()).await,
            StatusCode::INTERNAL_SERVER_ERROR,
            "查询故障是内部故障（500），不得伪装 401/404"
        );
        store.set("get_user", false);

        store.set("update_password", true);
        assert_eq!(
            change(token.clone()).await,
            StatusCode::INTERNAL_SERVER_ERROR,
            "写新口令故障是内部故障（500）"
        );
        store.set("update_password", false);

        store.set("password_missing", true);
        assert_eq!(
            change(token).await,
            StatusCode::NOT_FOUND,
            "写回未更新 = 用户不存在（404）"
        );
    }

    /// 改密：refresh token 不得用于改密（须 access 类型）；
    /// 过期 access token → 401「认证令牌已过期」。
    #[tokio::test]
    async fn change_password_rejects_refresh_and_expired_tokens() {
        let (app, _store) = app_with_fault_store().await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let refresh_token = v["refresh_token"].as_str().unwrap().to_string();

        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/change-password")
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {refresh_token}"))
                    .body(Body::from(
                        json!({"old_password": TEST_ADMIN_PW, "new_password": "brand-new-pw-2026"})
                            .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::UNAUTHORIZED,
            "refresh token 不得用于改密"
        );
    }

    /// refresh：过期令牌 → 401「刷新令牌已过期」（与"已被使用"分支区分）。
    #[tokio::test]
    async fn refresh_rejects_expired_token_with_distinct_message() {
        let (_app, _store) = app_with_fault_store().await;
        let record = agentos_core::types::UserRecord {
            user_id: "u-rf-exp".to_string(),
            username: "rf_expired".to_string(),
            password: hash_password(TEST_ADMIN_PW).unwrap(),
            email: None,
            role: "user".to_string(),
            tenant_id: "u-rf-exp".to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_login_at: None,
            must_change_password: false,
        };
        use agentos_core::traits::StorageBackend;
        let inner = std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        inner.create_user(&record).await.unwrap();
        let mut state = AppState::new();
        state.store = Some(inner.clone() as std::sync::Arc<dyn StorageBackend>);
        let app2 = crate::server::build_router(state);

        let expired_refresh = encode_token(TokenType::Refresh, &BuiltInUser::from(&record), 0);
        let resp = app2
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/refresh")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"refresh_token": expired_refresh}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = resp.status();
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(status, StatusCode::UNAUTHORIZED);
        assert_eq!(
            v["error"]["message"].as_str(),
            Some("刷新令牌已过期"),
            "过期分支须与复用/畸形分支文案区分: {v}"
        );
    }

    /// 注册：无 store 时命中内置用户表重名 → 400（不走落库路径）。
    #[tokio::test]
    async fn register_duplicate_builtin_username_rejected_without_store() {
        let state = AppState::new();
        let app = crate::server::build_router(state);
        // 内置表只有 admin（default_users）
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/register")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"username": "admin", "password": "reg-pw-12345",
                               "email": "a@test.dev"})
                        .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::BAD_REQUEST,
            "内置表重名须 400（用户名已存在）"
        );
    }

    /// 改密成功：新 token 对可用，且旧 token 立即失效（口令绑定吊销）。
    #[tokio::test]
    async fn change_password_response_tokens_work_and_old_token_dies() {
        let (app, _store) = app_with_fault_store().await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let old_access = v["access_token"].as_str().unwrap().to_string();

        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/change-password")
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {old_access}"))
                    .body(Body::from(
                        json!({"old_password": TEST_ADMIN_PW, "new_password": "rotated-pw-2026"})
                            .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let new: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let new_access = new["access_token"].as_str().unwrap().to_string();

        // 新 token 可访问 /me
        let ok = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/api/v1/auth/me")
                    .header("authorization", format!("Bearer {new_access}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(ok.status(), StatusCode::OK, "改密响应携带的 token 必须可用");

        // 旧 token 立即失效
        let dead = app
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/api/v1/auth/me")
                    .header("authorization", format!("Bearer {old_access}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            dead.status(),
            StatusCode::UNAUTHORIZED,
            "改密后旧 token 必须被口令绑定吊销"
        );
    }

    /// 改密：新口令长度不足 → 400 且不落库（旧口令仍可登录）。
    #[tokio::test]
    async fn change_password_short_new_password_leaves_old_usable() {
        let (app, _store) = app_with_fault_store().await;
        let v = login(&app, "admin", TEST_ADMIN_PW).await;
        let access = v["access_token"].as_str().unwrap().to_string();

        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/change-password")
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {access}"))
                    .body(Body::from(
                        json!({"old_password": TEST_ADMIN_PW, "new_password": "short"}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        // 旧口令仍可用（拒绝发生在写库之前）
        let v2 = login(&app, "admin", TEST_ADMIN_PW).await;
        assert!(v2["access_token"].is_string(), "拒绝必须先于落库");
    }
}
