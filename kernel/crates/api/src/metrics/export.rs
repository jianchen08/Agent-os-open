//! Prometheus exposition format 导出（监控设计 §十一 决策3）。
//!
//! 格式：
//! ```text
//! # HELP <name> <help>
//! # TYPE <name> <counter|gauge|histogram>
//! <name>{labels} <value>
//! ```

use super::aggregator::{MetricSeriesView, MetricType};

/// 把 series 的 labels 转成 Prometheus 标签串（`{k="v",k2="v2"}`）。
pub fn format_label_pairs(labels: &super::Labels) -> String {
    if labels.is_empty() {
        return String::new();
    }
    let mut out = String::from("{");
    let mut first = true;
    for (k, v) in labels {
        if !first {
            out.push(',');
        }
        first = false;
        out.push_str(k);
        out.push_str("=\"");
        out.push_str(&escape_label_value(v));
        out.push('"');
    }
    out.push('}');
    out
}

/// 转义 label 值中的特殊字符（Prometheus 规范：\ " \n）。
fn escape_label_value(v: &str) -> String {
    v.replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
}

/// 指标名转 Prometheus 合法名（plugin.metric → plugin_metric，非字母数字下划线转 _）。
pub fn prom_metric_name(plugin_id: &str, name: &str) -> String {
    let full = format!("{plugin_id}_{name}");
    full.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' {
                c
            } else {
                '_'
            }
        })
        .collect()
}

/// 把所有 series 导出为 Prometheus exposition format 文本。
pub fn export_prometheus(series: &[MetricSeriesView]) -> String {
    let mut out = String::new();
    for s in series {
        let mname = prom_metric_name(&s.plugin_id, &s.name);
        let typ = match s.metric_type {
            MetricType::Counter => "counter",
            MetricType::Gauge => "gauge",
            MetricType::Histogram => "histogram",
        };
        // HELP（可选）
        if let Some(help) = &s.help {
            out.push_str(&format!("# HELP {mname} {help}\n"));
        }
        out.push_str(&format!("# TYPE {mname} {typ}\n"));

        match s.metric_type {
            MetricType::Histogram => {
                if let Some(h) = &s.histogram {
                    // 各桶边界行
                    let bounds: &[f64] = super::DEFAULT_HISTOGRAM_BUCKETS;
                    for (i, &bound) in bounds.iter().enumerate() {
                        let labels = with_le_label(&s.labels, bound);
                        out.push_str(&format!("{mname}_bucket{labels} {}\n", h.counts[i]));
                    }
                    // +Inf 桶
                    let labels = with_le_label(&s.labels, f64::INFINITY);
                    out.push_str(&format!(
                        "{mname}_bucket{labels} {}\n",
                        h.counts.last().copied().unwrap_or(0)
                    ));
                    // sum / count
                    let base_labels = format_label_pairs(&s.labels);
                    out.push_str(&format!("{mname}_sum{base_labels} {}\n", h.sum));
                    out.push_str(&format!("{mname}_count{base_labels} {}\n", h.count));
                }
            }
            MetricType::Counter | MetricType::Gauge => {
                let value = s
                    .latest
                    .unwrap_or_else(|| s.samples.last().map(|x| x.value).unwrap_or(0.0));
                let labels = format_label_pairs(&s.labels);
                out.push_str(&format!("{mname}{labels} {value}\n"));
            }
        }
    }
    out
}

/// 在 labels 基础上加一个 `le="bound"` 标签（histogram 桶用）。
fn with_le_label(labels: &super::Labels, le: f64) -> String {
    let mut out = String::from("{");
    let mut first = true;
    for (k, v) in labels {
        if !first {
            out.push(',');
        }
        first = false;
        out.push_str(k);
        out.push_str("=\"");
        out.push_str(&escape_label_value(v));
        out.push('"');
    }
    if !first {
        out.push(',');
    }
    out.push_str("le=\"");
    if le.is_infinite() {
        out.push_str("+Inf");
    } else {
        out.push_str(&format_le_bound(le));
    }
    out.push_str("\"}");
    out
}

fn format_le_bound(b: f64) -> String {
    // Prometheus 习惯：整数不带小数点
    if b.fract() == 0.0 {
        format!("{}", b as u64)
    } else {
        format!("{b}")
    }
}

#[cfg(test)]
mod tests {
    use super::super::aggregator::{Labels, MetricType, Sample};
    use super::*;

    fn make_view(
        plugin: &str,
        name: &str,
        typ: MetricType,
        latest: Option<f64>,
        labels: Labels,
    ) -> MetricSeriesView {
        MetricSeriesView {
            plugin_id: plugin.to_string(),
            name: name.to_string(),
            metric_type: typ,
            labels,
            samples: vec![Sample {
                ts: 1000,
                value: latest.unwrap_or(0.0),
            }],
            unit: None,
            help: Some("test help".to_string()),
            latest,
            histogram: None,
        }
    }

    #[test]
    fn test_prom_counter_format() {
        let mut v = make_view(
            "llm_service",
            "tokens_used",
            MetricType::Counter,
            Some(12800.0),
            Labels::new(),
        );
        v.help = Some("Total tokens used".to_string());
        let out = export_prometheus(&[v]);
        assert!(out.contains("# HELP llm_service_tokens_used Total tokens used"));
        assert!(out.contains("# TYPE llm_service_tokens_used counter"));
        assert!(out.contains("llm_service_tokens_used 12800"));
    }

    #[test]
    fn test_prom_gauge_with_labels() {
        let mut labels = Labels::new();
        labels.insert("model".to_string(), "deepseek".to_string());
        let v = make_view("p1", "conn", MetricType::Gauge, Some(42.0), labels);
        let out = export_prometheus(&[v]);
        assert!(out.contains("# TYPE p1_conn gauge"));
        assert!(out.contains("p1_conn{model=\"deepseek\"} 42"));
    }

    #[test]
    fn test_prom_label_escaping() {
        let mut labels = Labels::new();
        labels.insert("path".to_string(), "a\"b\\c\n".to_string());
        let v = make_view("p1", "m", MetricType::Gauge, Some(1.0), labels);
        let out = export_prometheus(&[v]);
        assert!(out.contains("path=\"a\\\"b\\\\c\\n\""));
    }

    #[test]
    fn test_prom_metric_name_sanitization() {
        assert_eq!(
            prom_metric_name("llm.service", "tokens.used"),
            "llm_service_tokens_used"
        );
        assert_eq!(prom_metric_name("p-1", "m.x"), "p_1_m_x");
    }

    #[test]
    fn test_prom_label_pairs_empty() {
        let labels = Labels::new();
        assert_eq!(format_label_pairs(&labels), "");
    }

    // ── 多标签 / histogram 桶 / label 值回退（此前只测了单标签与 counter）──

    /// 多标签必须逗号分隔且**保序**（BTreeMap 序），且非首标签才加逗号。
    /// 两组区分度输入：两标签 / 三标签。
    #[test]
    fn test_label_pairs_multiple_labels_comma_separated() {
        let mut labels = Labels::new();
        labels.insert("a".to_string(), "1".to_string());
        labels.insert("b".to_string(), "2".to_string());
        assert_eq!(format_label_pairs(&labels), "{a=\"1\",b=\"2\"}");

        labels.insert("c".to_string(), "3".to_string());
        assert_eq!(format_label_pairs(&labels), "{a=\"1\",b=\"2\",c=\"3\"}");
    }

    /// histogram 导出：桶行带 `le` 标签（多标签时 `le` 追加在既有标签之后），
    /// 且 `+Inf` 桶与 sum/count 齐全。双组：无标签 / 单标签。
    #[test]
    fn test_prom_histogram_buckets_and_le_label() {
        use super::super::aggregator::HistogramBuckets;
        let mut h = HistogramBuckets::new();
        h.observe(0.003);
        h.observe(6.0);

        // 无标签：`{le="0.005"}` 形态
        let mut v = make_view("p1", "lat", MetricType::Histogram, Some(1.0), Labels::new());
        v.histogram = Some(h.clone());
        let plain = export_prometheus(&[v]);
        assert!(plain.contains("# TYPE p1_lat histogram"), "{plain}");
        assert!(plain.contains("p1_lat_bucket{le=\"0.005\"} 1"), "{plain}");
        assert!(plain.contains("p1_lat_bucket{le=\"+Inf\"} 2"), "{plain}");
        assert!(plain.contains("p1_lat_sum 6.003"), "{plain}");
        assert!(plain.contains("p1_lat_count 2"), "{plain}");

        // 单标签：既有标签在前、`le` 追加在后（逗号分隔）
        let mut labels = Labels::new();
        labels.insert("model".to_string(), "deepseek".to_string());
        let mut v2 = make_view("p1", "lat", MetricType::Histogram, Some(1.0), labels);
        v2.histogram = Some(h);
        let labeled = export_prometheus(&[v2]);
        assert!(
            labeled.contains("p1_lat_bucket{model=\"deepseek\",le=\"0.005\"} 1"),
            "{labeled}"
        );
    }

    /// 无 histogram 数据（None）的 histogram series 只出 TYPE 行，不 panic。
    #[test]
    fn test_prom_histogram_without_buckets_emits_type_only() {
        let v = make_view("p1", "h", MetricType::Histogram, None, Labels::new());
        let out = export_prometheus(&[v]);
        assert!(out.contains("# TYPE p1_h histogram"));
        assert!(!out.contains("p1_h_bucket"), "无桶数据不出桶行: {out}");
    }

    /// counter/gauge 缺 latest 时回退到最后一个 sample 值，都没有才 0。
    /// 两组区分度输入：有 samples 无 latest / 双空。
    #[test]
    fn test_prom_counter_gauge_falls_back_to_last_sample_then_zero() {
        let mut with_samples = make_view("p1", "c", MetricType::Counter, None, Labels::new());
        with_samples.samples = vec![Sample { ts: 1, value: 5.0 }, Sample { ts: 2, value: 9.0 }];
        let out = export_prometheus(&[with_samples]);
        assert!(out.contains("p1_c 9"), "回退到最后一个 sample: {out}");

        let mut empty = make_view("p1", "g", MetricType::Gauge, None, Labels::new());
        empty.samples = vec![];
        let out = export_prometheus(&[empty]);
        assert!(out.contains("p1_g 0"), "双空回退 0: {out}");
    }

    /// 无 help 声明时不出 HELP 行（HELP 可选）。
    #[test]
    fn test_prom_without_help_omits_help_line() {
        let mut v = make_view("p1", "m", MetricType::Gauge, Some(1.0), Labels::new());
        v.help = None;
        let out = export_prometheus(&[v]);
        assert!(!out.contains("# HELP"), "无 help 不出 HELP 行: {out}");
        assert!(out.contains("# TYPE p1_m gauge"));
    }

    /// 非整数桶边界保留小数（`format_le_bound` 小数分支），整数边界不带小数点。
    #[test]
    fn test_prom_le_bound_integer_and_fractional() {
        use super::super::aggregator::HistogramBuckets;
        let mut h = HistogramBuckets::new();
        h.observe(0.003);
        let mut v = make_view("p1", "lat", MetricType::Histogram, Some(1.0), Labels::new());
        v.histogram = Some(h);
        let out = export_prometheus(&[v]);
        assert!(out.contains("le=\"0.005\""), "小数边界保留小数点: {out}");
        assert!(out.contains("le=\"1\""), "整数边界（1.0）不带小数点: {out}");
    }
}
