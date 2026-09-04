//! MetricsAggregator——内核内置指标聚合器（ADR §四 + 监控设计 §四）。
//!
//! 三类型：
//! - counter（单调累加）：插入时 新值 = 旧值 + value
//! - gauge（覆盖）：插入时 新值覆盖旧值
//! - histogram（分布）：追加到桶（Prometheus 默认桶）
//!
//! 滚动桶留存（监控设计 §十一 决策1）：
//! - 1s 桶近 10min
//! - 10s 桶近 2h
//! - 超 2h 落 SQLite（监控设计 §十一 决策5；本模块只管内存两级桶，SQLite 落盘见 store 模块）
//!
//! 线程安全：RwLock<HashMap>。

use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};

/// Prometheus 默认 histogram 桶边界（秒，监控设计 §十一 决策2）。
pub const DEFAULT_HISTOGRAM_BUCKETS: &[f64] = &[
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
];

/// 1s 桶覆盖的时间范围（10 分钟）。
pub const TIER1_WINDOW: Duration = Duration::from_secs(10 * 60);
/// 1s 桶粒度。
pub const TIER1_BUCKET: Duration = Duration::from_secs(1);
/// 10s 桶覆盖的时间范围（2 小时，TIER1 之外到 2h）。
pub const TIER2_WINDOW: Duration = Duration::from_secs(2 * 60 * 60);
/// 10s 桶粒度。
pub const TIER2_BUCKET: Duration = Duration::from_secs(10);

/// 指标类型。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum MetricType {
    Counter,
    Gauge,
    Histogram,
}

impl MetricType {
    pub fn as_str(&self) -> &'static str {
        match self {
            MetricType::Counter => "counter",
            MetricType::Gauge => "gauge",
            MetricType::Histogram => "histogram",
        }
    }
}

/// 指标 key（plugin_id, name, labels_hash）。
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct MetricKey {
    pub plugin_id: String,
    pub name: String,
    pub labels_hash: u64,
}

/// labels——有序（BTreeMap）便于稳定哈希与导出。
pub type Labels = BTreeMap<String, String>;

/// 计算 labels 的稳定哈希（序列化后 FNV-1a 64bit）。
pub fn labels_hash(labels: &Labels) -> u64 {
    // 简单稳定的 FNV-1a 64bit，输入为排序后的 key=value 串
    const FNV_OFFSET: u64 = 14695981039346656037;
    const FNV_PRIME: u64 = 1099511628211;
    let mut h = FNV_OFFSET;
    for (k, v) in labels {
        for b in k.as_bytes() {
            h ^= *b as u64;
            h = h.wrapping_mul(FNV_PRIME);
        }
        h ^= b'=' as u64;
        h = h.wrapping_mul(FNV_PRIME);
        for b in v.as_bytes() {
            h ^= *b as u64;
            h = h.wrapping_mul(FNV_PRIME);
        }
        h ^= b';' as u64;
        h = h.wrapping_mul(FNV_PRIME);
    }
    h
}

/// 单个 sample（时间戳 + 值）。
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct Sample {
    pub ts: i64,
    pub value: f64,
}

/// 一个桶的聚合值（滚动桶降采样用）。
#[derive(Debug, Clone, Default)]
struct BucketAggregate {
    /// counter：桶内末值（最后一个收到的累加后值）。
    /// gauge：sum（用于求 avg）。
    /// histogram：sum（桶内观察值之和，供降采样合并）。
    sum: f64,
    /// gauge 计数（求 avg）。
    count: u64,
    /// counter/gauge 的末值。
    last: f64,
}

impl BucketAggregate {
    fn observe(&mut self, value: f64) {
        self.sum += value;
        self.count += 1;
        self.last = value;
    }

    /// 合并下一级桶（10min→2h 降采样时调用）。
    fn merge(&mut self, other: &BucketAggregate) {
        self.sum += other.sum;
        self.count += other.count;
        // 取较新的 last（按桶时间顺序合并，后来者覆盖）
        if other.count > 0 {
            self.last = other.last;
        }
    }
}

/// histogram 桶累计计数（Prometheus 风格）。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct HistogramBuckets {
    /// 各桶上界的累计计数（含 DEFAULT_HISTOGRAM_BUCKETS + +Inf）。
    /// 长度 = DEFAULT_HISTOGRAM_BUCKETS.len() + 1（最后一个 +Inf）。
    pub counts: Vec<u64>,
    /// 所有观察值之和。
    pub sum: f64,
    /// 总观察次数。
    pub count: u64,
}

impl HistogramBuckets {
    pub fn new() -> Self {
        Self {
            counts: vec![0; DEFAULT_HISTOGRAM_BUCKETS.len() + 1],
            sum: 0.0,
            count: 0,
        }
    }

    /// 记录一个观察值（秒）。
    pub fn observe(&mut self, value: f64) {
        self.sum += value;
        self.count += 1;
        // 找到第一个 >= value 的桶上界，该桶及之后所有桶累计 +1
        for (i, &bound) in DEFAULT_HISTOGRAM_BUCKETS.iter().enumerate() {
            if value <= bound {
                self.counts[i] += 1;
            }
        }
        // +Inf 桶永远 +1
        *self.counts.last_mut().unwrap() += 1;
    }

    /// 合并另一组 histogram 桶（降采样）。
    pub fn merge(&mut self, other: &HistogramBuckets) {
        for (a, b) in self.counts.iter_mut().zip(other.counts.iter()) {
            *a += b;
        }
        self.sum += other.sum;
        self.count += other.count;
    }
}

/// 一条 metric 时序。
#[derive(Debug, Clone)]
pub struct MetricSeries {
    pub metric_type: MetricType,
    pub labels: Labels,
    /// TIER1（1s 桶，近 10min）：bucket_start_ts -> 聚合值。
    tier1: BTreeMap<i64, BucketAggregate>,
    /// TIER2（10s 桶，10min~2h）：bucket_start_ts -> 聚合值。
    tier2: BTreeMap<i64, BucketAggregate>,
    /// histogram 累计桶（histogram 类型专用；counter/gauge 为 None）。
    histogram: Option<HistogramBuckets>,
    /// 单位（可选，record_metric 时声明，查询返回）。
    pub unit: Option<String>,
    /// HELP 文本（可选，Prometheus 导出用）。
    pub help: Option<String>,
}

impl MetricSeries {
    fn new(metric_type: MetricType, labels: Labels) -> Self {
        let histogram = match metric_type {
            MetricType::Histogram => Some(HistogramBuckets::new()),
            _ => None,
        };
        Self {
            metric_type,
            labels,
            tier1: BTreeMap::new(),
            tier2: BTreeMap::new(),
            histogram,
            unit: None,
            help: None,
        }
    }

    /// 把桶对齐到指定粒度的起始 ts。
    fn bucket_start(ts_secs: i64, bucket: Duration) -> i64 {
        let b = bucket.as_secs() as i64;
        if b == 0 {
            ts_secs
        } else {
            ts_secs - (ts_secs.rem_euclid(b))
        }
    }

    /// 插入一个值（ts 为 Unix 秒）。
    fn observe(&mut self, ts_secs: i64, value: f64) {
        let t1_start = Self::bucket_start(ts_secs, TIER1_BUCKET);
        let agg = self.tier1.entry(t1_start).or_default();
        match self.metric_type {
            MetricType::Counter => {
                // counter：累加（value 是本次增量），last 存累计值
                agg.last += value;
                agg.sum += value;
                agg.count += 1;
            }
            MetricType::Gauge => {
                // gauge：覆盖，last = value；sum/count 用于 avg
                agg.observe(value);
            }
            MetricType::Histogram => {
                // histogram：桶累计 + 桶内聚合
                agg.observe(value);
                if let Some(h) = &mut self.histogram {
                    h.observe(value);
                }
            }
        }
    }

    /// 把超出 TIER1 窗口的 1s 桶降采样合并到 TIER2（10s 桶）。
    fn roll_tier1_to_tier2(&mut self, now_secs: i64) {
        let tier1_cutoff = now_secs - TIER1_WINDOW.as_secs() as i64;
        let to_roll: Vec<i64> = self
            .tier1
            .keys()
            .filter(|&&ts| ts < tier1_cutoff)
            .copied()
            .collect();
        for ts in to_roll {
            if let Some(agg) = self.tier1.remove(&ts) {
                let t2_start = Self::bucket_start(ts, TIER2_BUCKET);
                let t2 = self.tier2.entry(t2_start).or_default();
                match self.metric_type {
                    MetricType::Counter => {
                        // counter：t1 桶 last 是该 1s 窗口的增量，跨桶合并必须累加
                        // （t2.last = 10s 桶总增量）；覆盖语义会把同桶其余 9 秒的
                        // 事件丢掉，窗口求和恒被低估。
                        t2.sum += agg.sum;
                        t2.count += agg.count;
                        t2.last += agg.last;
                    }
                    _ => t2.merge(&agg),
                }
            }
        }
    }

    /// 清理超出 TIER2 窗口的桶（超 2h 的落 SQLite，本模块只丢弃）。
    fn evict_expired(&mut self, now_secs: i64) {
        let tier2_cutoff = now_secs - TIER2_WINDOW.as_secs() as i64;
        let to_evict: Vec<i64> = self
            .tier2
            .keys()
            .filter(|&&ts| ts < tier2_cutoff)
            .copied()
            .collect();
        for ts in to_evict {
            self.tier2.remove(&ts);
        }
    }

    /// 取 TIER1 + TIER2 合并后的 sample 列表（按时间排序）。
    /// counter/histogram 返回累计值（last/sum）；gauge 返回 avg。
    fn samples(&self) -> Vec<Sample> {
        let mut out = Vec::new();
        for (ts, agg) in &self.tier2 {
            out.push(Sample {
                ts: *ts,
                value: self.sample_value(agg),
            });
        }
        for (ts, agg) in &self.tier1 {
            out.push(Sample {
                ts: *ts,
                value: self.sample_value(agg),
            });
        }
        out.sort_by_key(|s| s.ts);
        out
    }

    fn sample_value(&self, agg: &BucketAggregate) -> f64 {
        match self.metric_type {
            MetricType::Counter => agg.last,
            MetricType::Gauge => {
                if agg.count > 0 {
                    agg.sum / agg.count as f64
                } else {
                    agg.last
                }
            }
            MetricType::Histogram => agg.sum,
        }
    }

    /// 当前最新值（Prometheus 导出 / 状态栏推送用）。
    fn latest_value(&self) -> Option<f64> {
        // 取 tier1 最后一个桶的 last，否则 tier2
        if let Some((_, agg)) = self.tier1.last_key_value() {
            return Some(self.sample_value(agg));
        }
        if let Some((_, agg)) = self.tier2.last_key_value() {
            return Some(self.sample_value(agg));
        }
        None
    }
}

/// 内核指标聚合器（监控设计 §四）。
#[derive(Clone)]
pub struct MetricsAggregator {
    inner: Arc<RwLock<HashMap<MetricKey, MetricSeries>>>,
}

impl Default for MetricsAggregator {
    fn default() -> Self {
        Self::new()
    }
}

impl MetricsAggregator {
    /// 创建空聚合器。
    pub fn new() -> Self {
        Self {
            inner: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// 记录一个指标值。
    ///
    /// - counter：value 为增量，累加到现有值。
    /// - gauge：value 覆盖现有值。
    /// - histogram：value 追加到桶。
    ///
    /// unit/help 在 series 首次创建时写入；已存在则忽略（保持首次声明）。
    // 记录指标需要 7 个语义独立的入参；合并成结构体会改公共 API 签名，故保留。
    #[allow(clippy::too_many_arguments)]
    pub fn record(
        &self,
        plugin_id: &str,
        name: &str,
        metric_type: MetricType,
        value: f64,
        labels: &Labels,
        unit: Option<&str>,
        help: Option<&str>,
    ) {
        self.record_at(
            now_secs(),
            plugin_id,
            name,
            metric_type,
            value,
            labels,
            unit,
            help,
        );
    }

    /// 记录指标（指定时间戳，测试用）。
    // record + ts 入参，共 8 个；为与 record 对齐刻意保留独立参数，不改公共签名。
    #[allow(clippy::too_many_arguments)]
    pub fn record_at(
        &self,
        ts: i64,
        plugin_id: &str,
        name: &str,
        metric_type: MetricType,
        value: f64,
        labels: &Labels,
        unit: Option<&str>,
        help: Option<&str>,
    ) {
        let key = MetricKey {
            plugin_id: plugin_id.to_string(),
            name: name.to_string(),
            labels_hash: labels_hash(labels),
        };
        let mut inner = self.inner.write();
        let series = inner.entry(key).or_insert_with(|| {
            let mut s = MetricSeries::new(metric_type, labels.clone());
            s.unit = unit.map(str::to_string);
            s.help = help.map(str::to_string);
            s
        });
        series.observe(ts, value);
    }

    /// 触发降采样：把超 TIER1 窗口的桶合并到 TIER2，清理超 TIER2 的桶。
    /// 应由后台任务定期调用（每秒一次即可）。
    pub fn rollup(&self) {
        self.rollup_at(now_secs());
    }

    /// 降采样（指定时间，测试用）。
    pub fn rollup_at(&self, now: i64) {
        let mut inner = self.inner.write();
        for series in inner.values_mut() {
            series.roll_tier1_to_tier2(now);
            series.evict_expired(now);
        }
    }

    /// 查询符合条件的 series，返回所有 sample。
    ///
    /// 过滤：plugin（None=all）/ metric（None=all）/ window（从 now 往前）/ labels（子集匹配）。
    pub fn query(
        &self,
        plugin: Option<&str>,
        metric: Option<&str>,
        window: Option<Duration>,
        labels_filter: &Labels,
    ) -> Vec<MetricSeriesView> {
        self.query_at(now_secs(), plugin, metric, window, labels_filter)
    }

    /// 查询（指定 now，测试用）。
    pub fn query_at(
        &self,
        now: i64,
        plugin: Option<&str>,
        metric: Option<&str>,
        window: Option<Duration>,
        labels_filter: &Labels,
    ) -> Vec<MetricSeriesView> {
        let cutoff = window.map(|w| now - w.as_secs() as i64).unwrap_or(0);
        let inner = self.inner.read();
        let mut out = Vec::new();
        for (key, series) in inner.iter() {
            if let Some(p) = plugin {
                if key.plugin_id != p {
                    continue;
                }
            }
            if let Some(m) = metric {
                if key.name != m {
                    continue;
                }
            }
            // labels 子集匹配
            if !labels_subset(labels_filter, &series.labels) {
                continue;
            }
            let samples: Vec<Sample> = series
                .samples()
                .into_iter()
                .filter(|s| s.ts >= cutoff)
                .collect();
            // histogram 额外返回累计桶
            let histogram = series.histogram.clone();
            out.push(MetricSeriesView {
                plugin_id: key.plugin_id.clone(),
                name: key.name.clone(),
                metric_type: series.metric_type,
                labels: series.labels.clone(),
                samples,
                unit: series.unit.clone(),
                help: series.help.clone(),
                latest: series.latest_value(),
                histogram,
            });
        }
        out
    }

    /// 所有 series 的快照（不含时间过滤，Prometheus 导出用）。
    pub fn snapshot(&self) -> Vec<MetricSeriesView> {
        let inner = self.inner.read();
        inner
            .iter()
            .map(|(key, series)| MetricSeriesView {
                plugin_id: key.plugin_id.clone(),
                name: key.name.clone(),
                metric_type: series.metric_type,
                labels: series.labels.clone(),
                samples: series.samples(),
                unit: series.unit.clone(),
                help: series.help.clone(),
                latest: series.latest_value(),
                histogram: series.histogram.clone(),
            })
            .collect()
    }

    /// 清空（测试用）。
    pub fn clear(&self) {
        self.inner.write().clear();
    }
}

/// labels 子集匹配：filter 中每个 k=v 都必须在 series_labels 中存在且相等。
fn labels_subset(filter: &Labels, series_labels: &Labels) -> bool {
    for (k, v) in filter {
        match series_labels.get(k) {
            Some(sv) if sv == v => {}
            _ => return false,
        }
    }
    true
}

/// 查询返回的单条 series 视图。
#[derive(Debug, Clone, Serialize)]
pub struct MetricSeriesView {
    pub plugin_id: String,
    pub name: String,
    #[serde(rename = "type")]
    pub metric_type: MetricType,
    pub labels: Labels,
    pub samples: Vec<Sample>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub unit: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub help: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub latest: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub histogram: Option<HistogramBuckets>,
}

/// 当前 Unix 时间戳（秒）。
pub fn now_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn labels(pairs: &[(&str, &str)]) -> Labels {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    // ── M1 三类型插入 + 查询 ──

    #[test]
    fn test_counter_accumulates() {
        let agg = MetricsAggregator::new();
        let lbl = labels(&[]);
        agg.record_at(
            1000,
            "p1",
            "calls",
            MetricType::Counter,
            10.0,
            &lbl,
            None,
            None,
        );
        agg.record_at(
            1001,
            "p1",
            "calls",
            MetricType::Counter,
            5.0,
            &lbl,
            None,
            None,
        );
        let views = agg.query_at(1002, Some("p1"), Some("calls"), None, &labels(&[]));
        assert_eq!(views.len(), 1);
        // 两个 1s 桶：1000->10, 1001->5（counter 桶内存累计值）
        let samples = &views[0].samples;
        assert_eq!(samples.len(), 2);
        // 最新桶累计 = 5（1001 桶只有一次记录）
        assert_eq!(views[0].latest, Some(5.0));
    }

    #[test]
    fn test_counter_accumulates_within_same_bucket() {
        let agg = MetricsAggregator::new();
        let lbl = labels(&[]);
        // 同一 1s 桶内多次记录 → 累加
        agg.record_at(
            1000,
            "p1",
            "calls",
            MetricType::Counter,
            10.0,
            &lbl,
            None,
            None,
        );
        agg.record_at(
            1000,
            "p1",
            "calls",
            MetricType::Counter,
            5.0,
            &lbl,
            None,
            None,
        );
        let views = agg.query_at(1001, Some("p1"), Some("calls"), None, &labels(&[]));
        assert_eq!(views[0].latest, Some(15.0)); // 10 + 5
    }

    #[test]
    fn test_gauge_overwrites() {
        let agg = MetricsAggregator::new();
        let lbl = labels(&[]);
        agg.record_at(
            1000,
            "p1",
            "conn",
            MetricType::Gauge,
            10.0,
            &lbl,
            None,
            None,
        );
        agg.record_at(1000, "p1", "conn", MetricType::Gauge, 7.0, &lbl, None, None);
        agg.record_at(
            1000,
            "p1",
            "conn",
            MetricType::Gauge,
            14.0,
            &lbl,
            None,
            None,
        );
        let views = agg.query_at(1001, Some("p1"), Some("conn"), None, &labels(&[]));
        // gauge 同桶取 avg：(10+7+14)/3 = 10.333
        assert!((views[0].latest.unwrap() - 10.3333).abs() < 0.01);
    }

    #[test]
    fn test_histogram_buckets() {
        let agg = MetricsAggregator::new();
        let lbl = labels(&[]);
        agg.record_at(
            1000,
            "p1",
            "lat",
            MetricType::Histogram,
            0.003,
            &lbl,
            None,
            None,
        );
        agg.record_at(
            1000,
            "p1",
            "lat",
            MetricType::Histogram,
            0.02,
            &lbl,
            None,
            None,
        );
        agg.record_at(
            1000,
            "p1",
            "lat",
            MetricType::Histogram,
            2.0,
            &lbl,
            None,
            None,
        );
        let views = agg.query_at(1001, Some("p1"), Some("lat"), None, &labels(&[]));
        let h = views[0].histogram.as_ref().unwrap();
        assert_eq!(h.count, 3);
        // 0.003 <= 0.005 → counts[0] = 1
        assert_eq!(h.counts[0], 1);
        // 0.02 <= 0.025 → counts[2] 应含 0.003 + 0.02 = 2
        assert_eq!(h.counts[2], 2);
        // 2.0 <= 2.5 → counts[8] = 3
        assert_eq!(h.counts[8], 3);
        // +Inf = 3
        assert_eq!(*h.counts.last().unwrap(), 3);
    }

    #[test]
    fn test_labels_isolate_series() {
        let agg = MetricsAggregator::new();
        agg.record_at(
            1000,
            "p1",
            "tokens",
            MetricType::Counter,
            100.0,
            &labels(&[("model", "a")]),
            None,
            None,
        );
        agg.record_at(
            1000,
            "p1",
            "tokens",
            MetricType::Counter,
            50.0,
            &labels(&[("model", "b")]),
            None,
            None,
        );
        let views = agg.query_at(1001, Some("p1"), Some("tokens"), None, &labels(&[]));
        assert_eq!(views.len(), 2);
    }

    // ── 滚动桶降采样 ──

    #[test]
    fn test_rollup_tier1_to_tier2() {
        let agg = MetricsAggregator::new();
        let lbl = labels(&[]);
        // 在 1000s 记录（gauge 便于验证 avg 合并）
        agg.record_at(1000, "p1", "g", MetricType::Gauge, 10.0, &lbl, None, None);
        agg.record_at(1001, "p1", "g", MetricType::Gauge, 20.0, &lbl, None, None);
        // now 设为 1000 + 11min = 1660s（超过 10min 窗口）
        let now = 1000 + (TIER1_WINDOW.as_secs() as i64) + 60;
        agg.rollup_at(now);
        let views = agg.query_at(now, Some("p1"), Some("g"), None, &labels(&[]));
        // tier1 桶已滚动到 tier2；sample 仍可查到（来自 tier2）
        assert!(
            !views.is_empty(),
            "after rollup series should still exist via tier2"
        );
        let samples = &views[0].samples;
        assert!(
            samples.iter().any(|s| s.value > 0.0),
            "tier2 bucket should retain value"
        );
    }

    #[test]
    fn test_rollup_evicts_beyond_2h() {
        let agg = MetricsAggregator::new();
        let lbl = labels(&[]);
        // 在 1000s 记录
        agg.record_at(1000, "p1", "g", MetricType::Gauge, 10.0, &lbl, None, None);
        // now = 1000 + 3h → 超 2h 窗口，tier2 也应被清理
        let now = 1000 + (TIER2_WINDOW.as_secs() as i64) + 3600;
        agg.rollup_at(now);
        let views = agg.query_at(now, Some("p1"), Some("g"), None, &labels(&[]));
        // series 仍在 map 里（key 不删），但 samples 为空
        assert!(
            views.is_empty() || views[0].samples.is_empty(),
            "beyond 2h samples should be evicted"
        );
    }

    #[test]
    fn test_rollup_counter_accumulates_across_tier1_buckets() {
        // counter 跨 1s 桶合并到同一 10s 桶必须累加（每桶值是窗口增量）；
        // 覆盖语义会只留最后一秒的增量，窗口求和被低估（插件报错计数恒偏小的根因）。
        let agg = MetricsAggregator::new();
        let lbl = labels(&[]);
        // 同一 10s 桶（1000..1009）内三个 1s 桶分别增量 5/3/2 → 桶总值 10
        for (ts, delta) in [(1000, 5.0), (1005, 3.0), (1008, 2.0)] {
            agg.record_at(
                ts,
                "p1",
                "errors",
                MetricType::Counter,
                delta,
                &lbl,
                None,
                None,
            );
        }
        let now = 1000 + TIER1_WINDOW.as_secs() as i64 + 60;
        agg.rollup_at(now);
        let views = agg.query_at(now, Some("p1"), Some("errors"), None, &labels(&[]));
        assert_eq!(views.len(), 1);
        let total: f64 = views[0].samples.iter().map(|s| s.value).sum();
        assert!(
            (total - 10.0).abs() < f64::EPSILON,
            "counter 跨桶累加后窗口总量应为 10，实际 {total}"
        );
    }

    // ── 查询过滤 ──

    #[test]
    fn test_query_filter_by_plugin() {
        let agg = MetricsAggregator::new();
        agg.record_at(
            1000,
            "p1",
            "m",
            MetricType::Counter,
            1.0,
            &labels(&[]),
            None,
            None,
        );
        agg.record_at(
            1000,
            "p2",
            "m",
            MetricType::Counter,
            1.0,
            &labels(&[]),
            None,
            None,
        );
        let views = agg.query_at(1001, Some("p1"), None, None, &labels(&[]));
        assert_eq!(views.len(), 1);
        assert_eq!(views[0].plugin_id, "p1");
    }

    #[test]
    fn test_query_filter_by_labels() {
        let agg = MetricsAggregator::new();
        agg.record_at(
            1000,
            "p1",
            "m",
            MetricType::Counter,
            1.0,
            &labels(&[("env", "prod"), ("region", "us")]),
            None,
            None,
        );
        agg.record_at(
            1000,
            "p1",
            "m",
            MetricType::Counter,
            1.0,
            &labels(&[("env", "dev"), ("region", "us")]),
            None,
            None,
        );
        let views = agg.query_at(1001, Some("p1"), None, None, &labels(&[("env", "prod")]));
        assert_eq!(views.len(), 1);
        assert_eq!(views[0].labels.get("env").unwrap(), "prod");
    }

    #[test]
    fn test_query_window_filter() {
        let agg = MetricsAggregator::new();
        agg.record_at(
            1000,
            "p1",
            "m",
            MetricType::Counter,
            1.0,
            &labels(&[]),
            None,
            None,
        );
        agg.record_at(
            2000,
            "p1",
            "m",
            MetricType::Counter,
            1.0,
            &labels(&[]),
            None,
            None,
        );
        // now=2010, window=60s → 只看 1950 之后的
        let views = agg.query_at(
            2010,
            Some("p1"),
            Some("m"),
            Some(Duration::from_secs(60)),
            &labels(&[]),
        );
        assert_eq!(views.len(), 1);
        assert_eq!(views[0].samples.len(), 1);
        assert_eq!(views[0].samples[0].ts, 2000);
    }

    #[test]
    fn test_labels_hash_stable() {
        let l1 = labels(&[("a", "1"), ("b", "2")]);
        let l2 = labels(&[("b", "2"), ("a", "1")]);
        // BTreeMap 保证顺序一致 → 哈希一致
        assert_eq!(labels_hash(&l1), labels_hash(&l2));
    }
}
