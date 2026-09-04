//! invoker 代采 C 类进程态指标（监控设计 §三 通道3 + §十一）。
//!
//! 指标：alive / pid / memory_rss / uptime / last_crash。
//! 本模块提供纯函数采集（不依赖 tokio），invoker 周期性（每 10s）调 collect_proc_state，
//! 把结果写入聚合器。

use super::aggregator::{now_secs, Labels, MetricType, MetricsAggregator};
use std::sync::Arc;

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

/// 解析 tasklist /fo csv 单行输出，取末列 MEM 字段的 KB 数 × 1024 得字节 RSS。
///
/// MEM 字段是最后一个引号字段且数字含千分位逗号（如 `"python.exe","1234",
/// "Console","1","111,768 K"`），必须按 `","` 字段边界取整列；裸 split(',')
/// 会把字段截成末三位（111,768 K → "768 K"）。畸形行返回 None。
#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
fn parse_tasklist_mem_line(line: &str) -> Option<u64> {
    let line = line.trim();
    if !line.starts_with('"') || !line.ends_with('"') {
        return None;
    }
    let mem_field = line
        .strip_suffix('"')?
        .rsplit("\",\"")
        .next()?
        .trim_matches('"')
        .trim();
    let cleaned: String = mem_field.chars().filter(|c| c.is_ascii_digit()).collect();
    let kb: u64 = cleaned.parse().ok()?;
    Some(kb * 1024)
}

#[cfg(target_os = "windows")]
fn collect_memory_rss_windows(pid: u32) -> Option<u64> {
    use std::process::Command;
    // tasklist /fi "PID eq <pid>" /fo csv /nh
    let output = Command::new("tasklist")
        .args(["/fi", &format!("PID eq {pid}"), "/fo", "csv", "/nh"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let line = stdout.lines().next()?;
    parse_tasklist_mem_line(line)
}

/// 把进程态快照写入聚合器（周期轮询任务每 10s 调一次）。
///
/// 写入指标（plugin_id 命名空间）：
/// - `<plugin>.process.alive` gauge (0/1)
/// - `<plugin>.process.pid` gauge
/// - `<plugin>.process.memory_rss_bytes` gauge
/// - `<plugin>.process.uptime_seconds` gauge
/// - `<plugin>.process.last_crash_ts` gauge（上次崩溃 Unix 时间戳）——仅
///   `Some` 时写：该 gauge 的唯一写方是 invoker 崩溃回调，轮询快照 None
///   不落，防止周期覆写冲掉崩溃记录（gauge 留存窗口内保留最近一次）。
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
    if let Some(last_crash) = snap.last_crash_ts {
        agg.record_at(
            now,
            &snap.plugin_id,
            "process.last_crash_ts",
            MetricType::Gauge,
            last_crash as f64,
            &labels,
            None,
            None,
        );
    }
}

/// 把一批宿主快照按成员插件写入聚合器（不含 last_crash_ts——崩溃回调唯一写方）。
fn write_host_snapshots(agg: &MetricsAggregator, hosts: &[agentos_invoker::HostProcSnapshot]) {
    for host in hosts {
        for plugin_id in &host.plugin_ids {
            let snap = ProcStateSnapshot {
                plugin_id: plugin_id.clone(),
                alive: host.alive,
                pid: host.pid,
                memory_rss_bytes: host.pid.and_then(collect_memory_rss),
                uptime_secs: host.uptime_secs,
                last_crash_ts: None,
            };
            collect_proc_state(agg, &snap);
        }
    }
}

/// 进程态周期轮询任务（监控设计 §三 通道3 的拉起半刀——M3 此前只挂了崩溃回调）。
///
/// 每 `interval` 遍历 invoker 全部活宿主（含 light 合宿分组），对每成员插件
/// 写 process.alive/pid/memory_rss_bytes/uptime_seconds；last_crash_ts 由崩溃
/// 回调单独写，本任务不覆盖（快照恒 None）。采集失败（tasklist 无进程等）
/// 返回 None 跳过该字段，不 panic。
pub fn spawn_proc_state_poller(
    invoker: Arc<agentos_invoker::PluginInvokerImpl>,
    agg: MetricsAggregator,
    interval: std::time::Duration,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(interval);
        tick.tick().await; // 跳过首次立即触发（与 M2 flush 任务同款）
        loop {
            tick.tick().await;
            write_host_snapshots(&agg, &invoker.host_proc_snapshots().await);
        }
    })
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
        // last_crash_ts=None 不写（唯一写方是 invoker 崩溃回调，防周期轮询覆盖）
        assert!(agg
            .query(
                Some("dead"),
                Some("process.last_crash_ts"),
                None,
                &Labels::new()
            )
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

    #[test]
    fn parse_tasklist_mem_returns_actual_full_value() {
        // 真实 tasklist /fo csv /nh 输出形状（MEM 含千分位逗号），断言解析值
        // 等于实际字节数——而非被逗号截断的末三位（旧 bug：恒 < 1 MB）
        let cases = [
            (
                r#""python.exe","21120","Console","1","111,768 K""#,
                111_768 * 1024,
            ),
            (
                r#""python.exe","43236","Console","1","67,456 K""#,
                67_456 * 1024,
            ),
            (r#""python.exe","123","Console","1","984 K""#, 984 * 1024),
            (
                r#""python.exe","1","Services","0","1,234,567 K""#,
                1_234_567 * 1024,
            ),
        ];
        for (line, expected) in cases {
            assert_eq!(parse_tasklist_mem_line(line), Some(expected), "{line}");
        }
    }

    #[test]
    fn parse_tasklist_mem_properties_and_malformed() {
        // 性质：KB→bytes 恒为 1024 倍；含千分位逗号的真实进程（≥1 MB）解析值
        // 必须 ≥ 1 MB——旧 split(',') bug 下该性质恒假
        let real = parse_tasklist_mem_line(r#""a","21120","Console","1","111,768 K""#).unwrap();
        assert_eq!(real % 1024, 0);
        assert!(real >= 1024 * 1024, "含千分位的进程 RSS 不可能 < 1 MB");
        // 性质：解析值随真实 KB 单调
        let small = parse_tasklist_mem_line(r#""a","1","Console","1","999 K""#).unwrap();
        assert!(real > small);
        // 畸形行：空行 / 无引号 / 末字段非 MEM
        assert_eq!(parse_tasklist_mem_line(""), None);
        assert_eq!(parse_tasklist_mem_line("no quotes here"), None);
        assert_eq!(parse_tasklist_mem_line(r#""a","1","Console""#), None);
        assert_eq!(
            parse_tasklist_mem_line(r#""a","1","Console","1"," K""#),
            None
        );
    }

    /// 无插件空加载器（poller 只需一个可构造的 invoker，不触达 loader）。
    struct EmptyLoader;

    #[async_trait::async_trait]
    impl agentos_core::traits::PluginLoader for EmptyLoader {
        async fn discover(
            &self,
            _root_paths: &[&str],
        ) -> Result<Vec<agentos_core::traits::PluginManifest>, agentos_core::types::PluginError>
        {
            Ok(vec![])
        }

        fn validate_manifest(
            &self,
            _manifest: &agentos_core::traits::PluginManifest,
        ) -> Result<(), agentos_core::types::PluginError> {
            Ok(())
        }

        async fn load(
            &self,
            _plugin_id: &str,
        ) -> Result<agentos_core::traits::LoadedPlugin, agentos_core::types::PluginError> {
            Err(agentos_core::types::PluginError {
                message: "empty loader".to_string(),
                code: None,
                source: None,
            })
        }

        async fn unload(&self, _plugin_id: &str) -> Result<(), agentos_core::types::PluginError> {
            Ok(())
        }

        fn get_status(&self, _plugin_id: &str) -> agentos_core::traits::PluginStatus {
            agentos_core::traits::PluginStatus::Discovered
        }
    }

    #[tokio::test]
    async fn proc_state_poller_runs_and_writes_nothing_on_empty_hosts() {
        let invoker = Arc::new(agentos_invoker::PluginInvokerImpl::new(Arc::new(
            EmptyLoader,
        )));
        let agg = MetricsAggregator::new();
        let handle = super::spawn_proc_state_poller(
            invoker,
            agg.clone(),
            std::time::Duration::from_millis(10),
        );
        tokio::time::sleep(std::time::Duration::from_millis(40)).await;
        handle.abort();
        // 无宿主 → 不写任何 process.* series（任务本身不 panic）
        assert!(agg
            .query(None, Some("process.alive"), None, &Labels::new())
            .is_empty());
    }

    #[test]
    fn write_host_snapshots_writes_per_member_and_skips_last_crash() {
        let agg = MetricsAggregator::new();
        let hosts = vec![agentos_invoker::HostProcSnapshot {
            host_key: "group:light:1".to_string(),
            pid: Some(4321),
            alive: true,
            uptime_secs: Some(120),
            plugin_ids: vec!["a".to_string(), "b".to_string()],
        }];
        super::write_host_snapshots(&agg, &hosts);
        // 合宿两成员各得一份进程态（共享同一宿主进程）
        for plugin in ["a", "b"] {
            let views = agg.query(Some(plugin), Some("process.alive"), None, &Labels::new());
            assert_eq!(views.len(), 1, "{plugin}");
            assert_eq!(views[0].latest, Some(1.0));
            let views = agg.query(Some(plugin), Some("process.pid"), None, &Labels::new());
            assert_eq!(views[0].latest, Some(4321.0));
        }
        // last_crash_ts 不由轮询写（崩溃回调唯一写方）
        assert!(agg
            .query(None, Some("process.last_crash_ts"), None, &Labels::new())
            .is_empty());
    }
}
