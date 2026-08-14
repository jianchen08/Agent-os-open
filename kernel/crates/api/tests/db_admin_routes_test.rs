// @feature: FP-DB 统一通用数据接口 | @ci: rust-test
//! db-admin crate（/api/v1/db/*）端到端 HTTP 行为回归测试。
//!
//! task_kernel_cleanup_and_split 任务 1：db_routes 拆为独立 db-admin crate 后，
//! 原 db_routes.rs 内嵌的 12 个 HTTP 测试迁移至此（integration test）。
//! 经 api 的 build_router 全链路验证：登录/注册换 token → 打 /api/v1/db/* 端点，
//! 端点行为与拆分前完全一致（前端 dbAdmin.ts 契约无感知）。
//!
//! db-admin crate 自身另有纯逻辑单测 + 冒烟测试（agentos-db-admin）。

use std::sync::Arc;

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::Router;
use serde_json::{json, Value};
use tower::ServiceExt;

fn app_with_db() -> (Router, Arc<agentos_engine::SqliteStore>) {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let mut state = AppState::new();
    state.store = Some(store.clone());
    state.db = Some(store.clone());
    (build_router(state), store)
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
        "runs", "message_slots", "traces", "blobs", "branches", "sessions", "memory", "users",
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
