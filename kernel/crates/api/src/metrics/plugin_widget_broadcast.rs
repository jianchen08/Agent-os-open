//! 插件 widget 配置驱动推送（内核统一编排）。
//!
//! 与 [`super::broadcast::MetricBroadcaster`] 的区别：
//! - MetricBroadcaster：内核**写死白名单**，只推 `kernel.*`/`process.*` 状态栏指标。
//! - PluginWidgetBroadcaster：读插件 manifest 的 `contributes.widgets[].metric_bindings`
//!   声明，把**插件已上报的指标**按 widget_id 推给前端。
//!
//! ## 数据流（内核统一编排，插件被动）
//! ```text
//! 插件(被动)                内核                          前端
//!   │ metrics.record ──────→ MetricsAggregator             │
//!   │ (管道执行时顺带上报)   │                              │
//!   │                        │                              │
//!   │            contributes.widgets[].metric_bindings     │
//!   │            (启动期解析为 WidgetBinding 表)            │
//!   │                        │                              │
//!   │                PluginWidgetBroadcaster(后台 tick)     │
//!   │                按 binding 取 series.latest             │
//!   │                        │ broadcast_widget ──────────→│ 按 contributes 渲染
//!   │                        │   {widget_event}            │
//! ```
//!
//! **插件侧零改动**：只要插件已 `metrics.record` 上报某指标，并在 manifest 声明绑定，
//! 内核自动推送。插件不调 emit、不感知推送。
//!
//! 详见 ADR `docs/working/重要设计/插件能力统一模型设计.md` §3.5'。

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use parking_lot::Mutex;
use serde_json::{json, Value};
use tracing::{debug, info, warn};

use super::aggregator::{Labels, MetricsAggregator};

/// widget 推送出口（依赖倒置，便于测试用 spy）。
///
/// 生产实现是 `SessionCoordinator`（同时实现 broadcast_widget / emit_widget）。
#[async_trait]
pub trait WidgetEmitter: Send + Sync {
    /// 广播 widget 事件到全部活跃连接（EmitScope::Broadcast）。
    /// 返回投递数（0 = 无连接或被限流丢弃）。
    async fn broadcast_widget(
        &self,
        widget_id: &str,
        event: &str,
        data: Value,
        plugin_id: &str,
    ) -> usize;
}

/// metric_bindings 的 scope 枚举。MVP 只支持 broadcast。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BindingScope {
    Broadcast,
}

impl Default for BindingScope {
    fn default() -> Self {
        Self::Broadcast
    }
}

/// 一条 widget↔metric 绑定（启动期从 manifest 解析得到）。
#[derive(Debug, Clone)]
pub struct WidgetBinding {
    /// 目标 widget id（前端按此匹配 widget_event.data.widget_id）。
    pub widget_id: String,
    /// 指标来源插件 id（解析 "self" 时替换为该 manifest 的 plugin_id）。
    pub plugin_id: String,
    /// 指标名（对应 metrics.record 的 name）。
    pub metric: String,
    /// 推送间隔。MVP 单一 tick（TICK_INTERVAL）下，实际生效粒度受 tick 限制。
    pub interval: Duration,
    /// 推送 scope（MVP 仅 broadcast）。
    pub scope: BindingScope,
    /// 声明此绑定的插件 id（用于日志与 plugin_id="self" 解析）。
    pub owner_plugin_id: String,
}

/// 默认推送间隔（1s，与 MetricBroadcaster 对齐）。
pub const DEFAULT_INTERVAL_MS: u64 = 1_000;
/// 后台 tick 粒度（1s）。所有绑定共享一个 tick，按各自 last_pushed 判断是否到点。
const TICK_INTERVAL: Duration = Duration::from_secs(1);

/// 解析单个插件的 contributes.widgets[].metric_bindings → WidgetBinding 列表。
///
/// 容错策略（ADR §6.4 注册失败处理同构）：单条绑定解析失败只 warn 跳过，不阻断其他。
/// `contributes` 为 `Option<Value>` 透传结构（traits.rs），本函数负责解释其内部结构。
pub fn parse_plugin_bindings(
    owner_plugin_id: &str,
    contributes: Option<&Value>,
) -> Vec<WidgetBinding> {
    let widgets = match contributes.and_then(|c| c.get("widgets")).and_then(|w| w.as_array()) {
        Some(arr) => arr,
        None => return Vec::new(),
    };
    let mut out = Vec::new();
    for (idx, widget) in widgets.iter().enumerate() {
        let widget_id = match widget.get("id").and_then(|v| v.as_str()) {
            Some(s) => s.to_string(),
            None => {
                warn!(
                    plugin_id = owner_plugin_id,
                    widget_index = idx,
                    "contributes.widgets[] missing 'id', skip binding"
                );
                continue;
            }
        };
        let bindings = match widget.get("metric_bindings") {
            Some(b) => b,
            None => continue, // 无 metric_bindings = 纯静态 widget，正常跳过
        };
        let metric = match bindings.get("metric").and_then(|v| v.as_str()) {
            Some(s) => s.to_string(),
            None => {
                warn!(
                    plugin_id = owner_plugin_id,
                    widget_id = %widget_id,
                    "metric_bindings missing 'metric', skip"
                );
                continue;
            }
        };
        // plugin_id 解析："self" → owner，其他原样（允许引用别的插件指标）。
        let raw_pid = bindings.get("plugin_id").and_then(|v| v.as_str());
        let plugin_id = match raw_pid {
            Some("self") | None => owner_plugin_id.to_string(),
            Some(other) => other.to_string(),
        };
        let interval_ms = bindings
            .get("interval_ms")
            .and_then(|v| v.as_u64())
            .unwrap_or(DEFAULT_INTERVAL_MS)
            .max(TICK_INTERVAL.as_millis() as u64);
        let scope = match bindings
            .get("scope")
            .and_then(|v| v.as_str())
            .unwrap_or("broadcast")
        {
            "broadcast" | "" => BindingScope::Broadcast,
            other => {
                warn!(
                    plugin_id = owner_plugin_id,
                    widget_id = %widget_id,
                    scope = other,
                    "unsupported scope (only 'broadcast' in MVP), fallback to broadcast"
                );
                BindingScope::Broadcast
            }
        };
        out.push(WidgetBinding {
            widget_id,
            plugin_id,
            metric,
            interval: Duration::from_millis(interval_ms),
            scope,
            owner_plugin_id: owner_plugin_id.to_string(),
        });
    }
    out
}

/// 收集所有插件的绑定。
///
/// 接收 `(plugin_id, contributes)` 迭代器，避免在指标模块里硬依赖 `PluginManifest`
/// 类型（保持 metrics crate 边界清晰）。调用方（agentos-kernel）负责从 manifests 投影。
pub fn collect_all_bindings<'a, I>(manifests: I) -> Vec<WidgetBinding>
where
    I: IntoIterator<Item = (&'a str, Option<&'a Value>)>,
{
    let mut all = Vec::new();
    for (id, contributes) in manifests {
        all.extend(parse_plugin_bindings(id, contributes));
    }
    all
}

/// 取一条 binding 对应 series 的 latest 值（gauge/counter/histogram 通用）。
///
/// counter 的 latest 是累计值（aggregator.rs latest_value）；
/// gauge 是 avg；histogram 是 sum——由 metric_type 决定，本函数不转换。
fn latest_value(agg: &MetricsAggregator, binding: &WidgetBinding) -> Option<Value> {
    let empty = Labels::new();
    let views = agg.query(Some(&binding.plugin_id), Some(&binding.metric), None, &empty);
    // 同 plugin+metric 可能因 labels 不同有多条；MVP 合并所有 latest 为 {labels: value}。
    if views.is_empty() {
        return None;
    }
    if views.len() == 1 && views[0].labels.is_empty() {
        return views[0].latest.map(|v| json!({"value": v}));
    }
    // 多 labels：{ "by_label": { "<labels_json>": value } }
    let mut by_label = serde_json::Map::new();
    for v in &views {
        if let Some(latest) = v.latest {
            let key = labels_key(&v.labels);
            by_label.insert(key, json!(latest));
        }
    }
    if by_label.is_empty() {
        None
    } else {
        Some(Value::Object(by_label))
    }
}

/// labels → 稳定字符串 key（"{k=v,k=v}"）。
fn labels_key(labels: &Labels) -> String {
    let pairs: Vec<String> = labels
        .iter()
        .map(|(k, v)| format!("{k}={v}"))
        .collect();
    format!("{{{}}}", pairs.join(","))
}

/// 后台推送任务。
pub struct PluginWidgetBroadcaster;

impl PluginWidgetBroadcaster {
    /// 启动后台推送循环。返回 join handle（drop 不影响已 spawn 的任务）。
    ///
    /// - `agg`：指标聚合器（插件已 metrics.record 写入此处）
    /// - `bindings`：启动期解析出的绑定表
    /// - `emitter`：推送出口（生产用 SessionCoordinator）
    pub fn spawn(
        agg: Arc<MetricsAggregator>,
        bindings: Vec<WidgetBinding>,
        emitter: Arc<dyn WidgetEmitter>,
    ) -> tokio::task::JoinHandle<()> {
        // 每条绑定的上次推送时间（启动时全部置 0，首次 tick 即触发）。
        let last_pushed: Arc<Mutex<Vec<std::time::Instant>>> =
            Arc::new(Mutex::new(vec![std::time::Instant::now(); bindings.len()]));
        let bindings = Arc::new(bindings);
        tokio::spawn(async move {
            if bindings.is_empty() {
                debug!(target: "plugin_widget_broadcast", "no bindings, task idle");
                return;
            }
            let mut tick = tokio::time::interval(TICK_INTERVAL);
            tick.tick().await; // 跳过首次立即触发
            info!(
                target: "plugin_widget_broadcast",
                count = bindings.len(),
                "PluginWidgetBroadcaster started ({} bindings, {:?} tick)",
                bindings.len(),
                TICK_INTERVAL
            );
            loop {
                tick.tick().await;
                let now = std::time::Instant::now();
                // 计算本轮该推哪些绑定
                let due: Vec<usize> = {
                    let lp = last_pushed.lock();
                    (0..bindings.len())
                        .filter(|&i| now.duration_since(lp[i]) >= bindings[i].interval)
                        .collect()
                };
                for i in due {
                    let b = &bindings[i];
                    let data = match latest_value(&agg, b) {
                        Some(d) => d,
                        None => continue, // 该指标无数据 → 跳过（不推空帧）
                    };
                    let _ = emitter
                        .broadcast_widget(&b.widget_id, "snapshot", data, &b.owner_plugin_id)
                        .await;
                    last_pushed.lock()[i] = now;
                }
            }
        })
    }
}

/// 生产实现：SessionCoordinator 直接满足 WidgetEmitter。
///
/// broadcast_widget 签名与 SessionCoordinator::broadcast_widget 完全一致，
/// 直接转发。孤儿规则允许（trait 在本 crate 定义，类型在 agentos_session）。
#[async_trait]
impl WidgetEmitter for agentos_session::SessionCoordinator {
    async fn broadcast_widget(
        &self,
        widget_id: &str,
        event: &str,
        data: Value,
        plugin_id: &str,
    ) -> usize {
        agentos_session::SessionCoordinator::broadcast_widget(
            self,
            widget_id,
            event,
            data,
            plugin_id,
        )
        .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::aggregator::MetricType;
    use async_trait::async_trait;
    use parking_lot::Mutex as PlMutex;
    use serde_json::json;
    use std::sync::Arc as StdArc;

    /// spy emitter：记录所有调用。
    struct SpyEmitter {
        calls: PlMutex<Vec<(String, String, Value, String)>>,
    }

    #[async_trait]
    impl WidgetEmitter for SpyEmitter {
        async fn broadcast_widget(
            &self,
            widget_id: &str,
            event: &str,
            data: Value,
            plugin_id: &str,
        ) -> usize {
            self.calls.lock().push((
                widget_id.to_string(),
                event.to_string(),
                data,
                plugin_id.to_string(),
            ));
            1
        }
    }

    #[test]
    fn test_collect_all_bindings_multiple_plugins() {
        let c1 = json!({
            "widgets": [{ "id": "w1", "metric_bindings": { "metric": "a" } }]
        });
        let c2 = json!({
            "widgets": [{ "id": "w2", "metric_bindings": { "metric": "b" } }]
        });
        let entries: Vec<(&str, Option<&Value>)> = vec![
            ("p1", Some(&c1)),
            ("p2", Some(&c2)),
            ("p3", None), // 无 contributes
        ];
        let bindings = collect_all_bindings(entries);
        assert_eq!(bindings.len(), 2);
        assert_eq!(bindings[0].widget_id, "w1");
        assert_eq!(bindings[1].widget_id, "w2");
    }

    #[test]
    fn test_parse_valid_binding() {
        let contributes = json!({
            "widgets": [{
                "id": "cost_panel",
                "widget": "cost_dashboard",
                "metric_bindings": {
                    "metric": "tokens.used",
                    "plugin_id": "self",
                    "interval_ms": 2000
                }
            }]
        });
        let bindings = parse_plugin_bindings("cost_plugin", Some(&contributes));
        assert_eq!(bindings.len(), 1);
        assert_eq!(bindings[0].widget_id, "cost_panel");
        assert_eq!(bindings[0].plugin_id, "cost_plugin"); // self 解析为 owner
        assert_eq!(bindings[0].metric, "tokens.used");
        assert_eq!(bindings[0].interval, Duration::from_millis(2000));
        assert_eq!(bindings[0].scope, BindingScope::Broadcast);
    }

    #[test]
    fn test_parse_missing_metric_skipped() {
        let contributes = json!({
            "widgets": [{
                "id": "w1",
                "metric_bindings": { "plugin_id": "self" }  // 缺 metric
            }, {
                "id": "w2",
                "metric_bindings": { "metric": "ok" }       // 合法
            }]
        });
        let bindings = parse_plugin_bindings("p1", Some(&contributes));
        assert_eq!(bindings.len(), 1);
        assert_eq!(bindings[0].widget_id, "w2");
    }

    #[test]
    fn test_parse_no_metric_bindings_skipped_silently() {
        // 无 metric_bindings 的 widget 是纯静态 widget，正常跳过（不 warn）
        let contributes = json!({ "widgets": [{ "id": "static_w", "widget": "form" }] });
        let bindings = parse_plugin_bindings("p1", Some(&contributes));
        assert!(bindings.is_empty());
    }

    #[test]
    fn test_parse_missing_widget_id_skipped() {
        let contributes = json!({
            "widgets": [{ "metric_bindings": { "metric": "x" } }]  // 缺 id
        });
        let bindings = parse_plugin_bindings("p1", Some(&contributes));
        assert!(bindings.is_empty());
    }

    #[test]
    fn test_parse_cross_plugin_reference() {
        // plugin_id 引用别的插件
        let contributes = json!({
            "widgets": [{
                "id": "w1",
                "metric_bindings": { "metric": "other_metric", "plugin_id": "other_plugin" }
            }]
        });
        let bindings = parse_plugin_bindings("p1", Some(&contributes));
        assert_eq!(bindings[0].plugin_id, "other_plugin");
        assert_eq!(bindings[0].owner_plugin_id, "p1");
    }

    #[test]
    fn test_parse_no_contributes_returns_empty() {
        assert!(parse_plugin_bindings("p1", None).is_empty());
        assert!(parse_plugin_bindings("p1", Some(&json!({}))).is_empty());
        assert!(parse_plugin_bindings("p1", Some(&json!({"widgets": []}))).is_empty());
    }

    #[test]
    fn test_parse_unsupported_scope_fallback_broadcast() {
        let contributes = json!({
            "widgets": [{
                "id": "w1",
                "metric_bindings": { "metric": "x", "scope": "thread" }
            }]
        });
        let bindings = parse_plugin_bindings("p1", Some(&contributes));
        assert_eq!(bindings[0].scope, BindingScope::Broadcast);
    }

    #[test]
    fn test_parse_interval_clamped_to_tick() {
        let contributes = json!({
            "widgets": [{
                "id": "w1",
                "metric_bindings": { "metric": "x", "interval_ms": 100 }  // 小于 tick
            }]
        });
        let bindings = parse_plugin_bindings("p1", Some(&contributes));
        assert!(bindings[0].interval >= TICK_INTERVAL);
    }

    #[test]
    fn test_latest_value_single_series_no_labels() {
        let agg = MetricsAggregator::new();
        let empty = Labels::new();
        agg.record(
            "p1",
            "tokens",
            MetricType::Gauge,
            42.0,
            &empty,
            None,
            None,
        );
        let binding = WidgetBinding {
            widget_id: "w".into(),
            plugin_id: "p1".into(),
            metric: "tokens".into(),
            interval: Duration::from_secs(1),
            scope: BindingScope::Broadcast,
            owner_plugin_id: "p1".into(),
        };
        let v = latest_value(&agg, &binding).unwrap();
        assert_eq!(v["value"], json!(42.0));
    }

    #[test]
    fn test_latest_value_no_data_returns_none() {
        let agg = MetricsAggregator::new();
        let binding = WidgetBinding {
            widget_id: "w".into(),
            plugin_id: "p1".into(),
            metric: "missing".into(),
            interval: Duration::from_secs(1),
            scope: BindingScope::Broadcast,
            owner_plugin_id: "p1".into(),
        };
        assert!(latest_value(&agg, &binding).is_none());
    }

    #[test]
    fn test_latest_value_multiple_labels() {
        let agg = MetricsAggregator::new();
        let mut l1 = Labels::new();
        l1.insert("model".into(), "a".into());
        let mut l2 = Labels::new();
        l2.insert("model".into(), "b".into());
        agg.record("p1", "tokens", MetricType::Counter, 10.0, &l1, None, None);
        agg.record("p1", "tokens", MetricType::Counter, 20.0, &l2, None, None);
        let binding = WidgetBinding {
            widget_id: "w".into(),
            plugin_id: "p1".into(),
            metric: "tokens".into(),
            interval: Duration::from_secs(1),
            scope: BindingScope::Broadcast,
            owner_plugin_id: "p1".into(),
        };
        let v = latest_value(&agg, &binding).unwrap();
        let obj = v.as_object().unwrap();
        assert_eq!(obj.len(), 2);
        assert!(obj.contains_key("{model=a}"));
        assert!(obj.contains_key("{model=b}"));
    }

    #[tokio::test]
    async fn test_broadcaster_pushes_when_data_exists() {
        let agg = StdArc::new(MetricsAggregator::new());
        let empty = Labels::new();
        agg.record(
            "p1",
            "tokens",
            MetricType::Gauge,
            99.0,
            &empty,
            None,
            None,
        );
        let spy = StdArc::new(SpyEmitter {
            calls: PlMutex::new(Vec::new()),
        });
        let binding = WidgetBinding {
            widget_id: "w".into(),
            plugin_id: "p1".into(),
            metric: "tokens".into(),
            interval: Duration::from_millis(0), // 立即到期
            scope: BindingScope::Broadcast,
            owner_plugin_id: "p1".into(),
        };
        // 单次 tick 模拟：直接调用一次 push 逻辑（不 spawn loop）
        let data = latest_value(&agg, &binding).unwrap();
        spy.broadcast_widget(&binding.widget_id, "snapshot", data, &binding.owner_plugin_id)
            .await;
        let calls = spy.calls.lock();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "w");
        assert_eq!(calls[0].1, "snapshot");
        assert_eq!(calls[0].2["value"], json!(99.0));
        assert_eq!(calls[0].3, "p1");
    }

    #[tokio::test]
    async fn test_broadcaster_skips_when_no_data() {
        let agg = StdArc::new(MetricsAggregator::new());
        let spy = StdArc::new(SpyEmitter {
            calls: PlMutex::new(Vec::new()),
        });
        let binding = WidgetBinding {
            widget_id: "w".into(),
            plugin_id: "p1".into(),
            metric: "missing".into(),
            interval: Duration::from_millis(0),
            scope: BindingScope::Broadcast,
            owner_plugin_id: "p1".into(),
        };
        let data = latest_value(&agg, &binding);
        assert!(data.is_none());
        // broadcaster 内部 None 分支 continue，不调用 emitter
        assert_eq!(spy.calls.lock().len(), 0);
    }
}
