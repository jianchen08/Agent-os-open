//! `chat` namespace capability handler——把"向会话投递消息并跑管道"暴露给 sidecar。
//!
//! 触发器（trigger_setup_tool）等 sidecar 到期触发时，经 `chat.send_message`
//! 复用前端同一条 WS 派发路径（`dispatch_user_input` → `process_via_engine`）：
//! 以触发消息为新一轮用户消息投给该会话 agent，agent 处理后流式回复前端。
//!
//! 坐标语义：对外契约只含 `pipeline_id + message + user_id`，
//! thread 是由坐标推导的派生物——注入分支在接口内部用 `pipeline_id` 反查
//! `pipeline_sessions` 解析真实会话 thread（黑盒），解析失败即协议错误；
//! 两个 id 形态不同（12hex vs `thread-` 前缀），绝不互填、不做参数搬运。
//!
//! GAP-1 阶段 1（管道创建契约）：`send_message` 额外支持——
//! - 可选 `state` 对象：自由注入 initial_state 顶层扁平键（任务域 `task.*`），
//!   经 dispatch → process_via_engine 的 execution_context 合并点（1a2）之后并入；
//!   保留字（message/pipeline_id 等）受保护，命中即协议错误。
//! - 创建分支（`create: true` 或 `pipeline_id` 为空）：引擎生成新 pipeline_id
//!   并在响应返回（`{"status":"created","pipeline_id":...}`）。任务域出生键
//!   （血缘 `lineage.*`/`task.id`）由调用方经 `state` overlay 透传、输入阶段
//!   插件（context_build）投影写入，内核对血缘方案零知识。
//!
//! sidecar 光有展示通道（event-bus.emit 往前端推事件）不能唤醒 agent 跑一轮，
//! 还需注入通道——本 handler 即该通道：经
//! [`CapabilityHandlerRegistry`] 注册（router 优先查它），不新建传输、不动 router 结构，
//! 仅把内核既有的 `PipelineDispatcher::dispatch_user_input` 桥接成 sidecar 可达的能力。

use std::sync::Arc;

use agentos_core::traits::StorageBackend;
use agentos_core::types::PendingInputSource;
use async_trait::async_trait;
use serde_json::{json, Value};

use agentos_mcp::{CapabilityHandler, McpError};
use agentos_session::router::PipelineDispatcher;

/// send_message 实际读取的顶层参数清单（防"配置↔代码"双轨漂移的代码侧锚点）。
///
/// 与 `config/kernel/kernel_capabilities/chat.json` 的 `input_schema.properties` 集合
/// 必须一致——一致性由 kernel_capabilities::tests 的机械闸强制：加参数不改契约
/// （或反之）测试即红。新增参数三处同步：本清单、契约文件、读取代码。
#[allow(dead_code)] // 消费方是 kernel_capabilities::tests 机械闸（测试期使用）
pub(crate) const HANDLED_PARAM_NAMES: &[&str] = &[
    "message",
    "user_id",
    "create",
    "background",
    "no_dispatch",
    "pipeline_id",
    "thread_id",
    "execution_context",
    "agent_id",
    "state",
];

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
    /// 会话协调器（WS 事件投递）。None = 未接线（单测/兼容路径），
    /// 后台派发失败仅告警不推通知。
    session: Option<Arc<agentos_session::SessionCoordinator>>,
}

impl ChatSendHandler {
    /// 生产构造：带存储，注入分支用 pipeline_id 反查所属会话的真实 thread
    /// （坐标解析封装在接口内部黑盒，对外契约不含 thread 参数）。
    /// 测试无存储场景传 `None`。
    pub fn with_store(
        dispatcher: Arc<dyn PipelineDispatcher>,
        store: Option<Arc<dyn StorageBackend>>,
    ) -> Self {
        Self {
            dispatcher,
            store,
            session: None,
        }
    }

    /// 生产构造（会话协调器接线版）：后台派发失败推 system_notification
    /// （统一错误模型：假成功显式化——调用方已拿到 dispatched，失败经 WS
    /// 事件补报，前端实时可见"任务未启动"）。
    pub fn with_session(
        dispatcher: Arc<dyn PipelineDispatcher>,
        store: Option<Arc<dyn StorageBackend>>,
        session: Arc<agentos_session::SessionCoordinator>,
    ) -> Self {
        Self {
            dispatcher,
            store,
            session: Some(session),
        }
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

/// `send_message` 顶层解析结果包（字段与 [`HANDLED_PARAM_NAMES`] 一一对应）。
///
/// 字符串/对象字段借用调用方 `params`（零拷贝）；`overlay` 是经
/// [`validate_state_overlay`] 校验的 state 注入副本，出生固化后随派发透传。
struct SendParams<'a> {
    message: &'a str,
    user_id: &'a str,
    create: bool,
    background: bool,
    no_dispatch: bool,
    supplied_pipeline_id: Option<&'a str>,
    /// 归属会话声明，仅创建分支消费（None → 以引擎新 id 兼作派发坐标）。
    ownership_thread: Option<&'a str>,
    execution_context: Option<&'a Value>,
    /// 显式指定的执行 agent（可选，默认 "agentos"）：任务派发按 target 选执行
    /// agent。仅作派发簿记（registry 登记/run 日志）——run 的执行身份已改为
    /// 管道 state 持久键 `agent.id`（出生方经 state 透传、context_build 与
    /// 内核工具面消费），派发不再注入身份。
    agent_id: String,
    overlay: Option<Value>,
}

impl ChatSendHandler {
    /// `chat.send_message`：投递消息到会话管道并跑一轮（GAP-1 阶段 1 管道创建契约）。
    ///
    /// 两种分支：
    /// - **注入分支**（带 `pipeline_id` 且非 `create`，现状不变）：消息投给已有管道。
    /// - **创建分支**（`create: true` 或 `pipeline_id` 为空）：引擎生成新 pipeline_id
    ///   （uuid v4 simple，与 sessions 的 active_pipeline_id 同格式）并以该 id 走
    ///   既有 dispatch——resolve 链路对陌生 id 落回退分支 ③ 原样穿透到引擎
    ///   `get_or_init` 新 state。不接受调用方传入 id（三次定案：堵 id 冒占）。
    ///
    /// 可选 `state` 对象：自由注入 initial_state 顶层扁平键（任务域用 `task.*`
    /// 点号键，与 track.total_tokens 同款约定），经 dispatch → process_via_engine
    /// 的 execution_context 合并点（1a2）之后并入；保留字命中即协议错误。
    async fn send_message(&self, params: Value) -> Result<Value, McpError> {
        // 阶段一：顶层参数解析与校验（纯校验零副作用，见 parse_send_params）。
        let mut p = Self::parse_send_params(&params)?;

        // 阶段二：创建/注入双分支坐标解析（见 resolve_send_targets）。
        let (pipeline_id, thread_id, created) = self.resolve_send_targets(&mut p).await?;

        if created {
            // 阶段三（仅创建分支）：pipeline↔thread 映射落库 + 出生字段
            // 持久化（B7：出生字段落库失败整体报错终止本次发送，不进入
            // 引擎执行；映射失败 warn 由引擎路径补写）。
            self.persist_created_pipeline(&p, &pipeline_id, &thread_id)
                .await?;
        }

        tracing::info!(
            target: "capability:chat",
            pipeline = %pipeline_id,
            thread = %thread_id,
            user = %p.user_id,
            msg_len = p.message.len(),
            created,
            agent = %p.agent_id,
            has_execution_context = p.execution_context.is_some(),
            has_state = p.overlay.is_some(),
            "chat.send_message 派发触发消息"
        );

        // 阶段四：no_dispatch 只登记不执行（容器任务等声明写入）。互斥与
        // 必带 state 的协议校验在 record_no_dispatch 内完成。
        if p.no_dispatch {
            return self.record_no_dispatch(&p, &pipeline_id, &thread_id).await;
        }

        // 复用 WS 派发：thread_id 与 pipeline_id 各归其位——注入分支是解析出的
        // 真实会话坐标（thread-xxx），创建分支是引擎新 id；pipeline_id 槽位
        // 始终保持调用方/引擎生成的原值，不做参数搬运。dispatch_user_input
        // 内部会 resolve 真实 route_id 并发 stream_start → process_via_engine →
        // new_message，前端按既有协议流式渲染回复。
        // tenant 由 dispatch_user_input 用 user_id 反查（与 WS 路径同源）。
        // thinking_strength：HTTP 通道暂不携带（"" = 引擎不覆盖参数）。
        if p.background {
            // 阶段五：后台派发，spawn 后响应立即返回（见 spawn_background_dispatch）。
            return self.spawn_background_dispatch(&p, &pipeline_id, &thread_id, created);
        }
        self.dispatcher
            .dispatch_user_input(
                &thread_id,
                p.user_id,
                p.message,
                &pipeline_id,
                "",
                p.execution_context,
                p.overlay.as_ref(),
                &p.agent_id,
                "",
                PendingInputSource::Trigger,
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
    /// 无 store 构造（with_store 传 None，单测/兼容路径）回退现状：
    /// pipeline 兼作派发键。
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

    /// 阶段一：解析并校验 send_message 顶层参数（纯校验零副作用——失败时
    /// 不产生任何状态变更，「校验失败不得派发」契约依赖于此）。
    fn parse_send_params(params: &Value) -> Result<SendParams<'_>, McpError> {
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
        let create = params
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
        // no_dispatch（可选，默认 false）：只把 state overlay 并入目标管道 state
        // （registry + pipeline_state 表），不派发执行、不产生消息。用于容器任务
        // 等"只登记不执行"的声明写入——提交者管道自持 task.owned.*，本管道插件
        // 也能读它处理它。与 background 互斥（background 是派发语义）。
        let no_dispatch = params
            .get("no_dispatch")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let supplied_pipeline_id = params
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty());
        // 归属会话（可选，仅创建分支消费）：调用方可声明新管道所属 thread（任务
        // 管道传用户会话，随其路由/级联删除）；缺省以引擎新 id 作派发坐标（兼容
        // 无会话上下文的调用，此类管道保持独立）。
        let ownership_thread = params
            .get("thread_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty());
        // 任务级 execution_context（可选）：任务执行器从 task.metadata 组装
        // （workspace_mode/isolation_level 等），随消息派发并入 initial_state，
        // init 体 workspace_lifecycle / environment_lifecycle 插件消费。
        let execution_context = params.get("execution_context").filter(|v| v.is_object());

        // agent_id（可选，默认 "agentos"）：任务派发按 target 选执行 agent——
        // 仅作派发簿记；agent yaml 加载（人格/tool_ids）归 context_build 插件，
        // run 执行身份读管道 state 持久键 agent.id。
        let agent_id = params
            .get("agent_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or("agentos")
            .to_string();

        // state 注入（可选）：校验保留字后作为 overlay 透传。
        let overlay = validate_state_overlay(params.get("state"))?;

        Ok(SendParams {
            message,
            user_id,
            create,
            background,
            no_dispatch,
            supplied_pipeline_id,
            ownership_thread,
            execution_context,
            agent_id,
            overlay,
        })
    }

    /// 阶段二：创建/注入双分支的派发坐标解析，返回 `(pipeline_id, thread_id, created)`。
    ///
    /// - **创建分支**（`create:true` 或缺 pipeline_id）：不接受调用方传入 id。
    ///   id 由引擎生成（uuid v4 simple 前 12 位 hex，与 sessions 的
    ///   active_pipeline_id 同格式）；thread 取归属会话声明，缺省以新 id 兼作
    ///   派发坐标（resolve 链路对陌生 id 落回退分支 ③ 原样穿透到引擎
    ///   get_or_init 新 state）。任务域出生键（lineage.*/task.id）由调用方经
    ///   `state` overlay 透传、输入阶段插件（context_build）投影，内核零知识。
    /// - **注入分支**（显式 pipeline_id 且非 create）：经黑盒反查真实 thread，
    ///   pipeline_id 保持原值。
    async fn resolve_send_targets(
        &self,
        p: &mut SendParams<'_>,
    ) -> Result<(String, String, bool), McpError> {
        if p.create || p.supplied_pipeline_id.is_none() {
            // ── 创建分支 ──
            if let Some(pid) = p.supplied_pipeline_id {
                return Err(McpError::Protocol {
                    message: format!(
                        "chat.send_message 创建分支（create:true）不接受调用方传入 \
                         pipeline_id（{pid}）——注入已有管道请去掉 create 并显式传 pipeline_id"
                    ),
                });
            }
            // 三次定案：pipeline_id 由引擎生成（身份权威统一），uuid v4 simple
            // 取前 12 位 hex（与 0.1 uuid4().hex[:12]、插件侧短 id 截断同长，
            // 前端展示 slice(0,12) 原样透出）。
            let pipeline_id = uuid::Uuid::new_v4().simple().to_string()[..12].to_string();
            // 新管道坐标：归属会话有声明则落真实 thread（任务管道随归属会话路由/
            // 级联，前端 runs 快照据此定位归属）；缺省以引擎新 id 兼作派发坐标。
            let thread_id = p
                .ownership_thread
                .map(str::to_string)
                .unwrap_or_else(|| pipeline_id.clone());
            Ok((pipeline_id, thread_id, true))
        } else if let Some(pid) = p.supplied_pipeline_id {
            // ── 注入分支 ──
            // 坐标解析（接口内部黑盒）：pipeline_id → 所属会话真实 thread。
            // thread 是派生物，对外契约不含该参数；解析失败 = 孤儿/伪造 id。
            let thread_id = self.resolve_inject_thread(pid).await?;
            Ok((pid.to_string(), thread_id, false))
        } else {
            unreachable!("supplied_pipeline_id 为 None 时已在创建分支生成 pipeline_id 返回")
        }
    }

    /// 阶段三（仅创建分支）：先落 pipeline↔thread 映射（F8），再批量 upsert
    /// 出生字段到 pipeline_state 表。
    ///
    /// 映射须先于派发写入，否则 dispatch 内 resolve_pipeline_id_for_thread 校验
    /// 必失败——每次任务派发刷一条“前端传来的 pipeline_id 不属于该 thread”误告警；
    /// 映射幂等（INSERT OR IGNORE）。出生即落表：引擎 persistent_fields 投影只覆盖
    /// 插件 manifest 声明键（track.* 等），任务域/血缘键不属于任何插件——不在此
    /// 落库则冷读（cold_state_row）无基线、registry 内存丢失后任务面板归属链整行
    /// 缺失（任务 running 但 state 聚合不出口）。映射失败 warn 不阻断（引擎路径
    /// 补写）；出生字段批量 upsert（单事务 all-or-nothing）任一失败整体返回能力
    /// 调用错误（B7，对齐 task_birth「禁止部分成功」契约）；无 store 构造整体跳过。
    async fn persist_created_pipeline(
        &self,
        p: &SendParams<'_>,
        pipeline_id: &str,
        thread_id: &str,
    ) -> Result<(), McpError> {
        let Some(store) = self.store.as_ref() else {
            return Ok(());
        };
        let tenant_id = agentos_http::auth::resolve_tenant_id_by_user(Some(store), p.user_id).await;
        if let Err(e) = store
            .link_pipeline_session(pipeline_id, thread_id, &tenant_id)
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
        // 出生字段持久化：overlay 全量批量 upsert（幂等，单事务 all-or-nothing）
        // ——调用方经 state 透传的出生键（task.*/lineage.*/agent.id）创建即落表，
        // 冷读归属链与续跑身份解析有基线。内核对键语义零知识，只做透传持久化。
        // B7：任一失败即整体返回能力调用错误（本段先于引擎执行，失败返回即安全
        // 终止）——对齐 plugins/shared/task_birth.py「禁止部分成功」契约，禁止
        // 吞掉后按部分成功继续（冷读丢基线 = 任务面板归属链整行缺失）。
        if let Some(overlay_obj) = p.overlay.as_ref().and_then(|o| o.as_object()) {
            store
                .upsert_state_fields(pipeline_id, &tenant_id, overlay_obj)
                .await
                .map_err(|e| {
                    tracing::error!(
                        target: "capability:chat",
                        pipeline = %pipeline_id,
                        error = %e.to_string(),
                        "chat.send_message 出生字段落库失败（终止本次发送，不进入引擎执行）"
                    );
                    McpError::Protocol {
                        message: format!("chat.send_message 出生字段落库失败: {e}"),
                    }
                })?;
        }
        // 归属锚点创建的收尾：切线程活跃管道。置于出生键落库之后——B7 中止
        // （出生字段落库失败）时指针不得已被切走（管道未出生即死，切了=悬空）。
        if let Some(thread) = p.ownership_thread {
            Self::switch_session_active_pipeline(store, thread, pipeline_id, &tenant_id).await;
        }
        Ok(())
    }

    /// 归属锚点创建（`thread_id` 声明归属会话）的收尾：把该线程的
    /// `sessions.active_pipeline_id` 切到新管道。
    ///
    /// 「任务落用户会话线程，对话在聊天区继续」的必要条件——消息查询端点
    /// （GET /sessions/{id}/messages 回退链）与会话列表都读该指针，只落
    /// pipeline_sessions 映射不切指针时，线程视角恒停旧聊天管道，任务消息
    /// （如 roleplay 开场白）对用户不可见。
    ///
    /// 语义：**创建即切、失败保持**——任务 failed 不切回，失败消息留在同
    /// 管道对用户可见（错误可见性）；只在调用方显式声明归属锚点时切，无锚点
    /// 管道保持独立（后台自主任务不进用户视图）。锚点线程无 sessions 行或
    /// 读写失败：warn 留痕不阻断出生——任务执行不依赖该指针，pipeline_sessions
    /// 映射与出生键才是 B7 契约面；不凭空造会话行（缺 title/metadata 来源）。
    async fn switch_session_active_pipeline(
        store: &Arc<dyn StorageBackend>,
        thread_id: &str,
        pipeline_id: &str,
        tenant_id: &str,
    ) {
        let tenant =
            agentos_core::types::TenantContext::new(tenant_id.to_string(), thread_id.to_string());
        let store = store.clone();
        let tid = thread_id.to_string();
        let pid = pipeline_id.to_string();
        let switched: Result<bool, agentos_core::types::StorageError> =
            agentos_tenant::scope(tenant, async move {
                let mut session = match store.get_session(&tid).await? {
                    Some(s) => s,
                    None => return Ok(false),
                };
                session.active_pipeline_id = Some(pid);
                session.updated_at = chrono::Utc::now().to_rfc3339();
                store.update_session(&session).await?;
                Ok(true)
            })
            .await;
        if let Ok(false) = switched {
            // 单行宏（rustfmt 不改写宏内排布）：保证改动行与宏执行区一致可度量。
            tracing::warn!(target: "capability:chat", thread = %thread_id, pipeline = %pipeline_id, "chat.send_message 归属锚点线程无 sessions 行，跳过活跃管道切换");
        }
        if let Err(e) = &switched {
            let reason = e.to_string();
            // 同上单行宏：错误留痕不阻断出生（视图便利面失败 ≠ 任务失败）。
            tracing::warn!(target: "capability:chat", thread = %thread_id, pipeline = %pipeline_id, error = %reason, "chat.send_message 切线程活跃管道失败（跳过，任务管道不受影响）");
        }
    }

    /// 阶段四：no_dispatch 只写 state 不派发（容器任务等"只登记不执行"的
    /// 声明写入）。目标管道 state 并入 overlay（registry 热路径 + pipeline_state
    /// 表冷兜底），不产生消息、不触发引擎执行——提交者管道自持 task.owned.*，
    /// 本管道插件也能读它处理它。返回 `recorded` 响应。
    async fn record_no_dispatch(
        &self,
        p: &SendParams<'_>,
        pipeline_id: &str,
        thread_id: &str,
    ) -> Result<Value, McpError> {
        if p.background {
            return Err(McpError::Protocol {
                message: "chat.send_message no_dispatch 与 background 互斥（no_dispatch 不派发）"
                    .to_string(),
            });
        }
        let Some(overlay) = p.overlay.as_ref() else {
            return Err(McpError::Protocol {
                message: "chat.send_message no_dispatch 必须携带 state（只写 state 不派发）"
                    .to_string(),
            });
        };
        let tenant_id =
            agentos_http::auth::resolve_tenant_id_by_user(self.store.as_ref(), p.user_id).await;
        let registry = agentos_session::pipeline_state_registry::global_registry();
        let entry = registry.get(&tenant_id, pipeline_id);
        if let Some(entry) = entry {
            let mut e = entry.write();
            if let Some(obj) = e.state.as_object_mut() {
                for (k, v) in overlay.as_object().unwrap_or(&serde_json::Map::new()) {
                    obj.insert(k.clone(), v.clone());
                }
            }
        } else {
            // 冷路径：registry 未注册（重启后未再轮）——以 overlay 为基底注册
            // 新条目（thread/agent 用坐标值），后续轮次 get_or_init 命中即读到。
            let mut base = serde_json::Map::new();
            for (k, v) in overlay.as_object().unwrap_or(&serde_json::Map::new()) {
                base.insert(k.clone(), v.clone());
            }
            registry.get_or_init(
                &tenant_id,
                pipeline_id,
                thread_id,
                &p.agent_id,
                serde_json::Value::Object(base),
            );
        }
        // 冷兜底持久化：pipeline_state 表逐键 upsert（重启后冷恢复读它）。
        if let Some(store) = self.store.as_ref() {
            for (k, v) in overlay.as_object().unwrap_or(&serde_json::Map::new()) {
                if let Err(e) = store
                    .upsert_state_field(pipeline_id, &tenant_id, k, v)
                    .await
                {
                    tracing::warn!(
                        target: "capability:chat",
                        pipeline = %pipeline_id,
                        key = %k,
                        error = %e.to_string(),
                        "chat.send_message no_dispatch state 持久化失败（内存态仍生效）"
                    );
                }
            }
        }
        Ok(json!({
            "status": "recorded",
            "pipeline_id": pipeline_id,
        }))
    }

    /// 阶段五：background 派发——spawn 后台执行、响应立即返回。任务管道在
    /// RunChain 上照常 FIFO 执行；创建分支调用方即刻拿到 created/pipeline_id，
    /// 注入分支（UI resume/retry）即刻返回 dispatched。
    ///
    /// 派发失败仅告警 + 会话协调器推 system_notification 补报（统一错误模型
    /// 假成功显式化）。record_id 为前端 system 气泡唯一 id 来源（lifecycleHandlers
    /// 拒绝缺失）；未落库——实时可见，刷新后由全量对账清理（无对应 API 记录即消失）。
    fn spawn_background_dispatch(
        &self,
        p: &SendParams<'_>,
        pipeline_id: &str,
        thread_id: &str,
        created: bool,
    ) -> Result<Value, McpError> {
        let dispatcher = self.dispatcher.clone();
        let pid = pipeline_id.to_string();
        let tid = thread_id.to_string();
        let uid = p.user_id.to_string();
        let msg = p.message.to_string();
        let ec = p.execution_context.cloned();
        let ov = p.overlay.clone();
        let aid = p.agent_id.clone();
        let session = self.session.clone();
        tokio::spawn(async move {
            if let Err(e) = dispatcher
                .dispatch_user_input(
                    &tid,
                    &uid,
                    &msg,
                    &pid,
                    "",
                    ec.as_ref(),
                    ov.as_ref(),
                    &aid,
                    "",
                    PendingInputSource::Trigger,
                )
                .await
            {
                tracing::error!(
                    pipeline = %pid,
                    thread = %tid,
                    error = %e,
                    "chat.send_message 后台派发失败（任务管道未启动）"
                );
                if let Some(session) = session {
                    let _ = session
                        .emit_event(
                            &tid,
                            "system_notification",
                            serde_json::json!({
                                "pipeline_id": pid,
                                "thread_id": tid,
                                "record_id": format!("n_{pid}_dispatch_failed"),
                                "notification_id": "dispatch_failed",
                                "notificationType": "dispatch_failed",
                                "level": "error",
                                "content": format!("任务派发失败，管道未启动：{e}"),
                                "sequence": null,
                            }),
                        )
                        .await;
                }
            }
        });
        Ok(json!({
            "status": if created { "created" } else { "dispatched" },
            "pipeline_id": pipeline_id,
        }))
    }
}

/// 校验并提取 `state` 注入（可选对象 → 透传 overlay）。
///
/// 键约定：顶层扁平点号键（任务域 `task.*`，与 track.total_tokens 同款——
/// STATE_SUMMARY_KEYS 匹配的就是这种顶层扁平键）。引擎系统保留字受保护
/// 前缀命中即 [`McpError::Protocol`]。清单单一真值源在
/// [`crate::kernel_capabilities`]（契约文件锚定，本处为直连路径的防线）。
fn validate_state_overlay(state: Option<&Value>) -> Result<Option<Value>, McpError> {
    use crate::kernel_capabilities::RESERVED_STATE_KEYS;

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

#[cfg(test)]
mod tests {
    //! GAP-1 阶段 1：chat.send_message 管道创建契约单测。
    //! 覆盖：创建分支 id 生成与响应、保留字拒绝、state overlay（含血缘扁平键）
    //! 透传与出生落表——lineage 方案由调用方插件自持，内核零知识。

    use super::*;
    use serde_json::json;
    use std::sync::{Arc, Mutex};

    /// 单次 dispatch_user_input 记录（全参快照）：
    /// (thread_id, user_id, content, pipeline_id, thinking, execution_context, state_overlay, agent_id)
    type DispatchRecord = (
        String,
        String,
        String,
        String,
        String,
        Option<Value>,
        Option<Value>,
        String,
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
            agent_id: &str,
            _cmid: &str,
            _source: PendingInputSource,
        ) -> Result<(), String> {
            self.calls.lock().unwrap().push((
                thread_id.to_string(),
                user_id.to_string(),
                content.to_string(),
                pipeline_id.to_string(),
                thinking_strength.to_string(),
                execution_context.cloned(),
                state_overlay.cloned(),
                agent_id.to_string(),
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

        async fn dispatch_stop(&self, _thread_id: &str, _pipeline_id: &str) -> Result<(), String> {
            Ok(())
        }
    }

    /// 短 id 格式性质断言：12 位 hex 无连字符（uuid v4 simple 前 12 位）。
    fn assert_simple_uuid_v4(s: &str) {
        assert_eq!(s.len(), 12, "短 id 应为 12 位 hex 无连字符: {s}");
        assert!(
            s.chars().all(|c| c.is_ascii_hexdigit()),
            "应全为 hex 字符: {s}"
        );
    }

    fn handler() -> (ChatSendHandler, Arc<RecordingDispatcher>) {
        let d = RecordingDispatcher::shared();
        (ChatSendHandler::with_store(d.clone(), None), d)
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
        // 无 store 构造（with_store 传 None，单测/兼容路径）：注入坐标回退
        // pipeline 兼作 thread 派发键。生产注册点走 with_store + Some(store)
        // （见下方坐标解析三连测试）。
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
    async fn inject_passes_state_overlay() {
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
    }

    // ── 注入分支坐标解析 ──────────────────────────────────────
    // chat_send_handler 不得把同一个 pipeline_id 复制填进 dispatch 的
    // thread_id 与 pipeline_id 两个槽位——事件会发到无人订阅的频道（12hex 键
    // 在 registry 只注册 thread-xxx），表现为「LLM 日志有、前端收不到回复」。
    // 坐标解析封装在接口黑盒内（pipeline_sessions 反查真实 thread），
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
    async fn create_branch_persists_birth_overlay_to_state_table() {
        // 任务归属链语义：出生字段（lineage.*/task.* 扁平键）创建即落
        // pipeline_state 表——不落库则 registry 内存丢失后（重启）冷读
        // 无基线，任务面板归属链整行缺失。未知 user 回退 default 租户。
        let d = RecordingDispatcher::shared();
        let store: Arc<dyn StorageBackend> =
            Arc::new(agentos_engine::SqliteStore::open_memory().expect("open_memory"));
        let h = ChatSendHandler::with_store(d.clone(), Some(store.clone()));
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true,
                    "background": true,
                    "message": "执行任务「调研」。",
                    "user_id": "user_birth_persist",
                    "state": {"task.goal": "调研", "task.status": "pending",
                              "task.parent_project_id": "proj0011",
                              "lineage.parent_pipeline_id": "pipe_parent",
                              "lineage.origin_session_id": "th_1"},
                }),
            )
            .await
            .unwrap();
        let pid = res["pipeline_id"].as_str().unwrap().to_string();
        let fields = store
            .load_pipeline_state(&pid, "default")
            .await
            .expect("出生字段应已落 pipeline_state 表");
        let get = |k: &str| fields.get(k).expect(k);
        assert_eq!(get("task.goal"), "调研");
        assert_eq!(get("task.status"), "pending");
        assert_eq!(get("task.parent_project_id"), "proj0011");
        assert_eq!(get("lineage.parent_pipeline_id"), "pipe_parent");
        assert_eq!(get("lineage.origin_session_id"), "th_1");
    }

    // ── 执行 agent 身份（2026-08-31：身份属管道 state，派发只管簿记）──
    // 事故回归锚 75a097118e75：task_manage 催促（无 agent_id）把 general_agent
    // 子任务重跑成 agentos——run 执行身份不再取派发参数，改读管道 state 持久键
    // agent.id（出生方经 state 透传，context_build 与内核工具面消费）。

    #[tokio::test]
    async fn create_branch_persists_agent_identity_from_state_overlay() {
        // 出生身份由出生方经 state 透传（agent.id 自由字段，task_birth 单点
        // 写入）→ 内核零知识透传持久化；续跑由执行面从 state 读身份。
        let d = RecordingDispatcher::shared();
        let store: Arc<dyn StorageBackend> =
            Arc::new(agentos_engine::SqliteStore::open_memory().expect("open_memory"));
        let h = ChatSendHandler::with_store(d.clone(), Some(store.clone()));
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true,
                    "message": "执行任务「测试工具」。",
                    "user_id": "u_birth_agent",
                    "state": {"task.status": "pending", "agent.id": "general_agent"},
                }),
            )
            .await
            .unwrap();
        let pid = res["pipeline_id"].as_str().unwrap().to_string();
        let fields = store
            .load_pipeline_state(&pid, "default")
            .await
            .expect("出生字段应已落表");
        assert_eq!(
            fields
                .get("agent.id")
                .expect("agent.id 应随出生 state 落快照"),
            "general_agent"
        );
    }

    #[tokio::test]
    async fn create_branch_unspecified_agent_leaves_snapshot_absent() {
        // 出生方未透传 agent.id（会话类管道）→ 快照无该键；内核不得擅自写身份。
        // 身份缺省（主 agent）由消费面自持，不靠出生写面兜底。
        let d = RecordingDispatcher::shared();
        let store: Arc<dyn StorageBackend> =
            Arc::new(agentos_engine::SqliteStore::open_memory().expect("open_memory"));
        let h = ChatSendHandler::with_store(d.clone(), Some(store.clone()));
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true,
                    "message": "你好",
                    "user_id": "u_birth_default",
                }),
            )
            .await
            .unwrap();
        let pid = res["pipeline_id"].as_str().unwrap().to_string();
        let fields = store.load_pipeline_state(&pid, "default").await.unwrap();
        assert!(
            !fields.contains_key("agent.id"),
            "内核不得擅自写身份键：{:?}",
            fields.get("agent.id")
        );
    }

    #[tokio::test]
    async fn inject_branch_agent_param_is_bookkeeping_only() {
        // 注入分支（task_manage 催促形态：无 agent_id → 缺省 agentos）：
        // 该值只进派发簿记槽位，run 身份由管道 state agent.id 决定——
        // 催促方不带身份不再改变子任务的执行 agent。
        let (h, d) = handler();
        h.handle(
            "send_message",
            json!({
                "pipeline_id": "pipe_task_1",
                "message": "继续推进任务",
                "user_id": "task_manage",
            }),
        )
        .await
        .unwrap();
        assert_eq!(calls(&d)[0].7, "agentos");

        h.handle(
            "send_message",
            json!({
                "pipeline_id": "pipe_task_2",
                "message": "执行任务。",
                "user_id": "task_system",
                "agent_id": "general_agent",
            }),
        )
        .await
        .unwrap();
        assert_eq!(calls(&d)[1].7, "general_agent", "显式值原样透传簿记");
    }

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
                    "state": {"task.goal": "喝水提醒", "task.status": "pending",
                              "lineage.parent_pipeline_id": "pipe_parent",
                              "lineage.origin_session_id": "sess_root"}
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
        // overlay：task.* 自由键 + lineage 扁平键（调用方经 state 透传）
        let overlay = c[0].6.clone().expect("state overlay 应透传");
        assert_eq!(overlay["task.goal"], "喝水提醒");
        assert_eq!(overlay["task.status"], "pending");
        assert_eq!(overlay["lineage.parent_pipeline_id"], "pipe_parent");
        assert_eq!(overlay["lineage.origin_session_id"], "sess_root");
    }

    #[tokio::test]
    async fn create_with_ownership_thread_links_real_session() {
        // 创建分支携带归属会话（任务管道透传用户会话）：thread 槽位 = 调用方
        // 声明会话（不再自环），pipeline_sessions 落真实映射可反查；pipeline
        // 槽位保持引擎生成值。缺省（不传）行为不变 = 自环（create_with_flag 用例）。
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let d = RecordingDispatcher::shared();
        let h = ChatSendHandler::with_store(d.clone(), Some(store.clone()));
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true,
                    "message": "执行任务",
                    "user_id": "u1",
                    "thread_id": "thread-user-1",
                    "state": {"task.goal": "g"}
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "created");
        let pid = res["pipeline_id"].as_str().unwrap().to_string();
        let c = calls(&d);
        assert_eq!(c.len(), 1);
        assert_eq!(
            c[0].0, "thread-user-1",
            "thread 槽位应是调用方归属会话，不是管道自身（自环已按可选 thread_id 修正）"
        );
        assert_eq!(c[0].3, pid, "pipeline 槽位保持引擎生成值");
        // 映射落库：pipeline_sessions 反查命中真实会话（前端 runs 快照据此归属）
        let stored = store.get_thread_id_by_pipeline(&pid).await.unwrap();
        assert_eq!(stored.as_deref(), Some("thread-user-1"));
    }

    #[tokio::test]
    async fn create_without_flag_when_pipeline_id_absent() {
        // pipeline_id 缺省即隐式创建（"或 pipeline_id 为空"）；血缘扁平键随
        // state 透传（根形式声明由调用方插件构造，内核不解释）
        let (h, d) = handler();
        let res = h
            .handle(
                "send_message",
                json!({
                    "message": "m",
                    "user_id": "u1",
                    "state": {
                        "lineage.root": true,
                        "lineage.origin.kind": "channel",
                        "lineage.origin.source": "dingtalk"
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
                    json!({"create": true, "message": "m", "user_id": "u1"}),
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

    #[tokio::test]
    async fn create_rejects_caller_supplied_pipeline_id() {
        // 三次定案：创建路径不接受调用方传入 id（堵 id 冒占）
        let (h, d) = handler();
        expect_protocol_error(
            &h,
            &d,
            json!({
                "create": true, "pipeline_id": "pipe_mine",
                "message": "m", "user_id": "u1"
            }),
            "create 与显式 pipeline_id 互斥",
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
    async fn state_lineage_prefixed_keys_accepted() {
        // 血缘方案归插件自持后，lineage.* 扁平键经 state 透传放行（内核不解释）
        let (h, d) = handler();
        let res = h
            .handle(
                "send_message",
                json!({
                    "pipeline_id": "pipe_1", "message": "m", "user_id": "u1",
                    "state": {"lineage.root": true,
                              "lineage.parent_pipeline_id": "pipe_parent",
                              "lineage.origin.kind": "plugin"}
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "dispatched");
        let overlay = calls(&d)[0].6.clone().expect("lineage.* 应透传");
        assert_eq!(overlay["lineage.root"], true);
        assert_eq!(overlay["lineage.parent_pipeline_id"], "pipe_parent");
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
            json!({"create": true, "user_id": "u1"}),
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
                    "state": {"task.id": "t9"}
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
        // task.id 调用方预传值原样透传（内核零解释）
        assert_eq!(c[0].6.as_ref().unwrap()["task.id"], "t9");
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
            _cmid: &str,
            _source: PendingInputSource,
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
        async fn dispatch_stop(&self, _thread_id: &str, _pipeline_id: &str) -> Result<(), String> {
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
        let h = ChatSendHandler::with_store(d.clone(), None);
        let res = tokio::time::timeout(
            std::time::Duration::from_millis(300),
            h.handle(
                "send_message",
                json!({
                    "create": true, "message": "m", "user_id": "u1",
                    "background": true,
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
        assert_eq!(d.calls.lock().unwrap()[0]["task.id"], "t1", "任务域键透传");
    }

    #[tokio::test]
    async fn create_foreground_waits_for_dispatch_by_default() {
        // 默认（background 缺省）：维持既有语义——await 派发完成再返回
        // （触发器通知等需要投递确认的调用方）。派发挂起 → 超时打红。
        let d = Arc::new(GatedDispatcher {
            calls: Mutex::new(Vec::new()),
            gate: tokio::sync::Notify::new(),
        });
        let h = ChatSendHandler::with_store(d.clone(), None);
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
    async fn create_passes_caller_supplied_task_id_through() {
        // 内核对任务域键零知识：task.id 原样透传；身份权威（task.id == 引擎
        // pipeline_id）由输入阶段插件 context_build 落实（插件侧测试覆盖）
        let (h, d) = handler();
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true, "message": "m", "user_id": "u1",
                    "state": {"task.id": "fake_id_999", "task.goal": "g"}
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "created");
        assert_eq!(
            calls(&d)[0].6.as_ref().unwrap()["task.id"],
            "fake_id_999",
            "内核不解释任务域键，原样透传"
        );
    }

    #[tokio::test]
    async fn inject_background_returns_immediately() {
        // UI resume/retry 场景：注入已有管道也支持 background（fire-and-forget）
        let d = Arc::new(GatedDispatcher {
            calls: Mutex::new(Vec::new()),
            gate: tokio::sync::Notify::new(),
        });
        let h = ChatSendHandler::with_store(d.clone(), None);
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

    // ── no_dispatch：只写 state 不派发（容器任务等"只登记不执行"的声明写入）──

    /// no_dispatch 注入：state overlay 并入目标管道 state（registry 热路径），
    /// 不派发执行、不产生消息；响应 status=recorded。
    #[tokio::test]
    async fn no_dispatch_writes_state_without_dispatch() {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        // 注入分支坐标解析：目标管道须已落 pipeline_sessions 映射（真实会话）
        store
            .link_pipeline_session(
                "a1b2c3d4e5f64789abcdef0123456789",
                "thread-owner-1",
                "default",
            )
            .await
            .unwrap();
        let d = RecordingDispatcher::shared();
        let h = ChatSendHandler::with_store(d.clone(), Some(store.clone()));
        let res = h
            .handle(
                "send_message",
                json!({
                    "pipeline_id": "a1b2c3d4e5f64789abcdef0123456789",
                    "message": "登记容器任务",
                    "user_id": "u1",
                    "no_dispatch": true,
                    "state": {
                        "task.owned.c1d2e3f4a5b64789abcdef0123456789.title": "容器项目",
                        "task.owned.c1d2e3f4a5b64789abcdef0123456789.status": "active",
                        "task.owned.c1d2e3f4a5b64789abcdef0123456789.submitted_by": "u1",
                    },
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "recorded");
        assert_eq!(res["pipeline_id"], "a1b2c3d4e5f64789abcdef0123456789");
        // 未派发：dispatcher 零调用
        assert!(d.calls.lock().unwrap().is_empty(), "no_dispatch 不得派发");
        // registry 热路径：state 已并入（get_or_init 冷注册后读回）
        let registry = agentos_session::pipeline_state_registry::global_registry();
        let entry = registry
            .get("default", "a1b2c3d4e5f64789abcdef0123456789")
            .expect("no_dispatch 应注册/更新 registry 条目");
        let st = entry.read();
        assert_eq!(
            st.state["task.owned.c1d2e3f4a5b64789abcdef0123456789.title"],
            "容器项目"
        );
        assert_eq!(
            st.state["task.owned.c1d2e3f4a5b64789abcdef0123456789.submitted_by"],
            "u1"
        );
        // 冷兜底：pipeline_state 表已持久化（重启后冷恢复读它）
        let fields = store
            .load_pipeline_state("a1b2c3d4e5f64789abcdef0123456789", "default")
            .unwrap();
        assert_eq!(
            fields["task.owned.c1d2e3f4a5b64789abcdef0123456789.title"],
            "容器项目"
        );
    }

    /// no_dispatch 与 background 互斥（no_dispatch 不派发，background 是派发语义）。
    #[tokio::test]
    async fn no_dispatch_conflicts_with_background() {
        let (h, d) = handler();
        expect_protocol_error(
            &h,
            &d,
            json!({
                "pipeline_id": "pipe_1", "message": "m", "user_id": "u1",
                "no_dispatch": true, "background": true,
                "state": {"task.owned.x.title": "t"}
            }),
            "no_dispatch 与 background 互斥",
        )
        .await;
    }

    /// no_dispatch 缺 state：无内容可写，协议错误。
    #[tokio::test]
    async fn no_dispatch_requires_state() {
        let (h, d) = handler();
        expect_protocol_error(
            &h,
            &d,
            json!({"pipeline_id": "pipe_1", "message": "m", "user_id": "u1", "no_dispatch": true}),
            "no_dispatch 必须携带 state",
        )
        .await;
    }

    // ── 统一错误模型：background 派发失败补报 system_notification（P1a）──

    /// 恒失败派发器（模拟任务管道未启动）。
    struct FailingDispatcher {
        err: String,
    }

    #[async_trait]
    impl PipelineDispatcher for FailingDispatcher {
        async fn dispatch_user_input(
            &self,
            _t: &str,
            _u: &str,
            _c: &str,
            _p: &str,
            _ts: &str,
            _ec: Option<&Value>,
            _ov: Option<&Value>,
            _a: &str,
            _cmid: &str,
            _source: PendingInputSource,
        ) -> Result<(), String> {
            Err(self.err.clone())
        }
        async fn dispatch_interaction_response(
            &self,
            _t: &str,
            _r: &str,
            _resp: &Value,
        ) -> Result<(), String> {
            Ok(())
        }
        async fn dispatch_stop(&self, _t: &str, _p: &str) -> Result<(), String> {
            Ok(())
        }
    }

    /// 捕获型 EventSink：记录收到的全部文本帧。
    struct CapturingSink {
        frames: Arc<Mutex<Vec<String>>>,
    }

    #[async_trait]
    impl agentos_session::EventSink for CapturingSink {
        async fn send_text(&self, text: &str) -> bool {
            self.frames.lock().unwrap().push(text.to_string());
            true
        }
        fn id(&self) -> u64 {
            42
        }
    }

    /// background 派发失败：调用方仍拿 dispatched（响应后失败无法携带），
    /// 但经 WS system_notification 补报——前端实时可见"任务未启动"
    /// （统一错误模型：假成功显式化）。
    #[tokio::test]
    async fn background_dispatch_failure_emits_system_notification() {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let session = Arc::new(agentos_session::SessionCoordinator::new());
        let frames = Arc::new(Mutex::new(Vec::<String>::new()));
        session.register_thread("thread-fail-1", "u1");
        session.register(
            "u1",
            Arc::new(CapturingSink {
                frames: frames.clone(),
            }),
        );

        let dispatcher: Arc<dyn PipelineDispatcher> = Arc::new(FailingDispatcher {
            err: "pipeline init failed: engine not available".into(),
        });
        let h = ChatSendHandler::with_session(dispatcher, Some(store), session);

        let res = h
            .handle(
                "send_message",
                json!({"create": true, "message": "m", "user_id": "u1",
                       "thread_id": "thread-fail-1",
                       "background": true,
                       "lineage": {"root": true, "origin": {"kind": "system", "source": "gate"}}}),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "created", "后台派发响应立即返回: {res}");
        // 等 spawn 的派发失败补报完成（真实异步：轮询帧到达）
        let frames_arc = frames.clone();
        tokio::time::timeout(std::time::Duration::from_secs(5), async {
            loop {
                if !frames_arc.lock().unwrap().is_empty() {
                    break;
                }
                tokio::time::sleep(std::time::Duration::from_millis(20)).await;
            }
        })
        .await
        .expect("5s 内应收到 system_notification 帧");

        let frames = frames.lock().unwrap();
        let last = frames.last().unwrap();
        let frame: Value = serde_json::from_str(last).unwrap();
        assert_eq!(frame["type"], "system_notification");
        let data = &frame["data"];
        assert_eq!(data["level"], "error");
        assert_eq!(data["notificationType"], "dispatch_failed");
        assert!(
            data["content"].as_str().unwrap().contains("管道未启动"),
            "content 应含失败原因: {data}"
        );
    }

    /// 成功派发不产生 system_notification（补报只对失败路径）。
    #[tokio::test]
    async fn background_dispatch_success_no_notification() {
        let session = Arc::new(agentos_session::SessionCoordinator::new());
        let frames = Arc::new(Mutex::new(Vec::<String>::new()));
        session.register_thread("thread-ok-1", "u1");
        session.register(
            "u1",
            Arc::new(CapturingSink {
                frames: frames.clone(),
            }),
        );

        let d = RecordingDispatcher::shared();
        let dispatcher: Arc<dyn PipelineDispatcher> = d.clone();
        let h = ChatSendHandler::with_session(dispatcher, None, session);

        let res = h
            .handle(
                "send_message",
                json!({"create": true, "message": "m", "user_id": "u1",
                       "thread_id": "thread-ok-1",
                       "background": true,
                       "lineage": {"root": true, "origin": {"kind": "system", "source": "gate"}}}),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "created");
        // 给 spawn 留出执行窗口（成功路径无帧）
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        assert!(
            frames.lock().unwrap().is_empty(),
            "成功派发不应产生通知: {:?}",
            frames.lock().unwrap()
        );
    }

    // ── B7：出生字段落库失败整体上抛（先于引擎执行，失败返回即安全终止）──

    /// 出生字段批量 upsert 恒故障的存储 mock：link_pipeline_session 正常
    ///（warn 面）、用户解析面正常回 None（回退 default 租户）、批量出生字段
    /// 恒失败。其余方法 unreachable 桩（意外耦合即炸出）。
    struct FailingBirthStore;

    #[async_trait]
    impl StorageBackend for FailingBirthStore {
        async fn upsert_state_fields(
            &self,
            _pipeline_id: &str,
            _tenant_id: &str,
            _fields: &serde_json::Map<String, serde_json::Value>,
        ) -> Result<(), agentos_core::types::StorageError> {
            Err(agentos_core::types::StorageError::Database(
                "injected birth field failure".to_string(),
            ))
        }
        async fn link_pipeline_session(
            &self,
            _pipeline_id: &str,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn get_user_by_id(
            &self,
            _user_id: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            Ok(None)
        }
        async fn get_run(
            &self,
            _run_id: &str,
        ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn get_messages_by_pipeline(
            &self,
            _pipeline_id: &str,
            _opts: agentos_core::traits::MessageQueryOpts,
        ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError>
        {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn get_blob(
            &self,
            _blob_id: &str,
        ) -> Result<Vec<u8>, agentos_core::types::StorageError> {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn append_trace(
            &self,
            _entry: agentos_core::types::TraceEntry,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn update_run_status(
            &self,
            _run_id: &str,
            _status: agentos_core::types::RunStatus,
            _branch: Option<&str>,
            _seq: Option<u32>,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn create_run(
            &self,
            _run_id: &str,
            _config_hash: &str,
            _tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn store_blob(
            &self,
            _data: &[u8],
            _mime_type: &str,
        ) -> Result<String, agentos_core::types::StorageError> {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn create_session(
            &self,
            _session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn get_session(
            &self,
            _thread_id: &str,
        ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn list_sessions(
            &self,
            _filter: agentos_core::traits::SessionListFilter,
        ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn update_session(
            &self,
            _session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn delete_session(
            &self,
            _thread_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn get_step_traces_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError>
        {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn get_step_traces_by_pipeline(
            &self,
            _pipeline_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError>
        {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn create_user(
            &self,
            _user: &agentos_core::types::UserRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn get_user_by_username(
            &self,
            _username: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn list_users(
            &self,
        ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn update_last_login(
            &self,
            _user_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
        async fn update_user_password(
            &self,
            _user_id: &str,
            _password_hash: &str,
            _must_change_password: bool,
        ) -> Result<bool, agentos_core::types::StorageError> {
            unreachable!("mock 不提供口令更新")
        }
        async fn delete_user(
            &self,
            _user_id: &str,
        ) -> Result<bool, agentos_core::types::StorageError> {
            unreachable!("出生字段失败路径不应触碰其他存储方法")
        }
    }

    #[tokio::test]
    async fn create_branch_birth_field_failure_aborts_before_dispatch() {
        // B7 契约（对齐 task_birth「禁止部分成功」）：出生字段落库失败 →
        // 整体返回能力调用错误，且引擎未启动（派发零调用）——本段先于引擎
        // 执行，失败返回即安全终止，冷读不再丢出生基线。
        let d = RecordingDispatcher::shared();
        let store: Arc<dyn StorageBackend> = Arc::new(FailingBirthStore);
        let h = ChatSendHandler::with_store(d.clone(), Some(store));
        let err = h
            .handle(
                "send_message",
                json!({
                    "create": true,
                    "message": "执行任务「调研」。",
                    "user_id": "user_birth_fail",
                    "state": {"task.goal": "调研", "task.status": "pending"},
                }),
            )
            .await
            .expect_err("出生字段落库失败必须整体报错");
        assert!(
            matches!(err, McpError::Protocol { .. }),
            "应为能力调用协议错误，实际 {err:?}"
        );
        assert!(
            d.calls.lock().unwrap().is_empty(),
            "出生字段失败不得进入引擎执行（派发零调用）"
        );
    }

    #[tokio::test]
    async fn create_branch_birth_success_still_dispatches_foreground() {
        // 对照组（第二组输入）：真实 store + 前台创建 → 出生字段落表且正常派发
        // （批量接口替换后成功路径行为不变）。
        let d = RecordingDispatcher::shared();
        let store: Arc<dyn StorageBackend> =
            Arc::new(agentos_engine::SqliteStore::open_memory().expect("open_memory"));
        let h = ChatSendHandler::with_store(d.clone(), Some(store.clone()));
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true,
                    "message": "前台任务",
                    "user_id": "user_birth_ok",
                    "state": {"task.status": "pending", "lineage.parent_pipeline_id": "pipe_p"},
                }),
            )
            .await
            .expect("出生字段成功应正常创建派发");
        assert_eq!(res["status"], "created");
        let pid = res["pipeline_id"].as_str().unwrap().to_string();
        let fields = store
            .load_pipeline_state(&pid, "default")
            .await
            .expect("出生字段应已落表");
        assert_eq!(
            fields.get("task.status"),
            Some(&serde_json::json!("pending"))
        );
        assert_eq!(
            fields.get("lineage.parent_pipeline_id"),
            Some(&serde_json::json!("pipe_p"))
        );
        assert_eq!(calls(&d).len(), 1, "成功路径前台派发恰好一次");
    }
    // ── 覆盖率补测批：no_dispatch 热路径/留痕 + background 失败面 + 出生落库 ──

    /// no_dispatch 命中 registry 已有条目（热路径）→ 键并入既有 state，
    /// 而非以 overlay 为基底重建（避免顶掉运行期累积的其它键）。
    #[tokio::test]
    async fn no_dispatch_merges_into_existing_registry_entry() {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let pid = format!("pipe_merge_{}", uuid::Uuid::new_v4().simple());
        store
            .link_pipeline_session(&pid, "thread-merge-1", "default")
            .await
            .unwrap();
        // 预置热条目：已有运行期累积键（overlay 里没有）
        let registry = agentos_session::pipeline_state_registry::global_registry();
        registry.get_or_init(
            "default",
            &pid,
            "thread-merge-1",
            "agentos",
            json!({"task.status": "running", "turn": 7}),
        );

        let d = RecordingDispatcher::shared();
        let h = ChatSendHandler::with_store(d.clone(), Some(store.clone()));
        let res = h
            .handle(
                "send_message",
                json!({
                    "pipeline_id": pid,
                    "message": "更新登记态",
                    "user_id": "u1",
                    "no_dispatch": true,
                    "state": {"task.result.summary": "阶段完成"},
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "recorded");
        assert!(d.calls.lock().unwrap().is_empty(), "no_dispatch 不得派发");

        let entry = registry.get("default", &pid).expect("条目应在");
        let st = entry.read();
        assert_eq!(
            st.state["task.result.summary"], "阶段完成",
            "overlay 键写入既有条目"
        );
        assert_eq!(
            st.state["task.status"], "running",
            "既有键保留（合并非替换）"
        );
        assert_eq!(st.state["turn"], 7, "运行期累积键不得被顶掉");
        registry.remove("default", &pid);
    }

    /// 只让 `upsert_state_field` 失败、其余转发真实库的探针 store。
    struct UpsertFailStore {
        inner: Arc<agentos_engine::SqliteStore>,
    }

    #[async_trait]
    impl StorageBackend for UpsertFailStore {
        async fn upsert_state_field(
            &self,
            _pipeline_id: &str,
            _tenant_id: &str,
            _key: &str,
            _value: &Value,
        ) -> Result<(), agentos_core::types::StorageError> {
            Err(agentos_core::types::StorageError::Database(
                "injected upsert failure".to_string(),
            ))
        }
        async fn get_run(
            &self,
            run_id: &str,
        ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
            self.inner.get_run(run_id).await
        }
        async fn get_messages_by_pipeline(
            &self,
            pipeline_id: &str,
            opts: agentos_core::traits::MessageQueryOpts,
        ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError>
        {
            self.inner.get_messages_by_pipeline(pipeline_id, opts).await
        }
        async fn get_blob(
            &self,
            blob_id: &str,
        ) -> Result<Vec<u8>, agentos_core::types::StorageError> {
            self.inner.get_blob(blob_id).await
        }
        async fn append_trace(
            &self,
            entry: agentos_core::types::TraceEntry,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner.append_trace(entry).await
        }
        async fn update_run_status(
            &self,
            run_id: &str,
            status: agentos_core::types::RunStatus,
            branch: Option<&str>,
            seq: Option<u32>,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner
                .update_run_status(run_id, status, branch, seq)
                .await
        }
        async fn create_run(
            &self,
            run_id: &str,
            config_hash: &str,
            tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::create_run(
                self.inner.as_ref(),
                run_id,
                config_hash,
                tenant_id,
            )
            .await
        }
        async fn store_blob(
            &self,
            data: &[u8],
            mime_type: &str,
        ) -> Result<String, agentos_core::types::StorageError> {
            agentos_core::traits::StorageBackend::store_blob(self.inner.as_ref(), data, mime_type)
                .await
        }
        async fn create_session(
            &self,
            session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner.create_session(session).await
        }
        async fn get_session(
            &self,
            thread_id: &str,
        ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            self.inner.get_session(thread_id).await
        }
        async fn list_sessions(
            &self,
            filter: agentos_core::traits::SessionListFilter,
        ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            self.inner.list_sessions(filter).await
        }
        async fn update_session(
            &self,
            session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner.update_session(session).await
        }
        async fn delete_session(
            &self,
            thread_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            self.inner.delete_session(thread_id).await
        }
        async fn link_pipeline_session(
            &self,
            pipeline_id: &str,
            thread_id: &str,
            tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner
                .link_pipeline_session(pipeline_id, thread_id, tenant_id)
                .await
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            thread_id: &str,
            tenant_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            self.inner
                .list_pipeline_ids_by_thread(thread_id, tenant_id)
                .await
        }
        async fn get_thread_id_by_pipeline(
            &self,
            pipeline_id: &str,
        ) -> Result<Option<String>, agentos_core::types::StorageError> {
            self.inner.get_thread_id_by_pipeline(pipeline_id).await
        }
        async fn get_step_traces_by_thread(
            &self,
            thread_id: &str,
            tenant_id: &str,
        ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError>
        {
            self.inner
                .get_step_traces_by_thread(thread_id, tenant_id)
                .await
        }
        async fn create_user(
            &self,
            user: &agentos_core::types::UserRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner.create_user(user).await
        }
        async fn get_user_by_id(
            &self,
            user_id: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            self.inner.get_user_by_id(user_id).await
        }
        async fn get_user_by_username(
            &self,
            username: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            self.inner.get_user_by_username(username).await
        }
        async fn list_users(
            &self,
        ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            self.inner.list_users().await
        }
        async fn update_last_login(
            &self,
            user_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.inner.update_last_login(user_id).await
        }
        async fn update_user_password(
            &self,
            user_id: &str,
            password_hash: &str,
            must_change_password: bool,
        ) -> Result<bool, agentos_core::types::StorageError> {
            self.inner
                .update_user_password(user_id, password_hash, must_change_password)
                .await
        }
        async fn delete_user(
            &self,
            user_id: &str,
        ) -> Result<bool, agentos_core::types::StorageError> {
            self.inner.delete_user(user_id).await
        }
    }

    /// no_dispatch 的 state 持久化失败：内存态已生效 → 只 warn 留痕，
    /// 调用方仍拿 recorded（冷恢复会缺这批键，故留痕必须可诊断）。
    #[tokio::test]
    async fn no_dispatch_persist_failure_warns_but_reports_recorded() {
        let (guard, logs) = crate::test_env::capture_logs();
        let _ = &guard;
        let inner = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let pid = format!("pipe_nd_fail_{}", uuid::Uuid::new_v4().simple());
        inner
            .link_pipeline_session(&pid, "thread-nd-fail", "default")
            .await
            .unwrap();
        let store: Arc<dyn StorageBackend> = Arc::new(UpsertFailStore {
            inner: inner.clone(),
        });

        let d = RecordingDispatcher::shared();
        let h = ChatSendHandler::with_store(d.clone(), Some(store));
        let res = h
            .handle(
                "send_message",
                json!({
                    "pipeline_id": pid,
                    "message": "登记容器任务",
                    "user_id": "u1",
                    "no_dispatch": true,
                    "state": {"task.owned.nodispatch_fail.title": "容器项目"},
                }),
            )
            .await
            .expect("no_dispatch 持久化失败不得上抛（内存态仍生效）");
        let text = logs.text();

        assert_eq!(res["status"], "recorded");
        assert!(d.calls.lock().unwrap().is_empty(), "no_dispatch 不得派发");
        let registry = agentos_session::pipeline_state_registry::global_registry();
        let entry = registry.get("default", &pid).expect("内存态应已注册");
        assert_eq!(
            entry.read().state["task.owned.nodispatch_fail.title"],
            "容器项目",
            "持久化失败不影响内存态"
        );
        assert!(
            text.contains("no_dispatch state 持久化失败"),
            "持久化失败必须 warn 留痕: {text}"
        );
        registry.remove("default", &pid);
    }

    /// 创建分支出生字段落库失败 → 显式终止本次发送（不得静默丢出生字段：
    /// 任务血缘/归属依赖它，缺了后续轮次读不到）。
    #[tokio::test]
    async fn create_birth_persist_failure_terminates_send() {
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        sqlite
            .with_conn::<(), String>(|conn| {
                conn.execute("DROP TABLE pipeline_state", [])
                    .map(|_| ())
                    .map_err(|e| e.to_string())
            })
            .unwrap();
        let store: Arc<dyn StorageBackend> = sqlite;
        let d = RecordingDispatcher::shared();
        let h = ChatSendHandler::with_store(d.clone(), Some(store));
        let err = h
            .handle(
                "send_message",
                json!({
                    "create": true,
                    "message": "登记容器任务",
                    "user_id": "u1",
                    "state": {"task.owned.birth_fail.title": "容器项目"},
                }),
            )
            .await
            .expect_err("出生字段落库失败必须终止本次发送");
        assert!(
            format!("{err}").contains("出生字段落库失败"),
            "错误须指明失败环节: {err}"
        );
        assert!(d.calls.lock().unwrap().is_empty(), "终止即不得派发");
    }

    /// background 派发失败但 session 未接线：只 error 留痕，不 panic、无补报。
    #[tokio::test]
    async fn background_dispatch_failure_without_session_only_logs() {
        let (guard, logs) = crate::test_env::capture_logs();
        let _ = &guard;
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let dispatcher: Arc<dyn PipelineDispatcher> = Arc::new(FailingDispatcher {
            err: "engine not available".into(),
        });
        let h = ChatSendHandler::with_store(dispatcher, Some(store));
        let res = h
            .handle(
                "send_message",
                json!({"create": true, "message": "后台任务", "user_id": "u1",
                       "background": true}),
            )
            .await
            .unwrap();
        assert_eq!(
            res["status"], "created",
            "响应后失败无法携带，仍返回 created"
        );
        // 后台派发是 spawn：有界等待留痕落地（真实时钟，10ms 步长）
        let mut waited = 0;
        while !logs.text().contains("后台派发失败（任务管道未启动）") && waited < 200
        {
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            waited += 1;
        }
        let text = logs.text();
        assert!(
            text.contains("chat.send_message 后台派发失败（任务管道未启动）"),
            "后台派发失败必须 error 留痕: {text}"
        );
    }

    /// state overlay 含空串键 → 协议错误（空键会污染点号键空间且无法表达）。
    #[tokio::test]
    async fn state_empty_string_key_rejected() {
        let (h, d) = handler();
        expect_protocol_error(
            &h,
            &d,
            json!({
                "create": true, "message": "m", "user_id": "u1",
                "state": {"": "x"},
            }),
            "state 键不得为空串",
        )
        .await;
        // 第二组（有区分度输入）：空键与合法键混排也必须整体拒绝（不得部分写入）
        expect_protocol_error(
            &h,
            &d,
            json!({
                "create": true, "message": "m", "user_id": "u1",
                "state": {"task.goal": "正常目标", "": "空"},
            }),
            "混排空键同样拒绝",
        )
        .await;
    }

    // ── BUG-45 断点2：归属锚点创建同步切线程活跃管道 ──────────────
    //
    // 消息查询端点回退链读 sessions.active_pipeline_id；归属锚点创建只落
    // pipeline_sessions 映射不切指针时，线程视角恒停旧聊天管道——任务消息
    // （如 roleplay 开场白）对用户不可见。语义：创建即切、失败保持（不切回
    // ——失败任务的消息留在同管道继续可见；不为切回加 run 终态钩子）；
    // 只切显式声明归属锚点（thread_id）的创建，无锚点管道保持独立。

    /// sessions 行测试夹具：thread + 活跃管道指针。
    async fn seed_session(store: &std::sync::Arc<dyn StorageBackend>, thread: &str, active: &str) {
        store
            .create_session(&agentos_core::types::SessionRecord {
                thread_id: thread.to_string(),
                title: None,
                intent: None,
                current_state: "active".to_string(),
                agent_id: None,
                active_pipeline_id: Some(active.to_string()),
                pipeline_ids: vec![active.to_string()],
                metadata: None,
                created_at: "2026-09-18T00:00:00Z".to_string(),
                updated_at: "2026-09-18T00:00:00Z".to_string(),
                last_active_at: None,
            })
            .await
            .unwrap();
    }

    /// 归属锚点创建：线程的 active_pipeline_id 必须切到新任务管道。
    #[tokio::test]
    async fn create_with_ownership_thread_switches_session_active_pipeline() {
        let store: Arc<dyn StorageBackend> =
            Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        seed_session(&store, "thread-anchor-1", "p_old_chat").await;
        let d = RecordingDispatcher::shared();
        let h = ChatSendHandler::with_store(d.clone(), Some(store.clone()));
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true, "message": "登记任务", "user_id": "u1",
                    "thread_id": "thread-anchor-1",
                    "state": {"task.goal": "g"}
                }),
            )
            .await
            .unwrap();
        let pid = res["pipeline_id"].as_str().unwrap().to_string();
        let sess = store.get_session("thread-anchor-1").await.unwrap().unwrap();
        assert_eq!(
            sess.active_pipeline_id.as_deref(),
            Some(pid.as_str()),
            "归属锚点创建必须把线程活跃管道切到新管道（对话在聊天区继续的前提）"
        );
    }

    /// 切换只作用于声明的锚点线程：其他线程指针不受牵连（两组有区分度输入）。
    #[tokio::test]
    async fn create_pointer_switch_is_scoped_to_anchor_thread() {
        let store: Arc<dyn StorageBackend> =
            Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        seed_session(&store, "thread-anchor-a", "p_chat_a").await;
        seed_session(&store, "thread-bystander-b", "p_chat_b").await;
        let d = RecordingDispatcher::shared();
        let h = ChatSendHandler::with_store(d.clone(), Some(store.clone()));
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true, "message": "登记任务", "user_id": "u1",
                    "thread_id": "thread-anchor-a",
                    "state": {"task.goal": "g"}
                }),
            )
            .await
            .unwrap();
        let pid = res["pipeline_id"].as_str().unwrap();
        let anchored = store.get_session("thread-anchor-a").await.unwrap().unwrap();
        assert_eq!(anchored.active_pipeline_id.as_deref(), Some(pid));
        let bystander = store
            .get_session("thread-bystander-b")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            bystander.active_pipeline_id.as_deref(),
            Some("p_chat_b"),
            "未锚定线程的活跃管道不得被牵连切换"
        );
    }

    /// 无锚点创建（后台自主任务）：任何 sessions 行都不得被改——
    /// 独立任务不进用户会话视图。
    #[tokio::test]
    async fn create_without_ownership_thread_leaves_sessions_untouched() {
        let store: Arc<dyn StorageBackend> =
            Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        seed_session(&store, "thread-user-keep", "p_main").await;
        let d = RecordingDispatcher::shared();
        let h = ChatSendHandler::with_store(d.clone(), Some(store.clone()));
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true, "message": "自主任务", "user_id": "u1",
                    "state": {"task.goal": "g", "lineage.root": true}
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "created");
        let sess = store
            .get_session("thread-user-keep")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            sess.active_pipeline_id.as_deref(),
            Some("p_main"),
            "无锚点创建不得切任何线程指针"
        );
    }

    /// 锚点线程无 sessions 行（陈旧锚/伪造）：跳过切换不阻断出生——
    /// 任务执行不依赖该指针，不得凭空造会话行，也不得让出生失败。
    #[tokio::test]
    async fn create_with_unknown_anchor_thread_skips_pointer_switch() {
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let d = RecordingDispatcher::shared();
        let h = ChatSendHandler::with_store(d.clone(), Some(store.clone()));
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true, "message": "登记任务", "user_id": "u1",
                    "thread_id": "thread-ghost-1",
                    "state": {"task.goal": "g"}
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "created", "指针切换跳过不得阻断出生");
        assert!(
            store.get_session("thread-ghost-1").await.unwrap().is_none(),
            "不得凭空创建锚点线程的 sessions 行"
        );
    }

    /// 锚点线程会话读写失败（存储故障）：warn 留痕不阻断出生——指针是视图
    /// 便利面，pipeline_sessions 映射与出生键才是 B7 契约面。
    #[tokio::test]
    async fn create_pointer_switch_storage_failure_does_not_abort_birth() {
        let (guard, logs) = crate::test_env::capture_logs();
        let _ = &guard;
        let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        sqlite
            .with_conn::<(), String>(|conn| {
                conn.execute("DROP TABLE sessions", [])
                    .map(|_| ())
                    .map_err(|e| e.to_string())
            })
            .unwrap();
        let store: Arc<dyn StorageBackend> = sqlite;
        let d = RecordingDispatcher::shared();
        let h = ChatSendHandler::with_store(d.clone(), Some(store));
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true, "message": "登记任务", "user_id": "u1",
                    "thread_id": "thread-fail-sw",
                    "state": {"task.goal": "g"}
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "created", "指针切换失败不得阻断出生");
        assert!(
            logs.text().contains("切线程活跃管道失败"),
            "指针切换失败必须 warn 留痕"
        );
    }

    /// 无 store 构造（单测/兼容路径）+ 归属锚点：无持久化面可切，安静返回，
    /// 出生照常（响应 created）。
    #[tokio::test]
    async fn create_with_anchor_and_no_store_still_creates() {
        let (h, d) = handler(); // with_store(None)
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true, "message": "登记任务", "user_id": "u1",
                    "thread_id": "thread-nostore-1",
                    "state": {"task.goal": "g"}
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "created", "无 store 不得阻断出生");
        assert_eq!(calls(&d).len(), 1, "派发照常");
    }

    /// 语义钉子（创建即切、失败保持）：后台派发失败后指针保持在新任务管道——
    /// 失败消息留在同管道对用户可见（错误可见性），不切回旧聊天管道。
    #[tokio::test]
    async fn pointer_stays_switched_when_background_dispatch_fails() {
        let store: Arc<dyn StorageBackend> =
            Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        seed_session(&store, "thread-fail-keep", "p_old_chat").await;
        let session = Arc::new(agentos_session::SessionCoordinator::new());
        let frames = Arc::new(Mutex::new(Vec::<String>::new()));
        session.register_thread("thread-fail-keep", "u1");
        session.register(
            "u1",
            Arc::new(CapturingSink {
                frames: frames.clone(),
            }),
        );
        let dispatcher: Arc<dyn PipelineDispatcher> = Arc::new(FailingDispatcher {
            err: "pipeline init failed".into(),
        });
        let h = ChatSendHandler::with_session(dispatcher, Some(store.clone()), session);
        let res = h
            .handle(
                "send_message",
                json!({
                    "create": true, "message": "m", "user_id": "u1",
                    "thread_id": "thread-fail-keep", "background": true,
                    "state": {"task.goal": "g"}
                }),
            )
            .await
            .unwrap();
        let pid = res["pipeline_id"].as_str().unwrap().to_string();
        // 等后台派发失败补报完成（真实异步：轮询帧到达）
        let frames_arc = frames.clone();
        tokio::time::timeout(std::time::Duration::from_secs(5), async {
            loop {
                if !frames_arc.lock().unwrap().is_empty() {
                    break;
                }
                tokio::time::sleep(std::time::Duration::from_millis(20)).await;
            }
        })
        .await
        .expect("5s 内应收到 dispatch_failed 补报帧");
        let sess = store
            .get_session("thread-fail-keep")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            sess.active_pipeline_id.as_deref(),
            Some(pid.as_str()),
            "创建即切、失败保持：派发失败不得把指针切回旧聊天管道"
        );
    }
}
