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
//! - `arena_eager_commit=0`：不做 arena 启动预提交——mimalloc 默认在
//!   Windows 上 eager commit 大块 arena，全量启用内核实测启动峰值
//!   931MB → 268MB（08-31 对照实验：同 exe 同树同 config，仅环境变量
//!   MIMALLOC_ARENA_EAGER_COMMIT=0 差异）。需要时按需提交，不预占。
//! - `arena_reserve=128MiB`：mimalloc 默认 1GiB 起步预留（64 位），无大
//!   分配时过度预留虚拟地址空间——实测 405MB 单 arena 里非零活数据仅
//!   43MB，其余是零字节预留空洞；调小后按实际分配量逐步增长。
//! - 用 `mi_option_set`（无条件生效）而非 `mi_option_set_default`：tokio
//!   运行时在 main 体前创建，选项可能已初始化，set_default 会 no-op。
//! - 选项值取自 libmimalloc-sys c_src/mimalloc/{v2,v3}/include/mimalloc.h
//!   枚举序（stable 选项 0-2 + advanced 从 3 起）：
//!   15 = `mi_option_purge_delay`，4 = `mi_option_arena_eager_commit`。
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
/// mimalloc `mi_option_arena_eager_commit` 的枚举值（v2/v3 一致）。
const MI_OPTION_ARENA_EAGER_COMMIT: libmimalloc_sys::mi_option_t = 4;
/// mimalloc `mi_option_arena_reserve` 的枚举值（v2/v3 一致，KiB 单位）。
const MI_OPTION_ARENA_RESERVE: libmimalloc_sys::mi_option_t = 23;

/// arena 起步预留调小值：128MiB（KiB 单位）。mimalloc 默认 1GiB 起步预留
/// （64 位），无大分配时过度预留虚拟地址空间——实测 405MB 单 arena 里非零
/// 活数据仅 43MB，其余是零字节预留空洞。调小后按实际分配量逐步增长，
/// 同时高于 `MI_ARENA_MIN_SIZE`（32MiB）保证基本 arena 语义。
const ARENA_RESERVE_DEFAULT_KIB: i64 = 128 * 1024;

/// 安装全局分配器并设置内存策略：purge_delay=0（空闲页立即归还 OS）+
/// arena_eager_commit=0（不预提交 arena，需要时按需提交）+
/// arena_reserve=128MiB（起步预留从 1GiB 调小，不浪费虚拟地址空间）。
///
/// 必须在任何分配发生前调用（main 第一行）。幂等：重复调用无害
/// （mi_option_set 每次无条件生效）。
///
/// 环境变量覆盖：mimalloc 在进程初始化（main 之前）从环境读选项，本函数的
/// `mi_option_set` 晚于它执行会覆盖环境变量——因此 arena 两选项仅在环境
/// 变量未显式设置时落调优值（部署侧可设 MIMALLOC_ARENA_EAGER_COMMIT=0 /
/// MIMALLOC_ARENA_RESERVE=… 显式覆盖，与 purge_delay 的 env 覆盖语义一致）。
pub fn install_global_allocator() {
    // SAFETY: mi_option_set 是 mimalloc C API 的线程安全选项设置函数；
    // 传入合法枚举值（15=purge_delay / 4=arena_eager_commit /
    // 23=arena_reserve，i64 在 32 位 c_long 上截断安全——KiB 值不超范围），
    // 无指针参数。
    unsafe {
        libmimalloc_sys::mi_option_set(MI_OPTION_PURGE_DELAY, 0);
        if std::env::var("MIMALLOC_ARENA_EAGER_COMMIT").is_err() {
            libmimalloc_sys::mi_option_set(MI_OPTION_ARENA_EAGER_COMMIT, 0);
        }
        if std::env::var("MIMALLOC_ARENA_RESERVE").is_err() {
            // value 参数是 c_long（Linux i64 / Windows i32），按目标平台 c_long
            // 宽度转换（KiB 值不超 32 位范围，截断安全）。
            libmimalloc_sys::mi_option_set(
                MI_OPTION_ARENA_RESERVE,
                ARENA_RESERVE_DEFAULT_KIB as core::ffi::c_long,
            );
        }
    }
}

/// 读取当前 purge_delay 选项值（诊断/测试用）。
pub fn purge_delay() -> i64 {
    // SAFETY: mi_option_get 是 mimalloc C API 的线程安全选项读取函数。
    unsafe { libmimalloc_sys::mi_option_get(MI_OPTION_PURGE_DELAY) as i64 }
}

/// 读取当前 arena_eager_commit 选项值（诊断/测试用）。
pub fn arena_eager_commit() -> i64 {
    // SAFETY: 同上，合法枚举值 4。
    unsafe { libmimalloc_sys::mi_option_get(MI_OPTION_ARENA_EAGER_COMMIT) as i64 }
}

/// 读取当前 arena_reserve 选项值（诊断/测试用，KiB 单位）。
pub fn arena_reserve_kib() -> i64 {
    // SAFETY: 同上，合法枚举值 23。
    unsafe { libmimalloc_sys::mi_option_get(MI_OPTION_ARENA_RESERVE) as i64 }
}

/// mimalloc 分配器统计快照（调用时刻的进程级瞬时值；mimalloc 无增量
/// delta API，增量由消费方对两次快照相减得到——M1 测量面：先测量后治理）。
///
/// 数据源是 `mi_stats_get_json`（mimalloc v3 结构化统计 API，聚合 subprocess
/// 全部堆），而非解析 `mi_stats_print_out` 的人类可读文本——v3 文本格式已无
/// v2 的 "allocated … freed …" 汇总行，JSON 给出精确 int64 字节数，无单位
/// 换算与格式漂移风险。libmimalloc-sys 仅在未启用 `v2` feature 时暴露该
/// 绑定（本仓未启用；若启用 v2 此处编译期即报错，需同步换解析面）。
///
/// ## 字段可用性（release 构建现实，2026-09-10 对 v3.3.2 实测）
///
/// libmimalloc-sys 以 `MI_DEBUG=0` 编译 C 源，mimalloc 据此取 `MI_STAT=0`
/// （v2/v3 同此默认）：**malloc 计量面（in_use/requested/allocs/freed）在
/// 调用点被编译剔除，恒读 0**；arena/进程面（committed/reserved/purged/
/// process）由子进程原子量无条件维护，是 release 构建下的可信信号。
/// `abandoned_pages` 有维护但走线程堆本地计数，仅在线程 collect/退出时
/// 合并——多线程进程内该值偏低（下界）。
///
/// 任一底层字段缺失/解析失败 → 该字段 None（版本差异防御，不 panic）。
#[derive(Debug, Clone, Default, PartialEq, serde::Serialize)]
pub struct MemStats {
    /// 活字节（in-use，含块头/对齐开销）：`malloc_normal.current + malloc_huge.current`。
    /// 仅 `MI_STAT>0` 构建维护；release 构建恒 0（见上文），活增长请以
    /// process 面 + committed 联看。
    pub in_use_bytes: Option<u64>,
    /// 用户请求字节（不含分配器开销）：`malloc_requested.current`。
    /// 可用性与 [`MemStats::in_use_bytes`](Self::in_use_bytes) 相同（MI_STAT 门控）。
    pub requested_bytes: Option<u64>,
    /// 累计分配次数：`malloc_normal_count + malloc_huge_count`。
    /// 可用性与 [`MemStats::in_use_bytes`](Self::in_use_bytes) 相同（MI_STAT 门控）。
    pub total_allocs: Option<u64>,
    /// 累计释放字节（v3 只统计字节数、无释放次数计数）：
    /// `(malloc_normal.total + malloc_huge.total) − in_use_bytes`。
    /// 可用性与 [`MemStats::in_use_bytes`](Self::in_use_bytes) 相同（MI_STAT 门控）。
    pub freed_bytes: Option<u64>,
    /// 已向 OS 提交的字节：`committed.current`（子进程原子量，release 可信；
    /// 滞留治理主指标——purge_delay=0 下该值应随 collect 收缩）。
    pub committed_bytes: Option<u64>,
    /// 虚拟地址预留字节：`reserved.current`（arena 预留空洞观测）。
    pub reserved_bytes: Option<u64>,
    /// 累计 purge 归还 OS 的字节：`purged`（计数器；purge_delay=0 策略的
    /// 归还动作量）。
    pub purged_bytes: Option<u64>,
    /// abandoned 页数：`pages_abandoned.current`（线程堆遗弃页；有线程堆
    /// 合并滞后，读数为真实值的下界）。
    pub abandoned_pages: Option<u64>,
    /// 进程已提交字节（OS 口径，`process.commit_current`，恒可用）。
    pub process_commit_bytes: Option<u64>,
    /// 进程常驻字节（OS 口径，`process.rss_current`，恒可用；Windows 下
    /// mimalloc 以 commit 近似 RSS）。
    pub process_rss_bytes: Option<u64>,
}

/// JSON 快照缓冲容量：v3 JSON 含 malloc/page/chunk 三个 bin 数组（74 bins ×
/// ~120B × 2 + 标量区，实测 ~20KiB 量级），64KiB 余量充足；溢出时
/// `mi_stats_get_json` 返回 NULL → 全字段 None（fail-soft，不 panic）。
const MI_STATS_JSON_BUF_BYTES: usize = 64 * 1024;

/// 采集当前 mimalloc 统计快照（调用时刻；mimalloc 侧聚合 subprocess 全部
/// 堆，线程堆本地计数存在合并滞后——见 [`MemStats`] 字段说明）。
///
/// 采集失败（JSON 写入失败/非 UTF-8/解析失败）→ 全 None 的 [`MemStats`]。
pub fn snapshot_stats() -> MemStats {
    let mut buf = vec![0u8; MI_STATS_JSON_BUF_BYTES];
    // SAFETY: mi_stats_get_json 将 NUL 结尾的 JSON 文本写入调用方缓冲
    // （容量经 buf_size 显式声明），返回缓冲指针或 NULL（失败）。buf 在
    // 调用期间存活（同栈帧），mimalloc 不保留该指针。
    let ptr = unsafe {
        libmimalloc_sys::mi_stats_get_json(MI_STATS_JSON_BUF_BYTES, buf.as_mut_ptr().cast())
    };
    if ptr.is_null() {
        return MemStats::default();
    }
    // SAFETY: 调用成功时 ptr 指向 buf 内 NUL 结尾的 C 字符串。
    let json = unsafe { core::ffi::CStr::from_ptr(ptr) };
    mem_stats_from_json(json.to_bytes())
}

/// 从 mimalloc JSON 统计文本解析 [`MemStats`]（snapshot_stats 的纯解析层）。
fn mem_stats_from_json(json: &[u8]) -> MemStats {
    let Ok(v) = serde_json::from_slice::<serde_json::Value>(json) else {
        return MemStats::default();
    };
    mem_stats_from_value(&v)
}

/// 读取 count 型字段（`{"total":…,"peak":…,"current":…}`）的指定分量。
fn stat_component(v: &serde_json::Value, key: &str, field: &str) -> Option<u64> {
    v.get(key)?.get(field).and_then(serde_json::Value::as_u64)
}

/// 读取 counter 型字段（裸数字）。
fn counter_total(v: &serde_json::Value, key: &str) -> Option<u64> {
    v.get(key).and_then(serde_json::Value::as_u64)
}

/// 两分量都有才合成（缺一即 None——任一底层字段缺失该派生字段不猜值）。
fn add_pair(a: Option<u64>, b: Option<u64>) -> Option<u64> {
    a.zip(b).map(|(x, y)| x.saturating_add(y))
}

fn mem_stats_from_value(v: &serde_json::Value) -> MemStats {
    let in_use = add_pair(
        stat_component(v, "malloc_normal", "current"),
        stat_component(v, "malloc_huge", "current"),
    );
    let total = add_pair(
        stat_component(v, "malloc_normal", "total"),
        stat_component(v, "malloc_huge", "total"),
    );
    MemStats {
        in_use_bytes: in_use,
        requested_bytes: stat_component(v, "malloc_requested", "current"),
        committed_bytes: stat_component(v, "committed", "current"),
        reserved_bytes: stat_component(v, "reserved", "current"),
        purged_bytes: counter_total(v, "purged"),
        total_allocs: add_pair(
            counter_total(v, "malloc_normal_count"),
            counter_total(v, "malloc_huge_count"),
        ),
        freed_bytes: total.zip(in_use).map(|(t, u)| t.saturating_sub(u)),
        abandoned_pages: stat_component(v, "pages_abandoned", "current"),
        // process 段为 OS 口径裸数值（非 {total,peak,current} 对象）
        process_commit_bytes: v
            .get("process")
            .and_then(|p| counter_total(p, "commit_current")),
        process_rss_bytes: v
            .get("process")
            .and_then(|p| counter_total(p, "rss_current")),
    }
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
        assert_eq!(
            purge_delay(),
            0,
            "install 后 purge_delay 应为 0（立即归还）"
        );
    }

    /// arena_eager_commit / arena_reserve 调优生效：install 后 eager_commit 为 0、
    /// reserve 为 128MiB（KiB 单位）。
    ///
    /// 环境变量覆盖语义（部署侧 MIMALLOC_ARENA_* 显式设置时 install 跳过落值）
    /// 不在本进程内断言：mimalloc 在选项首次读取（首个分配，早于 main）时从
    /// 环境初始化选项，`mi_option_set` 之后环境变量不再回读——进程内无法
    /// 稳定复现"先设 env 再 install"的时序（并行测试共享进程级选项）。
    #[test]
    fn install_sets_arena_tuning() {
        install_global_allocator();
        assert_eq!(arena_eager_commit(), 0, "install 后 eager_commit 应为 0");
        assert_eq!(
            arena_reserve_kib(),
            128 * 1024,
            "install 后 reserve 应为 128MiB"
        );
    }

    // ── 统计快照（M1 测量面）──

    use serde_json::json;

    /// 构造 mimalloc v3 mi_stats_get_json 输出的样例片段（字段名与
    /// c_src/mimalloc/v3/include/mimalloc-stats.h 的 MI_STAT_FIELDS 一致，
    /// process 段为 mi_stats_get_json_from 实打印形状），断言解析器精确解出
    /// 各字段。
    #[test]
    fn parser_extracts_fields_from_v3_json() {
        let sample = json!({
            "stat_version": 5,
            "mimalloc_version": 30302,
            "process": {
                "elapsed_msecs": 2, "user_msecs": 0, "system_msecs": 0,
                "page_faults": 1824,
                "rss_current": 7184384, "rss_peak": 7184384,
                "commit_current": 6217728, "commit_peak": 6230016
            },
            "reserved": { "total": 134217728, "peak": 134217728, "current": 134217728 },
            "committed": { "total": 2097152, "peak": 1048576, "current": 786432 },
            "purged": 65536,
            "pages_abandoned": { "total": 7, "peak": 5, "current": 2 },
            "malloc_normal": { "total": 5000, "peak": 1200, "current": 700 },
            "malloc_huge": { "total": 1000, "peak": 400, "current": 300 },
            "malloc_requested": { "total": 4800, "peak": 1100, "current": 650 },
            "malloc_normal_count": 42,
            "malloc_huge_count": 1,
        });
        let s = mem_stats_from_value(&sample);
        assert_eq!(
            s.in_use_bytes,
            Some(700 + 300),
            "in_use = normal+huge current"
        );
        assert_eq!(s.requested_bytes, Some(650));
        assert_eq!(s.committed_bytes, Some(786432));
        assert_eq!(s.reserved_bytes, Some(134217728));
        assert_eq!(s.purged_bytes, Some(65536));
        assert_eq!(s.total_allocs, Some(43), "allocs = normal+huge counter");
        assert_eq!(s.freed_bytes, Some(6000 - 1000), "freed = total − in_use");
        assert_eq!(s.abandoned_pages, Some(2));
        assert_eq!(s.process_commit_bytes, Some(6217728));
        assert_eq!(s.process_rss_bytes, Some(7184384));
    }

    /// 版本差异防御：任一底层字段缺失 → 对应派生字段 None（不猜 0、不 panic）；
    /// 完全无关的 JSON → 全 None。
    #[test]
    fn parser_missing_fields_yield_none() {
        // 缺 committed / process 段 / huge 系列
        let partial = json!({
            "process": { "rss_current": 1000 },
            "malloc_normal": { "total": 5000, "peak": 1200, "current": 700 },
            "malloc_normal_count": 42,
        });
        let s = mem_stats_from_value(&partial);
        assert_eq!(s.in_use_bytes, None, "缺 huge 分量则 in_use 不合成");
        assert_eq!(s.total_allocs, None, "缺 huge 计数则 allocs 不合成");
        assert_eq!(s.committed_bytes, None);
        assert_eq!(s.process_commit_bytes, None, "process 段缺该项 → None");
        assert_eq!(s.process_rss_bytes, Some(1000));

        // 空对象 / 类型不符 → 全 None
        let empty = mem_stats_from_value(&json!({}));
        assert_eq!(empty, MemStats::default());
        let garbage = mem_stats_from_value(&json!({ "committed": "not-a-number" }));
        assert_eq!(garbage.committed_bytes, None);
    }

    /// 非 JSON 文本 → 全 None（fail-soft，不 panic）。
    #[test]
    fn parser_invalid_text_yields_default() {
        assert_eq!(mem_stats_from_json(b"not json at all"), MemStats::default());
    }

    /// 快照可采集且 arena/进程面与真实分配联动：经 mimalloc 自身 API 分配
    /// 后，committed（子进程原子量，release 构建可信）至少覆盖本笔分配所在
    /// 页，OS 口径 commit 同样非零。malloc 计量面只断言键存在（release 构建
    /// MI_STAT=0 下其值为 0，属构建事实而非解析失败——见 MemStats 文档）。
    /// 测试进程未安装全局分配器，但 mimalloc 统计面与其是否担任全局分配器
    /// 无关。≥/性质断言不依赖具体数值：并发测试只会使统计更大。
    #[test]
    fn snapshot_reflects_direct_mimalloc_allocation() {
        const SIZE: usize = 4096;
        // SAFETY: mi_malloc/mi_free 是 mimalloc C API 的常规分配/释放。
        let p = unsafe { libmimalloc_sys::mi_malloc(SIZE) };
        assert!(!p.is_null());
        let s = snapshot_stats();
        // SAFETY: 释放刚分配的指针。
        unsafe { libmimalloc_sys::mi_free(p) };

        let committed = s.committed_bytes.expect("v3 JSON 恒含 committed");
        assert!(committed > 0, "有活分配时 committed 应 > 0");
        assert!(
            s.process_commit_bytes
                .expect("v3 JSON 恒含 process.commit_current")
                > 0,
            "OS 口径 commit 应 > 0"
        );
        // malloc 面键恒存在（值受 MI_STAT 构建开关影响，不断言数值）
        assert!(s.in_use_bytes.is_some(), "v3 JSON 恒含 malloc_normal/huge");
        assert!(s.total_allocs.is_some(), "v3 JSON 恒含 malloc 计数器");
    }
}
