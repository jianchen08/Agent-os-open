//! Capability 路由器实现——处理 sidecar 插件反向调用内核能力。
//!
//! 持有引擎句柄，把 sidecar 的 `<capability>.<method>` 反向调用路由到内核实现：
//! - pipeline-executor.{suspend, resume, start_run} → AdrEngine
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

use async_trait::async_trait;
use agentos_core::traits::{AdrEngine, CapabilityRegistry, MessageQueryOpts, StorageBackend};
use agentos_core::types::{ExecutionRecord, MemoryRecord, PipelineRunSummary};
use agentos_mcp::{CapabilityRouter, McpError};
use serde_json::{json, Value};
use tracing::warn;

use crate::metrics::{Labels, MetricType, MetricsAggregator};

/// 管道执行能力错误码前缀。
const ERR_PIPELINE: i64 = -32010;

/// service-registry 能力错误码前缀（基础设施存储调用）。
const ERR_SERVICE_REGISTRY: i64 = -32020;

/// Capability 路由器实现。
pub struct KernelCapabilityRouter {
    /// 管道引擎（处理 pipeline-executor.* 调用）
    engine: Arc<dyn AdrEngine>,
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
}

impl KernelCapabilityRouter {
    /// 创建路由器（不带指标聚合器，兼容旧调用方）。
    pub fn new(engine: Arc<dyn AdrEngine>) -> Self {
        Self {
            engine,
            metrics: None,
            invoker: None,
            registry: None,
            session: None,
            store: None,
        }
    }

    /// 创建带指标聚合器的路由器（生产用，启用 metrics.record 反向调用）。
    pub fn with_metrics(engine: Arc<dyn AdrEngine>, metrics: MetricsAggregator) -> Self {
        Self {
            engine,
            metrics: Some(metrics),
            invoker: None,
            registry: None,
            session: None,
            store: None,
        }
    }

    /// 注入插件调用器（启用 tool-executor.invoke 反向调用）。
    pub fn with_invoker(mut self, invoker: Arc<dyn agentos_core::traits::PluginInvoker>) -> Self {
        self.invoker = Some(invoker);
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
}

#[async_trait]
impl CapabilityRouter for KernelCapabilityRouter {
    async fn handle(
        &self,
        capability: &str,
        method: &str,
        params: Value,
    ) -> Result<Value, McpError> {
        match (capability, method) {
            // ── pipeline-executor：暂停/恢复/启动管道 ──
            ("pipeline-executor", "suspend") => {
                let run_id = params
                    .get("run_id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| McpError::Protocol {
                        message: "suspend 缺少 run_id 参数".to_string(),
                    })?;
                let handle = self.engine.suspend(run_id).await.map_err(|e| McpError::Protocol {
                    message: format!("suspend 失败: {e}"),
                })?;
                // 返回完整 handle，sidecar resume 时需回传全部字段
                Ok(json!({
                    "status": "suspended",
                    "run_id": handle.run_id,
                    "branch_id": handle.branch_id,
                    "seq": handle.seq,
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
                let handle = agentos_core::types::SuspendHandle {
                    run_id: run_id.to_string(),
                    branch_id: params
                        .get("branch_id")
                        .and_then(|v| v.as_str())
                        .unwrap_or("main")
                        .to_string(),
                    seq: params
                        .get("seq")
                        .and_then(|v| v.as_u64())
                        .unwrap_or(0) as u32,
                };
                self.engine
                    .resume(&handle, agentos_core::types::WakeEvent::Manual)
                    .await
                    .map_err(|e| McpError::Protocol {
                        message: format!("resume 失败: {e}"),
                    })?;
                Ok(json!({"status": "resumed", "run_id": run_id}))
            }
            ("pipeline-executor", "start_run") => {
                let run_id = self
                    .engine
                    .start_run(&params)
                    .await
                    .map_err(|e| McpError::Protocol {
                        message: format!("start_run 失败: {e}"),
                    })?;
                Ok(json!({"status": "started", "run_id": run_id}))
            }

            // ── event-bus：发事件/通知。流式 chunk 推送的核心出口 ──
            // sidecar（如 llm_core）每生成一个 chunk 就 notify 一次 event-bus.emit，
            // 内核收到后调 session.emit_stream 把 chunk 推到前端 WS。
            ("event-bus", "emit") => {
                let event_name = params
                    .get("event")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                let payload = params.get("payload").cloned().unwrap_or(serde_json::Value::Null);
                tracing::debug!(target: "capability:event-bus", event = %event_name, "收到 event-bus.emit");

                // 流式事件族：stream_chunk / thinking_start / thinking_chunk / thinking_end
                // 对齐 0.1 协议（bridge_events._make_event）：信封必须含 pipeline_id +
                // message_id，否则前端 resolvePipelineId/extractMessageId 失败丢弃。
                // thinking 系列让前端渲染思考卡片（thinkingHandler.ts）。
                let stream_events = ["stream_chunk", "thinking_start", "thinking_chunk", "thinking_end"];
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
                        let needs_content = event_name == "stream_chunk" || event_name == "thinking_chunk";
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
                                obj.insert("pipeline_id".to_string(), serde_json::Value::String(pipeline_id.to_string()));
                                obj.insert("message_id".to_string(), serde_json::Value::String(message_id.to_string()));
                                obj.insert("_threadId".to_string(), serde_json::Value::String(thread_id.to_string()));
                            }
                            let _ = session.emit_event(thread_id, event_name, data).await;
                        }
                    }
                    return Ok(json!({"status": "emitted", "event": event_name}));
                }

                // 其他 event 暂只记日志（审批/通知等留后续）
                tracing::debug!(
                    target: "capability:event-bus",
                    "plugin event: {} payload={}",
                    event_name, payload
                );
                Ok(json!({"status": "emitted", "event": event_name}))
            }

            // ── config-reader：读配置节（P1 后为显式 no-op fallback）──
            // task_11 P1 已把配置注入改到源头：manifest.config_files → invoker
            // build_injected_config 在 spawn sidecar 时下发，插件经 plugin.get_config()
            // 直接拿到自己的命名空间配置，不再需要反向调用 config-reader.get。
            // 本 capability 名仍是 SDK 公共契约（STANDARD_CAPABILITIES），故保留 no-op
            // 兜底（返回 null value）。config_refs 已于 P6 删除，配置只走 config_files。
            ("config-reader", "get") => {
                let key = params
                    .get("key")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                Ok(json!({"key": key, "value": null}))
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
                let name = params
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
                let tool_args = params.get("args").cloned().unwrap_or(json!({}));
                // 从 CapabilityRegistry 反查 tool_name → plugin_id（tool 插件的 manifest id）
                let plugin_id = self
                    .registry
                    .as_ref()
                    .and_then(|r| r.get_tool(tool_name))
                    .map(|td| td.plugin_id.clone())
                    .unwrap_or_else(|| tool_name.to_string());
                let invoker = self.invoker.as_ref().ok_or_else(|| McpError::Protocol {
                    message: "tool-executor 未配置 invoker".to_string(),
                })?;
                match invoker.invoke_tool(&plugin_id, tool_name, &tool_args).await {
                    Ok(result) => Ok(serde_json::to_value(result).unwrap_or_else(|_| json!({"success": false}))),
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

            // tenant-context / logger 暂未实现具体 method（P0-4 多租户时补 tenant-context）
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
                Ok(got.map(|s| serde_json::to_value(s).unwrap_or(Value::Null))
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
                let limit = params.get("limit").and_then(|v| v.as_u64()).map(|l| l as usize);
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
                Ok(got.map(|m| serde_json::to_value(m).unwrap_or(Value::Null))
                    .unwrap_or(Value::Null))
            }
            ("memory", "list") => {
                let memory_type = params.get("memory_type").and_then(|v| v.as_str());
                let limit = params
                    .get("limit")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(20) as usize;
                let offset = params
                    .get("offset")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(0) as usize;
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
                let top_k = params
                    .get("top_k")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(5) as usize;
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
                message: format!("label value contains forbidden char (newline/dquote) for key: {k}"),
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
    use agentos_core::traits::AdrEngine;
    use agentos_core::types::{CompositeStep, EngineError, StepResult, SuspendHandle, WakeEvent};
    use serde_json::json;

    /// 不做任何事的 AdrEngine mock（metrics.record 测试不需要引擎）。
    struct StubEngine;
    #[async_trait]
    impl AdrEngine for StubEngine {
        async fn start_run(&self, _c: &Value) -> Result<String, EngineError> {
            Ok("stub".to_string())
        }
        async fn execute_step(
            &self,
            _: &str,
            _: &CompositeStep,
        ) -> Result<StepResult, EngineError> {
            unimplemented!()
        }
        async fn suspend(&self, _: &str) -> Result<SuspendHandle, EngineError> {
            unimplemented!()
        }
        async fn resume(&self, _: &SuspendHandle, _: WakeEvent) -> Result<(), EngineError> {
            unimplemented!()
        }
        async fn rollback(&self, _: &str, _: u32) -> Result<String, EngineError> {
            unimplemented!()
        }
        async fn end_run(&self, _: &str) -> Result<(), EngineError> {
            Ok(())
        }
    }

    fn router_with_metrics() -> (KernelCapabilityRouter, MetricsAggregator) {
        let agg = MetricsAggregator::new();
        let r = KernelCapabilityRouter::with_metrics(Arc::new(StubEngine), agg.clone());
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
        let router = KernelCapabilityRouter::new(Arc::new(StubEngine));
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
        KernelCapabilityRouter::with_metrics(Arc::new(StubEngine), agg)
            .with_session(coord)
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

    // ── M2：service-registry capability（用真实内存 SqliteStore 验证端到端）──

    /// 构造一个注入了内存 store 的路由器。
    fn router_with_store() -> KernelCapabilityRouter {
        let store: Arc<dyn StorageBackend> = Arc::new(
            agentos_engine::SqliteStore::open_memory().expect("open_memory"),
        );
        KernelCapabilityRouter::new(Arc::new(StubEngine)).with_store(store)
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
        let router = KernelCapabilityRouter::new(Arc::new(StubEngine));
        let res = router
            .handle(
                "service-registry",
                "memory.get",
                json!({"id": "x"}),
            )
            .await;
        assert!(res.is_err(), "service-registry must error when store not injected");
    }

    #[tokio::test]
    async fn test_service_registry_unknown_method() {
        let router = router_with_store();
        let res = router
            .handle("service-registry", "bogus.op", json!({}))
            .await;
        assert!(res.is_err(), "unknown service-registry domain/op must error");
    }
}
