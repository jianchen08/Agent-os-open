//! 内核指标聚合器与导出（监控设计 §四/§五/§十一）。
//!
//! 模块：
//! - [`aggregator`]：MetricsAggregator（三类型 + 滚动桶留存）
//! - [`export`]：Prometheus exposition format 导出
//! - [`counters`]：各 crate 自采 A 类指标的 AtomicCounter 注册中心
//! - [`proc_state`]：invoker 代采 C 类进程态指标
//! - [`broadcast`]：event_bus 采样广播高频指标（监控设计 §六 形态2）
//! - [`plugin_widget_broadcast`]：按插件 contributes.widget 配置驱动推送（ADR §3.5'）
//! - [`lifecycle`]：生命周期事件总线指标订阅者（lifecycle.* 计数器）
//! - [`capability`]：metrics-admin 读面 capability handler（boot-plugin 第三刀，
//!   写面 metrics.record 留 KernelCapabilityRouter 内置）

pub mod aggregator;
pub mod broadcast;
pub mod capability;
pub mod counters;
pub mod export;
pub mod lifecycle;
pub mod plugin_widget_broadcast;
pub mod proc_state;

pub use aggregator::{
    labels_hash, now_secs, HistogramBuckets, Labels, MetricKey, MetricSeriesView, MetricType,
    MetricsAggregator, Sample, DEFAULT_HISTOGRAM_BUCKETS, TIER1_BUCKET, TIER1_WINDOW, TIER2_BUCKET,
    TIER2_WINDOW,
};
pub use broadcast::{collect_broadcast_snapshot, MetricBroadcaster, BROADCAST_PREFIXES};
pub use capability::MetricsAdminCapabilityHandler;
pub use counters::{KernelCounters, KernelCountersSnapshot};
pub use export::{export_prometheus, format_label_pairs};
pub use lifecycle::spawn_lifecycle_metrics_subscriber;
pub use plugin_widget_broadcast::{
    collect_all_bindings, parse_plugin_bindings, remove_plugin_bindings, BindingScope,
    PluginWidgetBroadcaster, SharedBindings, WidgetBinding, WidgetEmitter,
};
pub use proc_state::{collect_proc_state, spawn_proc_state_poller, ProcStateSnapshot};
