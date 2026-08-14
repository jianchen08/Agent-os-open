//! 插件目录运行时自动发现（hot-discover）。
//!
//! 启动期一次性扫描之后，内核原先不会感知运行时新增的插件目录：只有客户端显式
//! `POST /api/v1/plugins/reload-all` 才会重扫。本模块补上"触发源"——
//! 用 notify 文件监听 + 轮询兜底（防 Docker volume / WSL / 网络盘上 notify 丢事件）
//! 两条路径，经防抖后串行调用既有 `discover_new_plugins()` + `register_new_plugins()`，
//! 让丢进 `plugins/` 的新插件 tools/route_signals 立即生效，无需重启内核。
//!
//! 设计：把"发现→diff→注册"的核心逻辑下沉为纯函数 [`apply_discovered_plugins`]，
//! 时序/IO（notify、轮询、mpsc）只负责触发，便于无 flaky 单测。
//!
//! [来源: plugins 热更新调研 / reload-all 端点复用]

use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;
use std::time::Duration;

use agentos_core::traits::{CapabilityRegistry, PluginInvoker, PluginManifest};
use agentos_core::types::PluginError;
use agentos_plugin_loader::CapabilityRegistryImpl;
use tokio::sync::mpsc::{self, UnboundedSender};
use tracing::{info, warn};

use crate::plugin_lifecycle::register_new_plugins;

/// 默认防抖窗口：plugin 目录创建会触发多次 notify 事件，收口到 300ms 内合并一次。
pub const DEFAULT_DEBOUNCE: std::time::Duration = std::time::Duration::from_millis(300);

/// 默认轮询兜底间隔：notify 不可靠环境（Docker/WSL）下保底发现新插件。
pub const DEFAULT_POLL_INTERVAL: std::time::Duration = std::time::Duration::from_secs(5);

/// 一次发现同步的结果（纯数据，便于断言）。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SyncReport {
    /// 本次新注册的插件 id（已注册过的不在内）。
    pub new_plugin_ids: Vec<String>,
    /// 本次注册的 tool 总数（仅 plugin_type==Tool 计入）。
    pub tools_registered: usize,
    /// 本次注册的插件 HTTP 端点数（写入 capability_registry → /ext/* catch-all 立即转发）。
    pub http_routes_registered: usize,
}

impl SyncReport {
    /// 无任何新增（用于 consumer 判空跳过日志）。
    pub fn is_empty(&self) -> bool {
        self.new_plugin_ids.is_empty()
    }
}

/// L0 纯函数：给定全量 manifests + 已知 id 集 + registry，注册新增插件的 tools/route_signals。
///
/// 幂等：注册后把新 id 并入 `known_ids`，重复调用不再注册。
/// 无 IO、无时序，可同步单测。复用 [`register_new_plugins`] 与 [`has_http_endpoints`]，
/// 行为对齐 `reload-all` 端点的新插件序列。
pub fn apply_discovered_plugins(
    all_manifests: &[PluginManifest],
    known_ids: &mut HashSet<String>,
    registry: &CapabilityRegistryImpl,
) -> SyncReport {
    // 复用 reload-all 的新插件序列：跳过已知 id，注册 tools/route_signals。
    let (new_ids, tools_registered) = register_new_plugins(all_manifests, known_ids, registry);
    // 注册新插件的 http_endpoints 到 capability_registry。内核用单条 /ext/{*rest} catch-all
    // （server.rs）+ registry 数据驱动分发：端点写进 registry 后立即生效，无需重启、无需
    // 动态挂 axum 路由。register_new_plugins 只处理 tools/route_signals，http_endpoints 在此
    // 补注册（对齐 main 启动期的 register_manifest_http_routes）。
    let new_id_set: HashSet<&String> = new_ids.iter().collect();
    let mut http_routes_registered = 0usize;
    for m in all_manifests {
        if !new_id_set.contains(&m.id) {
            continue;
        }
        for ep in &m.http_endpoints {
            // 冲突（同 path+method 已存在）忽略：新插件 id 唯一，正常不冲突。
            if registry.register_http_route(&m.id, ep.clone()).is_ok() {
                http_routes_registered += 1;
            }
        }
    }
    // 并入已知集，保证幂等：下次同步不再重复注册。
    for id in &new_ids {
        known_ids.insert(id.clone());
    }
    SyncReport {
        new_plugin_ids: new_ids,
        tools_registered,
        http_routes_registered,
    }
}

/// L1 async 编排：拉取全量 manifests → 调 L0 注册新增。
///
/// `invoker.discover_new_plugins()` 内部重扫插件目录（幂等），故本函数可被任意触发源
/// （notify / 轮询 / 手动）反复调用，无新插件时 no-op。
pub async fn sync_once(
    invoker: &dyn PluginInvoker,
    registry: &CapabilityRegistryImpl,
    known_ids: &mut HashSet<String>,
) -> Result<SyncReport, PluginError> {
    let all = invoker.discover_new_plugins().await?;
    Ok(apply_discovered_plugins(&all, known_ids, registry))
}

/// 运行时插件自动发现器（notify watch + 轮询兜底，二选一或并存均可）。
///
/// `spawn` 后返回 [`WatcherHandle`]：`trigger` 可手动注入同步信号（测试 / 外部 API 用），
/// `sync_count` 观察已执行同步次数（断言防抖用）。consumer 任务串行消费触发信号，
/// 独占 `known_ids`（无需锁），故 [`sync_once`] 调用天然无并发竞态。
pub struct PluginWatcher {
    plugins_dir: PathBuf,
    invoker: Arc<dyn PluginInvoker>,
    registry: Arc<CapabilityRegistryImpl>,
    known_ids: HashSet<String>,
    debounce: Duration,
    poll_interval: Duration,
}

impl PluginWatcher {
    pub fn new(
        plugins_dir: PathBuf,
        invoker: Arc<dyn PluginInvoker>,
        registry: Arc<CapabilityRegistryImpl>,
        initial_ids: HashSet<String>,
    ) -> Self {
        Self {
            plugins_dir,
            invoker,
            registry,
            known_ids: initial_ids,
            debounce: DEFAULT_DEBOUNCE,
            poll_interval: DEFAULT_POLL_INTERVAL,
        }
    }

    /// 自定义防抖窗口（测试用短值加速）。
    pub fn with_debounce(mut self, debounce: Duration) -> Self {
        self.debounce = debounce;
        self
    }

    /// 自定义轮询兜底间隔（测试用短值加速）。
    pub fn with_poll_interval(mut self, poll_interval: Duration) -> Self {
        self.poll_interval = poll_interval;
        self
    }

    /// 启动后台 consumer 任务，返回可注入触发源的 handle。
    ///
    /// consumer 逻辑：收到触发信号 → 防抖（窗口内持续有事件就重置，静默 `debounce` 后执行）
    /// → [`sync_once`]。两个自动触发源都只往同一个 mpsc 发 `()`，由 consumer 串行处理，
    /// 避免并发 reload 竞态：
    /// - **notify 文件监听**（低延迟主路径）：plugins_dir 下 Create/Modify 即唤醒；
    /// - **轮询兜底**（可靠性主体）：notify 在 Docker volume / WSL / 网络盘上常丢事件，
    ///   每 `poll_interval` 兜底扫一次。
    pub fn spawn(self) -> WatcherHandle {
        let Self {
            plugins_dir,
            invoker,
            registry,
            known_ids,
            debounce,
            poll_interval,
        } = self;
        let (trigger, mut rx) = mpsc::unbounded_channel::<()>();
        let sync_count = Arc::new(AtomicU32::new(0));
        let sync_count_task = Arc::clone(&sync_count);

        // 循环 E：notify 文件监听（低延迟主路径）。init/watch 失败不致命——轮询兜底。
        let notify_watcher = spawn_notify_watcher(plugins_dir.clone(), trigger.clone());

        // 循环 F：轮询兜底（notify 不可靠环境的可靠性主体）。
        {
            let poll_tx = trigger.clone();
            tokio::spawn(async move {
                let mut tick = tokio::time::interval(poll_interval);
                tick.tick().await; // 跳过首次立即触发
                loop {
                    tick.tick().await;
                    let _ = poll_tx.send(());
                }
            });
        }

        info!(
            target: "plugin_watcher",
            dir = %plugins_dir.display(),
            debounce_ms = debounce.as_millis() as u64,
            poll_secs = poll_interval.as_secs(),
            notify_attached = notify_watcher.is_some(),
            "plugin watcher started (notify+poll)"
        );

        tokio::spawn(async move {
            // 持有 notify_watcher 保活（drop 即停止监听）；与 consumer 同生命周期。
            let _notify = notify_watcher;
            let mut known = known_ids;

            loop {
                // 等首个触发事件；所有 sender drop → 退出。
                if rx.recv().await.is_none() {
                    break;
                }
                // 防抖：窗口内持续有事件就重新等待，静默 debounce 时长后才执行。
                while let Ok(Some(_)) = tokio::time::timeout(debounce, rx.recv()).await {
                    // 匹配到事件即继续等（重置防抖窗口）；超时或 channel 关闭则结束。
                }
                // 执行一次同步（幂等：无新插件则 no-op）。
                let report = match sync_once(invoker.as_ref(), registry.as_ref(), &mut known).await {
                    Ok(r) => r,
                    Err(e) => {
                        warn!(target: "plugin_watcher", error = %e.message, "discover sync failed");
                        continue;
                    }
                };
                sync_count_task.fetch_add(1, Ordering::Relaxed);
                if !report.is_empty() {
                    info!(
                        target: "plugin_watcher",
                        new_plugins = report.new_plugin_ids.len(),
                        tools = report.tools_registered,
                        http_routes = report.http_routes_registered,
                        "auto-discovered new plugins"
                    );
                }
            }
        });

        WatcherHandle {
            _join: (),
            trigger,
            sync_count,
        }
    }
}

/// 建 notify watcher 监听 `plugins_dir`（递归），Create/Modify 事件经临时文件过滤后
/// 往 `tx` 发 `()`。init/watch 失败返回 None（调用方靠轮询兜底）。
///
/// 返回的 watcher 必须被持有以保持监听（drop 即停），由 consumer 任务保活。
fn spawn_notify_watcher(
    plugins_dir: PathBuf,
    tx: UnboundedSender<()>,
) -> Option<notify::RecommendedWatcher> {
    use notify::{EventKind, RecommendedWatcher, RecursiveMode, Watcher};

    let mut watcher = match RecommendedWatcher::new(
        move |res: Result<notify::Event, notify::Error>| {
            if let Ok(event) = res {
                // 新插件 = 新目录 / 新 plugin.json，对应 Create/Modify。
                if matches!(event.kind, EventKind::Create(_) | EventKind::Modify(_)) {
                    // 过滤编辑器临时文件（.swp / ~backup 等）。
                    let noisy = event.paths.iter().any(|p| {
                        p.file_name()
                            .map(|n| {
                                let s = n.to_string_lossy();
                                s.starts_with('.') || s.starts_with('~')
                            })
                            .unwrap_or(false)
                    });
                    if !noisy {
                        let _ = tx.send(());
                    }
                }
            }
        },
        notify::Config::default(),
    ) {
        Ok(w) => w,
        Err(e) => {
            warn!(target: "plugin_watcher", error = %e, "notify watcher init failed; relying on polling fallback");
            return None;
        }
    };
    if let Err(e) = watcher.watch(&plugins_dir, RecursiveMode::Recursive) {
        warn!(target: "plugin_watcher", dir = %plugins_dir.display(), error = %e, "notify watch failed; relying on polling fallback");
        return None;
    }
    info!(target: "plugin_watcher", dir = %plugins_dir.display(), "notify watcher attached");
    Some(watcher)
}

/// watcher 后台任务的 handle。
pub struct WatcherHandle {
    /// 保留 join（生产丢弃；测试可 abort）。字段名下划线前缀避免未用警告。
    pub _join: (),
    /// 触发源：发 `()` 唤醒 consumer 执行一次同步（测试 / 外部 API 注入用）。
    pub trigger: UnboundedSender<()>,
    /// 已执行的同步次数（防抖断言用）。
    pub sync_count: Arc<AtomicU32>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::{CapabilityRegistry, HookContext, LifecycleHook};
    use agentos_core::types::{PluginContext, PluginResult, ToolExecutionResult};
    use async_trait::async_trait;
    use serde_json::json;

    /// 用 JSON 反序列化构造测试 manifest（省去手写全部字段；未给字段走 serde default）。
    fn mk_manifest(id: &str, plugin_type: &str, tools: &[&str], http: bool) -> PluginManifest {
        let tools_json: Vec<_> = tools
            .iter()
            .map(|t| json!({ "name": t, "description": t }))
            .collect();
        let caps = if tools.is_empty() {
            json!({})
        } else {
            json!({ "tools": tools_json })
        };
        let http_eps = if http {
            json!([{
                "route_id": "r", "method": "GET",
                "path": format!("/ext/{}/foo", id),
                "auth": "none", "handler_capability": "http.handle",
            }])
        } else {
            json!([])
        };
        let v = json!({
            "id": id, "name": id, "version": "1.0.0",
            "plugin_type": plugin_type, "language": "rust",
            "host_type": "sidecar", "entry": "x",
            "capabilities": caps,
            "http_endpoints": http_eps,
        });
        serde_json::from_value(v).expect("valid manifest")
    }

    /// 测试用 PluginInvoker：仅 discover_new_plugins 有意义，其余方法不可达。
    /// （仿 invoker.rs 的 MockLoader 风格手写，仓库无 mockall。）
    struct MockInvoker {
        manifests: Vec<PluginManifest>,
        fail: bool,
    }

    #[async_trait]
    impl PluginInvoker for MockInvoker {
        async fn invoke_pipeline_plugin(
            &self,
            _plugin_id: &str,
            _ctx: &PluginContext,
        ) -> Result<PluginResult, PluginError> {
            unimplemented!("sync_once 不走 invoke 路径")
        }
        async fn invoke_tool(
            &self,
            _plugin_id: &str,
            _tool_name: &str,
            _inputs: &serde_json::Value,
        ) -> Result<ToolExecutionResult, PluginError> {
            unimplemented!("sync_once 不走 invoke 路径")
        }
        async fn send_lifecycle_hook(
            &self,
            _plugin_id: &str,
            _hook: LifecycleHook,
            _context: &HookContext,
        ) -> Result<(), PluginError> {
            unimplemented!("sync_once 不走 hook 路径")
        }
        async fn discover_new_plugins(&self) -> Result<Vec<PluginManifest>, PluginError> {
            if self.fail {
                Err(PluginError {
                    message: "discover boom".into(),
                    code: None,
                    source: None,
                })
            } else {
                Ok(self.manifests.clone())
            }
        }
    }

    #[tokio::test]
    async fn sync_once_discovers_and_applies() {
        let invoker = MockInvoker {
            manifests: vec![mk_manifest("a", "tool", &["ta"], false), mk_manifest("b", "tool", &["tb"], false)],
            fail: false,
        };
        let registry = CapabilityRegistryImpl::new();
        let mut known = HashSet::new();
        let report = sync_once(&invoker, &registry, &mut known).await.unwrap();
        assert_eq!(report.new_plugin_ids.len(), 2);
        assert_eq!(report.tools_registered, 2);
        assert_eq!(known.len(), 2);
        assert_eq!(registry.list_tools().len(), 2);
    }

    #[tokio::test]
    async fn sync_once_idempotent_across_calls() {
        let invoker = MockInvoker {
            manifests: vec![mk_manifest("a", "tool", &["ta"], false)],
            fail: false,
        };
        let registry = CapabilityRegistryImpl::new();
        let mut known = HashSet::new();
        let first = sync_once(&invoker, &registry, &mut known).await.unwrap();
        assert_eq!(first.tools_registered, 1);
        let second = sync_once(&invoker, &registry, &mut known).await.unwrap();
        assert!(second.is_empty());
    }

    #[tokio::test]
    async fn sync_once_propagates_discover_error() {
        let invoker = MockInvoker { manifests: vec![], fail: true };
        let registry = CapabilityRegistryImpl::new();
        let mut known = HashSet::new();
        let err = sync_once(&invoker, &registry, &mut known).await.unwrap_err();
        assert!(err.message.contains("boom"));
        assert!(known.is_empty());
        assert!(registry.list_tools().is_empty());
    }

    #[test]
    fn apply_empty_manifests_noop() {
        let registry = CapabilityRegistryImpl::new();
        let mut known = HashSet::new();
        let report = apply_discovered_plugins(&[], &mut known, &registry);
        assert!(report.new_plugin_ids.is_empty());
        assert_eq!(report.tools_registered, 0);
        assert_eq!(report.http_routes_registered, 0);
        assert!(known.is_empty());
        assert!(registry.list_tools().is_empty());
    }

    #[test]
    fn apply_registers_new_tool_plugin_and_updates_known() {
        let registry = CapabilityRegistryImpl::new();
        let mut known = HashSet::new();
        let m = mk_manifest("p1", "tool", &["t1", "t2"], false);
        let report = apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry);
        assert_eq!(report.new_plugin_ids, vec!["p1".to_string()]);
        assert_eq!(report.tools_registered, 2);
        assert!(known.contains("p1"));
        let p1_tools: Vec<_> = registry
            .list_tools()
            .into_iter()
            .filter(|t| t.plugin_id == "p1")
            .collect();
        assert_eq!(p1_tools.len(), 2);
    }

    #[test]
    fn apply_is_idempotent() {
        let registry = CapabilityRegistryImpl::new();
        let mut known = HashSet::new();
        let m = mk_manifest("p1", "tool", &["t1"], false);
        let first = apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry);
        assert_eq!(first.tools_registered, 1);
        // 第二次：p1 已知，应跳过。
        let second = apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry);
        assert!(second.new_plugin_ids.is_empty());
        assert_eq!(second.tools_registered, 0);
    }

    #[test]
    fn apply_skips_known_plugin() {
        let registry = CapabilityRegistryImpl::new();
        let mut known = HashSet::from(["p1".to_string()]);
        let m = mk_manifest("p1", "tool", &["t1"], false);
        let report = apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry);
        assert!(report.new_plugin_ids.is_empty());
        assert_eq!(report.tools_registered, 0);
        assert!(registry.list_tools().is_empty());
    }

    #[test]
    fn apply_registers_http_endpoints_for_new_plugin() {
        let registry = CapabilityRegistryImpl::new();
        let mut known = HashSet::new();
        let m = mk_manifest("p1", "tool", &["t1"], true); // 带 /ext/p1/foo GET
        let report = apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry);
        assert_eq!(report.new_plugin_ids, vec!["p1".to_string()]);
        // http_endpoints 写进 registry → /ext/* catch-all 的 find_http_route 能查到（无需重启）。
        assert!(registry.find_http_route("/ext/p1/foo", "GET").is_some());
        assert_eq!(report.http_routes_registered, 1);
        // tools 仍注册。
        assert_eq!(report.tools_registered, 1);
    }

    #[test]
    fn apply_multiple_new_mixed() {
        let registry = CapabilityRegistryImpl::new();
        let mut known = HashSet::new();
        let m1 = mk_manifest("p1", "tool", &["t1"], false);
        let m2 = mk_manifest("p2", "tool", &["t2"], true);
        let m3 = mk_manifest("p3", "pipeline", &[], false);
        let all = vec![m1, m2, m3];
        let report = apply_discovered_plugins(&all, &mut known, &registry);
        assert_eq!(report.new_plugin_ids.len(), 3);
        // p1/p2 各 1 tool；p3 是 pipeline，其 capabilities.tools 不注册 → 共 2。
        assert_eq!(report.tools_registered, 2);
        // 仅 p2 带 http_endpoints（1 条端点），写进 registry 后 catch-all 立即转发。
        assert_eq!(report.http_routes_registered, 1);
        assert!(registry.find_http_route("/ext/p2/foo", "GET").is_some());
        assert_eq!(known.len(), 3);
    }
}
