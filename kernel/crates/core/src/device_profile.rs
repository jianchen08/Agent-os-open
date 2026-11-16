//! 设备档案与合宿档位（原则 P2 运行时执法面）。
//!
//! Python 侧契约/计划面：`agentos_plugin_sdk.device_profile`（G-M1 契约
//! tests/test_mobile_gm1_contracts，plan_hosts 纯函数）；本模块是同一档位
//! 表在内核的执法面镜像——合宿装箱（invoker）按档取槽容量与装箱域，SDK 面
//! `HostPlan` 仅为同一决策的计划投影，两处漂移由各自单测锚定同一公式
//! （与 `crate::types::bounded_spawn_concurrency` 同款分工）。
//!
//! 档位表（与 SDK device_profile.py 常量一一对应）：
//! - phone：cores ≤ 8 且 memory_gb ≤ 8（边界含于手机档）；
//! - desktop：任一维度越过手机上界，或探测不可得（保守不降档）；
//! - cloud：`AGENTOS_DEVICE_TIER=cloud` 显式钉档，硬件无关。
//!
//! 取值时机：每次调用现读现算（env 读取 + 单文件 procinfo / 单次 syscall，
//! 微秒级），不做进程级缓存——调用面为启动装配装箱与监控快照，非热路径；
//! 与 `light_host_max_members` 等环境变量取值函数同款惯例，测试可注入。

/// 档位词表（与能力门禁 min_profile、SDK `device_profile.TIERS` 同词表）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeviceTier {
    /// 手机档（含 ≤8c/≤8GB 边界设备与模拟器）。
    Phone,
    /// 档面默认档：桌面/工作站，含探测不可得的保守回退。
    Desktop,
    /// 显式云档（硬件无关）。
    Cloud,
}

/// 手机档上界（P2 原则原文「≤8核且≤8GB」，边界含于手机档）。
pub const PHONE_MAX_CORES: u32 = 8;
/// 手机档内存上界（GB）。
pub const PHONE_MAX_MEMORY_GB: f64 = 8.0;
/// 显式钉档环境变量（与 SDK 侧同名同义）。
pub const TIER_ENV: &str = "AGENTOS_DEVICE_TIER";

impl DeviceTier {
    /// 档位名（日志与观测面用，小写词表形态）。
    pub fn as_str(self) -> &'static str {
        match self {
            DeviceTier::Phone => "phone",
            DeviceTier::Desktop => "desktop",
            DeviceTier::Cloud => "cloud",
        }
    }
}

/// 档位解析纯函数：显式 tier 优先（整体覆盖硬件推断）；否则按硬件推断。
///
/// - 显式值不在词表内：告警后回退自动探测（与 SDK detect_device_profile
///   容错语义一致，不阻塞启动）；
/// - 硬件维度非法（cores == 0 或 memory_gb <= 0，探测失败的降级形态）：按
///   桌面档保守处理——无法证实手机档约束就不做降档，维持现役行为。
pub fn resolve_tier_from(cores: u32, memory_gb: f64, tier_env: Option<&str>) -> DeviceTier {
    // 与 SDK detect_device_profile 同容错：strip + lower 后比词表。
    let normalized = tier_env.map(|v| v.trim().to_ascii_lowercase());
    if let Some(raw) = normalized.as_deref().filter(|v| !v.is_empty()) {
        match raw {
            "phone" => return DeviceTier::Phone,
            "desktop" => return DeviceTier::Desktop,
            "cloud" => return DeviceTier::Cloud,
            other => tracing::warn!(
                "{TIER_ENV}={other:?} 不在词表 [phone, desktop, cloud] 内，回退自动探测"
            ),
        }
    }
    if cores == 0 || memory_gb <= 0.0 {
        return DeviceTier::Desktop;
    }
    if cores <= PHONE_MAX_CORES && memory_gb <= PHONE_MAX_MEMORY_GB {
        DeviceTier::Phone
    } else {
        DeviceTier::Desktop
    }
}

/// 逻辑核数探测；不可得返回 0（调用方按桌面档保守处理）。
///
/// pub：worker 线程数按档派生（api bin boot_env）与档位探测消费同一探测，
/// 保证 `min(核数, 上界)` 的核数与档位推断不漂移。
pub fn probe_cores() -> u32 {
    std::thread::available_parallelism()
        .map(|n| n.get() as u32)
        .unwrap_or(0)
}

/// 物理内存容量探测（GB）；不可得返回 0.0（调用方按桌面档保守处理）。
///
/// - Linux（含 proot 内手机栈，/proc 由宿主内核直供）：MemTotal；
/// - Windows：GetPhysicallyInstalledSystemMemory；
/// - 其他平台：0.0（保守桌面档）。
#[cfg(target_os = "linux")]
fn probe_memory_gb() -> f64 {
    let Ok(content) = std::fs::read_to_string("/proc/meminfo") else {
        return 0.0;
    };
    for line in content.lines() {
        if let Some(rest) = line.strip_prefix("MemTotal:") {
            let kb: u64 = rest
                .split_whitespace()
                .next()
                .and_then(|v| v.parse().ok())
                .unwrap_or(0);
            if kb > 0 {
                return kb as f64 * 1024.0 / (1024.0 * 1024.0 * 1024.0);
            }
            return 0.0;
        }
    }
    0.0
}

/// Windows 物理内存探测：GetPhysicallyInstalledSystemMemory（KB 计）。
#[cfg(target_os = "windows")]
fn probe_memory_gb() -> f64 {
    // windows-sys 0.52 只绑函数签名；失败返回 0 由调用方保守降档。
    let mut kilobytes: u64 = 0;
    let ok = unsafe {
        windows_sys::Win32::System::SystemInformation::GetPhysicallyInstalledSystemMemory(
            &mut kilobytes,
        )
    };
    if ok != 0 && kilobytes > 0 {
        kilobytes as f64 * 1024.0 / (1024.0 * 1024.0 * 1024.0)
    } else {
        0.0
    }
}

#[cfg(not(any(target_os = "linux", target_os = "windows")))]
fn probe_memory_gb() -> f64 {
    0.0
}

/// 启动/装配面设备档位：环境变量显式钉档优先，否则硬件探测推断。
///
/// 探测不完整时告警（进程内只告警一次，防监控轮询刷屏）——降档保守语义见
/// [`resolve_tier_from`]。
pub fn detect_device_tier() -> DeviceTier {
    let tier_env = std::env::var(TIER_ENV).ok();
    let cores = probe_cores();
    let memory_gb = probe_memory_gb();
    if cores == 0 || memory_gb <= 0.0 {
        // 探测失败是持续性状态（平台缺面/文件缺失），只告警一次。
        static WARNED: std::sync::Once = std::sync::Once::new();
        WARNED.call_once(|| {
            tracing::warn!(
                "设备档案探测不完整（cores={cores} memory_gb={memory_gb}），档位按桌面档保守处理 | os={}",
                std::env::consts::OS
            );
        });
    }
    resolve_tier_from(cores, memory_gb, tier_env.as_deref())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phone_boundary_inclusive() {
        // 边界含于手机档（P2 原则原文 ≤8核且≤8GB），四角各有区分度。
        assert_eq!(resolve_tier_from(4, 4.0, None), DeviceTier::Phone);
        assert_eq!(resolve_tier_from(8, 8.0, None), DeviceTier::Phone);
    }

    #[test]
    fn any_dimension_overflow_is_desktop() {
        assert_eq!(resolve_tier_from(8, 16.0, None), DeviceTier::Desktop);
        assert_eq!(resolve_tier_from(16, 8.0, None), DeviceTier::Desktop);
        assert_eq!(resolve_tier_from(16, 32.0, None), DeviceTier::Desktop);
    }

    #[test]
    fn incomplete_probe_is_conservative_desktop() {
        // 探测失败（记 0）不降档：维持现役行为。
        assert_eq!(resolve_tier_from(0, 0.0, None), DeviceTier::Desktop);
        assert_eq!(resolve_tier_from(4, 0.0, None), DeviceTier::Desktop);
        assert_eq!(resolve_tier_from(0, 4.0, None), DeviceTier::Desktop);
    }

    #[test]
    fn explicit_tier_overrides_hardware() {
        // 显式档整体覆盖推断（云主机/模拟器等无法按内存判档的场景）。
        assert_eq!(
            resolve_tier_from(64, 256.0, Some("phone")),
            DeviceTier::Phone
        );
        assert_eq!(resolve_tier_from(4, 4.0, Some("cloud")), DeviceTier::Cloud);
        assert_eq!(
            resolve_tier_from(4, 4.0, Some(" desktop ")),
            DeviceTier::Desktop,
            "显式值容忍首尾空白"
        );
        assert_eq!(
            resolve_tier_from(4, 4.0, Some("")),
            DeviceTier::Phone,
            "空串视同未钉档"
        );
    }

    #[test]
    fn unknown_tier_falls_back_to_auto() {
        // 非法显式值告警回退自动探测（不阻塞、不猜档）。
        assert_eq!(resolve_tier_from(4, 4.0, Some("watch")), DeviceTier::Phone);
        assert_eq!(
            resolve_tier_from(16, 32.0, Some("watch")),
            DeviceTier::Desktop
        );
    }

    #[test]
    fn resource_monotonicity() {
        // 性质：同档位内资源升档不回退（低配不比高配更紧）。
        let low = resolve_tier_from(4, 4.0, None);
        let mid = resolve_tier_from(8, 8.0, None);
        let high = resolve_tier_from(16, 32.0, None);
        assert_eq!(low, mid);
        // phone（更紧）升到 desktop 后档位只升不降。
        assert!(matches!(
            (low, high),
            (DeviceTier::Phone, DeviceTier::Desktop)
        ));
    }

    #[test]
    fn tier_names_match_sdk_vocabulary() {
        // 词表面与 SDK device_profile.TIERS 对齐（能力门禁共用词表的前提）。
        assert_eq!(DeviceTier::Phone.as_str(), "phone");
        assert_eq!(DeviceTier::Desktop.as_str(), "desktop");
        assert_eq!(DeviceTier::Cloud.as_str(), "cloud");
    }

    #[test]
    fn host_platform_probe_positive() {
        // 真依赖：本机探测可得（Windows GlobalMemory / Linux procinfo），
        // 核数探测全平台可得——保证正常宿主不进保守降档分支。
        assert!(probe_cores() > 0, "逻辑核数探测可得");
        #[cfg(any(target_os = "linux", target_os = "windows"))]
        assert!(probe_memory_gb() > 0.0, "内存容量探测可得");
    }

    #[test]
    fn detect_tier_returns_valid_tier() {
        // 真依赖链：探测 + 解析全链在宿主平台出词表内档位。
        let tier = detect_device_tier();
        assert!(
            ["phone", "desktop", "cloud"].contains(&tier.as_str()),
            "档位必须落在 SDK 共享词表内"
        );
    }
}
