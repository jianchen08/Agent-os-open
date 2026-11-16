//! 各 crate 自采 A 类指标的计数器注册中心（监控设计 §三 通道1 + §补引擎调度层）。
//!
//! 各 crate（session/api/engine）持自己的 AtomicU64/RwLock 计数器，关键路径 inc。
//! 本 KernelCounters 是聚合器侧的"拉快照"目标——各 crate 把自己计数器注册进来，
//! 聚合器定期（每秒）调 snapshot()，把各 crate 的瞬时值 record 到 MetricsAggregator。
//!
//! 仅承载 counter（cumulative total）；gauge 读面已随
//! docs/decisions/2026-09-09-kernel-dead-layer-adjudication.md 裁决移除
//! （生产从不注册 gauge，SessionMetrics 等内部计数仅自采不导出）。
//!
//! 设计权衡：各 crate 不直接写聚合器（避免每次 inc 都走 RwLock 写），
//! 而是先累加本地原子计数器，聚合器周期性拉快照批量 record。

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use parking_lot::RwLock;

use super::aggregator::{now_secs, Labels, MetricType, MetricsAggregator};

/// 单个原子计数器（cumulative total，inc 累加）。
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
    /// 各 crate 持的 AtomicCounter 存 **cumulative** total（inc 累加）。flush 时
    /// 算 delta（本次 - 上次），以 counter 类型写入聚合器（聚合器 counter 累加
    /// delta）→ 聚合器 latest = 该窗口增量。首次 flush 把当前 cumulative 全量
    /// 算作 delta（覆盖冷启动基数）。
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
                // KernelCounters 仅注册 counter；Gauge/Histogram 无注册入口
                MetricType::Gauge | MetricType::Histogram => {}
            }
        }
    }
}

// ── crate 自采静态计数器的 flush 桥（卡9：监控面补采）──────────────────────

/// 三条 crate 自采静态计数器在聚合器侧的 metric 名（plugin="kernel" 命名空间）。
///
/// inc 点不在本 crate 持有的 AtomicCounter（那些走 [`KernelCounters::flush_to`]），
/// 而是各 crate 的进程级静态（mcp `sidecar_stdout_eof_total` /
/// api `plugin_watcher::{g2_tools_list_observe_fail_total, tools_list_malformed_total}`，
/// 采样源形态同 mcp `reverse_call_levels` 先例）。flush 桥把 cumulative → delta
/// 换算后以 counter 类型写入聚合器，语义与 [`KernelCounters::flush_to`] 一致。
pub const CRATE_STATIC_COUNTER_NAMES: [&str; 3] = [
    "mcp.sidecar_stdout_eof_total",
    "mcp.tools_list_observe_fail_total",
    "mcp.tools_list_malformed_total",
];

/// crate 静态计数器 flush 的 cumulative 游标（main flush 循环持一个实例）。
#[derive(Debug, Default)]
pub struct CrateStaticCounterFlush {
    last_eof: u64,
    last_observe_fail: u64,
    last_malformed: u64,
}

/// 单条 cumulative 计数器的 delta 入账：跳变（value > last）才 record，
/// 停走/回退（进程重启致 cumulative 归零）不产生样本。counter 类型入账语义
/// 同 [`KernelCounters::flush_to`]（聚合器桶内累加 delta → 窗口求和 = 窗口增量）。
fn record_counter_delta(aggregator: &MetricsAggregator, name: &str, value: u64, last: &mut u64) {
    let delta = value.saturating_sub(*last);
    *last = value;
    if delta == 0 {
        return;
    }
    aggregator.record(
        "kernel",
        name,
        MetricType::Counter,
        delta as f64,
        &Labels::new(),
        None,
        None,
    );
}

impl CrateStaticCounterFlush {
    pub fn new() -> Self {
        Self::default()
    }

    /// 采样三条静态计数器，把各自增量以 counter 类型写入聚合器
    /// （plugin="kernel"；delta 语义同 [`KernelCounters::flush_to`]）。
    pub fn flush(&mut self, aggregator: &MetricsAggregator) {
        record_counter_delta(
            aggregator,
            CRATE_STATIC_COUNTER_NAMES[0],
            agentos_mcp::sidecar_stdout_eof_total(),
            &mut self.last_eof,
        );
        record_counter_delta(
            aggregator,
            CRATE_STATIC_COUNTER_NAMES[1],
            crate::plugin_watcher::g2_tools_list_observe_fail_total(),
            &mut self.last_observe_fail,
        );
        record_counter_delta(
            aggregator,
            CRATE_STATIC_COUNTER_NAMES[2],
            crate::plugin_watcher::tools_list_malformed_total(),
            &mut self.last_malformed,
        );
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
    fn test_multiple_counters() {
        let kc = KernelCounters::new();
        let _c1 = kc.register_counter("a", Labels::new());
        let _c2 = kc.register_counter("b", Labels::new());
        let _c3 = kc.register_counter("c", Labels::new());
        assert_eq!(kc.snapshot().len(), 3);
    }

    /// 卡9 flush 桥·跳变计数（合成值驱动，不触进程级静态——静态被并行测试
    /// 共享，直接驱动会有竞态假红）：cumulative 跳变入账 delta，停走不入账，
    /// 回退（进程重启归零）不入账；多次跳变入账之和 = 总增量。
    #[test]
    fn record_counter_delta_records_jumps_and_skips_plateau_and_rollback() {
        let agg = MetricsAggregator::new();
        let name = "mcp.sidecar_stdout_eof_total";
        let latest = |agg: &MetricsAggregator| {
            agg.query(Some("kernel"), Some(name), None, &Labels::new())
                .first()
                .and_then(|v| v.latest)
                .unwrap_or(0.0)
        };
        let mut last: u64 = 0;
        // 跳变 0→5：入账 5
        record_counter_delta(&agg, name, 5, &mut last);
        assert_eq!(last, 5);
        assert_eq!(latest(&agg), 5.0);
        // 停走（5→5）：delta 0 不入账，latest 不变
        record_counter_delta(&agg, name, 5, &mut last);
        assert_eq!(latest(&agg), 5.0);
        // 再跳变 5→8：入账 3 → 桶内累加 latest=8（= 总增量，cumulative 语义）
        record_counter_delta(&agg, name, 8, &mut last);
        assert_eq!(latest(&agg), 8.0);
        // 回退（8→2，进程重启归零后再涨）：saturating 不入账负 delta
        record_counter_delta(&agg, name, 2, &mut last);
        assert_eq!(last, 2, "游标随采样值推进（下轮从新基线算 delta）");
        assert_eq!(latest(&agg), 8.0, "回退不得入账负增量");
        // 回退后再创新高（2→4）：正常入账 delta 2 → latest=10
        record_counter_delta(&agg, name, 4, &mut last);
        assert_eq!(latest(&agg), 10.0);
    }

    /// 卡9 flush 桥·多实例分键：三条计数器名是聚合器中三个独立 series，
    /// 同一 flush 状态机分别追踪各自的 last 游标（互不串值）。
    #[test]
    fn crate_static_counter_names_form_distinct_series() {
        let agg = MetricsAggregator::new();
        let mut last_eof: u64 = 0;
        let mut last_fail: u64 = 0;
        let mut last_malformed: u64 = 0;
        // 三键各入不同增量
        record_counter_delta(&agg, CRATE_STATIC_COUNTER_NAMES[0], 3, &mut last_eof);
        record_counter_delta(&agg, CRATE_STATIC_COUNTER_NAMES[1], 1, &mut last_fail);
        record_counter_delta(&agg, CRATE_STATIC_COUNTER_NAMES[2], 7, &mut last_malformed);
        for (name, want) in [
            (CRATE_STATIC_COUNTER_NAMES[0], 3.0),
            (CRATE_STATIC_COUNTER_NAMES[1], 1.0),
            (CRATE_STATIC_COUNTER_NAMES[2], 7.0),
        ] {
            let views = agg.query(Some("kernel"), Some(name), None, &Labels::new());
            assert_eq!(views.len(), 1, "{name} 应恰一条 series");
            assert_eq!(views[0].name, name, "分键不串：{name}");
            assert_eq!(views[0].latest, Some(want), "{name} 增量独立入账");
        }
        let all = agg.query(Some("kernel"), None, None, &Labels::new());
        assert!(
            all.len() >= 3,
            "kernel 命名空间至少含三条卡9 计数器 series，实测 {}",
            all.len()
        );
    }
}
