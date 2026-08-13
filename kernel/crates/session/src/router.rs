//! 入站路由——user_input/interaction_response/stop_generation 分发（ADR §7.2）。
//!
//! 参考 0.1 `app_factory.py:320,431,486`。WS 入口收到消息后按 `type` 路由：
//! - `user_input` → dispatch_user_input（转发到管道引擎）
//! - `interaction_response` → dispatch_interaction_response（回复人工交互）
//! - `stop_generation` → dispatch_stop（取消生成）
//! - `heartbeat` → 心跳确认（不转发）
//! - 其余 → 忽略

use std::sync::Arc;

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
    async fn dispatch_user_input(
        &self,
        thread_id: &str,
        user_id: &str,
        content: &str,
        pipeline_id: &str,
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
            .or_else(|| msg.get("data").and_then(|d| d.get("pipeline_id")).and_then(|v| v.as_str()))
            .unwrap_or("")
            .to_string();
        match self
            .dispatcher
            .dispatch_user_input(&thread_id, user_id, &content, &pipeline_id)
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
}
