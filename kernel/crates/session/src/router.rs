//! 入站路由——user_input/interaction_response/stop_generation 分发（ADR §7.2）。
//!
//! 参考 0.1 `app_factory.py:320,431,486`。WS 入口收到消息后按 `type` 路由：
//! - `user_input` → dispatch_user_input（转发到管道引擎）
//! - `interaction_response` → dispatch_interaction_response（回复人工交互）
//! - `stop_generation` → dispatch_stop（取消生成）
//! - `active_thread_changed` → dispatch_active_thread（切换选中会话，排队优先级键）
//! - `heartbeat` → 心跳确认（不转发）
//! - 其余 → 忽略

use std::sync::Arc;

use agentos_core::types::PendingInputSource;
use async_trait::async_trait;
use serde_json::Value;

/// 管道分发器——session crate 通过此 trait 把入站消息交给引擎层（api crate 实现）。
///
/// 解耦设计：router 不直接依赖 engine，便于测试用 mock 验证路由正确性。
#[async_trait]
pub trait PipelineDispatcher: Send + Sync {
    /// 转发用户输入到管道引擎。
    ///
    /// pipeline_id 是前端消息路由键（后端创建会话时回填），引擎回推流式事件时
    /// 用它定位前端占位气泡。缺失时引擎可回退 thread_id。
    ///
    /// thinking_strength 是前端思考强度（off/low/medium/high，空串=未指定），
    /// 注入引擎初始状态后由 llm_core 路由到具体模型参数（temperature/max_tokens/
    /// reasoning_effort）。
    ///
    /// state_overlay 是自由 state 注入（GAP-1：chat.send_message 的 `state`
    /// 参数 + 引擎写入的 lineage 扁平键），在 execution_context 合并点之后并入
    /// initial_state 顶层扁平键；WS 前端路径不携带（None）。
    ///
    /// agent_id 指定执行管道加载的 agent 配置（config/agents/**/<id>.yaml，
    /// 决定人格/tool_ids/技能）。任务派发按 target 选 agent；WS 主会话路径
    /// 传空串 = 未指定，由 dispatcher 实现侧按线程绑定解析
    /// （registry → DB sessions.agent_id → 默认 "agentos"，2026-08-24 阶段1）。
    ///
    /// client_message_id 是前端幂等键（ADR 2026-08-21）：随 user 消息
    /// metadata 落库并在 GET messages 回显，前端据此对账去重乐观消息。
    /// 空串 = 无幂等键（触发器注入/旧客户端）。
    ///
    /// source 是 pending 输入来源标注（ADR-2026-08-26）：前端=User、
    /// 触发器=Trigger、任务派发=Task、HTTP=Http、系统=System。入队持久化，
    /// 前端队列条据此标注来源。
    #[allow(clippy::too_many_arguments)]
    async fn dispatch_user_input(
        &self,
        thread_id: &str,
        user_id: &str,
        content: &str,
        pipeline_id: &str,
        thinking_strength: &str,
        execution_context: Option<&serde_json::Value>,
        state_overlay: Option<&serde_json::Value>,
        agent_id: &str,
        client_message_id: &str,
        source: PendingInputSource,
    ) -> Result<(), String>;

    /// 转发人工交互响应（审批/选择）。
    ///
    /// `response` 为前端回传的响应体（response_type/selected_option/answers/feedback），
    /// 由实现转发到交互插件的 interaction.respond 以唤醒 wait_for_choice。
    async fn dispatch_interaction_response(
        &self,
        thread_id: &str,
        request_id: &str,
        response: &Value,
    ) -> Result<(), String>;

    /// 取消指定 thread 的生成。
    async fn dispatch_stop(&self, thread_id: &str) -> Result<(), String>;

    /// 前端通知当前选中会话切换（排队优先级策略键）。
    ///
    /// 内核据此把该用户的活跃管道更新为当前选中的主管道——全局并发闸门有
    /// 排队时活跃管道优先获得槽位。默认 no-op（测试 mock 无需关心）。
    async fn dispatch_active_thread(
        &self,
        _user_id: &str,
        _thread_id: &str,
        _pipeline_id: &str,
    ) -> Result<(), String> {
        Ok(())
    }
}

/// 路由结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RouteOutcome {
    /// 已分发处理。
    Handled,
    /// 心跳（调用方应回 heartbeat_ack）。
    Heartbeat,
    /// 未知类型，忽略。
    Ignored,
    /// 处理出错（含错误信息）。
    Error(String),
}

/// 入站消息路由器。
pub struct InboundRouter {
    dispatcher: Arc<dyn PipelineDispatcher>,
}

impl InboundRouter {
    /// 用指定分发器创建。
    pub fn new(dispatcher: Arc<dyn PipelineDispatcher>) -> Self {
        Self { dispatcher }
    }

    /// 解析原始 JSON 文本并路由。
    pub async fn route_raw(&self, raw: &str, user_id: &str) -> RouteOutcome {
        let msg: Value = match serde_json::from_str(raw) {
            Ok(v) => v,
            Err(e) => return RouteOutcome::Error(format!("invalid json: {e}")),
        };
        self.route(&msg, user_id).await
    }

    /// 路由已解析的消息。
    pub async fn route(&self, msg: &Value, user_id: &str) -> RouteOutcome {
        let msg_type = msg.get("type").and_then(|v| v.as_str()).unwrap_or("");
        match msg_type {
            "heartbeat" => RouteOutcome::Heartbeat,
            "user_input" => self.route_user_input(msg, user_id).await,
            "interaction_response" => self.route_interaction(msg).await,
            "stop_generation" => self.route_stop(msg).await,
            "active_thread_changed" => self.route_active_thread(msg, user_id).await,
            _ => RouteOutcome::Ignored,
        }
    }

    async fn route_user_input(&self, msg: &Value, user_id: &str) -> RouteOutcome {
        let thread_id = match msg.get("thread_id").and_then(|v| v.as_str()) {
            Some(t) if !t.is_empty() => t.to_string(),
            _ => return RouteOutcome::Error("user_input 缺少 thread_id".into()),
        };
        // content 兼容两种位置：
        // - data.content：未来标准契约（WS 消息 body 统一收进 data 信封）
        // - 顶层 content：前端 GlobalWebSocket.sendUserInput 现状（content 放顶层）
        // 两端对齐前，读 data.content 失败时回退顶层 content，避免取到空串导致
        // LLM 报「未正常接收到 prompt 参数」。
        let content = msg
            .get("data")
            .and_then(|d| d.get("content"))
            .and_then(|c| c.as_str())
            .or_else(|| msg.get("content").and_then(|c| c.as_str()))
            .unwrap_or("")
            .to_string();
        // pipeline_id：前端消息路由键，兼容顶层和 data.content 两种位置
        // （与 content 同理）。引擎回推流式事件时用它定位前端占位气泡。
        let pipeline_id = msg
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .or_else(|| {
                msg.get("data")
                    .and_then(|d| d.get("pipeline_id"))
                    .and_then(|v| v.as_str())
            })
            .unwrap_or("")
            .to_string();
        // thinking_strength：思考强度（off/low/medium/high），顶层优先、
        // data 信封兜底（与 pipeline_id 同法）。缺失为空串 = 引擎不覆盖参数。
        let thinking_strength = msg
            .get("thinking_strength")
            .and_then(|v| v.as_str())
            .or_else(|| {
                msg.get("data")
                    .and_then(|d| d.get("thinking_strength"))
                    .and_then(|v| v.as_str())
            })
            .unwrap_or("")
            .to_string();
        // client_message_id：前端幂等键（ADR 2026-08-21 消息幂等契约）。
        // 随 user 消息 metadata 落库并在 GET messages 回显，前端据此把乐观
        // 消息与权威记录对账去重。顶层优先、data 信封兜底（与 pipeline_id
        // 同法）。缺失为空串 = 无幂等键（触发器注入/旧客户端路径）。
        let client_message_id = msg
            .get("client_message_id")
            .and_then(|v| v.as_str())
            .or_else(|| {
                msg.get("data")
                    .and_then(|d| d.get("client_message_id"))
                    .and_then(|v| v.as_str())
            })
            .unwrap_or("")
            .to_string();
        match self
            .dispatcher
            .dispatch_user_input(
                &thread_id,
                user_id,
                &content,
                &pipeline_id,
                &thinking_strength,
                None,
                None,
                // 空串 = 未指定，agent 解析归 dispatcher
                // 实现侧（线程绑定 registry → DB sessions.agent_id → agentos）。
                // 硬编码 "agentos" 会使会话编辑切换的绑定成为纯展示字段。
                "",
                &client_message_id,
                // WS 入口即前端发送（ADR-2026-08-26 来源标注）。
                agentos_core::types::PendingInputSource::User,
            )
            .await
        {
            Ok(()) => RouteOutcome::Handled,
            Err(e) => RouteOutcome::Error(e),
        }
    }

    async fn route_interaction(&self, msg: &Value) -> RouteOutcome {
        let thread_id = match msg.get("thread_id").and_then(|v| v.as_str()) {
            Some(t) if !t.is_empty() => t.to_string(),
            _ => return RouteOutcome::Error("interaction_response 缺少 thread_id".into()),
        };
        let request_id = msg
            .get("data")
            .and_then(|d| d.get("request_id"))
            .and_then(|r| r.as_str())
            .unwrap_or("")
            .to_string();
        // 前端把响应体放 data.response（response_type/selected_option/feedback），整体透传。
        let response = msg
            .get("data")
            .and_then(|d| d.get("response"))
            .cloned()
            .unwrap_or(Value::Null);
        match self
            .dispatcher
            .dispatch_interaction_response(&thread_id, &request_id, &response)
            .await
        {
            Ok(()) => RouteOutcome::Handled,
            Err(e) => RouteOutcome::Error(e),
        }
    }

    async fn route_stop(&self, msg: &Value) -> RouteOutcome {
        let thread_id = match msg.get("thread_id").and_then(|v| v.as_str()) {
            Some(t) if !t.is_empty() => t.to_string(),
            _ => return RouteOutcome::Error("stop_generation 缺少 thread_id".into()),
        };
        match self.dispatcher.dispatch_stop(&thread_id).await {
            Ok(()) => RouteOutcome::Handled,
            Err(e) => RouteOutcome::Error(e),
        }
    }

    /// active_thread_changed——用户切换当前选中的会话。
    ///
    /// pipeline_id 兼容顶层与 data 信封两处（与 user_input 同法）；缺省时由
    /// dispatcher 侧回退该 thread 的主管道。
    async fn route_active_thread(&self, msg: &Value, user_id: &str) -> RouteOutcome {
        let thread_id = match msg.get("thread_id").and_then(|v| v.as_str()) {
            Some(t) if !t.is_empty() => t.to_string(),
            _ => return RouteOutcome::Error("active_thread_changed 缺少 thread_id".into()),
        };
        let pipeline_id = msg
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .or_else(|| {
                msg.get("data")
                    .and_then(|d| d.get("pipeline_id"))
                    .and_then(|v| v.as_str())
            })
            .unwrap_or("")
            .to_string();
        match self
            .dispatcher
            .dispatch_active_thread(user_id, &thread_id, &pipeline_id)
            .await
        {
            Ok(()) => RouteOutcome::Handled,
            Err(e) => RouteOutcome::Error(e),
        }
    }
}
