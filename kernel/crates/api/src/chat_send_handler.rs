//! `chat` namespace capability handler——把"向会话投递消息并跑管道"暴露给 sidecar。
//!
//! 触发器（trigger_setup_tool）等 sidecar 到期触发时，经 `chat.send_message`
//! 复用前端同一条 WS 派发路径（`dispatch_user_input` → `process_via_engine`）：
//! 以触发消息为新一轮用户消息投给该会话 agent，agent 处理后流式回复前端。
//!
//! 坐标语义（2026-08-19 定案）：对外契约只含 `pipeline_id + message + user_id`，
//! thread 是由坐标推导的派生物——注入分支在接口内部用 `pipeline_id` 反查
//! `pipeline_sessions` 解析真实会话 thread（黑盒），解析失败即协议错误；
//! 两个 id 形态不同（32hex vs `thread-` 前缀），绝不互填、不做参数搬运。
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

use agentos_core::traits::StorageBackend;
use async_trait::async_trait;
use serde_json::{json, Map, Value};

use agentos_mcp::{CapabilityHandler, McpError};
use agentos_session::router::PipelineDispatcher;

/// send_message 实际读取的顶层参数清单（防"配置↔代码"双轨漂移的代码侧锚点）。
///
/// 与 `config/kernel_capabilities/chat.json` 的 `input_schema.properties` 集合
/// 必须一致——一致性由 kernel_capabilities::tests 的机械闸强制：加参数不改契约
/// （或反之）测试即红。新增参数三处同步：本清单、契约文件、读取代码。
#[allow(dead_code)] // 消费方是 kernel_capabilities::tests 机械闸（测试期使用）
pub(crate) const HANDLED_PARAM_NAMES: &[&str] = &[
    "message",
    "user_id",
    "create",
    "background",
    "pipeline_id",
    "execution_context",
    "agent_id",
    "state",
    "lineage",
];

/// lineage 根形式 `origin.kind` 合法枚举（GAP-1 补定案：根是诚实声明，来源用
/// 类型描述符表达——channel | external_service | plugin | system）。
/// 契约文件 lineage.origin.kind.enum 的代码侧锚点（机械闸强制一致）。
pub(crate) const LINEAGE_ORIGIN_KINDS: &[&str] =
    &["channel", "external_service", "plugin", "system"];

/// `chat` namespace handler：sidecar → 投递消息到会话并跑管道。
///
/// 持有内核 WS 派发器（`EngineDispatcher` 实现的 `PipelineDispatcher`），与前端
/// 发消息走完全相同的链路（tenant 解析 / route_id 解析 / stream_start / 引擎执行 /
/// new_message），保证触发消息和用户手发的消息行为一致。
pub struct ChatSendHandler {
    dispatcher: Arc<dyn PipelineDispatcher>,
    /// 注入分支坐标解析用（pipeline_id → 所属会话 thread）。
    /// None = 无存储构造（单测/兼容路径），注入坐标回退 pipeline 兼作派发键。
    store: Option<Arc<dyn StorageBackend>>,
}

impl ChatSendHandler {
    /// 用内核 WS 派发器构造（无存储）：注入分支回退 pipeline 兼作 thread 派发键。
    /// 生产注册点请用 [`ChatSendHandler::with_store`]（坐标解析 + 未命中拒绝）。
    pub fn new(dispatcher: Arc<dyn PipelineDispatcher>) -> Self {
        Self {
            dispatcher,
            store: None,
        }
    }

    /// 生产构造：带存储，注入分支用 pipeline_id 反查所属会话的真实 thread
    /// （坐标解析封装在接口内部黑盒，对外契约不含 thread 参数）。
    pub fn with_store(
        dispatcher: Arc<dyn PipelineDispatcher>,
        store: Option<Arc<dyn StorageBackend>>,
    ) -> Self {
        Self { dispatcher, store }
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
        let create_flag = params
            .get("create")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        // background（可选，默认 false）：派发改为 spawn 后台执行、响应立即
        // 返回——任务派发（task_submit 创建 / UI resume·retry 注入）不能阻塞
        // 等待整条管道跑完。触发器通知保持默认 await（投递确认语义）。
        let background = params
            .get("background")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let supplied_pid = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty());
        // 任务级 execution_context（可选）：任务执行器从 task.metadata 组装
        // （workspace_mode/isolation_level 等），随消息派发并入 initial_state，
        // init 体 workspace_lifecycle / environment_lifecycle 插件消费。
        let execution_context = params.get("execution_context").filter(|v| v.is_object());

        // agent_id（可选，默认 "agentos"）：任务派发按 target 选执行 agent——
        // 引擎据此加载 config/agents/**/<agent_id>.yaml（人格/tool_ids）。
        let agent_id = params
            .get("agent_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or("agentos")
            .to_string();

        // state 注入（可选）：校验保留字后作为 overlay 透传。
        let mut overlay = validate_state_overlay(params.get("state"))?;

        let (pipeline_id, thread_id, created) = if create_flag || supplied_pid.is_none() {
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
            let lineage = params
                .get("lineage")
                .filter(|v| !v.is_null())
                .ok_or_else(|| McpError::Protocol {
                    message: "chat.send_message 创建新管道必须声明 lineage\
                              （有父/根二选一，杜绝孤儿管道）"
                        .to_string(),
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
            // 新管道尚无会话映射：以引擎新 id 兼作派发坐标（resolve 链路对
            // 陌生 id 落回退分支 ③ 原样穿透到引擎 get_or_init 新 state）。
            let thread_id = pipeline_id.clone();
            (pipeline_id, thread_id, true)
        } else if let Some(pid) = supplied_pid {
            // ── 注入分支 ──
            // lineage 是引擎出生写入的保护字段，注入已有管道不得携带（防覆写）。
            if params.get("lineage").filter(|v| !v.is_null()).is_some() {
                return Err(McpError::Protocol {
                    message: "chat.send_message 注入已有管道不可携带 lineage\
                              （血缘仅在创建时声明，创建后不可覆写）"
                        .to_string(),
                });
            }
            // 坐标解析（接口内部黑盒）：pipeline_id → 所属会话真实 thread。
            // thread 是派生物，对外契约不含该参数；解析失败 = 孤儿/伪造 id。
            let thread_id = self.resolve_inject_thread(pid).await?;
            (pid.to_string(), thread_id, false)
        } else {
            unreachable!("supplied_pid 为 None 时已在创建分支 return/自生成 pipeline_id")
        };

        // 创建分支先落 pipeline↔thread 映射（F8，2026-08-20）：此前映射由
        // 引擎 persist 路径稍后补写，dispatch 内 resolve_pipeline_id_for_thread
        // 校验必失败 → 每次任务派发刷一条“前端传来的 pipeline_id 不属于该
        // thread”误告警。映射幂等（INSERT OR IGNORE），落库失败不阻断派发
        // （引擎路径仍会补写——此处只是消除误告警 + 提前可见性）。
        if created {
            if let Some(store) = self.store.as_ref() {
                let tenant_id =
                    crate::auth::resolve_tenant_id_by_user(Some(store), user_id).await;
                if let Err(e) = store
                    .link_pipeline_session(&pipeline_id, &thread_id, &tenant_id)
                    .await
                {
                    tracing::warn!(
                        target: "capability:chat",
                        pipeline = %pipeline_id,
                        thread = %thread_id,
                        error = %e.to_string(),
                        "chat.send_message 创建分支 pipeline↔thread 映射落库失败（引擎路径将补写）"
                    );
                }
            }
        }

        tracing::info!(
            target: "capability:chat",
            pipeline = %pipeline_id,
            thread = %thread_id,
            user = %user_id,
            msg_len = message.len(),
            created,
            agent = %agent_id,
            has_execution_context = execution_context.is_some(),
            has_state = overlay.is_some(),
            "chat.send_message 派发触发消息"
        );

        // 复用 WS 派发：thread_id 与 pipeline_id 各归其位——注入分支是解析出的
        // 真实会话坐标（thread-xxx），创建分支是引擎新 id；pipeline_id 槽位
        // 始终保持调用方/引擎生成的原值，不做参数搬运。dispatch_user_input
        // 内部会 resolve 真实 route_id 并发 stream_start → process_via_engine →
        // new_message，前端按既有协议流式渲染回复。
        // tenant 由 dispatch_user_input 用 user_id 反查（与 WS 路径同源）。
        // thinking_strength：HTTP 通道暂不携带（"" = 引擎不覆盖参数）。
        //
        // background：spawn 后台派发，响应立即返回——任务管道在 RunChain 上
        // 照常 FIFO 执行。创建分支调用方即刻拿到 pipeline_id；注入分支（UI
        // resume/retry）即刻返回 dispatched。派发失败仅告警。
        if background {
            let dispatcher = self.dispatcher.clone();
            let pid = pipeline_id.clone();
            let tid = thread_id.clone();
            let uid = user_id.to_string();
            let msg = message.to_string();
            let ec = execution_context.cloned();
            let ov = overlay.clone();
            let aid = agent_id.clone();
            tokio::spawn(async move {
                if let Err(e) = dispatcher
                    .dispatch_user_input(&tid, &uid, &msg, &pid, "", ec.as_ref(), ov.as_ref(), &aid)
                    .await
                {
                    tracing::error!(
                        pipeline = %pid,
                        thread = %tid,
                        error = %e,
                        "chat.send_message 后台派发失败（任务管道未启动）"
                    );
                }
            });
            return Ok(json!({
                "status": if created { "created" } else { "dispatched" },
                "pipeline_id": pipeline_id,
            }));
        }
        self.dispatcher
            .dispatch_user_input(
                &thread_id,
                user_id,
                message,
                &pipeline_id,
                "",
                execution_context,
                overlay.as_ref(),
                &agent_id,
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

    /// 注入分支坐标解析（接口内部黑盒）：用 pipeline_id 查所属会话的真实 thread_id。
    ///
    /// 对外契约只含 `pipeline_id + message + user_id`——thread 是由坐标推导的
    /// 派生物，不该让调用方传（传了反而要被造伪）。解析失败 → [`McpError::Protocol`]：
    /// 宁可接口消费方报错，也不静默跑完把回复发到无人订阅的频道。
    /// 无 store（`new()` 构造）回退现状：pipeline 兼作派发键（单测/兼容路径）。
    async fn resolve_inject_thread(&self, pipeline_id: &str) -> Result<String, McpError> {
        let Some(store) = self.store.as_ref() else {
            return Ok(pipeline_id.to_string());
        };
        let thread = store
            .get_thread_id_by_pipeline(pipeline_id)
            .await
            .map_err(|e| McpError::Protocol {
                message: format!("chat.send_message 解析 pipeline 会话坐标失败: {e}"),
            })?;
        thread.ok_or_else(|| McpError::Protocol {
            message: format!(
                "chat.send_message 注入的 pipeline_id（{pipeline_id}）不属于任何会话\
                 （pipeline_sessions 未命中）：孤儿或伪造 id，拒绝派发——\
                 静默跑完会把回复发到无人订阅的频道"
            ),
        })
    }
}

/// 校验并提取 `state` 注入（可选对象 → 透传 overlay）。
///
/// 键约定：顶层扁平点号键（任务域 `task.*`，与 track.total_tokens 同款——
/// STATE_SUMMARY_KEYS 匹配的就是这种顶层扁平键）。保留字与 `lineage.*` 保护
/// 前缀命中即 [`McpError::Protocol`]。清单单一真值源在
/// [`crate::kernel_capabilities`]（契约文件锚定，本处为直连路径的防线）。
fn validate_state_overlay(state: Option<&Value>) -> Result<Option<Value>, McpError> {
    use crate::kernel_capabilities::{FORBIDDEN_STATE_KEY_PREFIXES, RESERVED_STATE_KEYS};

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
        if FORBIDDEN_STATE_KEY_PREFIXES
            .iter()
            .any(|p| key.starts_with(p))
        {
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
    let bad = |msg: String| -> Result<Map<String, Value>, McpError> {
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
            return bad("有父形式必须同时提供非空 origin_session_id（根人类会话锚点）".to_string());
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
            _agent_id: &str,
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
        // 无 store 构造（new()，单测/兼容路径）：注入坐标回退 pipeline 兼作
        // thread 派发键。生产注册点走 with_store（见下方坐标解析三连测试）。
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
            assert_eq!(c[0].0, pid, "无 store 回退：thread_id 与 pipeline_id 同值");
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

    // ── 注入分支坐标解析（2026-08-19 触发器空回复修复）──────────────
    // 修复前：chat_send_handler 把同一个 pipeline_id 复制填进 dispatch 的
    // thread_id 与 pipeline_id 两个槽位——事件发到无人订阅的频道（32hex 键
    // 在 registry 只注册 thread-xxx），表现为「LLM 日志有、前端收不到回复」。
    // 修复后：坐标解析封装在接口黑盒内（pipeline_sessions 反查真实 thread），
    // 解析失败即协议错误。三连负样本 = A.3 验收口径。

    /// 生产构造（with_store）+ 会话映射命中：派发的 thread_id 必须是解析出的
    /// 真实会话坐标（`thread-` 前缀），pipeline_id 槽位保持原值，绝不互填。
    #[tokio::test]
    async fn inject_with_store_resolves_real_thread_coordinate() {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        store
            .link_pipeline_session(
                "a1b2c3d4e5f64789abcdef0123456789",
                "thread-trig-1",
                "default",
            )
            .await
            .unwrap();
        let d = RecordingDispatcher::shared();
        let h = ChatSendHandler::with_store(d.clone(), Some(store));
        let res = h
            .handle(
                "send_message",
                json!({
                    "pipeline_id": "a1b2c3d4e5f64789abcdef0123456789",
                    "message": "触发提醒", "user_id": "u1"
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "dispatched");
        assert_eq!(res["pipeline_id"], "a1b2c3d4e5f64789abcdef0123456789");
        let c = calls(&d);
        assert_eq!(c.len(), 1);
        assert_eq!(
            c[0].0, "thread-trig-1",
            "thread 槽位应是黑盒解析出的真实会话坐标，不是 pipeline_id"
        );
        assert_eq!(
            c[0].3, "a1b2c3d4e5f64789abcdef0123456789",
            "pipeline 槽位保持调用方原值"
        );
    }

    /// 有 store 无记录：孤儿/伪造 pipeline_id → 协议错误且未派发
    /// （原 bug 的静默行为变成显式拒绝——宁可报错也不把回复发到无人频道）。
    #[tokio::test]
    async fn inject_orphan_pipeline_id_rejected_with_protocol_error() {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let d = RecordingDispatcher::shared();
        let h = ChatSendHandler::with_store(d.clone(), Some(store));
        expect_protocol_error(
            &h,
            &d,
            json!({"pipeline_id": "ffffffffffffffffffffffffffffffff", "message": "m", "user_id": "u1"}),
            "孤儿 pipeline_id（pipeline_sessions 未命中）应拒绝",
        )
        .await;
    }

    /// background 注入同刀：坐标解析在派发前完成，后台线程拿到的也是真实 thread。
    #[tokio::test]
    async fn inject_background_resolves_real_thread_coordinate() {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        store
            .link_pipeline_session(
                "b1b2c3d4e5f64789abcdef0123456789",
                "thread-trig-2",
                "default",
            )
            .await
            .unwrap();
        let d = RecordingDispatcher::shared();
        let h = ChatSendHandler::with_store(d.clone(), Some(store));
        let res = h
            .handle(
                "send_message",
                json!({
                    "pipeline_id": "b1b2c3d4e5f64789abcdef0123456789",
                    "message": "重跑一轮", "user_id": "u1", "background": true
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "dispatched");
        // 后台派发最终执行，且 thread 槽位 = 解析出的真实坐标
        let mut rec: Option<DispatchRecord> = None;
        for _ in 0..40 {
            if let Some(r) = calls(&d).into_iter().next() {
                rec = Some(r);
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
        let rec = rec.expect("background 注入应最终派发");
        assert_eq!(
            rec.0, "thread-trig-2",
            "background 分支 thread 槽位同样解析"
        );
        assert_eq!(rec.3, "b1b2c3d4e5f64789abcdef0123456789");
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
            (
                json!({"parent_pipeline_id": "p1"}),
                "有父形式缺 origin_session_id",
            ),
            (
                json!({"origin_session_id": "s1"}),
                "有父形式缺 parent_pipeline_id",
            ),
            (
                json!({"parent_pipeline_id": "", "origin_session_id": "s1"}),
                "parent_pipeline_id 为空串",
            ),
            (json!({"root": false}), "root=false 且无父形式"),
            (json!({"root": true}), "根形式缺 origin"),
            (
                json!({"root": true, "origin": {}}),
                "根形式 origin 缺 kind/source",
            ),
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
            let mut params = json!({"pipeline_id": "pipe_1", "message": "m", "user_id": "u1"});
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
            let mut params = json!({"pipeline_id": "pipe_1", "message": "m", "user_id": "u1"});
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
            _agent_id: &str,
        ) -> Result<(), String> {
            self.gate.notified().await;
            self.calls
                .lock()
                .unwrap()
                .push(state_overlay.cloned().unwrap_or(Value::Null));
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
        let res = tokio::time::timeout(
            std::time::Duration::from_millis(300),
            h.handle(
                "send_message",
                json!({
                    "create": true, "message": "m", "user_id": "u1",
                    "background": true,
                    "lineage": {"parent_pipeline_id": "p", "origin_session_id": "s"},
                    "state": {"task.id": "t1"}
                }),
            ),
        )
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
        let outcome = tokio::time::timeout(
            std::time::Duration::from_millis(200),
            h.handle(
                "send_message",
                json!({
                    "create": true, "message": "m", "user_id": "u1",
                    "lineage": {"root": true, "origin": {"kind": "system", "source": "kernel"}}
                }),
            ),
        )
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
        assert_eq!(
            calls(&d)[0].6.as_ref().unwrap()["task.id"],
            pid,
            "引擎 id 覆盖调用方预传"
        );
    }

    #[tokio::test]
    async fn inject_background_returns_immediately() {
        // UI resume/retry 场景：注入已有管道也支持 background（fire-and-forget）
        let d = Arc::new(GatedDispatcher {
            calls: Mutex::new(Vec::new()),
            gate: tokio::sync::Notify::new(),
        });
        let h = ChatSendHandler::new(d.clone());
        let res = tokio::time::timeout(
            std::time::Duration::from_millis(300),
            h.handle(
                "send_message",
                json!({
                    "pipeline_id": "pipe_existing", "message": "重跑一轮",
                    "user_id": "u1", "background": true,
                }),
            ),
        )
        .await
        .expect("background 注入应在派发完成前返回")
        .unwrap();
        assert_eq!(res["status"], "dispatched");
        assert_eq!(res["pipeline_id"], "pipe_existing");
        assert!(d.calls.lock().unwrap().is_empty(), "派发应仍在挂起");
        d.gate.notify_one();
        let mut done = false;
        for _ in 0..40 {
            if !d.calls.lock().unwrap().is_empty() {
                done = true;
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
        assert!(done, "后台注入应最终执行");
    }
}
