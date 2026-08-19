// @feature: FP-DB 统一通用数据接口 | @ci: rust-test
//! db-admin capability handler（SQL 能力层）行为回归测试。
//!
//! boot-plugin 第一刀（docs/working/重要设计/boot-plugin内核能力插件化立项.md）：
//! 原 `/api/v1/db/*` 7 端点收敛为 `db-admin` namespace 的 CapabilityHandler
//! （SQL 层留内核），HTTP 面由 plugins/shared/db_admin 插件承载
//! （`/ext/db_admin/**`）。本文件 12 个场景**语义与拆分前完全一致**，
//! 调用形态从 HTTP 端点改为 handler 直调——capability 层的鉴权/防注入/
//! 租户隔离/响应形状全部保真验证；HTTP 面（通配分发→插件→反调）的
//! 端到端验证在真机（启动日志 + /ext/db_admin/* 实测）覆盖。
//!
//! 鉴权：handler 从 params `_authorization`（HTTP 面插件透传的原始头）重建
//! HeaderMap 后走与拆分前同一套 resolve_request_user + require_* 链；
//! token 经 api build_router 的真实 login/register 端点签发。
//!
//! 取舍说明：原"重复 filter 参数 + filter[] 形态"是 HTTP query 序列化问题，
//! capability 层 params 天然是 JSON 数组（filter: [...]），两种形态在此合并验证。

use std::sync::Arc;

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_core::traits::StorageBackend;
use agentos_db_admin::DbAdminCapabilityHandler;
use agentos_mcp::CapabilityHandler;
use axum::body::Body;
use axum::http::Request;
use axum::Router;
use serde_json::{json, Value};
use tower::ServiceExt;

/// 内存库 + handler + 可登录的 router（token 签发用）。
/// handler 与 router 共享同一 store 实例（对齐生产装配）。
/// 播种 admin 对齐真实内核启动行为（agentos-kernel seed_admin_user）：
/// 安全加固 2026-08-19 后，store 存在但用户名未命中不再回退内置硬编码凭据。
async fn handler_setup() -> (
    DbAdminCapabilityHandler,
    Router,
    Arc<agentos_engine::SqliteStore>,
) {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    store
        .create_user(&agentos_core::types::UserRecord {
            user_id: "00000000-0000-0000-0000-000000000001".to_string(),
            username: "admin".to_string(),
            password: "admin12345".to_string(),
            email: Some("admin@agentos.dev".to_string()),
            role: "admin".to_string(),
            tenant_id: "default".to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_login_at: None,
        })
        .await
        .unwrap();
    let handler = DbAdminCapabilityHandler::new(Some(store.clone()), Some(store.clone()));
    let mut state = AppState::new();
    state.store = Some(store.clone());
    state.db = Some(store.clone());
    (handler, build_router(state), store)
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
                        "email": "alice@example.com",
                    })
                    .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    assert!(status.is_success(), "注册失败: status={status} body={json}");
    json["access_token"].as_str().unwrap().to_string()
}

/// 直调 handler。返回 (status, body_or_error)：
/// 成功 → (status, body)；失败 → (status, error json)。
async fn call(handler: &DbAdminCapabilityHandler, method: &str, params: Value) -> (u16, Value) {
    let resp = handler
        .handle(method, params)
        .await
        .expect("handler 不应返回协议错误");
    let status = resp["status"].as_u64().unwrap_or(500) as u16;
    let payload = if resp.get("body").is_some() {
        resp["body"].clone()
    } else {
        resp["error"].clone()
    };
    if status >= 400 {
        eprintln!("[call] method={method} status={status} payload={payload}");
    }
    (status, payload)
}

/// 带 token 的参数（_authorization = HTTP 面插件透传的原始头）。
fn with_auth(params: Value, token: &str) -> Value {
    let mut p = params;
    p["_authorization"] = json!(format!("Bearer {token}"));
    p
}

#[tokio::test]
async fn test_tables_lists_all_tables_dynamic() {
    let (handler, router, _store) = handler_setup().await;
    let token = admin_token(&router).await;
    let (status, json) = call(&handler, "list_tables", with_auth(json!({}), &token)).await;
    assert_eq!(status, 200);
    let tables = json["tables"].as_array().expect("tables 数组");
    let names: Vec<String> = tables
        .iter()
        .map(|t| t["name"].as_str().unwrap().to_string())
        .collect();
    for expect in [
        "runs",
        "message_slots",
        "traces",
        "blobs",
        "branches",
        "sessions",
        "memory",
        "users",
    ] {
        assert!(
            names.contains(&expect.to_string()),
            "缺少表 {expect}: {names:?}"
        );
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
    let (handler, router, store) = handler_setup().await;
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
    let (status, json) = call(
        &handler,
        "table_query",
        with_auth(
            json!({"table": "memory", "filter": "memory_type:eq:episode", "sort": "created_at:desc"}),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 200, "筛选+排序请求失败，响应体: {json}");
    assert_eq!(json["total"], 2);
    assert_eq!(json["rows"].as_array().unwrap().len(), 2);
    let first = &json["rows"][0];
    assert_eq!(first["id"], "m2"); // created_at desc: m2(01-03) 在前

    // contains 筛选
    let (status, json) = call(
        &handler,
        "table_query",
        with_auth(
            json!({"table": "memory", "filter": "content:contains:content 1"}),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 200, "contains 筛选失败，响应体: {json}");
    assert_eq!(json["total"], 1);
    assert_eq!(json["rows"][0]["id"], "m1");

    // limit/offset 分页
    let (status, json) = call(
        &handler,
        "table_query",
        with_auth(
            json!({"table": "memory", "limit": 2, "offset": 1, "sort": "created_at:asc"}),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(json["rows"].as_array().unwrap().len(), 2);
    assert_eq!(json["rows"][0]["id"], "m1");
}

#[tokio::test]
async fn test_query_rows_multi_filter_and() {
    let (handler, router, store) = handler_setup().await;
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

    // 多条件 AND（filter 数组；HTTP 重复参数/filter[] 两形态在 capability 层同为数组）
    let (status, json) = call(
        &handler,
        "table_query",
        with_auth(
            json!({"table": "memory", "filter": ["memory_type:eq:episode", "score:gt:3"]}),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 200, "filter 数组应 200，响应体: {json}");
    assert_eq!(json["total"], 1, "多条件 AND 应得交集，响应体: {json}");
    assert_eq!(json["rows"][0]["id"], "mf1");
}

#[tokio::test]
async fn test_query_injection_rejected() {
    let (handler, router, store) = handler_setup().await;
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
    let (status, _json) = call(
        &handler,
        "table_query",
        with_auth(
            json!({"table": "memory", "filter": "content:eq:'; DROP TABLE memory--"}),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 200); // 值绑定：查询正常返回（0 行）
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
    let (handler, router, _store) = handler_setup().await;
    let token = admin_token(&router).await;
    let (status, _json) = call(
        &handler,
        "table_query",
        with_auth(
            json!({"table": "memory", "filter": "nonexistent_col:eq:x"}),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 400);
    let (status, _json) = call(
        &handler,
        "table_query",
        with_auth(json!({"table": "not_a_table"}), &token),
    )
    .await;
    assert_eq!(status, 404);
}

#[tokio::test]
async fn test_crud_insert_update_delete() {
    let (handler, router, _store) = handler_setup().await;
    let token = admin_token(&router).await;

    // 插入
    let (status, json) = call(
        &handler,
        "table_insert",
        with_auth(
            json!({"table": "memory", "row": { "id": "crud1", "content": "hello", "memory_type": "episode", "created_at": "2025-01-01T00:00:00Z" }}),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 201, "插入失败: {json}");
    assert_eq!(json["row"]["id"], "crud1");
    assert_eq!(json["row_id"], "crud1");

    // 单行查询确认落库（tenant_id 已自动注入 default）
    let (status, json) = call(
        &handler,
        "table_get_row",
        with_auth(json!({"table": "memory", "pk_value": "crud1"}), &token),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(json["content"], "hello");
    assert_eq!(json["tenant_id"], "default");

    // 更新
    let (status, json) = call(
        &handler,
        "table_update_row",
        with_auth(
            json!({"table": "memory", "pk_value": "crud1", "updates": { "content": "updated" }}),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 200, "更新失败: {json}");
    assert_eq!(json["row"]["content"], "updated");

    // 更新不存在 → 404
    let (status, _json) = call(
        &handler,
        "table_update_row",
        with_auth(
            json!({"table": "memory", "pk_value": "nope", "updates": { "content": "x" }}),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 404);

    // 删除
    let (status, json) = call(
        &handler,
        "table_delete_row",
        with_auth(json!({"table": "memory", "pk_value": "crud1"}), &token),
    )
    .await;
    assert_eq!(status, 200, "删除失败: {json}");
    assert_eq!(json["deleted"], true);
    let (status, _json) = call(
        &handler,
        "table_get_row",
        with_auth(json!({"table": "memory", "pk_value": "crud1"}), &token),
    )
    .await;
    assert_eq!(status, 404);
}

#[tokio::test]
async fn test_composite_pk_crud() {
    let (handler, router, _store) = handler_setup().await;
    let token = admin_token(&router).await;

    // execution_records 复合主键 (record_id, sequence)
    let (status, json) = call(
        &handler,
        "table_insert",
        with_auth(
            json!({"table": "execution_records", "row": { "record_id": "r1", "sequence": 1, "pipeline_run_id": "p1", "content": "first", "created_at": "2025-01-01T00:00:00Z" }}),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 201, "复合主键插入失败: {json}");
    assert_eq!(json["row_id"], "r1,1");

    // 单行查询（`,` 拼接）
    let (status, json) = call(
        &handler,
        "table_get_row",
        with_auth(
            json!({"table": "execution_records", "pk_value": "r1,1"}),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 200, "复合主键查询失败: {json}");
    assert_eq!(json["content"], "first");

    // 更新
    let (status, json) = call(
        &handler,
        "table_update_row",
        with_auth(
            json!({"table": "execution_records", "pk_value": "r1,1", "updates": { "content": "second" }}),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(json["row"]["content"], "second");

    // 删除
    let (status, _json) = call(
        &handler,
        "table_delete_row",
        with_auth(
            json!({"table": "execution_records", "pk_value": "r1,1"}),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 200);
    let (status, _json) = call(
        &handler,
        "table_get_row",
        with_auth(
            json!({"table": "execution_records", "pk_value": "r1,1"}),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 404);
}

#[tokio::test]
async fn test_auth_required_401_and_forbidden() {
    let (handler, router, _store) = handler_setup().await;

    // 无 _authorization → 401
    let (status, _json) = call(&handler, "list_tables", json!({})).await;
    assert_eq!(status, 401);

    // 非 admin 用户：只读也 403（仅 admin/viewer）；写接口 403
    let user_tok = user_token(&router).await;
    let (status, _json) = call(&handler, "list_tables", with_auth(json!({}), &user_tok)).await;
    assert_eq!(status, 403, "普通用户读接口应 403");
    let (status, _json) = call(
        &handler,
        "table_insert",
        with_auth(json!({"table": "memory", "row": { "id": "x" }}), &user_tok),
    )
    .await;
    assert_eq!(status, 403, "普通用户写接口应 403");
}

#[tokio::test]
async fn test_sql_execute_select_and_write_confirm() {
    let (handler, router, _store) = handler_setup().await;
    let token = admin_token(&router).await;

    // SELECT 直接执行
    let (status, json) = call(
        &handler,
        "execute",
        with_auth(
            json!({ "sql": "SELECT 1 AS a, 'x' AS b", "confirm": false }),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 200, "SELECT 失败: {json}");
    assert_eq!(json["columns"], json!(["a", "b"]));
    assert_eq!(json["rows"][0], json!([1, "x"]));

    // 写语句无 confirm → 400
    let (status, _json) = call(
        &handler,
        "execute",
        with_auth(
            json!({ "sql": "UPDATE memory SET content='x' WHERE id='none'", "confirm": false }),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 400);

    // 写语句带 confirm → 200
    let (status, json) = call(
        &handler,
        "execute",
        with_auth(
            json!({ "sql": "UPDATE memory SET content='x' WHERE id='none'", "confirm": true }),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(json["rows_affected"], 0);
}

#[tokio::test]
async fn test_sql_execute_dangerous_rejected() {
    let (handler, router, _store) = handler_setup().await;
    let token = admin_token(&router).await;

    // DROP → 403
    let (status, _json) = call(
        &handler,
        "execute",
        with_auth(
            json!({ "sql": "DROP TABLE memory", "confirm": true }),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 403);

    // ALTER → 403
    let (status, _json) = call(
        &handler,
        "execute",
        with_auth(
            json!({ "sql": "ALTER TABLE memory ADD COLUMN x TEXT", "confirm": true }),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 403);

    // PRAGMA → 403
    let (status, _json) = call(
        &handler,
        "execute",
        with_auth(
            json!({ "sql": "PRAGMA journal_mode=WAL", "confirm": true }),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 403);

    // 全表 DELETE → 403
    let (status, _json) = call(
        &handler,
        "execute",
        with_auth(
            json!({ "sql": "DELETE FROM memory", "confirm": true }),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 403);

    // 多语句 → 400
    let (status, _json) = call(
        &handler,
        "execute",
        with_auth(
            json!({ "sql": "SELECT 1; SELECT 2", "confirm": false }),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 400);

    // 非 admin 执行 SQL → 403
    let user_tok = user_token(&router).await;
    let (status, _json) = call(
        &handler,
        "execute",
        with_auth(json!({ "sql": "SELECT 1", "confirm": false }), &user_tok),
    )
    .await;
    assert_eq!(status, 403);
}

#[tokio::test]
async fn test_extensibility_new_table_auto_visible() {
    let (handler, router, store) = handler_setup().await;
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
    let (status, json) = call(&handler, "list_tables", with_auth(json!({}), &token)).await;
    assert_eq!(status, 200);
    let names: Vec<String> = json["tables"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["name"].as_str().unwrap().to_string())
        .collect();
    assert!(
        names.contains(&"future_tasks".to_string()),
        "新表未自动可见: {names:?}"
    );

    // 新表可查询/可写
    let (status, json) = call(
        &handler,
        "table_insert",
        with_auth(
            json!({"table": "future_tasks", "row": { "task_id": "t1", "title": "auto", "created_at": "2025-01-01T00:00:00Z" }}),
            &token,
        ),
    )
    .await;
    assert_eq!(status, 201, "新表插入失败: {json}");
    let (status, json) = call(
        &handler,
        "table_get_row",
        with_auth(json!({"table": "future_tasks", "pk_value": "t1"}), &token),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(json["title"], "auto");

    // 清理测试表
    store
        .with_conn(|conn| {
            conn.execute_batch("DROP TABLE future_tasks;")
                .map_err(|e| e.to_string())
        })
        .unwrap();
}

#[tokio::test]
async fn test_tenant_isolation_applied() {
    let (handler, router, store) = handler_setup().await;
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
    let (status, json) = call(
        &handler,
        "table_query",
        with_auth(json!({"table": "memory"}), &token),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(json["total"], 1, "租户隔离未生效: {json}");
    assert_eq!(json["rows"][0]["id"], "t-d");
}
