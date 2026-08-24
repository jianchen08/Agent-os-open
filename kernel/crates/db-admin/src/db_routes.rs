//! 统一通用数据接口——DB Admin SQL 能力层（纯逻辑，无 HTTP 依赖）。
//!
//! 表驱动、动态枚举：表清单/列信息/主键由 `sqlite_master` + `PRAGMA table_info`
//! 运行时发现，不写死任何表名/列名——新增表/新增列自动可见、自动可查、自动可管。
//!
//! 安全约束：
//! - 只读逻辑（表枚举/行查询/单行）与写逻辑（CRUD/SQL 执行器）的角色闸在
//!   [`crate::capability`]（鉴权经 `_authorization` 在内核侧执行）；
//! - 表名/列名白名单校验（动态枚举），值走 prepared statement 参数绑定，杜绝 SQL 注入；
//! - SQL 执行器需 `confirm:true`、单语句（拒 `;` 分隔）、危险前缀黑名单（DROP/ALTER/
//!   VACUUM/ATTACH/DETACH/PRAGMA/全表 DELETE）→ 403，带 5s 超时；
//! - 租户隔离：有 `tenant_id` 列的表自动追加 `AND tenant_id = ?`（调用方传入，由
//!   capability handler 从 token 解析——插件不可伪造）；
//! - BLOB 安全：BLOB 列不返回内容、不允许经统一接口写入（blobs.data 由内核专用方法管理）。
//!
//! [来源: docs/working/unified_db_admin_plan.md §二 / .project/api_contract.md]
//!
//! 拆分演进（boot-plugin 第一刀）：原 axum 路由层（`/api/v1/db/*` 7 端点）已摘除，
//! HTTP 面由 `plugins/shared/db_admin`（Python sidecar 插件）承载，本文件保留全部
//! SQL 构建与校验逻辑，经 [`crate::capability::DbAdminCapabilityHandler`] 调用。

use agentos_http::error::ApiError;
use rusqlite::{Connection, OptionalExtension};
use serde_json::{json, Value};

/// 行查询参数（原契约 §2.2；HTTP query 解析已上移至调用方）。
#[derive(Debug, Default)]
pub struct ListParams {
    pub limit: Option<i64>,
    pub offset: Option<i64>,
    /// 可重复：`col:eq|ne|gt|lt|contains:value`，多条件 AND。
    pub filter: Vec<String>,
    /// `col:asc|desc`，默认主键 asc
    pub sort: Option<String>,
}

/// 列元数据（PRAGMA table_info 行）。
#[derive(Debug, Clone)]
pub struct ColumnMeta {
    pub name: String,
    pub type_name: String,
    pub notnull: bool,
    pub pk: bool,
    pub pk_order: i64,
}

// ─── 动态枚举辅助 ────────────────────────────────────────────────────

/// 表名白名单：sqlite_master 动态枚举（排除 sqlite_ 内部表）。
pub fn list_table_names(conn: &Connection) -> Result<Vec<String>, ApiError> {
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
pub fn get_table_columns(conn: &Connection, table: &str) -> Result<Vec<ColumnMeta>, ApiError> {
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
pub fn quote_ident(name: &str) -> String {
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

// ─── 行查询（原 GET /table/{table} 的 SQL 逻辑） ─────────────────────

pub fn query_rows_inner(
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
    let selectable: Vec<&ColumnMeta> = cols
        .iter()
        .filter(|c| !is_blob_type(&c.type_name))
        .collect();
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
            format!(
                " ORDER BY {} {}",
                quote_ident(col),
                dir.to_ascii_uppercase()
            )
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
    let mut stmt = conn.prepare(&data_sql).map_err(|e| ApiError::Internal {
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

// ─── 单行查询（原 GET /table/{table}/{pk_value} 的 SQL 逻辑） ────────

pub fn get_row_inner(
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
    let selectable: Vec<&ColumnMeta> = cols
        .iter()
        .filter(|c| !is_blob_type(&c.type_name))
        .collect();
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

// ─── 插入（原 POST /table/{table} 的 SQL 逻辑） ──────────────────────

pub fn insert_row_inner(
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
        let col = cols
            .iter()
            .find(|c| c.name == *k)
            .ok_or_else(|| ApiError::BadRequest {
                message: format!("列不存在: {table}.{k}"),
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
        conn.execute(
            &format!("INSERT INTO {} DEFAULT VALUES", quote_ident(table)),
            [],
        )
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
        conn.execute(
            &sql,
            rusqlite::params_from_iter(insert_values.iter().map(|p| p.as_ref())),
        )
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

// ─── 更新（原 PATCH /table/{table}/{pk_value} 的 SQL 逻辑） ──────────

pub fn update_row_inner(
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
        let col = cols
            .iter()
            .find(|c| c.name == *k)
            .ok_or_else(|| ApiError::BadRequest {
                message: format!("列不存在: {table}.{k}"),
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
        .execute(
            &sql,
            rusqlite::params_from_iter(all_values.iter().map(|p| p.as_ref())),
        )
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

// ─── 删除（原 DELETE /table/{table}/{pk_value} 的 SQL 逻辑） ──────────

pub fn delete_row_inner(
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
        .execute(
            &sql,
            rusqlite::params_from_iter(where_values.iter().map(|p| p.as_ref())),
        )
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

// ─── SQL 执行器（原 POST /execute 的 SQL 逻辑与防线） ────────────────

/// SQL 分类：只读（SELECT/WITH/EXPLAIN）vs 写语句。
///
/// PRAGMA 不参与分类：check_dangerous 在本函数之前执行且一律 403 拒绝
/// （只读元数据走统一接口，不进 SQL 执行器），分类分支里没有 PRAGMA 路径。
fn classify_sql(sql: &str) -> &'static str {
    let up = sql.trim_start().to_ascii_uppercase();
    if up.starts_with("SELECT") || up.starts_with("WITH") || up.starts_with("EXPLAIN") {
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

pub fn execute_sql_inner(conn: &Connection, sql: &str, confirm: bool) -> Result<Value, ApiError> {
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

    let mut stmt = conn.prepare(single).map_err(|e| ApiError::BadRequest {
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
        let affected = stmt.execute([]).map_err(|e| ApiError::BadRequest {
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

// ─── 全量执行数据清理（clear_execution_data） ────────────────────────

/// 执行数据清理白名单（9 表；users 刻意保留——与 2026-08-22 手动清库同口径）。
///
/// 走专用方法而非 SQL 执行器：execute 的"全表 DELETE 一律 403"是泛化防线，
/// 本方法以白名单常量显式声明"清什么"，与 engine store.rs 的 DDL 同仓演进。
pub const EXECUTION_DATA_TABLES: [&str; 9] = [
    "runs",
    "traces",
    "blobs",
    "branches",
    "sessions",
    "pipeline_sessions",
    "pipeline_state",
    "pipeline_checkpoints",
    "message_slots",
];

/// 清空前快照备份（`VACUUM INTO`，须在事务外执行）；内存库返回 None。
///
/// 备份失败按错误中止清理——不可撤销操作前的安全网，不静默降级。
fn backup_before_clear(conn: &Connection) -> Result<Option<String>, ApiError> {
    // PRAGMA database_list 首行 = main 库，第 3 列为文件路径（内存库为空串）
    let main_path: String = conn
        .query_row("PRAGMA database_list", [], |r| r.get::<_, String>(2))
        .map_err(|e| ApiError::Internal {
            message: format!("读取主库路径失败: {e}"),
        })?;
    if main_path.is_empty() {
        return Ok(None);
    }
    static SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    let seq = SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    // VACUUM INTO 目标已存在会报错；毫秒时间戳 + 进程内自增序号保证唯一
    let backup = format!("{main_path}.clear-backup-{ts}-{seq}");
    conn.execute("VACUUM INTO ?1", rusqlite::params![backup])
        .map_err(|e| ApiError::Internal {
            message: format!("清理备份失败（已中止清理）: {e}"),
        })?;
    Ok(Some(backup))
}

/// 全量执行数据清理：活跃防呆 → 快照备份 → 事务内清 9 表 → 内存 registry 同清。
///
/// - **活跃防呆**（检查非锁，与删除之间的新启动竞态窗口毫秒级、可接受）：
///   DB running 且内存 registry 命中 = 真运行中 → 409 拒绝；DB 残留 running
///   但内存无条目（进程重启后的僵尸 run）放行清理——清库正是清僵尸的手段。
/// - **users 保留**：清的是执行数据（记录/轨迹/消息/状态），账号体系不动。
/// - **registry 清理**：DB 行删掉后热路径常驻 state 全部作废，后续轮次走
///   冷启动重建（与重启后语义一致）。
pub fn clear_execution_data_inner(conn: &Connection) -> Result<Value, ApiError> {
    let registry = agentos_session::pipeline_state_registry::global_registry();
    // 1) 活跃管道防呆
    let running: Vec<(String, String)> = {
        let mut stmt = conn
            .prepare(
                "SELECT pipeline_id, tenant_id FROM runs
                 WHERE status = 'running' AND pipeline_id IS NOT NULL AND pipeline_id != ''",
            )
            .map_err(|e| ApiError::Internal {
                message: format!("活跃管道检查失败: {e}"),
            })?;
        let rows = stmt
            .query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
            .map_err(|e| ApiError::Internal {
                message: format!("活跃管道检查失败: {e}"),
            })?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| ApiError::Internal {
                message: format!("活跃管道检查失败: {e}"),
            })?
    };
    for (pid, tenant) in &running {
        if registry.contains(tenant, pid) {
            return Err(ApiError::Conflict {
                message: format!("管道 {pid} 正在运行，请等待任务结束后再清理"),
            });
        }
    }
    // 2) 快照备份（事务外；失败中止）
    let backup_path = backup_before_clear(conn)?;
    // 3) 事务内逐表清理（白名单常量，无用户输入拼接）
    let mut cleared = serde_json::Map::new();
    let mut total: i64 = 0;
    conn.execute_batch("BEGIN")
        .map_err(|e| ApiError::Internal {
            message: format!("开启清理事务失败: {e}"),
        })?;
    for table in EXECUTION_DATA_TABLES {
        match conn.execute(&format!("DELETE FROM {table}"), []) {
            Ok(n) => {
                cleared.insert(table.to_string(), Value::from(n as i64));
                total += n as i64;
            }
            Err(e) => {
                let _ = conn.execute_batch("ROLLBACK");
                return Err(ApiError::Internal {
                    message: format!("清理 {table} 失败（已回滚）: {e}"),
                });
            }
        }
    }
    conn.execute_batch("COMMIT")
        .map_err(|e| ApiError::Internal {
            message: format!("提交清理事务失败: {e}"),
        })?;
    // 4) 内存 registry 同清（事务已提交，防呆拒绝路径不会走到这里）
    registry.clear();
    Ok(json!({
        "cleared": Value::Object(cleared),
        "cleared_count": total,
        "backup_path": backup_path,
    }))
}


// ─── 测试（纯逻辑单测） ──────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

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
            ColumnMeta {
                name: "b".into(),
                type_name: "TEXT".into(),
                notnull: true,
                pk: true,
                pk_order: 2,
            },
            ColumnMeta {
                name: "a".into(),
                type_name: "TEXT".into(),
                notnull: true,
                pk: true,
                pk_order: 1,
            },
            ColumnMeta {
                name: "c".into(),
                type_name: "TEXT".into(),
                notnull: false,
                pk: false,
                pk_order: 0,
            },
        ];
        let pks = pk_columns(&cols);
        let names: Vec<&str> = pks.iter().map(|c| c.name.as_str()).collect();
        assert_eq!(names, vec!["a", "b"]);
    }

    #[test]
    fn classify_sql_read_vs_write() {
        assert_eq!(classify_sql("SELECT 1"), "read");
        assert_eq!(classify_sql("with x as (select 1) select * from x"), "read");
        assert_eq!(classify_sql("UPDATE memory SET x=1"), "write");
        assert_eq!(
            classify_sql("insert into memory (id) values ('a')"),
            "write"
        );
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
        assert!(
            check_dangerous("DELETE FROM memory").is_err(),
            "全表 DELETE 应拒绝"
        );
        assert!(check_dangerous("DELETE FROM memory WHERE id='x'").is_ok());
        assert!(check_dangerous("SELECT * FROM memory").is_ok());
    }

    #[test]
    fn list_table_names_dynamic_enumeration() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                let names = list_table_names(conn).unwrap();
                assert!(names.contains(&"runs".to_string()), "应含引擎表: {names:?}");
                assert!(
                    !names.iter().any(|n| n.starts_with("sqlite_")),
                    "应排除 sqlite_ 内部表"
                );
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
        assert_eq!(
            norm(serde_json_to_sql(&Value::Null).as_ref()),
            SqlValue::Null
        );
        assert_eq!(
            norm(serde_json_to_sql(&json!(true)).as_ref()),
            SqlValue::Integer(1)
        );
        assert_eq!(
            norm(serde_json_to_sql(&json!(42)).as_ref()),
            SqlValue::Integer(42)
        );
        assert_eq!(
            norm(serde_json_to_sql(&json!("s")).as_ref()),
            SqlValue::Text("s".into())
        );
        assert_eq!(
            norm(serde_json_to_sql(&json!([1, 2])).as_ref()),
            SqlValue::Text("[1,2]".into())
        );
    }
}
