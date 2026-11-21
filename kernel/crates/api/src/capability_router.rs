//! Capability 路由器实现——处理 sidecar 插件反向调用内核能力。
//!
//! 把 sidecar 的 `<capability>.<method>` 反向调用路由到内核实现：
//! - pipeline-executor.{suspend, resume, get_run_status} → 直接操作 runs 表
//!   （审批挂起/恢复与复盘轮询走 StorageBackend；resume_pipeline 额外经
//!   PipelineResumerFn 拉起续跑轮；任务执行统一走 chat.send_message →
//!   PipelineExecutor）——处理器在子模块 [`pipeline_executor`]
//! - event-bus.emit → 广播事件（当前记录日志，前端推送留 P1）
//! - metrics.record → 写入指标聚合器（监控设计 §三 通道2，第 6 个 capability）
//! - service-registry.<域>.<op> → 插件访问内核共享基础设施存储——处理器在
//!   子模块 [`service_registry`]
//! - tool-executor.invoke → 委托内核执行工具插件 sidecar——处理器在子模块
//!   [`tool_executor`]
//! - db-read.query/faces → 内核库读契约服务（执法核心在 db-admin crate
//!   read_faces 模块，本路由持 handler 注入臂）
//!
//! 域处理器子模块（file-length 冻结件同刀拆分，loader.rs 三分先例：分发骨架
//! 留母文件、按职责抽处理器）：[`pipeline_executor`] / [`service_registry`] /
//! [`tool_executor`]。子模块经 `impl KernelCapabilityRouter` 挂方法，私有字段
//! 对后代模块可见。
//!
//! [来源: ROADMAP.md 审批暂停/恢复、复盘调管道的前置地基]
//! [来源: docs/working/重要设计/插件监控与指标机制设计.md §三 通道2]

use std::sync::Arc;

use agentos_core::traits::{CapabilityRegistry, StorageBackend};
use agentos_mcp::{CapabilityRouter, McpError};
use async_trait::async_trait;
use serde_json::{json, Value};
use tracing::warn;

use crate::metrics::{Labels, MetricType, MetricsAggregator};

/// service-registry 能力错误码前缀（基础设施存储调用）。
pub(crate) const ERR_SERVICE_REGISTRY: i64 = -32020;

/// Capability 路由器实现。
pub struct KernelCapabilityRouter {
    /// 指标聚合器（处理 metrics.record 调用，监控设计 §三 通道2）。
    /// None = 不接受插件指标上报（聚合器未启用）。
    metrics: Option<MetricsAggregator>,
    /// 插件调用器（处理 tool-executor.invoke 调用——sidecar tool_core 反向请求
    /// 内核执行 tool 插件 sidecar，如 bash_execute）。None = 不支持工具委托执行。
    invoker: Option<Arc<dyn agentos_core::traits::PluginInvoker>>,
    /// 能力注册表（tool_name → plugin_id 反查,服务于 tool-executor.invoke）。
    registry: Option<Arc<dyn CapabilityRegistry>>,
    /// 会话协调器（处理 event-bus.emit 的流式 chunk 推送）。
    /// None = 不支持流式 chunk 推前端（session 未启用）。
    session: Option<Arc<agentos_session::SessionCoordinator>>,
    /// 内核存储后端（处理 service-registry.* 调用——基础设施下沉内核后插件共享的
    /// execution-records/summaries/memory 存储，M1 落地、M2 接通 capability）。
    /// None = 不支持 service-registry（存储未注入）。
    store: Option<Arc<dyn StorageBackend>>,
    /// SQLite 固有面句柄（runs.metadata 挂起凭据读写）：suspend/resume 接线
    /// `pending_interaction_request_id`（S7 恢复链写侧）。set_run_metadata 是
    /// SqliteStore 固有方法不在 StorageBackend trait 上，trait 对象触及不到。
    /// None = 挂起凭据不落库（resume-by-request 查不到 run，其余语义不变）。
    sqlite: Option<Arc<agentos_engine::SqliteStore>>,
    /// 动态 capability handler 注册表（M2/M4）——插件通过 manifest provides.capabilities
    /// 注册的 namespace 在这里路由。handle() 先查这里，miss 再走下方 match。
    /// None = 不支持插件自注册能力（仅内核内置能力可用）。
    handler_registry: Option<Arc<agentos_mcp::CapabilityHandlerRegistry>>,
    /// 动态工具注册器（G3：registry.register_tool 的执行通道）。
    /// 闭包负责三道闸的后两道——enablement 校验（插件须 Enabled）+
    /// 写入注册表（经 M1 guarded 注册入 scope）+ 持久化（可重建性闸，写 DB）。
    /// None = 动态注册不可用（返回显式错误）。
    dynamic_tool_registrar: Option<DynamicToolRegistrar>,
    /// 域事件广播闭包（DSH hook 翻译：event-bus.emit 的域事件名单事件同步
    /// 广播，使 approval.created 等触达触发器订阅者）。None = 不广播。
    domain_broadcaster: Option<DomainBroadcaster>,
    /// 内核能力契约（定义驱动入口校验）：config/kernel/kernel_capabilities/
    /// *.json 声明的 input_schema（含 pattern 形态）在本路由入口逐条执行——
    /// 派发之前。None = 未装配契约 → 宽泛放行（兼容旧装配/测试）。
    capability_contracts: Option<Arc<Vec<crate::kernel_capabilities::KernelCapabilityContract>>>,
    /// 流式声明查询器（ADR 2026-08-22 流式协议）：(plugin_id) → capabilities.streaming
    /// 声明。None = 未装配 → 声明闸放行（兼容旧装配/测试）。装配后未声明即拒。
    streaming_declaration_lookup: Option<StreamingDeclarationLookupFn>,
    /// 强制注入工具声明并集查询（P1-5 声明化，见 [`ForceIncludeToolsLookupFn`]）。
    force_include_tools_lookup: Option<ForceIncludeToolsLookupFn>,
    /// 工具面遮挡名单查询（BUG-51 注册门禁）：() → 当前处于「验证未完成
    /// （verify_incomplete）/ 验证失败终态（verify_failed）」的插件 id 集合。
    /// ADR 2026-11-12 起**不再遮挡工具面**，消费点改为 `tool-executor.invoke`
    /// 的插件级调用拦截（临时不可用，如实说明插件可能仍可用）。实现方读契约
    /// 账本现值。None = 未装配（不拦截，兼容旧装配/测试）。
    shadowed_plugin_ids_lookup: Option<ShadowedPluginIdsLookupFn>,
    /// 声明↔实现漂移被拒工具查询（ADR 2026-11-12，见
    /// [`RejectedToolNamesLookupFn`]）：工具级调用拦截（保留声明 schema 在面，
    /// 调用显式失败）。None = 未装配（不拦截，兼容旧装配/测试）。
    rejected_tool_names_lookup: Option<RejectedToolNamesLookupFn>,
    /// 工具连续失败告警器：同一工具名在调用侧连续返回
    /// success=false（参数校验失败/执行错误）达到阈值即告警——把"同一工具
    /// 连续 N 次失败"这个信号汇总成一条可操作的告警，避免空转无人察觉。
    tool_failure_tracker:
        Option<std::sync::Arc<dyn crate::tools::ToolFailureTracker + Send + Sync>>,
    /// state 出口声明查询闭包（manifest export_fields 并集，见 routes::ExportFields）。
    /// None = 无插件声明（仅内核基线出口，兼容旧装配/测试）。
    export_fields_lookup: Option<ExportFieldsLookupFn>,
    /// state 持久写面声明查询闭包（manifest `state.writes`，按插件 id 查）。
    /// None = 未装配 → 写面 fail-closed（无声明可放行，仅测试装配态需显式注入）。
    state_writes_lookup: Option<StateWritesLookupFn>,
    /// 管道恢复派发闭包（resume_pipeline 拉起续跑轮，见 PipelineResumerFn）。
    /// None = 恢复派发不可用（resume_pipeline 降级为纯 runs 表簿记，兼容旧装配/测试）。
    pipeline_resumer: Option<PipelineResumerFn>,
    /// 调用路径注册表自愈闭包（BUG-37，见 [`ToolRegistryHealFn`]）。
    /// None = 无自愈，反查 miss 直接 fail-closed（测试装配态）。
    tool_registry_heal: Option<ToolRegistryHealFn>,
    /// db-read 读契约服务 handler（C2 刀1：内核库只读数据面——face 注册表 +
    /// 自由只读 SELECT + 租户内建收窄 + 权限分档；执法核心在 db-admin crate
    /// read_faces 模块）。None = 不注入（非 SQLite driver / 未装配，臂诚实报错）。
    db_read: Option<Arc<agentos_db_admin::DbReadCapabilityHandler>>,
}

/// state 出口声明查询闭包：() → 当前 manifest 集合的 export_fields 并集。
pub type ExportFieldsLookupFn = Arc<dyn Fn() -> ExportFields + Send + Sync>;

/// state 持久写面声明查询闭包：(plugin_id) → 该插件 manifest 的
/// `state.writes` 声明（空 = 未声明/未找到 → 该插件无持久写权限）。
/// 内核只按声明放行，不解释键所属域（ADR 2026-10-02）。
/// 闭包实现方共享 manifests RwLock——watcher 热发现同步可见。
pub type StateWritesLookupFn = Arc<dyn Fn(&str) -> Vec<String> + Send + Sync>;

/// 按调用方身份解析 `state.writes` 声明（合宿宿主键 → 成员并集）。
///
/// 反调 `_plugin_id` 身份 = 进程身份（独占 = manifest.id；合宿 =
/// `group:<group>:<slot>`）——合宿宿主的写入必须归约到成员声明并集，否则
/// 合宿插件全部被判"无持久写权限"（2026-11-04 e2e 实测：workspace_lifecycle
/// 的 `task.ws_meta` 写被 group:light:2 身份拒绝，任务出生即卡 pending）。
///
/// 入参 `entries` = (插件 id, state.writes) 快照；`host_key_of` 提供插件 →
/// 宿主键映射（真实装箱表；闭包内不得再持 manifests 锁）。
pub fn state_writes_for_caller<F>(
    entries: &[(String, Vec<String>)],
    host_key_of: F,
    caller: &str,
) -> Vec<String>
where
    F: Fn(&str) -> String,
{
    if let Some((_, writes)) = entries.iter().find(|(id, _)| id == caller) {
        return writes.clone();
    }
    // 合宿宿主键（group:*）+ 独占宿主键（plugin:*）统一按装箱表归约成员
    entries
        .iter()
        .filter(|(id, _)| host_key_of(id) == caller)
        .flat_map(|(_, writes)| writes.iter().cloned())
        .collect()
}

/// 按调用方身份解析 `capabilities.streaming` 声明（合宿宿主键 → 成员归约）。
///
/// 与 `state_writes_for_caller` 同源问题：反调 `_plugin_id` 是进程身份，
/// 合宿时是 `group:<group>:<slot>`，manifest 里只有成员 id——精确匹配必然
/// miss，声明闸 fail-closed 把该进程的流式事件全部丢弃（2026-11-10 实测：
/// `group:light:1` 的 `plugin_error` 当日 9 次全被拒，用户可见的截断/降级
/// 提醒一条没到前端）。
///
/// 归约口径（进程身份共享，无法再按成员区分事件）：
/// - `conduit` 取**全体成员都声明**——它是"整个进程都是引擎器官"的强语义，
///   混装组（如 light 组同时含 conduit 的 tool_core 与普通 llm_service）
///   不得把整组升格为 conduit，否则事件命名空间要求会被换成内核 `a_` id；
/// - `events` / `part_types` 取并集；任一成员未声明镜头（None = 契约全集
///   均可发射）则归约结果同样不设限；
/// - `persist` 任一声明 true 即 true。
pub fn streaming_declaration_for_caller<F>(
    entries: &[(String, Option<agentos_core::traits::StreamingCapability>)],
    host_key_of: F,
    caller: &str,
) -> Option<agentos_core::traits::StreamingCapability>
where
    F: Fn(&str) -> String,
{
    if let Some((_, decl)) = entries.iter().find(|(id, _)| id == caller) {
        return decl.clone();
    }
    let members: Vec<&agentos_core::traits::StreamingCapability> = entries
        .iter()
        .filter(|(id, _)| host_key_of(id) == caller)
        .filter_map(|(_, decl)| decl.as_ref())
        .collect();
    if members.is_empty() {
        return None;
    }
    Some(agentos_core::traits::StreamingCapability {
        conduit: members.iter().all(|decl| decl.conduit),
        events: merge_streaming_lists(members.iter().map(|decl| decl.events.as_deref())),
        part_types: merge_streaming_lists(members.iter().map(|decl| decl.part_types.as_deref())),
        persist: members.iter().filter_map(|decl| decl.persist).max(),
    })
}

/// 声明镜头并集：任一成员为 None（不设限）即不设限，否则按序去重合并。
fn merge_streaming_lists<'a, I>(lists: I) -> Option<Vec<String>>
where
    I: Iterator<Item = Option<&'a [String]>>,
{
    let mut merged: Vec<String> = Vec::new();
    for list in lists {
        let items = list?;
        for item in items {
            if !merged.iter().any(|existing| existing == item) {
                merged.push(item.clone());
            }
        }
    }
    Some(merged)
}

pub use crate::routes::ExportFields;

/// 管道恢复派发闭包：(pipeline_id, thread_id, user_id, state_overlay) → 拉起一轮续跑。
///
/// 实现方经 `EngineDispatcher::dispatch_user_input` 走与聊天/催促同一条派发链
/// （pending 入队 → RunChain FIFO → process_via_engine → stage_recover_history
/// 快照恢复），空 content + `_skip_user_append` overlay = 不落 user 消息、
/// 纯按快照续跑。`state_overlay` 为调用方的可选状态复位指令（如任务域 resume
/// 链清 task.status 终态键），本闭包**不透明透传**，与 `_skip_user_append` 合并
/// 后随派发生效（内核零任务域知识）。生产装配在 agentos-kernel（dispatcher
/// 构造晚于 router，经 OnceLock 槽位二阶段接线）；None = 恢复派发不可用（仅簿记降级）。
pub type PipelineResumerFn = Arc<
    dyn Fn(
            String,
            String,
            String,
            Option<serde_json::Value>,
        )
            -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), String>> + Send>>
        + Send
        + Sync,
>;

/// 域事件广播闭包：(event_name, tags) → 点对点投递给声明 domain_event 的
/// 启用插件（组件版 broadcast_domain_event_from；观察总线由调用方决定）。
/// tags 键为静态字符串（调用处全部为字面量）。
pub type DomainBroadcaster = Arc<dyn Fn(&str, Vec<(String, serde_json::Value)>) + Send + Sync>;

/// 流式声明查询闭包：(plugin_id) → 该插件的 capabilities.streaming 声明
/// （None = 未声明 → 网关拒绝其流式事件）。
pub type StreamingDeclarationLookupFn =
    Arc<dyn Fn(&str) -> Option<agentos_core::traits::StreamingCapability> + Send + Sync>;

/// 强制注入工具声明查询闭包（P1-5 声明化）：() → 全部插件 manifest
/// `force_include_tools` 声明的并集（tool-surface 过滤时无视 tool_ids 注入）。
/// None/空 = 无声明，零强制注入（fail-closed）。
pub type ForceIncludeToolsLookupFn = Arc<dyn Fn() -> Vec<String> + Send + Sync>;

/// 工具调用拦截名单查询闭包（BUG-51 注册门禁）：() → 当前处于「验证未完成
/// （verify_incomplete）/ 验证失败终态（verify_failed）」的插件 id 集合。
/// ADR 2026-11-12 起不再遮挡工具面（声明 schema 恒进面，prompt 前缀缓存稳定），
/// 消费点改为 `tool-executor.invoke` 插件级调用拦截（临时不可用，如实说明插件
/// 可能仍可用）。实现方读契约账本现值。None = 未装配（不拦截，兼容旧装配/测试）。
pub type ShadowedPluginIdsLookupFn = Arc<dyn Fn() -> Vec<String> + Send + Sync>;

/// 声明↔实现漂移被拒工具查询闭包（ADR 2026-11-12）：() → 账本现值判定为
/// 声明漂移（`g2_consistency ∈ {drift, sanitized}`）且被拒的
/// `(plugin_id, tool_name)` 清单。这些工具的声明 schema 保留在工具面（失败桩），
/// 调用时 `tool-executor.invoke` 显式失败（`TOOL_DECLARATION_MISMATCH`）且不得到达
/// sidecar；复验通过（账本 ok）即自动清空。None = 未装配（不拦截，兼容旧装配/测试）。
pub type RejectedToolNamesLookupFn = Arc<dyn Fn() -> Vec<(String, String)> + Send + Sync>;

/// 工具调用错误码：声明与实现不一致（G2 判定漂移，工具保留声明 schema 在面、
/// 调用显式失败，ADR 2026-11-12）。
pub const ERR_TOOL_DECLARATION_MISMATCH: &str = "TOOL_DECLARATION_MISMATCH";

/// 工具调用错误码：插件校验未完成/校验失败终态（临时不可用，插件可能实际可用）。
pub const ERR_TOOL_TEMPORARILY_UNAVAILABLE: &str = "TOOL_TEMPORARILY_UNAVAILABLE";

/// 调用路径注册表自愈闭包（BUG-37）：(tool_name) → 重注册后的工具描述符。
///
/// tool-executor.invoke 反查 miss 时调用（有界：每次调用至多触发一次）。
/// 实现方按"manifest 在册且插件启用"判定是否重注册（reenable 语义），返回
/// `Some(描述符)` = 自愈成功，本次调用按返回描述符路由；`None` = 无可自愈
/// （manifest 缺席/插件已禁用），调用方保持 fail-closed 报"未注册"。
/// 未装配（字段 None）= 无自愈，反查 miss 直接 fail-closed（测试装配态）。
pub type ToolRegistryHealFn = Arc<
    dyn for<'a> Fn(
            &'a str,
        ) -> std::pin::Pin<
            Box<
                dyn std::future::Future<Output = Option<agentos_core::traits::ToolDescriptor>>
                    + Send
                    + 'a,
            >,
        > + Send
        + Sync,
>;

/// G3：动态工具注册器闭包。
///
/// (plugin_id, ToolDescriptor) → Ok(())。实现方负责 enablement 闸、
/// 注册表写入（M1 guarded + scope 登记）与持久化（可重建性闸）。
pub type DynamicToolRegistrar =
    Arc<dyn Fn(&str, agentos_core::traits::ToolDescriptor) -> Result<(), String> + Send + Sync>;

impl KernelCapabilityRouter {
    /// 兜底：未注册/未实现的 capability.method（logger/config-reader 已作为
    /// 死能力从两端 STANDARD_CAPABILITIES 删除，残余调用会落到这里）。
    fn unhandled(&self, capability: &str, method: &str, params: &Value) -> Result<Value, McpError> {
        // 运行时对账兜底（资源注册收敛 T5.5）：静态闸 + G2 闭包后的漏网情形
        // （动态拼名等）落此处——显式失败信封（不假成功）+ 结构化日志（ns/
        // method/调用方）+ 观测计数（service_unresolved，调用方作 series 归属，
        // 与 metrics.record 同一聚合器；聚合器未启用则只留日志）。
        let caller = params
            .get("_plugin_id")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown");
        warn!(
            target: "agentos-kernel",
            "service_unresolved: capability={capability} method={method} caller={caller}"
        );
        if let Some(aggregator) = self.metrics.as_ref() {
            let labels = Labels::from([
                ("capability".to_string(), capability.to_string()),
                ("method".to_string(), method.to_string()),
            ]);
            aggregator.record(
                caller,
                "service_unresolved",
                MetricType::Counter,
                1.0,
                &labels,
                None,
                None,
            );
        }
        Err(McpError::Protocol {
            message: format!("capability method not implemented: {capability}.{method}"),
        })
    }

    /// event-bus.emit：发事件/通知，流式 chunk 推送的核心出口。
    /// sidecar（如 llm_core）每生成一个 chunk 就 notify 一次 event-bus.emit，
    /// 内核收到后调 session.emit_event 把 chunk 推到前端 WS。
    async fn dispatch_event_bus(
        &self,
        method: &str,
        params: &Value,
    ) -> Option<Result<Value, McpError>> {
        match method {
            "emit" => Some(self.handle_event_bus_emit(params.clone()).await),
            // 域事件发射（ADR 2026-08-28 事件下沉底座）：插件向域事件总线发
            // 事件（观察总线 + domain_event 订阅插件点对点）。G6 信封闸（granted
            // 含 "event-bus"）由 handle() 单点覆盖。
            "emit_domain" => Some(self.handle_event_bus_emit_domain(params.clone()).await),
            _ => None,
        }
    }

    /// pipeline-state：state 聚合读面（GAP-2 CONDITION 求值上下文）与持久
    /// 写面（update 按调用方 manifest state.writes 声明放行，ADR 2026-10-02）。
    async fn dispatch_pipeline_state(
        &self,
        method: &str,
        params: &Value,
    ) -> Option<Result<Value, McpError>> {
        match method {
            "list" => Some(self.handle_pipeline_state_list().await),
            "update" => Some(self.handle_pipeline_state_update(params.clone()).await),
            _ => None,
        }
    }

    /// transient：插件中间态内存寄存器（ADR 2026-08-27，方案 §2.3）。
    /// 中间态不落库、引擎内存持有、用完即清；插件 manifest 声明
    /// capabilities.transient_state 即接入（复用声明→校验闸）。
    async fn dispatch_transient(
        &self,
        method: &str,
        params: &Value,
    ) -> Option<Result<Value, McpError>> {
        match method {
            "set" => Some(self.handle_transient_set(params.clone()).await),
            "get" => Some(self.handle_transient_get(params.clone()).await),
            "list" => Some(self.handle_transient_list(params.clone()).await),
            "clear" => Some(self.handle_transient_clear(params.clone()).await),
            _ => None,
        }
    }

    /// 创建带指标聚合器的路由器（生产用，启用 metrics.record 反向调用）。
    pub fn with_metrics(metrics: MetricsAggregator) -> Self {
        Self {
            metrics: Some(metrics),
            invoker: None,
            registry: None,
            session: None,
            store: None,
            sqlite: None,
            handler_registry: None,
            dynamic_tool_registrar: None,
            domain_broadcaster: None,
            capability_contracts: None,
            streaming_declaration_lookup: None,
            force_include_tools_lookup: None,
            shadowed_plugin_ids_lookup: None,
            rejected_tool_names_lookup: None,
            tool_failure_tracker: None,
            export_fields_lookup: None,
            state_writes_lookup: None,
            pipeline_resumer: None,
            tool_registry_heal: None,
            db_read: None,
        }
    }

    /// 注入内核能力契约（启用定义驱动入口校验：契约声明了 (namespace, method)
    /// 才按 input_schema 逐条校验——required/类型/pattern 形态/enum/闭包参数面；
    /// 未声明即宽泛放行）。生产装配自 config/kernel/kernel_capabilities/*.json。
    pub fn with_capability_contracts(
        mut self,
        contracts: Arc<Vec<crate::kernel_capabilities::KernelCapabilityContract>>,
    ) -> Self {
        self.capability_contracts = Some(contracts);
        self
    }

    /// 注入域事件广播闭包（启用 event-bus 域事件名单同步广播：approval.created 等）。
    pub fn with_domain_broadcaster(mut self, broadcaster: DomainBroadcaster) -> Self {
        self.domain_broadcaster = Some(broadcaster);
        self
    }

    /// 注入管道恢复派发闭包（启用 resume_pipeline 的续跑拉起，见 PipelineResumerFn）。
    pub fn with_pipeline_resumer(mut self, resumer: PipelineResumerFn) -> Self {
        self.pipeline_resumer = Some(resumer);
        self
    }

    /// 注入流式声明查询器（启用 event-bus.emit 的 streaming 声明闸）：
    /// (plugin_id) → 该插件的 capabilities.streaming 声明（None = 未声明）。
    /// 未声明即拒其流式事件（fail-closed：能力面一律声明即接入，声明缺位不放行）。
    /// 闭包实现方共享 manifests RwLock——watcher 热发现同步可见。
    pub fn with_streaming_declaration_lookup(
        mut self,
        lookup: StreamingDeclarationLookupFn,
    ) -> Self {
        self.streaming_declaration_lookup = Some(lookup);
        self
    }

    /// 注入强制注入工具声明查询（P1-5 声明化）。
    pub fn with_force_include_tools_lookup(mut self, lookup: ForceIncludeToolsLookupFn) -> Self {
        self.force_include_tools_lookup = Some(lookup);
        self
    }

    /// 注入工具调用拦截名单查询（BUG-51 注册门禁，插件级临时不可用）。
    pub fn with_shadowed_plugin_ids_lookup(mut self, lookup: ShadowedPluginIdsLookupFn) -> Self {
        self.shadowed_plugin_ids_lookup = Some(lookup);
        self
    }

    /// 注入声明↔实现漂移被拒工具查询（ADR 2026-11-12，工具级调用显式失败）。
    pub fn with_rejected_tool_names_lookup(mut self, lookup: RejectedToolNamesLookupFn) -> Self {
        self.rejected_tool_names_lookup = Some(lookup);
        self
    }

    /// 注入工具失败追踪器（启用「同一工具连续失败」告警）。
    pub fn with_tool_failure_tracker(
        mut self,
        tracker: std::sync::Arc<dyn crate::tools::ToolFailureTracker + Send + Sync>,
    ) -> Self {
        self.tool_failure_tracker = Some(tracker);
        self
    }

    /// 注入 state 出口声明查询器（pipeline-state.list 摘要与 HTTP
    /// /pipelines/state 同源：manifest export_fields 并集，热重载同步可见）。
    pub fn with_export_fields_lookup(mut self, lookup: ExportFieldsLookupFn) -> Self {
        self.export_fields_lookup = Some(lookup);
        self
    }

    /// 注入 state 持久写面声明查询器（ADR 2026-10-02 声明制）：
    /// `pipeline-state.update` 按调用方 `_plugin_id` 查 manifest `state.writes`，
    /// 未声明即拒绝。None = 未装配 → 写面 fail-closed。
    pub fn with_state_writes_lookup(mut self, lookup: StateWritesLookupFn) -> Self {
        self.state_writes_lookup = Some(lookup);
        self
    }

    /// 注入插件调用器（启用 tool-executor.invoke 反向调用）。
    pub fn with_invoker(mut self, invoker: Arc<dyn agentos_core::traits::PluginInvoker>) -> Self {
        self.invoker = Some(invoker);
        self
    }

    /// 注入动态工具注册器（G3：启用 registry.register_tool 运行时注册）。
    pub fn with_dynamic_tool_registrar(mut self, registrar: DynamicToolRegistrar) -> Self {
        self.dynamic_tool_registrar = Some(registrar);
        self
    }

    /// 注入能力注册表（tool_name → plugin_id 反查）。
    pub fn with_registry(mut self, registry: Arc<dyn CapabilityRegistry>) -> Self {
        self.registry = Some(registry);
        self
    }

    /// 注入调用路径注册表自愈闭包（BUG-37，见 [`ToolRegistryHealFn`]）。
    pub fn with_tool_registry_heal(mut self, heal: ToolRegistryHealFn) -> Self {
        self.tool_registry_heal = Some(heal);
        self
    }

    /// 注入 db-read 读契约服务 handler（C2 刀1：启用 `("db-read", m)` 内置臂）。
    pub fn with_db_read(mut self, handler: Arc<agentos_db_admin::DbReadCapabilityHandler>) -> Self {
        self.db_read = Some(handler);
        self
    }

    /// 注入会话协调器（启用 event-bus.emit 流式 chunk 推前端）。
    pub fn with_session(mut self, session: Arc<agentos_session::SessionCoordinator>) -> Self {
        self.session = Some(session);
        self
    }

    /// 注入内核存储后端（启用 service-registry.* 反向调用——基础设施下沉内核，
    /// 插件经此 capability 访问 execution-records/summaries/memory 共享存储）。
    pub fn with_store(mut self, store: Arc<dyn StorageBackend>) -> Self {
        self.store = Some(store);
        self
    }

    /// 注入 SQLite 固有面句柄（启用 suspend/resume 挂起凭据读写，见 struct 注释）。
    pub fn with_sqlite(mut self, db: Arc<agentos_engine::SqliteStore>) -> Self {
        self.sqlite = Some(db);
        self
    }

    /// 注入动态 capability handler 注册表（M2/M4）。
    ///
    /// 启用后，handle() 先查注册表（插件自注册的 namespace 在这里路由），
    /// miss 再走内置 match（内核自带能力）。这让交互等插件
    /// 声明的 namespace 不需修改内置 match 即可被路由。
    pub fn with_handler_registry(
        mut self,
        registry: Arc<agentos_mcp::CapabilityHandlerRegistry>,
    ) -> Self {
        self.handler_registry = Some(registry);
        self
    }
}

#[async_trait]
impl CapabilityRouter for KernelCapabilityRouter {
    async fn handle(
        &self,
        capability: &str,
        method: &str,
        params: Value,
    ) -> Result<Value, McpError> {
        // 定义驱动入口校验：契约声明了 (capability, method)
        // 时按 input_schema 逐条执行（required/类型/pattern 形态/enum/闭包参数面），
        // 未声明即宽泛放行——定义详细到什么程度，就校验到什么程度。这是"相同
        // 类型、不同语义"错误（如 thread 坐标填进 pipeline_id 槽）的暴露点：
        // 形状全合法的互填在形态档（pattern）抓红。
        if let Some(contracts) = self.capability_contracts.as_ref() {
            crate::kernel_capabilities::validate_params(contracts, capability, method, &params)?;
        }
        // 先查动态 handler 注册表（M2/M4：插件自注册的 namespace 在这里路由）。
        // 命中则委托，不再走下方内置 match。这让交互等插件能力
        // 不需修改内置 match 即可被路由。
        if let Some(reg) = &self.handler_registry {
            if reg.has_namespace(capability) {
                return reg.route(capability, method, params).await;
            }
        }
        // 内置能力分派：按 namespace 归组（多臂域抽 dispatch_*，见各函数）；
        // 未列出的组合仍落兜底臂（unhandled）。
        match (capability, method) {
            ("pipeline-executor", m) => self
                .dispatch_pipeline_executor(m, &params)
                .await
                .unwrap_or_else(|| self.unhandled(capability, method, &params)),
            // ── session.create：sidecar 兜底新建承载管道（日程触发器挂载，
            // ADR 2026-11-03-pim-trigger-anchor）。身份 = 执行上下文注入，
            // 无注入 fail-fast（同 resume_pipeline 刀3 契约）。
            ("session", "create") => self.handle_session_create(&params).await,
            ("event-bus", m) => self
                .dispatch_event_bus(m, &params)
                .await
                .unwrap_or_else(|| self.unhandled(capability, method, &params)),

            // ── frontend.emit：插件 → 内核 → 前端一次性事件出口（ADR §3.5）。
            // 承载低频观测/进度事件（cost_update/tool_progress）；
            // 分工：event-bus 承载 llm_core 流式 chunk，frontend.emit 承载一次性观测。──
            ("frontend", "emit") => self.handle_frontend_emit(params).await,

            // ── registry.register_tool：运行时动态注册（G3，VS Code 双层模型的
            // 动态层）。使能/写入/持久化都在装配闭包里（作用域收窄到注册插件自身）。──
            ("registry", "register_tool") => self.handle_registry_register_tool(params).await,

            // ── metrics.record：插件上报指标（监控设计 §三 通道2）。plugin_id 作
            // series 归属（不在 metric name 加前缀）；_plugin_id 为 invoker 注入
            // 的信任锚点。 ──
            ("metrics", "record") => self.handle_metrics_record(params).await,

            // ── tool-executor.invoke：tool_core 反向委托内核执行工具插件 sidecar ──
            ("tool-executor", "invoke") => self.handle_tool_executor_invoke(params).await,

            // ── tool-surface.schemas：LLM 工具面过滤服务（K10 执行时契约）──
            // agent 配置解析（读 yaml 取 tool_ids）归插件域：agent_config_load 写
            // state.tool_ids，tool_schema 管道插件转发本调用。内核零 agent 配置
            // 知识，只持"注册表过滤"这一纯执行面。
            ("tool-surface", "schemas") => self.handle_tool_surface_schemas(&params).await,

            // ── service-registry：基础设施下沉内核后的共享存储（M2）──────────
            // method 形如 `<域>.<op>`（如 execution-records.list / memory.create）。
            // 经此 capability，插件统一访问内核 execution_records / pipeline_run_summaries
            // / memory 三表（M1 落地），不再各自持有进程内 ServiceProvider/store。
            ("service-registry", m) => self.handle_service_registry(m, params).await,

            // ── db-read：内核库读契约服务（C2 刀1）─────────────────────────
            // 数据面声明式注册（config/kernel/read_faces.yaml）+ 自由只读 SELECT
            // + 租户内建收窄 + 权限分档（tenant | global-admin）。执法核心在
            // db-admin crate read_faces 模块，身份走 _authorization 原头转发 +
            // 内核复权（db-admin capability.rs 同款先例）；G6 授权闸已由本
            // handle() 单点先行。
            ("db-read", m) => {
                let Some(handler) = self.db_read.as_ref() else {
                    return Err(McpError::Protocol {
                        message: "db-read disabled: read faces not injected（非 SQLite driver \
                                  或 boot 未装配）"
                            .to_string(),
                    });
                };
                handler.call(m, params).await
            }

            ("pipeline-state", m) => self
                .dispatch_pipeline_state(m, &params)
                .await
                .unwrap_or_else(|| self.unhandled(capability, method, &params)),

            ("transient", m) => self
                .dispatch_transient(m, &params)
                .await
                .unwrap_or_else(|| self.unhandled(capability, method, &params)),

            // ── tenant-context：多租户上下文查询（F-TENANT-B-KERNEL）──
            // Python 侧 `plugins/shared/tenant_data.py` 经此能力取当前租户决定数据根；
            // 无活跃 task_local 时回退 "default"（与 Python 侧回退一致，永不报错）。
            ("tenant-context", "get") => {
                let ctx = agentos_tenant::current_or_default("default");
                Ok(json!({
                    "tenant_id": ctx.tenant_id,
                    "session_id": ctx.session_id,
                }))
            }

            (cap, m) => self.unhandled(cap, m, &params),
        }
    }

    /// 合并内置 STANDARD_CAPABILITIES + 动态注册表的 namespace。
    ///
    /// reader loop 据此做白名单解析，initialize 据此声明给 sidecar。
    /// 覆盖默认实现（只返回内置常量），让插件自注册的 namespace 自动可见。
    fn known_namespaces(&self) -> Vec<String> {
        let mut ns: Vec<String> = agentos_mcp::STANDARD_CAPABILITIES
            .iter()
            .map(|s| s.to_string())
            .collect();
        if let Some(reg) = &self.handler_registry {
            for n in reg.namespaces() {
                if !ns.contains(&n) {
                    ns.push(n);
                }
            }
        }
        ns
    }
}

impl KernelCapabilityRouter {
    /// tool-surface.schemas：按 tool_ids 白名单过滤能力注册表，返回 OpenAI
    /// function-calling schema 列表 + 工具输出契约。调用方（tool_schema 管道
    /// 插件）把结果写进 state["tool_schemas"]/["tool_output_contracts"]。
    ///
    /// 过滤语义：
    /// - 白名单命中 + 强制注入集合（全部插件 manifest `force_include_tools`
    ///   声明并集，无视白名单）注入；空白名单 = agent 声明零工具，仅强制
    ///   注入集合返回；白名单条目等于插件 id 时该插件全部工具注入（一行
    ///   接入配法）；
    /// - **G2 判定/观测失败不遮挡工具面**（ADR 2026-11-12）：`sanitized`/`drift`
    ///   工具的声明 schema 保留在面（失败桩），`verify_incomplete`/
    ///   `verify_failed` 插件同理——工具数组不因校验态变化，prompt 前缀缓存
    ///   稳定；调用时经 `tool-executor.invoke` 显式失败（错误码/原因/修复指引），
    ///   复验通过（账本 ok）自动放行；
    /// - input_schema 非 object 的工具不注入（LLM 严格校验 parameters 是
    ///   object；注册路径已对缺 schema 工具按 {} 补注册，本过滤只拦注册后
    ///   被改写成非 object 的极端形态）；
    /// - 契约表不过滤：凡声明 output_schema/render 的工具都产生条目
    ///   （tool_core 按 tool_name 查表校验，与 schema 面解耦）。
    async fn handle_tool_surface_schemas(&self, params: &Value) -> Result<Value, McpError> {
        let Some(registry) = self.registry.as_ref() else {
            return Err(McpError::Protocol {
                message: "tool-surface.schemas disabled: capability registry not injected"
                    .to_string(),
            });
        };
        let wanted: std::collections::HashSet<String> = params
            .get("tool_ids")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|t| t.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        // P1-5 声明化：强制注入集合 = 全部插件 manifest force_include_tools
        // 声明并集（各插件声明各自需强制注入的工具名）；未装配/空声明 = 零
        // 强制注入（fail-closed，内核零框架工具名单）。
        let force_include: std::collections::HashSet<String> = self
            .force_include_tools_lookup
            .as_ref()
            .map(|lookup| lookup().into_iter().collect())
            .unwrap_or_default();
        // 注意：G2 校验态（sanitized/drift/verify_incomplete/verify_failed）
        // 不参与工具面过滤（ADR 2026-11-12）——声明 schema 恒进面（失败桩），
        // 调用拦截在 tool-executor.invoke（账本现值）。
        let all_tools = registry.list_tools();
        // F-min 属地替换（设计文档 §53）：调用方可传 workspace（任务工作空间
        // 原样路径）；同名多实例工具按属地解析为宿主实例描述符（工具名不变，
        // 仅 schema/description 面换成属地实例的）。单实例/未传 workspace =
        // 主实例原样，既有行为逐字节一致。
        let workspace = params.get("workspace").and_then(|v| v.as_str());
        let all_tools: Vec<_> = match workspace {
            Some(ws) => all_tools
                .into_iter()
                .map(|t| {
                    let resolved = registry.resolve_tool_instance(&t.name, Some(ws));
                    match resolved {
                        Some(inst) if inst.plugin_id != t.plugin_id => inst,
                        _ => t,
                    }
                })
                .collect(),
            None => all_tools,
        };
        // tool_ids 条目等于插件 id → 该插件全部工具入面（一行接入的 agent 侧
        // 配法：白名单写插件名即透出其全部工具，动态导入的多工具 MCP 免逐个
        // 罗列；与精确工具名并存，同名时取并集）。
        let wanted_plugin_ids: std::collections::HashSet<&str> =
            wanted.iter().map(|s| s.as_str()).collect();
        let schemas: Vec<Value> = all_tools
            .iter()
            .filter(|t| {
                wanted.contains(&t.name)
                    || wanted_plugin_ids.contains(t.plugin_id.as_str())
                    || force_include.contains(&t.name)
            })
            .filter(|t| t.input_schema.is_object())
            .map(|t| {
                json!({
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    }
                })
            })
            .collect();
        let contracts: serde_json::Map<String, Value> = all_tools
            .iter()
            .filter(|t| t.output_schema.is_some() || t.render.is_some())
            .map(|t| {
                (
                    t.name.clone(),
                    json!({
                        "schema": t.output_schema.clone().unwrap_or(Value::Null),
                        "render": t.render.clone().unwrap_or(Value::Null),
                        "isolation_capable": t.isolation_capable.unwrap_or(false),
                    }),
                )
            })
            .collect();
        Ok(json!({
            "schemas": schemas,
            "contracts": Value::Object(contracts),
        }))
    }

    // ── 内置能力域处理器：handle() 的 match 按域一行委托到这里 ─────────────
    // （service-registry / tool-executor / pipeline-executor 三域处理器已按职责
    //   平移至子模块 service_registry.rs / tool_executor.rs / pipeline_executor.rs。）

    /// event-bus.emit：插件事件统一入口。流式契约网关执法后按族路由：
    /// 流式族 → 工具族 → 交互族 → 其余透传（approval.created 同步广播域总线）。
    async fn handle_event_bus_emit(&self, params: Value) -> Result<Value, McpError> {
        let event_name = params
            .get("event")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown");
        let payload = params
            .get("payload")
            .cloned()
            .unwrap_or(serde_json::Value::Null);
        tracing::debug!(target: "capability:event-bus", event = %event_name, "收到 event-bus.emit");

        let plugin_id = params.get("_plugin_id").and_then(|v| v.as_str());
        if let Some(rejection) = self.streaming_gateway_rejection(event_name, &payload, plugin_id) {
            return Ok(rejection);
        }
        // 分族路由：命中即终答（Some），None 落下一族。
        if let Some(resp) = self.try_route_stream_family(event_name, &payload).await {
            return resp;
        }
        if let Some(resp) = self.try_route_tool_family(event_name, &payload).await {
            return resp;
        }
        if let Some(resp) = self
            .try_route_interaction_family(event_name, &payload)
            .await
        {
            return resp;
        }
        self.forward_unmatched_event(event_name, &payload).await
    }

    /// event-bus.emit_domain：插件向域事件总线发事件。投递双腿：观察总线 +
    /// 声明 domain_event hook 的启用插件点对点（broadcast_domain_event_from）。
    /// 域事件本就不达前端 WS（前端走 stream_* 协议与 event-bus.emit），此处
    /// 不做 WS 单播。domain_broadcaster 未装配 = 显式错误（fail-closed，
    /// 测试装配点须显式声明无广播）。
    async fn handle_event_bus_emit_domain(&self, params: Value) -> Result<Value, McpError> {
        let event_name = params
            .get("event")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if event_name.is_empty() {
            return Err(McpError::Protocol {
                message: "event-bus.emit_domain 缺 event 参数".to_string(),
            });
        }
        let empty = serde_json::Map::new();
        let tags_obj = params
            .get("tags")
            .and_then(|v| v.as_object())
            .unwrap_or(&empty);
        let tags: Vec<(String, Value)> = tags_obj
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        match &self.domain_broadcaster {
            Some(broadcast) => broadcast(&event_name, tags),
            None => {
                return Err(McpError::Protocol {
                    message: "event-bus.emit_domain 不可用：domain_broadcaster 未装配".to_string(),
                })
            }
        }
        Ok(json!({"status": "emitted", "event": event_name}))
    }

    /// 流式契约网关（ADR 2026-08-22，streaming.json 单一真值源）：契约事件统一
    /// 执法——schema + message_id 命名空间 + thread_id 投递键。fail-closed：
    /// 非法即丢弃 + 告警（返回 Some(dropped)——插件发射侧拿到 dropped 状态而非
    /// RPC 错误）；非契约事件不归本闸（None 放行进分族）。
    fn streaming_gateway_rejection(
        &self,
        event_name: &str,
        payload: &Value,
        plugin_id: Option<&str>,
    ) -> Option<Value> {
        let contracts = self.capability_contracts.as_ref()?;
        // 契约族未声明该事件 = 不归本闸（None 放行进分族）。
        crate::kernel_capabilities::find_spec(contracts, "streaming", event_name)?;
        // 声明闸（ADR 2026-08-22）：插件须声明 capabilities.streaming 才能发射
        // 流式事件；未声明即拒（fail-closed）。引擎管道器官（manifest 声明
        // capabilities.streaming.conduit 的回环插件）豁免发射闸——它们携带内核
        // 签发的 a_ id，命名空间执法按声明走 kernel owner。
        if let Some(pid) = plugin_id {
            let declared = self
                .streaming_declaration_lookup
                .as_ref()
                .and_then(|lookup| lookup(pid));
            let is_conduit = declared.as_ref().is_some_and(|d| d.conduit);
            if !is_conduit {
                let Some(decl) = declared else {
                    tracing::warn!(
                        target: "capability:event-bus",
                        plugin = pid,
                        event = %event_name,
                        "流式事件被拒：插件未声明 capabilities.streaming"
                    );
                    return Some(json!({
                        "status": "dropped",
                        "reason": "capabilities.streaming not declared",
                        "event": event_name,
                    }));
                };
                // 声明了 events 清单 → 事件必须在其内（G2 声明↔实现对照）
                if let Some(allowed) = decl.events.as_deref() {
                    if !allowed.iter().any(|e| e == event_name) {
                        tracing::warn!(
                            target: "capability:event-bus",
                            plugin = pid,
                            event = %event_name,
                            "流式事件被拒：不在 capabilities.streaming.events 声明内"
                        );
                        return Some(json!({
                            "status": "dropped",
                            "reason": "event not in declared events",
                            "event": event_name,
                        }));
                    }
                }
            }
        }
        // P1-2 声明化：器官判定从 manifest capabilities.streaming.conduit 读
        //（lookup 未装配时按无声明处理，fail-closed）。
        let conduit_check = |pid: &str| {
            self.streaming_declaration_lookup
                .as_ref()
                .and_then(|lookup| lookup(pid))
                .is_some_and(|d| d.conduit)
        };
        if let Err(reason) = crate::kernel_capabilities::validate_streaming_event(
            contracts,
            event_name,
            payload,
            plugin_id,
            &conduit_check,
        ) {
            tracing::warn!(
                target: "capability:event-bus",
                event = %event_name,
                plugin = plugin_id.unwrap_or("<kernel>"),
                reason = %reason,
                "流式契约网关拒绝（fail-closed 丢弃）"
            );
            return Some(json!({
                "status": "dropped",
                "reason": reason,
                "event": event_name,
            }));
        }
        None
    }

    /// 流式事件族（名单单源 = config/kernel/ws_events.yaml 的 stream_family 节，
    /// ADR 2026-10-10：改名单改文件，勿在此登记）。命中返回 Some(响应)，
    /// 未命中返回 None 落下一族。0.1 协议信封必须含 pipeline_id +
    /// message_id，否则前端 resolvePipelineId/extractMessageId 失败丢弃；
    /// thinking 系列驱动前端思考卡片渲染。
    async fn try_route_stream_family(
        &self,
        event_name: &str,
        payload: &Value,
    ) -> Option<Result<Value, McpError>> {
        if !crate::ws_events::is_stream_family(event_name) {
            return None;
        }
        if let Some(session) = &self.session {
            let thread_id = payload
                .get("thread_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            // content 键为 0.1 协议固定字段（llm_core _consumer 只发 content）
            let content = payload
                .get("content")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let pipeline_id = payload
                .get("pipeline_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let message_id = payload
                .get("message_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            // thinking_start/thinking_end 无 content，跳过空 content 校验
            let needs_content = event_name == "stream_chunk" || event_name == "thinking_chunk";
            // 信封契约（0.1 协议）：thread_id + pipeline_id + message_id
            // 缺一即丢弃——前端 resolvePipelineId/extractMessageId 解析
            // 不出会丢事件，与其发一个"合法但坏"的信封不如显式拒绝。
            if thread_id.is_empty() || pipeline_id.is_empty() || message_id.is_empty() {
                tracing::warn!(
                    target: "capability:event-bus",
                    event = %event_name,
                    thread = %thread_id,
                    pipeline = %pipeline_id,
                    message = %message_id,
                    "流式事件信封不完整（缺 thread_id/pipeline_id/message_id），丢弃"
                );
                return Some(Ok(
                    json!({"status": "dropped", "reason": "incomplete envelope", "event": event_name}),
                ));
            }
            if !thread_id.is_empty() && (!needs_content || !content.is_empty()) {
                let mut data = serde_json::json!({
                    "pipeline_id": pipeline_id,
                    "message_id": message_id,
                    "_threadId": thread_id,
                });
                if !content.is_empty() {
                    data["content"] = serde_json::Value::String(content.to_string());
                }
                let delivered = session.emit_event(thread_id, event_name, data).await;
                // 流式拦截点顺带累积（一次 IPC 两个动作：推 WS + 写寄存器）。
                // chunk 累积（ADR 2026-08-27 §2.4）+ 钩子分发挂桩（P1 不实现
                // 钩子协议面，装载表恒空短路，零开销；协议面归管道步骤服务化
                // 提案 §3.6，留注引用）。注意：热路径查表优先于分配，勿在此
                // 追加大对象构造。
                self.accumulate_stream_interception(event_name, payload, thread_id, message_id);
                return Some(Ok(json!({
                    "status": if delivered { "emitted" } else { "dropped" },
                    "event": event_name,
                })));
            } else {
                // 诊断：事件被丢弃（stream_chunk content 空）
                // debug 级避免流式噪声；仅 stop/thinking_end 等无 content 事件偶发。
                tracing::debug!(
                    target: "capability:event-bus",
                    event = %event_name,
                    thread = %thread_id,
                    has_content = !content.is_empty(),
                    "流式事件被丢弃（content 空）"
                );
                return Some(Ok(
                    json!({"status": "dropped", "reason": "empty content", "event": event_name}),
                ));
            }
        }
        // session 未接线：引擎照常执行，无前端播报（视为 emitted）
        Some(Ok(json!({"status": "emitted", "event": event_name})))
    }

    /// 流式拦截点：chunk 累积 + 钩子分发挂桩（ADR 2026-08-27 §2.4）。
    ///
    /// 在 try_route_stream_family 的 emit 成功路径旁调用（信封校验已通过）：
    /// - `stream_chunk`/`thinking_chunk`：`accumulate_chunk` 节流累积
    ///   （每 N 个 chunk 或 500ms 一次写寄存器，计数与落盘都在寄存器模块内部）；
    /// - `stream_end`/`stream_error`：最终形态已落 message_slots，清 chunk 键；
    /// - 钩子分发挂桩：P1 不实现钩子协议面（装载表编译归管道步骤服务化提案
    ///   §3.6，compiler.rs hooks 解析为另一步），此处只保留发射点语义——
    ///   先查钩子装载表、空集直接短路。装载表恒空 → 零开销返回。
    ///
    /// [来源: docs/working/插件中间态统一管理方案_20260827.md §2.4/§3.6]
    /// [来源: docs/decisions/2026-08-27-transient-state-register.md 决策3]
    fn accumulate_stream_interception(
        &self,
        event_name: &str,
        payload: &Value,
        thread_id: &str,
        message_id: &str,
    ) {
        let pipeline_id = payload
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if pipeline_id.is_empty() {
            return;
        }
        // 钩子装载表查询（挂桩）：恒空集，短路返回——热路径零开销。
        // 钩子协议面（配置 schema/装载表编译/最小作用域装载）归管道步骤
        // 服务化提案 §3.6，本方案步骤 3 只保证挂点存在。
        //
        // 2026-08-27 后注：**同步边界事件**（step_start/step_end）已由引擎侧
        // 落地——pipeline_loop.rs execute_step_impl 进入/收尾经 compiled.step_hooks
        // 查表直派（step 级 + body 级两档，_pipe_hook 约定字段，terminate 决策
        // 置 ended）。本拦截点对应的是**跨层流事件**（stream_chunk 等）发射面，
        // 跨层流接线 P1 明确不做（提案 §3.6 诚实边界：step 归属需
        // message→step 绑定链，待 transient-register 消息上下文登记补齐），
        // 保持挂桩不动——此分支为流事件协议面就绪后的落点预留。
        if !hook_dispatch_stub(pipeline_id, thread_id, event_name).is_empty() {
            // 装载表非空时按 B 区登记的 message→step 归属过滤作用域后派发
            // （B 区登记由引擎 execute_step 派发时写入）；P1 恒空，此分支
            // 为协议面就绪后的落点预留。
            let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
            let _step_id = agentos_engine::global_registry().resolve_step_of(
                &tenant_id,
                pipeline_id,
                message_id,
            );
        }
        match event_name {
            "stream_chunk" | "thinking_chunk" => {
                let content = payload
                    .get("content")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                // thinking_chunk 的增量也走 content 字段（契约同构），
                // 统一累积进 thinking 快照
                let (text, thinking) = if event_name == "thinking_chunk" {
                    ("", content)
                } else {
                    (content, "")
                };
                let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
                agentos_engine::global_registry().accumulate_chunk(
                    &tenant_id,
                    pipeline_id,
                    message_id,
                    text,
                    thinking,
                );
            }
            "stream_end" | "stream_error" => {
                let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
                agentos_engine::global_registry().clear_chunk(&tenant_id, pipeline_id, message_id);
            }
            _ => {}
        }
    }

    /// 工具事件族（tool_start/tool_result/tool_multimedia_result）：tool_core
    /// 执行工具前后上报，前端渲染工具卡片。与流式族不同：工具事件携带结构化字段
    /// （call_id/tool_name/args/result/success/duration_ms 等），整体透传 payload
    /// 进 data，补 pipeline_id/message_id/_threadId 路由键即可（前端 handler
    /// 双取顶层/data）。命中返回 Some(响应)，未命中 None。
    async fn try_route_tool_family(
        &self,
        event_name: &str,
        payload: &Value,
    ) -> Option<Result<Value, McpError>> {
        let tool_events = ["tool_start", "tool_result", "tool_multimedia_result"];
        if !tool_events.contains(&event_name) {
            return None;
        }
        let mut delivered = false;
        if let Some(session) = &self.session {
            let thread_id = payload
                .get("thread_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let pipeline_id = payload
                .get("pipeline_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let message_id = payload
                .get("message_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            // 前端 handleToolStart/handleToolResult 硬门控：
            // pipeline_id（resolvePipelineId）、message_id（extractMessageId）、
            // call_id、tool_name。缺一即丢弃，故无 content 弱校验，但要 thread_id 非空。
            if !thread_id.is_empty() {
                // 整体透传 payload（含 call_id/tool_name/args/result 等业务字段），
                // 确保路由键齐全。
                let mut data = payload.clone();
                if let Some(obj) = data.as_object_mut() {
                    obj.insert(
                        "pipeline_id".to_string(),
                        serde_json::Value::String(pipeline_id.to_string()),
                    );
                    obj.insert(
                        "message_id".to_string(),
                        serde_json::Value::String(message_id.to_string()),
                    );
                    obj.insert(
                        "_threadId".to_string(),
                        serde_json::Value::String(thread_id.to_string()),
                    );
                }
                delivered = session.emit_event(thread_id, event_name, data).await;
            }
        }
        Some(Ok(json!({
            "status": if delivered { "emitted" } else { "dropped" },
            "event": event_name,
        })))
    }

    /// 交互事件族（interaction_request/cancelled/timeout/conversation_start 等）：
    /// 交互插件上报，前端 useInteractionHandler 渲染交互卡片/
    /// 全局浮层表单。与工具族一致整体透传 payload，补 _threadId 路由键。
    async fn try_route_interaction_family(
        &self,
        event_name: &str,
        payload: &Value,
    ) -> Option<Result<Value, McpError>> {
        if !event_name.starts_with("interaction_") {
            return None;
        }
        let mut delivered = false;
        if let Some(session) = &self.session {
            let thread_id = payload
                .get("thread_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if !thread_id.is_empty() {
                let mut data = payload.clone();
                if let Some(obj) = data.as_object_mut() {
                    obj.insert(
                        "_threadId".to_string(),
                        serde_json::Value::String(thread_id.to_string()),
                    );
                }
                delivered = session.emit_event(thread_id, event_name, data).await;
            } else {
                tracing::debug!(
                    target: "capability:event-bus",
                    event = %event_name,
                    "交互事件被丢弃（thread_id 空）"
                );
            }
        }
        Some(Ok(json!({
            "status": if delivered { "emitted" } else { "dropped" },
            "event": event_name,
        })))
    }

    /// 兜底透传：插件自定义事件（如审批类、widget 交互反馈）经此直达前端——
    /// "插件经内核推"通道的通用出口，前端按 type 订阅。payload 整体透传 +
    /// 补 pipeline_id/message_id/_threadId 路由键（与 frontend.emit 同构）。
    async fn forward_unmatched_event(
        &self,
        event_name: &str,
        payload: &Value,
    ) -> Result<Value, McpError> {
        // 域事件镜像（名单单源 = config/kernel/ws_events.yaml 的 domain_mirrors 节，
        // ADR 2026-10-10）：名单内事件（现仅 approval.created）除前端 WS 透传外，
        // 同步广播进域事件总线——生命周期订阅者（触发器/域事件插件）也能感知
        // 审批请求，插件只发一次 event-bus，内核单点分流。二期升级路径：改
        // plugin.json domain_events 声明驱动注册表分流（ADR 影响节登记）。
        if crate::ws_events::is_domain_mirror(event_name) {
            if let Some(broadcaster) = &self.domain_broadcaster {
                // tag 提取同 derive_run_terminal_events 的 v() 法：字段缺失以 Null 占位。
                let tag = |k: &'static str| {
                    (
                        k.to_string(),
                        payload.get(k).cloned().unwrap_or(serde_json::Value::Null),
                    )
                };
                broadcaster(
                    event_name,
                    vec![
                        tag("request_id"),
                        tag("run_id"),
                        tag("pipeline_id"),
                        tag("thread_id"),
                    ],
                );
            }
        }

        if let Some(session) = &self.session {
            let thread_id = payload
                .get("thread_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if !thread_id.is_empty() {
                let pipeline_id = payload
                    .get("pipeline_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let message_id = payload
                    .get("message_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let mut data = payload.clone();
                if let Some(obj) = data.as_object_mut() {
                    obj.insert(
                        "pipeline_id".to_string(),
                        serde_json::Value::String(pipeline_id.to_string()),
                    );
                    obj.insert(
                        "message_id".to_string(),
                        serde_json::Value::String(message_id.to_string()),
                    );
                    obj.insert(
                        "_threadId".to_string(),
                        serde_json::Value::String(thread_id.to_string()),
                    );
                }
                let delivered = session.emit_event(thread_id, event_name, data).await;
                return Ok(json!({
                    "status": if delivered { "emitted" } else { "dropped" },
                    "event": event_name,
                }));
            } else {
                tracing::debug!(
                    target: "capability:event-bus",
                    event = %event_name,
                    "透传事件被丢弃（thread_id 空）"
                );
            }
        }
        Ok(
            json!({"status": "dropped", "reason": "no session or empty thread_id", "event": event_name}),
        )
    }

    /// frontend.emit：一次性观测事件出口（ADR §3.5）。payload 整体透传 +
    /// 补 pipeline_id/message_id/_threadId 路由键，经 session.emit_event 推前端
    /// （{type,data,sequence} 信封）。v1 无 per-plugin 限流；源头自带节流
    /// （track 每轮一次、bash 进度 1KB/2s 阈值），如需限流可改挂 FrontendEventBus
    /// （session/src/event_bus.rs）。
    async fn handle_frontend_emit(&self, params: Value) -> Result<Value, McpError> {
        let event_name = params
            .get("event")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown");
        let payload = params
            .get("payload")
            .cloned()
            .unwrap_or(serde_json::Value::Null);
        // thread_id 双取：payload 内优先，params 顶层兜底（scope 语义）
        let thread_id = payload
            .get("thread_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .or_else(|| params.get("thread_id").and_then(|v| v.as_str()))
            .unwrap_or("");
        if thread_id.is_empty() {
            tracing::debug!(
                target: "capability:frontend",
                event = %event_name,
                "frontend.emit 被丢弃（thread_id 空）"
            );
            return Ok(json!({"status": "dropped", "event": event_name}));
        }
        let mut delivered = false;
        if let Some(session) = &self.session {
            let pipeline_id = payload
                .get("pipeline_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let message_id = payload
                .get("message_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let mut data = payload.clone();
            if let Some(obj) = data.as_object_mut() {
                obj.insert(
                    "pipeline_id".to_string(),
                    serde_json::Value::String(pipeline_id.to_string()),
                );
                obj.insert(
                    "message_id".to_string(),
                    serde_json::Value::String(message_id.to_string()),
                );
                obj.insert(
                    "_threadId".to_string(),
                    serde_json::Value::String(thread_id.to_string()),
                );
            }
            delivered = session.emit_event(thread_id, event_name, data).await;
        }
        Ok(json!({
            "status": if delivered { "emitted" } else { "dropped" },
            "event": event_name,
        }))
    }

    /// registry.register_tool：运行时动态注册工具（G3）。参数解析后委托
    /// registrar——enablement 闸 + 写入注册表 + 持久化都在装配闭包里，router
    /// 保持与具体注册表/存储类型解耦。
    async fn handle_registry_register_tool(&self, params: Value) -> Result<Value, McpError> {
        use agentos_core::traits::ToolDescriptor;
        use agentos_core::types::{ToolCategory, ToolSource};
        let plugin_id = params
            .get("_plugin_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if plugin_id.is_empty() {
            return Err(McpError::Protocol {
                message: "registry.register_tool 需要 _plugin_id（插件上下文调用）".to_string(),
            });
        }
        let name = params
            .get("name")
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpError::Protocol {
                message: "registry.register_tool 缺少 name 参数".to_string(),
            })?
            .to_string();
        let descriptor = ToolDescriptor {
            plugin_id: plugin_id.clone(),
            name,
            description: params
                .get("description")
                .and_then(|v| v.as_str())
                .unwrap_or("dynamically registered tool")
                .to_string(),
            input_schema: params
                .get("input_schema")
                .cloned()
                .unwrap_or(serde_json::json!({})),
            output_schema: params.get("output_schema").cloned(),
            category: params
                .get("category")
                .and_then(|v| v.as_str())
                .and_then(|c| match c {
                    "file" => Some(ToolCategory::File),
                    "filesystem" => Some(ToolCategory::FileSystem),
                    "search" => Some(ToolCategory::Search),
                    "web" => Some(ToolCategory::Web),
                    "memory" => Some(ToolCategory::Memory),
                    "task" => Some(ToolCategory::Task),
                    "execution" => Some(ToolCategory::Execution),
                    "analysis" => Some(ToolCategory::Analysis),
                    "monitoring" => Some(ToolCategory::Monitoring),
                    "system" | "" => Some(ToolCategory::System),
                    _ => None,
                })
                .unwrap_or(ToolCategory::System),
            // 动态注册一律记 Dynamic 来源（与 manifest 静态注册区分）。
            source: ToolSource::Dynamic,
            ui: params.get("ui").cloned(),
            render: params.get("render").cloned(),
            isolation_capable: params.get("isolation_capable").and_then(|v| v.as_bool()),
        };
        let registrar = self
            .dynamic_tool_registrar
            .as_ref()
            .ok_or_else(|| McpError::Protocol {
                message: "registry.register_tool 未装配动态注册器（G3 未启用）".to_string(),
            })?;
        registrar(&plugin_id, descriptor).map_err(|reason| McpError::Protocol {
            message: format!("registry.register_tool 拒绝: {reason}"),
        })?;
        // 注册成功即 schema.tools 变化——best-effort 经 session 广播 widget_event
        // {schema, changed}（前端 resync.ts 消费，与 resync_required 同一重载链）。
        // 失败静默（观察层不拖垮注册主流程）。
        if let Some(session) = &self.session {
            let _ = session
                .broadcast_widget(
                    "schema",
                    "changed",
                    json!({ "plugin_id": plugin_id, "source": "dynamic_register" }),
                    "kernel",
                )
                .await;
        }
        Ok(json!({"status": "registered", "plugin_id": plugin_id}))
    }

    /// metrics.record：插件指标写入聚合器。labels 限长 + 禁特殊字符
    /// （监控设计 §十），非法 metric_type 显式拒绝。
    async fn handle_metrics_record(&self, params: Value) -> Result<Value, McpError> {
        let agg = self.metrics.as_ref().ok_or_else(|| McpError::Protocol {
            message: "metrics aggregator not enabled".to_string(),
        })?;
        let plugin_id = params
            .get("_plugin_id")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown")
            .to_string();
        let name =
            params
                .get("name")
                .and_then(|v| v.as_str())
                .ok_or_else(|| McpError::Protocol {
                    message: "metrics.record 缺少 name 参数".to_string(),
                })?;
        let value = params
            .get("value")
            .and_then(|v| v.as_f64())
            .ok_or_else(|| McpError::Protocol {
                message: "metrics.record 缺少或非法 value 参数".to_string(),
            })?;
        let metric_type = match params
            .get("metric_type")
            .and_then(|v| v.as_str())
            .unwrap_or("counter")
        {
            "counter" => MetricType::Counter,
            "gauge" => MetricType::Gauge,
            "histogram" => MetricType::Histogram,
            other => {
                return Err(McpError::Protocol {
                    message: format!("unknown metric_type: {other}"),
                });
            }
        };
        let labels = parse_labels_safe(params.get("labels"))?;
        let unit = params
            .get("unit")
            .and_then(|v| v.as_str())
            .map(str::to_string);
        let help = params
            .get("help")
            .and_then(|v| v.as_str())
            .map(str::to_string);
        agg.record(
            &plugin_id,
            name,
            metric_type,
            value,
            &labels,
            unit.as_deref(),
            help.as_deref(),
        );
        Ok(json!({"status": "recorded", "plugin_id": plugin_id, "name": name}))
    }

    /// pipeline-state.list：当前租户全部管道的 state 摘要行（扁平点号键，task.*/
    /// lineage.* 经 STATE_SUMMARY_KEYS 出口）——内存 registry 热行为主，DB 冷数据
    /// 兜底；messages 全文不出口。CONDITION 求值上下文数据源（GAP-2）。
    async fn handle_pipeline_state_list(&self) -> Result<Value, McpError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let export = self
            .export_fields_lookup
            .as_ref()
            .map(|f| f())
            .unwrap_or_default();
        let registry = agentos_session::pipeline_state_registry::global_registry();
        let mut rows: Vec<Value> = Vec::new();
        let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut memory_row_idx: Vec<(String, usize)> = Vec::new();
        for listing in registry.list() {
            if listing.tenant_id != tenant_id {
                continue;
            }
            let Some(entry) = registry.get(&listing.tenant_id, &listing.pipeline_id) else {
                continue;
            };
            seen.insert(listing.pipeline_id.clone());
            let mut row = {
                let e = entry.read();
                crate::routes::summarize_state(&e.state, &export)
            };
            if let Some(obj) = row.as_object_mut() {
                obj.insert("pipeline_id".to_string(), json!(listing.pipeline_id));
                obj.insert("thread_id".to_string(), json!(listing.thread_id));
                if !listing.agent_id.is_empty() {
                    obj.insert("agent_id".to_string(), json!(listing.agent_id));
                }
                obj.insert("source".to_string(), json!("memory"));
            }
            memory_row_idx.push((listing.pipeline_id.clone(), rows.len()));
            rows.push(row);
        }
        // DB 任务域镜像补齐：pipeline-state.update 的热路径在管道尚未注册内存
        // registry 时（workspace init 早于引擎注册的窗口）只落 pipeline_state 表，
        // 内存聚合行会缺 task.ws_meta 等任务域键 → 运行中评估的 worktree 合并
        // 门控按"ws_meta 读取失败"误判。按 export 白名单从表行补齐内存行缺失键
        // （内存已有键不覆盖——运行时态权威）。
        if let Some(store) = &self.store {
            for (pid, idx) in &memory_row_idx {
                let fields = match store.load_pipeline_state(pid, &tenant_id).await {
                    Ok(f) => f,
                    Err(e) => {
                        // 补齐失败必须可观测：内存行将缺任务域键，下游 worktree
                        // 合并门控会把缺键当"ws_meta 读取失败"误判——warn 留痕 +
                        // 聚合器计数（metrics-admin 读面可查），继续处理其余行。
                        warn!(pipeline_id = %pid, error = %e,
                              "pipeline_state DB 补齐失败，内存行将缺任务域键");
                        if let Some(agg) = &self.metrics {
                            agg.record(
                                "kernel",
                                "pipeline_state.backfill_errors_total",
                                MetricType::Counter,
                                1.0,
                                &Labels::new(),
                                None,
                                None,
                            );
                        }
                        continue;
                    }
                };
                let Some(obj) = rows.get_mut(*idx).and_then(|r| r.as_object_mut()) else {
                    continue;
                };
                for (k, v) in fields {
                    if export.contains(&k) && !obj.contains_key(&k) {
                        obj.insert(k, v);
                    }
                }
            }
        }
        // DB 冷数据兜底（与 /api/v1/pipelines/state 同策略）：registry 未覆盖的
        // 冷管道（重启后未再轮）从「最新 checkpoint + pipeline_state 表最新标量
        // 覆盖」组装扁平行——任务完成态（task.status=completed 落 pipeline_state
        // 表）在内存 registry 丢失后仍可见，task_manage 冷任务查询不再
        // "任务不存在"/过期 pending。枚举失败降级为仅内存行（读面不崩）。
        if let Some(store) = &self.store {
            let owners = match store.list_state_pipeline_ids(&tenant_id).await {
                Ok(o) => o,
                Err(e) => {
                    warn!(
                        target: "capability_router",
                        tenant = %tenant_id, error = %e,
                        "pipeline-state.list DB 冷枚举失败（降级：仅内存行）"
                    );
                    Vec::new()
                }
            };
            for (pid, th) in owners {
                if seen.contains(&pid) {
                    continue;
                }
                // 运行状态 run_status 是表行簿记键，随 merge_cold_state 合并行
                // 如实出口；投影缺失即如实缺键——读面不补写不伪造（谁终态谁
                // 随写，runs 退役收尾 ADR 2026-10-08，旧 runs 表权威 overlay
                // 已废：fill-if-absent 会把簿记缺键 fabrication 成 running）。
                let Some(merged) = crate::routes::merge_cold_state(store, &pid, &tenant_id).await
                else {
                    continue; // checkpoint 与表行双空的真孤儿不出口（表行可独立兜底）
                };
                let mut row = crate::routes::summarize_state(&merged, &export);
                if let Some(obj) = row.as_object_mut() {
                    obj.insert("pipeline_id".to_string(), json!(pid));
                    obj.insert("thread_id".to_string(), json!(th));
                    obj.insert("source".to_string(), json!("checkpoint"));
                }
                rows.push(row);
            }
        }
        Ok(Value::Array(rows))
    }

    /// pipeline-state.update：持久写面（ADR 2026-10-02 声明制）。按调用方
    /// `_plugin_id`（invoker 注入的信任锚点）查 manifest `state.writes`，逐键
    /// 校验精确键/`前缀.*` 通配；未声明即拒绝——内核只执行"声明才可写"机制，
    /// 不解释键所属域（task.* 前缀特判已退役）。① 内存 registry 热路径 +
    /// ② pipeline_state 表冷路径双落点，state 单一真值。
    async fn handle_pipeline_state_update(&self, params: Value) -> Result<Value, McpError> {
        let pipeline_id = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| McpError::Protocol {
                message: "pipeline-state.update 缺少 pipeline_id 参数".to_string(),
            })?;
        let plugin_id = params
            .get("_plugin_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| McpError::Protocol {
                message: "pipeline-state.update 需要 _plugin_id（插件上下文调用）".to_string(),
            })?;
        // 参数面：键缺省与键类型错严格区分（审查 MF1）——fields 存在必须为
        // 对象、delete_keys 存在必须为数组，类型不符显式拒绝；静默降级成缺省
        // 会把删除/upsert 意图整段吞掉还返回 updated（假成功）。null 仍是合法
        // 值不解释为删除；缺省空 = 纯 upsert 调用，行为完全不变。
        let fields = match params.get("fields") {
            None => None,
            Some(v) => {
                let obj = v.as_object().ok_or_else(|| McpError::Protocol {
                    message: "pipeline-state.update fields 必须是对象".to_string(),
                })?;
                Some(obj.clone())
            }
        };
        let delete_keys = match params.get("delete_keys") {
            None => Vec::new(),
            Some(v) => v
                .as_array()
                .ok_or_else(|| McpError::Protocol {
                    message: "pipeline-state.update delete_keys 必须是数组".to_string(),
                })?
                .iter()
                .map(|k| {
                    k.as_str()
                        .filter(|s| !s.is_empty())
                        .map(String::from)
                        .ok_or_else(|| McpError::Protocol {
                            message: "pipeline-state.update delete_keys 元素必须是非空字符串"
                                .to_string(),
                        })
                })
                .collect::<Result<Vec<_>, _>>()?,
        };
        if fields.is_none() && delete_keys.is_empty() {
            return Err(McpError::Protocol {
                message: "pipeline-state.update 需要 fields 对象或 delete_keys 数组".to_string(),
            });
        }
        let fields = fields.unwrap_or_default();
        // 声明制校验：只认调用方 manifest `state.writes`（精确键/前缀通配）。
        // 未装配查询器或插件无声明 = 无持久写权限（fail-closed）。
        let declared = self
            .state_writes_lookup
            .as_ref()
            .map(|lookup| lookup(plugin_id))
            .unwrap_or_default();
        let writable = ExportFields::from_declarations(&declared);
        for k in fields.keys() {
            if !writable.contains(k) {
                return Err(McpError::Protocol {
                    message: format!(
                        "pipeline-state.update 键未在 manifest state.writes 声明（plugin={plugin_id}），收到: {k}"
                    ),
                });
            }
        }
        // delete_keys 同规声明制（与 upsert 共用 state.writes 校验面）：未声明
        // 整批拒绝（先校验后执行，无部分删除半态）。
        for k in &delete_keys {
            if !writable.contains(k) {
                return Err(McpError::Protocol {
                    message: format!(
                        "pipeline-state.update delete_keys 键未在 manifest state.writes 声明（plugin={plugin_id}），收到: {k}"
                    ),
                });
            }
        }
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        // ① 冷路径先行（B6 写序倒置：DB 权威、内存是缓存）——批量事务 upsert
        //    （upsert_state_fields）全成才 commit，任一失败整体回滚且内存不留
        //    新值，消灭「内存新值 + DB 半套」的分代态（首键失败重启后旧值复活）。
        if let Some(store) = &self.store {
            if !fields.is_empty() {
                store
                    .upsert_state_fields(pipeline_id, &tenant_id, &fields)
                    .await
                    .map_err(|e| McpError::Protocol {
                        message: format!("pipeline-state.update 持久化失败: {e}"),
                    })?;
            }
            // 键真删（同一 DB 权威落点）：幂等，不存在的键跳过；返回实际删除
            // 的键，not_found = 请求键 − 实际删除键。
            let mut deleted: Vec<String> = Vec::new();
            if !delete_keys.is_empty() {
                deleted = store
                    .delete_state_fields(pipeline_id, &tenant_id, &delete_keys)
                    .await
                    .map_err(|e| McpError::Protocol {
                        message: format!("pipeline-state.update 删除持久化失败: {e}"),
                    })?;
            }
            // ② 热路径：内存 registry（未注册则跳过——冷管道由表行兜底）。
            // DB 批量成功后才写内存；store 缺席（无持久化装配）时内存即唯一落点。
            // 删除必须同步 obj.remove——内存不删，下次任意写操作会把内存旧值
            // 刷回 DB，被删键复活（双落点设计的特征缺陷）。按请求键全集清理
            //（remove 不存在的键是 no-op）：DB 无行而内存有的陈旧缓存键
            //（如装配前写入的）也一并清掉，不留复活种子（缓存一致性维护，
            // 不改变 DB 权威口径的 deleted/not_found 上报）。
            let registry = agentos_session::pipeline_state_registry::global_registry();
            if let Some(entry) = registry.get(&tenant_id, pipeline_id) {
                let mut e = entry.write();
                if let Some(obj) = e.state.as_object_mut() {
                    for (k, v) in &fields {
                        obj.insert(k.clone(), v.clone());
                    }
                    for k in &delete_keys {
                        obj.remove(k);
                    }
                }
            }
            if !delete_keys.is_empty() {
                // ③ 审计 trace（update 单一路径约束：所有 state 变更经 update 留痕）。
                // 引擎侧 journal diff 只覆盖插件 state_updates 的 merge 路径，capability
                // 反调 update 不经过它——删除在此显式补 TraceEntry（发起方/目标管道/
                // 删除键全量可追溯）。仅记实际删除的键（请求了但不存在的不留痕）。
                if !deleted.is_empty() {
                    store
                        .append_trace(agentos_core::types::TraceEntry {
                            trace_id: format!("t_{}", uuid::Uuid::new_v4().simple()),
                            pipeline_id: pipeline_id.to_string(),
                            seq: 0,
                            plugin_id: plugin_id.to_string(),
                            patch_type: agentos_core::types::PatchType::StateUpdate,
                            patch_data: json!({
                                "action": "delete_keys",
                                "deleted_keys": deleted,
                            }),
                            created_at: chrono::Utc::now().to_rfc3339(),
                        })
                        .await
                        .map_err(|e| McpError::Protocol {
                            message: format!("pipeline-state.update 删除审计落迹失败: {e}"),
                        })?;
                }
                // ④ 响应单一出口（审查 MF2）：含 delete_keys 的调用恒定返回完整
                // 契约体——deleted=DB 实际删除集、not_found=请求集−实删集；
                // 全 not_found（deleted 为空）不退化成 {status, pipeline_id}。
                let not_found: Vec<String> = delete_keys
                    .iter()
                    .filter(|k| !deleted.contains(k))
                    .cloned()
                    .collect();
                if !not_found.is_empty() {
                    warn!(
                        pipeline_id = %pipeline_id,
                        plugin = %plugin_id,
                        keys = ?not_found,
                        "pipeline-state.update delete_keys 含不存在的键（幂等跳过）"
                    );
                }
                return Ok(json!({
                    "status": "updated",
                    "pipeline_id": pipeline_id,
                    "deleted": deleted,
                    "not_found": not_found,
                }));
            }
        } else {
            // store 缺席：内存即唯一落点——fields 照常插入、delete_keys 照常移除
            //（与原「仅内存」行为完全对齐，纯 upsert 调用零变化）。
            let registry = agentos_session::pipeline_state_registry::global_registry();
            let mut removed: Vec<String> = Vec::new();
            if let Some(entry) = registry.get(&tenant_id, pipeline_id) {
                let mut e = entry.write();
                if let Some(obj) = e.state.as_object_mut() {
                    for (k, v) in &fields {
                        obj.insert(k.clone(), v.clone());
                    }
                    // 诚实上报（审查 MF2 分支③）：deleted = 内存实际删除集
                    //（remove 返回 Some），不存在/未命中的键如实进 not_found
                    //——不得把不存在的键谎报为 deleted。
                    removed = delete_keys
                        .iter()
                        .filter(|k| obj.remove(k.as_str()).is_some())
                        .cloned()
                        .collect();
                }
            }
            if !delete_keys.is_empty() {
                let not_found: Vec<String> = delete_keys
                    .iter()
                    .filter(|k| !removed.contains(k))
                    .cloned()
                    .collect();
                warn!(
                    pipeline_id = %pipeline_id,
                    plugin = %plugin_id,
                    keys = ?not_found,
                    "pipeline-state.update delete_keys 无持久化装配，仅内存删除（无 DB 真删与持久审计）"
                );
                return Ok(json!({
                    "status": "updated",
                    "pipeline_id": pipeline_id,
                    "deleted": removed,
                    "not_found": not_found,
                }));
            }
        }
        Ok(json!({"status": "updated", "pipeline_id": pipeline_id}))
    }

    // ── transient.*：插件中间态内存寄存器四方法（ADR 2026-08-27 方案 §2.3）──
    // 中间态不落库：A 键值区（per-(tenant,pipeline) → per-key）+ B 运行上下文
    // 登记区（message→step 归属，供钩子最小作用域装载）；tenant 取
    // agentos_tenant::current_or_default（与 tenant-context/pipeline-state 同源）。
    // 节流是写面内建语义：同 key 连续写合并为"最新值 + 最后更新时间"。

    /// transient.set：写/覆盖一个中间态键（value 任意 JSON）。
    async fn handle_transient_set(&self, params: Value) -> Result<Value, McpError> {
        let pipeline_id = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| McpError::Protocol {
                message: "transient.set 缺少 pipeline_id 参数".to_string(),
            })?;
        let key = params
            .get("key")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| McpError::Protocol {
                message: "transient.set 缺少 key 参数".to_string(),
            })?;
        let value = params.get("value").cloned().unwrap_or(Value::Null);
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        agentos_engine::global_registry().set(&tenant_id, pipeline_id, key, value);
        Ok(json!({"status": "set", "pipeline_id": pipeline_id, "key": key}))
    }

    /// transient.get：引擎步骤/条件读一个中间态键（None → null）。
    async fn handle_transient_get(&self, params: Value) -> Result<Value, McpError> {
        let pipeline_id = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| McpError::Protocol {
                message: "transient.get 缺少 pipeline_id 参数".to_string(),
            })?;
        let key = params
            .get("key")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| McpError::Protocol {
                message: "transient.get 缺少 key 参数".to_string(),
            })?;
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let value = agentos_engine::global_registry().get(&tenant_id, pipeline_id, key);
        let found = value.is_some();
        Ok(json!({
            "pipeline_id": pipeline_id,
            "key": key,
            "value": value.unwrap_or(Value::Null),
            "found": found,
        }))
    }

    /// transient.list：枚举该管道全部存活中间态（前端 F5 刷新恢复数据源）。
    async fn handle_transient_list(&self, params: Value) -> Result<Value, McpError> {
        let pipeline_id = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| McpError::Protocol {
                message: "transient.list 缺少 pipeline_id 参数".to_string(),
            })?;
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let rows: Vec<Value> = agentos_engine::global_registry()
            .list(&tenant_id, pipeline_id)
            .into_iter()
            .map(|(key, value, updated_at)| {
                json!({
                    "key": key,
                    "value": value,
                    "updated_at_ms": updated_at.elapsed().as_millis(),
                })
            })
            .collect();
        Ok(json!({"pipeline_id": pipeline_id, "transient_states": rows}))
    }

    /// transient.clear：显式清一个中间态键（stream_end 处理器清 chunk 键）。
    async fn handle_transient_clear(&self, params: Value) -> Result<Value, McpError> {
        let pipeline_id = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| McpError::Protocol {
                message: "transient.clear 缺少 pipeline_id 参数".to_string(),
            })?;
        let key = params
            .get("key")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| McpError::Protocol {
                message: "transient.clear 缺少 key 参数".to_string(),
            })?;
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        agentos_engine::global_registry().clear(&tenant_id, pipeline_id, key);
        Ok(json!({"status": "cleared", "pipeline_id": pipeline_id, "key": key}))
    }
}

/// 钩子装载表查询挂桩（ADR 2026-08-27 决策7 / 方案 §2.4 协作点）。
///
/// 本挂桩对应**跨层流事件**（stream_chunk 等）发射面，P1 明确不做跨层流
/// 接线（管道步骤服务化提案 §3.6 诚实边界：step 归属需 message→step 绑定
/// 链，待 transient-register 消息上下文登记补齐），恒返回空集——流式
/// 热路径上的钩子零开销由"先查表再分发"保证，空集直接短路。
/// **同步边界事件**（step_start/step_end）已由引擎侧落地
/// （pipeline_loop.rs execute_step_impl 边界分发，step 级 + body 级两档），
/// 不经本挂桩。
/// 返回：命中当前 (pipeline, thread, event) 作用域的钩子 id 列表（空 = 无钩子）。
fn hook_dispatch_stub(_pipeline_id: &str, _thread_id: &str, _event_name: &str) -> Vec<String> {
    Vec::new()
}

/// 解析 labels 并做注入防护（监控设计 §十）。
/// - 限制：最多 20 个 label，每个 key/value 最长 256 字符。
/// - 禁止 value 含换行/双引号（Prometheus 导出安全）。
fn parse_labels_safe(raw: Option<&Value>) -> Result<Labels, McpError> {
    let mut out = Labels::new();
    let Some(obj) = raw.and_then(|v| v.as_object()) else {
        return Ok(out);
    };
    if obj.len() > 20 {
        return Err(McpError::Protocol {
            message: "too many labels (max 20)".to_string(),
        });
    }
    for (k, v) in obj {
        if k.len() > 256 {
            return Err(McpError::Protocol {
                message: format!("label key too long: {k}"),
            });
        }
        let val = v.as_str().unwrap_or("");
        if val.len() > 256 {
            return Err(McpError::Protocol {
                message: format!("label value too long for key: {k}"),
            });
        }
        // 禁换行/双引号（Prometheus exposition 安全，监控设计 §十）
        if val.contains('\n') || val.contains('"') {
            return Err(McpError::Protocol {
                message: format!(
                    "label value contains forbidden char (newline/dquote) for key: {k}"
                ),
            });
        }
        out.insert(k.clone(), val.to_string());
    }
    Ok(out)
}

// 域处理器子模块（按职责拆分，见模块文档）：
// - pipeline_executor：runs 表状态簿记（审批挂起/恢复/轮询/任务删除）
// - service_registry：基础设施共享存储读面（messages/traces/pipeline-runs）
// - tool_executor：工具插件 sidecar 委托执行（治理三段 + 失败告警）
mod pipeline_executor;
mod service_registry;
mod session;
mod tool_executor;

#[cfg(test)]
mod tests;

/// 工具连续失败告警集成：tool-executor.invoke 结果 success=false
/// 计入同工具连续失败计数，达到阈值产出告警——把「同一工具 100% 校验失败」
/// 从流水日志淹没中捞出来。
#[cfg(test)]
mod tool_failure_alert_tests {
    use super::*;
    use crate::tools::{ToolFailureAlert, ToolFailureTracker, FAILURE_ALERT_THRESHOLD};
    use std::sync::Mutex;

    /// 返回 success=false 的 invoker（模拟参数校验失败）。
    struct FailingInvoker;
    #[async_trait::async_trait]
    impl agentos_core::traits::PluginInvoker for FailingInvoker {
        async fn invoke_pipeline_plugin<'a>(
            &self,
            _plugin_id: &str,
            _ctx: &agentos_core::types::PluginContext<'a>,
        ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
            Err(agentos_core::types::PluginError {
                message: "n/a".into(),
                code: None,
                source: None,
            })
        }
        async fn send_lifecycle_hook(
            &self,
            _plugin_id: &str,
            _hook: agentos_core::traits::LifecycleHook,
            _ctx: &agentos_core::traits::HookContext,
        ) -> Result<(), agentos_core::types::PluginError> {
            Ok(())
        }
        async fn invoke_tool(
            &self,
            _plugin_id: &str,
            _tool_name: &str,
            _inputs: &Value,
        ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError>
        {
            Ok(agentos_core::types::ToolExecutionResult {
                success: false,
                data: json!({}),
                error: Some("mode Field required (pydantic validation)".into()),
                duration_ms: Some(1),
                metadata: None,
            })
        }
    }

    /// 记录告警产出的追踪器（代替 tracing 断言，测路由逻辑）。
    struct RecordingTracker {
        alerts: Arc<Mutex<Vec<ToolFailureAlert>>>,
        records: Arc<Mutex<Vec<(String, bool)>>>,
    }
    impl ToolFailureTracker for RecordingTracker {
        fn record(
            &self,
            tool_name: &str,
            success: bool,
            _sample: &str,
        ) -> Option<ToolFailureAlert> {
            self.records
                .lock()
                .unwrap()
                .push((tool_name.to_string(), success));
            let n = self
                .records
                .lock()
                .unwrap()
                .iter()
                .filter(|(n, s)| n == tool_name && !s)
                .count();
            if n as u32 >= FAILURE_ALERT_THRESHOLD {
                let a = ToolFailureAlert {
                    tool_name: tool_name.to_string(),
                    consecutive: n as u32,
                    error_sample: "mode Field required".to_string(),
                    since_secs: 1,
                };
                self.alerts.lock().unwrap().push(a.clone());
                Some(a)
            } else {
                None
            }
        }
    }

    #[tokio::test]
    async fn consecutive_tool_failures_produce_alert_through_router() {
        let alerts: Arc<Mutex<Vec<ToolFailureAlert>>> = Arc::new(Mutex::new(Vec::new()));
        let records: Arc<Mutex<Vec<(String, bool)>>> = Arc::new(Mutex::new(Vec::new()));
        let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
            .with_invoker(Arc::new(FailingInvoker))
            .with_tool_failure_tracker(Arc::new(RecordingTracker {
                alerts: alerts.clone(),
                records: records.clone(),
            }));
        let params = json!({
            "tool_name": "universal_search",
            "args": {"query": "x"},
            "plugin_id": "omnisearch",
        });
        // 阈值前（4 次）：无告警，record 计数 4
        for _ in 0..(FAILURE_ALERT_THRESHOLD - 1) {
            let resp = router
                .handle("tool-executor", "invoke", params.clone())
                .await
                .unwrap();
            assert_eq!(resp["success"], false, "失败结果按信封返回");
            assert!(
                resp["error"]
                    .as_str()
                    .unwrap_or("")
                    .contains("Field required"),
                "错误详情透传"
            );
        }
        assert!(alerts.lock().unwrap().is_empty(), "阈值前不得告警");
        // 第 5 次：达到阈值 → 告警产出（经 tracker 记录在案）
        let resp = router
            .handle("tool-executor", "invoke", params.clone())
            .await
            .unwrap();
        assert_eq!(resp["success"], false);
        let got = alerts.lock().unwrap().clone();
        assert_eq!(got.len(), 1, "连续失败达到阈值应产出告警");
        assert_eq!(got[0].tool_name, "universal_search");
        assert_eq!(got[0].consecutive, FAILURE_ALERT_THRESHOLD);
        // 冷却/清零语义由 tools::ConsecutiveFailureTracker 单测覆盖（本测试用
        // RecordingTracker 只验证「路由把 success=false 送进 tracker 并透传告警」），
        // 此处补一轮确认记录计数与失败次数一致。
        for _ in 0..3 {
            let _ = router
                .handle("tool-executor", "invoke", params.clone())
                .await
                .unwrap();
        }
        assert_eq!(records.lock().unwrap().len(), 8, "8 次失败全部送进 tracker");
    }
}
