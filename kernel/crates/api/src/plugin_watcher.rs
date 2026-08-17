//! 插件目录运行时自动发现（hot-discover）。
//!
//! 启动期一次性扫描之后，内核原先不会感知运行时新增的插件目录：只有客户端显式
//! `POST /api/v1/plugins/reload-all` 才会重扫。本模块补上"触发源"——
//! 用 notify 文件监听 + 轮询兜底（防 Docker volume / WSL / 网络盘上 notify 丢事件）
//! 两条路径，经防抖后串行调用既有 `discover_new_plugins()` + `register_new_plugins()`，
//! 让丢进 `plugins/` 的新插件 tools/route_signals 立即生效，无需重启内核。
//!
//! cdylib 集合变更自动重启（剩余项清仓批次 A3）：sidecar 插件可热注册，但
//! InProcess(cdylib) 插件的装/卸/换受 dlclose 死结限制无法热更新（invoker 侧只
//! warn "restart kernel"）。watcher 在每轮 sync 时对比 InProcess 插件 id 集合，
//! 新增或消失都视为变更 → 经注入的 restart hook 触发 G8 优雅重启（排空 + exit 75，
//! 复用 `routes::drain_and_exit75`）。开关：`AGENTOS_AUTO_RESTART_ON_CDYLIB_CHANGE`
//! （默认开，设 `0` 关闭只记日志）；`AGENTOS_DISABLE_SELF_EXIT=1` 逃生门语义由
//! 排空函数内部复用（只排空不退出）。产物文件变化检测（同 id 换 .dll）属 invoker
//! 面，不在此处。
//!
//! 设计：把"发现→diff→注册"的核心逻辑下沉为纯函数 [`apply_discovered_plugins`]，
//! 时序/IO（notify、轮询、mpsc）只负责触发，便于无 flaky 单测。
//!
//! [来源: plugins 热更新调研 / reload-all 端点复用]

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;
use std::time::Duration;

use agentos_core::traits::{CapabilityRegistry, HostType, PluginInvoker, PluginManifest};
use agentos_core::types::PluginError;
use agentos_invoker::verify::{
    compare_tools, declared_with_services, parse_actual_tools, rejected_tool_names,
};
use agentos_plugin_loader::{CapabilityRegistryImpl, PluginScopeRegistry};
use tokio::sync::mpsc::{self, UnboundedSender};
use tokio::sync::RwLock;
use tracing::{info, warn};

use crate::plugin_lifecycle::register_new_plugins;

/// 热发现 manifest 共享存储：watcher 每轮 sync 后把新增插件 manifest 合并进
/// `AppState.manifests`（按 id 去重的增量合并），保证 /api/v1/plugins 状态列表、
/// re-enable 重注册、actions/execute 命令查找等 manifest 消费面与注册表一致。
pub type ManifestsStore = Arc<RwLock<Vec<PluginManifest>>>;

/// 默认防抖窗口：plugin 目录创建会触发多次 notify 事件，收口到 300ms 内合并一次。
pub const DEFAULT_DEBOUNCE: std::time::Duration = std::time::Duration::from_millis(300);

/// 默认轮询兜底间隔：notify 不可靠环境（Docker/WSL）下保底发现新插件。
/// 1 分钟 = notify 失效时的低频兜底；正常环境变更全由 notify 事件驱动，
/// 轮询只兜底不抢跑（曾 5s 固定全量重扫 60+ manifest，无变更也在空转）。
pub const DEFAULT_POLL_INTERVAL: std::time::Duration = std::time::Duration::from_secs(60);

/// cdylib（InProcess）插件集合变更（A3：触发 G8 优雅重启的判据）。
///
/// 只看插件 id 集合的进出（新增/消失）；同 id 换产物文件（.dll 重编译）属
/// invoker 面的 stale 检测，不在此处。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CdylibChange {
    /// 本轮新出现的 InProcess 插件 id。
    pub added: Vec<String>,
    /// 本轮消失（目录被删/manifest 不再扫描到）的 InProcess 插件 id。
    pub removed: Vec<String>,
}

/// 一次发现同步的结果（纯数据，便于断言）。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SyncReport {
    /// 本次新注册的插件 id（已注册过的不在内）。
    pub new_plugin_ids: Vec<String>,
    /// 本次注册的 tool 总数（仅 plugin_type==Tool 计入）。
    pub tools_registered: usize,
    /// 本次注册的插件 HTTP 端点数（写入 capability_registry → /ext/* catch-all 立即转发）。
    pub http_routes_registered: usize,
    /// G2：本次发现但**拒绝注册漂移工具**的插件 id（安装期一致性校验——声明 vs
    /// 实际暴露不一致的贡献被拒绝，其余能力照常注册；报告可见不静默）。
    pub drifted_plugins: Vec<String>,
    /// GAP-6：本轮检测到 manifest 变更并**已重注册**的插件 id（指纹对比）。
    pub changed_plugin_ids: Vec<String>,
    /// A3：本轮检测到的 InProcess(cdylib) 插件集合变更（无变更为 None）。
    /// consumer 据此经 restart hook 触发 G8 优雅重启。
    pub cdylib_change: Option<CdylibChange>,
}

impl SyncReport {
    /// 无任何新增（用于 consumer 判空跳过日志）。
    pub fn is_empty(&self) -> bool {
        self.new_plugin_ids.is_empty() && self.changed_plugin_ids.is_empty()
    }
}

/// L0 纯函数：对比本轮全量 manifests 的 InProcess 插件 id 集合与已知集合。
///
/// `known` 为 `None` 时本轮只建立基线（返回 `None`，不触发重启）——避免"启动后
/// 首轮 sync 把启动期已存在的 cdylib 插件误判为新增"。之后每轮 diff：
/// 新增或消失都返回 `Some(CdylibChange)`，并把 `known` 推进为本轮集合。
/// 无 IO、无时序，可同步单测。
pub fn diff_cdylib_change(
    all_manifests: &[PluginManifest],
    known: &mut Option<HashSet<String>>,
) -> Option<CdylibChange> {
    let current: HashSet<String> = all_manifests
        .iter()
        .filter(|m| m.host_type == HostType::InProcess)
        .map(|m| m.id.clone())
        .collect();
    match known {
        None => {
            *known = Some(current);
            None
        }
        Some(prev) => {
            let added: Vec<String> = current.difference(prev).cloned().collect();
            let removed: Vec<String> = prev.difference(&current).cloned().collect();
            let change = if added.is_empty() && removed.is_empty() {
                None
            } else {
                Some(CdylibChange { added, removed })
            };
            *prev = current;
            change
        }
    }
}

/// 解析 `AGENTOS_AUTO_RESTART_ON_CDYLIB_CHANGE`（A3 开关，默认开）。
///
/// 未设 / 设任意非 "0" 值 → 开；仅显式 `0`（trim 后）关闭。
/// 纯函数（吃 Option<String> 而非直接读 env）——避免测试设置进程级环境变量的
/// 并发污染。
pub fn auto_restart_env_enabled(value: Option<String>) -> bool {
    !matches!(value, Some(v) if v.trim() == "0")
}

/// 检测到 cdylib 集合变更后的重启决策（纯逻辑，可单测）。
///
/// `enabled=false`（env 开关关闭）或无 hook 接线时只记日志不动作，返回是否触发。
/// hook 由装配方（agentos-kernel bin）注入：内部调 `routes::drain_and_exit75`
/// （排空在途 runs → AGENTOS_DISABLE_SELF_EXIT 未设时延迟 exit 75）。
fn trigger_cdylib_restart_if_enabled(
    change: &CdylibChange,
    hook: &Option<Arc<dyn Fn() + Send + Sync>>,
    enabled: bool,
) -> bool {
    if !enabled {
        warn!(
            target: "plugin_watcher",
            added = ?change.added,
            removed = ?change.removed,
            "InProcess(cdylib) plugin set changed but auto-restart disabled (AGENTOS_AUTO_RESTART_ON_CDYLIB_CHANGE=0); manual restart required"
        );
        return false;
    }
    match hook {
        Some(h) => {
            info!(
                target: "plugin_watcher",
                added = ?change.added,
                removed = ?change.removed,
                "InProcess(cdylib) plugin set changed -> triggering G8 graceful restart (drain + exit 75)"
            );
            h();
            true
        }
        None => {
            warn!(
                target: "plugin_watcher",
                added = ?change.added,
                removed = ?change.removed,
                "InProcess(cdylib) plugin set changed but no restart hook wired; manual restart required"
            );
            false
        }
    }
}

/// L0 纯函数：给定全量 manifests + 已知 id 集 + registry，注册新增插件的 tools/route_signals。
///
/// 幂等：注册后把新 id 并入 `known_ids`，重复调用不再注册。
/// 无 IO、无时序，可同步单测。复用 [`register_new_plugins`] 与 [`has_http_endpoints`]，
/// 行为对齐 `reload-all` 端点的新插件序列。
/// M1：`scopes` 为 Some 时经 guarded 注册入 scope（disable 时结构性收回）。
pub fn apply_discovered_plugins(
    all_manifests: &[PluginManifest],
    known_ids: &mut HashSet<String>,
    registry: &Arc<CapabilityRegistryImpl>,
    scopes: Option<&PluginScopeRegistry>,
) -> SyncReport {
    // 复用 reload-all 的新插件序列：跳过已知 id，注册 tools/route_signals。
    let (new_ids, tools_registered) =
        register_new_plugins(all_manifests, known_ids, registry, scopes);
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
        let scope = scopes.map(|s| s.scope_of(&m.id));
        for ep in &m.http_endpoints {
            // 冲突（同 path+method 已存在）忽略：新插件 id 唯一，正常不冲突。
            let ok = match &scope {
                Some(s) => registry
                    .register_http_route_guarded(&m.id, ep.clone())
                    .map(|(_d, guard)| s.track(guard))
                    .is_ok(),
                None => registry.register_http_route(&m.id, ep.clone()).is_ok(),
            };
            if ok {
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
        drifted_plugins: Vec::new(),
        changed_plugin_ids: Vec::new(),
        cdylib_change: None,
    }
}

/// L1 async 编排：拉取全量 manifests → 调 L0 注册新增 + cdylib 集合 diff。
///
/// `invoker.discover_new_plugins()` 内部重扫插件目录（幂等），故本函数可被任意触发源
/// （notify / 轮询 / 手动）反复调用，无新插件时 no-op。
///
/// `known_cdylib`：InProcess 插件 id 已知集合（`None` = 基线未建立，本轮只建基线）。
pub async fn sync_once(
    invoker: &dyn PluginInvoker,
    registry: &Arc<CapabilityRegistryImpl>,
    scopes: Option<&PluginScopeRegistry>,
    known_ids: &mut HashSet<String>,
    known_cdylib: &mut Option<HashSet<String>>,
) -> Result<SyncReport, PluginError> {
    // GAP-6：变更检测需跨调用持久基线；本便捷入口一次性 map（首轮建基线），
    // 生产路径走 consumer 循环的常驻 map（sync_once_with_store 直用）。
    let mut throwaway = HashMap::new();
    sync_once_with_store(
        invoker,
        registry,
        scopes,
        known_ids,
        known_cdylib,
        None,
        &mut throwaway,
    )
    .await
}

/// manifest 内容指纹（GAP-6 变更检测）：确定性序列化全文哈希。
///
/// 与 mtime/源码指纹（invoker 的 respawn 判定）不同——这里只关心 manifest
/// 声明本身是否变化（工具清单/entry/args/http_endpoints 等）。
/// 序列化前递归按键排序：`McpEndpoint.env` 等 HashMap 字段每次反序列化迭代
/// 顺序随机，直接 `to_string` 会让内容相同的 manifest 指纹漂移（omnisearch
/// 8 键 env 被 watcher 每轮误判"变更"重注册就是它），键序化后指纹只随内容变化。
fn manifest_fingerprint(m: &PluginManifest) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    // PluginManifest 为纯数据（全 String/Vec 字段），序列化不可能失败；若失败
    // 宁可 panic 也不能退空串——空串会让所有 manifest 指纹相同，变更检测静默失效。
    deterministic_json(&serde_json::to_value(m).expect("PluginManifest serialization is infallible"))
        .hash(&mut h);
    h.finish()
}

/// 把 `Value` 序列化为键序唯一的字符串（对象键递归升序），用作内容指纹。
fn deterministic_json(v: &serde_json::Value) -> String {
    match v {
        serde_json::Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            let inner = keys
                .iter()
                .map(|k| format!("{:?}:{}", k, deterministic_json(&map[*k])))
                .collect::<Vec<_>>()
                .join(",");
            format!("{{{}}}", inner)
        }
        serde_json::Value::Array(arr) => {
            let inner = arr
                .iter()
                .map(deterministic_json)
                .collect::<Vec<_>>()
                .join(",");
            format!("[{}]", inner)
        }
        other => other.to_string(),
    }
}

/// [`sync_once`] 的 store 感知变体：`manifests_store` 传入 `AppState.manifests`
/// 共享句柄时，本轮新注册插件的 manifest 会增量合并进 store（按 id 去重），
/// 修复热发现后状态列表/重启用等 manifest 消费面看不到新插件的不一致。
/// 只增不删：目录删除的卸载语义（工具摘除）仍是 watcher 已知 gap，不在此处扩大。
pub async fn sync_once_with_store(
    invoker: &dyn PluginInvoker,
    registry: &Arc<CapabilityRegistryImpl>,
    scopes: Option<&PluginScopeRegistry>,
    known_ids: &mut HashSet<String>,
    known_cdylib: &mut Option<HashSet<String>>,
    manifests_store: Option<&ManifestsStore>,
    known_manifest_hashes: &mut HashMap<String, u64>,
) -> Result<SyncReport, PluginError> {
    let all = invoker.discover_new_plugins().await?;
    // A3：InProcess(cdylib) 集合 diff 先行（下方 for 循环会 move `all`）。
    // 首轮建基线，之后新增/消失都报变更；用全量 `all`（G2 过滤只动 tools，
    // 不动 host_type 归属）。
    let cdylib_change = diff_cdylib_change(&all, known_cdylib);
    // G2 安装期一致性校验：对新发现的 tool 插件 spawn → tools/list → 对照
    // manifest 声明。漂移工具的贡献**拒绝注册**（克隆 manifest 剔除该工具），
    // 其余能力照常；校验失败（spawn 失败等）不阻断安装（warn 记录）。
    let mut filtered: Vec<PluginManifest> = Vec::with_capacity(all.len());
    let mut drifted_plugins = Vec::new();
    for m in all {
        if known_ids.contains(&m.id) || m.capabilities.tools.is_empty() {
            filtered.push(m);
            continue;
        }
        match invoker.list_plugin_tools(&m.id).await {
            Ok(raw) => {
                let (actual, _malformed) = parse_actual_tools(&raw);
                let mismatches = compare_tools(&declared_with_services(&m), &actual);
                let rejected = rejected_tool_names(&mismatches);
                if rejected.is_empty() {
                    if !mismatches.is_empty() {
                        // 仅 undeclared（实际多暴露）——不拒绝注册，但记录
                        warn!(
                            target: "plugin_watcher",
                            plugin = %m.id,
                            mismatches = mismatches.len(),
                            "G2 校验：插件存在未声明暴露的工具（不拒绝注册）"
                        );
                    }
                    filtered.push(m);
                } else {
                    warn!(
                        target: "plugin_watcher",
                        plugin = %m.id,
                        rejected = ?rejected,
                        "G2 校验：插件声明与实现漂移，拒绝注册漂移工具（其余能力照常）"
                    );
                    drifted_plugins.push(m.id.clone());
                    let mut sanitized = m;
                    sanitized
                        .capabilities
                        .tools
                        .retain(|t| !rejected.contains(&t.name));
                    filtered.push(sanitized);
                }
            }
            Err(e) => {
                warn!(
                    target: "plugin_watcher",
                    plugin = %m.id,
                    error = %e.message,
                    "G2 校验失败（spawn/上报不可用），不阻断安装"
                );
                filtered.push(m);
            }
        }
    }
    let mut report = apply_discovered_plugins(&filtered, known_ids, registry, scopes);
    report.drifted_plugins = drifted_plugins;
    report.cdylib_change = cdylib_change;

    // ── GAP-6：既有插件 manifest 变更 → 重注册 ──────────────────────────
    // watcher 此前只处理"新目录"（known_ids 之外），既有插件的 plugin.json
    // 变更（工具清单/entry/args）既不重建注册数据也不刷新 manifests store——
    // e2e 实测改 manifest 后 validate-all 仍用旧条目，须重启内核。
    // 指纹 = manifest 序列化内容哈希（纯内容比对，与 mtime 无关）。
    // 变更动作复用 re-enable 路径：revoke 旧 scope（guard drop 真撤销）→
    // 按新 manifest 重注册 tools/route_signals/http_endpoints + 替换 store 条目。
    // in_process(cdylib) 插件不在此列——代码变更走 A3 优雅重启路径整体重建。
    let mut changed_plugin_ids: Vec<String> = Vec::new();
    for m in &filtered {
        if !known_ids.contains(&m.id) || m.host_type == HostType::InProcess {
            continue;
        }
        let fp = manifest_fingerprint(m);
        let changed = match known_manifest_hashes.get(&m.id) {
            Some(old_fp) => *old_fp != fp,
            None => {
                // 无基线（升级前注册/异序）：建基线不动作，避免首轮误重注册
                known_manifest_hashes.insert(m.id.clone(), fp);
                continue;
            }
        };
        if !changed {
            continue;
        }
        let (tools, http_routes) =
            crate::plugin_lifecycle::reenable_plugin_capabilities(m, registry, scopes);
        if let Some(store) = manifests_store {
            let mut guard = store.write().await;
            match guard.iter_mut().find(|x| x.id == m.id) {
                Some(slot) => *slot = m.clone(),
                None => guard.push(m.clone()),
            }
        }
        info!(
            target: "plugin_watcher",
            plugin = %m.id,
            tools, http_routes,
            "manifest 变更已重注册（无需重启内核）"
        );
        changed_plugin_ids.push(m.id.clone());
        known_manifest_hashes.insert(m.id.clone(), fp);
    }
    // 新注册插件建立指纹基线（下轮起参与变更检测）
    for id in &report.new_plugin_ids {
        if let Some(m) = filtered.iter().find(|x| &x.id == id) {
            known_manifest_hashes.insert(id.clone(), manifest_fingerprint(m));
        }
    }
    report.changed_plugin_ids = changed_plugin_ids;
    // 增量合并新插件 manifest 进共享 store（AppState.manifests），保证状态列表 /
    // re-enable 重注册 / actions 命令查找与注册表一致。幂等：按 id 去重。
    if let Some(store) = manifests_store {
        let mut guard = store.write().await;
        for m in &filtered {
            if !guard.iter().any(|x| x.id == m.id) {
                guard.push(m.clone());
            }
        }
    }
    Ok(report)
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
    /// M1：guard 化注册的 scope 表（None = 旧路径不入账本）。
    scopes: Option<Arc<PluginScopeRegistry>>,
    known_ids: HashSet<String>,
    /// GAP-6：plugin_id → manifest 内容指纹（变更检测基线，consumer 独占）。
    known_manifest_hashes: HashMap<String, u64>,
    /// A3：InProcess 插件 id 已知集合（None = 未建基线，首轮 sync 建立）。
    known_cdylib: Option<HashSet<String>>,
    /// A3：cdylib 集合变更时的重启回调（None = 只记日志；bin 装配时注入，
    /// 内部走 routes::drain_and_exit75——排空 + exit 75 / 逃生门）。
    restart_hook: Option<Arc<dyn Fn() + Send + Sync>>,
    /// 热发现 manifest 共享存储（bin 接线时传 `AppState.manifests`）。
    /// None = 不同步 manifest 列表（旧行为，仅测试场景）。
    manifests_store: Option<ManifestsStore>,
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
            scopes: None,
            known_ids: initial_ids,
            known_manifest_hashes: HashMap::new(),
            known_cdylib: None,
            restart_hook: None,
            manifests_store: None,
            debounce: DEFAULT_DEBOUNCE,
            poll_interval: DEFAULT_POLL_INTERVAL,
        }
    }

    /// M1：注入 scope 表（guarded 注册入账本，disable 时结构性收回）。
    pub fn with_scopes(mut self, scopes: Arc<PluginScopeRegistry>) -> Self {
        self.scopes = Some(scopes);
        self
    }

    /// A3：注入启动期 InProcess 插件 id 基线（boot manifests 派生）。
    ///
    /// 显式给基线时首轮 sync 即可 diff（能捕捉 boot→首轮 sync 窗口内的变更）；
    /// 不给则首轮只建基线（保守，不误触发）。
    pub fn with_initial_cdylib_ids(mut self, ids: HashSet<String>) -> Self {
        self.known_cdylib = Some(ids);
        self
    }

    /// A3：注入 cdylib 集合变更的重启回调（生产由 bin 接线 drain_and_exit75）。
    pub fn with_restart_hook(mut self, hook: Arc<dyn Fn() + Send + Sync>) -> Self {
        self.restart_hook = Some(hook);
        self
    }

    /// 注入 `AppState.manifests` 共享句柄：每轮 sync 后把新发现插件的 manifest
    /// 增量合并进去（/api/v1/plugins 状态列表与注册表保持一致）。
    pub fn with_manifests_store(mut self, store: ManifestsStore) -> Self {
        self.manifests_store = Some(store);
        self
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
            scopes,
            known_ids,
            known_manifest_hashes,
            known_cdylib,
            restart_hook,
            manifests_store,
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
            let mut known_hashes = known_manifest_hashes;
            let mut known_cdylib = known_cdylib;

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
                let report = match sync_once_with_store(
                    invoker.as_ref(),
                    &registry,
                    scopes.as_deref(),
                    &mut known,
                    &mut known_cdylib,
                    manifests_store.as_ref(),
                    &mut known_hashes,
                )
                .await
                {
                    Ok(r) => r,
                    Err(e) => {
                        warn!(target: "plugin_watcher", error = %e.message, "discover sync failed");
                        continue;
                    }
                };
                sync_count_task.fetch_add(1, Ordering::Relaxed);
                if !report.is_empty() || !report.drifted_plugins.is_empty() {
                    info!(
                        target: "plugin_watcher",
                        new_plugins = report.new_plugin_ids.len(),
                        tools = report.tools_registered,
                        http_routes = report.http_routes_registered,
                        drifted = report.drifted_plugins.len(),
                        "auto-discovered new plugins (G2 drift-rejected: {:?})",
                        report.drifted_plugins
                    );
                }
                // A3：cdylib 集合变更 → 优雅重启（env 开关 + 注入 hook；无 hook 只记日志）。
                if let Some(change) = &report.cdylib_change {
                    let enabled = auto_restart_env_enabled(
                        std::env::var("AGENTOS_AUTO_RESTART_ON_CDYLIB_CHANGE").ok(),
                    );
                    trigger_cdylib_restart_if_enabled(change, &restart_hook, enabled);
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

/// 判定 notify 事件是否值得触发一次插件重扫：只关心 plugin.json 的 增/改
/// （GAP-6 manifest 变更重注册主路径）与**新目录创建**（新插件根）。
/// 其余文件（插件源码、llm_core/logs/payload_diag 等运行时产物、编辑器临时
/// 文件）不触发——watcher 只看 manifest 声明；sidecar 代码变更由 invoker 的
/// respawn 指纹判定，不走这里。
fn notify_event_relevant(kind: notify::EventKind, paths: &[std::path::PathBuf]) -> bool {
    match kind {
        notify::EventKind::Create(notify::event::CreateKind::Folder) => true,
        notify::EventKind::Create(_) | notify::EventKind::Modify(_) => paths
            .iter()
            .any(|p| p.file_name().map(|n| n == "plugin.json").unwrap_or(false)),
        _ => false,
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
    use notify::{RecommendedWatcher, RecursiveMode, Watcher};

    let mut watcher = match RecommendedWatcher::new(
        move |res: Result<notify::Event, notify::Error>| {
            if let Ok(event) = res {
                if notify_event_relevant(event.kind, &event.paths) {
                    let _ = tx.send(());
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

    /// 测试用 PluginInvoker：仅 discover_new_plugins / list_plugin_tools 有意义，
    /// 其余方法不可达。（仿 invoker.rs 的 MockLoader 风格手写，仓库无 mockall。）
    struct MockInvoker {
        manifests: Vec<PluginManifest>,
        fail: bool,
        /// G2：list_plugin_tools 的可编程响应（plugin_id → tools/list 原始 JSON）。
        /// 缺省 = 返回空工具列表（等价"声明有实际无"——测试中显式给）。
        list_tools: std::collections::HashMap<String, serde_json::Value>,
        /// 置 true 时 list_plugin_tools 返回错误（模拟 spawn/上报失败）。
        list_tools_fail: bool,
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
        async fn list_plugin_tools(
            &self,
            plugin_id: &str,
        ) -> Result<serde_json::Value, PluginError> {
            if self.list_tools_fail {
                return Err(PluginError {
                    message: "list_tools boom".into(),
                    code: None,
                    source: None,
                });
            }
            Ok(self
                .list_tools
                .get(plugin_id)
                .cloned()
                .unwrap_or(serde_json::json!({ "tools": [] })))
        }
    }

    impl MockInvoker {
        /// 构造并**自动回填与 manifest 一致的上报**（G2 默认一致——漂移场景测试
        /// 再显式覆盖 `list_tools` 或置 `list_tools_fail`）。
        fn new(manifests: Vec<PluginManifest>) -> Self {
            let mut list_tools = std::collections::HashMap::new();
            for m in &manifests {
                list_tools.insert(
                    m.id.clone(),
                    json!({ "tools": m.capabilities.tools.iter().map(|t| {
                        json!({"name": t.name, "description": t.description})
                    }).collect::<Vec<_>>() }),
                );
            }
            Self {
                manifests,
                fail: false,
                list_tools,
                list_tools_fail: false,
            }
        }
    }

    #[tokio::test]
    async fn sync_once_discovers_and_applies() {
        let invoker = MockInvoker::new(vec![
            mk_manifest("a", "tool", &["ta"], false),
            mk_manifest("b", "tool", &["tb"], false),
        ]);
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let report = sync_once(&invoker, &registry_arc, None, &mut known, &mut None)
            .await
            .unwrap();
        assert_eq!(report.new_plugin_ids.len(), 2);
        assert_eq!(report.tools_registered, 2);
        assert_eq!(known.len(), 2);
        assert_eq!(registry_arc.list_tools().len(), 2);
    }

    #[tokio::test]
    async fn sync_once_idempotent_across_calls() {
        let invoker = MockInvoker::new(vec![mk_manifest("a", "tool", &["ta"], false)]);
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let first = sync_once(&invoker, &registry_arc, None, &mut known, &mut None)
            .await
            .unwrap();
        assert_eq!(first.tools_registered, 1);
        let second = sync_once(&invoker, &registry_arc, None, &mut known, &mut None)
            .await
            .unwrap();
        assert!(second.is_empty());
    }

    #[tokio::test]
    async fn sync_once_propagates_discover_error() {
        let invoker = MockInvoker {
            manifests: vec![],
            fail: true,
            list_tools: std::collections::HashMap::new(),
            list_tools_fail: false,
        };
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let err = sync_once(&invoker, &registry_arc, None, &mut known, &mut None)
            .await
            .unwrap_err();
        assert!(err.message.contains("boom"));
        assert!(known.is_empty());
        assert!(registry_arc.list_tools().is_empty());
    }

    #[test]
    fn apply_empty_manifests_noop() {
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let report = apply_discovered_plugins(&[], &mut known, &registry_arc, None);
        assert!(report.new_plugin_ids.is_empty());
        assert_eq!(report.tools_registered, 0);
        assert_eq!(report.http_routes_registered, 0);
        assert!(known.is_empty());
        assert!(registry_arc.list_tools().is_empty());
    }

    #[test]
    fn apply_registers_new_tool_plugin_and_updates_known() {
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let m = mk_manifest("p1", "tool", &["t1", "t2"], false);
        let report =
            apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry_arc, None);
        assert_eq!(report.new_plugin_ids, vec!["p1".to_string()]);
        assert_eq!(report.tools_registered, 2);
        assert!(known.contains("p1"));
        let p1_tools: Vec<_> = registry_arc
            .list_tools()
            .into_iter()
            .filter(|t| t.plugin_id == "p1")
            .collect();
        assert_eq!(p1_tools.len(), 2);
    }

    #[test]
    fn apply_is_idempotent() {
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let m = mk_manifest("p1", "tool", &["t1"], false);
        let first =
            apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry_arc, None);
        assert_eq!(first.tools_registered, 1);
        // 第二次：p1 已知，应跳过。
        let second =
            apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry_arc, None);
        assert!(second.new_plugin_ids.is_empty());
        assert_eq!(second.tools_registered, 0);
    }

    #[test]
    fn apply_skips_known_plugin() {
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::from(["p1".to_string()]);
        let m = mk_manifest("p1", "tool", &["t1"], false);
        let report =
            apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry_arc, None);
        assert!(report.new_plugin_ids.is_empty());
        assert_eq!(report.tools_registered, 0);
        assert!(registry_arc.list_tools().is_empty());
    }

    #[test]
    fn apply_registers_http_endpoints_for_new_plugin() {
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let m = mk_manifest("p1", "tool", &["t1"], true); // 带 /ext/p1/foo GET
        let report =
            apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry_arc, None);
        assert_eq!(report.new_plugin_ids, vec!["p1".to_string()]);
        // http_endpoints 写进 registry → /ext/* catch-all 的 find_http_route 能查到（无需重启）。
        assert!(registry_arc.find_http_route("/ext/p1/foo", "GET").is_some());
        assert_eq!(report.http_routes_registered, 1);
        // tools 仍注册。
        assert_eq!(report.tools_registered, 1);
    }

    /// G2：新插件上报与声明一致 → 全部注册，drifted_plugins 为空。
    #[tokio::test]
    async fn sync_once_consistent_plugin_registers_all_tools() {
        let invoker = MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1", "t2"], false)]);
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let report = sync_once(&invoker, &registry_arc, None, &mut known, &mut None)
            .await
            .unwrap();
        assert_eq!(report.tools_registered, 2);
        assert!(report.drifted_plugins.is_empty());
        assert_eq!(registry_arc.list_tools().len(), 2);
    }

    /// G2：新插件声明有、实际无（missing）→ 漂移工具拒绝注册，其余照常。
    #[tokio::test]
    async fn sync_once_drifted_tool_is_rejected_from_registration() {
        let mut invoker =
            MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1", "ghost"], false)]);
        // 覆盖上报：只报 t1（ghost 声明有实际无 → missing 漂移）
        invoker.list_tools.insert(
            "p1".into(),
            json!({ "tools": [{"name": "t1", "description": "t1"}] }),
        );
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let report = sync_once(&invoker, &registry_arc, None, &mut known, &mut None)
            .await
            .unwrap();
        assert_eq!(
            report.drifted_plugins,
            vec!["p1".to_string()],
            "漂移插件报告可见"
        );
        assert_eq!(
            report.tools_registered, 1,
            "漂移工具 ghost 被拒绝，t1 照常注册"
        );
        let names: Vec<String> = registry_arc
            .list_tools()
            .into_iter()
            .map(|t| t.name)
            .collect();
        assert_eq!(names, vec!["t1".to_string()]);
    }

    /// G2：校验失败（list_tools 报错）不阻断安装（工具照常注册 + warn）。
    #[tokio::test]
    async fn sync_once_verify_failure_does_not_block_install() {
        let mut invoker = MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1"], false)]);
        invoker.list_tools_fail = true;
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let report = sync_once(&invoker, &registry_arc, None, &mut known, &mut None)
            .await
            .unwrap();
        assert!(report.drifted_plugins.is_empty());
        assert_eq!(report.tools_registered, 1, "校验失败不拒绝注册");
    }

    #[test]
    fn apply_multiple_new_mixed() {
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let m1 = mk_manifest("p1", "tool", &["t1"], false);
        let m2 = mk_manifest("p2", "tool", &["t2"], true);
        let m3 = mk_manifest("p3", "pipeline", &[], false);
        let all = vec![m1, m2, m3];
        let report = apply_discovered_plugins(&all, &mut known, &registry_arc, None);
        assert_eq!(report.new_plugin_ids.len(), 3);
        // p1/p2 各 1 tool；p3 是 pipeline，其 capabilities.tools 不注册 → 共 2。
        assert_eq!(report.tools_registered, 2);
        // 仅 p2 带 http_endpoints（1 条端点），写进 registry 后 catch-all 立即转发。
        assert_eq!(report.http_routes_registered, 1);
        assert!(registry_arc.find_http_route("/ext/p2/foo", "GET").is_some());
        assert_eq!(known.len(), 3);
    }

    // ── A3：cdylib 集合变更检测 / 自动重启开关 ─────────────────

    /// 构造指定 host_type 的 manifest。
    fn mk_manifest_host(id: &str, host_type: &str) -> PluginManifest {
        let v = json!({
            "id": id, "name": id, "version": "1.0.0",
            "plugin_type": "tool", "language": "rust",
            "host_type": host_type, "entry": "x",
            "capabilities": {},
        });
        serde_json::from_value(v).expect("valid manifest")
    }

    /// 首轮只建基线：启动期已存在的 cdylib 插件不误判为新增。
    #[test]
    fn diff_first_round_establishes_baseline() {
        let all = vec![mk_manifest_host("native_a", "in_process")];
        let mut known: Option<HashSet<String>> = None;
        assert_eq!(diff_cdylib_change(&all, &mut known), None);
        assert_eq!(known, Some(HashSet::from(["native_a".to_string()])));
    }

    /// 显式基线（with_initial_cdylib_ids 路径）：首轮即可 diff 出新增。
    #[tokio::test]
    async fn sync_once_with_explicit_baseline_detects_addition() {
        let invoker = MockInvoker::new(vec![mk_manifest_host("native_a", "in_process")]);
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let mut known_cdylib = Some(HashSet::new()); // boot 期无 cdylib
        let report = sync_once(&invoker, &registry_arc, None, &mut known, &mut known_cdylib)
            .await
            .unwrap();
        assert_eq!(
            report.cdylib_change,
            Some(CdylibChange {
                added: vec!["native_a".to_string()],
                removed: vec![],
            })
        );
    }

    /// 新增 + 消失都算变更；sidecar 插件进出不算。
    #[test]
    fn diff_detects_addition_and_removal_ignoring_sidecar() {
        let mut known: Option<HashSet<String>> = None;
        // 基线：native_a + sidecar_x
        let base = vec![
            mk_manifest_host("native_a", "in_process"),
            mk_manifest_host("sidecar_x", "sidecar"),
        ];
        assert_eq!(diff_cdylib_change(&base, &mut known), None);
        // 本轮：native_a 消失、native_b 新增、sidecar_x→sidecar_y（sidecar 不算）
        let next = vec![
            mk_manifest_host("native_b", "in_process"),
            mk_manifest_host("sidecar_y", "sidecar"),
        ];
        assert_eq!(
            diff_cdylib_change(&next, &mut known),
            Some(CdylibChange {
                added: vec!["native_b".to_string()],
                removed: vec!["native_a".to_string()],
            })
        );
        // 集合稳定 → 无变更
        assert_eq!(diff_cdylib_change(&next, &mut known), None);
    }

    /// 开关解析：未设/非 "0" → 开；仅 "0"（含空白）→ 关。
    #[test]
    fn auto_restart_env_switch_parsing() {
        assert!(auto_restart_env_enabled(None));
        assert!(auto_restart_env_enabled(Some("1".to_string())));
        assert!(auto_restart_env_enabled(Some("yes".to_string())));
        assert!(!auto_restart_env_enabled(Some("0".to_string())));
        assert!(!auto_restart_env_enabled(Some(" 0 ".to_string())));
    }

    /// 触发决策：enabled + hook → 调用；enabled=false / 无 hook → 不调用。
    #[test]
    fn restart_trigger_respects_switch_and_hook() {
        use std::sync::atomic::AtomicBool;
        let change = CdylibChange {
            added: vec!["native_a".to_string()],
            removed: vec![],
        };
        let fired = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&fired);
        let hook: Arc<dyn Fn() + Send + Sync> =
            Arc::new(move || flag.store(true, Ordering::Relaxed));

        // enabled + hook → 触发
        assert!(trigger_cdylib_restart_if_enabled(
            &change,
            &Some(Arc::clone(&hook)),
            true
        ));
        assert!(fired.load(Ordering::Relaxed));

        // 开关关 → 不触发
        fired.store(false, Ordering::Relaxed);
        assert!(!trigger_cdylib_restart_if_enabled(
            &change,
            &Some(Arc::clone(&hook)),
            false
        ));
        assert!(!fired.load(Ordering::Relaxed));

        // 无 hook → 不触发（诚实降级，只记日志）
        assert!(!trigger_cdylib_restart_if_enabled(&change, &None, true));
    }

    // ── GAP-6：既有插件 manifest 变更 → 重注册 ──────────────────────

    fn hash_map() -> std::collections::HashMap<String, u64> {
        std::collections::HashMap::new()
    }

    #[tokio::test]
    async fn sync_reregisters_plugin_on_manifest_change() {
        use agentos_core::traits::CapabilityRegistry;

        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let scopes = PluginScopeRegistry::new();
        let store: ManifestsStore = std::sync::Arc::new(tokio::sync::RwLock::new(Vec::new()));
        let mut known = HashSet::new();
        let mut hashes = hash_map();

        // 首轮：v1 manifest（工具 t_old）
        let inv1 = MockInvoker::new(vec![mk_manifest("chg", "tool", &["t_old"], false)]);
        sync_once_with_store(
            &inv1, &registry_arc, Some(&scopes), &mut known, &mut None,
            Some(&store), &mut hashes,
        )
        .await
        .unwrap();
        assert!(registry_arc.get_tool("t_old").is_some());

        // 次轮：manifest 变更（工具 t_old → t_new）
        let inv2 = MockInvoker::new(vec![mk_manifest("chg", "tool", &["t_new"], false)]);
        let report = sync_once_with_store(
            &inv2, &registry_arc, Some(&scopes), &mut known, &mut None,
            Some(&store), &mut hashes,
        )
        .await
        .unwrap();

        assert_eq!(report.changed_plugin_ids, vec!["chg".to_string()]);
        // 新 schema 生效、旧工具摘除（scope revoke → guard drop 真撤销）
        assert!(registry_arc.get_tool("t_new").is_some(), "新工具应注册");
        assert!(registry_arc.get_tool("t_old").is_none(), "旧工具应随 revoke 摘除");
        // manifests store 更新为新 manifest
        let guard = store.read().await;
        let m = guard.iter().find(|x| x.id == "chg").expect("store 应含 chg");
        assert!(m.capabilities.tools.iter().any(|t| t.name == "t_new"));
        drop(guard);

        // 第三轮：同 manifest 再同步 → 无变更（幂等，不重复重注册）
        let report3 = sync_once_with_store(
            &inv2, &registry_arc, Some(&scopes), &mut known, &mut None,
            Some(&store), &mut hashes,
        )
        .await
        .unwrap();
        assert!(report3.changed_plugin_ids.is_empty(), "未变更不得重注册");
    }

    #[tokio::test]
    async fn sync_http_endpoints_refreshed_on_change() {
        use agentos_core::traits::CapabilityRegistry;

        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let scopes = PluginScopeRegistry::new();
        let mut known = HashSet::new();
        let mut hashes = hash_map();

        let inv1 = MockInvoker::new(vec![mk_manifest("epc", "tool", &["t"], true)]);
        sync_once_with_store(
            &inv1, &registry_arc, Some(&scopes), &mut known, &mut None, None, &mut hashes,
        )
        .await
        .unwrap();
        let route_before = registry_arc
            .find_http_route("/ext/epc/foo", "GET")
            .expect("首轮应注册 http 路由");

        // 变更后路由描述重建（同 path 不同 handler 也能换）
        let inv2 = MockInvoker::new(vec![mk_manifest("epc", "tool", &["t2"], true)]);
        sync_once_with_store(
            &inv2, &registry_arc, Some(&scopes), &mut known, &mut None, None, &mut hashes,
        )
        .await
        .unwrap();
        let route_after = registry_arc
            .find_http_route("/ext/epc/foo", "GET")
            .expect("变更后路由应仍在");
        let _ = (route_before, route_after);
        assert!(registry_arc.get_tool("t2").is_some());
    }

    // ── manifest 指纹确定性（omnisearch 假变更回归）───────────────────

    /// omnisearch 同款场景：mcp endpoint env 含 8 个键（HashMap 迭代顺序随机源）。
    fn mk_manifest_with_env(id: &str) -> PluginManifest {
        let v = serde_json::json!({
            "id": id, "name": id, "version": "1.0.0",
            "plugin_type": "tool", "language": "external",
            "host_type": "sidecar", "entry": "mcp:external",
            "mcp": { "transport": "stdio", "endpoint": {
                "command": "x", "args": [],
                "env": {
                    "K1": "v1", "K2": "v2", "K3": "v3", "K4": "v4",
                    "K5": "v5", "K6": "v6", "K7": "v7", "K8": "v8",
                }
            } },
            "capabilities": { "tools": [ { "name": "t1", "description": "d" } ] },
        });
        serde_json::from_value(v).expect("valid manifest")
    }

    #[test]
    fn manifest_fingerprint_stable_across_reparse_with_multi_key_env() {
        // HashMap 字段（McpEndpoint.env）每次反序列化迭代顺序随机。若指纹依赖
        // 序列化键序，同一内容解析两次会得到不同指纹 → watcher 每轮误判"变更"
        // 重注册（omnisearch 每 5s 一次假变更就是这个）。多次迭代抵掉哈希顺序
        // 偶然一致的极小概率，保证回归测试必 Red。
        for _ in 0..20 {
            let a: PluginManifest = mk_manifest_with_env("fp_env");
            let b: PluginManifest = mk_manifest_with_env("fp_env");
            assert_eq!(
                manifest_fingerprint(&a),
                manifest_fingerprint(&b),
                "同一内容的 manifest 指纹必须稳定（与解析次数/轮询轮次无关）"
            );
        }
    }

    #[test]
    fn notify_event_relevant_only_fires_for_plugin_json_and_new_dirs() {
        use notify::event::{CreateKind, DataChange, ModifyKind, RemoveKind};
        use notify::EventKind;
        use std::path::PathBuf;

        let p = |s: &str| PathBuf::from(s);
        let modify = EventKind::Modify(ModifyKind::Data(DataChange::Any));
        // plugin.json 的 增/改 是 watcher 主路径（manifest 变更 → 重注册）
        assert!(notify_event_relevant(
            modify.clone(),
            &[p("/plugins/tools/bash/plugin.json")]
        ));
        assert!(notify_event_relevant(
            EventKind::Create(CreateKind::File),
            &[p("/plugins/tools/newtool/plugin.json")]
        ));
        // 新目录 = 新插件根（tools/<name>/ 嵌套），触发扫描
        assert!(notify_event_relevant(
            EventKind::Create(CreateKind::Folder),
            &[p("/plugins/tools/newtool")]
        ));
        // 运行时产物不许触发：llm_core/logs/payload_diag 缓存、普通源码、编辑器临时文件
        assert!(!notify_event_relevant(
            modify.clone(),
            &[p("/plugins/pipeline/core/llm_core/logs/payload_diag/1786__x.json")]
        ));
        assert!(!notify_event_relevant(
            modify.clone(),
            &[p("/plugins/tools/bash/tool.py")]
        ));
        assert!(!notify_event_relevant(
            EventKind::Create(CreateKind::File),
            &[p("/plugins/tools/bash/.bash_tool.swp")]
        ));
        // 无关事件类型（Remove/Rename）不触发
        assert!(!notify_event_relevant(
            EventKind::Remove(RemoveKind::File),
            &[p("/plugins/tools/bash/plugin.json")]
        ));
    }
}
