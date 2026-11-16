// @feature: FP-0.2.内核内存治理 内存治理执行面(G1a Job Object/G3b 自适应清扫/D5 预算闸) | @ci: rust-test
//! AgentOS 0.2 内核二进制入口。
//!
//! 启动 Axum HTTP/WebSocket API 服务器，提供 /health、/api/v1/* 端点和 /ws WebSocket。
//!
//! 集成插件系统：
//! 1. 扫描 plugins/shared/ 目录发现插件 manifest
//! 2. 将工具能力注册到 CapabilityRegistry
//! 3. 初始化管道引擎（AdrEngineImpl）
//! 4. 将所有组件注入 AppState
//!
//! 环境变量：
//! - AGENTOS_KERNEL_PORT：监听端口（默认 9100）
//! - AGENTOS_BIND：监听地址（默认 127.0.0.1；显式设 0.0.0.0 开启外网可达，
//!   AGENTOS_KERNEL_HOST 为过渡期别名，弃用中）
//! - AGENTOS_ADMIN_PASSWORD：内置 admin 口令（未设置时首启生成随机口令并打印
//!   一次；对已存在的 admin 设置不同口令即重置）
//! - AGENTOS_TOKEN_SECRET：token 签名密钥（显式配置最优先；未设置时持久化于
//!   用户空间 data/token_secret.key，首启生成、跨重启复用；默认不随机）
//! - AGENTOS_TOKEN_SECRET_EPHEMERAL：置 1 显式选择一次性密钥（重启即全量
//!   token 失效，强安全场景由部署者自行启用）
//! - AGENTOS_PLUGINS_DIR：内置插件根目录（默认 plugins/shared）
//! - AGENTOS_USER_PLUGINS_DIR：用户插件根目录（默认 OS 标准目录 agentos/plugins）
//! - AGENTOS_MAX_BLOCKING_THREADS：tokio 阻塞池上限（默认 512 = tokio 原生默认）
//! - AGENTOS_WORKER_THREADS：tokio worker 线程数（默认按设备档派生，原则 P2：
//!   手机档 min(核数,4) 收窄，桌面/云 = 核数即 tokio 默认）
//! - AGENTOS_THREAD_STACK_KIB：线程栈大小 KiB（默认 2048 = tokio 默认 2MiB）
//! - AGENTOS_MEM_WATERMARK_MB：水位自愈阈值 MB（**默认关闭**——2026-09-10
//!   用户裁定水位重启不得默认触发；显式正值才启用排空重启自愈）
//! - AGENTOS_MEM_WATERMARK_MIN：水位需持续的分钟数（启用时默认 10）
//! - AGENTOS_MEM_HARD_LIMIT_MB：进程内存硬限 MB（**默认关闭**——未设置/0/
//!   非法值不挂载，显式正值才启用；2026-09-10「显式正值才启用」裁定同语义）。
//!   Windows 经 Job Object PROCESS_MEMORY 给进程 commit 上硬顶（超限=分配
//!   失败）；已设时水位排水阈值取 min(水位阈值(如设), 95%×硬限)，优雅排空
//!   （exit 75）先于硬熔断
//! - AGENTOS_COLLECT_FAST_SECS：自适应清扫节奏（ADR 2026-10-19 D3 修订）——
//!   WARN 阈值触发期间 mi_collect 收紧到的间隔秒数（默认 10；显式 0/非法值=
//!   禁用收紧，恒常规节奏）。收紧只影响清扫频率，绝不触发重启（区别于水位/硬限）
//! - AGENTOS_MEM_BUDGET_MB：内存预算闸 MB（**默认关闭**——未设置/0/非法值
//!   不启用，显式正值才启用；2026-09-10「显式正值才启用」裁定同构，ADR
//!   2026-10-19 D5 闸面）。启用后宿主 spawn 前准入预检：当前内核 Private
//!   Bytes + 新宿主预估提交 > 预算 → 拒绝 spawn（load shedding，不入退避
//!   记账不触发熔断，预算回落后自动恢复）
//! - AGENTOS_HOST_EST_COMMIT_MB：新宿主预估提交 MB（默认 512；0/非法值回落
//!   默认，预估是配置项不是开关；仅预算闸启用时被消费）
//! - AGENTOS_HOST_POOL_KEEPWARM：宿主保活池全局配额（**默认 0=池关闭=旧行为**
//!   ——显式正值才启用，2026-09-10 裁定同构；ADR 2026-10-19 D1/B6）。启用后
//!   空闲回收扫描对最近使用且配额内的宿主免杀（LRU 排名，全局统一不分组），
//!   以常驻 N 个空闲宿主进程换风暴归零（每宿主 ~200-600MB commit，预算账
//!   显式化）
//! - AGENTOS_HOST_POOL_TTL_SECS：池内宿主最长保活时长秒（默认 900；0/非法值
//!   回落默认——硬顶不提供关闭语义）。按进程年龄判定（spawn 起算，防长跑慢
//!   性膨胀），超时照旧回收，与 max_requests 双保险
//! - AGENTOS_HOST_POOL_MAX_REQUESTS：宿主累计请求数轮换上限（**默认 0=关闭
//!   轮换**；gunicorn max_requests 同型）。>0 时超限（严格大于）宿主下次空闲
//!   标记轮换候选——保活豁免不再生效照旧回收，由池补位或照旧 respawn
//! - AGENTOS_TRACE_RETENTION_DAYS：traces 保留天数（默认 90；0 = 禁用清扫；
//!   超出上限按上限钳制，解析失败回落默认）——traces 过期行与孤儿 blob 由
//!   保留清扫周期回收（启动清一次 + 每 6h）

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use agentos_api::metrics::{Labels, MetricType};
use agentos_api::{
    load_pipeline_config, load_step_library, routes::AppState, start_server,
    validate_no_name_conflicts, KernelCapabilityRouter,
};
use agentos_core::traits::{CapabilityRegistry, ToolDescriptor};
use agentos_core::types::{ToolCategory, ToolSource};
use agentos_invoker::PluginInvokerImpl;
use agentos_plugin_loader::{CapabilityRegistryImpl, NativePluginLoader};
use tracing::{error, info, warn};
use tracing_subscriber::{fmt, prelude::*};

mod boot_env;
mod plugin_boot;

use agentos_core::device_profile::{detect_device_tier, probe_cores, DeviceTier};
use agentos_memory::{BlockingPoolConfig, PoolTier};
use boot_env::{
    attach_self_job_memory_limit, budget_exceeded, collect_fast_triggered,
    effective_sample_threshold_mb, migrate_plaintext_passwords, resolve_bind_host,
    resolve_collect_fast_secs, resolve_host_est_commit_mb, resolve_log_dir, resolve_mem_budget_mb,
    resolve_mem_hard_limit_mb, resolve_user_plugins_dir, resolve_watermark_config,
    resolve_worker_threads, sample_private_bytes, seed_admin_user, watermark_breach,
    PressureAlertEdge,
};
use plugin_boot::{
    build_plugin_loader, collect_boot_plugin_roots, commit_seed_reconciliation_at_boot,
    init_user_repo_at_boot, reconcile_mode_seeds_at_boot, warn_legacy_db_not_migrated,
};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // 全局分配器（mimalloc + purge_delay=0）：必须在任何分配前安装。
    // Windows 段堆并发高水位滞留修复（ADR 2026-08-31-mimalloc-global-allocator）。
    agentos_api::allocator::install_global_allocator();

    // 线程治理：#[tokio::main] 宏暴露不了 max_blocking_threads / thread_stack_size，
    // 手动构建等价 multi-thread runtime（enable_all = 宏默认）。阻塞池上限按设备档
    // /核数自适应（agentos-memory 单一策略面），空闲线程按可配 keep-alive 退休、
    // 后台按可配周期清理；worker_threads 按设备档派生（原则 P2 资源参数自适应）：
    // 手机档 min(核数,4) 收窄；桌面/云返回 None 不设 = 核数，同为宏默认。
    // env 钉值 AGENTOS_WORKER_THREADS 优先。
    let pool = BlockingPoolConfig::resolve(
        match detect_device_tier() {
            DeviceTier::Phone => PoolTier::Phone,
            DeviceTier::Desktop => PoolTier::Desktop,
            DeviceTier::Cloud => PoolTier::Cloud,
        },
        probe_cores(),
    );
    let mut builder = tokio::runtime::Builder::new_multi_thread();
    builder
        .enable_all()
        .max_blocking_threads(pool.max_threads)
        .thread_stack_size(pool.stack_size)
        .thread_keep_alive(pool.keep_alive);
    if let Some(workers) = resolve_worker_threads() {
        builder.worker_threads(workers);
    }
    let runtime = builder.build()?;
    runtime.block_on(async_main(pool))
}

#[allow(clippy::too_many_lines)] // 存量吸收(2026-10-12 棘轮): 主进程启动装配面,拆分已立项(文件规模台账)
async fn async_main(pool: BlockingPoolConfig) -> Result<(), Box<dyn std::error::Error>> {
    // 日志契约（2026-09-15 单日志面）：文件层是唯一常驻日志——tracing-appender
    // daily 轮转写 logs/kernel.log.YYYY-MM-DD，保留上限 30 份（D3：daily 轮转
    // 无限累积会让 logs/ 目录随运行时长无界增长）。轮转口径为纯 UTC（BUG-71
    // 裁决 2026-09-23）：文件名 = 写入时刻的 UTC 日期，轮转边界 = UTC 午夜 =
    // 本地 08:00（UTC+8）；本地日 X 的日志行因此横跨两个文件，且本地午夜
    // 00:00 时最新文件仍叫 X-1——按本地日期找文件会扑空，外部判活统一走
    // <日志目录>/kernel.liveness（见 log_liveness）。
    // stdout 不再双写（supervisor 侧重定向已退役：OS 级追加重定向是无界
    // 增长面，内核只持有无路径的 fd、无法自轮替）；仅当文件层构建失败
    // （只读盘/无目录权限）时才挂 stdout 层降级——此时它是唯一诊断面。
    // 日志目录解析（ADR 2026-09-28-venv-user-space-provisioning）：dev（CWD
    // 可写）落相对 `logs`（现状零变化）；装机态 CWD=只读安装目录（electron
    // spawn cwd=resources/kernel，建目录/写 IO 必败且静默）→ 落用户空间
    // `<USER_ROOT>/logs`——与 venv 同批用户空间化的同源惯例。
    let log_dir = resolve_log_dir(
        agentos_api::venv_provision::is_dir_writable(std::path::Path::new("logs")),
        agentos_core::user_space::user_root().as_deref(),
    );
    let (file_layer, log_guard) = match tracing_appender::rolling::RollingFileAppender::builder()
        .rotation(tracing_appender::rolling::Rotation::DAILY)
        .filename_prefix("kernel.log")
        // D3：保留上限 30 份——daily 轮转无限累积会让 logs/ 目录随运行时长
        // 无界增长（长期驻留内核的磁盘治理面）。
        .max_log_files(30)
        .build(&log_dir)
    {
        Ok(appender) => {
            // 活性面（BUG-71 裁决）：成功写打点、写失败显式告警（上游 worker 对
            // IO 错误全静默）、持续静默超阈值报警并停止刷新 kernel.liveness。
            // 告警面 stderr 在装机部署被丢弃（electron stdio=ignore），标记文件
            // 是外部判活权威面。
            let liveness = agentos_api::log_liveness::LogLiveness::new();
            let watched = agentos_api::log_liveness::LivenessWriter::new(
                appender,
                liveness.clone(),
                Arc::new(|msg: String| eprintln!("{msg}")),
                agentos_api::log_liveness::ERR_REPORT_RATE_LIMIT,
            );
            let (writer, guard) = tracing_appender::non_blocking(watched);
            // 丢帧监测接线（判别面）：lossy 丢弃制下通道满/worker 停滞对调用方
            // 不可见，看门狗周期读丢帧计数，前进即告警 + 证据落盘（log_liveness）。
            let drop_counter = writer.error_counter();
            agentos_api::log_liveness::spawn_silence_watchdog(
                liveness,
                &log_dir,
                agentos_api::log_liveness::WatchdogConfig::production(),
                Arc::new(|msg: String| eprintln!("{msg}")),
                Some(drop_counter),
            );
            (
                Some(
                    fmt::layer()
                        .with_target(false)
                        .with_ansi(false)
                        .with_writer(writer),
                ),
                Some(guard),
            )
        }
        Err(e) => {
            eprintln!("[boot] 日志文件轮转初始化失败（仅 stdout 日志可用）: {e}");
            (None, None)
        }
    };
    // guard 必须活到进程退出（提前丢 guard = 缓冲日志丢失），绑定在 async_main 帧
    let _log_guard = log_guard;

    // stdout 降级层：仅文件层缺席时挂载（None = 不挂载）。正常运行全部日志
    // 只走文件层；boot eprintln 与 panic 不走 tracing，不经此层。
    let stdout_fallback_layer = if file_layer.is_none() {
        Some(fmt::layer().with_target(false))
    } else {
        None
    };

    tracing_subscriber::registry()
        .with(file_layer)
        .with(stdout_fallback_layer)
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    // panic 痕迹落盘（观测面，2026-10-10 装机 B81 实证补面）：panic 默认只写
    // stderr，electron pipe（eacce1c35）只覆盖 electron 存活期；孤儿内核
    // （跨装机文件替换存活）pipe 无读者即零痕迹。本 hook 把 panic 消息+位置+
    // backtrace 追加到 <log_dir>/kernel-panic.log，与 stdio 接线状态无关；
    // 默认 stderr 输出经链式前置 hook 保留。
    agentos_api::panic_log::install(&log_dir);

    // token 密钥初始化（用户裁定 2026-10-22：默认持久化、随机 opt-in，ADR
    // 2026-10-22-token-secret-persistent-default）：解析序 env 显式 > ephemeral
    // opt-in > 用户空间 data/token_secret.key 复用/首启生成。挂在日志初始化后
    // （解析结论落启动日志）、任何子系统/token 签发之前（登录面先于 HTTP serve）。
    agentos_http::auth::init_token_secret();

    // 内存硬限挂载（ADR 2026-10-19 D4，G1a 第一刀）：显式 AGENTOS_MEM_HARD_LIMIT_MB
    // 正值才挂 Job Object（默认关闭，与水位 2026-09-10「显式正值才启用」裁定
    // 同语义）。Windows 经 JOB_OBJECT_LIMIT_PROCESS_MEMORY 给进程 commit 上硬
    // 顶——超限 = OS 拒绝提交（分配失败，进程暴毙）；因此「先排水后熔断」
    // 由水位采样承载：已设硬限时判定阈值取 min(水位阈值(如设), 95%×硬限)
    // （effective_sample_threshold_mb），让 exit 75 优雅排空先于硬熔断；
    // 60s 采样窗内的陡涨仍可能先触顶——采样粒度边界如实记录于此注释。挂载
    // 点选在日志初始化后、任何子系统（DB/插件/server）之前——启动早期即有
    // 硬顶；分配器安装（main 早期）到此处仅日志装配，量级可忽略。挂载失败
    // 只告警不阻断启动（硬限是护栏不是前置条件），排水阈值仍按显式配置生效。
    let mem_hard_limit_mb = resolve_mem_hard_limit_mb();
    // 自适应清扫节奏（D3 修订）：此处解析一份，自证行与采样循环同源共用，
    // 不重复读 env。
    let collect_fast_secs = resolve_collect_fast_secs();
    // 内存预算闸（D5 第一刀）：此处解析供自证行；闸装配在 invoker 构建后
    // （采样闭包注入，见下）。
    let mem_budget_mb = resolve_mem_budget_mb();
    let mut hard_limit_attached = false;
    if let Some(mb) = mem_hard_limit_mb {
        match attach_self_job_memory_limit(mb.saturating_mul(1024 * 1024)) {
            Ok(()) => {
                hard_limit_attached = true;
                info!(
                    target: "agentos-kernel",
                    hard_limit_mb = mb,
                    "内存硬限已挂载（Job Object PROCESS_MEMORY）；水位排水阈值压至硬限之下"
                );
            }
            Err(e) => warn!(
                target: "agentos-kernel",
                error = %e,
                "内存硬限挂载失败：本进程无 Job 硬顶（水位排水阈值仍按显式配置生效）"
            ),
        }
    }

    // 内存策略开机自证：压测/重启核对时无需猜生效值（策略唯一事实源
    // agentos-memory；allocator 选项读回自 mimalloc 本体）。
    info!(
        target: "agentos-kernel",
        max_blocking_threads = pool.max_threads,
        thread_keep_alive_secs = pool.keep_alive.as_secs(),
        thread_stack_kib = pool.stack_size / 1024,
        mem_collect_secs = pool.collect_interval.as_secs(),
        max_inflight_tools = pool.max_inflight_tools,
        purge_delay = agentos_api::allocator::purge_delay(),
        arena_eager_commit = agentos_api::allocator::arena_eager_commit(),
        page_reclaim_on_free = agentos_api::allocator::page_reclaim_on_free(),
        // 硬限生效值：0 = 未启用（与 env「0=关」口径一致）；attached=false
        // 含未启用与挂载失败两种（后者另有告警行可辨）。
        mem_hard_limit_mb = mem_hard_limit_mb.unwrap_or(0),
        mem_hard_limit_attached = hard_limit_attached,
        // 自适应清扫节奏（D3 修订）：WARN 触发期收紧到的秒数；0 = 禁用收紧
        // （恒常规节奏）。收紧只影响 mi_collect 频率，绝不触发重启。
        collect_fast_secs = collect_fast_secs,
        // 内存预算闸（D5 第一刀）：0 = 未启用。压力告警通知面为结构性常开
        // （WARN 阈值边沿触发即进通知中心，无独立开关），字段供装机核验
        // 该面已接线（D5 执法序列第一级：告警不再是仅内核日志）。
        mem_budget_mb = mem_budget_mb.unwrap_or(0),
        mem_pressure_notify = true,
        // 宿主保活池（D1/B6+B8）：keepwarm 0 = 池关闭（旧行为）；ttl/轮换
        // 仅在池启用时有消费面。逐轮消费与启动期读同值（env 运行期不热变）。
        // 经 `agentos_invoker::invoker::` 直引（venv_provision/plugin_watcher
        // 同款先例路径）——lib.rs 纯 re-export 行无可度量行，不进 diff-cov 面。
        host_pool_keepwarm = agentos_invoker::invoker::pool_keepwarm_from_env(),
        host_pool_ttl_secs = agentos_invoker::invoker::pool_ttl_secs_from_env(),
        host_pool_max_requests = agentos_invoker::invoker::pool_max_requests_from_env(),
        "内存策略生效值"
    );

    let host = resolve_bind_host();
    let port: u16 = std::env::var("AGENTOS_KERNEL_PORT")
        .unwrap_or_else(|_| "9100".into())
        .parse()
        .unwrap_or(9100);

    let addr: SocketAddr = format!("{host}:{port}").parse()?;
    if !addr.ip().is_loopback() {
        warn!(
            target: "agentos-kernel",
            bind = %addr,
            "内核绑定在非回环地址：HTTP/WS API 外网可达。仅限可信网络部署"
        );
    }

    info!(target: "agentos-kernel", "========================================");
    info!(target: "agentos-kernel", "  AgentOS 0.2 内核启动");
    info!(target: "agentos-kernel", "  监听地址: http://{}", addr);
    info!(target: "agentos-kernel", "  健康检查: http://{}/health", addr);
    info!(target: "agentos-kernel", "  WebSocket: ws://{}/ws", addr);
    info!(target: "agentos-kernel", "  Schema: http://{}/api/v1/schema", addr);
    info!(target: "agentos-kernel", "========================================");

    // 设备档位启动观测（原则 P2 合宿数=f(设备档案)）：合宿装箱（invoker）按
    // 此档取槽容量与装箱域——手机全成员装入 1-2 宿主 / 桌面收敛 4 宿主。
    // 显式钉档 AGENTOS_DEVICE_TIER 优先，未钉档按 cores+内存自动探测。
    info!(
        target: "agentos-kernel",
        "Device tier: {}（合宿装箱按档取值；钉档环境变量 AGENTOS_DEVICE_TIER）",
        agentos_core::device_profile::detect_device_tier().as_str()
    );

    // 资源参数档观测（原则 P2：tokio worker、限流阈值同律自适应）：紧随档位行
    // 汇总本进程按档生效的资源参数。worker=None 表示未设 worker_threads（桌面/
    // 云档 tokio 默认 = 核数）；各参数均可用对应环境变量钉值覆盖档位派生。
    let event_bus_rate = agentos_session::event_bus::resolved_rate_limit_config();
    info!(
        target: "agentos-kernel",
        "Resource params: tokio_workers={} / event_bus_rate={}/s burst={}（钉值环境变量 AGENTOS_WORKER_THREADS、AGENTOS_EVENT_BUS_REFILL_PER_SEC、AGENTOS_EVENT_BUS_BURST）",
        resolve_worker_threads()
            .map(|n| n.to_string())
            .unwrap_or_else(|| "default(cores)".to_string()),
        event_bus_rate.refill_per_sec,
        event_bus_rate.burst
    );

    // ── 插件系统初始化 ──

    // 确定插件目录
    let plugins_dir = std::env::var("AGENTOS_PLUGINS_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            // 默认路径：工作区根目录下的 plugins/shared/
            // 尝试从 CARGO_MANIFEST_DIR 向上查找，否则用相对路径
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .and_then(|p| p.parent())
                .and_then(|p| p.parent())
                .map(|root| root.join("plugins/shared"))
                .unwrap_or_else(|| PathBuf::from("plugins/shared"))
        });

    info!(
        target: "agentos-kernel",
        "Plugin directory (builtin root): {}",
        plugins_dir.display()
    );

    // 确定用户插件根目录（可写，第三方插件安装位置）
    // 解析优先级：环境变量 > OS 标准目录 > None（保持原行为）
    let user_plugins_dir = resolve_user_plugins_dir();
    if let Some(ref user_dir) = user_plugins_dir {
        info!(
            target: "agentos-kernel",
            "User plugin root: {}",
            user_dir.display()
        );
    } else {
        info!(
            target: "agentos-kernel",
            "User plugin root: disabled (no env var, OS data dir unavailable)"
        );
    }

    // 模式种子对账（设计稿 2026-09-15 §2「版本管理」）：必须在插件扫描之前——
    // 播种/静默升级要先落盘，discover 才能扫到正确的用户副本内容。
    let seed_outcomes = reconcile_mode_seeds_at_boot(&plugins_dir, user_plugins_dir.as_deref());

    // 用户仓 git 化（设计稿 2026-09-15 §2.2「用户目录 git 化边界」）：对账完成后
    // 确保用户仓就绪；对账有落盘变化时自动提交一笔（用户自己的改动不自动提交）。
    init_user_repo_at_boot();
    commit_seed_reconciliation_at_boot(&seed_outcomes);

    // config_root = 工作区根目录下的 config/（与 plugins_dir 同级基准）
    // 必须在创建 loader 之前推导：loader 需要 config_root 才能让 load_config() 不返回空 {}
    let config_root = std::env::var("AGENTOS_CONFIG_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .and_then(|p| p.parent())
                .and_then(|p| p.parent())
                .map(|root| root.join("config"))
                .unwrap_or_else(|| PathBuf::from("config"))
        });

    info!(
        target: "agentos-kernel",
        "Config root: {}",
        config_root.display()
    );

    // 内核能力契约加载（定义驱动入口校验 + schema 聚合的
    // 单一真值源）。目录缺失 = 未启用（宽泛放行）；文件损坏 = fail-fast 拒启
    // ——契约是校验器的眼睛，坏契约静默跳过等于校验器装瞎。
    let capability_contracts: Arc<Vec<agentos_api::kernel_capabilities::KernelCapabilityContract>> =
        Arc::new(
            agentos_api::kernel_capabilities::load_contracts(
                &config_root.join("kernel/kernel_capabilities"),
            )
            .unwrap_or_else(|e| panic!("内核能力契约文件加载失败（fail-closed）: {e}")),
        );
    info!(
        target: "agentos-kernel",
        namespaces = capability_contracts.len(),
        "Loaded kernel capability contracts (definition-driven entry validation)"
    );

    // 加载 .env 到进程环境（用户空间优先，回落项目根）。
    // sidecar 子进程默认继承父进程环境变量（tokio Command 无 env_clear），
    // 这样 sidecar 能解析配置里的 ${API_KEY} 等占位符（ADR §4.3 secrets）。
    // 仅设置进程未已有的变量（系统环境变量优先于 .env）。
    //
    // 用 mcp::env_file::env_path_for_root 而非自己拼 project_root/.env：
    // 写侧（设置页填 key）走同一函数，两侧必须同源——否则会出现"key 写进
    // 用户空间、启动读的还是项目根"这类静默分叉。
    if let Some(project_root) = config_root.parent() {
        let env_path = agentos_mcp::env_file::env_path_for_root(project_root);
        if env_path.is_file() {
            if let Ok(content) = std::fs::read_to_string(&env_path) {
                let mut loaded = 0usize;
                for line in content.lines() {
                    let line = line.trim();
                    if line.is_empty() || line.starts_with('#') {
                        continue;
                    }
                    if let Some((key, value)) = line.split_once('=') {
                        let key = key.trim();
                        // 仅当进程环境未已有该变量时设置（系统环境 > .env）
                        if std::env::var(key).is_err() {
                            let value = value.trim().trim_matches('"');
                            std::env::set_var(key, value);
                            loaded += 1;
                        }
                    }
                }
                info!(target: "agentos-kernel", "Loaded {} vars from {}", loaded, env_path.display());
            }
        }
    }

    // 把 config_root 发布到进程环境：invoker 插件指纹与 mcp spawn 的
    // .env 增量叠加（env_file 模块）靠它定位项目根 .env——用户在设置页
    // 填写 API Key 后无需重启内核即可生效。
    if std::env::var("AGENTOS_CONFIG_ROOT").is_err() {
        std::env::set_var("AGENTOS_CONFIG_ROOT", &config_root);
    }

    // 创建插件加载器——以 plugins/shared/ 为内置根，启用 user_root 覆盖语义，
    // 并接入 config_root（P0-1：否则 load_config() 恒返回空 {}，插件收不到配置）
    // 启动扫描根 = 内置根 + 用户根（与热路径 discover_new_plugins 同源）：用户
    // 空间模式包在 `modes/<mode_id>/plugin.json` 二级嵌套（装机布局契约），
    // discover 内建的用户根扫描（一级子目录）看不到；只传内置根会让启动装载
    // repo 份，而热路径同 id 解析用户份（loader 用户根子树赢）——首个 sync 周
    // 期源码目录翻转触发复验驱逐（/ext 路由空窗）。双根同 id 用户赢必须在启动
    // 路径同样成立。
    let root_paths = collect_boot_plugin_roots(&plugins_dir, user_plugins_dir.as_deref());
    let loader = build_plugin_loader(&plugins_dir, user_plugins_dir, &config_root);

    info!(
        target: "agentos-kernel",
        "Scanning {} root directories under {}",
        root_paths.len(),
        plugins_dir.display()
    );

    // A10：discover 失败 fail-fast——IO 故障/manifest 损坏意味着插件面不可用，
    // 静默降级为空集会让内核以"无插件"假象运行（工具/能力全缺，问题后置难查），
    // 故拒绝启动。逃生门：AGENTOS_ALLOW_EMPTY_PLUGINS=1（嵌入式/最小化部署/
    // 沙箱场景显式声明接受空插件集）时保留旧的降级启动行为。
    // boot 走 discover_boot_cached（T4/R-2 层 2）：先探测预烤发现缓存，对账
    // 全匹配即跳过现场扫描重解并透出缓存已验证集（G2 boot 免 spawn 复验）；
    // 缓存缺席/漂移/版本不识别 = 回退本 trait 路径（热路径 discover 原样）。
    let (manifests, discovery_cache_outcome) = match loader
        .discover_boot_cached(&root_paths.iter().map(|s| s.as_str()).collect::<Vec<_>>())
        .await
    {
        Ok(m) => m,
        Err(e) if std::env::var("AGENTOS_ALLOW_EMPTY_PLUGINS").as_deref() == Ok("1") => {
            warn!(
                target: "agentos-kernel",
                "Failed to discover plugins: {}. AGENTOS_ALLOW_EMPTY_PLUGINS=1 → continuing with empty plugin list.", e.message
            );
            (Vec::new(), Default::default())
        }
        Err(e) => {
            eprintln!(
                "[boot] 插件 discover 失败，拒绝启动: {}（设 AGENTOS_ALLOW_EMPTY_PLUGINS=1 可强制以空插件集启动）",
                e.message
            );
            std::io::Write::flush(&mut std::io::stderr()).ok();
            return Err(Box::<dyn std::error::Error>::from(format!(
                "plugin discover failed at boot: {}",
                e.message
            )));
        }
    };
    if discovery_cache_outcome.hit {
        info!(
            target: "agentos-kernel",
            cache_verified = discovery_cache_outcome.cache_verified_winner_ids.len(),
            "discovery cache: cold start served from prebaked face (unchanged seed, R-2)"
        );
    }

    // 注册闸-服务依赖解析（fail-closed，唯一依赖轴）：任意插件的 requires_services
    // 不满足（能力角色无人提供 / 服务端点未注册）→ 拒绝启动；服务→插件映射由服务面注册表
    // 完成，消费者不点名插件 id。把"依赖不满足"从运行期谜题提前到启动期暴露。
    agentos_plugin_loader::resolve_requires_services(&manifests).map_err(|e| {
        eprintln!("[boot] 插件服务依赖解析失败，拒绝启动: {e}");
        std::io::Write::flush(&mut std::io::stderr()).ok();
        Box::<dyn std::error::Error>::from(format!(
            "plugin service dependency resolution failed at boot: {e}"
        ))
    })?;

    // M2-static：启动期按 requires_services（服务边）静态拓扑排序——插件间启动顺序从
    // HashMap 任意序变为显式可证明的依赖序（依赖者后加载；tie-break 字典序）。
    // 服务依赖环 fail-fast（与 pipeline load_and_compile 的坏配置拒绝启动一致）。
    let manifests =
        agentos_plugin_loader::sort_manifests_topologically(&manifests).map_err(|e| {
            eprintln!("[boot] 插件依赖环检测失败，拒绝启动: {e}");
            std::io::Write::flush(&mut std::io::stderr()).ok();
            Box::<dyn std::error::Error>::from(format!("circular plugin dependencies at boot: {e}"))
        })?;

    info!(
        target: "agentos-kernel",
        "Discovered {} plugin manifests",
        manifests.len()
    );

    // ── 启动期按 manifest 声明面播种用户空间配置（2026-09-28 批次 A3）──
    // 读写同源公理：配置已迁移用户空间，则一切读取出自用户空间。对每个
    // manifest config_files 声明的引用形态文件（target != env）做「用户层
    // 缺失即播种 + 接管登记」，使运行时读（用户空间叠加）恒命中用户层。
    // 幂等（已存在/已登记跳过）；单文件失败告警不阻断启动。
    {
        let project_root = config_root
            .parent()
            .map(PathBuf::from)
            .unwrap_or_else(|| config_root.clone());
        let (seeded, failures) =
            agentos_api::config_service::seed_declared_configs(&project_root, &manifests);
        for (path, err) in &failures {
            warn!(
                target: "agentos-kernel",
                "config seeding skipped: {path} : {err}"
            );
        }
        if seeded > 0 {
            info!(
                target: "agentos-kernel",
                "config seeding: {seeded} 个声明配置文件播种至用户空间"
            );
        }
    }

    // 创建能力注册表
    let registry = Arc::new(CapabilityRegistryImpl::new());
    // M1：per-plugin 注册账本（guard 化）——启动注册循环经 guarded 注册入账本，
    // disable/unload 路径经 revoke 结构性收回（registry 四维 + broadcaster 绑定）。
    let plugin_scopes = Arc::new(agentos_plugin_loader::PluginScopeRegistry::new());

    // 安装触发模型 L1：加载 default_profile.yaml 启用层。
    // disabled 的插件不进注册表出口（tools/route_signals/http_routes 不暴露）。
    // 优先级：manifest.enabled > profile.plugins[id] > defaults > enabled=true。
    // K6：profile 解析失败 → 保守全禁（is_enabled 恒 false），此处升级为 error
    // 级启动报告——下方注册循环会以"全部插件 skipped_disabled"落地该裁决。
    let enablement = agentos_plugin_loader::PluginEnablement::load(&config_root);
    if enablement.is_corrupted() {
        error!(
            target: "agentos-kernel",
            "default_profile.yaml 解析失败：启用层进入保守全禁（K6 fail-closed），\
             所有插件本次启动不注册；修复 config/kernel/default_profile.yaml 后重启"
        );
    }

    // 将 manifest 中声明的工具注册到 CapabilityRegistry
    let mut tool_count = 0usize;
    let mut skipped_disabled = 0usize;
    // K9：缺 input_schema 而以 {} 补注册的工具计数（进启动报告）。
    let mut missing_input_schema = 0usize;
    for manifest in &manifests {
        // L1 Enabled 过滤：disabled 插件不进出口（安装触发模型 §1.1）
        if !enablement.is_enabled(&manifest.id, manifest.enabled) {
            skipped_disabled += 1;
            continue;
        }
        // D.6 槽位拆分：
        // capabilities.tools 语义唯一 = 给 LLM 的工具，声明即注册（不看类型）；
        // 内部服务方法在 capabilities.services（不注册，走 invoke_entry/
        // http_endpoints/显式 plugin_id/provides 通道）。
        {
            // 宿主领地登记（F-min 属地路由，设计文档 §53）：与运行时注册路径
            // plugin_lifecycle::register_plugin_capabilities 同款。
            if let Some(mcp_cfg) = &manifest.mcp {
                if !mcp_cfg.allowed_roots.is_empty() {
                    registry.register_host(&manifest.id, mcp_cfg.allowed_roots.clone());
                }
            }
            let scope = plugin_scopes.scope_of(&manifest.id);
            for tool_cap in &manifest.capabilities.tools {
                let category = tool_cap.category.clone().unwrap_or(ToolCategory::System);
                // K9：缺 input_schema 以 {} 补注册但必须可见（warn + 计数进启动
                // 报告）。{} 是 object，LLM 侧 input_schema.is_object() 过滤对它
                // 恒不触发——零参数描述进工具面，LLM 只能盲调；补声明属
                // plugins/ 侧治理。运行时新增插件注册路径同款 warn 见
                // plugin_lifecycle::register_plugin_capabilities。
                if tool_cap.input_schema.is_none() {
                    missing_input_schema += 1;
                    warn!(
                        target: "plugin-registration",
                        plugin_id = %manifest.id,
                        tool = %tool_cap.name,
                        "tool manifest 缺 input_schema，以 {{}} 补注册（LLM 侧 object 过滤恒不触发，LLM 只能盲调；请补声明）"
                    );
                }
                let descriptor = ToolDescriptor {
                    name: tool_cap.name.clone(),
                    description: tool_cap
                        .description
                        .clone()
                        .unwrap_or_else(|| format!("Tool from {}", manifest.name)),
                    plugin_id: manifest.id.clone(),
                    input_schema: tool_cap
                        .input_schema
                        .clone()
                        .unwrap_or(serde_json::json!({})),
                    output_schema: tool_cap.output_schema.clone(),
                    category,
                    source: if manifest.host_type == agentos_core::traits::HostType::Sidecar {
                        ToolSource::Mcp
                    } else {
                        ToolSource::Builtin
                    },
                    ui: tool_cap.ui.clone(),
                    render: tool_cap.render.clone(),
                    isolation_capable: tool_cap.isolation_capable,
                };
                // M1：guarded 注册——撤销 guard 入 scope，disable 时结构性收回。
                scope.track(registry.register_tool_guarded(&manifest.id, descriptor));
                tool_count += 1;
            }
        }

        // 注册路由信号（M1 guarded）
        if !manifest.capabilities.route_signals.is_empty() {
            let scope = plugin_scopes.scope_of(&manifest.id);
            scope.track(registry.register_route_signals_guarded(
                &manifest.id,
                manifest.capabilities.route_signals.clone(),
            ));
        }
    }

    info!(
        target: "agentos-kernel",
        "Registered {} tools from {} plugins (declaration-based, D.6 slot split; {} disabled by profile; {} missing input_schema registered as {{}} — K9)",
        tool_count,
        manifests.len(),
        skipped_disabled,
        missing_input_schema
    );

    // P3：注册插件 HTTP 端点（ADR §3.3）——聚合报错（fail-closed，不逐个 panic）。
    // 只注册 enabled 插件的 http_endpoints（安装触发模型 L1 过滤）。
    let enabled_manifests: Vec<agentos_core::traits::PluginManifest> = manifests
        .iter()
        .filter(|m| enablement.is_enabled(&m.id, m.enabled))
        .cloned()
        .collect();
    let http_errors = agentos_api::http_dispatcher::register_manifest_http_routes(
        &registry,
        &enabled_manifests,
        Some(&plugin_scopes),
    );
    let http_route_count = registry.list_http_routes().len();
    if !http_errors.is_empty() {
        panic!(
            "插件 HTTP 端点注册失败（路由治理 fail-closed），拒绝启动内核:\n{}",
            http_errors.join("\n")
        );
    }
    info!(
        target: "agentos-kernel",
        "Registered {} plugin HTTP endpoints",
        http_route_count
    );

    // 初始化存储——StorageBackend driver 化（§9.6）：按 config/kernel/storage.yaml 或
    // 环境变量选 driver（sqlite | memory；postgres 留桩），默认 sqlite +
    // 项目根 agentos_kernel.db（AGENTOS_DB_PATH/:memory: 兼容）。
    // 返回双句柄：store_dyn（业务账本 trait 面，runs/messages/traces/blobs/memory/
    // users——换 driver 时完全可用）+ sqlite_db（SQLite 专有 db-admin 表驱动接口，
    // 非 SQLite driver 下为 None → db-admin capability 诚实降级）。
    // 存储是自举必需件 + 审计真相源，driver 编译进内核而非插件轨（§9.6 判据）。
    // resolve_storage_config：config/kernel/storage.yaml 存在但损坏 → Err 拒绝启动
    // （数据正确性优先，不静默落默认库）。
    let storage_cfg = agentos_engine::storage_factory::resolve_storage_config(&config_root)?;
    info!(
        target: "agentos-kernel",
        driver = %storage_cfg.driver,
        sqlite_path = %storage_cfg.sqlite_path,
        "Storage driver resolved (config/kernel/storage.yaml > env > default sqlite)"
    );

    // BUG-85 启动迁移：装机形态默认库位置已改用户根（electron 注入
    // AGENTOS_DB_PATH），安装目录（resources\）残留的存量库须在首启时复制到
    // 新位置——否则静默卸载把它当程序文件删除，用户会话/消息/凭据全丢。
    // 复制失败 fail-closed 拒绝启动（不静默换新库）；dev 仓形态新旧同位，
    // 天然 no-op，现网行为不变。
    let migrated = agentos_engine::storage_factory::migrate_legacy_db_to_user_root(
        &storage_cfg,
        &config_root,
    )?;
    if migrated {
        eprintln!(
            "[boot] 存量库已迁移到本次生效位置 {}（旧库原件保留，随卸载清理）。",
            storage_cfg.sqlite_path
        );
        info!(
            target: "agentos-kernel",
            db = %storage_cfg.sqlite_path,
            "legacy install-dir database migrated to effective location (BUG-85)"
        );
    } else {
        // 迁移护栏（ADR 2026-09-13-unified-user-root §2.5）：默认库位置从项目根迁到
        // 用户空间后，存量部署若原样启动会**开出一个全新的空库**——数据没丢，但用户
        // 看到的是"全没了"，且此后写入都进新库、两边分叉。这里检测该形态并显式告警
        // 给出迁移命令；**不静默自动迁移**（magic 迁移不可观测，失败/半途中断时状态
        // 不明——沿用仓内 migrate_legacy_data_to_default 的显式调用裁定）。
        // BUG-85 后装机形态由上方 migrate_legacy_db_to_user_root 自动接管，本护栏
        // 只对未触发迁移的形态（dev 同位/全新安装/两处都有）兜底。
        warn_legacy_db_not_migrated(&storage_cfg, &config_root);
    }

    let (store, sqlite_db) = agentos_engine::storage_factory::open_storage(&storage_cfg)?;
    let store_dyn: Arc<dyn agentos_core::traits::StorageBackend> = store.clone();

    // 播种内置 admin 用户（0.5.0 最小持久化地基）：argon2 哈希落库 +
    // 随机口令置 must_change_password；存量明文口令行启动迁移哈希化（D1，fail-closed）。
    seed_admin_user(store.clone()).await;
    if let Err(e) = migrate_plaintext_passwords(store.clone()).await {
        eprintln!(
            "[boot] 明文口令迁移失败，拒绝启动: {e}\n\
             处置：设置 AGENTOS_ADMIN_PASSWORD=<新口令> 重启（重置 admin 口令），或修复存储后重试"
        );
        std::io::Write::flush(&mut std::io::stderr()).ok();
        return Err(Box::<dyn std::error::Error>::from(format!(
            "plaintext password migration failed at boot: {e}"
        )));
    }

    // B2：启动时清扫孤儿 run——上次进程崩溃留下的 status='running' 的 run
    // 标记为 failed + 补 ended_at（persist_run_end 未执行的真实表现；不清扫会永远卡
    // running，历史/会话状态悬空）。已结束的 run 不受影响。
    // B2 清扫是 SQLite 专有路径（reap_orphan_runs 固有方法）——非 SQLite driver
    // 跳过（孤儿 run 清扫属启动家政，跳过不影响正确性，仅留状态悬空到下次 sqlite 起时清）。
    if let Some(sqlite) = sqlite_db.as_ref() {
        match sqlite.reap_orphan_runs() {
            Ok(victims) if !victims.is_empty() => {
                // 失败原因落日志：这是这些 run 唯一的失败裁决点，受害者清单必须
                // 可见（装机 2026-10-10 B74/B75/B76 三连：簿记 failed 而全日志
                // 无 ERROR——排障者找不到失败裁决点）。
                error!(
                    target: "agentos-kernel",
                    reaped = victims.len(),
                    victims = ?victims,
                    "上次进程异常终止：启动清扫孤儿 run（簿记标 failed；投影随写由任务域对账补齐）"
                );
            }
            Ok(_) => {}
            Err(e) => {
                warn!(target: "agentos-kernel", error = %e, "启动清扫孤儿 run 失败（留待下次启动重扫）");
            }
        }
    }

    // traces/blobs 保留清扫（ADR 2026-09-11）：traces 按
    // AGENTOS_TRACE_RETENTION_DAYS（默认 90，0 = 禁用）保留窗口滚动清除，
    // 孤儿 blob（不被 message_slots 引用）同步回收——无保留机制两表无界增长。
    // purge SQL 与保留期解析在 engine store 侧，此处只接线：interval 首 tick
    // 立即到期 = 启动清一次 + 之后每 6h；关停随 runtime drop 自然退出。
    // 单轮失败 warn 留痕、下周期重试（家政失败不阻断内核）。
    if let Some(sqlite) = sqlite_db.clone() {
        let retention_days = agentos_engine::store::trace_retention_days();
        if retention_days == 0 {
            info!(
                target: "agentos-kernel",
                "traces/blobs 保留清扫禁用（AGENTOS_TRACE_RETENTION_DAYS=0）"
            );
        } else {
            info!(
                target: "agentos-kernel",
                retention_days,
                "traces/blobs 保留清扫启用（启动清一次 + 每 6h 周期）"
            );
            // 堆栈级诊断插桩：任务槽 + 等待点标签（tick await = 空闲等待）。
            let sweep_activity = agentos_core::task_activity::global_registry()
                .register("trace-blob-retention-sweep");
            tokio::spawn(agentos_core::task_activity::scope(
                sweep_activity,
                async move {
                    let mut ticker =
                        tokio::time::interval(std::time::Duration::from_secs(6 * 60 * 60));
                    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
                    agentos_core::task_activity::set_current_label("idle: next tick 6h");
                    loop {
                        ticker.tick().await;
                        agentos_core::task_activity::set_current_label(
                            "sweeping traces/orphan blobs",
                        );
                        // retention_days 经 trace_retention_days 钳制（≤36_500 天），
                        // cutoff 必在 DateTime 值域内。
                        let cutoff = chrono::Utc::now()
                            - chrono::Duration::seconds(retention_days as i64 * 86_400);
                        match sqlite.purge_traces_older_than(cutoff).await {
                            Ok(0) => {}
                            Ok(purged) => {
                                info!(target: "agentos-kernel", purged, "trace 保留清扫完成")
                            }
                            Err(e) => warn!(
                                target: "agentos-kernel",
                                error = %e,
                                "trace 保留清扫失败（下周期重试）"
                            ),
                        }
                        match sqlite.purge_orphan_blobs().await {
                            Ok(0) => {}
                            Ok(purged) => {
                                info!(target: "agentos-kernel", purged, "孤儿 blob 清扫完成")
                            }
                            Err(e) => warn!(
                                target: "agentos-kernel",
                                error = %e,
                                "孤儿 blob 清扫失败（下周期重试）"
                            ),
                        }
                        agentos_core::task_activity::set_current_label("idle: next tick 6h");
                    }
                },
            ));
        }
    }

    // 创建真实插件调用器——按 host_type 透明分发：
    //   Sidecar: 通过 MCP stdio fork Python sidecar 执行插件
    //   InProcess: 经 NativePluginLoader 加载 cdylib 走 C-ABI（放进插件目录即用）
    // 默认配置即可运行。原 Wasm 轨已按两轨终局决策关闭摘除。
    // 在 loader 被 move 进 Arc 之前，先取出插件根目录映射（venv 自愈批量面用）；
    // AppState 不再持快照（B1）——静态资源/配置保存/validate-all 消费点经
    // with_plugin_loader 注入的活真源现取。
    let plugin_dirs = loader.get_plugin_dirs();
    // venv 自愈（装机形态，BUG-55 方向③，ADR 2026-09-20 决策②）：门开时 boot
    // 后台对缺 venv 解释器的 Python sidecar 跑 `uv sync` 重建——打包资源排除
    // .venv 且装机链没有 dev launcher，不自愈则 sidecar spawn 期永久 fail-closed。
    // 重建落点（ADR 2026-09-28-venv-user-space-provisioning）：插件目录可写
    // 原地重建；只读（装机态）重定向用户空间登记处 plugin-venvs/<键>，spawn
    // 期解析序②据此命中。
    // 共享合宿宿主（_host）优先重建：合宿成员 spawn 只认它（HOST_VENV_MISSING
    // 无回退）；合宿成员自身 venv 不建（运行期零消费，去重 ADR 2026-09-07）。
    // dev 门不开（重建权在 launcher）。uv 缺席/失败只 warn 降级，不阻断启动。
    if agentos_api::venv_provision::autoprovision_enabled() {
        let targets =
            agentos_api::venv_provision::select_missing_venv_plugins(&manifests, &plugin_dirs);
        let host = agentos_api::venv_provision::select_shared_host_target(&manifests, &plugin_dirs);
        let aux = agentos_api::venv_provision::select_missing_aux_venvs(&manifests, &plugin_dirs);
        if !targets.is_empty() || host.is_some() || !aux.is_empty() {
            info!(
                target: "agentos-kernel",
                count = targets.len(),
                host = host.as_ref().map(|d| d.display().to_string()),
                aux = aux.len(),
                "检测到缺 venv 解释器的 Python sidecar，boot 后台 uv sync 自愈（共享合宿宿主优先；完成前相应 sidecar spawn 仍会失败）"
            );
            agentos_core::task_activity::spawn_named(
                "venv-provision",
                agentos_api::venv_provision::provision_and_log(targets, host),
            );
            if !aux.is_empty() {
                agentos_core::task_activity::spawn_named(
                    "venv-provision-aux",
                    agentos_api::venv_provision::provision_aux_and_log(aux),
                );
            }
        }
    }
    let loader_arc = Arc::new(loader);
    let native_loader = Arc::new(NativePluginLoader::new());
    // 监控 M2：内核自采 A 类指标计数器注册中心。提前到 invoker 构建前——
    // 预算闸（D5）装配要在首个 spawn 点（G2 启动期校验/预热）之前拿到
    // 计数器句柄（拒绝计数走 M2 flush 链路暴露）。
    let kernel_counters = Arc::new(agentos_api::metrics::KernelCounters::new());
    let invoker = Arc::new(
        PluginInvokerImpl::new(loader_arc.clone())
            .set_native_loader(native_loader)
            .with_tool_admission_limit(pool.max_inflight_tools)
            // M4 预派发等待界派生反查源（tool_name → plugin_id，ADR
            // 2026-11-05-timeout-taxonomy-and-merge）：registry 在本行之前
            // 已构建（工具注册完成面），invoker 只持 Arc 读。
            .with_tool_registry(registry.clone()),
    );
    // 内存预算闸装配（ADR 2026-10-19 D5 第一刀）：显式 AGENTOS_MEM_BUDGET_MB
    // 正值才启用。采样函数在 bin 层，invoker 不能反向依赖 bin——经闭包注入
    // （with_restart_hook 注入先例同构）；拒绝计数走内核自采计数器（M2 flush
    // 链路暴露），拒绝本身由 invoker 侧记 WARN + 结构化错误信封。注入点先于
    // 任何 spawn（G2 启动期校验/预热/请求路径全过同一闸）。
    if let Some(budget_mb) = mem_budget_mb {
        let est_mb = resolve_host_est_commit_mb();
        let budget_rejected =
            kernel_counters.register_counter("kernel.spawn_budget_rejected_total", Labels::new());
        invoker.set_pre_spawn_gate(Arc::new(move || {
            let current_mb = sample_private_bytes().map(|b| b / (1024 * 1024));
            if budget_exceeded(current_mb, est_mb, budget_mb) {
                budget_rejected.inc(1);
                // 超限分支必然有采样值（缺数不判），unwrap_or_default 仅兜底
                let current = current_mb.unwrap_or_default();
                return Err(format!(
                    "当前内核 Private Bytes {current}MB + 新宿主预估提交 {est_mb}MB \
                     > 预算 {budget_mb}MB"
                ));
            }
            Ok(())
        }));
        info!(
            target: "agentos-kernel",
            budget_mb,
            host_est_commit_mb = est_mb,
            "内存预算闸已启用：宿主 spawn 前准入预检，超预算拒绝 spawn（load shedding）"
        );
    }
    // 启动插件空闲软卸载 GC（生命周期管理：用到才加载 + 长时间不用自动 kill 进程，
    // manifest 保留，下次调用重新 spawn）。每 30s 扫描，阈值默认 300s。
    invoker.start_idle_gc();

    // 生命周期钩子事件总线（多消费者广播）：在既有"点对点"分发旁路接入一条 broadcast 通道，
    // 把 OnPipelineStart/OnPipelineEnd（engine）+ OnLoad（invoker sidecar spawn）等事件
    // fan-out 给审计日志 + 指标等订阅者。
    // 容量 1024：生命周期事件低频，足够吸收突发；emit best-effort 非阻塞，绝不拖慢引擎热路径。
    let hook_bus = Arc::new(agentos_hooks::HookEventBus::new(1024));
    // 把同一总线注入 invoker：sidecar spawn 的 OnLoad 事件在点对点直调（notifications/on_load）
    // 旁路 fan-out 给审计/指标订阅者（与 engine 的 OnPipelineStart/End 同一总线）。
    // 必须在 spawn 任何 sidecar 前完成（start_idle_gc 之后、请求接入之前即满足）。
    invoker.set_hook_bus(hook_bus.clone());
    // 域事件发射点（session_routes / ws_session 的 handler）经全局单例访问同一总线
    // （它们只持 AppState，不便穿层传总线句柄；未注册时观察层静默降级）。
    agentos_hooks::set_global(hook_bus.clone());

    // 闸2·观测：契约状态账本（boot 收口全量插件健康度，后续 reenable/热发现/
    // validate-all 共享写入；与 AppState 同一实例注入，`GET /contract-status` 消费）。
    let contract_states = Arc::new(agentos_api::contract::ContractLedger::new());
    // 注册闸 G2 启动期存量校验（与热发现同源公共函数）：对 enabled 的 sidecar
    // tool 插件 spawn → tools/list → 对照声明。判定失败（tools/list 成功但声明
    // 工具缺失）→ 剔除漂移工具并按净化后 manifest 重注册（前端经契约状态页可见）。
    // 观测失败（spawn/list 重试后仍失败）≠ 判定失败：保留
    // 声明注册 + 账本标记"校验未完成"，30s 后后台复验——复验出真漂移才净化。
    // 只验 tools 非空的 sidecar（services 方向 schema 契约在 Phase 1 补）。
    // 探测扇出有界并行（Phase 3 #1）：宿主 spawn 每个秒级且相互独立，并行度
    // min(8, 逻辑核数)——boot 串行段 45.6s 主瓶颈并行化；账本写入与漂移重注册
    // 收口仍在扇出后按原 manifest 序串行执行（语义同原串行循环）。
    {
        use agentos_api::plugin_watcher::g2_boot_verify_all;
        let summary = g2_boot_verify_all(
            invoker.clone(),
            &manifests,
            &enablement,
            &contract_states,
            &registry,
            &plugin_scopes,
            &discovery_cache_outcome.cache_verified_winner_ids,
            agentos_core::types::bounded_spawn_concurrency(),
        )
        .await;
        info!(
            target: "plugin-g2-boot",
            verified = summary.verified,
            drifted = summary.drifted,
            spawn_failed = summary.spawn_failed,
            cache_skipped = summary.cache_skipped,
            "注册闸 G2 启动期存量校验完成"
        );
        // 观测失败复验（fire-and-forget）：30s 后重验，复验出真漂移（判定失败）
        // 才净化重注册；复验仍观测失败则保持声明注册（下次 boot/热校验再试）。
        let observe_incomplete = summary.observe_incomplete;
        if !observe_incomplete.is_empty() {
            let inv2 = invoker.clone();
            let reg2 = registry.clone();
            let scopes2 = plugin_scopes.clone();
            let ledger2 = contract_states.clone();
            let reverify_fut = async move {
                agentos_core::task_activity::set_current_label("waiting 30s re-verify delay");
                tokio::time::sleep(std::time::Duration::from_secs(30)).await;
                for manifest in &observe_incomplete {
                    let outcome = agentos_api::plugin_watcher::g2_verify_and_sanitize(
                        inv2.as_ref(),
                        manifest.clone(),
                    )
                    .await;
                    ledger2.upsert(agentos_api::contract::PluginContractState::derived(
                        manifest,
                        true,
                        Some(&outcome),
                    ));
                    if outcome.spawn_failed {
                        warn!(
                            target: "plugin-g2-boot",
                            plugin = %manifest.id,
                            "注册闸 G2（复验）：观测仍失败——保持声明注册，待下次校验"
                        );
                        continue;
                    }
                    if outcome.drift {
                        let (tools, http_routes) =
                            agentos_api::plugin_lifecycle::reenable_plugin_capabilities(
                                &outcome.manifest,
                                &reg2,
                                &scopes2,
                            );
                        warn!(
                            target: "plugin-g2-boot",
                            plugin = %manifest.id,
                            rejected = ?outcome.rejected_tools,
                            tools,
                            http_routes,
                            "注册闸 G2（复验）：判定声明与实现不一致，已按净化后 manifest 重注册（需修改插件）"
                        );
                    } else {
                        info!(
                            target: "plugin-g2-boot",
                            plugin = %manifest.id,
                            "注册闸 G2（复验）：观测恢复，校验通过"
                        );
                    }
                }
            };
            agentos_core::task_activity::spawn_named("g2-boot-reverify", reverify_fut);
        }
    }

    // 监控 M1：创建指标聚合器（三通道汇聚：内核自采 + 插件 record_metric + invoker 代采进程态）。
    // M4：router 持聚合器，metrics.record 反向调用写入它。
    // （kernel_counters 已提前到 invoker 构建前创建——预算闸装配要早于首个
    // spawn 点拿计数器句柄，见上。）
    let metrics_aggregator = agentos_api::metrics::MetricsAggregator::new();

    // 生命周期事件订阅者接线：
    // - 审计订阅者（agentos-hooks）：每个生命周期事件记 structured log（hook + 目标）。
    // - 指标订阅者（metrics/lifecycle）：按 hook 类型 inc lifecycle.* 计数器，
    //   经 KernelCounters → flush_to → MetricsAggregator → Prometheus 链路暴露。
    // 两者均 spawn 后台任务，慢消费者 Lagged 自动 warn 恢复（绝不 fatal）。
    let _audit_handle = agentos_hooks::spawn_audit_subscriber(hook_bus.clone());
    let _lifecycle_metrics_handle = agentos_api::metrics::spawn_lifecycle_metrics_subscriber(
        hook_bus.clone(),
        kernel_counters.clone(),
    );
    info!(target: "agentos-kernel", "lifecycle hook event bus + subscribers (audit/metrics) started");

    // 启用 sidecar→内核反向 capability 通道（审批暂停/恢复、复盘调管道、event-bus 的地基）。
    // 监控 M4：router 持聚合器，metrics.record 分支写聚合器（第 6 个 capability）。
    // tool-executor：tool_core sidecar 委托内核执行 tool 插件 sidecar（bash_execute 等）。
    // event-bus.emit：流式 chunk 推前端——session 提前创建并注入 router + 后续 enable_session 复用。
    // service-registry.*（M2）：基础设施下沉内核——插件经此 capability 访问内核共享存储
    //   （execution-records/pipeline-summaries/memory，M1 落地）。复用同一 SqliteStore 实例。
    let session_coord = Arc::new(agentos_session::SessionCoordinator::new());
    // 插件失败 WS 实时面（错误面单点的消费端）：引擎步失败单点广播
    // plugin_step_failed 域事件，本订阅者翻译为既有 WS plugin_error 事件——
    // 步级失败实时浮出前端，不再攒到 run 收尾才结算（长跑 run 的步级失败
    // 此前全程不可见，2026-10-27 装机压缩死循环实证）。
    let _plugin_error_ws_handle = agentos_api::plugin_lifecycle::spawn_plugin_error_ws_forwarder(
        hook_bus.clone(),
        session_coord.clone(),
    );

    // M5：动态 capability handler 注册表 + McpBridge。
    // 扫描已启用 manifest 的 provides.capabilities，注册成 handler；
    // McpBridge 把 capability 调用转发到对应 sidecar 插件的工具。
    // 这让交互等插件自注册的 namespace 经 reader loop → router →
    // handler → bridge → invoker.invoke_tool → sidecar 完成闭环。
    // 路由完全从 manifest provides.capabilities 声明派生（含 tool_prefix），
    // 内核零硬编码——新插件声明 provides 即自动注册，无需改内核。
    let handler_registry = Arc::new(agentos_mcp::CapabilityHandlerRegistry::new());
    let mcp_bridge = Arc::new(agentos_plugin_loader::McpBridge::new(
        invoker.clone() as Arc<dyn agentos_core::traits::PluginInvoker>
    ));
    mcp_bridge.add_routes_from_manifests(&enabled_manifests);
    // G2 注册门禁（全能力面通用化，用户裁定 2026-10-26）：provides.capabilities
    // 动态 handler 与 LLM 工具面同源同语义——校验未完成/校验失败插件的
    // capability 调用（含 interaction-respond 协议角色等）一律拒绝，复验通过
    // （账本 ok）自动放行——实时读契约账本现值，无独立状态需要维护。
    let provides_verify_gate: agentos_plugin_loader::VerificationGateLookup = {
        let ledger_for_gate = contract_states.clone();
        Arc::new(move || ledger_for_gate.verify_pending_plugin_ids())
    };
    let registered = agentos_plugin_loader::register_provided_capabilities(
        &handler_registry,
        &enabled_manifests,
        Some(mcp_bridge.clone()),
        Some(provides_verify_gate),
    );
    info!(
        target: "agentos-kernel",
        "Registered {} provided capabilities from plugin manifests (handler registry)",
        registered
    );

    // boot-plugin 第一刀：db-admin capability（SQL 能力层留内核，HTTP 面在
    // plugins/shared/db_admin 插件）。handler 直连 SqliteStore（cdylib 级信任件，
    // 无 IPC），鉴权在 handler 内核侧执行（插件仅转发 Authorization 头，见
    // db-admin/src/capability.rs 模块文档）。注册先于任何 sidecar spawn——
    // initialize 握手的 build_declared_capabilities_from_namespaces 据此把
    // db-admin 声明给 db_admin 插件（SDK 创建 CapabilityHandle）。
    // db-admin 的 db 句柄按 driver 注入：非 SQLite driver 为 None（handler 的
    // get_db 返回"统一数据接口未启用"400，诚实降级）。
    handler_registry.register(std::sync::Arc::new(
        agentos_db_admin::DbAdminCapabilityHandler::new(Some(store_dyn.clone()), sqlite_db.clone()),
    ));
    info!(
        target: "agentos-kernel",
        "Registered db-admin capability handler (7 methods, SQL layer in-kernel, HTTP face in db_admin plugin)"
    );

    // boot-plugin 第二刀：user-admin capability（用户管理**策略面**留内核，
    // HTTP 面在 plugins/shared/user_admin 插件）。§9.6 精确拆分：auth 执行门
    // （login/logout/me/register/refresh 的验签与路由准入）永留内核（auth.rs
    // 一行不动）；本 handler 只承载管理性质操作（list_users/update_role/
    // update_tenant/delete_user——内核直接以插件化形态提供这些端点）。
    // 鉴权与 self-service 防护（admin 不能删自己/降自己角色/改自己租户）在
    // handler 内核侧执行（插件仅转发 Authorization 头，见
    // user-admin/src/capability.rs 模块文档）。注册先于任何 sidecar spawn——
    // initialize 握手据此把 user-admin 声明给 user_admin 插件。
    // update_role/update_tenant 的 db 句柄按 driver 注入：非 SQLite driver 为
    // None（handler 诚实降级 400）；list/delete 走 StorageBackend trait（跨 driver）。
    handler_registry.register(std::sync::Arc::new(
        agentos_user_admin::UserAdminCapabilityHandler::new(
            Some(store_dyn.clone()),
            sqlite_db.clone(),
        ),
    ));
    info!(
        target: "agentos-kernel",
        "Registered user-admin capability handler (4 methods, user-management policy layer in-kernel, HTTP face in user_admin plugin)"
    );

    // boot-plugin 第三刀：metrics-admin capability（指标读面留内核，HTTP 面在
    // plugins/shared/metrics_admin 插件）。写面 metrics.record（插件上报指标的
    // 热路径反向调用）仍是 KernelCapabilityRouter 内置 match，不经此 handler。
    // 聚合器与 router/AppState 共享同一实例（Clone 内部 Arc），查询读到实时数据。
    // 鉴权（admin/viewer 读面）在 handler 内核侧执行（插件仅转发 Authorization
    // 头，见 metrics/capability.rs 模块文档）。/metrics（Prometheus 抓取）作为
    // 运维契约保留内核路由，不经插件。
    handler_registry.register(std::sync::Arc::new(
        agentos_api::metrics::MetricsAdminCapabilityHandler::new(
            Some(store_dyn.clone()),
            Some(metrics_aggregator.clone()),
        ),
    ));
    info!(
        target: "agentos-kernel",
        "Registered metrics-admin capability handler (3 methods: query/list/prometheus, read layer in-kernel, HTTP face in metrics_admin plugin)"
    );

    // G3：动态工具注册器——enablement 闸 + 写入注册表（M1 guarded 入 scope，
    // disable 即结构性收回）。动态注册的工具是 state 域数据不落内核存储，
    // 跨重启重建由插件自持 state/config 承担（registry 内存注册机制与
    // capability 面不变）。
    // 信封闸（granted 须含 "registry"）已由 router 入口的 G6 单点校验覆盖。
    // enabled 集合提前构造（后续 AppState 复用同一 Arc）。
    let enabled_plugin_ids: Arc<tokio::sync::RwLock<std::collections::HashSet<String>>> =
        Arc::new(tokio::sync::RwLock::new(
            manifests
                .iter()
                .filter(|m| enablement.is_enabled(&m.id, m.enabled))
                .map(|m| m.id.clone())
                .collect(),
        ));
    let dynamic_registrar: agentos_api::capability_router::DynamicToolRegistrar = {
        let registry_for_dyn = registry.clone();
        let scopes_for_dyn = plugin_scopes.clone();
        let enabled_for_dyn = enabled_plugin_ids.clone();
        Arc::new(
            move |plugin_id: &str, tool: agentos_core::traits::ToolDescriptor| {
                // enablement 闸：disabled 插件不得注册。try_read 竞争失败时宽容放行
                // （注册低频，禁用竞态毫秒级窗口可接受；错误方向是"多注册一次"而非丢注册）。
                if let Ok(ids) = enabled_for_dyn.try_read() {
                    if !ids.contains(plugin_id) {
                        return Err(format!("plugin '{plugin_id}' is disabled (L1 Enabled 闸)"));
                    }
                }
                // 写入注册表（guarded：guard 入 scope，禁用插件时一次性收回）。
                scopes_for_dyn
                    .scope_of(plugin_id)
                    .track(registry_for_dyn.register_tool_guarded(plugin_id, tool.clone()));
                Ok(())
            },
        )
    };

    // 域事件广播闭包：capability_router 收到域事件名单（approval.created）时
    // 投递给声明 domain_event 的启用插件。manifests 用共享 RwLock（AppState
    // 构造后替换为同一副本，watcher 热发现同步可见）。
    let manifests_shared: Arc<tokio::sync::RwLock<Vec<agentos_core::traits::PluginManifest>>> =
        Arc::new(tokio::sync::RwLock::new(manifests.clone()));
    let domain_broadcaster: agentos_api::capability_router::DomainBroadcaster = {
        let inv_for_domain: Arc<dyn agentos_core::traits::PluginInvoker> = invoker.clone();
        let enabled_for_domain = enabled_plugin_ids.clone();
        let manifests_for_domain = manifests_shared.clone();
        Arc::new(
            move |event_name: &str, tags: Vec<(String, serde_json::Value)>| {
                let inv = inv_for_domain.clone();
                let enabled = enabled_for_domain.clone();
                let manifests = manifests_for_domain.clone();
                let name = event_name.to_string();
                tokio::spawn(async move {
                    agentos_api::plugin_lifecycle::broadcast_domain_event_from(
                        &inv, &enabled, &manifests, &name, tags,
                    )
                    .await;
                });
            },
        )
    };

    // 流式声明查询闭包（ADR 2026-08-22）：capability_router 收到流式事件时查
    // 插件 capabilities.streaming 声明（未声明即拒，fail-closed）。manifests 用
    // 共享 RwLock（与 domain_broadcaster 同源）——watcher 热发现同步可见。
    let streaming_declaration_lookup: agentos_api::capability_router::StreamingDeclarationLookupFn = {
        let manifests_for_streaming = manifests_shared.clone();
        Arc::new(move |plugin_id: &str| {
            let guard = manifests_for_streaming.try_read().ok()?;
            guard
                .iter()
                .find(|m| m.id == plugin_id)
                .and_then(|m| m.capabilities.streaming.clone())
        })
    };

    // state 出口声明查询闭包（ADR 2026-08-28 声明化出口）：pipeline-state.list
    // 摘要与 GET /pipelines/state 同源收集 manifest export_fields 并集。
    // 锁被占用（热重载写中）时按无声明降级——仅内核基线出口，不阻塞读面。
    let export_fields_lookup: agentos_api::capability_router::ExportFieldsLookupFn = {
        let manifests_for_export = manifests_shared.clone();
        Arc::new(move || {
            let Ok(guard) = manifests_for_export.try_read() else {
                return agentos_api::routes::ExportFields::default();
            };
            agentos_api::routes::ExportFields::from_manifests(guard.iter())
        })
    };

    // state 持久写面声明查询闭包（ADR 2026-10-02 声明制）：
    // pipeline-state.update 按调用方 _plugin_id 查该插件 manifest 的
    // state.writes；锁被占用（热重载写中）按无声明降级——该次写 fail-closed。
    // 身份=进程身份：独占=manifest.id，合宿=宿主键 `group:<group>:<slot>`
    // ——宿主键按装箱表归约到成员声明并集（否则合宿插件全员被判无写权限）。
    let state_writes_lookup: agentos_api::capability_router::StateWritesLookupFn = {
        let manifests_for_writes = manifests_shared.clone();
        let invoker_for_writes = invoker.clone();
        Arc::new(move |plugin_id: &str| {
            let entries: Vec<(String, Vec<String>)> = manifests_for_writes
                .try_read()
                .map(|guard| {
                    guard
                        .iter()
                        .map(|m| {
                            (
                                m.id.clone(),
                                m.state
                                    .as_ref()
                                    .map(|s| s.writes.clone())
                                    .unwrap_or_default(),
                            )
                        })
                        .collect()
                })
                .unwrap_or_default();
            // 先放 manifests 读锁再查 host_key_of（装箱表内部另有锁，避免锁序反转）
            agentos_api::capability_router::state_writes_for_caller(
                &entries,
                |id| {
                    agentos_core::traits::PluginInvoker::host_key_of(
                        invoker_for_writes.as_ref(),
                        id,
                    )
                },
                plugin_id,
            )
        })
    };

    // 强制注入工具声明查询闭包（P1-5 声明化）：tool-surface 过滤时收集全部
    // 插件 manifest force_include_tools 并集（如 spill_guard 声明 spill_retrieve）。
    // 锁被占用（热重载写中）时按无声明降级——零强制注入，不阻塞读面。
    let force_include_tools_lookup: agentos_api::capability_router::ForceIncludeToolsLookupFn = {
        let manifests_for_forced = manifests_shared.clone();
        Arc::new(move || {
            manifests_for_forced
                .try_read()
                .map(|guard| {
                    guard
                        .iter()
                        .flat_map(|m| m.force_include_tools.iter().cloned())
                        .collect()
                })
                .unwrap_or_default()
        })
    };

    // 工具面遮挡名单查询闭包（BUG-51 注册门禁）：G2 观测失败（账本
    // verify_incomplete）与复验超限终态（verify_failed）插件的工具不进 LLM
    // 工具面（必败面遮挡），复验通过（账本 ok）即自动转正——实时读契约账本
    // 现值，无独立状态需要维护。
    let shadowed_plugin_ids_lookup: agentos_api::capability_router::ShadowedPluginIdsLookupFn = {
        let ledger_for_shadow = contract_states.clone();
        Arc::new(move || ledger_for_shadow.verify_pending_plugin_ids())
    };

    // 管道恢复派发闭包（resume_pipeline 续跑拉起）：经 EngineDispatcher 走与
    // 聊天/催促同一条派发链。dispatcher 构造晚于 router（AppState 之后，见下方
    // chat handler 注册块），经 OnceLock 槽位二阶段接线——未 set 前调用报
    // 显式错误（装配期窗口极短，不静默降级）。
    // 空 content + _skip_user_append overlay：续跑轮不落 user 消息、纯按快照
    // 恢复执行（stage_recover_history 热/冷路径全量恢复 state），源标记 Trigger
    // （系统发起，与任务派发同源）。
    let resume_dispatcher_slot: Arc<
        std::sync::OnceLock<std::sync::Arc<dyn agentos_session::router::PipelineDispatcher>>,
    > = Arc::new(std::sync::OnceLock::new());
    let pipeline_resumer: agentos_api::capability_router::PipelineResumerFn = {
        let slot = resume_dispatcher_slot.clone();
        Arc::new(
            move |pipeline_id: String,
                  thread_id: String,
                  user_id: String,
                  state_overlay: Option<serde_json::Value>| {
                let slot = slot.clone();
                Box::pin(async move {
                    let dispatcher = slot
                        .get()
                        .ok_or_else(|| "恢复派发通道未就绪（dispatcher 尚未装配）".to_string())?;
                    // 调用方 overlay（如任务域清 task.status 终态键）不透明透传，
                    // 并入 _skip_user_append（续跑轮恒不落 user 消息）。
                    let mut overlay = state_overlay.unwrap_or_else(|| serde_json::json!({}));
                    if let Some(obj) = overlay.as_object_mut() {
                        obj.insert("_skip_user_append".into(), serde_json::json!(true));
                    }
                    dispatcher
                        .dispatch_user_input(
                            &thread_id,
                            &user_id,
                            "",
                            &pipeline_id,
                            "",
                            None,
                            Some(&overlay),
                            "",
                            None,
                            "",
                            agentos_core::types::PendingInputSource::Trigger,
                        )
                        .await
                })
                    as std::pin::Pin<
                        Box<dyn std::future::Future<Output = Result<(), String>> + Send>,
                    >
            },
        )
    };

    let mut router_builder = KernelCapabilityRouter::with_metrics(metrics_aggregator.clone())
        .with_invoker(invoker.clone())
        .with_registry(registry.clone())
        // BUG-37 调用路径注册表自愈：反查 miss 时按共享 manifest store 重注册
        // （仅启用插件；禁用/已删插件保持 fail-closed）。
        .with_tool_registry_heal(agentos_api::plugin_lifecycle::tool_registry_heal_fn(
            registry.clone(),
            plugin_scopes.clone(),
            manifests_shared.clone(),
            enabled_plugin_ids.clone(),
        ))
        .with_session(session_coord.clone())
        .with_store(store.clone())
        .with_handler_registry(handler_registry.clone())
        .with_dynamic_tool_registrar(dynamic_registrar.clone())
        .with_domain_broadcaster(domain_broadcaster)
        .with_streaming_declaration_lookup(streaming_declaration_lookup)
        .with_export_fields_lookup(export_fields_lookup)
        .with_state_writes_lookup(state_writes_lookup)
        .with_force_include_tools_lookup(force_include_tools_lookup)
        .with_shadowed_plugin_ids_lookup(shadowed_plugin_ids_lookup)
        .with_pipeline_resumer(pipeline_resumer)
        .with_capability_contracts(capability_contracts.clone());
    // SQLite 固有面：suspend/resume 挂起凭据读写（审批挂起恢复链写侧）。
    if let Some(sqlite) = sqlite_db.as_ref() {
        router_builder = router_builder.with_sqlite(sqlite.clone());
    }
    // C2/C3 刀1：db-read 读契约服务装配——face 注册表装载（fail-closed：YAML
    // 损坏/校验不过拒绝启动；文件缺失 = 空注册表宽松先例）+ HTTP 端点服务安装
    // + capability 臂 handler（执法核心在 db-admin read_faces；非 SQLite driver
    // /:memory: 返回 None，臂与端点诚实降级）。
    let db_read_handler = agentos_db_admin::read_faces::boot_setup(
        &config_root,
        (storage_cfg.driver == "sqlite").then_some(storage_cfg.sqlite_path.as_str()),
        Some(store_dyn.clone()),
    )?;
    if let Some(handler) = db_read_handler.as_ref() {
        router_builder = router_builder.with_db_read(handler.clone());
    }
    let router = Arc::new(
        router_builder
            // 工具连续失败告警器：挂默认实现，统一经 invoke 结果
            // 归一化点计数（见 capability_router handle 的 tool-executor 分支）。
            .with_tool_failure_tracker(Arc::new(
                agentos_api::tools::ConsecutiveFailureTracker::default(),
            )),
    );
    invoker.set_router(router);

    // 监控 M3：注册崩溃回调——invoker 检测到插件崩溃时记时间戳到聚合器（last_crash_ts）。
    // 进程态轮询（memory_commit/uptime/alive/pid）由独立后台任务周期采（见下方 spawn）。
    {
        let agg_for_crash = metrics_aggregator.clone();
        invoker.on_crash(Arc::new(move |plugin_id: &str| {
            let snap = agentos_api::metrics::ProcStateSnapshot {
                plugin_id: plugin_id.to_string(),
                alive: false,
                pid: None,
                memory_commit_bytes: None,
                uptime_secs: None,
                last_crash_ts: Some(agentos_api::metrics::now_secs()),
            };
            agentos_api::metrics::collect_proc_state(&agg_for_crash, &snap);
            info!(target: "agentos-kernel", plugin = plugin_id, "Plugin crash recorded as process.last_crash_ts metric");
        }));
    }

    // 卡3 通知面接线：判定/成员下线/恢复事件 → system_notification 双路投递
    // （内联：活跃管道系统气泡；全局：broadcast_event 广播全部活跃连接——前端
    // handlePluginUnavailableNotice 直订 toast+降级横幅）。判定源共用同一去重器
    // （同类同成员 10 分钟窗口只报一次——fail-soft 组 respawn 重打 member-offline
    // 行不刷屏），两路共享一次去重决策。装机 2026-10-18 补全局跳：组全灭时用户
    // 往往无相关活跃会话，单靠内联路由=零感知（50 分钟零通知实证）。
    let plugin_notice_deduper = agentos_api::plugin_notices::NoticeDeduper::new();
    let emit_plugin_notice: Arc<
        dyn Fn(agentos_api::plugin_notices::PluginUnavailableNotice) + Send + Sync,
    > = {
        let session = session_coord.clone();
        let deduper = plugin_notice_deduper.clone();
        Arc::new(move |notice| {
            let session = session.clone();
            let deduper = deduper.clone();
            tokio::spawn(async move {
                let delivery =
                    agentos_api::plugin_notices::emit_notice(&session, &notice, &deduper).await;
                if !delivery.passed {
                    return; // 去重窗口内重复：静默吞（首报已送达）
                }
                if delivery.threads == 0 && delivery.broadcast == 0 {
                    warn!(
                        target: "agentos-kernel",
                        kind = %notice.kind,
                        members = ?notice.members,
                        "插件通知：无活跃管道且无活跃连接可投递（仅内核日志留痕）"
                    );
                } else {
                    info!(
                        target: "agentos-kernel",
                        kind = %notice.kind,
                        threads = delivery.threads,
                        broadcast = delivery.broadcast,
                        "插件通知已投递（system_notification 内联+全局双路）"
                    );
                }
            });
        })
    };
    // 源③：spawn 恢复（卡3 恢复沿，装机 2026-10-18 补）——熔断宿主被成功
    // spawn 收束时 invoker 判定事件，kind=restored 同通道同去重（恢复通知）。
    {
        let emit = emit_plugin_notice.clone();
        invoker.on_spawn_recovered(Arc::new(
            move |n: &agentos_invoker::SpawnRecoveredNotice| {
                let group = n.host_key.strip_prefix("group:").map(|rest| {
                    rest.rsplit_once(':')
                        .map(|(g, _)| g.to_string())
                        .unwrap_or(rest.to_string())
                });
                emit(agentos_api::plugin_notices::PluginUnavailableNotice {
                    kind: agentos_api::plugin_notices::RESTORED_KIND.to_string(),
                    group,
                    members: n.members.clone(),
                    reason: String::new(),
                    tools_down: 0,
                });
            },
        ));
    }
    // 源①：spawn 熔断跳闸（invoker 卡2 判定事件）。
    {
        let emit = emit_plugin_notice.clone();
        let manifests_for_tools = manifests_shared.clone();
        invoker.on_spawn_breaker(Arc::new(move |n: &agentos_invoker::SpawnBreakerNotice| {
            // 组名从宿主键解析（group:{name}:{slot}）；工具数按成员从声明面合计
            // （store 锁被占时诚实降级 0——通知正文自动略去工具数措辞）。
            let group = n.host_key.strip_prefix("group:").map(|rest| {
                rest.rsplit_once(':')
                    .map(|(g, _)| g.to_string())
                    .unwrap_or(rest.to_string())
            });
            let tools_down = manifests_for_tools
                .try_read()
                .map(|guard| {
                    guard
                        .iter()
                        .filter(|m| n.members.contains(&m.id))
                        .map(|m| m.capabilities.tools.len())
                        .sum()
                })
                .unwrap_or(0);
            emit(agentos_api::plugin_notices::PluginUnavailableNotice {
                kind: "spawn_breaker".to_string(),
                group,
                members: n.members.clone(),
                reason: n.last_error.clone(),
                tools_down,
            });
        }));
    }
    // 源②：合宿成员下线结构性行（host.py fail-soft，卡1 产出 → stderr 观测）。
    {
        let emit = emit_plugin_notice.clone();
        let manifests_for_offline = manifests_shared.clone();
        invoker.on_cohost_member_offline(Arc::new(move |n: &agentos_mcp::MemberOfflineNotice| {
            let tools_down = manifests_for_offline
                .try_read()
                .map(|guard| {
                    guard
                        .iter()
                        .find(|m| m.id == n.plugin)
                        .map(|m| m.capabilities.tools.len())
                        .unwrap_or(0)
                })
                .unwrap_or(0);
            emit(agentos_api::plugin_notices::PluginUnavailableNotice {
                kind: "cohost_member_offline".to_string(),
                group: Some(n.group.clone()),
                members: vec![n.plugin.clone()],
                reason: format!("{}: {}", n.stage, n.error),
                tools_down,
            });
        }));
    }

    // 监控 M3 后半：进程态周期轮询（每 10s）——遍历活宿主（含 light 合宿组）按
    // 成员插件写 process.alive/pid/memory_commit_bytes/uptime_seconds gauge；
    // last_crash_ts 由上方崩溃回调单独写，轮询快照 None 不覆盖。
    let _proc_state_poller = agentos_api::metrics::spawn_proc_state_poller(
        Arc::clone(&invoker),
        metrics_aggregator.clone(),
        std::time::Duration::from_secs(10),
    );

    info!(
        target: "agentos-kernel",
        "Pipeline engine initialized (in-memory SQLite, reverse capability channel + metrics aggregator enabled)"
    );

    // ── 0.2 引擎接线：加载管道配置 + 公共 step 库 + 重名检测 ──
    // （config_root 已在上方 loader 创建前推导，此处复用）

    let pipeline_config = load_pipeline_config(&config_root).unwrap_or_else(|e| {
        warn!(
            "加载管道配置失败，以内置默认配置启动（chat 走降级路径，修复配置后热重载自动生效）: {}: {}",
            config_root.display(),
            e
        );
        agentos_core::types::PipelineConfig::default()
    });
    let step_library = load_step_library(&config_root).unwrap_or_else(|e| {
        warn!(
            "加载公共 step 库失败，以空库启动（修复配置后热重载自动生效）: {}: {}",
            config_root.display(),
            e
        );
        agentos_core::types::StepLibrary::default()
    });

    info!(
        target: "agentos-kernel",
        "Loaded pipeline '{}' with {} steps, step library with {} entries",
        pipeline_config.name,
        pipeline_config.loop_bodies.len(),
        step_library.steps.len()
    );

    // 收集已知插件 id（命中规则③判定 + 重名检测用）
    let plugin_ids: std::collections::HashSet<String> =
        manifests.iter().map(|m| m.id.clone()).collect();

    // 启动期重名检测：冲突不阻断启动（warn 留痕；运行时热重载每次请求重新
    // 校验并在修复后自动生效——配置问题不应让内核整体不可用）。
    if let Err(conflict) = validate_no_name_conflicts(&pipeline_config, &step_library, &plugin_ids)
    {
        warn!("命名冲突检测失败（内核继续启动，修复配置后热重载自动生效）: {conflict}");
    }

    // G10：加载期编译。when 语法错误 / 引用不存在的 step 或插件 / composite
    // 引用环——不阻断启动：warn 留痕，运行时热重载路径（server.rs
    // maybe_reload_compiled_pipeline）指纹驱动（配置 mtime ⊕ manifests 摘要，
    // B6 双维键域）——配置修复/提供方插件注册必改指纹，下个 TTL 窗口自动
    // 重编译；编译产物缺失/失败期间 chat 显式报错拒绝空跑（发送侧可见，
    // 不静默零步完成）。
    match agentos_api::server::load_and_compile(&config_root, &manifests) {
        Ok(compiled) => {
            // autonomous 编译产物预入管道编译缓存（其余管道配置用什么加载什么，
            // 按需加载+编译+缓存，绝不启动期全量扫描）。
            let compiled = Arc::new(compiled);
            agentos_api::server::pipeline_cache_seed("autonomous", Arc::clone(&compiled));
            // boot 后台预热：管道引用的 sidecar 宿主提前 spawn 进缓存，消除
            // "启动后首次消息"的管道链串行冷启动（每宿主 spawn→initialize 秒级，
            // 实测首条消息 41.5s vs 第二条 1.9s）。预热集只含管道引用插件
            // （referenced_plugin_ids 单一来源）——工具/服务插件保持纯懒加载，
            // idle GC 治理不变。预热须在 set_router 之后（spawn 的宿主拿到完整
            // capabilities 声明，反向调用不空快照）。
            agentos_api::sidecar_warmup::spawn_pipeline_sidecar_warmup(
                invoker.clone(),
                compiled,
                enabled_manifests.clone(),
            );
        }
        Err(compile_err) => {
            warn!("管道加载期编译失败（内核继续启动，修复配置后热重载自动生效）: {compile_err}");
        }
    }

    // 构建 AppState（注入 invoker / store / plugin_ids / project_root；管道配置
    // 与 step 库不注入——热路径现读盘，上方启动期加载只服务日志与重名检测）
    let project_root = config_root
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));
    let store_dyn: Arc<dyn agentos_core::traits::StorageBackend> = store.clone();
    let invoker_dyn: Arc<dyn agentos_core::traits::PluginInvoker> = invoker.clone();
    // P3：HTTP 端点 dispatcher 的生产 handler（经 invoker 调插件 http.handle）
    let http_handler: Arc<dyn agentos_core::traits::HttpHandleCapability> = Arc::new(
        agentos_api::http_dispatcher::SidecarHttpHandler::new(invoker_dyn.clone()),
    );
    // L1 启用集合（schema 据此过滤 contributes）——G3 时已提前构造（router 的
    // enablement 闸共享同一 Arc），此处仅取快照喂 with_plugins（其签名收 HashSet）。
    let enabled_snapshot: std::collections::HashSet<String> =
        enabled_plugin_ids.read().await.clone();
    let mut state = AppState::with_plugins(
        manifests.clone(),
        registry,
        invoker_dyn,
        store_dyn,
        project_root,
        enabled_snapshot,
    )
    .with_capability_handlers(handler_registry.clone());
    // 共享 manifests 副本（域事件广播闭包与 watcher 热发现读同一份）。
    state.manifests = manifests_shared.clone();
    // task_01：注入统一数据接口专用 SqliteStore 句柄（/api/v1/db/* 用，表驱动动态枚举）。
    // 与 store_dyn（trait object，业务语义方法）互补；with_db 不改任何持久化方式。
    // with_db 按 driver 注入（sqlite/memory → Some；其它 driver → None，
    // 统一数据接口与 G8 排空的 SQLite 专有路径诚实降级）。
    let state = match sqlite_db.clone() {
        Some(db) => state.with_db(db),
        None => state,
    };
    let state = state.with_http_handler(http_handler);
    // 内核能力契约注入 AppState（/api/v1/schema 聚合透出——消费端同源）。
    let state = state.with_kernel_capability_contracts(capability_contracts.clone());
    // 注入插件目录活真源（loader 活对象句柄），启用静态资源托管
    // （/ext/{plugin_id}/assets/{*path} → <plugin_dir>/web/<path> 直读）。
    // 插件只需在自己的目录下放 web/ 子目录即可被内核自动托管，无需声明
    // http_endpoints；消费点每请求现取活路径表（B1：boot 快照已退役，
    // 热发现插件无需重启即可托管）。
    let state = state.with_plugin_loader(loader_arc.clone());
    // 统一配置加载方案 TDD-4：构造 ConfigCenter 注入 AppState。
    // 后续 loader（agent/pipeline/plugin config_files）经此统一走 load()/load_dir()/store()。
    let state = if let Some(root) = state.project_root.as_ref() {
        let cc = std::sync::Arc::new(agentos_config::config_center::ConfigCenter::new(
            root.join("config"),
        ));
        state.with_config_center(cc)
    } else {
        state
    };
    // ADR §3.5'：插件 widget 绑定表（共享化，M1）。此处先建表注入 AppState，
    // broadcaster 在 session 启用后 spawn（见下方 Metrics 后台任务段）。
    let widget_bindings_shared: agentos_api::metrics::SharedBindings = {
        let entries: Vec<(&str, Option<&serde_json::Value>)> = manifests
            .iter()
            .map(|m| (m.id.as_str(), m.contributes.as_ref()))
            .collect();
        Arc::new(parking_lot::RwLock::new(
            agentos_api::metrics::collect_all_bindings(entries),
        ))
    };
    // M1：每个有绑定的插件登记一条 broadcaster 维度 guard（revoke = 移除其全部绑定，
    // 禁用插件时随 scope 结构性收回）。
    {
        let owners: std::collections::HashSet<String> = widget_bindings_shared
            .read()
            .iter()
            .map(|b| b.owner_plugin_id.clone())
            .collect();
        for owner in owners {
            let shared = Arc::clone(&widget_bindings_shared);
            plugin_scopes
                .scope_of(&owner)
                .track(agentos_core::traits::RegistrationGuard::new(move || {
                    agentos_api::metrics::plugin_widget_broadcast::remove_plugin_bindings(
                        &shared, &owner,
                    );
                }));
        }
    }
    let state = state
        // 监控 M1/M5/M5b：注入指标聚合器（启用 /api/v1/metrics + /metrics 端点）
        .with_metrics(metrics_aggregator.clone())
        // M1：注册账本 + widget 绑定表（disable 结构性收回）
        .with_plugin_scopes(plugin_scopes.clone())
        .with_widget_bindings(Arc::clone(&widget_bindings_shared))
        // 闸2·观测：boot 期 G2 校验结果与 /plugins/contract-status 面板同账本
        // （boot upsert 的 not_covered/derived 行落同一 Arc 实例，面板可见）。
        .with_contract_states(contract_states.clone());
    // P2：启用会话内核（WS 握手鉴权 + 连接注册 + 入站路由 + 断线重放）。
    // 复用 router 已持有的 session_coord（流式 chunk 推送与 WS 出站共享同一 SessionCoordinator）。
    let state = state.enable_session_with(session_coord);

    // chat namespace capability：把"向会话投递消息并跑管道"暴露给 sidecar。
    // 触发器（trigger_setup_tool）到期触发时经 chat.send_message 复用前端同一条
    // WS 派发（dispatch_user_input → process_via_engine）。本 handler 补上注入通道。
    // AppState 在 router 之后构造，故在此（启动末期）注册到既有 handler_registry。
    {
        let dispatcher: std::sync::Arc<dyn agentos_session::router::PipelineDispatcher> =
            std::sync::Arc::new(agentos_api::ws_session::EngineDispatcher::new(
                state.clone(),
            ));
        // 管道恢复派发通道接线（resume_pipeline 续跑拉起，见 router 装配处）。
        let _ = resume_dispatcher_slot.set(dispatcher.clone());
        handler_registry.register(std::sync::Arc::new(
            agentos_api::chat_send_handler::ChatSendHandler::with_session(
                dispatcher,
                state.store.clone(),
                state
                    .session
                    .clone()
                    .expect("session 必须在 chat handler 注册前装配"),
            ),
        ));
        info!(target: "agentos-kernel", "Registered chat.send_message capability (trigger fire path)");
        // 缺陷E（台账 R2）boot 竞态收口：本注册晚于 G2 启动期校验/预热宿主的
        // initialize 握手（设备实测晚 33ms）——握手快照缺 chat，SDK 能力句柄表
        // 终身缺句柄（task_submit get_capability("chat") KeyError，任务链首建
        // 必败；宿主内成员热重载重放陈旧快照，失效面复现）。注册完成后全量
        // 废弃缓存宿主：下次调用按含 chat 的声明面重握手；成员重载路径另由
        // reload_member 请求携带实时声明面兜底（unload.rs / cohost.py）。
        invoker.invalidate_all_sidecars(
            "chat.send_message capability registered (refresh boot-window host handshakes)",
        );
    }

    // 监控 M2/M6：后台任务——每秒把内核自采计数器 flush 到聚合器 + 滚动桶降采样。
    // M6：每秒采样关键指标广播给订阅 statusBar 的连接（widget_event）。
    if let Some(session) = state.session.clone() {
        let agg_flush = metrics_aggregator.clone();
        let kc_flush = kernel_counters.clone();
        let agg_rollup = metrics_aggregator.clone();
        // MetricBroadcaster::spawn 需要 Arc<MetricsAggregator>；MetricsAggregator 内部已
        // 用 Arc<RwLock> 共享，这里再包一层 Arc 满足签名（spawn 内只读快照）。
        let agg_bcast: Arc<agentos_api::metrics::MetricsAggregator> =
            Arc::new(metrics_aggregator.clone());
        let kc_bcast = kernel_counters.clone();
        let session_bcast = session.clone();
        // 卡9：crate 自采静态计数器（mcp sidecar stdout EOF / G2 tools-list
        // 观测失败 / tools-list malformed）的 flush 游标——cumulative → delta
        // 换算见 metrics::counters::CrateStaticCounterFlush。
        let mut crate_static_flush = agentos_api::metrics::CrateStaticCounterFlush::new();
        // R-2：thread 三映射 TTL 清理节拍复用本循环（每 60s 一扫）。
        let session_sweep = session.clone();
        let flush_activity =
            agentos_core::task_activity::global_registry().register("metrics-flush-rollup");
        tokio::spawn(agentos_core::task_activity::scope(
            flush_activity,
            async move {
                let mut tick = tokio::time::interval(std::time::Duration::from_secs(1));
                tick.tick().await; // 跳过首次立即触发
                agentos_core::task_activity::set_current_label("idle: next tick 1s");
                let mut ticks: u64 = 0;
                loop {
                    tick.tick().await;
                    ticks += 1;
                    agentos_core::task_activity::set_current_label("flushing counters + rollup");
                    // M2：内核自采计数器快照 → 聚合器
                    kc_flush.flush_to(&agg_flush);
                    // 卡9：crate 自采静态计数器采样 → 聚合器（EOF/观测失败/
                    // malformed 三键，counter delta 语义同上一行）
                    crate_static_flush.flush(&agg_flush);
                    // R-2：thread 三映射 TTL 兜底回收（每分钟一扫；读路径
                    // 惰性过期为主，无读/写触达的孤儿条目由此收回）。
                    if ticks % 60 == 0 {
                        session_sweep.registry().sweep_expired_threads();
                    }
                    // R-1：MCP 反向调用水位（gauge）。排队/在执是水位非累计，
                    // KernelCounters 的 counter 通道表达不了；按 2026-09-09 死层
                    // 裁决预留的升级路径「gauge 导出走聚合器真接」重立（ADR
                    // 2026-10-10-mcp-reverse-queue）。
                    let (mcp_queued, mcp_active) = agentos_mcp::reverse_call_levels();
                    agg_flush.record(
                        "kernel",
                        "mcp.reverse_queued",
                        MetricType::Gauge,
                        mcp_queued as f64,
                        &Labels::new(),
                        None,
                        Some("MCP 反向调用排队等待数（满载排队制水位）"),
                    );
                    agg_flush.record(
                        "kernel",
                        "mcp.reverse_active",
                        MetricType::Gauge,
                        mcp_active as f64,
                        &Labels::new(),
                        None,
                        Some("MCP 反向调用执行中数（并发闸门在用车位）"),
                    );
                    // M1：滚动桶降采样（1s→10s 合并 + 超 2h 清理）
                    agg_rollup.rollup();
                    agentos_core::task_activity::set_current_label("idle: next tick 1s");
                }
            },
        ));
        // M6：每秒采样关键指标广播（widget_event → 前端 statusBar）
        let _bcast_handle = agentos_api::metrics::MetricBroadcaster::spawn(
            agg_bcast,
            Some(kc_bcast),
            session_bcast,
            std::time::Duration::from_secs(1),
        );
        // ADR §3.5'：插件 widget 配置驱动推送——按 contributes.widgets[].metric_bindings
        // 把插件已上报的指标定时推给前端。插件被动（照常 metrics.record），内核统一编排。
        // M1：绑定表已共享化注入 AppState（禁用插件时从表移除其绑定，guard 已挂 scope）。
        let widget_bindings = match state.widget_bindings.as_ref() {
            Some(b) => Arc::clone(b),
            None => Arc::new(parking_lot::RwLock::new(Vec::new())),
        };
        if !widget_bindings.read().is_empty() {
            let agg_widget: Arc<agentos_api::metrics::MetricsAggregator> =
                Arc::new(metrics_aggregator.clone());
            let session_widget: Arc<dyn agentos_api::metrics::WidgetEmitter> = session.clone();
            let _widget_bcast_handle = agentos_api::metrics::PluginWidgetBroadcaster::spawn(
                agg_widget,
                Arc::clone(&widget_bindings),
                session_widget,
            );
            info!(
                target: "agentos-kernel",
                count = widget_bindings.read().len(),
                "PluginWidgetBroadcaster started ({} metric_bindings)",
                widget_bindings.read().len()
            );
        }
        info!(target: "agentos-kernel", "Metrics background tasks started (M2 flush + M1 rollup + M6 broadcast, 1s interval)");
    }

    // 插件运行时自动发现（notify watch + 轮询兜底）：往 plugins/ 丢新插件目录即生效，
    // 无需重启内核、无需手动调 reload-all。复用启动期已构造的 invoker / registry；
    // initial_ids 取启动 manifests，避免把已注册插件重复注册（与 reload-all 新插件序列对齐）。
    // 注入 enablement profile：热发现路径同样按 L1 过滤 disabled 插件（注册闸对齐启动期）。
    //
    // 关键前置：discover_new_plugins 内部读 AGENTOS_PLUGINS_DIR 推导 roots。启动期若走
    // 默认 plugins_dir（未设该环境变量），须在此补设，保证 watcher 监听目录与 invoker
    // 发现目录同源——否则 watcher 触发同步、discover 却读到空 roots、发现不到新插件。
    if std::env::var("AGENTOS_PLUGINS_DIR").is_err() {
        std::env::set_var("AGENTOS_PLUGINS_DIR", &plugins_dir);
        info!(
            target: "agentos-kernel",
            "AGENTOS_PLUGINS_DIR unset; defaulting to {} for hot-discover",
            plugins_dir.display()
        );
    }
    {
        let watcher_invoker: Arc<dyn agentos_core::traits::PluginInvoker> =
            state.invoker.clone().expect("invoker present at boot");
        let watcher_registry: Arc<CapabilityRegistryImpl> = state
            .capability_registry
            .clone()
            .expect("capability_registry present at boot");
        let initial_ids: std::collections::HashSet<String> =
            manifests.iter().map(|m| m.id.clone()).collect();
        // A3：cdylib 集合基线（boot manifests 的 InProcess id）——watcher 首轮
        // sync 即可 diff，能捕捉 boot→首轮 sync 窗口内的装/卸。
        let initial_cdylib_ids: std::collections::HashSet<String> = manifests
            .iter()
            .filter(|m| m.host_type == agentos_core::traits::HostType::InProcess)
            .map(|m| m.id.clone())
            .collect();
        // A3：cdylib 集合变更重启回调——复用 G8 排空+退出（在途 runs → suspended
        // 后 exit 75，监督者拉起新进程）；AGENTOS_DISABLE_SELF_EXIT=1 逃生门在
        // 函数内部生效（只排空不退出）。watcher 经 env 开关
        // AGENTOS_AUTO_RESTART_ON_CDYLIB_CHANGE（默认开，0 关）自行把关。
        let hook_db = state.db.clone();
        let hook_invoker = state.invoker.clone();
        let restart_hook: Arc<dyn Fn() + Send + Sync> = Arc::new(move || {
            let db = hook_db.clone();
            let invoker = hook_invoker.clone();
            tokio::spawn(async move {
                agentos_api::routes::drain_and_exit75(
                    db.as_ref(),
                    invoker,
                    "plugin_watcher: InProcess(cdylib) plugin set changed",
                )
                .await;
            });
        });
        let _watcher_handle = agentos_api::plugin_watcher::PluginWatcher::new(
            plugins_dir.clone(),
            watcher_invoker,
            watcher_registry,
            initial_ids,
        )
        .with_scopes(plugin_scopes.clone())
        .with_initial_cdylib_ids(initial_cdylib_ids)
        .with_restart_hook(restart_hook)
        // 代码指纹复验：sidecar 实现修复（.py 改动不改 manifest）后，watcher
        // 轮询即可复验恢复被 G2 净化剔除的工具——目录解析与 invoker respawn
        // 判据同源（同一 loader 发现结果 + 同一指纹函数）。
        .with_code_dir_resolver({
            let fp_invoker = invoker.clone();
            std::sync::Arc::new(move |plugin_id: &str| fp_invoker.plugin_source_dir(plugin_id))
        })
        .with_manifests_store(state.manifests.clone())
        .with_enabled_ids(state.enabled_plugin_ids.clone())
        .with_enablement(enablement.clone())
        // enablement 每次 sync 从盘上 profile 现读——boot 快照看不到
        // 运行期 PUT enabled 的写盘结果，卸载→重装按旧快照会把已禁用插件重新
        // 注册（运行期禁用被静默撤销）。
        .with_profile_reload(config_root.clone())
        // 闸2·观测：热发现校验结果收口进同一账本（新插件契约状态写入，
        // 与 boot/validate-all 面板数据同源）。
        .with_contract_states(contract_states.clone())
        // 卡3 源③：G2 复验超限落 verify_failed（判定「插件当前不可用」）→
        // 通知面（与 spawn 熔断/成员下线共用同一去重器与投递通道）。
        .with_unavailable_notifier({
            let emit = emit_plugin_notice.clone();
            Arc::new(
                move |notice: &agentos_api::plugin_notices::PluginUnavailableNotice| {
                    emit(notice.clone());
                },
            )
        })
        .spawn();
        info!(target: "agentos-kernel", "Plugin hot-discover watcher spawned (notify + polling fallback; cdylib change -> G8 auto-restart)");
    }

    // 内存维护（2026-09-09 零页普查：旧进程 860MB Private 中 557MB 为全零页）：
    // purge_delay=0 只归还"整体变空的页"，多 arena 成长后空块散布在各页内，
    // 需周期性 mi_collect 强制扫描收缩空闲 arena。
    // 清扫节奏自适应（ADR 2026-10-19 D3 修订）：WARN 阈值触发期间采样与
    // mi_collect 一并收紧到 AGENTOS_COLLECT_FAST_SECS（默认 10s），解除恢复
    // 常规节奏——10-10 方案 P1「超阈值才升级 force collect」伏笔的落地
    // （回落段证据：生成器一停 7 分钟排空 14GB，收紧节奏即提前触发排空）。
    // M1 测量面：同一循环内按可配周期（agentos-memory collect_interval，env
    // AGENTOS_MEM_COLLECT_SECS）采集 mimalloc 统计快照打日志（先采样后
    // collect——collect 会收缩 committed，若先 collect 再采样会掩盖滞留自然值），
    // 增量判活增长 vs 滞留由消费方对两次快照相减（mimalloc 无增量 delta API）。
    // collect 在每个采样点执行一次，把空闲线程退休留下的悬空页同周期结算返还。
    // 注意：libmimalloc-sys release 构建下 MI_STAT=0，malloc 计量面
    // （live_mb/allocs）恒 0，可信信号是 committed/rss/commit/purged——
    // 见 agentos_api::allocator::MemStats 文档。
    {
        // D3 自适应节奏：常规 = collect_interval（env AGENTOS_MEM_COLLECT_SECS，
        // 默认 60s）；WARN 触发期收紧到 collect_fast_secs（0=禁用收紧）。
        // P1 观测护栏阈值（方案 §3-P1）：WARN 告警与 D3 收紧触发判定同源共用。
        let base_secs = pool.collect_interval.as_secs();
        const MEM_WARN_COMMIT_MB: u64 = 8192;
        const MEM_WARN_ABANDONED_PAGES: u64 = 50_000;
        // D5 执法序列第一级（告警进通知中心）的投递句柄：与插件不可用通知
        // 共用同一 emit 闭包（内联+全局双路 + 同一去重器）。
        let emit_pressure = emit_plugin_notice.clone();
        let maint_activity =
            agentos_core::task_activity::global_registry().register("memory-maintenance");
        tokio::spawn(agentos_core::task_activity::scope(
            maint_activity,
            async move {
                // tokio interval 周期固定，双节奏自适应改 sleep 轮转：首轮
                // sleep(0)=立即采样（与 interval 首拍立即触发一致），此后每轮
                // 按触发态重算下一轮等待。
                let mut wait = std::time::Duration::ZERO;
                let mut fast_active = false;
                // D5 告警边沿状态机（进入/退出各发一条，持续态不重复）。
                let mut pressure_alert = PressureAlertEdge::default();
                loop {
                    tokio::time::sleep(wait).await;
                    agentos_core::task_activity::set_current_label(
                        "sampling mimalloc stats + mi_collect",
                    );
                    let stats = agentos_api::allocator::snapshot_stats();
                    info!(
                        target: "agentos-kernel",
                        committed_mb = stats.committed_bytes.map(|b| b / (1024 * 1024)),
                        rss_mb = stats.process_rss_bytes.map(|b| b / (1024 * 1024)),
                        commit_mb = stats.process_commit_bytes.map(|b| b / (1024 * 1024)),
                        purged_mb = stats.purged_bytes.map(|b| b / (1024 * 1024)),
                        reserved_mb = stats.reserved_bytes.map(|b| b / (1024 * 1024)),
                        abandoned_pages = stats.abandoned_pages,
                        live_mb = stats.in_use_bytes.map(|b| b / (1024 * 1024)),
                        allocs = stats.total_allocs,
                        "mimalloc stats snapshot"
                    );
                    // 全家进程 WorkingSet 快照（父 electron + 全部 sidecar 直接
                    // 子进程）——与 mimalloc 堆快照同 tick 同时间戳，进程级归因
                    // 与堆剖析在同一对日志行内完成；非 Windows 平台为空快照。
                    let family = agentos_api::family_watch::capture();
                    info!(
                        target: "agentos-kernel",
                        parent_rss_mb = family.parent_rss_mb,
                        sidecar_count = family.sidecars.len(),
                        sidecar_total_mb = family.sidecars_total_mb(),
                        sidecar_max_mb = family.sidecars_max_mb(),
                        sidecar_detail = family
                            .sidecars
                            .iter()
                            .map(|p| format!("{}:{}:{}MB", p.pid, p.name, p.rss_mb))
                            .collect::<Vec<_>>()
                            .join(","),
                        "family rss snapshot"
                    );
                    // P1 观测护栏（方案 §3-P1）：健康态 commit <600MB / abandoned
                    // <2k 页；阈值=事故态的 1/8 量级，给"整机卡顿"留 40 分钟级提前量。
                    if stats
                        .process_commit_bytes
                        .is_some_and(|b| b / (1024 * 1024) > MEM_WARN_COMMIT_MB)
                    {
                        warn!(
                            target: "agentos-kernel",
                            commit_mb = stats.process_commit_bytes.map(|b| b / (1024 * 1024)),
                            abandoned_pages = stats.abandoned_pages,
                            "mimalloc commit 超 8GB 阈值（历史健康 <600MB）——abandoned 页滞留嫌疑，检查线程 churn 与宿主重生风暴"
                        );
                    }
                    if stats
                        .abandoned_pages
                        .is_some_and(|n| n > MEM_WARN_ABANDONED_PAGES)
                    {
                        warn!(
                            target: "agentos-kernel",
                            abandoned_pages = stats.abandoned_pages,
                            "mimalloc abandoned 页超 50k（历史健康 <2k）——属主死亡悬空页累积"
                        );
                    }
                    // D3 收紧触发判定（与 WARN 同阈值）：状态翻转才打 INFO 防
                    // 刷屏；禁用（fast=0）时触发永不生效、恒常规节奏、无翻转
                    // 日志。收紧只影响本循环频率，绝不触发重启（区别于水位/硬限）。
                    // D5 告警消费同一判定（warn_triggered 本体，与收紧的启用
                    // 开关解耦——收紧可禁用，告警不随禁）。
                    let warn_triggered = collect_fast_triggered(
                        stats.process_commit_bytes.map(|b| b / (1024 * 1024)),
                        stats.abandoned_pages,
                        MEM_WARN_COMMIT_MB,
                        MEM_WARN_ABANDONED_PAGES,
                    );
                    let next_fast = warn_triggered && collect_fast_secs > 0;
                    if next_fast != fast_active {
                        fast_active = next_fast;
                        if next_fast {
                            info!(
                                target: "agentos-kernel",
                                collect_interval_secs = collect_fast_secs,
                                "WARN 阈值触发：采样与 mi_collect 收紧至快速节奏（只影响清扫频率）"
                            );
                        } else {
                            info!(
                                target: "agentos-kernel",
                                collect_interval_secs = base_secs,
                                "WARN 阈值解除：采样与 mi_collect 恢复常规节奏"
                            );
                        }
                    }
                    // D5 执法序列第一级：告警进通知中心（不再是仅内核日志）。
                    // 与 WARN 同源判定，边沿触发——进入/退出各发一条，持续态
                    // 不重复发（G3b 翻转日志同哲学）；同类事件 10 分钟去重窗
                    // 顺带抑制快速翻转刷屏。emit 内部 fire-and-forget，不拖慢
                    // 采样循环。
                    if let Some(entering) = pressure_alert.transition(warn_triggered) {
                        let commit_mb = stats
                            .process_commit_bytes
                            .map(|b| b / (1024 * 1024))
                            .map(|v| v.to_string())
                            .unwrap_or_else(|| "采样缺数".to_string());
                        let abandoned = stats
                            .abandoned_pages
                            .map(|v| v.to_string())
                            .unwrap_or_else(|| "采样缺数".to_string());
                        let detail = if entering {
                            format!(
                                "进程 commit {commit_mb}MB（WARN 阈值 {MEM_WARN_COMMIT_MB}MB）、\
                                 abandoned {abandoned} 页（阈值 {MEM_WARN_ABANDONED_PAGES}）超限——\
                                 宿主重生风暴或线程 churn 嫌疑；已收紧清扫节奏，\
                                 超预算时新增宿主 spawn 将被拒绝"
                            )
                        } else {
                            format!(
                                "进程 commit {commit_mb}MB、abandoned {abandoned} 页已回落 \
                                 WARN 阈值下，恢复常规节奏"
                            )
                        };
                        emit_pressure(agentos_api::plugin_notices::mem_pressure_notice(
                            !entering, detail,
                        ));
                    }
                    // 返还责任兜底：每个采样点主动 force collect——把"页空了也
                    // 无人触发 purge"的返还链从被动等属主/认领改为周期主动清扫；
                    // 与 allocator 的 page_reclaim_on_free=1 合围（退休页在 free
                    // 时即认领，未被认领的由本 collect 结算）。
                    agentos_api::allocator::collect_now();
                    let next_secs = if fast_active {
                        collect_fast_secs
                    } else {
                        base_secs
                    };
                    agentos_core::task_activity::set_current_label(format!(
                        "idle: next tick {next_secs}s"
                    ));
                    wait = std::time::Duration::from_secs(next_secs);
                }
            },
        ));
    }

    // 内存水位监控（可选自愈兜底，**默认关闭**——2026-09-10 用户裁定：水位
    // 重启可能打断正常业务）：显式设置 AGENTOS_MEM_WATERMARK_MB 或
    // AGENTOS_MEM_HARD_LIMIT_MB 为正值才启用；启用后每 60s 采样自身 Private
    // Bytes，连续 N 次超阈值 → 走与 G8 cdylib 重启相同的 drain_and_exit75
    // 排空路径（exit 75，监督脚本拉起新进程）。
    // AGENTOS_MEM_WATERMARK_MIN=持续分钟数（默认 10；采样间隔固定 60s）。
    // 判定阈值 = min(水位阈值(如设), 95%×硬限(如设))——排水契约见硬限挂载处。
    {
        let watermark_db = state.db.clone();
        let watermark_invoker = state.invoker.clone();
        let hard_limit_mb = mem_hard_limit_mb;
        let wm_activity =
            agentos_core::task_activity::global_registry().register("memory-watermark-watch");
        tokio::spawn(agentos_core::task_activity::scope(
            wm_activity,
            async move {
                agentos_core::task_activity::set_current_label("resolving config");
                const SAMPLE_INTERVAL_SECS: u64 = 60;
                let watermark_cfg = resolve_watermark_config();
                let Some(threshold_mb) =
                    effective_sample_threshold_mb(watermark_cfg.map(|(mb, _)| mb), hard_limit_mb)
                else {
                    info!(
                        target: "agentos-kernel",
                        env_watermark = "AGENTOS_MEM_WATERMARK_MB",
                        env_hard_limit = "AGENTOS_MEM_HARD_LIMIT_MB",
                        "内存水位监控默认关闭（不会触发重启）；设任一 env 为正值 MB 可启用自愈"
                    );
                    agentos_core::task_activity::set_current_label("disabled");
                    return;
                };
                let sustain_minutes = watermark_cfg.map(|(_, m)| m).unwrap_or(10);
                let consecutive = ((sustain_minutes * 60) / SAMPLE_INTERVAL_SECS).max(1) as usize;
                info!(
                    target: "agentos-kernel",
                    threshold_mb,
                    hard_limit_mb = hard_limit_mb.unwrap_or(0),
                    consecutive,
                    sample_interval_secs = SAMPLE_INTERVAL_SECS,
                    "内存水位监控已启动（持续超限即排空重启自愈；阈值压在硬限之下，排水先于硬熔断）"
                );
                let mut recent: Vec<u64> = Vec::with_capacity(consecutive);
                let mut tick =
                    tokio::time::interval(std::time::Duration::from_secs(SAMPLE_INTERVAL_SECS));
                agentos_core::task_activity::set_current_label("idle: next tick 60s");
                loop {
                    tick.tick().await;
                    agentos_core::task_activity::set_current_label("sampling private bytes");
                    let Some(usage_bytes) = sample_private_bytes() else {
                        agentos_core::task_activity::set_current_label("idle: next tick 60s");
                        continue; // 采样不可用（非 Windows/API 失败）：缺数不判，绝不误自愈
                    };
                    recent.push(usage_bytes / (1024 * 1024));
                    // 滑动窗口只留判定所需样本，长运行监控自身不积内存
                    let keep_from = recent.len().saturating_sub(consecutive);
                    recent.drain(..keep_from);
                    if watermark_breach(&recent, threshold_mb, consecutive) {
                        warn!(
                            target: "agentos-kernel",
                            threshold_mb,
                            consecutive,
                            latest_mb = recent.last().copied().unwrap_or(0),
                            "内存水位持续超限（自愈）：排空在途 runs 后 exit 75，监督脚本拉起新进程"
                        );
                        agentos_api::routes::drain_and_exit75(
                            watermark_db.as_ref(),
                            watermark_invoker.clone(),
                            "memory watermark self-heal: private bytes 持续超阈值",
                        )
                        .await;
                        // 逃生门（AGENTOS_DISABLE_SELF_EXIT=1）下 drain 只排空不退出：
                        // 停止本监控，避免每 60s 重复排空；进程交人工处置。
                        break;
                    }
                    agentos_core::task_activity::set_current_label("idle: next tick 60s");
                }
            },
        ));
    }

    // boot 航点日志（2026-11-01 真机 boot 回归排查插桩）：本行与下方
    // 「API server starting」之间的任何静默 = 卡在 start_server 内部
    // （build_router/bind）；本行缺失 = 卡在上方启动序列。两侧锚点把
    // boot 尾段一分为二，设备日志单轮即可定位卡段。
    info!(
        target: "agentos-kernel",
        "boot sequence complete; starting HTTP server (bind {addr})"
    );
    start_server(addr, state).await?;

    Ok(())
}
#[cfg(test)]
mod tests;
