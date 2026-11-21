//! 多轮历史恢复（registry 热路径 + checkpoint/traces/messages 冷路径）。

use super::engine_dispatch::EngineOutcome;
use super::stage_exec::merge_patch;
use super::stages::apply_state_overlay;
use super::stages::merge_recovered_scalars;
use super::*;
/// 阶段 2a：run 启动前刷新 registry 快照（实际状态轮中可见）。
pub(super) fn stage_refresh_registry_snapshot(
    tenant_id: &str,
    effective_pipeline_id: &str,
    initial_state: &serde_json::Value,
) {
    let reg = agentos_session::pipeline_state_registry::global_registry();
    if reg.contains(tenant_id, effective_pipeline_id) {
        reg.update_state(tenant_id, effective_pipeline_id, initial_state.clone());
    }
}

/// 阶段 1b：多轮上下文装配 + 本轮 user 消息入账，返回补全后的 initial_state。
///
/// state.messages 是 LLMCore._build_messages 读取的对话历史。单一权威（不搞降级路径）：
///   ① 热路径：PipelineStateRegistry 内存快照即上轮 final_state → messages 直接复用，
///      标量键（task.id/workspace/lineage.*/execution_context…）经
///      merge_recovered_scalars 一并恢复——快照是管道合法数据全集，续跑不丢键。
///   ② 冷路径：registry 未命中（重启/新会话/内存丢失）→ checkpoint/traces/表恢复
///      标量基线 + messages 表（持久化冷数据）按 effective_pipeline_id 恢复完整
///      历史，后续轮走热路径。
/// 恢复失败 = bug（内存丢 + DB 读不到）：显式 error 暴露，不静默吞掉。
///
/// `skip_user_append`（批次 D 显式重跑标志，经 state_overlay 注入）：regenerate
/// 重跑时目标 user 消息已在截断后历史中，跳过本轮 append——不借用 interrupted_tail
/// 启发式（同文/同 cmid 判定只服务崩溃重放场景）。
#[allow(clippy::too_many_arguments)]
pub(crate) async fn stage_recover_history(
    mut initial_state: serde_json::Value,
    store: &Arc<dyn StorageBackend>,
    message: &str,
    effective_pipeline_id: &str,
    tenant_id: &str,
    client_message_id: &str,
    skip_user_append: bool,
    state_overlay: Option<&serde_json::Value>,
    declared_volatile: &std::collections::HashSet<String>,
    run_id: &str,
) -> Result<serde_json::Value, EngineOutcome> {
    let mut history_prefix: Vec<serde_json::Value> = Vec::new();
    let mut history_loaded = false;
    let registry = agentos_session::global_registry();
    if let Some(entry) = registry.get(tenant_id, effective_pipeline_id) {
        // 热路径：内存快照即上轮 final_state——messages 复用，标量键随
        // merge_recovered_scalars 全量恢复。快照是管道合法数据全集：标量
        // 不恢复会让下游按身份缺席误判（任务管道缺 task.id 被判成主会话，
        // 工作区漂移到会话共享目录，产物落错位置）。
        let entry = entry.read();
        if let Some(msgs) = entry.state.get("messages").and_then(|v| v.as_array()) {
            history_prefix = msgs.clone();
            history_loaded = true;
        }
        merge_recovered_scalars(&mut initial_state, &entry.state, declared_volatile);
    }
    if !history_loaded {
        let (state, prefix) = recover_cold_history(
            initial_state,
            store,
            effective_pipeline_id,
            tenant_id,
            declared_volatile,
        )
        .await;
        initial_state = state;
        history_prefix = prefix;
    }
    // 派发 overlay 在恢复合并之后应用（B13① 域界定，ADR 2026-09-06）：overlay
    // 是调用方的显式派发指令（如任务域 resume 链清 task.status 终态键），必须
    // 优先于恢复基线——先应用会被热/冷恢复源（registry 快照/checkpoint/
    // pipeline_state 表）的残留值覆盖写回。保留字与 lineage.* 保护不变
    // （apply_state_overlay 纵深防御）。
    if let Some(overlay) = state_overlay {
        apply_state_overlay(&mut initial_state, overlay);
    }
    seed_history_and_reset_assistant_flag(&mut initial_state, history_prefix);
    let interrupted_tail = is_interrupted_tail(&initial_state, message, client_message_id);
    if !interrupted_tail && !skip_user_append {
        append_user_message(
            &mut initial_state,
            store,
            tenant_id,
            effective_pipeline_id,
            message,
            client_message_id,
            run_id,
        )
        .await
        .map_err(|e| {
            // B3：重试耗尽仍失败 = 本轮 user 消息未受理（内存侧也不留——
            // 「内存已注入、实录缺席」的静默分叉窗口消灭在源头）。
            error!(
                pipeline_id = %effective_pipeline_id,
                tenant_id = %tenant_id,
                error = %e,
                "user 消息落库重试耗尽，终止本轮发送（不进入引擎执行）"
            );
            EngineOutcome::terminated(format!(
                "消息未受理：user 消息落库失败（已重试仍失败），本轮发送已终止，请稍候重试。原因: {e}"
            ))
        })?;
    }
    Ok(initial_state)
}

/// 冷路径恢复（registry 未命中时）：checkpoint/traces/pipeline_state/message_slots
/// 四源装配标量基线与消息历史，返回（合并标量后的 state, messages 历史前缀）。
/// ① checkpoint 只提供**标量基线**——messages 一律不消费（load 后剥离丢弃），
///    无论新旧格式（任务 5 瘦身后新 checkpoint 本就不含 messages）；
/// ② 无 checkpoint → traces 回放**标量字段**（merge_patch 跳过 messages）；
/// ③ pipeline_state 表补充累计标量（每 step upsert 最新值）；
/// ④ messages 从 message_slots 表直读（消息队列持久真值，零回放）。
pub(crate) async fn recover_cold_history(
    mut initial_state: serde_json::Value,
    store: &Arc<dyn StorageBackend>,
    effective_pipeline_id: &str,
    tenant_id: &str,
    declared_volatile: &std::collections::HashSet<String>,
) -> (serde_json::Value, Vec<serde_json::Value>) {
    let mut history_prefix: Vec<serde_json::Value> = Vec::new();
    // 冷路径（ADR 2026-09-18：state 唯一真值，checkpoint 降级纯备份/DR）：
    // ① pipeline_state 表是主真值——直接读累计标量（每 step upsert 最新值）。
    // ② messages 从 message_slots 表直读（消息队列持久真值，零回放）。
    // ③ DR 路径（仅当 ①+② 双双为空 = state 缺失/损坏）：checkpoint 标量基线 +
    //    traces 回放重建。checkpoint/traces 不再参与常规恢复主路径。
    let mut recovered: serde_json::Value = serde_json::json!({});
    let mut state_hit = false;
    if !effective_pipeline_id.is_empty() {
        match store
            .load_pipeline_state(effective_pipeline_id, tenant_id)
            .await
        {
            Ok(state_fields) => {
                if !state_fields.is_empty() {
                    state_hit = true;
                }
                if let Some(rec_obj) = recovered.as_object_mut() {
                    for (k, v) in &state_fields {
                        rec_obj.insert(k.clone(), v.clone());
                    }
                }
            }
            Err(e) => {
                error!(
                    pipeline_id = %effective_pipeline_id,
                    error = %e,
                    "load_pipeline_state 冷恢复失败（标量基线可能不完整）"
                );
            }
        }
    }
    // messages 直读 message_slots（零回放；元素自带稳定 seq）
    if !effective_pipeline_id.is_empty() {
        match store
            .load_message_history(effective_pipeline_id, tenant_id)
            .await
        {
            Ok(msgs) if !msgs.is_empty() => {
                history_prefix = msgs;
                state_hit = true;
            }
            Ok(_) => {}
            Err(e) => {
                error!(
                    pipeline_id = %effective_pipeline_id,
                    error = %e,
                    "load_message_history 冷恢复失败（对话历史可能不完整）"
                );
            }
        }
    }
    // DR 路径：state 与消息双双缺席（state 丢失/损坏/首次恢复旧库）——
    // checkpoint 提供标量基线（messages 剥离丢弃），无 checkpoint 则 traces
    // 回放标量字段（merge_patch 跳过 messages）。
    if !state_hit && !effective_pipeline_id.is_empty() {
        warn!(
            pipeline_id = %effective_pipeline_id,
            "state 与消息双空：走 DR 重建（checkpoint 基线 + traces 回放）"
        );
        let mut ckpt_hit = false;
        match store
            .load_latest_checkpoint(effective_pipeline_id, tenant_id)
            .await
        {
            Ok(Some((_step_no, ckpt_state))) => {
                recovered = ckpt_state;
                // 队列真值在表：checkpoint 的 messages 剥离丢弃（新旧格式一律）
                if let Some(rec_obj) = recovered.as_object_mut() {
                    rec_obj.remove("messages");
                }
                ckpt_hit = true;
                debug!(
                    pipeline_id = %effective_pipeline_id,
                    "DR 重建从 checkpoint 取标量基线"
                );
            }
            Ok(None) => {
                // 无 checkpoint：继续 traces 回放
            }
            Err(e) => {
                // checkpoint 读失败不等同无 checkpoint：降级走 traces 回放时留痕，
                // 避免静默丢标量基线（错误是数据读失败，不是记录不存在）。
                error!(
                    pipeline_id = %effective_pipeline_id,
                    error = %e,
                    "load_latest_checkpoint DR 重建失败，降级 traces 回放"
                );
            }
        }
        if !ckpt_hit {
            // 只回放本管道自己的轨迹。thread 全量回放会把同会话其它管道
            // （父会话/兄弟子任务）的控制态（conversation_mode/core_type/router.*）
            // 种进本管道初始 state——曾致新子任务第 0 轮即被对话挂起路由误挂
            // （2026-08-30 管道身份裁定：执行态恢复只认 pipeline_id）。
            match store
                .get_step_traces_by_pipeline(effective_pipeline_id, tenant_id)
                .await
            {
                Ok(step_traces) => {
                    for entry in &step_traces {
                        merge_patch(&mut recovered, &entry.patch_data);
                    }
                }
                Err(e) => {
                    // 持久化恢复失败：bug 信号（内存丢 + DB 也读不到），显式 error 暴露。
                    error!(
                        pipeline_id = %effective_pipeline_id,
                        error = %e,
                        "get_step_traces_by_pipeline 回放失败"
                    );
                }
            }
        }
    }
    // 标量字段注入 state——跳过规则见 merge_recovered_scalars（GAP-3：
    // message/input/suspended 等 per-run 键残留会顶掉本轮新输入，重启后
    // 旧 user 消息被重放消费；_skip_user_append 等下划线键泄漏会吞消息）。
    merge_recovered_scalars(&mut initial_state, &recovered, declared_volatile);
    (initial_state, history_prefix)
}

/// messages 塞回 state + A1 复位标志（行为契约注释随代码平移保留）。
fn seed_history_and_reset_assistant_flag(
    initial_state: &mut serde_json::Value,
    history_prefix: Vec<serde_json::Value>,
) {
    // messages 塞回 state（热路径元素自带 seq；冷路径表读也已带 seq——零兼容，
    // 不做缺 seq 补位、不做客户端 history 兜底）
    if let Some(obj) = initial_state.as_object_mut() {
        obj.insert(
            "messages".to_string(),
            serde_json::Value::Array(history_prefix),
        );
    }
    // A1：复位"本轮首个 assistant 已注入 id"标志（checkpoint/registry 恢复可能带旧值，
    // 必须在恢复合并之后、管道执行之前强制复位，保证每 run 恰好注入一次 message_id）。
    if let Some(obj) = initial_state.as_object_mut() {
        obj.insert(
            "_assistant_id_assigned".to_string(),
            serde_json::json!(false),
        );
    }
}

/// GAP-3 中断重放幂等判定（ADR 2026-08-21 契约注释随代码平移保留，见函数体）。
fn is_interrupted_tail(
    initial_state: &serde_json::Value,
    message: &str,
    client_message_id: &str,
) -> bool {
    // GAP-3：中断重放幂等——上一次尝试若已把本轮 user 消息落槽（重启/崩溃
    // 截断 run，无 assistant 跟随），恢复出的 history 尾部就是它；再 append 会
    // 重复落槽+重复消费。判定按幂等键裁决
    // （ADR 2026-08-21）：cmid 在场时只有「同 cmid」才算同一次发送的重派（吞）；
    // cmid 不同 = 真发了两条（绝不吞）。无 cmid
    // 的路径（触发器注入/旧客户端）维持同文判定。
    initial_state
        .get("messages")
        .and_then(|v| v.as_array())
        .and_then(|msgs| msgs.last())
        .is_some_and(|last| {
            let same_user_content = last.get("role").and_then(|r| r.as_str()) == Some("user")
                && last.get("content").and_then(|c| c.as_str()) == Some(message);
            if !same_user_content {
                return false;
            }
            if client_message_id.is_empty() {
                return true;
            }
            last.get("metadata")
                .and_then(|m| m.get("client_message_id"))
                .and_then(|v| v.as_str())
                == Some(client_message_id)
        })
}

/// user 消息落库重试退避序列（对齐引擎侧 PERSIST_RETRY_BACKOFF_MS 范式：
/// 失败后再试 2 次、真实退避 100/300ms，共 3 次尝试）。
pub(super) const USER_APPEND_RETRY_BACKOFF_MS: &[u64] = &[100, 300];

/// 本轮 user 消息 append 落库（ADR 2026-08-21 / ops 即轨迹契约注释随代码平移保留）。
/// B3：落库失败先重试（瞬时 DB 抖动不打断对话），重试耗尽仍失败上抛给调用方
/// 终止本轮发送——会话面裁定"DB 故障不得假成功"，禁止"内存已注入、实录缺席"
/// 的静默分叉（重启后消息从历史消失且无对账锚点）。
pub(crate) async fn append_user_message(
    initial_state: &mut serde_json::Value,
    store: &Arc<dyn StorageBackend>,
    tenant_id: &str,
    effective_pipeline_id: &str,
    message: &str,
    client_message_id: &str,
    run_id: &str,
) -> Result<(), agentos_core::types::StorageError> {
    // user 经 append op(无 seq → 引擎分配 next seq)+ 落 message_slots。
    // 指纹实录塞 _pending_message_ops（内部字段）：executor.persist_run_start 落一条
    // user_input 轨迹后移除——首轮 user 由此进入审计/回放范围（ops 即轨迹）。
    // cmid 非空时随 metadata 落库（compute_message_id 纳入 metadata 参与 hash：
    // 同内容多次发送 record_id 各自唯一，顺带消除重复内容同 id 碰撞）。
    // _run_id（表侧内部字段，与 assistant 的 _message_id 同模式）：轮首 user 消息
    // 触发的 run 在 append 时已生成，随 op 落 message_slots.run_id——regenerate
    // 审计锚预检读它（NULL = 拒绝，轮首消息曾因此全部不可编辑重发/回退）。
    let mut user_msg = serde_json::json!({"role":"user","content":message});
    if !client_message_id.is_empty() {
        user_msg["metadata"] = serde_json::json!({"client_message_id": client_message_id});
    }
    let ops = [serde_json::json!({"op":"set","msg":user_msg,"_run_id": run_id})];
    // apply_messages_op_update 先改内存后落表：每次重试前按快照还原内存，
    // 防失败重试把同一条 user 消息双写进 messages。
    let messages_snapshot = initial_state.get("messages").cloned();

    let mut last_err: Option<agentos_core::types::StorageError> = None;
    for try_no in 0..=USER_APPEND_RETRY_BACKOFF_MS.len() {
        match agentos_engine::apply_messages_op_update(
            initial_state,
            store.as_ref(),
            tenant_id,
            &ops,
        )
        .await
        {
            Ok(user_ledger) if !user_ledger.is_empty() => {
                if let Some(obj) = initial_state.as_object_mut() {
                    obj.insert(
                        "_pending_message_ops".to_string(),
                        serde_json::Value::Array(user_ledger),
                    );
                }
                return Ok(());
            }
            Ok(_) => return Ok(()),
            Err(e) => {
                warn!(
                    target_op = "append_user_message",
                    attempt = try_no + 1,
                    pipeline_id = %effective_pipeline_id,
                    tenant_id = %tenant_id,
                    error = %e,
                    "user 消息落库失败"
                );
                // apply 先改内存后落表：每次失败（含最后一次）都按快照还原内存，
                // 保证出口处内存与落库一致——失败不留半条消息。
                if let Some(obj) = initial_state.as_object_mut() {
                    match &messages_snapshot {
                        Some(snapshot) => {
                            obj.insert("messages".to_string(), snapshot.clone());
                        }
                        None => {
                            obj.remove("messages");
                        }
                    }
                }
                if let Some(backoff_ms) = USER_APPEND_RETRY_BACKOFF_MS.get(try_no) {
                    tokio::time::sleep(Duration::from_millis(*backoff_ms)).await;
                } else {
                    last_err = Some(e);
                }
            }
        }
    }
    Err(last_err.expect("retry loop runs at least once"))
}

/// 阶段 3：构造 PipelineExecutor 并执行 initial_state，返回 final_state。
///
/// branch_id 用固定 "main"；run_id 由调用方生成（process_via_engine_inner 权威
/// 生成点，已注入 initial_state 供插件轮询），本函数只消费不再生成——保证
/// state 里的轮询锚与 runs 表落库 id 是同一个 uuid。租户上下文从 task_local 读取
/// （多租户 P0-4：调用方已在 agentos_tenant::scope 内）。
/// Pull 热加载：按需重载管道配置（autonomous.yaml + steps）——每次 chat 执行前
/// 检测配置 mtime，变了才重新加载到本次执行用的局部变量，不写回 AppState
/// （启动期配置保持不动，作为重载失败的兜底）。
/// 执行失败（executor.run 返回 Err）：把 run 标记 failed + ended_at（避免永远卡
/// running、历史悬空）并以 failed outcome 出口（Err 携带）。
#[allow(clippy::too_many_arguments)]
#[allow(clippy::too_many_lines)] // 存量吸收(2026-10-12 文件规模棘轮): chat 执行编排聚合,拆分随执行链批次
#[cfg(test)]
mod stage_refresh_registry_snapshot_tests {
    use super::*;

    /// 实际状态轮中可见（2026-09-03 双状态裁定）：上一轮 final_state（ended=true
    /// / 上轮阶段）滞留 registry 时，dispatch 启动前刷新为带 run_status=running
    /// 的本轮 initial——轮中可观测性不得读到陈旧终态（「显示已结束但还在输出」
    /// 的显示侧根因）。
    #[test]
    fn overwrites_stale_terminal_snapshot_before_run() {
        let reg = agentos_session::pipeline_state_registry::global_registry();
        let tenant = "tenant_refresh_test";
        let pid = "pipe_refresh_test";
        let stale = serde_json::json!({
            "run_status": "completed",
            "ended": true,
            "current_phase": "exit",
            "messages": [{"role": "assistant", "content": "old"}],
        });
        if reg.contains(tenant, pid) {
            reg.update_state(tenant, pid, stale);
        } else {
            reg.get_or_init(tenant, pid, "thread_refresh_test", "agentos", stale);
        }

        let fresh = serde_json::json!({
            "run_status": "running",
            "ended": false,
            "message": "新消息",
            "messages": [{"role": "user", "content": "新消息"}],
        });
        stage_refresh_registry_snapshot(tenant, pid, &fresh);

        let entry = reg.get(tenant, pid).expect("registry 条目存在");
        let e = entry.read();
        assert_eq!(e.state["run_status"], "running");
        assert_eq!(e.state["ended"], false);
        assert!(
            e.state.get("current_phase").is_none(),
            "上轮阶段残留不得跨轮滞留快照"
        );
        assert_eq!(
            e.state["messages"],
            serde_json::json!([{"role": "user", "content": "新消息"}]),
            "快照整体覆写为本轮 initial（热路径本就自 registry 恢复）"
        );
    }

    /// 条目不存在的管道（首轮/重启后首跑）：刷新为 no-op，不创建条目——
    /// 收尾 stage_finalize 的 get_or_init 负责补建。
    #[test]
    fn missing_entry_is_noop() {
        let reg = agentos_session::pipeline_state_registry::global_registry();
        let tenant = "tenant_refresh_absent";
        let pid = "pipe_refresh_absent";
        stage_refresh_registry_snapshot(tenant, pid, &serde_json::json!({"run_status": "running"}));
        assert!(!reg.contains(tenant, pid), "不得为不存在的条目抢先建项");
    }
}
