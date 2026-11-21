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
//! - `purge_delay=500ms`（2026-10-30 改判，旧值 0）：延迟保留窗——空闲页到期
//!   才 decommit 归还 OS，窗内复用原地发生、零 decommit/recommit。旧值 0 是
//!   08-31 事故（mimalloc 默认 10ms 滞留全量峰值）时的选择，彼时无周期
//!   mi_collect 与水位自愈兜底；现兜底齐备，0 的副作用（装机实测空载
//!   purged 累计 173GB 的 churn）远超收益。详见 install_global_allocator
//!   现状契约节与 env 覆盖链。
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
/// mimalloc `mi_option_page_reclaim_on_free`（v3 枚举 35；v2 旧名
/// `abandoned_reclaim_on_free`=26）。1 = 跨 theap 认领 abandoned 页——线程退休
/// 后其他线程一 free 就能回收，配合周期 mi_collect 收敛页库存；-1 = 完全禁止，
/// 0 = 仅认领本 theap 来源的页（默认）。
const MI_OPTION_PAGE_RECLAIM_ON_FREE: libmimalloc_sys::mi_option_t = 35;

/// arena 起步预留调小值：128MiB（KiB 单位）。mimalloc 默认 1GiB 起步预留
/// （64 位），无大分配时过度预留虚拟地址空间——实测 405MB 单 arena 里非零
/// 活数据仅 43MB，其余是零字节预留空洞。调小后按实际分配量逐步增长，
/// 同时高于 `MI_ARENA_MIN_SIZE`（32MiB）保证基本 arena 语义。
const ARENA_RESERVE_DEFAULT_KIB: i64 = 128 * 1024;

/// 安装全局分配器并设置内存策略：purge_delay=延迟保留窗（空闲页到期才归还
/// OS，窗内复用不发生 decommit/recommit）+ arena_eager_commit=0（不预提交
/// arena，需要时按需提交）+ arena_reserve=128MiB（起步预留从 1GiB 调小，
/// 不浪费虚拟地址空间）+ page_reclaim_on_free=1（线程退休后的 abandoned 页
/// 可在其他线程 free 时认领）。
///
/// 必须在任何分配发生前调用（main 第一行）。幂等：重复调用无害
/// （mi_option_set 每次无条件生效）。
///
/// ## purge_delay 现状契约（2026-10-30 改判）
///
/// v3 池语义原生实现：释放时 `mi_arena_schedule_purge` 若 delay==0 就地立即
/// 清退（decommit），否则调度 `expire=now+delay`，窗口内原地复用、到期才还
/// （arena.c:2057；arena 面实际延迟 = purge_delay × arena_purge_mult，默认
/// ×10）。历史包袱：2026-10-30 前本值被钉 0——08-31 事故时无周期 mi_collect
/// （60s，D3）与水位自愈兜底，delay=10 下「静置不归还」无法闭合，故以 0 换
/// 「保证归还」，代价是大块 decommit/recommit churn（装机实测空载 purged 累计
/// 173GB）。现兜底齐备，默认 500ms（arena 面 5s 保留窗）吸收热复用；冷页
/// 由「到期 + 60s collect 强制清退访问」双保险归还，滞留不再可能无限化。
///
/// ## 环境变量覆盖
///
/// mimalloc 环境变量（MIMALLOC_PURGE_DELAY 等）在选项首次读取时生效，但
/// `mi_option_set` 晚于它执行会**覆盖**环境变量（历史文档声称 env 可覆盖
/// purge_delay，实为不生效——本版一并修正）。现役覆盖链（高→低）：
/// `AGENTOS_PURGE_DELAY_MS` → `MIMALLOC_PURGE_DELAY` → 默认 500；显式 0 =
/// 恢复「立即归还」旧语义，-1 = 从不清退（保留全部已提交页，慎用）。
/// arena 两选项仅在环境变量未显式设置时落调优值（部署侧
/// MIMALLOC_ARENA_EAGER_COMMIT=0 / MIMALLOC_ARENA_RESERVE=… 显式覆盖）。
pub fn install_global_allocator() {
    // purge 延迟保留窗：AGENTOS_PURGE_DELAY_MS > MIMALLOC_PURGE_DELAY > 500ms。
    // 解析失败按缺省处理（fail-soft，不 panic）。
    let purge_delay_ms: i64 = std::env::var("AGENTOS_PURGE_DELAY_MS")
        .ok()
        .and_then(|v| v.parse::<i64>().ok())
        .or_else(|| {
            std::env::var("MIMALLOC_PURGE_DELAY")
                .ok()
                .and_then(|v| v.parse::<i64>().ok())
        })
        .unwrap_or(500);
    // c_long 在 Windows 为 i32：钳到 [-1, i32::MAX]（mimalloc 对任意负值同为
    // 「禁清退」语义，<-1 归 -1 等价；上限防御 env 垃圾值）。
    let purge_delay_clamped: i64 = purge_delay_ms.clamp(-1, i32::MAX as i64);
    // SAFETY: mi_option_set 是 mimalloc C API 的线程安全选项设置函数；
    // 传入合法枚举值（15=purge_delay / 4=arena_eager_commit /
    // 23=arena_reserve，i64 在 32 位 c_long 上截断安全——KiB 值不超范围），
    // 无指针参数。
    unsafe {
        libmimalloc_sys::mi_option_set(
            MI_OPTION_PURGE_DELAY,
            purge_delay_clamped as core::ffi::c_long,
        );
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
        if std::env::var("MIMALLOC_PAGE_RECLAIM_ON_FREE").is_err() {
            libmimalloc_sys::mi_option_set(MI_OPTION_PAGE_RECLAIM_ON_FREE, 1);
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

/// 读取当前 page_reclaim_on_free 选项值（诊断/测试用）。
pub fn page_reclaim_on_free() -> i64 {
    // SAFETY: 同上，合法枚举值 35（v3）/ 26（v2 旧名）。
    unsafe { libmimalloc_sys::mi_option_get(MI_OPTION_PAGE_RECLAIM_ON_FREE) as i64 }
}

/// 设置 page_reclaim_on_free（诊断/压测用）：-1 = 完全禁止跨 theap 认领，
/// 0 = 仅认领本 theap 来源，1 = 跨 theap 认领。生产默认 1（install 落值）；
/// 压测用它对照"仅周期 collect"与"free 即认领"两条返还路径。
pub fn set_page_reclaim_on_free(value: i64) {
    // SAFETY: mi_option_set 线程安全；枚举值 35（v3）/26（v2 旧名），值为
    // -1/0/1 合法语义。
    unsafe {
        libmimalloc_sys::mi_option_set(MI_OPTION_PAGE_RECLAIM_ON_FREE, value as core::ffi::c_long)
    };
}

/// 主动 force collect：结算空闲页/退休线程留下的 abandoned 页并归还 OS。
///
/// 生产由内核内存维护循环周期调用（周期 = `AGENTOS_MEM_COLLECT_SECS`）；
/// 压测/诊断可直接调用观察返还效果。
pub fn collect_now() {
    // SAFETY: mi_collect 是 mimalloc C API 的线程安全入口，force=true 表示
    // 积极归还（官方文档标注「可能昂贵」——生产按分钟级周期调用）。
    unsafe { libmimalloc_sys::mi_collect(true) };
}

/// abandoned 回收探针尺寸类阶梯上界：512KiB（regular 页类量级上限；singleton
/// 大页不走 abandoned 认领路径，探针不覆盖）。
const ABANDONED_PROBE_MAX_BLOCK_BYTES: usize = 512 * 1024;
/// 每尺寸类探针分配预算（字节）：足以溢出该类在用页的空闲容量、逼出**新页
/// 分配**（v3 只在新页分配路径上认领 abandoned 页，见 [`reclaim_abandoned_now`]
/// 文档）。
const ABANDONED_PROBE_BUDGET_PER_CLASS: usize = 256 * 1024;
/// 超预算大类的最少块数：保证至少一页量级的占用。
const ABANDONED_PROBE_MIN_BLOCKS: usize = 4;

/// 探针尺寸类阶梯（纯函数）：8B 起 1.25× 步进（与 mimalloc bin 增长同构）、
/// 8 字节对齐、封顶 512KiB。返回 (块大小, 块数)——块数 = 每类预算折算
/// （`max(预算/块大小, 4)`）：小类多块以溢出在用页，大类至少 4 块。
/// 测试断性质不锚全表（tests::abandoned_probe_plan_properties）。
fn abandoned_probe_plan() -> Vec<(usize, usize)> {
    let mut plan = Vec::new();
    let mut size = 8usize;
    while size <= ABANDONED_PROBE_MAX_BLOCK_BYTES {
        let blocks = (ABANDONED_PROBE_BUDGET_PER_CLASS / size).max(ABANDONED_PROBE_MIN_BLOCKS);
        plan.push((size, blocks));
        // 1.25× 步进 + 8 字节对齐；size ≥ 8 时步进 ≥ 2，严格递增保证终止。
        size = (size + size / 4 + 7) & !7;
    }
    plan
}

/// abandoned 页回收探针（ADR 2026-11-16 retention 闸 purge 级动作；调用方
/// 随后 [`collect_now()`] force purge 收尾完成返还）。
///
/// ## 为什么 mi_collect 不够（装机实证）
///
/// mimalloc v3 的 `mi_collect(force)` 只收**调用线程的 theap**（v3 theap.c：
/// "cannot really collect process wide, just a theap"），死线程遗留的
/// abandoned 页不被触碰——装机 2026-11-16 触发时刻快照 abandoned 11065 页，
/// collect 后仅 11062（差 3 页 = 调用线程自身）。v3 的 abandoned 回收驱动
/// 只有两条：**同尺寸类新页分配优先认领**（arena.c
/// `mi_arenas_page_regular_alloc` 第 1 步）与 free 认领
/// （page_reclaim_on_free=1，install 已开）。公开绑定无强制回收入口
/// （`mi_heap_visit_abandoned_blocks` 仅遍历不释放；
/// `_mi_arenas_page_unabandon` 等内部符号未导出，libmimalloc-sys 0.1.49
/// 查证）。
///
/// 本探针用公开 API 驱动认领路径：按 [`abandoned_probe_plan`] 阶梯分配
/// （溢出在用页 → 触发新页分配 → 认领 abandoned 页）后立即释放（页转全空
/// 进 purge 候选），调用方 collect_now 完成返还。瞬时分配 ≈ 类数 × 256KiB
/// ≈ 12MB 量级、释放即还；仅在 retention 闸命中时调用（每事件至多 2 次），
/// 不在常规路径。
///
/// ## 边界（诚实声明）
///
/// 含**真活块**的 abandoned 页（属主已死、块不可达）任何分配器 API 都收不
/// 回——该残余由 retention 闸连续命中升级判据兜底（purge 后仍超即排水）；
/// 探针前后 abandoned_pages/commit 差值由调用方实录进 WARN，一次真实事件
/// 即可定性「分配器滞留可回收 vs 真泄漏/碎片」。
pub fn reclaim_abandoned_now() {
    for (size, blocks) in abandoned_probe_plan() {
        let mut ptrs: Vec<*mut core::ffi::c_void> = Vec::with_capacity(blocks);
        for _ in 0..blocks {
            // SAFETY: mi_malloc 线程安全；OOM 返回 NULL——此时内存已极端
            // 吃紧，探针自身退场（释放已分配块后返回），不 panic 不硬撑。
            let p = unsafe { libmimalloc_sys::mi_malloc(size) };
            if p.is_null() {
                for allocated in ptrs.drain(..) {
                    // SAFETY: 指针来自上方 mi_malloc 且未释放过。
                    unsafe { libmimalloc_sys::mi_free(allocated) };
                }
                return;
            }
            ptrs.push(p);
        }
        for p in ptrs {
            // SAFETY: 指针来自上方 mi_malloc 且未释放过。
            unsafe { libmimalloc_sys::mi_free(p) };
        }
    }
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
/// ## 字段可用性（构建开关决定，2026-11-10 对 v3.3.2 实测）
///
/// libmimalloc-sys 以 `MI_DEBUG=0` 编译 C 源，mimalloc 据此取 `MI_STAT=0`
/// （v2/v3 同此默认）——此时 malloc 计量面在调用点被编译剔除，恒读 0。
/// 本仓经 `kernel/.cargo/config.toml` 的 `[env] CFLAGS/CXXFLAGS` 注入
/// `-DMI_STAT=1`（2026-11-10 装机内存诊断立项）：**`in_use_bytes`
/// （malloc_normal/huge 的 current）可测**；`requested/allocs/freed` 属
/// mimalloc v3 的 `MI_STAT>1` 分支（alloc.c 实测），MI_STAT=1 下仍恒 0——
/// 要看分 bin 分布/分配次数需升到 MI_STAT=2。arena/进程面（committed/
/// reserved/purged/process）由子进程原子量无条件维护，与 MI_STAT 无关。
/// `abandoned_pages` 有维护但走线程堆本地计数，仅在线程 collect/退出时
/// 合并——多线程进程内该值偏低（下界）。
///
/// 任一底层字段缺失/解析失败 → 该字段 None（版本差异防御，不 panic）。
#[derive(Debug, Clone, Default, PartialEq, serde::Serialize)]
pub struct MemStats {
    /// 活字节（in-use，含块头/对齐开销）：`malloc_normal.current + malloc_huge.current`。
    /// `MI_STAT>0` 构建维护——本仓已开 MI_STAT=1（见上文），故可用。
    ///
    /// ## 可信性闸（2026-11-12 装机实测）
    ///
    /// `current` 是跨线程堆合并量，长跑进程里合并会使其只增不减地漂移：装机
    /// 内核运行 17h 后读到 6.4GiB，而同一刻 committed 仅 1.5GiB、RSS 0.46GiB
    /// ——活字节在物理上不可能超过已提交量。故本字段只在 `in_use <= committed`
    /// 时给出；越界即 None（不虚构活量），原始漂移量见
    /// [`Self::in_use_drift_bytes`]。
    pub in_use_bytes: Option<u64>,
    /// `in_use` 超出 `committed` 的漂移量（原样相减，仅越界时 Some）。
    /// 诊断用：非零 = mimalloc 线程堆合并已失真，同时 `in_use_bytes` 与
    /// `freed_bytes` 均为 None。
    pub in_use_drift_bytes: Option<u64>,
    /// 用户请求字节（不含分配器开销）：`malloc_requested.current`。
    /// 属 `MI_STAT>1` 面（本仓 MI_STAT=1 → 恒 0），见上文。
    pub requested_bytes: Option<u64>,
    /// 累计分配次数：`malloc_normal_count + malloc_huge_count`。
    /// 属 `MI_STAT>1` 面（本仓 MI_STAT=1 → 恒 0），见上文。
    pub total_allocs: Option<u64>,
    /// 累计释放字节（v3 只统计字节数、无释放次数计数）：
    /// `(malloc_normal.total + malloc_huge.total) − in_use_bytes`。
    /// 组成项 total 属 `MI_STAT>0` 面，故本仓可用。
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
///
/// `current` 是跨线程堆合并后的全局活量，合并顺序可使其瞬时为负（先合并
/// free、后合并分配）：2026-11-11 装机实测 `live_mb`/`abandoned_pages`
/// 在周期快照里偶发缺失，即 `as_u64` 对负值返回 None。负值按 0 钳制
/// （下界语义），不让一个瞬时负量把整段归因读成 None。
fn stat_component(v: &serde_json::Value, key: &str, field: &str) -> Option<u64> {
    let raw = v.get(key)?.get(field)?;
    if let Some(n) = raw.as_u64() {
        return Some(n);
    }
    raw.as_i64().map(|n| n.max(0) as u64)
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
    let raw_in_use = add_pair(
        stat_component(v, "malloc_normal", "current"),
        stat_component(v, "malloc_huge", "current"),
    );
    let total = add_pair(
        stat_component(v, "malloc_normal", "total"),
        stat_component(v, "malloc_huge", "total"),
    );
    let committed = stat_component(v, "committed", "current");
    // 活字节不可能超过已提交量：越界 = 跨线程堆合并漂移（装机 17h 实测
    // 6.4GiB vs committed 1.5GiB），此时原值不可当活字节用。置 None 并留
    // 漂移量，调用方（周期快照/诊断增量）据此识别 metric 失真而非内存泄漏。
    let (in_use, in_use_drift) = match (raw_in_use, committed) {
        (Some(u), Some(c)) if u > c => {
            tracing::warn!(
                in_use_bytes = u,
                committed_bytes = c,
                drift_bytes = u - c,
                "mimalloc in_use 超过 committed（线程堆合并漂移），活字节归因置空"
            );
            (None, Some(u - c))
        }
        (u, _) => (u, None),
    };
    MemStats {
        in_use_bytes: in_use,
        in_use_drift_bytes: in_use_drift,
        requested_bytes: stat_component(v, "malloc_requested", "current"),
        committed_bytes: committed,
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

    /// purge_delay 延迟保留窗生效：install 后读取为 500（窗内复用、到期归还；
    /// 旧默认 0=立即归还已于 2026-10-30 改判，见 install_global_allocator 的
    /// 现状契约节）。
    ///
    /// 行为断言（不依赖分配器身份）：mi_option_set(15, 500) → mi_option_get(15)
    /// == 500。env 覆盖链（AGENTOS_PURGE_DELAY_MS/MIMALLOC_PURGE_DELAY）不在
    /// 本进程内断言：并行测试共享进程级选项与环境，无法稳定复现"先设 env 再
    /// install"的时序。
    #[test]
    fn install_sets_purge_delay_window() {
        install_global_allocator();
        assert_eq!(
            purge_delay(),
            500,
            "install 后 purge_delay 应为 500ms（延迟保留窗）"
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

    /// 线程退休后的 abandoned 页可在其他线程 free 时认领（install 落 1）。
    #[test]
    fn install_sets_page_reclaim_on_free() {
        install_global_allocator();
        assert_eq!(
            page_reclaim_on_free(),
            1,
            "install 后 page_reclaim_on_free 应为 1（跨 theap 认领）"
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
        assert_eq!(
            s.in_use_drift_bytes, None,
            "in_use < committed 时不产生漂移标记"
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

    /// 合并漂移闸（2026-11-12 装机实测形）：`malloc_normal/huge.current` 只增
    /// 不减地漂移越过 committed 时，活字节归因置 None（物理不可能超过已提交
    /// 量），漂移量原样留档；派生的 freed_bytes 同步置 None，不用失真活量相减。
    #[test]
    fn parser_nulls_in_use_when_drift_exceeds_committed() {
        let sample = json!({
            "committed": { "total": 0, "peak": 0, "current": 1_500_000_000u64 },
            "malloc_normal": { "total": 0, "peak": 0, "current": 6_000_000_000i64 },
            "malloc_huge": { "total": 0, "peak": 0, "current": 400_000_000i64 },
        });
        let s = mem_stats_from_value(&sample);
        assert_eq!(
            s.in_use_bytes, None,
            "in_use 越过 committed 即失真，不得当活字节给出"
        );
        assert_eq!(
            s.in_use_drift_bytes,
            Some(4_900_000_000),
            "漂移量 = 原值 − committed（原样留档，供诊断）"
        );
        assert_eq!(
            s.committed_bytes,
            Some(1_500_000_000),
            "committed 本身不受影响"
        );
        assert_eq!(
            s.freed_bytes, None,
            "活量失真时 freed（total − in_use）一并置 None，不拿失真值相减"
        );
    }

    /// 闸门只对「两值都可测且越界」生效：committed 缺失（版本差异）时保持
    /// 既有透传语义——不因缺校验基准就把可测的 in_use 丢掉。
    #[test]
    fn parser_keeps_in_use_without_committed_baseline() {
        let sample = json!({
            "malloc_normal": { "total": 0, "peak": 0, "current": 700 },
            "malloc_huge": { "total": 0, "peak": 0, "current": 300 },
        });
        let s = mem_stats_from_value(&sample);
        assert_eq!(s.in_use_bytes, Some(1000));
        assert_eq!(s.in_use_drift_bytes, None);
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

    /// 瞬时负值钳制：`current` 是跨线程合并量，可为负（先合并 free、后合并
    /// 分配）——负值读 0，不读 None，否则周期快照整字段偶发缺失。
    #[test]
    fn parser_clamps_transient_negative_current() {
        let sample = json!({
            "malloc_normal": { "total": 0, "peak": 0, "current": -128 },
            "malloc_huge": { "total": 0, "peak": 0, "current": -64 },
            "pages_abandoned": { "total": 0, "peak": 0, "current": -3 },
        });
        let s = mem_stats_from_value(&sample);
        assert_eq!(s.in_use_bytes, Some(0), "负 current 钳 0 后仍合成");
        assert_eq!(s.abandoned_pages, Some(0), "负计数钳 0");
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

    /// MI_STAT 生效校验：活字节必须随真实分配前进。
    ///
    /// mimalloc release 默认 MI_STAT=0 → `malloc_normal.current` 在编译点被
    /// 剔除，活集读不出，内存诊断只剩 committed/reserved 总量（无法回答
    /// "涨在谁身上"）。构建旗标见 `kernel/.cargo/config.toml` 的
    /// `[env] CFLAGS/CXXFLAGS`（本测试即该配置的机械门禁：删了旗标转红）。
    ///
    /// 分配次数计数器（`malloc_*_count`）不在断言内：mimalloc v3 把它放在
    /// `MI_STAT>1` 分支（alloc.c 实测），MI_STAT=1 下恒 0 属构建事实。
    #[test]
    fn mi_stat_tracks_live_bytes() {
        const COUNT: usize = 256;
        const SIZE: usize = 4096;
        let before = snapshot_stats();
        // SAFETY: mi_malloc/mi_free 是 mimalloc C API 的常规分配/释放；
        // 指针只在本作用域内成对使用。
        let ptrs: Vec<*mut std::ffi::c_void> = (0..COUNT)
            .map(|_| unsafe { libmimalloc_sys::mi_malloc(SIZE) })
            .collect();
        assert!(ptrs.iter().all(|p| !p.is_null()));
        let after = snapshot_stats();
        for p in ptrs {
            unsafe { libmimalloc_sys::mi_free(p) };
        }

        let live_before = before.in_use_bytes.expect("malloc_normal/huge 键恒存在");
        let live_after = after.in_use_bytes.expect("malloc_normal/huge 键恒存在");
        // 并发测试可能同时在释放，取半量作下界（MI_STAT=0 时两侧恒 0，必红）。
        assert!(
            live_after >= live_before + (COUNT * SIZE / 2) as u64,
            "MI_STAT 未生效：活字节未前进（{live_before} → {live_after}）"
        );
    }

    // ── abandoned 回收探针（ADR 2026-11-16 retention 闸 purge 级动作）──

    /// 探针阶梯性质断言（不锚全表）：首项 = (8B, 预算全量块)；尺寸严格递增
    /// 且全部 ≤ 512KiB；块数 = max(256KiB/size, 4)（预算折算性质）、随尺寸
    /// 单调不增、下限 4；中段抽样锚点验证折算公式本身。
    #[test]
    fn abandoned_probe_plan_properties() {
        let plan = abandoned_probe_plan();
        assert!(!plan.is_empty(), "阶梯非空");
        assert_eq!(
            plan[0],
            (8, 256 * 1024 / 8),
            "首项 = 最小类 8B × 预算全量块"
        );
        let (last_size, last_blocks) = plan[plan.len() - 1];
        assert!(
            last_size <= 512 * 1024,
            "阶梯封顶 512KiB，实际尾项 {last_size}"
        );
        assert!(
            last_size > 512 * 1024 * 3 / 4,
            "尾项应落在上界带内（覆盖大类）"
        );
        assert_eq!(last_blocks, 4, "超预算大类取最少块数 4");
        for w in plan.windows(2) {
            let (s1, b1) = w[0];
            let (s2, b2) = w[1];
            assert!(s2 > s1, "尺寸严格递增: {s1} → {s2}");
            assert!(b2 <= b1, "块数随尺寸单调不增: {s1}:{b1} → {s2}:{b2}");
        }
        assert!(plan.iter().all(|&(_, b)| b >= 4), "块数下限 4");
        // 全表公式断言：每项块数 = max(预算/块大小, 4)（预算折算即规格本体）
        assert!(
            plan.iter().all(|&(s, b)| b == (256 * 1024 / s).max(4)),
            "每项块数必须按预算折算"
        );
        // 次项锚点：8B 对齐粒度下首步落到 16B（翻倍），块数 = 256KiB/16
        assert_eq!(plan[1], (16, 256 * 1024 / 16), "次项 = 16B 类");
        // 1.25× 步进性质：次项起相邻尺寸比落在 (1.0, 1.6]（小尺寸段 8 字节对齐
        // 粒度会把步进抬到 1.5 甚至翻倍——首步 8→16、16→24=1.5 均系对齐所致）
        for w in plan.windows(2).skip(1) {
            let ratio = w[1].0 as f64 / w[0].0 as f64;
            assert!(
                ratio > 1.0 && ratio <= 1.6,
                "步进应贴近 1.25×（对齐允差 ≤1.6）：{} → {}",
                w[0].0,
                w[1].0
            );
        }
    }

    /// 探针冒烟：真实分配器上跑完整阶梯 + collect 收尾，不 panic、不崩。
    ///
    /// 回收效果不在进程内断言：测试进程无线程 churn、无 abandoned 页可收，
    /// 且并行测试共享进程级统计（before/after 差值会被并发分配污染）——
    /// 效果定性靠生产 WARN 的前后差值实录（reclaim_abandoned_now 文档）。
    #[test]
    fn reclaim_abandoned_now_smoke_runs_clean() {
        install_global_allocator();
        reclaim_abandoned_now();
        collect_now();
    }
}
