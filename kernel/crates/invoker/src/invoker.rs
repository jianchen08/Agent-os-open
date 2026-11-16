// @feature: FP-0.2.一 插件 invoker(保活池/成员级热换) | @ci: rust-test
//! PluginInvoker 实现
//!
//! 按 host_type 透明分发调用：
//! - InProcess: 经 `NativePluginLoader` 加载 cdylib，走 C-ABI 调用（JSON 经内存传递）
//! - Sidecar: 通过 MCP 客户端走 JSON-RPC 协议调用（进程隔离）
//!
//! 两种 host_type 共用 PluginInput / PluginResult JSON 契约，invoker 透明分发。
//! （原 Wasm 轨已按两轨终局决策关闭摘除，见 core::traits::HostType 文档。）

use std::collections::HashMap;
use std::sync::atomic::AtomicUsize;
use std::sync::Arc;
use std::time::{Duration, Instant};

use agentos_core::traits::{
    CapabilityRegistry, HookContext, HostBoxesSnapshot, HostType, LifecycleHook, PluginInvoker,
    PluginLoader, PluginManifest, PluginType,
};
use agentos_core::types::{PluginContext, PluginError, PluginResult, ToolExecutionResult};
use agentos_hooks::HookEventBus;
use agentos_mcp::member_offline::MemberOfflineNotice;
use agentos_mcp::{CapabilityRouter, McpClient};
use agentos_plugin_loader::NativePluginLoader;
use async_trait::async_trait;
use parking_lot::RwLock;
use serde_json::Value;
use tracing::{info, warn};

mod host_acquire;
mod host_job;
mod host_key;
mod host_pool;
mod inflight_gc;
mod invoke_paths;
mod lifecycle;
mod mcp_result;
mod monitoring;
mod native_services;
mod recovery;
mod spawn_backoff;
mod timeout;
mod tool_pre_dispatch;
mod unload;
mod venv;

use spawn_backoff::{GateDecision, SpawnBackoffLedger};

// 对外契约面：lib.rs re-export 与 api crate 直引（agentos_invoker::invoker::*）
// 的符号集合冻结，经本层 re-export 保持路径不变（ADR 2026-10-12 文件长度机械闸
// 拆分的契约面零漂移锚）。
pub use host_key::{
    builtin_group_host_dir, compute_plugin_fingerprint, find_group_host_dir, is_cohost_member,
    PluginScopedRouter, GROUP_HOST_DIR,
};
pub use host_pool::{pool_keepwarm_from_env, pool_max_requests_from_env, pool_ttl_secs_from_env};
pub use monitoring::HostProcSnapshot;
pub use venv::{
    find_registered_venv_interpreter, find_user_space_venv_interpreter, find_venv_interpreter,
    interpreter_layout, is_plain_python_command,
};

use host_job::HostJobGuard;
use host_key::LightPacking;
use mcp_result::{pipeline_error_params, tool_error_params};
pub use timeout::state_plugin_call_timeout;
use timeout::{
    raised_need_from_inputs, resolve_call_limit, tool_timeout_ms, with_capability_timeout,
    with_raisable_call_timeout, RaisedCallNeeds,
};

// tests.rs 以 `use super::*` 全量 glob 取名；符号搬入子模块后经本组 cfg(test)
// 导入维持 glob 可见面（lib 车道 cfg 掉零负担）。
#[cfg(test)]
use agentos_core::traits::HostBoxKind;
#[cfg(test)]
use agentos_mcp::McpError;
#[cfg(test)]
use host_key::{
    cohost_packing_domain, diff_member_fingerprints, group_host_key, host_start_timeout,
    is_valid_group_name, light_host_max_members, namespaced_tool_name, parse_group_slot,
    scoped_router_identity, slot_capacity_for_tier, solo_host_key, union_fingerprint_of,
    MemberFingerprintDiff,
};
use inflight_gc::InflightSinceRegistry;
#[cfg(test)]
use inflight_gc::StartingPluginsGuard;
#[cfg(test)]
use mcp_result::{
    extract_mcp_content, normalize_mcp_tool_result, normalize_native_tool_output,
    sanitize_external_mcp_arguments,
};
#[cfg(test)]
use native_services::{NativeHostServices, NATIVE_BRIDGE_SLOW_CALL_WARN_AFTER};
#[cfg(test)]
use std::time::SystemTime;
#[cfg(test)]
use timeout::bridge_timeout_ms;
#[cfg(test)]
use venv::{
    resolve_venv_direct_interpreter, resolve_venv_direct_interpreter_for_spawn,
    venv_interpreter_layout,
};

/// 共享 MCP 客户端句柄：宿主键粒度缓存条目——外层 [`RwLock`] 护表，内层
/// tokio RwLock 护连接本身的并发读（一次 execute 持读锁全程）。
type SharedMcpClient = Arc<tokio::sync::RwLock<McpClient>>;

/// spawn 熔断跳闸通知（卡2/卡3）：宿主连续 spawn 失败达阈值、停止自动
/// spawn 的判定事件——消费面（内核 main）据此发系统通知。恰好一次。
#[derive(Debug, Clone)]
pub struct SpawnBreakerNotice {
    /// 触发熔断的宿主键（`plugin:{id}` / `group:light:{n}`）。
    pub host_key: String,
    /// 本次触发判定的调用方插件 id（组宿主 = 其中一个成员）。
    pub plugin_id: String,
    /// 该宿主承载的全部成员（独占 = 单插件；通知面据此报下线范围）。
    pub members: Vec<String>,
    /// 触发时的连续失败次数（= 熔断阈值）。
    pub consecutive: u32,
    /// 末次失败根因（透传给用户，不再二次包装）。
    pub last_error: String,
}

/// 内存预算闸回调形态（ADR 2026-10-19 D5 闸面，见
/// [`PluginInvokerImpl::set_pre_spawn_gate`]）：宿主 spawn 前准入预检，
/// `Err(原因)` = 拒绝本次 spawn。同步回调（内核 Private Bytes 采样为单次
/// syscall 级）；采样与预算配置全在装配方闭包内——采样函数在内核 bin 层，
/// invoker 不能反向依赖 bin（with_restart_hook 注入先例同构）。
#[allow(clippy::type_complexity)]
pub type PreSpawnGate = Arc<dyn Fn() -> Result<(), String> + Send + Sync>;

/// spawn 恢复通知（卡3 恢复沿）：已熔断宿主被一次成功 spawn 收束的判定
/// 事件——消费面（内核 main）据此发 kind=restored 系统通知。恰好一次
/// （record_success 返回 true 时；未熔断的普通成功不产本事件）。
#[derive(Debug, Clone)]
pub struct SpawnRecoveredNotice {
    /// 收束的宿主键（`plugin:{id}` / `group:light:{n}`）。
    pub host_key: String,
    /// 本次成功 spawn 的调用方插件 id。
    pub plugin_id: String,
    /// 该宿主承载的全部成员（通知面据此报恢复范围）。
    pub members: Vec<String>,
}

/// PluginInvoker 实现。
///
/// 管理插件实例和 MCP 客户端连接，按 host_type 透明分发调用。
/// 支持崩溃隔离：检测子进程崩溃后卸载能力 + 告警。
pub struct PluginInvokerImpl {
    /// 插件加载器（用于查找 manifest）
    loader: Arc<dyn PluginLoader>,
    /// 已连接的 MCP 客户端 {宿主键: McpClient}（合宿进程模型 §4.2）。
    ///
    /// 宿主键：light 合宿插件 → `group:light:{n}`（同宿主成员共享一个客户端/
    /// 进程）；其余 sidecar → `plugin:{plugin_id}`（独占宿主，现状语义）。
    mcp_clients: RwLock<HashMap<String, SharedMcpClient>>,
    /// per-host spawn 互斥锁（single-flight，防并发请求竞态 spawn 多个宿主进程）。
    ///
    /// 同一宿主键的 spawn 串行化：首个请求持锁 spawn 并写缓存，后续请求拿锁后
    /// 二次查缓存命中直接复用；跨宿主并行互不阻塞（合宿进程模型 §4.2 第 4 条：
    /// spawn 锁粒度从 per-plugin 改为 per-host-key）。
    spawn_locks: RwLock<HashMap<String, Arc<tokio::sync::Mutex<()>>>>,
    /// 崩溃回调（插件崩溃时调用）
    #[allow(clippy::type_complexity)]
    crash_callbacks: RwLock<Vec<Arc<dyn Fn(&str) + Send + Sync>>>,
    /// spawn 熔断跳闸回调（卡3 通知面，见 [`SpawnBreakerNotice`]）。
    #[allow(clippy::type_complexity)]
    spawn_breaker_callbacks: RwLock<Vec<Arc<dyn Fn(&SpawnBreakerNotice) + Send + Sync>>>,
    /// spawn 恢复回调（卡3 恢复沿，见 [`SpawnRecoveredNotice`]）。
    #[allow(clippy::type_complexity)]
    spawn_recovered_callbacks: RwLock<Vec<Arc<dyn Fn(&SpawnRecoveredNotice) + Send + Sync>>>,
    /// 合宿成员下线回调（卡3：host.py `[cohost][member-offline]` → 系统通知）。
    #[allow(clippy::type_complexity)]
    member_offline_callbacks: RwLock<Vec<Arc<dyn Fn(&MemberOfflineNotice) + Send + Sync>>>,
    /// Capability 路由器——sidecar→内核反向调用通道。
    /// 设置后，新建的 MCP 客户端会带上路由器；已有客户端需重连才生效。
    router: RwLock<Option<Arc<dyn CapabilityRouter>>>,
    /// 生命周期钩子事件总线（旁路广播，可选注入）。
    ///
    /// `None`（默认）时行为不变——sidecar spawn 的 `notifications/on_load` 仍是
    /// 点对点直调目标插件的权威路径；`Some` 时在直调**旁路**额外把 OnLoad 等事件
    /// fan-out 给订阅者（审计/指标），best-effort、非阻塞。镜像 `router` 字段的
    /// `RwLock<Option<Arc<_>>>` 形态，经 [`set_hook_bus`] 在构造后注入。
    hook_bus: RwLock<Option<Arc<HookEventBus>>>,
    /// 原生插件加载器（task_11 N2）：host_type==InProcess 时用于加载/执行 cdylib。
    /// None 时原生插件调用返回 NATIVE_LOADER_NOT_CONFIGURED。
    native_loader: Option<Arc<NativePluginLoader>>,
    /// Pull 热加载指纹缓存 {宿主键: (上次指纹, 上次检测时刻)}。
    /// 调用 sidecar 时比对：TTL 内跳过 stat（零开销），TTL 过后 stat mtime 比对，
    /// 指纹变化则 kill 旧进程走 respawn 路径加载新代码/配置。
    /// （未用到也不检测——纯按需 pull。）
    /// 合宿宿主指纹 = 当前成员指纹并集 + 成员集本身（§4.6）：任一成员代码变更或
    /// 成员集变化 → kill 整宿主 respawn。
    fingerprints: RwLock<HashMap<String, (u64, Instant)>>,
    /// 成员最后调用时刻 {plugin_id: Instant}——空闲软卸载依据（成员粒度记账，
    /// ADR 2026-09-24-member-granularity-unload-mode-panel-warmup 取代原宿主键
    /// 记账：合宿卸载粒度成员化后，每个成员按自己的调用时钟与自己的
    /// `lifecycle.idle_timeout_secs` 独立判定，不再整组连坐）。
    ///
    /// 每次 get_or_create_mcp_client 命中/创建时刷新（调用路径已知 plugin_id）；
    /// 后台 GC 据此判定成员是否空闲超时。
    member_last_used: RwLock<HashMap<String, Instant>>,
    /// 假死 respawn 熔断账本 {宿主键: Vec<respawn 时刻>}（进程治理三信号模型）。
    ///
    /// 10 分钟窗口内 ≥3 次假死 respawn 则停手只告警——防循环重启掩盖根因
    /// （同构 K8s CrashLoopBackOff 退避语义）。窗口滑出后自然解除。
    freeze_respawns: RwLock<HashMap<String, Vec<Instant>>>,
    /// spawn 失败指数退避 + 熔断账本（按宿主键，见 [`spawn_backoff`]）：
    /// 连续 spawn 失败先退避后熔断，终结对坏插件的无限 respawn（装机
    /// 2026-10-17 实证 535 次 ImportError 循环）。指纹变化/成功/显式卸载清账。
    spawn_backoff: SpawnBackoffLedger,
    /// 宿主进程 spawn 时刻（监控 M3 进程态轮询的 uptime 源；同宿主键 respawn 覆盖写，
    /// 驱逐不清理——宿主键集合有界 `plugin:{id}`/`group:light:{n}`，快照按
    /// mcp_clients 键取值，陈旧条目无副作用）。
    host_spawned_at: RwLock<HashMap<String, Instant>>,
    /// light 组运行时装箱状态（分配表 + 槽位计数，见 [`LightPacking`]）。
    light_packing: RwLock<LightPacking>,
    /// light 宿主**实有成员集** {宿主键: 成员集}——进程服务面的协议应答记账。
    ///
    /// **权威是分配表**（[`LightPacking::assignments`] 反查，见
    /// [`Self::host_members`]），本表只是"宿主里此刻真有哪些成员"的镜像，
    /// 只由三次协议结果更新：spawn（`--members` 实参）、`load_member` 应答成功、
    /// `unload_member` 应答成功（ADR 2026-10-29 合宿成员集单一权威）。
    /// 它不持有期望态，也不参与"该不该有某成员"的判定。
    ///
    /// 消费面 = 两处需要「进程实有」而非「期望态」的判断：
    /// 成员集漂移检测（[`Self::light_host_members_drifted`]，权威 vs 镜像；
    /// 差异由 [`Self::sync_host_member_set`] 按权威补齐）与 idle GC 的
    /// "进程内是否只剩最后一名成员"判定（软卸载成员仍在分配表里，按表长会
    /// 误判，见 `inflight_gc` 注释）。
    ///
    /// 生命周期与 `mcp_clients` 条目同步：spawn 时写入（[`Self::build_sidecar_transport_client`]），
    /// 进程 kill/驱逐时清除（[`Self::unload_host`] / fast path 驱逐）。
    spawned_members: RwLock<HashMap<String, Vec<String>>>,
    /// light 宿主**软卸载待重加入**成员集 {宿主键: 成员 id 集}（暂离记账，
    /// 生命周期与 `spawned_members` 同步）。
    ///
    /// 成员粒度软卸载（[`Self::finalize_member_unload`]）从进程服务面摘除成员
    /// 但保留分配条目（f3a6a5e39 待重加入语义）。暂离 = 「期望成员集未变、
    /// 进程服务面暂时收缩」，不构成成员集变化：漂移检测
    /// （[`Self::light_host_members_drifted`]）据本集排除暂离成员——其余成员
    /// 的调用不因某成员暂离而整组 reload；被卸成员**自己**被调用时由
    /// [`Self::sync_host_member_set`] 只把它一个载回进程（其余暂离成员继续
    /// 暂离，ADR 2026-10-29 前是整组 respawn 一次带回全部）。
    /// `load_member` 应答成功或 respawn 重建快照时撤销；宿主驱逐/停机随
    /// `spawned_members` 一并清空。
    pending_rejoin_members: RwLock<HashMap<String, std::collections::HashSet<String>>>,
    /// 组宿主 **spawn 时刻**每成员指纹表 {宿主键: {plugin_id: 插件指纹}}
    /// （ADR 2026-10-19 D2：pull 路径成员级热换的定位基准）。
    ///
    /// 并集指纹只回答「宿主有没有变」，从差值无法定位是哪个成员变了（9-15
    /// 只覆盖 watcher 路径的按插件定向重载）；本表把 spawn 快照从「并集」
    /// 扩展为「并集 + 每成员」，pull 路径 stale 时逐成员比对即可定位变更
    /// 成员，走既有 `reload_member` 进程内热换（成员集不变时），失败才整组
    /// 驱逐。写入时机与 `spawned_members` 同点（[`PluginInvokerImpl::
    /// build_sidecar_transport_client`]，先于 spawn）；成员级重载成功
    /// （[`PluginInvokerImpl::reload_group_member`]）同步刷新该成员条目
    /// （进程内代码已是磁盘现码，不刷新会被 pull diff 误报仍需热换——9-15
    /// 根因 3 的每成员版）。
    ///
    /// 生命周期与 `spawned_members` 完全同步：spawn 时写入、宿主驱逐/卸载/
    /// double-check 重建时随记录清除；表缺失 = 进程内容未知，pull diff 按
    /// fail-closed 回退整组重建（BUG-44 同哲学）。
    host_member_fingerprints: RwLock<HashMap<String, std::collections::BTreeMap<String, u64>>>,
    /// 预热常驻集（boot 预热的管道引用插件 id）：这些插件所在宿主组豁免
    /// 空闲回收（预热常驻语义——否则预热被 GC 架空，首条消息重新全价冷启动）。
    /// 集合只增（boot 预热一次定型）；`AGENTOS_DISABLE_SIDECAR_WARMUP=1` 时
    /// 预热不运行、集合恒空，回到纯懒加载 + 全量 GC。
    keep_warm_plugins: RwLock<std::collections::HashSet<String>>,
    /// 宿主 in-flight capability 调用计数 {宿主键: 原子计数}（P12/P22，ADR 2026-09-07）。
    ///
    /// sidecar 统一调用入口（`invoke_pipeline_plugin` / `invoke_tool` 的 Sidecar
    /// 分支，覆盖管道步骤、tool-executor 反向调用含 llm_service 流式、http.handle
    /// 分发）+1（RAII [`InflightGuard`]，Drop -1；成功/失败/超时/流式中断随
    /// future drop 必然回落），空闲 GC 判定以 `inflight == 0` 为前置门——调用
    /// 进行中的宿主不被回收。流式调用的 MCP 响应在流完全结束/断连才返回，
    /// 计数即覆盖整个流生命周期。条目与 `mcp_clients` 同生命周期：
    /// [`Self::unload_host`] 清理；调用先于卸载重建条目也无碍（guard 持 Arc
    /// 克隆，减数不依赖条目存活）。
    inflight_calls: RwLock<HashMap<String, Arc<AtomicUsize>>>,
    /// 成员 in-flight capability 调用计数 {plugin_id: 原子计数}（成员粒度 GC 门：
    /// 该成员有调用在飞时不卸该成员；宿主仍由 [`Self::inflight_calls`] 守整组
    /// kill 面）。RAII 语义与 [`Self::inflight_calls`] 同构。
    member_inflight_calls: RwLock<HashMap<String, Arc<AtomicUsize>>>,
    /// sidecar 在飞 guard 创建时刻记账 {宿主键: {token: (创建时刻, 成员 id)}}：
    /// 残留 pin 判定的记账面（R7 F1，见 inflight_gc 模块文档）。条目生命周期
    /// 与 [`Self::inflight_calls`] 同构（unload_host 同刀清理）；guard 持 Arc
    /// 克隆自清 token，不依赖条目存活。
    inflight_since: RwLock<HashMap<String, InflightSinceRegistry>>,
    /// 正在 spawn/respawn 窗口内的插件 id 集（宿主连接 + initialize 握手未完成）。
    ///
    /// D1 宽限面（宿主重载风暴 2026-09-25）：HTTP dispatcher 据此区分「插件处理
    /// 慢」与「基础设施冷启动等待」，前者烧端点超时预算、后者暂停预算（等待
    /// 就绪而非 504，见 http_dispatcher 的分发循环）。spawn 锁内写入本次加载的
    /// 全体成员（组宿主 = 成员集，独占 = 单插件），spawn 出窗（成功/失败/panic）
    /// 由 RAII guard 摘除。键为 plugin_id 扁平集：dispatcher 侧按路由插件 id
    /// O(1) 探测，无需 plugin→宿主键反查。
    starting_plugins: RwLock<std::collections::HashSet<String>>,
    /// 全局在飞工具执行上限（异步准入闸）。工具是跨进程 I/O：等待者只占一个
    /// future，不占线程；此信号量限制同时执行中的工具数（sidecar 进程/内存
    /// 上界），与 tokio 阻塞池解耦。许可随调用完成/超时/取消 drop 归还。
    tool_admission: Arc<tokio::sync::Semaphore>,
    /// 准入上限（观测/日志用；boot 装配后不变）。
    tool_admission_limit: usize,
    /// 能力注册表（M4 预派发等待界派生的反查源，tool_name → plugin_id）：
    /// 预派发等待界 = 派发目标工具的声明界（ADR
    /// 2026-11-05-timeout-taxonomy-and-merge M4，BUG-60 人肉对齐常量退役）。
    /// None（未装配，测试/独立构造）= 反查不可达，派生落内核 capability 缺省界。
    tool_registry: RwLock<Option<Arc<dyn CapabilityRegistry>>>,
    /// L1 让位源登记面（ADR 2026-11-05 §5.1）：在飞内层调用声明需求
    /// {pipeline_id: {需求秒: 计数}}。登记点 = invoke_tool（声明界解析单点），
    /// 消费点 = 管道步壳让位查询（见 [`timeout::with_raisable_call_timeout`]）。
    raised_call_needs: RaisedCallNeeds,
    /// per-host Job Object 守卫 {宿主键: 守卫}（ADR 2026-10-19 D4 第二刀，
    /// containment 契约见 [`host_job`] 模块注释）。生命周期与 `mcp_clients`
    /// 条目同步：spawn 成功登记（[`Self::register_host_job`]），驱逐/kill 随
    /// 记录释放（[`Self::release_host_job`]）——Drop = CloseHandle =
    /// KILL_ON_JOB_CLOSE 整树陪葬，孤儿结构性归零。env 未启用（默认关）时
    /// 表恒空，全路径 no-op。
    host_jobs: RwLock<HashMap<String, HostJobGuard>>,
    /// 内存预算闸回调（ADR 2026-10-19 D5 闸面，可选注入，见 [`PreSpawnGate`]）：
    /// 宿主 spawn 前准入预检，`Err(原因)` = 拒绝本次 spawn。`None`（默认，
    /// 未注入 = 预算未启用）恒直通零开销；注入形态与生命周期同 `set_router`/
    /// `set_hook_bus`（启动期装配一次）。
    pre_spawn_gate: RwLock<Option<PreSpawnGate>>,
    /// 宿主累计请求数 {宿主键: 计数}（ADR 2026-10-19 D1/B8 轮换记账源，语义
    /// 见 [`host_pool`] 模块注释）。sidecar 统一调用入口每次 capability 调用
    /// +1（[`Self::record_host_request`]）；spawn 覆盖写归零（计数属进程，
    /// respawn = 新进程新计数，不跨代）；unload_host 随记录清理。其余驱逐
    /// 路径不清理无副作用——GC 只对 `mcp_clients` 在场宿主消费该值，而
    /// respawn 必经覆盖写归零（`host_spawned_at` 同款记账口径）。
    host_request_counts: RwLock<HashMap<String, u64>>,
}

/// 未显式装配时的准入上限：测试/独立构造不设闸（生产 main 用设备档值覆盖）。
const DEFAULT_TOOL_ADMISSION_LIMIT: usize = 1024;

impl PluginInvokerImpl {
    /// 创建 PluginInvoker。
    pub fn new(loader: Arc<dyn PluginLoader>) -> Self {
        Self {
            loader,
            mcp_clients: RwLock::new(HashMap::new()),
            spawn_locks: RwLock::new(HashMap::new()),
            crash_callbacks: RwLock::new(Vec::new()),
            spawn_breaker_callbacks: RwLock::new(Vec::new()),
            spawn_recovered_callbacks: RwLock::new(Vec::new()),
            member_offline_callbacks: RwLock::new(Vec::new()),
            router: RwLock::new(None),
            hook_bus: RwLock::new(None),
            native_loader: None,
            fingerprints: RwLock::new(HashMap::new()),
            member_last_used: RwLock::new(HashMap::new()),
            freeze_respawns: RwLock::new(HashMap::new()),
            spawn_backoff: SpawnBackoffLedger::new(),
            host_spawned_at: RwLock::new(HashMap::new()),
            light_packing: RwLock::new(LightPacking::default()),
            spawned_members: RwLock::new(HashMap::new()),
            pending_rejoin_members: RwLock::new(HashMap::new()),
            host_member_fingerprints: RwLock::new(HashMap::new()),
            keep_warm_plugins: RwLock::new(std::collections::HashSet::new()),
            inflight_calls: RwLock::new(HashMap::new()),
            member_inflight_calls: RwLock::new(HashMap::new()),
            inflight_since: RwLock::new(HashMap::new()),
            starting_plugins: RwLock::new(std::collections::HashSet::new()),
            tool_admission: Arc::new(tokio::sync::Semaphore::new(DEFAULT_TOOL_ADMISSION_LIMIT)),
            tool_admission_limit: DEFAULT_TOOL_ADMISSION_LIMIT,
            tool_registry: RwLock::new(None),
            raised_call_needs: RaisedCallNeeds::default(),
            host_jobs: RwLock::new(HashMap::new()),
            pre_spawn_gate: RwLock::new(None),
            host_request_counts: RwLock::new(HashMap::new()),
        }
    }

    /// 装配能力注册表（启动期，链式）：M4 预派发等待界派生的反查源。
    pub fn with_tool_registry(self, registry: Arc<dyn CapabilityRegistry>) -> Self {
        *self.tool_registry.write() = Some(registry);
        self
    }

    /// 测试注入 spawn 熔断阈值（生产读 env 一次定型，见 [`spawn_backoff`]）。
    #[cfg(test)]
    pub(crate) fn with_spawn_breaker_threshold(mut self, threshold: u32) -> Self {
        self.spawn_backoff = SpawnBackoffLedger::with_threshold(threshold);
        self
    }

    /// 取一个工具执行许可（全局异步准入闸；等待只挂 future，不占线程）。
    ///
    /// 供 [`PluginInvoker::invoke_tool`] 全路径调用；返回值即 RAII 许可，
    /// 调用完成/超时/取消时 drop 归还。
    pub(super) async fn acquire_tool_slot(
        &self,
        plugin_id: &str,
        tool_name: &str,
    ) -> Result<tokio::sync::SemaphorePermit<'_>, PluginError> {
        let started = Instant::now();
        let permit = self
            .tool_admission
            .acquire()
            .await
            .map_err(|_| PluginError {
                message: format!("tool admission closed while invoking '{plugin_id}.{tool_name}'"),
                code: Some("TOOL_ADMISSION_CLOSED".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
        let waited = started.elapsed();
        if waited >= Duration::from_secs(1) {
            warn!(
                target: "tool-admission",
                plugin = %plugin_id,
                tool = %tool_name,
                waited_ms = waited.as_millis() as u64,
                limit = self.tool_admission_limit,
                "tool execution waited for admission"
            );
        }
        Ok(permit)
    }

    /// 装配全局工具准入上限（启动期，链式）。
    ///
    /// 上限是**同时执行中的工具调用数**，不是线程数：超出的调用在 async
    /// 信号量上排队（future 挂起、零线程占用），许可随完成/超时/取消归还。
    pub fn with_tool_admission_limit(mut self, limit: usize) -> Self {
        let limit = limit.max(1);
        self.tool_admission = Arc::new(tokio::sync::Semaphore::new(limit));
        self.tool_admission_limit = limit;
        self
    }

    /// 当前工具准入上限（观测面）。
    pub fn tool_admission_limit(&self) -> usize {
        self.tool_admission_limit
    }

    /// 链式注入原生插件加载器（启动期装配用）。
    ///
    /// 启用 `host_type == InProcess` 的 cdylib 插件：放进插件目录 + 重启即用，
    /// 无需改任何代码（详见 [`Self::resolve_native_artifact`]）。
    pub fn set_native_loader(mut self, native_loader: Arc<NativePluginLoader>) -> Self {
        self.native_loader = Some(native_loader);
        self
    }

    /// 解析原生插件产物路径（manifest.native.artifact 相对插件目录）。
    ///
    /// `artifact` 可写裸名（如 `my_plugin`）
    /// 或带平台后缀（如 `my_plugin.dll`）。裸名时按平台补 `.dll`/`.so`/`.dylib`
    /// （[`NativePluginLoader::platform_artifact_name`]）。
    fn resolve_native_artifact(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
    ) -> Result<std::path::PathBuf, PluginError> {
        let artifact = manifest
            .native
            .as_ref()
            .map(|n| n.artifact.as_str())
            .ok_or_else(|| PluginError {
                message: format!(
                    "Native plugin '{plugin_id}' missing manifest.native.artifact (ADR 附录 D②)"
                ),
                code: Some("MISSING_NATIVE_ARTIFACT".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
        let dir = self
            .loader
            .get_plugin_dir(plugin_id)
            .ok_or_else(|| PluginError {
                message: format!(
                    "Native plugin '{plugin_id}' directory not found (not discovered?)"
                ),
                code: Some("NATIVE_PLUGIN_DIR_NOT_FOUND".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
        // 裸名自动补平台后缀；声明带异平台后缀时回退本平台重映射（与 loader 预检同规则）
        let dir_path = std::path::PathBuf::from(dir);
        NativePluginLoader::resolve_artifact(&dir_path, artifact).ok_or_else(|| PluginError {
            message: format!(
                "Native plugin '{}' artifact not found: {}（已按声明与本平台名双查；cdylib 未构建？）",
                plugin_id,
                dir_path.join(artifact).display()
            ),
            code: Some("NATIVE_ARTIFACT_NOT_FOUND".to_string()),
            source: Some("plugin-invoker".to_string()),
        })
    }

    /// 取 native loader 或返回配置缺失错误（三个 native 调用路径共用）。
    fn native_loader_or_err(
        &self,
        plugin_id: &str,
    ) -> Result<&Arc<NativePluginLoader>, PluginError> {
        self.native_loader.as_ref().ok_or_else(|| PluginError {
            message: format!(
                "Native plugin '{plugin_id}' invoked but no NativePluginLoader configured"
            ),
            code: Some("NATIVE_LOADER_NOT_CONFIGURED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })
    }

    /// 加载 cdylib（resolve artifact + 直接 trait object load，三处 native 路径共用）。
    fn load_native(
        &self,
        loader: &NativePluginLoader,
        plugin_id: &str,
        manifest: &PluginManifest,
    ) -> Result<(), PluginError> {
        let native_path = self.resolve_native_artifact(plugin_id, manifest)?;
        loader.load(plugin_id, &native_path)?;
        Ok(())
    }

    /// 插件权限声明的前置日志校验（P2-2）。
    ///
    /// 0.2 只做声明 + 日志告警，不做硬 enforce（filesystem/system_calls 留 0.3 沙箱）。
    /// 当前检测项：
    /// - 若 manifest 声明了 network 权限（`permissions.network.allowed_hosts` 非空），
    ///   则认为该插件可能联网；这是声明性记录，不阻断调用。
    ///
    /// 该函数不返回错误，**永远不阻断** invoke 流程。
    fn check_permissions(&self, plugin_id: &str, manifest: &PluginManifest) {
        let perms = &manifest.permissions;

        // 记录声明：network/filesystem/env_vars/system_calls 是否声明
        let has_network = !perms.network.allowed_hosts.is_empty();
        let has_fs =
            !perms.filesystem.read_paths.is_empty() || !perms.filesystem.write_paths.is_empty();
        let has_env = !perms.env_vars.is_empty();
        let has_syscalls = !perms.system_calls.is_empty();

        info!(
            plugin_id = plugin_id,
            network = has_network,
            filesystem = has_fs,
            env_vars = has_env,
            system_calls = has_syscalls,
            "Plugin permission declaration"
        );

        // 声明了要联网（有非空 host 列表）说明确实要联网，记 info 留痕。
        // 注：0.2 不强制 enforce，仅日志留痕供审计。
        if has_network {
            info!(
                plugin_id = plugin_id,
                hosts = ?perms.network.allowed_hosts,
                "Plugin declared network access"
            );
        }
    }
}
#[async_trait]
impl PluginInvoker for PluginInvokerImpl {
    /// 调用管道插件执行。
    ///
    /// 按 manifest 的 host_type 透明分发：
    /// - InProcess: 经 NativePluginLoader 加载 cdylib 并走 C-ABI 调用（JSON 契约）
    /// - Sidecar: 通过 MCP 客户端走 JSON-RPC tools/call
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        let loaded = self.loader.load(plugin_id).await?;
        let manifest = &loaded.manifest;

        // P2-2 插件权限声明前置日志校验（不阻断）
        self.check_permissions(plugin_id, manifest);

        // 冻结根因根修的插件级延伸刀（ADR 2026-10-15-plugin-call-timeout）+ L1
        // 让位机制（ADR 2026-11-05-timeout-taxonomy-and-merge §5.1）：有效界解析
        // 单点（[`timeout::resolve_call_limit`]，M3）——state 显式收紧 = Fixed
        // 压过一切；缺省档/声明档 = Raisable 可抬高 deadline：本插件执行期间
        // 在飞内层调用（反向工具/服务调用）声明需求更大时，等待 deadline 抬高
        // 至 now+声明值（max 合成单调不减，恒有界不变量保留）。缺省壳只管
        // 沉默者——human/approval 等已声明长等待的内层不再被 600s 缺省掐断
        // （装机 2026-11-05 阻塞交互卡叠卡事故的运行期修复本体）。让位源 =
        // [`RaisedCallNeeds`] 登记面（invoke_tool 解析声明界时登记，键
        // `_call_context.pipeline_id` 与本壳的 state.pipeline_id 对上）。
        // 到点放弃内层 future（最小取消语义，in-flight guard 随 drop 回落），
        // 错误码 STEP_CALL_TIMEOUT 与引擎 run 级封顶层同码（错误面单点可辨）。
        let pipeline_key = ctx
            .state
            .get("pipeline_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty());
        let limit = resolve_call_limit(manifest.capabilities.call_timeout_seconds, ctx.state);
        let raised = &self.raised_call_needs;
        let raise_lookup =
            pipeline_key.map(|pid| move || raised.max_in_flight(pid).map(Duration::from_secs));
        let result = with_raisable_call_timeout(limit, raise_lookup, plugin_id, async {
            match manifest.host_type {
                HostType::InProcess => {
                    // task_11 N2：经 NativePluginLoader 加载 cdylib 并通过 C-ABI 调用。
                    // config 注入与 sidecar 同源（shared::injected_config）——两轨对齐。
                    self.invoke_native_pipeline(plugin_id, manifest, ctx).await
                }
                HostType::Sidecar => {
                    // in-flight 计入（P22 统一入口）：本 trait 方法是所有 sidecar
                    // pipeline 调用的必经点（引擎管道步骤 / 直调），计数在此 +1，
                    // RAII guard 跨越透明恢复的 respawn+重试全程（成功/失败/超时/
                    // panic 随 future drop 回落），空闲 GC 不得回收本宿主。
                    let (_host_key, _inflight, _member_inflight) =
                        self.enter_sidecar_inflight(manifest);
                    // B1（M2-reactive 第一刀）：sidecar 死亡透明恢复——单次尝试抽出为
                    // attempt_sidecar_pipeline，死亡判定（PLUGIN_CRASHED）触发
                    // force_unload + respawn + 重试一次（长事务在途调用不被依赖死亡破坏）。
                    self.with_transparent_recovery(plugin_id, || async {
                        self.attempt_sidecar_pipeline(plugin_id, manifest, ctx)
                            .await
                    })
                    .await
                }
            }
        })
        .await;

        // 旁路广播 OnError：插件调用失败即在此中央返回处 emit 一次（审计日志 /
        // `lifecycle.plugin_error_total` 计数）。失败 = Err（进程崩溃/MCP/解析）
        // **或** Ok 携带失败结果（error 字段）——业务失败若不计，计数器恒零。
        // best-effort、非阻塞；bus=None 时 no-op。**不改错误处理语义**——原
        // Err 照常向上传播。
        if let Some((message, code)) = pipeline_error_params(&result) {
            self.emit_lifecycle_error(plugin_id, &message, code.as_deref());
        }
        result
    }

    /// 调用工具插件执行。
    ///
    /// 按 host_type 透明分发：
    /// - InProcess: 经 NativePluginLoader 加载 cdylib 走 C-ABI（inputs 作为 state）
    /// - Sidecar: 通过 MCP 客户端走 JSON-RPC tools/call
    async fn invoke_tool(
        &self,
        plugin_id: &str,
        tool_name: &str,
        inputs: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        let loaded = self.loader.load(plugin_id).await?;
        let manifest = &loaded.manifest;

        // P2-2 插件权限声明前置日志校验（不阻断）
        self.check_permissions(plugin_id, manifest);

        // 非流式调用总时长超时（P12，ADR 2026-09-07）：整体包界（含冷启动
        // spawn），到点放弃内层 future 返回结构化 CAPABILITY_TIMEOUT，in-flight
        // 计数随 drop 回落。流式豁免：流式走 invoke_pipeline_plugin（event-bus
        // 旁路 chunk），该路径不包界——流式语义是长连接，不做总时长熔断。
        // （P22 勘误：经 tool-executor 反向调用的流式工具如 llm_service
        // llm.complete_stream 实际也流经本方法——其等待界 = 插件自声明
        // `mcp.request_timeout_secs`（内核界与传输层界同源同值，行为与 P12 前
        // 一致）；需要超越插件声明的流式工具经 `timeout_ms: Some(0)` 声明豁免。）
        //
        // L1 让位登记（ADR 2026-11-05 §5.1）：本工具的声明界进入在飞登记面
        // （[`RaisedCallNeeds`]，键 = `_call_context.pipeline_id`）——包裹本
        // 调用的管道步壳（A5）在缺省档到点时据此让位。guard 活到本调用结束
        // （含超时/取消——future drop 时随局部变量递减，登记面不漏）。
        let tool_bound_ms = tool_timeout_ms(manifest, tool_name);
        let _raised_need =
            raised_need_from_inputs(inputs, tool_bound_ms).map(|(pipeline_id, need_secs)| {
                self.raised_call_needs.register(&pipeline_id, need_secs)
            });
        let result = with_capability_timeout(tool_bound_ms, plugin_id, tool_name, async {
            // 全局异步准入闸：超出的调用在这里挂起 future 排队，不占线程、
            // 不占阻塞池；许可随下面对话完成/超时/取消 drop 归还。
            let _permit = self.acquire_tool_slot(plugin_id, tool_name).await?;
            match manifest.host_type {
                HostType::InProcess => {
                    // task_11 N2：原生工具插件——inputs 作为 state，config 同 pipeline 路径注入。
                    // B2：经 execute 的 tool_call 约定字段表达工具语义（旧 pipeline 插件零破坏）。
                    self.invoke_native_tool(plugin_id, manifest, tool_name, inputs)
                        .await
                }
                HostType::Sidecar => {
                    // in-flight 计入（P22 统一入口，包在超时 future 内——超时放弃
                    // 内层 future 时 guard 随 drop 回落）：本 trait 方法是所有
                    // sidecar 工具调用的必经点（tool-executor 反向调用含
                    // llm.complete_stream 流式、http.handle 分发），计数覆盖
                    // 透明恢复的 respawn+重试全程；流式调用的 MCP 响应在流
                    // 完全结束才返回，guard 即覆盖整个流生命周期。
                    let (_host_key, _inflight, _member_inflight) =
                        self.enter_sidecar_inflight(manifest);
                    // B1（M2-reactive 第一刀）：sidecar 死亡透明恢复——与 pipeline 路径同构。
                    self.with_transparent_recovery(plugin_id, || async {
                        self.attempt_sidecar_tool(plugin_id, manifest, tool_name, inputs)
                            .await
                    })
                    .await
                }
            }
        })
        .await;

        // 旁路广播 OnError：工具插件调用失败即在此中央返回处 emit 一次（审计日志 /
        // `lifecycle.plugin_error_total` 计数）。失败 = Err（进程崩溃/MCP/解析）
        // **或** Ok 携带 success=false（isError/失败信封）——业务失败若不计，
        // 计数器恒零。best-effort、非阻塞；bus=None 时 no-op。**不改错误处理
        // 语义**——原 Err 照常向上传播。
        if let Some((message, code)) = tool_error_params(&result) {
            self.emit_lifecycle_error(plugin_id, &message, code.as_deref());
        }
        result
    }

    /// 发送生命周期钩子事件到指定插件。
    async fn send_lifecycle_hook(
        &self,
        plugin_id: &str,
        hook: LifecycleHook,
        context: &HookContext,
    ) -> Result<(), PluginError> {
        let loaded = self.loader.load(plugin_id).await?;
        let manifest = &loaded.manifest;

        // 组合插件不需要生命周期钩子（ADR ⑥）
        if manifest.plugin_type == PluginType::Composite {
            return Ok(());
        }

        let hook_name = match hook {
            LifecycleHook::OnLoad => "on_load",
            LifecycleHook::OnUnload => "on_unload",
            LifecycleHook::OnPipelineStart => "on_pipeline_start",
            LifecycleHook::OnPipelineEnd => "on_pipeline_end",
            LifecycleHook::OnError => "on_error",
            LifecycleHook::DomainEvent => "domain_event",
        };
        let tags = serde_json::to_value(context.tags()).unwrap_or_default();

        match manifest.host_type {
            HostType::Sidecar => {
                // Sidecar：经 MCP notification 发送（fire-and-forget）。客户端获取/
                // 创建失败同样仅 warn——对齐下方 InProcess 分支与 fire-and-forget
                // 语义（不阻断管道），但失败必须留痕、不得静默。
                match self.get_or_create_mcp_client(manifest).await {
                    Ok(client_arc) => {
                        let client = client_arc.read().await;
                        if client.is_alive().await {
                            let hook_method = format!("notifications/{hook_name}");
                            if let Err(e) = client.send_notification(&hook_method, Some(tags)).await
                            {
                                warn!("Lifecycle notification failed for {}: {}", plugin_id, e);
                            }
                        }
                    }
                    Err(e) => {
                        warn!(
                            "Lifecycle hook {hook_name} not delivered for {}: {}",
                            plugin_id, e
                        );
                    }
                }
            }
            HostType::InProcess => {
                // Native：没有 MCP 通知通道，钩子经 execute 传递——PluginInput 带
                // `hook` 字段（值为钩子名）+ config。插件 SDK 见到 hook 字段走钩子逻辑。
                // 错误仅 warn（与 sidecar 的 fire-and-forget 语义一致，不阻断管道）。
                if let Err(e) = self
                    .send_hook_via_execute(plugin_id, manifest, hook_name, &tags)
                    .await
                {
                    warn!("Lifecycle hook {hook_name} failed for {}: {}", plugin_id, e);
                }
            }
        }

        Ok(())
    }

    /// 宿主键解析（trait 默认按独占语义返回 plugin_id；此处交真实装箱表）。
    ///
    /// light 合宿成员经装箱分配表拿组键（粘性分配，与 spawn 路由同一把尺子），
    /// 域事件广播据此把同宿主订阅插件去重为一次投递；manifest 缺席按独占
    /// 语义回退（不抛错——广播是 fire-and-forget 面，缺席成员由 send 侧留痕）。
    fn host_key_of(&self, plugin_id: &str) -> String {
        match self.loader.get_manifest(plugin_id) {
            Some(manifest) => self.resolve_host_key(&manifest),
            None => plugin_id.to_string(),
        }
    }

    /// 强制卸载插件（覆盖 trait 默认实现）。
    /// 转发到 force_unload_impl：kill sidecar + 清缓存，下次调用自动 respawn 加载新代码。
    async fn force_unload(&self, plugin_id: &str) -> Result<(), PluginError> {
        self.force_unload_impl(plugin_id).await
    }

    /// 成员粒度热重载（覆盖 trait 默认实现，合宿多组扩展的姊妹能力）。
    ///
    /// 见 [`Self::reload_group_member`]：仅对「已装箱到组宿主且有存活进程」
    /// 的成员生效；独占插件返回 Err（独占本就无连坐面，调用方回退
    /// [`Self::force_unload`] 杀单进程即等价粒度）。
    async fn reload_member(&self, plugin_id: &str) -> Result<(), PluginError> {
        self.reload_group_member(plugin_id).await
    }

    async fn unload_member(&self, plugin_id: &str) -> Result<(), PluginError> {
        self.unload_group_member(plugin_id).await
    }
    /// 成员在场保证（域事件唤醒原语，ADR 2026-10-28 D5）。
    async fn ensure_member_present(&self, plugin_id: &str) -> Result<(), PluginError> {
        self.ensure_group_member_present(plugin_id).await
    }

    /// 重新扫描插件目录（覆盖 trait 默认实现）。
    ///
    /// 从 AGENTOS_PLUGINS_DIR 递归收集含 plugin.json 的目录的父目录作为 roots
    /// （对齐 main 启动期的 discover_plugin_roots 逻辑，支持 tools/<plugin>/ 嵌套），
    /// 再传给 loader.discover。幂等：cache.clear + 重插，不杀已 spawn 的进程。
    /// 返回全量 manifest，由调用方对比已知集合找新增。
    async fn discover_new_plugins(&self) -> Result<Vec<PluginManifest>, PluginError> {
        let roots = self.collect_plugin_roots();
        let root_refs: Vec<&str> = roots.iter().map(|s| s.as_str()).collect();
        self.loader.discover(&root_refs).await
    }

    /// G2：拉取插件实际上报的工具清单（`tools/list` 原始 JSON）。
    ///
    /// 仅 sidecar 支持（native/wasm 暂无 describe 通道，返回不支持错误——api 层
    /// 对这两类跳过校验，待 describe 机制落地后补全）。
    /// 语义：spawn/复用 MCP 连接 → `tools/list`；**本次是新 spawn 的连接则校验后
    /// 回收（kill + 移除缓存）**——安装期校验不破坏懒加载（复用中的连接不回收）。
    async fn list_plugin_tools(&self, plugin_id: &str) -> Result<serde_json::Value, PluginError> {
        let loaded = self.loader.load(plugin_id).await?;
        let manifest = &loaded.manifest;
        if manifest.host_type != HostType::Sidecar {
            return Err(PluginError {
                message: format!(
                    "list_plugin_tools 暂不支持 host_type={:?}（native/wasm 的 describe 通道待 G2 后续落地）",
                    manifest.host_type
                ),
                code: None,
                source: None,
            });
        }
        // 新 spawn 判定：校验前缓存里没有该插件所在宿主的存活连接（本次会 spawn 新进程）
        let host_key_probe = self.resolve_host_key(manifest);
        let was_new = {
            let cached = self.mcp_clients.read().get(&host_key_probe).cloned();
            match cached {
                Some(client) => {
                    let guard = client.read().await;
                    !guard.is_alive().await
                }
                None => true,
            }
        };
        let client = self.get_or_create_mcp_client(manifest).await?;
        let raw = {
            let guard = client.read().await;
            guard.list_tools().await.map_err(|e| PluginError {
                message: format!("tools/list failed for {plugin_id}: {e}"),
                code: None,
                source: None,
            })?
        };
        // 本次新 spawn 的宿主进程：校验完回收（kill + 移除缓存），懒加载语义不被
        // 破坏（宿主粒度——light 探测会 spawn 整组宿主，校验完连同整组回收）。
        // 例外（2026-08-20）：声明了 lifecycle_hooks 的插件 on_load 有副作用
        // （起子进程/绑端口——hindsight-api 占 8420 即实测案例），探测完 kill 会
        // 把初始化成果毁掉且孤儿子进程占端口 → 此类插件不 kill，进程交 idle GC
        // 空闲回收。
        if was_new && manifest.capabilities.lifecycle_hooks.is_empty() {
            // 回收 kill（含写锁获取）限时 2s：并发在飞调用持读锁时无超时写锁
            // 会让本请求无限悬挂。超时 warn 并返回明确错误（缓存条目保留——
            // 宿主是合法已初始化实例，后续调用照常复用，仅本次未回收）。
            let kill_fut = async { client.write().await.kill().await };
            match tokio::time::timeout(Duration::from_secs(2), kill_fut).await {
                Ok(Ok(())) => {}
                Ok(Err(e)) => {
                    tracing::debug!(
                        "G2 verify: best-effort kill of freshly spawned host {} failed (idle GC will reap): {e}",
                        host_key_probe
                    );
                }
                Err(_) => {
                    warn!(
                        "G2 verify: reclaim kill of freshly spawned host {} timed out (2s), returning error",
                        host_key_probe
                    );
                    return Err(PluginError {
                        message: format!(
                            "G2 verify: reclaim kill of freshly spawned host {host_key_probe} timed out (2s) — write lock held by in-flight caller"
                        ),
                        code: Some("HOST_RECLAIM_TIMEOUT".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    });
                }
            }
            self.mcp_clients.write().remove(&host_key_probe);
            // spawn 成员集快照与缓存条目同生命周期：回收即清（下次调用重记）
            self.spawned_members.write().remove(&host_key_probe);
            self.pending_rejoin_members.write().remove(&host_key_probe);
            // Job 守卫随记录释放（kill-on-close 兜底整树，见 host_jobs 字段注释）
            self.release_host_job(&host_key_probe);
        }
        Ok(raw)
    }

    /// 内核停机口（覆盖 trait 默认 no-op）：drain + 逐个 kill 全部缓存 sidecar。
    ///
    /// 见 inherent [`PluginInvokerImpl::shutdown_all`]（0.2 收尾 §3.3a）。
    async fn shutdown_all(&self) {
        PluginInvokerImpl::shutdown_all(self).await
    }

    /// disable 窄口 kill（覆盖 trait 默认 no-op）。
    ///
    /// 见 inherent [`PluginInvokerImpl::kill_sidecar_if_any`]（0.2 收尾 §3.3b）。
    async fn kill_sidecar_if_any(&self, plugin_id: &str) {
        PluginInvokerImpl::kill_sidecar_if_any(self, plugin_id).await
    }

    /// 目标插件所在宿主是否在 spawn/respawn 窗口（覆盖 trait 默认 false）。
    ///
    /// D1 宽限面：HTTP dispatcher 据此区分「插件处理慢」与「基础设施冷启动
    /// 等待」，后者暂停端点超时预算（等待就绪而非 504，宿主重载风暴
    /// 2026-09-25）。记账见 [`PluginInvokerImpl::starting_plugins`]。
    fn is_host_starting(&self, plugin_id: &str) -> bool {
        self.starting_plugins.read().contains(plugin_id)
    }

    /// 宿主盒子快照（覆盖 trait 默认空实现）。
    ///
    /// 见 M3 段 [`PluginInvokerImpl::host_boxes_snapshot_impl`]（api 只读端点
    /// `GET /api/v1/plugins/hosts` 取数面）。
    async fn host_boxes_snapshot(&self) -> HostBoxesSnapshot {
        self.host_boxes_snapshot_impl().await
    }

    /// 请求路径整组并行 respawn（覆盖 trait 默认 no-op）。
    ///
    /// 实现体见 [`PluginInvokerImpl::ensure_hosts_ready_impl`]（host_acquire：
    /// 有界并行拉起 + 就绪等待 + 失败隔离，不动 keep-warm/idle GC 治理）。
    async fn ensure_hosts_ready(&self, plugin_ids: &[String]) {
        self.ensure_hosts_ready_impl(plugin_ids).await
    }
}

impl PluginInvokerImpl {
    /// 确保生命周期钩子时机 native 插件已加载（钩子当前无 native 消费者，显式 no-op）。
    ///
    /// native 插件没有 MCP 通知通道，钩子当前也无 native 侧消费者，本函数
    /// 不传递负载，仅在钩子时机触发一次加载（保持热重载检测一致性）。
    async fn send_hook_via_execute(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
        _hook_name: &str,
        _tags: &Value,
    ) -> Result<(), PluginError> {
        // 钩子负载当前无 native 消费者，不构造；仅保留 config_files 读取失败的
        // Err 传播语义（injected_config 求值即校验）。
        let _config = crate::shared::injected_config(self.loader.as_ref(), manifest).await?;
        if manifest.host_type == HostType::InProcess {
            // 直接 trait 对象模型：生命周期钩子经 PipelinePlugin trait 之外
            // 的独立契约传递（当前 tool_core 无 on_load/on_unload 需求，暂 no-op）。
            // 仅确保插件已加载（保持热重载检测一致性）。
            //
            // B2 后注：PluginCtx 已有同构的「约定字段表达特殊调用」先例——
            // `tool_call_json`（工具调用语义，见 invoke_native_tool）。钩子若将来
            // 需要 native 侧消费，可按同一模式加 `hook_json` 字段经 execute 直调
            // （当前无消费者，保持 no-op）。
            if let Some(loader) = self.native_loader.as_ref() {
                self.load_native(loader, plugin_id, manifest)?;
            }
        }
        Ok(())
    }

    /// 递归收集插件 roots：从 AGENTOS_PLUGINS_DIR **与用户插件根**出发，找所有含
    /// plugin.json 的目录，取其父目录去重作为 discover 的 root_paths（对齐 main 的
    /// discover_plugin_roots）。
    ///
    /// 双根都要递归：`scan_root` 只扫一级，`tools/<name>/` 这类二级嵌套靠本函数补
    /// roots。此前只从内置根出发，用户根里放 `tools/foo/` 布局根本不会被发现
    /// （ADR 2026-09-13 §2.3 缺口二）；用户根因此被迫只支持平铺。
    fn collect_plugin_roots(&self) -> Vec<String> {
        let mut base_paths: Vec<std::path::PathBuf> = Vec::new();
        if let Ok(d) = std::env::var("AGENTOS_PLUGINS_DIR") {
            if !d.trim().is_empty() {
                base_paths.push(std::path::PathBuf::from(d));
            }
        }
        if let Some(user_root) = agentos_core::user_space::user_plugins_dir() {
            if !base_paths.contains(&user_root) {
                base_paths.push(user_root);
            }
        }

        let mut parent_set = std::collections::HashSet::new();
        for base_path in &base_paths {
            // 发现面收集原语单点在 plugin-loader（此前与 main 各持一份拷贝，收敛）。
            for dir in agentos_plugin_loader::collect_plugin_dirs(base_path) {
                if let Some(parent) = dir.parent() {
                    if let Some(s) = parent.to_str() {
                        parent_set.insert(s.to_string());
                    }
                }
            }
        }
        parent_set.into_iter().collect()
    }
}

#[cfg(test)]
mod tests;
