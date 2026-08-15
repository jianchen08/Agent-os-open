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
//! manifest 的 `http_endpoints[].auth: "admin"` 是声明性字段，实际执行
//! 点在本 handler。
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
}
