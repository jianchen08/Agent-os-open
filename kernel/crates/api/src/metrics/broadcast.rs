//! event_bus 采样广播高频指标（监控设计 §六 形态2 状态栏实时数字）。
//!
//! 内核每秒采样关键指标，包成 widget_event 广播给订阅了 statusBar 的连接：
//! ```jsonc
//! { "type":"widget_event",
//!   "data":{ "widget_id":"metrics_tick", "event":"tick",
//!            "data":{ "kernel.session.connections": 42, ... } },
//!   "sequence": 99 }
//! ```
//!
//! 复用 P2 event_bus（SessionCoordinator.emit_widget 的 Broadcast scope）。

use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Value};

use super::aggregator::MetricsAggregator;
use super::counters::KernelCounters;

/// 关键指标的"白名单"前缀——只广播这些，避免每秒推全量噪声（监控设计 §六 形态2）。
///
/// 命中其中任一前缀的 series 才进 widget_event data。
pub const BROADCAST_PREFIXES: &[&str] = &[
    "kernel.session.connections",
    "kernel.session.event_bus",
    "kernel.api.dispatcher",
    "kernel.engine.llm",
    "kernel.engine.tool",
    "process.alive",
];

/// 把聚合器快照里命中白名单的关键指标收集为扁平 {full_name: value} map。
///
/// full_name = `<plugin_id>.<name>`（与监控设计 §九 命名规范一致）。
pub fn collect_broadcast_snapshot(
    agg: &MetricsAggregator,
    kernel_counters: Option<&KernelCounters>,
) -> Value {
    let mut data = serde_json::Map::new();

    // 1. kernel 自采计数器（直接读，不经过聚合器时间桶，反映瞬时值）
    if let Some(kc) = kernel_counters {
        for snap in kc.snapshot() {
            let full = format!("kernel.{}", snap.name);
            if matches_prefix(&full) {
                data.insert(full, json!(snap.value));
            }
        }
    }

    // 2. 聚合器里其他插件的指标（取 latest）
    for view in agg.snapshot() {
        let full = format!("{}.{}", view.plugin_id, view.name);
        if matches_prefix(&full) {
            if let Some(v) = view.latest {
                data.insert(full, json!(v));
            }
        }
    }

    Value::Object(data)
}

fn matches_prefix(full_name: &str) -> bool {
    BROADCAST_PREFIXES.iter().any(|p| {
        if p.starts_with("kernel.") {
            // kernel.* 前缀：全名精确匹配，或以"前缀 + 边界字符(./_)"开头
            // （namespace 展开，兼容 event_bus → event_bus_push_total 的 _ 子指标）
            full_name == *p
                || full_name.starts_with(&format!("{p}."))
                || full_name.starts_with(&format!("{p}_"))
        } else {
            // 非 kernel 前缀（如 process.alive）：匹配任意 plugin 命名空间下的同名后缀
            full_name == *p || full_name.ends_with(&format!(".{p}"))
        }
    })
}

/// 后台广播任务句柄——启动后每 `interval` 秒采样并广播。
///
/// 调用方在 tokio runtime 内 spawn；drop 返回的 handle 不影响已 spawn 的任务
/// （tokio task 独立运行）。生产侧用 shutdown 信号 cancel。
pub struct MetricBroadcaster;

impl MetricBroadcaster {
    /// 启动后台广播循环。返回 join handle（可用于 shutdown cancel）。
    ///
    /// - `agg`：指标聚合器
    /// - `kernel_counters`：内核自采计数器（None = 不广播 kernel.* 指标）
    /// - `session`：会话协调器（emit_widget 广播）
    /// - `interval`：采样间隔（默认 1s）
    pub fn spawn(
        agg: Arc<MetricsAggregator>,
        kernel_counters: Option<Arc<KernelCounters>>,
        session: Arc<agentos_session::SessionCoordinator>,
        interval: Duration,
    ) -> tokio::task::JoinHandle<()> {
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(interval);
            // 第一次 tick 立即返回（不延迟），跳过避免启动期广播
            tick.tick().await;
            loop {
                tick.tick().await;
                let data = collect_broadcast_snapshot(&agg, kernel_counters.as_deref());
                if data.as_object().map(|o| o.is_empty()).unwrap_or(true) {
                    continue; // 无关键指标 → 不广播
                }
                // 广播给全部活跃连接（EmitScope::Broadcast，监控设计 §六 形态2）。
                // widget_event{metrics_tick, tick, {key:value, ...}} → 前端 statusBar 刷新。
                let _ = session
                    .broadcast_widget("metrics_tick", "tick", data, "kernel")
                    .await;
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::super::aggregator::{Labels, MetricType};
    use super::*;

    #[test]
    fn test_collect_snapshot_filters_by_prefix() {
        let agg = MetricsAggregator::new();
        // 命中前缀：process.alive
        agg.record(
            "p1",
            "process.alive",
            MetricType::Gauge,
            1.0,
            &Labels::new(),
            None,
            None,
        );
        // 不命中前缀：p1.some_business
        agg.record(
            "p1",
            "some_business",
            MetricType::Counter,
            100.0,
            &Labels::new(),
            None,
            None,
        );
        // 命中：kernel.session.connections（来自 kernel_counters）
        let kc = KernelCounters::new();
        let conn = kc.register_gauge("session.connections", Labels::new());
        conn.set(42);
        // 命中：kernel.engine.llm_calls（来自 kernel_counters）
        let llm = kc.register_counter("engine.llm_calls_total", Labels::new());
        llm.inc(5);
        // 不命中：kernel.engine.step_hits（不在白名单）
        let step = kc.register_counter("engine.step_hits_total", Labels::new());
        step.inc(99);

        let snap = collect_broadcast_snapshot(&agg, Some(&kc));
        let obj = snap.as_object().unwrap();
        assert!(
            obj.contains_key("p1.process.alive"),
            "process.alive should be included"
        );
        assert!(
            !obj.contains_key("p1.some_business"),
            "non-whitelisted should be excluded"
        );
        assert_eq!(obj["kernel.session.connections"], 42);
        assert_eq!(obj["kernel.engine.llm_calls_total"], 5);
        assert!(
            !obj.contains_key("kernel.engine.step_hits_total"),
            "step_hits not in broadcast whitelist"
        );
    }

    #[test]
    fn test_collect_snapshot_empty() {
        let agg = MetricsAggregator::new();
        let snap = collect_broadcast_snapshot(&agg, None);
        assert!(snap.as_object().unwrap().is_empty());
    }

    #[test]
    fn test_matches_prefix() {
        assert!(matches_prefix("kernel.session.connections"));
        assert!(matches_prefix("kernel.session.event_bus_push_total"));
        assert!(matches_prefix("kernel.api.dispatcher_errors"));
        assert!(matches_prefix("kernel.engine.llm_calls_total"));
        assert!(matches_prefix("kernel.engine.tool_calls_total"));
        assert!(matches_prefix("p1.process.alive"));
        assert!(!matches_prefix("p1.tokens_used"));
        assert!(!matches_prefix("kernel.engine.step_hits_total"));
    }
}
