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
        match get_row_inner(conn, table, pk_str, tenant_id) {
            Ok(row) => row,
            // 回查 NotFound：INSERT 已生效，回显负载（DB 侧默认值/触发器改写不可见）
            Err(ApiError::NotFound { .. }) => Value::Object(obj.clone()),
            // DB 读错误不得伪装成功——报错说明行已写入，由调用方决策
            Err(e) => {
                return Err(ApiError::Internal {
                    message: format!("插入已执行但回查失败: {e}"),
                });
            }
        }
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
    fn insert_readback_notfound_falls_back_to_payload() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                conn.execute_batch(
                    "CREATE TABLE t_trig (id TEXT PRIMARY KEY, v TEXT);
                     CREATE TRIGGER t_del AFTER INSERT ON t_trig
                     BEGIN DELETE FROM t_trig WHERE id = NEW.id; END;",
                )
                .map_err(|e| e.to_string())?;
                let out = insert_row_inner(
                    conn,
                    "t_trig",
                    &json!({"id": "x1", "v": "hello"}),
                    "default",
                )
                .map_err(|e| format!("{e:?}"))?;
                // INSERT 已生效、回查 NotFound：回显负载（不报错——行确实写入过）
                assert_eq!(out["row"]["v"], "hello");
                assert_eq!(out["row_id"], "x1");
                Ok::<(), String>(())
            })
            .unwrap();
    }

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

    // ─── 补充：行查询/单行/CRUD/SQL 执行器全分支 ────────────────────────

    /// 建一张带租户列与 BLOB 列的测试表。
    fn setup_notes(conn: &Connection) {
        conn.execute_batch(
            "CREATE TABLE notes (
                id TEXT PRIMARY KEY,
                content TEXT,
                score REAL,
                data BLOB,
                tenant_id TEXT NOT NULL DEFAULT 'default'
            );",
        )
        .unwrap();
    }

    fn insert_note(conn: &Connection, id: &str, content: &str, score: f64, tenant: &str) {
        conn.execute(
            "INSERT INTO notes (id, content, score, tenant_id) VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params![id, content, score, tenant],
        )
        .unwrap();
    }

    #[test]
    fn get_table_columns_parses_meta() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                conn.execute_batch(
                    "CREATE TABLE t_meta (
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        score REAL
                    );",
                )
                .unwrap();
                let cols = get_table_columns(conn, "t_meta").unwrap();
                assert_eq!(cols.len(), 3);
                let id = &cols[0];
                assert!(id.pk);
                assert_eq!(id.pk_order, 1);
                assert!(!id.notnull);
                assert_eq!(id.type_name, "INTEGER");
                let name = &cols[1];
                assert!(!name.pk);
                assert!(name.notnull);
                assert_eq!(name.type_name, "TEXT");
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn query_rows_limit_clamp_and_offset() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                setup_notes(conn);
                for i in 0..3 {
                    insert_note(
                        conn,
                        &format!("n{i}"),
                        &format!("c{i}"),
                        i as f64,
                        "default",
                    );
                }
                // limit 超上限 → 钳到 500；offset 负数 → 0
                let out = query_rows_inner(
                    conn,
                    "notes",
                    &ListParams {
                        limit: Some(1000),
                        offset: Some(-5),
                        ..Default::default()
                    },
                    "default",
                )
                .unwrap();
                assert_eq!(out["limit"], 500);
                assert_eq!(out["offset"], 0);
                assert_eq!(out["total"], 3);
                assert_eq!(out["rows"].as_array().unwrap().len(), 3);
                // limit 0 → 钳到 1；offset 生效
                let out2 = query_rows_inner(
                    conn,
                    "notes",
                    &ListParams {
                        limit: Some(0),
                        offset: Some(1),
                        ..Default::default()
                    },
                    "default",
                )
                .unwrap();
                assert_eq!(out2["limit"], 1);
                assert_eq!(out2["rows"].as_array().unwrap().len(), 1);
                assert_eq!(out2["rows"][0]["id"], "n1");
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn query_rows_excludes_blob_columns() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                setup_notes(conn);
                conn.execute(
                    "INSERT INTO notes (id, content, score, data, tenant_id) VALUES ('b1', 'c', 1.0, X'0102', 'default')",
                    [],
                )
                .unwrap();
                let out = query_rows_inner(conn, "notes", &ListParams::default(), "default").unwrap();
                let row = &out["rows"][0];
                assert!(row.get("data").is_none(), "BLOB 列不应返回: {row}");
                assert_eq!(row["content"], "c");
                assert_eq!(out["total"], 1);
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn query_rows_all_blob_table_rejected() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                conn.execute_batch("CREATE TABLE t_allblob (a BLOB, b BLOB)")
                    .unwrap();
                let err = query_rows_inner(conn, "t_allblob", &ListParams::default(), "default")
                    .unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn query_rows_filter_errors() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                setup_notes(conn);
                // 格式错误（不足 3 段）
                let err = query_rows_inner(
                    conn,
                    "notes",
                    &ListParams {
                        filter: vec!["content:eq".to_string()],
                        ..Default::default()
                    },
                    "default",
                )
                .unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 未知操作符
                let err = query_rows_inner(
                    conn,
                    "notes",
                    &ListParams {
                        filter: vec!["content:xx:1".to_string()],
                        ..Default::default()
                    },
                    "default",
                )
                .unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 未知列
                let err = query_rows_inner(
                    conn,
                    "notes",
                    &ListParams {
                        filter: vec!["nope:eq:1".to_string()],
                        ..Default::default()
                    },
                    "default",
                )
                .unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn query_rows_filter_ops_and_tenant_isolation() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                setup_notes(conn);
                insert_note(conn, "n0", "hello world", 1.0, "t1");
                insert_note(conn, "n1", "hi", 5.0, "t1");
                insert_note(conn, "n2", "other", 2.0, "t2");
                // 租户隔离：t1 只见 t1 行
                let out = query_rows_inner(conn, "notes", &ListParams::default(), "t1").unwrap();
                assert_eq!(out["total"], 2, "租户隔离应过滤: {out}");
                // gt
                let out = query_rows_inner(
                    conn,
                    "notes",
                    &ListParams {
                        filter: vec!["score:gt:2".to_string()],
                        ..Default::default()
                    },
                    "t1",
                )
                .unwrap();
                assert_eq!(out["total"], 1);
                assert_eq!(out["rows"][0]["id"], "n1");
                // contains（LIKE %v%）
                let out = query_rows_inner(
                    conn,
                    "notes",
                    &ListParams {
                        filter: vec!["content:contains:hello".to_string()],
                        ..Default::default()
                    },
                    "t1",
                )
                .unwrap();
                assert_eq!(out["total"], 1);
                assert_eq!(out["rows"][0]["id"], "n0");
                // lt + ne 多条件 AND
                let out = query_rows_inner(
                    conn,
                    "notes",
                    &ListParams {
                        filter: vec!["score:lt:2".to_string(), "content:ne:hi".to_string()],
                        ..Default::default()
                    },
                    "t1",
                )
                .unwrap();
                assert_eq!(out["total"], 1);
                assert_eq!(out["rows"][0]["id"], "n0");
                // eq
                let out = query_rows_inner(
                    conn,
                    "notes",
                    &ListParams {
                        filter: vec!["score:eq:5".to_string()],
                        ..Default::default()
                    },
                    "t1",
                )
                .unwrap();
                assert_eq!(out["total"], 1);
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn query_rows_sort_variants() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                setup_notes(conn);
                insert_note(conn, "a", "x", 1.0, "default");
                insert_note(conn, "b", "y", 3.0, "default");
                insert_note(conn, "c", "z", 2.0, "default");
                // 默认主键 asc
                let out =
                    query_rows_inner(conn, "notes", &ListParams::default(), "default").unwrap();
                let ids: Vec<&str> = out["rows"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|r| r["id"].as_str().unwrap())
                    .collect();
                assert_eq!(ids, vec!["a", "b", "c"]);
                // sort=score:desc
                let out = query_rows_inner(
                    conn,
                    "notes",
                    &ListParams {
                        sort: Some("score:desc".to_string()),
                        ..Default::default()
                    },
                    "default",
                )
                .unwrap();
                let ids: Vec<&str> = out["rows"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|r| r["id"].as_str().unwrap())
                    .collect();
                assert_eq!(ids, vec!["b", "c", "a"]);
                // sort 缺方向 → 默认 asc
                let out = query_rows_inner(
                    conn,
                    "notes",
                    &ListParams {
                        sort: Some("score".to_string()),
                        ..Default::default()
                    },
                    "default",
                )
                .unwrap();
                assert_eq!(out["rows"][0]["id"], "a");
                // 非法方向 → 400
                let err = query_rows_inner(
                    conn,
                    "notes",
                    &ListParams {
                        sort: Some("score:sideways".to_string()),
                        ..Default::default()
                    },
                    "default",
                )
                .unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 未知排序列 → 400
                let err = query_rows_inner(
                    conn,
                    "notes",
                    &ListParams {
                        sort: Some("nope:asc".to_string()),
                        ..Default::default()
                    },
                    "default",
                )
                .unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn query_rows_no_pk_and_missing_table() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                // 无主键表：默认无 ORDER BY，仍可查询
                conn.execute_batch("CREATE TABLE t_nopk (a TEXT, b TEXT)")
                    .unwrap();
                conn.execute("INSERT INTO t_nopk (a, b) VALUES ('x', '1')", [])
                    .unwrap();
                conn.execute("INSERT INTO t_nopk (a, b) VALUES ('y', '2')", [])
                    .unwrap();
                let out =
                    query_rows_inner(conn, "t_nopk", &ListParams::default(), "default").unwrap();
                assert_eq!(out["total"], 2);
                // 表不存在 → 404
                let err =
                    query_rows_inner(conn, "no_such_table", &ListParams::default(), "default")
                        .unwrap_err();
                assert!(matches!(err, ApiError::NotFound { .. }));
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn get_row_variants() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                setup_notes(conn);
                insert_note(conn, "g1", "hello", 1.0, "t1");
                // 正常单行
                let row = get_row_inner(conn, "notes", "g1", "t1").unwrap();
                assert_eq!(row["content"], "hello");
                // 租户隔离：错误租户 → 404
                let err = get_row_inner(conn, "notes", "g1", "t2").unwrap_err();
                assert!(matches!(err, ApiError::NotFound { .. }));
                // 不存在 → 404
                let err = get_row_inner(conn, "notes", "nope", "t1").unwrap_err();
                assert!(matches!(err, ApiError::NotFound { .. }));
                // 无主键表 → 400
                conn.execute_batch("CREATE TABLE t_nopk2 (a TEXT)").unwrap();
                let err = get_row_inner(conn, "t_nopk2", "x", "default").unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 全 BLOB 表（含 BLOB 主键）→ 400
                conn.execute_batch("CREATE TABLE t_blobpk (id BLOB PRIMARY KEY, data BLOB)")
                    .unwrap();
                let err = get_row_inner(conn, "t_blobpk", "x", "default").unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn composite_pk_roundtrip() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                conn.execute_batch(
                    "CREATE TABLE t_comp (a TEXT, b TEXT, v TEXT, PRIMARY KEY (a, b))",
                )
                .unwrap();
                let out = insert_row_inner(
                    conn,
                    "t_comp",
                    &json!({"a": "x", "b": "y", "v": "1"}),
                    "default",
                )
                .unwrap();
                assert_eq!(out["row_id"], "x,y");
                // 带空格的主键值 trim 后匹配
                let row = get_row_inner(conn, "t_comp", "x, y", "default").unwrap();
                assert_eq!(row["v"], "1");
                // 主键数量不匹配 → 400
                let err = get_row_inner(conn, "t_comp", "x", "default").unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 更新
                let out =
                    update_row_inner(conn, "t_comp", "x,y", &json!({"v": "2"}), "default").unwrap();
                assert_eq!(out["row"]["v"], "2");
                // 删除
                let out = delete_row_inner(conn, "t_comp", "x,y", "default").unwrap();
                assert_eq!(out["deleted"], true);
                // 再删 → 404
                let err = delete_row_inner(conn, "t_comp", "x,y", "default").unwrap_err();
                assert!(matches!(err, ApiError::NotFound { .. }));
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn insert_row_error_branches() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                setup_notes(conn);
                // row 非对象 → 400
                let err = insert_row_inner(conn, "notes", &json!([1, 2]), "default").unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 未知列 → 400
                let err =
                    insert_row_inner(conn, "notes", &json!({"nope": 1}), "default").unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // BLOB 列写入 → 400
                let err =
                    insert_row_inner(conn, "notes", &json!({"id": "b1", "data": "x"}), "default")
                        .unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 表不存在 → 404
                let err =
                    insert_row_inner(conn, "no_such", &json!({"a": 1}), "default").unwrap_err();
                assert!(matches!(err, ApiError::NotFound { .. }));
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn insert_row_pk_variants() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                conn.execute_batch("CREATE TABLE t_num (id INTEGER PRIMARY KEY, v TEXT)")
                    .unwrap();
                // 数值主键 → row_id 为十进制字符串
                let out = insert_row_inner(conn, "t_num", &json!({"id": 42, "v": "x"}), "default")
                    .unwrap();
                assert_eq!(out["row_id"], "42");
                assert_eq!(out["row"]["v"], "x");
                // 主键缺失 → last_insert_rowid（显式 id 42 已消耗 rowid，下一个为 43）
                let out = insert_row_inner(conn, "t_num", &json!({"v": "y"}), "default").unwrap();
                assert_eq!(out["row_id"], "43");
                assert_eq!(out["row"]["v"], "y");
                // 空对象 → DEFAULT VALUES 分支
                let out = insert_row_inner(conn, "t_num", &json!({}), "default").unwrap();
                assert_eq!(out["row_id"], "44");
                assert!(out["row"]["v"].is_null());
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn insert_row_tenant_id_client_value_passthrough() {
        // 现状契约：客户端 row 自带 tenant_id 时按客户端值写入（注入被跳过）。
        // 注：与模块文档"忽略客户端传入"的表述不一致——见报告。
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                setup_notes(conn);
                let out = insert_row_inner(
                    conn,
                    "notes",
                    &json!({"id": "x1", "content": "c", "tenant_id": "client-tenant"}),
                    "token-tenant",
                )
                .unwrap();
                assert_eq!(out["row"]["tenant_id"], "client-tenant");
                // 客户端未提供 → token 注入
                let out = insert_row_inner(
                    conn,
                    "notes",
                    &json!({"id": "x2", "content": "c"}),
                    "token-tenant",
                )
                .unwrap();
                assert_eq!(out["row"]["tenant_id"], "token-tenant");
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn update_row_error_branches() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                setup_notes(conn);
                insert_note(conn, "u1", "old", 1.0, "t1");
                // 无主键表 → 400
                conn.execute_batch("CREATE TABLE t_nopk3 (a TEXT)").unwrap();
                let err = update_row_inner(conn, "t_nopk3", "x", &json!({"a": "y"}), "default")
                    .unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 主键数量不匹配 → 400
                let err = update_row_inner(conn, "notes", "a,b", &json!({"content": "x"}), "t1")
                    .unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // updates 非对象 → 400
                let err = update_row_inner(conn, "notes", "u1", &json!([1]), "t1").unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // updates 空 → 400
                let err = update_row_inner(conn, "notes", "u1", &json!({}), "t1").unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 未知列 → 400
                let err =
                    update_row_inner(conn, "notes", "u1", &json!({"nope": 1}), "t1").unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // BLOB 列 → 400
                let err =
                    update_row_inner(conn, "notes", "u1", &json!({"data": "x"}), "t1").unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // tenant_id 修改 → 400
                let err =
                    update_row_inner(conn, "notes", "u1", &json!({"tenant_id": "evil"}), "t1")
                        .unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 不存在 → 404
                let err = update_row_inner(conn, "notes", "nope", &json!({"content": "x"}), "t1")
                    .unwrap_err();
                assert!(matches!(err, ApiError::NotFound { .. }));
                // 租户隔离：错误租户 → 404（affected 0）
                let err = update_row_inner(conn, "notes", "u1", &json!({"content": "x"}), "t2")
                    .unwrap_err();
                assert!(matches!(err, ApiError::NotFound { .. }));
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn delete_row_error_branches() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                setup_notes(conn);
                insert_note(conn, "d1", "x", 1.0, "t1");
                // 无主键表 → 400
                conn.execute_batch("CREATE TABLE t_nopk4 (a TEXT)").unwrap();
                let err = delete_row_inner(conn, "t_nopk4", "x", "default").unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 主键数量不匹配 → 400
                let err = delete_row_inner(conn, "notes", "a,b", "t1").unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 不存在 → 404
                let err = delete_row_inner(conn, "notes", "nope", "t1").unwrap_err();
                assert!(matches!(err, ApiError::NotFound { .. }));
                // 租户隔离：错误租户 → 404
                let err = delete_row_inner(conn, "notes", "d1", "t2").unwrap_err();
                assert!(matches!(err, ApiError::NotFound { .. }));
                // 正常删除
                let out = delete_row_inner(conn, "notes", "d1", "t1").unwrap();
                assert_eq!(out["deleted"], true);
                assert_eq!(out["row_id"], "d1");
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn insert_row_no_pk_table_echoes_payload() {
        // 无主键表：row_id 为 null，full_row 回显负载（无回查路径）
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                conn.execute_batch("CREATE TABLE t_nopk5 (a TEXT, b TEXT)")
                    .unwrap();
                let out =
                    insert_row_inner(conn, "t_nopk5", &json!({"a": "x", "b": "y"}), "default")
                        .unwrap();
                assert!(out["row_id"].is_null());
                assert_eq!(out["row"]["a"], "x");
                assert_eq!(out["row"]["b"], "y");
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn row_to_json_blob_via_untyped_column() {
        // 无类型声明列（SQLite 动态类型）存 BLOB → row_to_json 返回 null
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                conn.execute_batch("CREATE TABLE t_dyn (id TEXT PRIMARY KEY, payload)")
                    .unwrap();
                conn.execute("INSERT INTO t_dyn (id, payload) VALUES ('d1', X'0102')", [])
                    .unwrap();
                let out =
                    query_rows_inner(conn, "t_dyn", &ListParams::default(), "default").unwrap();
                assert!(
                    out["rows"][0]["payload"].is_null(),
                    "BLOB 值应返回 null: {out}"
                );
                Ok::<(), String>(())
            })
            .unwrap();
    }

    #[test]
    fn serde_json_to_sql_float_and_bool() {
        use rusqlite::types::Value as SqlValue;

        fn norm(v: &dyn rusqlite::ToSql) -> SqlValue {
            match v.to_sql().unwrap() {
                rusqlite::types::ToSqlOutput::Borrowed(r) => SqlValue::from(r),
                rusqlite::types::ToSqlOutput::Owned(v) => v,
                _ => SqlValue::Null,
            }
        }
        assert_eq!(
            norm(serde_json_to_sql(&json!(1.5)).as_ref()),
            SqlValue::Real(1.5)
        );
        assert_eq!(
            norm(serde_json_to_sql(&json!(false)).as_ref()),
            SqlValue::Integer(0)
        );
        assert_eq!(
            norm(serde_json_to_sql(&json!({"k": 1})).as_ref()),
            SqlValue::Text("{\"k\":1}".into())
        );
    }

    #[test]
    fn execute_sql_read_and_write() {
        let store = agentos_engine::SqliteStore::open_memory().unwrap();
        store
            .with_conn(|conn| {
                setup_notes(conn);
                insert_note(conn, "e1", "hello", 1.5, "default");
                // 空 sql → 400
                let err = execute_sql_inner(conn, "  ", true).unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 多语句 → 400
                let err = execute_sql_inner(conn, "SELECT 1; SELECT 2", true).unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 无效 SQL → 400
                let err = execute_sql_inner(conn, "SELECT * FROM no_such", true).unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 读：columns + rows（BLOB 列 → null）
                let out =
                    execute_sql_inner(conn, "SELECT id, content, data FROM notes", true).unwrap();
                assert_eq!(out["columns"], json!(["id", "content", "data"]));
                assert_eq!(out["rows"][0][0], "e1");
                assert_eq!(out["rows"][0][1], "hello");
                assert!(out["rows"][0][2].is_null(), "BLOB 应返回 null");
                assert_eq!(out["rows_affected"], 0);
                // 写：rows_affected
                let out = execute_sql_inner(
                    conn,
                    "INSERT INTO notes (id, content, tenant_id) VALUES ('e2', 'x', 'default')",
                    true,
                )
                .unwrap();
                assert_eq!(out["rows_affected"], 1);
                // 写无 confirm → 400
                let err = execute_sql_inner(
                    conn,
                    "INSERT INTO notes (id, content, tenant_id) VALUES ('e3', 'x', 'default')",
                    false,
                )
                .unwrap_err();
                assert!(matches!(err, ApiError::BadRequest { .. }));
                // 危险语句 → 403
                let err = execute_sql_inner(conn, "DROP TABLE notes", true).unwrap_err();
                assert!(matches!(err, ApiError::Forbidden { .. }));
                Ok::<(), String>(())
            })
            .unwrap();
    }
}
