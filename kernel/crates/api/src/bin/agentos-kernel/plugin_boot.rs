//! 插件装载与种子对齐引导面（agentos-kernel 子模块）：模式种子对齐、用户仓
//! 初始化、插件根发现与 loader 装配。
use std::path::PathBuf;

use agentos_plugin_loader::PluginLoaderImpl;

use tracing::{info, warn};

/// 模式种子对账（设计稿 2026-09-15 §2「版本管理」）：对出厂种子（发现面产出的
/// 插件目录中 `mode_` 前缀者，ADR 2026-10-13 拍平——不再依赖 modes/ 类目录）
/// 与用户副本 `<user_plugins>/modes/` 跑一轮播种/升级对账（语义见
/// [`agentos_core::user_space::reconcile_mode_seeds`]），返回本轮对账结论
/// （供 git 化联动提交判断"有无落盘变化"）。
///
/// 对账是启动家政：出厂种子缺席（存量部署/种子未随包）或用户空间不可用
/// → no-op（双根兜底，内置根模式插件仍可用）；单轮失败只 warn 不阻断启动
/// （fail 方向是"本次不播种不升级"，绝不破坏既有用户副本——账本损坏时
/// 对账整体不动任何文件，fail-closed 语义在 core 侧）。
pub fn reconcile_mode_seeds_at_boot(
    plugins_dir: &std::path::Path,
    user_plugins_dir: Option<&std::path::Path>,
) -> Vec<agentos_core::user_space::ModeSeedOutcome> {
    use agentos_core::user_space::ModeSeedOutcome;
    let Some(user_plugins) = user_plugins_dir else {
        return Vec::new();
    };
    let factory_plugin_dirs = agentos_plugin_loader::collect_plugin_dirs(plugins_dir);
    if factory_plugin_dirs.is_empty() {
        return Vec::new();
    }
    let user_modes = agentos_core::user_space::user_modes_dir(user_plugins);
    match agentos_core::user_space::reconcile_mode_seeds(&factory_plugin_dirs, &user_modes) {
        Ok(outcomes) => {
            for outcome in &outcomes {
                match outcome {
                    ModeSeedOutcome::Seeded { mode_id, version } => info!(
                        target: "agentos-kernel",
                        mode = %mode_id, version = %version,
                        "模式种子已播种到用户空间"
                    ),
                    ModeSeedOutcome::Upgraded { mode_id, from, to } => info!(
                        target: "agentos-kernel",
                        mode = %mode_id, from = %from, to = %to,
                        "模式副本未定制，已静默升级到出厂种子"
                    ),
                    ModeSeedOutcome::UpgradeAvailable {
                        mode_id,
                        seeded_version,
                        factory_version,
                    } => warn!(
                        target: "agentos-kernel",
                        mode = %mode_id, seeded = %seeded_version, factory = %factory_version,
                        "模式副本已定制且出厂种子有更新——保留用户副本，升级可用（恢复出厂后重播种即得新版）"
                    ),
                    ModeSeedOutcome::BaselineRegistered { mode_id, version } => info!(
                        target: "agentos-kernel",
                        mode = %mode_id, version = %version,
                        "用户手工放置的模式副本已补记账本（不替换）"
                    ),
                    ModeSeedOutcome::VersionUnparseable {
                        mode_id,
                        factory_version,
                        ledger_version,
                    } => warn!(
                        target: "agentos-kernel",
                        mode = %mode_id, factory = %factory_version, ledger = %ledger_version,
                        "模式种子 version 非 semver，无法判定新旧——本次不动用户副本（请修正种子 version）"
                    ),
                    ModeSeedOutcome::Current { .. } => {}
                }
            }
            outcomes
        }
        Err(e) => {
            warn!(
                target: "agentos-kernel",
                error = %e,
                "模式种子对账失败（本次不播种不升级，用户副本原样保留；下次启动重试）"
            );
            Vec::new()
        }
    }
}

/// 用户仓 git 化引导挂点（设计稿 2026-09-15 §2.2）：模式种子对账完成后确保
/// 用户仓已初始化（管辖面 `<USER_ROOT>/{plugins,config}`，`.env`/`data/` 及
/// 其余一切排除）。已初始化 → 幂等跳过；git 缺席或初始化失败 → warn 降级
/// （git 化是增强不是依赖，绝不阻断启动），绝不 push。
pub fn init_user_repo_at_boot() {
    let Some(user_root) = agentos_core::user_space::user_root() else {
        return;
    };
    match agentos_core::user_space::ensure_user_repo(&user_root) {
        Ok(true) => info!(
            target: "agentos-kernel",
            path = %user_root.display(),
            "用户仓已初始化（plugins/ 与 config/ 入仓，.env 与 data/ 排除）"
        ),
        Ok(false) => {}
        Err(e) => warn!(
            target: "agentos-kernel",
            error = %e,
            "用户仓初始化失败，git 化降级（功能不受损）"
        ),
    }
}

/// 种子对账联动提交（设计稿 2026-09-15 §2.2）：本轮对账有落盘变化（播种/
/// 升级/补账）→ 自动提交一笔 `seed: reconcile <n> files`（只暂存对账写下的
/// 文件，用户工作区改动不自动提交）。失败只 warn（增强降级，绝不阻断启动）。
pub fn commit_seed_reconciliation_at_boot(outcomes: &[agentos_core::user_space::ModeSeedOutcome]) {
    if outcomes.is_empty() {
        return;
    }
    let Some(user_root) = agentos_core::user_space::user_root() else {
        return;
    };
    match agentos_core::user_space::commit_seed_reconciliation(&user_root, outcomes) {
        Ok(Some(hash)) => info!(
            target: "agentos-kernel",
            commit = %hash,
            "模式种子对账变更已自动提交到用户仓"
        ),
        Ok(None) => {}
        Err(e) => warn!(
            target: "agentos-kernel",
            error = %e,
            "模式种子对账自动提交失败（用户副本已落盘，git 历史缺一笔，可稍后手动提交）"
        ),
    }
}

/// 递归发现包含 plugin.json 的目录的父目录路径列表。
///
/// `scan_root(root)` 扫描 root 的直接子目录，查找 `<child>/plugin.json`。
/// plugins/shared/ 的结构是 `tools/simple/plugin.json`（二级嵌套），
/// 因此需要收集所有直接包含 plugin.json 的目录的**父目录**作为扫描根。
pub fn discover_plugin_roots(base: &std::path::Path) -> Vec<String> {
    if !base.exists() {
        return Vec::new();
    }
    let plugin_dirs = agentos_plugin_loader::collect_plugin_dirs(base);

    // 收集父目录并去重
    let mut parent_set = std::collections::HashSet::new();
    for dir in &plugin_dirs {
        if let Some(parent) = dir.parent() {
            if let Some(s) = parent.to_str() {
                parent_set.insert(s.to_string());
            }
        }
    }
    parent_set.into_iter().collect()
}

/// 启动期 discover 的扫描根全集：Tier1 根 + Tier2 根（实例启动配置的三根；
/// Tier2 根缺席/与 Tier1 根同指 = 仅 Tier1 根）。
///
/// 双根都必须递归到插件目录父级：Tier2 根下模式包 `modes/<mode_id>/plugin.json`
/// 是二级嵌套（装机布局契约），discover 内建的根扫描（一级子目录）看不到。
/// 与热路径 `PluginInvokerImpl::collect_plugin_roots` 同源（tier1/tier2 两 env
/// 载体的父目录全集）。
pub fn collect_boot_plugin_roots(
    tier1_root: &std::path::Path,
    tier2_root: Option<&std::path::Path>,
) -> Vec<String> {
    let mut roots = discover_plugin_roots(tier1_root);
    if let Some(tier2_dir) = tier2_root {
        if tier2_dir != tier1_root {
            roots.extend(discover_plugin_roots(tier2_dir));
        }
    }
    roots
}

/// 迁移护栏：默认库位置迁到用户空间后，存量库仍在项目根时显式告警（不自动迁移）。
///
/// 触发条件（三者同时满足才告警，避免误报）：
/// 1. 本次解析出的库路径**不在**项目根 —— 即已按新默认（用户空间）开库；
/// 2. 项目根**存在** legacy 库文件；
/// 3. legacy 库**非空**（排除 0 字节残留——建了库没写过数据的部署无需迁移）。
///
/// 不阻断启动：告警 + 指明迁移命令。数据没丢（legacy 文件原样在），用户按提示
/// 迁移即可；若已在新库产生数据，迁移脚本的合并语义由脚本自己把关。
pub fn warn_legacy_db_not_migrated(
    storage_cfg: &agentos_engine::storage_factory::StorageConfig,
    config_root: &std::path::Path,
) {
    let Some(project_root) = config_root.parent() else {
        return;
    };
    let legacy = project_root.join(agentos_engine::storage_factory::DB_FILENAME);
    let current = std::path::Path::new(&storage_cfg.sqlite_path);

    // 当前库就在项目根（或 :memory:/非 sqlite）→ 无迁移问题
    if storage_cfg.driver != "sqlite" || storage_cfg.sqlite_path == ":memory:" {
        return;
    }
    if current.parent().map(|p| p == project_root).unwrap_or(false) {
        return;
    }
    let Ok(meta) = std::fs::metadata(&legacy) else {
        return; // 无 legacy 库
    };
    if meta.len() == 0 {
        return; // 空库残留，无数据可迁
    }

    eprintln!(
        "[boot] 检测到项目根存在存量库 {}（{} 字节），但本次按用户空间默认开库到 {}。\n\
         \x20     数据未丢失（老库文件原样保留）。两种处置：\n\
         \x20     ① 迁移存量数据（推荐）：python scripts/migrate_to_user_root.py --dry-run 预览，去掉 --dry-run 执行；\n\
         \x20     ② 继续用老库：设 AGENTOS_DB_PATH 指向老库路径后重启。",
        legacy.display(),
        meta.len(),
        current.display()
    );
    warn!(
        target: "agentos-kernel",
        legacy_db = %legacy.display(),
        current_db = %current.display(),
        "legacy project-root database detected; user-space default in effect (run scripts/migrate_to_user_root.py)"
    );
}

/// 构建插件加载器，并接入配置根目录（task_11 P0-1）。
///
/// loader 必须带 `.with_config_root(config_root)` 接入配置根目录——漏接会使
/// `load_config()` 恒返回空 `{}`，插件收不到任何配置。从 `main` 抽出便于
/// 单测验证「config_root 已接到 loader」。
///
/// # Arguments
/// * `plugins_dir` - Tier1 根（分级单副本安全级，应用目录只读）
/// * `user_plugins_dir` - Tier2 根（可选；与 Tier1 同指的 dev 退化配置传 None）
/// * `config_root` - 配置文件根目录（如 `config/`），loader 据此加载 YAML
pub fn build_plugin_loader(
    plugins_dir: &std::path::Path,
    user_plugins_dir: Option<PathBuf>,
    config_root: &std::path::Path,
) -> PluginLoaderImpl {
    // P0-2：allowlist 生产接线——config/kernel/plugin_allowlist.yaml 从"空挂"变真准入
    // （permissive 默认：放行 + 条目 sha256 校验，真实语料零误伤；strict 由部署方显式
    // 启用：白名单外插件 load 失败 fail-closed，与 deny_unknown_fields 一致）。
    let allowlist = agentos_plugin_loader::load_allowlist_file(
        &config_root.join("kernel/plugin_allowlist.yaml"),
    );
    // 双源同 id 裁决已退役（ADR 2026-11-07-plugin-tiered-single-copy-model §2.5）：
    // 跨根双源由启动守卫拒启（零仲裁），loader 内只剩 version-max 归约兜同根
    // 畸形布局，无策略可配。
    PluginLoaderImpl::new(plugins_dir, user_plugins_dir)
        // 接入 config_root：否则 load_config() 因 config_root=None 恒返回空 {}
        .with_config_root(config_root)
        .with_allowlist(allowlist)
}
