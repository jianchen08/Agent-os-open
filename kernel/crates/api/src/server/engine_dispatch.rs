//! 引擎执行编排（chat 请求 → executor.run → 终止事件派发）。

use super::history_recover::stage_recover_history;
use super::history_recover::stage_refresh_registry_snapshot;
use super::stage_exec::stage_execute;
use super::stage_exec::stage_finalize;
use super::stages::stage_build_initial_state;
use super::*;
use agentos_core::types::DispatchRequest;
/// 引擎执行结果。
///
/// - `content`：纯文本回复（raw_result/message 提取），HTTP/回退路径直接消费；
/// - `final_assistant`：本轮最终 assistant 消息的完整持久形态（含
///   `tool_calls`/`reasoning_content`/`seq`），WS 路径 new_message 携带它，
///   前端用与 DB 加载相同的 mapper 生成 parts——流式事件与 DB 冷热同构；
/// - `failed`：executor.run 返回 Err（WS 路径据此推 stream_error 收尾）。
/// - `plugin_errors`：本轮管道执行中插件失败（result.error / invoker Err）的
///   收集（引擎 warn+继续的假成功显式化）——WS 路径发射 plugin_error 事件
///   弹前端通知；HTTP 路径无消费面（REST chat 同步返回，错误进日志）。
#[derive(Debug, Clone)]
pub(crate) struct EngineOutcome {
    pub content: String,
    pub final_assistant: Option<serde_json::Value>,
    pub failed: bool,
    /// 降级应答标记（前置依赖缺失 echo_fallback）：调用方据此与正常回复区分，
    /// 不再把降级应答当成功处理（2026-08-26 统一错误模型：假成功显式化）。
    pub degraded: bool,
    /// 插件执行错误收集（`[{plugin_id, code, message}]`，引擎 `_plugin_errors` 键提取）。
    pub plugin_errors: Vec<serde_json::Value>,
    /// 轮边界注入消费的队列条目 cmid（引擎 `_consumed_pending_cmids` 键提取，
    /// 设计真值 docs/working/待输入队列轮边界注入_方案_20261025.md）：这些条目
    /// 不经链级 drain 独立成轮，run 收尾时调用方对每个 cmid 通知 REST /chat
    /// 同步桥等待者（同一 outcome），防等待者挂到超时层。
    pub consumed_pending_cmids: Vec<String>,
}

impl EngineOutcome {
    /// 终止型出口信封：无 assistant 产出、非降级、零插件错误的失败/拒绝构造
    /// （前置校验拒绝、引擎 Err 防御、落库重试耗尽等出口共用，消除同形字面量）。
    pub(crate) fn terminated(content: impl Into<String>) -> Self {
        Self {
            content: content.into(),
            final_assistant: None,
            failed: true,
            degraded: false,
            plugin_errors: Vec::new(),
            consumed_pending_cmids: Vec::new(),
        }
    }
}

pub(crate) async fn process_via_engine(
    state: &AppState,
    request: &DispatchRequest,
    agent_id: &str,
    message_id: &str,
) -> EngineOutcome {
    // Box::pin 到堆上：回写段 + executor.run 的深 sidecar 调用链让 Future 状态机
    // 在 release 下也接近 tokio worker 2MB 栈极限，堆分配规避溢出。
    Box::pin(process_via_engine_inner(
        state, request, agent_id, message_id,
    ))
    .await
}

#[inline(never)]
pub(crate) async fn process_via_engine_inner(
    state: &AppState,
    request: &DispatchRequest,
    agent_id: &str,
    message_id: &str,
) -> EngineOutcome {
    // 请求面字段一次性取本地名（下游编排段沿用原名，减少逐字段搬运噪音）。
    let message = request.content.as_str();
    let pipeline_id = request.pipeline_id.as_str();
    let thread_id = request.thread_id.as_str();
    let user_id = request.user_id.as_str();
    let execution_context = request.execution_context.as_ref();
    let state_overlay = request.state_overlay.as_ref();
    let pipeline_config_id = request.pipeline_config_id.as_deref();
    let client_message_id = request.client_message_id.as_str();
    // ── 前置依赖：invoker / store / project_root 任一缺席 → echo 降级 ──
    let Some(invoker) = state.invoker.clone() else {
        return echo_fallback("engine not available", message);
    };
    let Some(store) = state.store.clone() else {
        return echo_fallback("store not available", message);
    };
    let Some(project_root) = state.project_root.clone() else {
        return echo_fallback("project_root not configured", message);
    };

    // 0. 统一管道查询键：pipeline_id 是执行态唯一坐标，缺失即拒绝——会话
    //    thread_id 是组织集合 id，不得充当执行坐标（2026-08-30 用户裁定：
    //    thread 回退曾让消息/state 落进以 thread id 为键的幽灵管道）。
    let effective_pipeline_id = if pipeline_id.is_empty() {
        return EngineOutcome::terminated(
            "[error] 缺少 pipeline_id，拒绝派发（会话 id 不得充当管道执行坐标）",
        );
    } else {
        pipeline_id
    };
    // 已删除管道拒绝执行（删除终态）：删除提交后，派发链里可能还有已受理的
    // 排队消息；再跑一轮只会产生被写屏障丢弃的空转、重新长出 registry 幽灵
    // 条目，并继续执行工具副作用。
    if agentos_engine::deleted_pipelines::is_deleted(effective_pipeline_id) {
        return EngineOutcome::terminated("[已删除] 管道已删除，本轮不执行");
    }
    // 缺失租户上下文回退 default 是契约内兜底（非错误路径）；debug 留痕供
    // 排查"消息落错租户"类问题时定位缺失点。
    let tenant = agentos_tenant::current().unwrap_or_else(|| {
        tracing::debug!("process_via_engine 无租户上下文，回退 default/kernel");
        TenantContext::new("default", "kernel")
    });
    let tenant_id = tenant.tenant_id.clone();

    // run_id 权威生成点：注入 initial_state（插件侧 llm_core 取消轮询的定位锚，
    // 见 聊天中断保留与重新生成回退方案 §四.1），并贯穿到 executor 构造——同一
    // run 的两个消费面（state 轮询锚 vs runs 表落库）共享一个 uuid。
    let run_id = uuid::Uuid::new_v4().to_string();

    // 1/1a/1a2. 初始 state 构造（含会话级/任务级 execution_context 注入）。
    let initial_state = stage_build_initial_state(
        state,
        &store,
        message,
        effective_pipeline_id,
        thread_id,
        message_id,
        user_id,
        execution_context,
        &run_id,
    )
    .await;

    // 1b. 多轮上下文装配（热路径 registry / 冷路径 checkpoint+traces+state+messages）
    //     + 本轮 user 消息入账（metadata 携带 client_message_id 幂等键，
    //     ADR 2026-08-21：随消息落库回显，前端据此对账去重乐观消息）。
    //     skip_user_append：regenerate 重跑标志（批次 D，overlay 顶层键），
    //     目标 user 消息已在截断后历史中，本轮不重复 append。
    let skip_user_append = state_overlay
        .and_then(|o| o.get("_skip_user_append"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    // P1-4 声明化：收集全部插件 manifest `state.volatile_keys` 声明并集，
    // 与内核自有键同语义参与恢复合并跳过（checkpoint 落档剥离侧由
    // stage_execute 将同一并集注入 store 消费）。
    let declared_volatile: std::collections::HashSet<String> = state
        .manifests
        .read()
        .await
        .iter()
        .filter_map(|m| m.state.as_ref())
        .flat_map(|s| s.volatile_keys.iter().cloned())
        .collect();
    // B3：user 消息落库失败（重试耗尽）在此上抛终止本轮发送——不进入引擎执行，
    // failed outcome 经既有 WS 错误通道（run_pipeline_round → stream_error）回显。
    let initial_state = match stage_recover_history(
        initial_state,
        &store,
        message,
        effective_pipeline_id,
        &tenant_id,
        client_message_id,
        skip_user_append,
        state_overlay,
        &declared_volatile,
        &run_id,
    )
    .await
    {
        Ok(s) => s,
        Err(outcome) => return outcome,
    };

    // 直跑早回执（聊天消息流权威化设计真值 docs/working/聊天消息流权威化重设计_20261027.md
    // 不变量 1）：触发 user 消息此刻已带权威 seq/message_id 落库，即时回传认领
    // 载荷——前端气泡只认引擎确认（claimUserMessage 同一条认领链），亚秒级上屏
    // 替代乐观气泡。regenerate 重跑（skip_user_append）目标消息已在场，不发。
    if !skip_user_append && !client_message_id.is_empty() {
        if let Some(session) = state.session.as_ref() {
            if let Some(first) = initial_state
                .get("_pending_message_ops")
                .and_then(|v| v.as_array())
                .and_then(|a| a.first())
            {
                let payload = serde_json::json!({
                    "pipeline_id": effective_pipeline_id,
                    "thread_id": thread_id,
                    "client_message_id": client_message_id,
                    "record": {
                        "id": first.get("message_id").cloned().unwrap_or_default(),
                        "sequence": first.get("seq").cloned().unwrap_or(serde_json::json!(0)),
                        "content": message,
                    },
                    "_threadId": thread_id,
                });
                let _ = session
                    .emit_event(thread_id, "user_message_confirmed", payload)
                    .await;
            }
        }
    }

    // 2a. 实际状态轮中可见（2026-09-03 双状态裁定）：上一轮 final_state 若滞留
    //     registry，轮中可观测性（/pipelines/state）会读到陈旧终态——「显示已
    //     结束但还在输出」。dispatch 在引擎执行前用带 run_status=running 的
    //     initial_state 覆写 registry（热路径本就自 registry 恢复，内容等价、
    //     控制键已刷新）。条目不存在（管道首轮）跳过——stage_finalize 收尾补建。
    stage_refresh_registry_snapshot(&tenant_id, effective_pipeline_id, &initial_state);

    // 2/2b. agent 配置加载与工具面注入均归插件域：agent_config_load 按 state 持久键
    //    agent.id 加载 agent yaml 并写 state.tool_ids，tool_schema 插件经内核
    //    tool-surface capability 取过滤后 schema（prepare 链每轮刷新）。

    // 3. 构造 PipelineExecutor 并执行；失败（含 run 标记 Failed 的兜底）直接
    //    以失败 outcome 出口。
    let final_state = match stage_execute(
        state,
        invoker,
        project_root,
        tenant,
        store.clone(),
        initial_state,
        agent_id,
        message,
        &run_id,
        user_id,
        pipeline_config_id,
    )
    .await
    {
        Ok(s) => s,
        Err(outcome) => return outcome,
    };

    // 3b/4. registry 回写 + 响应提取。
    let outcome = stage_finalize(
        &final_state,
        Some(store.as_ref()),
        &tenant_id,
        effective_pipeline_id,
        thread_id,
        agent_id,
        &run_id,
    )
    .await;
    // GAP-2：run 终态域事件——内核只派生 run.* 终态事件（failed/suspended/
    // cancelled/completed，见 derive_run_terminal_events）；task_completed/
    // task_failed 由 task_service 插件订阅 run.* 后按任务域语义派生（ADR
    // 2026-08-28 事件下沉）。fire-and-forget，不影响响应。
    // 注：任务状态（task.status/ended_at）由任务域插件裁决写入（task_evaluate
    // 评估终态经 pipeline-state.update 落 state），内核只广播 run 终态事件，
    // 不写任务状态（职责边界：内核只管管道运行域，任务状态由任务域插件裁决）。
    emit_run_terminal_domain_events(state, &final_state, &run_id, outcome.failed).await;
    outcome
}

/// GAP-2：从 run 终态 state 派生域事件（run.* 终态 + 任务域派生）。
///
/// run 终态域事件派生（内核运行域）：仅运行生命周期事件，与 runs 落库
/// 共用同一终态映射单点（`RunStatus::from_control_state`，控制状态键契约
/// ADR 2026-08-30——落库 Failed 的 run 不得对外广播 run.completed）——
/// - `failed=true`（引擎执行失败）→ `run.failed`
/// - 映射 Suspended → `run.suspended`
/// - 映射 Cancelled → `run.cancelled`（用户停止/任务取消/删除署名）
/// - 映射 Failed（署名：超线/评估失败/重复循环等）→ `run.failed`
/// - 映射 Completed → `run.completed`
///
/// 任务域事件（task_completed/task_failed）由 task_service 插件订阅本组
/// run.* 事件后按任务域语义派生（ADR 2026-08-28 事件下沉：裁决词汇
/// task.status 值与 task.* 键面归插件，内核零知识）。
///
/// 载荷（ADR 2026-09-11）：终态事件随带该管道的 state 对象（`state` 标签），
/// 订阅方据此零回查派生任务域语义。内核只转发不挑选——不理解 `task.*`/
/// `lineage.*` 的语义，与零知识约束不冲突（约束的是不得**判定**任务域词汇，
/// 非不得**转发**插件域数据）。大字段经 [`terminal_event_state_payload`] 剥离。
pub(crate) fn derive_run_terminal_events(
    final_state: &serde_json::Value,
    failed: bool,
) -> Vec<(&'static str, Vec<(String, serde_json::Value)>)> {
    let pipeline_id = final_state
        .get("pipeline_id")
        .cloned()
        .unwrap_or(serde_json::Value::Null);
    let thread_id = final_state
        .get("session_id")
        .cloned()
        .unwrap_or(serde_json::Value::Null);
    let run_tags = vec![
        ("pipeline_id".to_string(), pipeline_id),
        ("thread_id".to_string(), thread_id),
        (
            "state".to_string(),
            terminal_event_state_payload(final_state),
        ),
    ];
    let mut events: Vec<(&'static str, Vec<(String, serde_json::Value)>)> = Vec::new();
    if failed {
        events.push(("run.failed", run_tags));
        return events;
    }
    let event = match agentos_core::types::RunStatus::from_control_state(final_state) {
        agentos_core::types::RunStatus::Suspended => "run.suspended",
        agentos_core::types::RunStatus::Cancelled => "run.cancelled",
        agentos_core::types::RunStatus::Failed => "run.failed",
        agentos_core::types::RunStatus::Completed | agentos_core::types::RunStatus::Running => {
            "run.completed"
        }
    };
    events.push((event, run_tags));
    events
}

/// 终态事件 state 载荷的剥离集：订阅方零消费的引擎/内核大字段。
///
/// 剔除项与理由（ADR 2026-09-11）：
/// - `messages`：全历史，单管道最有分量的常驻结构（事件载荷会到 MB 级）；
/// - `tool_schemas`：每轮 prepare 链重新拉取的 LLM 工具面，跨轮无意义
///   （与 `stage_finalize` 常驻快照剥离同款）；
/// - `raw_result` / `raw_thinking` / `raw_tool_calls`：引擎原始投影，
///   订阅方消费的是插件域键而非引擎通道键；
/// - `_` 前缀：引擎私有中间键（`_executed_tool_calls` 等）。
///
/// 实测剥离后终态标量载荷约 1.8 KB（最大单键 642 字节）。
const TERMINAL_EVENT_STATE_STRIP: &[&str] = &[
    "messages",
    "tool_schemas",
    "raw_result",
    "raw_thinking",
    "raw_tool_calls",
];

/// 构造终态事件的 state 载荷（剥离大字段后的对象；非对象输入返回空对象）。
pub(super) fn terminal_event_state_payload(final_state: &serde_json::Value) -> serde_json::Value {
    let Some(obj) = final_state.as_object() else {
        return serde_json::Value::Object(serde_json::Map::new());
    };
    let mut out = serde_json::Map::with_capacity(obj.len());
    for (k, v) in obj {
        if TERMINAL_EVENT_STATE_STRIP.contains(&k.as_str()) || k.starts_with('_') {
            continue;
        }
        out.insert(k.clone(), v.clone());
    }
    serde_json::Value::Object(out)
}

/// 广播 run 终态域事件（GAP-2：fire-and-forget，不阻塞引擎出口）。
///
/// run.failed 额外镜像一帧 `run_failed` 推前端（ADR 2026-09-28-run-failure-
/// frontend-notification）：域事件只达总线/订阅插件，前端不知情——署名失败
/// 路径（stop_reason 命中 FAILED_STOP_REASONS，DSL 正常收束 failed=false）与
/// 任务/注入管道的引擎失败此前对前端完全静默。completed/suspended/cancelled
/// 不镜像（每轮一帧 completed 是噪音，挂起/取消有既有通道）。
pub(crate) async fn emit_run_terminal_domain_events(
    state: &AppState,
    terminal_state: &serde_json::Value,
    run_id: &str,
    failed: bool,
) {
    let events = derive_run_terminal_events(terminal_state, failed);
    let run_failed = events.iter().any(|(name, _)| *name == "run.failed");
    for (name, tags) in events {
        crate::plugin_lifecycle::broadcast_domain_event(state, name, tags).await;
    }
    if run_failed {
        if let Some(session) = state.session.as_ref() {
            emit_run_failed_frontend_event(session, terminal_state, run_id).await;
        }
    }
    // 刀3（2026-09-09 内存驻留归因）：run 终态（非 suspended）注销该管道的
    // 热路径 state 缓存——final_state（含 messages 全历史）是单管道最有分量
    // 的常驻结构，且 remove() 此前在生产零调用，已终态管道的状态跨轮永久
    // 驻留（4788 条 pipeline_state 的内存镜像）。热缓存只为续跑服务：下轮
    // 冷启动经 persist_run_end 收尾 checkpoint + traces/message_slots 重建
    // （server.rs 冷路径，见 get_or_init 未命中分支）。suspended（停泊/
    // 审批挂起）保留热态，恢复路径零重建。
    let status = agentos_core::types::RunStatus::from_control_state(terminal_state);
    if status != agentos_core::types::RunStatus::Suspended {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let pipeline_id = terminal_state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if !pipeline_id.is_empty() {
            // 注销热缓存前把 run_status 落表（内核自有键，观测真值）：checkpoint
            // 落档按 VOLATILE 剥离 run_status，注销后内存副本也无——list 视图的
            // checkpoint 兜底行将永远读不到终态（评测判稳/前端观测断链，2026-09-10
            // 压测实证）。表行单字段 upsert，随兜底行合并出口；下轮起点注入
            // running 覆盖，无复活风险。
            let terminal_fields = terminal_run_fields(status, terminal_state);
            if let Some(store) = state.store.as_ref() {
                if let Err(e) = store
                    .upsert_state_fields(pipeline_id, &tenant_id, &terminal_fields)
                    .await
                {
                    warn!(
                        pipeline_id = %pipeline_id,
                        error = %e,
                        "终态 run_status/run_stop_reason 落表失败（观测兜底缺失，不影响执行）"
                    );
                }
            }
            agentos_session::pipeline_state_registry::global_registry()
                .remove(&tenant_id, pipeline_id);
        }
    }
}

/// run 收尾落表字段：`run_status` + `run_stop_reason`（同点、同生命周期、同事务）。
///
/// `run_status` 是内核持有的实际状态，checkpoint 落档按 VOLATILE 剥离，注销
/// 热缓存前必须旁路落表，否则 list 视图的 checkpoint 兜底行永远读不到终态。
///
/// `run_stop_reason` 与之同点：它是**失败归因的唯一真值载体**。`router.stop_reason`
/// 按设计在 `VOLATILE_RUN_KEYS` 内（防跨 run 残留，刻意不随 checkpoint 落档），
/// 代价是终态真因只活在 WS 事件与日志——插件侧对账面读不到，只能写死猜测文案
/// （事故 2026-10-17：墙钟超时被报成"启动清扫标记的进程异常终止"，与当日事实
/// 无关，把排查方向带偏）。
///
/// 未署名（键缺席 / 非字符串 / 空串）一律落**空串**而非跳过：每轮终态都写，
/// 杜绝上一轮署名残留被下一轮误读为本次归因。
pub(crate) fn terminal_run_fields(
    status: agentos_core::types::RunStatus,
    terminal_state: &serde_json::Value,
) -> serde_json::Map<String, serde_json::Value> {
    let stop_reason = terminal_state
        .get("router.stop_reason")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let mut fields = serde_json::Map::new();
    fields.insert("run_status".to_string(), serde_json::json!(status));
    fields.insert(
        "run_stop_reason".to_string(),
        serde_json::json!(stop_reason),
    );
    fields
}

/// run.failed 前端镜像帧：`run_failed` WS 事件（streaming.json 契约条目）。
///
/// 载荷坐标从终态 state 提取（session_id/pipeline_id 出生契约键；引擎 Err
/// 防御路径用预捕获的初始 state 剥离版，同样携带）。stop_reason 原值透传
/// （FAILED_STOP_REASONS 命中词；引擎 Err 无署名为 null）——内核只转发不
/// 解释。run_id 是前端通知幂等键（emit_event 进 thread 级重放缓冲，重放
/// 不重复弹卡）。会话坐标缺失 → 丢弃（无 WS 投递目标；域事件照发）。
///
/// `error`：首条插件错误原文（全量不截断，ADR 2026-10-27-pipeline-error-
/// notification-channel 错误透传裁定）。折叠失败轮（步失败零产出）stop_reason
/// 恒空串，通知卡此前只剩「管道异常退出」无归因——真实原因（限流/连接/
/// 认证等）随本字段出口，前端失败卡直接渲染。无插件错误的失败轮为 null。
pub(crate) async fn emit_run_failed_frontend_event(
    session: &agentos_session::SessionCoordinator,
    terminal_state: &serde_json::Value,
    run_id: &str,
) {
    let thread_id = terminal_state
        .get("session_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let pipeline_id = terminal_state
        .get("pipeline_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if thread_id.is_empty() || pipeline_id.is_empty() {
        return;
    }
    let first_plugin_error = terminal_state
        .get("_plugin_errors")
        .and_then(|v| v.as_array())
        .and_then(|errs| {
            errs.iter().find_map(|e| {
                let msg = e.get("message")?.as_str()?.trim();
                (!msg.is_empty()).then(|| msg.to_string())
            })
        });
    let _ = session
        .emit_event(
            thread_id,
            "run_failed",
            serde_json::json!({
                "pipeline_id": pipeline_id,
                "_threadId": thread_id,
                "status": "failed",
                "stop_reason": terminal_state.get("router.stop_reason")
                    .cloned()
                    .unwrap_or(serde_json::Value::Null),
                "error": first_plugin_error,
                "run_id": run_id,
            }),
        )
        .await;
}

/// 前置依赖缺失时的 echo 降级应答（invoker/store/project_root 任一缺席）。
/// degraded 标记供调用方（REST chat / 插件 send_message）识别降级应答，
/// 与正常回复区分（2026-08-26 统一错误模型：假成功显式化）。
pub(super) fn echo_fallback(missing: &str, message: &str) -> EngineOutcome {
    EngineOutcome {
        content: format!("[echo-fallback: {missing}] {message}"),
        final_assistant: None,
        failed: false,
        degraded: true,
        plugin_errors: Vec::new(),
        consumed_pending_cmids: Vec::new(),
    }
}

/// 按 `a.b.c` 点号路径把值写入嵌套对象（中间节点非对象时覆盖为对象）。
/// 声明驱动 execution_context 组装的路径写入器（路径来自插件
/// thread_fields 的 x_execution_path 声明，内核不预知键面）。
pub(super) fn set_execution_context_path(
    ec: &mut serde_json::Map<String, serde_json::Value>,
    path: &str,
    value: serde_json::Value,
) {
    let parts: Vec<&str> = path.split('.').collect();
    let mut cur = ec;
    for part in &parts[..parts.len() - 1] {
        let entry = cur
            .entry(part.to_string())
            .or_insert_with(|| serde_json::Value::Object(serde_json::Map::new()));
        if !entry.is_object() {
            *entry = serde_json::Value::Object(serde_json::Map::new());
        }
        cur = entry.as_object_mut().expect("中间节点已确保为对象");
    }
    cur.insert(parts[parts.len() - 1].to_string(), value);
}

/// 消息级 execution_context 逐键并入 base（1a2 合并点，消息级显式键优先）：
/// 两侧同键皆对象时按子键合并（消息级叶子覆盖，base 未携带子键保留），
/// 其余形态消息级值整体落位。execution_context 形态固定为两层对象
/// （mode 顶层标量 / isolation、workspace 为单层对象），两层足够。
pub(super) fn merge_message_execution_context(
    base: &mut serde_json::Value,
    message: &serde_json::Value,
) {
    let Some(msg_obj) = message.as_object() else {
        return;
    };
    if !base.is_object() {
        *base = message.clone();
        return;
    }
    let base_obj = base.as_object_mut().expect("base 已确保为对象");
    for (k, mv) in msg_obj {
        match (base_obj.get_mut(k), mv) {
            (Some(serde_json::Value::Object(bm)), serde_json::Value::Object(mm)) => {
                for (bk, bv) in mm {
                    bm.insert(bk.clone(), bv.clone());
                }
            }
            _ => {
                base_obj.insert(k.clone(), mv.clone());
            }
        }
    }
}
