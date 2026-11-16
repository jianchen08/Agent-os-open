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
