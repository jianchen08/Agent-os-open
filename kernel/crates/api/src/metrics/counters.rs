//! 各 crate 自采 A 类指标的计数器注册中心（监控设计 §三 通道1 + §补引擎调度层）。
//!
//! 各 crate（session/api/engine）持自己的 AtomicU64/RwLock 计数器，关键路径 inc。
//! 本 KernelCounters 是聚合器侧的"拉快照"目标——各 crate 把自己计数器注册进来，
//! 聚合器定期（每秒）调 snapshot()，把各 crate 的瞬时值 record 到 MetricsAggregator。
//!
//! 设计权衡：各 crate 不直接写聚合器（避免每次 inc 都走 RwLock 写），
//! 而是先累加本地原子计数器，聚合器周期性拉快照批量 record。

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use parking_lot::RwLock;

use super::aggregator::{now_secs, Labels, MetricType, MetricsAggregator};

/// 单个原子计数器（counter / gauge 通用——counter 用 load 看 cumulative，
/// gauge 用 store 看 current value）。
#[derive(Debug, Default)]
pub struct AtomicCounter {
    value: AtomicU64,
}

impl AtomicCounter {
    pub fn new() -> Self {
        Self {
            value: AtomicU64::new(0),
        }
    }

    /// counter：累加。
    pub fn inc(&self, n: u64) {
        self.value.fetch_add(n, Ordering::Relaxed);
    }

    /// gauge：设当前值。
    pub fn set(&self, n: u64) {
        self.value.store(n, Ordering::Relaxed);
    }

    pub fn load(&self) -> u64 {
        self.value.load(Ordering::Relaxed)
    }
}

/// 一个注册的计数器项（name + 类型 + 计数器 + 固定 labels）。
struct RegisteredCounter {
    name: String,
    metric_type: MetricType,
    counter: Arc<AtomicCounter>,
    labels: Labels,
    /// 上次 flush 时的 cumulative 值（用于 delta 计算，counter 类型专用）。
    last_flushed: AtomicU64,
}

/// 快照项（snapshot 返回，供测试断言）。
#[derive(Debug, Clone)]
pub struct KernelCountersSnapshot {
    pub name: String,
    pub metric_type: MetricType,
    pub value: u64,
    pub labels: Labels,
}

/// flush 时的快照克隆（避免在写聚合器时持有锁）。
struct RegisteredCounterClone {
    name: String,
    metric_type: MetricType,
    value: u64,
    last_flushed: u64,
    labels: Labels,
}

/// 内核自采计数器注册中心。
pub struct KernelCounters {
    items: RwLock<Vec<RegisteredCounter>>,
}

impl Default for KernelCounters {
    fn default() -> Self {
        Self::new()
    }
}

impl KernelCounters {
    pub fn new() -> Self {
        Self {
            items: RwLock::new(Vec::new()),
        }
    }

    /// 注册一个 counter 计数器，返回句柄供 inc。
    pub fn register_counter(&self, name: &str, labels: Labels) -> Arc<AtomicCounter> {
        self.register(name, MetricType::Counter, labels)
    }

    /// 注册一个 gauge 计数器，返回句柄供 set。
    pub fn register_gauge(&self, name: &str, labels: Labels) -> Arc<AtomicCounter> {
        self.register(name, MetricType::Gauge, labels)
    }

    fn register(&self, name: &str, metric_type: MetricType, labels: Labels) -> Arc<AtomicCounter> {
        let counter = Arc::new(AtomicCounter::new());
        let item = RegisteredCounter {
            name: name.to_string(),
            metric_type,
            counter: Arc::clone(&counter),
            labels,
            last_flushed: AtomicU64::new(0),
        };
        self.items.write().push(item);
        counter
    }

    /// 取所有计数器快照。
    pub fn snapshot(&self) -> Vec<KernelCountersSnapshot> {
        self.items
            .read()
            .iter()
            .map(|item| KernelCountersSnapshot {
                name: item.name.clone(),
                metric_type: item.metric_type,
                value: item.counter.load(),
                labels: item.labels.clone(),
            })
            .collect()
    }

    /// 把快照写入聚合器（聚合器周期性调，监控设计 §三 通道1）。
    /// plugin_id 用 "kernel"（内核指标的统一命名空间前缀，监控设计 §九）。
    ///
    /// 各 crate 持的 AtomicCounter：
    /// - counter：存 **cumulative** total（inc 累加）。flush 时算 delta（本次 - 上次），
    ///   以 counter 类型写入聚合器（聚合器 counter 累加 delta）→ 聚合器 latest = 该窗口增量。
    ///   首次 flush 把当前 cumulative 全量算作 delta（覆盖冷启动基数）。
    /// - gauge：存 current value（set 覆盖）。flush 时直接以 gauge 写当前值。
    ///
    /// Prometheus 导出 counter 取 latest 即得到"自上次 flush 的增量速率"，
    /// 仪表盘可对 cumulative 计数器求 sum 得到总量。
    pub fn flush_to(&self, aggregator: &MetricsAggregator) {
        let now = now_secs();
        let items: Vec<RegisteredCounterClone> = self
            .items
            .read()
            .iter()
            .map(|i| RegisteredCounterClone {
                name: i.name.clone(),
                metric_type: i.metric_type,
                value: i.counter.load(),
                last_flushed: i.last_flushed.load(Ordering::Relaxed),
                labels: i.labels.clone(),
            })
            .collect();

        // 计算后回写 last_flushed
        let items_idx = self.items.write();
        for (idx, it) in items.iter().enumerate() {
            match it.metric_type {
                MetricType::Counter => {
                    let delta = it.value.saturating_sub(it.last_flushed);
                    aggregator.record_at(
                        now,
                        "kernel",
                        &it.name,
                        MetricType::Counter,
                        delta as f64,
                        &it.labels,
                        None,
                        None,
                    );
                    items_idx[idx]
                        .last_flushed
                        .store(it.value, Ordering::Relaxed);
                }
                MetricType::Gauge => {
                    aggregator.record_at(
                        now,
                        "kernel",
                        &it.name,
                        MetricType::Gauge,
                        it.value as f64,
                        &it.labels,
                        None,
                        None,
                    );
                }
                MetricType::Histogram => {
                    // KernelCounters 不支持 histogram（用 AtomicCounter 无法表达分布）
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_counter_inc_and_snapshot() {
        let kc = KernelCounters::new();
        let c = kc.register_counter("api.requests", Labels::new());
        c.inc(1);
        c.inc(2);
        let snap = kc.snapshot();
        assert_eq!(snap.len(), 1);
        assert_eq!(snap[0].value, 3);
        assert_eq!(snap[0].name, "api.requests");
        assert_eq!(snap[0].metric_type, MetricType::Counter);
    }

    #[test]
    fn test_gauge_set_and_snapshot() {
        let kc = KernelCounters::new();
        let g = kc.register_gauge("session.connections", Labels::new());
        g.set(10);
        g.set(7);
        let snap = kc.snapshot();
        assert_eq!(snap[0].value, 7);
        assert_eq!(snap[0].metric_type, MetricType::Gauge);
    }

    #[test]
    fn test_flush_to_aggregator() {
        let kc = KernelCounters::new();
        let agg = MetricsAggregator::new();
        let c = kc.register_counter("api.requests", Labels::new());
        c.inc(5);
        kc.flush_to(&agg);
        // 首次 flush：cumulative=5，last_flushed=0 → delta=5，counter latest=5
        let views = agg.query(Some("kernel"), Some("api.requests"), None, &Labels::new());
        assert_eq!(views.len(), 1);
        assert_eq!(views[0].latest, Some(5.0));
        // 再 inc 3 → cumulative=8；flush → delta=3
        // 同时间桶（1s 内）→ counter 桶内累加：5 + 3 = 8
        c.inc(3);
        kc.flush_to(&agg);
        let views = agg.query(Some("kernel"), Some("api.requests"), None, &Labels::new());
        assert_eq!(views[0].latest, Some(8.0));
    }

    #[test]
    fn test_flush_gauge_overwrites() {
        let kc = KernelCounters::new();
        let agg = MetricsAggregator::new();
        let g = kc.register_gauge("session.connections", Labels::new());
        g.set(10);
        kc.flush_to(&agg);
        g.set(7);
        kc.flush_to(&agg);
        // gauge 同桶取 avg：(10+7)/2 = 8.5
        let views = agg.query(
            Some("kernel"),
            Some("session.connections"),
            None,
            &Labels::new(),
        );
        assert!((views[0].latest.unwrap() - 8.5).abs() < 0.01);
    }

    #[test]
    fn test_multiple_counters() {
        let kc = KernelCounters::new();
        let _c1 = kc.register_counter("a", Labels::new());
        let _c2 = kc.register_counter("b", Labels::new());
        let _c3 = kc.register_gauge("c", Labels::new());
        assert_eq!(kc.snapshot().len(), 3);
    }
}
