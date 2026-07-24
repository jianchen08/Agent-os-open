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
                        out.push_str(&format!(
                            "{mname}_bucket{labels} {}\n",
                            h.counts[i]
                        ));
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
                let value = s.latest.unwrap_or_else(|| {
                    s.samples.last().map(|x| x.value).unwrap_or(0.0)
                });
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
    use super::*;
    use super::super::aggregator::{Labels, MetricType, Sample};

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
            samples: vec![Sample { ts: 1000, value: latest.unwrap_or(0.0) }],
            unit: None,
            help: Some("test help".to_string()),
            latest,
            histogram: None,
        }
    }

    #[test]
    fn test_prom_counter_format() {
        let mut v = make_view("llm_service", "tokens_used", MetricType::Counter, Some(12800.0), Labels::new());
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
        assert_eq!(prom_metric_name("llm.service", "tokens.used"), "llm_service_tokens_used");
        assert_eq!(prom_metric_name("p-1", "m.x"), "p_1_m_x");
    }

    #[test]
    fn test_prom_label_pairs_empty() {
        let labels = Labels::new();
        assert_eq!(format_label_pairs(&labels), "");
    }
}
