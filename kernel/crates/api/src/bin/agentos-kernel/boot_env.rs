//! 引导环境解析与管理员用户面（agentos-kernel 子模块）：admin 口令解析/
//! 种子、明文口令迁移、绑定地址/日志目录/运行时线程与水位解析、用户插件目录。
use std::path::PathBuf;
use std::sync::Arc;

use agentos_core::types::UserRecord;
use tracing::{info, warn};

/// boot 静默段阶段日志（2026-11-20 启动优化 A）：装机 768MB 库上 DB 打开/
/// 迁移/清扫各步秒级~分钟级且此前零日志（2026-11-20 02:08Z boot 实测 27s
/// 零日志不可归因）。各步经 [`run_boot_stage`]/[`run_boot_stage_async`] 落
/// start/done+elapsed info!（target agentos-kernel），下次慢 boot 日志直读
/// 可归因。
pub fn log_stage_start(stage: &'static str) {
    info!(target: "agentos-kernel", stage, "boot stage start");
}

pub fn log_stage_done(stage: &'static str, started: std::time::Instant) {
    info!(
        target: "agentos-kernel",
        stage,
        elapsed_ms = started.elapsed().as_millis() as u64,
        "boot stage done"
    );
}

/// 同步 boot 阶段包装：落 start/done 日志，`op` 结果（含错误值）原样透传，
/// 本包装不做任何错误处理。
pub fn run_boot_stage<T>(stage: &'static str, op: impl FnOnce() -> T) -> T {
    log_stage_start(stage);
    let started = std::time::Instant::now();
    let out = op();
    log_stage_done(stage, started);
    out
}

/// 异步 boot 阶段包装：await 前后落 start/done 日志，future 输出原样透传。
pub async fn run_boot_stage_async<T>(
    stage: &'static str,
    fut: impl std::future::Future<Output = T>,
) -> T {
    log_stage_start(stage);
    let started = std::time::Instant::now();
    let out = fut.await;
    log_stage_done(stage, started);
    out
}

/// 管理员初始口令来源（D1）：
/// - 环境变量 `AGENTOS_ADMIN_PASSWORD` 非空 → 显式指定；
/// - 未设置 → 生成随机口令（128 bit uuid 对），仅在首次播种时打印一次到控制台。
///
/// 不存在任何硬编码默认口令。
pub fn resolve_admin_password() -> (String, bool) {
    match std::env::var("AGENTOS_ADMIN_PASSWORD") {
        Ok(p) if !p.trim().is_empty() => (p, false),
        _ => {
            let random = format!(
                "{}{}",
                uuid::Uuid::new_v4().simple(),
                uuid::Uuid::new_v4().simple()
            );
            (random, true)
        }
    }
}

/// 播种内置 admin 用户（首次启动插入，已存在则跳过；显式环境变量可重置）。
///
/// tenant_id = "default"（与多租户隔离地基的默认租户一致），保证旧数据
/// （0.5.0 前以 default 写入的会话/消息）仍归 admin 可见。
///
/// D1：口令 argon2id 哈希落库。`must_change_password` 只对随机生成的口令
/// 置位（console 一次性打印、无人持久持有 → 首登强制改密）；来自
/// `AGENTOS_ADMIN_PASSWORD` 的口令是操作员/launcher 自选的事实源（装机版
/// 自动登录契约，ADR 2026-10-02-packaged-auto-login-env-only），不置改密
/// 标记。对已存在的 admin，若环境变量与当前口令不符即重置（操作员恢复
/// 通道：迁移失败/口令遗失时设置该变量重启）。
pub async fn seed_admin_user(store: Arc<dyn agentos_core::traits::StorageBackend>) {
    const ADMIN_ID: &str = "00000000-0000-0000-0000-000000000001";
    let (password, generated) = resolve_admin_password();
    match store.get_user_by_id(ADMIN_ID).await {
        Ok(Some(existing)) => {
            // admin 已存在。环境变量提供且与当前口令不符 → 显式重置。
            if !generated && !agentos_http::auth::verify_password(&password, &existing.password) {
                let password_hash =
                    agentos_http::auth::hash_password(&password).unwrap_or_else(|e| {
                        panic!("AGENTOS_ADMIN_PASSWORD 重置口令哈希失败（fail-closed）: {e}")
                    });
                match store
                    .update_user_password(ADMIN_ID, &password_hash, false)
                    .await
                {
                    Ok(true) => warn!(
                        target: "agentos-kernel",
                        "已按 AGENTOS_ADMIN_PASSWORD 重置内置 admin 口令（旧会话全部失效）"
                    ),
                    Ok(false) => {
                        warn!(target: "agentos-kernel", "重置 admin 口令失败：用户行不存在")
                    }
                    Err(e) => warn!(target: "agentos-kernel", error = %e, "重置 admin 口令失败"),
                }
            }
        }
        Ok(None) => {
            // 首次启动：插入 admin 种子用户。
            // get_user_by_id 按 task_local tenant（此处为空→default）查，admin 的
            // tenant_id 正是 default，所以 None 表示确实没播过种。
            let password_hash = agentos_http::auth::hash_password(&password)
                .unwrap_or_else(|e| panic!("播种口令哈希失败（fail-closed）: {e}"));
            let now = chrono::Utc::now().to_rfc3339();
            let admin = UserRecord {
                user_id: ADMIN_ID.to_string(),
                username: "admin".to_string(),
                password: password_hash,
                email: Some("admin@agentos.dev".to_string()),
                role: "admin".to_string(),
                tenant_id: "default".to_string(),
                created_at: now,
                last_login_at: None,
                must_change_password: generated,
            };
            match store.create_user(&admin).await {
                Ok(()) => {
                    if generated {
                        // 随机口令仅首启打印一次（控制台直出，不经日志管线过滤）
                        println!("============================================================");
                        println!("  已播种内置 admin 用户 (tenant=default)");
                        println!("  本次初始口令（仅此一次显示，请立即登录并修改）:");
                        println!("    {password}");
                        println!("============================================================");
                    } else {
                        info!(target: "agentos-kernel",
                            "已播种内置 admin 用户 (tenant=default)，初始口令取自 AGENTOS_ADMIN_PASSWORD")
                    }
                }
                Err(e) => {
                    warn!(target: "agentos-kernel", error = %e, "播种 admin 用户失败（DB 查询将查不到 admin，登录不可用）")
                }
            }
        }
        Err(e) => {
            warn!(target: "agentos-kernel", error = %e, "查询 admin 用户失败，跳过播种")
        }
    }
}

/// 启动迁移：存量明文口令行 argon2id 哈希化回写（D1-2）。
///
/// 判据：password 列非 `$argon2` 前缀即视为待迁移明文。任一行哈希或回写失败
/// → Err 拒绝启动（fail-closed，不留明文兼容路径）——操作员按提示设置
/// `AGENTOS_ADMIN_PASSWORD` 重启重置，或修复库后重试。
pub async fn migrate_plaintext_passwords(
    store: Arc<dyn agentos_core::traits::StorageBackend>,
) -> Result<(), String> {
    let users = store
        .list_users()
        .await
        .map_err(|e| format!("迁移前读取用户表失败: {e}"))?;
    let mut migrated = 0usize;
    for u in users {
        if agentos_http::auth::is_password_hash(&u.password) {
            continue;
        }
        let password_hash = agentos_http::auth::hash_password(&u.password)?;
        let updated = store
            .update_user_password(&u.user_id, &password_hash, u.must_change_password)
            .await
            .map_err(|e| format!("用户 {} 口令哈希回写失败: {e}", u.username))?;
        if !updated {
            return Err(format!("用户 {} 口令回写未命中（行消失？）", u.username));
        }
        migrated += 1;
        warn!(
            target: "agentos-kernel",
            user = %u.username,
            "存量明文口令已哈希化回写（D1 启动迁移）"
        );
    }
    if migrated > 0 {
        info!(target: "agentos-kernel", migrated, "明文口令迁移完成");
    }
    Ok(())
}

/// 解析监听地址（D12）：默认 127.0.0.1，仅本机可达；
/// `AGENTOS_BIND=0.0.0.0` 显式开启外网可达（打印绑定面告警）。
/// `AGENTOS_KERNEL_HOST` 为弃用中的过渡别名。
pub fn resolve_bind_host() -> String {
    match std::env::var("AGENTOS_BIND") {
        Ok(h) if !h.trim().is_empty() => h,
        _ => match std::env::var("AGENTOS_KERNEL_HOST") {
            Ok(h) if !h.trim().is_empty() => {
                warn!(
                    target: "agentos-kernel",
                    "AGENTOS_KERNEL_HOST 已弃用，请改用 AGENTOS_BIND（本进程按其值继续启动）"
                );
                h
            }
            _ => "127.0.0.1".to_string(),
        },
    }
}

/// 日志目录解析（装机态用户空间化，ADR 2026-09-28-venv-user-space-provisioning）。
///
/// 解析序：① CWD 相对 `logs` 可写（dev 常态）→ 原样相对路径（dev 行为零变化）；
/// ② 不可写（装机态 CWD=只读安装目录，appender 建目录/写 IO 必败且静默）→
/// `<user_root>/logs`（用户空间，venv 同源惯例；目录在此确保创建）；③ 用户根
/// 不可用或创建失败 → 回落相对 `logs`（行为不劣于现状：构建失败走既有 stdout
/// 降级 + stderr 告警）。
pub fn resolve_log_dir(cwd_logs_writable: bool, user_root: Option<&std::path::Path>) -> PathBuf {
    if cwd_logs_writable {
        return PathBuf::from("logs");
    }
    if let Some(root) = user_root {
        let dir = root.join("logs");
        if std::fs::create_dir_all(&dir).is_ok() {
            return dir;
        }
    }
    PathBuf::from("logs")
}

/// 手机档 tokio worker 线程数上界（原则 P2 资源参数自适应：低端不过载——
/// worker 收窄留核给系统 UI 与 sidecar 宿主；桌面/云档不收窄）。
const PHONE_WORKER_THREADS_MAX: usize = 4;

/// 档位 → tokio worker 线程数（纯函数；env 钉值外的默认派生）。
///
/// - Phone：`min(cores, 4)`，下限 1——显式钉档 phone 而核数探测不可得
///   （cores=0）时不得产出零 worker；
/// - Desktop/Cloud：None = 调用方不设 worker_threads（tokio 默认 = 核数，
///   与收窄前行为逐位一致）。
pub fn worker_threads_for_tier(
    tier: agentos_core::device_profile::DeviceTier,
    cores: u32,
) -> Option<usize> {
    match tier {
        agentos_core::device_profile::DeviceTier::Phone => {
            Some((cores as usize).clamp(1, PHONE_WORKER_THREADS_MAX))
        }
        agentos_core::device_profile::DeviceTier::Desktop
        | agentos_core::device_profile::DeviceTier::Cloud => None,
    }
}

/// 解析核心（纯函数，测试注入 env/档位/核数）：env 显式钉值 > 档位派生
/// （与 invoker `light_host_max_members` 同构的三级序，本函数承载后两级）。
pub fn worker_threads_from(
    env: Option<&str>,
    tier: agentos_core::device_profile::DeviceTier,
    cores: u32,
) -> Option<usize> {
    env.and_then(|v| v.trim().parse::<usize>().ok())
        .filter(|&n| n > 0)
        .map(Some)
        .unwrap_or_else(|| worker_threads_for_tier(tier, cores))
}

/// tokio worker 线程数（线程治理）：`AGENTOS_WORKER_THREADS` 显式钉值优先
/// （未设置/解析失败/非正值按缺席）；未钉值按设备档位派生（原则 P2：手机档
/// `min(核数, 4)` 收窄，桌面/云维持 tokio 默认 = 核数）。档位探测与核数
/// 取 `agentos_core::device_profile` 同源。
pub fn resolve_worker_threads() -> Option<usize> {
    worker_threads_from(
        std::env::var("AGENTOS_WORKER_THREADS").ok().as_deref(),
        agentos_core::device_profile::detect_device_tier(),
        agentos_core::device_profile::probe_cores(),
    )
}

/// 水位自愈配置（MB 阈值, 持续分钟数）。**默认关闭**（2026-09-10 用户裁定：
/// 水位重启可能打断正常业务，不得默认触发）——显式设置 `AGENTOS_MEM_WATERMARK_MB`
/// 为正值才启用；未设置 / 0 / 非法值 = 不启动监控。`AGENTOS_MEM_WATERMARK_MIN`
/// 仅在启用时生效（默认 10 分钟）。2026-11-11 起阈值口径 = 进程 Working Set
/// （实际占用），提交内存尖峰不触发。
pub fn resolve_watermark_config() -> Option<(u64, u64)> {
    let threshold_mb = std::env::var("AGENTOS_MEM_WATERMARK_MB")
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|&v| v > 0)?;
    let sustain_minutes = std::env::var("AGENTOS_MEM_WATERMARK_MIN")
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|&v| v > 0)
        .unwrap_or(10);
    Some((threshold_mb, sustain_minutes))
}

/// 水位判定（纯函数）：最近 `consecutive` 个采样全部**严格大于** `threshold_mb`
/// 才判突破（瞬时尖峰不触发，恰好等于阈值不算超）。样本不足 `consecutive`
/// 个一律不判（含 `consecutive == 0` 防御）。
pub fn watermark_breach(samples: &[u64], threshold_mb: u64, consecutive: usize) -> bool {
    if consecutive == 0 || samples.len() < consecutive {
        return false;
    }
    samples[samples.len() - consecutive..]
        .iter()
        .all(|&mb| mb > threshold_mb)
}

/// 内存硬限（MB）。**默认关闭**（2026-09-10「显式正值才启用」裁定的同语义
/// 延伸，ADR 2026-10-19 D4）：未设置 / 0 / 非法值 = 不挂 Job Object。
pub fn resolve_mem_hard_limit_mb() -> Option<u64> {
    std::env::var("AGENTOS_MEM_HARD_LIMIT_MB")
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|&v| v > 0)
}

/// 排水安全垫：阈值 = 95% × 硬限，整数运算（`mb - max(mb/20, 1)`）避浮点
/// 非精确；至少扣 1MB，防极小配置下阈值==硬限（阈值必须严格低于硬限，
/// 保证优雅排空先于硬熔断，见 [`effective_sample_threshold_mb`]）。
pub fn hard_limit_drain_threshold_mb(hard_limit_mb: u64) -> u64 {
    hard_limit_mb.saturating_sub((hard_limit_mb / 20).max(1))
}

/// 采样判定阈值组合（纯函数，排水契约）：优雅排空（exit 75）必须先于 Job
/// 硬熔断（分配失败=进程暴毙）发生——硬限在席时阈值一律压到硬限之下。
/// - 仅水位 → 水位阈值；
/// - 仅硬限 → 95% × 硬限；
/// - 双设 → min（取小者先触发，两道防线互不越位）；
/// - 都无 → None（监控关闭）。
pub fn effective_sample_threshold_mb(
    watermark_mb: Option<u64>,
    hard_limit_mb: Option<u64>,
) -> Option<u64> {
    let from_hard = hard_limit_mb.map(hard_limit_drain_threshold_mb);
    match (watermark_mb, from_hard) {
        (None, None) => None,
        (Some(w), None) => Some(w),
        (None, Some(h)) => Some(h),
        (Some(w), Some(h)) => Some(w.min(h)),
    }
}

/// 自适应清扫节奏配置（ADR 2026-10-19 D3 修订）：WARN 阈值触发期间把
/// 采样循环的 `mi_collect` 间隔收紧到的秒数，env `AGENTOS_COLLECT_FAST_SECS`。
/// 未设置 = 默认 10s；显式 0 / 非法值 = 禁用收紧（恒常规节奏）。
/// **语义边界**：收紧只影响采样循环的 mi_collect 频率，绝不触发重启——
/// 区别于水位/硬限的执法动作（排水重启/硬熔断）。
pub fn resolve_collect_fast_secs() -> u64 {
    const DEFAULT_FAST_SECS: u64 = 10;
    match std::env::var("AGENTOS_COLLECT_FAST_SECS") {
        Ok(v) => v.trim().parse::<u64>().unwrap_or(0),
        Err(_) => DEFAULT_FAST_SECS,
    }
}

/// 清扫收紧触发判定（纯函数）：commit 或 abandoned 任一**严格大于**对应
/// WARN 阈值即触发（阈值与 P1 观测护栏同源，由调用方注入同一常量）。
/// None = 采样缺数，该维度不参与判定（另一侧超限仍触发，双侧皆缺不触发）。
pub fn collect_fast_triggered(
    commit_mb: Option<u64>,
    abandoned_pages: Option<u64>,
    commit_threshold_mb: u64,
    abandoned_threshold_pages: u64,
) -> bool {
    commit_mb.is_some_and(|mb| mb > commit_threshold_mb)
        || abandoned_pages.is_some_and(|n| n > abandoned_threshold_pages)
}

/// 内存预算闸（MB）。**默认关闭**（2026-09-10「显式正值才启用」裁定同构，
/// ADR 2026-10-19 D5 闸面）：未设置 / 0 / 非法值 = 不启用，spawn 准入检查
/// 不装配。
/// commit 相对保留闸判定（纯函数，ADR 2026-11-16）：commit 超绝对下限
/// **且**相对工作集倍率超 ratio 才判命中——水位只看驻留页，Windows 把冷页
/// 换出/压缩后工作集失真而 commit 无线可涨（装机实证 commit 10GB+ / 工作集
/// 数百 MB），本闸补 commit 维度。floor_mb 是绝对下限：小工作集下倍率噪声
/// 大（ws 几十 MB 时轻微 commit 就能凑出高倍率），先过绝对量再谈倍率。
/// 两道均为严格大于（恰等于不算超，与 [`watermark_breach`] 同口径）。
pub fn retention_breach(commit_mb: u64, working_set_mb: u64, ratio: f64, floor_mb: u64) -> bool {
    commit_mb > floor_mb && commit_mb as f64 > ratio * working_set_mb as f64
}

/// commit 相对保留闸倍率（ADR 2026-11-16）。**默认 1.5**（装机取样：滞留期
/// commit/ws ≈ 2791/1997 尚在容忍带，突发 10G/数百 MB 必拦）。0 / 负值 /
/// 非法值 = 禁用该闸（None）——负倍率恒真无意义，按非法同语义禁用；
/// 关闭时 watch 循环不做第二口径补采。
pub fn resolve_commit_retention_ratio() -> Option<f64> {
    const DEFAULT_COMMIT_RATIO: f64 = 1.5;
    match std::env::var("AGENTOS_MEM_COMMIT_RATIO") {
        Ok(v) => v.trim().parse::<f64>().ok().filter(|&r| r > 0.0),
        Err(_) => Some(DEFAULT_COMMIT_RATIO),
    }
}

/// commit 相对保留闸绝对下限（MB，ADR 2026-11-16）：防小工作集倍率噪声。
/// **默认 1024**；配置项非开关（闸的开关语义由 ratio 承载）——0 / 非法值
/// 回落默认（与 [`resolve_host_est_commit_mb`] 同构）。
pub fn resolve_retention_floor_mb() -> u64 {
    const DEFAULT_RETENTION_FLOOR_MB: u64 = 1024;
    std::env::var("AGENTOS_MEM_RETENTION_FLOOR_MB")
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|&v| v > 0)
        .unwrap_or(DEFAULT_RETENTION_FLOOR_MB)
}

/// commit 保留闸连续命中动作（watch 循环消费）：前两次命中 purge 清扫，
/// 连续第 3 次（purge 后仍超 = 滞留非可清扫，清扫无意义）升级排水自愈。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RetentionAction {
    /// 未命中或计数未满：本拍无动作。
    Idle,
    /// 连续命中第 1/2 次：full mi_collect+purge 清扫（日志带 purge 前后
    /// commit 差值，定性「分配器滞留可回收 vs 真泄漏/碎片」）。
    Purge,
    /// 连续第 3 次命中：走水位突破同路径排水重启（reason=commit retention
    /// guard，WARN 携带三次命中的 commit 序列）。
    Drain,
}

/// commit 保留闸连续计数状态机（ADR 2026-11-16；[`PressureAlertEdge`] 同
/// 风格：判定在纯函数 [`retention_breach`]，状态机只管计数、序列与升级）。
/// 未命中清零；命中累加并记录命中期 commit 序列（升级排空 WARN 携带——
/// purge 前后序列走向是「滞留可回收」与「真泄漏/碎片」的生产判据）。
#[derive(Default)]
pub struct RetentionGuardState {
    streak: usize,
    hit_commits_mb: Vec<u64>,
}

impl RetentionGuardState {
    /// 输入本拍命中判定与 commit 读数，返回本拍应执行的动作。
    pub(super) fn observe(&mut self, breach: bool, commit_mb: u64) -> RetentionAction {
        if !breach {
            self.streak = 0;
            self.hit_commits_mb.clear();
            return RetentionAction::Idle;
        }
        self.streak += 1;
        self.hit_commits_mb.push(commit_mb);
        if self.streak >= 3 {
            RetentionAction::Drain
        } else {
            RetentionAction::Purge
        }
    }

    /// 采样缺数拍：保守清零——陈旧 streak 不得跨数据缺口升级（与水位
    /// 「缺数不判」同哲学：宁可不升级，不凭过期证据自愈）。
    pub(super) fn reset(&mut self) {
        self.streak = 0;
        self.hit_commits_mb.clear();
    }

    /// 当前连续命中计数（WARN 日志「连续第 N 次」用）。
    pub(super) fn streak(&self) -> usize {
        self.streak
    }

    /// 连续命中期间的 commit 序列（升级排空 WARN 携带）。
    pub(super) fn hit_commits_mb(&self) -> &[u64] {
        &self.hit_commits_mb
    }
}

/// 内存预算闸（MB）。**默认关闭**（2026-09-10「显式正值才启用」裁定同构，
/// ADR 2026-10-19 D5 闸面）：未设置 / 0 / 非法值 = 不启用，spawn 准入检查
/// 不装配。
pub fn resolve_mem_budget_mb() -> Option<u64> {
    std::env::var("AGENTOS_MEM_BUDGET_MB")
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|&v| v > 0)
}

/// 新宿主预估提交 MB（D5 预算闸准入分子）。env `AGENTOS_HOST_EST_COMMIT_MB`，
/// 默认 512（ADR §6：单宿主 commit ~200-600MB 量级的中值）。0 / 非法值回落
/// 默认——预估是配置项不是开关，无「显式关闭」语义（预算闸未启用时本值
/// 不被消费）。
pub fn resolve_host_est_commit_mb() -> u64 {
    const DEFAULT_HOST_EST_COMMIT_MB: u64 = 512;
    std::env::var("AGENTOS_HOST_EST_COMMIT_MB")
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|&v| v > 0)
        .unwrap_or(DEFAULT_HOST_EST_COMMIT_MB)
}

/// 预算闸判定（纯函数）：当前内核 Private Bytes + 新宿主预估提交**严格大于**
/// 预算 → 拒绝（恰好等于预算放行，超限才是压力）。`commit_mb = None` = 采样
/// 缺数不判（fail-open，绝不因采样不可用拒绝全部 spawn；非 Windows 采样恒
/// None 时预算闸自然失效，与水位「缺数不判、绝不误自愈」同哲学）。
pub fn budget_exceeded(commit_mb: Option<u64>, est_commit_mb: u64, budget_mb: u64) -> bool {
    commit_mb.is_some_and(|mb| mb.saturating_add(est_commit_mb) > budget_mb)
}

/// 内存压力告警边沿（D5 执法序列第一级：告警进通知中心）。
///
/// 状态机：触发态翻转的那一拍产出一个沿（`Some(true)` = 进入 / `Some(false)`
/// = 退出），持续态恒 `None` 不重复发——G3b 翻转日志同哲学。消费面（内核
/// 采样循环）据沿各发一条通知。
#[derive(Default)]
pub struct PressureAlertEdge {
    active: bool,
}

impl PressureAlertEdge {
    /// 输入本拍触发判定，返回边沿（`None` = 无翻转，持续态）。
    pub(super) fn transition(&mut self, triggered: bool) -> Option<bool> {
        if triggered == self.active {
            return None;
        }
        self.active = triggered;
        Some(triggered)
    }
}

/// 内核自挂 Job Object 内存硬限（ADR 2026-10-19 D4 / G1a）：创建 Job →
/// SetInformationJobObject 设 `JOB_OBJECT_LIMIT_PROCESS_MEMORY` → Assign 当前
/// 进程。超限语义 = OS 层拒绝 commit（分配失败，Rust 侧 OOM abort），内核不
/// 被 OS 主动 kill；因此「先排水后熔断」由水位采样阈值承载（95% × 硬限，
/// 见 [`effective_sample_threshold_mb`]），本函数只管挂载。不读 env（调用方
/// 解析一份配置值同时供挂载/日志/阈值联动），挂载失败由调用方告警不阻断
/// 启动（硬限是护栏不是前置条件）。
#[cfg(windows)]
pub fn attach_self_job_memory_limit(limit_bytes: u64) -> Result<(), String> {
    use windows_sys::Win32::Foundation::{CloseHandle, GetLastError};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    };
    use windows_sys::Win32::System::Threading::GetCurrentProcess;

    unsafe {
        // 匿名 Job（不跨进程共享命名）；HANDLE 在 windows-sys 0.52 为 isize。
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job == 0 {
            return Err(format!(
                "CreateJobObjectW 失败（GetLastError={}）",
                GetLastError()
            ));
        }
        // 失败路径关句柄再返回；成功路径故意不关：Job 存活到进程退出即达
        // 目的（本进程即成员），留句柄给后续扩展（kill-on-close 等）。
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_PROCESS_MEMORY;
        limits.ProcessMemoryLimit = limit_bytes as usize;
        let ok = SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION as *const core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );
        if ok == 0 {
            let err = GetLastError();
            CloseHandle(job);
            return Err(format!(
                "SetInformationJobObject 失败（GetLastError={err}）"
            ));
        }
        if AssignProcessToJobObject(job, GetCurrentProcess()) == 0 {
            let err = GetLastError();
            CloseHandle(job);
            return Err(format!(
                "AssignProcessToJobObject 失败（GetLastError={err}）"
            ));
        }
        Ok(())
    }
}

/// 非 Windows 平台无 Job Object 原语：诚实报缺（Err，调用方告警），不假报
/// 已挂载；部署侧硬限走 systemd MemoryMax（ADR §3.5）。
#[cfg(not(windows))]
pub fn attach_self_job_memory_limit(_limit_bytes: u64) -> Result<(), String> {
    Err("非 Windows 平台无 Job Object 原语（部署侧硬限走 systemd MemoryMax）".to_string())
}

/// 采样当前进程工作集与提交内存（字节）。经 windows-sys 的
/// K32GetProcessMemoryInfo 一次读回两个口径：
///
/// - `WorkingSetSize` = 实际驻留（任务管理器「内存(专用工作集)」同口径，
///   水位自愈 2026-11-11 起的判定口径）；
/// - `PrivateUsage` = commit charge（任务管理器「提交大小」同口径，仅观测/
///   硬限模式使用；提交尖峰是分配器 arena 行为，不代表整机内存压力）。
///
/// 取舍：该 crate 仅 FFI 声明零运行时开销，且已是本仓依赖图既有传递依赖
/// （Cargo.lock 0.52.0，零新增下载）；否决了每 60s spawn powershell
/// Get-Process 的替代方案（进程启动秒级开销 + 依赖 powershell 在 PATH）。
#[cfg(windows)]
fn sample_memory_counters() -> Option<(u64, u64)> {
    use std::mem::zeroed;
    use windows_sys::Win32::System::ProcessStatus::{
        K32GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS, PROCESS_MEMORY_COUNTERS_EX,
    };
    use windows_sys::Win32::System::Threading::GetCurrentProcess;

    let mut counters: PROCESS_MEMORY_COUNTERS_EX = unsafe { zeroed() };
    counters.cb = std::mem::size_of::<PROCESS_MEMORY_COUNTERS_EX>() as u32;
    // K32GetProcessMemoryInfo 收基类指针；cb 声明为 EX 尺寸时 Windows 会补填
    // 尾部扩展字段 PrivateUsage（ProcessStatus API 契约）。
    let ok = unsafe {
        K32GetProcessMemoryInfo(
            GetCurrentProcess(),
            &mut counters as *mut PROCESS_MEMORY_COUNTERS_EX as *mut PROCESS_MEMORY_COUNTERS,
            counters.cb,
        )
    };
    if ok == 0 {
        return None;
    }
    Some((counters.WorkingSetSize as u64, counters.PrivateUsage as u64))
}

/// 当前进程提交内存（字节，commit charge）。
#[cfg(windows)]
pub fn sample_private_bytes() -> Option<u64> {
    sample_memory_counters().map(|(_, commit)| commit)
}

/// 当前进程工作集（字节，实际驻留）。
#[cfg(windows)]
pub fn sample_working_set_bytes() -> Option<u64> {
    sample_memory_counters().map(|(working_set, _)| working_set)
}

/// 非 Windows 平台无采样实现：恒 None（监控诚实缺数，不判突破、不自愈）。
#[cfg(not(windows))]
pub fn sample_private_bytes() -> Option<u64> {
    None
}

/// 非 Windows 平台无采样实现：恒 None。
#[cfg(not(windows))]
pub fn sample_working_set_bytes() -> Option<u64> {
    None
}

/// 解析用户插件根目录（可写，第三方插件安装位置）。
///
/// 解析优先级：`AGENTOS_USER_PLUGINS_DIR` > `<USER_ROOT>/plugins`（默认根按
/// OS 不同，见 [`agentos_core::user_space::user_root`]）> `None`（不启用 user_root）。
///
/// 实现单点在 `agentos_core::user_space`——内核 bin、HTTP 面（config 读写）、
/// 插件 loader 共用同一份解析，杜绝"三处各拼一次路径"（单真值 ADR 的实证根因）。
pub fn resolve_user_plugins_dir() -> Option<PathBuf> {
    agentos_core::user_space::user_plugins_dir()
}

/// 身份与启动契约键：`.env` 文件**绝不注入**（ambient/setx/launcher 钉值唯一
/// 事实源）。`AGENTOS_ADMIN_PASSWORD` = 口令 env-only 契约（ADR 2026-10-02，
/// .env 只是运维读取约定，不走文件通道）；`AGENTOS_USER_ROOT` = 自指 Hazard
/// （文件所在根不可被文件改写）；`AGENTOS_KERNEL_PORT`/`AGENTOS_BIND` =
/// launcher 四方钉值契约（探测/代理/内核同端口）；`AGENTOS_INSTANCE_CONFIG`/
/// `AGENTOS_PLUGINS_DIR` = 启动装配钉值。
const ENV_FILE_EXCLUDED_KEYS: [&str; 6] = [
    "AGENTOS_ADMIN_PASSWORD",
    "AGENTOS_USER_ROOT",
    "AGENTOS_KERNEL_PORT",
    "AGENTOS_BIND",
    "AGENTOS_INSTANCE_CONFIG",
    "AGENTOS_PLUGINS_DIR",
];

/// 解析 .env 文本（闭集子集，纯函数）：UTF-8 BOM 剥除；逐行 trim；跳过空行
/// 与 `#` 注释；首个 `=` 切分；键值 trim；值剥成对包裹引号（`"` 或 `'`）；
/// 空键 / 无 `=` / 空值行跳过（空值 ≈ 未设语义，注入空串会被
/// `env::var(...).is_ok()` 缺席检查误判为「已设」）。
pub(super) fn parse_env_file_content(content: &str) -> Vec<(String, String)> {
    let mut entries = Vec::new();
    let content = content.strip_prefix('\u{feff}').unwrap_or(content);
    for line in content.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        let value = value.trim();
        let value = match (value.chars().next(), value.chars().last()) {
            (Some('"'), Some('"')) if value.len() >= 2 => &value[1..value.len() - 1],
            (Some('\''), Some('\'')) if value.len() >= 2 => &value[1..value.len() - 1],
            _ => value,
        };
        if value.is_empty() {
            continue;
        }
        entries.push((key.to_string(), value.to_string()));
    }
    entries
}

/// 应用 .env 键到进程环境（ambient 优先 + 契约键排除）。返回
/// `(注入数, ambient 优先跳过数, 契约键排除数)`。进程环境已存在的键**绝不
/// 覆盖**（launcher 钉值 / setx 播种 > .env > 代码默认，三层优先序）。
pub(super) fn apply_env_file_entries(entries: &[(String, String)]) -> (usize, usize, usize) {
    let mut applied = 0usize;
    let mut skipped_ambient = 0usize;
    let mut skipped_excluded = 0usize;
    for (key, value) in entries {
        if ENV_FILE_EXCLUDED_KEYS.contains(&key.as_str()) {
            skipped_excluded += 1;
            continue;
        }
        if std::env::var(key).is_ok() {
            skipped_ambient += 1;
            continue;
        }
        std::env::set_var(key, value);
        applied += 1;
    }
    (applied, skipped_ambient, skipped_excluded)
}

/// 用户根 `.env` 引导装载（2026-11-17）：`<AGENTOS_USER_ROOT>/.env` 是运维
/// 开关的落盘事实层——内核在最早引导点（分配器选项 env 读取与全部
/// `resolve_*` 链之前，见 main() 首行调用）把文件键注入进程 env。
///
/// 为什么必须是内核自己读：2026-11-17 实证 `.env` 无任何加载方（壳纯 ambient
/// 继承、内核裸 `std::env`），且 dev 旧 shell 启动链 / 任务 scratch 环境洗白
/// 面 env 不可达——内核读自己的 user_root 文件与启动链解耦，三面同闭。
/// fail-open：`AGENTOS_USER_ROOT` 未设 / 文件缺失 / 非 UTF-8 一律静默跳过，
/// 绝不阻断启动。
pub fn load_user_root_env_file() {
    let Ok(root) = std::env::var("AGENTOS_USER_ROOT") else {
        return;
    };
    if root.trim().is_empty() {
        return;
    }
    let path = PathBuf::from(root.trim()).join(".env");
    let Ok(content) = std::fs::read_to_string(&path) else {
        return;
    };
    let entries = parse_env_file_content(&content);
    let (applied, skipped_ambient, skipped_excluded) = apply_env_file_entries(&entries);
    eprintln!(
        "[boot] 用户根 .env 已装载 path={} 注入={} ambient优先跳过={} 契约键排除={}",
        path.display(),
        applied,
        skipped_ambient,
        skipped_excluded
    );
}
