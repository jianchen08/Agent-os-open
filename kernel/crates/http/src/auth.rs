//! 请求用户解析 + token 编解码（api / db-admin 管理面共用，鉴权单一来源）。
//!
//! db-admin 独立 crate 后无法依赖 api（循环依赖），但 `/api/v1/db/*` 与 api 的
//! `write_surface_auth` 共用同一套用户解析逻辑。此处为唯一实现，
//! api / db-admin 均直接从本模块引入，无转发层。
//!
//! ## Token 格式（HMAC 签名，D12）
//!
//! `base64({type}:{user_id}:{username}:{exp_unix_secs}:{jti}:{pwv})`
//! `.` `base64(HMAC-SHA256(payload, secret))`
//!
//! - `jti`：token 实例 id（uuid），refresh 单次轮换的消费判定与登出吊销用；
//! - `pwv`：口令绑定段——当前口令哈希的 sha256 前 16 个 hex 字符。改密后哈希
//!   变化 → 持有旧 token 的所有会话（access + refresh）立即失效（持久吊销，
//!   跨重启有效）；校验点在能查到用户记录的路径（`resolve_request_user` /
//!   me / refresh）。WS 握手（`verify_access_token`）为同步上下文查不到
//!   store，只做签名/过期/类型校验，改密吊销由重连后的 token 刷新链路承接。
//!
//! 服务端密钥经环境变量 `AGENTOS_TOKEN_SECRET` 注入；未设置时每进程生成随机
//! 密钥（重启即全部 token 失效）并打 warn。旧格式（无签名 base64）token
//! 一律拒绝——签发入口全部在内核，刷新即得新 token，无兼容期（内部一刀切）。
//!
//! ## 口令存储（D1）
//!
//! user 表 `password` 列存 argon2id 哈希（PHC 字符串）。校验只经
//! [`verify_password`]；非哈希形态的存量值校验恒失败（fail-closed，明文
//! 登录拒绝），由内核启动迁移统一哈希化回写。

use std::sync::OnceLock;

use argon2::password_hash::{PasswordHash, SaltString};
use argon2::{Argon2, PasswordHasher, PasswordVerifier};
use axum::http::HeaderMap;
use hmac::{Hmac, Mac};
use rand_core::OsRng;
use sha2::{Digest, Sha256};

use crate::error::ApiError;

// ─── 常量 ────────────────────────────────────────────────────────────

/// 内置默认租户 ID——所有内置用户归属此租户。
pub const DEFAULT_TENANT_ID: &str = "default";

/// token 服务端密钥的环境变量名（与 AGENTOS_ADMIN_PASSWORD 同为认证链注入点）。
pub const TOKEN_SECRET_ENV: &str = "AGENTOS_TOKEN_SECRET";

/// 内置脚手架用户表（仅无 store 的测试场景兜底，不承载可登录凭据）。
///
/// `password` 恒为空串 = 不可登录（fail-closed：无持久化即无凭据校验依据，
/// 登录必须走 store）。持有 id/username/role/tenant 供 token 校验回退、
/// 租户回填与注册重名检查等非凭据路径使用。
pub fn default_users() -> Vec<BuiltInUser> {
    vec![BuiltInUser {
        id: "00000000-0000-0000-0000-000000000001".to_string(),
        username: "admin".to_string(),
        password: String::new(),
        email: "admin@agentos.dev".to_string(),
        role: "admin".to_string(),
        tenant_id: DEFAULT_TENANT_ID.to_string(),
        created_at: "2025-01-01T00:00:00Z".to_string(),
        must_change_password: false,
    }]
}

// ─── 口令哈希（argon2id，单一实现） ─────────────────────────────────

/// 判断存储值是否已是 argon2 哈希形态（PHC 字符串以 `$argon2` 起始）。
pub fn is_password_hash(stored: &str) -> bool {
    stored.starts_with("$argon2")
}

/// 生成 argon2id 哈希（随机盐，PHC 字符串）。参数用库默认（Argon2id v19）。
pub fn hash_password(plain: &str) -> Result<String, String> {
    let salt = SaltString::generate(&mut OsRng);
    Argon2::default()
        .hash_password(plain.as_bytes(), &salt)
        .map(|h| h.to_string())
        .map_err(|e| format!("口令哈希失败: {e}"))
}

/// 校验口令与存储哈希是否匹配。
///
/// 存储值非 argon2 哈希形态（如历史明文行）恒为 false——不提供明文比对
/// 回退（fail-closed），明文行只能经启动迁移哈希化后恢复登录。
pub fn verify_password(plain: &str, stored_hash: &str) -> bool {
    let parsed = match PasswordHash::new(stored_hash) {
        Ok(h) => h,
        Err(_) => return false,
    };
    Argon2::default()
        .verify_password(plain.as_bytes(), &parsed)
        .is_ok()
}

// ─── 数据结构 ────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct BuiltInUser {
    pub id: String,
    pub username: String,
    /// 口令（argon2id 哈希）；内置脚手架表为空串（不可登录）。
    pub password: String,
    pub email: String,
    pub role: String,
    /// 用户归属的租户 ID（多租户隔离）。
    pub tenant_id: String,
    pub created_at: String,
    /// 首登强制改密标记（D1-4）。
    pub must_change_password: bool,
}

impl From<&agentos_core::types::UserRecord> for BuiltInUser {
    /// 从持久化 UserRecord 构造 BuiltInUser（password 字段承载哈希）。
    fn from(u: &agentos_core::types::UserRecord) -> Self {
        Self {
            id: u.user_id.clone(),
            username: u.username.clone(),
            password: u.password.clone(),
            email: u.email.clone().unwrap_or_default(),
            role: u.role.clone(),
            tenant_id: u.tenant_id.clone(),
            created_at: u.created_at.clone(),
            must_change_password: u.must_change_password,
        }
    }
}

// ─── Token 服务端密钥 ────────────────────────────────────────────────

/// token 签名密钥（进程内单次初始化）：`AGENTOS_TOKEN_SECRET` 优先；
/// 未设置/为空时生成进程随机密钥并 warn（重启即全量 token 失效，生产部署
/// 应显式设置以跨重启保持会话）。
fn token_secret() -> &'static str {
    static SECRET: OnceLock<String> = OnceLock::new();
    SECRET.get_or_init(|| {
        match std::env::var(TOKEN_SECRET_ENV) {
            Ok(s) if !s.trim().is_empty() => {
                if s.trim().len() < 32 {
                    tracing::warn!(
                        "{TOKEN_SECRET_ENV} 长度不足 32 字符，签名强度受限；建议使用 ≥32 字符随机值"
                    );
                }
                s
            }
            _ => {
                let generated = format!(
                    "{}{}",
                    uuid::Uuid::new_v4().simple(),
                    uuid::Uuid::new_v4().simple()
                );
                tracing::warn!(
                    "{TOKEN_SECRET_ENV} 未设置：使用进程随机密钥签名 token（重启后全部 token 失效）；生产部署请设置该环境变量"
                );
                generated
            }
        }
    })
}

fn hmac_sha256(payload: &str) -> Vec<u8> {
    let mut mac =
        Hmac::<Sha256>::new_from_slice(token_secret().as_bytes()).expect("HMAC 接受任意长度密钥");
    mac.update(payload.as_bytes());
    mac.finalize().into_bytes().to_vec()
}

/// 口令绑定段：当前口令哈希的 sha256 前 16 个 hex 字符。
/// 改密 → 哈希变化 → 旧 token 的 pwv 失配 → 全会话吊销（持久，跨重启）。
pub fn password_binding(password_hash: &str) -> String {
    use std::fmt::Write as _;
    let digest = Sha256::digest(password_hash.as_bytes());
    // 前 16 个 hex 字符 = digest 前 8 字节，逐字节写入免整串分配再截断。
    let mut hex = String::with_capacity(16);
    for b in digest.iter().take(8) {
        write!(hex, "{b:02x}").expect("写入 String 不会失败");
    }
    hex
}

// ─── Token 编解码 ────────────────────────────────────────────────────

/// 注册用户名字符集白名单：`[A-Za-z0-9._@-]`，长度 1..=64。
///
/// username 原样进入 token 载荷（`:` 分隔六段，见模块头部格式），载荷段
/// 无转义——username 混入 `:` 令 exp 段错位，该账户签发的所有 token 恒
/// 解码失败（注册即自锁）。校验在用户创建入口调用（register 端点是
/// 唯一接受外部用户名的入口；查证/登录路径不拦）。
pub fn is_valid_username(username: &str) -> bool {
    !username.is_empty()
        && username.len() <= 64
        && username
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-' | '@'))
}

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

/// 签发 token（HMAC 签名 + 随机 jti + 口令绑定段）。
pub fn encode_token(token_type: TokenType, user: &BuiltInUser, ttl_secs: u64) -> String {
    let exp = chrono::Utc::now().timestamp() as u64 + ttl_secs;
    let payload = format!(
        "{}:{}:{}:{}:{}:{}",
        token_type.prefix(),
        user.id,
        user.username,
        exp,
        uuid::Uuid::new_v4().simple(),
        password_binding(&user.password),
    );
    use base64::Engine;
    // URL-safe 字母表（-_ 不含 +/）：token 经 WS `?token=` 查询参数透传，
    // 标准字母表的 `+` 会被表单解码成空格导致签名失配。
    let signature = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(hmac_sha256(&payload));
    format!(
        "{}.{}",
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(payload.as_bytes()),
        signature
    )
}

/// 解码后的 token 载荷。type 前缀随同一次解码一并取出——access 门禁
/// 经 [`DecodedToken::is_access`] 判定，不再对同一 token 二次解码。
/// 签名校验失败（含旧格式无签名 token、篡改载荷）→ None。
#[derive(Debug, Clone, PartialEq)]
pub struct DecodedToken {
    /// 载荷首段原样字符串（"access"/"refresh"/其他）。
    pub token_type: String,
    pub user_id: String,
    pub username: String,
    pub exp: u64,
    /// token 实例 id（refresh 单次轮换消费判定 / 登出吊销用）。
    pub jti: String,
    /// 口令绑定段（与用户当前口令哈希比对，改密即失配）。
    pub pwv: String,
}

impl DecodedToken {
    /// access 类型判定：首段前缀为 "access"；refresh 及未知前缀均否。
    pub fn is_access(&self) -> bool {
        self.token_type == TokenType::Access.prefix()
    }

    /// 口令绑定段与用户当前口令哈希是否匹配（改密吊销的判定点）。
    pub fn password_binding_matches(&self, current_password_hash: &str) -> bool {
        self.pwv == password_binding(current_password_hash)
    }
}

pub fn decode_token(token: &str) -> Option<DecodedToken> {
    use base64::Engine;
    let engine = base64::engine::general_purpose::URL_SAFE_NO_PAD;
    let (payload_b64, sig_b64) = token.trim().split_once('.')?;
    let payload = String::from_utf8(engine.decode(payload_b64).ok()?).ok()?;
    let signature = engine.decode(sig_b64).ok()?;
    // 签名校验（constant-time）：无签名旧 token / 篡改载荷在此一并拒绝。
    let mut mac =
        Hmac::<Sha256>::new_from_slice(token_secret().as_bytes()).expect("HMAC 接受任意长度密钥");
    mac.update(payload.as_bytes());
    mac.verify_slice(&signature).ok()?;

    let parts: Vec<&str> = payload.splitn(6, ':').collect();
    if parts.len() != 6 {
        return None;
    }
    let exp: u64 = parts[3].parse().ok()?;
    if parts[4].is_empty() || parts[5].is_empty() {
        return None;
    }
    Some(DecodedToken {
        token_type: parts[0].to_string(),
        user_id: parts[1].to_string(),
        username: parts[2].to_string(),
        exp,
        jti: parts[4].to_string(),
        pwv: parts[5].to_string(),
    })
}

pub fn is_token_expired(exp: u64) -> bool {
    chrono::Utc::now().timestamp() as u64 >= exp
}

/// 解析请求归属的租户 ID（HTTP 路径用，async）。
///
/// 当前 token 载荷尚未包含 tenant 段，因此本函数在 token 合法时查 store /
/// 内置用户表得到 `tenant_id`，否则回退到 [`DEFAULT_TENANT_ID`]。
///
/// TODO(多租户): 签发面（agentos-api auth）token 载荷扩展出 tenant claim 后，
/// 本函数改为优先从 token 解析；该变更未发生前，store/内置用户表查询是
/// 现行权威路径。
pub async fn resolve_request_tenant_id(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    headers: &HeaderMap,
) -> String {
    if let Some(token) = extract_bearer_token(headers) {
        if let Some(t) = decode_token(&token) {
            if !is_token_expired(t.exp) {
                return resolve_tenant_id_by_user(store, &t.user_id).await;
            }
        }
    }
    DEFAULT_TENANT_ID.to_string()
}

/// 解析请求认证用户（HTTP 管理面端点用，如 `/api/v1/db/*`）。
///
/// 返回 `(user_id, username, role, tenant_id)`。任一校验失败（缺失/无效/
/// 过期/非 access 类型/口令绑定失配/用户不存在）→ `ApiError::Unauthorized`。
///
/// 角色校验由调用方执行（只读 admin/viewer；写操作仅 admin）。
pub async fn resolve_request_user(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    headers: &HeaderMap,
) -> Result<(String, String, String, String), ApiError> {
    let (_t, user) = authenticate_bearer_user(store, headers, true).await?;
    Ok((user.id, user.username, user.role, user.tenant_id))
}

/// Bearer token 认证链（api `/auth/me` 与 http 管理面 [`resolve_request_user`]
/// 共享，差异点参数化）：提取 → 解码 → 过期 → `require_access` 时拒绝
/// refresh token（管理面仅收 access）→ 用户存在（K4 fail-closed）→ 口令绑定
/// （改密吊销）。任一步失败 → `ApiError::Unauthorized`，文案即对外的用户提示。
/// 返回（解码 token, 用户）供调用方按各自响应形状取用。
pub async fn authenticate_bearer_user(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    headers: &HeaderMap,
    require_access: bool,
) -> Result<(DecodedToken, BuiltInUser), ApiError> {
    let token = extract_bearer_token(headers).ok_or(ApiError::Unauthorized {
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
    if require_access && !t.is_access() {
        return Err(ApiError::Unauthorized {
            message: "无效的认证令牌".to_string(),
        });
    }
    // token 校验场景无 tenant scope，用 username 跨租户查询（token 自带 username）。
    // find_user_by_username 自身 fail-closed（store 存在未命中不回退内置表），
    // 此处不得再叠 or_else 内置回退——否则换库后已签发 token 依旧把"用户已
    // 不存在"伪装成合法内置用户（K4，对齐 find_user_by_credentials 2026-08-19 裁决）。
    let user = find_user_by_username(store, &t.username)
        .await
        .ok_or(ApiError::Unauthorized {
            message: "用户不存在".to_string(),
        })?;
    // 口令绑定校验：改密后的旧 token 一律拒绝（全会话持久吊销）。
    if !t.password_binding_matches(&user.password) {
        return Err(ApiError::Unauthorized {
            message: "认证令牌已失效（口令已变更）".to_string(),
        });
    }
    Ok((t, user))
}

/// 按 user_id 解析其归属的租户 ID（WS 路径用，与 HTTP 路径同源）。
///
/// store 优先查持久化用户的 tenant_id；store 存在但解析不出（用户已删除 /
/// 存储故障）时**不再静默**——warn 审计标记后回退 [`DEFAULT_TENANT_ID`]
/// （K8：归因可见的降级，防读写串租户无痕发生）。仅无 store（测试场景）
/// 走内置 admin 表，再不命中回退默认租户。
///
/// WebSocket 握手链路只透传 `user_id`（受 `PipelineDispatcher` trait 签名约束），
/// 无法携带 `HeaderMap`。本函数让 WS 入站分发器能用同一套用户表查出真正的
/// `tenant_id`，避免把 `user_id` 误当 `tenant_id` 导致读写 tenant 失配。
pub async fn resolve_tenant_id_by_user(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    user_id: &str,
) -> String {
    match find_user_by_id(store, user_id).await {
        Some(u) => u.tenant_id,
        None => {
            if store.is_some() {
                // K8 审计标记：store 在而用户解析不出（get_user_by_id 按 task_local
                // tenant 隔离，跨租户/已删除用户在此不可见）。降级为 default，
                // 但必须留 warn 可审计。
                tracing::warn!(
                    target: "auth-tenant-degrade",
                    user_id = %user_id,
                    fallback_tenant = DEFAULT_TENANT_ID,
                    "用户→租户解析失败，降级 default 租户（用户已删除/跨租户不可见/存储故障；K8 审计标记）"
                );
            }
            DEFAULT_TENANT_ID.to_string()
        }
    }
}

/// 从 Authorization 头提取 bearer token。
pub fn extract_bearer_token(headers: &HeaderMap) -> Option<String> {
    let auth_header = headers.get("authorization")?;
    let auth_str = auth_header.to_str().ok()?;
    auth_str.strip_prefix("Bearer ").map(|s| s.to_string())
}

// ─── 用户查找 ────────────────────────────────────────────────────────
//
// 持久化优先：先查 store（真实注册用户）。store 存在而未命中不回退内置
// （防换库/清库后硬编码 admin 后门复活）；仅无 store（AppState::new() 测试
// 场景）走内置脚手架表（不可登录，仅供非凭据路径）。
// get_user_by_id 走 task_local tenant 隔离，故需在调用方建立 tenant scope；
// 但 login/register 时还不知道 tenant，get_user_by_username 跨租户全局查。

/// 按凭据查用户（登录用）。store 优先，口令 argon2 校验。
///
/// store 存在但用户名未命中时**不回退**内置表：真实内核启动即把 admin 播种
/// 入库（agentos-kernel main），查不到即凭据不属于本实例。口令校验只认
/// argon2 哈希（历史明文行恒失败，fail-closed）。
/// **无 store 恒不可登录**（无持久化即无凭据校验依据，脚手架表不承载口令）。
pub async fn find_user_by_credentials(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    username: &str,
    password: &str,
) -> Option<BuiltInUser> {
    let store = store?;
    if let Ok(Some(u)) = store.get_user_by_username(username).await {
        if verify_password(password, &u.password) {
            return Some(BuiltInUser::from(&u));
        }
        return None; // 用户名命中但口令不符（含明文存量行），不回退内置
    }
    None // DB 无此用户名：不回退内置凭据
}

/// 按 user_id 查用户（token 解析 / WS 握手用）。
///
/// store 存在但未命中（含 get_user_by_id 按租户隔离导致的跨租户不可见）**不回退**
/// 内置 admin：换库/清库后按旧 token 的 user_id 命中硬编码表 = 已删除用户复活
/// （K4，对齐 find_user_by_credentials）。仅无 store（测试场景）走内置表。
pub async fn find_user_by_id(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    id: &str,
) -> Option<BuiltInUser> {
    if let Some(store) = store {
        if let Ok(Some(u)) = store.get_user_by_id(id).await {
            return Some(BuiltInUser::from(&u));
        }
        return None; // store 在而未命中：不回退内置（防硬编码后门复活）
    }
    // 无 store（测试/无持久化场景）：内置脚手架表
    default_users().into_iter().find(|u| u.id == id)
}

/// 按用户名查用户（token 校验场景用，跨租户全局查询，不校验密码）。
///
/// store 存在但未命中**不回退**内置 admin（K4，对齐 find_user_by_credentials：
/// token 校验路径与登录路径同一裁决——查不到即不属于本实例）。
/// 仅无 store（测试场景）走内置表。
pub async fn find_user_by_username(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    username: &str,
) -> Option<BuiltInUser> {
    if let Some(store) = store {
        if let Ok(Some(u)) = store.get_user_by_username(username).await {
            return Some(BuiltInUser::from(&u));
        }
        return None; // store 在而未命中：不回退内置（防硬编码后门复活）
    }
    // 无 store（测试/无持久化场景）：内置脚手架表
    default_users().into_iter().find(|u| u.username == username)
}

// ─── WS 握手鉴权（task_11 P2：session crate 复用） ────────────────

/// 已校验的用户身份（WS 握手鉴权出口）。
///
/// 与 [`BuiltInUser`] 的区别：不含口令等敏感字段，仅供 session crate
/// 注册连接 / 路由使用。token 格式见模块头部注释。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifiedUser {
    pub user_id: String,
    pub username: String,
    pub tenant_id: String,
}

/// 校验 access token（供 WS 握手从 `?token=` 查询参数鉴权）。
///
/// 返回 `Some(VerifiedUser)` 当且仅当：token 签名有效 + 未过期 + access 类型。
/// 同步上下文查不到 store，用户存在性与口令绑定（改密吊销）不在握手判定——
/// 前者由 dispatch 时 `resolve_tenant_id_by_user` 权威解析，后者由握手后
/// 的 token 刷新链路（refresh/me 均做口令绑定校验）承接。任一校验失败返回
/// `None`（调用方按 ADR §7.2 以 4001 拒绝握手）。
pub fn verify_access_token(token: &str) -> Option<VerifiedUser> {
    let t = decode_token(token)?;
    if is_token_expired(t.exp) {
        return None;
    }
    // payload 必须是 access token（拒绝 refresh token 用于 WS 鉴权）
    if !t.is_access() {
        return None;
    }
    // tenant_id：内置 admin 直接回填；动态用户先留空（dispatch 时权威解析）。
    // user_id/username 已从 token 解出，握手注册连接用它们即可。
    let tenant_id = default_users()
        .into_iter()
        .find(|u| u.id == t.user_id)
        .map(|u| u.tenant_id)
        .unwrap_or_default();
    Some(VerifiedUser {
        user_id: t.user_id,
        username: t.username,
        tenant_id,
    })
}

// ─── 行为测试（D1/D12 认证链） ───────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// 构造带 Bearer 认证头的 HeaderMap（多处用例共用的测试夹具）。
    fn mk_headers(token: &str) -> HeaderMap {
        let mut h = HeaderMap::new();
        h.insert("authorization", format!("Bearer {token}").parse().unwrap());
        h
    }

    fn test_user(password: &str) -> BuiltInUser {
        BuiltInUser {
            id: "u-1".to_string(),
            username: "alice".to_string(),
            password: password.to_string(),
            email: "a@t.com".to_string(),
            role: "user".to_string(),
            tenant_id: DEFAULT_TENANT_ID.to_string(),
            created_at: "2026-01-01T00:00:00Z".to_string(),
            must_change_password: false,
        }
    }

    // ── 口令哈希（D1-2）──

    #[test]
    fn hash_password_produces_argon2_hash_and_verifies() {
        let h1 = hash_password("correct horse").unwrap();
        let h2 = hash_password("correct horse").unwrap();
        assert!(is_password_hash(&h1), "哈希应为 $argon2 PHC 形态: {h1}");
        // 随机盐：同口令两次哈希不同（性质断言）
        assert_ne!(h1, h2, "随机盐下同口令哈希必须不同");
        assert!(verify_password("correct horse", &h1));
        assert!(verify_password("correct horse", &h2));
    }

    #[test]
    fn verify_password_rejects_wrong_and_plaintext_storage() {
        let h = hash_password("right-pass").unwrap();
        // 多组错误输入：错口令/空串/大小写差异——恒拒绝
        for wrong in ["wrong-pass", "", "Right-Pass", "right-pass "] {
            assert!(!verify_password(wrong, &h), "错误口令 {wrong:?} 必须拒绝");
        }
        // 存储值为明文（迁移前的存量形态）→ 恒失败（fail-closed，明文登录拒绝）
        assert!(!verify_password("right-pass", "right-pass"));
        assert!(!is_password_hash("right-pass"));
    }

    // ── token 签名（D12-5）──

    #[test]
    fn token_roundtrip_preserves_payload() {
        let user = test_user(&hash_password("pw-1").unwrap());
        let token = encode_token(TokenType::Access, &user, 3600);
        let t = decode_token(&token).expect("签名 token 应可解码");
        assert_eq!(t.token_type, "access");
        assert_eq!(t.user_id, "u-1");
        assert_eq!(t.username, "alice");
        assert!(t.exp > chrono::Utc::now().timestamp() as u64);
        assert!(t.is_access());
        assert!(t.password_binding_matches(&user.password));
    }

    #[test]
    fn tampered_token_rejected() {
        let user = test_user(&hash_password("pw-2").unwrap());
        let token = encode_token(TokenType::Access, &user, 3600);
        // 篡改方式一：换用户名（payload 变、签名沿用）
        let (payload_b64, sig) = token.split_once('.').unwrap();
        use base64::Engine;
        let mut payload = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .decode(payload_b64)
            .unwrap();
        let mutated = String::from_utf8(payload.clone())
            .unwrap()
            .replace("alice", "mallory");
        assert_ne!(
            mutated.as_bytes(),
            payload.as_slice(),
            "前置：用户名确实被改"
        );
        payload = mutated.into_bytes();
        let forged = format!(
            "{}.{sig}",
            base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(&payload)
        );
        assert!(decode_token(&forged).is_none(), "篡改载荷的 token 必须拒绝");
        // 篡改方式二：签名段改一字节
        let mut sig_bytes = sig.to_string().into_bytes();
        let last = sig_bytes.last_mut().unwrap();
        *last = if *last == b'A' { b'B' } else { b'A' };
        let forged_sig = format!("{payload_b64}.{}", String::from_utf8(sig_bytes).unwrap());
        assert!(
            decode_token(&forged_sig).is_none(),
            "篡改签名的 token 必须拒绝"
        );
    }

    #[test]
    fn unsigned_legacy_token_rejected() {
        // 旧格式：base64({type}:{user_id}:{username}:{exp})，无签名段。
        use base64::Engine;
        let exp = chrono::Utc::now().timestamp() as u64 + 3600;
        let legacy_payload = format!("access:00000000-0000-0000-0000-000000000001:admin:{exp}");
        let legacy = base64::engine::general_purpose::STANDARD_NO_PAD.encode(legacy_payload);
        assert!(decode_token(&legacy).is_none(), "无签名旧 token 一律拒绝");
        assert!(
            verify_access_token(&legacy).is_none(),
            "WS 握手同样拒绝旧 token"
        );
        // 带签名段但签名不符（自己 base64 一段当签名）→ 拒绝
        let fake_sig = base64::engine::general_purpose::STANDARD_NO_PAD.encode(b"fake-signature");
        let signed_looking = format!("{legacy}.{fake_sig}");
        assert!(decode_token(&signed_looking).is_none());
    }

    #[test]
    fn refresh_token_type_is_not_access() {
        let user = test_user(&hash_password("pw-3").unwrap());
        let refresh = encode_token(TokenType::Refresh, &user, 3600);
        let t = decode_token(&refresh).unwrap();
        assert!(!t.is_access());
        assert!(
            verify_access_token(&refresh).is_none(),
            "refresh token 不得过 WS 鉴权"
        );
    }

    #[test]
    fn password_change_invalidates_binding() {
        let old_hash = hash_password("old-pass").unwrap();
        let user = test_user(&old_hash);
        let token = encode_token(TokenType::Access, &user, 3600);
        let t = decode_token(&token).unwrap();
        let new_hash = hash_password("new-pass").unwrap();
        assert!(t.password_binding_matches(&old_hash));
        assert!(
            !t.password_binding_matches(&new_hash),
            "改密后旧 token 的口令绑定段必须失配（全会话吊销依据）"
        );
    }

    #[test]
    fn tokens_have_unique_jti() {
        let user = test_user(&hash_password("pw-4").unwrap());
        let t1 = decode_token(&encode_token(TokenType::Refresh, &user, 3600)).unwrap();
        let t2 = decode_token(&encode_token(TokenType::Refresh, &user, 3600)).unwrap();
        assert_ne!(
            t1.jti, t2.jti,
            "每次签发的 jti 必须唯一（单次轮换判定依据）"
        );
    }

    #[test]
    fn decode_rejects_garbage() {
        assert!(decode_token("not-a-token").is_none());
        assert!(decode_token("").is_none());
        assert!(
            decode_token("a.b").is_none(),
            "合法 base64 但载荷非法应拒绝"
        );
    }

    // ── username 字符集白名单（token 载荷分隔符安全）──

    #[test]
    fn username_charset_whitelist_boundaries() {
        // 允许：各字符类组合与长度边界
        assert!(is_valid_username("admin"));
        assert!(is_valid_username("user.name@x-y_01"));
        assert!(is_valid_username(&"a".repeat(64)), "长度上界 64 应放行");
        assert!(!is_valid_username(&"a".repeat(65)), "超长应拒绝");
        assert!(!is_valid_username(""), "空用户名应拒绝");
        // 拒绝：载荷分隔符/空白/控制字符/非 ASCII
        for bad in ["a:b", "has space", "用户名", "line\nbreak", "a\tb"] {
            assert!(!is_valid_username(bad), "非法用户名 {bad:?} 必须拒绝");
        }
    }
    // ── 共享认证链 authenticate_bearer_user（require_access 参数化）──

    /// 差异点契约：refresh token 在管理面形态（require_access=true）被拒，
    /// /auth/me 形态（false）放行；access token 两侧皆放行；缺失/垃圾 token 恒拒。
    #[tokio::test]
    async fn authenticate_bearer_user_require_access_gates_refresh_token() {
        // 无 store 场景走内置表（脚手架 admin，空口令哈希参与 pwv 绑定）
        let user = default_users().into_iter().next().unwrap();
        let store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>> = None;

        let mk_headers = |token: &str| {
            let mut h = HeaderMap::new();
            h.insert("authorization", format!("Bearer {token}").parse().unwrap());
            h
        };

        // refresh token：管理面拒，观档端点放行（参数化差异点）
        let refresh = mk_headers(&encode_token(TokenType::Refresh, &user, 3600));
        assert!(
            authenticate_bearer_user(store, &refresh, true)
                .await
                .is_err(),
            "refresh token 不得进管理面"
        );
        assert!(
            authenticate_bearer_user(store, &refresh, false)
                .await
                .is_ok(),
            "非管理面端点（/auth/me 形态）允许 refresh token"
        );

        // access token：两侧皆放行
        let access = mk_headers(&encode_token(TokenType::Access, &user, 3600));
        assert!(authenticate_bearer_user(store, &access, true).await.is_ok());
        assert!(authenticate_bearer_user(store, &access, false)
            .await
            .is_ok());

        // 缺失认证信息 / 垃圾 token：恒拒（两组反例输入）
        let empty = HeaderMap::new();
        assert!(authenticate_bearer_user(store, &empty, true).await.is_err());
        let junk = mk_headers("not-a-token");
        assert!(authenticate_bearer_user(store, &junk, false).await.is_err());
    }

    // ── 载荷结构畸形：段数不足 / 空 jti / 空 pwv ──
    //
    // decode_token 在校验签名后按 `:` 六段解析。这些用例用真实密钥重签
    // 一份畸形载荷，使签名校验通过、只有结构校验拦截——锁"结构门"本身，
    // 而非签名门（签名门另有专测）。

    /// 用当前进程密钥对任意载荷重签（构造"签名合法但结构畸形"的 token）。
    fn sign_payload(payload: &str) -> String {
        use base64::Engine;
        let engine = base64::engine::general_purpose::URL_SAFE_NO_PAD;
        let sig = engine.encode(hmac_sha256(payload));
        format!("{}.{}", engine.encode(payload.as_bytes()), sig)
    }

    #[test]
    fn decode_requires_six_segments() {
        // 段数不足（5 段，缺 pwv）必须拒绝
        assert!(
            decode_token(&sign_payload("access:u-1:alice:9999999999:jti-1")).is_none(),
            "5 段载荷必须拒绝"
        );
        // splitn(6) 语义：第 6 段吸收剩余 `:` 后的内容——pwv 本身可含分隔符，
        // 段数超过 6 不构成结构错误（签名仍覆盖全载荷）。
        let t = decode_token(&sign_payload(
            "access:u-1:alice:9999999999:jti-1:pwv-1:extra",
        ))
        .expect("第 6 段吸收剩余，应可解析");
        assert_eq!(t.jti, "jti-1");
        assert_eq!(t.pwv, "pwv-1:extra", "pwv 承载剩余段");
    }

    #[test]
    fn decode_rejects_empty_jti_or_pwv() {
        // 空 jti（第 5 段）与空 pwv（第 6 段）：单次轮换/改密吊销依据缺失，必须拒绝
        assert!(
            decode_token(&sign_payload("access:u-1:alice:9999999999::pwv-1")).is_none(),
            "空 jti 必须拒绝"
        );
        assert!(
            decode_token(&sign_payload("access:u-1:alice:9999999999:jti-1:")).is_none(),
            "空 pwv 必须拒绝"
        );
    }

    #[test]
    fn decode_rejects_non_numeric_exp() {
        assert!(
            decode_token(&sign_payload("access:u-1:alice:not-a-number:jti-1:pwv-1")).is_none(),
            "非数字 exp 必须拒绝"
        );
    }

    #[test]
    fn decode_accepts_wellformed_signed_payload() {
        // 对照：同法签名但六段齐全 → 解析成功且各段保真
        let t = decode_token(&sign_payload("refresh:u-9:bob:1234567890:jti-x:pwv-y"))
            .expect("结构合法且签名正确应解析成功");
        assert_eq!(t.token_type, "refresh");
        assert_eq!(t.user_id, "u-9");
        assert_eq!(t.username, "bob");
        assert_eq!(t.exp, 1234567890);
        assert_eq!(t.jti, "jti-x");
        assert_eq!(t.pwv, "pwv-y");
        assert!(!t.is_access(), "refresh 前缀不得判为 access");
    }

    // ── 过期 token 与租户解析降级 ──

    #[test]
    fn expired_token_is_not_accepted_for_tenant_resolution() {
        let user = test_user(&hash_password("pw-exp").unwrap());
        // ttl_secs=0 → exp = now，is_token_expired 判 >= now 即 true
        let expired = encode_token(TokenType::Access, &user, 0);
        let t = decode_token(&expired).expect("结构合法");
        assert!(is_token_expired(t.exp), "ttl=0 的 token 必须视为已过期");
        // verify_access_token 对过期 token 不得放行
        assert!(
            verify_access_token(&expired).is_none(),
            "过期 token 不得过 WS 鉴权"
        );
    }

    /// resolve_request_tenant_id 的四条路径：无头 → default；垃圾 token →
    /// default；合法且未过期 → 按用户解析（无 store 走内置表）；
    /// 合法但过期 → default。
    #[tokio::test]
    async fn resolve_request_tenant_id_paths() {
        let store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>> = None;

        // ① 无 Authorization 头
        assert_eq!(
            resolve_request_tenant_id(store, &HeaderMap::new()).await,
            DEFAULT_TENANT_ID
        );

        // ② 垃圾 token
        let junk = mk_headers("garbage-token");
        assert_eq!(
            resolve_request_tenant_id(store, &junk).await,
            DEFAULT_TENANT_ID
        );

        // ③ 合法未过期：内置表用户 → 其 tenant_id
        let user = default_users().into_iter().next().unwrap();
        let expect_tenant = user.tenant_id.clone();
        let good = mk_headers(&encode_token(TokenType::Access, &user, 3600));
        assert_eq!(
            resolve_request_tenant_id(store, &good).await,
            expect_tenant,
            "合法 token 应按用户解析租户"
        );

        // ④ 合法但过期 → default
        let expired = mk_headers(&encode_token(TokenType::Access, &user, 0));
        assert_eq!(
            resolve_request_tenant_id(store, &expired).await,
            DEFAULT_TENANT_ID,
            "过期 token 不得用于租户解析"
        );
    }

    /// resolve_tenant_id_by_user：无 store 且用户不在内置表 → default
    /// （不 panic，不返回空串）。
    #[tokio::test]
    async fn resolve_tenant_id_by_unknown_user_falls_back_to_default() {
        let store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>> = None;
        assert_eq!(
            resolve_tenant_id_by_user(store, "no-such-user").await,
            DEFAULT_TENANT_ID
        );
        assert_eq!(
            resolve_tenant_id_by_user(store, "").await,
            DEFAULT_TENANT_ID,
            "空 user_id 同样回退默认租户"
        );
    }

    /// extract_bearer_token：仅认 `Bearer ` 前缀（大小写敏感）；无头/非 UTF-8
    /// / 其他 scheme 一律 None。
    #[test]
    fn extract_bearer_token_requires_exact_prefix() {
        assert_eq!(
            extract_bearer_token(&mk_headers("tok-123")).as_deref(),
            Some("tok-123")
        );
        assert!(extract_bearer_token(&HeaderMap::new()).is_none(), "无头");

        // 其他 scheme / 前缀变体（大小写）不得误取
        for value in [
            "Basic tok-123",
            "bearer tok-123",
            "Token tok-123",
            "tok-123",
        ] {
            let mut h = HeaderMap::new();
            h.insert("authorization", value.parse().unwrap());
            assert!(
                extract_bearer_token(&h).is_none(),
                "{value:?} 不应被识别为 bearer token"
            );
        }
    }
}
