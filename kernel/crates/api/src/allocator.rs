//! 全局分配器（Windows 段堆并发高水位滞留修复，ADR 2026-08-31-mimalloc-global-allocator）。
//!
//! ## 为什么需要它
//!
//! 实测（tmp_mem_sampling/peak_analysis.md）：内核跑过 chat 后热态内存不回落
//! （0 会话 ~594 MB vs 冷启动 ~314 MB，滞留 ~280 MB）。根因是 Windows 默认
//! 段堆（segment heap）在**多线程并发分配/释放**下保留高水位 committed 页
//! （线程本地缓存 + 惰性 decommit），滞留量随轮次累积、静置不归还——受控
//! 对照实验（8 线程并发构造/释放 serde_json::Value 树）实证：System 分配器
//! 滞留 26-40% 峰值且逐轮累积；mimalloc + purge_delay=0 完全回落且逐轮稳定。
//!
//! ## 决策
//!
//! - 全局分配器换 mimalloc（`#[global_allocator]`），进程内所有 Rust 分配
//!   （含 tokio/axum/serde_json/rusqlite 等）统一走 mimalloc。
//! - `purge_delay=0`：空闲页立即 purge/decommit 归还 OS（mimalloc 默认
//!   purge_delay=10ms 不主动归还，实测滞留全量峰值）。
//! - 用 `mi_option_set`（无条件生效）而非 `mi_option_set_default`：tokio
//!   运行时在 main 体前创建，选项可能已初始化，set_default 会 no-op。
//! - 选项值 15 = `mi_option_purge_delay`（v2/v3 枚举序一致，见
//!   libmimalloc-sys c_src/mimalloc/{v2,v3}/include/mimalloc.h）。
//!
//! ## 环境变量覆盖
//!
//! mimalloc 环境变量（MIMALLOC_PURGE_DELAY 等）在选项首次读取时生效，
//! 优先级高于本模块的 `mi_option_set`（mimalloc 语义：env 先于代码设置）。
//! 部署侧可用 `MIMALLOC_PURGE_DELAY=-1` 显式关闭归还（如需要保留内存池
//! 提升分配吞吐的场景）。

/// 进程全局分配器：mimalloc。
///
/// 仅 agentos-kernel 二进制（agentos-api crate 的 bin）安装；库消费者
/// （测试/其他 bin）不安装，避免测试进程分配器被替换引入噪音。
#[global_allocator]
static GLOBAL_ALLOC: mimalloc::MiMalloc = mimalloc::MiMalloc;

/// mimalloc `mi_option_purge_delay` 的枚举值（v2/v3 一致）。
const MI_OPTION_PURGE_DELAY: libmimalloc_sys::mi_option_t = 15;

/// 安装全局分配器并设置 purge_delay=0（空闲页立即归还 OS）。
///
/// 必须在任何分配发生前调用（main 第一行）。幂等：重复调用无害
/// （mi_option_set 每次无条件生效）。
pub fn install_global_allocator() {
    // SAFETY: mi_option_set 是 mimalloc C API 的线程安全选项设置函数；
    // 传入合法枚举值 15（purge_delay），无指针参数。
    unsafe {
        libmimalloc_sys::mi_option_set(MI_OPTION_PURGE_DELAY, 0);
    }
}

/// 读取当前 purge_delay 选项值（诊断/测试用）。
pub fn purge_delay() -> i64 {
    // SAFETY: mi_option_get 是 mimalloc C API 的线程安全选项读取函数。
    unsafe { libmimalloc_sys::mi_option_get(MI_OPTION_PURGE_DELAY) as i64 }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::alloc::{GlobalAlloc, Layout};

    /// 分配器冒烟测试：全局分配器安装后基本分配/释放可用。
    ///
    /// 不直接断言 mimalloc 生效（进程内无法自证分配器身份），只保证
    /// `#[global_allocator]` 安装不破坏基本分配路径（回归保护：若有人误删
    /// global_allocator 或引入冲突，本测试在分配时即失败）。
    #[test]
    fn global_allocator_basic_alloc_works() {
        let layout = Layout::from_size_align(1024, 8).unwrap();
        // SAFETY: 合法 layout，分配后立即释放，无泄漏。
        let ptr = unsafe { GLOBAL_ALLOC.alloc(layout) };
        assert!(!ptr.is_null(), "mimalloc 分配不应返回 null");
        // SAFETY: 释放刚分配的指针（layout 匹配）。
        unsafe { GLOBAL_ALLOC.dealloc(ptr, layout) };
    }

    /// purge_delay 选项设置生效：install 后读取为 0（默认 10）。
    ///
    /// 行为断言（不依赖分配器身份）：mi_option_set(15, 0) → mi_option_get(15) == 0。
    /// 测试进程未安装全局分配器（库消费者不安装），但选项 API 与分配器身份无关，
    /// 直接可测。
    #[test]
    fn install_sets_purge_delay_zero() {
        install_global_allocator();
        assert_eq!(purge_delay(), 0, "install 后 purge_delay 应为 0（立即归还）");
    }
}
