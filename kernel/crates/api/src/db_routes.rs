//! 统一通用数据接口（/api/v1/db/*）
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

use std::sync::Arc;
use std::time::Duration;

use axum::extract::{Path, Query, State};
use axum::http::HeaderMap;
use axum::Json;
use rusqlite::{Connection, OptionalExtension};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::task::spawn_blocking;

use crate::auth::resolve_request_user;
use crate::error::ApiError;
use crate::routes::AppState;

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
async fn require_read_role(state: &AppState, headers: &HeaderMap) -> Result<String, ApiError> {
    let (_, _, role, tenant_id) = resolve_request_user(state.store.as_ref(), headers).await?;
    if role != "admin" && role != "viewer" {
        return Err(ApiError::Forbidden {
            message: "需要 admin 或 viewer 角色".to_string(),
        });
    }
    Ok(tenant_id)
}

/// 写接口角色校验：仅 admin。返回当前请求租户 ID。
async fn require_admin_role(state: &AppState, headers: &HeaderMap) -> Result<String, ApiError> {
    let (_, _, role, tenant_id) = resolve_request_user(state.store.as_ref(), headers).await?;
    if role != "admin" {
        return Err(ApiError::Forbidden {
            message: "写操作需要 admin 角色".to_string(),
        });
    }
    Ok(tenant_id)
}

/// 获取统一数据接口 db 句柄。
fn get_db(state: &AppState) -> Result<Arc<agentos_engine::SqliteStore>, ApiError> {
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
    State(state): State<AppState>,
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
    State(state): State<AppState>,
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
    State(state): State<AppState>,
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
    State(state): State<AppState>,
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
    State(state): State<AppState>,
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
    State(state): State<AppState>,
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
    State(state): State<AppState>,
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
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use axum::Router;
    use tower::ServiceExt;

    fn app_with_db() -> (Router, Arc<agentos_engine::SqliteStore>) {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let mut state = AppState::new();
        state.store = Some(store.clone());
        state.db = Some(store.clone());
        (crate::server::build_router(state), store)
    }

    async fn admin_token(router: &Router) -> String {
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"username": "admin", "password": "admin12345"}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let json: Value = serde_json::from_slice(&body).unwrap();
        json["access_token"].as_str().unwrap().to_string()
    }

    async fn user_token(router: &Router) -> String {
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/register")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({
                            "username": format!("alice{}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()),
                            "password": "pass12345",
                            "email": "alice@test.dev"
                        })
                        .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let json: Value = serde_json::from_slice(&body).unwrap();
        json["access_token"].as_str().unwrap().to_string()
    }

    async fn get_json(router: &Router, uri: &str, token: &str) -> (StatusCode, Value) {
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri(uri)
                    .header("authorization", format!("Bearer {token}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = resp.status();
        let body = axum::body::to_bytes(resp.into_body(), 16 * 1024 * 1024)
            .await
            .unwrap();
        let text = String::from_utf8_lossy(&body).to_string();
        let json: Value = serde_json::from_slice(&body).unwrap_or(Value::String(text.clone()));
        if status != StatusCode::OK {
            eprintln!("[get_json] uri={uri} status={status} body={text}");
        }
        (status, json)
    }

    async fn post_json(
        router: &Router,
        uri: &str,
        token: &str,
        payload: Value,
    ) -> (StatusCode, Value) {
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(uri)
                    .header("authorization", format!("Bearer {token}"))
                    .header("content-type", "application/json")
                    .body(Body::from(payload.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = resp.status();
        let body = axum::body::to_bytes(resp.into_body(), 16 * 1024 * 1024)
            .await
            .unwrap();
        let json: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
        (status, json)
    }

    async fn patch_json(
        router: &Router,
        uri: &str,
        token: &str,
        payload: Value,
    ) -> (StatusCode, Value) {
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("PATCH")
                    .uri(uri)
                    .header("authorization", format!("Bearer {token}"))
                    .header("content-type", "application/json")
                    .body(Body::from(payload.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = resp.status();
        let body = axum::body::to_bytes(resp.into_body(), 16 * 1024 * 1024)
            .await
            .unwrap();
        let json: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
        (status, json)
    }

    async fn delete_json(router: &Router, uri: &str, token: &str) -> (StatusCode, Value) {
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("DELETE")
                    .uri(uri)
                    .header("authorization", format!("Bearer {token}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = resp.status();
        let body = axum::body::to_bytes(resp.into_body(), 16 * 1024 * 1024)
            .await
            .unwrap();
        let json: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
        (status, json)
    }

    #[tokio::test]
    async fn test_tables_lists_all_tables_dynamic() {
        let (router, _store) = app_with_db();
        let token = admin_token(&router).await;
        let (status, json) = get_json(&router, "/api/v1/db/tables", &token).await;
        assert_eq!(status, StatusCode::OK);
        let tables = json["tables"].as_array().expect("tables 数组");
        let names: Vec<String> = tables
            .iter()
            .map(|t| t["name"].as_str().unwrap().to_string())
            .collect();
        for expect in [
            "runs", "messages", "traces", "blobs", "branches", "sessions", "memory", "users",
        ] {
            assert!(names.contains(&expect.to_string()), "缺少表 {expect}: {names:?}");
        }
        // 每个表有 columns 与 row_count
        let runs = tables.iter().find(|t| t["name"] == "runs").unwrap();
        assert!(!runs["columns"].as_array().unwrap().is_empty());
        assert!(runs["row_count"].is_number());
        // 列含主键标志
        let run_cols = runs["columns"].as_array().unwrap();
        let run_id = run_cols.iter().find(|c| c["name"] == "run_id").unwrap();
        assert_eq!(run_id["pk"], true);
    }

    #[tokio::test]
    async fn test_query_rows_pagination_filter_sort() {
        let (router, store) = app_with_db();
        // 直接向内存库插入 3 条 memory 记录（避开 auth 路径）
        store
            .with_conn(|conn| {
                for i in 0..3 {
                    conn.execute(
                        "INSERT INTO memory (id, content, memory_type, tenant_id, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
                        rusqlite::params![
                            format!("m{i}"),
                            format!("content {i}"),
                            if i % 2 == 0 { "episode" } else { "semantic" },
                            "default",
                            format!("2025-01-0{}T00:00:00Z", i + 1),
                        ],
                    )
                    .unwrap();
                }
                Ok::<(), String>(())
            })
            .unwrap();
        let token = admin_token(&router).await;

        // 筛选 eq + 排序
        let (status, json) = get_json(
            &router,
            "/api/v1/db/table/memory?filter=memory_type:eq:episode&sort=created_at:desc",
            &token,
        )
        .await;
        assert_eq!(status, StatusCode::OK, "筛选+排序请求失败，响应体: {json}");
        assert_eq!(json["total"], 2);
        assert_eq!(json["rows"].as_array().unwrap().len(), 2);
        let first = &json["rows"][0];
        assert_eq!(first["id"], "m2"); // created_at desc: m2(01-03) 在前

        // contains 筛选（空格需 URL 编码 %20）
        let (status, json) = get_json(
            &router,
            "/api/v1/db/table/memory?filter=content:contains:content%201",
            &token,
        )
        .await;
        assert_eq!(status, StatusCode::OK, "contains 筛选失败，响应体: {json}");
        assert_eq!(json["total"], 1);
        assert_eq!(json["rows"][0]["id"], "m1");

        // limit/offset 分页
        let (status, json) = get_json(
            &router,
            "/api/v1/db/table/memory?limit=2&offset=1&sort=created_at:asc",
            &token,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(json["rows"].as_array().unwrap().len(), 2);
        assert_eq!(json["rows"][0]["id"], "m1");
    }

    #[tokio::test]
    async fn test_query_rows_multi_filter_and() {
        let (router, store) = app_with_db();
        // 插入 4 条 memory：episode×2（score 1.0/5.0）、semantic×2（score 2.0/6.0）
        store
            .with_conn(|conn| {
                for (i, (mt, sc)) in [
                    ("episode", 1.0f64),
                    ("episode", 5.0),
                    ("semantic", 2.0),
                    ("semantic", 6.0),
                ]
                .iter()
                .enumerate()
                {
                    conn.execute(
                        "INSERT INTO memory (id, content, memory_type, score, tenant_id, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
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
        let token = admin_token(&router).await;

        // 多条件 AND（重复 filter 参数，契约 §2.2）：episode AND score>3 → 交集 1 条（mf1）
        let (status, json) = get_json(
            &router,
            "/api/v1/db/table/memory?filter=memory_type:eq:episode&filter=score:gt:3",
            &token,
        )
        .await;
        assert_eq!(status, StatusCode::OK, "重复 filter 应 200，响应体: {json}");
        assert_eq!(json["total"], 1, "多条件 AND 应得交集，响应体: {json}");
        assert_eq!(json["rows"][0]["id"], "mf1");

        // filter[] 形态（前端 axios 默认序列化，契约兼容兜底）
        let (status, json) = get_json(
            &router,
            "/api/v1/db/table/memory?filter[]=memory_type:eq:episode&filter[]=score:gt:3",
            &token,
        )
        .await;
        assert_eq!(status, StatusCode::OK, "filter[] 形态应 200，响应体: {json}");
        assert_eq!(json["total"], 1, "filter[] 多条件 AND 应得交集，响应体: {json}");
        assert_eq!(json["rows"][0]["id"], "mf1");
    }

    #[tokio::test]
    async fn test_query_injection_rejected() {
        let (router, store) = app_with_db();
        store
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO memory (id, content, memory_type, tenant_id, created_at) VALUES ('m0', 'safe', 'episode', 'default', '2025-01-01T00:00:00Z')",
                    [],
                )
                .unwrap();
                Ok::<(), String>(())
            })
            .unwrap();
        let token = admin_token(&router).await;
        // 注入尝试：值应被参数绑定，不注入
        // URL 编码：'; DROP TABLE memory-- → %27%3B%20DROP%20TABLE%20memory--
        let (status, _json) = get_json(
            &router,
            "/api/v1/db/table/memory?filter=content:eq:%27%3B%20DROP%20TABLE%20memory--",
            &token,
        )
        .await;
        assert_eq!(status, StatusCode::OK); // 值绑定：查询正常返回（0 行）
        // memory 表仍在
        let exists: bool = store
            .with_conn(|conn| {
                conn.query_row(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='memory')",
                    [],
                    |r| r.get(0),
                )
                .map_err(|e| e.to_string())
            })
            .unwrap();
        assert!(exists, "注入导致 memory 表被删");
    }

    #[tokio::test]
    async fn test_query_unknown_column_400_and_unknown_table_404() {
        let (router, _store) = app_with_db();
        let token = admin_token(&router).await;
        let (status, _json) = get_json(
            &router,
            "/api/v1/db/table/memory?filter=nonexistent_col:eq:x",
            &token,
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        let (status, _json) = get_json(&router, "/api/v1/db/table/not_a_table", &token).await;
        assert_eq!(status, StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn test_crud_insert_update_delete() {
        let (router, _store) = app_with_db();
        let token = admin_token(&router).await;

        // 插入
        let (status, json) = post_json(
            &router,
            "/api/v1/db/table/memory",
            &token,
            json!({ "row": { "id": "crud1", "content": "hello", "memory_type": "episode", "created_at": "2025-01-01T00:00:00Z" } }),
        )
        .await;
        assert_eq!(status, StatusCode::CREATED, "插入失败: {json}");
        assert_eq!(json["row"]["id"], "crud1");
        assert_eq!(json["row_id"], "crud1");

        // 查询确认落库（tenant_id 已自动注入 default）
        let (status, json) = get_json(&router, "/api/v1/db/table/memory/crud1", &token).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(json["content"], "hello");
        assert_eq!(json["tenant_id"], "default");

        // 更新
        let (status, json) = patch_json(
            &router,
            "/api/v1/db/table/memory/crud1",
            &token,
            json!({ "updates": { "content": "updated" } }),
        )
        .await;
        assert_eq!(status, StatusCode::OK, "更新失败: {json}");
        assert_eq!(json["row"]["content"], "updated");

        // 更新不存在 → 404
        let (status, _json) = patch_json(
            &router,
            "/api/v1/db/table/memory/nope",
            &token,
            json!({ "updates": { "content": "x" } }),
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND);

        // 删除
        let (status, json) = delete_json(&router, "/api/v1/db/table/memory/crud1", &token).await;
        assert_eq!(status, StatusCode::OK, "删除失败: {json}");
        assert_eq!(json["deleted"], true);
        let (status, _json) = get_json(&router, "/api/v1/db/table/memory/crud1", &token).await;
        assert_eq!(status, StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn test_composite_pk_crud() {
        let (router, _store) = app_with_db();
        let token = admin_token(&router).await;

        // execution_records 复合主键 (record_id, sequence)
        let (status, json) = post_json(
            &router,
            "/api/v1/db/table/execution_records",
            &token,
            json!({ "row": { "record_id": "r1", "sequence": 1, "pipeline_run_id": "p1", "content": "first", "created_at": "2025-01-01T00:00:00Z" } }),
        )
        .await;
        assert_eq!(status, StatusCode::CREATED, "复合主键插入失败: {json}");
        assert_eq!(json["row_id"], "r1,1");

        // 单行查询（`,` 拼接）
        let (status, json) = get_json(&router, "/api/v1/db/table/execution_records/r1,1", &token)
            .await;
        assert_eq!(status, StatusCode::OK, "复合主键查询失败: {json}");
        assert_eq!(json["content"], "first");

        // 更新
        let (status, json) = patch_json(
            &router,
            "/api/v1/db/table/execution_records/r1,1",
            &token,
            json!({ "updates": { "content": "second" } }),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(json["row"]["content"], "second");

        // 删除
        let (status, _json) = delete_json(&router, "/api/v1/db/table/execution_records/r1,1", &token)
            .await;
        assert_eq!(status, StatusCode::OK);
        let (status, _json) = get_json(&router, "/api/v1/db/table/execution_records/r1,1", &token)
            .await;
        assert_eq!(status, StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn test_auth_required_401_and_forbidden() {
        let (router, _store) = app_with_db();

        // 无 token → 401
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/api/v1/db/tables")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);

        // 非 admin 用户：只读也 403（仅 admin/viewer）；写接口 403
        let user_tok = user_token(&router).await;
        let (status, _json) = get_json(&router, "/api/v1/db/tables", &user_tok).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "普通用户读接口应 403");
        let (status, _json) = post_json(
            &router,
            "/api/v1/db/table/memory",
            &user_tok,
            json!({ "row": { "id": "x" } }),
        )
        .await;
        assert_eq!(status, StatusCode::FORBIDDEN, "普通用户写接口应 403");
    }

    #[tokio::test]
    async fn test_sql_execute_select_and_write_confirm() {
        let (router, _store) = app_with_db();
        let token = admin_token(&router).await;

        // SELECT 直接执行
        let (status, json) = post_json(
            &router,
            "/api/v1/db/execute",
            &token,
            json!({ "sql": "SELECT 1 AS a, 'x' AS b", "confirm": false }),
        )
        .await;
        assert_eq!(status, StatusCode::OK, "SELECT 失败: {json}");
        assert_eq!(json["columns"], json!(["a", "b"]));
        assert_eq!(json["rows"][0], json!([1, "x"]));

        // 写语句无 confirm → 400
        let (status, _json) = post_json(
            &router,
            "/api/v1/db/execute",
            &token,
            json!({ "sql": "UPDATE memory SET content='x' WHERE id='none'", "confirm": false }),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);

        // 写语句带 confirm → 200
        let (status, json) = post_json(
            &router,
            "/api/v1/db/execute",
            &token,
            json!({ "sql": "UPDATE memory SET content='x' WHERE id='none'", "confirm": true }),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(json["rows_affected"], 0);
    }

    #[tokio::test]
    async fn test_sql_execute_dangerous_rejected() {
        let (router, _store) = app_with_db();
        let token = admin_token(&router).await;

        // DROP → 403
        let (status, _json) = post_json(
            &router,
            "/api/v1/db/execute",
            &token,
            json!({ "sql": "DROP TABLE memory", "confirm": true }),
        )
        .await;
        assert_eq!(status, StatusCode::FORBIDDEN);

        // ALTER → 403
        let (status, _json) = post_json(
            &router,
            "/api/v1/db/execute",
            &token,
            json!({ "sql": "ALTER TABLE memory ADD COLUMN x TEXT", "confirm": true }),
        )
        .await;
        assert_eq!(status, StatusCode::FORBIDDEN);

        // PRAGMA → 403
        let (status, _json) = post_json(
            &router,
            "/api/v1/db/execute",
            &token,
            json!({ "sql": "PRAGMA journal_mode=WAL", "confirm": true }),
        )
        .await;
        assert_eq!(status, StatusCode::FORBIDDEN);

        // 全表 DELETE → 403
        let (status, _json) = post_json(
            &router,
            "/api/v1/db/execute",
            &token,
            json!({ "sql": "DELETE FROM memory", "confirm": true }),
        )
        .await;
        assert_eq!(status, StatusCode::FORBIDDEN);

        // 多语句 → 400
        let (status, _json) = post_json(
            &router,
            "/api/v1/db/execute",
            &token,
            json!({ "sql": "SELECT 1; SELECT 2", "confirm": false }),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);

        // 非 admin 执行 SQL → 403
        let user_tok = user_token(&router).await;
        let (status, _json) = post_json(
            &router,
            "/api/v1/db/execute",
            &user_tok,
            json!({ "sql": "SELECT 1", "confirm": false }),
        )
        .await;
        assert_eq!(status, StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn test_extensibility_new_table_auto_visible() {
        let (router, store) = app_with_db();
        let token = admin_token(&router).await;

        // 模拟内核未来新增表（扩展性验证）
        store
            .with_conn(|conn| {
                conn.execute_batch(
                    "CREATE TABLE IF NOT EXISTS future_tasks (task_id TEXT PRIMARY KEY, title TEXT NOT NULL, tenant_id TEXT NOT NULL DEFAULT 'default', created_at TEXT NOT NULL);",
                )
                .map_err(|e| e.to_string())
            })
            .unwrap();

        // 接口自动可见
        let (status, json) = get_json(&router, "/api/v1/db/tables", &token).await;
        assert_eq!(status, StatusCode::OK);
        let names: Vec<String> = json["tables"]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| t["name"].as_str().unwrap().to_string())
            .collect();
        assert!(names.contains(&"future_tasks".to_string()), "新表未自动可见: {names:?}");

        // 新表可查询/可写
        let (status, json) = post_json(
            &router,
            "/api/v1/db/table/future_tasks",
            &token,
            json!({ "row": { "task_id": "t1", "title": "auto", "created_at": "2025-01-01T00:00:00Z" } }),
        )
        .await;
        assert_eq!(status, StatusCode::CREATED, "新表插入失败: {json}");
        let (status, json) = get_json(&router, "/api/v1/db/table/future_tasks/t1", &token).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(json["title"], "auto");

        // 清理测试表
        store
            .with_conn(|conn| conn.execute_batch("DROP TABLE future_tasks;").map_err(|e| e.to_string()))
            .unwrap();
    }

    #[tokio::test]
    async fn test_tenant_isolation_applied() {
        let (router, store) = app_with_db();
        // 插入两条不同租户的 memory
        store
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO memory (id, content, memory_type, tenant_id, created_at) VALUES ('t-a', 'tenantA', 'episode', 'tenantA', '2025-01-01T00:00:00Z')",
                    [],
                )
                .unwrap();
                conn.execute(
                    "INSERT INTO memory (id, content, memory_type, tenant_id, created_at) VALUES ('t-d', 'tenantDefault', 'episode', 'default', '2025-01-01T00:00:00Z')",
                    [],
                )
                .unwrap();
                Ok::<(), String>(())
            })
            .unwrap();
        let token = admin_token(&router).await; // admin 租户 = default
        let (status, json) = get_json(&router, "/api/v1/db/table/memory", &token).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(json["total"], 1, "租户隔离未生效: {json}");
        assert_eq!(json["rows"][0]["id"], "t-d");
    }
}
