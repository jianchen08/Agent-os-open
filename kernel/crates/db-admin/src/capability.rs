//! db-admin capability handler——SQL 能力层（boot-plugin 第一刀）。
//!
//! 把原 `/api/v1/db/*` 7 端点的全部逻辑收敛为 `db-admin` namespace 的
//! [`CapabilityHandler`]，注册进内核 `CapabilityHandlerRegistry`
//! （agentos-kernel.rs 启动期，先于任何 sidecar spawn）。HTTP 面由
//! `plugins/shared/db_admin`（Python sidecar 插件）承载：内核 `/ext/{*rest}`
//! 通配分发 → 插件 `http.handle` → 反向调用 `db-admin.<method>` → 本 handler。
//!
//! ## method 清单（7 个，与原端点一一对应）
//!
//! | method | 原端点 | 角色要求 |
//! |---|---|---|
//! | `list_tables` | GET /api/v1/db/tables | admin/viewer |
//! | `table_query` | GET /api/v1/db/table/{table} | admin/viewer |
//! | `table_insert` | POST /api/v1/db/table/{table} | admin |
//! | `table_get_row` | GET /api/v1/db/table/{table}/{pk} | admin/viewer |
//! | `table_update_row` | PATCH /api/v1/db/table/{table}/{pk} | admin |
//! | `table_delete_row` | DELETE /api/v1/db/table/{table}/{pk} | admin |
//! | `execute` | POST /api/v1/db/execute | admin |
//!
//! 注：任务书原文 method 写作 `tables/list_tables`、`table/query` 等（含 `/`），
//! 但内核反向调用协议 [`parse_capability_method_with`] 拒绝含 `/` 的 method
//! （capability.rs:125，`/` 是 MCP 标准方法前缀的分隔符），故以 `_` 替代 `/`。
//!
//! ## 鉴权落点（信任锚点）
//!
//! HTTP 面插件**只透传凭证、不做鉴权决策**：它把入站请求的 `Authorization`
//! 头原样放进 params 的 `_authorization` 字段；本 handler 在内核侧重建
//! HeaderMap 后复用拆分前的 `resolve_request_user` + `require_read_role` /
//! `require_admin_role`（与 api 管理面同一实现）。角色/租户解析、租户隔离
//! 注入全部留在内核——插件无法伪造 `_user_role`/`_tenant_id`（不接收这类参数）。
//! manifest 的 `http_endpoints[].auth: "admin"` 目前是声明性字段，内核
//! dispatcher 不执行它（见交付报告鉴权说明），实际执行点在本 handler。
//!
//! ## 响应信封
//!
//! capability 调用成功返回 `{status, body}`（status=200/201）；
//! 业务失败返回 `{status, error: {code, message}}`（400/401/403/404/500，
//! 与拆分前 ApiError → HTTP 状态码语义一致）。插件据此组 HTTP 响应，
//! body 形状与拆分前 JSON 端点完全一致（前端无感知）。
//!
//! [来源: docs/working/重要设计/boot-plugin内核能力插件化立项.md §三]

use std::sync::Arc;
use std::time::Duration;

use agentos_http::auth::resolve_request_user;
use agentos_http::error::ApiError;
use agentos_mcp::{CapabilityHandler, McpError};
use async_trait::async_trait;
use axum::http::{header::AUTHORIZATION, HeaderMap, HeaderValue};
use serde_json::{json, Value};
use tokio::task::spawn_blocking;

use crate::db_routes::{
    delete_row_inner, execute_sql_inner, get_row_inner, insert_row_inner, query_rows_inner,
    update_row_inner, ListParams,
};

/// db-admin capability 的 namespace（manifest granted_capabilities 与此对齐）。
pub const NAMESPACE: &str = "db-admin";

/// handler 所需状态（原 [`crate::db_routes::DbAdminState`] 语义不变）。
#[derive(Clone)]
pub struct DbAdminState {
    /// 存储后端（用户解析 / 租户解析用；与 api `AppState.store` 同一实例）。
    pub store: Option<Arc<dyn agentos_core::traits::StorageBackend>>,
    /// 统一数据接口 db 句柄（引擎 SqliteStore）。
    pub db: Option<Arc<agentos_engine::SqliteStore>>,
}

/// 只读接口角色校验：admin 或 viewer。返回当前请求租户 ID。
pub async fn require_read_role(
    state: &DbAdminState,
    headers: &HeaderMap,
) -> Result<String, ApiError> {
    let (_, _, role, tenant_id) = resolve_request_user(state.store.as_ref(), headers).await?;
    if role != "admin" && role != "viewer" {
        return Err(ApiError::Forbidden {
            message: "需要 admin 或 viewer 角色".to_string(),
        });
    }
    Ok(tenant_id)
}

/// 写接口角色校验：仅 admin。返回当前请求租户 ID。
pub async fn require_admin_role(
    state: &DbAdminState,
    headers: &HeaderMap,
) -> Result<String, ApiError> {
    let (_, _, role, tenant_id) = resolve_request_user(state.store.as_ref(), headers).await?;
    if role != "admin" {
        return Err(ApiError::Forbidden {
            message: "写操作需要 admin 角色".to_string(),
        });
    }
    Ok(tenant_id)
}

/// 获取统一数据接口 db 句柄。
fn get_db(state: &DbAdminState) -> Result<Arc<agentos_engine::SqliteStore>, ApiError> {
    state.db.clone().ok_or_else(|| ApiError::BadRequest {
        message: "统一数据接口未启用（db 未注入）".to_string(),
    })
}

/// `db-admin` namespace 的 capability handler（7 method，逻辑与拆分前端点一致）。
pub struct DbAdminCapabilityHandler {
    state: DbAdminState,
}

impl DbAdminCapabilityHandler {
    /// 创建 handler。
    ///
    /// Args:
    /// - `store`: 用户/租户解析用的存储后端（api `AppState.store` 同一实例）；
    /// - `db`: 统一数据接口 db 句柄（引擎 SqliteStore）。
    pub fn new(
        store: Option<Arc<dyn agentos_core::traits::StorageBackend>>,
        db: Option<Arc<agentos_engine::SqliteStore>>,
    ) -> Self {
        Self {
            state: DbAdminState { store, db },
        }
    }

    /// 从 params 的 `_authorization`（HTTP 面插件转发的原始 Authorization 头值，
    /// 如 `Bearer eyJ...`）重建 HeaderMap，复用既有 require_* 鉴权链。
    fn auth_headers(params: &Value) -> HeaderMap {
        let mut headers = HeaderMap::new();
        if let Some(auth) = params.get("_authorization").and_then(|v| v.as_str()) {
            if let Ok(v) = HeaderValue::from_str(auth) {
                headers.insert(AUTHORIZATION, v);
            }
        }
        headers
    }

    /// 必填 string 参数提取（缺失/类型不符 → 400，对齐原路径参数语义）。
    fn require_str<'a>(params: &'a Value, key: &str) -> Result<&'a str, ApiError> {
        params
            .get(key)
            .and_then(|v| v.as_str())
            .ok_or_else(|| ApiError::BadRequest {
                message: format!("缺少或非法的 {key} 参数"),
            })
    }

    /// 把 params 中的 limit/offset/filter/sort 组装为 [`ListParams`]。
    ///
    /// `filter` 接受单字符串或字符串数组（capability 层保留多条件 AND 全量语义；
    /// HTTP 面经内核 dispatcher 的 `HashMap<String, String>` query 透传时重复
    /// key 会塌缩成最后一个值——见 server.py 与交付报告的取舍说明）。
    fn list_params(params: &Value) -> ListParams {
        let mut p = ListParams::default();
        if let Some(v) = params.get("limit") {
            p.limit = v
                .as_i64()
                .or_else(|| v.as_str().and_then(|s| s.parse().ok()));
        }
        if let Some(v) = params.get("offset") {
            p.offset = v
                .as_i64()
                .or_else(|| v.as_str().and_then(|s| s.parse().ok()));
        }
        match params.get("filter") {
            Some(Value::String(s)) => p.filter.push(s.clone()),
            Some(Value::Array(arr)) => {
                for v in arr {
                    if let Some(s) = v.as_str() {
                        p.filter.push(s.to_string());
                    }
                }
            }
            _ => {}
        }
        if let Some(s) = params.get("sort").and_then(|v| v.as_str()) {
            p.sort = Some(s.to_string());
        }
        p
    }

    // ─── 端点 1：list_tables（原 GET /tables） ─────────────────────────

    async fn list_tables(&self, params: &Value) -> Result<(u16, Value), ApiError> {
        let headers = Self::auth_headers(params);
        let _tenant_id = require_read_role(&self.state, &headers).await?;
        let db = get_db(&self.state)?;
        let result = spawn_blocking(move || {
            db.with_conn(|conn| {
                let names = crate::db_routes::list_table_names(conn)?;
                let mut tables = Vec::with_capacity(names.len());
                for name in &names {
                    let cols = crate::db_routes::get_table_columns(conn, name)?;
                    let col_objs: Vec<Value> = cols
                        .iter()
                        .map(|c| {
                            json!({
                                "name": c.name,
                                "type": c.type_name,
                                "pk": c.pk,
                                "notnull": c.notnull,
                            })
                        })
                        .collect();
                    let count_sql = format!(
                        "SELECT COUNT(*) FROM {}",
                        crate::db_routes::quote_ident(name)
                    );
                    let row_count: i64 =
                        conn.query_row(&count_sql, [], |r| r.get(0)).map_err(|e| {
                            ApiError::Internal {
                                message: format!("统计行数失败: {e}"),
                            }
                        })?;
                    tables.push(json!({
                        "name": name,
                        "columns": col_objs,
                        "row_count": row_count,
                    }));
                }
                Ok(json!({ "tables": tables }))
            })
        })
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("数据库任务失败: {e}"),
        })??;
        Ok((200, result))
    }

    // ─── 端点 2：table_query（原 GET /table/{table}） ──────────────────

    async fn query_rows(&self, params: &Value) -> Result<(u16, Value), ApiError> {
        let headers = Self::auth_headers(params);
        let tenant_id = require_read_role(&self.state, &headers).await?;
        let table = Self::require_str(params, "table")?.to_string();
        let list_params = Self::list_params(params);
        let db = get_db(&self.state)?;
        let result = spawn_blocking(move || {
            db.with_conn(|conn| query_rows_inner(conn, &table, &list_params, &tenant_id))
        })
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("数据库任务失败: {e}"),
        })??;
        Ok((200, result))
    }

    // ─── 端点 3：table_insert（原 POST /table/{table}，201） ───────────

    async fn insert_row(&self, params: &Value) -> Result<(u16, Value), ApiError> {
        let headers = Self::auth_headers(params);
        let tenant_id = require_admin_role(&self.state, &headers).await?;
        let table = Self::require_str(params, "table")?.to_string();
        let row = params
            .get("row")
            .cloned()
            .ok_or_else(|| ApiError::BadRequest {
                message: "缺少 row 参数".to_string(),
            })?;
        let db = get_db(&self.state)?;
        let result = spawn_blocking(move || {
            db.with_conn(|conn| insert_row_inner(conn, &table, &row, &tenant_id))
        })
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("数据库任务失败: {e}"),
        })??;
        Ok((201, result))
    }

    // ─── 端点 4：table_get_row（原 GET /table/{table}/{pk}） ───────────

    async fn get_row(&self, params: &Value) -> Result<(u16, Value), ApiError> {
        let headers = Self::auth_headers(params);
        let tenant_id = require_read_role(&self.state, &headers).await?;
        let table = Self::require_str(params, "table")?.to_string();
        let pk_value = Self::require_str(params, "pk_value")?.to_string();
        let db = get_db(&self.state)?;
        let result = spawn_blocking(move || {
            db.with_conn(|conn| get_row_inner(conn, &table, &pk_value, &tenant_id))
        })
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("数据库任务失败: {e}"),
        })??;
        Ok((200, result))
    }

    // ─── 端点 5：table_update_row（原 PATCH /table/{table}/{pk}） ──────

    async fn update_row(&self, params: &Value) -> Result<(u16, Value), ApiError> {
        let headers = Self::auth_headers(params);
        let tenant_id = require_admin_role(&self.state, &headers).await?;
        let table = Self::require_str(params, "table")?.to_string();
        let pk_value = Self::require_str(params, "pk_value")?.to_string();
        let updates = params
            .get("updates")
            .cloned()
            .ok_or_else(|| ApiError::BadRequest {
                message: "缺少 updates 参数".to_string(),
            })?;
        let db = get_db(&self.state)?;
        let result = spawn_blocking(move || {
            db.with_conn(|conn| update_row_inner(conn, &table, &pk_value, &updates, &tenant_id))
        })
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("数据库任务失败: {e}"),
        })??;
        Ok((200, result))
    }

    // ─── 端点 6：table_delete_row（原 DELETE /table/{table}/{pk}） ─────

    async fn delete_row(&self, params: &Value) -> Result<(u16, Value), ApiError> {
        let headers = Self::auth_headers(params);
        let tenant_id = require_admin_role(&self.state, &headers).await?;
        let table = Self::require_str(params, "table")?.to_string();
        let pk_value = Self::require_str(params, "pk_value")?.to_string();
        let db = get_db(&self.state)?;
        let result = spawn_blocking(move || {
            db.with_conn(|conn| delete_row_inner(conn, &table, &pk_value, &tenant_id))
        })
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("数据库任务失败: {e}"),
        })??;
        Ok((200, result))
    }

    // ─── 端点 7：execute（原 POST /execute，5s 超时） ──────────────────

    async fn execute_sql(&self, params: &Value) -> Result<(u16, Value), ApiError> {
        let headers = Self::auth_headers(params);
        let _tenant_id = require_admin_role(&self.state, &headers).await?;
        let sql = Self::require_str(params, "sql")?.to_string();
        let confirm = params
            .get("confirm")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let db = get_db(&self.state)?;
        let handle =
            spawn_blocking(move || db.with_conn(|conn| execute_sql_inner(conn, &sql, confirm)));
        let result = tokio::time::timeout(Duration::from_secs(5), handle)
            .await
            .map_err(|_| ApiError::Internal {
                message: "SQL 执行超时（>5s）".to_string(),
            })?
            .map_err(|e| ApiError::Internal {
                message: format!("数据库任务失败: {e}"),
            })??;
        Ok((200, result))
    }
}

/// ApiError → (HTTP 状态码, 消息)。与拆分前 `ApiError::IntoResponse` 的映射一致。
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
impl CapabilityHandler for DbAdminCapabilityHandler {
    fn namespace(&self) -> &str {
        NAMESPACE
    }

    async fn handle(&self, method: &str, params: Value) -> Result<Value, McpError> {
        let result: Result<(u16, Value), ApiError> = match method {
            "list_tables" => self.list_tables(&params).await,
            "table_query" => self.query_rows(&params).await,
            "table_insert" => self.insert_row(&params).await,
            "table_get_row" => self.get_row(&params).await,
            "table_update_row" => self.update_row(&params).await,
            "table_delete_row" => self.delete_row(&params).await,
            "execute" => self.execute_sql(&params).await,
            other => {
                return Err(McpError::Protocol {
                    message: format!(
                        "{NAMESPACE}.{other} not implemented (known: list_tables, table_query, \
                         table_insert, table_get_row, table_update_row, table_delete_row, execute)"
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
    use agentos_mcp::CapabilityRouter;
    use serde_json::json;

    /// 内存库 + 仅注入 db 的 state（无 store：token 校验走内置 admin 回退）。
    /// 专用夹具表 test_notes：0.1 投影表 memory 已 DROP（2026-08-19），通用
    /// CRUD/过滤/租户隔离语义用结构等价测试表承载，断言语义不变。
    fn handler_with_db() -> (DbAdminCapabilityHandler, Arc<agentos_engine::SqliteStore>) {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        store
            .with_conn(|conn| {
                conn.execute_batch(
                    "CREATE TABLE test_notes (                         id TEXT PRIMARY KEY, content TEXT, note_type TEXT, score REAL,                         tenant_id TEXT NOT NULL DEFAULT 'default', created_at TEXT NOT NULL                     );",
                )
                .unwrap();
                Ok::<(), String>(())
            })
            .unwrap();
        (
            DbAdminCapabilityHandler::new(None, Some(store.clone())),
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

    #[tokio::test]
    async fn list_tables_without_auth_returns_401_envelope() {
        let (handler, _store) = handler_with_db();
        let envelope = handler.handle("list_tables", json!({})).await.unwrap();
        assert_eq!(envelope["status"], 401, "无凭证应 401: {envelope}");
        assert!(envelope["error"]["message"].is_string());
    }

    #[tokio::test]
    async fn list_tables_with_admin_token_returns_tables() {
        let (handler, _store) = handler_with_db();
        let envelope = handler
            .handle("list_tables", authed(json!({})))
            .await
            .unwrap();
        assert_eq!(envelope["status"], 200, "{envelope}");
        let names: Vec<&str> = envelope["body"]["tables"]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| t["name"].as_str().unwrap())
            .collect();
        assert!(names.contains(&"runs"), "应枚举引擎表: {names:?}");
    }

    #[tokio::test]
    async fn query_rows_multi_filter_and_at_capability_layer() {
        // capability 层 filter 接受数组——多条件 AND 全量语义在此保留
        //（HTTP 面经 dispatcher 的单值 query 透传会塌缩，见模块文档）。
        let (handler, store) = handler_with_db();
        store
            .with_conn(|conn| {
                for (i, (mt, sc)) in
                    [("episode", 1.0f64), ("episode", 5.0), ("semantic", 2.0)].iter().enumerate()
                {
                    conn.execute(
                        "INSERT INTO test_notes (id, content, note_type, score, tenant_id, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                        rusqlite::params![
                            format!("mf{i}"),
                            format!("content {i}"),
                            mt,
                            sc,
                            "default",
                            format!("2025-01-0{}T00:00:00Z", i + 1),
                        ],
                    )
                    .unwrap();
                }
                Ok::<(), String>(())
            })
            .unwrap();
        let envelope = handler
            .handle(
                "table_query",
                authed(json!({
                    "table": "test_notes",
                    "filter": ["note_type:eq:episode", "score:gt:3"],
                })),
            )
            .await
            .unwrap();
        assert_eq!(envelope["status"], 200, "{envelope}");
        assert_eq!(envelope["body"]["total"], 1, "多条件 AND 交集: {envelope}");
        assert_eq!(envelope["body"]["rows"][0]["id"], "mf1");
    }

    #[tokio::test]
    async fn insert_get_update_delete_roundtrip() {
        let (handler, _store) = handler_with_db();
        // 插入 → 201 + row/row_id
        let env = handler
            .handle(
                "table_insert",
                authed(json!({
                    "table": "test_notes",
                    "row": { "id": "cap1", "content": "hello", "note_type": "episode", "created_at": "2025-01-01T00:00:00Z" },
                })),
            )
            .await
            .unwrap();
        assert_eq!(env["status"], 201, "{env}");
        assert_eq!(env["body"]["row_id"], "cap1");
        assert_eq!(env["body"]["row"]["tenant_id"], "default", "租户自动注入");

        // 单行读
        let env = handler
            .handle(
                "table_get_row",
                authed(json!({ "table": "test_notes", "pk_value": "cap1" })),
            )
            .await
            .unwrap();
        assert_eq!(env["status"], 200);
        assert_eq!(env["body"]["content"], "hello");

        // 更新
        let env = handler
            .handle(
                "table_update_row",
                authed(json!({ "table": "test_notes", "pk_value": "cap1", "updates": { "content": "updated" } })),
            )
            .await
            .unwrap();
        assert_eq!(env["status"], 200, "{env}");
        assert_eq!(env["body"]["row"]["content"], "updated");

        // 更新不存在 → 404
        let env = handler
            .handle(
                "table_update_row",
                authed(
                    json!({ "table": "test_notes", "pk_value": "nope", "updates": { "content": "x" } }),
                ),
            )
            .await
            .unwrap();
        assert_eq!(env["status"], 404, "{env}");

        // 删除
        let env = handler
            .handle(
                "table_delete_row",
                authed(json!({ "table": "test_notes", "pk_value": "cap1" })),
            )
            .await
            .unwrap();
        assert_eq!(env["status"], 200);
        assert_eq!(env["body"]["deleted"], true);
    }

    #[tokio::test]
    async fn execute_sql_write_requires_confirm() {
        let (handler, _store) = handler_with_db();
        let envelope = handler
            .handle(
                "execute",
                authed(json!({
                    "sql": "UPDATE test_notes SET content='x' WHERE id='none'",
                    "confirm": false,
                })),
            )
            .await
            .unwrap();
        assert_eq!(
            envelope["status"], 400,
            "写语句无 confirm 应 400: {envelope}"
        );
    }

    #[tokio::test]
    async fn execute_sql_dangerous_returns_403_envelope() {
        let (handler, _store) = handler_with_db();
        let envelope = handler
            .handle(
                "execute",
                authed(json!({ "sql": "DROP TABLE test_notes", "confirm": true })),
            )
            .await
            .unwrap();
        assert_eq!(envelope["status"], 403, "DROP 应 403: {envelope}");
    }

    #[tokio::test]
    async fn unknown_method_rejected() {
        let (handler, _store) = handler_with_db();
        let err = handler.handle("tables/list_tables", json!({})).await;
        assert!(err.is_err(), "含 / 的 method 不在清单，应拒绝");
        let msg = format!("{}", err.unwrap_err());
        assert!(msg.contains("not implemented"), "{msg}");
    }

    #[tokio::test]
    async fn registry_route_via_trait_roundtrip() {
        // 经 CapabilityHandlerRegistry（生产 reader loop 的真实路由路径）验证注册即路由。
        let registry = Arc::new(agentos_mcp::CapabilityHandlerRegistry::new());
        let (handler, _store) = handler_with_db();
        registry.register(Arc::new(handler));
        assert!(registry.has_namespace(NAMESPACE));
        let router: Arc<dyn CapabilityRouter> = registry;
        assert!(router.known_namespaces().contains(&NAMESPACE.to_string()));
        let envelope = router
            .handle(NAMESPACE, "list_tables", authed(json!({})))
            .await
            .unwrap();
        assert_eq!(envelope["status"], 200);
    }
}
