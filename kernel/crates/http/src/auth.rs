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
//! 服务端密钥解析序（ADR 2026-10-22-token-secret-persistent-default，用户裁定
//! 2026-10-22「默认不得随机，更强安全由部署者自定」）：`AGENTOS_TOKEN_SECRET`
//! 显式配置 > `AGENTOS_TOKEN_SECRET_EPHEMERAL=1` 显式一次性（重启即全量 token
//! 失效）> 默认持久化——首启在用户空间 `<data>/token_secret.key` 生成落盘，
//! 此后跨重启复用（重启不再掉登录）。持久化仅生产入口 [`init_token_secret`]
//! 启用；未初始化入口（测试/工具二进制）惰性回落一次性密钥、零 IO。旧格式
//! （无签名 base64）token 一律拒绝——签发入口全部在内核，刷新即得新 token，
//! 无兼容期（内部一刀切）。
//!
//! ## 口令存储（D1）
//!
//! user 表 `password` 列存 argon2id 哈希（PHC 字符串）。校验只经
//! [`verify_password`]；非哈希形态的存量值校验恒失败（fail-closed，明文
//! 登录拒绝），由内核启动迁移统一哈希化回写。

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
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

/// 显式选择一次性密钥的环境变量名：置 `1`/`true` 时跳过持久化层，每次启动
/// 生成进程随机密钥（重启即全量 token 失效）。随机是**显式选择**而非默认——
/// 默认必须跨重启稳定（用户裁定 2026-10-22）。
pub const TOKEN_SECRET_EPHEMERAL_ENV: &str = "AGENTOS_TOKEN_SECRET_EPHEMERAL";

/// 持久化密钥文件名（落 `agentos_core::user_space::user_data_dir()` 下；
/// 用户仓 git 化为 ignore-all 白名单制且不含 data/，密钥绝不入任何 git）。
pub const TOKEN_SECRET_FILE: &str = "token_secret.key";

/// 密钥最小长度（字符）：env 显式短值仅告警不拒；持久层短值视为损坏重生成。
const TOKEN_SECRET_MIN_LEN: usize = 32;

/// 密钥解析来源（启动日志分级与测试断言用）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TokenSecretSource {
    /// 部署者显式配置；`too_short` = 长度不足（仅告警不拒绝，尊重显式值）。
    EnvExplicit { too_short: bool },
    /// 部署者显式选择一次性密钥（opt-in，不读不写持久层）。
    EphemeralOptIn,
    /// 复用持久化副本。
    PersistedExisting,
    /// 首次生成并持久化落盘。
    GeneratedAndPersisted,
    /// 持久层不可用/写失败，降级一次性密钥（`reason` 供启动日志定位）。
    EphemeralFallback { reason: String },
}

/// 解析 token 签名密钥（纯函数，IO 仅限 `store` 落点，生成器注入可测）。
///
/// 解析序：env 显式（空白 = 未设置）> ephemeral opt-in（不读不写持久层）>
/// 持久层复用/首启生成（`Ok(落点)`）> 降级一次性（`Err(原因)`）。持久层短值
/// 视为损坏：重新生成并原子覆盖，绝不让坏文件砖掉启动。
pub fn resolve_token_secret(
    env_secret: Option<String>,
    ephemeral_opt_in: bool,
    store: Result<&Path, &str>,
    generate: &mut dyn FnMut() -> String,
) -> (String, TokenSecretSource) {
    if let Some(raw) = env_secret {
        let trimmed = raw.trim();
        if !trimmed.is_empty() {
            return (
                trimmed.to_string(),
                TokenSecretSource::EnvExplicit {
                    too_short: trimmed.len() < TOKEN_SECRET_MIN_LEN,
                },
            );
        }
    }
    if ephemeral_opt_in {
        return (generate(), TokenSecretSource::EphemeralOptIn);
    }
    let path = match store {
        Ok(p) => p,
        Err(reason) => {
            return (
                generate(),
                TokenSecretSource::EphemeralFallback {
                    reason: reason.to_string(),
                },
            );
        }
    };
    if let Ok(raw) = std::fs::read_to_string(path) {
        let stored = raw.trim();
        if stored.len() >= TOKEN_SECRET_MIN_LEN {
            return (stored.to_string(), TokenSecretSource::PersistedExisting);
        }
    }
    let generated = generate();
    match write_token_secret_file(path, &generated) {
        Ok(()) => (generated, TokenSecretSource::GeneratedAndPersisted),
        Err(reason) => (generated, TokenSecretSource::EphemeralFallback { reason }),
    }
}

/// 持久化密钥原子落盘：临时文件（uuid 后缀防双进程撞名）+ rename 换入；
/// Unix 上收紧到 0600（Windows 继承用户 profile ACL）。失败清理临时文件。
fn write_token_secret_file(path: &Path, secret: &str) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("{} 无父目录", path.display()))?;
    std::fs::create_dir_all(parent).map_err(|e| format!("创建目录 {}: {e}", parent.display()))?;
    let stem = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("token_secret");
    let tmp = path.with_file_name(format!("{stem}.tmp-{}", uuid::Uuid::new_v4().simple()));
    std::fs::write(&tmp, secret).map_err(|e| format!("写 {}: {e}", tmp.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600));
    }
    if let Err(e) = std::fs::rename(&tmp, path) {
        let _ = std::fs::remove_file(&tmp);
        return Err(format!("换入 {}: {e}", path.display()));
    }
    Ok(())
}

/// 生产生成器：两个 uuid v4 simple 拼接 = 64 个 hex 字符。
fn generate_token_secret() -> String {
    format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    )
}

static TOKEN_SECRET: OnceLock<String> = OnceLock::new();
/// 持久化层开关：仅 [`init_token_secret`]（生产 main）置位。惰性路径（测试/
/// 工具二进制）恒关闭——保证任何测试车道零真实用户空间 IO。
static TOKEN_SECRET_PERSIST_READY: AtomicBool = AtomicBool::new(false);

/// 生产入口启动初始化（内核 main 专用）：启用持久化层并立即解析一次。
/// 必须在日志初始化后调用（解析结论落启动日志）、任何 token 签发前调用。
pub fn init_token_secret() {
    TOKEN_SECRET_PERSIST_READY.store(true, Ordering::Release);
    let _ = token_secret();
}

/// token 签名密钥（进程内单次初始化，解析序见模块头注）。
fn token_secret() -> &'static str {
    TOKEN_SECRET.get_or_init(resolve_and_log).as_str()
}

fn resolve_and_log() -> String {
    let env_secret = std::env::var(TOKEN_SECRET_ENV).ok();
    let ephemeral = std::env::var(TOKEN_SECRET_EPHEMERAL_ENV)
        .map(|v| matches!(v.trim(), "1" | "true" | "True" | "TRUE"))
        .unwrap_or(false);
    let store = if TOKEN_SECRET_PERSIST_READY.load(Ordering::Acquire) {
        agentos_core::user_space::user_data_dir()
            .map(|d| d.join(TOKEN_SECRET_FILE))
            .ok_or_else(|| "用户空间不可用（无 OS 数据目录）".to_string())
    } else {
        Err("持久化未初始化（测试/工具入口未调 init_token_secret）".to_string())
    };
    let store_path = store.as_ref().ok().map(|p| p.to_path_buf());
    let (secret, source) = resolve_token_secret(
        env_secret,
        ephemeral,
        store.as_ref().map(|p| p.as_path()).map_err(|e| e.as_str()),
        &mut generate_token_secret,
    );
    match source {
        TokenSecretSource::EnvExplicit { too_short: false } => {
            tracing::info!("token 签名密钥：{TOKEN_SECRET_ENV} 显式配置（部署者提供）");
        }
        TokenSecretSource::EnvExplicit { too_short: true } => {
            tracing::warn!(
                "{TOKEN_SECRET_ENV} 长度不足 32 字符，签名强度受限；建议使用 ≥32 字符随机值"
            );
        }
        TokenSecretSource::EphemeralOptIn => {
            tracing::warn!(
                "{TOKEN_SECRET_EPHEMERAL_ENV} 显式启用一次性密钥：重启后全部 token 失效"
            );
        }
        TokenSecretSource::PersistedExisting => {
            tracing::info!(
                "token 签名密钥：复用持久化副本 {}（重启后登录态保持）",
                store_path
                    .as_deref()
                    .map(|p| p.display().to_string())
                    .unwrap_or_default()
            );
        }
        TokenSecretSource::GeneratedAndPersisted => {
            tracing::info!(
                "token 签名密钥：首次生成并持久化 {}（此后重启登录态保持）",
                store_path
                    .as_deref()
                    .map(|p| p.display().to_string())
                    .unwrap_or_default()
            );
        }
        TokenSecretSource::EphemeralFallback { reason } => {
            tracing::warn!(
                "token 密钥持久化不可用（{reason}）：本次使用一次性随机密钥，重启后全部 token 失效；可设 {TOKEN_SECRET_ENV} 显式固定"
            );
        }
    }
    secret
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

    /// refresh 类型判定：首段前缀为 "refresh"；access 及未知前缀均否。
    /// refresh 端点凭此拒绝 access token 换发（类型混淆拉平短/长效凭证）。
    pub fn is_refresh(&self) -> bool {
        self.token_type == TokenType::Refresh.prefix()
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
/// 内置用户表得到 `tenant_id`。无 token / 无效 / 过期 = 匿名请求，属合法
/// 状态，回退 [`DEFAULT_TENANT_ID`]；token 合法但解析用户失败（已删除 /
/// 存储故障）走 [`resolve_tenant_id_by_user`] 的拒绝语义（K8 修订，
/// ADR 2026-10-10），不再降级 default——写面降级即写串租户。
///
/// TODO(多租户): 签发面（agentos-api auth）token 载荷扩展出 tenant claim 后，
/// 本函数改为优先从 token 解析；该变更未发生前，store/内置用户表查询是
/// 现行权威路径。
pub async fn resolve_request_tenant_id(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    headers: &HeaderMap,
) -> Result<String, ApiError> {
    if let Some(token) = extract_bearer_token(headers) {
        if let Some(t) = decode_token(&token) {
            if !is_token_expired(t.exp) {
                return map_tenant_resolve_rejection(
                    resolve_tenant_id_by_user(store, &t.user_id).await,
                );
            }
        }
    }
    Ok(DEFAULT_TENANT_ID.to_string())
}

/// [`resolve_request_tenant_id`] 的失败翻译（错误要么处理要么传播）：
/// NotFound = 持有效签名的已删用户，与 [`authenticate_bearer_user`] 同文案 401；
/// StoreError = 基础设施故障，503（对齐会话列表「存储挂了」面，不伪装 4xx）。
fn map_tenant_resolve_rejection(r: Result<String, TenantResolveError>) -> Result<String, ApiError> {
    match r {
        Ok(t) => Ok(t),
        Err(TenantResolveError::NotFound) => Err(ApiError::Unauthorized {
            message: "用户不存在".to_string(),
        }),
        Err(TenantResolveError::StoreError(e)) => Err(ApiError::ServiceUnavailable {
            message: format!("用户→租户解析存储故障: {e}"),
        }),
    }
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

/// user→tenant 解析失败（tri-state 的两个失败态，K8 修订 ADR 2026-10-10）。
///
/// 失败类二分（总原则①）：NotFound 是"用户合法缺失"（已删除/从未存在），
/// StoreError 是基础设施故障——两类都拒绝分发，但审计语义与客户端处置不同
/// （前者 token 已死，后者重试可恢复）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TenantResolveError {
    /// 用户不存在：token 持有者已无分发资格（已删用户 token 不得继续分发）。
    NotFound,
    /// 存储查询故障：fail-closed 拒绝，不降级 default 继续分发。
    StoreError(String),
}

impl std::fmt::Display for TenantResolveError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TenantResolveError::NotFound => write!(f, "用户不存在（token 持有者已删除）"),
            TenantResolveError::StoreError(e) => write!(f, "用户→租户解析存储故障: {e}"),
        }
    }
}

/// 按 user_id 解析其归属的租户 ID（WS 路径用，与 HTTP 路径同源）。
///
/// 查询是 tenant-unscoped 主键查询（user_id 全局唯一，求真租户时 task_local
/// 不参与——由 [`find_user_by_id`] 契约保证）。三态消费（K8 修订，
/// ADR 2026-10-10，替代旧「降级 default + warn」过渡态）：
///
/// - **Found** → 返回真实租户；
/// - **NotFound（store 在）** → 直接拒绝（拍板①：已删用户 token 不应继续
///   分发），warn 留痕；**store 不在** → default 是文档化降级（无持久化即
///   无租户注册表，default 是唯一可能租户，非吞错——与"有 store 查不出"
///   二分，总原则①的"合法缺失"侧）；
/// - **StoreError** → fail-closed 拒绝，error 留痕（错误通道本在 Result 里，
///   旧实现被 `if let Ok(Some)` 丢弃后与 NotFound 合流降级 default）。
///
/// WebSocket 握手链路只透传 `user_id`（受 `PipelineDispatcher` trait 签名约束），
/// 无法携带 `HeaderMap`。本函数让 WS 入站分发器能用同一套用户表查出真正的
/// `tenant_id`，避免把 `user_id` 误当 `tenant_id` 导致读写 tenant 失配。
pub async fn resolve_tenant_id_by_user(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    user_id: &str,
) -> Result<String, TenantResolveError> {
    match find_user_by_id(store, user_id).await {
        Ok(Some(u)) => Ok(u.tenant_id),
        Ok(None) => {
            if store.is_none() {
                return Ok(DEFAULT_TENANT_ID.to_string());
            }
            tracing::warn!(
                target: "auth-tenant-resolve",
                user_id = %user_id,
                "用户→租户解析：用户不存在，拒绝分发（已删用户 token）"
            );
            Err(TenantResolveError::NotFound)
        }
        Err(e) => {
            tracing::error!(
                target: "auth-tenant-resolve",
                user_id = %user_id,
                error = %e,
                "用户→租户解析：存储故障，fail-closed 拒绝分发（不降级 default）"
            );
            Err(TenantResolveError::StoreError(e.to_string()))
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
// get_user_by_id / get_user_by_username 均为跨租户全局查询：前者 user_id 是
// 全局唯一主键（租户解析求真租户，task_local 不得参与），后者登录时还不知道
// 租户上下文。

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
/// tri-state（K8 修订 ADR 2026-10-10）：`Ok(Some)` = Found / `Ok(None)` =
/// NotFound（用户不存在）/ `Err` = StoreError（存储故障）。存储查询本身是
/// tenant-unscoped 主键查询（user_id 全局唯一，与 task_local 租户无关）——
/// 租户解析（[`resolve_tenant_id_by_user`]）正是求真租户，不能受 task_local
/// 影响。store 存在但未命中**不回退**内置 admin：换库/清库后按旧 token 的
/// user_id 命中硬编码表 = 已删除用户复活（K4，对齐 find_user_by_credentials）。
/// 仅无 store（测试场景）走内置表；无 store 且未命中同样是 `Ok(None)`
/// （动态用户 token 只可能由有 store 的实例签发，无 store 场景查无此用户
/// 属实然）。
pub async fn find_user_by_id(
    store: Option<&std::sync::Arc<dyn agentos_core::traits::StorageBackend>>,
    id: &str,
) -> Result<Option<BuiltInUser>, agentos_core::types::StorageError> {
    if let Some(store) = store {
        // 错误通道原样透传：Err ≠ None，调用方按 tri-state 分治处置。
        return store
            .get_user_by_id(id)
            .await
            .map(|u| u.map(|r| BuiltInUser::from(&r)));
    }
    // 无 store（测试/无持久化场景）：内置脚手架表（命中与否都是"查询成功"）
    Ok(default_users().into_iter().find(|u| u.id == id))
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
}

/// 校验 access token（供 WS 握手从 `?token=` 查询参数鉴权）。
///
/// 返回 `Some(VerifiedUser)` 当且仅当：token 签名有效 + 未过期 + access 类型。
/// 本函数是同步纯 token 校验（无 store 可查）：用户存在性由 WS 会话入口
/// （api `run_ws_session_with_auth`）握手后按 store 复用 [`find_user_by_id`]
/// 权威校验（已删用户 4001 拒连）；口令绑定（改密吊销）由握手后的 token
/// 刷新链路（refresh/me 均做口令绑定校验）承接。任一校验失败返回 `None`
/// （调用方按 ADR §7.2 以 4001 拒绝握手）。
pub fn verify_access_token(token: &str) -> Option<VerifiedUser> {
    let t = decode_token(token)?;
    if is_token_expired(t.exp) {
        return None;
    }
    // payload 必须是 access token（拒绝 refresh token 用于 WS 鉴权）
    if !t.is_access() {
        return None;
    }
    Some(VerifiedUser {
        user_id: t.user_id,
        username: t.username,
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
            resolve_request_tenant_id(store, &HeaderMap::new())
                .await
                .unwrap(),
            DEFAULT_TENANT_ID
        );

        // ② 垃圾 token
        let junk = mk_headers("garbage-token");
        assert_eq!(
            resolve_request_tenant_id(store, &junk).await.unwrap(),
            DEFAULT_TENANT_ID
        );

        // ③ 合法未过期：内置表用户 → 其 tenant_id
        let user = default_users().into_iter().next().unwrap();
        let expect_tenant = user.tenant_id.clone();
        let good = mk_headers(&encode_token(TokenType::Access, &user, 3600));
        assert_eq!(
            resolve_request_tenant_id(store, &good).await.unwrap(),
            expect_tenant,
            "合法 token 应按用户解析租户"
        );

        // ④ 合法但过期 → default（匿名语义，非解析失败）
        let expired = mk_headers(&encode_token(TokenType::Access, &user, 0));
        assert_eq!(
            resolve_request_tenant_id(store, &expired).await.unwrap(),
            DEFAULT_TENANT_ID,
            "过期 token 不得用于租户解析"
        );
    }

    // ── 租户解析 tri-state（K8 修订 ADR 2026-10-10）──
    //
    // fake storage 注入三态（Some/None/Err）——存储是外部依赖，mock 合规；
    // 断言解析行为本身：Found 真租户 / NotFound 拒绝 / StoreError fail-closed，
    // 任何失败态都不得出口 default（降级即写串租户）。

    /// 最小注入式用户查询桩：仅 get_user_by_id 有行为，其余方法不可达。
    struct UserLookupStore(
        Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>,
    );

    fn user_of(user_id: &str, tenant_id: &str) -> agentos_core::types::UserRecord {
        agentos_core::types::UserRecord {
            user_id: user_id.to_string(),
            username: format!("u-{user_id}"),
            password: String::new(),
            email: None,
            role: "user".to_string(),
            tenant_id: tenant_id.to_string(),
            created_at: "2026-10-10T00:00:00Z".to_string(),
            last_login_at: None,
            must_change_password: false,
        }
    }

    #[async_trait::async_trait]
    impl agentos_core::traits::StorageBackend for UserLookupStore {
        async fn get_run(
            &self,
            _run_id: &str,
        ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
            unreachable!("租户解析路径不读 run")
        }
        async fn get_messages_by_pipeline(
            &self,
            _pipeline_id: &str,
            _opts: agentos_core::traits::MessageQueryOpts,
        ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError>
        {
            unreachable!("租户解析路径不读消息")
        }
        async fn get_blob(
            &self,
            _blob_id: &str,
        ) -> Result<Vec<u8>, agentos_core::types::StorageError> {
            unreachable!("租户解析路径不读 blob")
        }
        async fn append_trace(
            &self,
            _entry: agentos_core::types::TraceEntry,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("租户解析路径不写轨迹")
        }
        async fn store_blob(
            &self,
            _data: &[u8],
            _mime_type: &str,
        ) -> Result<String, agentos_core::types::StorageError> {
            unreachable!("租户解析路径不写 blob")
        }
        async fn create_session(
            &self,
            _session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("租户解析路径不建会话")
        }
        async fn get_session(
            &self,
            _thread_id: &str,
        ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            unreachable!("租户解析路径不读会话")
        }
        async fn list_sessions(
            &self,
            _filter: agentos_core::traits::SessionListFilter,
        ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            unreachable!("租户解析路径不列会话")
        }
        async fn update_session(
            &self,
            _session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("租户解析路径不更会话")
        }
        async fn delete_session(
            &self,
            _thread_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            unreachable!("租户解析路径不删会话")
        }
        async fn link_pipeline_session(
            &self,
            _pipeline_id: &str,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("租户解析路径不写映射")
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            unreachable!("租户解析路径不读映射")
        }
        async fn get_step_traces_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError>
        {
            unreachable!("租户解析路径不读轨迹")
        }
        async fn create_user(
            &self,
            _user: &agentos_core::types::UserRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("租户解析路径不建用户")
        }
        async fn get_user_by_id(
            &self,
            _user_id: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            self.0.clone()
        }
        async fn get_user_by_username(
            &self,
            _username: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            unreachable!("租户解析路径按 id 查")
        }
        async fn list_users(
            &self,
        ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            unreachable!("租户解析路径不列用户")
        }
        async fn update_last_login(
            &self,
            _user_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("租户解析路径不更登录时间")
        }
        async fn update_user_password(
            &self,
            _user_id: &str,
            _password_hash: &str,
            _must_change_password: bool,
        ) -> Result<bool, agentos_core::types::StorageError> {
            unreachable!("租户解析路径不改密")
        }
        async fn delete_user(
            &self,
            _user_id: &str,
        ) -> Result<bool, agentos_core::types::StorageError> {
            unreachable!("租户解析路径不删用户")
        }
    }

    fn lookup_store(
        injected: Result<
            Option<agentos_core::types::UserRecord>,
            agentos_core::types::StorageError,
        >,
    ) -> std::sync::Arc<dyn agentos_core::traits::StorageBackend> {
        std::sync::Arc::new(UserLookupStore(injected))
    }

    /// find_user_by_id tri-state 三分支：Err 原样透传（不再被 if let 丢弃），
    /// None/Some 语义保真。
    #[tokio::test]
    async fn find_user_by_id_passes_tri_state_through() {
        // ① Found：返回用户且租户保真
        let store = lookup_store(Ok(Some(user_of("u-b", "tenant-b"))));
        let u = find_user_by_id(Some(&store), "u-b").await.unwrap().unwrap();
        assert_eq!(u.tenant_id, "tenant-b");
        assert_eq!(u.id, "u-b");

        // ② NotFound：Ok(None)（查询成功，用户不存在）
        let store = lookup_store(Ok(None));
        assert!(
            find_user_by_id(Some(&store), "u-gone")
                .await
                .unwrap()
                .is_none(),
            "用户不存在是查询成功的空结果，不是错误"
        );

        // ③ StoreError：Err 原样透传（性质断言：不得伪装成 None/Some）
        let store = lookup_store(Err(agentos_core::types::StorageError::Database(
            "injected outage".to_string(),
        )));
        assert!(
            find_user_by_id(Some(&store), "u-any").await.is_err(),
            "存储故障必须走 Err 通道，不得与 NotFound 合流"
        );

        // 无 store：内置表命中/未命中同样是 Ok 两态
        let admin_id = default_users()[0].id.clone();
        assert!(find_user_by_id(None, &admin_id).await.unwrap().is_some());
        assert!(find_user_by_id(None, "no-such").await.unwrap().is_none());
    }

    /// resolve_tenant_id_by_user：Found → 真实租户（非 default 租户用户不落
    /// 降级）；NotFound → 拒绝（拍板①）；StoreError → fail-closed 拒绝。
    /// 失败态一律不出租户值（降级 default = 写串租户，性质断言）。
    #[tokio::test]
    async fn resolve_tenant_id_by_user_consumes_tri_state() {
        // ① Found：两个不同非 default 租户（区分度第二组输入）各自解析为真
        for (uid, tenant) in [("u-b", "tenant-b"), ("u-c", "tenant-c")] {
            let store = lookup_store(Ok(Some(user_of(uid, tenant))));
            assert_eq!(
                resolve_tenant_id_by_user(Some(&store), uid).await.unwrap(),
                tenant,
                "动态用户必须解析出真实租户 {tenant}"
            );
        }

        // ② NotFound（已删用户）：拒绝，且不是 default
        let store = lookup_store(Ok(None));
        let err = resolve_tenant_id_by_user(Some(&store), "u-gone")
            .await
            .unwrap_err();
        assert_eq!(err, TenantResolveError::NotFound, "已删用户 token 必须拒绝");
        assert_ne!(
            err.to_string(),
            DEFAULT_TENANT_ID,
            "拒绝路径不得出口任何租户值"
        );

        // ③ StoreError：fail-closed 拒绝（错误串透传可归因）
        let store = lookup_store(Err(agentos_core::types::StorageError::Database(
            "injected outage".to_string(),
        )));
        let err = resolve_tenant_id_by_user(Some(&store), "u-any")
            .await
            .unwrap_err();
        match &err {
            TenantResolveError::StoreError(msg) => {
                assert!(msg.contains("injected outage"), "错误串可归因: {msg}")
            }
            other => panic!("存储故障必须是 StoreError 态: {other:?}"),
        }
    }

    /// 无 store 场景（测试/嵌入式降级模式）：default 是唯一可能租户——
    /// 内置 admin 解析其真实租户（恰为 default），未知用户走文档化降级
    /// （无持久化即无租户注册表，非吞错）。有 store 的拒绝路径见
    /// tri-state 用例（合法缺失与解析失败二分）。
    #[tokio::test]
    async fn resolve_tenant_id_by_user_no_store_is_documented_default() {
        let admin = default_users().into_iter().next().unwrap();
        let expect = admin.tenant_id.clone();
        assert_eq!(
            resolve_tenant_id_by_user(None, &admin.id).await.unwrap(),
            expect,
            "内置 admin 解析其真实租户"
        );
        for unknown in ["no-such-user", ""] {
            assert_eq!(
                resolve_tenant_id_by_user(None, unknown).await.unwrap(),
                DEFAULT_TENANT_ID,
                "无 store 时 {unknown:?} 走文档化降级（唯一可能租户）"
            );
        }
    }

    /// resolve_request_tenant_id：合法 token + 解析失败 → HTTP 面拒绝
    /// （NotFound → 401 文案与 authenticate_bearer_user 一致；StoreError →
    /// 503 不伪装 4xx）；无 token 保持匿名 default 契约。
    #[tokio::test]
    async fn resolve_request_tenant_id_propagates_resolution_failure() {
        let mk = |store: std::sync::Arc<dyn agentos_core::traits::StorageBackend>,
                  user: BuiltInUser| {
            let token = encode_token(TokenType::Access, &user, 3600);
            (Some(store), mk_headers(&token))
        };

        // NotFound → 401（对齐管理面"用户不存在"文案）
        let (store, headers) = mk(lookup_store(Ok(None)), test_user(""));
        match resolve_request_tenant_id(store.as_ref(), &headers).await {
            Err(ApiError::Unauthorized { message }) => {
                assert_eq!(message, "用户不存在");
            }
            other => panic!("已删用户必须 401: {other:?}"),
        }

        // StoreError → 503（基础设施故障不伪装客户端错误）
        let (store, headers) = mk(
            lookup_store(Err(agentos_core::types::StorageError::Database(
                "db down".into(),
            ))),
            test_user(""),
        );
        match resolve_request_tenant_id(store.as_ref(), &headers).await {
            Err(ApiError::ServiceUnavailable { message }) => {
                assert!(message.contains("db down"), "错误可归因: {message}");
            }
            other => panic!("存储故障必须 503: {other:?}"),
        }

        // 无 token：匿名 default 契约不变（合法缺失 ≠ 解析失败）
        let store = lookup_store(Ok(None));
        assert_eq!(
            resolve_request_tenant_id(Some(&store), &HeaderMap::new())
                .await
                .unwrap(),
            DEFAULT_TENANT_ID
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

// ─── token 密钥解析（持久化默认，ADR 2026-10-22-token-secret-persistent-default） ───

#[cfg(test)]
mod token_secret_tests {
    use super::*;
    use std::path::PathBuf;
    use tempfile::TempDir;

    /// 定长字符串夹具：`c` 重复 `n` 次。
    fn s(c: char, n: usize) -> String {
        std::iter::repeat_n(c, n).collect()
    }

    /// 确定性生成器：`tag` + 63 位计数补零，恒 64 字符（≥ MIN 32）。
    fn counting_gen(tag: char) -> impl FnMut() -> String {
        let mut n: u64 = 0;
        move || {
            n += 1;
            format!("{tag}{n:0>63}")
        }
    }

    fn key_path(tmp: &TempDir) -> PathBuf {
        tmp.path().join(TOKEN_SECRET_FILE)
    }

    #[test]
    fn env_explicit_wins_and_store_untouched() {
        let tmp = TempDir::new().unwrap();
        let key = key_path(&tmp);
        std::fs::write(&key, s('P', 64)).unwrap();

        let (secret, source) = resolve_token_secret(
            Some(s('E', 40)),
            false,
            Ok(key.as_path()),
            &mut counting_gen('G'),
        );
        assert_eq!(secret, s('E', 40));
        assert_eq!(source, TokenSecretSource::EnvExplicit { too_short: false });
        assert_eq!(
            std::fs::read_to_string(&key).unwrap(),
            s('P', 64),
            "env 显式时持久层绝不被触碰"
        );
    }

    #[test]
    fn env_explicit_short_used_with_flag() {
        let (secret, source) = resolve_token_secret(
            Some("short".into()),
            false,
            Err("不可用"),
            &mut counting_gen('G'),
        );
        assert_eq!(secret, "short", "显式短值仅告警不拒绝（尊重部署者）");
        assert_eq!(source, TokenSecretSource::EnvExplicit { too_short: true });
    }

    #[test]
    fn env_blank_treated_as_unset_falls_through() {
        let tmp = TempDir::new().unwrap();
        let key = key_path(&tmp);
        std::fs::write(&key, s('P', 64)).unwrap();

        // 空白 env = 未设置（与既有口径一致）→ 落持久层
        let (secret, source) = resolve_token_secret(
            Some("   ".into()),
            false,
            Ok(key.as_path()),
            &mut counting_gen('G'),
        );
        assert_eq!(secret, s('P', 64));
        assert_eq!(source, TokenSecretSource::PersistedExisting);
    }

    #[test]
    fn ephemeral_opt_in_ignores_and_never_touches_store() {
        let tmp = TempDir::new().unwrap();
        let key = key_path(&tmp);
        std::fs::write(&key, s('P', 64)).unwrap();

        let (secret, source) =
            resolve_token_secret(None, true, Ok(key.as_path()), &mut counting_gen('G'));
        assert_eq!(source, TokenSecretSource::EphemeralOptIn);
        assert_eq!(secret, format!("G{:0>63}", 1), "opt-in 走生成器");
        assert_eq!(
            std::fs::read_to_string(&key).unwrap(),
            s('P', 64),
            "opt-in 不读不写持久层（已存副本原样保留）"
        );
    }

    #[test]
    fn persisted_existing_reused_and_trimmed() {
        let tmp = TempDir::new().unwrap();
        let key = key_path(&tmp);
        // 尾随换行（编辑器手改常见形态）必须被 trim 后采用
        std::fs::write(&key, format!("{}\n", s('P', 64))).unwrap();

        let (secret, source) =
            resolve_token_secret(None, false, Ok(key.as_path()), &mut counting_gen('G'));
        assert_eq!(secret, s('P', 64), "尾随空白不应进入密钥");
        assert_eq!(source, TokenSecretSource::PersistedExisting);
        assert!(
            std::fs::read_dir(tmp.path()).unwrap().count() == 1,
            "复用路径不得产生临时文件残留"
        );
    }

    #[test]
    fn missing_file_generates_and_persists_atomically() {
        let tmp = TempDir::new().unwrap();
        let key = key_path(&tmp);

        let (secret, source) =
            resolve_token_secret(None, false, Ok(key.as_path()), &mut counting_gen('G'));
        assert_eq!(source, TokenSecretSource::GeneratedAndPersisted);
        assert_eq!(secret, format!("G{:0>63}", 1));
        assert_eq!(
            std::fs::read_to_string(&key).unwrap(),
            secret,
            "落盘内容必须与采用值逐字节一致"
        );
        assert!(
            std::fs::read_dir(tmp.path()).unwrap().count() == 1,
            "落盘后无临时文件残留（原子换入）"
        );
    }

    #[test]
    fn corrupt_short_file_regenerated_and_overwritten() {
        let tmp = TempDir::new().unwrap();
        let key = key_path(&tmp);
        std::fs::write(&key, "tooshort").unwrap();

        let (secret, source) =
            resolve_token_secret(None, false, Ok(key.as_path()), &mut counting_gen('G'));
        assert_eq!(source, TokenSecretSource::GeneratedAndPersisted);
        assert_eq!(
            std::fs::read_to_string(&key).unwrap(),
            secret,
            "过短存储视为损坏：重新生成并覆盖"
        );
    }

    /// 核心性质：同一持久层两次解析（模拟重启）必须得到同一密钥。
    #[test]
    fn restart_reuses_persisted_secret() {
        let tmp = TempDir::new().unwrap();
        let key = key_path(&tmp);

        let (first, s1) =
            resolve_token_secret(None, false, Ok(key.as_path()), &mut counting_gen('A'));
        assert_eq!(s1, TokenSecretSource::GeneratedAndPersisted);

        // 模拟重启：全新生成器、仅持久层还在
        let (second, s2) =
            resolve_token_secret(None, false, Ok(key.as_path()), &mut counting_gen('B'));
        assert_eq!(s2, TokenSecretSource::PersistedExisting);
        assert_eq!(first, second, "重启后必须复用同一密钥（本裁断的核心性质）");
    }

    #[test]
    fn store_unavailable_falls_back_ephemeral_with_reason() {
        let (secret, source) = resolve_token_secret(
            None,
            false,
            Err("用户空间不可用（无 OS 数据目录）"),
            &mut counting_gen('G'),
        );
        assert_eq!(secret.len(), 64);
        assert_eq!(
            source,
            TokenSecretSource::EphemeralFallback {
                reason: "用户空间不可用（无 OS 数据目录）".into()
            },
            "降级原因必须透传（启动日志定位用）"
        );
    }

    #[test]
    fn write_failure_falls_back_without_partial_file() {
        let tmp = TempDir::new().unwrap();
        let blocker = tmp.path().join("blocker");
        std::fs::write(&blocker, "x").unwrap();
        let key = blocker.join(TOKEN_SECRET_FILE); // 父路径是文件 → 建目录必败

        let (secret, source) =
            resolve_token_secret(None, false, Ok(key.as_path()), &mut counting_gen('G'));
        assert!(matches!(
            source,
            TokenSecretSource::EphemeralFallback { .. }
        ));
        assert_eq!(secret.len(), 64, "写失败降级仍给出可用密钥（不砖启动）");
        assert!(!key.exists(), "失败路径不得留下半写文件");
    }

    #[test]
    fn production_generator_is_64_hex_and_fresh() {
        let a = generate_token_secret();
        assert_eq!(a.len(), 64, "生产生成器 = 两个 uuid simple 拼接");
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()), "{a}");
        assert_ne!(a, generate_token_secret(), "两次生成必须不同（随机性）");
    }
}
