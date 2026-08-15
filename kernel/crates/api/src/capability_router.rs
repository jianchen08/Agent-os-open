//! Capability 路由器实现——处理 sidecar 插件反向调用内核能力。
//!
//! 把 sidecar 的 `<capability>.<method>` 反向调用路由到内核实现：
//! - pipeline-executor.{suspend, resume, get_run_status} → 直接操作 runs 表
//!   （0.2 收尾：旧引擎 AdrEngineImpl 已清理，审批挂起/恢复与复盘轮询
//!   改走 StorageBackend；start_run 占位能力随旧引擎移除，任务执行
//!   统一走 chat.send_message → PipelineExecutor）
//! - event-bus.emit → 广播事件（当前记录日志，前端推送留 P1）
//! - config-reader.get → 读取配置节（从 AppState 配置缓存）
//! - metrics.record → 写入指标聚合器（监控设计 §三 通道2，第 6 个 capability）
//! - service-registry.<域>.<op> → 插件访问内核共享基础设施存储（M2：execution-records/
//!   pipeline-summaries/memory 三域，对应 M1 内核存储层）。基础设施下沉内核后，
//!   插件不再各自持有进程内 ServiceProvider/store，统一经此 capability 调内核。
//!
//! [来源: ROADMAP.md 审批暂停/恢复、复盘调管道的前置地基]
//! [来源: docs/working/重要设计/插件监控与指标机制设计.md §三 通道2]
//! [来源: docs/working/channel_api_migration_plan.md §七 M2]

use std::sync::Arc;

use agentos_core::traits::{CapabilityRegistry, MessageQueryOpts, StorageBackend};
use agentos_core::types::{ExecutionRecord, MemoryRecord, PipelineRunSummary};
use agentos_mcp::{CapabilityRouter, McpError};
use async_trait::async_trait;
use serde_json::{json, Value};
use tracing::warn;

use crate::metrics::{Labels, MetricType, MetricsAggregator};

/// 管道执行能力错误码前缀。
const ERR_PIPELINE: i64 = -32010;

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
    /// 配置读取器（G5：config-reader.get 的真实读取通道）。
    /// 闭包签名 (plugin_id, file_id) → 该插件 manifest.config_files 声明的
    /// 配置节内容；未声明/读取失败返回 Err（拒绝越权读配置）。
    /// None = config-reader 保持 no-op 兜底（返回 null value，兼容旧装配）。
    config_reader: Option<ConfigReaderFn>,
    /// 授权查询器（G6：granted_capabilities 白名单的查找通道）。
    /// 闭包签名 (plugin_id) → Some(白名单) 当且仅当该插件声明了非空
    /// granted_capabilities；None = 未声明（默认全授予，存量插件零迁移）。
    /// None（字段）= 未装配授权查询 → 不做校验（兼容旧装配/测试）。
    grants_lookup: Option<GrantsLookupFn>,
    /// 动态工具注册器（G3：registry.register_tool 的执行通道）。
    /// 闭包负责三道闸的后两道——enablement 校验（插件须 Enabled）+
    /// 写入注册表（经 M1 guarded 注册入 scope）+ 持久化（可重建性闸，写 DB）。
    /// 信封闸（granted_capabilities 须含 "registry"）由上方 G6 单点校验覆盖。
    /// None = 动态注册不可用（返回显式错误）。
    dynamic_tool_registrar: Option<DynamicToolRegistrar>,
}

/// G3：动态工具注册器闭包。
///
/// (plugin_id, ToolDescriptor) → Ok(())。实现方负责 enablement 闸、
/// 注册表写入（M1 guarded + scope 登记）与持久化（可重建性闸）。
pub type DynamicToolRegistrar =
    Arc<dyn Fn(&str, agentos_core::traits::ToolDescriptor) -> Result<(), String> + Send + Sync>;

/// G5：config-reader.get 的读取器闭包。
///
/// (plugin_id, file_id) → 配置节 JSON。实现方负责"file_id 必须在调用方插件
/// manifest.config_files 里声明"的越权校验（插件只能读自己声明的配置）。
pub type ConfigReaderFn =
    Arc<dyn Fn(&str, &str) -> Result<serde_json::Value, String> + Send + Sync>;

/// G6：granted_capabilities 白名单查询闭包。
///
/// (plugin_id) → `Some(grants)` = 该插件声明了非空白名单（白名单制）；
/// `None` = 未声明（默认全授予，向后兼容存量插件）。
pub type GrantsLookupFn = Arc<dyn Fn(&str) -> Option<Vec<String>> + Send + Sync>;

impl Default for KernelCapabilityRouter {
    fn default() -> Self {
        Self::new()
    }
}

impl KernelCapabilityRouter {
    /// 创建路由器（不带指标聚合器，兼容旧调用方）。
    pub fn new() -> Self {
        Self {
            metrics: None,
            invoker: None,
            registry: None,
            session: None,
            store: None,
            handler_registry: None,
            config_reader: None,
            grants_lookup: None,
            dynamic_tool_registrar: None,
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
            handler_registry: None,
            config_reader: None,
            grants_lookup: None,
            dynamic_tool_registrar: None,
        }
    }

    /// 注入插件调用器（启用 tool-executor.invoke 反向调用）。
    pub fn with_invoker(mut self, invoker: Arc<dyn agentos_core::traits::PluginInvoker>) -> Self {
        self.invoker = Some(invoker);
        self
    }

    /// 注入配置读取器（G5：启用 config-reader.get 真实读取）。
    pub fn with_config_reader(mut self, reader: ConfigReaderFn) -> Self {
        self.config_reader = Some(reader);
        self
    }

    /// 注入授权查询器（G6：启用 granted_capabilities 白名单单点校验）。
    pub fn with_grants_lookup(mut self, lookup: GrantsLookupFn) -> Self {
        self.grants_lookup = Some(lookup);
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
        // 语义：未声明白名单 = 默认全授予（存量插件零迁移）；声明非空 = 白名单制，
        // capability namespace 不在名单内即拒绝。粒度 = namespace（§八.2 待评审项，
        // 现取粗粒度，capability.method 级细化留 G3 信封评审一并定）。
        if let (Some(lookup), Some(pid)) = (
            self.grants_lookup.as_ref(),
            params.get("_plugin_id").and_then(|v| v.as_str()),
        ) {
            if let Some(grants) = lookup(pid) {
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
        }
        // 先查动态 handler 注册表（M2/M4：插件自注册的 namespace 在这里路由）。
        // 命中则委托，不再走下方内置 match。这让 human-interaction 等插件能力
        // 不需修改内置 match 即可被路由。
        if let Some(reg) = &self.handler_registry {
            if reg.has_namespace(capability) {
                return reg.route(capability, method, params).await;
            }
        }
        match (capability, method) {
            // ── pipeline-executor：挂起/恢复管道（审批闭环）＋ 运行状态查询（复盘）──
            // 0.2 收尾：旧引擎 AdrEngineImpl 已清理，这些能力直接操作 runs 表
            // （审批挂起是状态簿记——新引擎执行流由 state.suspended 插件机制控制，
            // 此处仅同步 runs 表状态供查询/恢复语义；start_run 占位能力已移除）。
            ("pipeline-executor", "suspend") => {
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
            ("pipeline-executor", "resume") => {
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
            ("pipeline-executor", "get_run_status") => {
                // F-REVIEW-2：复盘侧轮询子管道真实完成状态的最小能力。
                // 直接查 runs 表（store 生产侧由 agentos-kernel.rs with_store 注入）。
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
                // 返回完整 RunRecord（run_id/status/ended_at/...），status 序列化为
                // lowercase（running/suspended/completed/failed）。
                serde_json::to_value(&run).map_err(|e| McpError::Protocol {
                    message: format!("get_run_status 编码失败: {e}"),
                })
            }

            // ── event-bus：发事件/通知。流式 chunk 推送的核心出口 ──
            // sidecar（如 llm_core）每生成一个 chunk 就 notify 一次 event-bus.emit，
            // 内核收到后调 session.emit_stream 把 chunk 推到前端 WS。
            ("event-bus", "emit") => {
                let event_name = params
                    .get("event")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                let payload = params
                    .get("payload")
                    .cloned()
                    .unwrap_or(serde_json::Value::Null);
                tracing::debug!(target: "capability:event-bus", event = %event_name, "收到 event-bus.emit");

                // 流式事件族：stream_chunk / thinking_start / thinking_chunk / thinking_end
                // 对齐 0.1 协议（bridge_events._make_event）：信封必须含 pipeline_id +
                // message_id，否则前端 resolvePipelineId/extractMessageId 失败丢弃。
                // thinking 系列让前端渲染思考卡片（thinkingHandler.ts）。
                // stream_end / stream_error：内核收尾裁决也会发（dispatch_user_input），
                // 插件侧想发同样放行（如 llm_core 感知流中途断掉时主动上报）。
                let stream_events = [
                    "stream_chunk",
                    "thinking_start",
                    "thinking_chunk",
                    "thinking_end",
                    "stream_end",
                    "stream_error",
                ];
                if stream_events.contains(&event_name) {
                    if let Some(session) = &self.session {
                        let thread_id = payload
                            .get("thread_id")
                            .and_then(|v| v.as_str())
                            .unwrap_or("");
                        let content = payload
                            .get("content")
                            .and_then(|v| v.as_str())
                            .or_else(|| payload.get("chunk").and_then(|v| v.as_str()))
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
                        let needs_content =
                            event_name == "stream_chunk" || event_name == "thinking_chunk";
                        if !thread_id.is_empty() && (!needs_content || !content.is_empty()) {
                            let mut data = serde_json::json!({
                                "pipeline_id": pipeline_id,
                                "message_id": message_id,
                                "_threadId": thread_id,
                            });
                            if !content.is_empty() {
                                data["content"] = serde_json::Value::String(content.to_string());
                            }
                            let _ = session.emit_event(thread_id, event_name, data).await;
                        } else {
                            // 诊断：事件被丢弃（thread_id 空 或 stream_chunk content 空）
                            // debug 级避免流式噪声；仅 stop/thinking_end 等无 content 事件偶发。
                            tracing::debug!(
                                target: "capability:event-bus",
                                event = %event_name,
                                thread = %thread_id,
                                has_content = !content.is_empty(),
                                "流式事件被丢弃（thread_id 空 或 content 空）"
                            );
                        }
                    }
                    return Ok(json!({"status": "emitted", "event": event_name}));
                }

                // 工具事件族：tool_start / tool_result / tool_multimedia_result
                // tool_core（sidecar 或原生 cdylib）执行工具前后经 event-bus.emit 上报，
                // 让前端渲染工具卡片（toolHandler.ts handleToolStart/handleToolResult）。
                // 与流式族不同：工具事件携带结构化字段（call_id/tool_name/args/result/
                // success/duration_ms/result_data 等），整体透传 payload 进 data，
                // 补 pipeline_id/message_id/_threadId 路由键即可（前端 handler 双取顶层/data）。
                let tool_events = ["tool_start", "tool_result", "tool_multimedia_result"];
                if tool_events.contains(&event_name) {
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
                            let _ = session.emit_event(thread_id, event_name, data).await;
                        }
                    }
                    return Ok(json!({"status": "emitted", "event": event_name}));
                }

                // 交互事件族：interaction_request / interaction_cancelled /
                // interaction_timeout / interaction_conversation_start 等
                // （human-interaction 插件经 event-bus.emit 上报，前端
                // useInteractionHandler 订阅后渲染 InteractionCard/全局浮层表单）。
                // 与工具族一致：整体透传 payload，补 _threadId 路由键。
                if event_name.starts_with("interaction_") {
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
                            let _ = session.emit_event(thread_id, event_name, data).await;
                        } else {
                            tracing::debug!(
                                target: "capability:event-bus",
                                event = %event_name,
                                "交互事件被丢弃（thread_id 空）"
                            );
                        }
                    }
                    return Ok(json!({"status": "emitted", "event": event_name}));
                }

                // 其他事件名：透传转发（补路由键）。插件自定义事件（如审批类、
                // widget 交互反馈）经此直接到达前端——"插件经内核推"通道的通用出口，
                // 前端按 type 订阅即可。与 frontend.emit 同构：payload 整体透传 +
                // 补 pipeline_id/message_id/_threadId 路由键。
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
                        let _ = session.emit_event(thread_id, event_name, data).await;
                        return Ok(json!({"status": "emitted", "event": event_name}));
                    } else {
                        tracing::debug!(
                            target: "capability:event-bus",
                            event = %event_name,
                            "透传事件被丢弃（thread_id 空）"
                        );
                    }
                }
                Ok(json!({"status": "emitted", "event": event_name}))
            }

            // ── frontend：插件 → 内核 → 前端一次性事件出口（ADR §3.5）──
            // 低频观测/进度事件（cost_update / tool_progress / termination_status，
            // task_observability 任务 1/2）统一走此通道：payload 整体透传 +
            // 补 pipeline_id/message_id/_threadId 路由键（与 event-bus 工具族
            // 同构），经 session.emit_event 推前端（{type,data,sequence} 信封，
            // 与现有前端事件契约一致）。与 event-bus.emit 的分工：event-bus 承载
            // llm_core 流式 chunk；frontend.emit 承载一次性观测事件。
            // v1 无 per-plugin 令牌桶限流（与 tool 事件通路一致）；源头自带
            // 节流（track 每轮一次、bash 进度 1KB/2s 阈值）。如需限流可改挂
            // FrontendEventBus（session/src/event_bus.rs）。
            ("frontend", "emit") => {
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
                    let _ = session.emit_event(thread_id, event_name, data).await;
                }
                Ok(json!({"status": "emitted", "event": event_name}))
            }

            // ── config-reader：读配置节（G5 接通真实读取）──
            // 语义（v1.5 ADR config_files）：插件按 file_id 读**自己在 manifest
            // config_files 里声明过的**配置节。信任锚点 _plugin_id 由 invoker 的
            // PluginScopedRouter 注入（sidecar 不可伪造）；native 经 NativeHostServices
            // 注入同字段。未声明 file_id = 越权读，拒绝。
            // 装配了读取器但调用方缺 _plugin_id（非插件上下文）→ 拒绝；
            // 未装配读取器（旧装配/测试）→ 保持 no-op 兜底（返回 null value）。
            ("config-reader", "get") => {
                let key = params.get("key").and_then(|v| v.as_str()).unwrap_or("");
                let Some(reader) = self.config_reader.as_ref() else {
                    warn!(target: "capability_router", "config-reader.get 未装配读取器，返回 null（no-op 兜底）");
                    return Ok(json!({"key": key, "value": null}));
                };
                let plugin_id = params
                    .get("_plugin_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                if plugin_id.is_empty() {
                    return Err(McpError::Protocol {
                        message: "config-reader.get 需要 _plugin_id（插件上下文调用）".to_string(),
                    });
                }
                match reader(plugin_id, key) {
                    Ok(value) => Ok(json!({"key": key, "value": value})),
                    Err(reason) => Err(McpError::Protocol {
                        message: format!("config-reader.get 拒绝: {}", reason),
                    }),
                }
            }

            // ── registry：运行时动态注册（G3，VS Code 双层模型的动态层）──
            // 信封闸已由上方 G6 单点覆盖（granted_capabilities 须含 "registry"）。
            // 本分支做参数解析 + 委托 registrar（enablement 闸 + 写入 + 持久化
            // 都在装配闭包里——router 保持与具体注册表/存储类型解耦）。
            ("registry", "register_tool") => {
                use agentos_core::traits::ToolDescriptor;
                use agentos_core::types::{ToolCategory, ToolSource};
                let plugin_id = params
                    .get("_plugin_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                if plugin_id.is_empty() {
                    return Err(McpError::Protocol {
                        message: "registry.register_tool 需要 _plugin_id（插件上下文调用）"
                            .to_string(),
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
                let registrar =
                    self.dynamic_tool_registrar
                        .as_ref()
                        .ok_or_else(|| McpError::Protocol {
                            message: "registry.register_tool 未装配动态注册器（G3 未启用）"
                                .to_string(),
                        })?;
                registrar(&plugin_id, descriptor).map_err(|reason| McpError::Protocol {
                    message: format!("registry.register_tool 拒绝: {}", reason),
                })?;
                // 剩余项清仓 D2：注册成功即 schema.tools 变化——best-effort 经
                // session 广播 widget_event {schema, changed}（前端 resync.ts 消费，
                // 与 resync_required 同一重载链）。失败静默（观察层不拖垮注册主流程）。
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

            // ── metrics：插件上报指标（监控设计 §三 通道2，第 6 个 capability）──
            // sidecar 调 ctx.record_metric(name, value, metric_type, labels) →
            // 经 PluginScopedRouter 注入 _plugin_id（信任锚点）→ 这里写入聚合器。
            // 命名空间：内核用 plugin_id 作 series 的 plugin_id 字段（监控设计 §九），
            // 不在 metric name 里加前缀，避免与 series.plugin_id 冗余。
            ("metrics", "record") => {
                let agg = self.metrics.as_ref().ok_or_else(|| McpError::Protocol {
                    message: "metrics aggregator not enabled".to_string(),
                })?;
                // plugin_id 来自 invoker 注入的 _plugin_id（不可被 sidecar 伪造——
                // invoker 用 manifest.id 设置，sidecar 无法覆盖信任锚点）。
                let plugin_id = params
                    .get("_plugin_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown")
                    .to_string();
                let name = params.get("name").and_then(|v| v.as_str()).ok_or_else(|| {
                    McpError::Protocol {
                        message: "metrics.record 缺少 name 参数".to_string(),
                    }
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
                // labels：限长 + 禁特殊字符（监控设计 §十，防聚合器内存爆炸）
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

            // ── tool-executor：sidecar（如 tool_core）委托内核执行 tool 插件 sidecar ──
            // 0.2 sidecar 架构：tool_core sidecar 进程内没有 ToolRegistry（那是 0.1 单进程
            // 装配的），找不到 bash_execute 等工具。tool_core 通过此 capability 反向请求
            // 内核，内核用 invoke_tool 调对应的 tool 插件 sidecar（MCP），结果返回给 tool_core。
            ("tool-executor", "invoke") => {
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
                // 解析目标插件：调用方可显式传 plugin_id（系统插件工具如
                // hindsight.recall 不在 CapabilityRegistry——ADR 附录D① 只注册
                // tool 类插件工具给 LLM，反查必然失败）；缺省时从注册表反查
                // tool_name → plugin_id（tool_core 走 LLM 工具链的既有路径）。
                let explicit_plugin_id = params
                    .get("plugin_id")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty());
                let plugin_id = match explicit_plugin_id {
                    Some(pid) => pid.to_string(),
                    None => self
                        .registry
                        .as_ref()
                        .and_then(|r| r.get_tool(tool_name))
                        .map(|td| td.plugin_id.clone())
                        .unwrap_or_else(|| tool_name.to_string()),
                };
                let invoker = self.invoker.as_ref().ok_or_else(|| McpError::Protocol {
                    message: "tool-executor 未配置 invoker".to_string(),
                })?;
                match invoker.invoke_tool(&plugin_id, tool_name, &tool_args).await {
                    Ok(result) => {
                        Ok(serde_json::to_value(result)
                            .unwrap_or_else(|_| json!({"success": false})))
                    }
                    Err(e) => Ok(json!({
                        "success": false,
                        "error": format!("tool execution failed: {}", e.message),
                    })),
                }
            }

            // ── service-registry：基础设施下沉内核后的共享存储（M2）──────────
            // method 形如 `<域>.<op>`（如 execution-records.list / memory.create）。
            // 经此 capability，插件统一访问内核 execution_records / pipeline_run_summaries
            // / memory 三表（M1 落地），不再各自持有进程内 ServiceProvider/store。
            ("service-registry", method) => self.handle_service_registry(method, params).await,

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

            // logger 暂未实现具体 method
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
            // ── execution-records 域（对齐 M1 ExecutionRecord 存储）──
            ("execution-records", "append") => {
                let record: ExecutionRecord = serde_json::from_value(params)
                    .map_err(|e| srv_err(format!("decode execution record: {e}")))?;
                store
                    .append_execution_record(&record)
                    .await
                    .map_err(|e| srv_err(format!("append: {e}")))?;
                Ok(json!({ "ok": true }))
            }
            ("execution-records", "list") => {
                let pipeline_run_id = params
                    .get("pipeline_run_id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| srv_err("missing pipeline_run_id".into()))?;
                let opts = parse_message_query_opts(&params);
                let rows = store
                    .list_execution_records(pipeline_run_id, opts)
                    .await
                    .map_err(|e| srv_err(format!("list: {e}")))?;
                serde_json::to_value(rows).map_err(|e| srv_err(format!("encode: {e}")))
            }
            ("execution-records", "count") => {
                let pipeline_run_id = params
                    .get("pipeline_run_id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| srv_err("missing pipeline_run_id".into()))?;
                let n = store
                    .count_execution_records(pipeline_run_id)
                    .await
                    .map_err(|e| srv_err(format!("count: {e}")))?;
                Ok(json!({ "count": n }))
            }
            ("execution-records", "delete_by_session") => {
                let pipeline_run_id = params
                    .get("pipeline_run_id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| srv_err("missing pipeline_run_id".into()))?;
                let n = store
                    .delete_execution_records_by_session(pipeline_run_id)
                    .await
                    .map_err(|e| srv_err(format!("delete_by_session: {e}")))?;
                Ok(json!({ "deleted": n }))
            }
            // ── pipeline-summaries 域（对齐 M1 PipelineRunSummary 存储）──
            ("pipeline-summaries", "save") => {
                let summary: PipelineRunSummary = serde_json::from_value(params)
                    .map_err(|e| srv_err(format!("decode run summary: {e}")))?;
                store
                    .save_run_summary(&summary)
                    .await
                    .map_err(|e| srv_err(format!("save: {e}")))?;
                Ok(json!({ "ok": true }))
            }
            ("pipeline-summaries", "get") => {
                let run_id = params
                    .get("run_id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| srv_err("missing run_id".into()))?;
                let got = store
                    .get_run_summary(run_id)
                    .await
                    .map_err(|e| srv_err(format!("get: {e}")))?;
                Ok(got
                    .map(|s| serde_json::to_value(s).unwrap_or(Value::Null))
                    .unwrap_or(Value::Null))
            }
            ("pipeline-summaries", "update") => {
                let run_id = params
                    .get("run_id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| srv_err("missing run_id".into()))?;
                let updates = params.get("updates").cloned().unwrap_or(Value::Null);
                store
                    .update_run_summary(run_id, &updates)
                    .await
                    .map_err(|e| srv_err(format!("update: {e}")))?;
                Ok(json!({ "ok": true }))
            }
            ("pipeline-summaries", "list") => {
                let limit = params
                    .get("limit")
                    .and_then(|v| v.as_u64())
                    .map(|l| l as usize);
                let rows = store
                    .list_run_summaries(limit)
                    .await
                    .map_err(|e| srv_err(format!("list: {e}")))?;
                serde_json::to_value(rows).map_err(|e| srv_err(format!("encode: {e}")))
            }
            // ── memory 域（对齐 M1 MemoryRecord 存储）──
            ("memory", "create") => {
                let memory: MemoryRecord = serde_json::from_value(params)
                    .map_err(|e| srv_err(format!("decode memory: {e}")))?;
                store
                    .create_memory(&memory)
                    .await
                    .map_err(|e| srv_err(format!("create: {e}")))?;
                Ok(json!({ "ok": true }))
            }
            ("memory", "get") => {
                let id = params
                    .get("id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| srv_err("missing id".into()))?;
                let got = store
                    .get_memory(id)
                    .await
                    .map_err(|e| srv_err(format!("get: {e}")))?;
                Ok(got
                    .map(|m| serde_json::to_value(m).unwrap_or(Value::Null))
                    .unwrap_or(Value::Null))
            }
            ("memory", "list") => {
                let memory_type = params.get("memory_type").and_then(|v| v.as_str());
                let limit = params.get("limit").and_then(|v| v.as_u64()).unwrap_or(20) as usize;
                let offset = params.get("offset").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                let rows = store
                    .list_memory(memory_type, limit, offset)
                    .await
                    .map_err(|e| srv_err(format!("list: {e}")))?;
                serde_json::to_value(rows).map_err(|e| srv_err(format!("encode: {e}")))
            }
            ("memory", "search") => {
                let query = params
                    .get("query")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| srv_err("missing query".into()))?;
                let top_k = params.get("top_k").and_then(|v| v.as_u64()).unwrap_or(5) as usize;
                let rows = store
                    .search_memory(query, top_k)
                    .await
                    .map_err(|e| srv_err(format!("search: {e}")))?;
                serde_json::to_value(rows).map_err(|e| srv_err(format!("encode: {e}")))
            }
            ("memory", "delete") => {
                let id = params
                    .get("id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| srv_err("missing id".into()))?;
                let ok = store
                    .delete_memory(id)
                    .await
                    .map_err(|e| srv_err(format!("delete: {e}")))?;
                Ok(json!({ "deleted": ok }))
            }
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
            (domain, op) => Err(McpError::Protocol {
                message: format!("service-registry method not implemented: {domain}.{op}"),
            }),
        }
    }
}

/// 从 params 对象构造 MessageQueryOpts（before_sequence/after_sequence/limit）。
fn parse_message_query_opts(params: &Value) -> MessageQueryOpts {
    MessageQueryOpts {
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

/// 抑制未使用的错误码常量警告（后续 event-bus 错误码扩展时启用）。
#[allow(dead_code)]
fn _pipeline_error_code() -> i64 {
    ERR_PIPELINE
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn router_with_metrics() -> (KernelCapabilityRouter, MetricsAggregator) {
        let agg = MetricsAggregator::new();
        let r = KernelCapabilityRouter::with_metrics(agg.clone());
        (r, agg)
    }

    #[tokio::test]
    async fn test_metrics_record_counter() {
        let (router, agg) = router_with_metrics();
        let params = json!({
            "_plugin_id": "llm_service",
            "name": "tokens_used",
            "value": 1280,
            "metric_type": "counter",
            "labels": {"model": "deepseek"},
            "unit": "tokens",
            "help": "Total tokens used"
        });
        let res = router.handle("metrics", "record", params).await.unwrap();
        assert_eq!(res["status"], "recorded");
        assert_eq!(res["plugin_id"], "llm_service");

        let views = agg.query(
            Some("llm_service"),
            Some("tokens_used"),
            None,
            &Labels::new(),
        );
        assert_eq!(views.len(), 1);
        assert_eq!(views[0].latest, Some(1280.0));
        assert_eq!(views[0].unit.as_deref(), Some("tokens"));
        // labels 透传
        assert_eq!(views[0].labels.get("model").unwrap(), "deepseek");
    }

    #[tokio::test]
    async fn test_metrics_record_accumulates_counter() {
        let (router, agg) = router_with_metrics();
        for _ in 0..3 {
            router
                .handle(
                    "metrics",
                    "record",
                    json!({"_plugin_id":"p1","name":"calls","value":10,"metric_type":"counter"}),
                )
                .await
                .unwrap();
        }
        let views = agg.query(Some("p1"), Some("calls"), None, &Labels::new());
        // 3 次 ×10 = 30（counter 累加）
        assert_eq!(views[0].latest, Some(30.0));
    }

    #[tokio::test]
    async fn test_metrics_record_gauge_overwrites() {
        let (router, agg) = router_with_metrics();
        router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id":"p1","name":"conn","value":10,"metric_type":"gauge"}),
            )
            .await
            .unwrap();
        router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id":"p1","name":"conn","value":7,"metric_type":"gauge"}),
            )
            .await
            .unwrap();
        let views = agg.query(Some("p1"), Some("conn"), None, &Labels::new());
        // gauge 同桶 avg：(10+7)/2 = 8.5
        assert!((views[0].latest.unwrap() - 8.5).abs() < 0.01);
    }

    #[tokio::test]
    async fn test_metrics_record_histogram() {
        let (router, agg) = router_with_metrics();
        router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id":"p1","name":"lat","value":0.02,"metric_type":"histogram"}),
            )
            .await
            .unwrap();
        let views = agg.query(Some("p1"), Some("lat"), None, &Labels::new());
        let h = views[0].histogram.as_ref().unwrap();
        assert_eq!(h.count, 1);
    }

    #[tokio::test]
    async fn test_metrics_record_without_aggregator_errors() {
        // 不带 metrics 的 router → metrics.record 报错
        let router = KernelCapabilityRouter::new();
        let res = router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id":"p1","name":"m","value":1.0}),
            )
            .await;
        assert!(res.is_err());
    }

    #[tokio::test]
    async fn test_metrics_record_rejects_too_many_labels() {
        let (router, _agg) = router_with_metrics();
        let mut labels = serde_json::Map::new();
        for i in 0..21 {
            labels.insert(format!("k{i}"), json!(i.to_string()));
        }
        let res = router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id":"p1","name":"m","value":1.0,"labels":labels}),
            )
            .await;
        assert!(res.is_err());
    }

    #[tokio::test]
    async fn test_metrics_record_rejects_newline_in_label() {
        let (router, _agg) = router_with_metrics();
        let res = router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id":"p1","name":"m","value":1.0,
                       "labels":{"k":"a\nb"}}),
            )
            .await;
        assert!(res.is_err(), "newline in label value must be rejected");
    }

    #[tokio::test]
    async fn test_metrics_record_unknown_type() {
        let (router, _agg) = router_with_metrics();
        let res = router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id":"p1","name":"m","value":1.0,"metric_type":"bogus"}),
            )
            .await;
        assert!(res.is_err());
    }

    #[tokio::test]
    async fn test_metrics_record_missing_name() {
        let (router, _agg) = router_with_metrics();
        let res = router
            .handle("metrics", "record", json!({"_plugin_id":"p1","value":1.0}))
            .await;
        assert!(res.is_err());
    }

    /// 捕获 event-bus 推送到 sink 的文本（验证 tool 事件转发）。
    struct CaptureSink {
        received: std::sync::Arc<std::sync::Mutex<Vec<Value>>>,
    }
    #[async_trait::async_trait]
    impl agentos_session::EventSink for CaptureSink {
        async fn send_text(&self, text: &str) -> bool {
            if let Ok(v) = serde_json::from_str::<Value>(text) {
                self.received.lock().unwrap().push(v);
            }
            true
        }
        fn id(&self) -> u64 {
            1
        }
    }

    /// 构建带 session 的 router + 捕获 sink（验证 event-bus.emit 转发到前端）。
    fn router_with_session(
        received: std::sync::Arc<std::sync::Mutex<Vec<Value>>>,
    ) -> KernelCapabilityRouter {
        use agentos_session::SessionCoordinator;
        let coord = Arc::new(SessionCoordinator::default());
        let sink = Arc::new(CaptureSink { received }) as Arc<dyn agentos_session::EventSink>;
        coord.register("user-test", sink);
        coord.register_thread("thread-1", "user-test");
        let agg = MetricsAggregator::new();
        KernelCapabilityRouter::with_metrics(agg).with_session(coord)
    }

    #[tokio::test]
    async fn test_event_bus_tool_start_forwarded_with_fields() {
        let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = router_with_session(received.clone());

        // 模拟 tool_core 经 host_call → event-bus.emit 上报的 tool_start 事件。
        let params = json!({
            "event": "tool_start",
            "payload": {
                "thread_id": "thread-1",
                "pipeline_id": "pipe-1",
                "message_id": "msg-1",
                "call_id": "call_abc",
                "tool_name": "bash_execute",
                "args": {"command": "echo hi"},
            }
        });
        let res = router.handle("event-bus", "emit", params).await.unwrap();
        assert_eq!(res["status"], "emitted");
        assert_eq!(res["event"], "tool_start");

        // sink 应收到透传后的事件（含路由键 + 业务字段）。
        let msgs = received.lock().unwrap().clone();
        assert_eq!(msgs.len(), 1);
        let ev = &msgs[0];
        assert_eq!(ev["type"], "tool_start");
        assert_eq!(ev["data"]["call_id"], "call_abc");
        assert_eq!(ev["data"]["tool_name"], "bash_execute");
        assert_eq!(ev["data"]["args"]["command"], "echo hi");
        assert_eq!(ev["data"]["pipeline_id"], "pipe-1");
        assert_eq!(ev["data"]["message_id"], "msg-1");
        assert_eq!(ev["data"]["_threadId"], "thread-1");
    }

    #[tokio::test]
    async fn test_event_bus_tool_result_forwarded() {
        let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = router_with_session(received.clone());

        let params = json!({
            "event": "tool_result",
            "payload": {
                "thread_id": "thread-1",
                "pipeline_id": "pipe-1",
                "message_id": "msg-1",
                "call_id": "call_abc",
                "tool_name": "bash_execute",
                "result": "hi\n",
                "success": true,
                "duration_ms": 5.2,
            }
        });
        let res = router.handle("event-bus", "emit", params).await.unwrap();
        assert_eq!(res["event"], "tool_result");

        let msgs = received.lock().unwrap().clone();
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0]["type"], "tool_result");
        assert_eq!(msgs[0]["data"]["call_id"], "call_abc");
        assert_eq!(msgs[0]["data"]["success"], true);
        assert_eq!(msgs[0]["data"]["duration_ms"], 5.2);
    }

    #[tokio::test]
    async fn test_event_bus_tool_event_no_thread_id_dropped() {
        // thread_id 缺失 → 前端无法路由，应丢弃（不推 sink）。
        let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = router_with_session(received.clone());

        let params = json!({
            "event": "tool_start",
            "payload": {"call_id": "c1", "tool_name": "f"}
        });
        let _ = router.handle("event-bus", "emit", params).await.unwrap();
        assert!(received.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn test_event_bus_interaction_request_forwarded() {
        let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = router_with_session(received.clone());
        let params = json!({
            "event": "interaction_request",
            "payload": {
                "thread_id": "thread-1",
                "request_id": "req-abc",
                "interaction_mode": "choice",
                "title": "请选择",
                "options": [{"id": "a", "label": "方案A"}],
            }
        });
        let res = router.handle("event-bus", "emit", params).await.unwrap();
        assert_eq!(res["status"], "emitted");
        assert_eq!(res["event"], "interaction_request");
        let msgs = received.lock().unwrap().clone();
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0]["type"], "interaction_request");
        assert_eq!(msgs[0]["data"]["request_id"], "req-abc");
        assert_eq!(msgs[0]["data"]["_threadId"], "thread-1");
    }

    #[tokio::test]
    async fn test_event_bus_interaction_event_no_thread_id_dropped() {
        let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = router_with_session(received.clone());
        let params = json!({
            "event": "interaction_request",
            "payload": {"request_id": "req-abc", "title": "x"}
        });
        let _ = router.handle("event-bus", "emit", params).await.unwrap();
        assert!(received.lock().unwrap().is_empty());
    }

    // ── frontend.emit：插件 → 内核 → 前端一次性事件出口（ADR §3.5，
    //    task_observability 任务 1/2 共享前置）──

    #[tokio::test]
    async fn test_frontend_emit_cost_update_forwarded() {
        // track 插件经 frontend.emit 推 cost_update：payload 携带路由键 +
        // 单轮/累计 token 指标，整体透传并补齐路由键后推前端。
        let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = router_with_session(received.clone());

        let params = json!({
            "event": "cost_update",
            "payload": {
                "thread_id": "thread-1",
                "pipeline_id": "pipe-1",
                "message_id": "msg-1",
                "input_tokens": 60632,
                "output_tokens": 512,
                "cached_tokens": 60000,
                "total_tokens": 61144,
                "cache_hit_ratio": 0.9895,
                "cumulative": {
                    "total_input": 2458025,
                    "total_output": 30000,
                    "total_cached": 2331456,
                    "missed": 126569
                }
            }
        });
        let res = router.handle("frontend", "emit", params).await.unwrap();
        assert_eq!(res["status"], "emitted");
        assert_eq!(res["event"], "cost_update");

        let msgs = received.lock().unwrap().clone();
        assert_eq!(msgs.len(), 1);
        let ev = &msgs[0];
        assert_eq!(ev["type"], "cost_update");
        assert_eq!(ev["data"]["pipeline_id"], "pipe-1");
        assert_eq!(ev["data"]["message_id"], "msg-1");
        assert_eq!(ev["data"]["_threadId"], "thread-1");
        assert_eq!(ev["data"]["input_tokens"], 60632);
        assert_eq!(ev["data"]["cached_tokens"], 60000);
        assert_eq!(ev["data"]["cumulative"]["missed"], 126569);
        // sequence 信封由 SessionCoordinator 分配（与 tool 事件同空间）
        assert!(ev.get("sequence").is_some());
    }

    #[tokio::test]
    async fn test_frontend_emit_tool_progress_forwarded() {
        // bash 工具执行中经 frontend.emit 推 tool_progress（stdout 增量）。
        let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = router_with_session(received.clone());

        let params = json!({
            "event": "tool_progress",
            "payload": {
                "thread_id": "thread-1",
                "pipeline_id": "pipe-1",
                "message_id": "msg-1",
                "call_id": "call_abc",
                "tool_name": "bash_execute",
                "delta": "build ok\n",
                "bytes_read": 4096,
                "elapsed_ms": 2100
            }
        });
        let res = router.handle("frontend", "emit", params).await.unwrap();
        assert_eq!(res["event"], "tool_progress");

        let msgs = received.lock().unwrap().clone();
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0]["type"], "tool_progress");
        assert_eq!(msgs[0]["data"]["call_id"], "call_abc");
        assert_eq!(msgs[0]["data"]["delta"], "build ok\n");
        assert_eq!(msgs[0]["data"]["pipeline_id"], "pipe-1");
        assert_eq!(msgs[0]["data"]["_threadId"], "thread-1");
    }

    #[tokio::test]
    async fn test_frontend_emit_thread_id_top_level_fallback() {
        // thread_id 允许在 params 顶层（ADR scope 语义）——payload 内缺失时兜底。
        let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = router_with_session(received.clone());

        let params = json!({
            "event": "termination_status",
            "thread_id": "thread-1",
            "payload": {
                "pipeline_id": "pipe-1",
                "convergence": "converging",
                "remaining_budget_percent": 73.5
            }
        });
        let res = router.handle("frontend", "emit", params).await.unwrap();
        assert_eq!(res["event"], "termination_status");

        let msgs = received.lock().unwrap().clone();
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0]["type"], "termination_status");
        assert_eq!(msgs[0]["data"]["_threadId"], "thread-1");
        assert_eq!(msgs[0]["data"]["convergence"], "converging");
    }

    #[tokio::test]
    async fn test_frontend_emit_no_thread_id_dropped() {
        // thread_id 缺失（payload 与顶层都无）→ 前端无法路由，丢弃。
        let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = router_with_session(received.clone());

        let params = json!({
            "event": "cost_update",
            "payload": {"pipeline_id": "pipe-1", "total_tokens": 100}
        });
        let _ = router.handle("frontend", "emit", params).await.unwrap();
        assert!(received.lock().unwrap().is_empty());
    }

    // ── tool-executor._call_context 透传（task_observability 任务 2）──

    /// 捕获 invoke_tool 收到的 (plugin_id, tool_name, inputs)。
    struct CaptureInvoker {
        captured: std::sync::Arc<std::sync::Mutex<Vec<(String, String, Value)>>>,
    }
    #[async_trait::async_trait]
    impl agentos_core::traits::PluginInvoker for CaptureInvoker {
        async fn invoke_pipeline_plugin(
            &self,
            _plugin_id: &str,
            _ctx: &agentos_core::types::PluginContext,
        ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
            Err(agentos_core::types::PluginError {
                message: "not used in test".into(),
                code: None,
                source: None,
            })
        }
        async fn invoke_tool(
            &self,
            plugin_id: &str,
            tool_name: &str,
            inputs: &Value,
        ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError>
        {
            self.captured.lock().unwrap().push((
                plugin_id.to_string(),
                tool_name.to_string(),
                inputs.clone(),
            ));
            Ok(agentos_core::types::ToolExecutionResult {
                success: true,
                data: json!({"output": "ok"}),
                error: None,
                duration_ms: Some(1),
            })
        }
        async fn send_lifecycle_hook(
            &self,
            _plugin_id: &str,
            _hook: agentos_core::traits::LifecycleHook,
            _context: &agentos_core::traits::HookContext,
        ) -> Result<(), agentos_core::types::PluginError> {
            Ok(())
        }
    }

    /// 构造带单工具注册表（bash_execute → plugin_bash）+ 捕获 invoker 的 router。
    fn router_with_tool_invoke(
        captured: std::sync::Arc<std::sync::Mutex<Vec<(String, String, Value)>>>,
    ) -> KernelCapabilityRouter {
        use agentos_core::traits::ToolDescriptor;
        use agentos_core::types::{ToolCategory, ToolSource};
        use agentos_plugin_loader::CapabilityRegistryImpl;
        let registry = CapabilityRegistryImpl::new();
        registry.register_tool(
            "plugin_bash",
            ToolDescriptor {
                name: "bash_execute".into(),
                description: String::new(),
                plugin_id: "plugin_bash".into(),
                input_schema: json!({}),
                output_schema: None,
                category: ToolCategory::System,
                source: ToolSource::Mcp,
                ui: None,
                render: None,
            },
        );
        KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
            .with_invoker(Arc::new(CaptureInvoker { captured }))
            .with_registry(Arc::new(registry))
    }

    #[tokio::test]
    async fn test_tool_executor_invoke_merges_call_context_into_args() {
        // tool_core 在 params 级携带 _call_context（前端路由键）→
        // 内核合入 tool args 透传给工具 sidecar（bash 据此推 tool_progress）。
        let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = router_with_tool_invoke(captured.clone());

        let params = json!({
            "tool_name": "bash_execute",
            "args": {"command": "echo hi", "session_id": "sess-1"},
            "_call_context": {
                "call_id": "call_abc",
                "pipeline_id": "pipe-1",
                "message_id": "msg-1",
                "thread_id": "sess-1"
            }
        });
        let res = router
            .handle("tool-executor", "invoke", params)
            .await
            .unwrap();
        assert_eq!(res["success"], true);

        let calls = captured.lock().unwrap().clone();
        assert_eq!(calls.len(), 1);
        let (plugin_id, tool_name, inputs) = &calls[0];
        assert_eq!(plugin_id, "plugin_bash");
        assert_eq!(tool_name, "bash_execute");
        assert_eq!(inputs["command"], "echo hi");
        assert_eq!(inputs["session_id"], "sess-1");
        assert_eq!(inputs["_call_context"]["call_id"], "call_abc");
        assert_eq!(inputs["_call_context"]["pipeline_id"], "pipe-1");
        assert_eq!(inputs["_call_context"]["message_id"], "msg-1");
        assert_eq!(inputs["_call_context"]["thread_id"], "sess-1");
    }

    #[tokio::test]
    async fn test_tool_executor_invoke_without_call_context_untouched() {
        // 无 _call_context（旧 tool_core / 无进度需求的工具）→ args 原样透传。
        let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = router_with_tool_invoke(captured.clone());

        let params = json!({
            "tool_name": "bash_execute",
            "args": {"command": "echo hi", "session_id": "sess-1"},
        });
        let _ = router
            .handle("tool-executor", "invoke", params)
            .await
            .unwrap();

        let calls = captured.lock().unwrap().clone();
        assert_eq!(calls.len(), 1);
        let inputs = &calls[0].2;
        assert!(inputs.get("_call_context").is_none());
        assert_eq!(inputs["command"], "echo hi");
    }

    // ── tool-executor.invoke 目标插件解析（显式 plugin_id 优先于注册表反查）──

    #[tokio::test]
    async fn test_tool_executor_explicit_plugin_id_wins_over_registry() {
        // 调用方显式传 plugin_id（系统插件工具如 hindsight.recall 不在 CapabilityRegistry，
        // 反查必然失败）→ 必须优先用显式 plugin_id，而不是注册表反查结果。
        let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        // 注册表把 bash_execute → plugin_bash；但显式 plugin_id 指向 system 插件 hindsight。
        let router = router_with_tool_invoke(captured.clone());

        let params = json!({
            "tool_name": "bash_execute",
            "plugin_id": "hindsight",
            "args": {"query": "where did I leave the keys", "session_id": "sess-1"},
        });
        let res = router
            .handle("tool-executor", "invoke", params)
            .await
            .unwrap();
        assert_eq!(res["success"], true);

        let calls = captured.lock().unwrap().clone();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "hindsight", "显式 plugin_id 应优先于注册表反查");
        assert_eq!(calls[0].1, "bash_execute");
    }

    #[tokio::test]
    async fn test_tool_executor_empty_plugin_id_falls_back_to_registry() {
        // 显式 plugin_id 为空字符串 → 视为未提供，回退注册表反查。
        let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = router_with_tool_invoke(captured.clone());

        let params = json!({
            "tool_name": "bash_execute",
            "plugin_id": "",
            "args": {"command": "echo hi"},
        });
        let _ = router
            .handle("tool-executor", "invoke", params)
            .await
            .unwrap();

        let calls = captured.lock().unwrap().clone();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "plugin_bash", "空 plugin_id 应回退注册表反查");
    }

    #[tokio::test]
    async fn test_tool_executor_internal_keys_stripped_from_args() {
        // _owner/_log_ctx/tenant_id/plugin_id 是内核/SDK 内部元数据，不得透传给工具 handler。
        let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = router_with_tool_invoke(captured.clone());

        let params = json!({
            "tool_name": "bash_execute",
            "plugin_id": "plugin_bash",
            "args": {
                "command": "echo hi",
                "session_id": "sess-1",
                "_owner": "user-1",
                "_log_ctx": {"request_id": "req-1"},
                "tenant_id": "tenant-a",
                "plugin_id": "spoofed",
                "pipeline_id": "pipe-1"
            },
            "_log_ctx": {"request_id": "req-1"},
        });
        let _ = router
            .handle("tool-executor", "invoke", params)
            .await
            .unwrap();

        let calls = captured.lock().unwrap().clone();
        let inputs = &calls[0].2;
        // 内部元数据全部剥离
        assert!(inputs.get("_owner").is_none(), "_owner 应被剥离");
        assert!(inputs.get("_log_ctx").is_none(), "_log_ctx 应被剥离");
        assert!(inputs.get("tenant_id").is_none(), "tenant_id 应被剥离");
        assert!(
            inputs.get("plugin_id").is_none(),
            "args 级 plugin_id 应被剥离（防伪造）"
        );
        // 业务字段保留（session_id/pipeline_id 是 param_inject 注入的显式参数）
        assert_eq!(inputs["command"], "echo hi");
        assert_eq!(inputs["session_id"], "sess-1");
        assert_eq!(inputs["pipeline_id"], "pipe-1");
    }

    #[tokio::test]
    async fn test_tool_executor_registry_fallback_without_explicit_id() {
        // 无显式 plugin_id → 注册表 tool_name → plugin_id 反查。
        let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = router_with_tool_invoke(captured.clone());

        let params = json!({
            "tool_name": "bash_execute",
            "args": {"command": "echo hi", "session_id": "sess-1"},
        });
        let _ = router
            .handle("tool-executor", "invoke", params)
            .await
            .unwrap();

        let calls = captured.lock().unwrap().clone();
        assert_eq!(calls[0].0, "plugin_bash");
    }

    #[tokio::test]
    async fn test_tool_executor_no_registry_falls_back_to_tool_name() {
        // 无注册表注入 + 无显式 plugin_id → plugin_id 兜底为 tool_name 本身。
        let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_invoker(
            Arc::new(CaptureInvoker {
                captured: captured.clone(),
            }),
        );

        let params = json!({
            "tool_name": "some_tool",
            "args": {"x": 1},
        });
        let _ = router
            .handle("tool-executor", "invoke", params)
            .await
            .unwrap();

        let calls = captured.lock().unwrap().clone();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "some_tool", "无注册表时应兜底为 tool_name");
    }

    /// 固定返回错误的 invoker（验证 tool-executor.invoke 的错误归一化）。
    struct ErroringInvoker;
    #[async_trait::async_trait]
    impl agentos_core::traits::PluginInvoker for ErroringInvoker {
        async fn invoke_pipeline_plugin(
            &self,
            _plugin_id: &str,
            _ctx: &agentos_core::types::PluginContext,
        ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
            Err(agentos_core::types::PluginError {
                message: "not used".into(),
                code: None,
                source: None,
            })
        }
        async fn invoke_tool(
            &self,
            _plugin_id: &str,
            _tool_name: &str,
            _inputs: &Value,
        ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError>
        {
            Err(agentos_core::types::PluginError {
                message: "sidecar crashed".into(),
                code: Some("MCP_TOOL_CALL_FAILED".into()),
                source: None,
            })
        }
        async fn send_lifecycle_hook(
            &self,
            _plugin_id: &str,
            _hook: agentos_core::traits::LifecycleHook,
            _context: &agentos_core::traits::HookContext,
        ) -> Result<(), agentos_core::types::PluginError> {
            Ok(())
        }
    }

    #[tokio::test]
    async fn test_tool_executor_invoke_error_normalized_to_failure_json() {
        // invoke_tool 返回 Err → capability 层归一化为 {"success": false, "error": ...}，
        // 不把内核 PluginError 泄漏给 sidecar。
        let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
            .with_invoker(Arc::new(ErroringInvoker));

        let res = router
            .handle(
                "tool-executor",
                "invoke",
                json!({"tool_name": "bash_execute", "args": {"command": "echo hi"}}),
            )
            .await
            .unwrap();
        assert_eq!(res["success"], false);
        assert!(res["error"].as_str().unwrap().contains("sidecar crashed"));
    }

    #[tokio::test]
    async fn test_tool_executor_missing_invoker_returns_protocol_error() {
        // 未注入 invoker → Protocol 错误（配置缺失早暴露）。
        let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new());
        let res = router
            .handle(
                "tool-executor",
                "invoke",
                json!({"tool_name": "bash_execute", "args": {}}),
            )
            .await;
        assert!(res.is_err());
        let err = res.unwrap_err();
        match err {
            agentos_mcp::McpError::Protocol { message } => {
                assert!(message.contains("未配置 invoker"), "got: {message}")
            }
            other => panic!("expected Protocol error, got {other:?}"),
        }
    }

    // ── M2：service-registry capability（用真实内存 SqliteStore 验证端到端）──

    /// 构造一个注入了内存 store 的路由器。
    fn router_with_store() -> KernelCapabilityRouter {
        let store: Arc<dyn StorageBackend> =
            Arc::new(agentos_engine::SqliteStore::open_memory().expect("open_memory"));
        KernelCapabilityRouter::new().with_store(store)
    }

    #[tokio::test]
    async fn test_service_registry_execution_records_roundtrip() {
        let router = router_with_store();
        // append 一条执行记录
        router
            .handle(
                "service-registry",
                "execution-records.append",
                json!({
                    "record_id": "rec_1",
                    "pipeline_run_id": "pipe_1",
                    "record_type": "ai",
                    "sequence": 0,
                    "iteration": 0,
                    "role": "assistant",
                    "content": "hello",
                    "created_at": "2026-08-01T00:00:00Z"
                }),
            )
            .await
            .unwrap();

        // count
        let c = router
            .handle(
                "service-registry",
                "execution-records.count",
                json!({"pipeline_run_id": "pipe_1"}),
            )
            .await
            .unwrap();
        assert_eq!(c["count"], 1);

        // list
        let rows = router
            .handle(
                "service-registry",
                "execution-records.list",
                json!({"pipeline_run_id": "pipe_1"}),
            )
            .await
            .unwrap();
        assert_eq!(rows.as_array().unwrap().len(), 1);
        assert_eq!(rows[0]["content"], "hello");

        // delete_by_session
        let d = router
            .handle(
                "service-registry",
                "execution-records.delete_by_session",
                json!({"pipeline_run_id": "pipe_1"}),
            )
            .await
            .unwrap();
        assert_eq!(d["deleted"], 1);
    }

    #[tokio::test]
    async fn test_service_registry_pipeline_summaries_roundtrip() {
        let router = router_with_store();
        router
            .handle(
                "service-registry",
                "pipeline-summaries.save",
                json!({
                    "run_id": "run_1",
                    "thread_id": "t1",
                    "total_iterations": 2,
                    "total_tokens": {"input_tokens": 50},
                    "total_seconds": 5.0,
                    "status": "completed",
                    "created_at": "2026-08-01T00:00:00Z"
                }),
            )
            .await
            .unwrap();

        // get
        let got = router
            .handle(
                "service-registry",
                "pipeline-summaries.get",
                json!({"run_id": "run_1"}),
            )
            .await
            .unwrap();
        assert_eq!(got["run_id"], "run_1");
        assert_eq!(got["total_tokens"]["input_tokens"], 50);

        // update（total_tokens 合并）
        router
            .handle(
                "service-registry",
                "pipeline-summaries.update",
                json!({"run_id": "run_1", "updates": {"status": "reviewed", "total_tokens": {"output_tokens": 30}}}),
            )
            .await
            .unwrap();
        let got2 = router
            .handle(
                "service-registry",
                "pipeline-summaries.get",
                json!({"run_id": "run_1"}),
            )
            .await
            .unwrap();
        assert_eq!(got2["status"], "reviewed");
        assert_eq!(got2["total_tokens"]["input_tokens"], 50);
        assert_eq!(got2["total_tokens"]["output_tokens"], 30);

        // list
        let listed = router
            .handle(
                "service-registry",
                "pipeline-summaries.list",
                json!({"limit": 10}),
            )
            .await
            .unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn test_service_registry_memory_roundtrip() {
        let router = router_with_store();
        router
            .handle(
                "service-registry",
                "memory.create",
                json!({
                    "id": "mem_1",
                    "content": "the quick brown fox",
                    "memory_type": "episode",
                    "tags": ["animal"],
                    "score": 0,
                    "created_at": "2026-08-01T00:00:00Z"
                }),
            )
            .await
            .unwrap();

        // get
        let got = router
            .handle("service-registry", "memory.get", json!({"id": "mem_1"}))
            .await
            .unwrap();
        assert_eq!(got["content"], "the quick brown fox");

        // search
        let found = router
            .handle(
                "service-registry",
                "memory.search",
                json!({"query": "fox", "top_k": 5}),
            )
            .await
            .unwrap();
        assert_eq!(found.as_array().unwrap().len(), 1);

        // delete
        let d = router
            .handle("service-registry", "memory.delete", json!({"id": "mem_1"}))
            .await
            .unwrap();
        assert_eq!(d["deleted"], true);
    }

    #[tokio::test]
    async fn test_service_registry_disabled_without_store() {
        // 不注入 store → service-registry 应返回错误
        let router = KernelCapabilityRouter::new();
        let res = router
            .handle("service-registry", "memory.get", json!({"id": "x"}))
            .await;
        assert!(
            res.is_err(),
            "service-registry must error when store not injected"
        );
    }

    #[tokio::test]
    async fn test_service_registry_unknown_method() {
        let router = router_with_store();
        let res = router
            .handle("service-registry", "bogus.op", json!({}))
            .await;
        assert!(
            res.is_err(),
            "unknown service-registry domain/op must error"
        );
    }

    // ── F-REVIEW-2：pipeline-executor.get_run_status（复盘轮询真实完成）──

    #[tokio::test]
    async fn test_pipeline_executor_get_run_status_ok() {
        // 建 run（模拟 start_run 后的 runs 表记录）→ get_run_status 返回状态
        let store: Arc<dyn StorageBackend> =
            Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let router = KernelCapabilityRouter::new().with_store(store.clone());
        store
            .create_run("run_status_1", "hash", "default")
            .await
            .unwrap();
        let res = router
            .handle(
                "pipeline-executor",
                "get_run_status",
                json!({"run_id": "run_status_1"}),
            )
            .await
            .unwrap();
        assert_eq!(res["run_id"], "run_status_1");
        assert_eq!(res["status"], "running", "新建 run 状态应为 running");
        // 更新为 completed 后再次查询应反映真实状态（复盘据此落 completed）
        store
            .update_run_status(
                "run_status_1",
                agentos_core::types::RunStatus::Completed,
                None,
                None,
            )
            .await
            .unwrap();
        let res2 = router
            .handle(
                "pipeline-executor",
                "get_run_status",
                json!({"run_id": "run_status_1"}),
            )
            .await
            .unwrap();
        assert_eq!(res2["status"], "completed");
        assert!(
            res2.get("ended_at").is_some(),
            "completed run 应有 ended_at"
        );
    }

    #[tokio::test]
    async fn test_pipeline_executor_get_run_status_errors() {
        // 无 store 注入 → 报错（与服务注册一致，不静默）
        let router = KernelCapabilityRouter::new();
        let res = router
            .handle(
                "pipeline-executor",
                "get_run_status",
                json!({"run_id": "x"}),
            )
            .await;
        assert!(res.is_err(), "store 未注入必须报错");

        // 缺 run_id 参数 → 报错
        let store: Arc<dyn StorageBackend> =
            Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let router2 = KernelCapabilityRouter::new().with_store(store.clone());
        let res2 = router2
            .handle("pipeline-executor", "get_run_status", json!({}))
            .await;
        assert!(res2.is_err(), "缺 run_id 必须报错");

        // run 不存在 → 报错（调用方降级为保持 running）
        let res3 = router2
            .handle(
                "pipeline-executor",
                "get_run_status",
                json!({"run_id": "ghost"}),
            )
            .await;
        assert!(res3.is_err(), "不存在的 run 必须报错");
    }

    // ── G5：config-reader.get 真实读取 ──

    async fn config_reader_roundtrip(
        reader: ConfigReaderFn,
        params: serde_json::Value,
    ) -> Result<serde_json::Value, agentos_mcp::McpError> {
        let router = KernelCapabilityRouter::new().with_config_reader(reader);
        router.handle("config-reader", "get", params).await
    }

    #[tokio::test]
    async fn g5_config_reader_returns_declared_value() {
        let reader: ConfigReaderFn = Arc::new(|_pid, key| {
            assert_eq!(key, "llm");
            Ok(json!({"model": "glm"}))
        });
        let out =
            config_reader_roundtrip(reader, json!({"key": "llm", "_plugin_id": "some_plugin"}))
                .await
                .unwrap();
        assert_eq!(out["value"]["model"], "glm");
    }

    #[tokio::test]
    async fn g5_config_reader_denies_undeclared_file_id() {
        let reader: ConfigReaderFn = Arc::new(|_pid, key| Err(format!("file_id '{}' 未声明", key)));
        let err = config_reader_roundtrip(reader, json!({"key": "secret", "_plugin_id": "p1"}))
            .await
            .unwrap_err();
        assert!(format!("{}", err).contains("拒绝"));
    }

    #[tokio::test]
    async fn g5_config_reader_requires_plugin_context() {
        let reader: ConfigReaderFn = Arc::new(|_, _| Ok(json!({})));
        let err = config_reader_roundtrip(reader, json!({"key": "llm"}))
            .await
            .unwrap_err();
        assert!(format!("{}", err).contains("_plugin_id"));
    }

    #[tokio::test]
    async fn g5_config_reader_without_reader_stays_noop() {
        let router = KernelCapabilityRouter::new();
        let out = router
            .handle(
                "config-reader",
                "get",
                json!({"key": "llm", "_plugin_id": "p1"}),
            )
            .await
            .unwrap();
        assert!(out["value"].is_null(), "未装配读取器保持 no-op 兜底");
    }

    // ── G6：granted_capabilities 白名单单点校验 ──

    #[tokio::test]
    async fn g6_granted_capability_allowed() {
        let lookup: GrantsLookupFn = Arc::new(|pid| {
            assert_eq!(pid, "p1");
            Some(vec!["event-bus".to_string()])
        });
        let router = KernelCapabilityRouter::new().with_grants_lookup(lookup);
        let out = router
            .handle(
                "event-bus",
                "emit",
                json!({"_plugin_id": "p1", "type": "x"}),
            )
            .await
            .unwrap();
        // event-bus.emit 返回 200-ish json（不因授权被拒即通过）。
        assert!(out.get("status").is_some() || !out.is_null());
    }

    #[tokio::test]
    async fn g6_ungranted_capability_denied() {
        let lookup: GrantsLookupFn = Arc::new(|_| Some(vec!["config-reader".to_string()]));
        let router = KernelCapabilityRouter::new().with_grants_lookup(lookup);
        let err = router
            .handle(
                "event-bus",
                "emit",
                json!({"_plugin_id": "p1", "type": "x"}),
            )
            .await
            .unwrap_err();
        assert!(
            format!("{}", err).contains("not granted"),
            "应被白名单拒绝: {}",
            err
        );
    }

    #[tokio::test]
    async fn g6_undeclared_grants_default_allow() {
        // 未声明 granted_capabilities（None）→ 默认全授予（存量兼容）。
        let lookup: GrantsLookupFn = Arc::new(|_| None);
        let router = KernelCapabilityRouter::new().with_grants_lookup(lookup);
        let out = router
            .handle(
                "event-bus",
                "emit",
                json!({"_plugin_id": "p1", "type": "x"}),
            )
            .await
            .unwrap();
        assert!(out.get("status").is_some() || !out.is_null());
    }

    #[tokio::test]
    async fn g6_no_lookup_no_check() {
        // 未装配授权查询器 → 不校验（旧装配兼容）。
        let router = KernelCapabilityRouter::new();
        let out = router
            .handle(
                "event-bus",
                "emit",
                json!({"_plugin_id": "p1", "type": "x"}),
            )
            .await
            .unwrap();
        assert!(out.get("status").is_some() || !out.is_null());
    }

    // ── G3：registry.register_tool 运行时动态注册 ──

    #[tokio::test]
    async fn g3_register_tool_calls_registrar() {
        use std::sync::Mutex;
        let captured: Arc<Mutex<Vec<(String, String)>>> = Arc::new(Mutex::new(Vec::new()));
        let cap2 = captured.clone();
        let registrar: DynamicToolRegistrar = Arc::new(move |pid, tool| {
            cap2.lock().unwrap().push((pid.to_string(), tool.name));
            Ok(())
        });
        let router = KernelCapabilityRouter::new().with_dynamic_tool_registrar(registrar);
        let out = router
            .handle(
                "registry",
                "register_tool",
                json!({
                    "_plugin_id": "connector",
                    "name": "dyn_query",
                    "description": "查询外部系统",
                    "input_schema": {"type": "object"},
                    "category": "search",
                }),
            )
            .await
            .unwrap();
        assert_eq!(out["status"], "registered");
        assert_eq!(out["plugin_id"], "connector");
        assert_eq!(
            *captured.lock().unwrap(),
            vec![("connector".into(), "dyn_query".into())]
        );
    }

    #[tokio::test]
    async fn g3_register_tool_requires_plugin_context() {
        let router = KernelCapabilityRouter::new();
        let err = router
            .handle("registry", "register_tool", json!({"name": "x"}))
            .await
            .unwrap_err();
        assert!(format!("{}", err).contains("_plugin_id"));
    }

    #[tokio::test]
    async fn g3_register_tool_requires_name() {
        let registrar: DynamicToolRegistrar = Arc::new(|_, _| Ok(()));
        let router = KernelCapabilityRouter::new().with_dynamic_tool_registrar(registrar);
        let err = router
            .handle("registry", "register_tool", json!({"_plugin_id": "p"}))
            .await
            .unwrap_err();
        assert!(format!("{}", err).contains("name"));
    }

    #[tokio::test]
    async fn g3_register_tool_without_registrar_errors() {
        let router = KernelCapabilityRouter::new();
        let err = router
            .handle(
                "registry",
                "register_tool",
                json!({"_plugin_id": "p", "name": "x"}),
            )
            .await
            .unwrap_err();
        assert!(format!("{}", err).contains("未装配"));
    }

    #[tokio::test]
    async fn g3_envelope_gate_applies_to_registry_namespace() {
        // 信封闸（G6 单点）：声明了白名单但不含 "registry" → 拒绝（信封二道闸验证）。
        let registrar: DynamicToolRegistrar = Arc::new(|_, _| Ok(()));
        let lookup: GrantsLookupFn = Arc::new(|_| Some(vec!["config-reader".to_string()]));
        let router = KernelCapabilityRouter::new()
            .with_dynamic_tool_registrar(registrar)
            .with_grants_lookup(lookup);
        let err = router
            .handle(
                "registry",
                "register_tool",
                json!({"_plugin_id": "p", "name": "x"}),
            )
            .await
            .unwrap_err();
        assert!(
            format!("{}", err).contains("not granted"),
            "信封闸应先于注册拒绝: {}",
            err
        );
    }

    #[tokio::test]
    async fn g3_envelope_grant_allows_registration() {
        // granted 含 "registry" → 信封闸放行,注册成功。
        let registrar: DynamicToolRegistrar = Arc::new(|_, _| Ok(()));
        let lookup: GrantsLookupFn = Arc::new(|_| Some(vec!["registry".to_string()]));
        let router = KernelCapabilityRouter::new()
            .with_dynamic_tool_registrar(registrar)
            .with_grants_lookup(lookup);
        let out = router
            .handle(
                "registry",
                "register_tool",
                json!({"_plugin_id": "p", "name": "x"}),
            )
            .await
            .unwrap();
        assert_eq!(out["status"], "registered");
    }
}
