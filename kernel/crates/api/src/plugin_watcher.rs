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

use agentos_core::traits::{
    CapabilityRegistry, HostType, PluginInvoker, PluginManifest, ToolCapability,
};
use agentos_core::types::PluginError;
use agentos_invoker::verify::{
    compare_tools, declared_with_services, normalize_member_actual_tools, parse_actual_tools,
    rejected_tool_names,
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

/// 一行接入（2026-09-03 用户裁定）判定：external MCP 允许零工具声明——
/// 空声明 = 观测即声明，采信握手 `tools/list` 全量导入（多工具服务免抄
/// schema）；声明非空的 external MCP 仍走静态 drift 对照，行为不变。
fn is_dynamic_mcp(manifest: &PluginManifest) -> bool {
    manifest.host_type == HostType::Sidecar
        && manifest.entry == "mcp:external"
        && manifest.capabilities.tools.is_empty()
        && manifest.capabilities.services.is_empty()
}

/// G2：单插件"声明 ↔ 实际暴露"一致性校验 + 冒烟 + 处置（公共化，供注册/启动/重启用复用）。
///
/// - 无 tools 且无 services（route 仅插件 / InProcess / native）→ 跳过，原样返回；
///   例外：零声明的 external MCP（[`is_dynamic_mcp`]）不跳过——观测导入其全部工具；
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
    // （route 仅插件）跳过——零声明的 external MCP 例外（观测导入，见下）。
    let dynamic_mcp = is_dynamic_mcp(&manifest);
    if manifest.host_type != HostType::Sidecar
        || (manifest.capabilities.tools.is_empty()
            && manifest.capabilities.services.is_empty()
            && !dynamic_mcp)
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
            // 一行接入：零声明 external MCP → 观测即声明，tools/list 全量回填
            // manifest（无可对照声明，不做 drift 对照；每次安装期/复验全量重导入，
            // 幂等）。spawn 失败走下方既有 Err 分支：manifest 保持零声明 +
            // spawn_failed，由 resync 零工具自愈重试。
            if dynamic_mcp {
                let mut manifest = manifest;
                manifest.capabilities.tools = actual
                    .into_iter()
                    .map(|t| ToolCapability {
                        name: t.name,
                        description: t.description,
                        input_schema: Some(t.input_schema),
                        output_schema: None,
                        category: None,
                        ui: None,
                        render: None,
                        smoke: None,
                    })
                    .collect();
                info!(
                    target: "plugin_watcher",
                    plugin = %manifest.id,
                    tools = manifest.capabilities.tools.len(),
                    "一行接入：external MCP 零声明，观测导入 tools/list 全量工具"
                );
                return G2VerifyOutcome {
                    manifest,
                    rejected_tools: Vec::new(),
                    drift: false,
                    spawn_failed: false,
                    smoke_failed: false,
                };
            }
            // 合宿成员（host_group="light"）的工具经宿主注册为 `{plugin_id}.{tool}`
            // （§4.2 命名空间），G2 对照声明裸名前必须归一前缀；否则成员 100%
            // 被误判 missing 漂移而剔光（08-31 实测 task_manage/memory/human 全灭）。
            let actual = if manifest.host_group.as_deref() == Some("light") {
                normalize_member_actual_tools(actual, &manifest.id)
            } else {
                actual
            };
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

/// 插件源码目录解析器（bin 装配注入：`PluginInvokerImpl::plugin_source_dir`）。
/// watcher 借此对已知插件算代码指纹，驱动"修复后复验恢复"——声明指纹只覆盖
/// plugin.json，sidecar 实现修复不改 manifest，须另以代码指纹为复验触发器。
pub type CodeDirResolver = dyn Fn(&str) -> Option<PathBuf> + Send + Sync;

/// 已知插件的当前代码指纹。无解析器（测试/未注入）或目录不可得 → 0（恒定
/// 值使 code_changed 恒 false，行为退化为仅声明指纹驱动）。
fn current_code_fp(
    code_dirs: Option<&std::sync::Arc<CodeDirResolver>>,
    manifest: &PluginManifest,
) -> u64 {
    let Some(resolver) = code_dirs else {
        return 0;
    };
    let Some(dir) = resolver(&manifest.id) else {
        return 0;
    };
    agentos_invoker::invoker::compute_plugin_fingerprint(&dir, manifest)
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
    known_code_hashes: &mut HashMap<String, u64>,
    code_dirs: Option<&std::sync::Arc<CodeDirResolver>>,
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
            || (m.capabilities.tools.is_empty()
                && m.capabilities.services.is_empty()
                && !is_dynamic_mcp(m))
        {
            // 已知插件/无 tools+services：此处不重验；已知插件的变更复验在下方
            // GAP-6 块按声明/代码指纹触发，账本保持既有状态（不覆盖）。
            // 例外：零声明 external MCP（一行接入）照常观测导入。
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
        // 基线以**声明（磁盘 manifest）指纹**为准，而非净化后的注册 manifest——
        // 若落净化版指纹，下轮 sync 的 raw 声明必与之相异，被误判"变更"后把
        // 被剔工具原样重注册（复活）。代码指纹同轮定型（复验触发器，见 GAP-6 块）。
        known_manifest_hashes.insert(m.id.clone(), manifest_fingerprint(m));
        known_code_hashes.insert(m.id.clone(), current_code_fp(code_dirs, m));
        filtered.push(outcome.manifest);
    }
    let mut report = apply_discovered_plugins(&filtered, known_ids, registry, scopes);
    report.drifted_plugins = drifted_plugins;
    report.skipped_disabled = skipped_disabled;
    report.dependency_rejected = dependency_rejected;
    report.cdylib_change = cdylib_change;

    // ── 卸载语义（P1）执行：目录消失 → 摘除能力 + 依赖者连带（fail-closed） ──
    let mut cascade_uninstalled: Vec<String> = Vec::new();
    if !uninstalled.is_empty() {
        for id in &uninstalled {
            scopes.revoke(id);
        }
        // 依赖者连带：**先前已登记**插件中，requires_services 因提供者被卸载而不满足
        // → 一并摘下（fail-closed：目录仍在，服务提供者回归后下轮自动重注册）。
        // 本轮新注册插件已在主循环过依赖闸（服务面已不含被卸载提供者）→ 不在此列；
        // InProcess 归 A3 重建，跳过。
        let remaining_surface = ServiceSurface::from_manifests(&filtered);
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
            known_code_hashes.remove(id);
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
        report.uninstalled = uninstalled.clone();
        report.cascade_uninstalled = cascade_uninstalled.clone();
    }

    // ── GAP-6：既有插件声明/实现变更 → 复验 + 重注册 ────────────────────
    // 声明指纹 = 磁盘 manifest 序列化内容哈希（纯内容比对，与 mtime 无关）；
    // 代码指纹 = 插件源码/配置 mtime 指纹（与 invoker respawn 判据同源，
    // compute_plugin_fingerprint）。任一变化即触发重注册：
    // - G2 适用插件（sidecar 且声明 tools/services）先 g2_verify_and_sanitize
    //   再按结果重注册——manifest 编辑不得绕过 G2 把已净化剔除的工具复活；
    //   仅代码变化（实现修复）同样复验，工具恢复无需等 manifest 再改或重启；
    // - 非 G2 插件维持"声明变更即重注册"（G2 对其本就 no-op）。
    // in_process(cdylib) 插件不在此列——代码变更走 A3 优雅重启路径整体重建。
    // 复验对象 = 本轮 sync **开始前**已登记且未被本轮卸载的插件：主循环刚注册
    // 的新插件已落声明基线，若在此按净化 manifest 重判会把基线覆写成净化指纹
    // （下轮 raw 声明必判"变更"→ 被剔工具复活）。
    let cascade_set: HashSet<String> = cascade_uninstalled.iter().cloned().collect();
    let mut changed_plugin_ids: Vec<String> = Vec::new();
    for m in &filtered {
        if !pre_registered.contains(&m.id)
            || cascade_set.contains(&m.id)
            || m.host_type == HostType::InProcess
        {
            continue;
        }
        let fp = manifest_fingerprint(m);
        // 一行接入自愈：动态 MCP 注册面零工具 = 观测未成功过（boot 纯声明注册
        // 不走 G2 / 上轮 spawn 失败）——本轮补观测导入，不依赖声明/代码指纹
        // （第三方服务的工具集变更本就不在本地指纹内）。导入成功后条件即失效，
        // 不再逐轮重观测。须在无基线分支之前算：boot 后首轮 sync 只建基线，
        // 不提前 hoist 会把导入推迟到第二轮。
        let dynamic_import =
            is_dynamic_mcp(m) && !registry.list_tools().iter().any(|t| t.plugin_id == m.id);
        let decl_changed = match known_manifest_hashes.get(&m.id) {
            Some(old_fp) => *old_fp != fp,
            None => {
                // 无基线（boot 注册/升级前）：建基线不动作，避免首轮误重注册
                known_manifest_hashes.insert(m.id.clone(), fp);
                known_code_hashes.insert(m.id.clone(), current_code_fp(code_dirs, m));
                if !dynamic_import {
                    continue;
                }
                false
            }
        };
        let g2_applicable = m.host_type == HostType::Sidecar
            && (!m.capabilities.tools.is_empty() || !m.capabilities.services.is_empty());
        let cur_code_fp = if g2_applicable {
            current_code_fp(code_dirs, m)
        } else {
            0
        };
        let code_changed = g2_applicable
            && known_code_hashes
                .get(&m.id)
                .is_some_and(|old| cur_code_fp != *old);
        if !decl_changed && !code_changed && !dynamic_import {
            continue;
        }
        let (tools, http_routes) = if g2_applicable || dynamic_import {
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
                        "G2 复验：插件存在未声明暴露的工具（不拒绝注册）"
                    );
                } else {
                    warn!(
                        target: "plugin_watcher",
                        plugin = %m.id,
                        rejected = ?outcome.rejected_tools,
                        decl_changed,
                        code_changed,
                        "G2 复验：声明与实现漂移，剔除漂移工具后重注册（其余能力照常）"
                    );
                    report.drifted_plugins.push(m.id.clone());
                }
            }
            if outcome.spawn_failed {
                warn!(
                    target: "plugin_watcher",
                    plugin = %m.id,
                    "G2 复验观测失败（重试后仍 spawn/上报不可用）——按声明重注册，账本标记校验未完成，待复验"
                );
            }
            crate::plugin_lifecycle::reenable_plugin_capabilities(
                &outcome.manifest,
                registry,
                scopes,
            )
        } else {
            crate::plugin_lifecycle::reenable_plugin_capabilities(m, registry, scopes)
        };
        // 共享 store（AppState.manifests）落**磁盘声明**（未净化）——净化版只存于
        // 注册面/账本；若净化版覆盖 store，enable 热路径读到空工具声明→永远复验
        // 无物→剔除死锁（08-31 实测 task_manage/memory disable-enable 救不回）。
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
            decl_changed, code_changed,
            "插件变更已复验重注册（无需重启内核）"
        );
        changed_plugin_ids.push(m.id.clone());
        // 基线一律落**声明**指纹与当前代码指纹——净化版指纹会让下轮 raw 声明
        // 必判"变更"，复验退化为每轮重注册（且复活被剔工具）。
        known_manifest_hashes.insert(m.id.clone(), fp);
        known_code_hashes.insert(m.id.clone(), cur_code_fp);
    }
    // 新注册插件建立指纹基线（下轮起参与变更检测）。G2 适用插件已在主循环以
    // 声明（raw）指纹落基线——此处 or_insert 只补无工具插件等未落基线者，
    // 不覆盖（filtered 里的净化版指纹一旦落库，下轮必误判变更）。
    for id in &report.new_plugin_ids {
        if let Some(m) = filtered.iter().find(|x| &x.id == id) {
            known_manifest_hashes
                .entry(id.clone())
                .or_insert_with(|| manifest_fingerprint(m));
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
    /// GAP-6：plugin_id → manifest 声明内容指纹（磁盘 manifest，变更检测基线，
    /// consumer 独占）。净化后的注册 manifest 指纹不落此表——否则下轮 raw 声明
    /// 必判"变更"，被剔工具随重注册复活。
    known_manifest_hashes: HashMap<String, u64>,
    /// 已知插件上次复验时的代码指纹（consumer 独占）：变化即触发 G2 复验——
    /// sidecar 实现修复不改 manifest，恢复只能靠代码指纹判定。
    known_code_hashes: HashMap<String, u64>,
    /// 插件源码目录解析器（bin 装配注入）。None = 不做代码指纹复验（测试）。
    code_dirs: Option<std::sync::Arc<CodeDirResolver>>,
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
            known_code_hashes: HashMap::new(),
            code_dirs: None,
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

    /// 注入插件源码目录解析器（生产装配传 `PluginInvokerImpl::plugin_source_dir`）：
    /// 已知插件代码指纹变化即触发 G2 复验（实现修复后工具恢复，无需等 manifest
    /// 再改或重启）。不注入 = 仅声明指纹驱动复验（测试/旧行为）。
    pub fn with_code_dir_resolver(mut self, resolver: std::sync::Arc<CodeDirResolver>) -> Self {
        self.code_dirs = Some(resolver);
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
    pub fn with_enabled_ids(mut self, enabled: Arc<tokio::sync::RwLock<HashSet<String>>>) -> Self {
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
            known_code_hashes,
            code_dirs,
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
            let mut known_code = known_code_hashes;
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
                    &mut known_code,
                    code_dirs.as_ref(),
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
mod tests;
