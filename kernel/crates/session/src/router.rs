//! 入站路由——user_input/interaction_response/stop_generation/regenerate 分发（ADR §7.2）。
//!
//! 参考 0.1 `app_factory.py:320,431,486`。WS 入口收到消息后按 `type` 路由：
//! - `user_input` → dispatch_user_input（转发到管道引擎）
//! - `interaction_response` → dispatch_interaction_response（回复人工交互）
//! - `stop_generation` → dispatch_stop（取消生成）
//! - `regenerate` → dispatch_regenerate（重新生成/回退/编辑重发，批次 D）
//! - `active_thread_changed` → dispatch_active_thread（切换选中会话，排队优先级键）
//! - `heartbeat` → 心跳确认（不转发）
//! - 其余 → 忽略

use std::sync::Arc;

use agentos_core::types::PendingInputSource;
use async_trait::async_trait;
use serde_json::Value;

/// 顶层与 data 信封两处取字符串字段：顶层优先、data 信封兜底
/// （WS 消息 body 统一收进 data 信封是目标契约，现状前端部分字段仍放顶层）。
/// 任意一处存在但非字符串 → 视为缺失（as_str 过滤），与各调用点原提取链一致。
fn field_or_data<'a>(msg: &'a Value, key: &str) -> Option<&'a str> {
    msg.get(key).and_then(|v| v.as_str()).or_else(|| {
        msg.get("data")
            .and_then(|d| d.get(key))
            .and_then(|v| v.as_str())
    })
}

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
    ///
    /// pipeline_id 是停止目标管道（一切管道相关操作必须携带管道 ID）：
    /// 前端正在查看的管道（含子任务管道）即停止目标；空串 = 旧客户端，
    /// 由实现侧回退 thread 主管道。
    async fn dispatch_stop(&self, thread_id: &str, pipeline_id: &str) -> Result<(), String>;

    /// 重新生成/回退/编辑重发（批次 D 原语）。
    ///
    /// 定位目标 user 消息 →（可选）set 改写内容 → 对其后全部槽位发 set(seq,null)
    /// 截断 → 追加 patch_type='rollback' trace → 以显式 skip_user_append 标志重跑。
    /// - user_message_id 缺省 = 最后一条 user 消息（重新生成）；
    /// - 指定更早 user = 回退；带 new_content = 编辑重发。
    /// pipeline_id 是消息路由键（同 user_input）；缺省由实现侧回退 thread 主管道。
    /// 默认 no-op（测试 mock 无需关心）。
    async fn dispatch_regenerate(
        &self,
        _user_id: &str,
        _thread_id: &str,
        _pipeline_id: &str,
        _user_message_id: &str,
        _new_content: Option<&str>,
    ) -> Result<(), String> {
        Ok(())
    }

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
            "regenerate" => self.route_regenerate(msg, user_id).await,
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
        let pipeline_id = field_or_data(msg, "pipeline_id").unwrap_or("").to_string();
        // thinking_strength：思考强度（off/low/medium/high），顶层优先、
        // data 信封兜底（与 pipeline_id 同法）。缺失为空串 = 引擎不覆盖参数。
        let thinking_strength = field_or_data(msg, "thinking_strength")
            .unwrap_or("")
            .to_string();
        // client_message_id：前端幂等键（ADR 2026-08-21 消息幂等契约）。
        // 随 user 消息 metadata 落库并在 GET messages 回显，前端据此把乐观
        // 消息与权威记录对账去重。顶层优先、data 信封兜底（与 pipeline_id
        // 同法）。缺失为空串 = 无幂等键（触发器注入/旧客户端路径）。
        let client_message_id = field_or_data(msg, "client_message_id")
            .unwrap_or("")
            .to_string();
        // execution_context：消息级执行上下文（{workspace:{source_path,mode},
        // isolation:{level}}），会话执行选项编辑后的最新值随消息生效——引擎合并
        // 点（1a2）优先于 thread metadata 会话级注入。顶层优先、data 信封兜底；
        // 缺失为 None = 后端按出生值注入（与旧客户端行为一致）。state_overlay
        // 仍仅服务端内部路径使用，WS 入站不收。
        let execution_context = msg
            .get("data")
            .and_then(|d| d.get("execution_context"))
            .filter(|v| v.is_object())
            .or_else(|| msg.get("execution_context").filter(|v| v.is_object()));
        match self
            .dispatcher
            .dispatch_user_input(
                &thread_id,
                user_id,
                &content,
                &pipeline_id,
                &thinking_strength,
                execution_context,
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
        let pipeline_id = field_or_data(msg, "pipeline_id")
            .unwrap_or_default()
            .to_string();
        match self
            .dispatcher
            .dispatch_stop(&thread_id, &pipeline_id)
            .await
        {
            Ok(()) => RouteOutcome::Handled,
            Err(e) => RouteOutcome::Error(e),
        }
    }

    /// regenerate——重新生成/回退/编辑重发（批次 D）。
    ///
    /// user_message_id 定位目标 user 消息（缺省=最后一条=重新生成，由实现侧
    /// 解析）；new_content 非空 = 编辑重发（改写该消息内容后重跑）；pipeline_id
    /// 同 user_input 兼容顶层与 data 信封两处（缺省由实现侧回退 thread 主管道）。
    async fn route_regenerate(&self, msg: &Value, user_id: &str) -> RouteOutcome {
        let thread_id = match msg.get("thread_id").and_then(|v| v.as_str()) {
            Some(t) if !t.is_empty() => t.to_string(),
            _ => return RouteOutcome::Error("regenerate 缺少 thread_id".into()),
        };
        let pipeline_id = field_or_data(msg, "pipeline_id")
            .unwrap_or_default()
            .to_string();
        let user_message_id = field_or_data(msg, "user_message_id")
            .unwrap_or_default()
            .to_string();
        // 缺失 = None（重新生成最后一条）；存在但非字符串同样按缺失处理（原提取链同此语义）
        let new_content = field_or_data(msg, "new_content");
        match self
            .dispatcher
            .dispatch_regenerate(
                user_id,
                &thread_id,
                &pipeline_id,
                &user_message_id,
                new_content,
            )
            .await
        {
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

#[cfg(test)]
mod user_input_ec_tests {
    //! route_user_input 的 execution_context 透传：消息级执行上下文（会话
    //! 执行选项编辑后的最新值）随前端 user_input 到达 dispatcher——顶层与
    //! data 信封两处解析，缺失为 None（后端回退 thread metadata 出生值）。

    use super::*;
    use serde_json::json;
    use std::sync::Mutex;

    #[derive(Default)]
    struct RecordingDispatcher {
        last_execution_context: Mutex<Option<Value>>,
    }

    #[async_trait]
    impl PipelineDispatcher for RecordingDispatcher {
        async fn dispatch_user_input(
            &self,
            _thread_id: &str,
            _user_id: &str,
            _content: &str,
            _pipeline_id: &str,
            _thinking_strength: &str,
            execution_context: Option<&Value>,
            _state_overlay: Option<&Value>,
            _agent_id: &str,
            _client_message_id: &str,
            _source: PendingInputSource,
        ) -> Result<(), String> {
            *self.last_execution_context.lock().unwrap() = execution_context.cloned();
            Ok(())
        }

        async fn dispatch_interaction_response(
            &self,
            _thread_id: &str,
            _request_id: &str,
            _response: &Value,
        ) -> Result<(), String> {
            Ok(())
        }

        async fn dispatch_stop(&self, _thread_id: &str, _pipeline_id: &str) -> Result<(), String> {
            Ok(())
        }
    }

    fn router() -> (InboundRouter, Arc<RecordingDispatcher>) {
        let d = Arc::new(RecordingDispatcher::default());
        (InboundRouter::new(d.clone()), d)
    }

    fn base_msg() -> Value {
        json!({
            "type": "user_input",
            "thread_id": "thread-abc123",
            "content": "hi",
            "pipeline_id": "p1",
        })
    }

    #[tokio::test]
    async fn top_level_execution_context_is_forwarded() {
        let (r, d) = router();
        let mut msg = base_msg();
        msg["execution_context"] = json!({
            "workspace": { "source_path": "D:/proj/demo", "mode": "worktree" },
            "isolation": { "level": "isolated" },
        });
        assert_eq!(r.route(&msg, "u1").await, RouteOutcome::Handled);
        let got = d.last_execution_context.lock().unwrap().clone();
        assert_eq!(
            got.as_ref().and_then(|v| v.get("workspace")).cloned(),
            Some(json!({ "source_path": "D:/proj/demo", "mode": "worktree" }))
        );
    }

    #[tokio::test]
    async fn data_envelope_execution_context_is_forwarded() {
        let (r, d) = router();
        let mut msg = base_msg();
        msg["data"] = json!({ "execution_context": { "isolation": { "level": "non_isolated" } } });
        assert_eq!(r.route(&msg, "u1").await, RouteOutcome::Handled);
        let got = d.last_execution_context.lock().unwrap().clone();
        assert_eq!(
            got.as_ref().and_then(|v| v.get("isolation")).cloned(),
            Some(json!({ "level": "non_isolated" }))
        );
    }

    #[tokio::test]
    async fn missing_execution_context_is_none_not_error() {
        let (r, d) = router();
        // 旧客户端不带该字段 → None（非对象值同样按缺失处理）
        assert_eq!(r.route(&base_msg(), "u1").await, RouteOutcome::Handled);
        assert!(d.last_execution_context.lock().unwrap().is_none());

        let (r2, d2) = router();
        let mut bad = base_msg();
        bad["execution_context"] = json!("not-an-object");
        assert_eq!(r2.route(&bad, "u1").await, RouteOutcome::Handled);
        assert!(d2.last_execution_context.lock().unwrap().is_none());
    }
}
