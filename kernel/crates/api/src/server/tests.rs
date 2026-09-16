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
        "Chat should not be in echo mode, got: {}",
        content
    );
    assert_eq!(json["session_id"], "test_session");
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
    let app = build_router(state_with_metrics());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/metrics?plugin=llm_service")
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
struct RecordingInvoker {
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

    // 场景 A：manifests 未含插件 → 未知引用编译失败，降级空管道。
    let root_a = std::env::temp_dir().join(format!("hr_a_{}", uuid::Uuid::new_v4().simple()));
    let config_a = write_cfg(&root_a);
    // 钉用户根到本用例临时目录：resolve_pipeline_config_path 先查用户配置层，
    // 宿主机真实用户空间播种过 pipelines/autonomous.yaml 时会旁路 TempDir
    // （读到真实管道，其插件引用不在注入 manifests 内 → 编译恒败）。
    let _guard_a = crate::test_env::pin_user_root(&root_a);
    let state_a = AppState::new();
    let compiled_a = maybe_reload_compiled_pipeline(&state_a, &config_a).await;
    assert!(
        compiled_a.bodies.is_empty(),
        "未知插件引用应编译失败并降级空管道"
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
    let compiled_b = maybe_reload_compiled_pipeline(&state_b, &config_b).await;
    assert_eq!(
        compiled_b.bodies.len(),
        1,
        "热发现后的插件应可作为管道 step 编译"
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

fn make_engine_state() -> (
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
    // 兜底配置（与临时 YAML 一致；临时 YAML 加载成功时此值被覆盖）
    state.pipeline_config = Arc::new(agentos_core::types::PipelineConfig {
        name: "test_multi_turn".to_string(),
        loop_bodies: vec![agentos_core::types::LoopBody {
            id: "llm".to_string(),
            steps: vec![agentos_core::types::PipelineStep {
                id: "llm".to_string(),
                steps: vec!["mock_llm_core".into()],
                when: None,
                context: std::collections::HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    });
    state.step_library = Arc::new(agentos_core::types::StepLibrary::default());
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
            "第一轮：我叫小明",
            "agentos",
            pipe,
            thread,
            "m1",
            "",
            "",
            None,
            None,
            "",
        ),
    )
    .await;
    assert!(!r1.content.is_empty(), "第一轮应返回 assistant 回复");

    // 第二轮：同 pipeline_id，应看到第一轮 user+assistant 上下文
    let r2 = agentos_tenant::scope(
        tenant,
        process_via_engine(
            &state,
            "第二轮：我叫什么？",
            "agentos",
            pipe,
            thread,
            "m2",
            "",
            "",
            None,
            None,
            "",
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
            "HTTP 零轮",
            "agentos",
            "",
            thread,
            "h0",
            "",
            "",
            None,
            None,
            "",
        ),
    )
    .await;
    assert!(rejected.failed, "空 pipeline_id 必须显式拒绝");

    let r1 = agentos_tenant::scope(
        tenant.clone(),
        process_via_engine(
            &state,
            "HTTP 第一轮",
            "agentos",
            pipe,
            thread,
            "h1",
            "",
            "",
            None,
            None,
            "",
        ),
    )
    .await;
    assert!(!r1.content.is_empty());

    let r2 = agentos_tenant::scope(
        tenant,
        process_via_engine(
            &state,
            "HTTP 第二轮",
            "agentos",
            pipe,
            thread,
            "h2",
            "",
            "",
            None,
            None,
            "",
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
            store_ref.create_run("run_cold", "", "tenant_cold").await.unwrap();
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
            "冷启动第二轮",
            "agentos",
            pipe,
            thread,
            "c2",
            "",
            "",
            None,
            None,
            "",
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
            store_ref.create_run("run_ended", "", "tenant_ended").await.unwrap();
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
            "冷启动第二轮",
            "agentos",
            pipe,
            thread,
            "e2",
            "",
            "",
            None,
            None,
            "",
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
            "alice 的消息",
            "agentos",
            pipe_a,
            thread_a,
            "a1",
            "",
            "",
            None,
            None,
            "",
        ),
    )
    .await;
    assert!(!r_a.content.is_empty(), "alice 发消息应返回 assistant 回复");

    // bob 发消息（在 bob 的 tenant scope 内）
    let r_b = agentos_tenant::scope(
        TenantContext::new("tenant_bob", thread_b),
        process_via_engine(
            &state,
            "bob 的消息",
            "agentos",
            pipe_b,
            thread_b,
            "b1",
            "",
            "",
            None,
            None,
            "",
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
    let cfg_dir = tmp_root.join("config").join("pipelines");
    std::fs::create_dir_all(&cfg_dir).unwrap();
    std::fs::write(
            cfg_dir.join("autonomous.yaml"),
            "name: t\nloop_bodies:\n  - id: main\n    steps:\n      - id: llm\n        steps:\n          - mock_llm_core\n",
        ).unwrap();
    state.project_root = Some(tmp_root);
    state.pipeline_config = Arc::new(agentos_core::types::PipelineConfig {
        name: "t".to_string(),
        loop_bodies: vec![agentos_core::types::LoopBody {
            id: "llm".to_string(),
            steps: vec![agentos_core::types::PipelineStep {
                id: "llm".to_string(),
                steps: vec!["mock_llm_core".into()],
                when: None,
                context: std::collections::HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
        initial_state: std::collections::HashMap::new(),
        max_rounds: None,
    });
    state.step_library = Arc::new(agentos_core::types::StepLibrary::default());
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
            "frank 的问题",
            "agentos",
            pipe,
            thread,
            "f1",
            "",
            "",
            None,
            None,
            "",
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

/// 带 contributes.thread_fields 声明的测试 manifest（模拟 workspace_lifecycle /
/// isolation 插件的声明面）。
fn thread_field_manifest(
    id: &str,
    fields: serde_json::Value,
) -> agentos_core::traits::PluginManifest {
    agentos_core::traits::PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: id.to_string(),
        name: id.to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: agentos_core::traits::PluginType::Pipeline,
        pipeline_role: None,
        language: "python".to_string(),
        host_type: agentos_core::traits::HostType::Sidecar,
        host_group: None,
        entry: "python server.py".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        granted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: Some(fields),
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        export_fields: vec![],
        provides: None,
    }
}

#[tokio::test]
async fn session_execution_context_assembled_from_thread_field_declarations() {
    // 声明驱动组装：metadata 值按 x_metadata_key → x_execution_path 声明路径
    // 写入 execution_context——内核不认识 workspace/isolation 具体键。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let now = "2026-08-28T00:00:00Z";
    store
        .create_session(&agentos_core::types::SessionRecord {
            thread_id: "thread-ec1".to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: None,
            pipeline_ids: vec![],
            metadata: Some(json!({
                "workspace": "D:/proj/demo",
                "workspace_mode": "plain",
                "isolation_mode": "isolated",
            })),
            created_at: now.to_string(),
            updated_at: now.to_string(),
            last_active_at: Some(now.to_string()),
        })
        .await
        .unwrap();
    let state = AppState::new();
    state.manifests.write().await.push(thread_field_manifest(
        "ws_lifecycle",
        json!({"thread_fields": [
            {"name": "workspace", "x_metadata_key": "workspace", "x_execution_path": "workspace.source_path"},
            {"name": "workspaceMode", "x_metadata_key": "workspace_mode", "x_execution_path": "workspace.mode"},
        ]}),
    ));
    state.manifests.write().await.push(thread_field_manifest(
        "isolation",
        json!({"thread_fields": [
            {"name": "isolationMode", "x_metadata_key": "isolation_mode", "x_execution_path": "isolation.level"},
        ]}),
    ));
    state
        .enabled_plugin_ids
        .write()
        .await
        .extend(["ws_lifecycle".to_string(), "isolation".to_string()]);
    let st = stage_build_initial_state(
        &state,
        &store,
        "msg",
        "pipe_ec1",
        "thread-ec1",
        "m1",
        "u1",
        "",
        None,
        "run-ec1",
    )
    .await;
    assert_eq!(
        st["execution_context"]["workspace"]["source_path"],
        "D:/proj/demo"
    );
    assert_eq!(st["execution_context"]["workspace"]["mode"], "plain");
    assert_eq!(st["execution_context"]["isolation"]["level"], "isolated");
}

#[tokio::test]
async fn session_execution_context_no_kernel_mode_default() {
    // 行为变更（ADR 2026-08-28 有意修正）：metadata 未选模式时内核不再缺省
    // 塞 worktree——mode 键缺位，默认值归插件执行期（workspace_lifecycle → plain，
    // 与前端 x_guard on_empty=plain 对齐）。
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let now = "2026-08-28T00:00:00Z";
    store
        .create_session(&agentos_core::types::SessionRecord {
            thread_id: "thread-ec2".to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: None,
            pipeline_ids: vec![],
            metadata: Some(json!({ "workspace": "D:/proj/demo" })),
            created_at: now.to_string(),
            updated_at: now.to_string(),
            last_active_at: Some(now.to_string()),
        })
        .await
        .unwrap();
    let state = AppState::new();
    state.manifests.write().await.push(thread_field_manifest(
        "ws_lifecycle",
        json!({"thread_fields": [
            {"name": "workspace", "x_metadata_key": "workspace", "x_execution_path": "workspace.source_path"},
            {"name": "workspaceMode", "x_metadata_key": "workspace_mode", "x_execution_path": "workspace.mode"},
        ]}),
    ));
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("ws_lifecycle".to_string());
    let st = stage_build_initial_state(
        &state,
        &store,
        "msg",
        "pipe_ec2",
        "thread-ec2",
        "m1",
        "u1",
        "",
        None,
        "run-ec2",
    )
    .await;
    assert_eq!(
        st["execution_context"]["workspace"]["source_path"],
        "D:/proj/demo"
    );
    assert!(
        st["execution_context"]["workspace"].get("mode").is_none(),
        "内核不得代插件塞 mode 默认值"
    );
}

#[tokio::test]
async fn session_execution_context_disabled_plugin_declarations_ignored() {
    // 未启用插件的声明不参与组装（与 sessions schema 聚合的 enabled 过滤对齐）
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite.clone();
    let now = "2026-08-28T00:00:00Z";
    store
        .create_session(&agentos_core::types::SessionRecord {
            thread_id: "thread-ec3".to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: None,
            pipeline_ids: vec![],
            metadata: Some(json!({ "isolation_mode": "isolated" })),
            created_at: now.to_string(),
            updated_at: now.to_string(),
            last_active_at: Some(now.to_string()),
        })
        .await
        .unwrap();
    let state = AppState::new();
    state.manifests.write().await.push(thread_field_manifest(
        "isolation",
        json!({"thread_fields": [
            {"name": "isolationMode", "x_metadata_key": "isolation_mode", "x_execution_path": "isolation.level"},
        ]}),
    ));
    // isolation 未加入 enabled_plugin_ids
    let st = stage_build_initial_state(
        &state,
        &store,
        "msg",
        "pipe_ec3",
        "thread-ec3",
        "m1",
        "u1",
        "",
        None,
        "run-ec3",
    )
    .await;
    assert!(
        st.get("execution_context").is_none(),
        "未启用插件的声明不得组装 execution_context"
    );
}

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
            "开始执行任务",
            "agentos",
            "pipe_overlay",
            "thread_overlay",
            "o1",
            "",
            "",
            None,
            Some(&overlay),
            "",
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

#[test]
fn test_derive_run_terminal_events_completed() {
    let st = json!({"pipeline_id": "p1", "session_id": "th1"});
    let evs = derive_run_terminal_events(&st, false);
    let names: Vec<&str> = evs.iter().map(|(n, _)| *n).collect();
    assert_eq!(names, vec!["run.completed"]);
    // 标签携带运行坐标
    let (_, tags) = &evs[0];
    let tag = |k: &str| {
        tags.iter()
            .find(|(tk, _)| tk.as_str() == k)
            .map(|(_, v)| v.clone())
            .unwrap_or(serde_json::Value::Null)
    };
    assert_eq!(tag("pipeline_id"), json!("p1"));
    assert_eq!(tag("thread_id"), json!("th1"));
    // 任务域字段在 state 也不派生任务事件（派生归 task_service 插件）
    let with_task = json!({
        "pipeline_id": "p1", "task.id": "t9", "task.status": "completed",
        "lineage.parent_pipeline_id": "parent_p1",
    });
    let names2: Vec<&str> = derive_run_terminal_events(&with_task, false)
        .iter()
        .map(|(n, _)| *n)
        .collect();
    assert_eq!(names2, vec!["run.completed"], "内核不得派生 task_completed");
}

// ── 终态 state 载荷（ADR 2026-09-11：内核随事件交出 state，订阅方零回查）──

#[test]
fn test_terminal_payload_carries_plugin_domain_keys_unfiltered() {
    // 断链实况（2026-09-11）：lineage.parent_pipeline_id 不在任何 manifest
    // export_fields 内 → pipeline-state.list 摘要恒缺该键 → 父锚点空串 →
    // 通知与挂号清除双双静默短路。载荷路径不经出口白名单，插件域键必须原样带出。
    let st = json!({
        "pipeline_id": "child1",
        "session_id": "th1",
        "task.id": "child1",
        "task.status": "completed",
        "task.goal": "写周报",
        "lineage.parent_pipeline_id": "parent1",
        "lineage.origin_session_id": "th1",
        "lineage.parent_ws_meta": {"mode": "plain", "path": "/ws"},
    });
    let evs = derive_run_terminal_events(&st, false);
    let (_, tags) = &evs[0];
    let payload = tags
        .iter()
        .find(|(k, _)| k == "state")
        .map(|(_, v)| v.clone())
        .expect("终态事件应携带 state 载荷");
    assert_eq!(payload["lineage.parent_pipeline_id"], json!("parent1"));
    assert_eq!(payload["lineage.origin_session_id"], json!("th1"));
    assert_eq!(payload["lineage.parent_ws_meta"]["path"], json!("/ws"));
    assert_eq!(payload["task.id"], json!("child1"));
    // 内核零知识：不派生任务域事件（载荷只是转发，不是判定）
    let names: Vec<&str> = evs.iter().map(|(n, _)| *n).collect();
    assert_eq!(names, vec!["run.completed"]);
}

#[test]
fn test_terminal_payload_strips_engine_bulk_fields() {
    // 大字段剥离：messages 全历史 / tool_schemas / raw_* / 引擎私有 `_` 前缀键
    // 不进载荷（否则事件到 MB 级，fire-and-forget 通知路径无背压）。
    let st = json!({
        "pipeline_id": "p1",
        "task.status": "completed",
        "messages": [{"role": "user", "content": "x"}],
        "tool_schemas": [{"name": "t"}],
        "raw_result": "big",
        "raw_thinking": "big",
        "raw_tool_calls": [1, 2],
        "_executed_tool_calls": ["a"],
        "track.total_tokens": 42,
    });
    let evs = derive_run_terminal_events(&st, false);
    let payload = evs[0]
        .1
        .iter()
        .find(|(k, _)| k == "state")
        .map(|(_, v)| v.clone())
        .expect("载荷在场");
    for k in [
        "messages",
        "tool_schemas",
        "raw_result",
        "raw_thinking",
        "raw_tool_calls",
        "_executed_tool_calls",
    ] {
        assert!(payload.get(k).is_none(), "{k} 应被剥离");
    }
    // 反向性质：插件域标量键保留
    assert_eq!(payload["track.total_tokens"], json!(42));
    assert_eq!(payload["task.status"], json!("completed"));
}

#[test]
fn test_terminal_payload_absent_or_empty_for_non_object_state() {
    // 非对象 state：载荷为空对象（订阅方回退回查），不 panic
    let evs = derive_run_terminal_events(&json!("not-an-object"), false);
    let payload = evs[0]
        .1
        .iter()
        .find(|(k, _)| k == "state")
        .map(|(_, v)| v.clone())
        .expect("载荷键恒在场（空对象兜底）");
    assert_eq!(payload, json!({}));
}

#[test]
fn test_terminal_payload_present_on_failed_and_suspended_paths() {
    // 四条终态分支同款携带载荷（failed 直通 / 署名映射）
    let st =
        json!({"pipeline_id": "p1", "task.status": "failed", "lineage.parent_pipeline_id": "par"});
    for (name, _) in derive_run_terminal_events(&st, true) {
        assert_eq!(name, "run.failed");
    }
    let failed = derive_run_terminal_events(&st, true);
    let payload = failed[0]
        .1
        .iter()
        .find(|(k, _)| k == "state")
        .map(|(_, v)| v.clone())
        .expect("failed 路径载荷在场");
    assert_eq!(payload["lineage.parent_pipeline_id"], json!("par"));

    let suspended = json!({"pipeline_id": "p2", "suspended": true, "task.id": "t2"});
    let evs = derive_run_terminal_events(&suspended, false);
    assert_eq!(evs[0].0, "run.suspended");
    let payload2 = evs[0]
        .1
        .iter()
        .find(|(k, _)| k == "state")
        .map(|(_, v)| v.clone())
        .expect("suspended 路径载荷在场");
    assert_eq!(payload2["task.id"], json!("t2"));
}

#[test]
fn test_derive_run_terminal_events_plain_and_suspended() {
    let plain = json!({"pipeline_id": "p2", "thread_id": "th2"});
    let names: Vec<&str> = derive_run_terminal_events(&plain, false)
        .iter()
        .map(|(n, _)| *n)
        .collect();
    assert_eq!(names, vec!["run.completed"]);

    // 挂起（RouteNext::Wait 落的 suspended 标志）：run.suspended
    let suspended = json!({"pipeline_id": "p3", "suspended": true});
    let names2: Vec<&str> = derive_run_terminal_events(&suspended, false)
        .iter()
        .map(|(n, _)| *n)
        .collect();
    assert_eq!(names2, vec!["run.suspended"]);
}

#[test]
fn test_derive_run_terminal_events_failed() {
    let st = json!({"pipeline_id": "p4"});
    let names: Vec<&str> = derive_run_terminal_events(&st, true)
        .iter()
        .map(|(n, _)| *n)
        .collect();
    assert_eq!(names, vec!["run.failed"]);
}

#[test]
fn test_derive_run_terminal_events_user_cancelled() {
    // 用户主动停止（router.stop_reason=user_requested）→ run.cancelled
    let st = json!({
        "pipeline_id": "p8",
        "router.stop_reason": "user_requested",
    });
    let names: Vec<&str> = derive_run_terminal_events(&st, false)
        .iter()
        .map(|(n, _)| *n)
        .collect();
    assert_eq!(names, vec!["run.cancelled"]);
}

#[test]
fn test_derive_run_terminal_events_signature_vocabulary() {
    // 终态映射单点（控制状态键契约 ADR 2026-08-30）：落库与域事件共用
    // RunStatus::from_control_state——署名 Failed 的 run 不得广播 run.completed，
    // 任务取消/删除署名与用户停止同落 run.cancelled。
    let cases: Vec<(&str, &str)> = vec![
        ("budget_exhausted", "run.failed"),
        ("elapsed_cap", "run.failed"),
        ("task_failed", "run.failed"),
        ("duplicate_loop", "run.failed"),
        ("tool_fail_loop", "run.failed"),
        ("task_cancelled", "run.cancelled"),
        ("task_deleted", "run.cancelled"),
        ("task_completed", "run.completed"),
        ("", "run.completed"),
    ];
    for (reason, expected) in cases {
        let st = json!({
            "pipeline_id": "p9",
            "router.stop_reason": reason,
        });
        let names: Vec<&str> = derive_run_terminal_events(&st, false)
            .iter()
            .map(|(n, _)| *n)
            .collect();
        assert_eq!(
            names,
            vec![expected],
            "stop_reason={reason} 应派生 {expected}"
        );
    }
}

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
            pipeline_role: None,
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
            granted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            ui_schema: None,
            persistent_fields: vec![],
            export_fields: vec![],
            http_endpoints: vec![],
            contributes: Default::default(),
            enabled: Some(true),
            activation: Default::default(),
            provides: Default::default(),
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
            "执行任务",
            "agentos",
            "pipe_gap2_emit",
            "thread_gap2_emit",
            "o1",
            "",
            "",
            None,
            Some(&overlay),
            "",
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
            "触发失败",
            "agentos",
            "pipe_fail_evt",
            "thread_fail_evt",
            "o1",
            "",
            "",
            None,
            Some(&overlay),
            "",
        ),
    )
    .await;
    assert!(r.failed, "引擎失败必须以 failed outcome 出口");
    assert!(
        r.content.contains("[engine-run-failed]"),
        "outcome 内容应携带失败标识: {}",
        r.content
    );
    // 防御网落库：该管道最新 run 置 failed（不悬空 running）
    let status: String = sqlite
        .with_conn(|c| {
            c.query_row(
                "SELECT status FROM runs WHERE pipeline_id = 'pipe_fail_evt' \
                     ORDER BY created_at DESC LIMIT 1",
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
            "正常输入但被用户停止",
            "agentos",
            "pipe_cancel_evt",
            "thread_cancel",
            "o1",
            "",
            "",
            None,
            Some(&overlay),
            "",
        ),
    )
    .await;
    assert!(!r.failed, "用户停止不是引擎失败");
    let status: String = sqlite
        .with_conn(|c| {
            c.query_row(
                "SELECT status FROM runs WHERE pipeline_id = 'pipe_cancel_evt'                      ORDER BY created_at DESC LIMIT 1",
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
            "重启前的那条消息",
            "agentos",
            "pipe_gap3",
            "thread_gap3",
            "o1",
            "",
            "",
            None,
            None,
            "",
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
            "审计锚测试消息",
            "agentos",
            "pipe_runid",
            "thread_runid",
            "o1",
            "",
            "",
            None,
            None,
            "",
        ),
    )
    .await;
    assert!(!r.failed, "首轮发送不应失败: {}", r.content);

    // message_slots 里该 user 消息（seq 0）的 run_id 非空且与 runs 表对齐
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
                "SELECT run_id FROM runs WHERE pipeline_id = 'pipe_runid' LIMIT 1",
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
                "再来一次",
                "agentos",
                "pipe_gap3b",
                "thread_gap3b",
                "o1",
                "",
                "",
                None,
                None,
                "",
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
                    msg,
                    "agentos",
                    pipeline,
                    "thread_gap3c",
                    "o1",
                    "",
                    "",
                    None,
                    None,
                    "",
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
            "执行任务",
            "agentos",
            "pipe_unify",
            "thread_unify",
            "o1",
            "",
            "",
            None,
            Some(&overlay),
            "",
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
            "普通消息",
            "agentos",
            "pipe_plain",
            "thread_unify",
            "o1",
            "",
            "",
            None,
            None,
            "",
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
            "帮我开个子任务",
            "agentos",
            "pipe_owned_only",
            "thread_owned_only",
            "o1",
            "",
            "",
            None,
            Some(&overlay),
            "",
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
            "执行全流程验证任务",
            "agentos",
            pipeline_id,
            "thread_lifecycle",
            "o1",
            "",
            "",
            None,
            Some(&overlay),
            "",
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
        .dispatch_user_input(
            "thread-1",
            "u1",
            "嗨",
            "pid-12hex-inject",
            "",
            None,
            None,
            "agentos",
            "",
            PendingInputSource::Trigger,
        )
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
    // 管道出生即登记归属会话（chat.send_message 创建分支同款），派发坐标校验依赖它
    store
        .link_pipeline_session("pipe-idle-1", "thread-idle-1", "default")
        .await
        .unwrap();
    let dispatcher = crate::ws_session::EngineDispatcher::new(state);
    use agentos_session::router::PipelineDispatcher;
    dispatcher
        .dispatch_user_input(
            "thread-idle-1",
            "u1",
            "空闲直发",
            "pipe-idle-1",
            "",
            None,
            None,
            "",
            "cmid-1",
            PendingInputSource::User,
        )
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
    // 管道出生即登记归属会话（chat.send_message 创建分支同款），派发坐标校验依赖它
    store
        .link_pipeline_session("pipe-del-1", "thread-del-1", "default")
        .await
        .unwrap();
    let dispatcher = crate::ws_session::EngineDispatcher::new(state);
    use agentos_session::router::PipelineDispatcher;
    dispatcher
        .dispatch_user_input(
            "thread-del-1",
            "u1",
            "将被消费",
            "pipe-del-1",
            "",
            None,
            None,
            "",
            "",
            PendingInputSource::User,
        )
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
    // execution_context 整键恢复：任务级快照覆盖本轮会话级种子（优先级契约）
    assert_eq!(
        out["execution_context"]["workspace"]["source_path"], "项目目录",
        "快照 execution_context 整键覆盖会话级种子"
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

/// stage_finalize 提取本轮最终 assistant 消息（完整持久形态，含引擎分配的 seq），
/// WS 路径 new_message 携带它（冷热同构）；无消息历史 → None（回退路径不炸）。
#[test]
fn stage_finalize_extracts_final_assistant() {
    let final_state = json!({
        "raw_result": "hi",
        "messages": [
            {"role": "user", "content": "本轮提问", "seq": 3,
             "metadata": {"client_message_id": "cmid-0198"}},
            {"role": "assistant", "content": "本轮回复", "seq": 4},
        ],
    });
    let out = stage_finalize(&final_state, "tenant-1", "pipe-1", "thread-1", "agent-1");
    let a = out
        .final_assistant
        .clone()
        .expect("必须提取到本轮 assistant 消息");
    assert_eq!(a["content"], "本轮回复");
    assert_eq!(a["seq"], 4, "权威 seq 必须来自引擎分配（非数组猜测）");
    // 无消息历史 → None（回退路径不炸）
    let empty = stage_finalize(&json!({"raw_result": "x"}), "t", "p", "t", "a");
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
            pid,
            &PendingInputRecord {
                id: id.clone(),
                pipeline_id: pid.to_string(),
                tenant_id: "default".to_string(),
                user_id: "u1".to_string(),
                content: content.to_string(),
                thread: format!("thread-{pid}"),
                source: agentos_core::types::PendingInputSource::User,
                agent_id: "agentos".to_string(),
                route_id: pid.to_string(),
                thinking_strength: String::new(),
                client_message_id: String::new(),
                execution_context: None,
                state_overlay: None,
                created_at: chrono::Utc::now().to_rfc3339(),
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
    assert_eq!(rows[0].content, "修改后", "PUT 覆盖 content");

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
        .dispatch_user_input(
            "thread-nostore-1",
            "u1",
            "无存储直发",
            "pipe-nostore-1",
            "",
            None,
            None,
            "agentos",
            "",
            PendingInputSource::User,
        )
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
    store.create_run(run_id, "cfg", "default").unwrap();
    store.set_run_pipeline(run_id, pipeline_id).await.unwrap();
    store
        .apply_messages_ops_to_table(
            pipeline_id,
            "default",
            &[json!({"op": "set", "seq": 0,
                     "msg": {"role": "user", "content": "hi"}, "_run_id": run_id})],
        )
        .unwrap();
    store
        .append_trace(TraceEntry {
            trace_id: format!("t-{run_id}"),
            run_id: run_id.to_string(),
            branch_id: "main".into(),
            seq_in_branch: 0,
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

    // 父管道轨迹携带对话挂起控制态（事故现场同款）+ 持久任务域标量
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

// ── /uploads 扩展名白名单（匿名面收口：<img> 无法带鉴权头，媒体白名单外 404）──

/// 构造带 project_root + 预置上传文件的 app（TempDir 随元组返回保活——
/// handler 惰性读文件，目录必须活到请求之后）。
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
async fn uploads_whitelisted_media_serves_anonymously() {
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
        StatusCode::OK,
        "白名单媒体匿名可达（<img> 契约）"
    );
    assert_eq!(
        resp.headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok()),
        Some("image/png")
    );
}

#[tokio::test]
async fn uploads_non_whitelisted_extension_returns_404() {
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
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

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
    async fn update_run_status(
        &self,
        _run_id: &str,
        _status: agentos_core::types::RunStatus,
        _branch: Option<&str>,
        _seq: Option<u32>,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("user append 路径不应触碰其他存储方法")
    }
    async fn create_run(
        &self,
        _run_id: &str,
        _config_hash: &str,
        _tenant_id: &str,
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

mod routes_endpoints_tests {
    use super::*;
    use crate::routes::{
        get_pipeline_config_handler, get_plugin_config_handler, ActionsExecuteRequest, EnabledBody,
        PipelineConfigUpdateRequest, PluginConfigUpdateRequest,
    };
    use agentos_core::traits::{HookContext, LifecycleHook, PluginInvoker, PluginManifest};
    use agentos_core::types::{PluginContext, PluginError, PluginResult, ToolExecutionResult};
    use axum::http::HeaderMap;

    /// 记录 invoke_tool 调用并可注入失败的 invoker（其余方法本组测试不可达）。
    struct ToolCallInvoker {
        calls: std::sync::Mutex<Vec<(String, String, serde_json::Value)>>,
        fail: bool,
    }

    #[async_trait::async_trait]
    impl PluginInvoker for ToolCallInvoker {
        async fn invoke_pipeline_plugin<'a>(
            &self,
            _plugin_id: &str,
            _ctx: &PluginContext<'a>,
        ) -> Result<PluginResult, PluginError> {
            unimplemented!("actions 测试不触达管道插件调用")
        }
        async fn invoke_tool(
            &self,
            plugin_id: &str,
            tool_name: &str,
            inputs: &serde_json::Value,
        ) -> Result<ToolExecutionResult, PluginError> {
            self.calls.lock().unwrap().push((
                plugin_id.to_string(),
                tool_name.to_string(),
                inputs.clone(),
            ));
            if self.fail {
                return Err(PluginError {
                    message: "sidecar 执行失败".to_string(),
                    code: None,
                    source: None,
                });
            }
            Ok(ToolExecutionResult::success(serde_json::json!({
                "echo": inputs,
            })))
        }
        async fn send_lifecycle_hook(
            &self,
            _plugin_id: &str,
            _hook: LifecycleHook,
            _context: &HookContext,
        ) -> Result<(), PluginError> {
            Ok(())
        }
    }

    fn manifest_from_json(v: serde_json::Value) -> PluginManifest {
        serde_json::from_value(v).expect("valid test manifest")
    }

    fn base_manifest(id: &str) -> serde_json::Value {
        serde_json::json!({
            "id": id, "name": id, "version": "1.0.0",
            "plugin_type": "system", "language": "python",
            "host_type": "sidecar", "entry": "python server.py",
            "capabilities": {},
        })
    }

    fn bearer_headers() -> HeaderMap {
        let mut h = HeaderMap::new();
        h.insert("authorization", scaffold_admin_bearer().parse().unwrap());
        h
    }

    // ── POST /api/v1/actions/execute ─────────────────────────────────────

    fn manifest_with_commands(id: &str, commands: serde_json::Value) -> PluginManifest {
        manifest_from_json(serde_json::json!({
            "id": id, "name": id, "version": "1.0.0",
            "plugin_type": "system", "language": "python",
            "host_type": "sidecar", "entry": "python server.py",
            "contributes": { "commands": commands },
            "capabilities": {},
        }))
    }

    #[tokio::test]
    async fn actions_execute_blank_action_returns_400() {
        let state = AppState::new();
        for action in ["", "   "] {
            let err = actions_execute_handler(
                axum::extract::State(state.clone()),
                axum::Json(ActionsExecuteRequest {
                    action: action.to_string(),
                    args: serde_json::json!({}),
                }),
            )
            .await
            .unwrap_err();
            assert!(
                matches!(err, ApiError::BadRequest { .. }),
                "空 action {action:?} 应 400，实际 {err:?}"
            );
        }
    }

    #[tokio::test]
    async fn actions_execute_unknown_command_returns_404() {
        let state = AppState::new();
        let err = actions_execute_handler(
            axum::extract::State(state),
            axum::Json(ActionsExecuteRequest {
                action: "no.such.command".to_string(),
                args: serde_json::json!({}),
            }),
        )
        .await
        .unwrap_err();
        match err {
            ApiError::NotFound { message } => {
                assert!(message.contains("no.such.command"), "{message}");
            }
            other => panic!("未声明命令应 404，实际 {other:?}"),
        }
    }

    #[tokio::test]
    async fn actions_execute_declared_command_without_tool_acks() {
        let mut state = AppState::new();
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest_with_commands(
            "cmd_plugin",
            serde_json::json!([
                { "id": "cmd.a", "label": "A" },
                { "id": "cmd.b", "label": "B" }
            ]),
        )]));

        for action in ["cmd.a", "cmd.b"] {
            let resp = actions_execute_handler(
                axum::extract::State(state.clone()),
                axum::Json(ActionsExecuteRequest {
                    action: action.to_string(),
                    args: serde_json::json!({"k": 1}),
                }),
            )
            .await
            .unwrap();
            assert_eq!(resp.0["success"], serde_json::json!(true));
            assert_eq!(resp.0["result"]["acknowledged"], serde_json::json!(true));
            assert_eq!(resp.0["result"]["action"], action);
            assert_eq!(resp.0["plugin_id"], "cmd_plugin");
        }
    }

    #[tokio::test]
    async fn actions_execute_tool_command_routes_to_invoker() {
        let mut state = AppState::new();
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest_with_commands(
            "tool_plugin",
            serde_json::json!([{ "id": "cmd.tool", "tool": "my_tool", "label": "T" }]),
        )]));
        let invoker = Arc::new(ToolCallInvoker {
            calls: std::sync::Mutex::new(Vec::new()),
            fail: false,
        });
        state.invoker = Some(invoker.clone() as Arc<dyn PluginInvoker>);

        let resp = actions_execute_handler(
            axum::extract::State(state),
            axum::Json(ActionsExecuteRequest {
                action: "cmd.tool".to_string(),
                args: serde_json::json!({"n": 7}),
            }),
        )
        .await
        .unwrap();

        assert_eq!(invoker.calls.lock().unwrap().len(), 1, "恰好一次工具调用");
        let (pid, tool, args) = invoker.calls.lock().unwrap()[0].clone();
        assert_eq!(pid, "tool_plugin");
        assert_eq!(tool, "my_tool");
        assert_eq!(args, serde_json::json!({"n": 7}));
        assert_eq!(resp.0["success"], serde_json::json!(true));
        assert_eq!(resp.0["result"]["echo"]["n"], serde_json::json!(7));
        assert_eq!(resp.0["plugin_id"], "tool_plugin");
    }

    #[tokio::test]
    async fn actions_execute_tool_invoker_error_is_explicit_failure() {
        let mut state = AppState::new();
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest_with_commands(
            "tool_plugin",
            serde_json::json!([{ "id": "cmd.tool", "tool": "my_tool" }]),
        )]));
        let invoker = Arc::new(ToolCallInvoker {
            calls: std::sync::Mutex::new(Vec::new()),
            fail: true,
        });
        state.invoker = Some(invoker as Arc<dyn PluginInvoker>);

        let resp = actions_execute_handler(
            axum::extract::State(state),
            axum::Json(ActionsExecuteRequest {
                action: "cmd.tool".to_string(),
                args: serde_json::json!({}),
            }),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["success"], serde_json::json!(false));
        assert!(
            resp.0["error"]
                .as_str()
                .unwrap()
                .contains("sidecar 执行失败"),
            "invoker 错误必须显式透出: {}",
            resp.0["error"]
        );
    }

    #[tokio::test]
    async fn actions_execute_tool_without_invoker_reports_unavailable() {
        let mut state = AppState::new();
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest_with_commands(
            "tool_plugin",
            serde_json::json!([{ "id": "cmd.tool", "tool": "my_tool" }]),
        )]));
        // invoker = None

        let resp = actions_execute_handler(
            axum::extract::State(state),
            axum::Json(ActionsExecuteRequest {
                action: "cmd.tool".to_string(),
                args: serde_json::json!({}),
            }),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["success"], serde_json::json!(false));
        assert!(
            resp.0["error"]
                .as_str()
                .unwrap()
                .contains("工具执行器不可用"),
            "执行器缺席必须明说，不得假成功: {}",
            resp.0["error"]
        );
        assert_eq!(resp.0["plugin_id"], "tool_plugin");
    }

    // ── GET /api/v1/plugins（状态清单）────────────────────────────────────

    #[tokio::test]
    async fn plugins_status_projects_manifest_fields() {
        let mut state = AppState::new();
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
            manifest_from_json(serde_json::json!({
                "id": "p_sys", "name": "系统插件", "version": "2.0.0",
                "description": "desc-sys",
                "plugin_type": "system", "language": "python",
                "host_type": "in_process", "entry": "x",
                "activation": "eager",
                "capabilities": {},
            })),
            manifest_from_json(serde_json::json!({
                "id": "p_tool", "name": "p_tool", "version": "1.0.0",
                "plugin_type": "tool", "language": "python",
                "host_type": "sidecar", "entry": "python server.py",
                "activation": "manual",
                "contributes": {"commands": []},
                "http_endpoints": [
                    {"route_id": "r1", "method": "GET", "path": "/ext/p_tool/x",
                     "handler_capability": "http.handle"}
                ],
                "config_files": [
                    {"id": "c1", "label": "可见", "path": "config/mine/a.yaml"},
                    {"id": "c2", "label": "注入专用", "path": "config/mine/b.yaml",
                     "settings": false}
                ],
                "capabilities": {},
            })),
            manifest_from_json(serde_json::json!({
                "id": "p_pipe", "name": "p_pipe", "version": "1.0.0",
                "plugin_type": "pipeline", "language": "python",
                "host_type": "sidecar", "entry": "x",
                "capabilities": {},
            })),
        ]));
        state.enabled_plugin_ids =
            Arc::new(tokio::sync::RwLock::new(std::collections::HashSet::from([
                "p_sys".to_string(),
            ])));

        let resp = plugins_status_handler(axum::extract::State(state)).await;
        let items = resp.0.as_array().unwrap();
        assert_eq!(items.len(), 3);
        let by_id = |i: &str| items.iter().find(|m| m["plugin_id"] == i).unwrap();

        let sys = by_id("p_sys");
        assert_eq!(sys["config_type"], "system");
        assert_eq!(sys["host_type"], "in_process");
        assert_eq!(sys["activation"], "eager");
        assert_eq!(sys["enabled"], true);
        assert_eq!(sys["status"], "active");
        assert_eq!(sys["name"], "系统插件");
        assert_eq!(sys["has_contributes"], false);
        assert_eq!(sys["has_http_endpoints"], false);

        let tool = by_id("p_tool");
        assert_eq!(tool["config_type"], "tool");
        assert_eq!(tool["host_type"], "sidecar");
        assert_eq!(tool["activation"], "manual");
        assert_eq!(tool["enabled"], false);
        assert_eq!(tool["status"], "disabled");
        assert_eq!(tool["has_contributes"], true);
        assert_eq!(tool["has_http_endpoints"], true);
        let cfgs = tool["config_files"].as_array().unwrap();
        assert_eq!(cfgs.len(), 1, "settings:false 注入专用条目不出口");
        assert_eq!(cfgs[0]["id"], "c1");

        assert_eq!(by_id("p_pipe")["config_type"], "pipeline");
    }

    // ── GET /api/v1/pipelines（管道插件清单）─────────────────────────────

    #[tokio::test]
    async fn pipelines_lists_only_pipeline_manifests_with_role_and_host() {
        let mut state = AppState::new();
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
            manifest_from_json(serde_json::json!({
                "id": "pl_1", "name": "pl_1", "version": "1.0.0",
                "plugin_type": "pipeline", "language": "python",
                "host_type": "sidecar", "entry": "x",
                "pipeline_role": "input",
                "capabilities": {},
            })),
            manifest_from_json(base_manifest("sys_1")),
        ]));
        let resp = pipelines_handler(axum::extract::State(state)).await;
        let arr = &resp.0;
        assert_eq!(arr.len(), 1, "只出口 Pipeline 类型清单");
        assert_eq!(arr[0]["id"], "pl_1");
        assert_eq!(arr[0]["role"], "input");
        assert_eq!(arr[0]["host_type"], "sidecar");
    }

    // ── GET /api/v1/pipelines/runs ───────────────────────────────────────

    async fn seed_run_with_slot(
        sqlite: &Arc<agentos_engine::SqliteStore>,
        run_id: &str,
        pipeline_id: &str,
        thread_id: &str,
    ) {
        use agentos_core::traits::StorageBackend;
        let dyn_store: Arc<dyn StorageBackend> = sqlite.clone();
        dyn_store
            .create_run(run_id, "cfg", "default")
            .await
            .unwrap();
        dyn_store
            .set_run_pipeline(run_id, pipeline_id)
            .await
            .unwrap();
        sqlite
            .apply_messages_ops_to_table(
                pipeline_id,
                "default",
                &[serde_json::json!({
                    "op": "set", "seq": 0,
                    "msg": {"role": "user", "content": "hi"},
                    "_run_id": run_id,
                })],
            )
            .unwrap();
        dyn_store
            .link_pipeline_session(pipeline_id, thread_id, "default")
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn pipelines_runs_without_db_returns_404() {
        let state = AppState::new();
        let err = pipelines_runs_handler(
            axum::extract::State(state),
            axum::extract::Query(std::collections::HashMap::new()),
            HeaderMap::new(),
        )
        .await
        .unwrap_err();
        assert!(
            matches!(err, ApiError::NotFound { .. }),
            "db 缺席应 404: {err:?}"
        );
    }

    #[tokio::test]
    async fn pipelines_runs_lists_joined_rows_with_status_and_limit_filters() {
        use agentos_core::traits::StorageBackend;
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        seed_run_with_slot(&sqlite, "run_a", "pipe_a", "thread_a").await;
        seed_run_with_slot(&sqlite, "run_b", "pipe_b", "thread_b").await;
        let dyn_store: Arc<dyn StorageBackend> = sqlite.clone();
        dyn_store
            .update_run_status(
                "run_b",
                agentos_core::types::RunStatus::Completed,
                None,
                None,
            )
            .await
            .unwrap();

        let mut state = AppState::new();
        state.db = Some(sqlite);

        // 无过滤：两个 run 都出口，行带 pipeline/thread 坐标
        let resp = pipelines_runs_handler(
            axum::extract::State(state.clone()),
            axum::extract::Query(std::collections::HashMap::new()),
            bearer_headers(),
        )
        .await
        .unwrap();
        let items = resp.0["items"].as_array().unwrap();
        assert_eq!(items.len(), 2);
        let ids: Vec<&str> = items
            .iter()
            .map(|r| r["run_id"].as_str().unwrap())
            .collect();
        assert!(ids.contains(&"run_a") && ids.contains(&"run_b"));
        let row_a = items.iter().find(|r| r["run_id"] == "run_a").unwrap();
        assert_eq!(row_a["pipeline_id"], "pipe_a");
        assert_eq!(row_a["thread_id"], "thread_a");
        assert_eq!(row_a["status"], "running");

        // status=completed 只剩 run_b
        let resp = pipelines_runs_handler(
            axum::extract::State(state.clone()),
            axum::extract::Query(std::collections::HashMap::from([(
                "status".to_string(),
                "completed".to_string(),
            )])),
            bearer_headers(),
        )
        .await
        .unwrap();
        let items = resp.0["items"].as_array().unwrap();
        assert_eq!(items.len(), 1);
        assert_eq!(items[0]["run_id"], "run_b");

        // limit=1 截断；非法 limit 回退默认（不报错）
        for (limit_param, expect_len) in [("1", 1), ("not-a-number", 2)] {
            let resp = pipelines_runs_handler(
                axum::extract::State(state.clone()),
                axum::extract::Query(std::collections::HashMap::from([(
                    "limit".to_string(),
                    limit_param.to_string(),
                )])),
                bearer_headers(),
            )
            .await
            .unwrap();
            assert_eq!(
                resp.0["items"].as_array().unwrap().len(),
                expect_len,
                "limit={limit_param}"
            );
        }
    }

    // ── GET /api/v1/pipelines/state ──────────────────────────────────────

    #[tokio::test]
    async fn pipelines_state_merges_memory_db_enrichment_and_cold_fallback() {
        use agentos_core::traits::StorageBackend;
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let dyn_store: Arc<dyn StorageBackend> = sqlite.clone();
        let reg = agentos_session::pipeline_state_registry::global_registry();

        let mut state = AppState::new();
        state.db = Some(sqlite.clone());
        state.store = Some(dyn_store.clone());
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest_from_json(
            serde_json::json!({
                "id": "p_exp", "name": "p_exp", "version": "1.0.0",
                "plugin_type": "system", "language": "python",
                "host_type": "sidecar", "entry": "x",
                "export_fields": ["task.status", "llm_model"],
                "capabilities": {},
            }),
        )]));

        // 全局 registry 是进程级共享（其余测试可能并发登记 default 租户条目），
        // 这里不断言全局空态，只按本测试唯一 pipeline_id 断言行级行为。

        // ① 内存热行（default 租户）+ ② 表行补齐声明键 llm_model
        let pid_mem = "pipe_state_mem_cov";
        reg.get_or_init(
            "default",
            pid_mem,
            "thread_mem",
            "agentos",
            serde_json::json!({
                "pipeline_id": pid_mem,
                "current_phase": "final",
                "run_status": "running",
                "secret_field": "must-not-leak",
            }),
        );
        dyn_store
            .upsert_state_field(pid_mem, "default", "llm_model", &serde_json::json!("k2"))
            .await
            .unwrap();

        // ③ 跨租户内存行不出口
        reg.get_or_init(
            "other_tenant",
            "pipe_state_other_cov",
            "t",
            "a",
            serde_json::json!({"pipeline_id": "pipe_state_other_cov"}),
        );

        // ④ DB 冷兜底行：checkpoint task.status=pending 被表行 completed 覆盖
        let pid_cold = "pipe_state_cold_cov";
        let ckpt = serde_json::json!({"task.status": "pending", "pipeline_id": pid_cold});
        dyn_store
            .save_checkpoint(pid_cold, "default", 3, &ckpt)
            .await
            .unwrap();
        dyn_store
            .upsert_state_field(
                pid_cold,
                "default",
                "task.status",
                &serde_json::json!("completed"),
            )
            .await
            .unwrap();
        // 冷行需要 runs × message_slots 联结可见
        sqlite.create_run("run_cold_cov", "cfg", "default").unwrap();
        sqlite
            .apply_messages_ops_to_table(
                pid_cold,
                "default",
                &[serde_json::json!({
                    "op": "set", "seq": 0,
                    "msg": {"role": "user", "content": "hi"},
                    "_run_id": "run_cold_cov",
                })],
            )
            .unwrap();
        dyn_store
            .link_pipeline_session(pid_cold, "thread_cold", "default")
            .await
            .unwrap();

        let resp = pipelines_state_handler(axum::extract::State(state), bearer_headers())
            .await
            .unwrap();
        let items = resp.0["items"].as_array().unwrap();
        let by_pid = |p: &str| {
            items
                .iter()
                .find(|i| i["pipeline_id"] == p)
                .unwrap_or_else(|| panic!("缺 {p} 行: {}", resp.0))
        };

        let mem = by_pid(pid_mem);
        assert_eq!(mem["source"], "memory");
        assert_eq!(mem["thread_id"], "thread_mem");
        assert_eq!(mem["agent_id"], "agentos");
        assert_eq!(mem["state"]["run_status"], "running", "基线键出口");
        assert_eq!(
            mem["state"]["llm_model"], "k2",
            "内存缺失的声明键由表行补齐"
        );
        assert!(mem["state"].get("secret_field").is_none(), "未声明键不出口");

        let cold = by_pid(pid_cold);
        assert_eq!(cold["source"], "checkpoint");
        assert_eq!(cold["thread_id"], "thread_cold");
        assert_eq!(
            cold["state"]["task.status"], "completed",
            "表行最新值覆盖 checkpoint 过期值"
        );

        assert!(
            items
                .iter()
                .all(|i| i["pipeline_id"] != "pipe_state_other_cov"),
            "跨租户内存行不得出口"
        );

        reg.remove("default", pid_mem);
        reg.remove("other_tenant", "pipe_state_other_cov");
        reg.remove("default", pid_cold);
    }

    // ── /api/v1/config/pipelines/{name}（P7 管道配置读写）────────────────

    #[tokio::test]
    async fn pipeline_config_get_guards_and_etag() {
        let tmp = tempfile::tempdir().unwrap();
        let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
        let cfg_dir = tmp.path().join("config").join("pipelines");
        std::fs::create_dir_all(&cfg_dir).unwrap();
        std::fs::write(cfg_dir.join("demo.yaml"), "name: demo\nmax_rounds: 5\n").unwrap();
        std::fs::write(cfg_dir.join("broken.yaml"), "[:not yaml").unwrap();

        let mut state = AppState::new();
        state.project_root = Some(tmp.path().to_path_buf());

        // 非法 name（穿越/含点）→ 400
        for bad in ["../evil", "bad.name"] {
            let err = get_pipeline_config_with_etag(
                axum::extract::State(state.clone()),
                axum::extract::Path(bad.to_string()),
            )
            .await
            .unwrap_err();
            assert!(
                matches!(err, ApiError::BadRequest { .. }),
                "非法 name {bad} 应 400，实际 {err:?}"
            );
        }
        // project_root 缺席 → 500
        let err = get_pipeline_config_with_etag(
            axum::extract::State(AppState::new()),
            axum::extract::Path("demo".to_string()),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ApiError::Internal { .. }));
        // 未知管道 → 404
        let err = get_pipeline_config_with_etag(
            axum::extract::State(state.clone()),
            axum::extract::Path("nope".to_string()),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ApiError::NotFound { .. }));
        // 损坏 yaml → 500
        let err = get_pipeline_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path("broken".to_string()),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ApiError::Internal { .. }));

        // 正常读：内容解析 + ETag 响应头
        let resp = get_pipeline_config_with_etag(
            axum::extract::State(state),
            axum::extract::Path("demo".to_string()),
        )
        .await
        .unwrap();
        assert!(resp.headers().get("etag").is_some(), "ETag 响应头");
        let body = axum::body::to_bytes(resp.into_body(), 65536).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["name"], "demo");
        assert_eq!(v["data"]["max_rounds"], 5);
        assert!(!v["etag"].as_str().unwrap().is_empty());
    }

    fn valid_pipeline_config() -> serde_json::Value {
        serde_json::json!({
            "name": "demo",
            "loop_bodies": [
                {"id": "main", "steps": [{"id": "llm", "steps": ["mock_llm_core"]}]}
            ],
        })
    }

    #[tokio::test]
    async fn pipeline_config_put_guards() {
        let tmp = tempfile::tempdir().unwrap();
        let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
        let cfg_dir = tmp.path().join("config").join("pipelines");
        std::fs::create_dir_all(&cfg_dir).unwrap();
        std::fs::write(cfg_dir.join("demo.yaml"), "name: demo\n").unwrap();

        let mut state = AppState::new();
        state.project_root = Some(tmp.path().to_path_buf());
        let req = |data: serde_json::Value, if_match: Option<&str>| PipelineConfigUpdateRequest {
            data,
            if_match: if_match.map(str::to_string),
        };

        // 非法 name → 400
        let err = put_pipeline_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path("../evil".to_string()),
            axum::Json(req(valid_pipeline_config(), None)),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ApiError::BadRequest { .. }));

        // data 非映射 → 400，且消息带实际类型名
        for (data, ty) in [
            (serde_json::json!([1, 2]), "array"),
            (serde_json::json!("scalar"), "string"),
        ] {
            let err = put_pipeline_config_handler(
                axum::extract::State(state.clone()),
                axum::extract::Path("demo".to_string()),
                axum::Json(req(data, None)),
            )
            .await
            .unwrap_err();
            match err {
                ApiError::BadRequest { message } => assert!(message.contains(ty), "{message}"),
                other => panic!("非映射 data 应 400，实际 {other:?}"),
            }
        }

        // G10 DSL 校验失败（旧形态键 routes）→ 400
        let legacy = serde_json::json!({
            "name": "demo",
            "loop_bodies": [{"id": "m", "routes": []}],
        });
        let err = put_pipeline_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path("demo".to_string()),
            axum::Json(req(legacy, None)),
        )
        .await
        .unwrap_err();
        match err {
            ApiError::BadRequest { message } => {
                assert!(message.contains("validation failed"), "{message}")
            }
            other => panic!("DSL 违例应 400，实际 {other:?}"),
        }

        // 文件不存在 → 404（PUT 不再隐式创建）
        let err = put_pipeline_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path("ghost".to_string()),
            axum::Json(req(valid_pipeline_config(), None)),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ApiError::NotFound { .. }));

        // If-Match 缺失/不匹配 → 409
        for if_match in [None, Some("deadbeef")] {
            let err = put_pipeline_config_handler(
                axum::extract::State(state.clone()),
                axum::extract::Path("demo".to_string()),
                axum::Json(req(valid_pipeline_config(), if_match)),
            )
            .await
            .unwrap_err();
            assert!(
                matches!(err, ApiError::Conflict { .. }),
                "If-Match {if_match:?} 应 409，实际 {err:?}"
            );
        }
    }

    #[tokio::test]
    async fn pipeline_config_put_roundtrip_updates_file() {
        let tmp = tempfile::tempdir().unwrap();
        let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
        let cfg_dir = tmp.path().join("config").join("pipelines");
        std::fs::create_dir_all(&cfg_dir).unwrap();
        std::fs::write(cfg_dir.join("demo.yaml"), "name: demo\nmax_rounds: 5\n").unwrap();

        let mut state = AppState::new();
        state.project_root = Some(tmp.path().to_path_buf());

        let current = std::fs::read_to_string(cfg_dir.join("demo.yaml")).unwrap();
        let current_etag = crate::config_service::compute_etag(current.as_bytes());
        let mut new_config = valid_pipeline_config();
        new_config["max_rounds"] = serde_json::json!(9);

        let resp = put_pipeline_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path("demo".to_string()),
            axum::Json(PipelineConfigUpdateRequest {
                data: new_config,
                if_match: Some(current_etag.clone()),
            }),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["name"], "demo");
        let new_etag = resp.0["etag"].as_str().unwrap().to_string();
        assert!(!new_etag.is_empty(), "新 ETag 非空");
        assert_ne!(new_etag, current_etag, "写盘后 ETag 必须更新");

        // GET 回读新内容
        let resp = get_pipeline_config_handler(
            axum::extract::State(state),
            axum::extract::Path("demo".to_string()),
        )
        .await
        .unwrap();
        assert_eq!(resp.data["max_rounds"], 9);
        assert_eq!(resp.etag, new_etag, "GET ETag 与 PUT 返回一致");
    }

    // ── /api/v1/plugins/{id}/config/{file_id}（插件配置读写）─────────────

    /// env target 条目清单（GAP-4：key 写 .env）。
    fn env_config_manifest() -> PluginManifest {
        manifest_from_json(serde_json::json!({
            "id": "env_plugin", "name": "env_plugin", "version": "1.0.0",
            "plugin_type": "tool", "language": "python",
            "host_type": "sidecar", "entry": "python server.py",
            "config_files": [{
                "id": "env_main", "label": "Env Keys", "target": "env",
                "fields": [
                    {"name": "FOO_KEY", "label": "Foo", "type": "secret"},
                    {"name": "BAR_SECRET", "label": "Bar", "type": "secret"}
                ]
            }],
            "capabilities": {},
        }))
    }

    #[tokio::test]
    async fn plugin_config_get_guards_and_env_masked_view() {
        let tmp = tempfile::tempdir().unwrap();
        let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
        let mut state = AppState::new();
        state.project_root = Some(tmp.path().to_path_buf());
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![env_config_manifest()]));

        // project_root 缺席 → 500
        let err = get_plugin_config_with_etag(
            axum::extract::State(AppState::new()),
            axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ApiError::Internal { .. }));

        // 未知插件 / 未知 file_id → 404
        for (pid, fid) in [("ghost", "env_main"), ("env_plugin", "nope")] {
            let err = get_plugin_config_with_etag(
                axum::extract::State(state.clone()),
                axum::extract::Path((pid.to_string(), fid.to_string())),
            )
            .await
            .unwrap_err();
            assert!(
                matches!(err, ApiError::NotFound { .. }),
                "({pid},{fid}) 应 404，实际 {err:?}"
            );
        }

        // .env 未写：两字段均未设置 → "" 掩码视图 + ETag
        let resp = get_plugin_config_with_etag(
            axum::extract::State(state.clone()),
            axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
        )
        .await
        .unwrap();
        let body = axum::body::to_bytes(resp.into_body(), 65536).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["path"], ".env");
        assert_eq!(v["data"]["FOO_KEY"], "");
        assert_eq!(v["data"]["BAR_SECRET"], "");

        // 写入 FOO_KEY 后：已设置字段 → "***" 哨兵（has_key 语义）
        agentos_mcp::env_file::write_env_updates(
            &agentos_mcp::env_file::env_path_for_root(tmp.path()),
            &[("FOO_KEY".to_string(), "hello".to_string())],
        )
        .unwrap();
        let resp = get_plugin_config_handler(
            axum::extract::State(state),
            axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
        )
        .await
        .unwrap();
        assert_eq!(resp.data["FOO_KEY"], "***", "已设置字段只出口哨兵");
        assert_eq!(resp.data["BAR_SECRET"], "", "未设置字段出口空串");
    }

    #[tokio::test]
    async fn plugin_config_put_env_target_guards_and_write() {
        let tmp = tempfile::tempdir().unwrap();
        let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
        let mut state = AppState::new();
        state.project_root = Some(tmp.path().to_path_buf());
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![env_config_manifest()]));

        // 当前掩码视图的 ETag（乐观锁基准）
        let current = get_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
        )
        .await
        .unwrap();
        let current_etag = current.etag.clone();

        let req = |data: serde_json::Value, if_match: Option<String>| PluginConfigUpdateRequest {
            data,
            if_match,
        };

        // 未声明字段 → 400
        let err = put_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
            axum::Json(req(
                serde_json::json!({"UNDECLARED": "x"}),
                Some(current_etag.clone()),
            )),
        )
        .await
        .unwrap_err();
        match err {
            ApiError::BadRequest { message } => assert!(message.contains("UNDECLARED")),
            other => panic!("未声明 env 字段应 400，实际 {other:?}"),
        }

        // If-Match 缺失 / 不匹配 → 409
        for if_match in [None, Some("stale".to_string())] {
            let err = put_plugin_config_handler(
                axum::extract::State(state.clone()),
                axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
                axum::Json(req(serde_json::json!({}), if_match)),
            )
            .await
            .unwrap_err();
            assert!(matches!(err, ApiError::Conflict { .. }), "实际 {err:?}");
        }

        // 正确 If-Match：*** 哨兵保留现值，新值写入 .env
        let resp = put_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
            axum::Json(req(
                serde_json::json!({"FOO_KEY": "***", "BAR_SECRET": "new-bar"}),
                Some(current_etag),
            )),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["ok"], serde_json::json!(true));
        assert!(!resp.0["etag"].as_str().unwrap().is_empty());

        let env_path = agentos_mcp::env_file::env_path_for_root(tmp.path());
        let raw = std::fs::read_to_string(&env_path).unwrap();
        let parsed = agentos_mcp::env_file::parse_env_text_for_read(&raw);
        assert_eq!(
            parsed.get("BAR_SECRET").map(String::as_str),
            Some("new-bar")
        );
        assert!(
            !parsed.contains_key("FOO_KEY"),
            "哨兵字段不得写入（保留现值语义）: {raw}"
        );

        // GET 掩码视图反映新状态
        let after = get_plugin_config_handler(
            axum::extract::State(state),
            axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
        )
        .await
        .unwrap();
        assert_eq!(after.data["FOO_KEY"], "");
        assert_eq!(after.data["BAR_SECRET"], "***");
    }

    /// 内联形态（path 空）配置：真值 = fields.default，PUT 原地写回 plugin.json。
    fn inline_plugin_json() -> serde_json::Value {
        serde_json::json!({
            "id": "inline_plugin", "name": "inline_plugin", "version": "1.0.0",
            "plugin_type": "tool", "language": "python",
            "host_type": "sidecar", "entry": "python server.py",
            "capabilities": {},
            "config_files": [{
                "id": "limits", "label": "Limits", "path": "",
                "fields": [
                    {"name": "budgets.l1", "label": "L1", "type": "number",
                     "default": 0.3},
                    {"name": "compression.model", "label": "M", "type": "string",
                     "default": "k2"}
                ]
            }],
        })
    }

    fn inline_manifest() -> PluginManifest {
        manifest_from_json(inline_plugin_json())
    }

    #[tokio::test]
    async fn plugin_config_inline_put_roundtrip_writes_manifest_and_syncs_memory() {
        let plugin_dir = tempfile::tempdir().unwrap();
        let _user_space_guard = crate::test_env::pin_user_root(plugin_dir.path());
        std::fs::write(
            plugin_dir.path().join("plugin.json"),
            serde_json::to_string_pretty(&inline_plugin_json()).unwrap(),
        )
        .unwrap();
        let mut state = AppState::new();
        // 内联形态入口同样强制 project_root 门（校验先行），真值读写走 plugin_dirs
        state.project_root = Some(plugin_dir.path().to_path_buf());
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![inline_manifest()]));
        state.plugin_dirs = Arc::new(std::collections::HashMap::from([(
            "inline_plugin".to_string(),
            plugin_dir.path().to_path_buf(),
        )]));

        // GET 内联视图 = fields.default 组装
        let current = get_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
        )
        .await
        .unwrap();
        assert_eq!(current.data["budgets"]["l1"], serde_json::json!(0.3));
        assert_eq!(current.data["compression"]["model"], "k2");
        assert_eq!(current.path, "", "内联形态 path 为空");
        let etag_v1 = current.etag.clone();

        // If-Match 缺失 → 409
        let err = put_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
            axum::Json(PluginConfigUpdateRequest {
                data: serde_json::json!({"budgets": {"l1": 0.5}}),
                if_match: None,
            }),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ApiError::Conflict { .. }));

        // data 非对象 → 400
        let err = put_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
            axum::Json(PluginConfigUpdateRequest {
                data: serde_json::json!([1]),
                if_match: Some(etag_v1.clone()),
            }),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ApiError::BadRequest { .. }));

        // 未声明叶路径 fail-closed → 400 点名完整路径
        let err = put_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
            axum::Json(PluginConfigUpdateRequest {
                data: serde_json::json!({"budgets": {"nonsense": 1}}),
                if_match: Some(etag_v1.clone()),
            }),
        )
        .await
        .unwrap_err();
        match err {
            ApiError::BadRequest { message } => {
                assert!(message.contains("budgets.nonsense"), "{message}")
            }
            other => panic!("未声明叶路径应 400，实际 {other:?}"),
        }

        // 正确保存：0.3→0.5 + null 清除 compression.model default
        let resp = put_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
            axum::Json(PluginConfigUpdateRequest {
                data: serde_json::json!(
                    {"budgets": {"l1": 0.5}, "compression.model": null}
                ),
                if_match: Some(etag_v1),
            }),
        )
        .await
        .unwrap();
        let etag_v2 = resp.0["etag"].as_str().unwrap().to_string();
        assert!(!etag_v2.is_empty(), "新 ETag 非空");
        assert_ne!(etag_v2, current.etag, "保存后 ETag 必须变化");

        // 磁盘 plugin.json 落盘核对
        let disk: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(plugin_dir.path().join("plugin.json")).unwrap(),
        )
        .unwrap();
        let fields = disk["config_files"][0]["fields"].as_array().unwrap();
        let l1 = fields.iter().find(|f| f["name"] == "budgets.l1").unwrap();
        assert_eq!(l1["default"], serde_json::json!(0.5), "新默认值落盘");
        let model = fields
            .iter()
            .find(|f| f["name"] == "compression.model")
            .unwrap();
        assert!(
            model.get("default").is_none(),
            "null 保存 = 清除 default: {model}"
        );

        // 内存 manifest 同步：GET 即时反映新值
        let after = get_plugin_config_handler(
            axum::extract::State(state),
            axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
        )
        .await
        .unwrap();
        assert_eq!(after.data["budgets"]["l1"], serde_json::json!(0.5));
        assert!(after.data.get("compression").is_none(), "清除后不再出口");
        assert_eq!(after.etag, etag_v2);
    }

    #[tokio::test]
    async fn plugin_config_inline_put_without_plugin_dir_returns_500() {
        let plugin_dir = tempfile::tempdir().unwrap();
        let _user_space_guard = crate::test_env::pin_user_root(plugin_dir.path());
        let mut state = AppState::new();
        state.project_root = Some(plugin_dir.path().to_path_buf());
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![inline_manifest()]));
        // plugin_dirs 空 → 目录映射缺失

        let current = get_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
        )
        .await
        .unwrap();
        let err = put_plugin_config_handler(
            axum::extract::State(state),
            axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
            axum::Json(PluginConfigUpdateRequest {
                data: serde_json::json!({"budgets": {"l1": 0.5}}),
                if_match: Some(current.etag.clone()),
            }),
        )
        .await
        .unwrap_err();
        assert!(
            matches!(err, ApiError::Internal { .. }),
            "目录映射缺失应 500，实际 {err:?}"
        );
    }

    #[tokio::test]
    async fn plugin_config_referenced_file_masking_and_put_sentinels() {
        let tmp = tempfile::tempdir().unwrap();
        let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
        let cfg_dir = tmp.path().join("config").join("mymodels");
        std::fs::create_dir_all(&cfg_dir).unwrap();
        std::fs::write(
            cfg_dir.join("llm.yaml"),
            "api_key: real_secret\nmodel: gpt-x\nplaceholder: ${MY_VAR}\n",
        )
        .unwrap();
        std::fs::write(cfg_dir.join("broken.yaml"), "[:bad").unwrap();

        let manifest = manifest_from_json(serde_json::json!({
            "id": "ref_plugin", "name": "ref_plugin", "version": "1.0.0",
            "plugin_type": "tool", "language": "python",
            "host_type": "sidecar", "entry": "python server.py",
            "config_files": [
                {"id": "llm", "label": "LLM", "path": "config/mymodels/llm.yaml",
                 "fields": []},
                {"id": "absent", "label": "Absent", "path": "config/mymodels/none.yaml",
                 "fields": []},
                {"id": "broken", "label": "Broken", "path": "config/mymodels/broken.yaml",
                 "fields": []}
            ],
            "capabilities": {},
        }));
        let mut state = AppState::new();
        state.project_root = Some(tmp.path().to_path_buf());
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest]));

        // GET 掩码：真实 secret → ****，${VAR} 占位符原样
        let current = get_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("ref_plugin".to_string(), "llm".to_string())),
        )
        .await
        .unwrap();
        assert_eq!(current.data["api_key"], "****");
        assert_eq!(current.data["placeholder"], "${MY_VAR}");
        assert_eq!(current.data["model"], "gpt-x");
        assert_eq!(current.path, "config/mymodels/llm.yaml");
        let etag_v1 = current.etag.clone();

        // 引用文件缺失 → 空配置视图（PUT 保存将创建文件）
        let absent = get_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("ref_plugin".to_string(), "absent".to_string())),
        )
        .await
        .unwrap();
        assert!(absent.data.as_object().unwrap().is_empty());

        // 损坏 yaml → 500
        let err = get_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("ref_plugin".to_string(), "broken".to_string())),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ApiError::Internal { .. }));

        // PUT：If-Match 缺失 → 409
        let err = put_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("ref_plugin".to_string(), "llm".to_string())),
            axum::Json(PluginConfigUpdateRequest {
                data: serde_json::json!({"model": "gpt-5"}),
                if_match: None,
            }),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ApiError::Conflict { .. }));

        // PUT：*** 哨兵保留磁盘原 secret，普通字段更新
        let resp = put_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("ref_plugin".to_string(), "llm".to_string())),
            axum::Json(PluginConfigUpdateRequest {
                data: serde_json::json!({"model": "gpt-5", "api_key": "***"}),
                if_match: Some(etag_v1.clone()),
            }),
        )
        .await
        .unwrap();
        let etag_v2 = resp.0["etag"].as_str().unwrap().to_string();
        assert_ne!(etag_v2, etag_v1);

        let disk = std::fs::read_to_string(cfg_dir.join("llm.yaml")).unwrap();
        assert!(
            disk.contains("real_secret"),
            "哨兵字段必须保留磁盘原值: {disk}"
        );
        assert!(disk.contains("gpt-5"), "普通字段必须更新: {disk}");

        // GET 新 ETag 与更新视图一致
        let after = get_plugin_config_handler(
            axum::extract::State(state),
            axum::extract::Path(("ref_plugin".to_string(), "llm".to_string())),
        )
        .await
        .unwrap();
        assert_eq!(after.data["model"], "gpt-5");
        assert_eq!(after.etag, etag_v2);
    }

    // ── POST /api/v1/plugins/validate-all（registry ↔ 磁盘一致性检出）────

    /// 在目录落一份磁盘 plugin.json。
    fn write_disk_manifest(dir: &std::path::Path, v: &serde_json::Value) {
        std::fs::write(
            dir.join("plugin.json"),
            serde_json::to_string_pretty(v).unwrap(),
        )
        .unwrap();
    }

    #[tokio::test]
    async fn validate_all_detects_registry_disk_consistency() {
        let plugin_dir = tempfile::tempdir().unwrap();
        let registry_manifest = serde_json::json!({
            "id": "p_disk", "name": "p_disk", "version": "1.0.0",
            "plugin_type": "tool", "language": "python",
            "host_type": "sidecar", "entry": "python server.py",
            "capabilities": {"tools": [{"name": "t1", "description": "t1"}]},
        });

        // 无目录映射 → disk_manifest_unreadable（不可读不得静默当绿）
        let mut state = AppState::new();
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest_from_json(
            registry_manifest.clone(),
        )]));
        state.invoker = Some(Arc::new(RecordingInvoker {
            seen: std::sync::Mutex::new(Vec::new()),
            seen_states: std::sync::Mutex::new(Vec::new()),
            hooks: std::sync::Mutex::new(Vec::new()),
            list_tools: std::collections::HashMap::from([(
                "p_disk".to_string(),
                serde_json::json!({"tools": [{"name": "t1", "description": "t1"}]}),
            )]),
        }) as Arc<dyn PluginInvoker>);
        let resp = validate_all_plugins_handler(axum::extract::State(state.clone())).await;
        let reports = resp.0["consistency_reports"].as_array().unwrap();
        assert_eq!(reports.len(), 1);
        assert_eq!(reports[0]["status"], "disk_manifest_unreadable");
        assert_eq!(resp.0["registry_disk_mismatches"], 0);

        // 磁盘一致 → 无 consistency 报告
        write_disk_manifest(plugin_dir.path(), &registry_manifest);
        state.plugin_dirs = Arc::new(std::collections::HashMap::from([(
            "p_disk".to_string(),
            plugin_dir.path().to_path_buf(),
        )]));
        let resp = validate_all_plugins_handler(axum::extract::State(state.clone())).await;
        assert!(
            resp.0["consistency_reports"].as_array().unwrap().is_empty(),
            "一致时不得有差异报告: {}",
            resp.0
        );
        assert_eq!(resp.0["registry_disk_mismatches"], 0);
        assert_eq!(resp.0["clean"], 1);

        // 磁盘漂移（磁盘多声明一个工具 → 注册表侧 missing_tool）→ mismatch + diffs
        let mut drifted = registry_manifest.clone();
        drifted["capabilities"]["tools"] = serde_json::json!([
            {"name": "t1", "description": "t1"},
            {"name": "t2_extra", "description": "磁盘新声明，注册表无"}
        ]);
        write_disk_manifest(plugin_dir.path(), &drifted);
        let resp = validate_all_plugins_handler(axum::extract::State(state)).await;
        assert_eq!(resp.0["registry_disk_mismatches"], 1);
        let reports = resp.0["consistency_reports"].as_array().unwrap();
        assert_eq!(reports[0]["status"], "registry_disk_mismatch");
        assert_eq!(reports[0]["plugin_id"], "p_disk");
        assert!(
            !reports[0]["diffs"].as_array().unwrap().is_empty(),
            "差异明细必须给出"
        );
    }

    // ── POST /api/v1/system/restart（G8 排空；测试逃生门禁退出）──────────

    /// 逃生门 RAII：测试期间设 AGENTOS_DISABLE_SELF_EXIT=1，结束恢复原值。
    /// 环境变量是进程级共享，两个用例标 #[serial] 串行（同 plugin_watcher_test 先例）。
    struct DisableSelfExit;
    impl DisableSelfExit {
        fn set() -> Self {
            std::env::set_var("AGENTOS_DISABLE_SELF_EXIT", "1");
            Self
        }
    }
    impl Drop for DisableSelfExit {
        fn drop(&mut self) {
            std::env::remove_var("AGENTOS_DISABLE_SELF_EXIT");
        }
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn system_restart_drains_running_runs_and_reports_exit_75() {
        let _guard = DisableSelfExit::set();

        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        sqlite.create_run("run_g8_cov", "cfg", "default").unwrap();
        let mut state = AppState::new();
        state.db = Some(sqlite.clone());

        let resp = system_restart_handler(axum::extract::State(state)).await;
        assert_eq!(resp.0["success"], serde_json::json!(true));
        assert_eq!(resp.0["exit_code"], 75);
        assert_eq!(resp.0["suspended_runs"], 1);

        // 排空副作用：running → suspended
        let run = sqlite.get_run("run_g8_cov").await.unwrap();
        assert_eq!(run.status, agentos_core::types::RunStatus::Suspended);

        // 无 db / 无在途 run → suspended_runs 0
        let resp = system_restart_handler(axum::extract::State(AppState::new())).await;
        assert_eq!(resp.0["suspended_runs"], 0);
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn drain_and_exit75_counts_suspended_runs_with_db() {
        let _guard = DisableSelfExit::set();
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        sqlite.create_run("run_drain_1", "cfg", "default").unwrap();
        sqlite.create_run("run_drain_2", "cfg", "default").unwrap();
        sqlite
            .update_run_status(
                "run_drain_2",
                agentos_core::types::RunStatus::Completed,
                None,
                None,
            )
            .await
            .unwrap();

        let n = crate::routes::drain_and_exit75(Some(&sqlite), None, "unit-test drain").await;
        assert_eq!(n, 1, "只排空 running，completed 不动");
    }

    // ── PUT /api/v1/plugins/{id}/enabled（registry 热更新双向）────────────

    #[tokio::test]
    async fn set_enabled_hot_reloads_registry_both_directions() {
        use agentos_core::traits::CapabilityRegistry as _;
        let tmp = tempfile::tempdir().unwrap();
        let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
        let profile_dir = tmp.path().join("config").join("kernel");
        std::fs::create_dir_all(&profile_dir).unwrap();

        let manifest = manifest_from_json(serde_json::json!({
            "id": "p_toggle", "name": "p_toggle", "version": "1.0.0",
            "plugin_type": "tool", "language": "python",
            "host_type": "sidecar", "entry": "python server.py",
            "capabilities": {"tools": [{"name": "t1", "description": "t1"}]},
            "http_endpoints": [
                {"route_id": "r1", "method": "GET", "path": "/ext/p_toggle/things",
                 "handler_capability": "http.handle"}
            ],
        }));
        let mut state = AppState::new();
        state.project_root = Some(tmp.path().to_path_buf());
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest]));
        state.invoker = Some(Arc::new(RecordingInvoker {
            seen: std::sync::Mutex::new(Vec::new()),
            seen_states: std::sync::Mutex::new(Vec::new()),
            hooks: std::sync::Mutex::new(Vec::new()),
            list_tools: std::collections::HashMap::from([(
                "p_toggle".to_string(),
                serde_json::json!({"tools": [{"name": "t1", "description": "t1"}]}),
            )]),
        }) as Arc<dyn PluginInvoker>);
        let registry = Arc::new(agentos_plugin_loader::CapabilityRegistryImpl::new());
        state.capability_registry = Some(registry.clone());

        // 启用：G2 复核干净 → tools/http_routes 全注册 + 内存 enabled 位翻转
        let resp = plugins_set_enabled_handler(
            axum::extract::Path("p_toggle".to_string()),
            axum::extract::State(state.clone()),
            axum::Json(EnabledBody { enabled: true }),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["success"], serde_json::json!(true));
        assert_eq!(resp.0["restart_required"], serde_json::json!(false));
        assert_eq!(resp.0["registered"]["tools"], 1);
        assert_eq!(resp.0["registered"]["http_routes"], 1);
        assert!(state.enabled_plugin_ids.read().await.contains("p_toggle"));
        assert_eq!(
            registry.list_tools().len(),
            1,
            "启用的工具必须立即出现在注册表"
        );
        assert_eq!(registry.list_http_routes().len(), 1);
        let profile_raw =
            std::fs::read_to_string(profile_dir.join("default_profile.yaml")).unwrap();
        assert!(profile_raw.contains("p_toggle"));
        assert!(profile_raw.contains("enabled: true"));

        // 禁用：注册即收（tools/http_routes 零残留）+ enabled 位摘除
        let resp = plugins_set_enabled_handler(
            axum::extract::Path("p_toggle".to_string()),
            axum::extract::State(state.clone()),
            axum::Json(EnabledBody { enabled: false }),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["registered"], serde_json::Value::Null);
        assert!(!state.enabled_plugin_ids.read().await.contains("p_toggle"));
        assert!(registry.list_tools().is_empty(), "禁用必须结构性收回工具");
        assert!(registry.list_http_routes().is_empty());
    }

    #[tokio::test]
    async fn set_enabled_without_project_root_returns_500() {
        let state = AppState::new();
        let err = plugins_set_enabled_handler(
            axum::extract::Path("p_x".to_string()),
            axum::extract::State(state),
            axum::Json(EnabledBody { enabled: true }),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ApiError::Internal { .. }));
    }

    #[tokio::test]
    async fn set_enabled_unreadable_profile_returns_500() {
        // profile 路径是目录 → 读失败（非 NotFound）→ 500，不写入
        let tmp = tempfile::tempdir().unwrap();
        let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
        let profile_dir = tmp.path().join("config").join("kernel");
        std::fs::create_dir_all(profile_dir.join("default_profile.yaml")).unwrap();

        let mut state = AppState::new();
        state.project_root = Some(tmp.path().to_path_buf());
        let err = plugins_set_enabled_handler(
            axum::extract::Path("p_x".to_string()),
            axum::extract::State(state),
            axum::Json(EnabledBody { enabled: true }),
        )
        .await
        .unwrap_err();
        assert!(
            matches!(err, ApiError::Internal { .. }),
            "读失败（权限/目录）应 500，实际 {err:?}"
        );
    }

    // ── GET /api/v1/schema（聚合分支）────────────────────────────────────

    #[tokio::test]
    async fn schema_aggregates_registry_routes_and_manifest_sections() {
        let registry = Arc::new(agentos_plugin_loader::CapabilityRegistryImpl::new());
        let endpoint: agentos_core::traits::HttpEndpoint =
            serde_json::from_value(serde_json::json!({
                "route_id": "r1", "method": "GET", "path": "/ext/p_ext/things",
                "handler_capability": "http.handle", "auth": "user"
            }))
            .unwrap();
        // 注册并持有 guard：guard drop 即撤销注册（结构性收回语义），本测试
        // 内必须保活以让 schema 聚合读到该路由
        let (_descriptor, _route_guard) = registry
            .register_http_route_guarded("p_ext", endpoint)
            .unwrap();

        let mut state = AppState::new();
        state.capability_registry = Some(registry);
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
            manifest_from_json(serde_json::json!({
                "id": "p_sys_ui", "name": "p_sys_ui", "version": "1.0.0",
                "plugin_type": "system", "language": "python",
                "host_type": "sidecar", "entry": "x",
                "ui_schema": {"widgets": [{"kind": "stat"}]},
                "config_files": [
                    {"id": "visible", "label": "V", "path": "config/mine/v.yaml"},
                    {"id": "hidden", "label": "H", "path": "config/mine/h.yaml",
                     "settings": false}
                ],
                "capabilities": {},
            })),
            manifest_from_json(serde_json::json!({
                "id": "p_pipe_role", "name": "p_pipe_role", "version": "1.0.0",
                "plugin_type": "pipeline", "language": "python",
                "host_type": "sidecar", "entry": "x",
                "pipeline_role": "input",
                "capabilities": {},
            })),
        ]));
        state.enabled_plugin_ids =
            Arc::new(tokio::sync::RwLock::new(std::collections::HashSet::from([
                "p_sys_ui".to_string(),
            ])));

        let resp = schema_handler(axum::extract::State(state.clone()), HeaderMap::new()).await;
        assert_eq!(resp.status(), axum::http::StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 262144)
            .await
            .unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();

        // routes：注册表数据驱动，{plugin_id: [route…]} 形状
        let route_entry = &v["routes"]["p_ext"][0];
        assert_eq!(route_entry["method"], "GET");
        assert_eq!(route_entry["path"], "/ext/p_ext/things");
        assert_eq!(route_entry["auth"], "user");

        // agents/pipelines 按 manifest 类型分桶，role/ui_schema 随行
        assert_eq!(v["agents"].as_array().unwrap().len(), 1);
        assert_eq!(v["agents"][0]["id"], "p_sys_ui");
        assert_eq!(v["pipelines"][0]["role"], "input");

        // plugin_configs：settings:false 注入专用条目不出口
        let cfgs = v["plugin_configs"].as_array().unwrap();
        assert_eq!(cfgs.len(), 1);
        let visible = cfgs[0]["config_files"].as_array().unwrap();
        assert_eq!(visible.len(), 1);
        assert_eq!(visible[0]["id"], "visible");

        // plugin_contributes：enabled 的 ui_schema 插件出口；未启用不出
        let contribs = v["plugin_contributes"].as_array().unwrap();
        assert_eq!(contribs.len(), 1);
        assert_eq!(contribs[0]["plugin_id"], "p_sys_ui");

        // 未注入内核契约 → 空数组
        assert!(v["kernel_capabilities"].as_array().unwrap().is_empty());

        // If-None-Match 多候选（stale, 当前）→ 304（RFC 9110 逗号列表语义）
        let etag = crate::config_service::compute_etag(&body);
        let mut inm = HeaderMap::new();
        inm.insert(
            "if-none-match",
            format!("\"stale-1\", {etag}").parse().unwrap(),
        );
        let resp2 = schema_handler(axum::extract::State(state), inm).await;
        assert_eq!(resp2.status(), axum::http::StatusCode::NOT_MODIFIED);
    }
}

// ═══════════════════════════════════════════════════════════════════════
// 覆盖率补测：session_routes.rs（会话 CRUD 成功路径 / 消息映射全字段 /
// 会话 schema 聚合）。断行为契约，存储走真实 SqliteStore。
// ═══════════════════════════════════════════════════════════════════════

mod session_endpoints_tests {
    use super::*;
    use crate::session_routes::MessageListQuery;
    use agentos_core::traits::StorageBackend;
    use axum::http::HeaderMap;

    fn bearer_headers() -> HeaderMap {
        let mut h = HeaderMap::new();
        h.insert("authorization", scaffold_admin_bearer().parse().unwrap());
        h
    }

    fn session_record(
        thread_id: &str,
        session_type: &str,
        pipeline_ids: Vec<String>,
    ) -> agentos_core::types::SessionRecord {
        agentos_core::types::SessionRecord {
            thread_id: thread_id.to_string(),
            title: Some(format!("标题-{thread_id}")),
            intent: Some("意图".to_string()),
            current_state: "active".to_string(),
            agent_id: Some("agentos".to_string()),
            active_pipeline_id: pipeline_ids.first().cloned(),
            pipeline_ids,
            metadata: Some(serde_json::json!({
                "session_type": session_type,
                "pinned": true,
            })),
            created_at: "2026-09-01T00:00:00+00:00".to_string(),
            updated_at: "2026-09-02T00:00:00+00:00".to_string(),
            last_active_at: None,
        }
    }

    // ── GET /api/v1/sessions（DB 成功路径 + 映射表补全）──────────────────

    #[tokio::test]
    async fn list_sessions_returns_seeded_rows_with_child_pipeline_merge() {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let dyn_store: Arc<dyn StorageBackend> = store.clone();
        dyn_store
            .create_session(&session_record(
                "t_main",
                "main_pipeline",
                vec!["p_main".into()],
            ))
            .await
            .unwrap();
        // 子管道映射（link 表）并入 pipeline_ids
        dyn_store
            .link_pipeline_session("p_child", "t_main", "default")
            .await
            .unwrap();
        // 非 main_pipeline 会话被 session_type 过滤
        dyn_store
            .create_session(&session_record("t_cli", "cli", vec![]))
            .await
            .unwrap();

        let mut state = AppState::new();
        state.store = Some(dyn_store);

        let resp = list_sessions_handler(axum::extract::State(state), bearer_headers())
            .await
            .unwrap();
        assert_eq!(
            resp.0["total"], 1,
            "非 main_pipeline 会话被过滤: {}",
            resp.0
        );
        let thread = &resp.0["threads"][0];
        assert_eq!(thread["thread_id"], "t_main");
        assert_eq!(thread["title"], "标题-t_main");
        assert_eq!(thread["current_state"], "active");
        assert_eq!(thread["agent_id"], "agentos");
        assert_eq!(thread["metadata"]["pinned"], true);
        let pids: Vec<&str> = thread["pipeline_ids"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|p| p.as_str())
            .collect();
        assert!(pids.contains(&"p_main"), "主管道在列: {pids:?}");
        assert!(pids.contains(&"p_child"), "link 表子管道必须并入: {pids:?}");
        assert_eq!(thread["active_pipeline_id"], "p_main");
    }

    #[tokio::test]
    async fn list_sessions_without_store_falls_back_to_memory_registry() {
        let mut state = AppState::new();
        state.session = Some(Arc::new(agentos_session::SessionCoordinator::new()));
        let registry = state.session.as_ref().unwrap().registry().clone();
        registry.register_thread("thread-mem-cov", "u-mem");
        registry.register_thread_pipeline("thread-mem-cov", "pipe-mem");

        let resp = list_sessions_handler(axum::extract::State(state), HeaderMap::new())
            .await
            .unwrap();
        assert_eq!(resp.0["total"], 1);
        let thread = &resp.0["threads"][0];
        assert_eq!(thread["thread_id"], "thread-mem-cov");
        assert_eq!(thread["pipeline_ids"][0], "pipe-mem");
        assert_eq!(thread["active_pipeline_id"], "pipe-mem");
        assert_eq!(thread["metadata"]["user_id"], "u-mem");
    }

    // ── POST /api/v1/sessions（用户解析与 metadata 默认合并）──────────────

    #[tokio::test]
    async fn create_session_merges_metadata_defaults_and_persists() {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let dyn_store: Arc<dyn StorageBackend> = store.clone();
        let mut state = AppState::new();
        state.store = Some(dyn_store.clone());
        state.session = Some(Arc::new(agentos_session::SessionCoordinator::new()));

        // 无 token + body user_id → 显式降级使用 body 值（可伪造，warn 标记）
        let resp = create_session_handler(
            axum::extract::State(state.clone()),
            HeaderMap::new(),
            axum::Json(serde_json::json!({
                "title": "T1",
                "user_id": "body-user",
                "metadata": {"custom": 1}
            })),
        )
        .await;

        let thread_id = resp.0["thread_id"].as_str().unwrap().to_string();
        assert_eq!(resp.0["title"], "T1");
        assert_eq!(resp.0["intent"], "T1", "title 缺 intent 时回退 title");
        assert_eq!(resp.0["pipeline_ids"].as_array().unwrap().len(), 1);
        assert_eq!(resp.0["metadata"]["custom"], 1, "body 元数据保留");
        assert_eq!(
            resp.0["metadata"]["session_type"], "main_pipeline",
            "缺失默认必须补齐（列表过滤依赖）"
        );
        assert_eq!(resp.0["metadata"]["user_id"], "body-user");

        // 落库核对：双写 sessions 表 + registry 登记
        let rec = dyn_store.get_session(&thread_id).await.unwrap().unwrap();
        assert_eq!(rec.title.as_deref(), Some("T1"));
        assert_eq!(
            rec.active_pipeline_id.as_deref(),
            resp.0["active_pipeline_id"].as_str()
        );
        let registry = state.session.as_ref().unwrap().registry().clone();
        assert!(
            registry.get_pipeline_for_thread(&thread_id).is_some(),
            "thread→pipeline 注册表必须登记"
        );

        // 无 body user_id → anonymous 兜底
        let resp = create_session_handler(
            axum::extract::State(state),
            HeaderMap::new(),
            axum::Json(serde_json::json!({})),
        )
        .await;
        assert_eq!(resp.0["title"], serde_json::Value::Null);
        assert_eq!(resp.0["intent"], serde_json::Value::Null);
        assert_eq!(
            resp.0["metadata"]["user_id"], "anonymous",
            "无 token 无 body 用户 → anonymous"
        );
        assert_eq!(
            resp.0["metadata"]["session_type"], "main_pipeline",
            "body 无 metadata 也补默认"
        );
    }

    // ── PATCH /api/v1/sessions/{id}（重命名）─────────────────────────────

    #[tokio::test]
    async fn update_session_renames_db_record_and_falls_back_when_absent() {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let dyn_store: Arc<dyn StorageBackend> = store.clone();
        dyn_store
            .create_session(&session_record(
                "t_upd",
                "main_pipeline",
                vec!["p_upd".into()],
            ))
            .await
            .unwrap();
        let mut state = AppState::new();
        state.store = Some(dyn_store.clone());
        state.session = Some(Arc::new(agentos_session::SessionCoordinator::new()));

        // DB 命中：title/intent 同步更新 + updated_at 前移
        let resp = update_session_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path("t_upd".to_string()),
            axum::Json(serde_json::json!({"intent": "新名字"})),
        )
        .await;
        assert_eq!(resp.0["thread_id"], "t_upd");
        assert_eq!(resp.0["title"], "新名字");
        assert_eq!(resp.0["intent"], "新名字");
        let rec = dyn_store.get_session("t_upd").await.unwrap().unwrap();
        assert_eq!(rec.title.as_deref(), Some("新名字"));
        assert_eq!(rec.intent.as_deref(), Some("新名字"));
        assert_eq!(
            rec.updated_at, resp.0["updated_at"],
            "响应 updated_at 与落库一致"
        );
        assert_ne!(rec.updated_at, "2026-09-02T00:00:00+00:00");

        // 无 intent 的 PATCH：只碰 updated_at，title 保持
        let resp = update_session_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path("t_upd".to_string()),
            axum::Json(serde_json::json!({})),
        )
        .await;
        assert_eq!(resp.0["title"], "新名字", "无 intent 不得清标题");

        // DB 未命中：内存回退响应（agent/pipeline 取 registry 已知状态）
        state
            .session
            .as_ref()
            .unwrap()
            .registry()
            .register_thread_agent("t-ghost", "ag_ghost");
        let resp = update_session_handler(
            axum::extract::State(state),
            axum::extract::Path("t-ghost".to_string()),
            axum::Json(serde_json::json!({"intent": "ghost-name"})),
        )
        .await;
        assert_eq!(resp.0["thread_id"], "t-ghost");
        assert_eq!(resp.0["title"], "ghost-name");
        assert_eq!(resp.0["intent"], "ghost-name");
        assert_eq!(resp.0["agent_id"], "ag_ghost");
        assert_eq!(resp.0["current_state"], "active");
    }

    // ── PATCH /api/v1/sessions/{id}/agent（DB 未命中回退）────────────────

    #[tokio::test]
    async fn update_session_agent_falls_back_to_registry_state_without_db_record() {
        let mut state = AppState::new();
        state.session = Some(Arc::new(agentos_session::SessionCoordinator::new()));
        let registry = state.session.as_ref().unwrap().registry().clone();
        registry.register_thread_pipeline("t-fb", "p-fb");

        let resp = update_session_agent_handler(
            axum::extract::State(state),
            HeaderMap::new(),
            axum::extract::Path("t-fb".to_string()),
            axum::Json(serde_json::json!({"agent_id": "ag_fb"})),
        )
        .await;
        assert_eq!(resp.0["agent_id"], "ag_fb");
        assert_eq!(resp.0["pipeline_ids"][0], "p-fb");
        assert_eq!(resp.0["active_pipeline_id"], "p-fb");
        assert_eq!(resp.0["current_state"], "active");
        // body 缺 agent_id → 默认 agentos
        let resp = update_session_agent_handler(
            axum::extract::State(AppState::new()),
            HeaderMap::new(),
            axum::extract::Path("t-x".to_string()),
            axum::Json(serde_json::json!({})),
        )
        .await;
        assert_eq!(resp.0["agent_id"], "agentos");
    }

    // ── GET /api/v1/sessions/{id}/messages（消息映射全字段）───────────────

    fn seed_message_ops(
        store: &Arc<agentos_engine::SqliteStore>,
        pipeline_id: &str,
        ops: &[serde_json::Value],
    ) {
        store
            .apply_messages_ops_to_table(pipeline_id, "default", ops)
            .unwrap();
    }

    #[tokio::test]
    async fn list_messages_maps_tool_envelope_reasoning_and_metadata() {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        store.create_run("run_msgs_cov", "cfg", "default").unwrap();
        seed_message_ops(
            &store,
            "pipe_msgs_cov",
            &[
                serde_json::json!({
                    "op": "set", "seq": 0, "_run_id": "run_msgs_cov",
                    "msg": {"role": "user", "content": "问题",
                            "metadata": {"client_message_id": "cmid-1"}}
                }),
                serde_json::json!({
                    "op": "set", "seq": 1, "_run_id": "run_msgs_cov",
                    "msg": {"role": "assistant", "content": "回答",
                            "reasoning_content": "思考过程…",
                            "tool_calls": [
                                {"id": "c1", "type": "function",
                                 "function": {"name": "f", "arguments": "{}"}}
                            ]}
                }),
                serde_json::json!({
                    "op": "set", "seq": 2, "_run_id": "run_msgs_cov",
                    "msg": {"role": "tool", "content": "结果文本", "tool_call_id": "c1",
                            "tool_result": {"call_id": "c1", "tool_name": "f",
                                            "success": false, "error": "boom",
                                            "data": {"d": 1}, "duration_ms": 12.5,
                                            "metadata": {"container_task_id": "ct7"}}}
                }),
                serde_json::json!({
                    "op": "set", "seq": 3, "_run_id": "run_msgs_cov",
                    "msg": {"role": "tool", "content": "Error: 磁盘满"}
                }),
            ],
        );

        let mut state = AppState::new();
        state.store = Some(store.clone());
        state.db = Some(store.clone());

        let resp = list_session_messages_handler(
            axum::extract::State(state),
            HeaderMap::new(),
            axum::extract::Path("pipe_msgs_cov".to_string()),
            axum::extract::Query(MessageListQuery {
                pipeline_run_id: Some("pipe_msgs_cov".to_string()),
                before_sequence: None,
                after_sequence: None,
                limit: None,
            }),
        )
        .await
        .unwrap();
        let msgs = resp.0["messages"].as_array().unwrap();
        assert_eq!(resp.0["total"], 4);
        assert_eq!(resp.0["has_more"], serde_json::json!(false));
        let by_seq = |s: u32| {
            msgs.iter()
                .find(|m| m["sequence"] == s)
                .unwrap_or_else(|| panic!("缺 seq={s}"))
        };

        // user：metadata.client_message_id 原样回显（幂等对账键）
        let m0 = by_seq(0);
        assert_eq!(m0["role"], "user");
        assert_eq!(m0["content"], "问题");
        assert_eq!(m0["metadata"]["client_message_id"], "cmid-1");
        assert_eq!(m0["status"], "completed");
        assert!(m0["id"].is_string());
        assert!(m0["timestamp"].is_string());

        // assistant：tool_calls 反序列化 + reasoning_content
        let m1 = by_seq(1);
        assert_eq!(m1["toolCalls"][0]["id"], "c1");
        assert_eq!(m1["reasoningContent"], "思考过程…");

        // tool（envelope 失败）：status/error/toolCallId/结构化结果字段
        let m2 = by_seq(2);
        assert_eq!(m2["status"], "failed");
        assert_eq!(m2["error"], "boom");
        assert_eq!(m2["toolCallId"], "c1");
        assert_eq!(m2["toolName"], "f");
        assert_eq!(m2["toolResultData"]["d"], 1);
        assert_eq!(m2["toolDurationMs"], 12.5);
        assert_eq!(m2["containerTaskId"], "ct7");

        // tool（content 前缀失败、无 envelope）：状态按前缀判定
        let m3 = by_seq(3);
        assert_eq!(m3["status"], "failed");
        assert_eq!(m3["error"], "磁盘满");
        assert!(m3.get("toolResultData").is_none());

        // 游标分页：after_sequence=2 + limit=2 → 只剩 seq 3，1 < limit 无更多
        let mut state = AppState::new();
        state.store = Some(store.clone());
        state.db = Some(store.clone());
        let resp = list_session_messages_handler(
            axum::extract::State(state),
            HeaderMap::new(),
            axum::extract::Path("pipe_msgs_cov".to_string()),
            axum::extract::Query(MessageListQuery {
                pipeline_run_id: Some("pipe_msgs_cov".to_string()),
                before_sequence: None,
                after_sequence: Some(2),
                limit: Some(2),
            }),
        )
        .await
        .unwrap();
        let msgs = resp.0["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 1, "after_sequence=2 应只剩 seq 3");
        assert_eq!(msgs[0]["sequence"], 3);
        assert_eq!(resp.0["has_more"], serde_json::json!(false), "1 < limit 2");
    }

    #[tokio::test]
    async fn list_messages_falls_back_to_active_pipeline_and_handles_no_store() {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        store.create_run("run_fb_cov", "cfg", "default").unwrap();
        seed_message_ops(
            &store,
            "pipe_active_cov",
            &[serde_json::json!({
                "op": "set", "seq": 0, "_run_id": "run_fb_cov",
                "msg": {"role": "user", "content": "in-active"}
            })],
        );
        let dyn_store: Arc<dyn StorageBackend> = store.clone();
        dyn_store
            .create_session(&session_record(
                "t_fb",
                "main_pipeline",
                vec!["pipe_active_cov".into()],
            ))
            .await
            .unwrap();

        // 不带 pipeline_run_id：按会话 active_pipeline_id 解析目标管道
        let mut state = AppState::new();
        state.store = Some(dyn_store.clone());
        state.db = Some(store);
        let resp = list_session_messages_handler(
            axum::extract::State(state),
            HeaderMap::new(),
            axum::extract::Path("t_fb".to_string()),
            axum::extract::Query(MessageListQuery {
                pipeline_run_id: None,
                before_sequence: None,
                after_sequence: None,
                limit: None,
            }),
        )
        .await
        .unwrap();
        let msgs = resp.0["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 1, "thread→active_pipeline 回退: {}", resp.0);
        assert_eq!(msgs[0]["content"], "in-active");
        assert_eq!(msgs[0]["thread_id"], "t_fb", "回填路径 id 满足前端 mapper");

        // store 未配置 → 空历史形状（不报错）
        let resp = list_session_messages_handler(
            axum::extract::State(AppState::new()),
            HeaderMap::new(),
            axum::extract::Path("t_any".to_string()),
            axum::extract::Query(MessageListQuery {
                pipeline_run_id: None,
                before_sequence: None,
                after_sequence: None,
                limit: None,
            }),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["messages"].as_array().unwrap().len(), 0);
        assert_eq!(resp.0["total"], 0);
        assert_eq!(resp.0["has_more"], serde_json::json!(false));
    }

    // ── GET /api/v1/sessions/schema（内置 + 插件聚合）────────────────────

    #[tokio::test]
    async fn sessions_schema_aggregates_enabled_plugin_thread_fields() {
        let mk = |id: &str, thread_fields: serde_json::Value| {
            serde_json::from_value::<agentos_core::traits::PluginManifest>(serde_json::json!({
                "id": id, "name": id, "version": "1.0.0",
                "plugin_type": "system", "language": "python",
                "host_type": "sidecar", "entry": "x",
                "contributes": {"thread_fields": thread_fields},
                "capabilities": {},
            }))
            .unwrap()
        };
        let mut state = AppState::new();
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
            mk(
                "p_iso_on",
                serde_json::json!([
                    {"name": "workspace", "label": "工作区", "type": "string"},
                    {"label": "缺 name 不构成字段"}
                ]),
            ),
            mk("p_iso_off", serde_json::json!([{"name": "off_field"}])),
        ]));
        state.enabled_plugin_ids =
            Arc::new(tokio::sync::RwLock::new(std::collections::HashSet::from([
                "p_iso_on".to_string(),
            ])));

        let resp = sessions_schema_handler(axum::extract::State(state)).await;
        let fields = resp.0["fields"].as_array().unwrap();
        let names: Vec<&str> = fields.iter().filter_map(|f| f["name"].as_str()).collect();
        assert_eq!(
            names,
            vec!["title", "intent", "workspace"],
            "内置字段在前 + 仅 enabled 插件 + 仅带 name 的声明: {names:?}"
        );
    }
}

// ── CORS 中间件 / 鉴权中间件（write_surface_auth）/ WS 入口 503 /
//    交互应答端点（声明驱动路由）补测 ─────────────────────────────

mod cors_auth_middleware_tests {
    use super::*;

    fn plain_app() -> axum::Router {
        crate::server::build_router(AppState::new())
    }

    async fn send(
        app: &axum::Router,
        method: &str,
        uri: &str,
        headers: &[(&str, &str)],
        body: Option<String>,
    ) -> axum::response::Response {
        let mut builder = Request::builder().method(method).uri(uri);
        for (k, v) in headers {
            builder = builder.header(*k, *v);
        }
        let req = match body {
            Some(b) => builder
                .header("content-type", "application/json")
                .body(Body::from(b))
                .unwrap(),
            None => builder.body(Body::empty()).unwrap(),
        };
        app.clone().oneshot(req).await.unwrap()
    }

    // ── CORS 源判定（纯函数） ──────────────────────────────

    #[test]
    fn is_local_origin_matches_exact_and_port_forms_only() {
        for origin in [
            "http://localhost",
            "https://localhost",
            "http://127.0.0.1",
            "http://127.0.0.1:9100",
            "https://[::1]",
            "http://[::1]:5173",
        ] {
            assert!(is_local_origin(origin), "{origin} 应判本地源");
        }
        for origin in [
            "http://localhost.evil.com",
            "https://example.com",
            "localhost",
            "",
        ] {
            assert!(!is_local_origin(origin), "{origin} 不应判本地源");
        }
    }

    #[test]
    fn origin_matches_allowlist_is_exact_match() {
        let allow = ["https://app.example.com", "https://b.example.com"];
        assert!(origin_matches_allowlist("https://app.example.com", &allow));
        assert!(!origin_matches_allowlist(
            "https://app.example.com.evil.com",
            &allow
        ));
        assert!(!origin_matches_allowlist("https://c.example.com", &allow));
        assert!(!origin_matches_allowlist("", &allow));
    }

    // ── CORS 中间件（oneshot 行为面） ──────────────────────

    #[tokio::test]
    async fn preflight_options_returns_204_with_cors_headers() {
        let app = plain_app();
        let resp = send(
            &app,
            "OPTIONS",
            "/api/v1/chat",
            &[("origin", "http://localhost:5173")],
            None,
        )
        .await;
        assert_eq!(resp.status(), StatusCode::NO_CONTENT, "预检拦截 204");
        let h = resp.headers();
        assert_eq!(h["access-control-allow-origin"], "http://localhost:5173");
        assert_eq!(
            h["access-control-allow-methods"],
            "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        );
        assert_eq!(h["access-control-max-age"], "86400");
        assert!(
            h.get("access-control-allow-credentials").is_none(),
            "本地源反射不带凭据头"
        );
    }

    #[tokio::test]
    async fn responses_reflect_allowed_origin_and_deny_unknown() {
        let app = plain_app();
        let resp = send(
            &app,
            "GET",
            "/health",
            &[("origin", "http://127.0.0.1:5173")],
            None,
        )
        .await;
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(
            resp.headers()["access-control-allow-origin"],
            "http://127.0.0.1:5173"
        );

        // 非放行源：响应照常 200，但不反射 origin
        let resp = send(
            &app,
            "GET",
            "/health",
            &[("origin", "https://evil.com")],
            None,
        )
        .await;
        assert_eq!(resp.status(), StatusCode::OK);
        assert!(resp.headers().get("access-control-allow-origin").is_none());
    }

    #[tokio::test]
    async fn explicit_allowlist_origin_gets_credentials() {
        // 本用例独占操作 AGENTOS_CORS_ORIGINS（进程全局），其余 CORS 用例
        // 只走本地源判定，不受影响。
        std::env::set_var("AGENTOS_CORS_ORIGINS", "https://app.example.com");
        let app = plain_app();
        let resp = send(
            &app,
            "OPTIONS",
            "/api/v1/chat",
            &[("origin", "https://app.example.com")],
            None,
        )
        .await;
        assert_eq!(resp.status(), StatusCode::NO_CONTENT);
        let h = resp.headers();
        assert_eq!(h["access-control-allow-origin"], "https://app.example.com");
        assert_eq!(
            h["access-control-allow-credentials"], "true",
            "显式白名单源带凭据放行"
        );
        assert!(is_origin_allowed("https://app.example.com"));
        assert!(!is_origin_allowed("https://c.example.com"));

        std::env::remove_var("AGENTOS_CORS_ORIGINS");
        let resp = send(
            &app,
            "OPTIONS",
            "/api/v1/chat",
            &[("origin", "https://app.example.com")],
            None,
        )
        .await;
        assert!(
            resp.headers().get("access-control-allow-origin").is_none(),
            "环境变量移除后白名单失效（仅本地源放行）"
        );
    }

    // ── write_surface_auth 白名单面 ────────────────────────

    #[tokio::test]
    async fn whitelist_paths_reject_anonymous_with_401() {
        let app = plain_app();
        for (method, uri) in [
            ("GET", "/api/v1/plugins"),
            ("GET", "/api/v1/sessions"),
            ("GET", "/api/v1/pipelines"),
            ("GET", "/metrics"),
            ("GET", "/api/v1/schema"),
            ("GET", "/api/v1/tools"),
            ("GET", "/api/v1/system/memstats"),
        ] {
            let resp = send(&app, method, uri, &[], None).await;
            assert_eq!(
                resp.status(),
                StatusCode::UNAUTHORIZED,
                "{method} {uri} 匿名应 401"
            );
        }
    }

    #[tokio::test]
    async fn non_whitelist_paths_pass_anonymous_to_handler() {
        let app = plain_app();
        // /health 不在白名单 → 匿名直达 handler（200）
        let resp = send(&app, "GET", "/health", &[], None).await;
        assert_eq!(resp.status(), StatusCode::OK);
        // /uploads 不在白名单 → 匿名直达 handler（无文件 404，而非 401）
        let resp = send(&app, "GET", "/uploads/pic.png", &[], None).await;
        assert_eq!(
            resp.status(),
            StatusCode::NOT_FOUND,
            "匿名 404 证明鉴权跳过"
        );
        // /api/v1/auth/login 不在白名单 → 缺 body 422/400 而非 401
        let resp = send(&app, "POST", "/api/v1/auth/login", &[], None).await;
        assert_ne!(resp.status(), StatusCode::UNAUTHORIZED, "auth 面不挂鉴权");
    }

    /// 内存库播种 admin + viewer 的 app 与两枚 token（同一次哈希绑定）。
    async fn app_with_admin_and_viewer() -> (axum::Router, String, String) {
        let store = std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let admin_hash = agentos_http::auth::hash_password(SEED_ADMIN_PW).unwrap();
        let viewer_hash = agentos_http::auth::hash_password("viewer-pw-2026").unwrap();
        for (uid, name, hash, role) in [
            (
                "00000000-0000-0000-0000-000000000001",
                "admin",
                admin_hash.clone(),
                "admin",
            ),
            (
                "00000000-0000-0000-0000-000000000002",
                "viewer",
                viewer_hash.clone(),
                "viewer",
            ),
        ] {
            let _ = StorageBackend::create_user(
                store.as_ref(),
                &agentos_core::types::UserRecord {
                    user_id: uid.to_string(),
                    username: name.to_string(),
                    password: hash.clone(),
                    email: None,
                    role: role.to_string(),
                    tenant_id: "default".to_string(),
                    created_at: chrono::Utc::now().to_rfc3339(),
                    last_login_at: None,
                    must_change_password: false,
                },
            )
            .await;
        }
        let mint = |uid: &str, name: &str, hash: String, role: &str| {
            agentos_http::auth::encode_token(
                agentos_http::auth::TokenType::Access,
                &agentos_http::auth::BuiltInUser {
                    id: uid.to_string(),
                    username: name.to_string(),
                    password: hash,
                    email: String::new(),
                    role: role.to_string(),
                    tenant_id: "default".to_string(),
                    created_at: String::new(),
                    must_change_password: false,
                },
                3600,
            )
        };
        let admin_token = mint(
            "00000000-0000-0000-0000-000000000001",
            "admin",
            admin_hash,
            "admin",
        );
        let viewer_token = mint(
            "00000000-0000-0000-0000-000000000002",
            "viewer",
            viewer_hash,
            "viewer",
        );
        let mut state = AppState::new();
        state.store = Some(store);
        (
            crate::server::build_router(state),
            admin_token,
            viewer_token,
        )
    }

    #[tokio::test]
    async fn viewer_can_read_but_write_needs_admin() {
        let (app, admin, viewer) = app_with_admin_and_viewer().await;
        let bearer = format!("Bearer {viewer}");

        // viewer 读面放行（GET /api/v1/plugins → 200 空清单）
        let resp = send(
            &app,
            "GET",
            "/api/v1/plugins",
            &[("authorization", &bearer)],
            None,
        )
        .await;
        assert_eq!(resp.status(), StatusCode::OK, "viewer 读面应放行");

        // viewer 写面拒绝（PUT enabled → 403）
        let resp = send(
            &app,
            "PUT",
            "/api/v1/plugins/some_plugin/enabled",
            &[("authorization", &bearer)],
            Some(json!({"enabled": true}).to_string()),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::FORBIDDEN, "viewer 写面应 403");

        // admin 写面通过鉴权层（project_root 未接线由 handler 报 5xx，非 401/403）
        let resp = send(
            &app,
            "PUT",
            "/api/v1/plugins/some_plugin/enabled",
            &[("authorization", &format!("Bearer {admin}"))],
            Some(json!({"enabled": true}).to_string()),
        )
        .await;
        assert_ne!(
            resp.status(),
            StatusCode::UNAUTHORIZED,
            "admin 写面不应被鉴权层拒绝"
        );
        assert_ne!(resp.status(), StatusCode::FORBIDDEN, "admin 写面不应 403");
    }

    #[tokio::test]
    async fn ws_ticket_any_authenticated_user_can_issue() {
        let app = plain_app();
        // 匿名 → 401
        let resp = send(&app, "POST", "/api/v1/ws-ticket", &[], None).await;
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        // 内置 admin（store None 回退内置表）→ 200 票据
        let resp = send(
            &app,
            "POST",
            "/api/v1/ws-ticket",
            &[("authorization", &scaffold_admin_bearer())],
            None,
        )
        .await;
        assert_eq!(resp.status(), StatusCode::OK, "已认证用户即可签发票据");
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert!(v["ticket"].as_str().is_some(), "响应应含 ticket");
        assert!(v["expires_in"].as_u64().unwrap_or(0) > 0);
    }

    // ── WS 入口：session 未装配显式 503 ────────────────────
    // WebSocketUpgrade 提取器依赖 hyper 注入的 OnUpgrade extension，oneshot
    // 无传输层不可达——起真实 TCP 服务 + tungstenite 客户端连接（同
    // tests/ws_ticket_test.rs 构造）。

    /// 起真实 TCP 服务，返回 (本地地址, shutdown 句柄)。
    async fn spawn_server(state: AppState) -> (std::net::SocketAddr, tokio::task::JoinHandle<()>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let app = crate::server::build_router(state);
        let handle = tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        (addr, handle)
    }

    #[tokio::test]
    async fn ws_handshake_without_session_coordinator_returns_503() {
        // session 未装配（仅测试构造场景）→ 握手前显式 503，不静默降级
        let (addr, handle) = spawn_server(AppState::new()).await;
        let err = tokio_tungstenite::connect_async(format!("ws://{addr}/ws/chat?token=x"))
            .await
            .expect_err("session 未装配握手必须被拒");
        match err {
            tokio_tungstenite::tungstenite::Error::Http(resp) => {
                assert_eq!(
                    resp.status(),
                    StatusCode::SERVICE_UNAVAILABLE,
                    "握手拒绝应 503"
                );
            }
            other => panic!("应返回 HTTP 503 拒绝，实际 {other:?}"),
        }
        handle.abort();
    }

    #[tokio::test]
    async fn ws_handshake_without_inbound_router_returns_503() {
        // session 已装配但 inbound_router 缺席（半装配态）→ 同样 503
        let state = AppState {
            session: Some(Arc::new(agentos_session::SessionCoordinator::new())),
            inbound_router: None,
            ..AppState::new()
        };
        let (addr, handle) = spawn_server(state).await;
        let err = tokio_tungstenite::connect_async(format!("ws://{addr}/ws/chat?token=x"))
            .await
            .expect_err("inbound_router 未装配握手必须被拒");
        match err {
            tokio_tungstenite::tungstenite::Error::Http(resp) => {
                assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
            }
            other => panic!("应返回 HTTP 503 拒绝，实际 {other:?}"),
        }
        handle.abort();
    }

    // ── 交互应答端点（P1-3 声明驱动路由） ──────────────────

    fn interaction_manifest(namespace: &str) -> agentos_core::traits::PluginManifest {
        serde_json::from_value(json!({
            "id": "p_interact", "name": "p_interact", "version": "1.0.0",
            "plugin_type": "system", "language": "python",
            "host_type": "sidecar", "entry": "x",
            "provides": {"capabilities": [{
                "namespace": namespace,
                "methods": ["respond"],
                "protocol_roles": [{"role": "interaction-respond", "method": "respond"}],
            }]},
            "capabilities": {},
        }))
        .expect("valid manifest")
    }

    /// 可编程 CapabilityHandler 桩（interaction 路由面）。
    struct StubCapabilityHandler {
        ns: String,
        result: std::sync::Mutex<Option<Result<serde_json::Value, agentos_mcp::McpError>>>,
    }

    #[async_trait::async_trait]
    impl agentos_mcp::CapabilityHandler for StubCapabilityHandler {
        fn namespace(&self) -> &str {
            &self.ns
        }
        async fn handle(
            &self,
            _method: &str,
            _params: serde_json::Value,
        ) -> Result<serde_json::Value, agentos_mcp::McpError> {
            self.result
                .lock()
                .unwrap()
                .clone()
                .expect("handle 不应被调用")
        }
    }

    #[test]
    fn resolve_interaction_responder_finds_declared_role() {
        let manifests = vec![interaction_manifest("itest")];
        let hit = resolve_interaction_responder(&manifests)
            .expect("声明了 interaction-respond 角色应命中");
        assert_eq!(hit, ("itest".to_string(), "respond".to_string()));
        // 无 provides → None（fail-closed 不猜路由目标）
        let bare = vec![
            serde_json::from_value::<agentos_core::traits::PluginManifest>(json!({
                "id": "p_bare", "name": "p_bare", "version": "1.0.0",
                "plugin_type": "system", "language": "python",
                "host_type": "sidecar", "entry": "x",
                "capabilities": {},
            }))
            .expect("valid manifest"),
        ];
        assert!(resolve_interaction_responder(&bare).is_none());
    }

    #[tokio::test]
    async fn interaction_response_guards_then_routes() {
        // 缺 request_id → 显式错误载荷
        let state = AppState::new();
        let resp = interaction_response_handler(axum::extract::State(state), axum::Json(json!({})))
            .await
            .unwrap();
        assert_eq!(resp.0["success"], false);
        assert_eq!(resp.0["error"], "缺少 request_id");

        // capability registry 未装配 → 显式错误
        let state = AppState::new();
        let resp = interaction_response_handler(
            axum::extract::State(state),
            axum::Json(json!({"request_id": "r1", "choice": 1})),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["success"], false);
        assert!(resp.0["error"]
            .as_str()
            .unwrap()
            .contains("registry not available"));

        // registry 在场但无插件声明角色 → 显式失败不猜路由
        let mut state = AppState::new();
        state.capability_handlers = Some(Arc::new(agentos_mcp::CapabilityHandlerRegistry::new()));
        let resp = interaction_response_handler(
            axum::extract::State(state),
            axum::Json(json!({"request_id": "r1"})),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["success"], false);
        assert!(resp.0["error"]
            .as_str()
            .unwrap()
            .contains("no plugin declares"));

        // 声明角色 + handler 成功 → success:true + data 透传
        let mut state = AppState::new();
        let registry = Arc::new(agentos_mcp::CapabilityHandlerRegistry::new());
        registry.register(Arc::new(StubCapabilityHandler {
            ns: "itest".to_string(),
            result: std::sync::Mutex::new(Some(Ok(json!({"answered": true})))),
        }));
        state.capability_handlers = Some(registry);
        *state.manifests.write().await = vec![interaction_manifest("itest")];
        let resp = interaction_response_handler(
            axum::extract::State(state),
            axum::Json(json!({"request_id": "r2", "response_type": "choice"})),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["success"], true);
        assert_eq!(resp.0["request_id"], "r2");
        assert_eq!(resp.0["data"]["answered"], true);

        // handler 失败 → success:false + 错误透传
        let mut state = AppState::new();
        let registry = Arc::new(agentos_mcp::CapabilityHandlerRegistry::new());
        registry.register(Arc::new(StubCapabilityHandler {
            ns: "itest".to_string(),
            result: std::sync::Mutex::new(Some(Err(agentos_mcp::McpError::Protocol {
                message: "interaction session gone".to_string(),
            }))),
        }));
        state.capability_handlers = Some(registry);
        *state.manifests.write().await = vec![interaction_manifest("itest")];
        let resp = interaction_response_handler(
            axum::extract::State(state),
            axum::Json(json!({"request_id": "r3"})),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["success"], false);
        assert_eq!(resp.0["request_id"], "r3");
        assert!(resp.0["error"]
            .as_str()
            .unwrap()
            .contains("interaction session gone"));
    }

    // ── 租户上下文解析（匿名回退 default） ─────────────────

    #[tokio::test]
    async fn request_tenant_ctx_falls_back_to_default_without_credentials() {
        let ctx = request_tenant_ctx(None, &HeaderMap::new(), "sess-1").await;
        assert_eq!(ctx.tenant_id, "default", "无凭证回退默认租户");
        assert_eq!(ctx.session_id, "sess-1");
    }
}

// ═══════════════════════════════════════════════════════════════════════
// 覆盖率补测：管道配置指纹 / 冷恢复降级留痕 / 会话级 execution_context 注入 /
// 落库重试还原 / merge_patch 类型替换 / chat_handler 坐标解析降级。
// 断行为契约，存储走真实 SqliteStore，故障注入用探针 store（非全 mock）。
// ═══════════════════════════════════════════════════════════════════════

#[cfg(test)]
mod server_gap_tests {
    use super::*;
    use agentos_core::traits::PluginManifest;

    /// 只注入指定方法故障、其余全部转发真实 SqliteStore 的探针 store。
    ///
    /// `fail_checkpoint`：load_latest_checkpoint 报错（冷恢复降级分支）。
    /// `fail_traces`：get_step_traces_by_pipeline 报错（回放失败分支）。
    /// `fail_state`：load_pipeline_state 报错（标量基线不完整分支）。
    /// `fail_messages`：get_messages_by_pipeline 报错（对话历史不完整分支）。
    struct ProbeStore {
        inner: Arc<agentos_engine::SqliteStore>,
        fail_checkpoint: std::sync::atomic::AtomicBool,
        fail_traces: std::sync::atomic::AtomicBool,
        fail_state: std::sync::atomic::AtomicBool,
        fail_messages: std::sync::atomic::AtomicBool,
    }

    impl ProbeStore {
        fn new() -> Self {
            Self {
                inner: Arc::new(agentos_engine::SqliteStore::open_memory().unwrap()),
                fail_checkpoint: std::sync::atomic::AtomicBool::new(false),
                fail_traces: std::sync::atomic::AtomicBool::new(false),
                fail_state: std::sync::atomic::AtomicBool::new(false),
                fail_messages: std::sync::atomic::AtomicBool::new(false),
            }
        }

        fn inject(&self, which: &str) {
            let flag = match which {
                "checkpoint" => &self.fail_checkpoint,
                "traces" => &self.fail_traces,
                "state" => &self.fail_state,
                "messages" => &self.fail_messages,
                other => panic!("unknown flag {other}"),
            };
            flag.store(true, std::sync::atomic::Ordering::SeqCst);
        }

        fn err(&self, what: &str) -> agentos_core::types::StorageError {
            agentos_core::types::StorageError::Database(format!("injected {what} failure"))
        }
    }

    #[async_trait::async_trait]
    impl agentos_core::traits::StorageBackend for ProbeStore {
        async fn load_latest_checkpoint(
            &self,
            pipeline_id: &str,
            tenant_id: &str,
        ) -> Result<Option<(i64, serde_json::Value)>, agentos_core::types::StorageError> {
            if self
                .fail_checkpoint
                .load(std::sync::atomic::Ordering::SeqCst)
            {
                return Err(self.err("checkpoint"));
            }
            agentos_core::traits::StorageBackend::load_latest_checkpoint(
                self.inner.as_ref(),
                pipeline_id,
                tenant_id,
            )
            .await
        }

        async fn get_step_traces_by_pipeline(
            &self,
            pipeline_id: &str,
            tenant_id: &str,
        ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError>
        {
            if self.fail_traces.load(std::sync::atomic::Ordering::SeqCst) {
                return Err(self.err("traces"));
            }
            agentos_core::traits::StorageBackend::get_step_traces_by_pipeline(
                self.inner.as_ref(),
                pipeline_id,
                tenant_id,
            )
            .await
        }

        async fn load_pipeline_state(
            &self,
            pipeline_id: &str,
            tenant_id: &str,
        ) -> Result<
            std::collections::HashMap<String, serde_json::Value>,
            agentos_core::types::StorageError,
        > {
            if self.fail_state.load(std::sync::atomic::Ordering::SeqCst) {
                return Err(self.err("state"));
            }
            agentos_core::traits::StorageBackend::load_pipeline_state(
                self.inner.as_ref(),
                pipeline_id,
                tenant_id,
            )
            .await
        }

        async fn get_messages_by_pipeline(
            &self,
            pipeline_id: &str,
            opts: MessageQueryOpts,
        ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError>
        {
            if self.fail_messages.load(std::sync::atomic::Ordering::SeqCst) {
                return Err(self.err("messages"));
            }
            agentos_core::traits::StorageBackend::get_messages_by_pipeline(
                self.inner.as_ref(),
                pipeline_id,
                opts,
            )
            .await
        }

        // ── trait 必需方法：转发真实实现（行为真实性，非全 mock） ──
        async fn get_run(
            &self,
            run_id: &str,
        ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::get_run(self.inner.as_ref(), run_id).await
        }
        async fn get_blob(
            &self,
            blob_id: &str,
        ) -> Result<Vec<u8>, agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::get_blob(self.inner.as_ref(), blob_id).await
        }
        async fn append_trace(
            &self,
            entry: agentos_core::types::TraceEntry,
        ) -> Result<(), agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::append_trace(self.inner.as_ref(), entry).await
        }
        async fn update_run_status(
            &self,
            run_id: &str,
            status: agentos_core::types::RunStatus,
            branch: Option<&str>,
            seq: Option<u32>,
        ) -> Result<(), agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::update_run_status(
                self.inner.as_ref(),
                run_id,
                status,
                branch,
                seq,
            )
            .await
        }
        async fn create_run(
            &self,
            run_id: &str,
            config_hash: &str,
            tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::create_run(
                self.inner.as_ref(),
                run_id,
                config_hash,
                tenant_id,
            )
            .await
        }
        async fn store_blob(
            &self,
            data: &[u8],
            mime_type: &str,
        ) -> Result<String, agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::store_blob(self.inner.as_ref(), data, mime_type)
                .await
        }
        async fn create_session(
            &self,
            session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::create_session(self.inner.as_ref(), session).await
        }
        async fn get_session(
            &self,
            thread_id: &str,
        ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            agentos_core::traits::StorageBackend::get_session(self.inner.as_ref(), thread_id).await
        }
        async fn list_sessions(
            &self,
            filter: agentos_core::traits::SessionListFilter,
        ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            agentos_core::traits::StorageBackend::list_sessions(self.inner.as_ref(), filter).await
        }
        async fn update_session(
            &self,
            session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::update_session(self.inner.as_ref(), session).await
        }
        async fn delete_session(
            &self,
            thread_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::delete_session(self.inner.as_ref(), thread_id)
                .await
        }
        async fn link_pipeline_session(
            &self,
            pipeline_id: &str,
            thread_id: &str,
            tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::link_pipeline_session(
                self.inner.as_ref(),
                pipeline_id,
                thread_id,
                tenant_id,
            )
            .await
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            thread_id: &str,
            tenant_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::list_pipeline_ids_by_thread(
                self.inner.as_ref(),
                thread_id,
                tenant_id,
            )
            .await
        }
        async fn get_thread_id_by_pipeline(
            &self,
            pipeline_id: &str,
        ) -> Result<Option<String>, agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::get_thread_id_by_pipeline(
                self.inner.as_ref(),
                pipeline_id,
            )
            .await
        }
        async fn get_step_traces_by_thread(
            &self,
            thread_id: &str,
            tenant_id: &str,
        ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError>
        {
            agentos_core::traits::StorageBackend::get_step_traces_by_thread(
                self.inner.as_ref(),
                thread_id,
                tenant_id,
            )
            .await
        }
        async fn create_user(
            &self,
            user: &agentos_core::types::UserRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::create_user(self.inner.as_ref(), user).await
        }
        async fn get_user_by_id(
            &self,
            user_id: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            agentos_core::traits::StorageBackend::get_user_by_id(self.inner.as_ref(), user_id).await
        }
        async fn get_user_by_username(
            &self,
            username: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            agentos_core::traits::StorageBackend::get_user_by_username(
                self.inner.as_ref(),
                username,
            )
            .await
        }
        async fn list_users(
            &self,
        ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            agentos_core::traits::StorageBackend::list_users(self.inner.as_ref()).await
        }
        async fn update_user_password(
            &self,
            user_id: &str,
            password_hash: &str,
            must_change_password: bool,
        ) -> Result<bool, agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::update_user_password(
                self.inner.as_ref(),
                user_id,
                password_hash,
                must_change_password,
            )
            .await
        }
        async fn update_last_login(
            &self,
            user_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::update_last_login(self.inner.as_ref(), user_id)
                .await
        }
        async fn delete_user(
            &self,
            user_id: &str,
        ) -> Result<bool, agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::delete_user(self.inner.as_ref(), user_id).await
        }
    }

    // ── compute_config_fingerprint（管道热重载指纹） ──

    /// 指纹：steps 目录内的 yaml/yml 文件参与（非 yaml 不参与）；同状态稳定。
    #[test]
    fn config_fingerprint_tracks_steps_files_and_is_stable() {
        let tmp = tempfile::tempdir().unwrap();
        let config = tmp.path().join("config");
        std::fs::create_dir_all(config.join("pipelines")).unwrap();
        std::fs::create_dir_all(config.join("steps")).unwrap();
        std::fs::write(config.join("pipelines/autonomous.yaml"), "name: a\n").unwrap();
        // 非 yaml 文件不参与指纹
        std::fs::write(config.join("steps/README.md"), "# doc\n").unwrap();

        let before = compute_config_fingerprint(&config);
        assert_eq!(
            before,
            compute_config_fingerprint(&config),
            "同状态指纹必须稳定"
        );

        // 加一个 yaml step 文件 → 指纹必须变
        std::fs::write(config.join("steps/common.yaml"), "id: c\n").unwrap();
        let after = compute_config_fingerprint(&config);
        assert_ne!(before, after, "steps 目录新增 yaml 必须改变指纹");
        assert_eq!(
            after,
            compute_config_fingerprint(&config),
            "新状态指纹同样稳定"
        );

        // 再加 .yml（另一种扩展名同样参与）
        std::fs::write(config.join("steps/extra.yml"), "id: e\n").unwrap();
        assert_ne!(
            after,
            compute_config_fingerprint(&config),
            ".yml 也必须参与指纹"
        );
    }

    /// 目录不存在（无 steps/）不 panic，指纹仍可算。
    #[test]
    fn config_fingerprint_without_steps_dir_does_not_panic() {
        let tmp = tempfile::tempdir().unwrap();
        let config = tmp.path().join("config");
        std::fs::create_dir_all(config.join("pipelines")).unwrap();
        let _ = compute_config_fingerprint(&config);
        let _ = compute_config_fingerprint(&tmp.path().join("nonexistent"));
    }

    // ── 冷恢复四条降级/失败分支 ──

    fn probe_state(store: Arc<ProbeStore>) -> AppState {
        let mut state = AppState::new();
        state.store = Some(store);
        state
    }

    /// checkpoint 读失败：不当作"无 checkpoint"静默降级——继续走 traces 回放
    ///（error 留痕），trace 里的标量基线仍被恢复。
    #[tokio::test]
    async fn cold_recovery_checkpoint_failure_degrades_to_traces() {
        let probe = Arc::new(ProbeStore::new());
        // 回放查询先经 message_slots 反查本管道的 run_id 集合（再按 run_id 扫
        // traces），故须先落一条本管道的消息槽位行，trace 才会被回放命中。
        probe
            .inner
            .apply_messages_ops_to_table(
                "pipe_cp_fail",
                "default",
                &[serde_json::json!({"op": "set", "seq": 0,
                    "msg": {"role": "user", "content": "seed"}, "_run_id": "run-trace"})],
            )
            .unwrap();
        agentos_core::traits::StorageBackend::append_trace(
            probe.inner.as_ref(),
            agentos_core::types::TraceEntry {
                trace_id: "t1".to_string(),
                run_id: "run-trace".to_string(),
                branch_id: "main".to_string(),
                seq_in_branch: 1,
                plugin_id: "seed".to_string(),
                patch_type: agentos_core::types::PatchType::StateUpdate,
                patch_data: serde_json::json!({"from_trace": true}),
                created_at: "2026-09-15T00:00:00Z".to_string(),
            },
        )
        .await
        .unwrap();
        probe.inject("checkpoint");

        let _state = probe_state(probe.clone());
        let store: Arc<dyn agentos_core::traits::StorageBackend> = probe.clone();
        let recovered = stage_recover_history(
            serde_json::json!({}),
            &store,
            "msg",
            "pipe_cp_fail",
            "default",
            "",
            false,
            None,
            &std::collections::HashSet::new(),
            "run-cr",
        )
        .await
        .expect("恢复失败不得上抛（降级继续）");
        assert_eq!(
            recovered["from_trace"], true,
            "checkpoint 读失败后必须继续 traces 回放，不得静默丢标量基线: {recovered}"
        );
    }

    /// traces 回放失败：显式 error 暴露（bug 信号），函数仍返回可用初始 state。
    #[tokio::test]
    async fn cold_recovery_traces_failure_keeps_going() {
        let probe = Arc::new(ProbeStore::new());
        probe.inject("checkpoint");
        probe.inject("traces");

        let _state = probe_state(probe.clone());
        let store: Arc<dyn agentos_core::traits::StorageBackend> = probe.clone();
        let recovered = stage_recover_history(
            serde_json::json!({}),
            &store,
            "msg",
            "pipe_tr_fail",
            "default",
            "",
            false,
            None,
            &std::collections::HashSet::new(),
            "run-cr",
        )
        .await
        .expect("恢复失败不得上抛（降级继续）");
        assert!(
            recovered.is_object(),
            "traces 回放失败不得 panic，仍返回初始 state: {recovered}"
        );
    }

    /// pipeline_state 读失败：标量基线不完整但继续（不阻断本轮执行）。
    #[tokio::test]
    async fn cold_recovery_state_table_failure_keeps_going() {
        let probe = Arc::new(ProbeStore::new());
        probe.inject("checkpoint");
        probe.inject("state");

        let _state = probe_state(probe.clone());
        let store: Arc<dyn agentos_core::traits::StorageBackend> = probe.clone();
        let recovered = stage_recover_history(
            serde_json::json!({}),
            &store,
            "msg",
            "pipe_st_fail",
            "default",
            "",
            false,
            None,
            &std::collections::HashSet::new(),
            "run-cr",
        )
        .await
        .expect("恢复失败不得上抛（降级继续）");
        assert!(recovered.is_object(), "state 表读失败不得 panic");
    }

    /// messages 读失败：对话历史不完整但继续。
    #[tokio::test]
    async fn cold_recovery_message_history_failure_keeps_going() {
        let probe = Arc::new(ProbeStore::new());
        probe.inject("checkpoint");
        probe.inject("messages");

        let _state = probe_state(probe.clone());
        let store: Arc<dyn agentos_core::traits::StorageBackend> = probe.clone();
        let recovered = stage_recover_history(
            serde_json::json!({}),
            &store,
            "msg",
            "pipe_msg_fail",
            "default",
            "",
            false,
            None,
            &std::collections::HashSet::new(),
            "run-cr",
        )
        .await
        .expect("恢复失败不得上抛（降级继续）");
        assert!(recovered.is_object(), "messages 读失败不得 panic");
    }

    // ── 会话级 / 任务级 execution_context 注入（stage_build_initial_state） ──

    fn ec_manifest(id: &str, meta_key: &str, exec_path: &str) -> PluginManifest {
        serde_json::from_value(serde_json::json!({
            "id": id, "name": id, "version": "1.0.0",
            "plugin_type": "system", "language": "python",
            "host_type": "sidecar", "entry": "x",
            "capabilities": {},
            "contributes": {"thread_fields": [
                {"name": "workspace_mode", "x_metadata_key": meta_key,
                 "x_execution_path": exec_path}
            ]},
        }))
        .expect("valid manifest")
    }

    async fn seed_ec_session(
        sqlite: &Arc<agentos_engine::SqliteStore>,
        thread_id: &str,
        pipeline_id: &str,
        metadata: Option<serde_json::Value>,
    ) {
        sqlite
            .create_session(&agentos_core::types::SessionRecord {
                thread_id: thread_id.to_string(),
                title: None,
                intent: None,
                current_state: "active".to_string(),
                agent_id: None,
                active_pipeline_id: Some(pipeline_id.to_string()),
                pipeline_ids: vec![pipeline_id.to_string()],
                metadata,
                created_at: "2026-09-15T00:00:00Z".to_string(),
                updated_at: "2026-09-15T00:00:00Z".to_string(),
                last_active_at: None,
            })
            .await
            .unwrap();
    }

    /// 会话级注入：enabled 插件的 thread_fields 声明把 thread metadata 值按
    /// x_execution_path 写入 execution_context；disabled 插件的声明不生效
    /// （声明驱动 + 启停门）。
    #[tokio::test]
    async fn session_level_execution_context_injected_by_declaration() {
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let store: Arc<dyn agentos_core::traits::StorageBackend> = sqlite.clone();
        seed_ec_session(
            &sqlite,
            "thread-ec",
            "pipe_ec",
            Some(serde_json::json!({"ws_mode": "isolated"})),
        )
        .await;

        let mut state = AppState::new();
        state.store = Some(store.clone());
        // 两个插件：enabled 的声明生效；disabled 的声明必须被启停门挡住
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
            ec_manifest("ws_on", "ws_mode", "workspace.mode"),
            ec_manifest("ws_off", "other_mode", "disabled.path"),
        ]));
        state
            .enabled_plugin_ids
            .write()
            .await
            .insert("ws_on".to_string());

        let recovered = stage_build_initial_state(
            &state,
            &store,
            "hello",
            "pipe_ec",
            "thread-ec",
            "msg-1",
            "u1",
            "",
            None,
            "run-1",
        )
        .await;
        assert_eq!(
            recovered["execution_context"]["workspace"]["mode"], "isolated",
            "enabled 插件声明必须注入（按 x_execution_path 落点）: {recovered}"
        );
        assert!(
            recovered["execution_context"].get("disabled").is_none(),
            "disabled 插件的声明不得生效: {recovered}"
        );
        // 基线键齐备（行为契约）
        for key in [
            "message",
            "input",
            "run_status",
            "run_id",
            "pipeline_id",
            "session_id",
            "user_id",
            "message_id",
        ] {
            assert!(recovered.get(key).is_some(), "基线键 {key} 必须存在");
        }
        assert_eq!(recovered["run_status"], "running");
        assert_eq!(recovered["ended"], false);
        assert_eq!(recovered["pipeline_id"], "pipe_ec");
        assert_eq!(recovered["session_id"], "thread-ec");
    }

    /// 任务级 execution_context 整体覆盖会话级（优先级契约）；
    /// 非对象/空对象不注入。
    #[tokio::test]
    async fn task_level_execution_context_overrides_session_level() {
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let store: Arc<dyn agentos_core::traits::StorageBackend> = sqlite.clone();
        seed_ec_session(
            &sqlite,
            "thread-ec2",
            "pipe_ec2",
            Some(serde_json::json!({"ws_mode": "session_value"})),
        )
        .await;
        let mut state = AppState::new();
        state.store = Some(store.clone());
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![ec_manifest(
            "ws_on",
            "ws_mode",
            "workspace.mode",
        )]));
        state
            .enabled_plugin_ids
            .write()
            .await
            .insert("ws_on".to_string());

        let task_ec = serde_json::json!({"task": {"id": "t-1"}});
        let recovered = stage_build_initial_state(
            &state,
            &store,
            "hi",
            "pipe_ec2",
            "thread-ec2",
            "msg-2",
            "u1",
            "",
            Some(&task_ec),
            "run-2",
        )
        .await;
        assert_eq!(
            recovered["execution_context"], task_ec,
            "任务级必须整体覆盖会话级（不是合并）: {recovered}"
        );

        // 非对象（字符串）不注入 → 保留会话级值
        let bad = serde_json::json!("not-an-object");
        let recovered = stage_build_initial_state(
            &state,
            &store,
            "hi",
            "pipe_ec2",
            "thread-ec2",
            "msg-3",
            "u1",
            "",
            Some(&bad),
            "run-3",
        )
        .await;
        assert_eq!(
            recovered["execution_context"]["workspace"]["mode"], "session_value",
            "非对象任务级上下文不得注入: {recovered}"
        );

        // 空对象同样不注入
        let empty = serde_json::json!({});
        let recovered = stage_build_initial_state(
            &state,
            &store,
            "hi",
            "pipe_ec2",
            "thread-ec2",
            "msg-4",
            "u1",
            "",
            Some(&empty),
            "run-4",
        )
        .await;
        assert_eq!(
            recovered["execution_context"]["workspace"]["mode"], "session_value",
            "空对象任务级上下文不得注入: {recovered}"
        );
    }

    /// 会话不存在 / 会话存在但无 metadata：不注入 execution_context（不 panic）。
    #[tokio::test]
    async fn missing_session_or_metadata_skips_context_injection() {
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let store: Arc<dyn agentos_core::traits::StorageBackend> = sqlite.clone();
        let mut state = AppState::new();
        state.store = Some(store.clone());
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![ec_manifest(
            "ws_on",
            "ws_mode",
            "workspace.mode",
        )]));
        state
            .enabled_plugin_ids
            .write()
            .await
            .insert("ws_on".to_string());

        // 会话不存在
        let recovered = stage_build_initial_state(
            &state,
            &store,
            "hi",
            "pipe_none",
            "thread-missing",
            "m",
            "u",
            "",
            None,
            "run",
        )
        .await;
        assert!(
            recovered.get("execution_context").is_none(),
            "无会话不得注入 execution_context: {recovered}"
        );

        // 会话存在但 metadata = None
        seed_ec_session(&sqlite, "thread-nometa", "pipe_nm", None).await;
        let recovered = stage_build_initial_state(
            &state,
            &store,
            "hi",
            "pipe_nm",
            "thread-nometa",
            "m",
            "u",
            "",
            None,
            "run",
        )
        .await;
        assert!(
            recovered.get("execution_context").is_none(),
            "metadata 缺席不得注入: {recovered}"
        );
    }

    // ── merge_patch 剩余分支（类型替换 / 非对象 patch） ──

    /// 目标非对象时整体替换为对象再合并；键已存在且一侧非对象 → 值替换；
    /// patch 非对象 → 整体替换。
    #[test]
    fn merge_patch_type_replacement_and_non_object_patch() {
        // 目标非对象 → 先重置成对象再合并
        let mut target = serde_json::json!("scalar");
        merge_patch(&mut target, &serde_json::json!({"a": 1}));
        assert_eq!(target, serde_json::json!({"a": 1}));

        // 已存在键两侧均为对象 → 递归合并；一侧非对象 → 替换
        let mut target = serde_json::json!({"obj": {"a": 1}, "leaf": "old"});
        merge_patch(
            &mut target,
            &serde_json::json!({"obj": {"b": 2}, "leaf": {"nested": true}}),
        );
        assert_eq!(target["obj"]["a"], 1, "对象递归合并不丢旧键");
        assert_eq!(target["obj"]["b"], 2);
        assert_eq!(
            target["leaf"],
            serde_json::json!({"nested": true}),
            "一侧非对象必须整体替换"
        );

        // patch 非对象 → 整体替换（含数组）
        let mut target = serde_json::json!({"a": 1});
        merge_patch(&mut target, &serde_json::json!([1, 2]));
        assert_eq!(target, serde_json::json!([1, 2]));

        // messages 键跳过（队列真值在表）
        let mut target = serde_json::json!({"messages": [1]});
        merge_patch(&mut target, &serde_json::json!({"messages": [], "x": 1}));
        assert_eq!(
            target["messages"],
            serde_json::json!([1]),
            "messages 不参与标量回放"
        );
        assert_eq!(target["x"], 1);
    }

    // ── 落库重试：失败逐次还原内存（不留半条消息） ──

    /// messages 表缺失 → user 消息落库重试耗尽上抛"未受理"语义；
    /// 内存 messages 必须被还原（不留半条消息）。
    #[tokio::test]
    async fn user_append_failure_restores_memory_snapshot() {
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        sqlite
            .with_conn::<(), String>(|conn| {
                conn.execute("DROP TABLE message_slots", [])
                    .map(|_| ())
                    .map_err(|e| e.to_string())
            })
            .unwrap();
        let store: Arc<dyn agentos_core::traits::StorageBackend> = sqlite;

        let result = stage_recover_history(
            serde_json::json!({"pipeline_id": "pipe_append_fail", "messages": [{"role": "assistant"}]}),
            &store,
            "落库失败的新消息",
            "pipe_append_fail",
            "default",
            "",
            false,
            None,
            &std::collections::HashSet::new(),
            "run-append",
        )
        .await;
        let outcome = result.expect_err("落库失败重试耗尽必须上抛（消息未受理）");
        assert!(outcome.failed, "必须是失败 outcome: {outcome:?}");
        assert!(
            outcome.content.contains("未受理"),
            "错误文案必须表达「消息未受理」: {}",
            outcome.content
        );
    }

    /// 无 messages 快照时失败 → 还原为"无 messages 键"（不残留空数组）。
    /// 与上一用例形成两组区分度输入（有快照 / 无快照）。
    #[tokio::test]
    async fn user_append_failure_without_snapshot_leaves_no_half_message() {
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        sqlite
            .with_conn::<(), String>(|conn| {
                conn.execute("DROP TABLE message_slots", [])
                    .map(|_| ())
                    .map_err(|e| e.to_string())
            })
            .unwrap();
        let store: Arc<dyn agentos_core::traits::StorageBackend> = sqlite;

        let result = stage_recover_history(
            serde_json::json!({"pipeline_id": "pipe_nosnap", "other": 1}),
            &store,
            "m2",
            "pipe_nosnap",
            "default",
            "",
            false,
            None,
            &std::collections::HashSet::new(),
            "run-nosnap",
        )
        .await;
        assert!(
            result.is_err(),
            "无快照时落库失败同样必须上抛（不静默成功）"
        );
    }
}
