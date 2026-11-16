// @feature: FP-0.2.内核内存治理 内存所有权与阻塞执行器策略 | @ci: rust-test
//! 内存所有权与阻塞执行器策略（跨平台共享层）。
//!
//! ## 所有权规则
//!
//! - **阻塞池按设备档/核数给初始上限**，运行期由 tokio 按需增长；空闲线程按
//!   可配 keep-alive 退休，后台按可配周期 `mi_collect` 结算 abandoned 页。
//! - **任务内临时字节/字符串走线程本地 scratch**：用完归还复用，不跨线程移交。
//! - **必须跨线程的结果由调用方拷贝/持有**：不让已死 worker 的堆结算释放。
//!
//! mimalloc 的 theap 在线程退出时把仍有块的页标记为 abandoned，跨线程释放
//! 只能走延迟队列；按实际设备资源给上限 + 定期清理，避免固定线程数把
//! 页库存放大。平台差异只在指标口径（Windows commit charge / Linux cgroup /
//! Android 单应用上限），所有权模型一致。

use std::time::Duration;

/// 阻塞线程空闲保活默认值（秒）。线程不是永久常驻：空闲即退休，退休潮由
/// 后台 `mi_collect` + `page_reclaim_on_free` 结算，避免页库存长期悬空。
pub const DEFAULT_THREAD_KEEP_ALIVE_SECS: u64 = 300;

/// 后台清理周期默认值（秒）：采样 mimalloc 统计并 `mi_collect(true)`。
pub const DEFAULT_MEM_COLLECT_SECS: u64 = 60;

/// 阻塞线程栈（KiB）。与 tokio 默认 2MiB 一致。
pub const DEFAULT_THREAD_STACK_KIB: usize = 2048;

/// 设备档（与 `agentos_core::device_profile::DeviceTier` 同词表，解耦依赖）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PoolTier {
    Phone,
    Desktop,
    Cloud,
}

/// 按档位/核数推导阻塞池初始上限（纯函数，可配置覆盖）。
///
/// 阻塞池主要承载 SQLite/文件/插件桥等 I/O，不是 CPU 并行度；核数只作为
/// "设备规模"的可得代理，真正上限由 env 覆盖。核数探测失败（0）按单核保守
/// 处理，落到各档下限。
pub fn resolve_max_threads(tier: PoolTier, cores: u32) -> usize {
    let cores = usize::try_from(cores).unwrap_or(0).max(1);
    match tier {
        PoolTier::Phone => cores.saturating_mul(2).clamp(8, 24),
        PoolTier::Desktop => cores.saturating_mul(4).clamp(32, 96),
        PoolTier::Cloud => cores.saturating_mul(4).clamp(32, 128),
    }
}

/// 全局在飞工具执行上限（异步准入闸，不是线程数）。
///
/// 工具调用是跨进程 I/O：内核只持有一个 future，等返回期间不占线程。准入闸
/// 限制的是同时执行中的工具数（含 sidecar 工具与流式 LLM 调用），用于给
/// sidecar 进程/内存一个上界，而不是把等待者变成线程。核数仅作设备规模代理，
/// env [`AGENTOS_MAX_INFLIGHT_TOOLS`] 可覆盖。
pub fn resolve_max_inflight_tools(tier: PoolTier, cores: u32) -> usize {
    let cores = usize::try_from(cores).unwrap_or(0).max(1);
    match tier {
        PoolTier::Phone => cores.saturating_mul(2).clamp(8, 32),
        PoolTier::Desktop => cores.saturating_mul(2).clamp(16, 64),
        PoolTier::Cloud => cores.saturating_mul(2).clamp(16, 96),
    }
}

/// tokio 多线程运行时阻塞池策略 + 后台清理周期 + 异步工具准入上限。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BlockingPoolConfig {
    pub max_threads: usize,
    pub keep_alive: Duration,
    pub stack_size: usize,
    pub collect_interval: Duration,
    /// 同时执行中的工具调用上限（异步信号量许可数；等待者只占 future）。
    pub max_inflight_tools: usize,
}

impl BlockingPoolConfig {
    /// 按设备档/核数解析，env 逐项覆盖；未设置/解析失败/非正值回落推导值。
    pub fn resolve(tier: PoolTier, cores: u32) -> Self {
        let max_threads = env_usize("AGENTOS_MAX_BLOCKING_THREADS")
            .filter(|n| *n > 0)
            .unwrap_or_else(|| resolve_max_threads(tier, cores));
        let keep_alive_secs = env_u64("AGENTOS_THREAD_KEEP_ALIVE_SECS")
            .filter(|n| *n > 0)
            .unwrap_or(DEFAULT_THREAD_KEEP_ALIVE_SECS);
        let stack_size = env_usize("AGENTOS_THREAD_STACK_KIB")
            .filter(|kib| *kib > 0)
            .and_then(|kib| kib.checked_mul(1024))
            .unwrap_or(DEFAULT_THREAD_STACK_KIB * 1024);
        let collect_secs = env_u64("AGENTOS_MEM_COLLECT_SECS")
            .filter(|n| *n > 0)
            .unwrap_or(DEFAULT_MEM_COLLECT_SECS);
        let max_inflight_tools = env_usize("AGENTOS_MAX_INFLIGHT_TOOLS")
            .filter(|n| *n > 0)
            .unwrap_or_else(|| resolve_max_inflight_tools(tier, cores));
        Self {
            max_threads,
            keep_alive: Duration::from_secs(keep_alive_secs),
            stack_size,
            collect_interval: Duration::from_secs(collect_secs),
            max_inflight_tools,
        }
    }
}

fn env_usize(name: &str) -> Option<usize> {
    std::env::var(name).ok()?.trim().parse().ok()
}

fn env_u64(name: &str) -> Option<u64> {
    std::env::var(name).ok()?.trim().parse().ok()
}

/// 线程本地 scratch：高频小分配按 worker 复用容量，清空后再用。
///
/// 禁止把 scratch 内的引用/缓冲区移出闭包；需要跨线程/跨 await 的结果由
/// 调用方复制到自有 `String`/`Vec`。嵌套调用不会 panic：重入时退化为临时
/// 缓冲区，不会破坏外层借用。
pub mod scratch {
    use std::cell::RefCell;

    thread_local! {
        static BYTES: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
    }

    /// 取走线程本地字节缓冲（已清空，容量随 worker 复用）。调用方负责用
    /// `put_bytes` 归还。
    pub fn take_bytes() -> Vec<u8> {
        BYTES.with(|slot| {
            let mut value = std::mem::take(&mut *slot.borrow_mut());
            value.clear();
            value
        })
    }

    /// 归还字节缓冲；只保留容量更大的那一份，避免容量回退。
    pub fn put_bytes(value: Vec<u8>) {
        BYTES.with(|slot| {
            let mut slot = slot.borrow_mut();
            if value.capacity() > slot.capacity() {
                *slot = value;
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn clear_env() {
        for name in [
            "AGENTOS_MAX_BLOCKING_THREADS",
            "AGENTOS_THREAD_KEEP_ALIVE_SECS",
            "AGENTOS_THREAD_STACK_KIB",
            "AGENTOS_MEM_COLLECT_SECS",
            "AGENTOS_MAX_INFLIGHT_TOOLS",
        ] {
            std::env::remove_var(name);
        }
    }

    #[test]
    fn adaptive_inflight_tools_follow_tier_and_cores() {
        assert_eq!(resolve_max_inflight_tools(PoolTier::Phone, 4), 8);
        assert_eq!(resolve_max_inflight_tools(PoolTier::Phone, 64), 32);
        assert_eq!(resolve_max_inflight_tools(PoolTier::Desktop, 4), 16);
        assert_eq!(resolve_max_inflight_tools(PoolTier::Desktop, 16), 32);
        assert_eq!(resolve_max_inflight_tools(PoolTier::Desktop, 64), 64);
        assert_eq!(resolve_max_inflight_tools(PoolTier::Cloud, 8), 16);
        assert_eq!(resolve_max_inflight_tools(PoolTier::Cloud, 64), 96);
        assert_eq!(resolve_max_inflight_tools(PoolTier::Desktop, 0), 16);
    }

    #[test]
    fn adaptive_threads_follow_tier_and_cores() {
        assert_eq!(resolve_max_threads(PoolTier::Phone, 8), 16);
        assert_eq!(resolve_max_threads(PoolTier::Phone, 64), 24);
        assert_eq!(resolve_max_threads(PoolTier::Desktop, 4), 32);
        assert_eq!(resolve_max_threads(PoolTier::Desktop, 16), 64);
        assert_eq!(resolve_max_threads(PoolTier::Desktop, 64), 96);
        assert_eq!(resolve_max_threads(PoolTier::Cloud, 4), 32);
        assert_eq!(resolve_max_threads(PoolTier::Cloud, 16), 64);
        assert_eq!(resolve_max_threads(PoolTier::Cloud, 64), 128);
        assert!(resolve_max_threads(PoolTier::Phone, 0) >= 8);
        assert_eq!(resolve_max_threads(PoolTier::Desktop, 0), 32);
    }

    #[test]
    fn resolve_defaults_and_env_overrides() {
        clear_env();
        let base = BlockingPoolConfig::resolve(PoolTier::Desktop, 16);
        assert_eq!(base.max_threads, 64);
        assert_eq!(base.keep_alive, Duration::from_secs(300));
        assert_eq!(base.stack_size, 2 * 1024 * 1024);
        assert_eq!(base.collect_interval, Duration::from_secs(60));
        assert_eq!(base.max_inflight_tools, 32);

        std::env::set_var("AGENTOS_MAX_BLOCKING_THREADS", "32");
        std::env::set_var("AGENTOS_THREAD_KEEP_ALIVE_SECS", "3600");
        std::env::set_var("AGENTOS_THREAD_STACK_KIB", "512");
        std::env::set_var("AGENTOS_MEM_COLLECT_SECS", "15");
        std::env::set_var("AGENTOS_MAX_INFLIGHT_TOOLS", "7");
        let cfg = BlockingPoolConfig::resolve(PoolTier::Desktop, 16);
        assert_eq!(cfg.max_threads, 32);
        assert_eq!(cfg.keep_alive, Duration::from_secs(3600));
        assert_eq!(cfg.stack_size, 512 * 1024);
        assert_eq!(cfg.collect_interval, Duration::from_secs(15));
        assert_eq!(cfg.max_inflight_tools, 7);

        std::env::set_var("AGENTOS_MAX_BLOCKING_THREADS", "0");
        std::env::set_var("AGENTOS_THREAD_KEEP_ALIVE_SECS", "not-a-number");
        std::env::set_var("AGENTOS_THREAD_STACK_KIB", "0");
        std::env::set_var("AGENTOS_MEM_COLLECT_SECS", "0");
        std::env::set_var("AGENTOS_MAX_INFLIGHT_TOOLS", "0");
        assert_eq!(BlockingPoolConfig::resolve(PoolTier::Desktop, 16), base);

        clear_env();
    }

    #[test]
    fn scratch_bytes_reuse_capacity_and_are_clear_on_take() {
        let mut first = scratch::take_bytes();
        first.reserve(4096);
        first.extend_from_slice(b"payload");
        let capacity = first.capacity();
        scratch::put_bytes(first);

        let second = scratch::take_bytes();
        assert!(second.is_empty(), "归还后取出的缓冲必须为空");
        assert!(
            second.capacity() >= capacity,
            "容量应随 worker 复用，实际 {} < {}",
            second.capacity(),
            capacity
        );
        scratch::put_bytes(second);
    }
}
