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

/// 滑动窗口起点：`now - window` 的饱和形式。Windows 上 Instant 自系统开机
/// 起算，裸减法在开机未满窗口宽（15 min）时下溢 panic——生产实锤
/// （2026-10-13 装机版 /auth/login 崩溃循环，全部落在开机 6~14.5 分钟）。
/// `None` = 窗口起点早于单调时钟纪元：进程内限流计数重启即清零，现存记录
/// 必然全部在窗口内，调用方按「全保留」消费（`is_none_or`）；误清（如
/// `unwrap_or(now)` 式回退）= 窗口内爆破防护失效。
fn sliding_window_cutoff(
    now: std::time::Instant,
    window: std::time::Duration,
) -> Option<std::time::Instant> {
    now.checked_sub(window)
}

/// 清理窗口外失败并判定：窗口内失败次数 ≥ 上限 → 429 + Retry-After。
/// 表级设界：顺带清空窗口全滑出的条目；条目数超上限（未认证 DoS 面：
/// 随机用户名刷失败）时逐出窗口起点最早的一条——限流对最旧用户名提前
/// 失忆，属可用性微损，换取内存有界。
fn login_rate_check(state: &AppState, username: &str) -> Result<(), ApiError> {
    login_rate_check_at(
        std::time::Instant::now(),
        std::time::Duration::from_secs(LOGIN_FAILURE_WINDOW_SECS),
        state,
        username,
    )
}

/// 限流判定核心（时钟/窗口注入点）：生产路径由外层供真实时钟与常量；
/// 测试注入超宽窗口（u64::MAX 秒）即可确定性复现「开机未满窗口宽」的
/// 下溢分支——std 无法构造开机前的 Instant，裸减法不可测且必 panic。
fn login_rate_check_at(
    now: std::time::Instant,
    window: std::time::Duration,
    state: &AppState,
    username: &str,
) -> Result<(), ApiError> {
    let cutoff = sliding_window_cutoff(now, window);
    let mut failures = state.login_failures.lock();
    failures.retain(|_, queue| {
        queue.retain(|t| cutoff.is_none_or(|c| *t > c));
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
    register_rate_check_at(
        std::time::Instant::now(),
        std::time::Duration::from_secs(REGISTER_WINDOW_SECS),
        state,
    )
}

/// 注册限频核心（时钟/窗口注入点）：注入契约同 `login_rate_check_at`。
fn register_rate_check_at(
    now: std::time::Instant,
    window: std::time::Duration,
    state: &AppState,
) -> Result<(), ApiError> {
    let cutoff = sliding_window_cutoff(now, window);
    let mut attempts = state.register_attempts.lock();
    attempts.retain(|t| cutoff.is_none_or(|c| *t > c));
    if attempts.len() >= REGISTER_MAX_ATTEMPTS {
        return Err(ApiError::TooManyRequests {
            message: format!("注册请求过于频繁，请 {REGISTER_WINDOW_SECS} 秒后重试"),
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

#[cfg(test)]
mod tests;
