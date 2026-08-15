//! `chat` namespace capability handler——把"向会话投递消息并跑管道"暴露给 sidecar。
//!
//! 触发器（trigger_setup_tool）等 sidecar 到期触发时，经 `chat.send_message`
//! 复用前端同一条 WS 派发路径（`dispatch_user_input` → `process_via_engine`）：
//! 以触发消息为新一轮用户消息投给该会话 agent，agent 处理后流式回复前端。
//!
//! sidecar 光有展示通道（event-bus.emit 往前端推事件）不能唤醒 agent 跑一轮，
//! 还需注入通道——本 handler 即该通道：经
//! [`CapabilityHandlerRegistry`] 注册（router 优先查它），不新建传输、不动 router 结构，
//! 仅把内核既有的 `PipelineDispatcher::dispatch_user_input` 桥接成 sidecar 可达的能力。

use std::sync::Arc;

use async_trait::async_trait;
use serde_json::{json, Value};

use agentos_mcp::{CapabilityHandler, McpError};
use agentos_session::router::PipelineDispatcher;

/// `chat` namespace handler：sidecar → 投递消息到会话并跑管道。
///
/// 持有内核 WS 派发器（`EngineDispatcher` 实现的 `PipelineDispatcher`），与前端
/// 发消息走完全相同的链路（tenant 解析 / route_id 解析 / stream_start / 引擎执行 /
/// new_message），保证触发消息和用户手发的消息行为一致。
pub struct ChatSendHandler {
    dispatcher: Arc<dyn PipelineDispatcher>,
}

impl ChatSendHandler {
    /// 用内核 WS 派发器构造。
    pub fn new(dispatcher: Arc<dyn PipelineDispatcher>) -> Self {
        Self { dispatcher }
    }
}

#[async_trait]
impl CapabilityHandler for ChatSendHandler {
    fn namespace(&self) -> &str {
        "chat"
    }

    async fn handle(&self, method: &str, params: Value) -> Result<Value, McpError> {
        match method {
            "send_message" => {
                let pipeline_id = params
                    .get("pipeline_id")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty())
                    .ok_or_else(|| McpError::Protocol {
                        message: "chat.send_message 缺少 pipeline_id 参数".to_string(),
                    })?;
                let message = params
                    .get("message")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty())
                    .ok_or_else(|| McpError::Protocol {
                        message: "chat.send_message 缺少 message 参数".to_string(),
                    })?;
                let user_id = params
                    .get("user_id")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty())
                    .ok_or_else(|| McpError::Protocol {
                        message: "chat.send_message 缺少 user_id 参数".to_string(),
                    })?;
                // 任务级 execution_context（可选）：任务执行器从 task.metadata 组装
                // （workspace_mode/isolation_level 等），随消息派发并入 initial_state，
                // init 体 workspace_lifecycle / environment_lifecycle 插件消费。
                let execution_context = params.get("execution_context").filter(|v| v.is_object());

                tracing::info!(
                    target: "capability:chat",
                    pipeline = %pipeline_id,
                    user = %user_id,
                    msg_len = message.len(),
                    has_execution_context = execution_context.is_some(),
                    "chat.send_message 派发触发消息"
                );

                // 复用 WS 派发：主会话下 thread_id 与 pipeline_id 同值（effective_pipeline_id），
                // dispatch_user_input 内部会 resolve 真实 route_id 并发 stream_start →
                // process_via_engine → new_message，前端按既有协议流式渲染回复。
                // tenant 由 dispatch_user_input 用 user_id 反查（与 WS 路径同源）。
                // thinking_strength：HTTP 通道暂不携带（"" = 引擎不覆盖参数）。
                self.dispatcher
                    .dispatch_user_input(
                        pipeline_id,
                        user_id,
                        message,
                        pipeline_id,
                        "",
                        execution_context,
                    )
                    .await
                    .map(|_| json!({"status": "dispatched", "pipeline_id": pipeline_id}))
                    .map_err(|e| McpError::Protocol {
                        message: format!("chat.send_message 派发失败: {e}"),
                    })
            }
            other => Err(McpError::Protocol {
                message: format!("capability method not implemented: chat.{other}"),
            }),
        }
    }
}
