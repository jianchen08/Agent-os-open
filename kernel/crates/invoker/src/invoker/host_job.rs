// @feature: FP-0.2.内核内存治理 G1b per-host Job Object 硬限 | @ci: rust-test
//! 宿主进程内存 containment（ADR 2026-10-19-memory-governor-and-process-pooling
//! §4 D4 第二刀）：宿主子进程 spawn 成功后 Assign 进 per-host Job Object——
//! `JOB_OBJECT_LIMIT_PROCESS_MEMORY` 硬限 + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
//! 整树陪葬（内核无论怎么死，宿主树随之终结，孤儿结构性归零——与 hindsight 侧
//! python Job 同语义，先例 plugins/shared/hindsight_memory/server.py）。
//!
//! 契约（本模块与调用面的分工边界）：
//! 1. **越限行为 = 宿主进程分配失败自然死亡 → 既有崩溃回调路径接管**：
//!    notify_crash / respawn / SpawnBreaker 熔断风暴全部现成；本刀只做
//!    containment，不做回调通知、不新增恢复路径。
//! 2. **Job 句柄随宿主客户端记录同生命周期**：spawn 成功登记进
//!    `host_jobs` 表，驱逐/kill 随记录释放——Drop = CloseHandle =
//!    kill-on-close 兜底整树（含一切漏杀路径）；内核进程退出时 OS 关闭全部
//!    句柄，同一原语兜底孤儿。驱逐即刻释放的取舍：被驱逐宿主已判死（崩溃/
//!    过期/显式卸载），若仍有在飞调用持客户端读锁（BUG-81 宽限窗），
//!    kill-on-close 会先于 kill_on_drop 终结残留树——containment 语义下
//!    「判死即终」优先；调用面由 with_transparent_recovery 的 respawn+重试
//!    一次兜底。
//! 3. **env 未设/0/非法 = 完全不挂 Job**（选「不挂」而非「挂 Job 不限内存」
//!    ——少一个内核对象，语义单一：显式正值才启用，默认零行为变化），与
//!    2026-09-10 水位启用裁定同构。
//! 4. **非 Windows 平台 containment 本体不接线**：Job Object 是 Windows 原语
//!    （Linux 部署侧对应 systemd MemoryMax，ADR §3.5，部署侧承载）；守卫退
//!    化为例位占位照常发放，登记/释放面全平台同构，非 Windows 侧零行为差异。

/// 解析 `AGENTOS_HOST_MEM_LIMIT_MB`（纯函数，单测面）：未设/空/非数字/0/负数
/// → `None`（不启用）；正整数 → `Some(MB)`（显式正值才启用）。
fn resolve_host_mem_limit_mb(raw: Option<&str>) -> Option<u64> {
    raw.and_then(|s| s.trim().parse::<u64>().ok())
        .filter(|&v| v >= 1)
}

/// env 现值（spawn 时刻读取；纯解析见 [`resolve_host_mem_limit_mb`]）。
pub(super) fn host_mem_limit_mb_from_env() -> Option<u64> {
    resolve_host_mem_limit_mb(std::env::var("AGENTOS_HOST_MEM_LIMIT_MB").ok().as_deref())
}

#[cfg(windows)]
use windows_sys::Win32::Foundation::CloseHandle;

/// per-host Job 句柄 RAII 守卫：Drop = CloseHandle，**最后一个句柄关闭即触发
/// KILL_ON_JOB_CLOSE 整树终止**——这是兜底语义的正身，不是副作用。句柄以
/// isize 存储（windows-sys 0.52 的 HANDLE 即 isize）：裸指针默认 !Send/!Sync，
/// isize 化使守卫可直接进 invoker 的 Send+Sync 状态面；非 Windows 平台为例位
/// 占位，调用面（登记/释放）全平台同构。
pub(super) struct HostJobGuard {
    /// Job 内核句柄；0 = 未持有（Drop 跳过 CloseHandle，测试登记面用）。
    #[cfg(windows)]
    job: isize,
}

impl HostJobGuard {
    /// 句柄非零（测试断言面：守卫持有内核对象；非 Windows 例位恒 false）。
    #[cfg(test)]
    pub(super) fn is_open(&self) -> bool {
        #[cfg(windows)]
        {
            self.job != 0
        }
        #[cfg(not(windows))]
        {
            false
        }
    }
}

#[cfg(windows)]
impl Drop for HostJobGuard {
    fn drop(&mut self) {
        if self.job != 0 {
            unsafe { CloseHandle(self.job) };
        }
    }
}

#[cfg(windows)]
mod imp {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    };
    use windows_sys::Win32::System::Threading::{
        OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
    };

    use super::HostJobGuard;

    /// 把 pid 指向的进程 Assign 进带内存硬限 + kill-on-close 的 per-host Job。
    /// 任何一步失败即 Err（已创建的 Job 句柄就地关闭——此刻树内无进程，
    /// kill-on-close 无目标，纯防句柄泄漏）。
    ///
    /// 越限即进程 commit 失败自然死亡（超限不弹杀，是后续分配无内存可用），
    /// 死亡经 stdio 断连暴露给既有判死/恢复路径（契约见模块注释第 1 条）。
    pub(super) fn assign_host_job(pid: u32, limit_mb: u64) -> Result<HostJobGuard, String> {
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job == 0 {
                return Err(format!(
                    "CreateJobObjectW failed: {}",
                    std::io::Error::last_os_error()
                ));
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags =
                JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            info.ProcessMemoryLimit = limit_mb.saturating_mul(1024 * 1024) as usize;
            if SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            ) == 0
            {
                CloseHandle(job);
                return Err(format!(
                    "SetInformationJobObject failed: {}",
                    std::io::Error::last_os_error()
                ));
            }
            // Assign 权限要求 PROCESS_SET_QUOTA | PROCESS_TERMINATE（MSDN）；
            // Child 句柄尚在（McpClient 持有），pid 不会被回收复用，无竞态。
            let proc = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
            if proc == 0 {
                CloseHandle(job);
                return Err(format!(
                    "OpenProcess({pid}) failed: {}",
                    std::io::Error::last_os_error()
                ));
            }
            let assigned = AssignProcessToJobObject(job, proc);
            CloseHandle(proc);
            if assigned == 0 {
                CloseHandle(job);
                return Err(format!(
                    "AssignProcessToJobObject({pid}) failed: {}",
                    std::io::Error::last_os_error()
                ));
            }
            Ok(HostJobGuard { job })
        }
    }

    /// spawn 成功后的挂载点（Windows 实面，best-effort）：Assign 失败仅 warn
    /// 留痕返回 None——containment 缺席可观测，不阻断 spawn 主路径。
    pub(super) fn contain(host_key: &str, pid: u32, limit_mb: u64) -> Option<HostJobGuard> {
        match assign_host_job(pid, limit_mb) {
            Ok(guard) => {
                tracing::info!(
                    target: "host-job",
                    host = %host_key,
                    pid,
                    limit_mb,
                    "宿主已挂 per-host Job（PROCESS_MEMORY 硬限 + kill-on-close）"
                );
                Some(guard)
            }
            Err(e) => {
                tracing::warn!(
                    target: "host-job",
                    host = %host_key,
                    pid,
                    limit_mb,
                    error = %e,
                    "挂 per-host Job 失败（containment 缺席，宿主不受内存硬限保护）"
                );
                None
            }
        }
    }
}

#[cfg(not(windows))]
mod imp {
    use super::HostJobGuard;

    /// 非 Windows 实面：Job Object 本体不接线（debug 留痕——部署侧 systemd
    /// MemoryMax 承载，属预期态而非告警态，不打 warn 刷屏），但**守卫照常
    /// 发放**：例位占位（无内核对象、Drop 无操作），使登记/释放面全平台同构
    /// ——同一套生命周期契约与单测在两侧等价执行，非 Windows 侧零行为差异。
    pub(super) fn contain(host_key: &str, _pid: u32, limit_mb: u64) -> Option<HostJobGuard> {
        tracing::debug!(
            target: "host-job",
            host = %host_key,
            limit_mb,
            "非 Windows 平台 Job Object 不接线（systemd MemoryMax 部署侧承载），containment 跳过"
        );
        Some(HostJobGuard {})
    }
}

/// spawn 成功后的 containment 挂载点（平台统一入口）：限未启用 → None 不建
/// Job 不碰进程（默认零行为变化）；pid 缺失（HTTP 运输无本地子进程）→ None
/// + warn 留痕；其余交平台实面 [`imp::contain`]。
pub(super) fn contain_spawned_host(
    host_key: &str,
    pid: Option<u32>,
    limit_mb: Option<u64>,
) -> Option<HostJobGuard> {
    let limit_mb = limit_mb?;
    let Some(pid) = pid else {
        tracing::warn!(
            target: "host-job",
            host = %host_key,
            "内存限已启用但宿主无本地子进程（HTTP 运输），跳过挂 Job"
        );
        return None;
    };
    imp::contain(host_key, pid, limit_mb)
}

/// 登记面测试用守卫：零句柄（Drop 跳过 CloseHandle）/非 Windows 例位，
/// 不持有内核对象。供 invoker 级登记面测试构造守卫值。
#[cfg(test)]
pub(super) fn test_guard() -> HostJobGuard {
    HostJobGuard {
        #[cfg(windows)]
        job: 0,
    }
}

impl super::PluginInvokerImpl {
    /// spawn 成功：Job 守卫随客户端记录登记。同宿主键覆盖写——respawn 时旧
    /// 守卫 drop = 旧 Job 关闭 = 旧树陪葬，与驱逐路径的显式 kill 正交兜底。
    pub(super) fn register_host_job(&self, host_key: &str, guard: HostJobGuard) {
        self.host_jobs.write().insert(host_key.to_string(), guard);
    }

    /// 驱逐/kill：随记录释放（drop = CloseHandle = kill-on-close 兜底整树，
    /// 语义取舍见模块注释第 2 条）。无条目时幂等 no-op。
    pub(super) fn release_host_job(&self, host_key: &str) {
        self.host_jobs.write().remove(host_key);
    }

    /// 全量 drain（停机/全量废弃）：随 `mcp_clients` 清空一并释放。
    pub(super) fn clear_host_jobs(&self) {
        self.host_jobs.write().clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mem_limit_parses_default_off_valid_and_invalid_fallback() {
        // 默认关：未设 / 空 / 纯空白
        assert_eq!(resolve_host_mem_limit_mb(None), None);
        assert_eq!(resolve_host_mem_limit_mb(Some("")), None);
        assert_eq!(resolve_host_mem_limit_mb(Some("   ")), None);
        // 合法：显式正值才启用（容忍首尾空白；1 为下界）
        assert_eq!(resolve_host_mem_limit_mb(Some("512")), Some(512));
        assert_eq!(resolve_host_mem_limit_mb(Some(" 1024 ")), Some(1024));
        assert_eq!(resolve_host_mem_limit_mb(Some("1")), Some(1));
        // 非法 / 0 / 负数 / 小数 / 溢出 → 回落不启用（与 spawn_breaker 阈值解析同构）
        assert_eq!(resolve_host_mem_limit_mb(Some("0")), None);
        assert_eq!(resolve_host_mem_limit_mb(Some("abc")), None);
        assert_eq!(resolve_host_mem_limit_mb(Some("-2")), None);
        assert_eq!(resolve_host_mem_limit_mb(Some("1.5")), None);
        assert_eq!(
            resolve_host_mem_limit_mb(Some("99999999999999999999999")),
            None
        );
    }

    #[test]
    fn contain_spawned_host_noop_when_limit_unset() {
        // 限未启用：不建 Job、不碰 pid（None/Some 均短路）——默认零行为变化
        assert!(contain_spawned_host("plugin:x", None, None).is_none());
        assert!(contain_spawned_host("plugin:x", Some(12345), None).is_none());
    }

    #[test]
    fn contain_spawned_host_skips_pidless_transport() {
        // HTTP 运输无本地子进程：跳过挂 Job（warn 留痕，不 panic 不 Err）
        assert!(contain_spawned_host("plugin:x", None, Some(512)).is_none());
    }

    #[cfg(not(windows))]
    #[test]
    fn contain_spawned_host_yields_noop_guard_on_non_windows() {
        // 非 Windows：Job Object 本体不接线，但守卫照常发放（例位占位）——
        // 登记面全平台同构，is_open 恒 false 标记无内核对象
        let guard = contain_spawned_host("plugin:x", Some(4321), Some(512))
            .expect("非 Windows 平台 assign 恒成功（no-op 守卫）");
        assert!(!guard.is_open());
    }

    #[cfg(windows)]
    #[test]
    fn host_job_assign_then_guard_drop_kills_process() {
        // 真实子进程行为测试：assign 成功且进程存活；守卫 drop（CloseHandle）
        // 触发 kill-on-close，轮询断言进程终止。cmd/ping 为 Windows 恒在件，
        // 不依赖 python/外部工具。
        let mut child = std::process::Command::new("cmd")
            .args(["/c", "ping -n 30 127.0.0.1 > NUL"])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .expect("spawn cmd ping");
        let pid = child.id();
        let guard = imp::assign_host_job(pid, 4096).expect("assign host job");
        assert!(guard.is_open(), "守卫应持有效 Job 句柄");
        assert!(
            child.try_wait().unwrap().is_none(),
            "assign 不应终止宿主进程"
        );
        drop(guard); // kill-on-close：整树随句柄关闭陪葬
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
        loop {
            if child.try_wait().unwrap().is_some() {
                break;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "kill-on-close 未在 5s 内终止宿主进程"
            );
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
    }

    #[cfg(windows)]
    #[test]
    fn host_job_assign_failure_returns_err_without_guard() {
        // Assign 失败路径（pid 无对应进程）：Err 收场，句柄不外泄。
        // pid 0 是系统空闲进程，OpenProcess(SET_QUOTA) 必被拒绝。
        let result = imp::assign_host_job(0, 4096);
        assert!(result.is_err(), "pid 0 不可 Assign，应报错");
    }
}
