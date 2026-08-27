//! Capability 路由器实现——处理 sidecar 插件反向调用内核能力。
//!
//! 把 sidecar 的 `<capability>.<method>` 反向调用路由到内核实现：
//! - pipeline-executor.{suspend, resume, get_run_status} → 直接操作 runs 表
//!   （审批挂起/恢复与复盘轮询走 StorageBackend；任务执行统一走
//!   chat.send_message → PipelineExecutor）
//! - event-bus.emit → 广播事件（当前记录日志，前端推送留 P1）
//! - metrics.record → 写入指标聚合器（监控设计 §三 通道2，第 6 个 capability）
//! - service-registry.<域>.<op> → 插件访问内核共享基础设施存储（execution-records/
//!   pipeline-summaries/memory 三域，对应 M1 内核存储层）。基础设施下沉内核后，
//!   插件不再各自持有进程内 ServiceProvider/store，统一经此 capability 调内核。
//!
//! [来源: ROADMAP.md 审批暂停/恢复、复盘调管道的前置地基]
//! [来源: docs/working/重要设计/插件监控与指标机制设计.md §三 通道2]

use std::sync::Arc;

use agentos_core::traits::{CapabilityRegistry, MessageQueryOpts, StorageBackend};
use agentos_mcp::{CapabilityRouter, McpError};
use async_trait::async_trait;
use serde_json::{json, Value};
use tracing::warn;

use crate::metrics::{Labels, MetricType, MetricsAggregator};

/// service-registry 能力错误码前缀（基础设施存储调用）。
const ERR_SERVICE_REGISTRY: i64 = -32020;

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
    /// 动态 capability handler 注册表（M2/M4）——插件通过 manifest provides.capabilities
    /// 注册的 namespace 在这里路由。handle() 先查这里，miss 再走下方 match。
    /// None = 不支持插件自注册能力（仅内核内置能力可用）。
    handler_registry: Option<Arc<agentos_mcp::CapabilityHandlerRegistry>>,
    /// 授权查询器（G6：granted_capabilities 白名单的查找通道）。
    /// 闭包签名 (plugin_id) → Some(白名单) 当且仅当该插件声明了非空
    /// granted_capabilities；None = 未声明。
    /// None（字段）= 未装配授权查询 → 不做校验（兼容旧装配/测试）。
    grants_lookup: Option<GrantsLookupFn>,
    /// G6 strict 开关（AGENTOS_GRANTS_STRICT=1 时由装配方置位）：
    /// strict = 未声明 granted_capabilities 的插件一律拒绝反向调用（fail-closed，
    /// 新插件必须显式声明）；非 strict = 未声明默认全授予（存量插件零迁移）。
    /// 判定语义见 [`Self::grants_lookup`] 与 handle() 的 G6 单点。
    grants_strict: bool,
    /// 动态工具注册器（G3：registry.register_tool 的执行通道）。
    /// 闭包负责三道闸的后两道——enablement 校验（插件须 Enabled）+
    /// 写入注册表（经 M1 guarded 注册入 scope）+ 持久化（可重建性闸，写 DB）。
    /// 信封闸（granted_capabilities 须含 "registry"）由上方 G6 单点校验覆盖。
    /// None = 动态注册不可用（返回显式错误）。
    dynamic_tool_registrar: Option<DynamicToolRegistrar>,
    /// 域事件广播闭包（DSH hook 翻译：event-bus.emit 的域事件名单事件同步
    /// 广播，使 approval.created 等触达触发器订阅者）。None = 不广播。
    domain_broadcaster: Option<DomainBroadcaster>,
    /// 内核能力契约（定义驱动入口校验）：config/kernel_capabilities/
    /// *.json 声明的 input_schema（含 pattern 形态）在本路由入口逐条执行——
    /// G6 授权之后、派发之前。None = 未装配契约 → 宽泛放行（兼容旧装配/测试）。
    capability_contracts: Option<Arc<Vec<crate::kernel_capabilities::KernelCapabilityContract>>>,
    /// 流式声明查询器（ADR 2026-08-22 流式协议）：(plugin_id) → capabilities.streaming
    /// 声明。None = 未装配 → 声明闸放行（兼容旧装配/测试）。装配后未声明即拒。
    streaming_declaration_lookup: Option<StreamingDeclarationLookupFn>,
    /// 工具连续失败告警器：同一工具名在调用侧连续返回
    /// success=false（参数校验失败/执行错误）达到阈值即告警——把"同一工具
    /// 连续 N 次失败"这个信号汇总成一条可操作的告警，避免空转无人察觉。
    tool_failure_tracker:
        Option<std::sync::Arc<dyn crate::tools::ToolFailureTracker + Send + Sync>>,
}

/// 域事件广播闭包：(event_name, tags) → 点对点投递给声明 domain_event 的
/// 启用插件（组件版 broadcast_domain_event_from；观察总线由调用方决定）。
/// tags 键为静态字符串（调用处全部为字面量）。
pub type DomainBroadcaster =
    Arc<dyn Fn(&str, Vec<(&'static str, serde_json::Value)>) + Send + Sync>;

/// 流式声明查询闭包：(plugin_id) → 该插件的 capabilities.streaming 声明
/// （None = 未声明 → 网关拒绝其流式事件）。
pub type StreamingDeclarationLookupFn =
    Arc<dyn Fn(&str) -> Option<agentos_core::traits::StreamingCapability> + Send + Sync>;

/// G3：动态工具注册器闭包。
///
/// (plugin_id, ToolDescriptor) → Ok(())。实现方负责 enablement 闸、
/// 注册表写入（M1 guarded + scope 登记）与持久化（可重建性闸）。
pub type DynamicToolRegistrar =
    Arc<dyn Fn(&str, agentos_core::traits::ToolDescriptor) -> Result<(), String> + Send + Sync>;

/// G6：granted_capabilities 白名单查询闭包。
///
/// (plugin_id) → `Some(grants)` = 该插件声明了非空白名单（白名单制）；
/// `None` = 未声明。未声明是否放行由 strict 开关决定（默认放行，存量零迁移；
/// `AGENTOS_GRANTS_STRICT=1` 时拒绝）。
pub type GrantsLookupFn = Arc<dyn Fn(&str) -> Option<Vec<String>> + Send + Sync>;

impl KernelCapabilityRouter {
    /// 创建带指标聚合器的路由器（生产用，启用 metrics.record 反向调用）。
    pub fn with_metrics(metrics: MetricsAggregator) -> Self {
        Self {
            metrics: Some(metrics),
            invoker: None,
            registry: None,
            session: None,
            store: None,
            handler_registry: None,
            grants_lookup: None,
            grants_strict: false,
            dynamic_tool_registrar: None,
            domain_broadcaster: None,
            capability_contracts: None,
            streaming_declaration_lookup: None,
            tool_failure_tracker: None,
        }
    }

    /// 注入内核能力契约（启用定义驱动入口校验：契约声明了 (namespace, method)
    /// 才按 input_schema 逐条校验——required/类型/pattern 形态/enum/闭包参数面；
    /// 未声明即宽泛放行）。生产装配自 config/kernel_capabilities/*.json。
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

    /// 注入工具失败追踪器（启用「同一工具连续失败」告警）。
    pub fn with_tool_failure_tracker(
        mut self,
        tracker: std::sync::Arc<dyn crate::tools::ToolFailureTracker + Send + Sync>,
    ) -> Self {
        self.tool_failure_tracker = Some(tracker);
        self
    }

    /// 注入插件调用器（启用 tool-executor.invoke 反向调用）。
    pub fn with_invoker(mut self, invoker: Arc<dyn agentos_core::traits::PluginInvoker>) -> Self {
        self.invoker = Some(invoker);
        self
    }

    /// 注入授权查询器（G6：启用 granted_capabilities 白名单单点校验）。
    pub fn with_grants_lookup(mut self, lookup: GrantsLookupFn) -> Self {
        self.grants_lookup = Some(lookup);
        self
    }

    /// 启用 G6 strict 模式（AGENTOS_GRANTS_STRICT=1 语义）：未声明
    /// granted_capabilities 的插件反向调用一律拒绝（fail-closed，新插件必须
    /// 显式声明）；不调用保持默认——未声明 = 全授予（存量插件零迁移）。
    pub fn with_grants_strict(mut self) -> Self {
        self.grants_strict = true;
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

    /// 注入动态 capability handler 注册表（M2/M4）。
    ///
    /// 启用后，handle() 先查注册表（插件自注册的 namespace 在这里路由），
    /// miss 再走内置 match（内核自带能力）。这让 `human-interaction` 等插件
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
        // G6 授权单点校验：所有反向 capability 调用（sidecar JSON-RPC / native
        // HostServices）都经过本方法——在此一处校验 granted_capabilities 白名单，
        // 两轨同判同一拒绝语义。_plugin_id 是 invoker 注入的信任锚点（插件不可伪造）。
        // 判定：声明非空 = 白名单制，namespace 不在名单内即拒绝；未声明走开关——
        // 默认全授予（存量插件零迁移），AGENTOS_GRANTS_STRICT=1（装配方经
        // with_grants_strict 置位）时未声明 = 拒绝（fail-closed，新插件必须显式
        // 声明）。粒度 = namespace（§八.2 待评审项，现取粗粒度，
        // capability.method 级细化留 G3 信封评审一并定）。
        if let (Some(lookup), Some(pid)) = (
            self.grants_lookup.as_ref(),
            params.get("_plugin_id").and_then(|v| v.as_str()),
        ) {
            match lookup(pid) {
                Some(grants) => {
                    if !grants.iter().any(|g| g == capability) {
                        warn!(
                            target: "capability_router",
                            plugin = pid,
                            capability = capability,
                            "G6 授权拒绝：capability 不在 granted_capabilities 白名单"
                        );
                        return Err(McpError::Protocol {
                            message: format!(
                                "capability '{}' not granted to plugin '{}' (granted_capabilities)",
                                capability, pid
                            ),
                        });
                    }
                }
                // 未声明 granted_capabilities：strict = 拒绝（fail-closed），
                // 非 strict = 默认全授予（存量兼容）
                None if self.grants_strict => {
                    warn!(
                        target: "capability_router",
                        plugin = pid,
                        capability = capability,
                        "G6 授权拒绝（strict）：插件未声明 granted_capabilities"
                    );
                    return Err(McpError::Protocol {
                        message: format!(
                            "capability '{}' not granted to plugin '{}': no granted_capabilities declared (AGENTOS_GRANTS_STRICT=1)",
                            capability, pid
                        ),
                    });
                }
                None => {}
            }
        }
        // 定义驱动入口校验：契约声明了 (capability, method)
        // 时按 input_schema 逐条执行（required/类型/pattern 形态/enum/闭包参数面），
        // 未声明即宽泛放行——定义详细到什么程度，就校验到什么程度。这是"相同
        // 类型、不同语义"错误（如 thread 坐标填进 pipeline_id 槽）的暴露点：
        // 形状全合法的互填在形态档（pattern）抓红。
        if let Some(contracts) = self.capability_contracts.as_ref() {
            crate::kernel_capabilities::validate_params(contracts, capability, method, &params)?;
        }
        // 先查动态 handler 注册表（M2/M4：插件自注册的 namespace 在这里路由）。
        // 命中则委托，不再走下方内置 match。这让 human-interaction 等插件能力
        // 不需修改内置 match 即可被路由。
        if let Some(reg) = &self.handler_registry {
            if reg.has_namespace(capability) {
                return reg.route(capability, method, params).await;
            }
        }
        // 内置能力分派：(capability, method) 精确对位一行委托到同文件域处理器
        // （handle_service_registry 先例）；语义、错误文案与派发集合与拆分前一致，
        // 未列出的组合仍落兜底臂。
        match (capability, method) {
            // ── pipeline-executor 域：runs 表状态簿记（审批挂起/恢复、复盘轮询、
            // 任务删除）；新引擎执行流由 state.suspended 插件机制控制，此处仅
            // 同步 runs 表状态供查询/恢复语义 ──
            ("pipeline-executor", "suspend") => self.handle_pipeline_executor_suspend(params).await,
            ("pipeline-executor", "resume") => self.handle_pipeline_executor_resume(params).await,
            // GAP-1 统一：按管道挂起/恢复（task_manage stop/resume 映射）
            ("pipeline-executor", "suspend_pipeline") => {
                self.handle_pipeline_executor_suspend_pipeline(params).await
            }
            ("pipeline-executor", "resume_pipeline") => {
                self.handle_pipeline_executor_resume_pipeline(params).await
            }
            ("pipeline-executor", "get_run_status") => {
                self.handle_pipeline_executor_get_run_status(params).await
            }
            ("pipeline-executor", "delete_pipeline") => {
                self.handle_pipeline_executor_delete_pipeline(params).await
            }

            // ── event-bus.emit：发事件/通知，流式 chunk 推送的核心出口。
            // sidecar（如 llm_core）每生成一个 chunk 就 notify 一次 event-bus.emit，
            // 内核收到后调 session.emit_event 把 chunk 推到前端 WS。 ──
            ("event-bus", "emit") => self.handle_event_bus_emit(params).await,

            // ── frontend.emit：插件 → 内核 → 前端一次性事件出口（ADR §3.5）。
            // 承载低频观测/进度事件（cost_update/tool_progress/termination_status）；
            // 分工：event-bus 承载 llm_core 流式 chunk，frontend.emit 承载一次性观测。──
            ("frontend", "emit") => self.handle_frontend_emit(params).await,

            // ── registry.register_tool：运行时动态注册（G3，VS Code 双层模型的
            // 动态层）。信封闸已由 G6 单点覆盖；使能/写入/持久化都在装配闭包里。──
            ("registry", "register_tool") => self.handle_registry_register_tool(params).await,

            // ── metrics.record：插件上报指标（监控设计 §三 通道2）。plugin_id 作
            // series 归属（不在 metric name 加前缀）；_plugin_id 为 invoker 注入
            // 的信任锚点。 ──
            ("metrics", "record") => self.handle_metrics_record(params).await,

            // ── tool-executor.invoke：tool_core 反向委托内核执行工具插件 sidecar ──
            ("tool-executor", "invoke") => self.handle_tool_executor_invoke(params).await,

            // ── service-registry：基础设施下沉内核后的共享存储（M2）──────────
            // method 形如 `<域>.<op>`（如 execution-records.list / memory.create）。
            // 经此 capability，插件统一访问内核 execution_records / pipeline_run_summaries
            // / memory 三表（M1 落地），不再各自持有进程内 ServiceProvider/store。
            ("service-registry", method) => self.handle_service_registry(method, params).await,

            // ── pipeline-state：state 聚合读面（GAP-2 CONDITION 求值上下文）──
            ("pipeline-state", "list") => self.handle_pipeline_state_list().await,

            // ── pipeline-state.update：任务域写面（仅允许 task.* 前缀键）──
            ("pipeline-state", "update") => self.handle_pipeline_state_update(params).await,

            // ── transient：插件中间态内存寄存器（ADR 2026-08-27，方案 §2.3）──
            // 中间态不落库、引擎内存持有、用完即清；插件 manifest 声明
            // capabilities.transient_state 即接入（复用声明→校验闸）。
            ("transient", "set") => self.handle_transient_set(params).await,
            ("transient", "get") => self.handle_transient_get(params).await,
            ("transient", "list") => self.handle_transient_list(params).await,
            ("transient", "clear") => self.handle_transient_clear(params).await,

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

            // 兜底：未注册/未实现的 capability.method（logger/config-reader 已作为
            // 死能力从两端 STANDARD_CAPABILITIES 删除，残余调用会落到这里）。
            (cap, m) => {
                warn!(
                    "unhandled capability call: {}.{} (params={})",
                    cap, m, params
                );
                Err(McpError::Protocol {
                    message: format!("capability method not implemented: {cap}.{m}"),
                })
            }
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
    /// 处理 service-registry.<域>.<op> 反向调用。
    ///
    /// method 形如 `execution-records.list`，先 split 出 (domain, op) 再分派。
    /// store 未注入时统一返回 ERR_SERVICE_REGISTRY。
    async fn handle_service_registry(
        &self,
        method: &str,
        params: Value,
    ) -> Result<Value, McpError> {
        let store = self.store.as_ref().ok_or_else(|| McpError::Protocol {
            message: "service-registry disabled: kernel store not injected".to_string(),
        })?;
        // method = "<domain>.<op>"
        let (domain, op) = method.split_once('.').ok_or_else(|| McpError::Protocol {
            message: format!("invalid service-registry method (expect <domain>.<op>): {method}"),
        })?;
        match (domain, op) {
            // ── messages 域（按 pipeline_id 查对话消息，复盘/压缩块恢复用）──
            ("messages", "list") => {
                let pipeline_id = params
                    .get("pipeline_id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| srv_err("missing pipeline_id".into()))?;
                let opts = parse_message_query_opts(&params);
                let rows = store
                    .get_messages_by_pipeline(pipeline_id, opts)
                    .await
                    .map_err(|e| srv_err(format!("messages.list: {e}")))?;
                serde_json::to_value(rows).map_err(|e| srv_err(format!("encode: {e}")))
            }
            // ── traces 域（按 thread_id 查插件 state 变更轨迹，复盘骨架用）──
            ("traces", "list") => {
                let thread_id = params
                    .get("thread_id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| srv_err("missing thread_id".into()))?;
                let tenant_id = params
                    .get("tenant_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("default");
                let rows = store
                    .get_step_traces_by_thread(thread_id, tenant_id)
                    .await
                    .map_err(|e| srv_err(format!("traces.list: {e}")))?;
                serde_json::to_value(rows).map_err(|e| srv_err(format!("encode: {e}")))
            }
            // ── pipeline-runs 域（runs 快照列表，调试中心会话/执行记录数据源）──
            // 与 GET /api/v1/pipelines/runs 同查询（list_pipelines_inner 四表联结），
            // 按 started_at 倒序；插件侧（channel_api）以此为「有执行记录的会话」清单。
            ("pipeline-runs", "list") => {
                let tenant_id = params
                    .get("tenant_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("default");
                let status = params
                    .get("status")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty());
                let limit = params
                    .get("limit")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(100)
                    .min(500) as u32;
                let rows = store
                    .list_pipelines(tenant_id, status, limit)
                    .await
                    .map_err(|e| srv_err(format!("pipeline-runs.list: {e}")))?;
                serde_json::to_value(rows).map_err(|e| srv_err(format!("encode: {e}")))
            }
            (domain, op) => Err(McpError::Protocol {
                message: format!("service-registry method not implemented: {domain}.{op}"),
            }),
        }
    }

    // ── 内置能力域处理器：handle() 的 match 按域一行委托到这里 ─────────────

    /// pipeline-executor.suspend：按 run_id 挂起。已挂起直接返回当前句柄（幂等）。
    async fn handle_pipeline_executor_suspend(&self, params: Value) -> Result<Value, McpError> {
        let run_id = params
            .get("run_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpError::Protocol {
                message: "suspend 缺少 run_id 参数".to_string(),
            })?;
        let store = self.store.as_ref().ok_or_else(|| McpError::Protocol {
            message: "suspend disabled: kernel store not injected".to_string(),
        })?;
        let run = store
            .get_run(run_id)
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("suspend 失败: {e}"),
            })?;
        // 已终态（completed/failed）的 run 不再挂起，直接返回当前句柄（幂等）
        if run.status == agentos_core::types::RunStatus::Suspended {
            return Ok(json!({
                "status": "suspended",
                "run_id": run.run_id,
                "branch_id": run.current_branch,
                "seq": run.current_seq,
            }));
        }
        store
            .update_run_status(
                run_id,
                agentos_core::types::RunStatus::Suspended,
                Some(&run.current_branch),
                Some(run.current_seq),
            )
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("suspend 失败: {e}"),
            })?;
        // 返回完整 handle，sidecar resume 时需回传全部字段
        Ok(json!({
            "status": "suspended",
            "run_id": run.run_id,
            "branch_id": run.current_branch,
            "seq": run.current_seq,
        }))
    }

    /// pipeline-executor.resume：按 run_id 恢复为 Running。
    async fn handle_pipeline_executor_resume(&self, params: Value) -> Result<Value, McpError> {
        // resume 需要完整的 SuspendHandle（run_id + branch_id + seq）。
        // sidecar 在 suspend 时拿到 handle，resume 时回传完整字段。
        let run_id = params
            .get("run_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpError::Protocol {
                message: "resume 缺少 run_id 参数".to_string(),
            })?;
        let store = self.store.as_ref().ok_or_else(|| McpError::Protocol {
            message: "resume disabled: kernel store not injected".to_string(),
        })?;
        store
            .update_run_status(run_id, agentos_core::types::RunStatus::Running, None, None)
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("resume 失败: {e}"),
            })?;
        Ok(json!({"status": "resumed", "run_id": run_id}))
    }

    /// pipeline-executor.suspend_pipeline：挂起该管道最新非终态 run
    /// （task_manage stop 映射；GAP-1 统一）。幂等：无匹配 run 返回 ok。
    async fn handle_pipeline_executor_suspend_pipeline(
        &self,
        params: Value,
    ) -> Result<Value, McpError> {
        let pipeline_id = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpError::Protocol {
                message: "suspend_pipeline 缺少 pipeline_id 参数".to_string(),
            })?;
        let store = self.store.as_ref().ok_or_else(|| McpError::Protocol {
            message: "suspend_pipeline disabled: kernel store not injected".to_string(),
        })?;
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let runs = store
            .list_runs_by_pipeline(pipeline_id, &tenant_id)
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("suspend_pipeline 查询失败: {e}"),
            })?;
        let target = runs.into_iter().find(|r| {
            r.status == agentos_core::types::RunStatus::Running
                || r.status == agentos_core::types::RunStatus::Suspended
        });
        match target {
            Some(run) if run.status == agentos_core::types::RunStatus::Suspended => {
                Ok(json!({"status": "suspended", "pipeline_id": pipeline_id, "run_id": run.run_id}))
            }
            Some(run) => {
                store
                    .update_run_status(
                        &run.run_id,
                        agentos_core::types::RunStatus::Suspended,
                        Some(&run.current_branch),
                        Some(run.current_seq),
                    )
                    .await
                    .map_err(|e| McpError::Protocol {
                        message: format!("suspend_pipeline 失败: {e}"),
                    })?;
                Ok(json!({"status": "suspended", "pipeline_id": pipeline_id, "run_id": run.run_id}))
            }
            None => Ok(json!({"status": "suspended", "pipeline_id": pipeline_id, "run_id": ""})),
        }
    }

    /// pipeline-executor.resume_pipeline：恢复该管道最新 suspended run。
    async fn handle_pipeline_executor_resume_pipeline(
        &self,
        params: Value,
    ) -> Result<Value, McpError> {
        let pipeline_id = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpError::Protocol {
                message: "resume_pipeline 缺少 pipeline_id 参数".to_string(),
            })?;
        let store = self.store.as_ref().ok_or_else(|| McpError::Protocol {
            message: "resume_pipeline disabled: kernel store not injected".to_string(),
        })?;
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let runs = store
            .list_runs_by_pipeline(pipeline_id, &tenant_id)
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("resume_pipeline 查询失败: {e}"),
            })?;
        let target = runs
            .into_iter()
            .find(|r| r.status == agentos_core::types::RunStatus::Suspended);
        match target {
            Some(run) => {
                store
                    .update_run_status(
                        &run.run_id,
                        agentos_core::types::RunStatus::Running,
                        None,
                        None,
                    )
                    .await
                    .map_err(|e| McpError::Protocol {
                        message: format!("resume_pipeline 失败: {e}"),
                    })?;
                Ok(json!({"status": "resumed", "pipeline_id": pipeline_id, "run_id": run.run_id}))
            }
            None => Ok(json!({"status": "resumed", "pipeline_id": pipeline_id, "run_id": ""})),
        }
    }

    /// pipeline-executor.get_run_status：复盘侧轮询子管道真实完成状态
    /// （F-REVIEW-2），直接查 runs 表并返回完整 RunRecord。
    async fn handle_pipeline_executor_get_run_status(
        &self,
        params: Value,
    ) -> Result<Value, McpError> {
        let run_id = params
            .get("run_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpError::Protocol {
                message: "get_run_status 缺少 run_id 参数".to_string(),
            })?;
        let store = self.store.as_ref().ok_or_else(|| McpError::Protocol {
            message: "get_run_status disabled: kernel store not injected".to_string(),
        })?;
        let run = store
            .get_run(run_id)
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("get_run_status 失败: {e}"),
            })?;
        serde_json::to_value(&run).map_err(|e| McpError::Protocol {
            message: format!("get_run_status 编码失败: {e}"),
        })
    }

    /// pipeline-executor.delete_pipeline：任务删除语义（0.2 任务 = 管道），删除
    /// 该管道全部执行数据并清内存 registry 条目。幂等：无记录返回 ok。
    async fn handle_pipeline_executor_delete_pipeline(
        &self,
        params: Value,
    ) -> Result<Value, McpError> {
        let pipeline_id = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| McpError::Protocol {
                message: "delete_pipeline 缺少 pipeline_id 参数".to_string(),
            })?;
        let store = self.store.as_ref().ok_or_else(|| McpError::Protocol {
            message: "delete_pipeline disabled: kernel store not injected".to_string(),
        })?;
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        store
            .delete_pipeline(pipeline_id)
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("delete_pipeline 失败: {e}"),
            })?;
        let registry = agentos_session::pipeline_state_registry::global_registry();
        registry.remove(&tenant_id, pipeline_id);
        Ok(json!({"status": "deleted", "pipeline_id": pipeline_id}))
    }

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
        if crate::kernel_capabilities::find_spec(contracts, "streaming", event_name).is_none() {
            return None;
        }
        // 声明闸（ADR 2026-08-22）：插件须声明 capabilities.streaming 才能发射
        // 流式事件；未声明即拒（fail-closed）。引擎管道家族（llm_core/tool_core）
        // 是内核 LLM 路径的器官，豁免——它们携带内核签发的 a_ id，命名空间执法见下。
        if let Some(pid) = plugin_id {
            let is_conduit = crate::kernel_capabilities::ENGINE_CONDUIT_PLUGINS.contains(&pid);
            if !is_conduit {
                let declared = self
                    .streaming_declaration_lookup
                    .as_ref()
                    .and_then(|lookup| lookup(pid));
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
        if let Err(reason) = crate::kernel_capabilities::validate_streaming_event(
            contracts, event_name, payload, plugin_id,
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

    /// 流式事件族（stream_chunk/thinking_*/stream_end/stream_error）。命中返回
    /// Some(响应)，未命中返回 None 落下一族。0.1 协议信封必须含 pipeline_id +
    /// message_id，否则前端 resolvePipelineId/extractMessageId 失败丢弃；
    /// thinking 系列驱动前端思考卡片渲染。
    async fn try_route_stream_family(
        &self,
        event_name: &str,
        payload: &Value,
    ) -> Option<Result<Value, McpError>> {
        let stream_events = [
            "stream_chunk",
            "thinking_start",
            "thinking_chunk",
            "thinking_end",
            "stream_end",
            "stream_error",
        ];
        if !stream_events.contains(&event_name) {
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
                self.accumulate_stream_interception(event_name, payload, &thread_id, &message_id);
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
            let _step_id =
                agentos_engine::global_registry().resolve_step_of(&tenant_id, pipeline_id, message_id);
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
    /// human-interaction 插件上报，前端 useInteractionHandler 渲染交互卡片/
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
        // 审批创建事件（approval.created）同步广播进域事件总线：除前端 WS 透传外，
        // 生命周期订阅者（触发器/域事件插件）也能感知审批请求——插件只发一次
        // event-bus，内核单点分流。
        if event_name == "approval.created" {
            if let Some(broadcaster) = &self.domain_broadcaster {
                // tag 提取同 derive_run_terminal_events 的 v() 法：字段缺失以 Null 占位。
                let tag = |k: &'static str| {
                    (
                        k,
                        payload.get(k).cloned().unwrap_or(serde_json::Value::Null),
                    )
                };
                broadcaster(
                    "approval.created",
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
        };
        let registrar = self
            .dynamic_tool_registrar
            .as_ref()
            .ok_or_else(|| McpError::Protocol {
                message: "registry.register_tool 未装配动态注册器（G3 未启用）".to_string(),
            })?;
        registrar(&plugin_id, descriptor).map_err(|reason| McpError::Protocol {
            message: format!("registry.register_tool 拒绝: {}", reason),
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

    /// tool-executor.invoke：tool_core 反向委托内核执行工具插件 sidecar
    /// （sidecar 进程内没有 ToolRegistry）。治理三段：会话身份告警 → 内部元数据
    /// 剥离 → 目标插件解析（fail-closed）后经 invoker 执行；连续失败计入告警器。
    async fn handle_tool_executor_invoke(&self, params: Value) -> Result<Value, McpError> {
        let tool_name = params
            .get("tool_name")
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpError::Protocol {
                message: "tool-executor.invoke 缺少 tool_name 参数".to_string(),
            })?;
        let tool_args_raw = params.get("args").cloned().unwrap_or(json!({}));
        // 越权防护（治理）：0.2 工具调用应携带会话身份。会话身份由 param_inject
        // 插件从 pipeline state 注入 session_id 到 args（state.session_id 来自
        // server.rs 构造的 initial_state），所有走 LLM 工具调用链的工具都带得上。
        // 缺失时告警不阻断——bash 等有状态工具插件侧用 _owner/session_id fallback
        // 链（bash/tool.py::_owner_from_inputs）做 pid 级越权兜底；此告警用于发现
        // 绕过 param_inject 的调用方（如 hindsight 经 memory_read 直接调用）。
        let has_owner = tool_args_raw
            .get("_owner")
            .and_then(|v| v.as_str())
            .is_some_and(|s| !s.is_empty())
            || tool_args_raw
                .get("session_id")
                .and_then(|v| v.as_str())
                .is_some_and(|s| !s.is_empty());
        if !has_owner {
            warn!(
                "tool-executor.invoke 缺少会话身份（_owner/session_id）| tool={} | args_keys={:?}",
                tool_name,
                tool_args_raw
                    .as_object()
                    .map(|m| m.keys().cloned().collect::<Vec<_>>()),
            );
        }
        // 剥离纯内部元数据字段：_owner 是治理身份注入、_log_ctx 是日志上下文
        // （SDK 在 _handle_tools_call 也会 pop _log_ctx）、tenant_id 是内核多租户
        // 上下文（经 task_local 传递，不作为工具参数）。这些仅供内核/SDK 使用，
        // 不应透传给工具 handler。
        //
        // 注意：session_id / pipeline_id / task_id 必须保留——它们是工具在
        // injected_params 中显式声明的参数，由 param_inject 插件从 pipeline state
        // 注入到 args；task/trigger 系工具（task_manage / trigger_setup /
        // trigger_review 等）依赖它们做权限校验与会话/管道/任务绑定。剥离它们会导致
        // sidecar 收到空值，报 MISSING_PIPELINE_ID / missing task_id 等。
        // 纯函数工具（file_read 等）不受影响：SDK 的 _filter_handler_kwargs
        // (agentos_plugin_sdk/server.py:54) 按 handler 签名过滤参数——无 **kwargs
        // 的工具自动丢弃这些字段，不会因 unexpected keyword argument 崩溃。
        let internal_keys = [
            "_owner",
            "_log_ctx",
            "tenant_id",
            "_call_context",
            "plugin_id",
        ];
        let mut tool_args = tool_args_raw;
        if let Some(obj) = tool_args.as_object_mut() {
            for k in internal_keys {
                obj.remove(k);
            }
        }
        // task_observability 任务 2：tool_core 在 params 级携带 _call_context
        // （前端路由键 call_id/pipeline_id/message_id/thread_id），合入 tool args
        // 透传给工具 sidecar——bash 等长任务工具据此经 frontend.emit 推
        // tool_progress 执行中进度。args 级的同名字段已在上方剥离（防伪造）。
        // 无 **kwargs 的纯函数工具由 SDK _filter_handler_kwargs 静默丢弃，无影响。
        if let Some(call_ctx) = params.get("_call_context") {
            if !call_ctx.is_null() {
                if let Some(obj) = tool_args.as_object_mut() {
                    obj.insert("_call_context".to_string(), call_ctx.clone());
                }
            }
        }
        // 解析目标插件：调用方可显式传 plugin_id（系统插件工具如 hindsight.recall
        // 不在 CapabilityRegistry——ADR 附录D① 只注册 tool 类插件工具给 LLM，反查
        // 必然失败）；缺省时从注册表反查 tool_name → plugin_id（tool_core 走 LLM
        // 工具链的既有路径）。反查失败 = 工具未注册，fail-closed 直接报错（不得用
        // "工具名当插件 ID"兜底——隐式契约"工具名==插件名才碰巧能用"会掩盖配置
        // 错误，报出误导性的 plugin not found）。
        let explicit_plugin_id = params
            .get("plugin_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty());
        let plugin_id = match explicit_plugin_id {
            Some(pid) => pid.to_string(),
            None => match self.registry.as_ref().and_then(|r| r.get_tool(tool_name)) {
                Some(td) => td.plugin_id.clone(),
                None => {
                    return Ok(json!({
                        "success": false,
                        "error": format!(
                            "工具 {} 未注册（不在插件工具注册表；可能被 G2 注册闸净化或插件未启用；非注册表工具请显式传 plugin_id）",
                            tool_name
                        ),
                    }));
                }
            },
        };
        let invoker = self.invoker.as_ref().ok_or_else(|| McpError::Protocol {
            message: "tool-executor 未配置 invoker".to_string(),
        })?;
        match invoker.invoke_tool(&plugin_id, tool_name, &tool_args).await {
            Ok(result) => {
                // 工具连续失败告警：结果 success=false（参数校验失败/执行错误，
                // 非网络 Err）计入同工具连续失败计数，达到阈值即告警——防止「同一
                // 工具 100% 失败」在流水日志里被淹没。
                if !result.success {
                    if let Some(tracker) = &self.tool_failure_tracker {
                        let sample = result
                            .error
                            .clone()
                            .unwrap_or_else(|| result.data.to_string());
                        if let Some(alert) = tracker.record(tool_name, false, &sample) {
                            tracing::error!(
                                target: "tool-executor",
                                tool = %alert.tool_name,
                                consecutive = alert.consecutive,
                                since_secs = alert.since_secs,
                                "工具连续失败 {} 次（校验/执行错误，非网络类），请检查工具声明与实现: {}",
                                alert.consecutive,
                                alert.error_sample
                            );
                        }
                    }
                } else if let Some(tracker) = &self.tool_failure_tracker {
                    tracker.record(tool_name, true, "");
                }
                // 序列化失败 = ToolExecutionResult 信封无法编码（契约 bug 级异常）：
                // 以 success=false 占位信封返回（sidecar 侧拿到显式失败而非挂起），
                // 但必须 error 可见——伪造信封不允许静默。
                Ok(serde_json::to_value(result).unwrap_or_else(|e| {
                    tracing::error!(
                        target: "tool-executor",
                        tool = %tool_name,
                        plugin = %plugin_id,
                        error = %e,
                        "工具结果序列化失败，降级 success=false 占位信封返回"
                    );
                    json!({"success": false})
                }))
            }
            Err(e) => Ok(json!({
                "success": false,
                "error": format!("tool execution failed: {}", e.message),
            })),
        }
    }

    /// pipeline-state.list：当前租户全部管道的 state 摘要行（扁平点号键，task.*/
    /// lineage.* 经 STATE_SUMMARY_KEYS 出口）——内存 registry 热行为主，DB 冷数据
    /// 兜底；messages 全文不出口。CONDITION 求值上下文数据源（GAP-2）。
    async fn handle_pipeline_state_list(&self) -> Result<Value, McpError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let registry = agentos_session::pipeline_state_registry::global_registry();
        let mut rows: Vec<Value> = Vec::new();
        let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
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
                crate::routes::summarize_state(&e.state)
            };
            if let Some(obj) = row.as_object_mut() {
                obj.insert("pipeline_id".to_string(), json!(listing.pipeline_id));
                obj.insert("thread_id".to_string(), json!(listing.thread_id));
                if !listing.agent_id.is_empty() {
                    obj.insert("agent_id".to_string(), json!(listing.agent_id));
                }
                obj.insert("source".to_string(), json!("memory"));
            }
            rows.push(row);
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
                let Some(merged) = crate::routes::cold_state_row(store, &pid, &tenant_id).await
                else {
                    continue; // checkpoint 与表行双空的真孤儿不出口（表行可独立兜底）
                };
                let mut row = crate::routes::summarize_state(&merged);
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

    /// pipeline-state.update：任务域写面。仅允许 task.* 前缀键（管道运行域字段
    /// 仍归引擎）；① 内存 registry 热路径 + ② pipeline_state 表冷路径双落点，
    /// state 单一真值（评估裁决 task_evaluate / 放行检测 task_reminder 写入通道）。
    async fn handle_pipeline_state_update(&self, params: Value) -> Result<Value, McpError> {
        let pipeline_id = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| McpError::Protocol {
                message: "pipeline-state.update 缺少 pipeline_id 参数".to_string(),
            })?;
        let fields = params
            .get("fields")
            .and_then(|v| v.as_object())
            .ok_or_else(|| McpError::Protocol {
                message: "pipeline-state.update 缺少 fields 对象参数".to_string(),
            })?;
        for k in fields.keys() {
            if !k.starts_with("task.") {
                return Err(McpError::Protocol {
                    message: format!(
                        "pipeline-state.update 仅允许写 task.* 前缀键（任务域），收到: {k}"
                    ),
                });
            }
        }
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        // ① 热路径：内存 registry（未注册则跳过——冷管道由表行兜底）
        let registry = agentos_session::pipeline_state_registry::global_registry();
        if let Some(entry) = registry.get(&tenant_id, pipeline_id) {
            let mut e = entry.write();
            if let Some(obj) = e.state.as_object_mut() {
                for (k, v) in fields {
                    obj.insert(k.clone(), v.clone());
                }
            }
        }
        // ② 冷路径：pipeline_state 表（重启后冷恢复读它）
        if let Some(store) = &self.store {
            for (k, v) in fields {
                store
                    .upsert_state_field(pipeline_id, &tenant_id, k, v)
                    .await
                    .map_err(|e| McpError::Protocol {
                        message: format!("pipeline-state.update 持久化失败: {e}"),
                    })?;
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

/// 从 params 对象构造 MessageQueryOpts（before_sequence/after_sequence/limit）。
fn parse_message_query_opts(params: &Value) -> MessageQueryOpts {    MessageQueryOpts {
        before_sequence: params
            .get("before_sequence")
            .and_then(|v| v.as_u64())
            .map(|s| s as u32),
        after_sequence: params
            .get("after_sequence")
            .and_then(|v| v.as_u64())
            .map(|s| s as u32),
        limit: params
            .get("limit")
            .and_then(|v| v.as_u64())
            .map(|l| l as usize),
    }
}

/// service-registry 错误（带固定错误码前缀）。
fn srv_err(msg: String) -> McpError {
    McpError::Protocol {
        message: format!("[{ERR_SERVICE_REGISTRY}] service-registry: {msg}"),
    }
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
        async fn invoke_pipeline_plugin(
            &self,
            _plugin_id: &str,
            _ctx: &agentos_core::types::PluginContext,
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
