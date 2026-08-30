// @feature: FP-0.2.一 插件协议 | @ci: rust-test
// 由 server.rs 的主 #[cfg(test)] 测试块体平移而来（保留私有项访问）。

#[cfg(test)]
use agentos_core::traits::MessageQueryOpts;

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
                    json!({"username": "admin", "password": "admin12345"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
    v["access_token"].as_str().unwrap().to_string()
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
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
}

#[tokio::test]
async fn test_chat_post_returns_200() {
    let (state, _invoker, _store, sqlite) = make_engine_state();
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
    let (state, _invoker, _store, sqlite) = make_engine_state();
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
async fn test_schema_with_config() {
    let config = json!({
        "agents": [{"id": "agent1", "name": "Test Agent"}],
        "pipelines": [{"id": "default", "name": "Default Pipeline"}],
        "tools": [{"name": "search", "description": "Search tool"}],
        "routes": {"input": ["plugin1"], "output": ["plugin2"]}
    });
    let app = build_router(AppState::with_config(config));
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/schema")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let body = axum::body::to_bytes(response.into_body(), 4096)
        .await
        .unwrap();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
    // config-based 模式下 agents 为空（因为 manifests 为空）
    assert_eq!(json["agents"].as_array().unwrap().len(), 0);
    // tools 来自 config（capability_registry 为 None 时 fallback 到 config）
    assert_eq!(json["tools"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn test_tools_handler_returns_tools_list() {
    // 验证 tools handler 从 config 返回工具列表（无 registry 时）。
    // W-C2：响应信封统一为 {items, total}。
    let config = json!({
        "tools": [{"name": "calculator", "description": "A calculator"}],
    });
    let app = build_router(AppState::with_config(config));
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/tools")
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
    assert_eq!(json["total"], 1);
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["name"], "calculator");
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
    async fn invoke_pipeline_plugin(
        &self,
        _plugin_id: &str,
        ctx: &agentos_core::types::PluginContext,
    ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
        let history = ctx
            .state
            .get("messages")
            .cloned()
            .unwrap_or_else(|| serde_json::json!([]));
        self.seen.lock().unwrap().push(history.clone());
        self.seen_states.lock().unwrap().push(ctx.state.clone());
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
    let state_a = AppState::new();
    let compiled_a = maybe_reload_compiled_pipeline(&state_a, &config_a).await;
    assert!(
        compiled_a.bodies.is_empty(),
        "未知插件引用应编译失败并降级空管道"
    );

    // 场景 B：同 YAML，manifests store 已含该插件（热发现合并后）→ 编译成功。
    let root_b = std::env::temp_dir().join(format!("hr_b_{}", uuid::Uuid::new_v4().simple()));
    let config_b = write_cfg(&root_b);
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
        password: "admin12345".to_string(),
        email: Some("admin@agentos.dev".to_string()),
        role: "admin".to_string(),
        tenant_id: tenant_id.to_string(),
        created_at: "2026-08-30T00:00:00Z".to_string(),
        last_login_at: None,
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
    });
    state.step_library = Arc::new(agentos_core::types::StepLibrary::default());
    // 已知插件面 = 共享 manifests store（live_plugin_ids 现读，与热发现语义一致）
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![mk_manifest_json(
        "mock_llm_core",
    )]));
    (state, invoker, store, sqlite)
}

#[tokio::test]
async fn test_multi_turn_second_round_sees_first_round_context() {
    let (state, invoker, _store, _sqlite) = make_engine_state();
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
    let (state, invoker, _store, _sqlite) = make_engine_state();
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
    let (state, invoker, store, sqlite) = make_engine_state();
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
    let (state, invoker, store, sqlite) = make_engine_state();
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
    let (state, _invoker, store, _sqlite) = make_engine_state();

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
        password: "admin12345".to_string(),
        email: None,
        role: "admin".to_string(),
        tenant_id: "default".to_string(),
        created_at: now.clone(),
        last_login_at: None,
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

// ── spill_guard 配套：框架级强制工具注入 ──────────────────────

/// 构造含 spill_retrieve + 普通工具的 registry，注入 AppState。
fn app_state_with_tools(tool_names: &[&str]) -> AppState {
    use agentos_core::traits::ToolDescriptor;
    use agentos_core::types::{ToolCategory, ToolSource};
    use agentos_plugin_loader::CapabilityRegistryImpl;
    let registry = Arc::new(CapabilityRegistryImpl::new());
    for name in tool_names {
        registry.register_tool(
            "test_plugin",
            ToolDescriptor {
                name: (*name).to_string(),
                description: format!("test tool {name}"),
                plugin_id: "test_plugin".to_string(),
                input_schema: json!({"type": "object", "properties": {}}),
                output_schema: None,
                category: ToolCategory::System,
                source: ToolSource::Builtin,
                ui: None,
                render: None,
            },
        );
    }
    let mut state = AppState::new();
    state.capability_registry = Some(registry);
    state
}

/// spill_retrieve 即使不在 agent tool_ids 里也必须注入——spill_guard 替换
/// 文本引导 LLM 调它取回原文，若 schema 不可见就是死路。
#[test]
fn inject_tool_schemas_forces_spill_retrieve_regardless_of_tool_ids() {
    let app_state = app_state_with_tools(&["bash_execute", "spill_retrieve"]);
    let mut state = json!({
        "tool_ids": ["bash_execute"],  // 显式不含 spill_retrieve
    });
    inject_tool_schemas(&mut state, &app_state);
    let schemas = state["tool_schemas"].as_array().unwrap();
    let names: Vec<&str> = schemas
        .iter()
        .map(|s| s["function"]["name"].as_str().unwrap())
        .collect();
    assert!(
        names.contains(&"spill_retrieve"),
        "spill_retrieve 必须强制注入: {names:?}"
    );
    assert!(
        names.contains(&"bash_execute"),
        "tool_ids 命中的正常注入: {names:?}"
    );
}

/// K10 新契约：无 tool_ids（且解析不出 agent yaml 的 tool_ids）= 配置断链
/// → 空工具面，仅 FRAMEWORK_ALWAYS_INCLUDE_TOOLS（spill_retrieve）保留。
/// 旧语义"无 tool_ids → 全量兜底"已废止（权限边界不得静默放宽）。
#[test]
fn inject_tool_schemas_missing_tool_ids_yields_framework_only() {
    let app_state = app_state_with_tools(&["bash_execute", "spill_retrieve"]);
    let mut state = json!({"agent_id": "ghost_agent"}); // 无 tool_ids，AppState 无 config_center
    inject_tool_schemas(&mut state, &app_state);
    let names: Vec<&str> = state["tool_schemas"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["function"]["name"].as_str().unwrap())
        .collect();
    assert!(
        names.contains(&"spill_retrieve"),
        "框架强制工具保留: {names:?}"
    );
    assert!(
        !names.contains(&"bash_execute"),
        "配置断链不得兜底全量（K10）: {names:?}"
    );
}

/// K10：state 无 tool_ids 但 agent yaml 可解析 → 按 yaml 的 tool_ids 过滤
/// （agentos.yaml tool_ids 白名单是 0.2 工具面契约，内核读 yaml 的权威点）。
#[test]
fn inject_tool_schemas_resolves_tool_ids_from_agent_yaml() {
    let tmp = tempfile::tempdir().unwrap();
    let agents_dir = tmp.path().join("agents");
    std::fs::create_dir_all(&agents_dir).unwrap();
    std::fs::write(
        agents_dir.join("main_agent.yaml"),
        "name: t\ntool_ids: [bash_execute]\n",
    )
    .unwrap();
    let cc = std::sync::Arc::new(agentos_config::config_center::ConfigCenter::new(
        tmp.path().to_path_buf(),
    ));

    let mut app_state = app_state_with_tools(&["bash_execute", "file_read", "spill_retrieve"]);
    app_state.config_center = Some(cc);
    let mut state = json!({"agent.id": "main_agent"});
    inject_tool_schemas(&mut state, &app_state);

    let names: Vec<&str> = state["tool_schemas"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["function"]["name"].as_str().unwrap())
        .collect();
    assert!(names.contains(&"bash_execute"), "yaml tool_ids 命中注入");
    assert!(
        !names.contains(&"file_read"),
        "yaml 未列的工具不得注入: {names:?}"
    );
    assert!(
        names.contains(&"spill_retrieve"),
        "框架强制工具无视 yaml 保留: {names:?}"
    );
}

/// K10：agent yaml 存在且解析正常但无 tool_ids 键 = 白名单未声明 = 配置断链
/// → 空面（仅框架强制工具）；yaml 本身没坏，不打 _agent_config_missing 标记。
#[test]
fn inject_tool_schemas_yaml_without_tool_ids_keys_is_empty_surface() {
    let tmp = tempfile::tempdir().unwrap();
    let agents_dir = tmp.path().join("agents");
    std::fs::create_dir_all(&agents_dir).unwrap();
    std::fs::write(agents_dir.join("no_tools.yaml"), "name: t\n").unwrap();
    let cc = std::sync::Arc::new(agentos_config::config_center::ConfigCenter::new(
        tmp.path().to_path_buf(),
    ));

    let mut app_state = app_state_with_tools(&["bash_execute", "spill_retrieve"]);
    app_state.config_center = Some(cc);
    let mut state = json!({"agent.id": "no_tools"});
    inject_tool_schemas(&mut state, &app_state);

    let names: Vec<&str> = state["tool_schemas"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["function"]["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, vec!["spill_retrieve"], "仅框架强制工具");
    assert!(
        state.get("_agent_config_missing").is_none(),
        "yaml 正常解析不打配置缺失标记"
    );
}

/// K10 + K5 联动：agent yaml 缺失（agent_id 打错字）→ 空面 + 真实 state 打
/// _agent_config_missing 标记（诊断出口可见）。
#[test]
fn inject_tool_schemas_missing_agent_yaml_marks_state() {
    let tmp = tempfile::tempdir().unwrap();
    let cc = std::sync::Arc::new(agentos_config::config_center::ConfigCenter::new(
        tmp.path().to_path_buf(),
    ));

    let mut app_state = app_state_with_tools(&["bash_execute", "spill_retrieve"]);
    app_state.config_center = Some(cc);
    let mut state = json!({"agent_id": "typo_agent"});
    inject_tool_schemas(&mut state, &app_state);

    assert_eq!(
        state["_agent_config_missing"], true,
        "agent yaml 缺失应打标记（K5/K10 联动）"
    );
    let names: Vec<&str> = state["tool_schemas"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["function"]["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, vec!["spill_retrieve"], "断链空面（仅框架工具）");
}

/// registry 里没有 spill_retrieve（插件未安装）时不会凭空注入。
#[test]
fn inject_tool_schemas_no_spill_retrieve_when_not_installed() {
    let app_state = app_state_with_tools(&["bash_execute"]); // 无 spill_retrieve
    let mut state = json!({"tool_ids": ["bash_execute"]});
    inject_tool_schemas(&mut state, &app_state);
    let names: Vec<&str> = state["tool_schemas"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["function"]["name"].as_str().unwrap())
        .collect();
    assert!(!names.contains(&"spill_retrieve"), "未安装不应凭空出现");
}

/// task_dsh_plugin_adapter 任务 1：声明了 output_schema/render 的工具，
/// 输出契约注入 state["tool_output_contracts"]（tool_core 校验 + 前端路由的
/// 数据源）；未声明者不产生条目（存量工具零负担）。
#[test]
fn inject_tool_schemas_also_injects_output_contracts() {
    use agentos_core::traits::ToolDescriptor;
    use agentos_core::types::{ToolCategory, ToolSource};
    use agentos_plugin_loader::CapabilityRegistryImpl;
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry.register_tool(
        "test_plugin",
        ToolDescriptor {
            name: "dsh_read".to_string(),
            description: "read".to_string(),
            plugin_id: "test_plugin".to_string(),
            input_schema: json!({"type": "object", "properties": {}}),
            output_schema: Some(json!({"type": "object", "required": ["path"]})),
            category: ToolCategory::File,
            source: ToolSource::Builtin,
            ui: None,
            render: Some(json!({"card": "read"})),
        },
    );
    registry.register_tool(
        "test_plugin",
        ToolDescriptor {
            name: "legacy_tool".to_string(),
            description: "no contract".to_string(),
            plugin_id: "test_plugin".to_string(),
            input_schema: json!({"type": "object", "properties": {}}),
            output_schema: None,
            category: ToolCategory::System,
            source: ToolSource::Builtin,
            ui: None,
            render: None,
        },
    );
    let mut app_state = AppState::new();
    app_state.capability_registry = Some(registry);

    let mut state = json!({});
    inject_tool_schemas(&mut state, &app_state);

    let contracts = state["tool_output_contracts"].as_object().unwrap();
    assert_eq!(contracts.len(), 1, "只有声明契约的工具进入: {contracts:?}");
    assert_eq!(contracts["dsh_read"]["schema"]["required"][0], "path");
    assert_eq!(contracts["dsh_read"]["render"]["card"], "read");
    assert!(contracts.get("legacy_tool").is_none());
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
async fn test_stage_build_initial_state_merges_overlay_after_execution_context() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store: Arc<dyn StorageBackend> = sqlite;
    let overlay = json!({
        "task.goal": "喝水提醒",
        "task.status": "pending",
        "task.id": "31bfdee19720",
        "lineage.parent_pipeline_id": "pipe_parent",
        "lineage.origin_session_id": "sess_root",
        "lineage.root": true,
    });
    let state = AppState::new();
    let st = stage_build_initial_state(
        &state,
        &store,
        "msg",
        "pipe_new",
        "thread_new",
        "m1",
        "u1",
        "",
        Some(&json!({"workspace": {"mode": "worktree"}})),
        Some(&overlay),
        "run-abc",
    )
    .await;
    // execution_context 合并点（1a2）优先成立（overlay 不侵蚀其结构）
    assert_eq!(st["execution_context"]["workspace"]["mode"], "worktree");
    // overlay 顶层扁平键并入（与 track.total_tokens 同款约定）
    assert_eq!(st["task.goal"], "喝水提醒");
    assert_eq!(st["task.status"], "pending");
    assert_eq!(st["task.id"], "31bfdee19720");
    assert_eq!(st["lineage.parent_pipeline_id"], "pipe_parent");
    assert_eq!(st["lineage.origin_session_id"], "sess_root");
    assert_eq!(st["lineage.root"], true);
    // 引擎系统字段基线完好
    assert_eq!(st["message"], "msg");
    assert_eq!(st["pipeline_id"], "pipe_new");
    assert_eq!(st["session_id"], "thread_new");
    assert_eq!(st["user_id"], "u1");
    assert_eq!(st["run_id"], "run-abc", "run_id 注入为轮询定位锚（批次 C）");
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
    let (state, invoker, _store, _sqlite) = make_engine_state();
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
    let (state, invoker, _store, _sqlite) = make_engine_state();
    // 订阅方插件：manifest 声明 DomainEvent hook 且启用
    {
        let mut manifests = state.manifests.write().await;
        manifests.push(agentos_core::traits::PluginManifest {
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
    let (state, _invoker, _store, sqlite) = make_engine_state();
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
    let (state, _invoker, _store, sqlite) = make_engine_state();
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
    let (state, invoker, store, _sqlite) = make_engine_state();
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

/// 正常连续两轮同文消息不受幂等影响：第一轮已消费（assistant 跟随），
/// 第二轮同文 user 是新输入 → 应正常 append（2 条 user）。
#[tokio::test]
async fn test_repeated_user_message_after_reply_still_appends() {
    let (state, _invoker, store, _sqlite) = make_engine_state();
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
    let (state, _invoker, store, _sqlite) = make_engine_state();
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
    let (state, _invoker, store, _sqlite) = make_engine_state();
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

    // registry 热数据：task.status 保持出生值 pending（内核不补 completed）
    let reg = agentos_session::global_registry();
    let entry = reg
        .get("tenant_unify", "pipe_unify")
        .expect("registry 应有该管道");
    let st = entry.read();
    assert_eq!(
        st.state["task.status"],
        json!("pending"),
        "run 终态不得回写 task.status（任务域插件裁决）"
    );
    assert!(
        st.state.get("task.ended_at").is_none(),
        "run 终态不得写 task.ended_at"
    );
    drop(st);

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
    let (state, _invoker, store, _sqlite) = make_engine_state();
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

    // registry 热路径：不得出现 task.status/task.ended_at
    let reg = agentos_session::global_registry();
    let entry = reg
        .get("tenant_owned_only", "pipe_owned_only")
        .expect("registry 应有该管道");
    let st = entry.read();
    assert!(
        st.state.get("task.status").is_none(),
        "owned-only 管道不得回写 task.status，实际 {:?}",
        st.state.get("task.status")
    );
    assert!(
        st.state.get("task.ended_at").is_none(),
        "owned-only 管道不得回写 task.ended_at，实际 {:?}",
        st.state.get("task.ended_at")
    );
    drop(st);

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
    let (state, _invoker, store, _sqlite) = make_engine_state();

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

    // ③ registry 热路径：任务状态保持出生值 pending（run 终态不写任务状态）
    let reg = agentos_session::global_registry();
    let entry = reg
        .get("tenant_lifecycle", pipeline_id)
        .expect("registry 应有管道");
    let st = entry.read();
    assert_eq!(
        st.state["task.status"],
        json!("pending"),
        "run 终态不得回写 task.status（任务域插件裁决）"
    );
    assert!(
        st.state.get("task.ended_at").is_none(),
        "run 终态不得写 task.ended_at"
    );
    // 出生字段保留（goal/scope/lineage）
    assert_eq!(st.state["task.goal"], "全流程验证");
    assert_eq!(st.state["task.scope"], "non_container");
    assert_eq!(st.state["lineage.root"], true);
    drop(st);

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

    let (mut state, _invoker, store, _sqlite) = make_engine_state();
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
    let (state, _invoker, store, _sqlite) = make_engine_state();
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
    let (state, _invoker, store, _sqlite) = make_engine_state();
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
    )
    .await;
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
    )
    .await;
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
        let out =
            stage_recover_history(st, &store, "ok", "p_it", "tenant_it", incoming_cmid, false)
                .await;
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
        )
        .await;
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
    )
    .await;
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
    )
    .await;
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
    let (state, _invoker, store, _sqlite) = make_engine_state();
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
    // 无 store（AppState::new()）：GET → 404（store not injected）
    let app = build_router(AppState::new());
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/pipelines/pipe-x/pending-inputs")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);

    // 有 store：PUT 空 content → 400
    let (state, _invoker, _store, _sqlite) = make_engine_state();
    let app = build_router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/api/v1/pipelines/pipe-x/pending-inputs/abc")
                .header("content-type", "application/json")
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
    let (state, _invoker, _store, _sqlite) = make_engine_state();
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

    // 父管道轨迹携带对话挂起控制态（事故现场同款）
    seed_pipeline_trace(
        &store,
        "pipe-parent",
        "run-parent",
        json!({"conversation_mode": true, "core_type": "tool_execute",
               "core_plugin": "pipeline_tool_core"}),
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

    // 子任务冷恢复：初始 state 是新鲜默认（core_type=llm_call）
    let initial = json!({"core_type": "llm_call", "core_plugin": "pipeline_llm_core"});
    let recovered = super::stage_recover_history(
        initial,
        &(store.clone() as Arc<dyn agentos_core::traits::StorageBackend>),
        "kickoff",
        "pipe-child",
        "default",
        "",
        true,
    )
    .await;

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

    // 父管道自身冷恢复仍能拿回自己的控制态（作用域收窄不丢自己的历史）
    let parent_recovered = super::stage_recover_history(
        json!({}),
        &(store as Arc<dyn agentos_core::traits::StorageBackend>),
        "resume",
        "pipe-parent",
        "default",
        "",
        true,
    )
    .await;
    assert_eq!(
        parent_recovered.get("conversation_mode"),
        Some(&json!(true)),
        "本管道历史控制态必须完整恢复"
    );
}
