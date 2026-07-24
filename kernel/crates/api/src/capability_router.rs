//! Capability 路由器实现——处理 sidecar 插件反向调用内核能力。
//!
//! 持有引擎句柄，把 sidecar 的 `<capability>.<method>` 反向调用路由到内核实现：
//! - pipeline-executor.{suspend, resume, start_run} → AdrEngine
//! - event-bus.emit → 广播事件（当前记录日志，前端推送留 P1）
//! - config-reader.get → 读取配置节（从 AppState 配置缓存）
//! - metrics.record → 写入指标聚合器（监控设计 §三 通道2，第 6 个 capability）
//!
//! [来源: ROADMAP.md 审批暂停/恢复、复盘调管道的前置地基]
//! [来源: docs/working/重要设计/插件监控与指标机制设计.md §三 通道2]

use std::sync::Arc;

use async_trait::async_trait;
use agentos_core::traits::AdrEngine;
use agentos_mcp::{CapabilityRouter, McpError};
use serde_json::{json, Value};
use tracing::warn;

use crate::metrics::{Labels, MetricType, MetricsAggregator};

/// 管道执行能力错误码前缀。
const ERR_PIPELINE: i64 = -32010;

/// Capability 路由器实现。
pub struct KernelCapabilityRouter {
    /// 管道引擎（处理 pipeline-executor.* 调用）
    engine: Arc<dyn AdrEngine>,
    /// 指标聚合器（处理 metrics.record 调用，监控设计 §三 通道2）。
    /// None = 不接受插件指标上报（聚合器未启用）。
    metrics: Option<MetricsAggregator>,
}

impl KernelCapabilityRouter {
    /// 创建路由器（不带指标聚合器，兼容旧调用方）。
    pub fn new(engine: Arc<dyn AdrEngine>) -> Self {
        Self {
            engine,
            metrics: None,
        }
    }

    /// 创建带指标聚合器的路由器（生产用，启用 metrics.record 反向调用）。
    pub fn with_metrics(engine: Arc<dyn AdrEngine>, metrics: MetricsAggregator) -> Self {
        Self {
            engine,
            metrics: Some(metrics),
        }
    }
}

#[async_trait]
impl CapabilityRouter for KernelCapabilityRouter {
    async fn handle(
        &self,
        capability: &str,
        method: &str,
        params: Value,
    ) -> Result<Value, McpError> {
        match (capability, method) {
            // ── pipeline-executor：暂停/恢复/启动管道 ──
            ("pipeline-executor", "suspend") => {
                let run_id = params
                    .get("run_id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| McpError::Protocol {
                        message: "suspend 缺少 run_id 参数".to_string(),
                    })?;
                let handle = self.engine.suspend(run_id).await.map_err(|e| McpError::Protocol {
                    message: format!("suspend 失败: {e}"),
                })?;
                // 返回完整 handle，sidecar resume 时需回传全部字段
                Ok(json!({
                    "status": "suspended",
                    "run_id": handle.run_id,
                    "branch_id": handle.branch_id,
                    "seq": handle.seq,
                }))
            }
            ("pipeline-executor", "resume") => {
                // resume 需要完整的 SuspendHandle（run_id + branch_id + seq）。
                // sidecar 在 suspend 时拿到 handle，resume 时回传完整字段。
                let run_id = params
                    .get("run_id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| McpError::Protocol {
                        message: "resume 缺少 run_id 参数".to_string(),
                    })?;
                let handle = agentos_core::types::SuspendHandle {
                    run_id: run_id.to_string(),
                    branch_id: params
                        .get("branch_id")
                        .and_then(|v| v.as_str())
                        .unwrap_or("main")
                        .to_string(),
                    seq: params
                        .get("seq")
                        .and_then(|v| v.as_u64())
                        .unwrap_or(0) as u32,
                };
                self.engine
                    .resume(&handle, agentos_core::types::WakeEvent::Manual)
                    .await
                    .map_err(|e| McpError::Protocol {
                        message: format!("resume 失败: {e}"),
                    })?;
                Ok(json!({"status": "resumed", "run_id": run_id}))
            }
            ("pipeline-executor", "start_run") => {
                let run_id = self
                    .engine
                    .start_run(&params)
                    .await
                    .map_err(|e| McpError::Protocol {
                        message: format!("start_run 失败: {e}"),
                    })?;
                Ok(json!({"status": "started", "run_id": run_id}))
            }

            // ── event-bus：发事件/通知（当前记录日志，前端推送留 P1-2 审批接线）──
            ("event-bus", "emit") => {
                let event_name = params
                    .get("event")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                // DEBT: 前端 WS 推送在 P1-2 审批闭环接线时实现。ceiling: 当前仅日志。
                // upgrade: 接入 AppState 的 WS 广播通道。
                tracing::info!(
                    target: "capability:event-bus",
                    "plugin event: {} payload={}",
                    event_name,
                    params.get("payload").unwrap_or(&serde_json::Value::Null)
                );
                Ok(json!({"status": "emitted", "event": event_name}))
            }

            // ── config-reader：读配置节（P1 后为显式 no-op fallback）──
            // task_11 P1 已把配置注入改到源头：manifest.config_files → invoker
            // build_injected_config 在 spawn sidecar 时下发，插件经 plugin.get_config()
            // 直接拿到自己的命名空间配置，不再需要反向调用 config-reader.get。
            // 本 capability 名仍是 SDK 公共契约（STANDARD_CAPABILITIES），故保留 no-op
            // 兜底（返回 null value）。config_refs 已于 P6 删除，配置只走 config_files。
            ("config-reader", "get") => {
                let key = params
                    .get("key")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                Ok(json!({"key": key, "value": null}))
            }

            // ── metrics：插件上报指标（监控设计 §三 通道2，第 6 个 capability）──
            // sidecar 调 ctx.record_metric(name, value, metric_type, labels) →
            // 经 PluginScopedRouter 注入 _plugin_id（信任锚点）→ 这里写入聚合器。
            // 命名空间：内核用 plugin_id 作 series 的 plugin_id 字段（监控设计 §九），
            // 不在 metric name 里加前缀，避免与 series.plugin_id 冗余。
            ("metrics", "record") => {
                let agg = self.metrics.as_ref().ok_or_else(|| McpError::Protocol {
                    message: "metrics aggregator not enabled".to_string(),
                })?;
                // plugin_id 来自 invoker 注入的 _plugin_id（不可被 sidecar 伪造——
                // invoker 用 manifest.id 设置，sidecar 无法覆盖信任锚点）。
                let plugin_id = params
                    .get("_plugin_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown")
                    .to_string();
                let name = params
                    .get("name")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| McpError::Protocol {
                        message: "metrics.record 缺少 name 参数".to_string(),
                    })?;
                let value = params
                    .get("value")
                    .and_then(|v| v.as_f64())
                    .ok_or_else(|| McpError::Protocol {
                        message: "metrics.record 缺少或非法 value 参数".to_string(),
                    })?;
                let metric_type = match params
                    .get("metric_type")
                    .and_then(|v| v.as_str())
                    .unwrap_or("counter")
                {
                    "counter" => MetricType::Counter,
                    "gauge" => MetricType::Gauge,
                    "histogram" => MetricType::Histogram,
                    other => {
                        return Err(McpError::Protocol {
                            message: format!("unknown metric_type: {other}"),
                        });
                    }
                };
                // labels：限长 + 禁特殊字符（监控设计 §十，防聚合器内存爆炸）
                let labels = parse_labels_safe(params.get("labels"))?;
                let unit = params
                    .get("unit")
                    .and_then(|v| v.as_str())
                    .map(str::to_string);
                let help = params
                    .get("help")
                    .and_then(|v| v.as_str())
                    .map(str::to_string);
                agg.record(
                    &plugin_id,
                    name,
                    metric_type,
                    value,
                    &labels,
                    unit.as_deref(),
                    help.as_deref(),
                );
                Ok(json!({"status": "recorded", "plugin_id": plugin_id, "name": name}))
            }

            // tenant-context / logger 暂未实现具体 method（P0-4 多租户时补 tenant-context）
            (cap, m) => {
                warn!(
                    "unhandled capability call: {}.{} (params={})",
                    cap, m, params
                );
                Err(McpError::Protocol {
                    message: format!("capability method not implemented: {cap}.{m}"),
                })
            }
        }
    }
}

/// 解析 labels 并做注入防护（监控设计 §十）。
/// - 限制：最多 20 个 label，每个 key/value 最长 256 字符。
/// - 禁止 value 含换行/双引号（Prometheus 导出安全）。
fn parse_labels_safe(raw: Option<&Value>) -> Result<Labels, McpError> {
    let mut out = Labels::new();
    let Some(obj) = raw.and_then(|v| v.as_object()) else {
        return Ok(out);
    };
    if obj.len() > 20 {
        return Err(McpError::Protocol {
            message: "too many labels (max 20)".to_string(),
        });
    }
    for (k, v) in obj {
        if k.len() > 256 {
            return Err(McpError::Protocol {
                message: format!("label key too long: {k}"),
            });
        }
        let val = v.as_str().unwrap_or("");
        if val.len() > 256 {
            return Err(McpError::Protocol {
                message: format!("label value too long for key: {k}"),
            });
        }
        // 禁换行/双引号（Prometheus exposition 安全，监控设计 §十）
        if val.contains('\n') || val.contains('"') {
            return Err(McpError::Protocol {
                message: format!("label value contains forbidden char (newline/dquote) for key: {k}"),
            });
        }
        out.insert(k.clone(), val.to_string());
    }
    Ok(out)
}

/// 抑制未使用的错误码常量警告（后续 event-bus 错误码扩展时启用）。
#[allow(dead_code)]
fn _pipeline_error_code() -> i64 {
    ERR_PIPELINE
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::AdrEngine;
    use agentos_core::types::{CompositeStep, EngineError, StepResult, SuspendHandle, WakeEvent};
    use serde_json::json;

    /// 不做任何事的 AdrEngine mock（metrics.record 测试不需要引擎）。
    struct StubEngine;
    #[async_trait]
    impl AdrEngine for StubEngine {
        async fn start_run(&self, _c: &Value) -> Result<String, EngineError> {
            Ok("stub".to_string())
        }
        async fn execute_step(
            &self,
            _: &str,
            _: &CompositeStep,
        ) -> Result<StepResult, EngineError> {
            unimplemented!()
        }
        async fn suspend(&self, _: &str) -> Result<SuspendHandle, EngineError> {
            unimplemented!()
        }
        async fn resume(&self, _: &SuspendHandle, _: WakeEvent) -> Result<(), EngineError> {
            unimplemented!()
        }
        async fn rollback(&self, _: &str, _: u32) -> Result<String, EngineError> {
            unimplemented!()
        }
        async fn end_run(&self, _: &str) -> Result<(), EngineError> {
            Ok(())
        }
    }

    fn router_with_metrics() -> (KernelCapabilityRouter, MetricsAggregator) {
        let agg = MetricsAggregator::new();
        let r = KernelCapabilityRouter::with_metrics(Arc::new(StubEngine), agg.clone());
        (r, agg)
    }

    #[tokio::test]
    async fn test_metrics_record_counter() {
        let (router, agg) = router_with_metrics();
        let params = json!({
            "_plugin_id": "llm_service",
            "name": "tokens_used",
            "value": 1280,
            "metric_type": "counter",
            "labels": {"model": "deepseek"},
            "unit": "tokens",
            "help": "Total tokens used"
        });
        let res = router.handle("metrics", "record", params).await.unwrap();
        assert_eq!(res["status"], "recorded");
        assert_eq!(res["plugin_id"], "llm_service");

        let views = agg.query(
            Some("llm_service"),
            Some("tokens_used"),
            None,
            &Labels::new(),
        );
        assert_eq!(views.len(), 1);
        assert_eq!(views[0].latest, Some(1280.0));
        assert_eq!(views[0].unit.as_deref(), Some("tokens"));
        // labels 透传
        assert_eq!(views[0].labels.get("model").unwrap(), "deepseek");
    }

    #[tokio::test]
    async fn test_metrics_record_accumulates_counter() {
        let (router, agg) = router_with_metrics();
        for _ in 0..3 {
            router
                .handle(
                    "metrics",
                    "record",
                    json!({"_plugin_id":"p1","name":"calls","value":10,"metric_type":"counter"}),
                )
                .await
                .unwrap();
        }
        let views = agg.query(Some("p1"), Some("calls"), None, &Labels::new());
        // 3 次 ×10 = 30（counter 累加）
        assert_eq!(views[0].latest, Some(30.0));
    }

    #[tokio::test]
    async fn test_metrics_record_gauge_overwrites() {
        let (router, agg) = router_with_metrics();
        router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id":"p1","name":"conn","value":10,"metric_type":"gauge"}),
            )
            .await
            .unwrap();
        router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id":"p1","name":"conn","value":7,"metric_type":"gauge"}),
            )
            .await
            .unwrap();
        let views = agg.query(Some("p1"), Some("conn"), None, &Labels::new());
        // gauge 同桶 avg：(10+7)/2 = 8.5
        assert!((views[0].latest.unwrap() - 8.5).abs() < 0.01);
    }

    #[tokio::test]
    async fn test_metrics_record_histogram() {
        let (router, agg) = router_with_metrics();
        router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id":"p1","name":"lat","value":0.02,"metric_type":"histogram"}),
            )
            .await
            .unwrap();
        let views = agg.query(Some("p1"), Some("lat"), None, &Labels::new());
        let h = views[0].histogram.as_ref().unwrap();
        assert_eq!(h.count, 1);
    }

    #[tokio::test]
    async fn test_metrics_record_without_aggregator_errors() {
        // 不带 metrics 的 router → metrics.record 报错
        let router = KernelCapabilityRouter::new(Arc::new(StubEngine));
        let res = router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id":"p1","name":"m","value":1.0}),
            )
            .await;
        assert!(res.is_err());
    }

    #[tokio::test]
    async fn test_metrics_record_rejects_too_many_labels() {
        let (router, _agg) = router_with_metrics();
        let mut labels = serde_json::Map::new();
        for i in 0..21 {
            labels.insert(format!("k{i}"), json!(i.to_string()));
        }
        let res = router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id":"p1","name":"m","value":1.0,"labels":labels}),
            )
            .await;
        assert!(res.is_err());
    }

    #[tokio::test]
    async fn test_metrics_record_rejects_newline_in_label() {
        let (router, _agg) = router_with_metrics();
        let res = router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id":"p1","name":"m","value":1.0,
                       "labels":{"k":"a\nb"}}),
            )
            .await;
        assert!(res.is_err(), "newline in label value must be rejected");
    }

    #[tokio::test]
    async fn test_metrics_record_unknown_type() {
        let (router, _agg) = router_with_metrics();
        let res = router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id":"p1","name":"m","value":1.0,"metric_type":"bogus"}),
            )
            .await;
        assert!(res.is_err());
    }

    #[tokio::test]
    async fn test_metrics_record_missing_name() {
        let (router, _agg) = router_with_metrics();
        let res = router
            .handle("metrics", "record", json!({"_plugin_id":"p1","value":1.0}))
            .await;
        assert!(res.is_err());
    }
}
