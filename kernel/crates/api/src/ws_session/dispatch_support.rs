//! 消息分发辅助面（ws_session 子模块）：regenerate 目标解析（live/ledger
//! 双源）、消息段冻结与 segment/pending-inputs 事件发射、blob 状态透出。
//!
//! 由 ws_session.rs `mod dispatch_support` 挂载：EngineDispatcher 派发路径与
//! tests 经父模块 `use` 再导出消费，引用形态不变。
use std::sync::Arc;

use tracing::warn;

use super::AppState;

pub(super) async fn emit_segment_activated(
    state: &AppState,
    thread_id: &str,
    route_id: &str,
    segment_id: &str,
) {
    if let Some(session) = state.session.as_ref() {
        let _ = session
            .emit_event(
                thread_id,
                "segment_activated",
                serde_json::json!({
                    "pipeline_id": route_id,
                    "segment_id": segment_id,
                    "_threadId": thread_id,
                }),
            )
            .await;
    }
}

/// 构造 freeze_segment op（域12 原语）：id 服务端生成 `seg_<uuid>`，
/// visible_to=''（替换事件冻结的旧代段 = 全可见，‹i/n› 可切换激活），
/// preview 取首条 content ≤200 字符（‹i/n› 代际预览免解 blob）。
/// 成员须已剥离 seq（位置标记属槽位坐标，冻结原文携带会在写回时变陈旧残留）。
pub(super) fn freeze_segment_op(
    base_seq: u32,
    members: Vec<serde_json::Value>,
) -> serde_json::Value {
    serde_json::json!({
        "op": "freeze_segment",
        "id": format!("seg_{}", uuid::Uuid::new_v4().simple()),
        "base_seq": base_seq,
        "base_len": members.len(),
        "visible_to": "",
        "preview": members
            .first()
            .and_then(|m| m.get("content"))
            .and_then(|c| c.as_str())
            .map(|c| c.chars().take(200).collect::<String>()),
        "members": members,
    })
}

/// 推送 `pending_inputs_changed` 事件（ADR-2026-08-26）：入队/消费/修改/删除时
/// 前端据 payload 的 items 全量列表同步队列条。事件坐标 = 派发 thread（与
/// new_message 同款单播）；session 未接线时静默跳过（无连接可推）。
///
/// 核心实现在 [`emit_pending_inputs_changed_with`]（session+store 直取形态），
/// 供轮边界注入消费路径（SessionRoundEvents，无 AppState）复用。
pub(super) async fn emit_pending_inputs_changed(
    state: &AppState,
    thread_id: &str,
    pipeline_id: &str,
    tenant_id: &str,
    store: &Arc<dyn agentos_core::traits::StorageBackend>,
    action: &str,
) {
    let Some(session) = state.session.as_ref() else {
        return;
    };
    emit_pending_inputs_changed_with(
        session,
        store,
        thread_id,
        pipeline_id,
        tenant_id,
        action,
        &[],
    )
    .await;
}

/// pending_inputs_changed 发射核心（session+store 直取形态）。`consumed_records`
/// 非空时随 payload 附加 `consumed_messages` 字段（轮边界注入专用：注入消息无
/// 独立消费轮的 stream_start/认领链路，前端据逐条权威 record——指纹 record_id/
/// seq/原文——走 claimUserMessage 认领，与正常发送同一条链；链级 drain 路径恒
/// 空传，认领链路不受扰）。
pub(super) async fn emit_pending_inputs_changed_with(
    session: &Arc<agentos_session::SessionCoordinator>,
    store: &Arc<dyn agentos_core::traits::StorageBackend>,
    thread_id: &str,
    pipeline_id: &str,
    tenant_id: &str,
    action: &str,
    consumed_records: &[agentos_engine::ConsumedPendingRecord],
) {
    let items = match store.list_pending_inputs(tenant_id, pipeline_id).await {
        Ok(rows) => rows
            .into_iter()
            .map(|r| {
                serde_json::json!({
                    "id": r.id,
                    "pipeline_id": r.request.pipeline_id,
                    "content": r.request.content,
                    "source": r.request.source,
                    // cmid 随条目透出（returned 撤乐观气泡 / 前端对账同键）。
                    "client_message_id": r.request.client_message_id,
                    "created_at": r.created_at,
                })
            })
            .collect::<Vec<_>>(),
        Err(e) => {
            warn!(
                pipeline = %pipeline_id,
                error = %e,
                "pending_inputs_changed 列表读取失败（事件跳过）"
            );
            Vec::new()
        }
    };
    let mut payload = serde_json::json!({
        "pipeline_id": pipeline_id,
        "thread_id": thread_id,
        "action": action,
        "items": items,
    });
    if !consumed_records.is_empty() {
        payload["consumed_messages"] = serde_json::json!(consumed_records
            .iter()
            .map(|r| {
                serde_json::json!({
                    "client_message_id": r.client_message_id,
                    "id": r.message_id,
                    "sequence": r.sequence,
                    "content": r.content,
                })
            })
            .collect::<Vec<_>>());
    }
    let _ = session
        .emit_event(thread_id, "pending_inputs_changed", payload)
        .await;
}

/// 定位 regenerate 目标 user 消息的 seq（批次 D）。
///
/// user_message_id 非空：按 message_id 精确匹配（历史读路径 record_id 与表侧
/// 落库 id 一致——流式注入 message_id 或内容指纹）；miss 时按 user 消息
/// `metadata.client_message_id` 等值兜底——认领缺失窗口（失败 run 不发认领
/// 载荷，前端消息无 recordId）出站退回 UI uuid，而 cmid 本就是随 blob 持久化
/// 的幂等键（ADR 2026-08-21，读路径原样回填），单射可直接定位；空串：取最后
/// 一条 user 消息（重新生成缺省语义）。只接受 role=user——截断边界必须是
/// user 消息（方案 §三 工具配对完整性硬约束）。找不到返回 None（调用方报错，
/// 不静默截断）。
pub(super) fn find_target_user_seq(
    messages: &[agentos_core::types::MessageRecord],
    user_message_id: &str,
) -> Option<u32> {
    if user_message_id.is_empty() {
        return messages
            .iter()
            .rev()
            .find(|m| m.role == "user")
            .map(|m| m.seq_in_branch);
    }
    messages
        .iter()
        .find(|m| m.role == "user" && m.message_id == user_message_id)
        .or_else(|| {
            messages.iter().find(|m| {
                m.role == "user"
                    && m.metadata
                        .as_ref()
                        .and_then(|md| md.get("client_message_id"))
                        .and_then(|v| v.as_str())
                        == Some(user_message_id)
            })
        })
        .map(|m| m.seq_in_branch)
}

/// regenerate 目标解析结果：定位 + 审计锚。
pub(super) enum RegenTarget {
    /// 槽位在场且 run 锚有效（常规路径：id 精确命中或缺省末条 user）。
    Live { seq: u32, run_id: String },
    /// 槽位缺失或身份变异（压缩链路改写/删槽后，前端认领指纹不在槽位键空间）——
    /// 经 trace 账本回溯定位：账本 append-only，原始 append 实录携带
    /// (seq, 指纹, blob 锚)，blob 原文可验指纹与 role。审计锚 = 账本轨迹
    /// （run 锚随槽位改写/删除不可恢复，rollback trace 如实记账本指针）。
    Ledger {
        seq: u32,
        original_msg: serde_json::Value,
        ledger_trace_id: String,
        ledger_blob_id: String,
    },
}

impl RegenTarget {
    pub(super) fn seq(&self) -> u32 {
        match self {
            RegenTarget::Live { seq, .. } | RegenTarget::Ledger { seq, .. } => *seq,
        }
    }
}

/// 定位 regenerate 目标：槽位精确匹配优先，miss/run 锚缺失时经 trace 账本
/// 回溯（BUG-57：压缩链路改写目标槽位后 record_id 变异，前端认领的原始指纹
/// 仍在账本中可验可查）。账本也验不了 → 维持原拒绝语义（fail-closed，不静默
/// 截断）。存储故障显式报错，不吞。
pub(super) async fn resolve_regenerate_target(
    store: &Arc<dyn agentos_core::traits::StorageBackend>,
    pipeline_id: &str,
    tenant_id: &str,
    messages: &[agentos_core::types::MessageRecord],
    user_message_id: &str,
) -> Result<RegenTarget, String> {
    let live_seq = find_target_user_seq(messages, user_message_id);
    if live_seq.is_none() && user_message_id.is_empty() {
        return Err(format!(
            "regenerate 未找到目标 user 消息: {user_message_id}"
        ));
    }
    let live_run_id = live_seq
        .and_then(|seq| {
            messages
                .iter()
                .find(|m| m.seq_in_branch == seq)
                .map(|m| m.run_id.clone())
        })
        .unwrap_or_default();
    if let Some(seq) = live_seq {
        if !live_run_id.is_empty() {
            return Ok(RegenTarget::Live {
                seq,
                run_id: live_run_id,
            });
        }
    }
    // 槽位 miss 或 run 锚缺失 → 账本回溯；验不出锚则按原语义拒绝。
    // 缺省定位（user_message_id 空 = 重新生成最后一条 user）没有外部指纹可验：
    // 以活槽自身 message_id 作账本锚——否则 run 锚缺失的遗留行（改写 op 未携
    // _run_id 的旧 bug）永远无法回溯，缺省重试被永久拒绝（2026-11-09 装机实证）。
    let ledger_anchor = if user_message_id.is_empty() {
        live_seq
            .and_then(|seq| messages.iter().find(|m| m.seq_in_branch == seq))
            .map(|m| m.message_id.as_str())
            .unwrap_or("")
    } else {
        user_message_id
    };
    resolve_target_via_ledger(store, pipeline_id, tenant_id, ledger_anchor)
        .await?
        .ok_or_else(|| {
            if live_seq.is_some() {
                "regenerate 拒绝: 目标 user 消息无有效 run_id(审计不可见)".to_string()
            } else {
                format!("regenerate 未找到目标 user 消息: {user_message_id}")
            }
        })
}

/// trace 账本回溯：扫描本管道 step 轨迹的指纹降级实录（messages._ops），取
/// 携带该指纹的 set op 中 seq 最小者（对齐槽位路径 find 的最早命中语义；
/// 同文多次发送共享同一指纹时定位最早一条），再经 blob 原文验证指纹一致且
/// role=user（截断边界硬约束）。任一环验证不过 → None（调用方维持拒绝）。
pub(super) async fn resolve_target_via_ledger(
    store: &Arc<dyn agentos_core::traits::StorageBackend>,
    pipeline_id: &str,
    tenant_id: &str,
    user_message_id: &str,
) -> Result<Option<RegenTarget>, String> {
    if user_message_id.is_empty() {
        return Ok(None); // 缺省定位无 id 可验，账本无从锚定
    }
    let traces = store
        .get_step_traces_by_pipeline(pipeline_id, tenant_id)
        .await
        .map_err(|e| format!("regenerate 账本回溯失败: {e}"))?;
    let mut best: Option<(u32, String, String)> = None; // (seq, blob_id, trace_id)
    for t in &traces {
        let Some(ops) = t
            .patch_data
            .get("messages")
            .and_then(|m| m.get("_ops"))
            .and_then(|v| v.as_array())
        else {
            continue;
        };
        for op in ops {
            if op.get("op").and_then(|v| v.as_str()) != Some("set") {
                continue;
            }
            // 指纹/内容锚双形态：引擎实录 op 带 message_id/blob_id；dispatch 侧
            // 自建的改写 op（chat_regenerate rollback）只有 seq+msg——按 msg 推导
            // 同一组内容寻址（compute_message_id 剥离位置/身份/审计锚字段，与
            // 槽位写表同口径），否则编辑重发写出的槽位无法经账本回溯，run 锚
            // 一旦缺失即被永久拒绝（2026-11-09 装机实证）。
            let msg = op.get("msg").filter(|m| m.is_object());
            let op_message_id = op
                .get("message_id")
                .and_then(|v| v.as_str())
                .map(str::to_string)
                .or_else(|| msg.map(agentos_core::ids::compute_message_id));
            if op_message_id.as_deref() != Some(user_message_id) {
                continue;
            }
            let op_blob_id = op
                .get("blob_id")
                .and_then(|v| v.as_str())
                .map(str::to_string)
                .or_else(|| {
                    msg.map(|m| {
                        let src = serde_json::to_string(m)
                            .expect("serde_json Value serialization is infallible");
                        agentos_core::ids::compute_blob_id(src.as_bytes())
                    })
                });
            let (Some(seq), Some(blob_id)) = (
                op.get("seq").and_then(|v| v.as_u64()).map(|s| s as u32),
                op_blob_id,
            ) else {
                continue;
            };
            if best.as_ref().is_none_or(|(s, _, _)| seq < *s) {
                best = Some((seq, blob_id, t.trace_id.clone()));
            }
        }
    }
    let Some((seq, blob_id, trace_id)) = best else {
        return Ok(None);
    };
    let bytes = store
        .get_blob(&blob_id)
        .await
        .map_err(|e| format!("regenerate 账本回溯失败: {e}"))?;
    let msg: serde_json::Value = serde_json::from_slice(&bytes)
        .map_err(|e| format!("regenerate 账本回溯失败: blob 解析 {e}"))?;
    if agentos_core::ids::compute_message_id(&msg) != user_message_id {
        return Ok(None); // 指纹不符 = 锚不可信，维持拒绝
    }
    if msg.get("role").and_then(|v| v.as_str()) != Some("user") {
        return Ok(None); // 非 user 不可作截断边界（工具配对完整性硬约束）
    }
    Ok(Some(RegenTarget::Ledger {
        seq,
        original_msg: msg,
        ledger_trace_id: trace_id,
        ledger_blob_id: blob_id,
    }))
}

/// 解析消息应路由到的真实 pipeline_id（防御性校验，后端不盲目信任前端数据）。
///
/// 校验逻辑：
/// 1. 前端传的 pipeline_id 非空 且 属于该 thread_id（查 pipeline_sessions）→ 信任前端值
/// 2. 否则取该 thread 的真实 active_pipeline_id（主管道）作为权威值
/// 3. 仍取不到 → 回退 thread_id（兼容旧路径，与原 route_id 语义一致）
///
/// 这与前端源头修复（router.tsx 实时读 sessionStore）互补，形成双层防线：
/// 解析语义：前端显式管道经成员校验后信任；解析不出真实管道（缺失/不属于
/// 该会话/无会话）一律返回空串由调用方显式处置——绝不静默换管道（拿主管道
/// 顶替用户指向的管道 = 写错桶，2026-08-30 管道身份裁定）。
pub(super) async fn resolve_pipeline_id_for_thread(
    store: &Arc<dyn agentos_core::traits::StorageBackend>,
    thread_id: &str,
    frontend_pipeline_id: &str,
    tenant_id: &str,
) -> String {
    // ① 前端值非空且属于该 thread → 信任
    if !frontend_pipeline_id.is_empty() {
        match store
            .list_pipeline_ids_by_thread(thread_id, tenant_id)
            .await
        {
            Ok(pids) if pids.iter().any(|p| p == frontend_pipeline_id) => {
                return frontend_pipeline_id.to_string();
            }
            Ok(_) => {
                warn!(
                    thread_id = %thread_id,
                    frontend_pid = %frontend_pipeline_id,
                    "前端传来的 pipeline_id 不属于该 thread（可能残留旧会话值），拒绝派发——不回落主管道（静默换管道=写错桶）"
                );
                return String::new();
            }
            // 存储故障 ≠ 无关联：报错可见，不得兜成空列表误诊为"前端值不属于该 thread"。
            // 且不再回落主管道改写 route_id（2026-08-22 裁决）——用户在子管道视图
            // 发消息时路由落主管道 = 写错桶；校验不可用即透传前端值（前端已是
            // 权威 activePipelineId 源头），让故障期间路由保持用户所见。
            Err(e) => {
                warn!(
                    thread_id = %thread_id,
                    frontend_pid = %frontend_pipeline_id,
                    error = %e,
                    "list_pipeline_ids_by_thread 查询失败，校验不可用，透传前端 pipeline_id（不回落主管道）"
                );
                return frontend_pipeline_id.to_string();
            }
        }
    }

    // ② 取该 thread 的真实 active_pipeline_id
    // 簿记读面（store.get_session）：scope 内无反调派发、无身份消费点，
    // 两参 `new` 不丢执行者身份；执行/反调路径才须 `TenantContext::inherit`
    // （A1 统一收口契约）。
    let tenant =
        agentos_core::types::TenantContext::new(tenant_id.to_string(), thread_id.to_string());
    let tid = thread_id.to_string();
    let store_clone = store.clone();
    let session =
        match agentos_tenant::scope(tenant, async move { store_clone.get_session(&tid).await })
            .await
        {
            Ok(s) => s,
            Err(e) => {
                warn!(
                    thread_id = %thread_id,
                    error = %e,
                    "get_session 读取失败，active_pipeline_id 解析不可用"
                );
                None
            }
        };
    if let Some(active) = session.and_then(|s| s.active_pipeline_id) {
        if !active.is_empty() {
            return active;
        }
    }

    // ③ 解析不出真实管道 → 空串，由调用方显式报错/幂等跳过。
    // 会话 thread_id 是组织集合 id，绝不充当执行坐标回填（否则消息/state
    // 落进以 thread id 为键的幽灵管道；2026-08-30 管道身份裁定）。
    String::new()
}

/// new_message 事件 message.status 的取值规则：从消息 blob（final_assistant）
/// 读取——中断/错误半截消息落库时带 interrupted/error，正常消息缺省 completed。
pub(super) fn message_status_from_blob(fa: &serde_json::Value) -> serde_json::Value {
    fa.get("status")
        .cloned()
        .unwrap_or(serde_json::json!("completed"))
}
