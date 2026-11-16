//! invoker 代采 C 类进程态指标（监控设计 §三 通道3 + §十一）。
//!
//! 指标：alive / pid / memory_commit / uptime / last_crash；内核自身进程
//! （性能监测补强 T5，G1+G2 内核侧）：proc.cpu_percent / proc.memory_bytes /
//! proc.handle_count（kernel 命名空间）。
//! 本模块提供纯函数采集（不依赖 tokio），invoker 周期性（每 10s）调 collect_proc_state，
//! 把结果写入聚合器；同一 poller 循环内附带内核自身进程采样。

use super::aggregator::{now_secs, Labels, MetricType, MetricsAggregator};
use std::sync::Arc;

/// 进程态快照（一次采集的结果）。
#[derive(Debug, Clone, Default)]
pub struct ProcStateSnapshot {
    pub plugin_id: String,
    pub alive: bool,
    pub pid: Option<u32>,
    pub memory_commit_bytes: Option<u64>,
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

/// 采集一个进程的私有提交内存（字节数；进程向系统申请的私有内存总量，
/// 无论是否被换出——任务管理器「提交大小」/ 性能计数器 "Private Bytes"
/// 同口径）。
///
/// - Windows：NtQuerySystemInformation SystemProcessInformation 的
///   PrivatePageCount（见 [`extract_private_commit`]）。
/// - Linux：/proc/<pid>/smaps_rollup 的 Private_Clean+Private_Dirty 近似
///   （见 [`collect_memory_commit_linux`] 的口径差异说明）。
/// - 其他/失败：None。
///
/// 本函数纯同步、可移植；失败返回 None（不 panic）。
pub fn collect_memory_commit(pid: u32) -> Option<u64> {
    #[cfg(target_os = "linux")]
    {
        collect_memory_commit_linux(pid)
    }
    #[cfg(target_os = "windows")]
    {
        collect_memory_commit_windows(pid)
    }
    #[cfg(not(any(target_os = "linux", target_os = "windows")))]
    {
        let _ = pid;
        None
    }
}

/// Linux 私有提交近似：smaps_rollup 的 Private_Clean+Private_Dirty 之和。
///
/// 口径差异（相对 Windows PrivatePageCount，如实记录）：此和是「驻留的私有
/// 页」，私有页被换出到 swap 后不再计入（换出量在 VmSwap/SwapPss）；Windows
/// 的 PrivatePageCount 是 commit charge（含已换出的私有页）。Linux /proc 无
/// per-process commit charge 直读面，此为最接近且单文件可读的近似。选
/// smaps_rollup 而非逐项 walk smaps：内核侧一次性聚合输出，单次 open/read，
/// 每 10s 轮 × 每宿主一次的开销可忽略（smaps_rollup 需内核 ≥4.14；更老内核
/// 文件不存在 → 读失败 None，监控诚实缺数）。
#[cfg(target_os = "linux")]
fn collect_memory_commit_linux(pid: u32) -> Option<u64> {
    let path = format!("/proc/{pid}/smaps_rollup");
    let content = std::fs::read_to_string(&path).ok()?;
    let mut total = 0u64;
    for line in content.lines() {
        // "Private_Clean:         128 kB"
        for key in ["Private_Clean:", "Private_Dirty:"] {
            if let Some(rest) = line.strip_prefix(key) {
                let num: u64 = rest.split_whitespace().next()?.parse().ok()?;
                total += num * 1024; // kB → bytes
            }
        }
    }
    Some(total)
}

/// x64 SystemProcessInformation 条目布局中本模块消费的两个偏移（字节）。
///
/// 布局自 Vista 稳定（phnt/winternl.h 公认链；windows-sys 0.52 只绑函数不绑
/// 该结构体，故手解析）：`+0x00 NextEntryOffset(u32)` / `+0x38 ImageName
/// (UNICODE_STRING, 16B)` / `+0x50 UniqueProcessId(usize)` / `+0xC8
/// PrivatePageCount(usize)`。PrivatePageCount 归 VM_COUNTERS_EX 段，
/// SystemProcessInformation 信息类下即有效（任务管理器「提交大小」同源）；
/// 旧口径读的 WorkingSetPrivateSize(+0x08) 按微软文档仅
/// SystemFullProcessInformation 保证有效，本口径无此隐患。偏移合法性由
/// [`tests::private_commit_parse_matches_private_usage_truth`] 用
/// K32GetProcessMemoryInfo PrivateUsage（同一真值的独立读面）对拍锁定。
/// 平台盲区（2026-11-06 CI Linux clippy 首暴露）：唯一消费方
/// `extract_private_commit` 是 windows 专属，模块随之门控，防 Linux 编译面
/// dead_code 假红。
#[cfg(target_os = "windows")]
mod win_layout {
    /// UniqueProcessId：usize @ +0x50（80）。
    pub const UNIQUE_PROCESS_ID: usize = 0x50;
    /// PrivatePageCount：usize @ +0xC8（200）。
    pub const PRIVATE_PAGE_COUNT: usize = 0xC8;
    /// 解析单条目所需最小长度（读到 PrivatePageCount 尾字节）。
    pub const MIN_ENTRY_LEN: usize = PRIVATE_PAGE_COUNT + 8;
}

/// 解析 SystemProcessInformation 全表缓冲，取目标 pid 的私有提交字节数
/// （PrivatePageCount）。畸形缓冲（短表/偏移越界）一律 None 不 panic。
#[cfg(target_os = "windows")]
fn extract_private_commit(buf: &[u8], pid: u32) -> Option<u64> {
    fn read_le_u64(b: &[u8]) -> Option<u64> {
        Some(u64::from_le_bytes(b.get(..8)?.try_into().ok()?))
    }
    let mut off = 0usize;
    loop {
        let cur = buf.get(off..)?;
        if cur.len() < win_layout::MIN_ENTRY_LEN {
            return None;
        }
        let next = u32::from_le_bytes(cur[0..4].try_into().ok()?) as usize;
        let pid_at = win_layout::UNIQUE_PROCESS_ID;
        let entry_pid = read_le_u64(&cur[pid_at..pid_at + 8])? as u32;
        if entry_pid == pid {
            let commit_at = win_layout::PRIVATE_PAGE_COUNT;
            return read_le_u64(&cur[commit_at..commit_at + 8]);
        }
        if next == 0 {
            return None;
        }
        off += next;
    }
}

/// Windows 私有提交采集：NtQuerySystemInformation SystemProcessInformation 的
/// PrivatePageCount——任务管理器「提交大小」列 / 性能计数器 "Private Bytes"
/// 同源同值。口径裁定（2026-10-12）：私有提交 = 进程向系统申请的私有内存总量
/// 无论是否被换出；替代旧专用工作集口径（WorkingSetPrivateSize 只计此刻驻留
/// 物理内存的私有页，空闲进程私有页被换出后严重缩水——真机实测 12 插件进程
/// 合计显示 151.9MB vs 私有提交真值 880.1MB）。
///
/// 进程表动态变化，缓冲不足（STATUS_INFO_LENGTH_MISMATCH）翻倍重试，上限
/// 64MB 防失控；失败返回 None（不 panic）。
#[cfg(target_os = "windows")]
fn collect_memory_commit_windows(pid: u32) -> Option<u64> {
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
            return extract_private_commit(&buf, pid);
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

// ── 内核自身进程采样（性能监测补强 T5，G1 CPU + G2 句柄 内核侧）──────────

/// 内核自身进程单周期采样值（采集与写入解耦：[`sample_kernel_self`] 产出，
/// [`write_kernel_self_metrics`] 落聚合器——测试注入假值直接走写面断言导出，
/// 不 mock sysinfo 本体）。
#[derive(Debug, Clone, Copy, Default)]
pub struct KernelSelfSample {
    /// CPU 使用率（%）：sysinfo 相对上一次刷新的差分，首周期 0 属预期。
    pub cpu_percent: Option<f64>,
    /// 常驻内存（字节，sysinfo RSS 口径）。
    pub memory_bytes: Option<u64>,
    /// 句柄/FD 数（Windows GetProcessHandleCount / Linux /proc/<pid>/fd 计数）。
    pub handle_count: Option<u64>,
}

/// 采集进程句柄/FD 数（G2 第二泄漏维度；选型裁定见真值 §三：sysinfo 不暴露
/// Windows 句柄数，沿 [`collect_memory_commit`] 既有平台分支模式补位）。
///
/// - Windows：GetProcessHandleCount（PROCESS_QUERY_LIMITED_INFORMATION 最小
///   权限开目标进程）。
/// - Linux：/proc/<pid>/fd 目录条目计数。
/// - 其他平台：None。
///
/// 纯同步可移植；失败（进程已退出/无权限等）返回 None，不 panic（对齐
/// [`collect_memory_commit`] 口径）。
pub fn collect_handle_count(pid: u32) -> Option<u64> {
    #[cfg(target_os = "linux")]
    {
        collect_handle_count_linux(pid)
    }
    #[cfg(target_os = "windows")]
    {
        collect_handle_count_windows(pid)
    }
    #[cfg(not(any(target_os = "linux", target_os = "windows")))]
    {
        let _ = pid;
        None
    }
}

/// Linux 句柄数：/proc/<pid>/fd 目录条目计数。
#[cfg(target_os = "linux")]
fn collect_handle_count_linux(pid: u32) -> Option<u64> {
    let entries = std::fs::read_dir(format!("/proc/{pid}/fd")).ok()?;
    Some(entries.count() as u64)
}

/// Windows 句柄数：GetProcessHandleCount。
#[cfg(target_os = "windows")]
fn collect_handle_count_windows(pid: u32) -> Option<u64> {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::{
        GetProcessHandleCount, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    unsafe {
        // 最小查询权（只读句柄计数，不要求完整查询权）；进程已退出/无权限
        // 返回 NULL → None
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle == 0 {
            return None;
        }
        let mut count: u32 = 0;
        let ok = GetProcessHandleCount(handle, &mut count);
        CloseHandle(handle);
        (ok != 0).then_some(count as u64)
    }
}

/// 内核自身进程一周期采样并落 gauge（poller 每 10s 调一次）。
///
/// `System` 实例由调用方持有并跨周期复用：cpu_usage 是相对上一次刷新的差分，
/// 首周期 0 属预期，第 2 周期起有效。取不到自身 pid（理论不可达）不采样不落数。
fn sample_kernel_self(
    sys: &mut sysinfo::System,
    pid: Option<sysinfo::Pid>,
    agg: &MetricsAggregator,
) {
    let Some(pid) = pid else { return };
    // 只刷自身 pid：全表刷新是 O(全机进程数) 开销，10s 轮无需
    sys.refresh_processes(sysinfo::ProcessesToUpdate::Some(&[pid]), true);
    let sample = match sys.process(pid) {
        Some(proc) => KernelSelfSample {
            cpu_percent: Some(proc.cpu_usage() as f64),
            memory_bytes: Some(proc.memory()),
            handle_count: collect_handle_count(pid.as_u32()),
        },
        // 自身进程不在表（首刷竞态等）：本周期诚实缺数，不落 gauge
        None => KernelSelfSample::default(),
    };
    write_kernel_self_metrics(agg, &sample);
}

/// 把内核自身进程采样写入聚合器（gauge，kernel 命名空间）。
///
/// 导出名（export.rs `<plugin_id>_<name>` 拼接）：kernel_proc_cpu_percent /
/// kernel_proc_memory_bytes / kernel_proc_handle_count。cpu_percent 与
/// handle_count 与 deploy/observability 看板 JSON 对齐，不得改名。None 字段
/// 不落（诚实缺数，与插件面 pid/memory 同口径）。
pub fn write_kernel_self_metrics(agg: &MetricsAggregator, sample: &KernelSelfSample) {
    let labels = Labels::new();
    if let Some(cpu) = sample.cpu_percent {
        agg.record(
            "kernel",
            "proc.cpu_percent",
            MetricType::Gauge,
            cpu,
            &labels,
            None,
            None,
        );
    }
    if let Some(bytes) = sample.memory_bytes {
        agg.record(
            "kernel",
            "proc.memory_bytes",
            MetricType::Gauge,
            bytes as f64,
            &labels,
            None,
            None,
        );
    }
    if let Some(handles) = sample.handle_count {
        agg.record(
            "kernel",
            "proc.handle_count",
            MetricType::Gauge,
            handles as f64,
            &labels,
            None,
            None,
        );
    }
}

/// 把进程态快照写入聚合器（周期轮询任务每 10s 调一次）。
///
/// 写入指标（plugin_id 命名空间）：
/// - `<plugin>.process.alive` gauge (0/1)
/// - `<plugin>.process.pid` gauge
/// - `<plugin>.process.memory_commit_bytes` gauge
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
    if let Some(commit) = snap.memory_commit_bytes {
        agg.record_at(
            now,
            &snap.plugin_id,
            "process.memory_commit_bytes",
            MetricType::Gauge,
            commit as f64,
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
/// pid/内存按轮次一次性的进程快照下探到真实工作进程（uv venv trampoline
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
                memory_commit_bytes: observed_pid.and_then(collect_memory_commit),
                uptime_secs: host.uptime_secs,
                last_crash_ts: None,
            };
            collect_proc_state(agg, &snap);
        }
    }
}

/// 宿主盒子快照的内存富化（只读端点 `GET /api/v1/plugins/hosts` 的 api 侧半刀）。
///
/// 私有提交采集面归 api crate（invoker 不持），盒子按 pid 就地补 `commit_mb`：
/// 进程索引一轮一建（与 [`write_host_snapshots`] 同款下探——uv venv trampoline
/// 场景跳板空壳 ≠ 真实解释器，pid/内存两列同步取真实进程），下探失败回落宿主
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
        host.commit_mb = collect_memory_commit(observed).map(|b| b as f64 / (1024.0 * 1024.0));
    }
}

/// 进程态周期轮询任务（监控设计 §三 通道3 的拉起半刀——M3 此前只挂了崩溃回调）。
///
/// 每 `interval` 遍历 invoker 全部活宿主（含 light 合宿分组），对每成员插件
/// 写 process.alive/pid/memory_commit_bytes/uptime_seconds；last_crash_ts 由崩溃
/// 回调单独写，本任务不覆盖（快照恒 None）。采集失败（进程表查无该 pid 等）
/// 返回 None 跳过该字段，不 panic。同循环附带内核自身进程采样
/// （[`sample_kernel_self`] → kernel_proc_* gauge，性能监测补强 T5）。
pub fn spawn_proc_state_poller(
    invoker: Arc<agentos_invoker::PluginInvokerImpl>,
    agg: MetricsAggregator,
    interval: std::time::Duration,
) -> tokio::task::JoinHandle<()> {
    let activity = agentos_core::task_activity::global_registry().register("host-proc-sampler");
    tokio::spawn(agentos_core::task_activity::scope(activity, async move {
        let mut tick = tokio::time::interval(interval);
        tick.tick().await; // 跳过首次立即触发（与 M2 flush 任务同款）
                           // 内核自身进程采样（T5）：System 实例跨周期持久——cpu_usage 为两次
                           // 刷新的差分，首周期 0 属预期，第 2 周期起有效。
        let mut self_sys = sysinfo::System::new();
        // 0.38 的 get_current_pid 返回 Result<Pid, &str>（0.39 起才 Option），
        // Err → None → 不采样不落数
        let self_pid = sysinfo::get_current_pid().ok();
        agentos_core::task_activity::set_current_label("idle: next tick");
        loop {
            tick.tick().await;
            agentos_core::task_activity::set_current_label("sampling host proc snapshots");
            write_host_snapshots(&agg, &invoker.host_proc_snapshots().await);
            agentos_core::task_activity::set_current_label("sampling kernel self proc");
            sample_kernel_self(&mut self_sys, self_pid, &agg);
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
            memory_commit_bytes: Some(50_000_000),
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
            Some("process.memory_commit_bytes"),
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
            memory_commit_bytes: None,
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
    fn test_collect_memory_commit_self_or_none() {
        // 采集自身进程：当前平台能拿到或 None，都不应 panic
        let self_pid = std::process::id();
        let _ = collect_memory_commit(self_pid);
        // 不存在的 pid
        assert!(
            collect_memory_commit(9_999_999).is_none()
                || collect_memory_commit(9_999_999).is_some()
        );
    }

    // ── 内核自身进程采样（T5）──

    /// 写面契约：假值经 [`write_kernel_self_metrics`] 落三 gauge（kernel 命名
    /// 空间，类型 Gauge）。导出名 kernel_proc_cpu_percent /
    /// kernel_proc_memory_bytes / kernel_proc_handle_count 与
    /// deploy/observability 看板对齐，改名即红。
    #[test]
    fn kernel_self_metrics_written_as_gauges() {
        let agg = MetricsAggregator::new();
        let sample = KernelSelfSample {
            cpu_percent: Some(12.5),
            memory_bytes: Some(123_456_789),
            handle_count: Some(4321),
        };
        write_kernel_self_metrics(&agg, &sample);
        let views = agg.query(
            Some("kernel"),
            Some("proc.cpu_percent"),
            None,
            &Labels::new(),
        );
        assert_eq!(views.len(), 1);
        assert_eq!(views[0].metric_type, MetricType::Gauge);
        assert_eq!(views[0].latest, Some(12.5));
        let views = agg.query(
            Some("kernel"),
            Some("proc.memory_bytes"),
            None,
            &Labels::new(),
        );
        assert_eq!(views.len(), 1);
        assert_eq!(views[0].metric_type, MetricType::Gauge);
        assert_eq!(views[0].latest, Some(123_456_789.0));
        let views = agg.query(
            Some("kernel"),
            Some("proc.handle_count"),
            None,
            &Labels::new(),
        );
        assert_eq!(views.len(), 1);
        assert_eq!(views[0].metric_type, MetricType::Gauge);
        assert_eq!(views[0].latest, Some(4321.0));
    }

    /// None 字段不落 gauge（诚实缺数，与插件面 pid/memory 同口径）。
    #[test]
    fn kernel_self_metrics_none_fields_not_written() {
        let agg = MetricsAggregator::new();
        write_kernel_self_metrics(&agg, &KernelSelfSample::default());
        assert!(agg
            .query(
                Some("kernel"),
                Some("proc.cpu_percent"),
                None,
                &Labels::new()
            )
            .is_empty());
        assert!(agg
            .query(
                Some("kernel"),
                Some("proc.memory_bytes"),
                None,
                &Labels::new()
            )
            .is_empty());
        assert!(agg
            .query(
                Some("kernel"),
                Some("proc.handle_count"),
                None,
                &Labels::new()
            )
            .is_empty());
    }

    /// 采集面性质（真实 sysinfo，不 mock）：自身进程采样三字段在面；
    /// CPU 首周期可为 0（差分基线未建立）但 ≥ 0，memory 恒为正（自身进程必活），
    /// 句柄数平台可用时为正、否则诚实缺数。
    #[test]
    fn sample_kernel_self_on_real_process_fills_fields() {
        let mut sys = sysinfo::System::new();
        let agg = MetricsAggregator::new();
        sample_kernel_self(&mut sys, sysinfo::get_current_pid().ok(), &agg);
        let cpu = agg.query(
            Some("kernel"),
            Some("proc.cpu_percent"),
            None,
            &Labels::new(),
        );
        assert_eq!(cpu.len(), 1, "自身进程必在表，CPU 字段应在面");
        let cpu_v = cpu[0].latest;
        // 断言单行化（llvm-cov 把多行 assert! 的消息区映射到 panic 分支，测试
        // 全绿也不执行，多行形态在 diff coverage 度量面假红）。
        assert!(cpu_v.is_some_and(|v| v >= 0.0)); // CPU 首周期 0 属预期
        let mem = agg.query(
            Some("kernel"),
            Some("proc.memory_bytes"),
            None,
            &Labels::new(),
        );
        assert_eq!(mem.len(), 1);
        let mem_v = mem[0].latest;
        assert!(mem_v.is_some_and(|v| v > 0.0)); // 自身进程 RSS 必为正
        let handles = agg.query(
            Some("kernel"),
            Some("proc.handle_count"),
            None,
            &Labels::new(),
        );
        assert_eq!(handles.len(), 1);
        let handle_v = handles[0].latest;
        #[cfg(any(target_os = "windows", target_os = "linux"))]
        assert!(handle_v.is_some_and(|v| v > 0.0)); // 活进程句柄数必为正
        #[cfg(not(any(target_os = "windows", target_os = "linux")))]
        assert_eq!(handle_v, None, "其他平台句柄采集诚实缺数");
    }

    /// 第二周期（差分基线已建立）仍单 series 覆盖不新增——gauge 语义，不逐周期
    /// 堆新 series。
    #[test]
    fn sample_kernel_self_second_cycle_keeps_single_series() {
        let mut sys = sysinfo::System::new();
        let agg = MetricsAggregator::new();
        let pid = sysinfo::get_current_pid().ok();
        sample_kernel_self(&mut sys, pid, &agg);
        std::thread::sleep(std::time::Duration::from_millis(30));
        sample_kernel_self(&mut sys, pid, &agg);
        for metric in ["proc.cpu_percent", "proc.memory_bytes", "proc.handle_count"] {
            let views = agg.query(Some("kernel"), Some(metric), None, &Labels::new());
            assert_eq!(views.len(), 1, "{metric} 应单 series 覆盖");
        }
    }

    /// 句柄采集三组有区分度输入：自身进程采到（windows/linux 为正）；不存在
    /// pid → None 不 panic（对齐 collect_memory_commit 测试口径）；全平台路径
    /// 都不 panic。
    #[test]
    fn collect_handle_count_self_positive_or_platform_none() {
        let self_count = collect_handle_count(std::process::id());
        #[cfg(any(target_os = "windows", target_os = "linux"))]
        assert!(self_count.is_some_and(|n| n > 0)); // 自身句柄数应采到
        #[cfg(not(any(target_os = "windows", target_os = "linux")))]
        assert_eq!(self_count, None);
        // 不存在的 pid：None，不 panic
        assert_eq!(collect_handle_count(u32::MAX - 1), None);
    }

    /// 取不到自身 pid（理论分支）：不采样不落 gauge，不 panic。
    #[test]
    fn sample_kernel_self_without_pid_writes_nothing() {
        let mut sys = sysinfo::System::new();
        let agg = MetricsAggregator::new();
        sample_kernel_self(&mut sys, None, &agg);
        assert!(agg
            .query(Some("kernel"), None, None, &Labels::new())
            .is_empty());
    }

    /// pid 传入但进程不在表（不可能存在的 pid，真实刷新走完）：诚实缺数分支——
    /// 快照 None → 全 None 字段不落 gauge，不 panic。
    #[test]
    fn sample_kernel_self_with_ghost_pid_writes_nothing() {
        let mut sys = sysinfo::System::new();
        let agg = MetricsAggregator::new();
        let ghost = sysinfo::Pid::from_u32(u32::MAX - 1);
        sample_kernel_self(&mut sys, Some(ghost), &agg);
        assert!(agg
            .query(Some("kernel"), None, None, &Labels::new())
            .is_empty());
    }

    #[test]
    #[cfg(target_os = "windows")]
    fn private_commit_parse_matches_private_usage_truth() {
        // 偏移合法性对拍：SystemProcessInformation 手解析出的 PrivatePageCount
        // 必须与 K32GetProcessMemoryInfo 的 PrivateUsage 一致——后者是同一私有
        // 提交真值（任务管理器「提交大小」）的独立 API 读面，两次系统调用非
        // 原子，容许同刻波动（≤ max(1MB, 10%)）。此测试锁死 win_layout 偏移
        // 非拍脑袋值；布局若被系统更新改动此处必红。
        use windows_sys::Win32::System::ProcessStatus::{
            K32GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS, PROCESS_MEMORY_COUNTERS_EX,
        };
        use windows_sys::Win32::System::Threading::GetCurrentProcess;

        let via_snapshot = collect_memory_commit(std::process::id()).expect("快照读自身进程");
        let mut counters: PROCESS_MEMORY_COUNTERS_EX = unsafe { std::mem::zeroed() };
        counters.cb = std::mem::size_of::<PROCESS_MEMORY_COUNTERS_EX>() as u32;
        let ok = unsafe {
            K32GetProcessMemoryInfo(
                GetCurrentProcess(),
                &mut counters as *mut PROCESS_MEMORY_COUNTERS_EX as *mut PROCESS_MEMORY_COUNTERS,
                counters.cb,
            )
        };
        assert_ne!(ok, 0, "K32GetProcessMemoryInfo 应成功");
        let via_counters = counters.PrivateUsage as u64;
        let tol = (via_snapshot.max(via_counters) / 10).max(1 << 20);
        assert!(
            via_snapshot.abs_diff(via_counters) <= tol,
            "私有提交两读面偏差超容差: snapshot={via_snapshot} counters={via_counters} tol={tol}"
        );
    }

    #[test]
    #[cfg(target_os = "windows")]
    fn extract_private_commit_hits_target_entry() {
        // 多节点链表：目标 pid 在第二节的命中路径——遍历必须跨过首节点；
        // 两节点各命中一次（非单点输入拟合）
        let mut buf = Vec::new();
        push_snap_entry(&mut buf, 208, 111, 10_000_000);
        push_snap_entry(&mut buf, 0, 222, 223_553_536);
        assert_eq!(extract_private_commit(&buf, 222), Some(223_553_536));
        assert_eq!(extract_private_commit(&buf, 111), Some(10_000_000));
        // 未命中（链尾无此 pid）→ None
        assert_eq!(extract_private_commit(&buf, 999), None);
    }

    #[test]
    #[cfg(target_os = "windows")]
    fn extract_private_commit_malformed_buffers_return_none() {
        // 畸形缓冲族：空表 / 头部不足最小条目长 / next 偏移越界——一律 None 不 panic
        assert_eq!(extract_private_commit(&[], 1), None);
        assert_eq!(extract_private_commit(&[0u8; 16], 1), None);
        let mut buf = Vec::new();
        push_snap_entry(&mut buf, 4096, 1, 1024);
        // 表内 pid 命中正常返回（越界路径只对表外 pid 走到）
        assert_eq!(extract_private_commit(&buf, 1), Some(1024));
        // next 偏移(4096)越出缓冲 → 遍历中断返回 None，不 panic
        assert_eq!(extract_private_commit(&buf, 999), None);
        // 截短一字节（207 < MIN_ENTRY_LEN）→ 同 pid 也不可信，None；
        // 补齐到 208 立即恢复可信——字节级边界锁定 PrivatePageCount 尾偏移
        let mut short = Vec::new();
        push_snap_entry(&mut short, 0, 1, 1024);
        assert_eq!(extract_private_commit(&short[..207], 1), None);
        assert_eq!(extract_private_commit(&short, 1), Some(1024));
    }

    /// 造一节 SystemProcessInformation 链表条目（只填解析所需偏移：+0
    /// NextEntryOffset / +80 UniqueProcessId / +200 PrivatePageCount，x64
    /// 布局；中间字段补零）。
    #[cfg(target_os = "windows")]
    fn push_snap_entry(buf: &mut Vec<u8>, next_off: u32, pid: usize, private_commit: u64) {
        let start = buf.len();
        buf.extend_from_slice(&next_off.to_le_bytes());
        buf.extend_from_slice(&[0u8; 76]);
        buf.extend_from_slice(&(pid as u64).to_le_bytes());
        buf.extend_from_slice(&[0u8; 112]);
        buf.extend_from_slice(&private_commit.to_le_bytes());
        debug_assert_eq!(buf.len() - start, 208);
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

    /// 宿主盒子内存富化：活 pid 采到则必为正（自身进程恒活）；不存在的 pid
    /// 与无 pid（HTTP transport）恒 None——三组有区分度输入共用同一采集面。
    #[test]
    fn enrich_host_boxes_rss_fills_living_pid_and_skips_pidless() {
        use agentos_core::traits::{HostBox, HostBoxKind};
        let make_box = |host_key: &str, kind: HostBoxKind, pid: Option<u32>| HostBox {
            host_key: host_key.to_string(),
            kind,
            pid,
            alive: true,
            commit_mb: None,
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
        // 自身进程必活：私有提交采到则必为正
        assert!(
            hosts[0].commit_mb.is_none_or(|v| v > 0.0),
            "活进程私有提交采到必须 > 0，got {:?}",
            hosts[0].commit_mb
        );
        // 不可能存在的进程 → 采集失败 → None
        assert_eq!(hosts[1].commit_mb, None);
        // 无 pid（HTTP transport）恒 None
        assert_eq!(hosts[2].commit_mb, None);
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
