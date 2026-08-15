//! 请求用户解析 + token 编解码（api / db-admin 管理面共用，鉴权单一来源）。
//!
//! db-admin 独立 crate 后无法依赖 api（循环依赖），但 `/api/v1/db/*` 与 api 的
//! `write_surface_auth` 共用同一套用户解析逻辑。此处为唯一实现，
//! api::auth 以 `pub use` 再导出保持既有引用不变。
//!
//! Token 格式：base64({type}:{user_id}:{username}:{exp_unix_secs})
//! DEBT: base64 编码无签名，可被任何人解码伪造。ceiling: 仅限 0.2 开发/演示环境。
//! upgrade: 接入正式认证后替换为 JWT/HMAC 签名 + 密钥轮换。
//! 前端 client.ts 仅注入 Bearer 头，不解析 token 内容（已验证 NEED-1），
//! 因此该方案在开发阶段安全。

use axum::http::HeaderMap;

use crate::error::ApiError;

// ─── 常量 ────────────────────────────────────────────────────────────

/// 内置默认租户 ID——所有内置用户归属此租户。
pub const DEFAULT_TENANT_ID: &str = "default";

/// 内置默认用户（硬编码，无密码哈希开销，满足"简单内置用户"需求）。
/// DEBT: 明文密码仅用于开发/演示。ceiling: 无多用户管理。
/// upgrade: 接入正式用户系统时替换为数据库 + 哈希校验。
pub fn default_users() -> Vec<BuiltInUser> {
    vec![BuiltInUser {
        id: "00000000-0000-0000-0000-000000000001".to_string(),
        username: "admin".to_string(),
        password: "admin12345".to_string(),
        email: "admin@agentos.dev".to_string(),
        role: "admin".to_string(),
        tenant_id: DEFAULT_TENANT_ID.to_string(),
        created_at: "2025-01-01T00:00:00Z".to_string(),
    }]
}

// ─── 数据结构 ────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct BuiltInUser {
    pub id: String,
    pub username: String,
    pub password: String,
    pub email: String,
    pub role: String,
    /// 用户归属的租户 ID（多租户隔离）。
    pub tenant_id: String,
    pub created_at: String,
}

impl From<&agentos_core::types::UserRecord> for BuiltInUser {
    /// 从持久化 UserRecord 构造 BuiltInUser（用于 encode_token 复用）。
    fn from(u: &agentos_core::types::UserRecord) -> Self {
        Self {
            id: u.user_id.clone(),
            username: u.username.clone(),
            password: u.password.clone(),
            email: u.email.clone().unwrap_or_default(),
            role: u.role.clone(),
            tenant_id: u.tenant_id.clone(),
            created_at: u.created_at.clone(),
        }
    }
}

// ─── Token 编解码 ────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum TokenType {
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

pub fn encode_token(token_type: TokenType, user: &BuiltInUser, ttl_secs: u64) -> String {
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

pub fn decode_token(token: &str) -> Option<(String, String, u64)> {
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

pub fn is_token_expired(exp: u64) -> bool {
    chrono::Utc::now().timestamp() as u64 >= exp
}

/// 解析请求归属的租户 ID（HTTP 路径用，async）。
///
/// 当前 token 载荷（`{type}:{user_id}:{username}:{exp}`）尚未包含 tenant 段，
/// 因此本函数在 token 合法时查 store / 内置用户表得到 `tenant_id`，
/// 否则回退到 [`DEFAULT_TENANT_ID`]。
///
/// TODO(多租户): token 格式扩展为携带 tenant 段后，改为优先从 token 解析。
pub async fn resolve_request_tenant_id(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    headers: &HeaderMap,
) -> String {
    if let Some(token) = extract_bearer_token(headers) {
        if let Some((user_id, _, exp)) = decode_token(&token) {
            if !is_token_expired(exp) {
                return resolve_tenant_id_by_user(store, &user_id).await;
            }
        }
    }
    DEFAULT_TENANT_ID.to_string()
}

/// 解析请求认证用户（HTTP 管理面端点用，如 `/api/v1/db/*`）。
///
/// 返回 `(user_id, username, role, tenant_id)`。任一校验失败（缺失/无效/
/// 过期/非 access 类型）→ `ApiError::Unauthorized`。
///
/// 角色校验由调用方执行（只读 admin/viewer；写操作仅 admin）。
pub async fn resolve_request_user(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    headers: &HeaderMap,
) -> Result<(String, String, String, String), ApiError> {
    let token = extract_bearer_token(headers).ok_or(ApiError::Unauthorized {
        message: "缺少认证信息".to_string(),
    })?;
    let (user_id, username, exp) = decode_token(&token).ok_or(ApiError::Unauthorized {
        message: "无效的认证令牌".to_string(),
    })?;
    if is_token_expired(exp) {
        return Err(ApiError::Unauthorized {
            message: "认证令牌已过期".to_string(),
        });
    }
    // 必须是 access token（拒绝 refresh token 用于管理面）
    if !is_access_token(&token) {
        return Err(ApiError::Unauthorized {
            message: "无效的认证令牌".to_string(),
        });
    }
    // token 校验场景无 tenant scope，用 username 跨租户查询（token 自带 username）
    let user = find_user_by_username(store, &username)
        .await
        .or_else(|| default_users().into_iter().find(|u| u.id == user_id))
        .ok_or(ApiError::Unauthorized {
            message: "用户不存在".to_string(),
        })?;
    Ok((user.id, user.username, user.role, user.tenant_id))
}

/// 按 user_id 解析其归属的租户 ID（WS 路径用，与 HTTP 路径同源）。
///
/// store 优先查持久化用户的 tenant_id；未命中回退内置 admin（兼容无 store 测试），
/// 再不命中回退 [`DEFAULT_TENANT_ID`]。
///
/// WebSocket 握手链路只透传 `user_id`（受 `PipelineDispatcher` trait 签名约束），
/// 无法携带 `HeaderMap`。本函数让 WS 入站分发器能用同一套用户表查出真正的
/// `tenant_id`，避免把 `user_id` 误当 `tenant_id` 导致读写 tenant 失配。
pub async fn resolve_tenant_id_by_user(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    user_id: &str,
) -> String {
    find_user_by_id(store, user_id)
        .await
        .map(|u| u.tenant_id)
        .unwrap_or_else(|| DEFAULT_TENANT_ID.to_string())
}

/// 从 Authorization 头提取 bearer token。
pub fn extract_bearer_token(headers: &HeaderMap) -> Option<String> {
    let auth_header = headers.get("authorization")?;
    let auth_str = auth_header.to_str().ok()?;
    auth_str.strip_prefix("Bearer ").map(|s| s.to_string())
}

// ─── 用户查找 ────────────────────────────────────────────────────────
//
// 持久化优先：先查 store（真实注册用户），未命中再回退内置 admin（兼容
// AppState::new() 无 store 的测试场景 + 首次启动未播种的情况）。
// get_user_by_id 走 task_local tenant 隔离，故需在调用方建立 tenant scope；
// 但 login/register 时还不知道 tenant，get_user_by_username 跨租户全局查。

/// 按凭据查用户（登录用）。store 优先，密码明文比对。
pub async fn find_user_by_credentials(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    username: &str,
    password: &str,
) -> Option<BuiltInUser> {
    if let Some(store) = store {
        if let Ok(Some(u)) = store.get_user_by_username(username).await {
            if u.password == password {
                return Some(BuiltInUser::from(&u));
            }
            return None; // 用户名命中但密码不符，不再回退内置（避免 admin 绕过）
        }
    }
    // 回退内置 admin（无 store 或 DB 无此用户名）
    default_users()
        .into_iter()
        .find(|u| u.username == username && u.password == password)
}

/// 按 user_id 查用户（token 解析 / WS 握手用）。
/// 注意：get_user_by_id 按 task_local tenant 隔离，调用方需在正确租户 scope 内。
/// 跨租户场景下若 task_local 未命中，会回退内置 admin 兜底。
pub async fn find_user_by_id(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    id: &str,
) -> Option<BuiltInUser> {
    if let Some(store) = store {
        if let Ok(Some(u)) = store.get_user_by_id(id).await {
            return Some(BuiltInUser::from(&u));
        }
    }
    default_users().into_iter().find(|u| u.id == id)
}

/// 按用户名查用户（token 校验场景用，跨租户全局查询，不校验密码）。
/// store 优先，未命中回退内置 admin。
pub async fn find_user_by_username(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    username: &str,
) -> Option<BuiltInUser> {
    if let Some(store) = store {
        if let Ok(Some(u)) = store.get_user_by_username(username).await {
            return Some(BuiltInUser::from(&u));
        }
    }
    default_users().into_iter().find(|u| u.username == username)
}

// ─── WS 握手鉴权（task_11 P2：session crate 复用） ────────────────

/// 已校验的用户身份（WS 握手鉴权出口）。
///
/// 与 [`BuiltInUser`] 的区别：不含密码等敏感字段，仅供 session crate
/// 注册连接 / 路由使用。token 格式见模块头部注释。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifiedUser {
    pub user_id: String,
    pub username: String,
    pub tenant_id: String,
}

/// 校验 access token（供 WS 握手从 `?token=` 查询参数鉴权）。
///
/// 返回 `Some(VerifiedUser)` 当且仅当：token 能解码 + 类型为 access +
/// 未过期。tenant_id 在握手阶段无法查 store（同步上下文），此处仅对内置
/// admin 回填真实 tenant；动态用户的 tenant_id 在 `dispatch_user_input`
///（已有 store 的 async 上下文）里由 `resolve_tenant_id_by_user` 权威解析。
/// 任一校验失败返回 `None`（调用方按 ADR §7.2 以 4001 拒绝握手）。
pub fn verify_access_token(token: &str) -> Option<VerifiedUser> {
    let (user_id, username, exp) = decode_token(token)?;
    if is_token_expired(exp) {
        return None;
    }
    // payload 必须是 access token（拒绝 refresh token 用于 WS 鉴权）
    if !is_access_token(token) {
        return None;
    }
    // tenant_id：内置 admin 直接回填；动态用户先留空（dispatch 时权威解析）。
    // user_id/username 已从 token 解出，握手注册连接用它们即可。
    let tenant_id = default_users()
        .into_iter()
        .find(|u| u.id == user_id)
        .map(|u| u.tenant_id)
        .unwrap_or_default();
    Some(VerifiedUser {
        user_id,
        username,
        tenant_id,
    })
}

/// 判断 token 是否为 access 类型（payload 首段为 "access"）。
pub fn is_access_token(token: &str) -> bool {
    use base64::Engine;
    let decoded = base64::engine::general_purpose::STANDARD_NO_PAD
        .decode(token.trim())
        .ok();
    let payload = decoded.and_then(|b| String::from_utf8(b).ok());
    match payload {
        Some(s) => s.split(':').next() == Some("access"),
        None => false,
    }
}
