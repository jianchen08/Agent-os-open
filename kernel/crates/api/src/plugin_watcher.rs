//! 插件目录运行时自动发现（hot-discover）。
//!
//! 启动期一次性扫描之后，内核原先不会感知运行时新增的插件目录：只有客户端显式
//! `POST /api/v1/plugins/reload-all` 才会重扫。本模块补上"触发源"——
//! 用 notify 文件监听 + 轮询兜底（防 Docker volume / WSL / 网络盘上 notify 丢事件）
//! 两条路径，经防抖后串行调用既有 `discover_new_plugins()` + `register_new_plugins()`，
//! 让丢进 `plugins/` 的新插件 tools/route_signals 立即生效，无需重启内核。
//!
//! cdylib 集合变更自动重启（A3）：sidecar 插件可热注册，但
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

use agentos_core::traits::{HostType, PluginInvoker, PluginManifest};
use agentos_core::types::PluginError;
use agentos_invoker::verify::{
    compare_tools, declared_with_services, parse_actual_tools, rejected_tool_names,
};
use agentos_plugin_loader::{
    output_schema_error, provides_methods_unbacked, CapabilityRegistryImpl, PluginEnablement,
    PluginScopeRegistry, ServiceSurface,
};
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
/// 轮询只兜底不抢跑。
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
    /// Phase 0（注册闸 L1）：本轮被 enablement profile 过滤（disabled）而未注册的
    /// 插件数——热发现路径与启动期注册循环对齐，按 enablement profile 过滤。
    pub skipped_disabled: usize,
    /// Phase 0（注册闸服务依赖）：本轮因 `requires_services`
    /// 无人提供（能力角色/服务端点未注册）而被拒绝注册的**新**插件 id。
    pub dependency_rejected: Vec<String>,
    /// GAP-6：本轮检测到 manifest 变更并**已重注册**的插件 id（指纹对比）。
    pub changed_plugin_ids: Vec<String>,
    /// A3：本轮检测到的 InProcess(cdylib) 插件集合变更（无变更为 None）。
    /// consumer 据此经 restart hook 触发 G8 优雅重启。
    pub cdylib_change: Option<CdylibChange>,
    /// P1（卸载语义）：本轮目录从磁盘消失、能力被摘下（scope revoke / clear_plugin）
    /// 的已登记插件 id（参照全量发现集，disabled/G2 暂拒不算）。
    pub uninstalled: Vec<String>,
    /// P1（卸载语义）：因提供者被卸载、`requires_services` 不再满足而被连带摘下
    /// （fail-closed 级联）的插件 id——目录仍在，服务提供者回归后自动重注册。
    pub cascade_uninstalled: Vec<String>,
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
/// 无 IO、无时序，可同步单测。复用 [`register_new_plugins`]，
/// 行为对齐 `reload-all` 端点的新插件序列。
/// M1：经 guarded 注册入 scope（disable 时结构性收回）。
pub fn apply_discovered_plugins(
    all_manifests: &[PluginManifest],
    known_ids: &mut HashSet<String>,
    registry: &Arc<CapabilityRegistryImpl>,
    scopes: &PluginScopeRegistry,
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
        let scope = scopes.scope_of(&m.id);
        for ep in &m.http_endpoints {
            // 冲突（同 path+method 已存在）忽略：新插件 id 唯一，正常不冲突。
            let ok = registry
                .register_http_route_guarded(&m.id, ep.clone())
                .map(|(_d, guard)| scope.track(guard))
                .is_ok();
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
        skipped_disabled: 0,
        dependency_rejected: Vec::new(),
        changed_plugin_ids: Vec::new(),
        cdylib_change: None,
        uninstalled: Vec::new(),
        cascade_uninstalled: Vec::new(),
    }
}

/// G2 校验+净化结果。
#[derive(Debug, Clone)]
pub struct G2VerifyOutcome {
    /// 校验后的 manifest：漂移工具被剔除；strict + spawn 失败时 tools 被清空。
    pub manifest: PluginManifest,
    /// 被拒绝注册的工具名（漂移 missing/schema_mismatch / strict 失败全量 / 冒烟失败）。
    pub rejected_tools: Vec<String>,
    /// 存在漂移信号（missing/schema_mismatch 或仅有 undeclared）。
    pub drift: bool,
    /// spawn/tools-list 本身失败（区别于"比对出漂移"）。
    pub spawn_failed: bool,
    /// 注册闸冒烟（smoke:true 工具）有调用失败被拒。
    pub smoke_failed: bool,
}

/// 从 JSON Schema 生成一条代表性样例输入（注册闸冒烟用）。
///
/// 覆盖常用子集：object→逐属性按 type/enum/items 递归填值；type 缺省→空对象。
/// 无法决断的（`$ref`/组合关键字）→ 空对象/空串，宁可不完整也不编造业务值。
/// 纯函数，可同步单测。冒烟只验"调用不崩 + 返回成功"，不验业务语义。
pub fn sample_value_from_schema(schema: &serde_json::Value) -> serde_json::Value {
    use serde_json::{json, Map};
    match schema.get("type").and_then(|t| t.as_str()) {
        Some("object") => {
            let mut obj = Map::new();
            if let Some(props) = schema.get("properties").and_then(|p| p.as_object()) {
                for (k, v) in props {
                    obj.insert(k.clone(), sample_value_from_schema(v));
                }
            }
            serde_json::Value::Object(obj)
        }
        Some("array") => json!([]),
        Some("string") => schema
            .get("enum")
            .and_then(|e| e.as_array())
            .and_then(|a| a.first())
            .cloned()
            .unwrap_or(json!("")),
        Some("number") | Some("integer") => json!(0),
        Some("boolean") => json!(false),
        Some("null") => json!(null),
        _ => json!({}),
    }
}

/// 注册闸：`output_schema` 声明合法性（与插件其它声明同一套 fail-closed——
/// 不再是 tool_core 运行时才暴露的特殊小岛）。畸形声明 → 拒绝该工具并返回名单。
fn reject_malformed_output_schemas(manifest: &mut PluginManifest) -> Vec<String> {
    let mut rejected = Vec::new();
    for tool in &manifest.capabilities.tools {
        if let Some(out) = &tool.output_schema {
            if let Some(msg) = output_schema_error(out) {
                warn!(
                    target: "plugin_watcher",
                    plugin = %manifest.id,
                    tool = %tool.name,
                    error = %msg,
                    "注册闸：output_schema 声明不合法，拒绝该工具（声明即校验）"
                );
                rejected.push(tool.name.clone());
            }
        }
    }
    if !rejected.is_empty() {
        manifest
            .capabilities
            .tools
            .retain(|t| !rejected.contains(&t.name));
    }
    rejected
}

/// 注册闸冒烟：对声明了 `smoke: true` 的工具构造样例输入真调一次，验证"基本能力
/// 能跑"（fail-closed）。调用异常/返回 failure → 拒绝该工具 + 记 `smoke_failed`。
/// 副作用敏感/需要真实参数的能力由插件**显式** `smoke: true` 才被冒烟——缺省不冒烟，
/// 避免注册期误伤生产工具。
async fn run_smoke(
    invoker: &dyn PluginInvoker,
    manifest: &mut PluginManifest,
) -> (Vec<String>, bool) {
    let mut rejected = Vec::new();
    let mut failed = false;
    for tool in &manifest.capabilities.tools {
        if tool.smoke != Some(true) {
            continue;
        }
        let sample =
            sample_value_from_schema(tool.input_schema.as_ref().unwrap_or(&serde_json::json!({})));
        match invoker.invoke_tool(&manifest.id, &tool.name, &sample).await {
            Ok(r) if r.success => {
                info!(
                    target: "plugin_smoke",
                    plugin = %manifest.id,
                    tool = %tool.name,
                    "注册闸冒烟：样例调用成功"
                );
            }
            Ok(r) => {
                warn!(
                    target: "plugin_smoke",
                    plugin = %manifest.id,
                    tool = %tool.name,
                    error = ?r.error,
                    "注册闸冒烟：调用返回失败，拒绝该工具"
                );
                rejected.push(tool.name.clone());
                failed = true;
            }
            Err(e) => {
                warn!(
                    target: "plugin_smoke",
                    plugin = %manifest.id,
                    tool = %tool.name,
                    error = %e.message,
                    "注册闸冒烟：调用异常，拒绝该工具"
                );
                rejected.push(tool.name.clone());
                failed = true;
            }
        }
    }
    if !rejected.is_empty() {
        manifest
            .capabilities
            .tools
            .retain(|t| !rejected.contains(&t.name));
    }
    (rejected, failed)
}

/// 观测失败重试退避序列（300ms / 1s）：启动期 sidecar 冷启动竞态下探测进程
/// 可能瞬态死亡——重试即过；真漂移走"比对出不一致"路径不受影响。
const G2_OBSERVE_RETRY_BACKOFF_MS: [u64; 2] = [300, 1000];

/// G2：单插件"声明 ↔ 实际暴露"一致性校验 + 冒烟 + 处置（公共化，供注册/启动/重启用复用）。
///
/// - 无 tools 且无 services（route 仅插件 / InProcess / native）→ 跳过，原样返回；
/// - 比对出可拒绝漂移（missing / schema_mismatch）→ 剔除漂移工具，其余照常；
/// - spawn/list_tools 失败 = **观测失败 ≠ 判定失败**：重试 2 次（退避见
///   [`G2_OBSERVE_RETRY_BACKOFF_MS`]）后仍失败 → 保留声明注册不动
///   （spawn_failed=true 供账本标记"校验未完成"，调用方择机复验）。观测通道
///   故障永远无权处置被校验对象——不得把好插件工具砍掉；
/// - 声明了 `smoke: true` 的工具 → 样例输入真调一次，失败拒绝（fail-closed）。
///
/// 纯校验+净化，无注册副作用；调用方决定后续注册行为。
pub async fn g2_verify_and_sanitize(
    invoker: &dyn PluginInvoker,
    manifest: PluginManifest,
) -> G2VerifyOutcome {
    // G2 只验 sidecar（InProcess/native 无 describe 通道，verify.rs 自述——它们
    // 的一致性走 A3 重启 + native describe 后续落地）；无 tools 且无 services
    // （route 仅插件）跳过。均原样返回。
    if manifest.host_type != HostType::Sidecar
        || (manifest.capabilities.tools.is_empty() && manifest.capabilities.services.is_empty())
    {
        return G2VerifyOutcome {
            manifest,
            rejected_tools: Vec::new(),
            drift: false,
            spawn_failed: false,
            smoke_failed: false,
        };
    }
    let listed = 'probe: {
        let mut attempt = 0usize;
        loop {
            match invoker.list_plugin_tools(&manifest.id).await {
                Ok(raw) => break 'probe Ok(raw),
                Err(first) => {
                    if attempt >= G2_OBSERVE_RETRY_BACKOFF_MS.len() {
                        break 'probe Err(first);
                    }
                    let backoff = G2_OBSERVE_RETRY_BACKOFF_MS[attempt];
                    attempt += 1;
                    warn!(
                        target: "plugin_watcher",
                        plugin = %manifest.id,
                        attempt,
                        backoff_ms = backoff,
                        error = %first.message,
                        "G2 观测失败（spawn/tools-list），退避重试（观测失败≠判定失败）"
                    );
                    tokio::time::sleep(std::time::Duration::from_millis(backoff)).await;
                }
            }
        }
    };
    match listed {
        Ok(raw) => {
            let (actual, _malformed) = parse_actual_tools(&raw);
            let mismatches = compare_tools(&declared_with_services(&manifest), &actual);
            let rejected = rejected_tool_names(&mismatches);
            let mut outcome = if rejected.is_empty() {
                G2VerifyOutcome {
                    manifest,
                    rejected_tools: Vec::new(),
                    drift: !mismatches.is_empty(),
                    spawn_failed: false,
                    smoke_failed: false,
                }
            } else {
                let mut sanitized = manifest;
                sanitized
                    .capabilities
                    .tools
                    .retain(|t| !rejected.contains(&t.name));
                G2VerifyOutcome {
                    manifest: sanitized,
                    rejected_tools: rejected.into_iter().collect(),
                    drift: true,
                    spawn_failed: false,
                    smoke_failed: false,
                }
            };
            // output_schema 声明合法性：畸形 → 拒绝该工具（声明即校验，fail-closed）
            let malformed = reject_malformed_output_schemas(&mut outcome.manifest);
            if !malformed.is_empty() {
                outcome.drift = true;
                outcome.rejected_tools.extend(malformed);
            }
            // 服务注册检查：provides 公告的方法必须有已声明工具（"service 未注册"），
            // 否则消费者可见却调不到——硬上报（与依赖管理同一类：声明不生效）。
            let unregistered = provides_methods_unbacked(&outcome.manifest);
            if !unregistered.is_empty() {
                warn!(
                    target: "plugin_gate_services",
                    plugin = %outcome.manifest.id,
                    advertised_but_unregistered = ?unregistered,
                    "注册闸：provides 公告的服务没有对应已声明工具（服务声明了但未注册），消费者将调不到"
                );
                outcome.drift = true;
                outcome.rejected_tools.extend(unregistered);
            }
            // 冒烟：声明了 smoke:true 的工具样例调用一次，失败剔除（fail-closed）
            let (smoke_rejected, smoke_failed) = run_smoke(invoker, &mut outcome.manifest).await;
            if smoke_failed {
                outcome.smoke_failed = true;
                outcome.drift = true;
                outcome.rejected_tools.extend(smoke_rejected);
            }
            outcome
        }
        Err(e) => {
            // 观测失败 ≠ 判定失败：探测通道自身故障，不是"实现缺了声明的工具"
            // ——保留声明注册（spawn_failed 供账本标记"校验未完成"，调用方复验）。
            warn!(
                target: "plugin_watcher",
                plugin = %manifest.id,
                error = %e.message,
                "G2 观测失败（spawn/上报不可用，重试后仍失败）——保留声明注册，待复验"
            );
            G2VerifyOutcome {
                manifest,
                rejected_tools: Vec::new(),
                drift: false,
                spawn_failed: true,
                smoke_failed: false,
            }
        }
    }
}

/// L0 纯函数：按 enablement 谓词过滤 manifests，返回（保留的引用, 跳过的 disabled 数）。
///
/// `is_enabled` 与 [`agentos_plugin_loader::PluginEnablement::is_enabled`] 同签名，
/// 便于直接传入或测试注入。disabled 插件不进注册表出口（与启动期注册循环一致）。
pub fn filter_enabled_manifests<F>(
    all: &[PluginManifest],
    mut is_enabled: F,
) -> (Vec<&PluginManifest>, usize)
where
    F: FnMut(&str, Option<bool>) -> bool,
{
    let mut kept = Vec::with_capacity(all.len());
    let mut skipped = 0usize;
    for m in all {
        if is_enabled(&m.id, m.enabled) {
            kept.push(m);
        } else {
            skipped += 1;
        }
    }
    (kept, skipped)
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
    deterministic_json(
        &serde_json::to_value(m).expect("PluginManifest serialization is infallible"),
    )
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

/// 热发现注册结果并入 L1 启用集合：仅本轮新注册的插件（changed 的已在集合中；
/// drifted 拒注册的不进）。空报告零成本返回。
///
/// 消费面：sessions schema 的 thread_fields contributes 过滤与 domain_event
/// 点对点投递均按 `AppState.enabled_plugin_ids` 过滤——不并入则新插件这两个面
/// 要等 PUT enabled 或重启才生效。
pub(crate) async fn merge_report_into_enabled_ids(
    report: &SyncReport,
    enabled: &Arc<tokio::sync::RwLock<HashSet<String>>>,
) {
    if report.new_plugin_ids.is_empty() {
        return;
    }
    enabled
        .write()
        .await
        .extend(report.new_plugin_ids.iter().cloned());
}

/// store 感知的同步入口：`manifests_store` 传入 `AppState.manifests`
/// 共享句柄时，本轮新注册插件的 manifest 会增量合并进 store（按 id 去重），
/// 修复热发现后状态列表/重启用等 manifest 消费面看不到新插件的不一致。
/// 只增不删：目录删除的卸载语义由下方 P1 卸载块处理。
// 多依赖注入的编排泵（invoker/registry/scopes/known 集/账本/报告），参数分组
// 收构成本超出收益，保留为内部函数（调用面收敛于 consumer 循环与测试）。
#[allow(clippy::too_many_arguments)]
pub async fn sync_once_with_store(
    invoker: &dyn PluginInvoker,
    registry: &Arc<CapabilityRegistryImpl>,
    scopes: &PluginScopeRegistry,
    known_ids: &mut HashSet<String>,
    known_cdylib: &mut Option<HashSet<String>>,
    manifests_store: Option<&ManifestsStore>,
    known_manifest_hashes: &mut HashMap<String, u64>,
    enablement: Option<&PluginEnablement>,
    contract_states: Option<&crate::contract::ContractLedger>,
) -> Result<SyncReport, PluginError> {
    let all = invoker.discover_new_plugins().await?;
    // A3：InProcess(cdylib) 集合 diff 先行（下方 for 循环会 move `all`）。
    // 首轮建基线，之后新增/消失都报变更；用全量 `all`（enablement/G2 过滤
    // 只动注册对象，不动 host_type 归属）。
    let cdylib_change = diff_cdylib_change(&all, known_cdylib);
    // 注册闸 L1：enablement 过滤（与启动期注册循环对齐）；依赖完整性参照
    // "将注册集合"（disabled 不在集合 → 依赖它视为缺失，fail-closed）。
    let (kept_refs, skipped_disabled) = filter_enabled_manifests(&all, |id, def| {
        enablement.is_none_or(|e| e.is_enabled(id, def))
    });
    // 服务依赖解析参照"将注册集合"（服务面）：requires_services 的 ns/ns.method 必须被集合内
    // 某插件提供；disabled 插件不在集合 → 其服务不在面 → 依赖它的插件拒注册（fail-closed）。
    let kept_owned: Vec<PluginManifest> = kept_refs.iter().map(|m| (*m).clone()).collect();
    let service_surface = ServiceSurface::from_manifests(&kept_owned);

    // ── 卸载语义（P1）：已登记插件目录从磁盘消失 → 本轮摘下能力 ──────────
    // 参照"全量发现集 all"（含 disabled）而非 kept filtered：目录仍在的
    // disabled / G2 暂拒 / 依赖被拒插件都不算卸载。known_ids 稍后被
    // apply_discovered_plugins 扩充，故在此先拍"先前已登记"快照再 diff。
    // InProcess(cdylib) 除外——其消失/重建归 A3 优雅重启路径（diff_cdylib_change），
    // 不在此处与它抢着摘除。
    // 登记全集 = known_ids ∪ store 已有条目：store 里有而未注册的
    // （disabled/依赖被拒，见尾部合并块）插件目录消失时同样要走卸载
    // 摘除，否则 store 残留幽灵条目、插件列表永远显示已删插件。
    let mut pre_registered: HashSet<String> = known_ids.clone();
    if let Some(store) = manifests_store {
        for m in store.read().await.iter() {
            pre_registered.insert(m.id.clone());
        }
    }
    let present_ids: HashSet<&str> = all.iter().map(|m| m.id.as_str()).collect();
    let is_known_inprocess = |id: &str| known_cdylib.as_ref().is_some_and(|s| s.contains(id));
    let mut uninstalled: Vec<String> = pre_registered
        .iter()
        .filter(|id| !present_ids.contains(id.as_str()) && !is_known_inprocess(id))
        .cloned()
        .collect();
    uninstalled.sort();

    // G2 安装期一致性校验（公共化 g2_verify_and_sanitize）：对新发现的 tool 插件
    // spawn → tools/list → 对照声明。漂移工具的贡献拒绝注册，其余能力照常；
    // 观测失败（重试后仍 spawn/list 失败）保留声明注册。
    let mut filtered: Vec<PluginManifest> = Vec::with_capacity(kept_refs.len());
    let mut drifted_plugins = Vec::new();
    let mut dependency_rejected = Vec::new();
    for m in kept_refs {
        if known_ids.contains(&m.id)
            || (m.capabilities.tools.is_empty() && m.capabilities.services.is_empty())
        {
            // 已知插件/无 tools+services：不重验，账本保持既有状态（不覆盖）。
            filtered.push(m.clone());
            continue;
        }
        // 注册闸服务依赖：新插件 requires_services 不满足（能力角色无人提供/端点未注册）→
        // 整插件拒注册（fail-closed，与旧 id 依赖同一语义，改按服务面解析）。
        if let Some(err) = service_surface.first_error_for(m) {
            warn!(
                target: "plugin_watcher",
                plugin = %m.id,
                error = %err.to_string(),
                "注册闸服务依赖：拒绝注册（能力无人提供/服务未注册）"
            );
            if let Some(ledger) = contract_states {
                let mut st = crate::contract::PluginContractState::not_covered(m, true);
                st.gates.dep_ok = false;
                st.gates.last_error = Some(err.to_string());
                ledger.upsert(st);
            }
            dependency_rejected.push(m.id.clone());
            continue;
        }
        let outcome = g2_verify_and_sanitize(invoker, m.clone()).await;
        if let Some(ledger) = contract_states {
            ledger.upsert(crate::contract::PluginContractState::derived(
                m,
                true,
                Some(&outcome),
            ));
        }
        if outcome.drift {
            if outcome.rejected_tools.is_empty() {
                // 仅 undeclared（实际多暴露）——不拒绝注册，但记录
                warn!(
                    target: "plugin_watcher",
                    plugin = %m.id,
                    "G2 校验：插件存在未声明暴露的工具（不拒绝注册）"
                );
            } else {
                warn!(
                    target: "plugin_watcher",
                    plugin = %m.id,
                    rejected = ?outcome.rejected_tools,
                    "G2 校验：插件声明与实现漂移，拒绝注册漂移工具（其余能力照常）"
                );
                drifted_plugins.push(m.id.clone());
            }
        }
        if outcome.spawn_failed {
            warn!(
                target: "plugin_watcher",
                plugin = %m.id,
                "G2 观测失败（重试后仍 spawn/上报不可用）——按声明注册，账本标记校验未完成，待复验"
            );
        }
        filtered.push(outcome.manifest);
    }
    let mut report = apply_discovered_plugins(&filtered, known_ids, registry, scopes);
    report.drifted_plugins = drifted_plugins;
    report.skipped_disabled = skipped_disabled;
    report.dependency_rejected = dependency_rejected;
    report.cdylib_change = cdylib_change;

    // ── 卸载语义（P1）执行：目录消失 → 摘除能力 + 依赖者连带（fail-closed） ──
    if !uninstalled.is_empty() {
        for id in &uninstalled {
            scopes.revoke(id);
        }
        // 依赖者连带：**先前已登记**插件中，requires_services 因提供者被卸载而不满足
        // → 一并摘下（fail-closed：目录仍在，服务提供者回归后下轮自动重注册）。
        // 本轮新注册插件已在主循环过依赖闸（服务面已不含被卸载提供者）→ 不在此列；
        // InProcess 归 A3 重建，跳过。
        let remaining_surface = ServiceSurface::from_manifests(&filtered);
        let mut cascade_uninstalled: Vec<String> = Vec::new();
        for m in &filtered {
            if m.host_type == HostType::InProcess
                || !pre_registered.contains(&m.id)
                || m.requires_services.is_empty()
            {
                continue;
            }
            if let Some(err) = remaining_surface.first_error_for(m) {
                warn!(
                    target: "plugin_watcher",
                    plugin = %m.id,
                    error = %err.to_string(),
                    "卸载连带：依赖的服务已无提供者，摘下该插件能力（fail-closed，服务回归自动重注册）"
                );
                scopes.revoke(&m.id);
                cascade_uninstalled.push(m.id.clone());
            }
        }
        cascade_uninstalled.sort();
        for id in uninstalled.iter().chain(cascade_uninstalled.iter()) {
            known_ids.remove(id);
            known_manifest_hashes.remove(id);
        }
        if let Some(store) = manifests_store {
            let mut guard = store.write().await;
            guard.retain(|m| !uninstalled.contains(&m.id) && !cascade_uninstalled.contains(&m.id));
        }
        info!(
            target: "plugin_watcher",
            uninstalled = ?uninstalled,
            cascade_uninstalled = ?cascade_uninstalled,
            "卸载语义：目录消失插件已摘下能力（含依赖者连带）"
        );
        report.uninstalled = uninstalled;
        report.cascade_uninstalled = cascade_uninstalled;
    }

    // ── GAP-6：既有插件 manifest 变更 → 重注册 ──────────────────────────
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
        // disabled/依赖被拒的运行期新发现插件 manifest 也进 store：boot 路径是
        // 全量注入（含 disabled），watcher 对齐该语义——store = "磁盘上已发现"
        // 全集，enabled 与否由 enabled_plugin_ids/enablement 表达；注册面不变
        // （仍只注册 filtered）。否则 PUT /plugins/{id}/enabled 查不到 manifest，
        // 启用静默不注册。
        // enabled 条目不覆盖（保留上方合并的 G2 净化版）；disabled 条目内容变更时
        // 刷新（防后续启用拿到过期 manifest）。
        let filtered_ids: HashSet<&str> = filtered.iter().map(|m| m.id.as_str()).collect();
        for m in &all {
            if filtered_ids.contains(m.id.as_str()) {
                continue;
            }
            match guard.iter_mut().find(|x| x.id == m.id) {
                Some(slot) => *slot = m.clone(),
                None => guard.push(m.clone()),
            }
        }
    }
    Ok(report)
}

/// 运行时插件自动发现器（notify watch + 轮询兜底，二选一或并存均可）。
///
/// `spawn` 后返回 [`WatcherHandle`]：`trigger` 可手动注入同步信号（测试 / 外部 API 用），
/// `sync_count` 观察已执行同步次数（断言防抖用）。consumer 任务串行消费触发信号，
/// 独占 `known_ids`（无需锁），故同步调用天然无并发竞态。
pub struct PluginWatcher {
    plugins_dir: PathBuf,
    invoker: Arc<dyn PluginInvoker>,
    registry: Arc<CapabilityRegistryImpl>,
    /// M1：guard 化注册的 scope 表（disable 时结构性收回；默认空表，测试/无注入场景
    /// 退化为 clear_plugin 等价语义——guarded 注册内部仍走 register_tool）。
    scopes: Arc<PluginScopeRegistry>,
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
    /// 注册闸 L1：enablement profile。Some 时热发现路径过滤 disabled 插件
    /// （与启动期注册循环对齐）；None = 不过滤（旧行为/测试）。
    enablement: Option<PluginEnablement>,
    /// enablement 重读根（bin 装配时传 config_root）。Some 时每次 sync 前从
    /// `plugins/default_profile.yaml` 重读：PUT enabled 写 profile + 改
    /// enabled_plugin_ids，而注入的 `enablement` 是 boot 快照，卸载→重装的
    /// 插件按旧快照判定会静默撤销运行期禁用——每次 sync 现读消除快照分歧。
    /// sync 稀疏、文件小，重读成本可忽略；None（测试 with_enablement 注入）
    /// 沿用快照。
    profile_reload_root: Option<PathBuf>,
    /// 闸2·观测：热发现校验结果收口（boot 已收口全量；此处补热发现新插件的
    /// 契约状态）。None = 不记录（测试/旧行为）。
    contract_states: Option<Arc<crate::contract::ContractLedger>>,
    /// L1 启用集合共享句柄（`AppState.enabled_plugin_ids`）：热发现注册的新插件
    /// 即时并入——sessions thread_fields contributes 与 domain_event 点对点投递
    /// 随热发现生效。None = 不同步（测试/旧行为，要等 PUT enabled 或重启）。
    enabled_ids: Option<Arc<tokio::sync::RwLock<HashSet<String>>>>,
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
            scopes: Arc::new(PluginScopeRegistry::new()),
            known_ids: initial_ids,
            known_manifest_hashes: HashMap::new(),
            known_cdylib: None,
            restart_hook: None,
            manifests_store: None,
            enablement: None,
            profile_reload_root: None,
            contract_states: None,
            enabled_ids: None,
            debounce: DEFAULT_DEBOUNCE,
            poll_interval: DEFAULT_POLL_INTERVAL,
        }
    }

    /// M1：注入共享 scope 表（guarded 注册入账本，disable 时结构性收回；
    /// 默认空表时 guarded 注册等效于普通注册）。
    pub fn with_scopes(mut self, scopes: Arc<PluginScopeRegistry>) -> Self {
        self.scopes = scopes;
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

    /// 注入 enablement profile：热发现路径过滤 disabled 插件（注册闸 L1 对齐
    /// 启动期注册循环）。生产装配请改用 [`Self::with_profile_reload`]——快照
    /// 看不到运行期 PUT enabled 的写盘结果。
    pub fn with_enablement(mut self, enablement: PluginEnablement) -> Self {
        self.enablement = Some(enablement);
        self
    }

    /// 注入 profile 重读根（生产装配）：每次 sync 前从
    /// `<config_root>/plugins/default_profile.yaml` 重读 enablement，消除
    /// boot 快照与运行期 PUT enabled 写盘的分歧（见字段注释）。
    pub fn with_profile_reload(mut self, config_root: PathBuf) -> Self {
        self.profile_reload_root = Some(config_root);
        self
    }

    /// 注入闸2·观测账本：热发现校验结果收口（新插件契约状态写入）。
    pub fn with_contract_states(
        mut self,
        contract_states: Arc<crate::contract::ContractLedger>,
    ) -> Self {
        self.contract_states = Some(contract_states);
        self
    }

    /// 注入 L1 启用集合共享句柄：热发现注册的新插件即时并入（语义对齐
    /// `PUT /plugins/{id}/enabled` 的集合更新），thread_fields / domain_event
    /// 面随热发现生效，无需 re-enable 或重启。
    pub fn with_enabled_ids(
        mut self,
        enabled: Arc<tokio::sync::RwLock<HashSet<String>>>,
    ) -> Self {
        self.enabled_ids = Some(enabled);
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
    /// → [`sync_once_with_store`]。两个自动触发源都只往同一个 mpsc 发 `()`，由 consumer 串行处理，
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
            enablement,
            profile_reload_root,
            contract_states,
            enabled_ids,
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
                // enablement 取数：配置了重读根 → 每次 sync 从盘上 profile 现读
                // （运行期 PUT enabled 的写盘结果即时可见，消除 boot 快照分歧）；
                // 否则用注入快照（测试路径）。
                let effective_enablement: Option<PluginEnablement> = match &profile_reload_root {
                    Some(root) => Some(PluginEnablement::load(root)),
                    None => enablement.clone(),
                };
                let report = match sync_once_with_store(
                    invoker.as_ref(),
                    &registry,
                    &scopes,
                    &mut known,
                    &mut known_cdylib,
                    manifests_store.as_ref(),
                    &mut known_hashes,
                    effective_enablement.as_ref(),
                    contract_states.as_deref(),
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
                // 热发现注册的新插件并入 L1 启用集合（thread_fields /
                // domain_event 面随热发现生效）。
                if let Some(enabled) = enabled_ids.as_ref() {
                    merge_report_into_enabled_ids(&report, enabled).await;
                }
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
        /// 前 N 次调用失败后恢复成功（模拟瞬态观测失败，测重试路径）。
        list_tools_fail_times: usize,
        /// list_plugin_tools 调用计数（断言重试次数）。
        list_calls: std::sync::atomic::AtomicUsize,
        /// 置 true 时 invoke_tool 返回 Err（模拟冒烟调用异常）。
        invoke_tool_fail: bool,
        /// 冒烟 invoke_tool 的调用计数（断言未声明 smoke 的工具不被冒烟）。
        invoke_calls: std::sync::atomic::AtomicUsize,
    }

    #[async_trait]
    impl PluginInvoker for MockInvoker {
        async fn invoke_pipeline_plugin(
            &self,
            _plugin_id: &str,
            _ctx: &PluginContext,
        ) -> Result<PluginResult, PluginError> {
            unimplemented!("sync 不走 invoke 路径")
        }
        async fn invoke_tool(
            &self,
            _plugin_id: &str,
            _tool_name: &str,
            _inputs: &serde_json::Value,
        ) -> Result<ToolExecutionResult, PluginError> {
            self.invoke_calls
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            if self.invoke_tool_fail {
                return Err(PluginError {
                    message: "invoke_tool boom".into(),
                    code: None,
                    source: None,
                });
            }
            Ok(ToolExecutionResult::success(serde_json::json!({})))
        }
        async fn send_lifecycle_hook(
            &self,
            _plugin_id: &str,
            _hook: LifecycleHook,
            _context: &HookContext,
        ) -> Result<(), PluginError> {
            unimplemented!("sync 不走 hook 路径")
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
            let calls = self
                .list_calls
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            if self.list_tools_fail || calls < self.list_tools_fail_times {
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
                list_tools_fail_times: 0,
                list_calls: std::sync::atomic::AtomicUsize::new(0),
                invoke_tool_fail: false,
                invoke_calls: std::sync::atomic::AtomicUsize::new(0),
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
        let scopes = PluginScopeRegistry::new();
        let mut known = HashSet::new();
        let report = sync_once_with_store(
            &invoker,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut HashMap::new(),
            None,
            None,
        )
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
        let scopes = PluginScopeRegistry::new();
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let first = sync_once_with_store(
            &invoker,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut HashMap::new(),
            None,
            None,
        )
        .await
        .unwrap();
        assert_eq!(first.tools_registered, 1);
        let second = sync_once_with_store(
            &invoker,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut HashMap::new(),
            None,
            None,
        )
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
            list_tools_fail_times: 0,
            list_calls: std::sync::atomic::AtomicUsize::new(0),
            invoke_tool_fail: false,
            invoke_calls: std::sync::atomic::AtomicUsize::new(0),
        };
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let scopes = PluginScopeRegistry::new();
        let mut known = HashSet::new();
        let err = sync_once_with_store(
            &invoker,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut HashMap::new(),
            None,
            None,
        )
        .await
        .unwrap_err();
        assert!(err.message.contains("boom"));
        assert!(known.is_empty());
        assert!(registry_arc.list_tools().is_empty());
    }

    #[test]
    fn apply_empty_manifests_noop() {
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let scopes = PluginScopeRegistry::new();
        let mut known = HashSet::new();
        let report = apply_discovered_plugins(&[], &mut known, &registry_arc, &scopes);
        assert!(report.new_plugin_ids.is_empty());
        assert_eq!(report.tools_registered, 0);
        assert_eq!(report.http_routes_registered, 0);
        assert!(known.is_empty());
        assert!(registry_arc.list_tools().is_empty());
    }

    #[test]
    fn apply_registers_new_tool_plugin_and_updates_known() {
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let scopes = PluginScopeRegistry::new();
        let mut known = HashSet::new();
        let m = mk_manifest("p1", "tool", &["t1", "t2"], false);
        let report =
            apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry_arc, &scopes);
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
        let scopes = PluginScopeRegistry::new();
        let mut known = HashSet::new();
        let m = mk_manifest("p1", "tool", &["t1"], false);
        let first =
            apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry_arc, &scopes);
        assert_eq!(first.tools_registered, 1);
        // 第二次：p1 已知，应跳过。
        let second =
            apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry_arc, &scopes);
        assert!(second.new_plugin_ids.is_empty());
        assert_eq!(second.tools_registered, 0);
    }

    #[test]
    fn apply_skips_known_plugin() {
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let scopes = PluginScopeRegistry::new();
        let mut known = HashSet::from(["p1".to_string()]);
        let m = mk_manifest("p1", "tool", &["t1"], false);
        let report =
            apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry_arc, &scopes);
        assert!(report.new_plugin_ids.is_empty());
        assert_eq!(report.tools_registered, 0);
        assert!(registry_arc.list_tools().is_empty());
    }

    #[test]
    fn apply_registers_http_endpoints_for_new_plugin() {
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let scopes = PluginScopeRegistry::new();
        let mut known = HashSet::new();
        let m = mk_manifest("p1", "tool", &["t1"], true); // 带 /ext/p1/foo GET
        let report =
            apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry_arc, &scopes);
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
        let scopes = PluginScopeRegistry::new();
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let report = sync_once_with_store(
            &invoker,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut HashMap::new(),
            None,
            None,
        )
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
        let scopes = PluginScopeRegistry::new();
        // 覆盖上报：只报 t1（ghost 声明有实际无 → missing 漂移）
        invoker.list_tools.insert(
            "p1".into(),
            json!({ "tools": [{"name": "t1", "description": "t1"}] }),
        );
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let report = sync_once_with_store(
            &invoker,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut HashMap::new(),
            None,
            None,
        )
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

    /// G2：观测失败（list_tools 报错）≠ 判定失败——按声明注册不净化，
    /// 注册流程继续不阻断（drifted_plugins 不含该插件，账本另标记
    /// verify_incomplete 待复验）。
    #[tokio::test]
    async fn sync_once_verify_failure_does_not_block_install() {
        let mut invoker = MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1"], false)]);
        let scopes = PluginScopeRegistry::new();
        invoker.list_tools_fail = true;
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let report = sync_once_with_store(
            &invoker,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut HashMap::new(),
            None,
            None,
        )
        .await
        .unwrap();
        assert!(
            !report.drifted_plugins.contains(&"p1".to_string()),
            "观测失败不是漂移：不进 drift 报告（账本标记 verify_incomplete）"
        );
        assert_eq!(
            report.tools_registered, 1,
            "观测失败：按声明注册，不净化工具"
        );
        assert_eq!(registry_arc.list_tools().len(), 1);
    }

    /// G2：校验失败 lenient（灰度回退）→ 保留声明工具（旧行为）。
    #[tokio::test]
    async fn sync_once_verify_failure_lenient_keeps_tools() {
        let mut invoker = MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1"], false)]);
        let scopes = PluginScopeRegistry::new();
        invoker.list_tools_fail = true;
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let report = sync_once_with_store(
            &invoker,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut HashMap::new(),
            None,
            None,
        )
        .await
        .unwrap();
        assert!(report.drifted_plugins.is_empty());
        assert_eq!(
            report.tools_registered, 1,
            "lenient：校验失败仍按声明注册（warn）"
        );
        assert_eq!(registry_arc.list_tools().len(), 1);
    }

    #[test]
    fn apply_multiple_new_mixed() {
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let scopes = PluginScopeRegistry::new();
        let mut known = HashSet::new();
        let m1 = mk_manifest("p1", "tool", &["t1"], false);
        let m2 = mk_manifest("p2", "tool", &["t2"], true);
        let m3 = mk_manifest("p3", "pipeline", &[], false);
        let all = vec![m1, m2, m3];
        let report = apply_discovered_plugins(&all, &mut known, &registry_arc, &scopes);
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
        let scopes = PluginScopeRegistry::new();
        let mut known = HashSet::new();
        let mut known_cdylib = Some(HashSet::new()); // boot 期无 cdylib
        let report = sync_once_with_store(
            &invoker,
            &registry_arc,
            &scopes,
            &mut known,
            &mut known_cdylib,
            None,
            &mut HashMap::new(),
            None,
            None,
        )
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

    /// 热发现注册的新插件并入 L1 启用集合（thread_fields / domain_event 面随热发现生效）。
    #[tokio::test]
    async fn merge_report_extends_enabled_ids() {
        let enabled = Arc::new(tokio::sync::RwLock::new(HashSet::new()));
        let mut report = SyncReport::default();
        merge_report_into_enabled_ids(&report, &enabled).await;
        assert!(enabled.read().await.is_empty(), "空报告不动集合");

        report.new_plugin_ids = vec!["hot_new".to_string()];
        merge_report_into_enabled_ids(&report, &enabled).await;
        assert!(enabled.read().await.contains("hot_new"));
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
            &inv1,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            Some(&store),
            &mut hashes,
            None,
            None,
        )
        .await
        .unwrap();
        assert!(registry_arc.get_tool("t_old").is_some());

        // 次轮：manifest 变更（工具 t_old → t_new）
        let inv2 = MockInvoker::new(vec![mk_manifest("chg", "tool", &["t_new"], false)]);
        let report = sync_once_with_store(
            &inv2,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            Some(&store),
            &mut hashes,
            None,
            None,
        )
        .await
        .unwrap();

        assert_eq!(report.changed_plugin_ids, vec!["chg".to_string()]);
        // 新 schema 生效、旧工具摘除（scope revoke → guard drop 真撤销）
        assert!(registry_arc.get_tool("t_new").is_some(), "新工具应注册");
        assert!(
            registry_arc.get_tool("t_old").is_none(),
            "旧工具应随 revoke 摘除"
        );
        // manifests store 更新为新 manifest
        let guard = store.read().await;
        let m = guard
            .iter()
            .find(|x| x.id == "chg")
            .expect("store 应含 chg");
        assert!(m.capabilities.tools.iter().any(|t| t.name == "t_new"));
        drop(guard);

        // 第三轮：同 manifest 再同步 → 无变更（幂等，不重复重注册）
        let report3 = sync_once_with_store(
            &inv2,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            Some(&store),
            &mut hashes,
            None,
            None,
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
            &inv1,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut hashes,
            None,
            None,
        )
        .await
        .unwrap();
        let route_before = registry_arc
            .find_http_route("/ext/epc/foo", "GET")
            .expect("首轮应注册 http 路由");

        // 变更后路由描述重建（同 path 不同 handler 也能换）
        let inv2 = MockInvoker::new(vec![mk_manifest("epc", "tool", &["t2"], true)]);
        sync_once_with_store(
            &inv2,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut hashes,
            None,
            None,
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
            modify,
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
            modify,
            &[p(
                "/plugins/pipeline/core/llm_core/logs/payload_diag/1786__x.json"
            )]
        ));
        assert!(!notify_event_relevant(
            modify,
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

    // ── Phase 0：注册闸——G2 公共化 / disabled 过滤 / 依赖拒绝 ──────────────

    /// G2 spawn 失败处置开关：默认严格（fail-closed），仅 "0" 置 lenient。
    /// enablement 过滤纯函数：disabled 跳过并计数，保留序不变。
    #[test]
    fn filter_enabled_skips_disabled_and_counts() {
        let all = vec![
            mk_manifest("a", "tool", &["t_a"], false),
            mk_manifest("b", "tool", &["t_b"], false),
            mk_manifest("c", "tool", &["t_c"], false),
        ];
        let (kept, skipped) = filter_enabled_manifests(&all, |id, default| {
            if id == "b" {
                false
            } else {
                default.unwrap_or(true)
            }
        });
        assert_eq!(skipped, 1);
        let ids: Vec<&str> = kept.iter().map(|m| m.id.as_str()).collect();
        assert_eq!(ids, vec!["a", "c"]);
    }

    #[tokio::test]
    async fn g2_verify_consistent_returns_unchanged() {
        let invoker = MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1", "t2"], false)]);
        let m = mk_manifest("p1", "tool", &["t1", "t2"], false);
        let out = g2_verify_and_sanitize(&invoker, m).await;
        assert!(!out.drift && !out.spawn_failed);
        assert!(out.rejected_tools.is_empty());
        assert_eq!(out.manifest.capabilities.tools.len(), 2);
    }

    #[tokio::test]
    async fn g2_verify_drift_sanitizes_rejected_tool_only() {
        let mut invoker =
            MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1", "ghost"], false)]);
        invoker.list_tools.insert(
            "p1".into(),
            json!({ "tools": [{"name": "t1", "description": "t1"}] }),
        );
        let m = mk_manifest("p1", "tool", &["t1", "ghost"], false);
        let out = g2_verify_and_sanitize(&invoker, m).await;
        assert!(out.drift);
        assert_eq!(out.rejected_tools, vec!["ghost".to_string()]);
        let names: Vec<String> = out
            .manifest
            .capabilities
            .tools
            .iter()
            .map(|t| t.name.clone())
            .collect();
        assert_eq!(names, vec!["t1".to_string()], "漂移工具被剔除、其余照常");
    }

    #[tokio::test]
    async fn g2_verify_spawn_fail_keeps_declared_tools() {
        // 观测失败≠判定失败：重试后仍 spawn/list 失败 → 保留声明注册
        // （spawn_failed 供账本标记校验未完成），不再净化工具。
        let mut invoker = MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1", "t2"], false)]);
        invoker.list_tools_fail = true;
        let m = mk_manifest("p1", "tool", &["t1", "t2"], false);
        let out = g2_verify_and_sanitize(&invoker, m).await;
        assert!(out.spawn_failed && !out.drift);
        assert_eq!(
            out.manifest.capabilities.tools.len(),
            2,
            "观测失败：声明注册原样保留"
        );
        assert!(out.rejected_tools.is_empty(), "观测失败无权拒工具");
    }

    #[tokio::test]
    async fn g2_verify_observation_fail_retries_then_passes() {
        // 瞬态观测失败（首探失败，重试成功）→ 正常走比对路径，不产生 spawn_failed。
        let mut invoker = MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1"], false)]);
        invoker.list_tools_fail_times = 1; // 首次 Err，重试 Ok
        invoker.list_tools.insert(
            "p1".into(),
            json!({ "tools": [{"name": "t1", "description": "t1"}] }),
        );
        let m = mk_manifest("p1", "tool", &["t1"], false);
        let out = g2_verify_and_sanitize(&invoker, m).await;
        assert!(!out.spawn_failed && !out.drift, "重试即过，不误杀");
        assert_eq!(out.manifest.capabilities.tools.len(), 1);
    }

    /// services-only（无 tools）插件：不 spawn 校验，原样返回（若被调用会因
    /// list_tools_fail 炸——应被跳过）。
    #[tokio::test]
    async fn g2_verify_skips_services_only_plugin() {
        let mut invoker = MockInvoker::new(vec![mk_manifest("s", "system", &[], false)]);
        invoker.list_tools_fail = true;
        let m = mk_manifest("s", "system", &[], false);
        let out = g2_verify_and_sanitize(&invoker, m).await;
        assert!(!out.spawn_failed && !out.drift);
        assert!(out.manifest.capabilities.tools.is_empty());
    }

    /// InProcess（cdylib）tool 插件：无 describe 通道，不做 G2（若被调用会因
    /// list_tools_fail 炸——应被跳过）。
    #[tokio::test]
    async fn g2_verify_skips_in_process_plugin() {
        let mut invoker = MockInvoker::new(vec![mk_manifest_host("nativeish", "in_process")]);
        invoker.list_tools_fail = true;
        let m = mk_manifest_host("nativeish", "in_process");
        let out = g2_verify_and_sanitize(&invoker, m).await;
        assert!(!out.spawn_failed && !out.drift);
    }

    /// 注册闸 L1：disabled 插件在热发现路径不进注册表。
    #[tokio::test]
    async fn sync_skips_disabled_plugin_in_hot_discovery() {
        use agentos_plugin_loader::{PluginEnablement, PluginProfile, ProfileEntry};
        let mut plugins = std::collections::HashMap::new();
        plugins.insert(
            "b".to_string(),
            ProfileEntry {
                enabled: Some(false),
                activation: None,
            },
        );
        let enablement = PluginEnablement::with_profile(PluginProfile {
            version: 1,
            plugins,
            defaults: Default::default(),
        });
        let invoker = MockInvoker::new(vec![
            mk_manifest("a", "tool", &["t_a"], false),
            mk_manifest("b", "tool", &["t_b"], false),
            mk_manifest("c", "tool", &["t_c"], false),
        ]);
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let scopes = PluginScopeRegistry::new();
        let mut known = HashSet::new();
        let report = sync_once_with_store(
            &invoker,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut HashMap::new(),
            Some(&enablement),
            None,
        )
        .await
        .unwrap();
        assert_eq!(report.skipped_disabled, 1, "b 被 profile 禁用");
        let ids: Vec<String> = registry_arc
            .list_tools()
            .iter()
            .map(|t| t.plugin_id.clone())
            .collect();
        assert!(!ids.contains(&"b".to_string()), "disabled 插件不进注册表");
        assert!(ids.contains(&"a".to_string()) && ids.contains(&"c".to_string()));
    }

    /// 回归锚：disabled 插件不进注册表，但 manifest 必须进 manifests store——
    /// 否则 PUT /plugins/{id}/enabled 查不到 manifest，启用静默不注册。store =
    /// "磁盘上已发现"全集，与 boot 全量注入语义对齐（e2e 见
    /// tests/e2e_02/test_07_plugin_lifecycle_e2e.py）。
    #[tokio::test]
    async fn sync_disabled_plugin_manifest_still_enters_store() {
        use agentos_plugin_loader::{PluginEnablement, PluginProfile, ProfileEntry};
        let mut plugins = std::collections::HashMap::new();
        let scopes = PluginScopeRegistry::new();
        plugins.insert(
            "b".to_string(),
            ProfileEntry {
                enabled: Some(false),
                activation: None,
            },
        );
        let enablement = PluginEnablement::with_profile(PluginProfile {
            version: 1,
            plugins,
            defaults: Default::default(),
        });
        let invoker = MockInvoker::new(vec![
            mk_manifest("a", "tool", &["t_a"], false),
            mk_manifest("b", "tool", &["t_b"], false),
        ]);
        let registry_arc = Arc::new(CapabilityRegistryImpl::new());
        let store: ManifestsStore = Arc::new(RwLock::new(Vec::new()));
        let mut known = HashSet::new();
        sync_once_with_store(
            &invoker,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            Some(&store),
            &mut HashMap::new(),
            Some(&enablement),
            None,
        )
        .await
        .unwrap();

        let store_ids: Vec<String> = store.read().await.iter().map(|m| m.id.clone()).collect();
        assert!(
            store_ids.contains(&"a".to_string()) && store_ids.contains(&"b".to_string()),
            "disabled 插件 manifest 也须进 store（PUT enabled 的查找源），实际: {store_ids:?}"
        );
        assert!(
            registry_arc.list_tools().iter().all(|t| t.plugin_id != "b"),
            "disabled 插件不得注册进 LLM 面"
        );
    }

    /// 回归锚：store-only（disabled、从未注册）插件目录消失 → store 条目
    /// 同样摘除——否则卸载判定只看 known_ids，disabled 卸载留幽灵条目、
    /// 插件列表永远显示已删插件。
    #[tokio::test]
    async fn sync_uninstalls_store_only_disabled_plugin() {
        use agentos_plugin_loader::{PluginEnablement, PluginProfile, ProfileEntry};
        let mut plugins = std::collections::HashMap::new();
        let scopes = PluginScopeRegistry::new();
        plugins.insert(
            "b".to_string(),
            ProfileEntry {
                enabled: Some(false),
                activation: None,
            },
        );
        let enablement = PluginEnablement::with_profile(PluginProfile {
            version: 1,
            plugins,
            defaults: Default::default(),
        });
        let invoker = MockInvoker::new(vec![mk_manifest("b", "tool", &["t_b"], false)]);
        let registry_arc = Arc::new(CapabilityRegistryImpl::new());
        let store: ManifestsStore = Arc::new(RwLock::new(Vec::new()));
        let mut known = HashSet::new();
        sync_once_with_store(
            &invoker,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            Some(&store),
            &mut HashMap::new(),
            Some(&enablement),
            None,
        )
        .await
        .unwrap();
        assert!(known.is_empty(), "disabled 插件不注册，known 应为空");
        assert!(store.read().await.iter().any(|m| m.id == "b"));

        // 目录消失（discover 集不再含 b）→ store 条目须摘除（修复点：
        // pre_registered = known_ids ∪ store）。
        let invoker2 = MockInvoker::new(vec![]);
        let report = sync_once_with_store(
            &invoker2,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            Some(&store),
            &mut HashMap::new(),
            Some(&enablement),
            None,
        )
        .await
        .unwrap();
        assert_eq!(report.uninstalled, vec!["b".to_string()]);
        assert!(
            store.read().await.iter().all(|m| m.id != "b"),
            "disabled store-only 插件卸载后不得残留幽灵条目"
        );
    }

    /// 注册闸服务依赖（服务唯一轴）：新插件 requires_services 无人提供 → 整插件拒绝注册。
    #[tokio::test]
    async fn sync_rejects_new_plugin_with_missing_required_dep() {
        let mut m = mk_manifest("dep_app", "tool", &["t_a"], false);
        let scopes = PluginScopeRegistry::new();
        m.requires_services = vec!["ghost.read".to_string()];
        let invoker = MockInvoker::new(vec![m]);
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let report = sync_once_with_store(
            &invoker,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut HashMap::new(),
            None,
            None,
        )
        .await
        .unwrap();
        assert_eq!(report.dependency_rejected, vec!["dep_app".to_string()]);
        assert!(report.new_plugin_ids.is_empty(), "依赖缺失的插件不注册");
        assert!(registry_arc.list_tools().is_empty());
        assert!(known.is_empty());
    }

    /// 卸载语义（P1）：目录消失 → 摘下能力；依赖者（requires_services 无人提供）
    /// 一并级联摘下；服务提供者回归 → 下轮自动重注册（自愈）。
    #[tokio::test]
    async fn sync_uninstalls_removed_plugin_and_cascades_dependents() {
        // 提供者 p 提供 x.m；消费者 c 依赖 x.m（round1 双双注册）。
        let scopes = PluginScopeRegistry::new();
        let p: PluginManifest = serde_json::from_value(json!({
            "id": "p", "name": "p", "version": "1.0.0",
            "plugin_type": "tool", "language": "python", "host_type": "sidecar",
            "entry": "python server.py",
            "capabilities": { "services": [{ "name": "x.m" }] },
        }))
        .unwrap();
        let mut c = mk_manifest("c", "tool", &["tc"], false);
        c.requires_services = vec!["x.m".to_string()];

        let r1 = {
            let inv = MockInvoker::new(vec![p.clone(), c.clone()]);
            let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
            let mut known = HashSet::new();
            let report = sync_once_with_store(
                &inv,
                &registry_arc,
                &scopes,
                &mut known,
                &mut None,
                None,
                &mut HashMap::new(),
                None,
                None,
            )
            .await
            .unwrap();
            assert_eq!(report.new_plugin_ids.len(), 2, "round1 全注册");
            assert_eq!(registry_arc.list_tools().len(), 1, "c 的工具 tc 注册");
            (registry_arc, known)
        };
        let (registry_arc, mut known) = r1;

        // 次轮：p 目录从磁盘消失（discover 只剩 c）→ p 卸载 + c 依赖级联摘下。
        let inv2 = MockInvoker::new(vec![c.clone()]);
        let r2 = sync_once_with_store(
            &inv2,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut HashMap::new(),
            None,
            None,
        )
        .await
        .unwrap();
        assert_eq!(r2.uninstalled, vec!["p".to_string()]);
        assert_eq!(r2.cascade_uninstalled, vec!["c".to_string()]);
        assert!(
            registry_arc.list_tools().is_empty(),
            "依赖者 c 的能力也一并摘下"
        );
        assert!(!known.contains("p") && !known.contains("c"));

        // 提供者回归 → 下轮自动重注册（含消费者，自愈）。
        let inv3 = MockInvoker::new(vec![p.clone(), c.clone()]);
        let r3 = sync_once_with_store(
            &inv3,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut HashMap::new(),
            None,
            None,
        )
        .await
        .unwrap();
        assert_eq!(r3.new_plugin_ids.len(), 2, "服务回归自动重注册");
        assert_eq!(registry_arc.list_tools().len(), 1, "c 的工具 tc 重新注册");
    }

    /// 卸载语义（P1）：无依赖者的插件被卸载 → 只摘自身能力，不波及其它插件。
    #[tokio::test]
    async fn sync_uninstall_isolated_plugin_leaves_others() {
        let inv1 = MockInvoker::new(vec![
            mk_manifest("a", "tool", &["ta"], false),
            mk_manifest("b", "tool", &["tb"], false),
        ]);
        let scopes = PluginScopeRegistry::new();
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let r1 = sync_once_with_store(
            &inv1,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut HashMap::new(),
            None,
            None,
        )
        .await
        .unwrap();
        assert_eq!(r1.new_plugin_ids.len(), 2);

        // 只删 a 的目录：b 无 requires_services → 不受牵连。
        let inv2 = MockInvoker::new(vec![mk_manifest("b", "tool", &["tb"], false)]);
        let r2 = sync_once_with_store(
            &inv2,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut HashMap::new(),
            None,
            None,
        )
        .await
        .unwrap();
        assert_eq!(r2.uninstalled, vec!["a".to_string()]);
        assert!(r2.cascade_uninstalled.is_empty());
        let ids: Vec<String> = registry_arc
            .list_tools()
            .iter()
            .map(|t| t.plugin_id.clone())
            .collect();
        assert_eq!(ids, vec!["b".to_string()], "b 不受牵连");
        assert!(!known.contains("a"));
        assert!(known.contains("b"));
    }

    // ── Phase 1-C5b：注册闸冒烟样例跑（smoke:true 逐能力放行） ──────────────

    fn mk_manifest_smoke(id: &str, tool: &str, smoke: bool) -> PluginManifest {
        let v = serde_json::json!({
            "id": id, "name": id, "version": "1.0.0",
            "plugin_type": "tool", "language": "rust",
            "host_type": "sidecar", "entry": "x",
            "capabilities": { "tools": [
                { "name": tool, "description": tool, "smoke": smoke,
                  "input_schema": {"type": "object", "properties": {"a": {"type": "string"}}} }
            ] },
        });
        serde_json::from_value(v).expect("valid manifest")
    }

    fn assert_input_schema_ok(schema: serde_json::Value) -> serde_json::Value {
        // 与 mk_manifest_smoke 的 input_schema 完全一致，避免 G2 schema 误漂移
        serde_json::json!({
            "tools": [ { "name": schema["tools"][0]["name"], "description": "d",
                         "inputSchema": {"type": "object", "properties": {"a": {"type": "string"}}} } ]
        })
    }

    #[test]
    fn sample_from_schema_fills_typed_properties() {
        let schema = serde_json::json!({
            "type": "object",
            "properties": {
                "n": {"type": "number"},
                "s": {"type": "string"},
                "e": {"type": "string", "enum": ["fast", "slow"]},
                "b": {"type": "boolean"},
                "a": {"type": "array", "items": {"type": "string"}},
                "o": {"type": "object", "properties": {"k": {"type": "integer"}}}
            }
        });
        let sample = sample_value_from_schema(&schema);
        assert_eq!(sample["n"], serde_json::json!(0));
        assert_eq!(sample["s"], serde_json::json!(""));
        assert_eq!(sample["e"], serde_json::json!("fast"), "enum 取首项");
        assert_eq!(sample["b"], serde_json::json!(false));
        assert_eq!(sample["a"], serde_json::json!([]));
        assert_eq!(sample["o"]["k"], serde_json::json!(0), "嵌套对象递归填值");
    }

    #[tokio::test]
    async fn smoke_failure_rejects_the_tool() {
        let mut invoker = MockInvoker::new(vec![mk_manifest_smoke("p1", "t_smoke", true)]);
        invoker.list_tools = std::collections::HashMap::from([(
            "p1".into(),
            assert_input_schema_ok(json!({"tools":[{ "name": "t_smoke" }]})),
        )]);
        invoker.invoke_tool_fail = true;
        let out = g2_verify_and_sanitize(&invoker, mk_manifest_smoke("p1", "t_smoke", true)).await;
        assert!(out.smoke_failed, "冒烟失败须标记");
        assert!(
            out.manifest.capabilities.tools.is_empty(),
            "冒烟失败的工具被拒绝"
        );
        assert!(out.rejected_tools.contains(&"t_smoke".to_string()));
    }

    #[tokio::test]
    async fn smoke_success_keeps_the_tool() {
        let mut invoker = MockInvoker::new(vec![mk_manifest_smoke("p1", "t_smoke", true)]);
        invoker.list_tools = std::collections::HashMap::from([(
            "p1".into(),
            assert_input_schema_ok(json!({"tools":[{ "name": "t_smoke" }]})),
        )]);
        let out = g2_verify_and_sanitize(&invoker, mk_manifest_smoke("p1", "t_smoke", true)).await;
        assert!(!out.smoke_failed);
        assert_eq!(out.manifest.capabilities.tools.len(), 1, "冒烟成功则保留");
        assert_eq!(
            invoker
                .invoke_calls
                .load(std::sync::atomic::Ordering::Relaxed),
            1
        );
    }

    #[tokio::test]
    async fn smoke_not_run_without_optin() {
        let mut invoker = MockInvoker::new(vec![mk_manifest_smoke("p1", "t_no_smoke", false)]);
        invoker.list_tools = std::collections::HashMap::from([(
            "p1".into(),
            assert_input_schema_ok(json!({"tools":[{ "name": "t_no_smoke" }]})),
        )]);
        let _ =
            g2_verify_and_sanitize(&invoker, mk_manifest_smoke("p1", "t_no_smoke", false)).await;
        assert_eq!(
            invoker
                .invoke_calls
                .load(std::sync::atomic::Ordering::Relaxed),
            0,
            "未声明 smoke:true 的工具不被冒烟（避免注册期副作用）"
        );
    }

    /// output_schema 声明合法性（声明即校验）：畸形声明 → 注册期拒绝该工具，
    /// 不再等到 tool_core 运行时才暴露。
    #[tokio::test]
    async fn malformed_output_schema_rejected_at_registration() {
        let v = serde_json::json!({
            "id": "p1", "name": "p1", "version": "1.0.0",
            "plugin_type": "tool", "language": "rust",
            "host_type": "sidecar", "entry": "x",
            "capabilities": { "tools": [
                { "name": "t_bad", "description": "d",
                  "output_schema": {"type": "object", "properties": "oops"} },
                { "name": "t_ok", "description": "d",
                  "output_schema": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]} }
            ] },
        });
        let m: PluginManifest = serde_json::from_value(v).unwrap();
        let invoker = MockInvoker::new(vec![m.clone()]);
        let out = g2_verify_and_sanitize(&invoker, m).await;
        assert!(
            out.rejected_tools.contains(&"t_bad".to_string()),
            "畸形 output_schema 的工具必须被拒"
        );
        assert_eq!(out.manifest.capabilities.tools.len(), 1, "t_ok 保留");
        assert_eq!(out.manifest.capabilities.tools[0].name, "t_ok");
    }
}
