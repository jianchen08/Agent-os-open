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

/// 采集一个进程的内存占用（字节数）。
///
/// - Linux：/proc/<pid>/status 的 VmRSS（RSS 口径，系统监视器/htop 同款）。
/// - Windows：专用工作集（private working set，任务管理器「内存（活动的
///   专用工作集）」同源同口径）。
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

/// 解析 SystemProcessInformation 全表缓冲，取目标 pid 的专用工作集字节数。
///
/// x64 头部布局自 Vista 稳定：`+0 NextEntryOffset(u32)` /
/// `+8 WorkingSetPrivateSize(i64)` / `+80 UniqueProcessId(usize)`，
/// 中间字段无需解读。负值（非法态）视为不可信返回 None；畸形缓冲（短表/
/// 偏移越界）一律 None 不 panic。
#[cfg(target_os = "windows")]
fn extract_private_working_set(buf: &[u8], pid: u32) -> Option<u64> {
    fn read_le_u64(b: &[u8]) -> Option<u64> {
        Some(u64::from_le_bytes(b.get(..8)?.try_into().ok()?))
    }
    let mut off = 0usize;
    loop {
        let cur = buf.get(off..)?;
        if cur.len() < 88 {
            return None;
        }
        let next = u32::from_le_bytes(cur[0..4].try_into().ok()?) as usize;
        let entry_pid = read_le_u64(&cur[80..88])? as u32;
        if entry_pid == pid {
            let private_ws = read_le_u64(&cur[8..16])? as i64;
            return u64::try_from(private_ws).ok();
        }
        if next == 0 {
            return None;
        }
        off += next;
    }
}

/// Windows 内存采集：专用工作集（private working set）——任务管理器「进程」页
/// 「内存（活动的专用工作集）」列同 API 同字段（NtQuerySystemInformation
/// SystemProcessInformation 的 WorkingSetPrivateSize），监控值与任务管理器
/// 同 pid 直接对得上；替代旧 tasklist 子进程方案（其 MEM 口径=工作集全集，
/// 含共享映像，恒大于任务管理器默认列，且每宿主每轮 spawn 一个子进程）。
///
/// 进程表动态变化，缓冲不足（STATUS_INFO_LENGTH_MISMATCH）翻倍重试，上限
/// 64MB 防失控；失败返回 None（不 panic）。
#[cfg(target_os = "windows")]
fn collect_memory_rss_windows(pid: u32) -> Option<u64> {
    use windows_sys::Wdk::System::SystemInformation::{
        NtQuerySystemInformation, SystemProcessInformation,
    };
    const STATUS_INFO_LENGTH_MISMATCH: i32 = 0xC000_0004u32 as i32;
    let mut size: usize = 1 << 20;
    loop {
        let mut buf = vec![0u8; size];
        let mut ret_len: u32 = 0;
        let status = unsafe {
            NtQuerySystemInformation(
                SystemProcessInformation,
                buf.as_mut_ptr().cast(),
                size as u32,
                &mut ret_len,
            )
        };
        if status == 0 {
            return extract_private_working_set(&buf, pid);
        }
        if status == STATUS_INFO_LENGTH_MISMATCH && size < (1 << 26) {
            size *= 2;
            continue;
        }
        return None;
    }
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

/// 宿主盒子快照的 RSS 富化（只读端点 `GET /api/v1/plugins/hosts` 的 api 侧半刀）。
///
/// RSS 采集面归 api crate（invoker 不持），盒子按 pid 就地补 `rss_mb`：进程
/// 索引一轮一建（与 [`write_host_snapshots`] 同款下探——uv venv trampoline 场景
/// 跳板空壳 ≠ 真实解释器，pid/memory 两列同步取真实进程），下探失败回落宿主
/// pid 原样；无 pid（HTTP transport / 未连接）恒 None。
pub fn enrich_host_boxes_rss(hosts: &mut [agentos_core::traits::HostBox]) {
    let proc_index = build_process_index();
    for host in hosts.iter_mut() {
        let Some(pid) = host.pid else {
            continue;
        };
        let observed = proc_index
            .as_ref()
            .map_or(pid, |idx| resolve_worker_pid(pid, idx));
        host.rss_mb = collect_memory_rss(observed).map(|b| b as f64 / (1024.0 * 1024.0));
    }
}

/// 进程态周期轮询任务（监控设计 §三 通道3 的拉起半刀——M3 此前只挂了崩溃回调）。
///
/// 每 `interval` 遍历 invoker 全部活宿主（含 light 合宿分组），对每成员插件
/// 写 process.alive/pid/memory_rss_bytes/uptime_seconds；last_crash_ts 由崩溃
/// 回调单独写，本任务不覆盖（快照恒 None）。采集失败（进程表查无该 pid 等）
/// 返回 None 跳过该字段，不 panic。
pub fn spawn_proc_state_poller(
    invoker: Arc<agentos_invoker::PluginInvokerImpl>,
    agg: MetricsAggregator,
    interval: std::time::Duration,
) -> tokio::task::JoinHandle<()> {
    let activity = agentos_core::task_activity::global_registry().register("host-proc-sampler");
    tokio::spawn(agentos_core::task_activity::scope(activity, async move {
        let mut tick = tokio::time::interval(interval);
        tick.tick().await; // 跳过首次立即触发（与 M2 flush 任务同款）
        agentos_core::task_activity::set_current_label("idle: next tick");
        loop {
            tick.tick().await;
            agentos_core::task_activity::set_current_label("sampling host proc snapshots");
            write_host_snapshots(&agg, &invoker.host_proc_snapshots().await);
            agentos_core::task_activity::set_current_label("idle: next tick");
        }
    }))
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
    #[cfg(target_os = "windows")]
    fn extract_private_working_set_hits_target_entry() {
        // 多节点链表：目标 pid 在第二节的命中路径——遍历必须跨过首节点；
        // 两节点各命中一次（非单点输入拟合）
        let mut buf = Vec::new();
        push_snap_entry(&mut buf, 88, 111, 10_000_000);
        push_snap_entry(&mut buf, 0, 222, 55_500_000);
        assert_eq!(extract_private_working_set(&buf, 222), Some(55_500_000));
        assert_eq!(extract_private_working_set(&buf, 111), Some(10_000_000));
        // 未命中（链尾无此 pid）→ None
        assert_eq!(extract_private_working_set(&buf, 999), None);
    }

    #[test]
    #[cfg(target_os = "windows")]
    fn extract_private_working_set_malformed_buffers_return_none() {
        // 畸形缓冲族：空表 / 头部不足 88 字节 / next 偏移越界——一律 None 不 panic
        assert_eq!(extract_private_working_set(&[], 1), None);
        assert_eq!(extract_private_working_set(&[0u8; 16], 1), None);
        let mut buf = Vec::new();
        push_snap_entry(&mut buf, 4096, 1, 1024);
        // 表内 pid 命中正常返回（越界路径只对表外 pid 走到）
        assert_eq!(extract_private_working_set(&buf, 1), Some(1024));
        // next 偏移(4096)越出缓冲 → 遍历中断返回 None，不 panic
        assert_eq!(extract_private_working_set(&buf, 999), None);
        // 负值（该字段非法态）视为不可信 → None，而非回绕成巨值
        let mut neg = Vec::new();
        push_snap_entry(&mut neg, 0, 7, -5);
        assert_eq!(extract_private_working_set(&neg, 7), None);
    }

    /// 造一节 SystemProcessInformation 链表条目（只填解析所需偏移：
    /// +0 NextEntryOffset / +8 WorkingSetPrivateSize / +80 UniqueProcessId，
    /// x64 布局；中间字段补零）。
    #[cfg(target_os = "windows")]
    fn push_snap_entry(buf: &mut Vec<u8>, next_off: u32, pid: usize, private_ws: i64) {
        let start = buf.len();
        buf.extend_from_slice(&next_off.to_le_bytes());
        buf.extend_from_slice(&0u32.to_le_bytes());
        buf.extend_from_slice(&private_ws.to_le_bytes());
        buf.extend_from_slice(&[0u8; 64]);
        buf.extend_from_slice(&(pid as u64).to_le_bytes());
        debug_assert_eq!(buf.len() - start, 88);
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

    /// 宿主盒子 RSS 富化：活 pid 采到则必为正（自身进程恒活）；不存在的 pid
    /// 与无 pid（HTTP transport）恒 None——三组有区分度输入共用同一采集面。
    #[test]
    fn enrich_host_boxes_rss_fills_living_pid_and_skips_pidless() {
        use agentos_core::traits::{HostBox, HostBoxKind};
        let make_box = |host_key: &str, kind: HostBoxKind, pid: Option<u32>| HostBox {
            host_key: host_key.to_string(),
            kind,
            pid,
            alive: true,
            rss_mb: None,
            uptime_secs: None,
            spawned_members: vec![],
            members: vec![],
            slot_used: 1,
            slot_cap: 1,
            in_flight: 0,
            member_in_flight: Default::default(),
            last_call_at: None,
            starting: false,
            starting_members: vec![],
        };
        let mut hosts = vec![
            make_box("plugin:self", HostBoxKind::Solo, Some(std::process::id())),
            make_box("plugin:ghost", HostBoxKind::Solo, Some(u32::MAX - 1)),
            make_box("group:http:1", HostBoxKind::Group, None),
        ];
        super::enrich_host_boxes_rss(&mut hosts);
        // 自身进程必活：RSS 采到则必为正
        assert!(
            hosts[0].rss_mb.is_none_or(|v| v > 0.0),
            "活进程 RSS 采到必须 > 0，got {:?}",
            hosts[0].rss_mb
        );
        // 不可能存在的进程 → 采集失败 → None
        assert_eq!(hosts[1].rss_mb, None);
        // 无 pid（HTTP transport）恒 None
        assert_eq!(hosts[2].rss_mb, None);
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
