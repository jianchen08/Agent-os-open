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

use agentos_api::plugin_watcher::PluginWatcher;
use agentos_core::traits::{CapabilityRegistry, PluginLoader};
use agentos_invoker::PluginInvokerImpl;
use agentos_plugin_loader::{CapabilityRegistryImpl, PluginLoaderImpl};
use serial_test::serial;
use tokio::time::timeout;

/// 在 root 下建一个 tool 类型插件目录（带 tools 能力），写真实 plugin.json。
fn create_plugin_dir(root: &Path, id: &str, tools: &[&str]) {
    let dir = root.join(id);
    std::fs::create_dir_all(&dir).unwrap();
    let tools_json: Vec<String> = tools
        .iter()
        .map(|t| format!(r#"{{"name":"{}","description":"{}"}}"#, t, t))
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
        "burst of 3 triggers must debounce to exactly 1 sync, got {}",
        count
    );
}

/// 循环 F：轮询兜底是可靠性主体——不依赖 notify，即便 notify 丢事件也能发现新插件。
#[tokio::test]
#[serial]
async fn watcher_detects_new_plugin_via_polling_fallback() {
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
    let tmp = tempfile::tempdir().unwrap();
    std::env::set_var("AGENTOS_PLUGINS_DIR", tmp.path());
    // stub sidecar 无 server.py，真实 spawn 必失败；本测试目的是"sidecar-only 不触发
    // 重启 hook"而非验证 G2（G2 严格/宽松已在 plugin_watcher.rs 单测覆盖），故置 lenient。
    std::env::set_var("AGENTOS_G2_STRICT_SPAWN_FAIL", "0");

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
