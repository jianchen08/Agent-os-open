// @feature: FP-0.2.可观测性 全家进程 RSS 观测 | @ci: rust-test
//! 全家进程 RSS 观测（family watch）。
//!
//! 内核 mimalloc 快照（[`crate::allocator`]）只覆盖自身堆；electron 壳（父
//! 进程）与 python sidecar（直接子进程）的内存时序此前无内核侧数据源，进程
//! 死后不可验尸。本模块经 Win32 直读全家 WorkingSet（任务管理器同源口径），
//! 与 mimalloc 堆快照同 tick 输出，进程级归因与堆剖析共用同一时间戳。
//! 非 Windows 平台返回空快照（字段全空，日志行保持形状不变）。

/// 单个家族成员的 RSS 样本。
#[derive(Debug, Clone)]
pub struct ProcRss {
    pub pid: u32,
    pub name: String,
    pub rss_mb: u64,
}

/// 一次全家采样：父进程（装机形态=electron 壳）+ 全部直接子进程（sidecar）。
#[derive(Debug, Clone, Default)]
pub struct FamilySnapshot {
    /// 父进程 RSS；无父（如手动裸启内核）或读取失败为 None。
    pub parent_rss_mb: Option<u64>,
    pub parent_name: Option<String>,
    /// 直接子进程 RSS，按 RSS 降序。
    pub sidecars: Vec<ProcRss>,
}

impl FamilySnapshot {
    pub fn sidecars_total_mb(&self) -> u64 {
        self.sidecars.iter().map(|p| p.rss_mb).sum()
    }

    pub fn sidecars_max_mb(&self) -> Option<u64> {
        self.sidecars.iter().map(|p| p.rss_mb).max()
    }
}

/// 采样一次全家（自身堆面由 mimalloc 行覆盖，此处只采父+直接子进程）。
/// 进程在枚举与打开之间退出、或无权限打开（AccessDenied）→ 该成员跳过
/// （fail-soft，不影响其余成员与调用方）。
#[cfg(windows)]
pub fn capture() -> FamilySnapshot {
    let my_pid = std::process::id();
    let entries = process_entries();
    let Some(self_entry) = entries.iter().find(|(pid, _, _)| *pid == my_pid) else {
        return FamilySnapshot::default();
    };
    let mut snapshot = FamilySnapshot::default();
    if let Some((pid, _, name)) = entries.iter().find(|(pid, _, _)| *pid == self_entry.1) {
        snapshot.parent_rss_mb = rss_mb_of_pid(*pid);
        snapshot.parent_name = Some(name.clone());
    }
    let mut sidecars: Vec<ProcRss> = entries
        .iter()
        .filter(|(_, parent, _)| *parent == my_pid)
        .filter_map(|(pid, _, name)| {
            rss_mb_of_pid(*pid).map(|rss_mb| ProcRss {
                pid: *pid,
                name: name.clone(),
                rss_mb,
            })
        })
        .collect();
    sidecars.sort_by(|a, b| b.rss_mb.cmp(&a.rss_mb));
    snapshot.sidecars = sidecars;
    snapshot
}

#[cfg(not(windows))]
pub fn capture() -> FamilySnapshot {
    FamilySnapshot::default()
}

#[cfg(windows)]
fn process_entries() -> Vec<(u32, u32, String)> {
    use std::mem;

    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
        TH32CS_SNAPPROCESS,
    };

    let mut out = Vec::new();
    unsafe {
        let handle = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if handle == INVALID_HANDLE_VALUE {
            return out;
        }
        let mut entry: PROCESSENTRY32W = mem::zeroed();
        entry.dwSize = mem::size_of::<PROCESSENTRY32W>() as u32;
        if Process32FirstW(handle, &mut entry) != 0 {
            loop {
                let name_len = entry
                    .szExeFile
                    .iter()
                    .position(|&c| c == 0)
                    .unwrap_or(entry.szExeFile.len());
                out.push((
                    entry.th32ProcessID,
                    entry.th32ParentProcessID,
                    String::from_utf16_lossy(&entry.szExeFile[..name_len]),
                ));
                if Process32NextW(handle, &mut entry) == 0 {
                    break;
                }
            }
        }
        CloseHandle(handle);
    }
    out
}

#[cfg(windows)]
fn rss_mb_of_pid(pid: u32) -> Option<u64> {
    use std::mem;

    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::ProcessStatus::{
        GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS,
    };
    use windows_sys::Win32::System::Threading::{OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION};

    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle == 0 {
            return None;
        }
        let mut counters: PROCESS_MEMORY_COUNTERS = mem::zeroed();
        counters.cb = mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32;
        let ok = GetProcessMemoryInfo(handle, &mut counters, counters.cb);
        CloseHandle(handle);
        if ok == 0 {
            return None;
        }
        Some((counters.WorkingSetSize / (1024 * 1024)) as u64)
    }
}

/// 本进程之外的内核实例样本（孤儿观测载荷，main.rs mem 维护循环 WARN 消费）。
#[derive(Debug, Clone, PartialEq)]
pub struct ForeignKernelInstance {
    pub pid: u32,
    /// 可执行文件名（枚举快照原值）。
    pub exe: String,
    /// 进程创建时刻（UTC RFC3339；OpenProcess/GetProcessTimes 失败为 None）。
    pub started_at: Option<String>,
    /// 父进程 pid（枚举快照内不可见 = 父已退或系统进程，None）。
    pub parent_pid: Option<u32>,
}

/// 内核可执行文件名判定（纯函数，枚举与单测同一口径）：剥 `.exe` 后缀 +
/// 大小写不敏感等于 "agentos-kernel"。
pub fn is_kernel_exe_name(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    let stem = lower.strip_suffix(".exe").unwrap_or(&lower);
    stem == "agentos-kernel"
}

/// 纯判定（进程枚举可 mock，单测面）：从进程快照 `(pid, parent_pid, exe_name)`
/// 列表筛出本进程之外的 agentos-kernel 实例，父 pid 就近查快照。
///
/// 消费方 = 孤儿内核观测（run 生命周期防线④，ADR
/// 2026-11-17-run-lifecycle-defense-lines）：非空 = 本进程外还有第二个内核
/// （任务侧裸拉/残留形态）。**只观测不处置**——进程管理不属观测面（误杀
/// 在用实例裁定禁止；scratch 内核是合法测试车道）。
pub fn foreign_kernel_instances(
    self_pid: u32,
    entries: &[(u32, u32, String)],
) -> Vec<ForeignKernelInstance> {
    entries
        .iter()
        .filter(|(pid, _, name)| *pid != self_pid && is_kernel_exe_name(name))
        .map(|(pid, parent, name)| ForeignKernelInstance {
            pid: *pid,
            exe: name.clone(),
            started_at: None,
            parent_pid: entries
                .iter()
                .find(|(other_pid, _, _)| other_pid == parent)
                .map(|(found_pid, _, _)| *found_pid),
        })
        .collect()
}

/// 采样本进程外的内核实例（孤儿观测，main.rs mem 维护循环每拍消费）。
/// 启动时刻经 GetProcessTimes 补齐（打开失败留 None，fail-soft）；
/// 非 Windows 平台恒空（与 [`capture`] 平台面一致）。
#[cfg(windows)]
pub fn capture_foreign_kernels() -> Vec<ForeignKernelInstance> {
    let entries = process_entries();
    let mut out = foreign_kernel_instances(std::process::id(), &entries);
    for instance in &mut out {
        instance.started_at = process_start_time_rfc3339(instance.pid);
    }
    out
}

#[cfg(not(windows))]
pub fn capture_foreign_kernels() -> Vec<ForeignKernelInstance> {
    Vec::new()
}

/// 进程创建时刻（UTC RFC3339；FILETIME 100ns 自 1601-01-01 → SystemTime）。
/// 打开失败/查询失败返回 None（fail-soft，不拖垮观测面）。
#[cfg(windows)]
fn process_start_time_rfc3339(pid: u32) -> Option<String> {
    use std::mem;

    use windows_sys::Win32::Foundation::{CloseHandle, FILETIME};
    use windows_sys::Win32::System::Threading::{
        GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };

    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle == 0 {
            return None;
        }
        let mut creation: FILETIME = mem::zeroed();
        let mut exit: FILETIME = mem::zeroed();
        let mut kernel: FILETIME = mem::zeroed();
        let mut user: FILETIME = mem::zeroed();
        let ok = GetProcessTimes(handle, &mut creation, &mut exit, &mut kernel, &mut user);
        CloseHandle(handle);
        if ok == 0 {
            return None;
        }
        let filetime_100ns =
            ((creation.dwHighDateTime as u64) << 32) | creation.dwLowDateTime as u64;
        // 1601-01-01 → 1970-01-01 的 100ns 间隔（Windows epoch → Unix epoch）。
        const EPOCH_DIFF_100NS: u64 = 11_644_473_600_000_000;
        let unix_100ns = filetime_100ns.saturating_sub(EPOCH_DIFF_100NS);
        let secs = unix_100ns / 10_000_000;
        chrono::DateTime::from_timestamp(secs as i64, 0)
            .map(|utc| utc.format("%Y-%m-%dT%H:%M:%SZ").to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn summary_math_over_sidecar_list() {
        let snapshot = FamilySnapshot {
            parent_rss_mb: Some(245),
            parent_name: Some("灵汐助手.exe".into()),
            sidecars: vec![
                ProcRss {
                    pid: 1,
                    name: "python.exe".into(),
                    rss_mb: 462,
                },
                ProcRss {
                    pid: 2,
                    name: "python.exe".into(),
                    rss_mb: 75,
                },
            ],
        };
        assert_eq!(snapshot.sidecars_total_mb(), 537);
        assert_eq!(snapshot.sidecars_max_mb(), Some(462));
        assert_eq!(FamilySnapshot::default().sidecars_max_mb(), None);
        assert_eq!(FamilySnapshot::default().sidecars_total_mb(), 0);
    }

    // ── 孤儿内核观测（run 生命周期防线④，mock 进程枚举）──

    #[test]
    fn kernel_exe_name_matches_case_insensitive_and_strips_exe() {
        assert!(is_kernel_exe_name("agentos-kernel.exe"));
        assert!(is_kernel_exe_name("agentos-kernel"));
        assert!(is_kernel_exe_name("AGENTOS-KERNEL.EXE"));
        assert!(!is_kernel_exe_name("agentos-kernel2.exe"));
        assert!(!is_kernel_exe_name("灵汐助手.exe"));
        assert!(!is_kernel_exe_name("python.exe"));
        assert!(!is_kernel_exe_name(""));
    }

    #[test]
    fn foreign_kernel_detection_with_mocked_entries() {
        let entries = vec![
            (100u32, 50u32, "agentos-kernel.exe".to_string()), // self
            (200u32, 100u32, "agentos-kernel.exe".to_string()), // 裸拉内核（父=self）
            (300u32, 999u32, "agentos-kernel.exe".to_string()), // 孤儿（父已退）
            (400u32, 100u32, "python.exe".to_string()),        // sidecar，非内核
            (500u32, 100u32, "agentos-kernel2.exe".to_string()), // 同名异件，非内核
        ];
        let foreign = foreign_kernel_instances(100, &entries);
        assert_eq!(foreign.len(), 2, "self 排除 + 非内核名排除后剩两实例");
        let pids: Vec<u32> = foreign.iter().map(|f| f.pid).collect();
        assert_eq!(pids, vec![200, 300]);
        // 父 pid 就近查快照：200 的父=self(100) 在场；300 的父=999 不在 → None。
        assert_eq!(foreign[0].parent_pid, Some(100));
        assert_eq!(foreign[1].parent_pid, None);
        assert_eq!(foreign[0].exe, "agentos-kernel.exe");
        // 采集时序面（started_at）由平台采集补齐，纯判定面恒 None 占位。
        assert_eq!(foreign[0].started_at, None);
    }

    #[test]
    fn foreign_kernel_none_when_only_self() {
        let entries = vec![(100u32, 0u32, "agentos-kernel.exe".to_string())];
        assert!(foreign_kernel_instances(100, &entries).is_empty());
        assert!(foreign_kernel_instances(1, &[]).is_empty());
    }

    #[cfg(windows)]
    #[test]
    fn capture_foreign_kernels_smoke_shape() {
        // 真实进程面冒烟：本测试进程外的内核实例一般不存在（CI/dev 环境偶有
        // 并行构建内核除外），只断言枚举链路不炸、形状合法。
        let foreign = capture_foreign_kernels();
        for instance in &foreign {
            assert!(is_kernel_exe_name(&instance.exe));
            assert!(instance.parent_pid != Some(std::process::id()));
        }
    }

    #[cfg(windows)]
    #[test]
    fn capture_smoke_enumerates_without_panicking() {
        // 真实进程面冒烟：枚举+OpenProcess 链路不炸、形状正确（测试宿主
        // 父进程存在与否均合法，故只断言字段类型面而非具体值）。
        let snapshot = capture();
        if let Some(name) = &snapshot.parent_name {
            assert!(!name.is_empty());
        }
        for proc in &snapshot.sidecars {
            assert!(proc.rss_mb > 0, "枚举到的活进程 WorkingSet 必大于 0");
        }
    }
}
