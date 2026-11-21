//! run 收尾（孤儿工具卡合成 / merge_and_project / StepBindingGuard / state 辅助）。

use std::time::Duration;

use super::journal::messages_ops_chars_delta;
use super::*;

impl PipelineExecutor {
    /// 终局孤儿工具卡合成（interrupted/error 终局专用）：run 到达
    /// Cancelled/Failed 终局时，扫描 messages 历史中已发起但尚无对应 role=tool
    /// 结果消息的 tool_call_id，逐个合成终态 tool 结果消息（稳定标记
    /// [`INTERRUPTED_TOOL_RESULT_MARKER`] + 人读文本，envelope success=false），
    /// 经 [`apply_messages_op_update`] 同一通道落库（内存 state + message_slots
    /// 表 + 实录 ledger 同落）——恢复 tool_calls/tool 一一配对，前端不再把
    /// 孤儿卡渲染成永动计时。
    ///
    /// 同范式：LangGraph interrupt 恢复合成 ToolMessage("rejected by user")；
    /// LibreChat v0.8.8 #15517 Forward Run Abort Signal to Foreground Tool Calls。
    ///
    /// 范围裁定：
    /// - 只在 Cancelled（用户 stop/抢占中断）与 Failed（error 终局）合成；
    ///   Completed 不合成（正常路径工具都已返回）；Suspended 非终局不合成
    ///   （human_interaction 等待恢复期间工具调用合法在途）。
    /// - 扫描整条 messages 历史（跨 run 累积数组）：历史孤儿一并治愈——配对
    ///   是会话级不变量，已治愈者天然幂等（有了结果消息不再命中孤儿集）。
    /// - 内核进程自身硬崩不经过本路径，孤儿由前端终态渲染兜底刀承接。
    /// - 落库失败 warn + 计数软降级：终局点不因合成消息写失败掩盖原始终态。
    ///   先落 DB 后动内存（DB-first 同款语义）保证失败时内存零副作用——合成
    ///   消息两边同缺（孤儿配对留给前端兜底刀），无「内存已注入、实录缺席」
    ///   分叉；成功则两边同有。
    pub(crate) async fn synthesize_orphan_tool_results(
        &self,
        state: &mut serde_json::Value,
        terminal: &agentos_core::types::RunStatus,
    ) {
        if !matches!(
            terminal,
            agentos_core::types::RunStatus::Cancelled | agentos_core::types::RunStatus::Failed
        ) {
            return;
        }
        let orphans = orphan_tool_call_ids(state);
        if orphans.is_empty() {
            return;
        }
        let ops: Vec<serde_json::Value> = orphans
            .iter()
            .map(|(call_id, tool_name)| {
                serde_json::json!({
                    "op": "set",
                    "msg": synthetic_tool_result_message(call_id, tool_name),
                })
            })
            .collect();
        let tenant_id = self.default_tenant.tenant_id.clone();
        let count = ops.len();
        match apply_messages_op_update(state, self.store.as_ref(), &tenant_id, &ops).await {
            Ok(ledger) => {
                if !ledger.is_empty() {
                    self.ops_ledger.lock().extend(ledger);
                }
                info!(
                    run_id = %self.run_id,
                    synthesized = count,
                    "interrupted/error 终局：在途工具调用已合成终态 tool 结果消息（恢复配对）"
                );
            }
            Err(e) => {
                warn!(
                    run_id = %self.run_id,
                    error = %e,
                    "孤儿工具调用合成消息落库失败（软降级，终态不受影响）"
                );
                self.metrics.inc_persist_failure();
            }
        }
    }

    /// merge 插件 state_updates 进 state（纯内存合并）。
    ///
    /// messages **只接受 op 声明**（`{_ops:[set/insert]}`）→ "一次 apply" 到表、
    /// 内存、实录、字符记账四落点，**先落 DB 后动内存**（B3 user 输入同款语义）。
    /// 全量数组形式零兼容：收到即 warn 丢弃——改队列必须走 ops，声明式契约由
    /// 所有插件（llm_core/tool_core/context_window_guard）履行。
    ///
    /// 返回 `Err` = messages 表写重试耗尽（对齐 trace/投影写面同款 retry 标准）：
    /// 内存零副作用，上抛终止 run——内存态与冷数据无分叉可对账，禁止静默降级。
    pub(crate) async fn merge_and_project(
        &self,
        state: &mut serde_json::Value,
        updates: &HashMap<String, serde_json::Value>,
    ) -> Result<(), agentos_core::types::StorageError> {
        for (k, v) in updates {
            if k == "messages" {
                if self.iteration_depth > 0 {
                    // 并行 for-each 迭代局部：ops 只缓冲不 apply（内容移植自
                    // sync/open-main-20261008 c2db32c26）——父循环按迭代序统一
                    // "一次 apply"。已知限制：迭代内后续步骤对 messages 的写后
                    // 读不保证。
                    if let Some(ops) = v.get("_ops").and_then(|o| o.as_array()) {
                        self.deferred_ops.lock().extend(ops.iter().cloned());
                    } else {
                        warn!("messages 更新未携带 _ops（全量数组已退役，零兼容），该更新被忽略");
                    }
                    continue;
                }
                if let Some(ops) = v.get("_ops").and_then(|o| o.as_array()) {
                    let tenant_id = self.default_tenant.tenant_id.clone();
                    // 归属标记：每个 op 带上 run_id（表侧写 message_slots.run_id，
                    // 供会话删除/轨迹反查定位；内存/实录侧自然忽略该字段）
                    let ops_owned: Vec<serde_json::Value> = ops
                        .iter()
                        .map(|op| {
                            let mut o = op.clone();
                            if let Some(obj) = o.as_object_mut() {
                                obj.insert("_run_id".into(), serde_json::json!(self.run_id));
                            }
                            o
                        })
                        .collect();
                    // W2a 记账：old 侧字节须读应用前数组——先算 delta 再 apply
                    // （重试期间内存不动，delta 跨重试有效）。
                    let chars_delta = messages_ops_chars_delta(state, &ops_owned);
                    // 先落 DB 后动内存（B3 同款语义推广）：失败重试（退避
                    // PERSIST_RETRY_BACKOFF_MS，对齐 trace/投影写面同款标准），
                    // 耗尽 = Err 上抛终止 run——内存零副作用，无「内存已注入、
                    // 实录缺席」分叉。手动循环替代 retry_persist：apply 需跨
                    // 重试持有 &mut state，FnMut 闭包返回借用 future 不成立
                    // （E0521）；apply 内部 DB-first，重试无部分写入。
                    let apply_result = {
                        let mut attempt = 0usize;
                        loop {
                            match apply_messages_op_update(
                                state,
                                self.store.as_ref(),
                                &tenant_id,
                                &ops_owned,
                            )
                            .await
                            {
                                Ok(ledger) => break Ok(ledger),
                                Err(e) => {
                                    warn!(
                                        target_op = "apply_messages_ops",
                                        attempt = attempt + 1,
                                        error = %e,
                                        "持久化失败"
                                    );
                                    self.metrics.inc_persist_failure();
                                    match PERSIST_RETRY_BACKOFF_MS.get(attempt).copied() {
                                        Some(ms) => {
                                            attempt += 1;
                                            tokio::time::sleep(Duration::from_millis(ms)).await;
                                        }
                                        None => break Err(e),
                                    }
                                }
                            }
                        }
                    };
                    match apply_result {
                        Ok(ledger) => {
                            // ops 即轨迹：指纹实录累积到 per-step 缓冲，step 末尾落
                            // traces（表写成功才产出——实录与 message_slots 同源同批）
                            if !ledger.is_empty() {
                                self.ops_ledger.lock().extend(ledger);
                            }
                            // 落库自动清（ADR 2026-08-27 §2.2 生命周期第二清）：
                            // 消息最终形态已落 message_slots，同 message_id 的
                            // chunk 中间态 + B 区绑定使命完成。
                            self.clear_transient_for_ops(state, &ops_owned, &tenant_id);
                            // 记账对齐**内存**数组：apply 已在内存生效后推进。
                            self.maintain_messages_chars(state, chars_delta);
                        }
                        Err(e) => return Err(e),
                    }
                } else {
                    warn!("messages 更新未携带 _ops（全量数组已退役，零兼容），该更新被忽略");
                }
                continue;
            }
            if k == "run_status" {
                // 实际状态内核持有（2026-09-03 双状态裁定）：插件表达意图走预期层
                // 控制键（ended/suspended/router.stop_reason），折算成 run_status 是
                // 引擎收尾单点的职责——插件直写一律丢弃，防实际状态被伪造。
                warn!(key = %k, "插件试图写内核持有的 run_status，该更新被忽略");
                continue;
            }
            self.apply_state_key_update(state, k, v.clone());
        }
        Ok(())
    }

    /// 落库自动清（ADR 2026-08-27 §2.2 生命周期）：消息最终形态已落
    /// message_slots 后，按同 message_id 清 chunk 中间态（A 区）与运行上下文
    /// 绑定（B 区）——所有终局（completed/interrupted/error）必经
    /// `merge_and_project`，中断路径零额外改动。
    ///
    /// message_id 双源取：`state["message_id"]`（A1 注入的流式占位 a_ id，
    /// `inject_run_message_id` 的注入源，本轮助手消息的权威 id）+ op 消息
    /// 自带的 `id` 字段（插件路径 p_ id）——覆盖两条发射路径防漏清。
    /// 指纹 message_id（mc_）不参与：chunk 键命名空间是流式占位 id，
    /// 与内容指纹无关。
    pub(crate) fn clear_transient_for_ops(
        &self,
        state: &serde_json::Value,
        ops: &[serde_json::Value],
        tenant_id: &str,
    ) {
        let pipeline_id = state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if pipeline_id.is_empty() {
            return;
        }
        let reg = crate::transient::global_registry();
        if let Some(mid) = state.get("message_id").and_then(|v| v.as_str()) {
            if !mid.is_empty() {
                reg.clear(tenant_id, pipeline_id, &format!("chunk:{mid}"));
                reg.clear_message_binding(tenant_id, pipeline_id, mid);
            }
        }
        for op in ops {
            let mid = op
                .get("msg")
                .and_then(|m| m.get("id"))
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty());
            if let Some(mid) = mid {
                reg.clear(tenant_id, pipeline_id, &format!("chunk:{mid}"));
                reg.clear_message_binding(tenant_id, pipeline_id, mid);
            }
        }
    }

    /// B 区运行上下文登记挂点（ADR 2026-08-27 §2.2 / 方案 §2.4）：step 进入时
    /// 把当前活跃 message（`state["message_id"]`，A1 流式占位 id）登记到
    /// message→step 归属表，供 api 层流式拦截点按 message 反查 step（钩子
    /// 最小作用域装载，管道步骤服务化提案 §3.6 条款④）。
    ///
    /// 返回 Drop 守卫：step 收尾（含 loop 早退路径）自动清除绑定——流式窗口
    /// （invoke 期间 chunk 发射）内绑定即当前活跃 step，窗口外不残留。
    /// state 缺 pipeline_id/message_id（测试/未注入）时返回 None 零操作。
    pub(crate) fn bind_step_message(
        &self,
        state: &serde_json::Value,
        step_id: &str,
    ) -> Option<StepBindingGuard> {
        let pipeline_id = state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let message_id = state
            .get("message_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if pipeline_id.is_empty() || message_id.is_empty() {
            return None;
        }
        let tenant_id = self.default_tenant.tenant_id.clone();
        let prev_step = crate::transient::global_registry().resolve_step_of(
            &tenant_id,
            pipeline_id,
            message_id,
        );
        crate::transient::global_registry().register_message_binding(
            &tenant_id,
            pipeline_id,
            message_id,
            step_id,
        );
        Some(StepBindingGuard {
            tenant_id,
            pipeline_id: pipeline_id.to_string(),
            message_id: message_id.to_string(),
            step_id: step_id.to_string(),
            prev_step,
        })
    }

    /// 每个配置 step 完成后推进一步：自增步数计数器，达 interval 则落档。
    ///
    /// checkpoint = 把当前完整 state 复制到 pipeline_checkpoints（留档快照）。
    /// 冷启动重建优先取最近 checkpoint（O(1) 基线）+ 回放其后 traces 增量。
    /// interval_steps 从 PipelineConfig.checkpoint 读，引擎可配（默认 1000）。0/负数=禁用。
    ///
    /// 计数单位 = 实际执行的**配置 step**（组级 when 跳过的 step 不执行、不进
    /// trace、不计步；step 内部循环一次计一步，与 trace 粒度一致）——与轨迹
    /// （persist_step_trace）同为配置 step 边界，故在此推进而非按轮计数。
    pub(crate) async fn count_step_and_maybe_checkpoint(
        &self,
        ckpt_cfg: &agentos_core::types::CheckpointConfig,
        state: &serde_json::Value,
    ) {
        if !ckpt_cfg.enabled || ckpt_cfg.interval_steps <= 0 {
            return;
        }
        let pipeline_id = state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if pipeline_id.is_empty() {
            return;
        }
        let new_since = self.steps_since_checkpoint.fetch_add(1, Ordering::SeqCst) + 1;
        let step_no = self.total_step_no.fetch_add(1, Ordering::SeqCst) + 1;
        if new_since >= ckpt_cfg.interval_steps {
            let tenant_id = self.default_tenant.tenant_id.clone();
            if let Err(e) = self
                .store
                .save_checkpoint(pipeline_id, &tenant_id, step_no, state)
                .await
            {
                warn!(pipeline_id = %pipeline_id, error = %e, "save_checkpoint 失败（继续执行）");
                self.metrics.inc_persist_failure();
            }
            // 重置间隔计数器（留档完成，开始下一个 interval）
            self.steps_since_checkpoint.store(0, Ordering::SeqCst);
        }
    }

    /// 应用转移分支：按 YAML 顺序匹配第一个 `when` 为真的分支，执行其 `then`。
    ///
    /// 匹配后立即 `break`（priority 由 YAML 顺序体现）。返回命中的 `RouteNext`
    /// （克隆），供调用方（循环体转移决策 / step 级跳转）使用。
    /// `when` 已在加载期编译为 AST（G10）：None = 恒真短路，其余只求值零解析。
    ///
    /// 路由命中应用（set 字段 + 终止/挂起/相位转移控制键）。写顶层键前统一
    /// 走首触实录（step 窗口内的路由写进本 step 轨迹；循环体出口路由在
    /// step 窗口外，journal 由下一 step 边界清空，与旧全量 diff 语义一致
    /// ——体间变更不落任何 step 轨迹）。
    pub(crate) fn apply_routes(
        &self,
        routes: &[CompiledRoute],
        state: &mut serde_json::Value,
    ) -> Option<RouteNext> {
        for route in routes {
            let matched = match &route.when {
                None => true,
                Some(cond) => eval_expr(cond, state),
            };
            if matched {
                // set 字段
                for (k, v) in &route.set {
                    self.apply_state_key_update(state, k, v.clone());
                }
                match &route.next {
                    RouteNext::Loop => { /* 继续，外层 while 会循环 */ }
                    RouteNext::End => {
                        self.journal_top_key(state, "ended");
                        set_key(state, "ended", serde_json::Value::Bool(true));
                    }
                    RouteNext::Wait => {
                        self.journal_top_key(state, "suspended");
                        set_key(state, "suspended", serde_json::Value::Bool(true));
                    }
                    // Step 真跳转由 execute_steps 消费返回值完成（G10 新 DSL "回头"语义）；
                    // 此处不写 state.next_step。
                    RouteNext::Step(_id) => {}
                    RouteNext::Phase(id) => {
                        // 转移到指定循环体：记到 state.next_phase（run() 在循环体
                        // 结束时消费；step 级路由设置后在本循环体结束时生效）
                        self.journal_top_key(state, "next_phase");
                        set_key(state, "next_phase", serde_json::Value::String(id.clone()));
                    }
                }
                return Some(route.next.clone());
            }
        }
        None
    }
}

// ── 路由处理 ──────────────────────────────────────────────────

/// B 区绑定守卫：Drop 时恢复/清除 message→step 归属（[`PipelineExecutor::bind_step_message`]
/// 返回，step 收尾自动触发——含 loop 早退路径）。
///
/// 嵌套恢复：登记前先读既有绑定存为 prev——composite 递归内层 step 收尾时
/// 把归属**恢复为外层 step**（内层流式窗口关闭，执行权回到外层剩余项）；
/// 无 prev（首次登记）时直接清除。比较清除兜底：绑定已被外层守卫清掉
/// （resolve 为 None）时零操作。
pub(crate) struct StepBindingGuard {
    tenant_id: String,
    pipeline_id: String,
    message_id: String,
    step_id: String,
    prev_step: Option<String>,
}

impl Drop for StepBindingGuard {
    fn drop(&mut self) {
        let reg = crate::transient::global_registry();
        if reg
            .resolve_step_of(&self.tenant_id, &self.pipeline_id, &self.message_id)
            .as_deref()
            != Some(self.step_id.as_str())
        {
            return; // 归属已变（外层已清/他处覆盖），不动
        }
        match &self.prev_step {
            Some(prev) => reg.register_message_binding(
                &self.tenant_id,
                &self.pipeline_id,
                &self.message_id,
                prev,
            ),
            None => reg.clear_message_binding(&self.tenant_id, &self.pipeline_id, &self.message_id),
        }
    }
}

// ── state 操作工具 ─────────────────────────────────────────────

/// 合成 tool 结果消息的稳定机器标记（content 前缀）：interrupted/error 终局
/// 为孤儿 tool_calls 合成的结果消息一律以它开头——前端终态渲染与测试断言的
/// 共同锚点。措辞冻结：改动即破坏消费方契约。
const INTERRUPTED_TOOL_RESULT_MARKER: &str = "[interrupted]";

/// 收集 messages 历史中已发起但无对应 role=tool 结果消息的 tool_call_id
/// （(call_id, tool_name) 按首现顺序，去重）。配对判定与 tool_core 同口径：
/// role=tool 消息按 `tool_call_id` 字段认领。
fn orphan_tool_call_ids(state: &serde_json::Value) -> Vec<(String, String)> {
    let Some(messages) = state.get("messages").and_then(|v| v.as_array()) else {
        return Vec::new();
    };
    let mut issued: Vec<(String, String)> = Vec::new();
    let mut returned: HashSet<String> = HashSet::new();
    for msg in messages {
        match msg.get("role").and_then(|v| v.as_str()) {
            Some("assistant") => {
                for call in msg
                    .get("tool_calls")
                    .and_then(|v| v.as_array())
                    .into_iter()
                    .flatten()
                {
                    let Some(id) = call
                        .get("id")
                        .and_then(|v| v.as_str())
                        .filter(|s| !s.is_empty())
                    else {
                        continue;
                    };
                    if issued.iter().any(|(i, _)| i == id) {
                        continue;
                    }
                    let name = call
                        .pointer("/function/name")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    issued.push((id.to_string(), name));
                }
            }
            Some("tool") => {
                if let Some(id) = msg.get("tool_call_id").and_then(|v| v.as_str()) {
                    returned.insert(id.to_string());
                }
            }
            _ => {}
        }
    }
    issued
        .into_iter()
        .filter(|(id, _)| !returned.contains(id))
        .collect()
}

/// 构造孤儿 tool_call 的合成终态结果消息：形状对齐 tool_core
/// `tool_result_message`（envelope 七键封闭），content = 稳定标记 + 人读
/// 文本；envelope success=false → 读时重建派生 status=failed（前端终态）。
fn synthetic_tool_result_message(call_id: &str, tool_name: &str) -> serde_json::Value {
    let content = format!(
        "{INTERRUPTED_TOOL_RESULT_MARKER} run terminated before tool returned (tool: {tool_name})"
    );
    serde_json::json!({
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
        "tool_result": {
            "call_id": call_id,
            "tool_name": tool_name,
            "success": false,
            "error": content,
            "data": serde_json::Value::Null,
            "metadata": { "synthetic": true },
            "duration_ms": serde_json::Value::Null,
        },
    })
}

/// 异常终局检测（凡异常终止必须显式报错，2026-10-08 用户裁定）：返回应
/// 署名的失败词，None = 正常收束。
///
/// 只读引擎级控制键，零领域知识：
/// - 挂起（等待恢复）非终局，不算异常；
/// - `router.stop_reason` 已署名 = 终止方权威（含 task_* / user_requested /
///   max_iterations 等），引擎不覆写；
/// - 无署名却带未消费工具调用（`raw_tool_calls` 非空数组——post 路由表
///   漏配回环路由致调用被吞）→ `dropped_tool_calls`；
/// - 无署名且空回复耗尽未裁决（`llm_empty_exhausted`——会话管道无任务域
///   收束方，agent 零产出即退出）→ `empty_reply_exhausted`。
pub(super) fn abnormal_end_signature(state: &serde_json::Value) -> Option<&'static str> {
    if truthy_flag(state, "suspended") {
        return None;
    }
    let unsigned = state
        .get("router.stop_reason")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .is_empty();
    if !unsigned {
        return None;
    }
    let pending_calls = state
        .get("raw_tool_calls")
        .and_then(|v| v.as_array())
        .map(|a| !a.is_empty())
        .unwrap_or(false);
    if pending_calls {
        return Some("dropped_tool_calls");
    }
    if truthy_flag(state, "llm_empty_exhausted") {
        return Some("empty_reply_exhausted");
    }
    None
}

/// 确保 state 是 Object（非 Object 时替换为空 Object）。
pub(super) fn ensure_object(state: &mut serde_json::Value) {
    if !state.is_object() {
        *state = serde_json::Value::Object(serde_json::Map::new());
    }
}

/// state 是否含指定 key。
pub(super) fn key_present(state: &serde_json::Value, key: &str) -> bool {
    state
        .as_object()
        .map(|o| o.contains_key(key))
        .unwrap_or(false)
}

/// 设置 state[key] = value（state 必须是 Object，否则忽略）。
pub(crate) fn set_key(state: &mut serde_json::Value, key: &str, value: serde_json::Value) {
    if let Some(obj) = state.as_object_mut() {
        obj.insert(key.to_string(), value);
    }
}

/// 单条插件错误消息在 state 账本里的最大字符数（超出截断）。
///
/// 错误原文只服务熔断判定与失败卡片归因，完整堆栈/响应体没有任何消费面；
/// 不设界时一条异常消息就可能把 state 撑到 MB 级（且随 MCP 每次调用复制）。
pub(crate) const MAX_PLUGIN_ERROR_MESSAGE_CHARS: usize = 1024;

/// state 账本最多保留的插件错误条目数（按 `(plugin_id, message)` 聚合后）。
///
/// 账本不是错误明细日志：同插件同文本只留一条「计数」，不同文本至多
/// [`MAX_PLUGIN_ERROR_ENTRIES`] 条；超出的新文本直接丢弃（用户裁定
/// 2026-11-17：多的直接丢掉）。文本维度是 LLM 失败连击判定所需（不同错误
/// 不累加），因此不能只按 plugin_id 聚合。
pub(crate) const MAX_PLUGIN_ERROR_ENTRIES: usize = 8;

/// 账本键：`[{"plugin_id","code","message","count","seq"}]`（引擎内部 staging
/// 键，`_` 前缀落库前剥离；run 起点清空、run 收尾物化为 `_plugin_errors`
/// 最终结论数组）。`seq` 单调递增标记最近一次失败（并行迭代归并按归并序
/// 重发），供连击判定取「最新错误文本」。
pub(crate) const PLUGIN_ERROR_STATS_KEY: &str = "_plugin_error_stats";

/// 截断错误消息到字符（非字节）上界，保证 state 账本有界。
fn truncate_error_message(message: &str, max_chars: usize) -> String {
    match message.char_indices().nth(max_chars) {
        Some((idx, _)) => format!("{}…", &message[..idx]),
        None => message.to_string(),
    }
}

/// 记一次插件步失败：**按 `(plugin_id, message)` 聚合成有界账本**，不追加
/// 错误明细数组。
///
/// 契约（用户裁定 2026-11-17）：
/// - 同插件同文本的重复失败只累加 `count`（反复失败只留一条）；
/// - 不同文本各留一条（LLM 失败连击判定需要区分「同错反复」与「异错交替」）；
/// - 每条消息截断到 [`MAX_PLUGIN_ERROR_MESSAGE_CHARS`]；
/// - 条目数上限 [`MAX_PLUGIN_ERROR_ENTRIES`]，超出的新文本直接丢弃；
/// - 工具级错误不进本账本（它们是 `tool_results` / messages 的一部分）。
///
/// 账本随 run 收尾经 [`materialize_plugin_errors`] 物化为最终结论数组
/// （`_plugin_errors`），供 api 层提取为 `EngineOutcome.plugin_errors` /
/// `run.plugin_errors` 常驻镜像。
pub(super) fn record_plugin_error(
    state: &mut serde_json::Value,
    plugin_id: &str,
    err: &PluginError,
) {
    let code = err
        .code
        .clone()
        .unwrap_or_else(|| "PLUGIN_EXEC_FAILED".to_string());
    let message = truncate_error_message(&err.message, MAX_PLUGIN_ERROR_MESSAGE_CHARS);
    let Some(obj) = state.as_object_mut() else {
        return;
    };
    let stats = obj
        .entry(PLUGIN_ERROR_STATS_KEY.to_string())
        .or_insert_with(|| serde_json::json!([]));
    if !stats.is_array() {
        // 非法形态（插件/恢复路径写坏）：重置为账本数组，维持 fail-closed 记账。
        *stats = serde_json::json!([]);
    }
    let Some(arr) = stats.as_array_mut() else {
        return;
    };
    let next_seq = next_error_seq(arr);
    if let Some(entry) = arr
        .iter_mut()
        .find(|entry| same_error_entry(entry, plugin_id, &message))
    {
        let count = entry.get("count").and_then(|v| v.as_u64()).unwrap_or(0) + 1;
        if let Some(obj) = entry.as_object_mut() {
            obj.insert("count".to_string(), serde_json::json!(count));
            obj.insert("code".to_string(), serde_json::json!(code));
            obj.insert("seq".to_string(), serde_json::json!(next_seq));
        }
        return;
    }
    if arr.len() >= MAX_PLUGIN_ERROR_ENTRIES {
        return; // 超上限的新文本直接丢弃（有界账本优先）
    }
    arr.push(serde_json::json!({
        "plugin_id": plugin_id,
        "code": code,
        "message": message,
        "count": 1,
        "seq": next_seq,
    }));
}

/// 账本下一条序号（单调递增；渲染最近失败用）。
fn next_error_seq(entries: &[serde_json::Value]) -> u64 {
    entries
        .iter()
        .map(|entry| entry.get("seq").and_then(|v| v.as_u64()).unwrap_or(0))
        .max()
        .unwrap_or(0)
        + 1
}

/// 账本条目是否命中 `(plugin_id, message)` 聚合键。
fn same_error_entry(entry: &serde_json::Value, plugin_id: &str, message: &str) -> bool {
    entry.get("plugin_id").and_then(|v| v.as_str()) == Some(plugin_id)
        && entry.get("message").and_then(|v| v.as_str()) == Some(message)
}

/// 归并一个循环迭代的账本到父 state：按 `(plugin_id, message)` 累加计数，
/// 并按归并序重发 `seq`（后者 = 归并序更晚，作为连击判定的「最新文本」）。
///
/// 迭代 state 克隆后已清空账本（见 steps.rs），因此各自只含本迭代失败；
/// 本函数只搬运 ≤ 条目上限 的小对象，不复制错误明细数组。
pub(super) fn merge_plugin_error_stats(
    parent: &mut serde_json::Value,
    iter_state: &serde_json::Value,
) {
    let Some(iter_stats) = iter_state
        .get(PLUGIN_ERROR_STATS_KEY)
        .and_then(|v| v.as_array())
    else {
        return;
    };
    if iter_stats.is_empty() {
        return;
    }
    let Some(parent_obj) = parent.as_object_mut() else {
        return;
    };
    let stats = parent_obj
        .entry(PLUGIN_ERROR_STATS_KEY.to_string())
        .or_insert_with(|| serde_json::json!([]));
    if !stats.is_array() {
        *stats = serde_json::json!([]);
    }
    let Some(parent_stats) = stats.as_array_mut() else {
        return;
    };
    for entry in iter_stats {
        let count = entry.get("count").and_then(|v| v.as_u64()).unwrap_or(0);
        if count == 0 {
            continue;
        }
        let plugin_id = entry
            .get("plugin_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let code = entry
            .get("code")
            .and_then(|v| v.as_str())
            .unwrap_or("PLUGIN_EXEC_FAILED");
        let message = entry.get("message").and_then(|v| v.as_str()).unwrap_or("");
        let next_seq = next_error_seq(parent_stats);
        if let Some(existing) = parent_stats
            .iter_mut()
            .find(|existing| same_error_entry(existing, plugin_id, message))
        {
            if let Some(obj) = existing.as_object_mut() {
                let merged = obj.get("count").and_then(|v| v.as_u64()).unwrap_or(0) + count;
                obj.insert("count".to_string(), serde_json::json!(merged));
                obj.insert("code".to_string(), serde_json::json!(code));
                obj.insert("seq".to_string(), serde_json::json!(next_seq));
            }
        } else if parent_stats.len() < MAX_PLUGIN_ERROR_ENTRIES {
            parent_stats.push(serde_json::json!({
                "plugin_id": plugin_id,
                "code": code,
                "message": message,
                "count": count,
                "seq": next_seq,
            }));
        }
    }
}

/// run 收尾物化：账本 → `_plugin_errors` 最终结论数组（含计数）。
///
/// 物化顺序 = 首次失败出现序（首条与旧追加数组同义，= 最早失败）；`seq`
/// 只留在账本内部用于连击判定取「最新文本」，不对外暴露。
///
/// 这是 run 内**唯一**写入 `_plugin_errors` 的位置：运行期 state 不携带错误
/// 明细数组（用户裁定 2026-11-17），下游（api 提取 / run.plugin_errors 镜像 /
/// 失败卡片）契约形状保持不变。`count` 为新增字段，旧消费者忽略即可。
pub(crate) fn materialize_plugin_errors(state: &mut serde_json::Value) {
    let entries: Vec<serde_json::Value> = state
        .get(PLUGIN_ERROR_STATS_KEY)
        .and_then(|v| v.as_array())
        .map(|stats| {
            stats
                .iter()
                .take(MAX_PLUGIN_ERROR_ENTRIES)
                .map(|entry| {
                    serde_json::json!({
                        "plugin_id": entry
                            .get("plugin_id")
                            .cloned()
                            .unwrap_or_else(|| serde_json::json!("")),
                        "code": entry
                            .get("code")
                            .cloned()
                            .unwrap_or_else(|| serde_json::json!("PLUGIN_EXEC_FAILED")),
                        "message": entry
                            .get("message")
                            .cloned()
                            .unwrap_or_else(|| serde_json::json!("")),
                        "count": entry
                            .get("count")
                            .cloned()
                            .unwrap_or_else(|| serde_json::json!(0)),
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    if entries.is_empty() {
        return;
    }
    set_key(state, "_plugin_errors", serde_json::Value::Array(entries));
}

/// 读取 state[key] 的布尔值（缺失/非 bool 返回 false）。
pub(crate) fn truthy_flag(state: &serde_json::Value, key: &str) -> bool {
    state
        .as_object()
        .and_then(|o| o.get(key))
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
}

/// 读取 state[key] 的字符串（缺失/非 string 返回 None）。
pub(super) fn state_str(state: &serde_json::Value, key: &str) -> Option<String> {
    state
        .as_object()
        .and_then(|o| o.get(key))
        .and_then(|v| v.as_str())
        .map(str::to_string)
}

/// state["messages"] 中指定 role 的消息数。
pub(super) fn count_role(state: &serde_json::Value, role: &str) -> usize {
    state
        .get("messages")
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter(|m| m.get("role").and_then(|r| r.as_str()) == Some(role))
                .count()
        })
        .unwrap_or(0)
}

/// state["messages"] 中指定 role 的最后一条消息（倒序首个；无则 None）。
pub(super) fn last_role(state: &serde_json::Value, role: &str) -> Option<serde_json::Value> {
    state
        .get("messages")
        .and_then(|v| v.as_array())
        .and_then(|a| {
            a.iter()
                .rev()
                .find(|m| m.get("role").and_then(|r| r.as_str()) == Some(role))
                .cloned()
        })
}
