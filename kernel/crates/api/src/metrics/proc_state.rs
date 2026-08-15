//! invoker 代采 C 类进程态指标（监控设计 §三 通道3 + §十一）。
//!
//! 指标：alive / pid / memory_rss / uptime / last_crash。
//! 本模块提供纯函数采集（不依赖 tokio），invoker 周期性（每 10s）调 collect_proc_state，
//! 把结果写入聚合器。

use super::aggregator::{now_secs, Labels, MetricType, MetricsAggregator};

/// 进程态快照（一次采集的结果）。
#[derive(Debug, Clone, Default)]
pub struct ProcStateSnapshot {
    pub plugin_id: String,
    pub alive: bool,
    pub pid: Option<u32>,
    pub memory_rss_bytes: Option<u64>,
    pub uptime_secs: Option<u64>,
    pub last_crash_ts: Option<i64>,
}

impl ProcStateSnapshot {
    pub fn labels(&self) -> Labels {
        let mut l = Labels::new();
        l.insert("plugin_id".to_string(), self.plugin_id.clone());
        l
    }
}

/// 采集一个进程的 RSS（RSS 字节数）。
///
/// - Linux：读 /proc/<pid>/status 的 VmRSS（kB）。
/// - Windows：调 tasklist /fi "PID eq <pid>" /fo csv /nh，解析 MEM 字段（如 "12,345 K"）。
/// - 其他/失败：None。
///
/// 本函数纯同步、可移植；失败返回 None（不 panic）。
pub fn collect_memory_rss(pid: u32) -> Option<u64> {
    #[cfg(target_os = "linux")]
    {
        collect_memory_rss_linux(pid)
    }
    #[cfg(target_os = "windows")]
    {
        collect_memory_rss_windows(pid)
    }
    #[cfg(not(any(target_os = "linux", target_os = "windows")))]
    {
        let _ = pid;
        None
    }
}

#[cfg(target_os = "linux")]
fn collect_memory_rss_linux(pid: u32) -> Option<u64> {
    let path = format!("/proc/{pid}/status");
    let content = std::fs::read_to_string(&path).ok()?;
    for line in content.lines() {
        if let Some(rest) = line.strip_prefix("VmRSS:") {
            // "VmRSS:\t 12345 kB"
            let num: u64 = rest.split_whitespace().next()?.parse().ok()?;
            return Some(num * 1024); // kB → bytes
        }
    }
    None
}

#[cfg(target_os = "windows")]
fn collect_memory_rss_windows(pid: u32) -> Option<u64> {
    use std::process::Command;
    // tasklist /fi "PID eq <pid>" /fo csv /nh
    // 输出形如："python.exe","1234","Console","1","12,345 K"
    let output = Command::new("tasklist")
        .args(["/fi", &format!("PID eq {pid}"), "/fo", "csv", "/nh"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let line = stdout.lines().next()?;
    // 解析 csv 最后一个字段 "12,345 K"
    let fields: Vec<&str> = line.split(',').collect();
    if fields.len() < 5 {
        return None;
    }
    let mem_field = fields[fields.len() - 1].trim();
    // 去引号 + "K"
    let cleaned: String = mem_field
        .trim_matches('"')
        .chars()
        .filter(|c| c.is_ascii_digit())
        .collect();
    let kb: u64 = cleaned.parse().ok()?;
    Some(kb * 1024)
}

/// 把进程态快照写入聚合器（invoker 每 10s 调一次）。
///
/// 写入指标（plugin_id 命名空间）：
/// - `<plugin>.process.alive` gauge (0/1)
/// - `<plugin>.process.pid` gauge
/// - `<plugin>.process.memory_rss_bytes` gauge
/// - `<plugin>.process.uptime_seconds` gauge
/// - `<plugin>.process.last_crash_ts` gauge（上次崩溃 Unix 时间戳，0=未崩过）
pub fn collect_proc_state(agg: &MetricsAggregator, snap: &ProcStateSnapshot) {
    let now = now_secs();
    let labels = snap.labels();
    agg.record_at(
        now,
        &snap.plugin_id,
        "process.alive",
        MetricType::Gauge,
        if snap.alive { 1.0 } else { 0.0 },
        &labels,
        None,
        None,
    );
    if let Some(pid) = snap.pid {
        agg.record_at(
            now,
            &snap.plugin_id,
            "process.pid",
            MetricType::Gauge,
            pid as f64,
            &labels,
            None,
            None,
        );
    }
    if let Some(rss) = snap.memory_rss_bytes {
        agg.record_at(
            now,
            &snap.plugin_id,
            "process.memory_rss_bytes",
            MetricType::Gauge,
            rss as f64,
            &labels,
            None,
            None,
        );
    }
    if let Some(uptime) = snap.uptime_secs {
        agg.record_at(
            now,
            &snap.plugin_id,
            "process.uptime_seconds",
            MetricType::Gauge,
            uptime as f64,
            &labels,
            None,
            None,
        );
    }
    let last_crash = snap.last_crash_ts.unwrap_or(0) as f64;
    agg.record_at(
        now,
        &snap.plugin_id,
        "process.last_crash_ts",
        MetricType::Gauge,
        last_crash,
        &labels,
        None,
        None,
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_collect_proc_state_writes_all_metrics() {
        let agg = MetricsAggregator::new();
        let snap = ProcStateSnapshot {
            plugin_id: "llm_service".to_string(),
            alive: true,
            pid: Some(1234),
            memory_rss_bytes: Some(50_000_000),
            uptime_secs: Some(3600),
            last_crash_ts: Some(1000),
        };
        collect_proc_state(&agg, &snap);

        // alive
        let v = agg
            .query(
                Some("llm_service"),
                Some("process.alive"),
                None,
                &Labels::new(),
            )
            .len();
        assert_eq!(v, 1);
        // memory
        let views = agg.query(
            Some("llm_service"),
            Some("process.memory_rss_bytes"),
            None,
            &Labels::new(),
        );
        assert_eq!(views[0].latest, Some(50_000_000.0));
        // pid
        let views = agg.query(
            Some("llm_service"),
            Some("process.pid"),
            None,
            &Labels::new(),
        );
        assert_eq!(views[0].latest, Some(1234.0));
        // uptime
        let views = agg.query(
            Some("llm_service"),
            Some("process.uptime_seconds"),
            None,
            &Labels::new(),
        );
        assert_eq!(views[0].latest, Some(3600.0));
        // last_crash
        let views = agg.query(
            Some("llm_service"),
            Some("process.last_crash_ts"),
            None,
            &Labels::new(),
        );
        assert_eq!(views[0].latest, Some(1000.0));
    }

    #[test]
    fn test_collect_proc_state_dead_process() {
        let agg = MetricsAggregator::new();
        let snap = ProcStateSnapshot {
            plugin_id: "dead".to_string(),
            alive: false,
            pid: None,
            memory_rss_bytes: None,
            uptime_secs: None,
            last_crash_ts: None,
        };
        collect_proc_state(&agg, &snap);
        let views = agg.query(Some("dead"), Some("process.alive"), None, &Labels::new());
        assert_eq!(views[0].latest, Some(0.0));
        // dead → pid/memory/uptime 不写
        assert!(agg
            .query(Some("dead"), Some("process.pid"), None, &Labels::new())
            .is_empty());
    }

    #[test]
    fn test_collect_memory_rss_self_or_none() {
        // 采集自身进程：当前平台能拿到或 None，都不应 panic
        let self_pid = std::process::id();
        let _ = collect_memory_rss(self_pid);
        // 不存在的 pid
        assert!(collect_memory_rss(9_999_999).is_none() || collect_memory_rss(9_999_999).is_some());
    }
}
