// @feature: FP-0.2.一 插件协议 | @ci: rust-test
// 由 server.rs 的主 #[cfg(test)] 测试块体平移而来（保留私有项访问）。

#[cfg(test)]
use agentos_core::traits::MessageQueryOpts;

const SEED_ADMIN_PW: &str = "test-admin-pw-2026";

use super::*;
use agentos_core::types::PendingInputSource;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::json;
use tower::ServiceExt;

/// 登录内置 admin（无 store 时回退内置用户表）返回 access_token。
async fn admin_token(app: &axum::Router) -> String {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/auth/login")
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({"username": "admin", "password": SEED_ADMIN_PW}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
    v["access_token"].as_str().unwrap().to_string()
}

/// 无 store 场景（AppState::new()）的管理面 token：内置脚手架 admin
/// （store=None 时 resolve 走内置表，pwv 与空口令哈希绑定一致）。
fn scaffold_admin_token() -> String {
    #[allow(unused_imports)] // 路径解析面：crate::server::X 经此供给（glob 非直接消费）
    use crate::server::*;
    use agentos_http::auth::{encode_token, BuiltInUser, TokenType};
    encode_token(
        TokenType::Access,
        &BuiltInUser {
            id: "00000000-0000-0000-0000-000000000001".to_string(),
            username: "admin".to_string(),
            password: String::new(),
            email: String::new(),
            role: "admin".to_string(),
            tenant_id: "default".to_string(),
            created_at: String::new(),
            must_change_password: false,
        },
        3600,
    )
}

fn scaffold_admin_bearer() -> String {
    format!("Bearer {}", scaffold_admin_token())
}

/// 为 store 播种 admin 并返回绑定同一次口令哈希的 access token（store 在场时
/// resolve 走 DB 用户表，token 的 pwv 必须与播种哈希同源——hash_password 随机盐
/// 使两次调用产物不同，故播种与编码必须共用同一个哈希值）。
async fn seed_admin_token(sqlite: &Arc<agentos_engine::SqliteStore>) -> String {
    use agentos_core::traits::StorageBackend;
    let hash = agentos_http::auth::hash_password(SEED_ADMIN_PW).unwrap();
    let admin = agentos_core::types::UserRecord {
        user_id: "00000000-0000-0000-0000-000000000001".to_string(),
        username: "admin".to_string(),
        password: hash.clone(),
        email: Some("admin@agentos.dev".to_string()),
        role: "admin".to_string(),
        tenant_id: "default".to_string(),
        created_at: "2026-08-30T00:00:00Z".to_string(),
        last_login_at: None,
        must_change_password: false,
    };
    let _ = StorageBackend::create_user(sqlite.as_ref(), &admin).await;
    use agentos_http::auth::{encode_token, BuiltInUser, TokenType};
    encode_token(
        TokenType::Access,
        &BuiltInUser {
            id: admin.user_id,
            username: admin.username,
            password: hash,
            email: String::new(),
            role: admin.role,
            tenant_id: admin.tenant_id,
            created_at: String::new(),
            must_change_password: false,
        },
        3600,
    )
}

/// G2：validate-all 全量巡检——声明 vs 实际对照，漂移分类报告。
#[tokio::test]
async fn test_validate_all_reports_drift_and_clean() {
    let mut state = AppState::new();
    // 两个 tool 插件：p_drift（声明 t1+ghost，上报只有 t1 → missing 漂移）、
    // p_clean（声明 t2，上报一致 → clean）
    let mk_manifest = |id: &str, tools: &[&str]| -> agentos_core::traits::PluginManifest {
        serde_json::from_value(json!({
            "id": id, "name": id, "version": "1.0.0",
            "plugin_type": "tool", "language": "python",
            "host_type": "sidecar", "entry": "python server.py",
            "capabilities": { "tools": tools.iter().map(|t| {
                json!({"name": t, "description": t})
            }).collect::<Vec<_>>() },
        }))
        .expect("valid manifest")
    };
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
        mk_manifest("p_drift", &["t1", "ghost"]),
        mk_manifest("p_clean", &["t2"]),
    ]));
    let invoker = Arc::new(RecordingInvoker {
        seen: std::sync::Mutex::new(Vec::new()),
        seen_states: std::sync::Mutex::new(Vec::new()),
        hooks: std::sync::Mutex::new(Vec::new()),
        list_tools: std::collections::HashMap::from([
            (
                "p_drift".to_string(),
                json!({ "tools": [{"name": "t1", "description": "t1"}] }),
            ),
            (
                "p_clean".to_string(),
                json!({ "tools": [{"name": "t2", "description": "t2"}] }),
            ),
        ]),
    });
    state.invoker = Some(invoker);

    let resp = validate_all_plugins_handler(axum::extract::State(state)).await;
    let body = resp.0;
    assert_eq!(body["checked"], 2);
    assert_eq!(body["clean"], 1);
    assert_eq!(body["drifted"], 1);
    assert_eq!(body["errors"], 0);
    let drift_report = body["reports"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["plugin_id"] == "p_drift")
        .expect("p_drift 报告");
    assert_eq!(drift_report["status"], "drifted");
    let kinds: Vec<&str> = drift_report["mismatches"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["kind"].as_str().unwrap())
        .collect();
    assert_eq!(kinds, vec!["missing"], "ghost 声明有实际无 → missing");
    let clean_report = body["reports"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["plugin_id"] == "p_clean")
        .expect("p_clean 报告");
    assert_eq!(clean_report["status"], "clean");
    assert_eq!(clean_report["mismatches"].as_array().unwrap().len(), 0);
}

/// G2：validate-all 在 invoker 未接线时返回错误计数（不 panic）。
#[tokio::test]
async fn test_validate_all_without_invoker_reports_error() {
    let state = AppState::new(); // invoker = None
    let resp = validate_all_plugins_handler(axum::extract::State(state)).await;
    assert_eq!(resp.0["errors"], 1);
    assert!(resp.0["message"].as_str().is_some());
}

/// 闸2·观测：validate-all 写健康度账本（drift→g2=drift+last_error；
/// clean→g2=ok），随后 contract-status 把它带进 `{plugins:[...]}` 响应。
#[tokio::test]
async fn test_validate_all_writes_contract_health_then_status() {
    let mut state = AppState::new();
    let mk_manifest = |id: &str, tools: &[&str]| -> agentos_core::traits::PluginManifest {
        serde_json::from_value(json!({
            "id": id, "name": id, "version": "1.0.0",
            "plugin_type": "tool", "language": "python",
            "host_type": "sidecar", "entry": "python server.py",
            "capabilities": { "tools": tools.iter().map(|t| {
                json!({"name": t, "description": t})
            }).collect::<Vec<_>>() },
        }))
        .expect("valid manifest")
    };
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
        mk_manifest("p_drift", &["t1", "ghost"]),
        mk_manifest("p_clean", &["t2"]),
    ]));
    state.enabled_plugin_ids =
        Arc::new(tokio::sync::RwLock::new(std::collections::HashSet::from([
            "p_drift".to_string(),
            "p_clean".to_string(),
        ])));
    let invoker = Arc::new(RecordingInvoker {
        seen: std::sync::Mutex::new(Vec::new()),
        seen_states: std::sync::Mutex::new(Vec::new()),
        hooks: std::sync::Mutex::new(Vec::new()),
        list_tools: std::collections::HashMap::from([
            (
                "p_drift".to_string(),
                json!({ "tools": [{"name": "t1", "description": "t1"}] }),
            ),
            (
                "p_clean".to_string(),
                json!({ "tools": [{"name": "t2", "description": "t2"}] }),
            ),
        ]),
    });
    state.invoker = Some(invoker);

    let resp = validate_all_plugins_handler(axum::extract::State(state.clone())).await;
    assert_eq!(resp.0["drifted"], 1);

    let status = plugins_contract_status_handler(axum::extract::State(state))
        .await
        .0;
    assert_eq!(status["count"], 2);
    let plugins = status["plugins"].as_array().unwrap();
    let by_id = |id: &str| plugins.iter().find(|p| p["plugin_id"] == id).unwrap();
    let drift_gates = &by_id("p_drift")["gates"];
    assert_eq!(drift_gates["g2_consistency"], "drift");
    assert!(
        drift_gates["last_error"].as_str().unwrap().contains("t1")
            || drift_gates["last_error"]
                .as_str()
                .unwrap()
                .contains("ghost"),
        "漂移工具进 last_error: {:?}",
        drift_gates["last_error"]
    );
    let clean_gates = &by_id("p_clean")["gates"];
    assert_eq!(clean_gates["g2_consistency"], "ok");
    assert_eq!(by_id("p_clean")["enabled"], true);
}

/// 闸2·观测：contract-status 契约形状——`{plugins:[...], count}`，账本
/// 未登记补 not_covered 缺省，`enabled` 一律以当前快照为准。
#[tokio::test]
async fn test_contract_status_handler_shape() {
    let mut state = AppState::new();
    let m: agentos_core::traits::PluginManifest = serde_json::from_value(json!({
        "id": "p_svc", "name": "p_svc", "version": "1.0.0",
        "plugin_type": "system", "language": "python",
        "host_type": "sidecar", "entry": "python server.py",
        "capabilities": {},
    }))
    .expect("valid manifest");
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![m.clone()]));
    // 未登记：只 enabled，不登记账本 → not_covered 缺省
    state.enabled_plugin_ids =
        Arc::new(tokio::sync::RwLock::new(std::collections::HashSet::from([
            "p_svc".to_string(),
        ])));

    let status = plugins_contract_status_handler(axum::extract::State(state))
        .await
        .0;
    let plugins = status["plugins"].as_array().unwrap();
    assert_eq!(plugins.len(), 1);
    assert_eq!(plugins[0]["plugin_id"], "p_svc");
    assert_eq!(plugins[0]["enabled"], true);
    assert_eq!(plugins[0]["gates"]["g2_consistency"], "not_covered");
    assert_eq!(plugins[0]["gates"]["manifest_schema_valid"], true);
}

#[tokio::test]
async fn test_health_returns_200() {
    let app = build_router(AppState::new());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/health")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
}

#[tokio::test]
async fn test_schema_returns_200() {
    let app = build_router(AppState::new());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/schema")
                .header("authorization", scaffold_admin_bearer())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
}

/// 剩余项清仓 D2：schema ETag——首次 200 带 ETag；If-None-Match 命中
/// （含 *）返回 304 空体；未命中返回 200 新体。
#[tokio::test]
async fn test_schema_etag_if_none_match_304() {
    let app = build_router(AppState::new());

    // 首次：200 + ETag 响应头
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/v1/schema")
                .header("authorization", scaffold_admin_bearer())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let etag = resp
        .headers()
        .get("etag")
        .and_then(|v| v.to_str().ok())
        .expect("ETag header")
        .to_string();

    // If-None-Match 命中 → 304 空体（ETag 仍带回，便于客户端续用）
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/v1/schema")
                .header("If-None-Match", &etag)
                .header("authorization", scaffold_admin_bearer())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_MODIFIED);
    assert_eq!(
        resp.headers().get("etag").and_then(|v| v.to_str().ok()),
        Some(etag.as_str())
    );
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    assert!(body.is_empty(), "304 必须空体");

    // If-None-Match: * → 304
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/v1/schema")
                .header("If-None-Match", "*")
                .header("authorization", scaffold_admin_bearer())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_MODIFIED);

    // 未命中的 ETag → 200 全量体
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/schema")
                .header("If-None-Match", "\"stale-etag\"")
                .header("authorization", scaffold_admin_bearer())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 65536).await.unwrap();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
    // 响应体形状不变（agents/pipelines/tools/routes/plugin_*）
    for key in ["agents", "pipelines", "tools", "routes"] {
        assert!(json.get(key).is_some(), "schema 响应应含 {key}");
    }
}

#[tokio::test]
async fn test_pipelines_returns_200() {
    let app = build_router(AppState::new());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/pipelines")
                .header("authorization", scaffold_admin_bearer())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
}

#[tokio::test]
async fn test_tools_returns_200() {
    let app = build_router(AppState::new());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/tools")
                .header("authorization", scaffold_admin_bearer())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
}

#[tokio::test]
async fn test_chat_post_returns_200() {
    let (state, _invoker, _store, sqlite, _user_space_guard) = make_engine_state();
    // 会话先建：REST chat 按 session 的 active_pipeline_id 解析执行坐标，
    // 会话不存在 = 协议违约（会话 id 不得充当管道坐标回退）。
    seed_session_with_pipeline(&sqlite, "s1", "pipe-s1", "default").await;
    let app = build_router(state);
    // A11：chat 已纳入写面鉴权，先 login 拿 token
    let token = admin_token(&app).await;
    let body = serde_json::to_string(&WsRequest {
        message: "hello".to_string(),
        session_id: "s1".to_string(),
        agent_id: String::new(),
    })
    .unwrap();
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/chat")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
}

/// A11：匿名 POST /api/v1/chat → 401（0.2 收紧：消息驱动管道执行属写面）。
#[tokio::test]
async fn test_chat_post_anonymous_returns_401() {
    let app = build_router(AppState::new());
    let body = serde_json::to_string(&WsRequest {
        message: "hello".to_string(),
        session_id: "s1".to_string(),
        agent_id: String::new(),
    })
    .unwrap();
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/chat")
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn test_health_response_body() {
    let app = build_router(AppState::new());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/health")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let body = axum::body::to_bytes(response.into_body(), 4096)
        .await
        .unwrap();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(json["status"], "ok");
    assert!(json["version"].is_string());
}

#[tokio::test]
async fn test_chat_uses_engine_not_echo() {
    // 验证 chat 响应不再是简单的 "Response to: xxx"
    let (state, _invoker, _store, sqlite, _user_space_guard) = make_engine_state();
    seed_session_with_pipeline(&sqlite, "test_session", "pipe-test_session", "default").await;
    let app = build_router(state);
    // A11：chat 已纳入写面鉴权，先 login 拿 token
    let token = admin_token(&app).await;
    let body = serde_json::to_string(&WsRequest {
        message: "hello world".to_string(),
        session_id: "test_session".to_string(),
        agent_id: String::new(),
    })
    .unwrap();
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/chat")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    let body = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(json["type"], "message");
    // 响应内容不应再是 "Response to: hello world"（echo 模式）
    let content = json["content"].as_str().unwrap();
    assert!(
        !content.starts_with("Response to:"),
        "Chat should not be in echo mode, got: {content}"
    );
    assert_eq!(json["session_id"], "test_session");
}

/// P2 守卫（ADR-2026-11-17 后继）：chat_handler 顶层 agent_id 下压 agent.id
/// overlay 前过 `valid_frame_field_value`（与 WS router 帧字段值守卫同款）——
/// 合法值随 overlay 并入 state、经 run 收尾 checkpoint 持久化；非法值
/// （控制字符）/空串静默丢弃该键 + WARN，请求本身照常成功（可选提示性字段
/// 不得让投递失败）。观察面 = 收尾 checkpoint 的全量 state（checkpoint 写入
/// 不保证先于 HTTP outcome 通知，按 `run_started_at` 轮询等本轮快照落库）。
#[tokio::test]
async fn test_chat_agent_id_guard_drops_malformed_and_keeps_valid() {
    let (state, _invoker, _store, sqlite, _user_space_guard) = make_engine_state();
    seed_session_with_pipeline(&sqlite, "s-ag-guard", "pipe-ag-guard", "default").await;
    let app = build_router(state);
    let token = admin_token(&app).await;

    let post_chat = |app: axum::Router, token: String, message: String, agent_id: String| async move {
        let body = serde_json::to_string(&WsRequest {
            message,
            session_id: "s-ag-guard".to_string(),
            agent_id,
        })
        .unwrap();
        app.oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/chat")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap()
    };

    // 非法（控制字符）与空 agent_id：请求成功，且收尾 checkpoint 不含 agent.id
    for (label, agent_id) in [("控制字符", "bad\u{0001}id"), ("空串", "")] {
        let prev = latest_run_started(&sqlite, "pipe-ag-guard").await;
        let resp = post_chat(
            app.clone(),
            token.clone(),
            format!("hi-{label}"),
            agent_id.to_string(),
        )
        .await;
        assert_eq!(
            resp.status(),
            StatusCode::OK,
            "{label}：agent_id 非法不得让请求失败（可选提示性字段）"
        );
        let ckpt = wait_next_run_checkpoint(&sqlite, "pipe-ag-guard", prev).await;
        assert!(
            ckpt.get("agent.id").is_none(),
            "{label}：非法 agent_id 不得并入 state（收尾 checkpoint 无该键），实际: {:?}",
            ckpt.get("agent.id")
        );
    }

    // 合法 agent_id：随 overlay 并入 state，收尾 checkpoint 持久化（执行身份契约键）
    let prev = latest_run_started(&sqlite, "pipe-ag-guard").await;
    let resp = post_chat(
        app.clone(),
        token.clone(),
        "hi-valid".to_string(),
        "custom_agent".to_string(),
    )
    .await;
    assert_eq!(resp.status(), StatusCode::OK);
    let ckpt = wait_next_run_checkpoint(&sqlite, "pipe-ag-guard", prev).await;
    assert_eq!(
        ckpt.get("agent.id"),
        Some(&serde_json::json!("custom_agent")),
        "合法 agent_id 须随 overlay 并入 state 并经 checkpoint 持久化"
    );
}

/// 上一个 run 的 checkpoint 起点时间戳（无 checkpoint → None）。
async fn latest_run_started(
    sqlite: &agentos_engine::SqliteStore,
    pipeline_id: &str,
) -> Option<String> {
    sqlite
        .load_latest_checkpoint(pipeline_id, "default")
        .ok()
        .flatten()
        .and_then(|(_, ckpt)| {
            ckpt.get("run_started_at")
                .and_then(|v| v.as_str())
                .map(str::to_string)
        })
}

/// 等本轮 run 的收尾 checkpoint 落库后返回快照。
///
/// HTTP outcome 通知与收尾 checkpoint 写入无先后契约（checkpoint 是尽力备份），
/// 响应返回即读最新快照可能仍是上一轮——用 `run_started_at` 变化识别新一轮，
/// 轮询到落库（3s 上限，超时即失败而非静默读旧值）。
async fn wait_next_run_checkpoint(
    sqlite: &agentos_engine::SqliteStore,
    pipeline_id: &str,
    prev_run_started_at: Option<String>,
) -> serde_json::Value {
    for _ in 0..300 {
        if let Some((_, ckpt)) = sqlite
            .load_latest_checkpoint(pipeline_id, "default")
            .ok()
            .flatten()
        {
            let started = ckpt
                .get("run_started_at")
                .and_then(|v| v.as_str())
                .map(str::to_string);
            if started != prev_run_started_at {
                return ckpt;
            }
        }
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
    panic!("等待本轮收尾 checkpoint 超时（3s）：latest 快照仍是上一轮或尚未落库");
}

#[tokio::test]
async fn test_schema_shape_without_registry() {
    // config 树已删：无 registry 装配时 schema 形状不变但 agents/pipelines/
    // tools 全部为空（生产装配必有 registry；空面是测试装配态的真实反映）。
    let app = build_router(AppState::new());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/schema")
                .header("authorization", scaffold_admin_bearer())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let body = axum::body::to_bytes(response.into_body(), 4096)
        .await
        .unwrap();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
    // 形状不变（agents/pipelines/tools/routes 恒在）
    for key in ["agents", "pipelines", "tools", "routes"] {
        assert!(json.get(key).is_some(), "schema 响应应含 {key}");
    }
    assert_eq!(json["agents"].as_array().unwrap().len(), 0);
    assert_eq!(json["tools"].as_array().unwrap().len(), 0);
}

#[tokio::test]
async fn test_tools_handler_without_registry_returns_empty() {
    // config 树已删：无 registry 时 tools handler 返回空工具面。
    // W-C2：响应信封统一为 {items, total}。
    let app = build_router(AppState::new());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/tools")
                .header("authorization", scaffold_admin_bearer())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let body = axum::body::to_bytes(response.into_body(), 4096)
        .await
        .unwrap();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
    let items = json["items"].as_array().expect("应含 items 数组");
    assert_eq!(json["total"], 0);
    assert!(items.is_empty());
}

// ── 监控 M5/M5b：指标查询端点 + Prometheus 导出端点 ──

fn state_with_metrics() -> AppState {
    use crate::metrics::{Labels, MetricType, MetricsAggregator};
    let agg = MetricsAggregator::new();
    let mut labels = Labels::new();
    labels.insert("model".to_string(), "deepseek".to_string());
    agg.record(
        "llm_service",
        "tokens_used",
        MetricType::Counter,
        12800.0,
        &labels,
        Some("tokens"),
        Some("Total tokens used"),
    );
    agg.record(
        "llm_service",
        "latency",
        MetricType::Histogram,
        0.02,
        &Labels::new(),
        Some("seconds"),
        Some("LLM latency"),
    );
    AppState::new().with_metrics(agg)
}

#[tokio::test]
async fn test_metrics_query_endpoint_migrated_to_plugin() {
    // boot-plugin 第三刀：查询面迁 /ext/metrics_admin/query（metrics-admin
    // capability，语义测试在 metrics/capability.rs）；旧内核路由应已摘除。
    // 带鉴权探测：匿名请求受 /api/v1/ 前缀 default-deny 先行 401（ADR
    // 2026-10-10），路由是否在场须以已认证请求判别（在场走 handler、
    // 缺席 404）。
    let app = build_router(state_with_metrics());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/metrics?plugin=llm_service")
                .header("authorization", scaffold_admin_bearer())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn test_metrics_prometheus_endpoint() {
    let app = build_router(state_with_metrics());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/metrics")
                .header("authorization", scaffold_admin_bearer())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    let text = String::from_utf8(body.to_vec()).unwrap();
    // counter 导出
    assert!(text.contains("# HELP llm_service_tokens_used Total tokens used"));
    assert!(text.contains("# TYPE llm_service_tokens_used counter"));
    assert!(text.contains("llm_service_tokens_used{model=\"deepseek\"}"));
    // histogram 导出
    assert!(text.contains("# TYPE llm_service_latency histogram"));
    assert!(text.contains("llm_service_latency_bucket{le=\"0.025\"}"));
    assert!(text.contains("llm_service_latency_bucket{le=\"+Inf\"}"));
    assert!(text.contains("llm_service_latency_count"));
}

#[tokio::test]
async fn test_metrics_prometheus_no_aggregator_404() {
    let app = build_router(AppState::new());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/metrics")
                .header("authorization", scaffold_admin_bearer())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

// ── 多轮对话上下文修复集成测试 ──────────────────────────────
// 验证：process_via_engine_inner 从 store/registry 加载历史，state["messages"]
// 组装为完整消息序列（历史 + 当前 user），第二轮能看到第一轮上下文。

/// 模拟 LLM 插件：读取 state["messages"]，append assistant 回复后写回。
/// 记录每次收到的 messages（按调用顺序），供测试断言。
/// 测试 invoker（server::pipeline_version_tests 复用）：记录 LLM 调用入参
/// history / 完整 state 快照 / 生命周期钩子，回复以增量 op emit。
pub(crate) struct RecordingInvoker {
    seen: std::sync::Mutex<Vec<serde_json::Value>>,
    /// GAP-1：记录每次收到的完整 state 快照（断言 state overlay / lineage
    /// 是否进入插件可见 state）。
    seen_states: std::sync::Mutex<Vec<serde_json::Value>>,
    /// G2：list_plugin_tools 响应（plugin_id → tools/list JSON）。缺省空。
    list_tools: std::collections::HashMap<String, serde_json::Value>,
    /// GAP-2：记录收到的生命周期钩子 (plugin_id, hook 名, ctx JSON)。
    hooks: std::sync::Mutex<Vec<(String, String, serde_json::Value)>>,
}

#[async_trait::async_trait]
impl agentos_core::traits::PluginInvoker for RecordingInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        ctx: &agentos_core::types::PluginContext<'a>,
    ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
        let history = ctx
            .state
            .get("messages")
            .cloned()
            .unwrap_or_else(|| serde_json::json!([]));
        self.seen.lock().unwrap().push(history.clone());
        self.seen_states.lock().unwrap().push((*ctx.state).clone());
        // 模拟 LLM：构造 assistant 回复（内容基于收到的消息数，便于断言），
        // 以增量 op emit（零兼容：所有插件一律 op 模型，无全量数组分支）
        let reply_msg = serde_json::json!({
            "role": "assistant",
            "content": format!("回复第{}条", history.as_array().map(|a| a.len()).unwrap_or(1)),
        });
        let reply = reply_msg["content"].as_str().unwrap_or("").to_string();
        let mut updates = std::collections::HashMap::new();
        updates.insert("raw_result".to_string(), serde_json::json!(reply));
        updates.insert(
            "messages".to_string(),
            serde_json::json!({ "_ops": [{ "op": "set", "msg": reply_msg }] }),
        );
        Ok(agentos_core::types::PluginResult {
            state_updates: updates,
            ..Default::default()
        })
    }

    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &serde_json::Value,
    ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError> {
        Ok(agentos_core::types::ToolExecutionResult::success(
            serde_json::Value::Null,
        ))
    }

    async fn send_lifecycle_hook(
        &self,
        plugin_id: &str,
        hook: agentos_core::traits::LifecycleHook,
        context: &agentos_core::traits::HookContext,
    ) -> Result<(), agentos_core::types::PluginError> {
        let tag = |k: &str| context.get(k).cloned().unwrap_or(serde_json::Value::Null);
        self.hooks.lock().unwrap().push((
            plugin_id.to_string(),
            format!("{hook:?}"),
            serde_json::json!({
                "event": tag("event"),
                "pipeline_id": tag("pipeline_id"),
                "task_id": tag("task_id"),
                "parent_pipeline_id": tag("parent_pipeline_id"),
            }),
        ));
        Ok(())
    }
    async fn list_plugin_tools(
        &self,
        plugin_id: &str,
    ) -> Result<serde_json::Value, agentos_core::types::PluginError> {
        Ok(self
            .list_tools
            .get(plugin_id)
            .cloned()
            .unwrap_or(serde_json::json!({ "tools": [] })))
    }
}

/// 测试用最小 manifest（serde 默认填可选字段，与 plugin_watcher 测试同构）。
fn mk_manifest_json(id: &str) -> agentos_core::traits::PluginManifest {
    serde_json::from_value(serde_json::json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "pipeline", "language": "python",
        "host_type": "sidecar", "entry": "x", "capabilities": {},
    }))
    .expect("valid manifest")
}

/// live_plugin_ids 反映 manifests store 的运行期变化（watcher 热发现合并后立即可见）。
#[tokio::test]
async fn live_plugin_ids_reflects_manifests_store() {
    let state = AppState::new();
    assert!(live_plugin_ids(&state).await.is_empty());
    state
        .manifests
        .write()
        .await
        .push(mk_manifest_json("late_plugin"));
    assert!(live_plugin_ids(&state).await.contains("late_plugin"));
}

/// 管道引用"启动后才热发现"的插件能编译成功——已知插件面取自 manifests store
/// 而非启动快照，新插件热注册后无需重启即可作为管道 step。
#[tokio::test]
async fn hot_reload_compiles_step_referencing_plugin_discovered_after_boot() {
    let yaml = "name: t\nloop_bodies:\n  - id: main\n    steps:\n      - id: one\n        steps:\n          - late_plugin\n";
    let write_cfg = |root: &std::path::Path| {
        let cfg = root.join("config").join("pipelines");
        std::fs::create_dir_all(&cfg).unwrap();
        std::fs::write(cfg.join("autonomous.yaml"), yaml).unwrap();
        root.join("config")
    };

    // 场景 A：manifests 未含插件 → 未知引用编译失败且无历史产物 → None
    //（调用方显式报错拒绝空跑；空管道轮次零步"完成"= 零回复零错误帧）。
    let root_a = std::env::temp_dir().join(format!("hr_a_{}", uuid::Uuid::new_v4().simple()));
    let config_a = write_cfg(&root_a);
    // 钉用户根到本用例临时目录：resolve_pipeline_config_path 先查用户配置层，
    // 宿主机真实用户空间播种过 pipelines/autonomous.yaml 时会旁路 TempDir
    // （读到真实管道，其插件引用不在注入 manifests 内 → 编译恒败）。
    let _guard_a = crate::test_env::pin_user_root(&root_a);
    let state_a = AppState::new();
    assert!(
        maybe_reload_compiled_pipeline(&state_a, &config_a)
            .await
            .is_none(),
        "未知插件引用且无历史产物应返回 None（拒绝空跑）"
    );

    // 场景 B：同 YAML，manifests store 已含该插件（热发现合并后）→ 编译成功。
    let root_b = std::env::temp_dir().join(format!("hr_b_{}", uuid::Uuid::new_v4().simple()));
    let config_b = write_cfg(&root_b);
    // 同线程重复 pin「后钉者赢」：场景 B 的解析切到 root_b。
    let _guard_b = crate::test_env::pin_user_root(&root_b);
    let mut state_b = AppState::new();
    state_b.manifests = Arc::new(tokio::sync::RwLock::new(vec![mk_manifest_json(
        "late_plugin",
    )]));
    let compiled_b = maybe_reload_compiled_pipeline(&state_b, &config_b)
        .await
        .expect("提供方在场应编译成功");
    assert_eq!(
        compiled_b.bodies.len(),
        1,
        "热发现后的插件应可作为管道 step 编译"
    );
}

/// 测试用显式 steps 声明的 manifest（③级异名步骤提供方形态，如 todo.fold）。
fn mk_step_manifest_json(id: &str, step_name: &str) -> agentos_core::traits::PluginManifest {
    serde_json::from_value(serde_json::json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "pipeline", "language": "python",
        "host_type": "sidecar", "entry": "x",
        "capabilities": {"steps": [{"name": step_name}]},
    }))
    .expect("valid manifest")
}

/// 声明维自愈（回归核心，B6）：首次编译失败（步骤提供方未注册）后，提供方经
/// manifests 热注册补齐且**不改任何配置文件 mtime**——指纹的 manifests 摘要
/// 维度必须驱动重编译并自愈。指纹若只盖配置面，本场景恒 None（正是步骤引用
/// 超前于插件启停时序时的装机断言路径）。
#[tokio::test]
async fn reload_recovers_when_step_provider_registers_without_config_change() {
    let yaml = "name: t\nloop_bodies:\n  - id: main\n    steps:\n      - id: one\n        steps:\n          - late.fold\n";
    let root = std::env::temp_dir().join(format!("hr_fix_{}", uuid::Uuid::new_v4().simple()));
    let cfg = root.join("config").join("pipelines");
    std::fs::create_dir_all(&cfg).unwrap();
    std::fs::write(cfg.join("autonomous.yaml"), yaml).unwrap();
    let config_root = root.join("config");
    let _guard = crate::test_env::pin_user_root(&root);
    let state = AppState::new();

    // 首次：提供方缺位 → 编译失败且无历史产物 → None（显式报错拒绝空跑）
    assert!(maybe_reload_compiled_pipeline(&state, &config_root)
        .await
        .is_none());

    // 提供方插件热注册（磁盘全程不动：重编译由指纹的 manifests 摘要维驱动）
    state
        .manifests
        .write()
        .await
        .push(mk_step_manifest_json("pipeline_late_fold", "late.fold"));

    // TTL 门（1s）过后下一请求重试编译 → 自愈
    tokio::time::sleep(std::time::Duration::from_millis(1101)).await;
    let healed = maybe_reload_compiled_pipeline(&state, &config_root)
        .await
        .expect("提供方注册后必须重试编译并恢复（manifests 维必须驱动重编译）");
    assert_eq!(healed.bodies.len(), 1);

    // 自愈后健康态：TTL 内指纹不变 → 直接复用同一编译产物（零重编译）
    let cached = maybe_reload_compiled_pipeline(&state, &config_root).await;
    assert!(
        std::sync::Arc::ptr_eq(&healed, cached.as_ref().expect("健康态应返回产物")),
        "健康态必须复用缓存产物"
    );
}

/// B6 回归（声明维）：纯 manifests 变更（不改任何配置文件 mtime、上次编译
/// 成功）必须触发重编译——manifests 是编译输入第二维（步骤服务索引 + 已知
/// 插件集合都来自它），指纹键域必须覆盖；指纹缺声明维时成功态下声明变更
/// 只能命中旧产物。
#[tokio::test]
async fn reload_recompiles_when_manifests_change_without_config_change() {
    let yaml = "name: t\nloop_bodies:\n  - id: main\n    steps:\n      - id: one\n        steps:\n          - plain_plugin\n";
    let root = std::env::temp_dir().join(format!("hr_fp_{}", uuid::Uuid::new_v4().simple()));
    let cfg = root.join("config").join("pipelines");
    std::fs::create_dir_all(&cfg).unwrap();
    std::fs::write(cfg.join("autonomous.yaml"), yaml).unwrap();
    let config_root = root.join("config");
    let _guard = crate::test_env::pin_user_root(&root);
    let mut state = AppState::new();
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![mk_manifest_json(
        "plain_plugin",
    )]));

    let v1 = maybe_reload_compiled_pipeline(&state, &config_root)
        .await
        .expect("提供方在场应编译成功");

    // 声明面变更：新插件热注册（磁盘全程不动，配置 mtime 不变）
    state
        .manifests
        .write()
        .await
        .push(mk_manifest_json("newcomer_plugin"));
    tokio::time::sleep(std::time::Duration::from_millis(1101)).await;
    let v2 = maybe_reload_compiled_pipeline(&state, &config_root)
        .await
        .expect("声明变更后重编译仍应成功");
    assert!(
        !std::sync::Arc::ptr_eq(&v1, &v2),
        "manifests 变更必须触发重编译（不得命中旧产物）"
    );

    // 反向：声明面与配置面均不再变化 → TTL 过期后仍命中缓存（零重编译）
    tokio::time::sleep(std::time::Duration::from_millis(1101)).await;
    let v3 = maybe_reload_compiled_pipeline(&state, &config_root)
        .await
        .expect("健康态应持续供产物");
    assert!(
        std::sync::Arc::ptr_eq(&v2, &v3),
        "双维指纹均未变化必须命中缓存"
    );
}

/// B6 反向护栏（代码维）：纯插件代码变更不构成重编译理由——编译产物只编码
/// 配置面（yaml）与声明面（manifests），代码面变更由插件 respawn 承载；
/// manifest 内容不变的"watcher 重读合并"与插件代码文件改动都必须命中缓存。
#[tokio::test]
async fn reload_hits_cache_on_pure_plugin_code_change() {
    let yaml = "name: t\nloop_bodies:\n  - id: main\n    steps:\n      - id: one\n        steps:\n          - plain_plugin\n";
    let root = std::env::temp_dir().join(format!("hr_code_{}", uuid::Uuid::new_v4().simple()));
    let cfg = root.join("config").join("pipelines");
    std::fs::create_dir_all(&cfg).unwrap();
    std::fs::write(cfg.join("autonomous.yaml"), yaml).unwrap();
    let config_root = root.join("config");
    let _guard = crate::test_env::pin_user_root(&root);
    let mut state = AppState::new();
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![mk_manifest_json(
        "plain_plugin",
    )]));

    let v1 = maybe_reload_compiled_pipeline(&state, &config_root)
        .await
        .expect("提供方在场应编译成功");

    // 纯代码变更：插件实现文件改写 + manifest 原样重读合并（内容不变）——
    // 指纹两维均不得动。
    let plugins_dir = root.join("plugins");
    std::fs::create_dir_all(&plugins_dir).unwrap();
    std::fs::write(
        plugins_dir.join("plain_plugin.py"),
        "def execute():\n    ...\n",
    )
    .unwrap();
    *state.manifests.write().await = vec![mk_manifest_json("plain_plugin")];
    tokio::time::sleep(std::time::Duration::from_millis(1101)).await;
    let v2 = maybe_reload_compiled_pipeline(&state, &config_root)
        .await
        .expect("健康态应持续供产物");
    assert!(
        std::sync::Arc::ptr_eq(&v1, &v2),
        "纯代码变更（manifest 内容不变）必须命中缓存零重编译"
    );
}

/// 缺省路径无可用编译产物（编译失败且无历史产物）必须显式报错拒绝空跑；
/// 正常配置对照返回 Ok——两组输入区分失败面与健康面。
#[tokio::test]
async fn compiled_for_none_errors_when_no_artifact_and_ok_when_healthy() {
    // 场景 A（失败面）：引用未注册步骤 → Err 且点名拒绝空跑；TTL 窗口后仍
    // Err（失败态稳定，直至提供方/配置修复）。
    let root_a =
        std::env::temp_dir().join(format!("lazy_none_a_{}", uuid::Uuid::new_v4().simple()));
    let cfg_a = root_a.join("config").join("pipelines");
    std::fs::create_dir_all(&cfg_a).unwrap();
    std::fs::write(
        cfg_a.join("autonomous.yaml"),
        "name: t\nloop_bodies:\n  - id: main\n    steps:\n      - id: one\n        steps:\n          - ghost.fold\n",
    )
    .unwrap();
    let config_a = root_a.join("config");
    let _guard_a = crate::test_env::pin_user_root(&root_a);
    let state = AppState::new();
    let err = crate::server::compiled_for(&state, &config_a, None, None)
        .await
        .expect_err("无可用产物必须报错（不静默空跑）");
    assert!(err.contains("拒绝空管道"), "错误应点名拒绝空跑: {err}");
    tokio::time::sleep(std::time::Duration::from_millis(1101)).await;
    assert!(
        crate::server::compiled_for(&state, &config_a, None, None)
            .await
            .is_err(),
        "修复到达前失败态必须稳定"
    );

    // 场景 B（健康对照）：合法配置 → Ok。
    let root_b =
        std::env::temp_dir().join(format!("lazy_none_b_{}", uuid::Uuid::new_v4().simple()));
    let cfg_b = root_b.join("config").join("pipelines");
    std::fs::create_dir_all(&cfg_b).unwrap();
    std::fs::write(
        cfg_b.join("autonomous.yaml"),
        "name: t\nloop_bodies:\n  - id: main\n    steps:\n      - id: one\n        steps: []\n",
    )
    .unwrap();
    let config_b = root_b.join("config");
    let _guard_b = crate::test_env::pin_user_root(&root_b);
    crate::server::compiled_for(&state, &config_b, None, None)
        .await
        .expect("合法配置应编译成功");
}

/// 已有成功产物后配置再损坏：继续供旧产物（快照语义），失败重试不丢产物、
/// 不换静默空跑。
#[tokio::test]
async fn reload_keeps_serving_old_artifact_when_config_breaks_later() {
    let root = std::env::temp_dir().join(format!("hr_old_{}", uuid::Uuid::new_v4().simple()));
    let cfg = root.join("config").join("pipelines");
    std::fs::create_dir_all(&cfg).unwrap();
    std::fs::write(
        cfg.join("autonomous.yaml"),
        "name: t\nloop_bodies:\n  - id: main\n    steps:\n      - id: one\n        steps: []\n",
    )
    .unwrap();
    let config_root = root.join("config");
    let _guard = crate::test_env::pin_user_root(&root);
    let state = AppState::new();

    let healthy = maybe_reload_compiled_pipeline(&state, &config_root)
        .await
        .expect("合法配置应编译成功");

    // 配置损坏（引用未知步骤，mtime 必然变化）→ TTL 过后重载失败 → 旧产物续供
    std::fs::write(
        cfg.join("autonomous.yaml"),
        "name: t\nloop_bodies:\n  - id: main\n    steps:\n      - id: one\n        steps:\n          - ghost.step\n",
    )
    .unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(1101)).await;
    let degraded = maybe_reload_compiled_pipeline(&state, &config_root)
        .await
        .expect("损坏期间必须续供旧产物（不空跑不丢失）");
    assert!(
        std::sync::Arc::ptr_eq(&healthy, &degraded),
        "续供的必须是同一旧产物"
    );

    // 失败态下 TTL 窗口重试仍失败 → 旧产物继续（重试不破坏服务面）
    tokio::time::sleep(std::time::Duration::from_millis(1101)).await;
    let again = maybe_reload_compiled_pipeline(&state, &config_root)
        .await
        .expect("反复失败期间旧产物必须持续可用");
    assert!(std::sync::Arc::ptr_eq(&healthy, &again));
}

// ── 管道配置按需加载编译缓存（compiled_for 收敛点）──────────────────

/// 构造临时 config 根并写入指定管道配置（钉用户根保证解析只看本用例目录）。
fn write_lazy_config(root: &std::path::Path, name: &str, body_id: &str) -> std::path::PathBuf {
    let cfg = root.join("config").join("pipelines");
    std::fs::create_dir_all(&cfg).unwrap();
    std::fs::write(
        cfg.join(format!("{name}.yaml")),
        format!(
            "name: {name}\nloop_bodies:\n  - id: {body_id}\n    steps:\n      - id: only\n        steps: []\n"
        ),
    )
    .unwrap();
    root.join("config")
}

/// 按需加载：首次显式指定触发磁盘加载并编译；二次命中缓存（磁盘 mtime 变化
/// 不影响——一期不校验 mtime，PUT 失效是唯一失效路径）；失效后重载新内容。
#[tokio::test]
async fn compiled_for_lazy_loads_caches_and_invalidates() {
    let name = format!("lazy_{}", uuid::Uuid::new_v4().simple());
    let root = std::env::temp_dir().join(format!("lazy_a_{}", uuid::Uuid::new_v4().simple()));
    let _guard = crate::test_env::pin_user_root(&root);
    let config_root = write_lazy_config(&root, &name, "body_v1");
    let state = AppState::new();

    // 首次：磁盘加载 + 编译 + 入缓存
    let first = crate::server::compiled_for(&state, &config_root, Some(&name), None)
        .await
        .expect("首次按需加载应成功");
    assert_eq!(first.bodies.len(), 1);
    assert_eq!(first.bodies[0].id, "body_v1");

    // 磁盘内容改变（mtime 必然变化）→ 二次调用仍命中缓存（旧内容）
    write_lazy_config(&root, &name, "body_v2");
    let second = crate::server::compiled_for(&state, &config_root, Some(&name), None)
        .await
        .expect("缓存命中应成功");
    assert!(
        std::sync::Arc::ptr_eq(&first, &second),
        "缓存命中必须返回同一编译产物（不重读磁盘）"
    );
    assert_eq!(
        second.bodies[0].id, "body_v1",
        "mtime 变化不触发重载（一期契约）"
    );

    // 失效（PUT 热更唯一失效路径）→ 下次按需重载新内容
    crate::server::pipeline_cache_invalidate(&name);
    let third = crate::server::compiled_for(&state, &config_root, Some(&name), None)
        .await
        .expect("失效后重载应成功");
    assert_eq!(third.bodies[0].id, "body_v2", "失效后必须读到磁盘新内容");
    crate::server::pipeline_cache_invalidate(&name);
}

/// 失败透传：显式指定不存在的配置 → Err 带管道名，绝不回落 autonomous
///（静默回落 = 用错管道）；坏 YAML 同样报错且点名配置。
#[tokio::test]
async fn compiled_for_explicit_config_failure_errors_without_fallback() {
    let state = AppState::new();
    let root = std::env::temp_dir().join(format!("lazy_b_{}", uuid::Uuid::new_v4().simple()));
    let _guard = crate::test_env::pin_user_root(&root);
    let config_root = root.join("config");
    std::fs::create_dir_all(config_root.join("pipelines")).unwrap();

    let missing = format!("ghost_{}", uuid::Uuid::new_v4().simple());
    let err = crate::server::compiled_for(&state, &config_root, Some(&missing), None)
        .await
        .expect_err("不存在的配置必须报错（回落 autonomous 会返回 Ok）");
    assert!(err.contains(&missing), "错误必须带管道名便于定位: {err}");

    // 坏 YAML：错误同样透传且点名配置（不降级、不回落）
    let broken = format!("broken_{}", uuid::Uuid::new_v4().simple());
    std::fs::write(
        config_root.join("pipelines").join(format!("{broken}.yaml")),
        "name: [unclosed\n",
    )
    .unwrap();
    let err = crate::server::compiled_for(&state, &config_root, Some(&broken), None)
        .await
        .expect_err("坏配置必须报错");
    assert!(err.contains(&broken), "错误必须带管道名: {err}");
    crate::server::pipeline_cache_invalidate(&broken);
}

/// 缺省路径（None）= autonomous 热重载单例：与 maybe_reload_compiled_pipeline
/// 返回同一实例（现状行为不变，回归红线）。
#[tokio::test]
async fn compiled_for_none_returns_hot_reload_singleton() {
    let yaml =
        "name: t\nloop_bodies:\n  - id: main\n    steps:\n      - id: one\n        steps: []\n";
    let root = std::env::temp_dir().join(format!("lazy_c_{}", uuid::Uuid::new_v4().simple()));
    let _guard = crate::test_env::pin_user_root(&root);
    let cfg = root.join("config").join("pipelines");
    std::fs::create_dir_all(&cfg).unwrap();
    std::fs::write(cfg.join("autonomous.yaml"), yaml).unwrap();
    let config_root = root.join("config");
    let state = AppState::new();

    let via_none = crate::server::compiled_for(&state, &config_root, None, None)
        .await
        .expect("缺省路径应成功");
    let via_reload = crate::server::maybe_reload_compiled_pipeline(&state, &config_root)
        .await
        .expect("热重载路径应有产物");
    assert!(
        std::sync::Arc::ptr_eq(&via_none, &via_reload),
        "None 必须收敛到既有热重载单例（同一 Arc）"
    );
}

/// 构造带 store + mock invoker 的 AppState（enable_session 以启用 registry 路径）。
/// 创建临时 config 目录 + autonomous.yaml（引用 mock LLM 插件），使
/// maybe_reload_pipeline_configs 能加载真实配置（否则 load_pipeline_config
/// 在文件缺失时返回空 steps 配置，executor 不会调用任何插件）。
/// 建会话并绑定 active_pipeline_id + pipeline_sessions 映射（REST chat 坐标解析前置）。
async fn seed_session_with_pipeline(
    sqlite: &Arc<agentos_engine::SqliteStore>,
    thread_id: &str,
    pipeline_id: &str,
    tenant_id: &str,
) {
    // 播种 admin（与生产 seed_admin_user 一致）：带 store 的 router 登录走 DB 用户表
    let admin = agentos_core::types::UserRecord {
        user_id: "00000000-0000-0000-0000-000000000001".to_string(),
        username: "admin".to_string(),
        password: agentos_http::auth::hash_password(SEED_ADMIN_PW).unwrap(),
        email: Some("admin@agentos.dev".to_string()),
        role: "admin".to_string(),
        tenant_id: tenant_id.to_string(),
        created_at: "2026-08-30T00:00:00Z".to_string(),
        last_login_at: None,
        must_change_password: false,
    };
    let _ = StorageBackend::create_user(sqlite.as_ref(), &admin).await;
    let now = "2026-08-30T00:00:00Z";
    sqlite
        .create_session(&agentos_core::types::SessionRecord {
            thread_id: thread_id.to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: Some(pipeline_id.to_string()),
            pipeline_ids: vec![pipeline_id.to_string()],
            metadata: None,
            created_at: now.to_string(),
            updated_at: now.to_string(),
            last_active_at: Some(now.to_string()),
        })
        .await
        .unwrap();
    sqlite
        .link_pipeline_session(pipeline_id, thread_id, tenant_id)
        .await
        .unwrap();
}

/// 带真实内存 SqliteStore + RecordingInvoker 的引擎执行夹具
/// （server::pipeline_version_tests 复用，D9 版本钉住）。
pub(crate) fn make_engine_state() -> (
    AppState,
    Arc<RecordingInvoker>,
    Arc<dyn agentos_core::traits::StorageBackend>,
    Arc<agentos_engine::SqliteStore>,
    crate::test_env::UserSpaceGuard,
) {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn agentos_core::traits::StorageBackend> = sqlite.clone();
    let invoker = Arc::new(RecordingInvoker {
        seen: std::sync::Mutex::new(Vec::new()),
        seen_states: std::sync::Mutex::new(Vec::new()),
        hooks: std::sync::Mutex::new(Vec::new()),
        list_tools: std::collections::HashMap::new(),
    });
    // 临时项目根：含 config/pipelines/autonomous.yaml，引用 mock LLM 插件
    let tmp_root = std::env::temp_dir().join(format!("mt_test_{}", uuid::Uuid::new_v4().simple()));
    // 用户空间钉到该临时根：`resolve_pipeline_config_path` 先查用户配置层，
    // 宿主机真实用户空间被播种过 pipelines/autonomous.yaml（真机跑过一次应用
    // 即会）便旁路下面的临时 YAML，用例读到真实配置而误红。guard 随返回值交给
    // 调用方保活（drop 即还原）。
    let _user_space_guard = crate::test_env::pin_user_root(&tmp_root);
    let cfg_dir = tmp_root.join("config").join("pipelines");
    std::fs::create_dir_all(&cfg_dir).unwrap();
    std::fs::write(
            cfg_dir.join("autonomous.yaml"),
            "name: test_multi_turn\nloop_bodies:\n  - id: main\n    steps:\n      - id: llm\n        steps:\n          - mock_llm_core\n",
        )
        .unwrap();
    let mut state = AppState::new();
    state.store = Some(store.clone());
    state.invoker = Some(invoker.clone());
    state.project_root = Some(tmp_root);
    // 注入统一数据接口句柄（pipeline-state.update 冷路径写依赖）
    state.db = Some(sqlite.clone());
    // 已知插件面 = 共享 manifests store（live_plugin_ids 现读，与热发现语义一致）
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![mk_manifest_json(
        "mock_llm_core",
    )]));
    (state, invoker, store, sqlite, _user_space_guard)
}

#[tokio::test]
async fn test_multi_turn_second_round_sees_first_round_context() {
    let (state, invoker, _store, _sqlite, _user_space_guard) = make_engine_state();
    let tenant = TenantContext::new("tenant_mt", "thread_mt");
    let pipe = "pipe_mt";
    let thread = "thread_mt";

    // 第一轮：pipeline_id=pipe_mt 非空（WS 路径 route_id 语义）
    let r1 = agentos_tenant::scope(
        tenant.clone(),
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: (thread).to_string(),
                user_id: ("").to_string(),
                pipeline_id: (pipe).to_string(),
                content: ("第一轮：我叫小明").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: None,
                pipeline_config_id: None,
            },
            "agentos",
            "m1",
        ),
    )
    .await;
    assert!(!r1.content.is_empty(), "第一轮应返回 assistant 回复");

    // 第二轮：同 pipeline_id，应看到第一轮 user+assistant 上下文
    let r2 = agentos_tenant::scope(
        tenant,
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: (thread).to_string(),
                user_id: ("").to_string(),
                pipeline_id: (pipe).to_string(),
                content: ("第二轮：我叫什么？").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: None,
                pipeline_config_id: None,
            },
            "agentos",
            "m2",
        ),
    )
    .await;
    assert!(!r2.content.is_empty(), "第二轮应返回 assistant 回复");

    // 断言：第二轮 LLM 收到的 messages 是完整序列（历史 + 当前）
    let seen = invoker.seen.lock().unwrap();
    assert_eq!(seen.len(), 2, "应有两轮 LLM 调用");
    let first = seen[0].as_array().unwrap();
    assert_eq!(first.len(), 1, "第一轮应只有当前 user 消息");
    assert_eq!(first[0]["role"], "user");
    assert_eq!(first[0]["content"], "第一轮：我叫小明");

    let second = seen[1].as_array().unwrap();
    // 完整序列 = 第一轮 user + 第一轮 assistant + 第二轮 user
    assert_eq!(
        second.len(),
        3,
        "第二轮应含第一轮上下文（user+assistant）+ 当前 user"
    );
    assert_eq!(second[0]["role"], "user");
    assert_eq!(second[0]["content"], "第一轮：我叫小明");
    assert_eq!(second[1]["role"], "assistant");
    assert!(second[1]["content"].as_str().unwrap().contains("回复第1条"));
    assert_eq!(second[2]["role"], "user");
    assert_eq!(second[2]["content"], "第二轮：我叫什么？");
}

#[tokio::test]
async fn test_multi_turn_http_pipeline_coordinate_context_and_reject_missing() {
    // pipeline_id 是执行态唯一坐标：多轮上下文按管道坐标累积；空坐标 = 协议
    // 违约，显式拒绝（会话 id 是组织集合 id，不得回退充当执行坐标）。
    let (state, invoker, _store, _sqlite, _user_space_guard) = make_engine_state();
    let tenant = TenantContext::new("tenant_http", "thread_http");
    let thread = "thread_http";
    let pipe = "pipe_http";

    // 空坐标拒绝：failed outcome，不产生任何执行
    let rejected = agentos_tenant::scope(
        tenant.clone(),
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: (thread).to_string(),
                user_id: ("").to_string(),
                pipeline_id: ("").to_string(),
                content: ("HTTP 零轮").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: None,
                pipeline_config_id: None,
            },
            "agentos",
            "h0",
        ),
    )
    .await;
    assert!(rejected.failed, "空 pipeline_id 必须显式拒绝");

    let r1 = agentos_tenant::scope(
        tenant.clone(),
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: (thread).to_string(),
                user_id: ("").to_string(),
                pipeline_id: (pipe).to_string(),
                content: ("HTTP 第一轮").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: None,
                pipeline_config_id: None,
            },
            "agentos",
            "h1",
        ),
    )
    .await;
    assert!(!r1.content.is_empty());

    let r2 = agentos_tenant::scope(
        tenant,
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: (thread).to_string(),
                user_id: ("").to_string(),
                pipeline_id: (pipe).to_string(),
                content: ("HTTP 第二轮").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: None,
                pipeline_config_id: None,
            },
            "agentos",
            "h2",
        ),
    )
    .await;
    assert!(!r2.content.is_empty());

    let seen = invoker.seen.lock().unwrap();
    assert_eq!(seen.len(), 2);
    let second = seen[1].as_array().unwrap();
    assert_eq!(
        second.len(),
        3,
        "HTTP 路径多轮上下文按 pipeline 坐标累积，第二轮应看到历史"
    );
    assert_eq!(second[0]["content"], "HTTP 第一轮");
    assert_eq!(second[2]["content"], "HTTP 第二轮");
}

#[tokio::test]
async fn test_multi_turn_cold_start_recovers_from_store() {
    // 冷路径验证：registry 未命中（新进程/重启）时，从 message_slots 表恢复历史
    // （零兼容重排：messages 持久真值 = slots 表，checkpoint/traces 只管标量）。
    // 模拟：直接向 slots 写入第一轮 user+assistant（pipeline_id=pipe_cold），
    // 再调用 process_via_engine，断言 LLM 收到历史 + 当前。
    let (state, invoker, store, sqlite, _user_space_guard) = make_engine_state();
    let tenant = TenantContext::new("tenant_cold", "thread_cold");
    let pipe = "pipe_cold";
    let thread = "thread_cold";

    // 直接写 slots（模拟上一轮已持久化，registry 无该管道——冷启动）
    let store_ref = store.clone();
    agentos_tenant::scope(tenant.clone(), async {
            store_ref
                .record_run_start(pipe, "tenant_cold", "run_cold", "", "2026-10-15T00:00:00+00:00")
                .await
                .unwrap();
            store_ref.link_pipeline_session(pipe, thread, "tenant_cold").await.unwrap();
            sqlite
                .apply_messages_ops_to_table(pipe, "tenant_cold", &[
                    serde_json::json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "冷启动第一轮"}}),
                    serde_json::json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "冷启动回复"}}),
                ])
                .unwrap();
        })
        .await;

    // 验证写库成功（恢复前置条件）
    let check_store = store.clone();
    let found = agentos_tenant::scope(tenant.clone(), async {
        check_store
            .get_messages_by_pipeline(pipe, MessageQueryOpts::default())
            .await
            .unwrap()
    })
    .await;
    assert_eq!(found.len(), 2, "冷启动写库应成功且 tenant 一致");

    let r = agentos_tenant::scope(
        tenant,
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: (thread).to_string(),
                user_id: ("").to_string(),
                pipeline_id: (pipe).to_string(),
                content: ("冷启动第二轮").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: None,
                pipeline_config_id: None,
            },
            "agentos",
            "c2",
        ),
    )
    .await;
    assert!(!r.content.is_empty(), "冷启动第二轮应返回 assistant 回复");

    let seen = invoker.seen.lock().unwrap();
    assert_eq!(seen.len(), 1, "冷启动应从 store 恢复历史并调用 LLM");
    let msgs = seen[0].as_array().unwrap();
    assert_eq!(
        msgs.len(),
        3,
        "冷启动应从 store 恢复第一轮 user+assistant + 当前 user"
    );
    assert_eq!(msgs[0]["content"], "冷启动第一轮");
    assert_eq!(msgs[1]["role"], "assistant");
    assert_eq!(msgs[2]["content"], "冷启动第二轮");
}

#[tokio::test]
async fn test_cold_recovery_ignores_stale_ended_flag() {
    // 回归锚：冷恢复（registry 丢失）时，旧 checkpoint 的
    // `ended=true`（post 阶段 pipeline_track 每轮写入）若残留进本轮 initial_state，
    // 引擎 execute_steps/execute_body 见 ended 即短路——run 秒终 completed、
    // LLM 一次请求都不发（真机：主管道 38ms 秒终 + 两个任务管道 1-2s 秒终，
    // 仅 1 条 user_input trace）。ended 属 per-run 易变键（VOLATILE_RUN_KEYS），
    // 冷恢复必须跳过，本轮以 stage_build_initial_state 的 ended=false 起跑。
    let (state, invoker, store, sqlite, _user_space_guard) = make_engine_state();
    let tenant = TenantContext::new("tenant_ended", "thread_ended");
    let pipe = "pipe_ended";
    let thread = "thread_ended";

    // 模拟上一轮已持久化（registry 无该管道 = 冷启动），且旧 checkpoint 带
    // ended=true（修复前版本落档形态）。
    let store_ref = store.clone();
    agentos_tenant::scope(tenant.clone(), async {
            store_ref
                .record_run_start(pipe, "tenant_ended", "run_ended", "", "2026-10-15T00:00:00+00:00")
                .await
                .unwrap();
            store_ref.link_pipeline_session(pipe, thread, "tenant_ended").await.unwrap();
            sqlite
                .apply_messages_ops_to_table(pipe, "tenant_ended", &[
                    serde_json::json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "上一轮"}}),
                    serde_json::json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "上一轮回复"}}),
                ])
                .unwrap();
            // 旧 checkpoint：ended=true + 其它标量（模拟修复前 save_checkpoint 落档）
            let stale = serde_json::json!({
                "ended": true,
                "current_phase": "exit",
                "core_plugin": "pipeline_llm_core",
                "track.total_tokens": 130433,
            });
            store_ref
                .save_checkpoint(pipe, "tenant_ended", 1, &stale)
                .await
                .unwrap();
        })
        .await;

    let r = agentos_tenant::scope(
        tenant,
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: (thread).to_string(),
                user_id: ("").to_string(),
                pipeline_id: (pipe).to_string(),
                content: ("冷启动第二轮").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: None,
                pipeline_config_id: None,
            },
            "agentos",
            "e2",
        ),
    )
    .await;
    assert!(!r.content.is_empty(), "冷启动第二轮应返回 assistant 回复");

    // ★ 回归锚：LLM 必须被调用（ended=true 残留时引擎短路，seen 为空）
    let seen = invoker.seen.lock().unwrap();
    assert_eq!(seen.len(), 1, "ended 残留不得让 run 秒终——LLM 应被调用");
    let msgs = seen[0].as_array().unwrap();
    assert_eq!(msgs.len(), 3, "应恢复上一轮 user+assistant + 当前 user");
    assert_eq!(msgs[2]["content"], "冷启动第二轮");
}

// ── 多用户持久化 + 数据隔离端到端测试（0.5.0 最小持久化地基）──
//
// 验证核心契约：两个不同用户（不同 tenant）各自发消息 → 各自能读到自己的历史
// → 跨 tenant 读不到对方（隔离）。链路与生产一致：process_via_engine → 落库，
// get_messages_by_pipeline 按 task_local tenant 过滤。

/// 端到端：两用户各自发消息 + 读历史，验证数据隔离。
///
/// 复用 make_engine_state 的 mock 引擎（RecordingInvoker），但注入两个真实
/// 用户到 store（一用户一租户）。模拟 chat_handler / dispatch_user_input 的
/// 核心链路：在各自 tenant scope 内调 process_via_engine（落库），再用
/// get_messages_by_pipeline 在各自 scope 内读回。
#[tokio::test]
async fn test_multi_user_isolation_end_to_end() {
    let (state, _invoker, store, _sqlite, _user_space_guard) = make_engine_state();

    // 播种两个用户：alice → tenant_alice，bob → tenant_bob（一用户一租户）
    let now = chrono::Utc::now().to_rfc3339();
    let alice = agentos_core::types::UserRecord {
        user_id: "u-alice-001".to_string(),
        username: "alice".to_string(),
        password: "x".to_string(),
        email: None,
        role: "user".to_string(),
        tenant_id: "tenant_alice".to_string(),
        created_at: now.clone(),
        last_login_at: None,
        must_change_password: false,
    };
    let bob = agentos_core::types::UserRecord {
        user_id: "u-bob-002".to_string(),
        username: "bob".to_string(),
        password: "x".to_string(),
        email: None,
        role: "user".to_string(),
        tenant_id: "tenant_bob".to_string(),
        created_at: now,
        last_login_at: None,
        must_change_password: false,
    };
    store.create_user(&alice).await.unwrap();
    store.create_user(&bob).await.unwrap();

    let pipe_a = "pipe_alice";
    let pipe_b = "pipe_bob";
    let thread_a = "thread_alice";
    let thread_b = "thread_bob";

    // alice 发消息（在 alice 的 tenant scope 内，模拟 dispatch_user_input）
    let r_a = agentos_tenant::scope(
        TenantContext::new("tenant_alice", thread_a),
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: (thread_a).to_string(),
                user_id: ("").to_string(),
                pipeline_id: (pipe_a).to_string(),
                content: ("alice 的消息").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: None,
                pipeline_config_id: None,
            },
            "agentos",
            "a1",
        ),
    )
    .await;
    assert!(!r_a.content.is_empty(), "alice 发消息应返回 assistant 回复");

    // bob 发消息（在 bob 的 tenant scope 内）
    let r_b = agentos_tenant::scope(
        TenantContext::new("tenant_bob", thread_b),
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: (thread_b).to_string(),
                user_id: ("").to_string(),
                pipeline_id: (pipe_b).to_string(),
                content: ("bob 的消息").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: None,
                pipeline_config_id: None,
            },
            "agentos",
            "b1",
        ),
    )
    .await;
    assert!(!r_b.content.is_empty(), "bob 发消息应返回 assistant 回复");

    // alice 在自己 scope 内能读到自己的消息（user + assistant ≥ 2 条）
    let store_a = store.clone();
    let msgs_a = agentos_tenant::scope(TenantContext::new("tenant_alice", thread_a), async move {
        store_a
            .get_messages_by_pipeline(pipe_a, MessageQueryOpts::default())
            .await
    })
    .await
    .unwrap();
    assert!(
        msgs_a.len() >= 2,
        "alice 应能读到自己的 user+assistant 消息，实际 {}",
        msgs_a.len()
    );

    // bob 在自己 scope 内能读到自己的消息
    let store_b = store.clone();
    let msgs_b = agentos_tenant::scope(TenantContext::new("tenant_bob", thread_b), async move {
        store_b
            .get_messages_by_pipeline(pipe_b, MessageQueryOpts::default())
            .await
    })
    .await
    .unwrap();
    assert!(msgs_b.len() >= 2, "bob 应能读到自己的消息");

    // ★ 隔离断言：在 bob 的 scope 内读 alice 的 pipeline，必须为空
    let store_cross = store.clone();
    let cross = agentos_tenant::scope(TenantContext::new("tenant_bob", thread_b), async move {
        store_cross
            .get_messages_by_pipeline(pipe_a, MessageQueryOpts::default())
            .await
    })
    .await
    .unwrap();
    assert!(
        cross.is_empty(),
        "tenant_bob 必须读不到 tenant_alice 的消息（数据隔离）"
    );

    // 反向：alice scope 内读 bob 的 pipeline，也必须为空
    let store_cross2 = store.clone();
    let cross2 = agentos_tenant::scope(TenantContext::new("tenant_alice", thread_a), async move {
        store_cross2
            .get_messages_by_pipeline(pipe_b, MessageQueryOpts::default())
            .await
    })
    .await
    .unwrap();
    assert!(
        cross2.is_empty(),
        "tenant_alice 必须读不到 tenant_bob 的消息"
    );

    // 验证消息内容确实是各自的（alice 的 user 消息内容含 "alice"）
    let alice_user_msg = msgs_a
        .iter()
        .find(|m| m.role == "user")
        .expect("alice 应有 user 消息");
    assert!(
        alice_user_msg
            .content_preview
            .as_deref()
            .unwrap_or("")
            .contains("alice"),
        "alice 的消息内容应含 'alice'"
    );
}

/// 验证 register → login → 发消息 → 读历史 的完整用户流程（含持久化用户）。
///
/// 用真实 store 跑 register/login handler（经 build_router），拿到 token 后
/// 模拟 WS 路径发消息，验证新注册用户能正常保存和读取自己的历史。
#[tokio::test]
async fn test_registered_user_can_save_and_read_history() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    // 播种 admin（login admin 兜底用）
    let now = chrono::Utc::now().to_rfc3339();
    let admin = agentos_core::types::UserRecord {
        user_id: "00000000-0000-0000-0000-000000000001".to_string(),
        username: "admin".to_string(),
        password: agentos_http::auth::hash_password(SEED_ADMIN_PW).unwrap(),
        email: None,
        role: "admin".to_string(),
        tenant_id: "default".to_string(),
        created_at: now.clone(),
        last_login_at: None,
        must_change_password: false,
    };
    store.create_user(&admin).await.unwrap();

    // 注册新用户 frank（一用户一租户）
    let frank_id = "u-frank-003".to_string();
    let frank = agentos_core::types::UserRecord {
        user_id: frank_id.clone(),
        username: "frank".to_string(),
        password: "frank123".to_string(),
        email: None,
        role: "user".to_string(),
        tenant_id: frank_id.clone(), // 一用户一租户
        created_at: now,
        last_login_at: None,
        must_change_password: false,
    };
    store.create_user(&frank).await.unwrap();

    // frank 的 tenant = frank_id（非 default），在自己的 scope 内发消息 + 读
    let mut state = AppState::new();
    state.store = Some(store.clone());
    state.invoker = Some(Arc::new(RecordingInvoker {
        seen: std::sync::Mutex::new(Vec::new()),
        seen_states: std::sync::Mutex::new(Vec::new()),
        hooks: std::sync::Mutex::new(Vec::new()),
        list_tools: std::collections::HashMap::new(),
    }));
    // 临时 config（make_engine_state 的精简版，足够 process_via_engine 跑通）
    let tmp_root =
        std::env::temp_dir().join(format!("frank_test_{}", uuid::Uuid::new_v4().simple()));
    // 用户空间钉到该临时根（与 make_engine_state 同纪律，8d3df5ded 漏网点）：
    // resolve_pipeline_config_path 用户配置层优先，宿主机真实用户空间被播种过
    // pipelines/autonomous.yaml（真机跑过一次应用即会）便旁路下面的临时 YAML，
    // 用例读到真实生产管道而误红——本用例手工搭 state 不经 make_engine_state，
    // 单跑隔离必红；满套件并行时邻居用例的 pin 恰好接管进程环境才绿（llvm-cov
    // 下一次抖红即让 api crate 覆盖整体归零，line% 假跌，见
    // .github/rust-coverage-baseline.txt 度量陷阱节）。guard 保活到用例结束。
    let _user_space_guard = crate::test_env::pin_user_root(&tmp_root);
    let cfg_dir = tmp_root.join("config").join("pipelines");
    std::fs::create_dir_all(&cfg_dir).unwrap();
    std::fs::write(
            cfg_dir.join("autonomous.yaml"),
            "name: t\nloop_bodies:\n  - id: main\n    steps:\n      - id: llm\n        steps:\n          - mock_llm_core\n",
        ).unwrap();
    state.project_root = Some(tmp_root);
    // 已知插件面 = 共享 manifests store（live_plugin_ids 现读，与热发现语义一致）
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![mk_manifest_json(
        "mock_llm_core",
    )]));

    let pipe = "pipe_frank";
    let thread = "thread_frank";
    // frank 发消息（tenant = frank_id）
    let r = agentos_tenant::scope(
        TenantContext::new(&frank_id, thread),
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: (thread).to_string(),
                user_id: ("").to_string(),
                pipeline_id: (pipe).to_string(),
                content: ("frank 的问题").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: None,
                pipeline_config_id: None,
            },
            "agentos",
            "f1",
        ),
    )
    .await;
    assert!(!r.content.is_empty(), "frank 发消息应成功");

    // frank 能读到自己的历史
    let store_read = store.clone();
    let msgs = agentos_tenant::scope(TenantContext::new(&frank_id, thread), async move {
        store_read
            .get_messages_by_pipeline(pipe, MessageQueryOpts::default())
            .await
    })
    .await
    .unwrap();
    assert!(msgs.len() >= 2, "frank 应能读到自己的历史");

    // admin（default 租户）读不到 frank 的消息
    let store_admin = store.clone();
    let admin_msgs =
        agentos_tenant::scope(TenantContext::new("default", "admin_thread"), async move {
            store_admin
                .get_messages_by_pipeline(pipe, MessageQueryOpts::default())
                .await
        })
        .await
        .unwrap();
    assert!(
        admin_msgs.is_empty(),
        "admin(default) 不应读到 frank 的消息"
    );
}

// ── CORS Origin 白名单（回归：反射任意 Origin + 凭据 = 跨域数据泄露）──

#[test]
fn local_origins_allowed_any_port() {
    assert!(super::is_local_origin("http://localhost:5173"));
    assert!(super::is_local_origin("https://localhost:9100"));
    assert!(super::is_local_origin("http://127.0.0.1:3000"));
    assert!(super::is_local_origin("https://127.0.0.1:443"));
    assert!(super::is_local_origin("http://[::1]:8080"));
}

#[test]
fn nonlocal_origins_rejected_by_local_check() {
    assert!(!super::is_local_origin("https://evil.com"));
    // 边界：localhost.evil.com 不应冒充 localhost（防前缀绕过）
    assert!(!super::is_local_origin("http://localhost.evil.com"));
    assert!(!super::is_local_origin("http://127.0.0.1.evil.com"));
}

#[test]
fn allowlist_exact_match_only() {
    let allow = ["https://app.example.com", "https://www.example.com"];
    assert!(super::origin_matches_allowlist(
        "https://app.example.com",
        &allow
    ));
    // 精确匹配——子域/变体不应通过
    assert!(!super::origin_matches_allowlist(
        "https://evil.example.com",
        &allow
    ));
    assert!(!super::origin_matches_allowlist(
        "https://app.example.com.evil.com",
        &allow
    ));
}

// ── GAP-1 阶段 1：自由 state overlay / lineage 并入 initial_state ──────
// 契约：chat.send_message 的 state 注入在 execution_context 合并点
// （1a/1a2）之后并入顶层扁平键（task.*/lineage.* 皆透传，内核零解释）。

mod execution_context;

#[tokio::test]
async fn test_stage_recover_history_merges_overlay_with_birth_fields() {
    // overlay 应用点已移至恢复合并之后（B13① 域界定，ADR 2026-09-06）：
    // 本测试钉死"overlay 顶层扁平键并入 + 引擎出生字段/保护字段不破"的合并面。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite;
    let pipeline = format!("pipe_ov_birth_{}", uuid::Uuid::new_v4().simple());
    let overlay = json!({
        "task.goal": "喝水提醒",
        "task.status": "pending",
        "task.id": "31bfdee19720",
        "lineage.parent_pipeline_id": "pipe_parent",
        "lineage.origin_session_id": "sess_root",
        "lineage.root": true,
    });
    let initial = json!({
        "message": "msg",
        "pipeline_id": pipeline,
        "session_id": "thread_new",
        "user_id": "u1",
        "run_id": "run-abc",
        "execution_context": {"workspace": {"mode": "worktree"}},
    });
    let st = stage_recover_history(
        initial,
        &store,
        "msg",
        &pipeline,
        "default",
        "",
        true,
        Some(&overlay),
        &Default::default(),
        "",
    )
    .await
    .expect("stage_recover_history 应成功（本测试注入无故障）");
    // overlay 顶层扁平键并入（与 track.total_tokens 同款约定）
    assert_eq!(st["task.goal"], "喝水提醒");
    assert_eq!(st["task.status"], "pending");
    assert_eq!(st["task.id"], "31bfdee19720");
    assert_eq!(st["lineage.parent_pipeline_id"], "pipe_parent");
    assert_eq!(st["lineage.origin_session_id"], "sess_root");
    assert_eq!(st["lineage.root"], true);
    // 引擎系统字段基线完好
    assert_eq!(st["message"], "msg");
    assert_eq!(st["pipeline_id"], pipeline);
    assert_eq!(st["session_id"], "thread_new");
    assert_eq!(st["user_id"], "u1");
    assert_eq!(st["run_id"], "run-abc", "run_id 注入为轮询定位锚（批次 C）");
    // 保留字 execution_context 不被 overlay 侵蚀（apply_state_overlay 纵深防御）
    assert_eq!(st["execution_context"]["workspace"]["mode"], "worktree");
}

#[test]
fn test_apply_state_overlay_skips_engine_system_fields() {
    // 纵深防御：即使 overlay 携带保留字（handler 层已拦截，此处防内部旁路
    // 调用者），合并点也跳过引擎系统字段
    let mut st = json!({
        "message": "real",
        "pipeline_id": "pipe_real",
        "user_id": "u_real",
        "messages": [{"role": "user", "content": "real"}],
    });
    apply_state_overlay(
        &mut st,
        &json!({
            "message": "evil",
            "pipeline_id": "evil",
            "user_id": "evil",
            "messages": [],
            "execution_context": {"evil": true},
            "task.goal": "ok"
        }),
    );
    assert_eq!(st["message"], "real");
    assert_eq!(st["pipeline_id"], "pipe_real");
    assert_eq!(st["user_id"], "u_real");
    assert_eq!(st["messages"].as_array().unwrap().len(), 1);
    assert!(st.get("execution_context").is_none());
    assert_eq!(st["task.goal"], "ok", "非保留字自由键应并入");
}

#[test]
fn test_apply_state_overlay_lineage_keys_not_overwritten_once_present() {
    // lineage 出生写入后为引擎保护字段：后续 overlay 同名键跳过（引擎值保留）
    let mut st = json!({});
    apply_state_overlay(
        &mut st,
        &json!({
            "lineage.root": true,
            "lineage.origin.kind": "channel",
            "task.status": "pending"
        }),
    );
    apply_state_overlay(
        &mut st,
        &json!({"lineage.root": false, "task.status": "running"}),
    );
    assert_eq!(st["lineage.root"], true, "lineage 已存在 → 引擎值保留");
    assert_eq!(st["lineage.origin.kind"], "channel");
    assert_eq!(st["task.status"], "running", "非保护键后续可更新");
}

#[tokio::test]
async fn test_process_via_engine_state_overlay_reaches_plugin_context() {
    // 真实引擎路径（非 mock 合并点）：overlay 键进入插件可见 state——
    // task.* 消费契约（task_evaluate / child_task_guard 等读 state 直读）
    let (state, invoker, _store, _sqlite, _user_space_guard) = make_engine_state();
    let tenant = TenantContext::new("tenant_overlay", "thread_overlay");
    let overlay = json!({
        "task.goal": "写周报",
        "task.status": "pending",
        "lineage.parent_pipeline_id": "pipe_parent",
        "lineage.origin_session_id": "thread_human"
    });
    let r = agentos_tenant::scope(
        tenant,
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: ("thread_overlay").to_string(),
                user_id: ("").to_string(),
                pipeline_id: ("pipe_overlay").to_string(),
                content: ("开始执行任务").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: Some(overlay.clone()),
                pipeline_config_id: None,
            },
            "agentos",
            "o1",
        ),
    )
    .await;
    assert!(!r.content.is_empty());
    let states = invoker.seen_states.lock().unwrap();
    assert!(!states.is_empty(), "引擎应至少调用一次 LLM 插件");
    assert_eq!(states[0]["task.goal"], "写周报");
    assert_eq!(states[0]["task.status"], "pending");
    assert_eq!(states[0]["lineage.parent_pipeline_id"], "pipe_parent");
    assert_eq!(states[0]["lineage.origin_session_id"], "thread_human");
}

// ── GAP-2：run 终态域事件（EVENT 触发器的输入源） ─────────────────────
// 契约（ADR 2026-08-28 事件下沉后）：内核只派生运行域 run.* 事件；任务域
// task_completed/task_failed 由 task_service 插件订阅 run.* 后按任务域语义
// 派生（插件侧测试覆盖），内核对 task.*/task.status 词汇零知识。

mod run_terminal_events;

#[tokio::test]
async fn test_process_via_engine_emits_run_terminal_domain_events() {
    // wiring：真实引擎跑一轮 → 声明 domain_event hook 的启用插件收到
    // run.completed（任务域事件派生已下沉 task_service 插件，内核只发 run.*）
    let (state, invoker, _store, _sqlite, _user_space_guard) = make_engine_state();
    // 订阅方插件：manifest 声明 DomainEvent hook 且启用
    {
        let mut manifests = state.manifests.write().await;
        manifests.push(agentos_core::traits::PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
            id: "trigger_sub".to_string(),
            name: "trigger_sub".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: agentos_core::traits::PluginType::System,
            language: "python".to_string(),
            host_type: agentos_core::traits::HostType::Sidecar,
            host_group: None,
            entry: String::new(),
            capabilities: agentos_core::traits::ManifestCapabilities {
                lifecycle_hooks: vec![agentos_core::traits::LifecycleHook::DomainEvent],
                ..Default::default()
            },
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            restricted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            ui_schema: None,
            persistent_fields: vec![],
            export_fields: vec![],
            aux_venvs: Vec::new(),
            http_endpoints: vec![],
            contributes: Default::default(),
            enabled: Some(true),
            activation: Default::default(),
            provides: Default::default(),
            visibility: Default::default(),
        });
    }
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("trigger_sub".to_string());

    let tenant = TenantContext::new("tenant_gap2_emit", "thread_gap2_emit");
    // lineage.*/task.* 经 state overlay 透传（血缘方案归调用方插件自持），
    // 内核对任务域键零解释——不因 state 带 task.* 而多发任务域事件。
    let overlay = json!({
        "task.id": "t77", "task.goal": "写周报", "task.status": "completed",
        "lineage.parent_pipeline_id": "pipe_parent_gap2",
    });
    let r = agentos_tenant::scope(
        tenant,
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: ("thread_gap2_emit").to_string(),
                user_id: ("").to_string(),
                pipeline_id: ("pipe_gap2_emit").to_string(),
                content: ("执行任务").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: Some(overlay.clone()),
                pipeline_config_id: None,
            },
            "agentos",
            "o1",
        ),
    )
    .await;
    assert!(!r.content.is_empty());

    // 广播 spawn 是 fire-and-forget：轮询等待钩子抵达（上限 5s）
    let mut hooks = Vec::new();
    for _ in 0..50 {
        hooks = invoker.hooks.lock().unwrap().clone();
        if hooks
            .iter()
            .any(|(_, _, ctx)| ctx["event"] == json!("run.completed"))
        {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
    let events: Vec<String> = hooks
        .iter()
        .map(|(pid, _, ctx)| {
            assert_eq!(pid, "trigger_sub");
            ctx["event"].as_str().unwrap_or("").to_string()
        })
        .collect();
    assert!(
        events.contains(&"run.completed".to_string()),
        "应广播 run.completed，实际 {events:?}"
    );
    assert!(
        !events.iter().any(|e| e.starts_with("task_")),
        "内核不得派生任务域事件（归 task_service 插件），实际 {events:?}"
    );
}

// ── GAP-3 后半：resume 幂等（重启后 user 消息不重复消费） ────────────

/// 引擎防御网（B2）：run_compiled 返回 Err 时把 run 置 failed + ended_at
/// （避免永远卡 running），并以 failed outcome 出口。
/// 触发路径：state.next_phase 指向不存在的循环体（引擎转移决策 Err）。
#[tokio::test]
async fn test_engine_run_failure_marks_run_failed() {
    let (state, _invoker, _store, sqlite, _user_space_guard) = make_engine_state();
    let tenant = TenantContext::new("tenant_gap2_err", "thread_gap2_err");
    let overlay = json!({"next_phase": "ghost_body"});
    let r = agentos_tenant::scope(
        tenant,
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: ("thread_fail_evt").to_string(),
                user_id: ("").to_string(),
                pipeline_id: ("pipe_fail_evt").to_string(),
                content: ("触发失败").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: Some(overlay.clone()),
                pipeline_config_id: None,
            },
            "agentos",
            "o1",
        ),
    )
    .await;
    assert!(r.failed, "引擎失败必须以 failed outcome 出口");
    assert!(
        r.content.contains("[engine-run-failed]"),
        "outcome 内容应携带失败标识: {}",
        r.content
    );
    // 防御网落库：该管道最新 run 置 failed（不悬空 running）——runs 表退役后
    // 运行状态真值 = pipeline_state 运行键（ADR 2026-09-18）
    let status: String = sqlite
        .with_conn(|c| {
            c.query_row(
                "SELECT value FROM pipeline_state \
                     WHERE field_key = 'run_status' AND pipeline_id = 'pipe_fail_evt' \
                     AND tenant_id = 'tenant_gap2_err'",
                [],
                |row| row.get(0),
            )
        })
        .unwrap();
    assert_eq!(status, "failed", "run 应标记 failed（B2 防御网）");
}

/// 用户停止收尾：state 带 router.stop_reason=user_requested（llm_core 中断路径
/// 写入）→ persist_run_end 落 run=cancelled，不再覆写为 Completed。
#[tokio::test]
async fn test_engine_user_stop_marks_run_cancelled() {
    let (state, _invoker, _store, sqlite, _user_space_guard) = make_engine_state();
    let tenant = TenantContext::new("tenant_cancel", "thread_cancel");
    let overlay = json!({"router.stop_reason": "user_requested"});
    let r = agentos_tenant::scope(
        tenant,
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: ("thread_cancel").to_string(),
                user_id: ("").to_string(),
                pipeline_id: ("pipe_cancel_evt").to_string(),
                content: ("正常输入但被用户停止").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: Some(overlay.clone()),
                pipeline_config_id: None,
            },
            "agentos",
            "o1",
        ),
    )
    .await;
    assert!(!r.failed, "用户停止不是引擎失败");
    // runs 表退役：运行状态真值 = pipeline_state 运行键（ADR 2026-09-18）
    let status: String = sqlite
        .with_conn(|c| {
            c.query_row(
                "SELECT value FROM pipeline_state \
                     WHERE field_key = 'run_status' AND pipeline_id = 'pipe_cancel_evt' \
                     AND tenant_id = 'tenant_cancel'",
                [],
                |row| row.get(0),
            )
        })
        .unwrap();
    assert_eq!(status, "cancelled", "用户停止的 run 落 cancelled");
}

/// 中断签名：user 消息已落槽（上一次尝试被重启截断）且无 assistant 跟随
/// → 冷启动重放同一消息时**不得再次落槽**（修复前无条件 append 导致
/// 重复 run / 同消息双份 / 陈旧回复——e2e GAP-3 现象②）。
#[tokio::test]
async fn test_replay_after_interrupt_does_not_duplicate_user_message() {
    let (state, invoker, store, _sqlite, _user_space_guard) = make_engine_state();
    let tenant = TenantContext::new("tenant_gap3", "thread_gap3");

    // 模拟中断：user 消息已持久化（slot 落库）但 run 未产出 assistant
    let _ = store
        .apply_messages_ops_to_table(
            "pipe_gap3",
            "tenant_gap3",
            &[json!({"op":"set","seq":1,"msg":{"role":"user","content":"重启前的那条消息"}})],
        )
        .await;

    // 冷启动重放同一消息（registry 无条目 → 冷路径）
    let r = agentos_tenant::scope(
        tenant,
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: ("thread_gap3").to_string(),
                user_id: ("").to_string(),
                pipeline_id: ("pipe_gap3").to_string(),
                content: ("重启前的那条消息").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: None,
                pipeline_config_id: None,
            },
            "agentos",
            "o1",
        ),
    )
    .await;
    assert!(!r.content.is_empty());

    // 幂等断言：该内容的 user 消息在 message_slots 里恰好 1 条
    let msgs = store
        .load_message_history("pipe_gap3", "tenant_gap3")
        .await
        .unwrap();
    let dup = msgs
        .iter()
        .filter(|m| {
            m.get("role") == Some(&json!("user"))
                && m.get("content") == Some(&json!("重启前的那条消息"))
        })
        .count();
    assert_eq!(dup, 1, "中断重放不得重复落槽：{msgs:?}");
    // 引擎基于既有历史正常跑完（assistant 已产出）
    assert!(
        msgs.iter()
            .any(|m| m.get("role") == Some(&json!("assistant"))),
        "重放应继续执行产出回复：{msgs:?}"
    );
    let _ = invoker; // 引擎确实调用了 LLM 插件（seen_states 非空即跑过）
    assert!(!invoker.seen_states.lock().unwrap().is_empty());
}

/// 轮首 user 消息槽位必须带 run_id（append op 的 _run_id 落表）：regenerate
/// 审计锚预检读 message_slots.run_id，NULL 即"目标 user 消息无有效 run_id"
/// 整轮拒绝——轮首消息（触发新 run 的那条）曾因此全部不可编辑重发/回退。
#[tokio::test]
async fn test_user_message_slot_carries_run_id() {
    let (state, _invoker, _store, sqlite, _user_space_guard) = make_engine_state();
    let tenant = TenantContext::new("tenant_runid", "thread_runid");
    let r = agentos_tenant::scope(
        tenant,
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: ("thread_runid").to_string(),
                user_id: ("").to_string(),
                pipeline_id: ("pipe_runid").to_string(),
                content: ("审计锚测试消息").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: None,
                pipeline_config_id: None,
            },
            "agentos",
            "o1",
        ),
    )
    .await;
    assert!(!r.failed, "首轮发送不应失败: {}", r.content);

    // message_slots 里该 user 消息（seq 0）的 run_id 非空且与 state 运行键对齐
    let slot_run: String = sqlite
        .with_conn(|c| {
            c.query_row(
                "SELECT COALESCE(s.run_id, '') FROM message_slots s \
                 WHERE s.pipeline_id = 'pipe_runid' AND s.seq = 0 LIMIT 1",
                [],
                |row| row.get(0),
            )
        })
        .unwrap();
    let run_row: String = sqlite
        .with_conn(|c| {
            c.query_row(
                "SELECT value FROM pipeline_state \
                 WHERE field_key = 'run_id' AND pipeline_id = 'pipe_runid' \
                 AND tenant_id = 'tenant_runid' LIMIT 1",
                [],
                |row| row.get(0),
            )
        })
        .unwrap();
    assert!(
        !slot_run.is_empty(),
        "轮首 user 消息槽位 run_id 不得为空（审计锚）"
    );
    assert_eq!(slot_run, run_row, "槽位 run_id 必须与本轮 run 对齐");
}

/// 正常连续两轮同文消息不受幂等影响：第一轮已消费（assistant 跟随），
/// 第二轮同文 user 是新输入 → 应正常 append（2 条 user）。
#[tokio::test]
async fn test_repeated_user_message_after_reply_still_appends() {
    let (state, _invoker, store, _sqlite, _user_space_guard) = make_engine_state();
    let tenant = TenantContext::new("tenant_gap3b", "thread_gap3b");
    for _ in 0..2 {
        let _ = agentos_tenant::scope(
            tenant.clone(),
            process_via_engine(
                &state,
                &agentos_core::types::DispatchRequest {
                    thread_id: ("thread_gap3b").to_string(),
                    user_id: ("").to_string(),
                    pipeline_id: ("pipe_gap3b").to_string(),
                    content: ("再来一次").to_string(),
                    client_message_id: ("").to_string(),
                    source: agentos_core::types::PendingInputSource::System,
                    execution_context: None,
                    state_overlay: None,
                    pipeline_config_id: None,
                },
                "agentos",
                "o1",
            ),
        )
        .await;
    }
    let msgs = store
        .load_message_history("pipe_gap3b", "tenant_gap3b")
        .await
        .unwrap();
    let n = msgs
        .iter()
        .filter(|m| {
            m.get("role") == Some(&json!("user")) && m.get("content") == Some(&json!("再来一次"))
        })
        .count();
    assert_eq!(n, 2, "已消费后同文再发是合法新输入（应 2 条）：{msgs:?}");
}

/// 重启压力近似（GAP-3 验证标准的单进程版）：3 个会话（不同管道）并发
/// 各跑一条消息 + 其中一个管道带中断重放 → 终态各管道 user 计数精确、
/// 序列严格递增、无 NULL blob。同管道并发在生产入口必经 RunChain FIFO
/// 串行（ws_session/HTTP handler），此处按会话维度并发与生产同构。
#[tokio::test]
async fn test_concurrent_chats_with_interrupted_replay_consistent() {
    let (state, _invoker, store, _sqlite, _user_space_guard) = make_engine_state();
    // 管道 A 预置中断消息（user 已落槽、run 未产出 assistant——重启截断签名）
    let _ = store
        .apply_messages_ops_to_table(
            "pipe_gap3c_a",
            "tenant_gap3c",
            &[json!({"op":"set","seq":1,"msg":{"role":"user","content":"被中断的并发消息"}})],
        )
        .await;

    let mk = |pipeline: &'static str, msg: &'static str| {
        let st = state.clone();
        async move {
            agentos_tenant::scope(
                TenantContext::new("tenant_gap3c", "thread_gap3c"),
                process_via_engine(
                    &st,
                    &agentos_core::types::DispatchRequest {
                        thread_id: ("thread_gap3c").to_string(),
                        user_id: ("").to_string(),
                        pipeline_id: (pipeline).to_string(),
                        content: (msg).to_string(),
                        client_message_id: ("").to_string(),
                        source: agentos_core::types::PendingInputSource::System,
                        execution_context: None,
                        state_overlay: None,
                        pipeline_config_id: None,
                    },
                    "agentos",
                    "o1",
                ),
            )
            .await
        }
    };
    let (ra, rb, rc, rr) = tokio::join!(
        mk("pipe_gap3c_a", "被中断的并发消息"), // 中断重放（同文尾部）
        mk("pipe_gap3c_b", "会话B的消息"),
        mk("pipe_gap3c_c", "会话C的消息"),
        mk("pipe_gap3c_d", "会话D的消息"),
    );
    for r in [&ra, &rb, &rc, &rr] {
        assert!(!r.content.is_empty());
    }

    for (pid, expect_user_contents) in [
        ("pipe_gap3c_a", vec!["被中断的并发消息"]),
        ("pipe_gap3c_b", vec!["会话B的消息"]),
        ("pipe_gap3c_c", vec!["会话C的消息"]),
        ("pipe_gap3c_d", vec!["会话D的消息"]),
    ] {
        let msgs = store
            .load_message_history(pid, "tenant_gap3c")
            .await
            .unwrap();
        let seqs: Vec<i64> = msgs
            .iter()
            .filter_map(|m| m.get("seq").and_then(|s| s.as_i64()))
            .collect();
        let uniq: std::collections::BTreeSet<i64> = seqs.iter().copied().collect();
        assert_eq!(seqs.len(), uniq.len(), "{pid} 序列应严格唯一：{msgs:?}");
        for content in &expect_user_contents {
            let n = msgs
                .iter()
                .filter(|m| {
                    m.get("role") == Some(&json!("user"))
                        && m.get("content") == Some(&json!(content))
                })
                .count();
            assert_eq!(n, 1, "{pid}「{content}」应恰好 1 条：{msgs:?}");
        }
        for m in &msgs {
            assert!(
                m.get("role").is_some() && m.get("content").is_some(),
                "{pid} 消息应可从 blob 完整重建：{m:?}"
            );
        }
    }
}

// ── 职责边界：run 终态不写任务状态 ────────────────────────────────
// 内核只管管道运行域：run 结束只广播域事件（run.completed/task_completed），
// task.status/task.ended_at 由任务域插件（task_evaluate 经 pipeline-state
// update）裁决写入。此处断言：引擎跑完任务管道后 state 保持出生值 pending，
// 不出现内核补写的 completed。

#[tokio::test]
#[allow(clippy::await_holding_lock)]
async fn test_run_terminal_does_not_write_task_status() {
    // overlay 带 task.* 字段的管道跑完 → registry 常驻 state 与
    // pipeline_state 表都不得出现内核回写的 task.status=completed——
    // 任务终态裁决在任务域插件，内核只广播 run 终态域事件。
    let (state, _invoker, store, _sqlite, _user_space_guard) = make_engine_state();
    let tenant = TenantContext::new("tenant_unify", "thread_unify");
    let overlay = json!({"task.id": "t_unify", "task.goal": "统一验证", "task.status": "pending"});
    let r = agentos_tenant::scope(
        tenant,
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: ("thread_unify").to_string(),
                user_id: ("").to_string(),
                pipeline_id: ("pipe_unify").to_string(),
                content: ("执行任务").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: Some(overlay.clone()),
                pipeline_config_id: None,
            },
            "agentos",
            "o1",
        ),
    )
    .await;
    assert!(!r.content.is_empty());

    // registry 热数据：run 终态即注销（刀3：防 final_state 全历史跨轮驻留）——
    // "内核不写 task.status" 不变量以更强形式成立：终态后无热条目可写；
    // 下一轮经收尾 checkpoint 冷路径重建
    let reg = agentos_session::global_registry();
    assert!(
        reg.get("tenant_unify", "pipe_unify").is_none(),
        "终态后热条目应注销（冷路径由收尾 checkpoint 重建）"
    );

    // 冷路径表：引擎不投影 task.* 键（出生落库在 chat_send_handler 创建
    // 分支），此处无内核回写行
    let fields = store
        .load_pipeline_state("pipe_unify", "tenant_unify")
        .await
        .unwrap();
    assert!(
        !fields.contains_key("task.status"),
        "引擎 run 不得写 pipeline_state 表的 task.status"
    );
    assert!(!fields.contains_key("task.ended_at"));

    // 普通会话管道（无 task.*）不受影响——不写任务字段
    let _ = agentos_tenant::scope(
        TenantContext::new("tenant_unify", "thread_unify"),
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: ("thread_unify").to_string(),
                user_id: ("").to_string(),
                pipeline_id: ("pipe_plain").to_string(),
                content: ("普通消息").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: None,
                pipeline_config_id: None,
            },
            "agentos",
            "o1",
        ),
    )
    .await;
    let fields2 = store
        .load_pipeline_state("pipe_plain", "tenant_unify")
        .await
        .unwrap();
    assert!(
        !fields2.contains_key("task.status"),
        "非任务管道不写任务字段"
    );
}

#[tokio::test]
#[allow(clippy::await_holding_lock)]
async fn test_run_terminal_skips_writeback_for_owned_only_pipeline() {
    // 幽灵任务行根因回归：仅登记过子任务的聊天主管道，state
    // 只含 `task.owned.*` 扁平键（无自身 task.* 声明）——不得被误判为任务
    // 管道，run 结束不得回写 task.status/task.ended_at（否则任务聚合出口
    // 出现无标题无 task.id 的幽灵任务行）。判定口径与插件侧聚合
    // `_list_tasks_from_state` 第一趟一致：含 `task.` 且不含 `task.owned.`。
    let (state, _invoker, store, _sqlite, _user_space_guard) = make_engine_state();
    let tenant = TenantContext::new("tenant_owned_only", "thread_owned_only");
    let overlay = json!({
        "task.owned.child_pipe_1.title": "AI行业近月发展调研",
        "task.owned.child_pipe_1.status": "running",
        "task.owned.child_pipe_1.scope": "non_container",
    });
    let r = agentos_tenant::scope(
        tenant,
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: ("thread_owned_only").to_string(),
                user_id: ("").to_string(),
                pipeline_id: ("pipe_owned_only").to_string(),
                content: ("帮我开个子任务").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: Some(overlay.clone()),
                pipeline_config_id: None,
            },
            "agentos",
            "o1",
        ),
    )
    .await;
    assert!(!r.content.is_empty());

    // registry 热路径：终态即注销（刀3）——"不得出现 task.status/task.ended_at"
    // 以更强形式成立：无热条目即无回写面
    let reg = agentos_session::global_registry();
    assert!(
        reg.get("tenant_owned_only", "pipe_owned_only").is_none(),
        "终态后热条目应注销"
    );

    // 冷路径表：同样不得落任务终态键
    let fields = store
        .load_pipeline_state("pipe_owned_only", "tenant_owned_only")
        .await
        .unwrap();
    assert!(
        !fields.contains_key("task.status"),
        "owned-only 管道不得落库 task.status，实际 {fields:?}"
    );
    assert!(
        !fields.contains_key("task.ended_at"),
        "owned-only 管道不得落库 task.ended_at，实际 {fields:?}"
    );
}

// ── GAP-1 全流程数据流转：提交 → 管道创建 → run → 终态回写 → 聚合可见 ──

#[tokio::test]
#[allow(clippy::await_holding_lock)]
async fn test_task_lifecycle_end_to_end_state_flow() {
    // 组合验证（各环节单测已绿，此处串全链）：
    // ① chat.send_message create 分支生成 pipeline_id（task.id 引擎注入）
    // ② 同一 overlay 派发 → run 完成
    // ③ 任务状态保持出生值 pending（职责边界：run 终态不写
    //    task.status，终态由任务域插件经 pipeline-state.update 裁决）
    // ④ pipeline-state.list 聚合行完整（task.* + lineage.* + status）
    let (state, _invoker, store, _sqlite, _user_space_guard) = make_engine_state();

    // ① 创建契约（chat handler 侧独立测试覆盖；此处手工构造同参，
    // 聚焦引擎侧流转）：
    let overlay = json!({
        "task.goal": "全流程验证",
        "task.status": "pending",
        "task.scope": "non_container",
        "lineage.root": true,
        "lineage.origin.kind": "plugin",
        "lineage.origin.source": "task_submit",
    });
    let pipeline_id = "pipe_lifecycle_1";
    // 引擎注入 task.id（与 chat_send_handler create 分支同语义）
    let mut overlay = overlay;
    if let Some(obj) = overlay.as_object_mut() {
        obj.insert("task.id".to_string(), json!(pipeline_id));
    }

    // ② 派发 → run 完成
    let tenant = TenantContext::new("tenant_lifecycle", "thread_lifecycle");
    let r = agentos_tenant::scope(
        tenant,
        process_via_engine(
            &state,
            &agentos_core::types::DispatchRequest {
                thread_id: ("thread_lifecycle").to_string(),
                user_id: ("").to_string(),
                pipeline_id: (pipeline_id).to_string(),
                content: ("执行全流程验证任务").to_string(),
                client_message_id: ("").to_string(),
                source: agentos_core::types::PendingInputSource::System,
                execution_context: None,
                state_overlay: Some(overlay.clone()),
                pipeline_config_id: None,
            },
            "agentos",
            "o1",
        ),
    )
    .await;
    assert!(!r.content.is_empty());

    // ③ registry 热路径：终态即注销（刀3）——"run 终态不写任务状态" 以更强
    // 形式成立：无热条目即无回写面；出生字段（goal/scope/lineage）由冷路径
    // 收尾 checkpoint 承载，见下方 ④ 的 DB 断言
    let reg = agentos_session::global_registry();
    assert!(
        reg.get("tenant_lifecycle", pipeline_id).is_none(),
        "终态后热条目应注销"
    );

    // ④ 聚合出口（pipeline-state.list 同源）行完整
    let fields = store
        .load_pipeline_state(pipeline_id, "tenant_lifecycle")
        .await
        .unwrap();
    assert!(
        !fields.contains_key("task.status"),
        "引擎 run 不得写 pipeline_state 表的 task.status"
    );
}

/// 触发器注入回归：chat.send_message 注入只持有管道唯一坐标
/// （12hex pipeline_id），事件按该坐标 emit 后必须能经 registry 反查直达
/// 在线 user 的 WS 连接——否则出现「LLM 日志有、前端收不到回复」。
#[tokio::test]
async fn inject_dispatch_events_reach_user_connection_via_pipeline_coordinate() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    #[derive(Clone)]
    struct RecSink {
        delivered: Arc<AtomicUsize>,
    }
    #[async_trait::async_trait]
    impl agentos_session::EventSink for RecSink {
        async fn send_text(&self, _t: &str) -> bool {
            self.delivered.fetch_add(1, Ordering::SeqCst);
            true
        }
        fn id(&self) -> u64 {
            42
        }
    }

    let (mut state, _invoker, store, _sqlite, _user_space_guard) = make_engine_state();
    // 租户解析按真值走（K8 修订 ADR 2026-10-10）：派发用户先在库。
    store
        .create_user(&agentos_core::types::UserRecord {
            user_id: "u1".to_string(),
            username: "u-1".to_string(),
            password: String::new(),
            email: None,
            role: "user".to_string(),
            tenant_id: "default".to_string(),
            created_at: "2026-10-10T00:00:00Z".to_string(),
            last_login_at: None,
            must_change_password: false,
        })
        .await
        .unwrap();

    let coord = Arc::new(agentos_session::SessionCoordinator::new());
    state = state.enable_session_with(coord.clone());
    let sink = Arc::new(RecSink {
        delivered: Default::default(),
    });
    coord.register("u1", sink.clone());
    // 前端已按会话 thread 注册；注入路径的派发键 = 管道唯一坐标，未注册
    coord.register_thread("thread-1", "u1");
    // 生产形状（chat.send_message）：管道出生即登记归属会话，派发带真实
    // thread + pipeline 各归其位（会话 id 不充当管道坐标）。
    store
        .link_pipeline_session("pid-12hex-inject", "thread-1", "default")
        .await
        .unwrap();

    let dispatcher = crate::ws_session::EngineDispatcher::new(state);
    // stream_start 在链任务激活时才发（ADR-2026-08-26 等待窗口不占位），轮询等待。
    use agentos_session::router::PipelineDispatcher;
    let _ = dispatcher
        .dispatch_user_input(agentos_core::types::DispatchRequest {
            thread_id: ("thread-1").to_string(),
            user_id: ("u1").to_string(),
            pipeline_id: ("pid-12hex-inject").to_string(),
            content: ("嗨").to_string(),
            client_message_id: ("").to_string(),
            source: PendingInputSource::Trigger,
            execution_context: None,
            state_overlay: None,
            pipeline_config_id: None,
        })
        .await;
    let delivered = sink.delivered.clone();
    tokio::time::timeout(std::time::Duration::from_secs(5), async {
        loop {
            if delivered.load(Ordering::SeqCst) >= 1 {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("5s 内应收到注入派发事件——LLM 日志有、前端收不到 = 该坐标缺注册");
}

// ── pending 输入队列（ADR-2026-08-26）：入队/排队/删除/消费 ──

/// dispatch_user_input 在链空闲时消费：消息落表后立即激活执行，
/// 消费即删行（队列清空），等待窗口不存在（空闲管道行为与旧 dispatch 一致）。
#[tokio::test]
async fn test_pending_input_idle_consumed_immediately() {
    let (state, _invoker, store, _sqlite, _user_space_guard) = make_engine_state();
    // 租户解析按真值走（K8 修订 ADR 2026-10-10）：派发用户先在库。
    store
        .create_user(&agentos_core::types::UserRecord {
            user_id: "u1".to_string(),
            username: "u-1".to_string(),
            password: String::new(),
            email: None,
            role: "user".to_string(),
            tenant_id: "default".to_string(),
            created_at: "2026-10-10T00:00:00Z".to_string(),
            last_login_at: None,
            must_change_password: false,
        })
        .await
        .unwrap();

    // 管道出生即登记归属会话（chat.send_message 创建分支同款），派发坐标校验依赖它
    store
        .link_pipeline_session("pipe-idle-1", "thread-idle-1", "default")
        .await
        .unwrap();
    let dispatcher = crate::ws_session::EngineDispatcher::new(state);
    use agentos_session::router::PipelineDispatcher;
    dispatcher
        .dispatch_user_input(agentos_core::types::DispatchRequest {
            thread_id: ("thread-idle-1").to_string(),
            user_id: ("u1").to_string(),
            pipeline_id: ("pipe-idle-1").to_string(),
            content: ("空闲直发").to_string(),
            client_message_id: ("cmid-1").to_string(),
            source: PendingInputSource::User,
            execution_context: None,
            state_overlay: None,
            pipeline_config_id: None,
        })
        .await
        .unwrap();
    // 消费任务在链空闲时 pop → 立即执行；轮询等待队列清空（消费瞬态 = 删行）。
    let store2 = store.clone();
    tokio::time::timeout(std::time::Duration::from_secs(5), async {
        loop {
            let rows = store2
                .list_pending_inputs("default", "pipe-idle-1")
                .await
                .unwrap();
            if rows.is_empty() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("空闲管道消息应被消费（队列清空）");
}

/// 等待窗口内删除：删除语义由存储层单测覆盖（store.rs）；此处验证 dispatch
/// 入队后不产生队列残留——空闲管道消息被消费即删行，删除语义（PUT/DELETE
/// 端点）在 T2 联测覆盖。
#[tokio::test]
async fn test_pending_input_dispatch_leaves_no_residue() {
    let (state, _invoker, store, _sqlite, _user_space_guard) = make_engine_state();
    // 租户解析按真值走（K8 修订 ADR 2026-10-10）：派发用户先在库。
    store
        .create_user(&agentos_core::types::UserRecord {
            user_id: "u1".to_string(),
            username: "u-1".to_string(),
            password: String::new(),
            email: None,
            role: "user".to_string(),
            tenant_id: "default".to_string(),
            created_at: "2026-10-10T00:00:00Z".to_string(),
            last_login_at: None,
            must_change_password: false,
        })
        .await
        .unwrap();

    // 管道出生即登记归属会话（chat.send_message 创建分支同款），派发坐标校验依赖它
    store
        .link_pipeline_session("pipe-del-1", "thread-del-1", "default")
        .await
        .unwrap();
    let dispatcher = crate::ws_session::EngineDispatcher::new(state);
    use agentos_session::router::PipelineDispatcher;
    dispatcher
        .dispatch_user_input(agentos_core::types::DispatchRequest {
            thread_id: ("thread-del-1").to_string(),
            user_id: ("u1").to_string(),
            pipeline_id: ("pipe-del-1").to_string(),
            content: ("将被消费").to_string(),
            client_message_id: ("").to_string(),
            source: PendingInputSource::User,
            execution_context: None,
            state_overlay: None,
            pipeline_config_id: None,
        })
        .await
        .unwrap();
    // 空闲管道消息应被消费（异步链任务），轮询等待队列清空（消费瞬态 = 删行）。
    let store2 = store.clone();
    tokio::time::timeout(std::time::Duration::from_secs(5), async {
        loop {
            let rows = store2
                .list_pending_inputs("default", "pipe-del-1")
                .await
                .unwrap();
            if rows.is_empty() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("dispatch 后队列应被消费清空，不留残留");
}

// ── 消息幂等契约（ADR 2026-08-21）：cmid 随 user 消息落库 + interrupted_tail 尊重 cmid ──

/// cmid 非空时 user 消息必须携带 metadata.client_message_id（对账去重桥接键）。
#[tokio::test]
async fn test_stage_recover_history_stamps_cmid_metadata() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite;
    let st = json!({"messages": []});
    let out = stage_recover_history(
        st,
        &store,
        "带键消息",
        "pipe_cmid1",
        "tenant_cmid1",
        "0198-cmid-a",
        false,
        None,
        &Default::default(),
        "",
    )
    .await
    .expect("stage_recover_history 应成功（本测试注入无故障）");
    let msgs = out["messages"].as_array().expect("messages 数组");
    let user = msgs
        .iter()
        .find(|m| m["role"] == "user" && m["content"] == "带键消息")
        .expect("user 消息应 append");
    assert_eq!(
        user["metadata"]["client_message_id"], "0198-cmid-a",
        "cmid 非空时必须随 metadata 落库"
    );
    // 无 cmid 路径（触发器注入/旧客户端）不造空 metadata
    let st2 = json!({"messages": []});
    let out2 = stage_recover_history(
        st2,
        &store,
        "无键消息",
        "pipe_cmid1",
        "tenant_cmid1",
        "",
        false,
        None,
        &Default::default(),
        "",
    )
    .await
    .expect("stage_recover_history 应成功（本测试注入无故障）");
    let user2 = out2["messages"]
        .as_array()
        .expect("messages 数组")
        .iter()
        .find(|m| m["role"] == "user")
        .expect("user 消息应 append");
    assert!(user2.get("metadata").is_none(), "无 cmid 不造空 metadata");
}

/// interrupted_tail 幂等判定按 cmid 裁决：同 cmid 重派吞；不同 cmid 绝不吞
/// （修复连发两条相同内容第二条被吞）；无 cmid 路径维持同文判定（GAP-3 兼容）。
/// 尾部消息须播进 store（stage_recover_history 冷路径以 message_slots 为真值重载）。
#[tokio::test]
async fn test_interrupted_tail_respects_client_message_id() {
    async fn run_case(tail_cmid: Option<&str>, incoming_cmid: &str) -> usize {
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let store: Arc<dyn StorageBackend> = sqlite;
        let msg = match tail_cmid {
            Some(c) => json!({"role": "user", "content": "ok",
                                  "metadata": {"client_message_id": c}}),
            None => json!({"role": "user", "content": "ok"}),
        };
        store
            .apply_messages_ops_to_table(
                "p_it",
                "tenant_it",
                &[json!({"op": "set", "seq": 7, "msg": msg})],
            )
            .await
            .unwrap();
        let st = json!({"pipeline_id": "p_it"});
        let out = stage_recover_history(
            st,
            &store,
            "ok",
            "p_it",
            "tenant_it",
            incoming_cmid,
            false,
            None,
            &Default::default(),
            "",
        )
        .await
        .expect("stage_recover_history 应成功（本测试注入无故障）");
        out["messages"].as_array().unwrap().len()
    }
    // ① 同 cmid 重派 → 吞（真·断线重试幂等）
    assert_eq!(
        run_case(Some("0198-same"), "0198-same").await,
        1,
        "同 cmid 重派应吞"
    );
    // ② 同文不同 cmid → 不吞（用户真发了两条）
    assert_eq!(
        run_case(Some("0198-first"), "0198-second").await,
        2,
        "同文不同 cmid 绝不吞（连发两条相同内容是真实用户行为）"
    );
    // ③ tail 无 cmid + 来稿带 cmid → 以键裁决，不吞
    assert_eq!(
        run_case(None, "0198-third").await,
        2,
        "来稿带 cmid 而尾部无键：不是同一次发送，不吞"
    );
    // ④ 双方都无 cmid → 维持 GAP-3 同文判定（旧路径兼容）
    assert_eq!(run_case(None, "").await, 1, "无键路径维持同文判定");
}

/// 批次 D：regenerate 显式 skip_user_append——目标 user 消息已在截断后历史中，
/// 重跑不重复 append（不借用 interrupted_tail 启发式）。
#[tokio::test]
async fn test_stage_recover_history_skip_user_append() {
    async fn run_case(
        tail: serde_json::Value,
        incoming: &str,
        incoming_cmid: &str,
        skip: bool,
    ) -> usize {
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let store: Arc<dyn StorageBackend> = sqlite;
        store
            .apply_messages_ops_to_table(
                "p_skip",
                "tenant_skip",
                &[json!({"op": "set", "seq": 0, "msg": tail})],
            )
            .await
            .unwrap();
        let st = json!({"pipeline_id": "p_skip"});
        let out = stage_recover_history(
            st,
            &store,
            incoming,
            "p_skip",
            "tenant_skip",
            incoming_cmid,
            skip,
            None,
            &Default::default(),
            "",
        )
        .await
        .expect("stage_recover_history 应成功（本测试注入无故障）");
        out["messages"].as_array().unwrap().len()
    }
    let tail_with_cmid = json!({"role": "user", "content": "ok",
                                    "metadata": {"client_message_id": "0198-skip"}});
    // skip=false 尾部同文同 cmid → interrupted_tail 吞（既有幂等语义不变）
    assert_eq!(
        run_case(tail_with_cmid.clone(), "ok", "0198-skip", false).await,
        1,
        "非重跑路径维持既有 interrupted_tail 判定"
    );
    // skip=false 尾部不同文 → 正常 append（多轮对话基线）
    assert_eq!(
        run_case(
            json!({"role": "user", "content": "旧问题"}),
            "新问题",
            "",
            false
        )
        .await,
        2,
        "非重跑路径正常 append 新 user"
    );
    // skip=true 尾部不同文 → 不 append（显式命令，启发式不适用）
    assert_eq!(
        run_case(
            json!({"role": "user", "content": "旧问题"}),
            "新问题",
            "",
            true
        )
        .await,
        1,
        "skip_user_append 下不重复 append（重跑消息已在截断后历史）"
    );
}

/// user 消息落库失败必须可观测（warn 留痕）且重试耗尽后上抛失败 outcome——
/// 静默降级会让对话历史在重启后无声缺条（message_slots 未落、实录缺席）。
/// 故障注入：DROP message_slots 表 → apply_messages_ops_to_table 报 Database 错。
/// B3 上抛语义契约：消息未受理（failed outcome 终止本轮发送，不进引擎），
/// 重试期间逐次 warn 留痕。
#[tokio::test]
async fn test_stage_recover_history_user_append_failure_is_observable() {
    #[derive(Clone)]
    struct SharedBuf(Arc<std::sync::Mutex<Vec<u8>>>);
    impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for SharedBuf {
        type Writer = SharedBuf;
        fn make_writer(&'a self) -> Self::Writer {
            self.clone()
        }
    }
    impl std::io::Write for SharedBuf {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(buf);
            Ok(buf.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    sqlite
        .with_conn::<(), String>(|conn| {
            conn.execute("DROP TABLE message_slots", [])
                .map(|_| ())
                .map_err(|e| e.to_string())
        })
        .unwrap();
    let store: Arc<dyn StorageBackend> = sqlite;

    let buf = SharedBuf(Arc::new(std::sync::Mutex::new(Vec::new())));
    let subscriber = tracing_subscriber::fmt()
        .with_ansi(false)
        .with_writer(buf.clone())
        .finish();
    let _guard = tracing::subscriber::set_default(subscriber);

    let out = stage_recover_history(
        json!({"pipeline_id": "pipe_failobs"}),
        &store,
        "落库失败消息",
        "pipe_failobs",
        "tenant_failobs",
        "",
        false,
        None,
        &Default::default(),
        "",
    )
    .await;

    let log = String::from_utf8(buf.0.lock().unwrap().clone()).unwrap();
    assert!(
        log.contains("user 消息落库失败"),
        "落库失败必须 warn 留痕，实际日志：{log}"
    );
    // B3 上抛语义：落库失败（重试耗尽）= 消息未受理，返回失败 outcome
    // 终止本轮发送（不进入引擎执行），不再降级继续。
    let outcome = match out {
        Ok(s) => panic!("落库失败重试耗尽必须上抛，实际成功: {s:?}"),
        Err(o) => o,
    };
    assert!(outcome.failed, "必须是失败 outcome: {outcome:?}");
    assert!(outcome.final_assistant.is_none());
    assert!(
        outcome.content.contains("未受理"),
        "错误文案必须表达「消息未受理」: {}",
        outcome.content
    );
}

// ── 热路径全量快照恢复：state 快照是管道合法数据全集，续跑不丢键 ──

/// 热路径（registry 命中）：上轮 final_state 的标量键（task.id / workspace /
/// execution_context…）必须随续跑恢复——缺恢复会让下游按身份缺席误判
/// （任务管道缺 task.id 被判成主会话，工作区漂移到会话共享目录）。
/// 同时锁跳过契约：per-run 键（message/suspended/ended/run_id）与下划线键
/// 保持本轮新值，快照残留不得顶掉新锚点。
#[tokio::test]
async fn test_hot_resume_restores_snapshot_scalars() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite;
    let snapshot = json!({
        "messages": [{"role": "user", "content": "上轮提问", "seq": 0}],
        "task.id": "task_hot1",
        "task.parent_project_id": "proj_hot1",
        "workspace": "D:/ws/thread__wt_hot1",
        "ws_meta": {"mode": "worktree", "path": "D:/ws/thread__wt_hot1"},
        "execution_context": {
            "workspace": {"source_path": "项目目录", "mode": "worktree"},
            "isolation": {"level": "isolated"},
        },
        // per-run 键残留：合并必须全部跳过
        "run_id": "old-run",
        "suspended": true,
        "ended": true,
        "message": "旧输入",
        "_skip_user_append": true,
    });
    agentos_session::global_registry().get_or_init(
        "tenant_hot1",
        "pipe_hot1",
        "thread_hot1",
        "agent_hot1",
        snapshot,
    );
    let out = stage_recover_history(
        json!({
            "message": "新输入",
            "input": "新输入",
            "run_id": "new-run",
            "suspended": false,
            "ended": false,
            "pipeline_id": "pipe_hot1",
            "session_id": "thread_hot1",
            "execution_context": {"workspace": {"source_path": "会话级目录"}},
        }),
        &store,
        "新输入",
        "pipe_hot1",
        "tenant_hot1",
        "",
        false,
        None,
        &Default::default(),
        "",
    )
    .await
    .expect("stage_recover_history 应成功（本测试注入无故障）");
    // 标量身份键全量恢复
    assert_eq!(out["task.id"], "task_hot1", "task.id 必须随快照恢复");
    assert_eq!(out["task.parent_project_id"], "proj_hot1");
    assert_eq!(out["workspace"], "D:/ws/thread__wt_hot1");
    assert_eq!(out["ws_meta"]["mode"], "worktree");
    // execution_context 本轮已注入（会话级种子形态，1a/1a2 产物同理）→ 快照
    // 残留不得整键顶掉本轮声明（消息级显式键 > 会话级默认 > 恢复兜底；
    // BUG-35：上轮残留曾把本轮消息级 mode 顶回会话默认）
    assert_eq!(
        out["execution_context"]["workspace"]["source_path"], "会话级目录",
        "本轮声明的 execution_context 优先于快照残留"
    );
    assert_eq!(
        out["execution_context"].get("isolation"),
        None,
        "快照残留的其余执行键同样不得混入本轮声明"
    );
    // per-run 键保持本轮新值（快照残留被跳过）
    assert_eq!(out["run_id"], "new-run", "旧 run_id 不得顶掉取消轮询新锚");
    assert_eq!(out["suspended"], false);
    assert_eq!(out["ended"], false);
    assert_eq!(out["message"], "新输入");
    assert!(out.get("_skip_user_append").is_none(), "下划线键不跨轮恢复");
    // messages 热路径复用 + 本轮新消息 append 在尾部
    let msgs = out["messages"].as_array().expect("messages 数组");
    assert_eq!(msgs[0]["content"], "上轮提问", "历史首条来自快照");
    assert_eq!(
        msgs.last().unwrap()["content"],
        "新输入",
        "本轮消息 append 在尾部"
    );
}

/// 热路径边界：快照只有 messages（无标量键）→ 合并零副作用，本轮种子值原样保留。
#[tokio::test]
async fn test_hot_resume_without_scalars_is_noop_merge() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite;
    let snapshot = json!({
        "messages": [{"role": "user", "content": "只有历史", "seq": 0}],
    });
    agentos_session::global_registry().get_or_init(
        "tenant_hot2",
        "pipe_hot2",
        "thread_hot2",
        "agent_hot2",
        snapshot,
    );
    let out = stage_recover_history(
        json!({"run_id": "new-run-2", "message": "第二轮"}),
        &store,
        "第二轮",
        "pipe_hot2",
        "tenant_hot2",
        "",
        false,
        None,
        &Default::default(),
        "",
    )
    .await
    .expect("stage_recover_history 应成功（本测试注入无故障）");
    assert_eq!(out["run_id"], "new-run-2");
    assert_eq!(out["message"], "第二轮");
    assert!(out.get("workspace").is_none(), "无标量快照不得凭空造键");
    assert_eq!(
        out["messages"].as_array().unwrap().len(),
        2,
        "历史复用 + 本轮 append"
    );
}

/// 热路径边界：本轮两级皆无（1a 无声明值且消息未携带 execution_context）→
/// 快照值照常恢复——续跑连续性兜底（无透传轮沿用上轮执行上下文）。
#[tokio::test]
async fn test_hot_resume_recovers_execution_context_when_round_has_none() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite;
    let snapshot = json!({
        "messages": [{"role": "user", "content": "上轮提问", "seq": 0}],
        "execution_context": {"isolation": {"level": "isolated"}},
    });
    agentos_session::global_registry().get_or_init(
        "tenant_hot3",
        "pipe_hot3",
        "thread_hot3",
        "agent_hot3",
        snapshot,
    );
    let out = stage_recover_history(
        json!({"run_id": "new-run-3", "message": "第三轮"}),
        &store,
        "第三轮",
        "pipe_hot3",
        "tenant_hot3",
        "",
        false,
        None,
        &Default::default(),
        "",
    )
    .await
    .expect("stage_recover_history 应成功（本测试注入无故障）");
    assert_eq!(
        out["execution_context"]["isolation"]["level"], "isolated",
        "本轮无声明时快照 execution_context 兜底恢复"
    );
}

/// stage_finalize 提取本轮最终 assistant 消息（完整持久形态，含引擎分配的 seq），
/// WS 路径 new_message 携带它（冷热同构）；无消息历史 → None（回退路径不炸）。
#[tokio::test]
async fn stage_finalize_extracts_final_assistant() {
    let final_state = json!({
        "raw_result": "hi",
        "messages": [
            {"role": "user", "content": "本轮提问", "seq": 3,
             "metadata": {"client_message_id": "cmid-0198"}},
            {"role": "assistant", "content": "本轮回复", "seq": 4},
        ],
    });
    let out = stage_finalize(
        &final_state,
        None,
        "tenant-1",
        "pipe-1",
        "thread-1",
        "agent-1",
        "run_t1",
    )
    .await;
    let a = out
        .final_assistant
        .clone()
        .expect("必须提取到本轮 assistant 消息");
    assert_eq!(a["content"], "本轮回复");
    assert_eq!(a["seq"], 4, "权威 seq 必须来自引擎分配（非数组猜测）");
    // 无消息历史 → None（回退路径不炸）
    let empty = stage_finalize(
        &json!({"raw_result": "x"}),
        None,
        "t",
        "p",
        "t",
        "a",
        "run_t2",
    )
    .await;
    assert!(empty.final_assistant.is_none());
}

// ── pending 输入端点（ADR-2026-08-26）：GET/PUT/DELETE/clear ──

/// 入队一条 pending（绕过 dispatcher 直写 store，端点测试用）。
async fn seed_pending(store: &Arc<dyn StorageBackend>, pid: &str, content: &str) -> String {
    use agentos_core::types::PendingInputRecord;
    let id = format!("p_seed_{}", &uuid::Uuid::new_v4().simple().to_string()[..8]);
    store
        .enqueue_pending_input(
            "default",
            &PendingInputRecord {
                id: id.clone(),
                tenant_id: "default".to_string(),
                created_at: chrono::Utc::now().to_rfc3339(),
                request: agentos_core::types::DispatchRequest {
                    thread_id: format!("thread-{pid}"),
                    user_id: "u1".to_string(),
                    pipeline_id: pid.to_string(),
                    content: content.to_string(),
                    client_message_id: String::new(),
                    source: agentos_core::types::PendingInputSource::User,
                    execution_context: None,
                    state_overlay: None,
                    pipeline_config_id: None,
                },
            },
        )
        .await
        .unwrap();
    id
}

#[tokio::test]
async fn test_pending_inputs_endpoints_crud() {
    let (state, _invoker, store, sqlite, _user_space_guard) = make_engine_state();
    let token = seed_admin_token(&sqlite).await;
    let bearer = format!("Bearer {token}");
    let app = build_router(state);
    let pid = "pipe-endpoint-1";

    // 入队两条（FIFO 序）
    let id1 = seed_pending(&store, pid, "第一条").await;
    let id2 = seed_pending(&store, pid, "第二条").await;

    // GET 列表
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!("/api/v1/pipelines/{pid}/pending-inputs"))
                .header("authorization", &bearer)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = serde_json::from_slice::<serde_json::Value>(
        &axum::body::to_bytes(resp.into_body(), 1 << 20)
            .await
            .unwrap(),
    )
    .unwrap();
    let items = body["items"].as_array().unwrap();
    assert_eq!(items.len(), 2, "两条 pending 输入");
    assert_eq!(items[0]["content"], "第一条", "FIFO 序");
    assert_eq!(items[1]["content"], "第二条");

    // PUT 修改
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri(format!("/api/v1/pipelines/{pid}/pending-inputs/{id1}"))
                .header("content-type", "application/json")
                .header("authorization", &bearer)
                .body(Body::from(r#"{"content":"修改后"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let rows = store.list_pending_inputs("default", pid).await.unwrap();
    assert_eq!(rows[0].request.content, "修改后", "PUT 覆盖 content");

    // PUT 不存在 → 404
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri(format!("/api/v1/pipelines/{pid}/pending-inputs/ghost"))
                .header("content-type", "application/json")
                .header("authorization", &bearer)
                .body(Body::from(r#"{"content":"x"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);

    // DELETE 单条
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("DELETE")
                .uri(format!("/api/v1/pipelines/{pid}/pending-inputs/{id2}"))
                .header("authorization", &bearer)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let rows = store.list_pending_inputs("default", pid).await.unwrap();
    assert_eq!(rows.len(), 1, "删掉一条剩一条");

    // DELETE 不存在 → 404
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("DELETE")
                .uri(format!("/api/v1/pipelines/{pid}/pending-inputs/ghost"))
                .header("authorization", &bearer)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);

    // clear 清空
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("DELETE")
                .uri(format!("/api/v1/pipelines/{pid}/pending-inputs"))
                .header("authorization", &bearer)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert!(
        store
            .list_pending_inputs("default", pid)
            .await
            .unwrap()
            .is_empty(),
        "clear 后队列空"
    );
}

/// 端点守卫分支：无 store → 404；PUT 空 content → 400；不存在的端点路径 404。
#[tokio::test]
async fn test_pending_inputs_endpoints_guards() {
    // 无 store（AppState::new()）：鉴权过闸（脚手架 token）后 GET → 404（store not injected）
    let app = build_router(AppState::new());
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/pipelines/pipe-x/pending-inputs")
                .header("authorization", scaffold_admin_bearer())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);

    // 有 store：PUT 空 content → 400
    let (state, _invoker, _store, sqlite, _user_space_guard) = make_engine_state();
    let token = seed_admin_token(&sqlite).await;
    let app = build_router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/api/v1/pipelines/pipe-x/pending-inputs/abc")
                .header("content-type", "application/json")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::from(r#"{"content":""}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
}

// ── 消息段端点（多代切换/压缩原文存档）：GET 清单 + GET 单段 ──

#[tokio::test]
async fn test_message_segments_endpoints_happy_path_and_guards() {
    let (state, _invoker, store, sqlite, _user_space_guard) = make_engine_state();
    let token = seed_admin_token(&sqlite).await;
    let bearer = format!("Bearer {token}");
    let app = build_router(state);
    let pid = "pipe-seg-ep";

    // 冻结一段（store 直写）：段行 + 成员全文
    store
        .apply_messages_ops_to_table(
            pid,
            "default",
            &[json!({
                "op": "freeze_segment", "id": "seg_ep1",
                "base_seq": 1, "base_len": 1, "visible_to": "[user]",
                "preview": "被压原文", "members": [{"role": "user", "content": "被压原文"}],
            })],
        )
        .await
        .unwrap();

    // GET 清单：无成员字段、含 preview，租户过滤
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!("/api/v1/pipelines/{pid}/message-segments"))
                .header("authorization", &bearer)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = serde_json::from_slice::<serde_json::Value>(
        &axum::body::to_bytes(resp.into_body(), 1 << 20)
            .await
            .unwrap(),
    )
    .unwrap();
    let segments = body["segments"].as_array().unwrap();
    assert_eq!(segments.len(), 1);
    assert_eq!(segments[0]["id"], "seg_ep1");
    assert_eq!(segments[0]["base_seq"], 1);
    assert_eq!(segments[0]["preview"], "被压原文");
    assert_eq!(segments[0]["visible_to"], "[user]");
    assert!(segments[0].get("members").is_none(), "清单不解析成员");

    // GET 单段：成员全文解析
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/v1/message-segments/seg_ep1")
                .header("authorization", &bearer)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = serde_json::from_slice::<serde_json::Value>(
        &axum::body::to_bytes(resp.into_body(), 1 << 20)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(body["segment"]["id"], "seg_ep1");
    assert_eq!(
        body["segment"]["members"][0]["content"], "被压原文",
        "成员全文可达"
    );

    // 不存在的段 → 404
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/v1/message-segments/seg_ghost")
                .header("authorization", &bearer)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);

    // 未鉴权 → 401（段行含会话内容，读面必须过闸）
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/message-segments/seg_ep1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
}

/// 无 store 路径（单测/兼容）：dispatch 直接入链执行（spawn_chain），
/// 不经持久化队列——消息仍被消费（队列语义被旁路）。
#[tokio::test]
async fn test_pending_input_no_store_dispatch_direct() {
    let (state, _invoker, _store, _sqlite, _user_space_guard) = make_engine_state();
    // 剥离 store：模拟无存储构造（单测/兼容路径）
    let mut state = state;
    state.store = None;
    let dispatcher = crate::ws_session::EngineDispatcher::new(state);
    use agentos_session::router::PipelineDispatcher;
    // 不报错（直接入链执行）
    dispatcher
        .dispatch_user_input(agentos_core::types::DispatchRequest {
            thread_id: ("thread-nostore-1").to_string(),
            user_id: ("u1").to_string(),
            pipeline_id: ("pipe-nostore-1").to_string(),
            content: ("无存储直发").to_string(),
            client_message_id: ("").to_string(),
            source: PendingInputSource::User,
            execution_context: None,
            state_overlay: None,
            pipeline_config_id: None,
        })
        .await
        .unwrap();
    // 给链任务留出执行窗口（spawn 异步；无副作用可断言——不 panic 即验证路径）
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
}

/// REST chat waiter 桥（remove 先到先得语义）：命中 cmid 发送并移除；
/// 未命中 cmid 静默（非 REST 路径 cmid 恒 miss）；同 cmid 二次通知 miss。
#[tokio::test]
async fn test_outcome_waiter_bridge_remove_semantics() {
    fn outcome(content: &str) -> crate::server::EngineOutcome {
        crate::server::EngineOutcome {
            content: content.to_string(),
            final_assistant: None,
            failed: false,
            degraded: false,
            plugin_errors: Vec::new(),
            consumed_pending_cmids: Vec::new(),
        }
    }
    let (tx, mut rx) = tokio::sync::oneshot::channel();
    // 唯一键：全局 static 表，避免与其他测试注册的 waiter 冲突。
    crate::ws_session::register_outcome_waiter("http_waiter_t1".to_string(), tx);
    // 未命中：静默跳过，不 panic、不影响已注册 waiter
    crate::ws_session::notify_outcome_waiter("http_someone_else", outcome("other"));
    assert!(rx.try_recv().is_err());
    // 命中：发送并移除
    crate::ws_session::notify_outcome_waiter("http_waiter_t1", outcome("done"));
    let got = rx.try_recv().expect("命中 cmid 应送达 outcome");
    assert_eq!(got.content, "done");
    // 二次同 cmid：已移除，miss 不 panic
    crate::ws_session::notify_outcome_waiter("http_waiter_t1", outcome("again"));
}

#[test]
fn test_extract_response_content_prefers_raw_result() {
    let state = json!({"raw_result": "LLM 真实回复", "message": "用户输入原文"});
    assert_eq!(extract_response_content(&state), "LLM 真实回复");
}

#[test]
fn test_extract_response_content_never_falls_back_to_user_message() {
    // 回归（08-27 前端回显根因）：无 raw_result 时不得回退 state.message
    // ——那是用户输入原文，回退即把用户消息当回复回发（assistant 气泡回显）。
    let state = json!({"raw_result": "", "message": "用户输入原文"});
    assert_eq!(extract_response_content(&state), "pipeline finished");
}

#[test]
fn test_extract_response_content_no_keys_returns_fixed_text() {
    assert_eq!(extract_response_content(&json!({})), "pipeline finished");
}

// ── 冷恢复轨迹回放作用域（2026-08-30 管道身份裁定回归）──────────────
// pipeline_id 是执行态唯一坐标：同会话其它管道（父会话/兄弟子任务）的
// 轨迹一律不得回放进本管道初始 state——曾致新子任务被父线程轨迹里的
// conversation_mode=true + core_type=tool_execute 送到对话挂起路由，
// 第 0 轮未跑 LLM 即 suspended，任务假 running 永挂。

use agentos_core::types::{PatchType, TraceEntry};

async fn seed_pipeline_trace(
    store: &agentos_engine::SqliteStore,
    pipeline_id: &str,
    run_id: &str,
    patch_data: serde_json::Value,
) {
    // 只落轨迹、不写 state 运行键/消息槽：ADR 2026-09-18 冷恢复新序下
    // traces 回放仅在 state 与消息双空的 DR 分支参与，预置保持双空回放才命中
    // （回放按 pipeline_id 直查，traces 已是 pipeline 级 op 流）。
    store
        .append_trace(TraceEntry {
            trace_id: format!("t-{run_id}"),
            pipeline_id: pipeline_id.to_string(),
            // 写入侧 seq 恒 0，存储层按 (pipeline_id, MAX(seq)+1) 分配日志序
            seq: 0,
            plugin_id: "post".into(),
            patch_type: PatchType::StateUpdate,
            patch_data,
            created_at: chrono::Utc::now().to_rfc3339(),
        })
        .await
        .unwrap();
}

#[tokio::test]
async fn cold_recovery_replays_only_own_pipeline_traces() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    store
        .link_pipeline_session("pipe-parent", "T1", "default")
        .await
        .unwrap();
    store
        .link_pipeline_session("pipe-child", "T1", "default")
        .await
        .unwrap();

    // 父管道轨迹携带对话挂起控制态（事故现场同款）+ 持久任务域标量。
    // 两管道只落轨迹（state/消息双空）——回放是 DR 分支的专属路径。
    seed_pipeline_trace(
        &store,
        "pipe-parent",
        "run-parent",
        json!({"conversation_mode": true, "core_type": "tool_execute",
               "core_plugin": "pipeline_tool_core", "task.status": "running"}),
    )
    .await;
    // 子任务自己的轨迹只含任务域标量
    seed_pipeline_trace(
        &store,
        "pipe-child",
        "run-child",
        json!({"task.status": "running"}),
    )
    .await;

    // 子任务冷恢复：初始 state 是新鲜默认（core_type=llm_call）。
    // P1-4 声明化：插件语义 per-run 键由 manifest 声明并集驱动跳过
    // （生产中 stage_build_initial_state 从 manifests 收集；此处模拟
    // llm_core/conversation_mode/tool_schema 插件已声明并装载）。
    let declared: std::collections::HashSet<String> = [
        "thinking_strength",
        "tool_schemas",
        "conversation_mode",
        "core_type",
        "core_plugin",
    ]
    .into_iter()
    .map(String::from)
    .collect();
    let initial = json!({"core_type": "llm_call", "core_plugin": "pipeline_llm_core"});
    let recovered = super::stage_recover_history(
        initial,
        &(store.clone() as Arc<dyn agentos_core::traits::StorageBackend>),
        "kickoff",
        "pipe-child",
        "default",
        "",
        true,
        None,
        &declared,
        "",
    )
    .await
    .expect("stage_recover_history 应成功（本测试注入无故障）");

    assert!(
        recovered.get("conversation_mode").is_none(),
        "父管道控制态 conversation_mode 不得串进子任务: {:?}",
        recovered.get("conversation_mode")
    );
    assert_eq!(
        recovered.get("core_type").and_then(|v| v.as_str()),
        Some("llm_call"),
        "父管道 core_type=tool_execute 不得覆盖子任务初始轮次类型"
    );
    // 正对照：本管道自己的轨迹正常回放
    assert_eq!(
        recovered.get("task.status").and_then(|v| v.as_str()),
        Some("running"),
        "子任务自己的轨迹标量必须回放"
    );

    // 父管道自身冷恢复仍能拿回自己的历史（作用域收窄不丢自己的历史）：
    // 持久键 task.status 照常恢复；2026-09-02 VOLATILE 裁定（4f6cb15fb）后
    // conversation_mode/core_type 属本轮执行态，跨 run 剥离不得复活——
    // 旧断言「conversation_mode 必须完整恢复」随该裁定过期，同刀更新。
    let parent_recovered = super::stage_recover_history(
        json!({}),
        &(store as Arc<dyn agentos_core::traits::StorageBackend>),
        "resume",
        "pipe-parent",
        "default",
        "",
        true,
        None,
        &declared,
        "",
    )
    .await
    .expect("stage_recover_history 应成功（本测试注入无故障）");
    assert_eq!(
        parent_recovered.get("task.status"),
        Some(&json!("running")),
        "本管道持久标量必须完整恢复"
    );
    assert!(
        parent_recovered.get("conversation_mode").is_none(),
        "对话等待态是本轮执行态，跨 run 剥离不得复活（2026-09-02 裁定）"
    );
}

// ── B13① 域界定修复（ADR 2026-09-06-resume-wake-semantics）：派发 overlay
// 必须在恢复合并之后应用——显式派发指令 > 持久化基线。残留终态键
// （task.status=failed）不得覆盖任务域 resume 链的复位 overlay。 ──

/// 构造带终态残留的 registry 热路径快照（上一 run final_state 形态）。
fn overlay_test_registry_snapshot() -> serde_json::Value {
    json!({
        "messages": [],
        "task.id": "pipe_ov_residue",
        "task.status": "failed",
        "task.goal": "残留终态任务",
    })
}

#[tokio::test]
async fn resume_overlay_reset_survives_hot_path_recovery() {
    // 热路径：registry 内存快照残留 task.status=failed（上一 failed run 的
    // final_state），任务域 resume overlay 复位 task.status=running——
    // 恢复合并不得把残留终态回写覆盖复位（B13① 第一轮秒杀根因）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite;
    let pipeline = format!("pipe_ov_hot_{}", uuid::Uuid::new_v4().simple());
    agentos_session::global_registry().get_or_init(
        "default",
        &pipeline,
        "thread_ov_hot",
        "agentos",
        overlay_test_registry_snapshot(),
    );
    let overlay = json!({"task.status": "running", "task_status": "running"});
    let st = super::stage_recover_history(
        json!({"pipeline_id": pipeline, "message": "resume"}),
        &store,
        "resume",
        &pipeline,
        "default",
        "",
        true,
        Some(&overlay),
        &Default::default(),
        "",
    )
    .await
    .expect("stage_recover_history 应成功（本测试注入无故障）");
    assert_eq!(
        st["task.status"], "running",
        "恢复合并后 overlay 必须生效：终态残留不得覆盖任务域复位"
    );
    assert_eq!(
        st["task_status"], "running",
        "stop_check 缓存读面双键的另一个键同样不得残留终态"
    );
    // 性质断言：overlay 未触及的持久标量照常恢复（复位只赢在有 overlay 的键）
    assert_eq!(st["task.goal"], "残留终态任务");
}

#[tokio::test]
async fn resume_overlay_reset_survives_cold_path_recovery() {
    // 冷路径（真实 SQLite）：checkpoint 残留 task.status=failed（save_checkpoint
    // 只剥 messages/VOLATILE_RUN_KEYS，task.* 照落），overlay 复位同样必须赢。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite;
    let pipeline = format!("pipe_ov_cold_{}", uuid::Uuid::new_v4().simple());
    store
        .save_checkpoint(
            &pipeline,
            "default",
            7,
            &json!({
                "messages": [],
                "task.id": pipeline,
                "task.status": "failed",
            }),
        )
        .await
        .unwrap();
    let overlay = json!({"task.status": "running", "task_status": "running"});
    let st = super::stage_recover_history(
        json!({"pipeline_id": pipeline, "message": "resume"}),
        &store,
        "resume",
        &pipeline,
        "default",
        "",
        true,
        Some(&overlay),
        &Default::default(),
        "",
    )
    .await
    .expect("stage_recover_history 应成功（本测试注入无故障）");
    assert_eq!(
        st["task.status"], "running",
        "冷路径 checkpoint 终态残留同样不得覆盖复位 overlay"
    );
}

#[tokio::test]
async fn recovery_without_overlay_still_restores_persisted_scalars() {
    // 防过度修复：无 overlay 的普通续跑（resume 按钮不传复位/正常多轮对话）
    // 恢复合并行为必须原样——持久标量（含终态）照常恢复。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite;
    let pipeline = format!("pipe_ov_plain_{}", uuid::Uuid::new_v4().simple());
    store
        .save_checkpoint(
            &pipeline,
            "default",
            3,
            &json!({"messages": [], "task.id": pipeline, "task.status": "failed"}),
        )
        .await
        .unwrap();
    let st = super::stage_recover_history(
        json!({"pipeline_id": pipeline, "message": "next"}),
        &store,
        "next",
        &pipeline,
        "default",
        "",
        true,
        None,
        &Default::default(),
        "",
    )
    .await
    .expect("stage_recover_history 应成功（本测试注入无故障）");
    assert_eq!(
        st["task.status"], "failed",
        "无 overlay 时恢复基线原样生效（复位只属于显式派发指令）"
    );
}

// ── P1-3 interaction/response 声明驱动解绑 ─────────────────────────────

/// 回显 handler：记录路由到的 (namespace, method, params)。
struct EchoCapabilityHandler {
    ns: &'static str,
    calls: std::sync::Mutex<Vec<serde_json::Value>>,
}

#[async_trait::async_trait]
impl agentos_mcp::CapabilityHandler for EchoCapabilityHandler {
    fn namespace(&self) -> &str {
        self.ns
    }
    async fn handle(
        &self,
        method: &str,
        params: serde_json::Value,
    ) -> Result<serde_json::Value, agentos_mcp::McpError> {
        self.calls
            .lock()
            .unwrap()
            .push(serde_json::json!({"namespace": self.ns, "method": method, "params": params}));
        Ok(serde_json::json!({"echoed": true, "method": method}))
    }
}

fn interaction_manifest(
    id: &str,
    namespace: &str,
    method: &str,
) -> agentos_core::traits::PluginManifest {
    serde_json::from_value(serde_json::json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "tool", "language": "python",
        "host_type": "sidecar", "entry": "x",
        "capabilities": {},
        "provides": {
            "capabilities": [{
                "namespace": namespace,
                "methods": [method],
                "host": "sidecar",
                "protocol_roles": [{"role": "interaction-respond", "method": method}]
            }]
        }
    }))
    .expect("valid manifest")
}

/// 提供者从 provides.protocol_roles 角色声明解析：声明方（含换插件 id/namespace
/// 的同角色声明）即端点后端，内核零插件 id 知识；无声明 → None。
#[test]
fn resolve_interaction_responder_follows_declaration() {
    assert!(super::resolve_interaction_responder(&[]).is_none());
    let builtin = interaction_manifest("human_interaction_tool", "human-interaction", "respond");
    assert_eq!(
        super::resolve_interaction_responder(&[builtin]),
        Some(("human-interaction".to_string(), "respond".to_string())),
    );
    // 任意插件声明同形交互能力即可复用该端点（换 id 换 namespace 同样解析）
    let custom = interaction_manifest("custom_asker", "custom-interaction", "answer");
    assert_eq!(
        super::resolve_interaction_responder(&[custom]),
        Some(("custom-interaction".to_string(), "answer".to_string())),
    );
}

/// HTTP 端点行为：路由到声明提供者 + 应答载荷原样透传；无声明 → 显式失败。
#[tokio::test]
async fn interaction_response_routes_to_declared_provider_and_passthrough() {
    use axum::extract::State;
    let mut state = AppState::new();
    let handler = std::sync::Arc::new(EchoCapabilityHandler {
        ns: "human-interaction",
        calls: std::sync::Mutex::new(Vec::new()),
    });
    let registry = agentos_mcp::CapabilityHandlerRegistry::new();
    registry.register(handler.clone());
    state.capability_handlers = Some(std::sync::Arc::new(registry));
    state.manifests = std::sync::Arc::new(tokio::sync::RwLock::new(vec![interaction_manifest(
        "human_interaction_tool",
        "human-interaction",
        "respond",
    )]));

    let body = serde_json::json!({
        "request_id": "r1",
        "response_type": "answered",
        "selected_option": "A",
        "feedback": "ok",
    });
    let resp = super::interaction_response_handler(State(state.clone()), axum::Json(body)).await;
    let resp = resp.unwrap().0;
    assert_eq!(
        resp["success"],
        serde_json::json!(true),
        "声明提供者在场应成功: {resp}"
    );
    assert_eq!(resp["data"]["method"], "respond");

    let calls = handler.calls.lock().unwrap();
    assert_eq!(calls.len(), 1, "应恰好路由一次");
    let call = &calls[0];
    assert_eq!(call["namespace"], "human-interaction");
    // 载荷透传：request_id + response 原样信封（键契约归交互插件自持）
    assert_eq!(call["params"]["request_id"], "r1");
    assert_eq!(call["params"]["response"]["response_type"], "answered");
    assert_eq!(call["params"]["response"]["selected_option"], "A");
    assert_eq!(call["params"]["response"]["feedback"], "ok");
}

#[tokio::test]
async fn interaction_response_fails_closed_without_declaration() {
    use axum::extract::State;
    let mut state = AppState::new();
    let registry = agentos_mcp::CapabilityHandlerRegistry::new();
    registry.register(std::sync::Arc::new(EchoCapabilityHandler {
        ns: "human-interaction",
        calls: std::sync::Mutex::new(Vec::new()),
    }));
    state.capability_handlers = Some(std::sync::Arc::new(registry));
    // manifests 空 = 无 interaction-respond 声明
    let resp = super::interaction_response_handler(
        State(state),
        axum::Json(serde_json::json!({"request_id": "r1", "response_type": "answered"})),
    )
    .await
    .unwrap()
    .0;
    assert_eq!(resp["success"], serde_json::json!(false));
    let err = resp["error"].as_str().unwrap_or_default();
    assert!(
        err.contains("interaction-respond"),
        "无声明应显式报缺声明（fail-closed），实际: {err}"
    );
}

/// 轨迹回放消费契约（与引擎 state_diff_from_journal 配对）：diff 中的 null 是
/// 删除标记（RFC 7396）——merge_patch 应用 null 后目标键被移除，普通值照常合并。
#[test]
fn merge_patch_null_deletes_key() {
    // 删除 + 变更混合 diff：x 记 null → x 被移除；keep 记新值 → 覆盖。
    let mut target = serde_json::json!({"x": {"n": 1}, "keep": 1});
    super::merge_patch(&mut target, &serde_json::json!({"x": null, "keep": 2}));
    assert_eq!(target, serde_json::json!({"keep": 2}));

    // 深层对象递归 merge 不受 null 语义影响（null 只在顶层键位是删除标记）。
    let mut target = serde_json::json!({"obj": {"a": 1, "b": 2}});
    super::merge_patch(&mut target, &serde_json::json!({"obj": {"b": 3}}));
    assert_eq!(target, serde_json::json!({"obj": {"a": 1, "b": 3}}));
}

// ── CORS 凭据收敛（认证走 Bearer 头，本地源反射不带 Allow-Credentials）──

/// CORS 凭据策略三态（单测串行断言，env 为进程级全局不可并行改写）：
/// 1. 本地开发源：反射 Origin 但不回 Allow-Credentials（内核认证走
///    Authorization 头不依赖 cookie，凭据头只给显式配置的生产白名单源）；
/// 2. 显式生产白名单源（AGENTOS_CORS_ORIGINS 精确命中）：反射 + 凭据头保持；
/// 3. 白名单外源：不反射不回凭据。
#[test]
fn cors_credential_policy_local_vs_explicit_allowlist() {
    // 本地源（任意端口）：反射、无凭据头
    let mut headers = axum::http::HeaderMap::new();
    super::apply_cors_headers(&mut headers, Some("http://localhost:5173"));
    assert_eq!(
        headers
            .get("access-control-allow-origin")
            .and_then(|v| v.to_str().ok()),
        Some("http://localhost:5173"),
        "本地源反射 Origin（开发直连语义不变）"
    );
    assert!(
        headers.get("access-control-allow-credentials").is_none(),
        "本地源不得回 Allow-Credentials（收敛反射任意源+凭据的面）"
    );

    // 显式生产白名单源：反射 + 凭据头（行为不变）
    std::env::set_var("AGENTOS_CORS_ORIGINS", "https://app.example.com");
    let mut headers = axum::http::HeaderMap::new();
    super::apply_cors_headers(&mut headers, Some("https://app.example.com"));
    std::env::remove_var("AGENTOS_CORS_ORIGINS");
    assert_eq!(
        headers
            .get("access-control-allow-origin")
            .and_then(|v| v.to_str().ok()),
        Some("https://app.example.com")
    );
    assert_eq!(
        headers
            .get("access-control-allow-credentials")
            .and_then(|v| v.to_str().ok()),
        Some("true"),
        "显式生产白名单源保留凭据放行"
    );

    // 白名单外源：不反射不回凭据
    let mut headers = axum::http::HeaderMap::new();
    super::apply_cors_headers(&mut headers, Some("https://evil.example.org"));
    assert!(headers.get("access-control-allow-origin").is_none());
    assert!(headers.get("access-control-allow-credentials").is_none());
}

// ── /uploads 鉴权（ADR 2026-10-10 收紧）+ 扩展名白名单（媒体类型契约）──

/// 构造带 project_root + 预置上传文件的 app（TempDir 随元组返回保活——
/// handler 惰性读文件，目录必须活到请求之后）。store=None → 内置脚手架
/// admin token（resolve 回退内置表）。
fn app_with_uploads(files: &[(&str, &[u8])]) -> (tempfile::TempDir, axum::Router) {
    let tmp = tempfile::tempdir().unwrap();
    let uploads = tmp.path().join("data").join("default").join("uploads");
    std::fs::create_dir_all(&uploads).unwrap();
    for (name, bytes) in files {
        std::fs::write(uploads.join(name), bytes).unwrap();
    }
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    (tmp, build_router(state))
}

#[tokio::test]
async fn uploads_reject_anonymous_even_when_file_exists() {
    let (_tmp, app) = app_with_uploads(&[("pic.png", b"pngbytes")]);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/uploads/pic.png")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::UNAUTHORIZED,
        "文件在场也必须 401（/uploads 收紧，匿名面闭合）"
    );
}

#[tokio::test]
async fn uploads_whitelisted_media_serves_authed() {
    let (_tmp, app) = app_with_uploads(&[("pic.png", b"pngbytes")]);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/uploads/pic.png")
                .header("authorization", scaffold_admin_bearer())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::OK,
        "已认证读面放行（viewer/admin 均可，GET 读面）"
    );
    assert_eq!(
        resp.headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok()),
        Some("image/png")
    );
}

#[tokio::test]
async fn uploads_non_whitelisted_extension_returns_404_authed() {
    let (_tmp, app) = app_with_uploads(&[
        ("archive.zip", b"zz"),
        ("notes.txt", b"secret"),
        ("db.sqlite", b"xx"),
    ]);
    for name in ["archive.zip", "notes.txt", "db.sqlite"] {
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri(format!("/uploads/{name}"))
                    .header("authorization", scaffold_admin_bearer())
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::NOT_FOUND,
            "白名单外扩展名 {name} 必须 404（不泄露存在性）"
        );
    }
}

#[tokio::test]
async fn uploads_path_traversal_still_rejected() {
    let (_tmp, app) = app_with_uploads(&[]);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/uploads/..%2Fconfig.yaml")
                .header("authorization", scaffold_admin_bearer())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

// ── P-2 鉴权面对账枚举（盘点即测试：注册面 × 两清单，ADR 2026-10-10）──
//
// 枚举键 = server.rs / http_dispatcher.rs 注册调用里的路径字面量 ×
// AUTH_* 清单（scripts/gen_openapi.py 后续复用同一枚举，Python 侧同型扫描）。

mod auth_surface_reconciliation_tests;

// ── B3：user 消息落库失败重试（3 次退避）后上抛终止本轮发送 ─────────────────

/// apply_messages_ops_to_table 按剩余失败配额失败并计数的存储 mock。其余必需
/// 方法以 unreachable! 桩实现——错误路径若意外耦合其他存储调用会在此炸出。
struct B3FlakyUserAppendStore {
    apply_attempts: std::sync::atomic::AtomicUsize,
    failures_remaining: std::sync::atomic::AtomicUsize,
}

impl B3FlakyUserAppendStore {
    fn new(failures: usize) -> Self {
        Self {
            apply_attempts: std::sync::atomic::AtomicUsize::new(0),
            failures_remaining: std::sync::atomic::AtomicUsize::new(failures),
        }
    }

    fn attempt_count(&self) -> usize {
        self.apply_attempts
            .load(std::sync::atomic::Ordering::SeqCst)
    }
}

#[async_trait::async_trait]
impl StorageBackend for B3FlakyUserAppendStore {
    async fn apply_messages_ops_to_table(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _ops: &[serde_json::Value],
    ) -> Result<(), agentos_core::types::StorageError> {
        self.apply_attempts
            .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let remaining = self
            .failures_remaining
            .fetch_update(
                std::sync::atomic::Ordering::SeqCst,
                std::sync::atomic::Ordering::SeqCst,
                |n| n.saturating_sub(1).into(),
            )
            .unwrap_or(0);
        if remaining > 0 {
            Err(agentos_core::types::StorageError::Database(
                "injected user append failure".into(),
            ))
        } else {
            Ok(())
        }
    }
    async fn get_run(
        &self,
        _run_id: &str,
    ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn get_messages_by_pipeline(
        &self,
        _pipeline_id: &str,
        _opts: MessageQueryOpts,
    ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn append_trace(
        &self,
        _entry: agentos_core::types::TraceEntry,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn store_blob(
        &self,
        _data: &[u8],
        _mime_type: &str,
    ) -> Result<String, agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn create_session(
        &self,
        _session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn get_session(
        &self,
        _thread_id: &str,
    ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn list_sessions(
        &self,
        _filter: agentos_core::traits::SessionListFilter,
    ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn update_session(
        &self,
        _session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn delete_session(
        &self,
        _thread_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn link_pipeline_session(
        &self,
        _pipeline_id: &str,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn list_pipeline_ids_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn get_step_traces_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn get_step_traces_by_pipeline(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn create_user(
        &self,
        _user: &agentos_core::types::UserRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn get_user_by_id(
        &self,
        _user_id: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn get_user_by_username(
        &self,
        _username: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn list_users(
        &self,
    ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn update_last_login(
        &self,
        _user_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn update_user_password(
        &self,
        _user_id: &str,
        _password_hash: &str,
        _must_change_password: bool,
    ) -> Result<bool, agentos_core::types::StorageError> {
        unreachable!("mock 不提供口令更新")
    }
    async fn delete_user(&self, _user_id: &str) -> Result<bool, agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
}

#[tokio::test]
async fn b3_user_append_fails_all_retries_then_err_and_memory_restored() {
    let store = Arc::new(B3FlakyUserAppendStore::new(usize::MAX));
    let mut state = json!({"pipeline_id": "pipe_b3a", "messages": []});

    let result = append_user_message(
        &mut state,
        &(store.clone() as Arc<dyn StorageBackend>),
        "default",
        "pipe_b3a",
        "hello",
        "",
        "test-run-id",
    )
    .await;

    assert!(result.is_err(), "重试耗尽必须上抛而非吞掉");
    // 退避序列 [100, 300] → 恰好 3 次尝试（1 首次 + 2 重试）
    assert_eq!(
        store.attempt_count(),
        USER_APPEND_RETRY_BACKOFF_MS.len() + 1,
        "必须重试满退避序列长度"
    );
    // 失败后内存不留新值：快照还原生效，无「内存已注入、实录缺席」分叉
    assert_eq!(
        state["messages"].as_array().unwrap().len(),
        0,
        "落库失败后 messages 必须还原到调用前状态: {state}"
    );
    assert!(
        state.get("_pending_message_ops").is_none(),
        "失败路径不得留实录指纹: {state}"
    );
}

#[tokio::test]
async fn b3_user_append_recovers_after_transient_failures_without_double_write() {
    let store = Arc::new(B3FlakyUserAppendStore::new(2));
    let mut state = json!({"pipeline_id": "pipe_b3b", "messages": []});

    let result = append_user_message(
        &mut state,
        &(store.clone() as Arc<dyn StorageBackend>),
        "default",
        "pipe_b3b",
        "transient",
        "",
        "test-run-id",
    )
    .await;

    assert!(result.is_ok(), "瞬时失败 2 次后第 3 次应成功");
    assert_eq!(store.attempt_count(), 3, "失败 2 次 + 成功 1 次 = 3 次尝试");
    let msgs = state["messages"].as_array().unwrap();
    assert_eq!(
        msgs.len(),
        1,
        "重试成功后 messages 必须恰好一条（快照还原防双写）: {state}"
    );
    assert_eq!(msgs[0]["role"], "user");
    assert_eq!(msgs[0]["content"], "transient");
    let ledger = state["_pending_message_ops"].as_array().unwrap();
    assert!(!ledger.is_empty(), "成功路径实录指纹必须就位: {state}");
}

#[tokio::test]
async fn b3_user_append_first_try_carries_client_message_id() {
    let store = Arc::new(B3FlakyUserAppendStore::new(0));
    let mut state = json!({"pipeline_id": "pipe_b3c", "messages": []});

    let result = append_user_message(
        &mut state,
        &(store.clone() as Arc<dyn StorageBackend>),
        "default",
        "pipe_b3c",
        "with-cmid",
        "cm-123",
        "test-run-id",
    )
    .await;

    assert!(result.is_ok(), "无失败应一次成功");
    assert_eq!(store.attempt_count(), 1, "无失败不得触发重试");
    let msgs = state["messages"].as_array().unwrap();
    assert_eq!(msgs.len(), 1);
    assert_eq!(
        msgs[0]["metadata"]["client_message_id"], "cm-123",
        "cmid 必须随消息落库（幂等键契约）: {state}"
    );
}

#[tokio::test]
async fn b3_stage_recover_history_maps_persist_failure_to_failed_outcome() {
    const PIPE: &str = "pipe_b3_recover";
    const TENANT: &str = "default";
    // 热路径：registry 预置历史（该路径不触碰 store 读面，只触碰 user append 写面）
    agentos_session::pipeline_state_registry::global_registry().get_or_init(
        TENANT,
        PIPE,
        "thread_b3",
        "",
        json!({"pipeline_id": PIPE, "messages": [{"role": "user", "content": "old", "seq": 0}]}),
    );
    let store = Arc::new(B3FlakyUserAppendStore::new(usize::MAX));

    let result = stage_recover_history(
        json!({"pipeline_id": PIPE, "session_id": "thread_b3"}),
        &(store.clone() as Arc<dyn StorageBackend>),
        "new message",
        PIPE,
        TENANT,
        "",
        false,
        None,
        &std::collections::HashSet::new(),
        "",
    )
    .await;

    agentos_session::pipeline_state_registry::global_registry().remove(TENANT, PIPE);

    match result {
        Ok(_) => panic!("落库重试耗尽必须上抛失败 outcome，不得带未落库消息进引擎"),
        Err(outcome) => {
            assert!(outcome.failed, "必须是失败 outcome");
            assert!(outcome.final_assistant.is_none());
            assert!(!outcome.degraded);
            assert!(
                outcome.content.contains("未受理"),
                "错误文案必须表达「消息未受理」: {}",
                outcome.content
            );
            assert!(store.attempt_count() >= 3, "必须先重试满再上抛");
        }
    }
}

// ═══════════════════════════════════════════════════════════════════════
// 覆盖率补测：routes.rs HTTP 路由层（actions / plugins / plugin+pipeline
// config / pipelines runs+state / system restart / schema 聚合）。
// 断行为契约（请求→响应状态码/响应体/落盘副作用），mock 只落在 invoker 边界。
// ═══════════════════════════════════════════════════════════════════════

mod routes_endpoints_tests;

// ═══════════════════════════════════════════════════════════════════════
// 覆盖率补测：session_routes.rs（会话 CRUD 成功路径 / 消息映射全字段 /
// 会话 schema 聚合）。断行为契约，存储走真实 SqliteStore。
// ═══════════════════════════════════════════════════════════════════════

mod session_endpoints_tests;

// ── CORS 中间件 / 鉴权中间件（write_surface_auth）/ WS 入口 503 /
//    交互应答端点（声明驱动路由）补测 ─────────────────────────────

mod cors_auth_middleware_tests;

// ═══════════════════════════════════════════════════════════════════════
// 覆盖率补测：管道配置指纹 / 冷恢复降级留痕 / 会话级 execution_context 注入 /
// 落库重试还原 / merge_patch 类型替换 / chat_handler 坐标解析降级。
// 断行为契约，存储走真实 SqliteStore，故障注入用探针 store（非全 mock）。
// ═══════════════════════════════════════════════════════════════════════

#[cfg(test)]
mod server_gap_tests;
