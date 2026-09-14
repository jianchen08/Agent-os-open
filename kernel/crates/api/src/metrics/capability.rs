//! metrics-admin capability handler——指标读面（boot-plugin 第三刀）。
//!
//! 拆分原则（对齐 db-admin 第一刀，boot-plugin 立项 §五）：写面
//! （`metrics.record`——插件上报指标的反向调用，热路径）留内核
//! `KernelCapabilityRouter` 内置 match 不动；读面（查询/枚举/导出）
//! 收敛为本 `metrics-admin` namespace 的 [`CapabilityHandler`]，注册进
//! 内核 `CapabilityHandlerRegistry`（agentos-kernel.rs 启动期，先于任何
//! sidecar spawn）。HTTP 面由 `plugins/shared/metrics_admin`（Python
//! sidecar 插件）承载：内核 `/ext/{*rest}` 通配分发 → 插件 `http.handle`
//! → 反向调用 `metrics-admin.<method>` → 本 handler。
//!
//! ## method 清单（3 个）
//!
//! | method | 原端点 | 语义 |
//! |---|---|---|
//! | `query` | GET /api/v1/metrics（已迁除） | 过滤查询（plugin/metric/window/labels），响应形状与拆分前逐字段一致 |
//! | `list` | —（新增） | 枚举 series 元信息（不带 samples，轻量目录面） |
//! | `prometheus` | GET /metrics（内核保留） | Prometheus exposition format 全量快照（插件面副本；内核 /metrics 为运维契约保留） |
//!
//! ## 鉴权落点（信任锚点，对齐 db-admin capability.rs）
//!
//! HTTP 面插件**只透传凭证、不做鉴权决策**：入站 `Authorization` 头原样
//! 进 params 的 `_authorization` 字段；本 handler 在内核侧重建 HeaderMap
//! 后走 `resolve_request_user`，要求 admin 或 viewer 角色（读面；拆分前
//! /api/v1/metrics 无鉴权，插件面收敛为角色读——能力出内核必须带门）。
//! manifest 的 `http_endpoints[].auth: "admin"` 由内核 dispatcher 执行（W3-2/D2
//! 第一刀），本 handler 内再校验一次（纵深防御，语义一致）。
//!
//! ## 响应信封（与 db-admin 一致）
//!
//! 成功 `{status: 200, body}`；业务失败 `{status, error: {code, message}}`
//! （401/403/404）。`prometheus` 的 body 是 exposition 文本字符串（插件
//! 侧以 text/plain 返回）。
//!
//! [来源: docs/working/重要设计/boot-plugin内核能力插件化立项.md §二/§五]

use std::sync::Arc;
use std::time::Duration;

use agentos_http::auth::resolve_request_user;
use agentos_http::error::ApiError;
use agentos_mcp::{CapabilityHandler, McpError};
use async_trait::async_trait;
use axum::http::{header::AUTHORIZATION, HeaderMap, HeaderValue};
use serde_json::{json, Value};

use super::aggregator::MetricSeriesView;
use super::{export_prometheus, Labels, MetricsAggregator};

/// metrics-admin capability 的 namespace（manifest granted_capabilities 与此对齐）。
pub const NAMESPACE: &str = "metrics-admin";

/// `metrics-admin` namespace 的 capability handler（读面 3 method）。
///
/// `agg: None` = 聚合器未注入（handler 返回 404 信封，诚实降级）。
pub struct MetricsAdminCapabilityHandler {
    /// 用户/角色解析用的存储后端（api `AppState.store` 同一实例）。
    store: Option<Arc<dyn agentos_core::traits::StorageBackend>>,
    /// 指标聚合器（与 KernelCapabilityRouter / AppState.metrics 同一实例，Clone 共享内部 Arc）。
    agg: Option<MetricsAggregator>,
}

impl MetricsAdminCapabilityHandler {
    /// 创建 handler。
    ///
    /// Args:
    /// - `store`: 用户/租户解析用的存储后端；
    /// - `agg`: 指标聚合器（生产为 bin 创建的同一实例）。
    pub fn new(
        store: Option<Arc<dyn agentos_core::traits::StorageBackend>>,
        agg: Option<MetricsAggregator>,
    ) -> Self {
        Self { store, agg }
    }

    /// 从 params 的 `_authorization`（HTTP 面插件转发的原始 Authorization 头值）
    /// 重建 HeaderMap，复用既有 resolve_request_user 鉴权链（对齐 db-admin 模式）。
    fn auth_headers(params: &Value) -> HeaderMap {
        let mut headers = HeaderMap::new();
        if let Some(auth) = params.get("_authorization").and_then(|v| v.as_str()) {
            if let Ok(v) = HeaderValue::from_str(auth) {
                headers.insert(AUTHORIZATION, v);
            }
        }
        headers
    }

    /// 读面角色校验：admin 或 viewer（对齐 db-admin require_read_role）。
    async fn require_read_role(&self, headers: &HeaderMap) -> Result<(), ApiError> {
        let (_, _, role, _) = resolve_request_user(self.store.as_ref(), headers).await?;
        if role != "admin" && role != "viewer" {
            return Err(ApiError::Forbidden {
                message: "需要 admin 或 viewer 角色".to_string(),
            });
        }
        Ok(())
    }

    /// 聚合器可用性检查（None → 404，对齐拆分前 "aggregator not enabled" 语义）。
    fn agg(&self) -> Result<&MetricsAggregator, ApiError> {
        self.agg.as_ref().ok_or_else(|| ApiError::NotFound {
            message: "metrics aggregator not enabled".to_string(),
        })
    }

    /// `query`：过滤查询（原 GET /api/v1/metrics，响应形状逐字段一致）。
    async fn query(&self, params: &Value) -> Result<(u16, Value), ApiError> {
        let headers = Self::auth_headers(params);
        self.require_read_role(&headers).await?;
        let agg = self.agg()?;
        let window = params
            .get("window")
            .and_then(|v| v.as_str())
            .map(parse_window)
            .unwrap_or_else(|| Duration::from_secs(60 * 60));
        let labels_filter = params
            .get("labels")
            .and_then(|v| v.as_str())
            .map(parse_labels_query)
            .unwrap_or_default();
        let views = agg.query(
            params.get("plugin").and_then(|v| v.as_str()),
            params.get("metric").and_then(|v| v.as_str()),
            Some(window),
            &labels_filter,
        );
        let metrics: Vec<Value> = views.iter().map(series_to_json).collect();
        Ok((200, json!({ "metrics": metrics })))
    }

    /// `list`：枚举 series 元信息（不带 samples——目录面，避免大响应）。
    async fn list(&self, params: &Value) -> Result<(u16, Value), ApiError> {
        let headers = Self::auth_headers(params);
        self.require_read_role(&headers).await?;
        let agg = self.agg()?;
        let views = agg.query(
            params.get("plugin").and_then(|v| v.as_str()),
            params.get("metric").and_then(|v| v.as_str()),
            None,
            &Labels::new(),
        );
        let series: Vec<Value> = views
            .iter()
            .map(|v| {
                json!({
                    "plugin_id": v.plugin_id,
                    "name": v.name,
                    "type": v.metric_type.as_str(),
                    "labels": serde_json::to_value(&v.labels).unwrap_or(json!({})),
                    "unit": v.unit,
                    "help": v.help,
                    "latest": v.latest,
                })
            })
            .collect();
        let total = series.len();
        Ok((200, json!({ "series": series, "total": total })))
    }

    /// `prometheus`：Prometheus exposition format 全量快照（body 为文本字符串）。
    async fn prometheus(&self, params: &Value) -> Result<(u16, Value), ApiError> {
        let headers = Self::auth_headers(params);
        self.require_read_role(&headers).await?;
        let agg = self.agg()?;
        let views = agg.snapshot();
        Ok((200, Value::String(export_prometheus(&views))))
    }
}

/// 单条 series → 响应 JSON（与拆分前 MetricSeriesResponse 逐字段一致）。
fn series_to_json(v: &MetricSeriesView) -> Value {
    json!({
        "plugin_id": v.plugin_id,
        "name": v.name,
        "type": v.metric_type.as_str(),
        "labels": serde_json::to_value(&v.labels).unwrap_or(json!({})),
        "samples": v.samples.iter().map(|s| json!({"ts": s.ts, "value": s.value})).collect::<Vec<_>>(),
        "unit": v.unit,
        "latest": v.latest,
    })
}

/// 解析 window 字符串为 Duration（原 routes.rs 同名函数迁入，语义不变）。
fn parse_window(s: &str) -> Duration {
    match s.trim() {
        "5m" => Duration::from_secs(5 * 60),
        "1h" => Duration::from_secs(60 * 60),
        "24h" => Duration::from_secs(24 * 60 * 60),
        _ => Duration::from_secs(60 * 60), // 默认 1h
    }
}

/// 解析 labels 查询串（"model:deepseek,region:us"，原 routes.rs 同名函数迁入）。
fn parse_labels_query(s: &str) -> Labels {
    let mut out = Labels::new();
    for pair in s.split(',') {
        let pair = pair.trim();
        if let Some((k, v)) = pair.split_once(':') {
            let k = k.trim();
            let v = v.trim();
            if !k.is_empty() {
                out.insert(k.to_string(), v.to_string());
            }
        }
    }
    out
}

/// ApiError → (HTTP 状态码, 消息)（与 db-admin capability.rs 同一映射）。
fn api_error_parts(e: &ApiError) -> (u16, String) {
    match e {
        ApiError::BadRequest { message } => (400, message.clone()),
        ApiError::Unauthorized { message } => (401, message.clone()),
        ApiError::Forbidden { message } => (403, message.clone()),
        ApiError::NotFound { message } => (404, message.clone()),
        ApiError::Conflict { message } => (409, message.clone()),
        ApiError::UnprocessableEntity { message } => (422, message.clone()),
        ApiError::TooManyRequests { message, .. } => (429, message.clone()),
        ApiError::Internal { message } | ApiError::WebSocket { message } => (500, message.clone()),
        ApiError::ServiceUnavailable { message } => (503, message.clone()),
    }
}

#[async_trait]
impl CapabilityHandler for MetricsAdminCapabilityHandler {
    fn namespace(&self) -> &str {
        NAMESPACE
    }

    async fn handle(&self, method: &str, params: Value) -> Result<Value, McpError> {
        let result: Result<(u16, Value), ApiError> = match method {
            "query" => self.query(&params).await,
            "list" => self.list(&params).await,
            "prometheus" => self.prometheus(&params).await,
            other => {
                return Err(McpError::Protocol {
                    message: format!(
                        "{NAMESPACE}.{other} not implemented (known: query, list, prometheus)"
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

    /// 带样本数据的 handler（无 store：token 校验走内置回退）。
    fn handler_with_data() -> MetricsAdminCapabilityHandler {
        let agg = MetricsAggregator::new();
        let mut lbl = Labels::new();
        lbl.insert("model".to_string(), "deepseek".to_string());
        agg.record(
            "llm_service",
            "tokens_used",
            super::super::aggregator::MetricType::Counter,
            12800.0,
            &lbl,
            Some("tokens"),
            Some("Total tokens used"),
        );
        agg.record(
            "llm_service",
            "latency",
            super::super::aggregator::MetricType::Histogram,
            0.02,
            &Labels::new(),
            Some("seconds"),
            Some("LLM latency"),
        );
        MetricsAdminCapabilityHandler::new(None, Some(agg))
    }

    #[tokio::test]
    async fn query_without_auth_returns_401_envelope() {
        let handler = handler_with_data();
        let envelope = handler.handle("query", json!({})).await.unwrap();
        assert_eq!(envelope["status"], 401, "无凭证应 401: {envelope}");
    }

    #[tokio::test]
    async fn query_with_admin_returns_series_matching_legacy_shape() {
        let handler = handler_with_data();
        let envelope = handler
            .handle("query", authed(json!({ "plugin": "llm_service" })))
            .await
            .unwrap();
        assert_eq!(envelope["status"], 200, "{envelope}");
        let metrics = envelope["body"]["metrics"].as_array().unwrap();
        assert_eq!(metrics.len(), 2, "{envelope}");
        // 聚合器 HashMap 迭代序不定——按 name 定位（拆分前 /api/v1/metrics 同样不排序）
        let tokens = metrics
            .iter()
            .find(|m| m["name"] == "tokens_used")
            .expect("tokens_used series");
        // 响应形状与拆分前 /api/v1/metrics 逐字段一致
        assert_eq!(tokens["plugin_id"], "llm_service");
        assert_eq!(tokens["type"], "counter");
        assert_eq!(tokens["labels"]["model"], "deepseek");
        assert!(!tokens["samples"].as_array().unwrap().is_empty());
    }

    #[tokio::test]
    async fn query_filters_by_metric_and_labels() {
        let handler = handler_with_data();
        let envelope = handler
            .handle(
                "query",
                authed(json!({ "metric": "tokens_used", "labels": "model:deepseek" })),
            )
            .await
            .unwrap();
        assert_eq!(envelope["status"], 200, "{envelope}");
        let metrics = envelope["body"]["metrics"].as_array().unwrap();
        assert_eq!(metrics.len(), 1);
        assert_eq!(metrics[0]["name"], "tokens_used");
    }

    #[tokio::test]
    async fn list_returns_metadata_without_samples() {
        let handler = handler_with_data();
        let envelope = handler.handle("list", authed(json!({}))).await.unwrap();
        assert_eq!(envelope["status"], 200, "{envelope}");
        let series = envelope["body"]["series"].as_array().unwrap();
        assert_eq!(envelope["body"]["total"], 2);
        assert!(
            series.iter().all(|s| s.get("samples").is_none()),
            "list 不带 samples"
        );
    }

    #[tokio::test]
    async fn prometheus_returns_exposition_text() {
        let handler = handler_with_data();
        let envelope = handler
            .handle("prometheus", authed(json!({})))
            .await
            .unwrap();
        assert_eq!(envelope["status"], 200, "{envelope}");
        let text = envelope["body"].as_str().unwrap();
        assert!(text.contains("# TYPE llm_service_tokens_used counter"));
        assert!(text.contains("# TYPE llm_service_latency histogram"));
    }

    #[tokio::test]
    async fn no_aggregator_returns_404_envelope() {
        let handler = MetricsAdminCapabilityHandler::new(None, None);
        let envelope = handler.handle("query", authed(json!({}))).await.unwrap();
        assert_eq!(envelope["status"], 404, "{envelope}");
    }

    #[tokio::test]
    async fn unknown_method_rejected() {
        let handler = handler_with_data();
        let err = handler.handle("record", json!({})).await;
        assert!(err.is_err(), "写面 record 不在读面 handler，应拒绝");
        assert!(format!("{}", err.unwrap_err()).contains("not implemented"));
    }

    #[tokio::test]
    async fn registry_route_via_trait_roundtrip() {
        // 经 CapabilityHandlerRegistry（生产 reader loop 的真实路由路径）验证注册即路由。
        let registry = Arc::new(agentos_mcp::CapabilityHandlerRegistry::new());
        registry.register(Arc::new(handler_with_data()));
        assert!(registry.has_namespace(NAMESPACE));
        let router: Arc<dyn CapabilityRouter> = registry;
        let envelope = router
            .handle(NAMESPACE, "query", authed(json!({})))
            .await
            .unwrap();
        assert_eq!(envelope["status"], 200);
    }

    // ── 读面角色闸：非 admin/viewer（如普通 user）必须 403 ──

    /// 播种一个在库用户（角色可指定），返回 (库句柄, 该用户的 access token)。
    /// token 与库内记录同源（用户名 + 口令哈希绑定一致，否则先撞口令绑定校验）。
    async fn store_with_role_user(role: &str) -> (Arc<agentos_engine::SqliteStore>, String) {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let password = agentos_http::auth::hash_password("metrics-test-pw").unwrap();
        let record = agentos_core::types::UserRecord {
            user_id: "00000000-0000-0000-0000-0000000000aa".to_string(),
            username: "ops_user".to_string(),
            password: password.clone(),
            email: None,
            role: role.to_string(),
            tenant_id: agentos_http::auth::DEFAULT_TENANT_ID.to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_login_at: None,
            must_change_password: false,
        };
        agentos_core::traits::StorageBackend::create_user(store.as_ref(), &record)
            .await
            .unwrap();
        let built = agentos_http::auth::BuiltInUser {
            id: record.user_id.clone(),
            username: record.username.clone(),
            password,
            email: String::new(),
            role: role.to_string(),
            tenant_id: record.tenant_id.clone(),
            created_at: String::new(),
            must_change_password: false,
        };
        let token = encode_token(TokenType::Access, &built, 3600);
        (store, token)
    }

    /// 角色解析真值源是用户记录（非 token 内嵌角色）：角色为 user 的合法凭证
    /// 在三个读面方法一律 403（读面收敛为角色读——能力出内核必须带门）。
    #[tokio::test]
    async fn non_admin_viewer_role_forbidden_on_all_read_methods() {
        let (store, token) = store_with_role_user("user").await;
        let agg = MetricsAggregator::new();
        let handler = MetricsAdminCapabilityHandler::new(Some(store), Some(agg));
        for method in ["query", "list", "prometheus"] {
            let mut params = json!({});
            params["_authorization"] = json!(format!("Bearer {token}"));
            let envelope = handler.handle(method, params).await.unwrap();
            assert_eq!(envelope["status"], 403, "{method} 应 403: {envelope}");
            assert_eq!(
                envelope["error"]["message"], "需要 admin 或 viewer 角色",
                "{method} 拒绝文案"
            );
        }
    }

    /// viewer 角色（非 admin）读面放行——只认 admin 会把监控只读账号误伤。
    #[tokio::test]
    async fn viewer_role_allowed_on_read_methods() {
        let (store, token) = store_with_role_user("viewer").await;
        let handler =
            MetricsAdminCapabilityHandler::new(Some(store), Some(MetricsAggregator::new()));
        for method in ["query", "list", "prometheus"] {
            let mut params = json!({});
            params["_authorization"] = json!(format!("Bearer {token}"));
            let envelope = handler.handle(method, params).await.unwrap();
            assert_eq!(
                envelope["status"], 200,
                "{method} viewer 应放行: {envelope}"
            );
        }
    }

    /// 无法解析的 Authorization 头（非 HeaderValue 合法字节）→ 401 信封，
    /// 不 panic（auth_headers 的 from_str 失败分支）。
    #[tokio::test]
    async fn malformed_authorization_header_yields_401() {
        let handler = handler_with_data();
        let envelope = handler
            .handle("query", json!({"_authorization": "Bearer \u{7f}\u{1}"}))
            .await
            .unwrap();
        assert_eq!(envelope["status"], 401, "{envelope}");
    }

    // ── window / labels / ApiError 映射（纯函数面）──

    /// `parse_window` 全档：5m / 1h / 24h / 未知 / 带空白，未知回退 1h。
    #[test]
    fn parse_window_covers_all_branches() {
        for (input, want) in [
            ("5m", Duration::from_secs(5 * 60)),
            ("1h", Duration::from_secs(60 * 60)),
            ("24h", Duration::from_secs(24 * 60 * 60)),
            ("weird", Duration::from_secs(60 * 60)),
            ("", Duration::from_secs(60 * 60)),
            ("  5m  ", Duration::from_secs(5 * 60)),
        ] {
            assert_eq!(parse_window(input), want, "window={input:?}");
        }
    }

    /// `parse_labels_query`：空段/无冒号/空 key 各自跳过，合法对入列。
    #[test]
    fn parse_labels_query_skips_malformed_pairs() {
        let got = parse_labels_query("model:deepseek,,region:us,:novalue, nocolon ,k: v ");
        assert_eq!(got.get("model").map(String::as_str), Some("deepseek"));
        assert_eq!(got.get("region").map(String::as_str), Some("us"));
        assert_eq!(got.get("k").map(String::as_str), Some("v"));
        assert_eq!(got.len(), 3, "畸形段不得入列: {got:?}");
        assert!(parse_labels_query("").is_empty());
    }

    /// 查询参数透传：window=5m 限窗、labels 过滤生效（真实聚合器数据驱动）。
    #[tokio::test]
    async fn query_applies_window_and_labels_filters() {
        let agg = MetricsAggregator::new();
        let mut lbl = Labels::new();
        lbl.insert("model".to_string(), "deepseek".to_string());
        agg.record(
            "llm_service",
            "tokens_used",
            super::super::aggregator::MetricType::Counter,
            1.0,
            &lbl,
            None,
            None,
        );
        let handler = MetricsAdminCapabilityHandler::new(None, Some(agg));

        let envelope = handler
            .handle(
                "query",
                authed(json!({"window": "5m", "labels": "model:deepseek"})),
            )
            .await
            .unwrap();
        assert_eq!(envelope["status"], 200, "{envelope}");
        assert_eq!(envelope["body"]["metrics"].as_array().unwrap().len(), 1);

        // 不匹配的 labels 过滤 → 空结果（证明过滤真在生效，非恒真）
        let envelope = handler
            .handle(
                "query",
                authed(json!({"window": "5m", "labels": "model:other"})),
            )
            .await
            .unwrap();
        assert_eq!(
            envelope["body"]["metrics"].as_array().unwrap().len(),
            0,
            "不匹配 labels 应过滤掉: {envelope}"
        );
    }

    /// `api_error_parts` 全档映射（与 db-admin capability 同一映射表）。
    #[test]
    fn api_error_parts_covers_all_variants() {
        let msg = |s: &str| s.to_string();
        let cases: Vec<(ApiError, u16)> = vec![
            (ApiError::BadRequest { message: msg("a") }, 400),
            (ApiError::Unauthorized { message: msg("b") }, 401),
            (ApiError::Forbidden { message: msg("c") }, 403),
            (ApiError::NotFound { message: msg("d") }, 404),
            (ApiError::Conflict { message: msg("e") }, 409),
            (ApiError::UnprocessableEntity { message: msg("f") }, 422),
            (
                ApiError::TooManyRequests {
                    message: msg("g"),
                    retry_after_secs: 7,
                },
                429,
            ),
            (ApiError::Internal { message: msg("h") }, 500),
            (ApiError::WebSocket { message: msg("i") }, 500),
            (ApiError::ServiceUnavailable { message: msg("j") }, 503),
        ];
        for (err, want) in cases {
            let (status, message) = api_error_parts(&err);
            assert_eq!(status, want, "状态码映射错误: {err:?}");
            assert!(
                message.len() == 1 && message.as_bytes()[0] >= b'a',
                "消息应原样透传（不夹带状态码前缀）: {message:?}"
            );
        }
    }

    /// no_aggregator 在 list/prometheus 同样 404（不是只有 query 有闸）。
    #[tokio::test]
    async fn no_aggregator_returns_404_on_all_methods() {
        let handler = MetricsAdminCapabilityHandler::new(None, None);
        for method in ["query", "list", "prometheus"] {
            let envelope = handler.handle(method, authed(json!({}))).await.unwrap();
            assert_eq!(envelope["status"], 404, "{method}: {envelope}");
        }
    }

    /// store 注入但用户不存在（store 在而未命中不回退内置表，K4）→ 401，
    /// 不是静默放行内置 admin。
    #[tokio::test]
    async fn store_without_matching_user_rejects_builtin_token() {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let handler =
            MetricsAdminCapabilityHandler::new(Some(store), Some(MetricsAggregator::new()));
        let envelope = handler.handle("query", authed(json!({}))).await.unwrap();
        assert_eq!(
            envelope["status"], 401,
            "store 在场未命中不得回退内置表: {envelope}"
        );
    }
}
