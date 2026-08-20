//! user-admin capability handler——用户管理策略面（boot-plugin 第二刀）。
//!
//! §9.6 判据的精确拆分：**auth 执行门永留内核**（登录验签/JWT 校验/路由准入
//! ——`/api/v1/auth/login|logout|me|register|refresh`，前端与 WS 握手在用，
//! api/src/auth.rs 一行不动）；本 handler 承载的是**管理性质**的用户管理
//! 策略面（用户列表/改角色/改租户/删用户）。这些管理端点在拆分前**不存在**，
//! 本刀直接以插件化形态新建——HTTP 面由 `plugins/shared/user_admin`
//! （Python sidecar 插件）承载：内核 `/ext/{*rest}` 通配分发 → 插件
//! `http.handle` → 反向调用 `user-admin.<method>` → 本 handler（注册进内核
//! `CapabilityHandlerRegistry`，agentos-kernel.rs 启动期，先于任何 sidecar
//! spawn）。
//!
//! ## method 清单（4 个）
//!
//! | method | HTTP 面（插件 manifest） | 数据通道 |
//! |---|---|---|
//! | `list_users` | GET /ext/user_admin/users | trait `StorageBackend::list_users`（跨 driver 可用） |
//! | `update_role` | PATCH /ext/user_admin/users/{id}/role | SqliteStore `with_conn` 直连（trait 无 update 方法，侵入小优先，对齐 db-admin 模式） |
//! | `update_tenant` | PATCH /ext/user_admin/users/{id}/tenant | 同上 |
//! | `delete_user` | DELETE /ext/user_admin/users/{id} | trait `StorageBackend::delete_user`（跨 driver 可用） |
//!
//! ## 鉴权落点（信任锚点）
//!
//! 与第一刀（db-admin）同一模式：HTTP 面插件**只透传凭证、不做鉴权决策**——
//! 入站请求的 `Authorization` 头原样放进 params 的 `_authorization` 字段，
//! 本 handler 在内核侧重建 HeaderMap 后复用 `resolve_request_user`
//! （agentos-http 单一实现，与 api 执行门同源），校验 **admin 角色**。
//! 用户列表含全员租户归属，属敏感管理面——viewer 亦拒绝（全 method 仅 admin，
//! 与 db-admin 的"读面 admin/viewer"不同，本面无只读豁免）。
//! manifest 的 `http_endpoints[].auth: "admin"` 目前是声明性字段，内核
//! dispatcher 不执行它，实际执行点在本 handler。
//!
//! ## self-service 防护（鉴权铁律，防锁死系统）
//!
//! 全部三个变更 method（update_role/update_tenant/delete_user）在 handler 内
//! 校验 `_authorization` 解析出的 actor user_id ≠ 目标 user_id——admin 不能
//! 删自己、不能降自己的角色、也不能改自己的租户（把唯一的 admin 降级/移出
//! default 租户都会锁死系统）。命中即 403，与目标是否存在无关。
//!
//! ## 响应信封（与 db-admin 一致）
//!
//! 成功返回 `{status, body}`；业务失败返回 `{status, error: {code, message}}`
//! （400/401/403/404/500/503）。用户 JSON **永不包含 password 字段**（明文
//! 密码不得经管理面外泄）：`{id, username, email, role, tenant_id,
//! created_at, last_login_at}`。
//!
//! [来源: docs/working/重要设计/boot-plugin内核能力插件化立项.md §四/§五]

use std::sync::Arc;

use agentos_core::traits::StorageBackend;
use agentos_core::types::UserRecord;
use agentos_http::auth::resolve_request_user;
use agentos_http::error::ApiError;
use agentos_mcp::{CapabilityHandler, McpError};
use async_trait::async_trait;
use axum::http::{header::AUTHORIZATION, HeaderMap, HeaderValue};
use serde_json::{json, Value};
use tokio::task::spawn_blocking;

/// user-admin capability 的 namespace（manifest granted_capabilities 与此对齐）。
pub const NAMESPACE: &str = "user-admin";

/// 允许的角色枚举（update_role 白名单——与 UserRecord.role 注释一致：
/// admin/user，RBAC 完整化留给 0.5.0）。
const ALLOWED_ROLES: [&str; 2] = ["admin", "user"];

/// handler 所需状态（对齐 db-admin 的 DbAdminState）。
#[derive(Clone)]
pub struct UserAdminState {
    /// 存储后端（用户解析/list/delete 用；与 api `AppState.store` 同一实例）。
    pub store: Option<Arc<dyn StorageBackend>>,
    /// 统一数据接口 db 句柄（引擎 SqliteStore——update_role/update_tenant 的
    /// with_conn 直连通道；非 SQLite driver 下为 None，诚实降级）。
    pub db: Option<Arc<agentos_engine::SqliteStore>>,
}

/// `user-admin` namespace 的 capability handler（4 method）。
pub struct UserAdminCapabilityHandler {
    state: UserAdminState,
}

impl UserAdminCapabilityHandler {
    /// 创建 handler。
    ///
    /// Args:
    /// - `store`: 用户解析/list/delete 的存储后端（api `AppState.store` 同一实例）；
    /// - `db`: 统一数据接口 db 句柄（引擎 SqliteStore，update_* 直连用）。
    pub fn new(
        store: Option<Arc<dyn StorageBackend>>,
        db: Option<Arc<agentos_engine::SqliteStore>>,
    ) -> Self {
        Self {
            state: UserAdminState { store, db },
        }
    }

    /// 从 params 的 `_authorization`（HTTP 面插件转发的原始 Authorization 头值，
    /// 如 `Bearer eyJ...`）重建 HeaderMap，复用既有鉴权链。
    fn auth_headers(params: &Value) -> HeaderMap {
        let mut headers = HeaderMap::new();
        if let Some(auth) = params.get("_authorization").and_then(|v| v.as_str()) {
            if let Ok(v) = HeaderValue::from_str(auth) {
                headers.insert(AUTHORIZATION, v);
            }
        }
        headers
    }

    /// 全 method 统一角色校验：仅 admin（管理面无只读豁免，见模块文档）。
    /// 返回 (actor_user_id, tenant_id)——actor_user_id 供 self-service 防护。
    async fn require_admin(&self, headers: &HeaderMap) -> Result<(String, String), ApiError> {
        let (user_id, _, role, tenant_id) =
            resolve_request_user(self.state.store.as_ref(), headers).await?;
        if role != "admin" {
            return Err(ApiError::Forbidden {
                message: "用户管理操作需要 admin 角色".to_string(),
            });
        }
        Ok((user_id, tenant_id))
    }

    /// self-service 防护：actor 不得把管理操作对准自己（删自己/降自己角色/
    /// 改自己租户都会锁死系统——唯一 admin 失效即无人能管理）。
    fn ensure_not_self(actor_user_id: &str, target_user_id: &str) -> Result<(), ApiError> {
        if actor_user_id == target_user_id {
            return Err(ApiError::Forbidden {
                message: "不能对自身执行用户管理操作（self-service 防护，防锁死系统）".to_string(),
            });
        }
        Ok(())
    }

    /// 必填 string 参数提取（缺失/类型不符 → 400）。
    fn require_str<'a>(params: &'a Value, key: &str) -> Result<&'a str, ApiError> {
        params
            .get(key)
            .and_then(|v| v.as_str())
            .ok_or_else(|| ApiError::BadRequest {
                message: format!("缺少或非法的 {key} 参数"),
            })
    }

    /// 获取存储后端（list/delete 用；生产恒非空，None → 503 对齐
    /// auth register 的无 store 语义）。
    fn get_store(&self) -> Result<Arc<dyn StorageBackend>, ApiError> {
        self.state
            .store
            .clone()
            .ok_or_else(|| ApiError::ServiceUnavailable {
                message: "存储后端未初始化，无法执行用户管理操作".to_string(),
            })
    }

    /// 获取统一数据接口 db 句柄（update_* 直连用；非 SQLite driver → 400，
    /// 对齐 db-admin 的诚实降级语义）。
    fn get_db(&self) -> Result<Arc<agentos_engine::SqliteStore>, ApiError> {
        self.state.db.clone().ok_or_else(|| ApiError::BadRequest {
            message: "统一数据接口未启用（db 未注入）".to_string(),
        })
    }

    /// UserRecord → 管理面 JSON（**剥离 password**——明文密码不得经管理面外泄）。
    fn user_to_json(u: &UserRecord) -> Value {
        json!({
            "id": u.user_id,
            "username": u.username,
            "email": u.email,
            "role": u.role,
            "tenant_id": u.tenant_id,
            "created_at": u.created_at,
            "last_login_at": u.last_login_at,
        })
    }

    /// 单行 UPDATE（users 表，列名由调用方限定白名单：role / tenant_id）。
    /// 返回受影响行数；0 行 = 目标不存在（调用方转 404）。
    async fn update_user_column(
        &self,
        user_id: &str,
        column: &str,
        new_value: &str,
    ) -> Result<usize, ApiError> {
        let db = self.get_db()?;
        // spawn_blocking 要求 'static：参数先转 owned。
        let user_id = user_id.to_string();
        let new_value = new_value.to_string();
        let sql = format!("UPDATE users SET {column} = ?1 WHERE user_id = ?2");
        spawn_blocking(move || {
            db.with_conn(|conn| {
                conn.execute(&sql, rusqlite::params![new_value, user_id])
                    .map_err(|e| ApiError::Internal {
                        message: format!("更新用户失败: {e}"),
                    })
            })
        })
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("数据库任务失败: {e}"),
        })?
    }

    /// 按 user_id 查单条（with_conn 直连，跨租户——user_id 是全局主键），
    /// 供 update_* 返回更新后的记录。不存在 → Ok(None)。
    fn select_user(
        db: &Arc<agentos_engine::SqliteStore>,
        user_id: &str,
    ) -> Result<Option<Value>, ApiError> {
        db.with_conn(|conn| {
            let row = conn.query_row(
                "SELECT user_id, username, email, role, tenant_id, created_at, last_login_at
                 FROM users WHERE user_id = ?1",
                rusqlite::params![user_id],
                |row| {
                    Ok(json!({
                        "id": row.get::<_, String>(0)?,
                        "username": row.get::<_, String>(1)?,
                        "email": row.get::<_, Option<String>>(2)?,
                        "role": row.get::<_, String>(3)?,
                        "tenant_id": row.get::<_, String>(4)?,
                        "created_at": row.get::<_, String>(5)?,
                        "last_login_at": row.get::<_, Option<String>>(6)?,
                    }))
                },
            );
            match row {
                Ok(v) => Ok(Some(v)),
                Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
                Err(e) => Err(ApiError::Internal {
                    message: format!("查询用户失败: {e}"),
                }),
            }
        })
    }

    // ─── method 1：list_users（GET /ext/user_admin/users） ─────────────

    async fn list_users(&self, params: &Value) -> Result<(u16, Value), ApiError> {
        let headers = Self::auth_headers(params);
        let _ = self.require_admin(&headers).await?;
        let store = self.get_store()?;
        let users = store.list_users().await.map_err(|e| ApiError::Internal {
            message: format!("列用户失败: {e}"),
        })?;
        let users_json: Vec<Value> = users.iter().map(Self::user_to_json).collect();
        Ok((
            200,
            json!({ "users": users_json, "total": users_json.len() }),
        ))
    }

    // ─── method 2：update_role（PATCH /ext/user_admin/users/{id}/role） ─

    async fn update_role(&self, params: &Value) -> Result<(u16, Value), ApiError> {
        let headers = Self::auth_headers(params);
        let (actor, _) = self.require_admin(&headers).await?;
        let target = Self::require_str(params, "user_id")?;
        Self::ensure_not_self(&actor, target)?;

        let role = Self::require_str(params, "role")?;
        if !ALLOWED_ROLES.contains(&role) {
            return Err(ApiError::BadRequest {
                message: format!("非法角色 '{role}'（允许: admin/user）"),
            });
        }

        let db = self.get_db()?;
        let affected = self.update_user_column(target, "role", role).await?;
        if affected == 0 {
            return Err(ApiError::NotFound {
                message: format!("用户 {target} 不存在"),
            });
        }
        let user = Self::select_user(&db, target)?.ok_or_else(|| ApiError::NotFound {
            message: format!("用户 {target} 不存在"),
        })?;
        Ok((200, json!({ "user": user })))
    }

    // ─── method 3：update_tenant（PATCH /ext/user_admin/users/{id}/tenant） ─

    async fn update_tenant(&self, params: &Value) -> Result<(u16, Value), ApiError> {
        let headers = Self::auth_headers(params);
        let (actor, _) = self.require_admin(&headers).await?;
        let target = Self::require_str(params, "user_id")?;
        Self::ensure_not_self(&actor, target)?;

        let tenant_id = Self::require_str(params, "tenant_id")?;
        if tenant_id.trim().is_empty() {
            return Err(ApiError::BadRequest {
                message: "tenant_id 不能为空".to_string(),
            });
        }

        let db = self.get_db()?;
        let affected = self
            .update_user_column(target, "tenant_id", tenant_id)
            .await?;
        if affected == 0 {
            return Err(ApiError::NotFound {
                message: format!("用户 {target} 不存在"),
            });
        }
        let user = Self::select_user(&db, target)?.ok_or_else(|| ApiError::NotFound {
            message: format!("用户 {target} 不存在"),
        })?;
        Ok((200, json!({ "user": user })))
    }

    // ─── method 4：delete_user（DELETE /ext/user_admin/users/{id}） ────

    async fn delete_user(&self, params: &Value) -> Result<(u16, Value), ApiError> {
        let headers = Self::auth_headers(params);
        let (actor, _) = self.require_admin(&headers).await?;
        let target = Self::require_str(params, "user_id")?;
        Self::ensure_not_self(&actor, target)?;

        let store = self.get_store()?;
        let deleted = store
            .delete_user(target)
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("删除用户失败: {e}"),
            })?;
        if !deleted {
            return Err(ApiError::NotFound {
                message: format!("用户 {target} 不存在"),
            });
        }
        Ok((200, json!({ "deleted": true, "user_id": target })))
    }
}

/// ApiError → (HTTP 状态码, 消息)。与 db-admin / api `ApiError::IntoResponse`
/// 的映射一致。
fn api_error_parts(e: &ApiError) -> (u16, String) {
    match e {
        ApiError::BadRequest { message } => (400, message.clone()),
        ApiError::Unauthorized { message } => (401, message.clone()),
        ApiError::Forbidden { message } => (403, message.clone()),
        ApiError::NotFound { message } => (404, message.clone()),
        ApiError::Conflict { message } => (409, message.clone()),
        ApiError::UnprocessableEntity { message } => (422, message.clone()),
        ApiError::Internal { message } | ApiError::WebSocket { message } => (500, message.clone()),
        ApiError::ServiceUnavailable { message } => (503, message.clone()),
    }
}

#[async_trait]
impl CapabilityHandler for UserAdminCapabilityHandler {
    fn namespace(&self) -> &str {
        NAMESPACE
    }

    async fn handle(&self, method: &str, params: Value) -> Result<Value, McpError> {
        let result: Result<(u16, Value), ApiError> = match method {
            "list_users" => self.list_users(&params).await,
            "update_role" => self.update_role(&params).await,
            "update_tenant" => self.update_tenant(&params).await,
            "delete_user" => self.delete_user(&params).await,
            other => {
                return Err(McpError::Protocol {
                    message: format!(
                        "{NAMESPACE}.{other} not implemented (known: list_users, update_role, \
                         update_tenant, delete_user)"
                    ),
                });
            }
        };
        Ok(match result {
            Ok((status, body)) => json!({ "status": status, "body": body }),
            Err(e) => {
                let (status, message) = api_error_parts(&e);
                json!({
                    "status": status,
                    "error": { "code": status.to_string(), "message": message },
                })
            }
        })
    }
}

// ─── 测试 ────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_http::auth::{default_users, encode_token, TokenType};

    /// 内存库 + 完整 state（store/db 同一实例，对齐生产装配），
    /// 并播种内置 admin（与生产 seed_admin_user 一致）。
    async fn handler_with_store() -> (UserAdminCapabilityHandler, Arc<agentos_engine::SqliteStore>)
    {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let admin = default_users().into_iter().next().unwrap();
        let record = UserRecord {
            user_id: admin.id.clone(),
            username: admin.username.clone(),
            password: admin.password.clone(),
            email: Some(admin.email.clone()),
            role: admin.role.clone(),
            tenant_id: admin.tenant_id.clone(),
            created_at: admin.created_at.clone(),
            last_login_at: None,
        };
        store.create_user(&record).await.unwrap();
        (
            UserAdminCapabilityHandler::new(Some(store.clone()), Some(store.clone())),
            store,
        )
    }

    /// 铸造内置 admin 的 access token（与 api 登录签发同格式）。
    fn admin_token() -> String {
        let admin = default_users().into_iter().next().unwrap();
        encode_token(TokenType::Access, &admin, 3600)
    }

    fn authed(extra: Value) -> Value {
        let mut params = extra;
        params["_authorization"] = json!(format!("Bearer {}", admin_token()));
        params
    }

    /// 建一个普通用户，返回其 user_id。
    async fn seed_user(
        store: &Arc<agentos_engine::SqliteStore>,
        username: &str,
        role: &str,
    ) -> String {
        let user_id = format!("u-{username}");
        store
            .create_user(&UserRecord {
                user_id: user_id.clone(),
                username: username.to_string(),
                password: "pass12345".to_string(),
                email: Some(format!("{username}@t.com")),
                role: role.to_string(),
                tenant_id: user_id.clone(),
                created_at: "2026-01-01T00:00:00Z".to_string(),
                last_login_at: None,
            })
            .await
            .unwrap();
        user_id
    }

    #[tokio::test]
    async fn list_users_without_auth_returns_401_envelope() {
        let (handler, _store) = handler_with_store().await;
        let envelope = handler.handle("list_users", json!({})).await.unwrap();
        assert_eq!(envelope["status"], 401, "无凭证应 401: {envelope}");
        assert!(envelope["error"]["message"].is_string());
    }

    #[tokio::test]
    async fn list_users_returns_users_without_password() {
        let (handler, store) = handler_with_store().await;
        seed_user(&store, "alice", "user").await;
        let envelope = handler
            .handle("list_users", authed(json!({})))
            .await
            .unwrap();
        assert_eq!(envelope["status"], 200, "{envelope}");
        let users = envelope["body"]["users"].as_array().unwrap();
        assert_eq!(envelope["body"]["total"], users.len() as u64);
        assert_eq!(users.len(), 2, "admin + alice: {users:?}");
        for u in users {
            assert!(
                u.get("password").is_none(),
                "管理面响应不得含 password: {u}"
            );
            assert!(u["id"].is_string() && u["role"].is_string() && u["tenant_id"].is_string());
        }
    }

    #[tokio::test]
    async fn update_role_changes_and_returns_sanitized_user() {
        let (handler, store) = handler_with_store().await;
        let alice = seed_user(&store, "alice", "user").await;
        let envelope = handler
            .handle(
                "update_role",
                authed(json!({ "user_id": alice, "role": "admin" })),
            )
            .await
            .unwrap();
        assert_eq!(envelope["status"], 200, "{envelope}");
        assert_eq!(envelope["body"]["user"]["role"], "admin");
        assert!(envelope["body"]["user"].get("password").is_none());
        // 落库验证
        let updated = store.get_user_by_id(&alice).await.unwrap().unwrap();
        assert_eq!(updated.role, "admin");
    }

    #[tokio::test]
    async fn update_role_validates_role_whitelist_and_404() {
        let (handler, store) = handler_with_store().await;
        let alice = seed_user(&store, "alice", "user").await;

        // 非法角色 → 400
        let envelope = handler
            .handle(
                "update_role",
                authed(json!({ "user_id": alice, "role": "superadmin" })),
            )
            .await
            .unwrap();
        assert_eq!(envelope["status"], 400, "{envelope}");

        // 不存在的用户 → 404
        let envelope = handler
            .handle(
                "update_role",
                authed(json!({ "user_id": "u-nope", "role": "admin" })),
            )
            .await
            .unwrap();
        assert_eq!(envelope["status"], 404, "{envelope}");

        // 缺参数 → 400
        let envelope = handler
            .handle("update_role", authed(json!({ "user_id": alice })))
            .await
            .unwrap();
        assert_eq!(envelope["status"], 400, "{envelope}");
    }

    #[tokio::test]
    async fn update_tenant_moves_user_tenant() {
        let (handler, store) = handler_with_store().await;
        let alice = seed_user(&store, "alice", "user").await;
        let envelope = handler
            .handle(
                "update_tenant",
                authed(json!({ "user_id": alice, "tenant_id": "team-b" })),
            )
            .await
            .unwrap();
        assert_eq!(envelope["status"], 200, "{envelope}");
        assert_eq!(envelope["body"]["user"]["tenant_id"], "team-b");
        let updated = store.get_user_by_id(&alice).await.unwrap().unwrap();
        assert_eq!(updated.tenant_id, "team-b");

        // 空租户 → 400；不存在用户 → 404
        let envelope = handler
            .handle(
                "update_tenant",
                authed(json!({ "user_id": alice, "tenant_id": "  " })),
            )
            .await
            .unwrap();
        assert_eq!(envelope["status"], 400, "{envelope}");
        let envelope = handler
            .handle(
                "update_tenant",
                authed(json!({ "user_id": "u-nope", "tenant_id": "t" })),
            )
            .await
            .unwrap();
        assert_eq!(envelope["status"], 404, "{envelope}");
    }

    #[tokio::test]
    async fn delete_user_removes_and_404_on_missing() {
        let (handler, store) = handler_with_store().await;
        let alice = seed_user(&store, "alice", "user").await;
        let envelope = handler
            .handle("delete_user", authed(json!({ "user_id": alice })))
            .await
            .unwrap();
        assert_eq!(envelope["status"], 200, "{envelope}");
        assert_eq!(envelope["body"]["deleted"], true);
        assert!(store.get_user_by_id(&alice).await.unwrap().is_none());

        let envelope = handler
            .handle("delete_user", authed(json!({ "user_id": "u-nope" })))
            .await
            .unwrap();
        assert_eq!(envelope["status"], 404, "{envelope}");
    }

    #[tokio::test]
    async fn self_protection_blocks_all_mutations_on_self() {
        let (handler, _store) = handler_with_store().await;
        let admin_id = default_users()[0].id.clone();
        // 删自己 → 403
        let envelope = handler
            .handle("delete_user", authed(json!({ "user_id": admin_id })))
            .await
            .unwrap();
        assert_eq!(envelope["status"], 403, "admin 删自己应 403: {envelope}");
        // 降自己角色 → 403
        let envelope = handler
            .handle(
                "update_role",
                authed(json!({ "user_id": admin_id, "role": "user" })),
            )
            .await
            .unwrap();
        assert_eq!(
            envelope["status"], 403,
            "admin 降自己角色应 403: {envelope}"
        );
        // 改自己租户 → 403
        let envelope = handler
            .handle(
                "update_tenant",
                authed(json!({ "user_id": admin_id, "tenant_id": "other" })),
            )
            .await
            .unwrap();
        assert_eq!(
            envelope["status"], 403,
            "admin 改自己租户应 403: {envelope}"
        );
    }

    #[tokio::test]
    async fn unknown_method_rejected() {
        let (handler, _store) = handler_with_store().await;
        let err = handler.handle("users/list", json!({})).await;
        assert!(err.is_err(), "不在清单的 method 应拒绝");
        let msg = format!("{}", err.unwrap_err());
        assert!(msg.contains("not implemented"), "{msg}");
    }

    #[tokio::test]
    async fn registry_route_via_trait_roundtrip() {
        // 经 CapabilityHandlerRegistry（生产 reader loop 的真实路由路径）验证注册即路由。
        let registry = Arc::new(agentos_mcp::CapabilityHandlerRegistry::new());
        let (handler, _store) = handler_with_store().await;
        registry.register(Arc::new(handler));
        assert!(registry.has_namespace(NAMESPACE));
        let router: Arc<dyn agentos_mcp::CapabilityRouter> = registry;
        assert!(router.known_namespaces().contains(&NAMESPACE.to_string()));
        let envelope = router
            .handle(NAMESPACE, "list_users", authed(json!({})))
            .await
            .unwrap();
        assert_eq!(envelope["status"], 200);
    }
}
