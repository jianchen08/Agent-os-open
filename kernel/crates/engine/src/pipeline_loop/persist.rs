//! 运行持久化（run 起止登记 / 终态落库 / 补偿标记）。

use super::*;

impl PipelineExecutor {
    // ── 持久化（ADR ②③：引擎独占落库，插件只返回 Patch）──────────

    /// 创建运行实例并登记 run 坐标：runs 表建行、run→pipeline 归属、
    /// pipeline↔session 映射，以及首轮 user_input 轨迹。
    ///
    /// 在 run() 开头调用一次。用户消息全文由派发层在 run 前经 message_slots
    /// append op 落库（引擎不单独写 messages 表）；state 各键由
    /// server.rs 的 stage_build_initial_state 注入。
    pub(crate) async fn persist_run_start(&self, state: &mut serde_json::Value, config_hash: &str) {
        // config_hash = 编译期对 PipelineConfig 的确定性指纹
        // （compiler::pipeline_config_hash：serde_json 规范化 + SHA-256 前 16 hex）。
        // runs 表退役（ADR 2026-09-18）：运行簿记 = state 运行键（run_id /
        // run_status='running' / run_started_at / run_config_hash）。
        // 单写者（刀5）：run_started_at 的唯一值 origin 是本函数上方 run_init
        // 写入 state 的那一次 Utc::now()——此处读回同串传给 store（store 侧
        // 不自产时间戳），保证 state 与 DB 两读面恒同值。
        let tenant_id = self.default_tenant.tenant_id.clone();

        // pipeline_id 从 state 读（stage_build_initial_state 注入，前端创建会话
        // 时生成、每轮回传）。它是消息层查询主键，适配"通过 state 通路执行持久化"。
        let pipeline_id = state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let started_at = state
            .get("run_started_at")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if !pipeline_id.is_empty() {
            if let Err(e) = self
                .store
                .record_run_start(
                    &pipeline_id,
                    &tenant_id,
                    &self.run_id,
                    config_hash,
                    &started_at,
                )
                .await
            {
                warn!(run_id = %self.run_id, error = %e, "运行开始簿记落库失败（继续执行）");
                self.metrics.inc_persist_failure();
            }

            // 插件级失败常驻镜像 run 起点清零（[`RUN_PLUGIN_ERRORS_KEY`] 契约）：
            // 镜像只在 run 终态非空时覆写（api stage_exec），从不主动清——上一
            // run 的失败会跨 run 残留，挂起/长跑 run 的面板详情卡把它冒充成当前
            // 错误（装机 2026-11-18 634adfc6a364 实证：run1 PARSE_ERROR 残留
            // 伴 run2 挂起 80+ 分钟）。清零语义 = 每 run 错误面独立；上一 run
            // 的失败事实已随其终态 WS 事件/轨迹面交付，不因清零丢失。
            if let Err(e) = self
                .store
                .upsert_state_fields(
                    &pipeline_id,
                    &tenant_id,
                    &[(
                        crate::pipeline_loop::RUN_PLUGIN_ERRORS_KEY.to_string(),
                        serde_json::json!([]),
                    )]
                    .into_iter()
                    .collect(),
                )
                .await
            {
                warn!(run_id = %self.run_id, error = %e, "run.plugin_errors 起点清零落库失败（继续执行）");
                self.metrics.inc_persist_failure();
            }
        }

        // 写入 pipeline↔session 映射（每次管道开跑时记录，含子任务管道）。
        // 删除会话时据此按 thread_id 找到全部 pipeline_id 级联清理。
        // session_id 即 thread_id（stage_build_initial_state 注入）；两者任一为空则跳过（幂等）。
        let session_id = state
            .get("session_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if let Err(e) = self
            .store
            .link_pipeline_session(&pipeline_id, session_id, &tenant_id)
            .await
        {
            warn!(error = %e, "link_pipeline_session 失败（继续）");
        }

        // 用户消息不再单独落库：server.rs 在 run 前经 append op push 进 state["messages"]
        // 并落 message_slots；其指纹实录经 state["_pending_message_ops"] 传入，此处落一条
        // 轨迹（plugin_id="user_input"）保证首轮 user 也在审计/回放范围内，然后移除内部字段。
        if let Some(ledger) = state
            .as_object_mut()
            .and_then(|o| o.remove("_pending_message_ops"))
        {
            if let Some(ops) = ledger.as_array() {
                if !ops.is_empty() {
                    use agentos_core::types::{PatchType, TraceEntry};
                    let entry = TraceEntry {
                        trace_id: format!("t_{}", uuid::Uuid::new_v4().simple()),
                        pipeline_id: pipeline_id.clone(),
                        seq: 0,
                        plugin_id: "user_input".to_string(),
                        patch_type: PatchType::StateUpdate,
                        patch_data: serde_json::json!({ "messages": { "_ops": ops } }),
                        created_at: chrono::Utc::now().to_rfc3339(),
                    };
                    if let Err(e) = self.store.append_trace(entry).await {
                        warn!(error = %e, "user_input 实录落轨迹失败（继续）");
                        self.metrics.inc_persist_failure();
                    }
                }
            }
        }
    }

    /// 追加插件 step 的 Patch 到 traces 表（Append-Only，ADR ③）。
    ///
    /// 一轮结束时更新 run 状态。assistant 消息不再此处单独落库——它已在最后一个
    /// llm_core step 的 merge_and_project 中投影到 messages 表（含 tool_calls）。
    /// 分层持久化：投影是"merge state_updates 时同步落库"，不延迟到 run 结束。
    pub(crate) async fn persist_run_end(&self, final_state: &serde_json::Value) {
        let tenant_id = self.default_tenant.tenant_id.clone();
        // 收尾 checkpoint：run 结束时无条件落一份最终态快照（最终态是重建的最佳基线）。
        // 这样重建时能直接从本 run 最终 state 接着跑，不必回放整轮 traces。
        // checkpoint_id 用 step_no 锚点，同 step 重放幂等。
        let pipeline_id = final_state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if !pipeline_id.is_empty() {
            let step_no = self.total_step_no.load(Ordering::SeqCst);
            // B5：收尾快照走 retry_persist（对齐 append_trace 同一标准）——
            // 最终态缺席 = 重建退化为整轮 traces 回放，重试耗尽 error 强观测。
            if let Err(e) = self
                .retry_persist("save_checkpoint", || {
                    let tenant_id = tenant_id.clone();
                    async move {
                        self.store
                            .save_checkpoint(pipeline_id, &tenant_id, step_no, final_state)
                            .await
                    }
                })
                .await
            {
                error!(
                    pipeline_id = %pipeline_id,
                    error = %e,
                    "收尾 save_checkpoint 重试耗尽：最终态快照未落库"
                );
            }
            // run 收尾整管道兜底清理（ADR 2026-08-27 §2.2 生命周期）：
            // 中断/异常路径若在插件侧直接收尾（半截组装发生在 llm_core sidecar，
            // 引擎无插点），merge 清键可能漏掉未落库的 chunk 中间态与 B 区
            // 绑定——run 结束无条件清整管道两区，防内存残留跨轮存活。
            crate::transient::global_registry().clear_pipeline(&tenant_id, pipeline_id);
        }

        // 终态映射单点（控制状态键契约 ADR 2026-08-30）：RunStatus::from_control_state
        // 与域事件派生（api derive_run_terminal_events）共用，两处不得各持一套词汇。
        // runs 表退役（ADR 2026-09-18）：终态直接写 state 运行键（run_status 折算值
        // + run_ended_at）。
        let final_status = agentos_core::types::RunStatus::from_control_state(final_state);
        let status_str = match final_status {
            agentos_core::types::RunStatus::Running => "running",
            agentos_core::types::RunStatus::Suspended => "suspended",
            agentos_core::types::RunStatus::Completed => "completed",
            agentos_core::types::RunStatus::Failed => "failed",
            agentos_core::types::RunStatus::Cancelled => "cancelled",
        };
        let terminal = !matches!(
            final_status,
            agentos_core::types::RunStatus::Running | agentos_core::types::RunStatus::Suspended
        );
        if let Err(e) = self
            .retry_persist("update_run_status", || {
                let status_str = status_str.to_string();
                let tenant_id = tenant_id.clone();
                async move {
                    let now = chrono::Utc::now().to_rfc3339();
                    let mut fields = serde_json::Map::new();
                    fields.insert("run_status".into(), serde_json::json!(status_str));
                    if terminal {
                        fields.insert("run_ended_at".into(), serde_json::json!(now));
                    }
                    self.store
                        .upsert_state_fields(pipeline_id, tenant_id.as_str(), &fields)
                        .await
                }
            })
            .await
        {
            // B5：终态落库重试耗尽——投影滞留 running，G8 优雅重启排空会把
            // 已结束的运行误标 suspended（当可恢复挂起）、崩溃清扫会误标 failed。
            // 终态点已无法终止 run（run 已结束），重试 + 显式补偿标记是该点位
            // 的最正确语义：error 强观测 + 写「终态未落库」标记供重启排空精准补写。
            error!(
                run_id = %self.run_id,
                pipeline_id = %pipeline_id,
                intended_status = ?final_status,
                error = %e,
                "update_run_status 重试耗尽：run 终态未落库"
            );
            if !pipeline_id.is_empty() {
                self.write_terminal_persist_failed_marker(pipeline_id, &tenant_id, &final_status)
                    .await;
            }
        }
    }

    /// B5：「终态未落库」补偿标记（best-effort 单次，不重试——retry_persist
    /// 的重试窗口已耗尽，再失败只能靠长期 running 异常扫描兜底）。
    /// 写 [`TERMINAL_PERSIST_FAILED_KEY`] → pipeline_state 表，值为
    /// {run_id, intended_status, failed_at}；消费方按 run_id 匹配当前卡
    /// running 的 run，不匹配即陈旧标记（该管道后续 run 已正常收尾，可清）。
    pub(crate) async fn write_terminal_persist_failed_marker(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        intended_status: &agentos_core::types::RunStatus,
    ) {
        let marker = serde_json::json!({
            "run_id": self.run_id,
            "intended_status": intended_status,
            "failed_at": chrono::Utc::now().to_rfc3339(),
        });
        if let Err(e) = self
            .store
            .upsert_state_field(pipeline_id, tenant_id, TERMINAL_PERSIST_FAILED_KEY, &marker)
            .await
        {
            error!(
                run_id = %self.run_id,
                pipeline_id = %pipeline_id,
                error = %e,
                "终态未落库补偿标记写入失败"
            );
        }
    }
}
