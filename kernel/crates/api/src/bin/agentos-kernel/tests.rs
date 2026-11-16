use super::*;

use agentos_core::types::UserRecord;
// load_config 为 trait 方法（boot 路径已改走 loader 固有 discover_boot_cached，
// main.rs 不再整体导入 PluginLoader trait）——本测试文件用例局部导入。
use agentos_core::traits::PluginLoader as _;

use super::boot_env::{
    budget_exceeded, collect_fast_triggered, effective_sample_threshold_mb,
    hard_limit_drain_threshold_mb, resolve_admin_password, resolve_collect_fast_secs,
    resolve_host_est_commit_mb, resolve_mem_budget_mb, resolve_mem_hard_limit_mb,
    worker_threads_for_tier, worker_threads_from, PressureAlertEdge,
};
use super::plugin_boot::discover_plugin_roots;

/// 日志目录解析·dev 态不变：CWD 可写 → 恒为相对 `logs`
///（现状路径逐位不变，用户根即使可用也不参与）。
#[test]
fn log_dir_dev_stays_cwd_relative() {
    assert_eq!(resolve_log_dir(true, None), PathBuf::from("logs"));
    let user = tempfile::tempdir().unwrap();
    assert_eq!(
        resolve_log_dir(true, Some(user.path())),
        PathBuf::from("logs"),
        "dev 态用户根不参与解析"
    );
}

/// 日志目录解析·只读 CWD 态落用户空间：目录在解析时确保创建，
/// 返回绝对路径（性质：用户根为其前缀）。
#[test]
fn log_dir_readonly_cwd_falls_to_user_space() {
    let user = tempfile::tempdir().unwrap();
    let resolved = resolve_log_dir(false, Some(user.path()));
    assert_eq!(resolved, user.path().join("logs"));
    assert!(resolved.is_absolute(), "用户空间落点必须是绝对路径");
    assert!(resolved.is_dir(), "解析时必须确保日志目录已创建");
}

/// 日志目录解析·用户根不可用 → 回落相对 `logs`（不劣于现状：构建失败
/// 走既有 stdout 降级），绝不 panic。
#[test]
fn log_dir_without_user_root_degrades_to_cwd_relative() {
    assert_eq!(resolve_log_dir(false, None), PathBuf::from("logs"));
}

/// 管理员初始口令解析（D1）：AGENTOS_ADMIN_PASSWORD 设置时原样生效；
/// 未设置时生成随机口令——绝不等于任何硬编码值，且每次生成互不相同
/// （性质断言）。两个分支串在一个测试内执行：环境变量是进程全局态，
/// 拆两个测试会互相竞争。
#[test]
fn admin_password_env_overrides_and_random_fallback() {
    std::env::remove_var("AGENTOS_ADMIN_PASSWORD");
    let (random1, generated1) = resolve_admin_password();
    let (random2, generated2) = resolve_admin_password();
    assert!(generated1 && generated2, "未设置环境变量必须走随机生成");
    assert!(random1.len() >= 24, "随机口令须满足长度下限（128 bit hex）");
    assert_ne!(random1, random2, "两次生成必须互不相同（随机性）");
    std::env::set_var("AGENTOS_ADMIN_PASSWORD", "custom-secret-pw");
    let (explicit, generated) = resolve_admin_password();
    assert!(!generated, "显式环境变量不算生成");
    assert_eq!(explicit, "custom-secret-pw");
    std::env::remove_var("AGENTOS_ADMIN_PASSWORD");
}

/// 监听地址解析（D12）：默认 127.0.0.1；AGENTOS_BIND 显式生效；
/// 弃用别名 AGENTOS_KERNEL_HOST 过渡期仍被采纳。
#[test]
fn bind_host_default_is_loopback() {
    for k in ["AGENTOS_BIND", "AGENTOS_KERNEL_HOST"] {
        std::env::remove_var(k);
    }
    assert_eq!(resolve_bind_host(), "127.0.0.1", "默认必须绑定回环地址");
    std::env::set_var("AGENTOS_BIND", "0.0.0.0");
    assert_eq!(
        resolve_bind_host(),
        "0.0.0.0",
        "AGENTOS_BIND 显式开启外网可达"
    );
    std::env::remove_var("AGENTOS_BIND");
    std::env::set_var("AGENTOS_KERNEL_HOST", "192.168.1.9");
    assert_eq!(resolve_bind_host(), "192.168.1.9", "弃用别名过渡期生效");
    std::env::remove_var("AGENTOS_KERNEL_HOST");
}

/// 模式种子对账挂点（设计稿 §2）：出厂种子在 → 副本播种进用户根并落账本；
/// 出厂种子缺席（存量部署）→ no-op 且不建用户目录。真实临时目录，不 mock。
#[test]
fn reconcile_mode_seeds_at_boot_seeds_before_plugin_scan() {
    let tmp = tempfile::tempdir().unwrap();
    let plugins_dir = tmp.path().join("plugins/shared");
    let seed = plugins_dir.join("modes/mode_coding");
    std::fs::create_dir_all(&seed).unwrap();
    std::fs::write(
        seed.join("plugin.json"),
        r#"{"id":"mode_coding","version":"0.1.0"}"#,
    )
    .unwrap();
    std::fs::write(seed.join("profile.yaml"), "chain: v1\n").unwrap();
    let user_root = tmp.path().join("user-root");

    // 生产形态：挂点收用户插件根（<USER_ROOT>/plugins），副本落其下 modes/
    reconcile_mode_seeds_at_boot(&plugins_dir, Some(&user_root.join("plugins")));
    // 播种发生在插件扫描之前：discover 之前用户副本与账本已在盘上
    assert_eq!(
        std::fs::read_to_string(user_root.join("plugins/modes/mode_coding/profile.yaml")).unwrap(),
        "chain: v1\n"
    );
    assert!(user_root.join("plugins/modes/.seeds.json").is_file());

    // 出厂种子缺席 → no-op，不建用户 modes 目录
    let user_root2 = tmp.path().join("user-root-2");
    let empty_plugins = tmp.path().join("plugins-empty");
    std::fs::create_dir_all(&empty_plugins).unwrap();
    reconcile_mode_seeds_at_boot(&empty_plugins, Some(&user_root2));
    assert!(!user_root2.join("plugins/modes").exists());

    // 用户空间不可用（None）→ no-op 不 panic
    reconcile_mode_seeds_at_boot(&plugins_dir, None);
}

/// 启动扫描根（双根用户赢·启动侧）：内置根与用户根的插件父目录都必须进
/// root_paths——模式包 `modes/<id>/plugin.json` 二级嵌套只有父目录根扫得到，
/// 缺用户根会让启动装载 repo 份、热路径解析用户份（首个 sync 周期源码目录
/// 翻转触发复验驱逐，/ext 路由空窗）。
#[test]
fn collect_boot_plugin_roots_includes_user_root_parents() {
    let tmp = tempfile::tempdir().unwrap();
    let plugins_dir = tmp.path().join("plugins/shared");
    let repo_mode = plugins_dir.join("modes/mode_coding");
    std::fs::create_dir_all(&repo_mode).unwrap();
    std::fs::write(repo_mode.join("plugin.json"), r#"{"id":"mode_coding"}"#).unwrap();
    let user_plugins = tmp.path().join("user-root/plugins");
    let user_mode = user_plugins.join("modes/mode_coding");
    std::fs::create_dir_all(&user_mode).unwrap();
    std::fs::write(user_mode.join("plugin.json"), r#"{"id":"mode_coding"}"#).unwrap();

    let roots = collect_boot_plugin_roots(&plugins_dir, Some(&user_plugins));
    let repo_parent = plugins_dir.join("modes").to_string_lossy().to_string();
    let user_parent = user_plugins.join("modes").to_string_lossy().to_string();
    assert!(
        roots.contains(&repo_parent),
        "内置根模式包父目录必须在扫描根: {roots:?}"
    );
    assert!(
        roots.contains(&user_parent),
        "用户根模式包父目录必须在扫描根（双根同 id 用户赢的启动前提）: {roots:?}"
    );

    // 用户根缺席（存量部署）= 仅内置根，不建目录不报错。
    let only = collect_boot_plugin_roots(&plugins_dir, None);
    assert_eq!(only.len(), 1);
    assert_eq!(only[0], repo_parent);
}

/// git 化挂点（设计稿 §2.2）：对账后确保用户仓就绪 + 播种变更自动提交一笔；
/// 重复引导零操作（对账幂等 → 无第二笔提交）。真实临时目录 + 真实 git。
#[test]
fn boot_initializes_user_repo_and_commits_seed_reconciliation() {
    use agentos_core::user_space::USER_ROOT_ENV;
    let _lock = user_space_env_lock();
    let tmp = tempfile::tempdir().unwrap();
    let plugins_dir = tmp.path().join("plugins/shared");
    let seed = plugins_dir.join("modes/mode_git");
    std::fs::create_dir_all(&seed).unwrap();
    std::fs::write(
        seed.join("plugin.json"),
        r#"{"id":"mode_git","version":"1.0.0"}"#,
    )
    .unwrap();
    std::fs::write(seed.join("profile.yaml"), "chain: v1\n").unwrap();
    let user_root = tmp.path().join("user-root");
    struct RootGuard(Option<String>);
    impl Drop for RootGuard {
        fn drop(&mut self) {
            match self.0.take() {
                Some(v) => std::env::set_var(USER_ROOT_ENV, v),
                None => std::env::remove_var(USER_ROOT_ENV),
            }
        }
    }
    let _guard = RootGuard(std::env::var(USER_ROOT_ENV).ok());
    std::env::set_var(USER_ROOT_ENV, &user_root);

    // 首次引导：播种 → 建仓 → 联动提交一笔
    let outcomes = reconcile_mode_seeds_at_boot(&plugins_dir, Some(&user_root.join("plugins")));
    init_user_repo_at_boot();
    assert!(user_root.join(".git").exists());
    assert!(user_root.join(".gitignore").is_file());
    commit_seed_reconciliation_at_boot(&outcomes);

    let head = |args: &[&str]| {
        let out = std::process::Command::new("git")
            .arg("-C")
            .arg(&user_root)
            .args(args)
            .output()
            .unwrap();
        assert!(out.status.success());
        String::from_utf8_lossy(&out.stdout).trim().to_string()
    };
    let count = head(&["rev-list", "--count", "HEAD"]);
    assert_eq!(count, "1", "联动提交恰好一笔");
    let subject = head(&["log", "-1", "--format=%s"]);
    assert!(subject.starts_with("seed: reconcile "));
    let tracked = head(&["ls-files"]);
    assert!(tracked.contains("plugins/modes/mode_git/profile.yaml"));
    assert!(tracked.contains("plugins/modes/.seeds.json"));

    // 重复引导：对账全 Current → 不再产生提交（幂等零操作）
    let rerun = reconcile_mode_seeds_at_boot(&plugins_dir, Some(&user_root.join("plugins")));
    init_user_repo_at_boot();
    commit_seed_reconciliation_at_boot(&rerun);
    let count = head(&["rev-list", "--count", "HEAD"]);
    assert_eq!(count, "1", "重复引导零新增提交");
}

/// P0-1：build_plugin_loader 接入 config_root 后，load_config 返回非空（含 models 节）。
///
/// 回归保护：若有人移除 `.with_config_root(config_root)`，此测试会失败——
/// 因为 loader 的 config_root 为 None 时 load_config 恒返回空 `{}`。
///
/// 环境变量是进程全局态：读写 `AGENTOS_USER_*` 的用例互斥执行。
///
/// 同二进制内 `build_plugin_loader_wires_config_root`（钉桩用户根）与
/// `user_plugins_dir_env_first_then_data_dir`（断言分区解析）都读这些变量，
/// 并行跑会互相踩（实测整套 workspace 连跑时偶发失败）。
fn user_space_env_lock() -> std::sync::MutexGuard<'static, ()> {
    static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    LOCK.lock().unwrap_or_else(|e| e.into_inner())
}

/// 用户根钉到本用例的 config_dir：`load_config` 经 `apply_user_config_overlay`
/// 做**用户层优先整体替换**（ADR 2026-09-13-unified-user-root），若用户层恰好存在
/// 同路径文件（开发机的 `%APPDATA%/agentos/config/models/llm.yaml`），factory 的
/// 用例夹具会被整个顶掉，断言依赖环境。钉桩保证只读本用例自己写的文件。
#[tokio::test]
// 刻意持 std Mutex 跨 await：锁串行化的正是进程全局 AGENTOS_USER_*，
// 必须覆盖 load_config 的整个生命周期（测试独占语义）。
#[allow(clippy::await_holding_lock)]
async fn build_plugin_loader_wires_config_root() {
    let plugins_dir = tempfile::tempdir().unwrap();
    let config_dir = tempfile::tempdir().unwrap();

    // 隔离用户空间：本用例自带 config_dir 即用户配置层根，无残留文件
    struct UserRootGuard {
        _lock: std::sync::MutexGuard<'static, ()>,
        root: Option<String>,
        cfg: Option<String>,
    }
    impl Drop for UserRootGuard {
        fn drop(&mut self) {
            match self.root.take() {
                Some(v) => std::env::set_var(agentos_core::user_space::USER_ROOT_ENV, v),
                None => std::env::remove_var(agentos_core::user_space::USER_ROOT_ENV),
            }
            match self.cfg.take() {
                Some(v) => std::env::set_var(agentos_core::user_space::USER_CONFIG_DIR_ENV, v),
                None => std::env::remove_var(agentos_core::user_space::USER_CONFIG_DIR_ENV),
            }
        }
    }
    let user_root = tempfile::tempdir().unwrap();
    let _user_guard = UserRootGuard {
        _lock: user_space_env_lock(),
        root: std::env::var(agentos_core::user_space::USER_ROOT_ENV).ok(),
        cfg: std::env::var(agentos_core::user_space::USER_CONFIG_DIR_ENV).ok(),
    };
    std::env::set_var(agentos_core::user_space::USER_ROOT_ENV, user_root.path());
    std::env::set_var(
        agentos_core::user_space::USER_CONFIG_DIR_ENV,
        user_root.path().join("config"),
    );

    // 构造与真实 config/ 同构的最小结构：config/models/llm.yaml
    let models_dir = config_dir.path().join("models");
    std::fs::create_dir_all(&models_dir).unwrap();
    std::fs::write(
        models_dir.join("llm.yaml"),
        "models:\n  glm-5.2:\n    provider: zhipu_coding\nproviders:\n  zhipu_coding:\n    type: openai\n",
    )
    .unwrap();

    let loader = build_plugin_loader(plugins_dir.path(), None, config_dir.path());

    let config = loader.load_config().await.expect("load_config 应成功");
    let obj = config
        .as_object()
        .expect("load_config 应返回对象，而非空 {{}}");

    // P0-1 验收：非空 + 含 models 节（对应 config_files 映射 models 的插件）
    assert!(
        !obj.is_empty(),
        "config_root 接入后 load_config 不应返回空 {{}}（P0-1 bug 回归）"
    );
    assert!(
        obj.contains_key("models"),
        "应含 models 节（config/models/llm.yaml 经 collect_yaml_configs 递归收录）"
    );

    // 递归结构验证：models.llm 应是 llm.yaml 的解析结果
    let models = obj
        .get("models")
        .and_then(|v| v.as_object())
        .expect("models 节应为对象");
    assert!(
        models.contains_key("llm"),
        "models 节下应含 llm.yaml 的解析结果"
    );
    let llm = models.get("llm").and_then(|v| v.as_object()).unwrap();
    assert!(llm.contains_key("providers"), "llm.yaml 内容应含 providers");
}

/// 档位 → worker 线程数映射（原则 P2 资源参数自适应）：手机档
/// min(核数,4) 收窄、桌面/云不收窄（None = 不设 worker_threads，tokio
/// 默认 = 核数）；探测不可得（cores=0）不产零 worker。性质：worker 数
/// ≥1 且 ≤ max(核数,1)。
#[test]
fn worker_threads_distinct_by_tier() {
    use agentos_core::device_profile::DeviceTier;
    // 手机档：核数富余时收到上界 4；核数少于上界时 = 核数（≤ cores）。
    assert_eq!(worker_threads_for_tier(DeviceTier::Phone, 16), Some(4));
    assert_eq!(worker_threads_for_tier(DeviceTier::Phone, 8), Some(4));
    assert_eq!(worker_threads_for_tier(DeviceTier::Phone, 2), Some(2));
    assert_eq!(
        worker_threads_for_tier(DeviceTier::Phone, 0),
        Some(1),
        "核数探测不可得不得产出零 worker"
    );
    // 桌面/云：维持现状（None = tokio 默认），与手机档有区分度。
    assert_eq!(worker_threads_for_tier(DeviceTier::Desktop, 16), None);
    assert_eq!(worker_threads_for_tier(DeviceTier::Cloud, 16), None);
    // 性质：全核数域上 1 ≤ n ≤ max(cores,1)。
    for cores in 1u32..=64 {
        if let Some(n) = worker_threads_for_tier(DeviceTier::Phone, cores) {
            assert!(
                n >= 1 && n <= cores.max(1) as usize,
                "cores={cores} 越界: {n}"
            );
        }
    }
}

/// worker 线程数解析序：env 显式钉值 > 档位派生（host_key 同构三级序）；
/// 非法/非正值按缺席回落档位派生。
#[test]
fn worker_threads_env_overrides_beat_tier() {
    use agentos_core::device_profile::DeviceTier;
    assert_eq!(
        worker_threads_from(Some("2"), DeviceTier::Phone, 8),
        Some(2),
        "env 钉值压过手机档派生的 4"
    );
    assert_eq!(
        worker_threads_from(Some("6"), DeviceTier::Desktop, 16),
        Some(6),
        "env 钉值对桌面档（默认 None）同样生效"
    );
    for bad in ["not-a-number", "0", ""] {
        assert_eq!(
            worker_threads_from(Some(bad), DeviceTier::Phone, 8),
            Some(4),
            "非法值 {bad:?} 回落档位派生"
        );
    }
}

/// worker 线程数生产解析链（真依赖）：env 未钉值 = 注入式解析同参结果；
/// 钉值生效后还原。环境变量进程全局态，分支串在一个测试内执行。
#[test]
fn resolve_worker_threads_reads_env_and_tier() {
    std::env::remove_var("AGENTOS_WORKER_THREADS");
    let tier = agentos_core::device_profile::detect_device_tier();
    let cores = agentos_core::device_profile::probe_cores();
    assert_eq!(
        resolve_worker_threads(),
        worker_threads_from(None, tier, cores),
        "未钉值 = 按当前档位派生"
    );

    std::env::set_var("AGENTOS_WORKER_THREADS", "3");
    assert_eq!(resolve_worker_threads(), Some(3), "生产链 env 钉值生效");

    std::env::remove_var("AGENTOS_WORKER_THREADS");
}

/// blocking 池保活行为锚（commit 爆涨主修的语义面）：保活窗内空闲
/// worker 被复用。线程存活 ⇒ 其 mimalloc 堆滞留属主手中不进废弃池
/// ——这正是压 abandoned 悬空 commit 的机制；若回归为「每任务新建
/// 线程/线程即用即毁」，本例必红。默认 600s 由
/// thread_keep_alive_env_defaults_and_overrides 钉死，此处用显式
/// 长窗验证池复用语义本身。
#[test]
fn blocking_pool_reuses_worker_within_keep_alive_window() {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .thread_keep_alive(std::time::Duration::from_secs(60))
        .build()
        .expect("构建测试 runtime");
    let mut ids = Vec::new();
    rt.block_on(async {
        for _ in 0..8 {
            ids.push(
                tokio::task::spawn_blocking(|| std::thread::current().id())
                    .await
                    .expect("spawn_blocking 不应 panic"),
            );
        }
    });
    // 回归判据=distinct worker 数有界：线程即用即毁回归 → 8 任务 8 个
    // distinct id；健康池复用 → 恒 1。容差 2 是对 tokio 调度的让步
    // （池无「同线程复用」的文档契约，2026-11-06 CI 慢机实证逐任务同线程
    // 断言偶发假红）——但 2 << 8，回归检测力不受损。
    let distinct: std::collections::HashSet<_> = ids.iter().collect();
    assert!(
        distinct.len() <= 2,
        "保活窗内连续任务应复用少量 worker（got {ids:?}），线程即用即毁=废弃堆生成器回归"
    );
}

/// 同锚反面：空闲超窗后 worker 被退役、后续任务换新线程——保活不是
/// 「永不回收」，窗必须真实生效。短窗（100ms）+ 10 倍窗距观察退役。
#[test]
fn blocking_pool_retires_worker_after_keep_alive_window() {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .thread_keep_alive(std::time::Duration::from_millis(100))
        .build()
        .expect("构建测试 runtime");
    let (id1, id2) = rt.block_on(async {
        let id1 = tokio::task::spawn_blocking(|| std::thread::current().id())
            .await
            .expect("spawn_blocking 不应 panic");
        tokio::time::sleep(std::time::Duration::from_millis(1000)).await;
        let id2 = tokio::task::spawn_blocking(|| std::thread::current().id())
            .await
            .expect("spawn_blocking 不应 panic");
        (id1, id2)
    });
    assert_ne!(
        id1, id2,
        "空闲超保活窗后 worker 应退役（新任务换新线程），窗未生效=线程滞留堆滞留回归"
    );
}

/// 水位配置：**默认 None（关闭）**；显式正值启用；0/非法值=关闭；
/// 启用时 _MIN 缺省 10。环境变量进程全局态，分支串单测试执行。
#[test]
fn watermark_env_defaults_and_overrides() {
    std::env::remove_var("AGENTOS_MEM_WATERMARK_MB");
    std::env::remove_var("AGENTOS_MEM_WATERMARK_MIN");
    assert_eq!(resolve_watermark_config(), None, "未设置=默认关闭");

    std::env::set_var("AGENTOS_MEM_WATERMARK_MB", "500");
    std::env::set_var("AGENTOS_MEM_WATERMARK_MIN", "3");
    assert_eq!(resolve_watermark_config(), Some((500, 3)));

    // _MIN 未设/非法/非正：启用状态下回落默认 10（一维非法不拖累另一维）
    std::env::remove_var("AGENTOS_MEM_WATERMARK_MIN");
    assert_eq!(resolve_watermark_config(), Some((500, 10)));

    std::env::set_var("AGENTOS_MEM_WATERMARK_MB", "0");
    std::env::set_var("AGENTOS_MEM_WATERMARK_MIN", "0");
    assert_eq!(resolve_watermark_config(), None, "0=显式关闭");

    std::env::set_var("AGENTOS_MEM_WATERMARK_MB", "bogus");
    assert_eq!(resolve_watermark_config(), None, "非法值=关闭");

    std::env::remove_var("AGENTOS_MEM_WATERMARK_MB");
    std::env::remove_var("AGENTOS_MEM_WATERMARK_MIN");
}

/// 水位判定（纯函数）三组：正常（低于阈值不触发）、临界（恰等于阈值不算
/// "超"；单次尖峰后回落不触发——连续性被打断）、持续超限（连续窗口全超
/// 触发）。另覆盖：长样本只看最近 consecutive 个（性质：判定只依赖尾部
/// 窗口）、样本不足不判、阈值升高翻转结果。
#[test]
fn watermark_breach_requires_sustained_strict_exceedance() {
    // 正常：全部低于阈值
    assert!(!watermark_breach(&[100, 200, 299], 300, 3));
    // 临界：恰好等于阈值不算超（严格大于）
    assert!(!watermark_breach(&[300, 300, 300], 300, 3));
    // 临界：尖峰后回落，连续性打断不触发（回落落在尾部窗口内）
    assert!(!watermark_breach(&[400, 400, 200, 400, 400], 300, 3));
    // 持续超限：恰好 consecutive 个全超 → 触发
    assert!(watermark_breach(&[301, 500, 900], 300, 3));
    // 性质：判定只依赖尾部 consecutive 窗口——前缀任意不影响结果
    assert!(watermark_breach(&[200, 301, 500, 900], 300, 3));
    assert!(!watermark_breach(&[900, 900, 900, 900, 200], 300, 3));
    // 样本不足 consecutive 个不判
    assert!(!watermark_breach(&[900], 300, 3));
    // consecutive = 0 防御（不判）
    assert!(!watermark_breach(&[900, 900], 300, 0));
    // 阈值升高翻转结果（单调性）
    assert!(!watermark_breach(&[301, 500, 900], 900, 3));
}

/// 硬限配置：**默认 None（不挂 Job）**；显式正值启用；0/非法值=关闭。
/// 环境变量进程全局态，分支串单测试执行（同文件约定）。
#[test]
fn mem_hard_limit_env_defaults_and_overrides() {
    std::env::remove_var("AGENTOS_MEM_HARD_LIMIT_MB");
    assert_eq!(resolve_mem_hard_limit_mb(), None, "未设置=默认关闭");

    std::env::set_var("AGENTOS_MEM_HARD_LIMIT_MB", "1536");
    assert_eq!(resolve_mem_hard_limit_mb(), Some(1536), "合法正值=启用");

    std::env::set_var("AGENTOS_MEM_HARD_LIMIT_MB", "0");
    assert_eq!(resolve_mem_hard_limit_mb(), None, "0=显式关闭");

    std::env::set_var("AGENTOS_MEM_HARD_LIMIT_MB", "bogus");
    assert_eq!(resolve_mem_hard_limit_mb(), None, "非法值=回落关闭");

    std::env::remove_var("AGENTOS_MEM_HARD_LIMIT_MB");
}

/// 硬限排水安全垫：阈值恒 = 硬限 - max(5%, 1MB)。性质断言：任意配置下
/// 阈值**严格低于**硬限（排水先于熔断的契约底线）；字面值锚定 5% 比例。
#[test]
fn hard_limit_drain_threshold_stays_below_limit() {
    for mb in [1u64, 19, 20, 21, 100, 1000, 1024, 1536, 10240] {
        let threshold = hard_limit_drain_threshold_mb(mb);
        assert!(
            threshold < mb,
            "阈值必须严格低于硬限（排水先于 Job 熔断）：{mb} → {threshold}"
        );
    }
    assert_eq!(
        hard_limit_drain_threshold_mb(1000),
        950,
        "1000MB 扣 5% = 950"
    );
    assert_eq!(
        hard_limit_drain_threshold_mb(1024),
        973,
        "1024MB 扣 max(51,1) = 973"
    );
    assert_eq!(
        hard_limit_drain_threshold_mb(1),
        0,
        "极小值至少扣 1MB，防阈值==硬限"
    );
}

/// 阈值组合判定（纯函数）四象限：仅水位=原样；仅硬限=95%×硬限；
/// 双设取 min（任一侧更小都取小者，防线互不越位）；都无=None（监控关闭）。
#[test]
fn sample_threshold_combines_watermark_and_hard_limit() {
    // 都无 → 关闭
    assert_eq!(effective_sample_threshold_mb(None, None), None);
    // 仅水位 → 原样生效
    assert_eq!(effective_sample_threshold_mb(Some(500), None), Some(500));
    // 仅硬限 → 95% × 硬限
    assert_eq!(effective_sample_threshold_mb(None, Some(1000)), Some(950));
    // 双设取 min：水位更小取水位
    assert_eq!(
        effective_sample_threshold_mb(Some(500), Some(1000)),
        Some(500)
    );
    // 双设取 min：95%×硬限更小取硬限侧（水位形同虚设时排水契约兜底）
    assert_eq!(
        effective_sample_threshold_mb(Some(2000), Some(1000)),
        Some(950)
    );
}

/// 清扫收紧节奏配置（ADR 2026-10-19 D3 修订）四态：未设置=默认 10s；
/// 显式正值覆盖；0=显式禁用收紧；非法值（非数字/负数/纯空白）=回落禁用
/// （恒常规节奏）。环境变量进程全局态，分支串单测试执行（同文件约定）。
#[test]
fn collect_fast_secs_env_defaults_and_overrides() {
    std::env::remove_var("AGENTOS_COLLECT_FAST_SECS");
    assert_eq!(
        resolve_collect_fast_secs(),
        10,
        "未设置 = 默认 10s 收紧间隔"
    );

    std::env::set_var("AGENTOS_COLLECT_FAST_SECS", "5");
    assert_eq!(resolve_collect_fast_secs(), 5, "显式正值覆盖默认");

    std::env::set_var("AGENTOS_COLLECT_FAST_SECS", "0");
    assert_eq!(resolve_collect_fast_secs(), 0, "0 = 显式禁用收紧");

    for bad in ["bogus", "-3", "   "] {
        std::env::set_var("AGENTOS_COLLECT_FAST_SECS", bad);
        assert_eq!(
            resolve_collect_fast_secs(),
            0,
            "非法值 {bad:?} = 回落禁用（恒常规节奏），绝不回落默认"
        );
    }

    std::env::remove_var("AGENTOS_COLLECT_FAST_SECS");
}

/// 清扫收紧触发判定（纯函数）四象限：双未超/仅 commit 超/仅 abandoned 超/
/// 双超。另锚定严格大于语义（恰等于阈值不算超）、采样缺数（None）不参与
/// 判定（另一侧超限仍触发、双侧皆缺不触发）与阈值升高翻转结果。
#[test]
fn collect_fast_triggered_four_quadrants() {
    const C: u64 = 8192;
    const A: u64 = 50_000;
    // 双未超：恰等于阈值不算超（严格大于，与 WARN 护栏同口径）
    assert!(!collect_fast_triggered(Some(C), Some(A), C, A));
    // 仅 commit 超
    assert!(collect_fast_triggered(Some(C + 1), Some(A), C, A));
    // 仅 abandoned 超
    assert!(collect_fast_triggered(Some(0), Some(A + 1), C, A));
    // 双超
    assert!(collect_fast_triggered(Some(C + 1), Some(A + 1), C, A));
    // 缺数不判：双侧 None 不触发；单侧 None 另一侧超限仍触发
    assert!(!collect_fast_triggered(None, None, C, A));
    assert!(collect_fast_triggered(None, Some(A + 1), C, A));
    assert!(collect_fast_triggered(Some(C + 1), None, C, A));
    assert!(!collect_fast_triggered(None, Some(A), C, A));
    // 阈值升高翻转结果（对阈参数单调）
    assert!(!collect_fast_triggered(Some(C + 1), Some(0), C + 2, A));
}

/// 内存预算闸配置（ADR 2026-10-19 D5 第一刀）三态：未设置=默认关闭（闸不
/// 装配）；显式正值=启用；0=显式关闭；非法值=回落关闭。环境变量进程全局态，
/// 分支串单测试执行（同文件约定）。
#[test]
fn mem_budget_env_defaults_and_overrides() {
    std::env::remove_var("AGENTOS_MEM_BUDGET_MB");
    assert_eq!(resolve_mem_budget_mb(), None, "未设置=默认关闭");

    std::env::set_var("AGENTOS_MEM_BUDGET_MB", "4096");
    assert_eq!(resolve_mem_budget_mb(), Some(4096), "显式正值=启用");

    std::env::set_var("AGENTOS_MEM_BUDGET_MB", "0");
    assert_eq!(resolve_mem_budget_mb(), None, "0=显式关闭");

    for bad in ["bogus", "-1", "   "] {
        std::env::set_var("AGENTOS_MEM_BUDGET_MB", bad);
        assert_eq!(
            resolve_mem_budget_mb(),
            None,
            "非法值 {bad:?} = 回落关闭，绝不半启用"
        );
    }

    std::env::remove_var("AGENTOS_MEM_BUDGET_MB");
}

/// 新宿主预估提交（D5 预算闸准入分子）三态：未设置=默认 512；显式正值
/// 生效；0/非法值回落默认——预估是配置项不是开关，无「显式关闭」语义。
/// 环境变量进程全局态，分支串单测试执行（同文件约定）。
#[test]
fn host_est_commit_env_defaults_and_overrides() {
    std::env::remove_var("AGENTOS_HOST_EST_COMMIT_MB");
    assert_eq!(resolve_host_est_commit_mb(), 512, "未设置=默认 512");

    std::env::set_var("AGENTOS_HOST_EST_COMMIT_MB", "768");
    assert_eq!(resolve_host_est_commit_mb(), 768, "显式正值生效");

    for bad in ["0", "bogus", "-5", "  "] {
        std::env::set_var("AGENTOS_HOST_EST_COMMIT_MB", bad);
        assert_eq!(
            resolve_host_est_commit_mb(),
            512,
            "0/非法值 {bad:?} 回落默认（预估无关闭语义）"
        );
    }

    std::env::remove_var("AGENTOS_HOST_EST_COMMIT_MB");
}

/// 预算闸判定（纯函数）：严格大于语义（恰好等于预算放行，超 1MB 即拒绝）；
/// 采样缺数不判（fail-open，绝不因采样不可用拒绝全部 spawn）；饱和加法
/// 防溢出（极大采样值不 panic、仍如实判超）。
#[test]
fn budget_exceeded_strict_over_and_missing_sample_fails_open() {
    // 恰好等于预算放行（3072 + 1024 = 4096）
    assert!(!budget_exceeded(Some(3072), 1024, 4096));
    // 超 1MB 即拒绝
    assert!(budget_exceeded(Some(3073), 1024, 4096));
    // 采样缺数不判（fail-open）
    assert!(!budget_exceeded(None, 1024, 4096));
    // 预算升高翻转结果（对预算参数单调）
    assert!(!budget_exceeded(Some(3073), 1024, 8192));
    // 溢出安全：饱和加法如实判超，不 panic
    assert!(budget_exceeded(Some(u64::MAX - 1), 4, 4096));
}

/// 内存压力告警边沿状态机（D5 执法序列第一级）：触发态翻转才产沿——
/// 首拍触发=进入沿；持续态不重复发；退出恰一条；再进入再发（边沿语义，
/// G3b 翻转日志同哲学）。
#[test]
fn pressure_alert_edge_notifies_on_transitions_only() {
    let mut edge = PressureAlertEdge::default();
    assert_eq!(edge.transition(true), Some(true), "首拍触发 = 进入沿");
    assert_eq!(edge.transition(true), None, "持续触发态不重复发");
    assert_eq!(edge.transition(false), Some(false), "退出沿恰一条");
    assert_eq!(edge.transition(false), None, "持续正常态不重复发");
    assert_eq!(edge.transition(true), Some(true), "再进入再发（边沿语义）");
}

/// 真实 FFI 挂载全链路（关键路径真实依赖，不 mock 内部）：匿名 Job 创建 →
/// PROCESS_MEMORY 设限 → Assign 测试进程。上限取 64GiB——高于测试进程可达
/// commit，纯验证挂载链路、绝不触发限制；Win8+ 支持嵌套 Job，测试进程宿在
/// 其它 Job（CI runner）中亦可 assign。
#[cfg(windows)]
#[test]
fn attach_self_job_memory_limit_real_ffi_succeeds() {
    use super::boot_env::attach_self_job_memory_limit;
    let limit_bytes = 64u64 * 1024 * 1024 * 1024;
    attach_self_job_memory_limit(limit_bytes).expect("真实 Job Object 挂载应成功");
}

/// 播种与口令迁移（真实 SQLite 内存库，非 mock）：四阶段串行——
/// A 首启播种（显式口令）、B 已存在+显式新口令=重置、C 已存在+随机生成
/// =不动、D 首启+随机生成=播种并走 banner 分支；迁移三态：明文回写、
/// 已哈希跳过、空表直通。环境变量进程全局态，串单测试执行（同文件约定）。
#[tokio::test]
async fn seed_admin_and_migrate_plaintext_passwords_lifecycle() {
    use agentos_engine::store::SqliteStore;

    fn fresh_store() -> Arc<dyn agentos_core::traits::StorageBackend> {
        Arc::new(SqliteStore::open_memory().expect("内存库创建失败"))
    }

    // ── 迁移：空表直通 ──
    let store = fresh_store();
    migrate_plaintext_passwords(store.clone())
        .await
        .expect("空表迁移应直通");

    // ── 迁移：明文行哈希化回写；已哈希行跳过 ──
    let now = chrono::Utc::now().to_rfc3339();
    let mk_user = |uid: &str, name: &str, pw: String| UserRecord {
        user_id: uid.to_string(),
        username: name.to_string(),
        password: pw,
        email: None,
        role: "user".to_string(),
        tenant_id: "default".to_string(),
        created_at: now.clone(),
        last_login_at: None,
        must_change_password: false,
    };
    store
        .create_user(&mk_user("u-plain", "plain_user", "明文口令".to_string()))
        .await
        .expect("建明文用户失败");
    let hashed = agentos_http::auth::hash_password("已是哈希").unwrap();
    store
        .create_user(&mk_user("u-hashed", "hashed_user", hashed))
        .await
        .expect("建哈希用户失败");
    migrate_plaintext_passwords(store.clone())
        .await
        .expect("明文迁移不应失败");
    let migrated = store
        .get_user_by_id("u-plain")
        .await
        .expect("查询失败")
        .expect("用户行应在");
    assert!(
        agentos_http::auth::is_password_hash(&migrated.password),
        "明文行迁移后必须是哈希"
    );
    assert!(
        agentos_http::auth::verify_password("明文口令", &migrated.password),
        "迁移哈希须能验出原口令"
    );
    let untouched = store
        .get_user_by_id("u-hashed")
        .await
        .expect("查询失败")
        .expect("用户行应在");
    assert!(
        agentos_http::auth::verify_password("已是哈希", &untouched.password),
        "已哈希行不应被改写"
    );

    // ── 播种 A：首启 + 显式口令 → 播种 admin（哈希、无改密标记）──
    std::env::set_var("AGENTOS_ADMIN_PASSWORD", "seed-phase-pw");
    let store = fresh_store();
    seed_admin_user(store.clone()).await;
    let admin = store
        .get_user_by_id("00000000-0000-0000-0000-000000000001")
        .await
        .expect("查询失败")
        .expect("首启必须播种 admin");
    assert_eq!(admin.username, "admin");
    assert_eq!(admin.tenant_id, "default");
    assert!(
        !admin.must_change_password,
        "env 播种口令是操作员自选事实源，不得强制改密"
    );
    assert!(
        agentos_http::auth::verify_password("seed-phase-pw", &admin.password),
        "播种口令须与环境变量一致"
    );

    // ── 播种 B：已存在 + 显式新口令 → 重置 ──
    std::env::set_var("AGENTOS_ADMIN_PASSWORD", "reset-phase-pw");
    seed_admin_user(store.clone()).await;
    let admin = store
        .get_user_by_id("00000000-0000-0000-0000-000000000001")
        .await
        .expect("查询失败")
        .expect("admin 应在");
    assert!(
        agentos_http::auth::verify_password("reset-phase-pw", &admin.password),
        "显式新口令必须重置旧口令"
    );

    // ── 播种 C：已存在 + 随机生成 → 不重置 ──
    std::env::remove_var("AGENTOS_ADMIN_PASSWORD");
    seed_admin_user(store.clone()).await;
    let admin = store
        .get_user_by_id("00000000-0000-0000-0000-000000000001")
        .await
        .expect("查询失败")
        .expect("admin 应在");
    assert!(
        agentos_http::auth::verify_password("reset-phase-pw", &admin.password),
        "随机生成口令不得重置既有口令"
    );

    // ── 播种 D：首启 + 随机生成 → 播种且口令为合法哈希 ──
    let store = fresh_store();
    seed_admin_user(store.clone()).await;
    let admin = store
        .get_user_by_id("00000000-0000-0000-0000-000000000001")
        .await
        .expect("查询失败")
        .expect("随机首启也必须播种 admin");
    assert!(
        agentos_http::auth::is_password_hash(&admin.password),
        "随机口令同样落哈希"
    );
    assert!(
        admin.must_change_password,
        "随机生成口令无人持久持有，须标记首登强制改密"
    );
    std::env::remove_var("AGENTOS_ADMIN_PASSWORD");
}

/// 插件根目录发现（真实临时目录）：不存在根=空；二级嵌套 plugin.json 的
/// 父目录入列；plugin.yaml 同权；同父多插件去重。
#[test]
fn discover_plugin_roots_parents_and_dedup() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let base = tmp.path().join("plugins");
    // 不存在的根
    assert!(discover_plugin_roots(&tmp.path().join("nope")).is_empty());

    // 空目录（无任何插件）= 空
    std::fs::create_dir_all(base.join("empty")).unwrap();
    assert!(discover_plugin_roots(&base).is_empty());

    // tools/simple/plugin.json（二级嵌套）→ 父目录 tools 入列
    std::fs::create_dir_all(base.join("tools").join("simple")).unwrap();
    std::fs::write(base.join("tools").join("simple").join("plugin.json"), "{}").unwrap();
    // 另一父下 yaml 插件 + 同父第二插件（去重）
    std::fs::create_dir_all(base.join("sys").join("a")).unwrap();
    std::fs::create_dir_all(base.join("sys").join("b")).unwrap();
    std::fs::write(base.join("sys").join("a").join("plugin.yaml"), "{}").unwrap();
    std::fs::write(base.join("sys").join("b").join("plugin.json"), "{}").unwrap();

    let mut roots = discover_plugin_roots(&base);
    roots.sort();
    let expect_tools = base.join("tools").to_string_lossy().to_string();
    let expect_sys = base.join("sys").to_string_lossy().to_string();
    assert_eq!(
        roots,
        vec![expect_sys, expect_tools],
        "父目录去重后恰为 tools 与 sys 两个扫描根"
    );
}

/// 用户插件目录解析：环境变量优先（含空值忽略回落），回落落 OS 数据目录
/// 下的 agentos/plugins。
#[test]
fn user_plugins_dir_env_first_then_data_dir() {
    // 与 build_plugin_loader_wires_config_root 互斥：同读 AGENTOS_USER_* 进程全局态
    let _lock = user_space_env_lock();
    std::env::set_var("AGENTOS_USER_PLUGINS_DIR", "D:/custom/plugins");
    assert_eq!(
        resolve_user_plugins_dir(),
        Some(PathBuf::from("D:/custom/plugins"))
    );

    // 空白值视同未设置 → 回落 OS 数据目录
    std::env::set_var("AGENTOS_USER_PLUGINS_DIR", "   ");
    let fallback = resolve_user_plugins_dir();
    assert!(fallback.unwrap().to_string_lossy().contains("agentos"));

    std::env::remove_var("AGENTOS_USER_PLUGINS_DIR");
    assert!(
        resolve_user_plugins_dir().is_some(),
        "Windows 下 data_dir 恒可用"
    );
}

/// 私有内存采样（Windows 真实 API 冒烟）：进程自身采样必然成功且量级
/// 合理（> 1MB——测试进程不可能更小）。
#[cfg(windows)]
#[test]
fn sample_private_bytes_returns_plausible_value() {
    let bytes = sample_private_bytes().expect("进程采样不应失败");
    assert!(bytes > 1024 * 1024, "采样值量级不合理: {bytes}");
}
