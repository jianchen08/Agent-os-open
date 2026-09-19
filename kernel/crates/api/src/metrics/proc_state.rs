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

// ── 跳板下探（uv venv trampoline）────────────────────────────────────────
//
// Windows 上 uv 创建的 .venv/Scripts/python.exe 是 trampoline 跳板：启动后
// spawn 真实解释器（uv 管理的 base python）为子进程，自身退居 ~5MB 空壳等
// 待（私有提交 <1MB）。invoker 记录的 child.id() 是跳板 pid，直接采集跳板
// RSS 恒为空壳值——监控页插件内存恒显 4-5MB 的根因（真机实证：合宿宿主
// 跳板 4.7MB vs 真实解释器 79.8MB，llm_service 真身 313.9MB）。采集时沿
// pid 下探一层选真实解释器，pid/memory 两列同步取真实进程（与任务管理器
// 同 pid 对得上）。alive 判定不受影响：跳板等待子进程退出，真身死跳板也退。

/// 单次全表进程快照：pid → (exe 名小写, 直接子进程列表)。
///
/// Toolhelp 快照一次遍历全建（~千级进程毫秒级开销），供本轮全部宿主下探
/// 复用；快照失败（句柄分配失败等）返回 None，调用方回落宿主 pid 原样采集。
#[cfg(windows)]
fn build_process_index() -> Option<std::collections::HashMap<u32, (String, Vec<u32>)>> {
    use std::collections::HashMap;
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
        TH32CS_SNAPPROCESS,
    };

    let mut index: HashMap<u32, (String, Vec<u32>)> = HashMap::new();
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snap == windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE {
            return None;
        }
        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
        if Process32FirstW(snap, &mut entry) != 0 {
            loop {
                let pid = entry.th32ProcessID;
                let ppid = entry.th32ParentProcessID;
                let exe = String::from_utf16_lossy(
                    entry.szExeFile.split(|&c| c == 0).next().unwrap_or(&[]),
                )
                .to_ascii_lowercase();
                // 父条目可能先于父进程自身遍历到，两侧都登记
                index
                    .entry(pid)
                    .or_insert_with(|| (String::new(), Vec::new()))
                    .0 = exe;
                index
                    .entry(ppid)
                    .or_insert_with(|| (String::new(), Vec::new()))
                    .1
                    .push(pid);
                if Process32NextW(snap, &mut entry) == 0 {
                    break;
                }
            }
        }
        CloseHandle(snap);
    }
    Some(index)
}

/// 从直接子进程中选真实工作进程 pid：exe 名含 "python" 的优先（trampoline
/// 场景真实解释器是唯一 python 子进程）；无 python 名子进程时取第一个子进
/// 程（.cmd 包装场景 cmd.exe → node.exe 等异构链）；无子进程返回 None
/// （非跳板，调用方回落宿主 pid 自身）。
#[cfg(windows)]
fn pick_worker_child(
    host_pid: u32,
    index: &std::collections::HashMap<u32, (String, Vec<u32>)>,
) -> Option<u32> {
    let children = &index.get(&host_pid)?.1;
    children
        .iter()
        .copied()
        .find(|pid| {
            index
                .get(pid)
                .map(|(exe, _)| exe.contains("python"))
                .unwrap_or(false)
        })
        .or_else(|| children.first().copied())
}

/// 解析用于 RSS/pid 观测的真实进程 pid：有子进程按下探规则选真身，否则原
/// 样返回宿主 pid（非 Windows 无跳板形态，恒原样）。
#[cfg(windows)]
fn resolve_worker_pid(
    host_pid: u32,
    index: &std::collections::HashMap<u32, (String, Vec<u32>)>,
) -> u32 {
    pick_worker_child(host_pid, index).unwrap_or(host_pid)
}

#[cfg(not(windows))]
fn resolve_worker_pid(host_pid: u32, _index: &()) -> u32 {
    host_pid
}

/// 非 Windows 平台无进程索引（下探恒原样，占位类型零开销）。
#[cfg(not(windows))]
fn build_process_index() -> Option<()> {
    Some(())
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
///
/// pid/RSS 按轮次一次性的进程快照下探到真实工作进程（uv venv trampoline
/// 场景跳板空壳 ≠ 真实解释器，见 [`build_process_index`]）；下探失败回落
/// 宿主 pid 原样。alive/uptime 语义不变（跳板与真身同生命周期）。
fn write_host_snapshots(agg: &MetricsAggregator, hosts: &[agentos_invoker::HostProcSnapshot]) {
    let proc_index = build_process_index();
    for host in hosts {
        for plugin_id in &host.plugin_ids {
            let observed_pid = host.pid.map(|pid| {
                proc_index
                    .as_ref()
                    .map_or(pid, |idx| resolve_worker_pid(pid, idx))
            });
            let snap = ProcStateSnapshot {
                plugin_id: plugin_id.clone(),
                alive: host.alive,
                pid: observed_pid.or(host.pid),
                memory_rss_bytes: observed_pid.and_then(collect_memory_rss),
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
            // 不可能存在的 pid（u32::MAX-1）：确保下探索引里无此进程、回落
            // 原样采集——测试不受宿主机真实进程表影响
            pid: Some(u32::MAX - 1),
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
            assert_eq!(views[0].latest, Some((u32::MAX - 1) as f64));
        }
        // last_crash_ts 不由轮询写（崩溃回调唯一写方）
        assert!(agg
            .query(None, Some("process.last_crash_ts"), None, &Labels::new())
            .is_empty());
    }

    // ── 跳板下探 ──

    #[test]
    #[cfg(windows)]
    fn pick_worker_child_prefers_python_named_child() {
        use std::collections::HashMap;
        let index: HashMap<u32, (String, Vec<u32>)> = HashMap::from([
            (100, ("python.exe".into(), vec![201, 202])),
            (201, ("cmd.exe".into(), vec![])),
            (202, ("python.exe".into(), vec![])),
        ]);
        // 含 python 名的子进程优先（trampoline 场景真实解释器）
        assert_eq!(super::pick_worker_child(100, &index), Some(202));
        // 无 python 名子进程：取第一个（.cmd → node 等异构包装链）
        let index2: HashMap<u32, (String, Vec<u32>)> = HashMap::from([
            (300, ("cmd.exe".into(), vec![401, 402])),
            (401, ("node.exe".into(), vec![])),
            (402, ("conhost.exe".into(), vec![])),
        ]);
        assert_eq!(super::pick_worker_child(300, &index2), Some(401));
        // 无子进程：非跳板 → None（调用方回落宿主 pid）
        let index3: HashMap<u32, (String, Vec<u32>)> =
            HashMap::from([(500, ("python.exe".into(), vec![]))]);
        assert_eq!(super::pick_worker_child(500, &index3), None);
        // 索引中不存在的 pid：None
        assert_eq!(super::pick_worker_child(999, &index), None);
    }

    /// 真实进程表性质：spawn 存活子进程后，全表快照索引能把当前测试进程
    /// 下探到该子进程（trampoline 解析链路走真实 OS 数据）。
    #[test]
    #[cfg(windows)]
    fn resolve_worker_pid_finds_real_spawned_child() {
        use std::process::{Command, Stdio};
        let mut child = Command::new("ping")
            .args(["-n", "30", "127.0.0.1"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn ping");
        let child_pid = child.id();
        let index = super::build_process_index().expect("process snapshot");
        let resolved = super::resolve_worker_pid(std::process::id(), &index);
        let _ = child.kill();
        let _ = child.wait(); // kill 后 wait 收尸，避免测试进程留僵尸句柄
                              // 全量跑下同二进制的其他测试可能并发 spawn 姐妹子进程，选择器
                              // 「python 优先/取第一个」不保证选中本测试的 ping——钉两条真不变量：
                              // 快照能看到真实 spawn 的子进程；下探结果必是真实子进程之一（不回宿主、不虚构）
        let (_, siblings) = index
            .get(&std::process::id())
            .expect("快照应含当前测试进程");
        assert!(
            siblings.contains(&child_pid),
            "快照应看到真实 spawn 的子进程 {child_pid}"
        );
        assert_ne!(
            resolved,
            std::process::id(),
            "存在子进程时下探不得回落宿主 pid"
        );
        assert!(
            siblings.contains(&resolved),
            "下探应命中真实子进程之一，got {resolved}"
        );
    }

    /// 不可观测 pid（索引外）回落原样：宿主 pid 语义不变。
    #[test]
    #[cfg(windows)]
    fn resolve_worker_pid_falls_back_for_unknown_pid() {
        use std::collections::HashMap;
        let index: HashMap<u32, (String, Vec<u32>)> = HashMap::new();
        assert_eq!(
            super::resolve_worker_pid(u32::MAX - 1, &index),
            u32::MAX - 1
        );
    }
}
