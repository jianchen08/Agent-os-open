//! state 键级写面（journal 实录 / 轨迹落库 / 分层投影）。

use super::finalize::set_key;
use super::finalize::state_str;
use super::journal::global_invariant_baseline;
use super::journal::invariant_update_suppressed;
use super::journal::messages_chars_total;
use super::journal::state_diff_from_journal;
use super::*;
use crate::pipeline_loop::journal::TRACE_MIRROR_DIGEST_KEYS;

impl PipelineExecutor {
    /// 落 step 级轨迹：checkpoint 计步 + 窗口轨迹落库 + 分层投影的 step 入口。
    /// diff/实录/字段过滤语义见 [`Self::append_window_trace`]。
    ///
    /// **每执行一个配置 step 必调本函数**（组级 when 跳过的 step 在 execute_steps
    /// 直接 continue，不进本函数）——checkpoint 按步计数即在此推进
    /// （[`Self::count_step_and_maybe_checkpoint`]），保证"实际执行的 step"才计步。
    /// 顶层键首触实录（写前调用）：step 内首次触碰某键时定格其旧值，
    /// 供 [`Self::append_window_trace`] 算 diff——内核内 state 借用不克隆，
    /// 实录体积 ∝ 实际变更面而非 state 体积。
    ///
    /// 同时是 state_delta 键级事件的统一发射落点（设计 §4.3 引擎侧升级③，
    /// journal_top_key/set_key 写路径落点）：窗口首触即记（含写前旧值），由
    /// 步边界 checks 分发消费（state 触发命中即触发，未命中事件不残留）。
    /// 同窗口重复写不重复发射（首触旧值 + 消费时点新值 = 窗口级 delta，
    /// 与轨迹 diff 同语义）。
    pub(crate) fn journal_top_key(&self, state: &serde_json::Value, key: &str) {
        let mut journal = self.step_key_journal.lock();
        if let std::collections::hash_map::Entry::Vacant(entry) = journal.entry(key.to_string()) {
            let old = state.get(key).cloned();
            entry.insert(old.clone());
            let mut deltas = self.pending_state_deltas.lock();
            if !deltas.iter().any(|d| d.key == key) {
                deltas.push(StateDeltaEvent {
                    key: key.to_string(),
                    old,
                });
            }
        }
    }

    /// 单键状态更新统一入口（同值不更新，2026-10-30）：不变清单键且序列化
    /// ≥[`INVARIANT_SUPPRESS_MIN_BYTES`] 且内容与本管道基线一致 → 跳过
    /// journal——没有 journal 就没有 diff，也就没有轨迹（冗余在更新层消失，
    /// 而非落库前剔除）。**set 不在抑制范围**：run state 每轮重建而基线跨
    /// run 存活，跳写会让重建 state 永缺该键（2026-11-02 装机实证：tool_
    /// schemas/system_message 断链 → LLM 裸请求无 tools 无系统提示）；同值
    /// set 幂等，state 语义零影响。首写/真变更照常 journal 并刷新基线。
    pub(crate) fn apply_state_key_update(
        &self,
        state: &mut serde_json::Value,
        key: &str,
        value: serde_json::Value,
    ) {
        let pipeline_id = state_str(state, "pipeline_id").unwrap_or_default();
        let suppressed = invariant_update_suppressed(
            &mut global_invariant_baseline().lock(),
            &pipeline_id,
            key,
            &value,
        );
        if !suppressed {
            self.journal_top_key(state, key);
        }
        set_key(state, key, value);
    }

    /// 维护平铺键 `track.messages_chars`（W2a 上下文估算账本）：messages ops
    /// 应用后按 delta 增量推进；键缺失/为 0 且 messages 非空时全量重算一次
    /// （冷启动兜底——恢复/旧会话无账可依时宁可重算不可漏账）。
    ///
    /// 单位与一致性基准见 [`message_content_bytes`]（紧凑 JSON UTF-8 字节，
    /// 剔除 seq）。写入走 journal + set_key（进 step 轨迹，检查点/回放同源）。
    pub(crate) fn maintain_messages_chars(&self, state: &mut serde_json::Value, delta: i64) {
        let existing = state.get("track.messages_chars").and_then(|v| v.as_i64());
        let nonempty = state
            .get("messages")
            .and_then(|v| v.as_array())
            .is_some_and(|a| !a.is_empty());
        let new_val = match existing {
            Some(c) if c > 0 => c + delta,
            // 冷启动兜底：无账且消息非空 → 全量重算（messages 空 → 保持缺失）
            _ if nonempty => messages_chars_total(state),
            _ => return,
        };
        if existing == Some(new_val) {
            return; // 无变化不写（省一次 journal/diff）
        }
        self.journal_top_key(state, "track.messages_chars");
        set_key(
            state,
            "track.messages_chars",
            serde_json::Value::from(new_val),
        );
    }

    /// 持久化写失败重试：失败后再试 2 次（退避 [`PERSIST_RETRY_BACKOFF_MS`]，
    /// 真实 sleep），仍失败则把最后一次错误上抛——持久化失败不允许静默丢段
    /// （DB 写失败时内存前进 = 重启后缺失段永久丢失且无对账，任务域禁止降级）。
    /// 每次失败计 `persist_failures_total` 指标并 warn 留痕。
    pub(crate) async fn retry_persist<T, F, Fut>(
        &self,
        what: &str,
        mut attempt: F,
    ) -> Result<T, agentos_core::types::StorageError>
    where
        F: FnMut() -> Fut,
        Fut: std::future::Future<Output = Result<T, agentos_core::types::StorageError>>,
    {
        let mut last_err: Option<agentos_core::types::StorageError> = None;
        for try_no in 0..=PERSIST_RETRY_BACKOFF_MS.len() {
            match attempt().await {
                Ok(v) => return Ok(v),
                Err(e) => {
                    warn!(target_op = %what, attempt = try_no + 1, error = %e, "持久化失败");
                    self.metrics.inc_persist_failure();
                    if let Some(backoff_ms) = PERSIST_RETRY_BACKOFF_MS.get(try_no) {
                        tokio::time::sleep(Duration::from_millis(*backoff_ms)).await;
                    } else {
                        last_err = Some(e);
                    }
                }
            }
        }
        Err(last_err.expect("retry loop runs at least once"))
    }

    pub(crate) async fn persist_step_trace(
        &self,
        step_id: &str,
        ckpt: &agentos_core::types::CheckpointConfig,
        state_after: &serde_json::Value,
    ) -> Result<(), EngineError> {
        if self.iteration_depth > 0 {
            // 并行 for-each 迭代局部：不落轨迹/不计 checkpoint/不投影——迭代
            // 写面经父归并在父 step 收尾统一落（轨迹颗粒度 = 配置 step，不钻迭代）。
            return Ok(());
        }
        // checkpoint 按配置 step 计数（在轨迹入口统一推进，含无产出 step——
        // 无产出也消耗了一步；0/禁用 + pipeline_id 为空时内部跳过）。
        self.count_step_and_maybe_checkpoint(ckpt, state_after)
            .await;
        self.append_window_trace(step_id, state_after).await?;
        // 分层持久化投影：累计标量字段用 state_after 的完整值 upsert（覆盖最新
        // 值）。无产出 step 也投影（messages 原文已在 merge 时实时落表）。
        self.project_state_snapshot(state_after).await
    }

    /// 轨迹窗口落库核（step 窗口与引擎窗口共用）：取走 journal 算 diff（滤
    /// 已投影冗余键）→ 取走 messages 实录拼 patch_data → diff 与实录均空跳过
    /// （窗口无产出）→ append_trace。
    ///
    /// 凡 state 变更必可追溯（ADR 2026-09-11）：`window_id` 即轨迹的 plugin_id
    /// ——step.id，或引擎窗口 run_init / body_enter / body_route / run_finalize。
    /// checkpoint 计步与分层投影不在此（step 专属，见 persist_step_trace）。
    pub(crate) async fn append_window_trace(
        &self,
        window_id: &str,
        state_after: &serde_json::Value,
    ) -> Result<(), EngineError> {
        let journal = std::mem::take(&mut *self.step_key_journal.lock());
        let mut diff = state_diff_from_journal(&journal, state_after);

        // 过滤已投影到 messages 表的冗余字段（原文不进 trace，全文在 blobs）
        const REDUNDANT_KEYS: &[&str] = &[
            "raw_result",     // → messages.content_preview
            "raw_thinking",   // → messages.reasoning_content
            "raw_tool_calls", // → messages.tool_calls_json
        ];
        if let Some(obj) = diff.as_object_mut() {
            for key in REDUNDANT_KEYS {
                obj.remove(*key);
            }
            // 工具结果 staging 键定位符化（ADR 2026-09-29-trace-mirror-dedup）：
            // 全文权威副本在工具缓存（tool_cache_writer 同 run 落缓存）与展示
            // 信封（messages 截断版），轨迹镜像只留摘要定位符——装机实测该两键
            // 单补丁可达 250KB+（traces 53.9MB 的主要成分）。
            for key in TRACE_MIRROR_DIGEST_KEYS {
                if let Some(v) = obj.get(*key) {
                    let digest = trace_mirror_digest(v);
                    obj.insert((*key).to_string(), digest);
                }
            }
        }

        // 取走本窗口的 messages 实录，拼进 patch_data。引擎窗口无插件运行、
        // 实录恒空——统一取走防残留串窗（跨窗口误归因）。
        let ledger: Vec<serde_json::Value> = std::mem::take(&mut *self.ops_ledger.lock());
        if !ledger.is_empty() {
            if let Some(obj) = diff.as_object_mut() {
                obj.insert("messages".into(), serde_json::json!({ "_ops": ledger }));
            }
        }

        // diff 与实录均为空则跳过（窗口无产出）
        if diff.as_object().is_none_or(|o| o.is_empty()) {
            return Ok(());
        }

        use agentos_core::types::{PatchType, TraceEntry};
        let entry = TraceEntry {
            trace_id: format!("t_{}", uuid::Uuid::new_v4().simple()),
            pipeline_id: state_str(state_after, "pipeline_id").unwrap_or_default(),
            seq: 0,
            plugin_id: window_id.to_string(),
            patch_type: PatchType::StateUpdate,
            patch_data: diff,
            created_at: chrono::Utc::now().to_rfc3339(),
        };
        // 轨迹落库：失败重试后仍失败 = 上抛终止 run（trace 缺失段不可对账重建）。
        self.retry_persist("append_trace", || {
            let entry = entry.clone();
            async move { self.store.append_trace(entry).await }
        })
        .await?;
        Ok(())
    }

    /// 从完整 state 投影到业务表（messages + 声明的累计字段）。
    ///
    /// 在 persist_step_trace 内调用（每个配置 step 后），用 state 的当前完整值投影。
    /// project_messages 内部按索引增量对齐（幂等），upsert_state_field 覆盖最新值。
    /// pipeline_id 为空（测试/首轮未注入）时跳过。
    ///
    /// 投影写失败重试后仍失败 = Err 上抛（调用方终止 run）——投影缺失段
    /// 重启后不可对账，禁止静默降级。
    pub(crate) async fn project_state_snapshot(
        &self,
        state: &serde_json::Value,
    ) -> Result<(), EngineError> {
        let pipeline_id = state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if pipeline_id.is_empty() {
            return Ok(());
        }
        let tenant_id = self.default_tenant.tenant_id.clone();
        // messages 不在此投影——op 模型下 merge 时已实时落 message_slots（一次 apply）。
        // 此处只投影 persistent_fields 声明的累计标量字段（声明即契约，逐键 upsert）。
        for key in &self.persistent_fields {
            if let Some(v) = state.get(key) {
                self.retry_persist("upsert_state_field", || {
                    self.store
                        .upsert_state_field(pipeline_id, &tenant_id, key, v)
                })
                .await?;
            }
        }
        Ok(())
    }
}
