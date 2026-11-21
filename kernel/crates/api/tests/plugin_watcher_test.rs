// @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: rust-test
//! 插件运行时自动发现的集成测试（真 loader + 真 invoker，非 mock）。
//!
//! 这些测试会设置进程级环境变量 `AGENTOS_PLUGINS_DIR`（discover_new_plugins 的目录源），
//! 故一律标 `#[serial]` 串行执行，避免互相污染。纯函数（apply/sync_once）的单测在
//! `src/plugin_watcher.rs` 内联，不碰环境变量，可并行。

use std::collections::HashSet;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use agentos_api::plugin_watcher::{PluginWatcher, WatcherHandle};
use agentos_core::traits::{CapabilityRegistry, PluginInvoker, PluginLoader};
use agentos_invoker::PluginInvokerImpl;
use agentos_plugin_loader::{CapabilityRegistryImpl, PluginLoaderImpl};
use serial_test::serial;
use tokio::time::timeout;

/// 钉用户根到一次性空临时目录（进程级 Once）：
/// sync 会全量重扫插件根（含用户根 modes 二级嵌套的模式包）——
/// 宿主真实用户空间被播种/并行会话同步副本时，该扫描拖长 sync 耗时，
/// 测试的秒级等待窗即超时（0 syncs/无注册/无 hook）。串行整测共享同一空根。
fn pin_temp_user_root() {
    static ONCE: std::sync::Once = std::sync::Once::new();
    ONCE.call_once(|| {
        let dir =
            std::env::temp_dir().join(format!("pin_usr_watcher_{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(&dir).unwrap();
        std::env::set_var(agentos_core::user_space::USER_ROOT_ENV, &dir);
    });
}

/// 在 root 下建一个 tool 类型插件目录（带 tools 能力），写真实 plugin.json。
fn create_plugin_dir(root: &Path, id: &str, tools: &[&str]) {
    let dir = root.join(id);
    std::fs::create_dir_all(&dir).unwrap();
    let tools_json: Vec<String> = tools
        .iter()
        .map(|t| format!(r#"{{"name":"{t}","description":"{t}"}}"#))
        .collect();
    let manifest = format!(
        r#"{{
    "id": "{}", "name": "{}", "version": "1.0.0",
    "plugin_type": "tool", "language": "rust",
    "host_type": "in_process", "entry": "{}",
    "capabilities": {{ "tools": [{}] }}
}}"#,
        id,
        id,
        id,
        tools_json.join(",")
    );
    std::fs::write(dir.join("plugin.json"), manifest).unwrap();
}

#[tokio::test]
#[serial]
async fn watcher_registers_plugin_on_manual_trigger() {
    pin_temp_user_root();
    let tmp = tempfile::tempdir().unwrap();
    // 安全：edition 2021，set_var 非 unsafe。整测串行（#[serial]），无并发污染。
    std::env::set_var("AGENTOS_PLUGINS_DIR", tmp.path());

    // 预置一个旧插件 → 作为 initial_ids（watcher 启动时已知，sync 时应跳过）。
    create_plugin_dir(tmp.path(), "old_plug", &["t_old"]);
    let loader: Arc<dyn PluginLoader> = Arc::new(PluginLoaderImpl::new(tmp.path(), None));
    let initial: Vec<_> = loader
        .discover(&[tmp.path().to_str().unwrap()])
        .await
        .unwrap();
    let initial_ids: HashSet<String> = initial.iter().map(|m| m.id.clone()).collect();
    assert!(initial_ids.contains("old_plug"));

    let invoker = Arc::new(PluginInvokerImpl::new(loader));
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let handle = PluginWatcher::new(
        tmp.path().to_path_buf(),
        invoker as Arc<dyn agentos_core::traits::PluginInvoker>,
        registry.clone(),
        initial_ids,
    )
    .with_debounce(Duration::from_millis(50))
    .spawn();

    // 运行时新增插件 + 手动触发同步。
    create_plugin_dir(tmp.path(), "new_plug", &["t_new"]);
    handle.trigger.send(()).unwrap();

    // 轮询 registry：new_plug 的 tool 应在 3s 内出现。
    let appeared = timeout(Duration::from_secs(3), async {
        loop {
            let has = registry
                .list_tools()
                .iter()
                .any(|t| t.plugin_id == "new_plug");
            if has {
                return Ok::<(), ()>(());
            }
            tokio::time::sleep(Duration::from_millis(30)).await;
        }
    })
    .await;
    assert!(
        appeared.is_ok(),
        "new_plug tool should appear after manual trigger within 3s"
    );
}

#[tokio::test]
#[serial]
async fn watcher_debounces_burst() {
    pin_temp_user_root();
    let tmp = tempfile::tempdir().unwrap();
    std::env::set_var("AGENTOS_PLUGINS_DIR", tmp.path());

    let loader: Arc<dyn PluginLoader> = Arc::new(PluginLoaderImpl::new(tmp.path(), None));
    let invoker = Arc::new(PluginInvokerImpl::new(loader));
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let handle = PluginWatcher::new(
        tmp.path().to_path_buf(),
        invoker as Arc<dyn agentos_core::traits::PluginInvoker>,
        registry,
        HashSet::new(),
    )
    .with_debounce(Duration::from_millis(100))
    .spawn();

    create_plugin_dir(tmp.path(), "burst_plug", &["tb"]);
    // 同一突发连发 3 次 trigger，应被防抖合并为 1 次同步。
    for _ in 0..3 {
        handle.trigger.send(()).unwrap();
    }
    // 等 debounce(100ms) + sync 完成。
    tokio::time::sleep(Duration::from_millis(700)).await;

    let count = handle.sync_count.load(Ordering::Relaxed);
    assert_eq!(
        count, 1,
        "burst of 3 triggers must debounce to exactly 1 sync, got {count}"
    );
}

/// 循环 F：轮询兜底是可靠性主体——不依赖 notify，即便 notify 丢事件也能发现新插件。
#[tokio::test]
#[serial]
async fn watcher_detects_new_plugin_via_polling_fallback() {
    pin_temp_user_root();
    let tmp = tempfile::tempdir().unwrap();
    std::env::set_var("AGENTOS_PLUGINS_DIR", tmp.path());

    let loader: Arc<dyn PluginLoader> = Arc::new(PluginLoaderImpl::new(tmp.path(), None));
    let invoker = Arc::new(PluginInvokerImpl::new(loader));
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let _handle = PluginWatcher::new(
        tmp.path().to_path_buf(),
        invoker as Arc<dyn agentos_core::traits::PluginInvoker>,
        registry.clone(),
        HashSet::new(),
    )
    .with_debounce(Duration::from_millis(50))
    .with_poll_interval(Duration::from_millis(200))
    .spawn();

    // 不手动 trigger：仅靠轮询兜底自动发现。
    create_plugin_dir(tmp.path(), "polled_plug", &["tp"]);

    let appeared = timeout(Duration::from_secs(3), async {
        loop {
            if registry
                .list_tools()
                .iter()
                .any(|t| t.plugin_id == "polled_plug")
            {
                return Ok::<(), ()>(());
            }
            tokio::time::sleep(Duration::from_millis(30)).await;
        }
    })
    .await;
    assert!(
        appeared.is_ok(),
        "polling fallback should discover new plugin within 3s"
    );
}

/// 循环 E：notify 文件监听（低延迟主路径）。Windows 上 ReadDirectoryChangesW 时序不稳，
/// 标 ignore；Linux/macOS 上验证 notify 接线正确（polling 设 30s 确保不会先于 notify 触发）。
#[tokio::test]
#[serial]
#[cfg_attr(
    windows,
    ignore = "notify timing unreliable on Windows; polling fallback covers it"
)]
async fn watcher_detects_new_plugin_dir_via_notify() {
    pin_temp_user_root();
    let tmp = tempfile::tempdir().unwrap();
    std::env::set_var("AGENTOS_PLUGINS_DIR", tmp.path());

    let loader: Arc<dyn PluginLoader> = Arc::new(PluginLoaderImpl::new(tmp.path(), None));
    let invoker = Arc::new(PluginInvokerImpl::new(loader));
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let _handle = PluginWatcher::new(
        tmp.path().to_path_buf(),
        invoker as Arc<dyn agentos_core::traits::PluginInvoker>,
        registry.clone(),
        HashSet::new(),
    )
    .with_debounce(Duration::from_millis(50))
    .with_poll_interval(Duration::from_secs(30)) // 故意拉长：让 notify 先于轮询触发
    .spawn();

    create_plugin_dir(tmp.path(), "notified_plug", &["tn"]);

    let appeared = timeout(Duration::from_secs(5), async {
        loop {
            if registry
                .list_tools()
                .iter()
                .any(|t| t.plugin_id == "notified_plug")
            {
                return Ok::<(), ()>(());
            }
            tokio::time::sleep(Duration::from_millis(30)).await;
        }
    })
    .await;
    assert!(
        appeared.is_ok(),
        "notify watcher should detect new plugin dir within 5s (polling disabled at 30s)"
    );
}

/// A3：运行时新增 InProcess(cdylib) 插件 → watcher 检测集合变更并触发注入的
/// restart hook（env 开关 AGENTOS_AUTO_RESTART_ON_CDYLIB_CHANGE 默认开；
/// 生产 hook 走 routes::drain_and_exit75 排空 + exit 75，逃生门语义在函数内部）。
#[tokio::test]
#[serial]
async fn watcher_fires_restart_hook_on_cdylib_addition() {
    pin_temp_user_root();
    let tmp = tempfile::tempdir().unwrap();
    std::env::set_var("AGENTOS_PLUGINS_DIR", tmp.path());

    let loader: Arc<dyn PluginLoader> = Arc::new(PluginLoaderImpl::new(tmp.path(), None));
    let invoker = Arc::new(PluginInvokerImpl::new(loader));
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let fired = Arc::new(AtomicBool::new(false));
    let flag = Arc::clone(&fired);
    let hook: Arc<dyn Fn() + Send + Sync> = Arc::new(move || flag.store(true, Ordering::Relaxed));
    let handle = PluginWatcher::new(
        tmp.path().to_path_buf(),
        invoker as Arc<dyn agentos_core::traits::PluginInvoker>,
        registry,
        HashSet::new(),
    )
    .with_debounce(Duration::from_millis(50))
    .with_initial_cdylib_ids(HashSet::new()) // boot 期无 cdylib：首轮 diff 即可报新增
    .with_restart_hook(hook)
    .spawn();

    // create_plugin_dir 写的是 host_type: in_process（cdylib 轨）。
    create_plugin_dir(tmp.path(), "native_new", &["tn"]);
    handle_trigger_and_wait(&handle, &fired).await;
    assert!(
        fired.load(Ordering::Relaxed),
        "cdylib 插件新增应触发 restart hook（3s 内）"
    );
}

/// A3 对照组：只新增 sidecar 插件（热注册可达，无需重启）→ hook 不触发。
#[tokio::test]
#[serial]
async fn watcher_does_not_fire_restart_hook_for_sidecar_only() {
    pin_temp_user_root();
    let tmp = tempfile::tempdir().unwrap();
    std::env::set_var("AGENTOS_PLUGINS_DIR", tmp.path());
    // stub sidecar 无 server.py，真实 spawn 必失败；本测试目的是"sidecar-only 不触发
    // 重启 hook"而非验证 G2（G2 严格/宽松已在 plugin_watcher.rs 单测覆盖）。

    let loader: Arc<dyn PluginLoader> = Arc::new(PluginLoaderImpl::new(tmp.path(), None));
    let invoker = Arc::new(PluginInvokerImpl::new(loader));
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let fired = Arc::new(AtomicBool::new(false));
    let flag = Arc::clone(&fired);
    let hook: Arc<dyn Fn() + Send + Sync> = Arc::new(move || flag.store(true, Ordering::Relaxed));
    let handle = PluginWatcher::new(
        tmp.path().to_path_buf(),
        invoker as Arc<dyn agentos_core::traits::PluginInvoker>,
        // clone：watcher 持有一份，本测试保留一份轮询注册结果。
        registry.clone(),
        HashSet::new(),
    )
    .with_debounce(Duration::from_millis(50))
    .with_initial_cdylib_ids(HashSet::new())
    .with_restart_hook(hook)
    .spawn();

    // sidecar 插件目录（host_type: sidecar）。
    let dir = tmp.path().join("sidecar_new");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("plugin.json"),
        r#"{
    "id": "sidecar_new", "name": "sidecar_new", "version": "1.0.0",
    "plugin_type": "tool", "language": "python",
    "host_type": "sidecar", "entry": "python server.py",
    "capabilities": { "tools": [{"name":"ts","description":"ts"}] }
}"#,
    )
    .unwrap();

    handle.trigger.send(()).unwrap();
    // 等 sync 完成（sidecar 插件注册出现在 registry = sync 已跑完）+ 缓冲。
    let synced = timeout(Duration::from_secs(3), async {
        loop {
            if registry
                .list_tools()
                .iter()
                .any(|t| t.plugin_id == "sidecar_new")
            {
                return Ok::<(), ()>(());
            }
            tokio::time::sleep(Duration::from_millis(30)).await;
        }
    })
    .await;
    assert!(synced.is_ok(), "sidecar 插件应正常热注册");
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(
        !fired.load(Ordering::Relaxed),
        "sidecar-only 变更不应触发 restart hook"
    );
    drop(handle);
}

/// 重载波收尾巡检端到端：波次摘掉的声明 http 路由在下一轮 sync 收尾被主动
/// 补注册——无需任何请求触发（惰性 heal 窗消除的整环锁，2026-10-15 酒馆面板
/// 404 消缺）。
#[tokio::test]
#[serial]
async fn watcher_sweep_restores_revoked_http_routes_at_sync_tail() {
    pin_temp_user_root();
    let tmp = tempfile::tempdir().unwrap();
    std::env::set_var("AGENTOS_PLUGINS_DIR", tmp.path());

    // 带 http_endpoints 的 in_process 插件（boot 形态：启动时已发现；无进程模型，
    // 不触 spawn）。in_process 在复验循环头部被排除，sync 其余段零扰动。
    let dir = tmp.path().join("mode_swp");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("plugin.json"),
        r#"{
    "id": "mode_swp", "name": "mode_swp", "version": "1.0.0",
    "plugin_type": "system", "language": "rust",
    "host_type": "in_process", "entry": "mode_swp",
    "capabilities": {},
    "http_endpoints": [{
        "route_id": "d1", "method": "GET", "path": "/ext/mode_swp/data",
        "auth": "none", "handler_capability": "http.handle"
    }]
}"#,
    )
    .unwrap();

    let loader: Arc<dyn PluginLoader> = Arc::new(PluginLoaderImpl::new(tmp.path(), None));
    let invoker = Arc::new(PluginInvokerImpl::new(loader));
    let initial: Vec<_> = invoker.discover_new_plugins().await.unwrap();
    let initial_ids: HashSet<String> = initial.iter().map(|m| m.id.clone()).collect();
    assert!(initial_ids.contains("mode_swp"));

    let registry = Arc::new(CapabilityRegistryImpl::new());
    let scopes = Arc::new(agentos_plugin_loader::PluginScopeRegistry::new());
    // boot 注册声明路由 → 波次收回（uninstall/复验 revoke 语义）。
    let manifest = initial.iter().find(|m| m.id == "mode_swp").unwrap().clone();
    agentos_api::plugin_lifecycle::reenable_plugin_capabilities(&manifest, &registry, &scopes);
    assert!(registry
        .find_http_route("/ext/mode_swp/data", "GET")
        .is_some());
    scopes.revoke("mode_swp");
    assert!(
        registry
            .find_http_route("/ext/mode_swp/data", "GET")
            .is_none(),
        "前置：波次收回后路由缺失"
    );

    let manifests_store: agentos_api::plugin_watcher::ManifestsStore =
        Arc::new(tokio::sync::RwLock::new(initial));
    let enabled = Arc::new(tokio::sync::RwLock::new(initial_ids));
    let handle = PluginWatcher::new(
        tmp.path().to_path_buf(),
        invoker as Arc<dyn agentos_core::traits::PluginInvoker>,
        registry.clone(),
        HashSet::new(),
    )
    .with_scopes(Arc::clone(&scopes))
    .with_manifests_store(manifests_store)
    .with_enabled_ids(enabled)
    .with_debounce(Duration::from_millis(50))
    .spawn();
    handle.trigger.send(()).unwrap();

    // 路由应在 3s 内被波尾巡检补回（零请求触发）。
    let restored = timeout(Duration::from_secs(3), async {
        loop {
            if registry
                .find_http_route("/ext/mode_swp/data", "GET")
                .is_some()
            {
                return Ok::<(), ()>(());
            }
            tokio::time::sleep(Duration::from_millis(30)).await;
        }
    })
    .await;
    assert!(
        restored.is_ok(),
        "波尾巡检应在 sync 收尾补回波次摘掉的声明路由"
    );
}

/// 手动触发一次同步并轮询 flag（最多 3s）。
async fn handle_trigger_and_wait(
    handle: &agentos_api::plugin_watcher::WatcherHandle,
    flag: &Arc<AtomicBool>,
) {
    handle.trigger.send(()).unwrap();
    let _ = timeout(Duration::from_secs(3), async {
        loop {
            if flag.load(Ordering::Relaxed) {
                return Ok::<(), ()>(());
            }
            tokio::time::sleep(Duration::from_millis(30)).await;
        }
    })
    .await;
}

/// 模式包资源按需聚合端到端（ADR 2026-10-15-mode-registry-retirement）：
/// 嵌套装机布局（`modes/<pkg>/`）的模式包经 invoker 递归根收集发现（boot 同源），
/// 约定资源视图 = 活 manifests + loader 路径表 + 盘上现状的按需聚合——热增
/// agents 文件下次聚合即时入键表，全程零注册状态、零刷新机器（原 watcher
/// 增量重扫特例已随 mode_registry 注册状态机退役）。
#[tokio::test]
#[serial]
async fn mode_resources_aggregate_on_demand_from_discovered_packages() {
    pin_temp_user_root();
    let tmp = tempfile::tempdir().unwrap();
    std::env::set_var("AGENTOS_PLUGINS_DIR", tmp.path());
    // 用户模式根钉进 tmp 内隔离：真实用户根的包/损坏文件不得漏进本测试
    // （env 泄漏到后续串行用例无害——目录不存在 = 用户根零包，轮询兜底照常）。
    std::env::set_var("AGENTOS_USER_PLUGINS_DIR", tmp.path().join("user"));

    // 模式包（light sidecar 形态，与出厂种子一致；boot 形态：启动时已发现）
    let pkg = tmp.path().join("modes").join("mode_e2e");
    std::fs::create_dir_all(&pkg).unwrap();
    std::fs::write(
        pkg.join("plugin.json"),
        r#"{
    "id": "mode_e2e", "name": "mode_e2e", "version": "1.0.0",
    "plugin_type": "system", "language": "python",
    "host_type": "sidecar", "entry": "python server.py",
    "capabilities": { "tools": [], "services": [] }
}"#,
    )
    .unwrap();
    let loader = Arc::new(PluginLoaderImpl::new(tmp.path(), None));
    // 发现与 boot 同源：走 invoker 的递归根收集（mode 包嵌套在
    // modes/<pkg>/，loader.discover 的单层扫描看不到）。
    let invoker = Arc::new(PluginInvokerImpl::new(loader.clone()));
    let manifests: Vec<_> = invoker.discover_new_plugins().await.unwrap();
    let enabled: HashSet<String> = manifests.iter().map(|m| m.id.clone()).collect();
    assert!(enabled.contains("mode_e2e"), "模式包应作为插件被发现");

    // 聚合取数 = 生产 schema 装配同款三参：活 manifests + 启用集 + loader 路径表。
    let dirs = loader.get_plugin_dirs();
    let keys = || {
        let (agents, pipelines) =
            agentos_api::mode_view::aggregate_mode_resources(&manifests, &enabled, &dirs);
        (
            agents
                .iter()
                .map(|a| a["key"].as_str().unwrap().to_string())
                .collect::<Vec<_>>(),
            pipelines
                .iter()
                .map(|p| p["key"].as_str().unwrap().to_string())
                .collect::<Vec<_>>(),
        )
    };
    let (agents, pipelines) = keys();
    assert!(
        agents.is_empty() && pipelines.is_empty(),
        "无约定文件 = 零条目"
    );

    // 热增 agents/pipelines 约定文件 → 下次聚合键即时出现（无需注册/重启/刷新）。
    std::fs::create_dir_all(pkg.join("agents")).unwrap();
    std::fs::write(pkg.join("agents/novelist.yaml"), "model_tier: large\n").unwrap();
    std::fs::create_dir_all(pkg.join("pipelines")).unwrap();
    std::fs::write(
        pkg.join("pipelines/main.yaml"),
        "task_kinds: [roleplay.chat]\nsteps: []\n",
    )
    .unwrap();
    let (agents, pipelines) = keys();
    assert_eq!(
        agents,
        vec!["mode_e2e/novelist"],
        "热增文件无需重启即入键表"
    );
    assert_eq!(
        pipelines,
        vec!["mode_e2e/main"],
        "编排键 + task_kinds 随盘上现状派生"
    );
}

// ── 同步冷却（1c 风暴降噪）行为冒烟：真实短时钟（与既有 debounce 测试同习语）──

/// 风暴连发对照：波次间隔 > 防抖窗（各成波）但 < 冷却窗 → sync 频率被冷却钳住。
/// 对照组（冷却 ≈0）波次各跑一整轮；处理组（冷却 250ms）合并显著降频。
#[tokio::test]
#[serial]
async fn storm_burst_coalesced_by_sync_cooldown() {
    pin_temp_user_root();
    let tmp = tempfile::tempdir().unwrap();
    std::env::set_var("AGENTOS_PLUGINS_DIR", tmp.path());

    let make_watcher = |cooldown: Duration| {
        let loader: Arc<dyn PluginLoader> = Arc::new(PluginLoaderImpl::new(tmp.path(), None));
        let invoker = Arc::new(PluginInvokerImpl::new(loader));
        let registry = Arc::new(CapabilityRegistryImpl::new());
        PluginWatcher::new(
            tmp.path().to_path_buf(),
            invoker as Arc<dyn agentos_core::traits::PluginInvoker>,
            registry,
            HashSet::new(),
        )
        .with_debounce(Duration::from_millis(10))
        .with_sync_cooldown(cooldown)
        .spawn()
    };

    // 波次间隔 50ms：> 防抖 10ms（各成一波）< 冷却 250ms（应被合并）。
    async fn send_burst(handle: &WatcherHandle) {
        for _ in 0..6 {
            handle.trigger.send(()).unwrap();
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }

    // 对照组：冷却 ≈0 → 波次各跑一整轮（≥5 次）。
    let without = make_watcher(Duration::from_millis(1));
    send_burst(&without).await;
    tokio::time::sleep(Duration::from_millis(300)).await;
    let without_count = without.sync_count.load(Ordering::Relaxed);
    assert!(
        without_count >= 5,
        "对照组（无冷却）应波波全量重扫，实得 {without_count}"
    );

    // 处理组：冷却 250ms → 风暴合并，sync 次数显著低于波数（300ms 窗口 ≤ 3 轮）。
    let with = make_watcher(Duration::from_millis(250));
    send_burst(&with).await;
    tokio::time::sleep(Duration::from_millis(600)).await;
    let with_count = with.sync_count.load(Ordering::Relaxed);
    assert!(
        with_count <= 4,
        "处理组（冷却 250ms）应把 6 波合并到 ≤4 轮，实得 {with_count}"
    );
    assert!(with_count >= 1, "冷却不吞事件：至少执行一轮");
}

/// 单事件：一次触发恰一次 sync（冷却不延迟首次执行）。
#[tokio::test]
#[serial]
async fn single_event_still_syncs_once_promptly() {
    pin_temp_user_root();
    let tmp = tempfile::tempdir().unwrap();
    std::env::set_var("AGENTOS_PLUGINS_DIR", tmp.path());

    let loader: Arc<dyn PluginLoader> = Arc::new(PluginLoaderImpl::new(tmp.path(), None));
    let invoker = Arc::new(PluginInvokerImpl::new(loader));
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let handle = PluginWatcher::new(
        tmp.path().to_path_buf(),
        invoker as Arc<dyn agentos_core::traits::PluginInvoker>,
        registry,
        HashSet::new(),
    )
    .with_debounce(Duration::from_millis(10))
    .with_sync_cooldown(Duration::from_millis(250))
    .spawn();

    handle.trigger.send(()).unwrap();
    // 首轮无 last_sync_done → 不等待冷却，防抖后立即执行。
    let appeared = timeout(Duration::from_secs(3), async {
        while handle.sync_count.load(Ordering::Relaxed) < 1 {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await;
    assert!(
        appeared.is_ok(),
        "单事件应在 3s 内完成首轮 sync（冷却不吞首波）"
    );
}

/// 无变化轮询退避：连续无变化轮冷却拉长，但上限 = 轮询间隔——轮询节奏不被吞。
#[tokio::test]
#[serial]
async fn idle_polls_keep_polling_rhythm() {
    pin_temp_user_root();
    let tmp = tempfile::tempdir().unwrap();
    std::env::set_var("AGENTOS_PLUGINS_DIR", tmp.path());

    let loader: Arc<dyn PluginLoader> = Arc::new(PluginLoaderImpl::new(tmp.path(), None));
    let invoker = Arc::new(PluginInvokerImpl::new(loader));
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let handle = PluginWatcher::new(
        tmp.path().to_path_buf(),
        invoker as Arc<dyn agentos_core::traits::PluginInvoker>,
        registry,
        HashSet::new(),
    )
    .with_debounce(Duration::from_millis(10))
    .with_poll_interval(Duration::from_millis(150))
    .with_sync_cooldown(Duration::from_millis(300))
    .spawn();

    // 800ms ≥ 3 个 poll tick（150ms）：退避封顶 = poll 间隔，节奏不被吞。
    tokio::time::sleep(Duration::from_millis(800)).await;
    let count = handle.sync_count.load(Ordering::Relaxed);
    assert!(
        count >= 2,
        "无变化期轮询节奏不被冷却吞（800ms/150ms ≥ 3 tick，实得 {count} 轮）"
    );
}
