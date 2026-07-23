//! Capability 路由器实现——处理 sidecar 插件反向调用内核能力。
//!
//! 持有引擎句柄，把 sidecar 的 `<capability>.<method>` 反向调用路由到内核实现：
//! - pipeline-executor.{suspend, resume, start_run} → AdrEngine
//! - event-bus.emit → 广播事件（当前记录日志，前端推送留 P1）
//! - config-reader.get → 读取配置节（从 AppState 配置缓存）
//!
//! [来源: ROADMAP.md 审批暂停/恢复、复盘调管道的前置地基]

use std::sync::Arc;

use async_trait::async_trait;
use agentos_core::traits::AdrEngine;
use agentos_mcp::{CapabilityRouter, McpError};
use serde_json::{json, Value};
use tracing::warn;

/// 管道执行能力错误码前缀。
const ERR_PIPELINE: i64 = -32010;

/// Capability 路由器实现。
pub struct KernelCapabilityRouter {
    /// 管道引擎（处理 pipeline-executor.* 调用）
    engine: Arc<dyn AdrEngine>,
}

impl KernelCapabilityRouter {
    /// 创建路由器。
    pub fn new(engine: Arc<dyn AdrEngine>) -> Self {
        Self { engine }
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

            // ── event-bus：发事件/通知（当前记录日志，前端推送留 P1-2 审批接线）──
            ("event-bus", "emit") => {
                let event_name = params
                    .get("event")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                // DEBT: 前端 WS 推送在 P1-2 审批闭环接线时实现。ceiling: 当前仅日志。
                // upgrade: 接入 AppState 的 WS 广播通道。
                tracing::info!(
                    target: "capability:event-bus",
                    "plugin event: {} payload={}",
                    event_name,
                    params.get("payload").unwrap_or(&serde_json::Value::Null)
                );
                Ok(json!({"status": "emitted", "event": event_name}))
            }

            // ── config-reader：读配置节（P1 后为显式 no-op fallback）──
            // task_11 P1 已把配置注入改到源头：manifest.config_files → invoker
            // build_injected_config 在 spawn sidecar 时下发，插件经 plugin.get_config()
            // 直接拿到自己的命名空间配置，不再需要反向调用 config-reader.get。
            // 本 capability 名仍是 SDK 公共契约（STANDARD_CAPABILITIES），故保留 no-op
            // 兜底（返回 null value），完整 capability 下线留 P6（config_refs 一并清理）。
            ("config-reader", "get") => {
                let key = params
                    .get("key")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                Ok(json!({"key": key, "value": null}))
            }

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

/// 抑制未使用的错误码常量警告（后续 event-bus 错误码扩展时启用）。
#[allow(dead_code)]
fn _pipeline_error_code() -> i64 {
    ERR_PIPELINE
}
