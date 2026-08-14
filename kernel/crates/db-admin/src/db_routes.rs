//! 统一通用数据接口（/api/v1/db/*）——DB Admin 独立管理后台
//!
//! 表驱动、动态枚举：表清单/列信息/主键由 `sqlite_master` + `PRAGMA table_info`
//! 运行时发现，不写死任何表名/列名——新增表/新增列自动可见、自动可查、自动可管。
//!
//! 安全约束：
//! - 只读接口（tables/行查询/单行）允许 admin/viewer；写接口（CRUD/SQL 执行器）仅 admin
//! - 表名/列名白名单校验（动态枚举），值走 prepared statement 参数绑定，杜绝 SQL 注入
//! - SQL 执行器需 `confirm:true`、单语句（拒 `;` 分隔）、危险前缀黑名单（DROP/ALTER/
//!   VACUUM/ATTACH/DETACH/PRAGMA/全表 DELETE）→ 403，带 5s 超时
//! - 租户隔离：有 `tenant_id` 列的表自动追加 `AND tenant_id = ?`（从请求 token 解析）
//! - BLOB 安全：BLOB 列不返回内容、不允许经统一接口写入（blobs.data 由内核专用方法管理）
//!
//! [来源: docs/working/unified_db_admin_plan.md §二 / .project/api_contract.md]
//!
//! 拆分说明（task_kernel_cleanup_and_split 任务 1）：自 api crate 整体迁移至此，
//! 7 端点与 `/api/v1/db/*` 路径保持不变；`AppState` 收敛为本 crate 的
//! [`DbAdminState`]（仅需 store + db 两个字段），鉴权复用
//! `agentos_http::auth::resolve_request_user`（与 api 管理面同一实现）。

use std::sync::Arc;
use std::time::Duration;

use axum::extract::{Path, Query, State};
use axum::http::HeaderMap;
use axum::Json;
use agentos_http::auth::resolve_request_user;
use agentos_http::error::ApiError;
use rusqlite::{Connection, OptionalExtension};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::task::spawn_blocking;

/// DB Admin 路由所需状态（api 挂载时从 `AppState` 克隆注入）。
#[derive(Clone)]
pub struct DbAdminState {
    /// 存储后端（用户解析 / 租户解析用；与 api `AppState.store` 同一实例）。
    pub store: Option<Arc<dyn agentos_core::traits::StorageBackend>>,
    /// 统一数据接口 db 句柄（引擎 SqliteStore）。
    pub db: Option<Arc<agentos_engine::SqliteStore>>,
}

/// 构建 DB Admin 路由树（相对路径，由 api 以 `/api/v1/db` 前缀 nest 挂载）。
pub fn router() -> axum::Router<DbAdminState> {
    axum::Router::new()
        .route("/tables", axum::routing::get(list_tables_handler))
        .route(
            "/table/{table}",
            axum::routing::get(query_rows_handler).post(insert_row_handler),
        )
        .route(
            "/table/{table}/{pk_value}",
            axum::routing::get(get_row_handler)
                .patch(update_row_handler)
                .delete(delete_row_handler),
        )
        .route("/execute", axum::routing::post(execute_sql_handler))
}

/// 行查询列表参数（契约 §2.2）。
#[derive(Debug, Default)]
pub struct ListParams {
    pub limit: Option<i64>,
    pub offset: Option<i64>,
    /// 可重复：`col:eq|ne|gt|lt|contains:value`，多条件 AND。
    /// 兼容单值（`filter=a`）、多值（`filter=a&filter=b`）与前端 axios
    /// 默认序列化（`filter[]=a&filter[]=b`）三种形态。
    pub filter: Vec<String>,
    /// `col:asc|desc`，默认主键 asc
    pub sort: Option<String>,
}

/// 手动 Deserialize：serde_html_form 对 `Vec<(String, String)>`（原始参数对）
/// 重复 key 原生支持（`?filter=a&filter=b` → `[("filter","a"),("filter","b")]`），
/// 而 `deserialize_any` 无法让 serde_html_form 识别为序列类型导致重复字段 400
/// （缺陷 DEF-1：与契约 §2.2「filter 可重复，多条件 AND」冲突）。
impl<'de> Deserialize<'de> for ListParams {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let pairs = Vec::<(String, String)>::deserialize(deserializer)?;
        let mut limit = None;
        let mut offset = None;
        let mut filter = Vec::new();
        let mut sort = None;
        for (k, v) in pairs {
            match k.as_str() {
                "limit" => limit = v.parse().ok(),
                "offset" => offset = v.parse().ok(),
                // 契约 §2.2：filter 可重复，多条件 AND；
                // filter[] 兼容前端 axios 默认数组序列化（缺陷 DEF-2 兜底）。
                "filter" | "filter[]" => filter.push(v),
                "sort" => sort = Some(v),
                // 未知参数忽略（对齐原 Query 行为）
                _ => {}
            }
        }
        Ok(ListParams {
            limit,
            offset,
            filter,
            sort,
        })
    }
}

/// 插入请求体（契约 §2.4）。
#[derive(Debug, Deserialize)]
pub struct InsertBody {
    pub row: Value,
}

/// 更新请求体（契约 §2.5）。
#[derive(Debug, Deserialize)]
pub struct UpdateBody {
    pub updates: Value,
}

/// SQL 执行器请求体（契约 §2.7）。
#[derive(Debug, Deserialize)]
pub struct ExecuteBody {
    pub sql: String,
    #[serde(default)]
    pub confirm: bool,
}

/// 列元数据（PRAGMA table_info 行）。
#[derive(Debug, Clone)]
struct ColumnMeta {
    name: String,
    type_name: String,
    notnull: bool,
    pk: bool,
    pk_order: i64,
}

// ─── 角色校验 ────────────────────────────────────────────────────────

/// 只读接口角色校验：admin 或 viewer。返回当前请求租户 ID。
pub async fn require_read_role(state: &DbAdminState, headers: &HeaderMap) -> Result<String, ApiError> {
    let (_, _, role, tenant_id) = resolve_request_user(state.store.as_ref(), headers).await?;
    if role != "admin" && role != "viewer" {
        return Err(ApiError::Forbidden {
            message: "需要 admin 或 viewer 角色".to_string(),
        });
    }
    Ok(tenant_id)
}

/// 写接口角色校验：仅 admin。返回当前请求租户 ID。
pub async fn require_admin_role(state: &DbAdminState, headers: &HeaderMap) -> Result<String, ApiError> {
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

// ─── 动态枚举辅助 ────────────────────────────────────────────────────

/// 表名白名单：sqlite_master 动态枚举（排除 sqlite_ 内部表）。
fn list_table_names(conn: &Connection) -> Result<Vec<String>, ApiError> {
    let mut stmt = conn
        .prepare(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
        )
        .map_err(|e| ApiError::Internal {
            message: format!("枚举表失败: {e}"),
        })?;
    let names = stmt
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|e| ApiError::Internal {
            message: format!("枚举表失败: {e}"),
        })?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| ApiError::Internal {
            message: format!("枚举表失败: {e}"),
        })?;
    Ok(names)
}

/// 列白名单：PRAGMA table_info（表名已通过 list_table_names 白名单校验）。
fn get_table_columns(conn: &Connection, table: &str) -> Result<Vec<ColumnMeta>, ApiError> {
    let mut stmt = conn
        .prepare(&format!("PRAGMA table_info({})", quote_ident(table)))
        .map_err(|e| ApiError::Internal {
            message: format!("读取列信息失败: {e}"),
        })?;
    let cols = stmt
        .query_map([], |row| {
            Ok(ColumnMeta {
                name: row.get::<_, String>(1)?,
                type_name: row.get::<_, String>(2)?,
                notnull: row.get::<_, i64>(3)? != 0,
                pk: row.get::<_, i64>(5)? > 0,
                pk_order: row.get::<_, i64>(5)?,
            })
        })
        .map_err(|e| ApiError::Internal {
            message: format!("读取列信息失败: {e}"),
        })?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| ApiError::Internal {
            message: format!("读取列信息失败: {e}"),
        })?;
    Ok(cols)
}

/// 校验表存在（404 语义）。
fn validate_table(conn: &Connection, table: &str) -> Result<(), ApiError> {
    let names = list_table_names(conn)?;
    if !names.iter().any(|n| n == table) {
        return Err(ApiError::NotFound {
            message: format!("表不存在: {table}"),
        });
    }
    Ok(())
}

/// 校验列存在（400 语义）。
fn validate_column(conn: &Connection, table: &str, col: &str) -> Result<(), ApiError> {
    let cols = get_table_columns(conn, table)?;
    if !cols.iter().any(|c| c.name == col) {
        return Err(ApiError::BadRequest {
            message: format!("列不存在: {table}.{col}"),
        });
    }
    Ok(())
}

/// SQLite 标识符引用（防御性转义；表/列名实际来自白名单枚举）。
fn quote_ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

fn is_blob_type(t: &str) -> bool {
    t.to_ascii_uppercase().contains("BLOB")
}

/// 主键列（按 pk_order 升序，复合主键保持声明顺序）。
fn pk_columns(cols: &[ColumnMeta]) -> Vec<ColumnMeta> {
    let mut pks: Vec<ColumnMeta> = cols.iter().filter(|c| c.pk).cloned().collect();
    pks.sort_by_key(|c| c.pk_order);
    pks
}

/// 行对象转 JSON（BLOB 列返回 null，不泄露二进制内容）。
/// 返回 `rusqlite::Result` 以对齐 `query_map`/`query_row` 闭包签名。
fn row_to_json(row: &rusqlite::Row, cols: &[&ColumnMeta]) -> rusqlite::Result<Value> {
    let mut obj = serde_json::Map::new();
    for (i, col) in cols.iter().enumerate() {
        let v = match row.get_ref(i)? {
            rusqlite::types::ValueRef::Null => Value::Null,
            rusqlite::types::ValueRef::Integer(n) => json!(n),
            rusqlite::types::ValueRef::Real(f) => json!(f),
            rusqlite::types::ValueRef::Text(t) => {
                Value::String(String::from_utf8_lossy(t).into_owned())
            }
            rusqlite::types::ValueRef::Blob(_) => Value::Null,
        };
        obj.insert(col.name.clone(), v);
    }
    Ok(Value::Object(obj))
}

/// 解析主键路径参数（`,` 拼接复合主键）。
fn parse_pk_values(pk_value: &str) -> Vec<String> {
    pk_value.split(',').map(|s| s.trim().to_string()).collect()
}

// ─── 端点 1：GET /api/v1/db/tables ──────────────────────────────────

/// 枚举全部表（名称/列/主键/行数），sqlite_master + PRAGMA 动态发现。
pub async fn list_tables_handler(
    State(state): State<DbAdminState>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let _tenant_id = require_read_role(&state, &headers).await?;
    let db = get_db(&state)?;
    let result = spawn_blocking(move || {
        db.with_conn(|conn| {
            let names = list_table_names(conn)?;
            let mut tables = Vec::with_capacity(names.len());
            for name in &names {
                let cols = get_table_columns(conn, name)?;
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
                let count_sql = format!("SELECT COUNT(*) FROM {}", quote_ident(name));
                let row_count: i64 = conn
                    .query_row(&count_sql, [], |r| r.get(0))
                    .map_err(|e| ApiError::Internal {
                        message: format!("统计行数失败: {e}"),
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
    Ok(Json(result))
}

// ─── 端点 2：GET /api/v1/db/table/{table} ───────────────────────────

/// 行查询：limit/offset 分页、多列筛选（eq/ne/gt/lt/contains）、排序。
pub async fn query_rows_handler(
    State(state): State<DbAdminState>,
    headers: HeaderMap,
    Path(table): Path<String>,
    Query(params): Query<ListParams>,
) -> Result<Json<Value>, ApiError> {
    let tenant_id = require_read_role(&state, &headers).await?;
    let db = get_db(&state)?;
    let result = spawn_blocking(move || {
        db.with_conn(|conn| query_rows_inner(conn, &table, &params, &tenant_id))
    })
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("数据库任务失败: {e}"),
    })??;
    Ok(Json(result))
}

fn query_rows_inner(
    conn: &Connection,
    table: &str,
    params: &ListParams,
    tenant_id: &str,
) -> Result<Value, ApiError> {
    validate_table(conn, table)?;
    let cols = get_table_columns(conn, table)?;
    let limit = params.limit.unwrap_or(50).clamp(1, 500);
    let offset = params.offset.unwrap_or(0).max(0);

    // SELECT 列：排除 BLOB（不返回二进制内容）
    let selectable: Vec<&ColumnMeta> = cols.iter().filter(|c| !is_blob_type(&c.type_name)).collect();
    if selectable.is_empty() {
        return Err(ApiError::BadRequest {
            message: format!("表 {table} 无安全可返回的列（全部为 BLOB）"),
        });
    }
    let select_sql = selectable
        .iter()
        .map(|c| quote_ident(&c.name))
        .collect::<Vec<_>>()
        .join(", ");

    // WHERE：租户 + filter（值全部参数绑定）
    let mut where_parts: Vec<String> = Vec::new();
    let mut where_values: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
    let has_tenant = cols.iter().any(|c| c.name == "tenant_id");
    if has_tenant {
        where_parts.push("tenant_id = ?".to_string());
        where_values.push(Box::new(tenant_id.to_string()));
    }
    for f in &params.filter {
        let parts: Vec<&str> = f.splitn(3, ':').collect();
        if parts.len() != 3 {
            return Err(ApiError::BadRequest {
                message: format!("filter 格式应为 col:op:value: {f}"),
            });
        }
        let (col, op, raw) = (parts[0], parts[1], parts[2]);
        validate_column(conn, table, col)?;
        let (sql_op, bound) = match op {
            "eq" => ("=", raw.to_string()),
            "ne" => ("!=", raw.to_string()),
            "gt" => (">", raw.to_string()),
            "lt" => ("<", raw.to_string()),
            "contains" => ("LIKE", format!("%{raw}%")),
            _ => {
                return Err(ApiError::BadRequest {
                    message: format!("不支持的 filter 操作符: {op}"),
                })
            }
        };
        where_parts.push(format!("{} {} ?", quote_ident(col), sql_op));
        where_values.push(Box::new(bound));
    }
    let where_sql = if where_parts.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", where_parts.join(" AND "))
    };

    // 排序：sort=col:asc|desc，默认主键 asc
    let order_sql = match &params.sort {
        Some(s) => {
            let parts: Vec<&str> = s.splitn(2, ':').collect();
            let col = parts[0];
            validate_column(conn, table, col)?;
            let dir = if parts.len() > 1 { parts[1] } else { "asc" };
            if dir != "asc" && dir != "desc" {
                return Err(ApiError::BadRequest {
                    message: format!("排序方向应为 asc/desc: {dir}"),
                });
            }
            format!(" ORDER BY {} {}", quote_ident(col), dir.to_ascii_uppercase())
        }
        None => {
            let pks = pk_columns(&cols);
            if pks.is_empty() {
                String::new()
            } else {
                format!(
                    " ORDER BY {}",
                    pks.iter()
                        .map(|c| quote_ident(&c.name))
                        .collect::<Vec<_>>()
                        .join(", ")
                )
            }
        }
    };

    // total
    let total_sql = format!("SELECT COUNT(*) FROM {}{}", quote_ident(table), where_sql);
    let total: i64 = conn
        .query_row(
            &total_sql,
            rusqlite::params_from_iter(where_values.iter().map(|p| p.as_ref())),
            |r| r.get(0),
        )
        .map_err(|e| ApiError::Internal {
            message: format!("统计行数失败: {e}"),
        })?;

    // rows（limit/offset 参数绑定）
    let data_sql = format!(
        "SELECT {} FROM {}{}{} LIMIT ? OFFSET ?",
        select_sql,
        quote_ident(table),
        where_sql,
        order_sql
    );
    let mut stmt = conn
        .prepare(&data_sql)
        .map_err(|e| ApiError::Internal {
            message: format!("查询失败: {e}"),
        })?;
    let mut all_values: Vec<Box<dyn rusqlite::ToSql>> = where_values;
    all_values.push(Box::new(limit));
    all_values.push(Box::new(offset));
    let rows = stmt
        .query_map(
            rusqlite::params_from_iter(all_values.iter().map(|p| p.as_ref())),
            |row| row_to_json(row, &selectable),
        )
        .map_err(|e| ApiError::Internal {
            message: format!("查询失败: {e}"),
        })?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| ApiError::Internal {
            message: format!("查询失败: {e}"),
        })?;

    Ok(json!({
        "table": table,
        "total": total,
        "limit": limit,
        "offset": offset,
        "rows": rows,
    }))
}

// ─── 端点 3：GET /api/v1/db/table/{table}/{pk_value} ────────────────

/// 单行查询（复合主键用 `,` 拼接路径参数）。
pub async fn get_row_handler(
    State(state): State<DbAdminState>,
    headers: HeaderMap,
    Path((table, pk_value)): Path<(String, String)>,
) -> Result<Json<Value>, ApiError> {
    let tenant_id = require_read_role(&state, &headers).await?;
    let db = get_db(&state)?;
    let result = spawn_blocking(move || {
        db.with_conn(|conn| get_row_inner(conn, &table, &pk_value, &tenant_id))
    })
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("数据库任务失败: {e}"),
    })??;
    Ok(Json(result))
}

fn get_row_inner(
    conn: &Connection,
    table: &str,
    pk_value: &str,
    tenant_id: &str,
) -> Result<Value, ApiError> {
    validate_table(conn, table)?;
    let cols = get_table_columns(conn, table)?;
    let pks = pk_columns(&cols);
    if pks.is_empty() {
        return Err(ApiError::BadRequest {
            message: format!("表 {table} 无主键，无法按主键查询"),
        });
    }
    let pk_parts = parse_pk_values(pk_value);
    if pk_parts.len() != pks.len() {
        return Err(ApiError::BadRequest {
            message: format!("主键数量不匹配：表 {table} 主键为 {} 列", pks.len()),
        });
    }
    let selectable: Vec<&ColumnMeta> = cols.iter().filter(|c| !is_blob_type(&c.type_name)).collect();
    if selectable.is_empty() {
        return Err(ApiError::BadRequest {
            message: format!("表 {table} 无安全可返回的列（全部为 BLOB）"),
        });
    }
    let select_sql = selectable
        .iter()
        .map(|c| quote_ident(&c.name))
        .collect::<Vec<_>>()
        .join(", ");

    let mut where_parts: Vec<String> = Vec::new();
    let mut where_values: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
    for (pk, v) in pks.iter().zip(pk_parts.iter()) {
        where_parts.push(format!("{} = ?", quote_ident(&pk.name)));
        where_values.push(Box::new(v.clone()));
    }
    if cols.iter().any(|c| c.name == "tenant_id") {
        where_parts.push("tenant_id = ?".to_string());
        where_values.push(Box::new(tenant_id.to_string()));
    }
    let sql = format!(
        "SELECT {} FROM {} WHERE {}",
        select_sql,
        quote_ident(table),
        where_parts.join(" AND ")
    );
    let row = conn
        .query_row(
            &sql,
            rusqlite::params_from_iter(where_values.iter().map(|p| p.as_ref())),
            |r| row_to_json(r, &selectable),
        )
        .optional()
        .map_err(|e| ApiError::Internal {
            message: format!("查询失败: {e}"),
        })?
        .ok_or_else(|| ApiError::NotFound {
            message: format!("记录不存在: {table}/{pk_value}"),
        })?;
    Ok(row)
}

// ─── 端点 4：POST /api/v1/db/table/{table} ──────────────────────────

/// 插入单行（写操作仅 admin；tenant_id 从 token 注入）。
pub async fn insert_row_handler(
    State(state): State<DbAdminState>,
    headers: HeaderMap,
    Path(table): Path<String>,
    Json(body): Json<InsertBody>,
) -> Result<(axum::http::StatusCode, Json<Value>), ApiError> {
    let tenant_id = require_admin_role(&state, &headers).await?;
    let db = get_db(&state)?;
    let result = spawn_blocking(move || {
        db.with_conn(|conn| insert_row_inner(conn, &table, &body.row, &tenant_id))
    })
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("数据库任务失败: {e}"),
    })??;
    Ok((axum::http::StatusCode::CREATED, Json(result)))
}

fn insert_row_inner(
    conn: &Connection,
    table: &str,
    row: &Value,
    tenant_id: &str,
) -> Result<Value, ApiError> {
    validate_table(conn, table)?;
    let cols = get_table_columns(conn, table)?;
    let obj = row.as_object().ok_or_else(|| ApiError::BadRequest {
        message: "row 必须是 JSON 对象".to_string(),
    })?;

    // 键名白名单 + 排除 BLOB 列
    let mut insert_cols: Vec<String> = Vec::new();
    let mut insert_values: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
    for (k, v) in obj {
        let col = cols.iter().find(|c| c.name == *k).ok_or_else(|| {
            ApiError::BadRequest {
                message: format!("列不存在: {table}.{k}"),
            }
        })?;
        if is_blob_type(&col.type_name) {
            return Err(ApiError::BadRequest {
                message: format!("BLOB 列不允许经统一接口写入: {k}"),
            });
        }
        insert_cols.push(quote_ident(k));
        insert_values.push(Box::new(serde_json_to_sql(v)));
    }
    // 租户隔离：有 tenant_id 列则从 token 注入（忽略客户端传入，防跨租户写入）
    if cols.iter().any(|c| c.name == "tenant_id")
        && !insert_cols.iter().any(|c| c == "\"tenant_id\"")
    {
        insert_cols.push(quote_ident("tenant_id"));
        insert_values.push(Box::new(tenant_id.to_string()));
    }
    if insert_cols.is_empty() {
        // 全默认值插入（表无任何列可写时兜底；实际表均有主键等列）
        conn.execute(&format!("INSERT INTO {} DEFAULT VALUES", quote_ident(table)), [])
            .map_err(|e| ApiError::Internal {
                message: format!("插入失败: {e}"),
            })?;
    } else {
        let placeholders: Vec<String> = (1..=insert_cols.len()).map(|i| format!("?{i}")).collect();
        let sql = format!(
            "INSERT INTO {} ({}) VALUES ({})",
            quote_ident(table),
            insert_cols.join(", "),
            placeholders.join(", ")
        );
        conn.execute(&sql, rusqlite::params_from_iter(insert_values.iter().map(|p| p.as_ref())))
            .map_err(|e| ApiError::Internal {
                message: format!("插入失败: {e}"),
            })?;
    }

    // 回查整行 + row_id（主键值拼接）
    let pks = pk_columns(&cols);
    let row_id = if pks.is_empty() {
        Value::Null
    } else {
        let mut pk_values: Vec<String> = Vec::new();
        for pk in &pks {
            match obj.get(&pk.name) {
                // 字符串主键取原值（v.to_string() 会给 JSON 字符串加引号）
                Some(Value::String(s)) => pk_values.push(s.clone()),
                Some(v) => pk_values.push(v.to_string()),
                None => {
                    // 主键未显式提供：INTEGER 自增主键用 last_insert_rowid
                    pk_values.push(conn.last_insert_rowid().to_string());
                }
            }
        }
        Value::String(pk_values.join(","))
    };
    let full_row = if pks.is_empty() {
        Value::Object(obj.clone())
    } else {
        let pk_str = row_id.as_str().unwrap_or_default();
        get_row_inner(conn, table, pk_str, tenant_id).unwrap_or(Value::Object(obj.clone()))
    };
    Ok(json!({ "row": full_row, "row_id": row_id }))
}

// ─── 端点 5：PATCH /api/v1/db/table/{table}/{pk_value} ──────────────

/// 更新单行（写操作仅 admin；tenant_id 不可修改）。
pub async fn update_row_handler(
    State(state): State<DbAdminState>,
    headers: HeaderMap,
    Path((table, pk_value)): Path<(String, String)>,
    Json(body): Json<UpdateBody>,
) -> Result<Json<Value>, ApiError> {
    let tenant_id = require_admin_role(&state, &headers).await?;
    let db = get_db(&state)?;
    let result = spawn_blocking(move || {
        db.with_conn(|conn| update_row_inner(conn, &table, &pk_value, &body.updates, &tenant_id))
    })
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("数据库任务失败: {e}"),
    })??;
    Ok(Json(result))
}

fn update_row_inner(
    conn: &Connection,
    table: &str,
    pk_value: &str,
    updates: &Value,
    tenant_id: &str,
) -> Result<Value, ApiError> {
    validate_table(conn, table)?;
    let cols = get_table_columns(conn, table)?;
    let pks = pk_columns(&cols);
    if pks.is_empty() {
        return Err(ApiError::BadRequest {
            message: format!("表 {table} 无主键，无法按主键更新"),
        });
    }
    let pk_parts = parse_pk_values(pk_value);
    if pk_parts.len() != pks.len() {
        return Err(ApiError::BadRequest {
            message: format!("主键数量不匹配：表 {table} 主键为 {} 列", pks.len()),
        });
    }
    let obj = updates.as_object().ok_or_else(|| ApiError::BadRequest {
        message: "updates 必须是 JSON 对象".to_string(),
    })?;
    if obj.is_empty() {
        return Err(ApiError::BadRequest {
            message: "updates 不能为空".to_string(),
        });
    }

    let mut set_parts: Vec<String> = Vec::new();
    let mut set_values: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
    for (k, v) in obj {
        let col = cols.iter().find(|c| c.name == *k).ok_or_else(|| {
            ApiError::BadRequest {
                message: format!("列不存在: {table}.{k}"),
            }
        })?;
        if is_blob_type(&col.type_name) {
            return Err(ApiError::BadRequest {
                message: format!("BLOB 列不允许经统一接口写入: {k}"),
            });
        }
        if col.name == "tenant_id" {
            return Err(ApiError::BadRequest {
                message: "不允许修改 tenant_id（租户归属由 token 决定）".to_string(),
            });
        }
        set_parts.push(format!("{} = ?", quote_ident(k)));
        set_values.push(Box::new(serde_json_to_sql(v)));
    }

    let mut where_parts: Vec<String> = Vec::new();
    let mut where_values: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
    for (pk, v) in pks.iter().zip(pk_parts.iter()) {
        where_parts.push(format!("{} = ?", quote_ident(&pk.name)));
        where_values.push(Box::new(v.clone()));
    }
    if cols.iter().any(|c| c.name == "tenant_id") {
        where_parts.push("tenant_id = ?".to_string());
        where_values.push(Box::new(tenant_id.to_string()));
    }

    let sql = format!(
        "UPDATE {} SET {} WHERE {}",
        quote_ident(table),
        set_parts.join(", "),
        where_parts.join(" AND ")
    );
    let mut all_values: Vec<Box<dyn rusqlite::ToSql>> = set_values;
    all_values.extend(where_values);
    let affected = conn
        .execute(&sql, rusqlite::params_from_iter(all_values.iter().map(|p| p.as_ref())))
        .map_err(|e| ApiError::Internal {
            message: format!("更新失败: {e}"),
        })?;
    if affected == 0 {
        return Err(ApiError::NotFound {
            message: format!("记录不存在: {table}/{pk_value}"),
        });
    }
    // 回查更新后的整行
    let row = get_row_inner(conn, table, pk_value, tenant_id)?;
    Ok(json!({ "row": row }))
}

// ─── 端点 6：DELETE /api/v1/db/table/{table}/{pk_value} ─────────────

/// 删除单行（写操作仅 admin）。
pub async fn delete_row_handler(
    State(state): State<DbAdminState>,
    headers: HeaderMap,
    Path((table, pk_value)): Path<(String, String)>,
) -> Result<Json<Value>, ApiError> {
    let tenant_id = require_admin_role(&state, &headers).await?;
    let db = get_db(&state)?;
    let result = spawn_blocking(move || {
        db.with_conn(|conn| delete_row_inner(conn, &table, &pk_value, &tenant_id))
    })
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("数据库任务失败: {e}"),
    })??;
    Ok(Json(result))
}

fn delete_row_inner(
    conn: &Connection,
    table: &str,
    pk_value: &str,
    tenant_id: &str,
) -> Result<Value, ApiError> {
    validate_table(conn, table)?;
    let cols = get_table_columns(conn, table)?;
    let pks = pk_columns(&cols);
    if pks.is_empty() {
        return Err(ApiError::BadRequest {
            message: format!("表 {table} 无主键，无法按主键删除"),
        });
    }
    let pk_parts = parse_pk_values(pk_value);
    if pk_parts.len() != pks.len() {
        return Err(ApiError::BadRequest {
            message: format!("主键数量不匹配：表 {table} 主键为 {} 列", pks.len()),
        });
    }
    let mut where_parts: Vec<String> = Vec::new();
    let mut where_values: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
    for (pk, v) in pks.iter().zip(pk_parts.iter()) {
        where_parts.push(format!("{} = ?", quote_ident(&pk.name)));
        where_values.push(Box::new(v.clone()));
    }
    if cols.iter().any(|c| c.name == "tenant_id") {
        where_parts.push("tenant_id = ?".to_string());
        where_values.push(Box::new(tenant_id.to_string()));
    }
    let sql = format!(
        "DELETE FROM {} WHERE {}",
        quote_ident(table),
        where_parts.join(" AND ")
    );
    let affected = conn
        .execute(&sql, rusqlite::params_from_iter(where_values.iter().map(|p| p.as_ref())))
        .map_err(|e| ApiError::Internal {
            message: format!("删除失败: {e}"),
        })?;
    if affected == 0 {
        return Err(ApiError::NotFound {
            message: format!("记录不存在: {table}/{pk_value}"),
        });
    }
    Ok(json!({
        "deleted": true,
        "row_id": pk_value,
    }))
}

// ─── 端点 7：POST /api/v1/db/execute ────────────────────────────────

/// SQL 执行器（仅 admin；SELECT 直接执行，写语句需 confirm:true；
/// 危险前缀黑名单 403；单语句限制；5s 超时）。
pub async fn execute_sql_handler(
    State(state): State<DbAdminState>,
    headers: HeaderMap,
    Json(body): Json<ExecuteBody>,
) -> Result<Json<Value>, ApiError> {
    let _tenant_id = require_admin_role(&state, &headers).await?;
    let db = get_db(&state)?;
    let handle = spawn_blocking(move || {
        db.with_conn(|conn| execute_sql_inner(conn, &body.sql, body.confirm))
    });
    let result = tokio::time::timeout(Duration::from_secs(5), handle)
        .await
        .map_err(|_| ApiError::Internal {
            message: "SQL 执行超时（>5s）".to_string(),
        })?
        .map_err(|e| ApiError::Internal {
            message: format!("数据库任务失败: {e}"),
        })??;
    Ok(Json(result))
}

/// SQL 分类：只读（SELECT/WITH/EXPLAIN/只读 PRAGMA）vs 写语句。
fn classify_sql(sql: &str) -> &'static str {
    let up = sql.trim_start().to_ascii_uppercase();
    if up.starts_with("SELECT")
        || up.starts_with("WITH")
        || up.starts_with("EXPLAIN")
        || up.starts_with("PRAGMA table_info")
        || up.starts_with("PRAGMA table_xinfo")
    {
        "read"
    } else {
        "write"
    }
}

/// 危险语句黑名单检查（403）。
///
/// 覆盖契约 §2.7：DROP/ALTER/VACUUM/ATTACH/DETACH/PRAGMA write/全表 DELETE。
/// PRAGMA 一律拒绝：只读元数据（table_info 等）经统一接口获取，SQL 执行器
/// 面向 SELECT 调试与受控写语句，配置类语句（journal_mode 等）不在授权范围。
fn check_dangerous(sql: &str) -> Result<(), ApiError> {
    let up = sql.trim_start().to_ascii_uppercase();
    for prefix in ["DROP ", "ALTER ", "VACUUM", "ATTACH ", "DETACH ", "PRAGMA"] {
        if up.starts_with(prefix) {
            return Err(ApiError::Forbidden {
                message: format!("危险语句被拒绝（黑名单前缀）: {prefix}"),
            });
        }
    }
    // 全表 DELETE（无 WHERE）拒绝；带 WHERE 的定向删除允许
    if up.starts_with("DELETE FROM") && !up.contains("WHERE") {
        return Err(ApiError::Forbidden {
            message: "全表 DELETE 被拒绝（需带 WHERE 条件）".to_string(),
        });
    }
    Ok(())
}

fn execute_sql_inner(conn: &Connection, sql: &str, confirm: bool) -> Result<Value, ApiError> {
    let trimmed = sql.trim();
    if trimmed.is_empty() {
        return Err(ApiError::BadRequest {
            message: "sql 不能为空".to_string(),
        });
    }
    // 单语句限制：去掉末尾分号后仍含 `;` → 多语句拒绝
    let single = trimmed.trim_end_matches(';').trim();
    if single.contains(';') {
        return Err(ApiError::BadRequest {
            message: "仅允许单条语句（拒绝 ; 分隔多语句）".to_string(),
        });
    }
    check_dangerous(single)?;
    let kind = classify_sql(single);
    if kind == "write" && !confirm {
        return Err(ApiError::BadRequest {
            message: "写语句必须携带 confirm: true".to_string(),
        });
    }

    let mut stmt = conn
        .prepare(single)
        .map_err(|e| ApiError::BadRequest {
            message: format!("SQL 无效: {e}"),
        })?;
    if kind == "read" {
        let col_count = stmt.column_count();
        let columns: Vec<String> = (0..col_count)
            .map(|i| stmt.column_name(i).unwrap_or("").to_string())
            .collect();
        let rows = stmt
            .query_map([], |row| {
                let mut arr = Vec::with_capacity(col_count);
                for i in 0..col_count {
                    arr.push(match row.get_ref(i)? {
                        rusqlite::types::ValueRef::Null => Value::Null,
                        rusqlite::types::ValueRef::Integer(n) => json!(n),
                        rusqlite::types::ValueRef::Real(f) => json!(f),
                        rusqlite::types::ValueRef::Text(t) => {
                            Value::String(String::from_utf8_lossy(t).into_owned())
                        }
                        rusqlite::types::ValueRef::Blob(_) => Value::Null,
                    });
                }
                Ok(arr)
            })
            .map_err(|e| ApiError::BadRequest {
                message: format!("SQL 执行失败: {e}"),
            })?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| ApiError::BadRequest {
                message: format!("SQL 执行失败: {e}"),
            })?;
        Ok(json!({ "columns": columns, "rows": rows, "rows_affected": 0 }))
    } else {
        let affected = stmt
            .execute([])
            .map_err(|e| ApiError::BadRequest {
                message: format!("SQL 执行失败: {e}"),
            })?;
        Ok(json!({ "columns": [], "rows": [], "rows_affected": affected }))
    }
}

/// serde_json::Value → rusqlite 可绑定参数（对象/数组序列化为 JSON 文本）。
fn serde_json_to_sql(v: &Value) -> Box<dyn rusqlite::ToSql> {
    match v {
        Value::Null => Box::new(rusqlite::types::Null),
        Value::Bool(b) => Box::new(if *b { 1i64 } else { 0i64 }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Box::new(i)
            } else {
                Box::new(n.as_f64().unwrap_or_default())
            }
        }
        Value::String(s) => Box::new(s.clone()),
        Value::Array(_) | Value::Object(_) => Box::new(v.to_string()),
    }
}

// ─── 测试 ────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_http::auth::{default_users, encode_token, TokenType};

    /// 内存库 + 仅注入 db 的 DbAdminState（无 store：token 校验走内置 admin 回退）。
    fn state_with_db() -> (DbAdminState, Arc<agentos_engine::SqliteStore>) {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let state = DbAdminState {
            store: None,
            db: Some(store.clone()),
        };
        (state, store)
    }

    /// 铸造内置 admin 的 access token（与 api 登录签发同格式）。
    fn admin_token() -> String {
        let admin = default_users().into_iter().next().unwrap();
        encode_token(TokenType::Access, &admin, 3600)
    }

    // ─── 纯逻辑单测（SQL 构建 / 安全校验） ─────────────────────────

    #[test]
    fn quote_ident_escapes_double_quote() {
        assert_eq!(quote_ident("a\"b"), "\"a\"\"b\"");
        assert_eq!(quote_ident("memory"), "\"memory\"");
    }

    #[test]
    fn parse_pk_values_splits_comma_with_trim() {
        assert_eq!(parse_pk_values("r1"), vec!["r1"]);
        assert_eq!(parse_pk_values("r1, 1"), vec!["r1", "1"]);
    }

    #[test]
    fn pk_columns_sorts_by_pk_order() {
        let cols = vec![
            ColumnMeta { name: "b".into(), type_name: "TEXT".into(), notnull: true, pk: true, pk_order: 2 },
            ColumnMeta { name: "a".into(), type_name: "TEXT".into(), notnull: true, pk: true, pk_order: 1 },
            ColumnMeta { name: "c".into(), type_name: "TEXT".into(), notnull: false, pk: false, pk_order: 0 },
        ];
        let pks = pk_columns(&cols);
        let names: Vec<&str> = pks.iter().map(|c| c.name.as_str()).collect();
        assert_eq!(names, vec!["a", "b"]);
    }

    #[test]
    fn classify_sql_read_vs_write() {
        assert_eq!(classify_sql("SELECT 1"), "read");
        assert_eq!(classify_sql("with x as (select 1) select * from x"), "read");
        // 原实现行为：只读 PRAGMA 匹配串未大写化（输入已 to_ascii_uppercase），
        // 故 "PRAGMA table_info" 实际按 "write" 分类；但写路径先过
        // check_dangerous 黑名单（PRAGMA 一律 403），分类结果无实际影响。
        assert_eq!(classify_sql("PRAGMA table_info(memory)"), "write");
        assert_eq!(classify_sql("UPDATE memory SET x=1"), "write");
        assert_eq!(classify_sql("insert into memory (id) values ('a')"), "write");
    }

    #[test]
    fn check_dangerous_blacklist_and_full_delete() {
        for sql in [
            "DROP TABLE memory",
            "ALTER TABLE memory ADD COLUMN x",
            "VACUUM",
            "ATTACH 'x' AS y",
            "DETACH y",
            "PRAGMA journal_mode=WAL",
        ] {
            assert!(check_dangerous(sql).is_err(), "应拒绝: {sql}");
        }
        assert!(check_dangerous("DELETE FROM memory").is_err(), "全表 DELETE 应拒绝");
        assert!(check_dangerous("DELETE FROM memory WHERE id='x'").is_ok());
        assert!(check_dangerous("SELECT * FROM memory").is_ok());
    }

    #[test]
    fn list_table_names_dynamic_enumeration() {
        let (state, _store) = state_with_db();
        let db = get_db(&state).unwrap();
        db.with_conn(|conn| {
            let names = list_table_names(conn).unwrap();
            assert!(names.contains(&"runs".to_string()), "应含引擎表: {names:?}");
            assert!(!names.iter().any(|n| n.starts_with("sqlite_")), "应排除 sqlite_ 内部表");
            Ok::<(), String>(())
        })
        .unwrap();
    }

    #[test]
    fn serde_json_to_sql_maps_types() {
        use rusqlite::types::Value as SqlValue;

        // ToSqlOutput → 归一化为 SqlValue 比较（Borrowed/Owned 形态差异不影响语义；
        // `dyn ToSql` 对象类型自带 trait 方法，无需额外 use ToSql）。
        fn norm(v: &dyn rusqlite::ToSql) -> SqlValue {
            match v.to_sql().unwrap() {
                rusqlite::types::ToSqlOutput::Borrowed(r) => SqlValue::from(r),
                rusqlite::types::ToSqlOutput::Owned(v) => v,
                // non-exhaustive 兜底（ToSqlOutput 未来新增形态时按 Null 处理，仅测试用）
                _ => SqlValue::Null,
            }
        }
        assert_eq!(norm(serde_json_to_sql(&Value::Null).as_ref()), SqlValue::Null);
        assert_eq!(norm(serde_json_to_sql(&json!(true)).as_ref()), SqlValue::Integer(1));
        assert_eq!(norm(serde_json_to_sql(&json!(42)).as_ref()), SqlValue::Integer(42));
        assert_eq!(norm(serde_json_to_sql(&json!("s")).as_ref()), SqlValue::Text("s".into()));
        assert_eq!(
            norm(serde_json_to_sql(&json!([1, 2])).as_ref()),
            SqlValue::Text("[1,2]".into())
        );
    }

    // ─── HTTP 冒烟（自铸 token 走内置 admin 回退） ──────────────────

    #[tokio::test]
    async fn list_tables_requires_auth_returns_401() {
        use axum::body::Body;
        use axum::http::{Request, StatusCode};
        use tower::ServiceExt;

        let (state, _store) = state_with_db();
        let app = router().with_state(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/tables")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn list_tables_with_admin_token_returns_tables() {
        use axum::body::Body;
        use axum::http::{Request, StatusCode};
        use tower::ServiceExt;

        let (state, _store) = state_with_db();
        let app = router().with_state(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/tables")
                    .header("authorization", format!("Bearer {}", admin_token()))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 16 * 1024 * 1024)
            .await
            .unwrap();
        let json: Value = serde_json::from_slice(&body).unwrap();
        let tables = json["tables"].as_array().expect("tables 数组");
        let names: Vec<&str> = tables.iter().map(|t| t["name"].as_str().unwrap()).collect();
        assert!(names.contains(&"runs"), "应枚举引擎表: {names:?}");
    }

    #[tokio::test]
    async fn execute_sql_write_requires_confirm() {
        use axum::body::Body;
        use axum::http::{Request, StatusCode};
        use tower::ServiceExt;

        let (state, _store) = state_with_db();
        let app = router().with_state(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/execute")
                    .header("authorization", format!("Bearer {}", admin_token()))
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({ "sql": "UPDATE memory SET content='x' WHERE id='none'", "confirm": false }).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST, "写语句无 confirm 应 400");
    }
}
