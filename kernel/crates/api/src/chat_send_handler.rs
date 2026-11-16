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
//!   插件（agent_config_load）投影写入，内核对血缘方案零知识。
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
    "pipeline_config_id",
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
    /// 管道 state 持久键 `agent.id`（出生方经 state 透传、agent_config_load 与
    /// 内核工具面消费），派发不再注入身份。
    agent_id: String,
    /// 显式指定的管道配置（config/pipelines/{id}.yaml）：按需加载编译该配置
    /// 执行；None = autonomous 缺省路径。
    pipeline_config_id: Option<String>,
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
        // 前台派发同刀2：注入身份优先作为派发 user（本 task 在桥 scope 内，
        // current() 直读即可；spawn 面见 spawn_background_dispatch）。
        let effective_user = agentos_tenant::current()
            .filter(|c| c.user_id.as_deref().is_some_and(|u| !u.is_empty()))
            .and_then(|c| c.user_id)
            .filter(|u| !u.is_empty())
            .unwrap_or_else(|| p.user_id.to_string());
        if p.background {
            // 阶段五：后台派发，spawn 后响应立即返回（见 spawn_background_dispatch）。
            return self
                .spawn_background_dispatch(&p, &pipeline_id, &thread_id, created)
                .await;
        }
        self.dispatcher
            .dispatch_user_input(
                &thread_id,
                &effective_user,
                p.message,
                &pipeline_id,
                "",
                p.execution_context,
                p.overlay.as_ref(),
                &p.agent_id,
                p.pipeline_config_id.as_deref(),
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
        // user_id：身份形参在反调注入面可省略（刀2，ADR
        // docs/decisions/2026-10-17-reverse-call-user-identity-injection.md）——
        // 插件经桥 scope 携带内核下发的执行上下文身份，user 以注入身份为准，
        // 形参不可指派。空 user_id + 无注入身份 → 派发前预检同步拒绝
        // （spawn_background_dispatch / 前台 dispatch 的 user→tenant 解析）。
        let user_id = params.get("user_id").and_then(|v| v.as_str()).unwrap_or("");
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
        // 仅作派发簿记；agent yaml 加载（人格/tool_ids）归 agent_config_load 插件，
        // run 执行身份读管道 state 持久键 agent.id。
        let agent_id = params
            .get("agent_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or("agentos")
            .to_string();

        // pipeline_config_id（可选）：显式指定执行管道的配置，按需加载编译缓存。
        // 形态校验对齐 WS router 同款（valid_config_id），非法按缺失 = autonomous
        // 缺省路径（可选提示性参数不得让消息投递失败）。
        let pipeline_config_id = params
            .get("pipeline_config_id")
            .and_then(|v| v.as_str())
            .filter(|s| agentos_session::router::valid_config_id(s))
            .map(str::to_string);

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
            pipeline_config_id,
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
    ///   `state` overlay 透传、输入阶段插件（agent_config_load）投影，内核零知识。
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
        // 租户解析失败拒继续（K8 修订 ADR 2026-10-10）：出生字段落库/映射
        // 落库都带租户坐标，解析不出真租户就写 = 数据串入 default 租户。
        let tenant_id =
            match agentos_http::auth::resolve_tenant_id_by_user(Some(store), p.user_id).await {
                Ok(t) => t,
                Err(e) => {
                    tracing::error!(
                        target: "capability:chat",
                        user_id = %p.user_id,
                        error = %e,
                        "chat.send_message 租户解析失败，拒绝创建分支（fail-closed）"
                    );
                    return Err(McpError::Protocol {
                        message: format!("chat.send_message 租户解析失败: {e}"),
                    });
                }
            };
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
        // 租户解析失败拒登记（K8 修订 ADR 2026-10-10）：no_dispatch 的 state
        // 落库带租户坐标，解析不出真租户就写 = 数据串入 default 租户。
        let tenant_id =
            match agentos_http::auth::resolve_tenant_id_by_user(self.store.as_ref(), p.user_id)
                .await
            {
                Ok(t) => t,
                Err(e) => {
                    tracing::error!(
                        target: "capability:chat",
                        user_id = %p.user_id,
                        error = %e,
                        "chat.send_message no_dispatch 租户解析失败，拒绝登记（fail-closed）"
                    );
                    return Err(McpError::Protocol {
                        message: format!("chat.send_message no_dispatch 租户解析失败: {e}"),
                    });
                }
            };
        // 真值先落库：pipeline_state 表批量 upsert（重启后冷恢复读它），单事务
        // 全量 upsert 不留「DB 半套」。写失败 = 登记失败（可重试）：只回内存会让
        // 「已 recorded」的声明在重启后蒸发，不再静默放行。
        if let Some(store) = self.store.as_ref() {
            store
                .upsert_state_fields(
                    pipeline_id,
                    &tenant_id,
                    overlay.as_object().unwrap_or(&serde_json::Map::new()),
                )
                .await
                .map_err(|e| {
                    tracing::error!(
                        target: "capability:chat",
                        pipeline = %pipeline_id,
                        error = %e.to_string(),
                        "chat.send_message no_dispatch state 持久化失败（登记失败，请重试）"
                    );
                    McpError::Protocol {
                        message: format!("chat.send_message no_dispatch state 持久化失败: {e}"),
                    }
                })?;
        }
        // 真值落库成功后才并 registry（缓存不得领先真值）。
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
        Ok(json!({
            "status": "recorded",
            "pipeline_id": pipeline_id,
        }))
    }

    /// 阶段五：background 派发——spawn 后台执行、响应立即返回。任务管道在
    /// RunChain 上照常 FIFO 执行；创建分支调用方即刻拿到 created/pipeline_id，
    /// 注入分支（UI resume/retry）即刻返回 dispatched。
    ///
    /// 身份注入面（刀2，ADR docs/decisions/2026-10-17-reverse-call-user-
    /// identity-injection.md）：task_local 身份 scope 跨 `tokio::spawn` 丢失，
    /// 须在本函数（桥 scope 内）先捕获注入身份，闭包内 `agentos_tenant::scope`
    /// 重建后派发——`dispatch_user_input` 的注入分支据此以内核下发身份为准，
    /// 插件手写 user_id 不可指派。无注入身份（外部调用面）→ spawn 前**同步**
    /// 执行 user→tenant 解析预检：解析失败错误返回调用方（消灭「工具报成功、
    /// 实际零派发」假成功——1017 task_manage continue 事故根因面）。
    ///
    /// 预检后的派发失败仅告警 + 会话协调器推 system_notification 补报（统一
    /// 错误模型假成功显式化）。record_id 为前端 system 气泡唯一 id 来源
    /// （lifecycleHandlers 拒绝缺失）；未落库——实时可见，刷新后由全量对账
    /// 清理（无对应 API 记录即消失）。
    async fn spawn_background_dispatch(
        &self,
        p: &SendParams<'_>,
        pipeline_id: &str,
        thread_id: &str,
        created: bool,
    ) -> Result<Value, McpError> {
        let injected_identity = agentos_tenant::current()
            .filter(|c| c.user_id.as_deref().is_some_and(|u| !u.is_empty()));
        if injected_identity.is_none() {
            agentos_http::auth::resolve_tenant_id_by_user(self.store.as_ref(), p.user_id)
                .await
                .map_err(|e| {
                    tracing::error!(
                        target: "capability:chat",
                        pipeline = %pipeline_id,
                        user = %p.user_id,
                        error = %e,
                        "chat.send_message 派发预检失败（user→tenant 不可解析，未派发）"
                    );
                    McpError::Protocol {
                        message: format!(
                            "chat.send_message 派发预检失败（user_id 不可解析，未派发）: {e}"
                        ),
                    }
                })?;
        }
        // 派发簿记归属（pending record / run chain 优先级）以有效用户为准：
        // 注入身份优先，形参仅外部面生效。dispatch_user_input 内部还有同序
        // 注入分支（纵深，覆盖直达路径）。
        let effective_user = injected_identity
            .as_ref()
            .and_then(|c| c.user_id.as_deref())
            .filter(|u| !u.is_empty())
            .unwrap_or(p.user_id);
        let dispatcher = self.dispatcher.clone();
        let pid = pipeline_id.to_string();
        let tid = thread_id.to_string();
        let uid = effective_user.to_string();
        let msg = p.message.to_string();
        let ec = p.execution_context.cloned();
        let ov = p.overlay.clone();
        let aid = p.agent_id.clone();
        let pcid = p.pipeline_config_id.clone();
        let session = self.session.clone();
        tokio::spawn(async move {
            let dispatch = dispatcher.dispatch_user_input(
                &tid,
                &uid,
                &msg,
                &pid,
                "",
                ec.as_ref(),
                ov.as_ref(),
                &aid,
                pcid.as_deref(),
                "",
                PendingInputSource::Trigger,
            );
            // task_local 不跨 spawn：捕获的注入身份在此重建（外部面为 None，
            // 直跑——dispatch 内部走 user→tenant 解析分支）。
            let dispatched = match injected_identity {
                Some(ctx) => agentos_tenant::scope(ctx, dispatch).await,
                None => dispatch.await,
            };
            if let Err(e) = dispatched {
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
mod tests;
