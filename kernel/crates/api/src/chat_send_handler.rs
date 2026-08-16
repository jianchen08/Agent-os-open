//! `chat` namespace capability handler——把"向会话投递消息并跑管道"暴露给 sidecar。
//!
//! 触发器（trigger_setup_tool）等 sidecar 到期触发时，经 `chat.send_message`
//! 复用前端同一条 WS 派发路径（`dispatch_user_input` → `process_via_engine`）：
//! 以触发消息为新一轮用户消息投给该会话 agent，agent 处理后流式回复前端。
//!
//! GAP-1 阶段 1（管道创建契约）：`send_message` 额外支持——
//! - 可选 `state` 对象：自由注入 initial_state 顶层扁平键（任务域 `task.*`），
//!   经 dispatch → process_via_engine 的 execution_context 合并点（1a2）之后并入；
//!   保留字（message/pipeline_id 等）受保护，命中即协议错误。
//! - 创建分支（`create: true` 或 `pipeline_id` 为空）：引擎生成新 pipeline_id
//!   并在响应返回（`{"status":"created","pipeline_id":...}`）；必须声明 `lineage`
//!   （有父/根二选一，见 [`parse_lineage`]），lineage 由引擎展开为扁平键写入
//!   state，作为出生即固化的保护字段。
//!
//! sidecar 光有展示通道（event-bus.emit 往前端推事件）不能唤醒 agent 跑一轮，
//! 还需注入通道——本 handler 即该通道：经
//! [`CapabilityHandlerRegistry`] 注册（router 优先查它），不新建传输、不动 router 结构，
//! 仅把内核既有的 `PipelineDispatcher::dispatch_user_input` 桥接成 sidecar 可达的能力。

use std::sync::Arc;

use async_trait::async_trait;
use serde_json::{json, Map, Value};

use agentos_mcp::{CapabilityHandler, McpError};
use agentos_session::router::PipelineDispatcher;

/// state 注入保留字：引擎系统字段，调用方不可覆盖（GAP-1 定案——堵消息/路由/
/// 上下文被注入方篡改）。`lineage` 及 `lineage.*` 前缀同属保护（引擎出生写入，
/// 见 [`parse_lineage`]）。任务域字段用 `task.*` 前缀。
pub(crate) const RESERVED_STATE_KEYS: &[&str] = &[
    "message",
    "messages",
    "agent_id",
    "pipeline_id",
    "session_id",
    "thread_id",
    "user_id",
    "run_id",
    "execution_context",
    "lineage",
    "message_id",
];

/// lineage 根形式 `origin.kind` 合法枚举（GAP-1 补定案：根是诚实声明，来源用
/// 类型描述符表达——channel | external_service | plugin | system）。
const LINEAGE_ORIGIN_KINDS: &[&str] = &["channel", "external_service", "plugin", "system"];

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
            "send_message" => self.send_message(params).await,
            other => Err(McpError::Protocol {
                message: format!("capability method not implemented: chat.{other}"),
            }),
        }
    }
}

impl ChatSendHandler {
    /// `chat.send_message`：投递消息到会话管道并跑一轮（GAP-1 阶段 1 管道创建契约）。
    ///
    /// 两种分支：
    /// - **注入分支**（带 `pipeline_id` 且非 `create`，现状不变）：消息投给已有管道。
    /// - **创建分支**（`create: true` 或 `pipeline_id` 为空）：引擎生成新 pipeline_id
    ///   （uuid v4 simple，与 sessions 的 active_pipeline_id 同格式）并以该 id 走
    ///   既有 dispatch——resolve 链路对陌生 id 落回退分支 ③ 原样穿透到引擎
    ///   `get_or_init` 新 state。创建**必须**声明 `lineage`（二选一，杜绝孤儿），
    ///   且不接受调用方传入 id（三次定案：堵 id 冒占）。
    ///
    /// 可选 `state` 对象：自由注入 initial_state 顶层扁平键（任务域用 `task.*`
    /// 点号键，与 track.total_tokens 同款约定），经 dispatch → process_via_engine
    /// 的 execution_context 合并点（1a2）之后并入；保留字命中即协议错误。
    async fn send_message(&self, params: Value) -> Result<Value, McpError> {
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
        let create_flag = params.get("create").and_then(|v| v.as_bool()).unwrap_or(false);
        // background（可选，默认 false）：创建分支的派发改为 spawn 后台执行、
        // 响应立即返回 pipeline_id——任务派发场景（task_submit）不能阻塞等待
        // 整条任务管道跑完。注入分支不适用（触发通知需要投递确认）。
        let background = params.get("background").and_then(|v| v.as_bool()).unwrap_or(false);
        let supplied_pid = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty());
        // 任务级 execution_context（可选）：任务执行器从 task.metadata 组装
        // （workspace_mode/isolation_level 等），随消息派发并入 initial_state，
        // init 体 workspace_lifecycle / environment_lifecycle 插件消费。
        let execution_context = params.get("execution_context").filter(|v| v.is_object());

        // state 注入（可选）：校验保留字后作为 overlay 透传。
        let mut overlay = validate_state_overlay(params.get("state"))?;

        let (pipeline_id, created) = if create_flag || supplied_pid.is_none() {
            // ── 创建分支 ──
            if let Some(pid) = supplied_pid {
                return Err(McpError::Protocol {
                    message: format!(
                        "chat.send_message 创建分支（create:true）不接受调用方传入 \
                         pipeline_id（{pid}）——注入已有管道请去掉 create 并显式传 pipeline_id"
                    ),
                });
            }
            // 血缘二选一强制（补定案：出生结构性写入，非可选元数据）。
            let lineage = params.get("lineage").filter(|v| !v.is_null()).ok_or_else(|| {
                McpError::Protocol {
                    message: "chat.send_message 创建新管道必须声明 lineage\
                              （有父/根二选一，杜绝孤儿管道）"
                        .to_string(),
                }
            })?;
            let lineage_keys = parse_lineage(lineage)?;
            let overlay_obj = overlay.get_or_insert_with(|| Value::Object(Map::new()));
            if let Some(obj) = overlay_obj.as_object_mut() {
                for (k, v) in lineage_keys {
                    obj.insert(k, v);
                }
            }
            // 三次定案：pipeline_id 由引擎生成（身份权威统一），uuid v4 simple
            // （32 位 hex 无连字符，与 sessions 表 active_pipeline_id 同格式）。
            let pipeline_id = uuid::Uuid::new_v4().simple().to_string();
            // GAP-1 统一（task = pipeline）：task.id 即管道 id——调用方派发时
            // 尚不知道引擎身份，此处引擎强制注入（与 lineage 同级保护字段），
            // 调用方预传的 task.id 一律覆盖为引擎 id（堵身份冒占）。
            if let Some(obj) = overlay_obj.as_object_mut() {
                obj.insert("task.id".to_string(), Value::String(pipeline_id.clone()));
            }
            (pipeline_id, true)
        } else {
            // ── 注入分支（现状不变）──
            // lineage 是引擎出生写入的保护字段，注入已有管道不得携带（防覆写）。
            if params.get("lineage").filter(|v| !v.is_null()).is_some() {
                return Err(McpError::Protocol {
                    message: "chat.send_message 注入已有管道不可携带 lineage\
                              （血缘仅在创建时声明，创建后不可覆写）"
                        .to_string(),
                });
            }
            (supplied_pid.unwrap().to_string(), false)
        };

        tracing::info!(
            target: "capability:chat",
            pipeline = %pipeline_id,
            user = %user_id,
            msg_len = message.len(),
            created,
            has_execution_context = execution_context.is_some(),
            has_state = overlay.is_some(),
            "chat.send_message 派发触发消息"
        );

        // 复用 WS 派发：主会话下 thread_id 与 pipeline_id 同值（effective_pipeline_id），
        // dispatch_user_input 内部会 resolve 真实 route_id 并发 stream_start →
        // process_via_engine → new_message，前端按既有协议流式渲染回复。
        // tenant 由 dispatch_user_input 用 user_id 反查（与 WS 路径同源）。
        // thinking_strength：HTTP 通道暂不携带（"" = 引擎不覆盖参数）。
        //
        // background（仅创建分支）：spawn 后台派发，响应立即返回——调用方
        // （task_submit）即刻拿到 pipeline_id 写任务关联，任务管道在
        // RunChain 上照常 FIFO 执行。派发失败仅告警（任务已创建，重试走
        // 显式 pipeline_id 注入）。
        if created && background {
            let dispatcher = self.dispatcher.clone();
            let pid = pipeline_id.clone();
            let uid = user_id.to_string();
            let msg = message.to_string();
            let ec = execution_context.cloned();
            let ov = overlay.clone();
            tokio::spawn(async move {
                if let Err(e) = dispatcher
                    .dispatch_user_input(&pid, &uid, &msg, &pid, "", ec.as_ref(), ov.as_ref())
                    .await
                {
                    tracing::error!(
                        pipeline = %pid,
                        error = %e,
                        "chat.send_message 后台派发失败（任务管道未启动）"
                    );
                }
            });
            return Ok(json!({"status": "created", "pipeline_id": pipeline_id}));
        }
        self.dispatcher
            .dispatch_user_input(
                &pipeline_id,
                user_id,
                message,
                &pipeline_id,
                "",
                execution_context,
                overlay.as_ref(),
            )
            .await
            .map(|_| {
                if created {
                    json!({"status": "created", "pipeline_id": pipeline_id})
                } else {
                    json!({"status": "dispatched", "pipeline_id": pipeline_id})
                }
            })
            .map_err(|e| McpError::Protocol {
                message: format!("chat.send_message 派发失败: {e}"),
            })
    }
}

/// 校验并提取 `state` 注入（可选对象 → 透传 overlay）。
///
/// 键约定：顶层扁平点号键（任务域 `task.*`，与 track.total_tokens 同款——
/// STATE_SUMMARY_KEYS 匹配的就是这种顶层扁平键）。保留字与 `lineage.*` 保护
/// 前缀命中即 [`McpError::Protocol`]。
fn validate_state_overlay(state: Option<&Value>) -> Result<Option<Value>, McpError> {
    let Some(state) = state.filter(|v| !v.is_null()) else {
        return Ok(None);
    };
    let Some(obj) = state.as_object() else {
        return Err(McpError::Protocol {
            message: "chat.send_message state 参数必须为对象（顶层扁平点号键，如 task.goal）"
                .to_string(),
        });
    };
    for key in obj.keys() {
        if key.is_empty() {
            return Err(McpError::Protocol {
                message: "chat.send_message state 键不得为空串".to_string(),
            });
        }
        if key.starts_with("lineage.") {
            return Err(McpError::Protocol {
                message: format!(
                    "chat.send_message state 键 {key} 受保护：lineage 为引擎出生写入字段，\
                     不可经 state 注入"
                ),
            });
        }
        if RESERVED_STATE_KEYS.contains(&key.as_str()) {
            return Err(McpError::Protocol {
                message: format!(
                    "chat.send_message state 键 {key} 为引擎系统保留字，不可覆盖\
                     （任务域请用 task.* 前缀）"
                ),
            });
        }
    }
    Ok(Some(state.clone()))
}

/// 校验血缘参数（二选一强制）并展开为引擎写入的扁平 state 键。
///
/// - **有父形式**：`{"parent_pipeline_id": "...", "origin_session_id": "..."}`
///   （直接创建者管道 + 根人类会话锚点——向上最近的会话，跨级不变）。
/// - **根形式**：`{"root": true, "origin": {"kind": "...", "source": "..."}}`，
///   `kind` 枚举 [`LINEAGE_ORIGIN_KINDS`]；外部通道为外部用户建了会话的（钉钉/
///   飞书模式）该会话即锚点，走有父形式。
///
/// 不伪造默认父/默认 session（假父会在任务树聚合里成为幽灵节点）。两种形式
/// 同时出现或均缺失 → [`McpError::Protocol`]。
fn parse_lineage(lineage: &Value) -> Result<Map<String, Value>, McpError> {
    let bad =
        |msg: String| -> Result<Map<String, Value>, McpError> {
            Err(McpError::Protocol {
                message: format!("chat.send_message lineage 不合法: {msg}"),
            })
        };
    let Some(lin) = lineage.as_object() else {
        return bad("必须为对象（有父/根二选一）".to_string());
    };
    let parent_pid = lin
        .get("parent_pipeline_id")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty());
    let root = lin.get("root").and_then(|v| v.as_bool()).unwrap_or(false);

    if parent_pid.is_some() && root {
        return bad("有父形式与根形式二选一，不可同时出现".to_string());
    }
    if let Some(pid) = parent_pid {
        let Some(sess) = lin
            .get("origin_session_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
        else {
            return bad(
                "有父形式必须同时提供非空 origin_session_id（根人类会话锚点）".to_string(),
            );
        };
        let mut m = Map::new();
        m.insert("lineage.parent_pipeline_id".to_string(), json!(pid));
        m.insert("lineage.origin_session_id".to_string(), json!(sess));
        return Ok(m);
    }
    if root {
        let Some(origin) = lin.get("origin").and_then(|v| v.as_object()) else {
            return bad("根形式必须提供 origin 对象 {kind, source}".to_string());
        };
        let Some(kind) = origin
            .get("kind")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
        else {
            return bad("根形式 origin.kind 必填".to_string());
        };
        if !LINEAGE_ORIGIN_KINDS.contains(&kind) {
            return bad(format!(
                "根形式 origin.kind 必须为 {LINEAGE_ORIGIN_KINDS:?} 之一，实际 {kind}"
            ));
        }
        let Some(source) = origin
            .get("source")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
        else {
            return bad("根形式 origin.source 必填（非空来源描述）".to_string());
        };
        let mut m = Map::new();
        m.insert("lineage.root".to_string(), json!(true));
        m.insert("lineage.origin.kind".to_string(), json!(kind));
        m.insert("lineage.origin.source".to_string(), json!(source));
        return Ok(m);
    }
    bad(
        "缺少有效血缘：有父形式（parent_pipeline_id + origin_session_id）或根形式\
         （root:true + origin）二选一"
            .to_string(),
    )
}

#[cfg(test)]
mod tests {
    //! GAP-1 阶段 1：chat.send_message 管道创建契约单测。
    //! 覆盖：创建分支 id 生成与响应、血缘二选一校验（含非法/缺失）、保留字拒绝、
    //! state overlay 透传与 lineage 引擎写入。

    use super::*;
    use serde_json::json;
    use std::sync::{Arc, Mutex};

    /// 单次 dispatch_user_input 记录（全参快照）：
    /// (thread_id, user_id, content, pipeline_id, thinking, execution_context, state_overlay)
    type DispatchRecord = (
        String,
        String,
        String,
        String,
        String,
        Option<Value>,
        Option<Value>,
    );

    /// 记录型 mock dispatcher：捕获每次 dispatch 调用供断言。
    struct RecordingDispatcher {
        calls: Mutex<Vec<DispatchRecord>>,
    }

    impl RecordingDispatcher {
        fn shared() -> Arc<Self> {
            Arc::new(Self {
                calls: Mutex::new(Vec::new()),
            })
        }
    }

    #[async_trait]
    impl PipelineDispatcher for RecordingDispatcher {
        async fn dispatch_user_input(
            &self,
            thread_id: &str,
            user_id: &str,
            content: &str,
            pipeline_id: &str,
            thinking_strength: &str,
            execution_context: Option<&Value>,
            state_overlay: Option<&Value>,
        ) -> Result<(), String> {
            self.calls.lock().unwrap().push((
                thread_id.to_string(),
                user_id.to_string(),
                content.to_string(),
                pipeline_id.to_string(),
                thinking_strength.to_string(),
                execution_context.cloned(),
                state_overlay.cloned(),
            ));
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

        async fn dispatch_stop(&self, _thread_id: &str) -> Result<(), String> {
            Ok(())
        }
    }

    /// uuid v4 simple 格式性质断言：32 位 hex 无连字符、version=4、variant∈[8,b]。
    fn assert_simple_uuid_v4(s: &str) {
        assert_eq!(s.len(), 32, "simple uuid 应为 32 位 hex 无连字符: {s}");
        assert!(
            s.chars().all(|c| c.is_ascii_hexdigit()),
            "应全为 hex 字符: {s}"
        );
        assert_eq!(&s[12..13], "4", "version nibble 应为 4: {s}");
        let variant = s.as_bytes()[16] as char;
        assert!(
            ['8', '9', 'a', 'b'].contains(&variant),
            "variant nibble 应 ∈ [8,b]: {s}"
        );
    }

    fn handler() -> (ChatSendHandler, Arc<RecordingDispatcher>) {
        let d = RecordingDispatcher::shared();
        (ChatSendHandler::new(d.clone()), d)
    }

    fn calls(d: &RecordingDispatcher) -> Vec<DispatchRecord> {
        d.calls.lock().unwrap().clone()
    }

    /// 断言协议错误且未派发（校验失败不得产生副作用）。
    async fn expect_protocol_error(
        h: &ChatSendHandler,
        d: &RecordingDispatcher,
        params: Value,
        why: &str,
    ) {
        let err = h.handle("send_message", params).await.expect_err(why);
        assert!(
            matches!(err, McpError::Protocol { .. }),
            "{why}: 应为协议错误，实际 {err:?}"
        );
        assert!(
            d.calls.lock().unwrap().is_empty(),
            "{why}: 校验失败不得派发"
        );
    }

    // ── 注入分支（现状不变）──────────────────────────────────────

    #[tokio::test]
    async fn inject_existing_pipeline_keeps_current_contract() {
        // 两组有区分度的输入：不同 pipeline_id 均按原样派发、响应 status=dispatched
        for pid in ["pipe_existing", "pipe_trigger_42"] {
            let (h, d) = handler();
            let res = h
                .handle(
                    "send_message",
                    json!({"pipeline_id": pid, "message": "hi", "user_id": "u1"}),
                )
                .await
                .unwrap();
            assert_eq!(res["status"], "dispatched");
            assert_eq!(res["pipeline_id"], pid);
            let c = calls(&d);
            assert_eq!(c.len(), 1);
            assert_eq!(c[0].0, pid, "thread_id 应与 pipeline_id 同值");
            assert_eq!(c[0].3, pid);
            assert!(c[0].6.is_none(), "无 state 参数时 overlay 为 None");
        }
    }

    #[tokio::test]
    async fn inject_passes_state_overlay_without_lineage() {
        let (h, d) = handler();
        let res = h
            .handle(
                "send_message",
                json!({
                    "pipeline_id": "pipe_1",
                    "message": "更新任务状态",
                    "user_id": "u1",
                    "state": {"task.status": "running", "task.progress": 30}
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "dispatched");
        let overlay = calls(&d)[0]
            .6
            .clone()
            .expect("state overlay 应透传到派发层");
        assert_eq!(overlay["task.status"], "running");
        assert_eq!(overlay["task.progress"], 30);
        assert!(
            overlay
                .as_object()
                .unwrap()
                .keys()
                .all(|k| !k.starts_with("lineage")),
            "注入分支 overlay 不应携带 lineage 键"
        );
    }

    // ── 创建分支：id 生成与响应 ──────────────────────────────────

    #[tokio::test]
    async fn create_with_flag_generates_engine_pipeline_id() {
        let (h, d) = handler();
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true,
                    "message": "执行任务",
                    "user_id": "u1",
                    "lineage": {
                        "parent_pipeline_id": "pipe_parent",
                        "origin_session_id": "sess_root"
                    },
                    "state": {"task.goal": "喝水提醒", "task.status": "pending"}
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "created");
        let pid = res["pipeline_id"].as_str().unwrap().to_string();
        assert_simple_uuid_v4(&pid);
        // 一致性：响应返回的 id 即派发所用 id（thread 与 pipeline 同值——
        // resolve 链路对陌生 id 落回退分支 ③ 原样穿透）
        let c = calls(&d);
        assert_eq!(c.len(), 1);
        assert_eq!(c[0].0, pid);
        assert_eq!(c[0].3, pid);
        // overlay：task.* 自由键 + lineage 扁平键（引擎写入）
        let overlay = c[0].6.clone().expect("创建分支 overlay 必含 lineage");
        assert_eq!(overlay["task.goal"], "喝水提醒");
        assert_eq!(overlay["task.status"], "pending");
        assert_eq!(overlay["lineage.parent_pipeline_id"], "pipe_parent");
        assert_eq!(overlay["lineage.origin_session_id"], "sess_root");
        // task.id 由引擎注入 == 响应 pipeline_id（身份权威统一）
        assert_eq!(overlay["task.id"], pid);
    }

    #[tokio::test]
    async fn create_without_flag_when_pipeline_id_absent() {
        // pipeline_id 缺省即隐式创建（"或 pipeline_id 为空"），根形式血缘
        let (h, d) = handler();
        let res = h
            .handle(
                "send_message",
                json!({
                    "message": "m",
                    "user_id": "u1",
                    "lineage": {
                        "root": true,
                        "origin": {"kind": "channel", "source": "dingtalk"}
                    }
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "created");
        assert_simple_uuid_v4(res["pipeline_id"].as_str().unwrap());
        let overlay = calls(&d)[0].6.clone().unwrap();
        assert_eq!(overlay["lineage.root"], true);
        assert_eq!(overlay["lineage.origin.kind"], "channel");
        assert_eq!(overlay["lineage.origin.source"], "dingtalk");
    }

    #[tokio::test]
    async fn create_ids_unique_and_well_formed() {
        // 性质断言：多次创建 id 两两不同（引擎生成身份，非常量）
        let (h, _d) = handler();
        let mut ids = Vec::new();
        for _ in 0..3 {
            let res = h
                .handle(
                    "send_message",
                    json!({
                        "create": true, "message": "m", "user_id": "u1",
                        "lineage": {
                            "root": true,
                            "origin": {"kind": "system", "source": "kernel"}
                        }
                    }),
                )
                .await
                .unwrap();
            ids.push(res["pipeline_id"].as_str().unwrap().to_string());
        }
        for id in &ids {
            assert_simple_uuid_v4(id);
        }
        assert_eq!(
            ids.iter().collect::<std::collections::HashSet<_>>().len(),
            3,
            "三次创建应产生三个不同 id"
        );
    }

    // ── 血缘二选一强制 ───────────────────────────────────────────

    #[tokio::test]
    async fn create_without_lineage_rejected() {
        let (h, d) = handler();
        expect_protocol_error(
            &h,
            &d,
            json!({"create": true, "message": "m", "user_id": "u1"}),
            "缺 lineage",
        )
        .await;
        expect_protocol_error(
            &h,
            &d,
            json!({"create": true, "message": "m", "user_id": "u1", "lineage": null}),
            "lineage 显式 null 视为缺失",
        )
        .await;
    }

    #[tokio::test]
    async fn create_rejects_invalid_lineage_forms() {
        let (h, d) = handler();
        let cases = vec![
            (json!({}), "空对象：两种形式都不满足"),
            (json!({"parent_pipeline_id": "p1"}), "有父形式缺 origin_session_id"),
            (json!({"origin_session_id": "s1"}), "有父形式缺 parent_pipeline_id"),
            (
                json!({"parent_pipeline_id": "", "origin_session_id": "s1"}),
                "parent_pipeline_id 为空串",
            ),
            (json!({"root": false}), "root=false 且无父形式"),
            (json!({"root": true}), "根形式缺 origin"),
            (json!({"root": true, "origin": {}}), "根形式 origin 缺 kind/source"),
            (
                json!({"root": true, "origin": {"kind": "channel"}}),
                "根形式 origin 缺 source",
            ),
            (
                json!({"root": true, "origin": {"kind": "human", "source": "x"}}),
                "kind 不在枚举",
            ),
            (
                json!({"root": true, "origin": {"kind": "channel", "source": ""}}),
                "source 为空串",
            ),
            (
                json!({
                    "root": true,
                    "origin": {"kind": "channel", "source": "x"},
                    "parent_pipeline_id": "p1",
                    "origin_session_id": "s1"
                }),
                "两种形式同时出现",
            ),
            (json!("not-an-object"), "lineage 非对象"),
        ];
        for (lin, why) in cases {
            let mut params = json!({"create": true, "message": "m", "user_id": "u1"});
            params["lineage"] = lin;
            expect_protocol_error(&h, &d, params, why).await;
        }
    }

    #[tokio::test]
    async fn create_rejects_caller_supplied_pipeline_id() {
        // 三次定案：创建路径不接受调用方传入 id（堵 id 冒占）
        let (h, d) = handler();
        expect_protocol_error(
            &h,
            &d,
            json!({
                "create": true, "pipeline_id": "pipe_mine",
                "message": "m", "user_id": "u1",
                "lineage": {"root": true, "origin": {"kind": "plugin", "source": "task_submit"}}
            }),
            "create 与显式 pipeline_id 互斥",
        )
        .await;
    }

    #[tokio::test]
    async fn inject_with_lineage_rejected() {
        // lineage 是出生写入的保护字段：注入已有管道不得携带（防覆写）
        let (h, d) = handler();
        expect_protocol_error(
            &h,
            &d,
            json!({
                "pipeline_id": "pipe_1", "message": "m", "user_id": "u1",
                "lineage": {"root": true, "origin": {"kind": "plugin", "source": "x"}}
            }),
            "注入分支携带 lineage 应拒绝",
        )
        .await;
    }

    // ── 保留字保护 ──────────────────────────────────────────────

    #[tokio::test]
    async fn state_reserved_keys_rejected() {
        let (h, d) = handler();
        for key in [
            "pipeline_id",
            "message",
            "messages",
            "agent_id",
            "session_id",
            "thread_id",
            "user_id",
            "run_id",
            "execution_context",
            "lineage",
            "message_id",
        ] {
            let mut state = serde_json::Map::new();
            state.insert(key.to_string(), json!("evil"));
            let mut params =
                json!({"pipeline_id": "pipe_1", "message": "m", "user_id": "u1"});
            params["state"] = Value::Object(state);
            expect_protocol_error(&h, &d, params, &format!("保留字 {key}")).await;
        }
    }

    #[tokio::test]
    async fn state_lineage_prefixed_keys_rejected() {
        let (h, d) = handler();
        for key in [
            "lineage.root",
            "lineage.parent_pipeline_id",
            "lineage.origin.kind",
        ] {
            let mut state = serde_json::Map::new();
            state.insert(key.to_string(), json!(true));
            let mut params =
                json!({"pipeline_id": "pipe_1", "message": "m", "user_id": "u1"});
            params["state"] = Value::Object(state);
            expect_protocol_error(&h, &d, params, &format!("保护前缀 {key}")).await;
        }
    }

    #[tokio::test]
    async fn state_must_be_object() {
        let (h, d) = handler();
        expect_protocol_error(
            &h,
            &d,
            json!({"pipeline_id": "pipe_1", "message": "m", "user_id": "u1", "state": "oops"}),
            "state 非对象",
        )
        .await;
        expect_protocol_error(
            &h,
            &d,
            json!({"pipeline_id": "pipe_1", "message": "m", "user_id": "u1", "state": [1, 2]}),
            "state 数组",
        )
        .await;
    }

    // ── 既有必选参数与 execution_context 兼容 ───────────────────

    #[tokio::test]
    async fn missing_required_params_still_error() {
        let (h, d) = handler();
        // 创建形态下缺 message / user_id 仍应报协议错误（不因创建分支放宽）
        expect_protocol_error(
            &h,
            &d,
            json!({
                "create": true, "user_id": "u1",
                "lineage": {"root": true, "origin": {"kind": "system", "source": "kernel"}}
            }),
            "缺 message",
        )
        .await;
        expect_protocol_error(
            &h,
            &d,
            json!({
                "create": true, "message": "m",
                "lineage": {"root": true, "origin": {"kind": "system", "source": "kernel"}}
            }),
            "缺 user_id",
        )
        .await;
    }

    #[tokio::test]
    async fn create_passes_execution_context_alongside_state() {
        let (h, d) = handler();
        let ec = json!({"workspace": {"mode": "worktree"}, "isolation": {"level": "plain"}});
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true, "message": "m", "user_id": "u1",
                    "execution_context": ec,
                    "state": {"task.id": "t9"},
                    "lineage": {"parent_pipeline_id": "p", "origin_session_id": "s"}
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "created");
        let c = calls(&d);
        assert_eq!(
            c[0].5,
            Some(json!({"workspace": {"mode": "worktree"}, "isolation": {"level": "plain"}}))
        );
        // task.id 由引擎注入（调用方预传的 t9 被覆盖为引擎 id）
        assert_eq!(c[0].6.as_ref().unwrap()["task.id"], res["pipeline_id"]);
    }

    // ── GAP-1：background 参数（任务派发不阻塞等待任务完成） ──────────

    /// 带闸门的派发器：dispatch_user_input 挂起直到 gate 收到信号——用于
    /// 区分 background 语义（前台 = 调用方被阻塞；后台 = 立即返回）。
    struct GatedDispatcher {
        calls: Mutex<Vec<Value>>,
        gate: tokio::sync::Notify,
    }

    #[async_trait]
    impl PipelineDispatcher for GatedDispatcher {
        async fn dispatch_user_input(
            &self,
            _thread_id: &str,
            _user_id: &str,
            _content: &str,
            _pipeline_id: &str,
            _thinking_strength: &str,
            _execution_context: Option<&Value>,
            state_overlay: Option<&Value>,
        ) -> Result<(), String> {
            self.gate.notified().await;
            self.calls.lock().unwrap().push(state_overlay.cloned().unwrap_or(Value::Null));
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
        async fn dispatch_stop(&self, _thread_id: &str) -> Result<(), String> {
            Ok(())
        }
    }

    #[tokio::test]
    async fn create_with_background_returns_before_dispatch_completes() {
        // 任务派发场景：task_submit 需要立即拿到 pipeline_id 返回给 LLM，
        // 不能阻塞等待整条任务管道跑完。派发挂起（gate 未开）时响应仍立即返回。
        let d = Arc::new(GatedDispatcher {
            calls: Mutex::new(Vec::new()),
            gate: tokio::sync::Notify::new(),
        });
        let h = ChatSendHandler::new(d.clone());
        let res = tokio::time::timeout(std::time::Duration::from_millis(300), h.handle(
            "send_message",
            json!({
                "create": true, "message": "m", "user_id": "u1",
                "background": true,
                "lineage": {"parent_pipeline_id": "p", "origin_session_id": "s"},
                "state": {"task.id": "t1"}
            }),
        ))
        .await
        .expect("background 创建应在派发完成前返回（300ms 闸门未开）")
        .unwrap();
        assert_eq!(res["status"], "created");
        assert_simple_uuid_v4(res["pipeline_id"].as_str().unwrap());
        // 派发仍在挂起（gate 未开）——但调用方已拿到响应
        assert!(d.calls.lock().unwrap().is_empty());
        // 放行闸门 → 后台派发完成
        d.gate.notify_one();
        let mut dispatched = false;
        for _ in 0..40 {
            if !d.calls.lock().unwrap().is_empty() {
                dispatched = true;
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
        assert!(dispatched, "background 派发应最终执行");
        assert_eq!(d.calls.lock().unwrap()[0]["task.id"], res["pipeline_id"]);
    }

    #[tokio::test]
    async fn create_foreground_waits_for_dispatch_by_default() {
        // 默认（background 缺省）：维持既有语义——await 派发完成再返回
        // （触发器通知等需要投递确认的调用方）。派发挂起 → 超时打红。
        let d = Arc::new(GatedDispatcher {
            calls: Mutex::new(Vec::new()),
            gate: tokio::sync::Notify::new(),
        });
        let h = ChatSendHandler::new(d.clone());
        let outcome = tokio::time::timeout(std::time::Duration::from_millis(200), h.handle(
            "send_message",
            json!({
                "create": true, "message": "m", "user_id": "u1",
                "lineage": {"root": true, "origin": {"kind": "system", "source": "kernel"}}
            }),
        ))
        .await;
        assert!(
            outcome.is_err(),
            "前台创建应等待派发完成（闸门未开 → 超时）"
        );
        d.gate.notify_one(); // 清理：放行挂起的派发
    }

    #[tokio::test]
    async fn unknown_method_still_rejected() {
        let (h, _d) = handler();
        let err = h.handle("bogus", json!({})).await.unwrap_err();
        assert!(matches!(err, McpError::Protocol { .. }));
    }

    #[tokio::test]
    async fn create_overrides_caller_supplied_task_id() {
        // task.id 是引擎保护字段（同 lineage）：调用方预传的身份被引擎 id 覆盖
        let (h, d) = handler();
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true, "message": "m", "user_id": "u1",
                    "lineage": {"root": true, "origin": {"kind": "plugin", "source": "task_submit"}},
                    "state": {"task.id": "fake_id_999", "task.goal": "g"}
                }),
            )
            .await
            .unwrap();
        let pid = res["pipeline_id"].as_str().unwrap();
        assert_eq!(calls(&d)[0].6.as_ref().unwrap()["task.id"], pid, "引擎 id 覆盖调用方预传");
    }
}
