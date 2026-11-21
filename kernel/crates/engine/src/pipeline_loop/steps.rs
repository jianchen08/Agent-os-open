//! 步骤执行与边界检查（execute_steps / 触发步 / 并行 for-each）。

use super::finalize::set_key;
use super::finalize::truthy_flag;
use super::*;
use crate::condition::{Expr, PathStep};

impl PipelineExecutor {
    /// 遍历 step 列表，遇到 `ended`（非收尾模式）/ `suspended` 即停。
    ///
    /// G10：支持 step 级 `RouteNext::Step` **真跳转**（新 DSL "回头"语义——
    /// `then: <step id>` 跳到本循环体内指定 step 重新执行；编译期已校验目标存在）。
    /// 组级 when 门在编译期已 AST 化（`Option<Expr>`），此处只求值零解析。
    ///
    /// 跳转护栏：跳转次数上限 = steps 数 × 4（至少 16），与 run() 的循环体级
    /// 转移护栏同款语义——恒跳回自身的路由在此截断为 Err，而非无限执行。
    ///
    /// `run_started`：超时看门狗锚点——每个 step 执行（含其内部全部插件调用
    /// 与并行 for-each）被 `timeout_seconds` 剩余预算包界，到点署名短路。
    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn execute_steps(
        &self,
        steps: &[CompiledStep],
        body_id: &str,
        state: &mut serde_json::Value,
        compiled: &CompiledPipeline,
        ignore_ended: bool,
        run_started: tokio::time::Instant,
    ) -> Result<(), EngineError> {
        let max_jumps = steps.len().saturating_mul(4).max(16);
        let mut jumps: usize = 0;
        let mut i = 0usize;
        while i < steps.len() {
            if truthy_flag(state, "suspended") {
                break;
            }
            if !ignore_ended && truthy_flag(state, "ended") {
                break;
            }
            let step = &steps[i];
            // G9 组级 when 门（已编译 AST）：假则整组跳过（组内零调用、零轨迹）。
            if let Some(cond) = &step.when {
                if !eval_expr(cond, state) {
                    debug!(step = %step.id, "组级 when 门为假，跳过整组");
                    i += 1;
                    continue;
                }
            }
            // checks 统一分发·pre 边界（钩子收敛终局，设计 §4.3）：pre:<步>
            // 触发的 check 在步执行前消费——when 过滤假则零效果；动作 run 先执行
            // 触发步再落主步，route 立即转移（共享跳数护栏），terminate 置
            // ended 短路本循环体。
            match self
                .dispatch_boundary_checks(
                    compiled,
                    body_id,
                    state,
                    CheckBoundary::Pre(step.id.as_str()),
                    false,
                    ignore_ended,
                    run_started,
                )
                .await?
            {
                Some(CheckDivert::Halted) => return Ok(()),
                Some(CheckDivert::Jump(target)) => {
                    jumps += 1;
                    if jumps > max_jumps {
                        return Err(EngineError::Other {
                            message: format!(
                                "check 路由跳转超限（{jumps} 次，上限 {max_jumps}，疑似死循环）"
                            ),
                        });
                    }
                    debug!(step = %step.id, target = %target, "check pre 路由转移");
                    match steps.iter().position(|s| s.id == target) {
                        Some(j) => {
                            i = j;
                            continue;
                        }
                        // 编译期已校验目标存在；防御路径（热重载计划不一致）顺序推进
                        None => warn!(
                            step = %step.id,
                            target = %target,
                            "check 路由目标未在本循环体找到（编译期已校验；防御路径，继续顺序执行）"
                        ),
                    }
                }
                None => {}
            }
            // 超时看门狗执法点：step 执行（含内部全部插件调用/并行 for-each）被
            // 剩余预算包界——到点放弃冻结等待（invoker in-flight guard 随 drop
            // 回落），署名后短路执行，run 收尾按既有路径折算 failed。
            let Some(routed) = budget_await!(
                self,
                run_started,
                state,
                self.execute_step(step, body_id, state, compiled, ignore_ended, None)
            ) else {
                return Ok(());
            };
            let routed = routed?;
            // checks 统一分发·post 边界：post:<步> 触发的 check 在步轨迹落库后
            // 消费；步产出路由（then/RouteNext）优先——check route 在步已产出
            // 路由时抑制（`routed.is_some()`），run/terminate 不受抑制。
            let check_divert = self
                .dispatch_boundary_checks(
                    compiled,
                    body_id,
                    state,
                    CheckBoundary::Post(step.id.as_str()),
                    routed.is_some(),
                    ignore_ended,
                    run_started,
                )
                .await?;
            if let Some(CheckDivert::Halted) = check_divert {
                return Ok(());
            }
            // G10：step 级 Step 跳转（真跳转）——目标下标在本循环体内查找
            if let Some(RouteNext::Step(id)) = routed {
                if let Some(j) = steps.iter().position(|s| s.id == id) {
                    jumps += 1;
                    if jumps > max_jumps {
                        return Err(EngineError::Other {
                            message: format!(
                                "step 级跳转次数超限（{jumps} 次，上限 {max_jumps}，疑似 step 路由死循环）"
                            ),
                        });
                    }
                    debug!(step = %step.id, target = %id, "step 级跳转");
                    i = j;
                    continue;
                }
                // 编译期已校验目标存在；此处为防御路径（如热重载后计划与配置不一致），
                // 跳过跳转继续顺序执行
                warn!(
                    step = %step.id,
                    target = %id,
                    "Step 跳转目标未在本循环体找到（编译期已校验；防御路径，继续顺序执行）"
                );
            }
            // check post 路由转移（步自身未产出路由时才可能到达，护栏①优先级）
            if let Some(CheckDivert::Jump(target)) = check_divert {
                jumps += 1;
                if jumps > max_jumps {
                    return Err(EngineError::Other {
                        message: format!(
                            "check 路由跳转超限（{jumps} 次，上限 {max_jumps}，疑似死循环）"
                        ),
                    });
                }
                debug!(step = %step.id, target = %target, "check post 路由转移");
                match steps.iter().position(|s| s.id == target) {
                    Some(j) => {
                        i = j;
                        continue;
                    }
                    None => warn!(
                        step = %step.id,
                        target = %target,
                        "check 路由目标未在本循环体找到（编译期已校验；防御路径，继续顺序执行）"
                    ),
                }
            }
            i += 1;
        }
        Ok(())
    }

    /// 执行单个 step：context 注入 →（可选）step 自带循环 → 列表项执行 → 路由。
    ///
    /// 注意：step 自带循环时，路由放在循环体内每轮末尾应用（及时结束/挂起）。
    /// 返回 step 级路由命中的 `RouteNext`（G10：`Step` 真跳转由 `execute_steps`
    /// 消费；`Phase` 已在 apply_routes 写 `state.next_phase`，循环体结束时消费）。
    ///
    /// `body_id`：宿主循环体 id（复合递归与 B 区 message 归属定位用）。
    ///
    /// `check_trigger`：触发上下文（checks 统一机制的 `_pipe_check` 约定字段，
    /// config 注入通道）——None = 顺序执行（零改动）；Some = 本步由 check 动作
    /// `run:<步>` 触发执行（一次性触发步），触发上下文随本步全部插件调用透传。
    ///
    /// 由于 `execute_step` 与 `execute_step_inner` 相互递归调用（Composite 项会
    /// 递归执行），Rust async fn 无法直接表达无限大小的 future，
    /// 这里用 `Box::pin` 引入间接层（boxed future）打破无限大小。
    pub(crate) fn execute_step<'a>(
        &'a self,
        step: &'a CompiledStep,
        body_id: &'a str,
        state: &'a mut serde_json::Value,
        compiled: &'a CompiledPipeline,
        ignore_ended: bool,
        check_trigger: Option<serde_json::Value>,
    ) -> std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<Option<RouteNext>, EngineError>> + Send + 'a>,
    > {
        Box::pin(self.execute_step_impl(
            step,
            body_id,
            state,
            compiled,
            ignore_ended,
            check_trigger,
        ))
    }

    pub(crate) async fn execute_step_impl(
        &self,
        step: &CompiledStep,
        body_id: &str,
        state: &mut serde_json::Value,
        compiled: &CompiledPipeline,
        ignore_ended: bool,
        check_trigger: Option<serde_json::Value>,
    ) -> Result<Option<RouteNext>, EngineError> {
        // step 边界清空实录缓冲（messages ops 与顶层键 journal 同点位）。
        // 轨迹颗粒度 = 配置 step（prepare/core/post 等），不钻插件。
        // patch_data 含本 step 期间所有顶层 key 变更；messages 走 ops 实录（见 ops_ledger）。
        self.ops_ledger.lock().clear();
        self.step_key_journal.lock().clear();

        // B 区登记挂点（ADR 2026-08-27 §2.2）：step 进入时登记 message→step
        // 归属（流式窗口 = 本 step invoke 期间），守卫在 step 收尾自动清除。
        let _binding = self.bind_step_message(state, &step.id);

        // 1. context 注入：渲染 step.context 模板（模板原文保留，动态点），merge 进 state
        let rendered = render_value(
            &serde_json::to_value(&step.context)
                .unwrap_or(serde_json::Value::Object(Default::default())),
            state,
            &self.project_root,
        );
        if let Some(obj) = rendered.as_object() {
            for (k, v) in obj {
                self.apply_state_key_update(state, k, v.clone());
            }
        }

        // 2. step 自带 loop_config：循环执行列表项
        if let Some(loop_cfg) = &step.loop_config {
            if loop_cfg.enabled {
                // max_iterations 契约：>0 = 迭代上限；-1 = 无限（缺省）。
                // 0 在 `max_iters > 0` 旧语义下等价无限轮，属非法配置——fail-closed
                // 报错，不静默按无限处理。
                if loop_cfg.max_iterations == 0 {
                    return Err(EngineError::Other {
                        message: format!(
                            "step '{}' 的 loop_config.max_iterations=0 非法（0 = 无限循环已被禁用；>0 = 上限，-1 = 无限）",
                            step.id
                        ),
                    });
                }
                // 并行 for-each（over 存在，编译期已保证与 as 成对、字段合法）：
                // 对集合逐元素执行——单调用 ≡ 循环单元素展开
                //（内容移植自 sync/open-main-20261008 c2db32c26）。
                if loop_cfg.over.is_some() {
                    self.execute_parallel_for(step, body_id, state, compiled, ignore_ended)
                        .await?;
                    // 与串行循环分支同一收尾路径：step 级轨迹
                    self.persist_step_trace(&step.id, &compiled.checkpoint, state)
                        .await?;
                    return Ok(None);
                }
                let max_iters = loop_cfg.max_iterations;
                let mut i: i32 = 0;
                loop {
                    if truthy_flag(state, "suspended") {
                        break;
                    }
                    if !ignore_ended && truthy_flag(state, "ended") {
                        break;
                    }
                    i += 1;
                    if max_iters > 0 && i > max_iters {
                        break;
                    }
                    self.execute_step_inner(
                        step,
                        body_id,
                        state,
                        compiled,
                        ignore_ended,
                        check_trigger.as_ref(),
                    )
                    .await?;
                    // 循环体里也应用路由（及时结束/挂起）
                    if !step.routes.is_empty() {
                        self.apply_routes(&step.routes, state);
                    }
                    if truthy_flag(state, "suspended") {
                        break;
                    }
                    if !ignore_ended && truthy_flag(state, "ended") {
                        break;
                    }
                }
                // 循环 step 执行完，落 step 级轨迹后返回（循环内路由不参与跳转）
                self.persist_step_trace(&step.id, &compiled.checkpoint, state)
                    .await?;
                return Ok(None);
            }
        }

        // 3. 非循环：直接执行
        self.execute_step_inner(
            step,
            body_id,
            state,
            compiled,
            ignore_ended,
            check_trigger.as_ref(),
        )
        .await?;

        // 4. 路由处理：返回命中结果（Step 跳转由 execute_steps 消费）
        let routed = if !step.routes.is_empty() {
            self.apply_routes(&step.routes, state)
        } else {
            None
        };

        // 5. 落 step 级轨迹（非循环分支）
        self.persist_step_trace(&step.id, &compiled.checkpoint, state)
            .await?;

        Ok(routed)
    }

    /// 并行 for-each 迭代执行器：共享 Arc 资源、独立日志/实录/延迟缓冲。
    ///
    /// 与父执行器共享 invoker/store/metrics/hook_bus 等 Arc 资源；键实录与 ops
    /// 实录换新（迭代写面按迭代隔离提取归并），`iteration_depth` +1 使迭代内
    /// persist_step_trace 跳过、messages ops 走延迟缓冲（见两处定义）。
    pub(crate) fn clone_for_iteration(&self) -> Self {
        Self {
            invoker: Arc::clone(&self.invoker),
            project_root: self.project_root.clone(),
            default_tenant: self.default_tenant.clone(),
            plugin_ids: self.plugin_ids.clone(),
            store: Arc::clone(&self.store),
            run_id: self.run_id.clone(),
            metrics: Arc::clone(&self.metrics),
            persistent_fields: self.persistent_fields.clone(),
            metrics_classes: self.metrics_classes.clone(),
            steps_since_checkpoint: AtomicI64::new(0),
            total_step_no: AtomicI64::new(0),
            ops_ledger: parking_lot::Mutex::new(Vec::new()),
            step_key_journal: parking_lot::Mutex::new(HashMap::new()),
            deferred_ops: parking_lot::Mutex::new(Vec::new()),
            iteration_depth: self.iteration_depth + 1,
            iteration_base: None,
            pipeline_end_hooks: self.pipeline_end_hooks.clone(),
            round_events: self.round_events.clone(),
            round_counter: AtomicI64::new(0),
            check_epoch: AtomicI64::new(self.check_epoch.load(Ordering::Relaxed)),
            pending_state_deltas: parking_lot::Mutex::new(Vec::new()),
            hook_bus: self.hook_bus.clone(),
            max_rounds: self.max_rounds,
            run_anchor: parking_lot::Mutex::new(*self.run_anchor.lock()),
            // LLM 连败熔断：迭代执行器与顶层共享同一计数器（Arc）——LLM 面
            // 不可用是 run 级事实，并行迭代内的连败同计一个 run。
            llm_fail_streak: Arc::clone(&self.llm_fail_streak),
            llm_fail_loop_max: self.llm_fail_loop_max,
        }
    }

    /// 单个迭代的产出归并进父 state（按迭代下标/链式序调用，确定性归并）。
    ///
    /// 策略（并行 for-each 原语，内容移植自 sync/open-main-20261008 c2db32c26）：
    /// ① messages ops 父侧统一"一次 apply"（seq 分配/落库/实录/记账全在父）；
    /// ② collect/collect_append 按迭代序拼接（迭代写的是该迭代的数组，逐项追加）；
    /// ③ 其余顶层键 last-迭代-wins（迭代实录为写面清单；as 变量/collect 键/
    ///    终止标志/插件错误走专属策略，不在此列）；
    /// ④ _plugin_errors 拼接；⑤ ended/suspended 任一迭代置位即父置位。
    pub(crate) async fn merge_iteration(
        &self,
        state: &mut serde_json::Value,
        iter_exec: PipelineExecutor,
        iter_state: &serde_json::Value,
        loop_cfg: &agentos_core::types::LoopConfig,
        as_name: &str,
    ) -> Result<(), EngineError> {
        // ① messages ops（先落 DB 后动内存：表写失败上抛终止 run，内存零副作用）
        let ops = std::mem::take(&mut *iter_exec.deferred_ops.lock());
        if !ops.is_empty() {
            let mut updates = HashMap::new();
            updates.insert("messages".to_string(), serde_json::json!({ "_ops": ops }));
            self.merge_and_project(state, &updates).await?;
        }
        // ② collect / collect_append 拼接
        for key in loop_cfg.collect.iter().chain(&loop_cfg.collect_append) {
            if let Some(entries) = iter_state.get(key).and_then(|v| v.as_array()) {
                if entries.is_empty() {
                    continue;
                }
                self.journal_top_key(state, key);
                let mut arr = state
                    .get(key)
                    .and_then(|v| v.as_array())
                    .cloned()
                    .unwrap_or_default();
                arr.extend(entries.iter().cloned());
                set_key(state, key, serde_json::Value::Array(arr));
            }
        }
        // ③ 其余顶层键 last-迭代-wins
        let journal = std::mem::take(&mut *iter_exec.step_key_journal.lock());
        for key in journal.keys() {
            if key == "messages"
                || key == as_name
                || key == "ended"
                || key == "suspended"
                || key == "_plugin_errors"
                || key == super::finalize::PLUGIN_ERROR_STATS_KEY
                || loop_cfg.collect.contains(key)
                || loop_cfg.collect_append.contains(key)
                || loop_cfg.consume_keys.contains(key)
            {
                continue;
            }
            if let Some(v) = iter_state.get(key) {
                self.journal_top_key(state, key);
                set_key(state, key, v.clone());
            }
        }
        // ④ 插件错误账本按 (plugin_id, message) 汇总（计数累加、seq 按归并序
        // 重发）。账本是有界小对象（≤ MAX_PLUGIN_ERROR_ENTRIES 条），不存在
        // 明细数组复制；迭代 state 克隆后已清空账本，各自只含本迭代失败。
        super::finalize::merge_plugin_error_stats(state, iter_state);
        // ⑤ 终止/挂起标志：任一迭代置位即父置位
        for flag in ["ended", "suspended"] {
            if truthy_flag(iter_state, flag) && !truthy_flag(state, flag) {
                self.journal_top_key(state, flag);
                set_key(state, flag, serde_json::Value::Bool(true));
            }
        }
        Ok(())
    }

    /// 并行迭代共享键判定：返回可从迭代 state 剥离、由共享基座承载的
    /// 只读大键。仅在可证明安全时启用（否则返回空 = 全量克隆，行为与现状
    /// 一致，零回归面）：
    /// - 管道无 checks（边界 check 的 when 可能读任意键）；
    /// - 循环体全部是静态插件项，且无 when / inputs / context / routes
    ///   （条件与模板可能在迭代内读被剥离键）；
    /// - 每个体插件都声明了非空 `state.reads`（插件投喂可回填；未声明插件
    ///   走全量投喂，剥键会缺数据）。
    fn iteration_share_keys(
        &self,
        state: &serde_json::Value,
        step: &CompiledStep,
        compiled: &CompiledPipeline,
    ) -> Vec<String> {
        const CANDIDATES: &[&str] = &[
            "messages",
            "system_message",
            "tool_schemas",
            "tool_ids",
            "tool_output_contracts",
        ];
        if !compiled.checks.is_empty()
            || !step.context.is_empty()
            || !step.routes.is_empty()
            || step.items.is_empty()
        {
            return Vec::new();
        }
        if let Some(when) = &step.when {
            if expr_references_any(when, CANDIDATES) {
                return Vec::new();
            }
        }
        for item in &step.items {
            let CompiledItem::Plugin {
                plugin_id,
                when,
                inputs,
                ..
            } = item
            else {
                return Vec::new();
            };
            if !inputs.is_empty() {
                return Vec::new();
            }
            if let Some(w) = when {
                if expr_references_any(w, CANDIDATES) {
                    return Vec::new();
                }
            }
            match self.invoker.plugin_declared_reads(plugin_id) {
                Some(reads) if !reads.is_empty() => {}
                _ => return Vec::new(),
            }
        }
        CANDIDATES
            .iter()
            .filter(|key| state.get(**key).is_some())
            .map(|key| (*key).to_string())
            .collect()
    }

    /// 并行 for-each 执行（LoopConfig.over 存在；内容移植自
    /// sync/open-main-20261008 c2db32c26）。
    ///
    /// - max_concurrency=1：串行链式——每迭代从上一迭代合并后的 state 取快照
    ///   （等价重复循环的链式可见性），迭代完即归并，终止/挂起即停（同串行循环）；
    /// - max_concurrency>1 / -1：有界并行快照——所有迭代从循环开始时的同一快照
    ///   克隆（迭代隔离），buffer_unordered 有界并发，全部完成后按迭代下标序归并。
    ///
    /// 收尾：consume 清 over 源键、consume_keys 清 []（消费即清，杜绝跨轮陈旧）。
    pub(crate) async fn execute_parallel_for(
        &self,
        step: &CompiledStep,
        body_id: &str,
        state: &mut serde_json::Value,
        compiled: &CompiledPipeline,
        ignore_ended: bool,
    ) -> Result<(), EngineError> {
        let loop_cfg = step
            .loop_config
            .as_ref()
            .expect("caller ensured loop_config");
        let as_name = loop_cfg.as_iter.as_deref().expect("compile-time validated");
        let src_key = loop_cfg
            .over
            .as_deref()
            .and_then(|o| o.strip_prefix("state."))
            .expect("compile-time validated")
            .to_string();

        // 迭代元素：缺键视为空批（when 门通常已拦截）；max_iterations 安全阀截断。
        let items: Vec<serde_json::Value> = state
            .get(&src_key)
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let items: Vec<serde_json::Value> = if loop_cfg.max_iterations > 0 {
            items
                .into_iter()
                .take(loop_cfg.max_iterations as usize)
                .collect()
        } else {
            items
        };

        // 每轮置换键先清 []（本轮新鲜产出语义；写前实录进父 step 轨迹）。
        for key in &loop_cfg.collect {
            self.journal_top_key(state, key);
            set_key(state, key, serde_json::Value::Array(vec![]));
        }

        if !items.is_empty()
            && !truthy_flag(state, "suspended")
            && (ignore_ended || !truthy_flag(state, "ended"))
        {
            let concurrency = if loop_cfg.max_concurrency < 0 {
                items.len().max(1)
            } else {
                (loop_cfg.max_concurrency as usize).max(1)
            };
            if concurrency == 1 {
                // 串行链式：迭代完即归并，下一迭代看到合并后的 state。
                for item in items {
                    if truthy_flag(state, "suspended")
                        || (!ignore_ended && truthy_flag(state, "ended"))
                    {
                        break;
                    }
                    let iter_exec = self.clone_for_iteration();
                    let mut iter_state = state.clone();
                    // 迭代账本清零：克隆带入的父账本不得再参与本迭代记账，
                    // 否则归并时父计数被重复累加（历史 _plugin_errors 指数膨胀
                    // 根因）。父账本只由 merge_iteration 收口合并。
                    set_key(
                        &mut iter_state,
                        super::finalize::PLUGIN_ERROR_STATS_KEY,
                        serde_json::json!([]),
                    );
                    set_key(&mut iter_state, as_name, item);
                    iter_exec
                        .execute_step_inner(
                            step,
                            body_id,
                            &mut iter_state,
                            compiled,
                            ignore_ended,
                            None,
                        )
                        .await?;
                    if !step.routes.is_empty() {
                        iter_exec.apply_routes(&step.routes, &mut iter_state);
                    }
                    self.merge_iteration(state, iter_exec, &iter_state, loop_cfg, as_name)
                        .await?;
                }
            } else {
                // 有界并行快照：写集克隆 + buffer_unordered，完成后按下标序归并。
                // 单迭代执行错误不炸整批：记为该迭代错误，归并照常，全部完成后上抛。
                //
                // 写集克隆（用户裁定 2026-11-17）：`messages`/管道不变量等只读
                // 大键只克隆一次进共享基座（Arc），N 个迭代共享；迭代 state 只
                // 含写集与小控制键。插件投喂时按 state.reads 从基座回填——插件
                // 视角与全量 state 一致，内存成本与 state 总量解耦。判定不满足
                // （条件/模板/未声明 reads）时 share_keys 为空 = 全量克隆回退。
                let share_keys = self.iteration_share_keys(state, step, compiled);
                let (base_map, skeleton) = split_iteration_state(state, &share_keys);
                let base_arc = if base_map.is_empty() {
                    None
                } else {
                    Some(Arc::new(base_map))
                };
                use futures::StreamExt;
                let mut results = futures::stream::iter(items.into_iter().enumerate())
                    .map(|(idx, item)| {
                        let mut iter_exec = self.clone_for_iteration();
                        iter_exec.iteration_base = base_arc.clone();
                        let mut iter_state = if base_arc.is_some() {
                            serde_json::Value::Object(skeleton.clone())
                        } else {
                            state.clone()
                        };
                        // 同上：并行迭代从干净账本起算，归并只取本迭代计数。
                        set_key(
                            &mut iter_state,
                            super::finalize::PLUGIN_ERROR_STATS_KEY,
                            serde_json::json!([]),
                        );
                        set_key(&mut iter_state, as_name, item);
                        async move {
                            let outcome = iter_exec
                                .execute_step_inner(
                                    step,
                                    body_id,
                                    &mut iter_state,
                                    compiled,
                                    ignore_ended,
                                    None,
                                )
                                .await;
                            let err_msg = match outcome {
                                Ok(()) => {
                                    if !step.routes.is_empty() {
                                        iter_exec.apply_routes(&step.routes, &mut iter_state);
                                    }
                                    None
                                }
                                Err(e) => Some(e.to_string()),
                            };
                            (idx, iter_exec, iter_state, err_msg)
                        }
                    })
                    .buffer_unordered(concurrency)
                    .collect::<Vec<_>>()
                    .await;
                results.sort_by_key(|(idx, _, _, _)| *idx);
                let mut first_err: Option<String> = None;
                for (_, iter_exec, iter_state, err_msg) in results {
                    self.merge_iteration(state, iter_exec, &iter_state, loop_cfg, as_name)
                        .await?;
                    if err_msg.is_some() && first_err.is_none() {
                        first_err = err_msg;
                    }
                }
                if let Some(msg) = first_err {
                    return Err(EngineError::Other {
                        message: format!("并行 for-each step '{}' 迭代执行失败: {msg}", step.id),
                    });
                }
            }
        }

        // 收尾：消费即清（over 源键 + consume_keys）——与批执行插件「消费即清」
        // 同语义（预填结果批内共享、消费后清除，杜绝跨轮陈旧命中）。
        if loop_cfg.consume {
            self.journal_top_key(state, &src_key);
            set_key(state, &src_key, serde_json::Value::Array(vec![]));
        }
        for key in &loop_cfg.consume_keys {
            self.journal_top_key(state, key);
            set_key(state, key, serde_json::Value::Array(vec![]));
        }
        Ok(())
    }

    /// checks 统一边界分发（钩子收敛终局，设计 §4.3：HookScope/
    /// dispatch_boundary_hooks 泛化为此）。
    ///
    /// 统一模型 = 管道 = 流（顺序 steps + then 跳转）+ checks（触发时机 ×
    /// when × 动作）。分发点仅在触发边界（`execute_steps` 的步前/步后）消费：
    ///
    /// - **pre/post 触发**：`(循环体, 触发步)` 查分发表，按声明序逐条消费——
    ///   when 谓词假则跳过（零效果）；动作 `run` 先执行触发步再落主步（顺序
    ///   语义不变），`route` 立即转移（[`CheckDivert::Jump`]，跳数护栏由调用方
    ///   计），`terminate` 置 `ended=true` 短路（[`CheckDivert::Halted`]，后续
    ///   check 不再消费）。首个转移/终止动作之后的 check 不消费（单边界单转移）。
    /// - **state 触发**：消费本边界的 state_delta 键级事件（写路径
    ///   journal 落点发射、此处统一取走）——订阅键命中事件的 check 才触发
    ///   （触发一次执行一次）；when 求值读当前 state（equals 值条件在此表达）。
    ///   未被任何订阅命中的事件不残留（写路径通知不积压）。
    /// - **护栏①优先级**：`step_routed=true`（post 边界步已产出路由）时 check
    ///   route 抑制（步产出路由优先），run/terminate 不受抑制。
    /// - **边界写归属**（State 单一写面纪律③）：terminate 的 ended 写落
    ///   `check@<at>` 专属窗口轨迹（window_id 即来源标注）。
    ///
    /// 空集短路：无 checks 且无 pending 事件 → 零开销返回。
    #[allow(clippy::too_many_arguments)] // 分发参数面与 execute_steps 同族
    pub(crate) async fn dispatch_boundary_checks(
        &self,
        compiled: &CompiledPipeline,
        body_id: &str,
        state: &mut serde_json::Value,
        boundary: CheckBoundary<'_>,
        step_routed: bool,
        ignore_ended: bool,
        run_started: tokio::time::Instant,
    ) -> Result<Option<CheckDivert>, EngineError> {
        if compiled.checks.is_empty() && self.pending_state_deltas.lock().is_empty() {
            return Ok(None); // 空集短路：无 checks 配置的主干零开销
        }
        // 触发边界匹配 + state_delta 事件统一消费（state 触发在本循环体的每个
        // 步边界都可消费——写后首个边界即触发；只对命中键触发；未命中事件就地
        // 丢弃——写路径通知不积压）
        let deltas = std::mem::take(&mut *self.pending_state_deltas.lock());
        let hit: Vec<CompiledCheck> = compiled
            .checks
            .iter()
            .filter(|c| {
                c.body_id == body_id
                    && (boundary.matches(&c.trigger)
                        || match &c.trigger {
                            // state 触发：仅当订阅键在待消费事件里才进入本边界
                            CheckTrigger::State(key) => deltas.iter().any(|d| &d.key == key),
                            _ => false,
                        })
            })
            .cloned()
            .collect();
        if hit.is_empty() {
            return Ok(None);
        }
        let mut divert: Option<CheckDivert> = None;
        let mut terminate_at: Option<String> = None;
        for check in &hit {
            // when 谓词（None = 恒真；求值读当前 state）
            if let Some(cond) = &check.when {
                if !eval_expr(cond, state) {
                    debug!(body = %body_id, at = %check.at_label(), "check when 为假，不触发");
                    continue;
                }
            }
            // state 触发：订阅键须在本边界消费的事件里（触发一次执行一次）
            if let CheckTrigger::State(key) = &check.trigger {
                if !deltas.iter().any(|d| d.key == *key) {
                    continue;
                }
            }
            match &check.action {
                CheckAction::Run(target) => {
                    self.run_triggered_step(
                        compiled,
                        body_id,
                        state,
                        target,
                        check,
                        &deltas,
                        ignore_ended,
                        run_started,
                    )
                    .await?;
                }
                CheckAction::Route(target) => {
                    if step_routed {
                        // 护栏①：步产出路由优先，check route 仅在步未产出路由时消费
                        debug!(
                            body = %body_id,
                            at = %check.at_label(),
                            target = %target,
                            "步已产出路由，check route 抑制（步产出路由优先）"
                        );
                        continue;
                    }
                    divert = Some(CheckDivert::Jump(target.clone()));
                    break;
                }
                CheckAction::Terminate => {
                    warn!(
                        body = %body_id,
                        at = %check.at_label(),
                        "check terminate——置 ended=true 终止当前循环体"
                    );
                    self.journal_top_key(state, "ended");
                    set_key(state, "ended", serde_json::Value::Bool(true));
                    terminate_at = Some(check.at_label());
                    divert = Some(CheckDivert::Halted);
                    break;
                }
            }
        }
        // 边界写归属（State 单一写面纪律③）：触发所在边界事件带来源标注——
        // check 引擎写（ended 等）落 `check@<at>` 专属窗口轨迹。
        if let Some(at) = terminate_at {
            self.append_window_trace(&format!("check@{at}"), state)
                .await?;
        }
        Ok(divert)
    }

    /// check 动作 `run:<步>` 的一次性触发步执行（设计 §4.3：触发一次执行一次，
    /// 与顺序步同语义——经同一 invoke/轨迹/checkpoint 面）。
    ///
    /// 轨迹身份 = 模板步 id + 触发纪元（`"<步id>#<纪元>"`，State 单一写面纪律
    /// ①：多次触发多条目不混叠）。触发上下文经 `_pipe_check` 约定字段随本步
    /// 全部插件调用透传（at/动作/state_delta 键级 payload）。
    ///
    /// 本步自身路由的状态效果（ended/suspended/next_phase 的 set 写）照常落
    /// state（同语义）；`RouteNext::Step` 跳转返回值不消费——触发边界无跳转
    /// 宿主（run 步不在顺序流位置），warn 留痕。
    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn run_triggered_step(
        &self,
        compiled: &CompiledPipeline,
        body_id: &str,
        state: &mut serde_json::Value,
        target: &str,
        check: &CompiledCheck,
        deltas: &[StateDeltaEvent],
        ignore_ended: bool,
        run_started: tokio::time::Instant,
    ) -> Result<(), EngineError> {
        let Some(mut step) = compiled.find_step(target).cloned() else {
            // 编译期已校验目标存在；防御路径（热重载后计划与配置不一致）
            warn!(
                body = %body_id,
                target = %target,
                "check run 目标步不在编译池（编译期已校验；防御路径，跳过触发）"
            );
            return Ok(());
        };
        let epoch = self.check_epoch.fetch_add(1, Ordering::Relaxed) + 1;
        step.id = format!("{target}#{epoch}");
        // 触发上下文（_pipe_check 约定字段）：at/动作/state_delta 键级 payload
        // （new = 消费时点当前值；窗口内多次写以最终值为准，与轨迹 diff 同语义）
        let state_delta = serde_json::Value::Array(
            deltas
                .iter()
                .map(|d| {
                    serde_json::json!({
                        "key": d.key,
                        "old": d.old,
                        "new": state.get(&d.key).cloned().unwrap_or(serde_json::Value::Null),
                    })
                })
                .collect::<Vec<_>>(),
        );
        let trigger_ctx = serde_json::json!({
            "at": check.at_label(),
            "action": match check.action {
                CheckAction::Run(_) => "run".to_string(),
                _ => String::new(),
            },
            "target": target,
            "epoch": epoch,
            "state_delta": state_delta,
        });
        debug!(
            body = %body_id,
            step = %step.id,
            at = %check.at_label(),
            "check 触发一次性 run 步"
        );
        let Some(routed) = budget_await!(
            self,
            run_started,
            state,
            self.execute_step(
                &step,
                body_id,
                state,
                compiled,
                ignore_ended,
                Some(trigger_ctx)
            )
        ) else {
            // 预算到点署名短路：触发步放弃，主流程按既有收尾路径折算 failed
            return Ok(());
        };
        if let Some(RouteNext::Step(jump)) = routed? {
            warn!(
                step = %step.id,
                target = %jump,
                "check run 步自身产出 Step 跳转（触发边界无跳转宿主，不消费）"
            );
        }
        Ok(())
    }
}

/// 表达式是否引用指定顶层 state 键（并行迭代剥键安全判定）。
///
/// 覆盖条件 AST 全部节点；路径根或 `state.<key>` 首段命中即算引用。
/// 引用被剥离键 → 该循环禁用共享（回退全量克隆），保证迭代内条件求值不变。
fn expr_references_any(expr: &Expr, keys: &[&str]) -> bool {
    match expr {
        Expr::Literal(_) => false,
        Expr::List(items) => items.iter().any(|item| expr_references_any(item, keys)),
        Expr::Path { root, steps } => {
            if keys.contains(&root.as_str()) {
                return true;
            }
            if root == "state" {
                if let Some(PathStep::Field(first)) = steps.first() {
                    return keys.contains(&first.as_str());
                }
            }
            false
        }
        Expr::Not(inner) => expr_references_any(inner, keys),
        Expr::And(left, right) | Expr::Or(left, right) => {
            expr_references_any(left, keys) || expr_references_any(right, keys)
        }
        Expr::Compare { left, right, .. } | Expr::Arith { left, right, .. } => {
            expr_references_any(left, keys) || expr_references_any(right, keys)
        }
        Expr::Fn { args, .. } => args.iter().any(|arg| expr_references_any(arg, keys)),
    }
}

/// 按共享键把父 state 拆成 (只读基座, 迭代骨架)：共享键只克隆一次（Arc 共享），
/// 骨架只含写集与小控制键——迭代内存成本与 state 总量解耦。
fn split_iteration_state(
    state: &serde_json::Value,
    share_keys: &[String],
) -> (
    serde_json::Map<String, serde_json::Value>,
    serde_json::Map<String, serde_json::Value>,
) {
    let mut base = serde_json::Map::new();
    let mut skeleton = serde_json::Map::new();
    if let Some(obj) = state.as_object() {
        for (key, value) in obj {
            if share_keys.iter().any(|shared| shared == key) {
                base.insert(key.clone(), value.clone());
            } else {
                skeleton.insert(key.clone(), value.clone());
            }
        }
    }
    (base, skeleton)
}
