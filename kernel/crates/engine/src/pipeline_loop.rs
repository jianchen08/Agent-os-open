//! # 配置驱动的管道执行器（Pipeline Executor）
//!
//! 0.2 引擎的"解释执行器"职责实现（ADR ⑥）：读 `PipelineConfig`，按 `steps` 顺序执行，
//! 据 `loop` / `routes` 决定循环与分支。一套执行器 + 不同 YAML = 不同行为。
//!
//! ## 三级命中规则（[来源: 任务 §execute_step_inner]）
//!
//! 对 `step.steps` 里的每一项，按如下顺序解析：
//! ① 当前管道 step id（`config.find_step`）→ 组合节点递归执行
//! ② 公共 step 库（`step_library.find`）→ 组合节点递归执行
//! ③ 插件名（在已知插件集合中）→ 通过 `PluginInvoker` 调用原子插件
//!
//! 三级都 miss：记 `error!` 但不 panic（插件错误统一为记录后继续，不按 error_policy 分发，ADR 2026-08-18）。
//!
//! ## 状态流转
//!
//! `state` 是 `serde_json::Value`（Object）。三个特殊 key 控制流程：
//! - `ended` = true：当前循环体的循环立即终止；run 仍按顺序推进后续循环体
//!   （`run_on_error` 收尾体照常执行），最后一个循环体结束 = run 结束
//! - `suspended` = true：当前轮立即挂起，整个 run 停止推进（等待恢复）
//! - `current_phase`：当前循环体 id（插件据此按阶段分发）
//! - `next_phase`：`RouteNext::Phase` 设置的循环体转移目标（循环体结束时消费）
//!
//! [来源: docs/tasks/task_06_pipeline_engine.md]
//! [来源: docs/working/adr_engine_design.md]

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;

use tracing::{debug, error, warn};

use agentos_core::traits::{PluginInvoker, StorageBackend};
use agentos_core::types::{
    ContentLoader, EngineError, PluginContext, PluginError, PluginResult, RouteNext, TenantContext,
};

use crate::compiler::{CompiledBody, CompiledItem, CompiledPipeline, CompiledRoute, CompiledStep};
use crate::condition::eval_expr;
use crate::template::{render_template, render_value};

/// 配置驱动的管道执行器。
///
/// 持有：
/// - `invoker`：调用原子插件（命中规则③）
/// - `project_root`：`{{path:...}}` 模板解析基准
/// - `default_tenant`：构造 `PluginContext` 时注入的租户上下文
/// - `plugin_ids`：已知插件 id 集合，`lookup_plugin` 在里面查（命中规则③判定）
/// - `store` / `run_id` / `branch_id`：构造 `ContentLoader`（ADR ⑦）
pub struct PipelineExecutor {
    invoker: Arc<dyn PluginInvoker>,
    project_root: PathBuf,
    default_tenant: TenantContext,
    plugin_ids: HashSet<String>,
    store: Arc<dyn StorageBackend>,
    run_id: String,
    branch_id: String,
    /// 监控 M2：engine 自采计数器（监控设计 §三 通道1 + §补引擎调度层）。
    /// 默认 new 一个；生产侧可用 with_metrics 注入共享实例。
    metrics: Arc<crate::metrics::EngineMetrics>,
    /// 分层持久化：插件 manifest 声明需持久化的 state 标量字段（累计型）并集。
    /// 引擎 merge state_updates 时，对在此集合内的 key 走 upsert_state_field 投影；
    /// messages 是系统字段（固定投影），不在此集合内；传送带字段不投影。
    /// 为空时只投影 messages（向后兼容）。
    persistent_fields: HashSet<String>,
    /// checkpoint 计数器：每个配置 step 完成后 +1，达 config.checkpoint.interval_steps
    /// 时落一份全量 state 到 pipeline_checkpoints 并重置。run 间不重置（跨 run 连续累计）。
    /// AtomicI64：persist_step_trace 是 &self（不可变借用），用原子量实现内部可变。
    steps_since_checkpoint: AtomicI64,
    /// 全局累计 step 序号（跨 run），作为 checkpoint 的 step_no 锚点。
    total_step_no: AtomicI64,
    /// per-step messages ops 实录缓冲（ops 即轨迹）。
    ///
    /// merge_and_project 应用插件 ops 时把指纹降级实录（`{op, seq, message_id}`）累积到这，
    /// execute_step_impl 在 step 开头清空，persist_step_trace 在 step 末尾取走落 traces。
    /// 轨迹因此是插件声明的**实录**，不做 diff 推断。Mutex：&self 内部可变。
    ops_ledger: parking_lot::Mutex<Vec<serde_json::Value>>,
    /// 声明了 `on_pipeline_end` 生命周期钩子的插件 id（manifest 收集）。
    ///
    /// run 结束时逐个 best-effort 分发 [`LifecycleHook::OnPipelineEnd`]（HookContext
    /// 携带 pipeline_id/run_id 标签）——spill_guard 的原文清理（spill_retrieve
    /// sidecar 收通知删 `{base}/{pipeline_id}/`）依赖此通道。空集零开销。
    pipeline_end_hooks: Vec<String>,
}

impl PipelineExecutor {
    /// 构造执行器。
    ///
    /// # Arguments
    /// * `invoker` - 插件调用器（命中规则③用）
    /// * `project_root` - 模板 `{{path:...}}` 解析基准
    /// * `default_tenant` - 默认租户上下文
    /// * `plugin_ids` - 已知插件 id 列表（从 manifest 加载）
    /// * `store` - 存储后端，用于构造 `ContentLoader`
    /// * `run_id` / `branch_id` - 当前运行实例 / 分支标识
    pub fn new(
        invoker: Arc<dyn PluginInvoker>,
        project_root: PathBuf,
        default_tenant: TenantContext,
        plugin_ids: impl IntoIterator<Item = String>,
        store: Arc<dyn StorageBackend>,
        run_id: impl Into<String>,
        branch_id: impl Into<String>,
    ) -> Self {
        Self {
            invoker,
            project_root,
            default_tenant,
            plugin_ids: plugin_ids.into_iter().collect(),
            store,
            run_id: run_id.into(),
            branch_id: branch_id.into(),
            metrics: Arc::new(crate::metrics::EngineMetrics::new()),
            persistent_fields: HashSet::new(),
            steps_since_checkpoint: AtomicI64::new(0),
            total_step_no: AtomicI64::new(0),
            ops_ledger: parking_lot::Mutex::new(Vec::new()),
            pipeline_end_hooks: Vec::new(),
        }
    }

    /// 注入声明了 `on_pipeline_end` 钩子的插件 id 集合（spill_guard 清理通道）。
    ///
    /// 生产侧从插件 manifest 的 `capabilities.lifecycle_hooks` 收集；run 结束时
    /// 逐个 best-effort 分发（失败仅 warn，不影响 run 返回值）。不调用为空集。
    pub fn with_pipeline_end_hook_plugins(
        mut self,
        plugin_ids: impl IntoIterator<Item = String>,
    ) -> Self {
        self.pipeline_end_hooks = plugin_ids.into_iter().collect();
        self
    }

    /// 监控 M2：注入共享 engine 计数器（生产侧用，聚合器周期性 snapshot）。
    pub fn with_metrics(mut self, metrics: Arc<crate::metrics::EngineMetrics>) -> Self {
        self.metrics = metrics;
        self
    }

    /// 分层持久化：注入插件 manifest 声明需持久化的 state 字段集合。
    /// 生产侧从插件加载器收集所有插件的 persistent_fields 并集后传入。
    /// 不调用时为空集（向后兼容，只投影 messages）。
    pub fn with_persistent_fields(mut self, fields: impl IntoIterator<Item = String>) -> Self {
        self.persistent_fields = fields.into_iter().collect();
        self
    }

    /// 监控 M2：暴露计数器句柄（聚合器周期性 snapshot）。
    pub fn metrics(&self) -> &Arc<crate::metrics::EngineMetrics> {
        &self.metrics
    }

    /// 已知插件 id 集合（测试/诊断用：`compile_pipeline` 需要 plugin_ids 做
    /// 未知引用校验；生产路径在启动期编译，不经过此访问器）。
    pub fn plugin_ids(&self) -> &HashSet<String> {
        &self.plugin_ids
    }

    /// 执行已编译管道（G10 生产路径：运行时零解析、零三级命中重算）。
    ///
    /// 差异仅在编译时机——生产路径在启动期 / 热重载时编译一次，`Arc` 原子换入，
    /// 在途 run 持旧计划跑完（快照语义），此处直接消费 [`CompiledPipeline`]。
    /// （旧 `run` 兼容路径已随死代码清理删除：生产零调用，测试改走
    /// `compile_pipeline` + 本方法。）
    pub async fn run_compiled(
        &self,
        compiled: &CompiledPipeline,
        initial_state: serde_json::Value,
    ) -> Result<serde_json::Value, EngineError> {
        // 监控 M2：pipeline 执行次数 + 耗时（监控设计 §三 通道1）
        let run_start = std::time::Instant::now();
        let mut state = initial_state;
        ensure_object(&mut state);
        // 默认设 ended=false（如未设）
        if !key_present(&state, "ended") {
            set_key(&mut state, "ended", serde_json::Value::Bool(false));
        }

        // ADR ②③：引擎独占落库。run 开始时建 runs 记录 + 落 user 消息。
        // 失败只 warn 不阻断执行（持久化不应让管道跑不通）。
        self.persist_run_start(&mut state, &compiled.config_hash)
            .await;

        // ── 多循环体执行 ──
        // 转移死循环防护：Phase 跳转/循环体数上限的乘积保险。
        let max_guard = compiled.bodies.len().saturating_mul(4).max(16);
        let mut idx: usize = 0;
        let mut guard: usize = 0;
        while idx < compiled.bodies.len() {
            if guard > max_guard {
                return Err(EngineError::Other {
                    message: format!("循环体转移次数超限（{} 次，疑似 Phase 转移死循环）", guard),
                });
            }
            let body = &compiled.bodies[idx];
            // 挂起：整个管道等待恢复，不再推进任何循环体
            if truthy_flag(&state, "suspended") {
                break;
            }
            // 插件可读 state["current_phase"] 按循环体分发（如 workspace_lifecycle）
            set_key(&mut state, "current_phase", serde_json::json!(body.id));
            // 收尾语义：管道已 ended 时，run_on_error 循环体仍照常执行（忽略 ended）
            let ignore_ended = truthy_flag(&state, "ended") && body.run_on_error;
            let iterations = self
                .execute_body(body, &mut state, compiled, ignore_ended)
                .await?;
            // 监控 M2：迭代轮数（仅 loop 模式计，按循环体累计）
            if iterations > 0 {
                self.metrics.inc_iterations(iterations as u64);
            }

            // ── 循环体间转移决策 ──
            // 优先级：step 级路由设置的 next_phase > 本循环体 exit_routes > 顺序推进
            let mut jump: Option<usize> = None;
            let mut stop = false;
            if let Some(id) = state
                .get("next_phase")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
            {
                // 消费一次性的 next_phase（不残留到 checkpoint/state）
                if let Some(obj) = state.as_object_mut() {
                    obj.remove("next_phase");
                }
                match compiled.body_index(&id) {
                    Some(i) => jump = Some(i),
                    None => {
                        return Err(EngineError::Other {
                            message: format!("路由转移到不存在的循环体: {id}"),
                        })
                    }
                }
            } else if !body.exit_routes.is_empty() && !truthy_flag(&state, "suspended") {
                if let Some(matched) = apply_routes(&body.exit_routes, &mut state) {
                    // apply_routes 的 Phase 分支会写 state.next_phase；此处已消费
                    // 返回值完成转移，立即移除，防止残留导致下一循环体结束时复跳。
                    if let Some(obj) = state.as_object_mut() {
                        obj.remove("next_phase");
                    }
                    match matched {
                        RouteNext::Phase(id) => match compiled.body_index(&id) {
                            Some(i) => jump = Some(i),
                            None => {
                                return Err(EngineError::Other {
                                    message: format!("exit_routes 指向不存在的循环体: {id}"),
                                })
                            }
                        },
                        // End / Wait：终止推进（Wait 已在 apply_routes 置 suspended）
                        RouteNext::End | RouteNext::Wait => stop = true,
                        // Loop / Step：循环体级无意义，忽略走默认顺序推进
                        _ => {}
                    }
                }
            }
            if stop {
                break;
            }
            idx = jump.unwrap_or(idx + 1);
            guard += 1;
        }

        // 监控 M2：pipeline 执行累计耗时
        let elapsed = run_start.elapsed().as_micros() as u64;
        self.metrics.inc_pipeline_exec(elapsed);

        // ADR ②③：一轮结束时落完整 assistant 消息 + 更新 run 状态。
        // 流式期间只更新内存 state，此处 stream_end 一次性原子落库。
        self.persist_run_end(&state).await;

        // on_pipeline_end 钩子分发（spill_guard 原文清理等）：run 结束后逐个
        // best-effort 通知（HookContext 带 pipeline_id/run_id 标签；sidecar 未活
        // 会被 respawn 后收通知）。失败仅 warn——清理类钩子不得让 run 翻车。
        self.dispatch_pipeline_end_hooks(&state).await;

        Ok(state)
    }

    /// 执行单个循环体：按自身 `loop_config` 循环或单次执行 steps。
    ///
    /// `ignore_ended`：收尾语义（exit 体在 `ended=true` 下照常执行）；挂起
    /// （`suspended`）始终终止执行（等待恢复，不跑收尾）。
    ///
    /// 返回迭代轮数（仅 loop 模式计；单次执行返回 0，对齐旧"仅 loop 计迭代"）。
    /// Err = step 级跳转护栏触发（见 [`Self::execute_steps`]），向上传播终止 run。
    async fn execute_body(
        &self,
        body: &CompiledBody,
        state: &mut serde_json::Value,
        compiled: &CompiledPipeline,
        ignore_ended: bool,
    ) -> Result<i32, EngineError> {
        let mut iteration: i32 = 0;
        // G10 单轨：循环模式 = while_cond 存在（编译期已归一）；迭代上限不在
        // 引擎层表达——生产阀门是 stop_check 按 Agent 配置 max_iterations 兜底
        let looping = body.looping;
        if looping {
            loop {
                if truthy_flag(state, "suspended") {
                    break;
                }
                if !ignore_ended && truthy_flag(state, "ended") {
                    break;
                }
                // G10 DSL：while 循环继续条件（同一 eval_condition 求值器，
                // 已编译 AST 零解析）；假则退出循环（正常推进后续循环体）。
                if let Some(cond) = &body.while_cond {
                    if !eval_expr(cond, state) {
                        debug!(body = %body.id, "while 条件为假，退出循环");
                        break;
                    }
                }
                iteration += 1;
                // checkpoint 计数在 persist_step_trace 里按「配置 step」推进
                // （每执行一个配置 step +1，达 interval_steps 落档），此处不再按轮计数。
                self.execute_steps(&body.steps, state, compiled, ignore_ended)
                    .await?;
                if truthy_flag(state, "suspended") {
                    break;
                }
                if !ignore_ended && truthy_flag(state, "ended") {
                    break;
                }
            }
        } else {
            // 单次执行（前处理/后处理体）
            self.execute_steps(&body.steps, state, compiled, ignore_ended)
                .await?;
        }
        Ok(iteration)
    }

    /// 向声明 `on_pipeline_end` 的插件分发管道结束钩子（best-effort）。
    async fn dispatch_pipeline_end_hooks(&self, state: &serde_json::Value) {
        if self.pipeline_end_hooks.is_empty() {
            return;
        }
        let pipeline_id = state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let mut hook_ctx = agentos_core::traits::HookContext::new();
        hook_ctx.set("pipeline_id", serde_json::json!(pipeline_id));
        hook_ctx.set("run_id", serde_json::json!(self.run_id));
        for plugin_id in &self.pipeline_end_hooks {
            if let Err(e) = self
                .invoker
                .send_lifecycle_hook(
                    plugin_id,
                    agentos_core::traits::LifecycleHook::OnPipelineEnd,
                    &hook_ctx,
                )
                .await
            {
                warn!(plugin = %plugin_id, error = ?e, "OnPipelineEnd 分发失败（继续）");
            }
        }
    }

    /// 遍历 step 列表，遇到 `ended`（非收尾模式）/ `suspended` 即停。
    ///
    /// G10：支持 step 级 `RouteNext::Step` **真跳转**（新 DSL "回头"语义——
    /// `then: <step id>` 跳到本循环体内指定 step 重新执行；编译期已校验目标存在）。
    /// 组级 when 门在编译期已 AST 化（`Option<Expr>`），此处只求值零解析。
    ///
    /// 跳转护栏：跳转次数上限 = steps 数 × 4（至少 16），与 run() 的循环体级
    /// 转移护栏同款语义——恒跳回自身的路由在此截断为 Err，而非无限执行。
    async fn execute_steps(
        &self,
        steps: &[CompiledStep],
        state: &mut serde_json::Value,
        compiled: &CompiledPipeline,
        ignore_ended: bool,
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
            let routed = self.execute_step(step, state, compiled, ignore_ended).await;
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
    /// 由于 `execute_step` 与 `execute_step_inner` 相互递归调用（Composite 项会
    /// 递归执行），Rust async fn 无法直接表达无限大小的 future，
    /// 这里用 `Box::pin` 引入间接层（boxed future）打破无限大小。
    fn execute_step<'a>(
        &'a self,
        step: &'a CompiledStep,
        state: &'a mut serde_json::Value,
        compiled: &'a CompiledPipeline,
        ignore_ended: bool,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Option<RouteNext>> + Send + 'a>> {
        Box::pin(self.execute_step_impl(step, state, compiled, ignore_ended))
    }

    async fn execute_step_impl(
        &self,
        step: &CompiledStep,
        state: &mut serde_json::Value,
        compiled: &CompiledPipeline,
        ignore_ended: bool,
    ) -> Option<RouteNext> {
        // 0. 快照 step 执行前的 state，用于事后算 diff 落 step 级轨迹。
        //    轨迹颗粒度 = 配置 step（prepare/core/post 等），不钻插件。
        //    patch_data 含本 step 期间所有顶层 key 变更；messages 走 ops 实录（见 ops_ledger）。
        let state_before = state.clone();
        // 清空本 step 的 messages 实录缓冲（step 边界，防跨 step 泄漏）
        self.ops_ledger.lock().clear();

        // 1. context 注入：渲染 step.context 模板（模板原文保留，动态点），merge 进 state
        let rendered = render_value(
            &serde_json::to_value(&step.context)
                .unwrap_or(serde_json::Value::Object(Default::default())),
            state,
            &self.project_root,
        );
        if let Some(obj) = rendered.as_object() {
            for (k, v) in obj {
                set_key(state, k, v.clone());
            }
        }

        // 2. step 自带 loop_config：循环执行列表项
        if let Some(loop_cfg) = &step.loop_config {
            if loop_cfg.enabled {
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
                    self.execute_step_inner(step, state, compiled, ignore_ended)
                        .await;
                    // 循环体里也应用路由（及时结束/挂起）
                    if !step.routes.is_empty() {
                        apply_routes(&step.routes, state);
                    }
                    if truthy_flag(state, "suspended") {
                        break;
                    }
                    if !ignore_ended && truthy_flag(state, "ended") {
                        break;
                    }
                }
                // 循环 step 执行完，落 step 级轨迹后返回（循环内路由不参与跳转）
                self.persist_step_trace(&step.id, &compiled.checkpoint, &state_before, state)
                    .await;
                return None;
            }
        }

        // 3. 非循环：直接执行
        self.execute_step_inner(step, state, compiled, ignore_ended)
            .await;

        // 4. 路由处理：返回命中结果（Step 跳转由 execute_steps 消费）
        let routed = if !step.routes.is_empty() {
            apply_routes(&step.routes, state)
        } else {
            None
        };

        // 5. 落 step 级轨迹（非循环分支）
        self.persist_step_trace(&step.id, &compiled.checkpoint, &state_before, state)
            .await;
        routed
    }

    /// 落 step 级轨迹：对比 step 执行前后的 state，把变更的顶层 key 聚合为一条
    /// patch_data，plugin_id = step.id。
    ///
    /// **messages 走实录**（ops 即轨迹）：本 step 内插件 emit 的 ops 在
    /// merge_and_project 时已降级为指纹实录累积到 `ops_ledger`，此处取走拼进
    /// patch_data——轨迹记录的是"插件声明过什么"，不是 diff 推断。
    ///
    /// **字段过滤**：已投影到 messages 表的原文字段（raw_result/raw_thinking/
    /// raw_tool_calls）不进 trace——全文真值在 blobs/messages 表，trace 只存指纹。
    /// system_message 保留（追踪提示词演变，state_diff 已去重，仅在变化时记录）。
    /// diff 与实录均为空（step 无产出）则不落轨迹。
    ///
    /// **每执行一个配置 step 必调本函数**（组级 when 跳过的 step 在 execute_steps
    /// 直接 continue，不进本函数）——checkpoint 按步计数即在此推进
    /// （[`Self::count_step_and_maybe_checkpoint`]），保证"实际执行的 step"才计步。
    async fn persist_step_trace(
        &self,
        step_id: &str,
        ckpt: &agentos_core::types::CheckpointConfig,
        state_before: &serde_json::Value,
        state_after: &serde_json::Value,
    ) {
        // checkpoint 按配置 step 计数（在轨迹入口统一推进，含无产出 step——
        // 无产出也消耗了一步；0/禁用 + pipeline_id 为空时内部跳过）。
        self.count_step_and_maybe_checkpoint(ckpt, state_after)
            .await;
        let mut diff = state_diff(state_before, state_after);

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
        }

        // 取走本 step 的 messages 实录，拼进 patch_data
        let ledger: Vec<serde_json::Value> = std::mem::take(&mut *self.ops_ledger.lock());
        if !ledger.is_empty() {
            if let Some(obj) = diff.as_object_mut() {
                obj.insert("messages".into(), serde_json::json!({ "_ops": ledger }));
            }
        }

        // diff 与实录均为空则跳过（step 无产出）
        if diff.as_object().is_none_or(|o| o.is_empty()) {
            // 仍需投影累计标量字段（messages 原文已在 merge 时实时落表）
            self.project_state_snapshot(state_after).await;
            return;
        }

        // 分层持久化投影：累计标量字段用 state_after 的完整值 upsert（覆盖最新值）。
        self.project_state_snapshot(state_after).await;

        use agentos_core::types::{PatchType, TraceEntry};
        let entry = TraceEntry {
            trace_id: format!("t_{}", uuid::Uuid::new_v4().simple()),
            run_id: self.run_id.clone(),
            branch_id: self.branch_id.clone(),
            seq_in_branch: 0,
            plugin_id: step_id.to_string(),
            patch_type: PatchType::StateUpdate,
            patch_data: diff,
            created_at: chrono::Utc::now().to_rfc3339(),
        };
        if let Err(e) = self.store.append_trace(entry).await {
            warn!(step = %step_id, error = %e, "persist_step_trace 失败");
            self.metrics.inc_persist_failure();
        }
    }

    /// 从完整 state 投影到业务表（messages + 声明的累计字段）。
    ///
    /// 在 persist_step_trace 内调用（每个配置 step 后），用 state 的当前完整值投影。
    /// project_messages 内部按索引增量对齐（幂等），upsert_state_field 覆盖最新值。
    /// pipeline_id 为空（测试/首轮未注入）时跳过。
    async fn project_state_snapshot(&self, state: &serde_json::Value) {
        let pipeline_id = state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if pipeline_id.is_empty() {
            return;
        }
        let tenant_id = self.default_tenant.tenant_id.clone();
        // messages 不在此投影——op 模型下 merge 时已实时落 message_slots（一次 apply）。
        // 声明的累计标量字段投影
        for key in &self.persistent_fields {
            if let Some(v) = state.get(key) {
                if let Err(e) = self
                    .store
                    .upsert_state_field(pipeline_id, &tenant_id, key, v)
                    .await
                {
                    warn!(key = %key, error = %e, "upsert_state_field 失败（继续）");
                    self.metrics.inc_persist_failure();
                }
            }
        }
    }

    /// 执行已编译列表项（G10：引用已在加载期解析为三类——插件 / composite / 动态模板）。
    ///
    /// 运行时只做：项级 when AST 求值（零解析）→ 按类别分派。composite 查统一步骤池
    /// 递归执行；动态模板项渲染后走同样的池/插件查找（显式保留的动态点）。
    async fn execute_step_inner(
        &self,
        step: &CompiledStep,
        state: &mut serde_json::Value,
        compiled: &CompiledPipeline,
        ignore_ended: bool,
    ) {
        for item in &step.items {
            if truthy_flag(state, "suspended") {
                break;
            }
            if !ignore_ended && truthy_flag(state, "ended") {
                break;
            }
            // G9 项级 when 门（已编译 AST）：invoke 前求值，假则整项跳过（零调用）。
            // 语法错误在加载期编译时已暴露（不再有"静默 false"）。
            if let Some(cond) = item.when() {
                if !eval_expr(cond, state) {
                    debug!(step = %step.id, "when 门为假，跳过项");
                    continue;
                }
            }
            match item {
                // 静态命中管道/库 step（加载期已定，运行时查池递归）
                CompiledItem::Composite { step_id, .. } => {
                    let target = match compiled.find_step(step_id) {
                        Some(t) => t.clone(),
                        None => {
                            // 编译期保证存在；防御路径（配置在热重载后池缩水）
                            error!(
                                step_or_plugin = %step_id,
                                "composite step '{}' 不在编译池中（配置与插件集不同步），跳过",
                                step_id
                            );
                            continue;
                        }
                    };
                    self.execute_step(&target, state, compiled, ignore_ended)
                        .await;
                }
                // 静态命中插件（per-plugin inputs 经 config 通道传给插件，
                // 不 merge 进 state、不落 trace）
                CompiledItem::Plugin {
                    plugin_id, inputs, ..
                } => {
                    if self.invoke_item_plugin(plugin_id, inputs, state).await {
                        break; // skip_remaining
                    }
                }
                // 动态点：模板名（{{state.xxx}}），渲染后走池 → 插件
                CompiledItem::Dynamic { template, .. } => {
                    let resolved = render_template(template, state, &self.project_root);
                    if let Some(target) = compiled.find_step(&resolved) {
                        let target = target.clone();
                        self.execute_step(&target, state, compiled, ignore_ended)
                            .await;
                    } else if self.lookup_plugin(&resolved) {
                        // 动态点无静态 inputs（模板运行时才定），传空
                        if self
                            .invoke_item_plugin(&resolved, &HashMap::new(), state)
                            .await
                        {
                            break;
                        }
                    } else {
                        error!(
                            step_or_plugin = %resolved,
                            "动态 step/plugin '{}' 未找到，请下载或安装（已记录，继续后续步骤）",
                            resolved
                        );
                    }
                }
            }
        }
    }

    /// 调用原子插件并 merge state_updates；返回 true = 应跳过同组后续
    /// （`skip_remaining`）。插件错误统一 warn + 继续（不再按 error_policy 分发，ADR 2026-08-18）。
    ///
    /// `inputs`：per-plugin 输入（经 config 通道传给插件，不进 state、不落 trace；
    /// 空 = 等价旧行为）。
    async fn invoke_item_plugin(
        &self,
        plugin_id: &str,
        inputs: &HashMap<String, serde_json::Value>,
        state: &mut serde_json::Value,
    ) -> bool {
        match self.invoke_plugin(plugin_id, inputs, state.clone()).await {
            Ok(result) => {
                if result.error.is_none() {
                    // merge state_updates（轨迹在 step 级统一落，不钻插件）
                    // 分层持久化：merge 的同时投影到业务表（messages 增量、声明字段 upsert）
                    self.merge_and_project(state, &result.state_updates).await;
                    result.skip_remaining
                } else {
                    // 引擎统一 warn + 继续（不按 error_policy 分发，ADR 2026-08-18）
                    warn!(
                        plugin = %plugin_id,
                        error = ?result.error,
                        "plugin returned error, continuing (error_policy unified, ADR 2026-08-18)"
                    );
                    false
                }
            }
            Err(e) => {
                // invoker 自身报错（如 sidecar 不可达）：warn + 继续
                warn!(
                    plugin = %plugin_id,
                    error = %e,
                    "plugin invoker error, continuing"
                );
                false
            }
        }
    }

    /// 判定插件是否存在于已知插件集合（命中规则③）。
    fn lookup_plugin(&self, plugin_id: &str) -> bool {
        self.plugin_ids.contains(plugin_id)
    }

    /// 构造 `PluginContext` 并调用 `invoker`。
    ///
    /// `inputs` 走既有 config 通道：非空时填 `config = {"inputs": <inputs>}`
    /// （sidecar 路径 invoker.rs 原样转发 ctx.config，插件在 execute 收 `config`）；
    /// 空时保持空对象 = 旧行为零变化。不进 state → 不产生 step diff/trace。
    async fn invoke_plugin(
        &self,
        plugin_id: &str,
        inputs: &HashMap<String, serde_json::Value>,
        state: serde_json::Value,
    ) -> Result<PluginResult, PluginError> {
        // 监控 M2：step 命中（每 invoke 一次 = 命中一个 step 的插件）
        self.metrics.inc_step_hit();
        let content_loader = ContentLoader::new(
            Arc::clone(&self.store),
            self.run_id.clone(),
            self.branch_id.clone(),
        );
        let config = if inputs.is_empty() {
            serde_json::Value::Object(Default::default())
        } else {
            serde_json::json!({ "inputs": inputs })
        };
        let ctx = PluginContext::new(
            state,
            config,
            self.default_tenant.clone(),
            uuid::Uuid::nil(),
            content_loader,
        );
        // 监控 M2：LLM/工具调用次数 + 耗时（调度层视角 = invoke 前后差，
        // 监控设计 §补引擎调度层）。按 plugin_id 启发式分类：
        // - 含 "llm" → LLM 调用
        // - 含 "tool" → 工具调用
        // - 其他 → 不计 LLM/tool（如 context_build/condition 等编排插件）
        let is_llm = plugin_id.to_lowercase().contains("llm");
        let is_tool = plugin_id.to_lowercase().contains("tool");
        let invoke_start = std::time::Instant::now();
        let result = self.invoker.invoke_pipeline_plugin(plugin_id, &ctx).await;
        let elapsed = invoke_start.elapsed().as_micros() as u64;
        if is_llm {
            self.metrics.inc_llm_call(elapsed);
        } else if is_tool {
            self.metrics.inc_tool_call(elapsed);
        }
        result
    }

    // ── 持久化（ADR ②③：引擎独占落库，插件只返回 Patch）──────────

    /// 创建运行实例（runs 表）并落用户消息（messages 表）。
    ///
    /// 在 run() 开头调用一次。user 消息从 initial_state["message"] 取
    /// （server.rs:242 注入的当前用户输入）。完整内容存 blobs 表，
    /// messages 表存摘要 preview + blob_id 指针（ADR ⑦ 懒加载）。
    async fn persist_run_start(&self, state: &mut serde_json::Value, config_hash: &str) {
        // config_hash = 编译期对 PipelineConfig 的确定性指纹
        // （compiler::pipeline_config_hash：serde_json 规范化 + SHA-256 前 16 hex），
        // 随 CompiledPipeline 走到此落 runs 表。
        let tenant_id = self.default_tenant.tenant_id.clone();
        if let Err(e) = self
            .store
            .create_run(&self.run_id, config_hash, &tenant_id)
            .await
        {
            warn!(run_id = %self.run_id, error = %e, "create_run 落库失败（继续执行）");
            self.metrics.inc_persist_failure();
        }
        // GAP-1 统一：记录 run 的管道归属（state.pipeline_id = effective id），
        // 供按管道挂起/恢复（suspend_pipeline/resume_pipeline）定位 run。
        let run_pipeline_id = state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if !run_pipeline_id.is_empty() {
            let _ = self
                .store
                .set_run_pipeline(&self.run_id, run_pipeline_id)
                .await;
        }

        // pipeline_id 从 state 读（server.rs:261 注入，前端创建会话时生成、每轮回传）。
        // 它是消息层查询主键（对齐 0.1 pipeline_run_id），适配"通过 state 通路执行持久化"。
        let pipeline_id = state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");

        // 写入 pipeline↔session 映射（每次管道开跑时记录，含子任务管道）。
        // 删除会话时据此按 thread_id 找到全部 pipeline_id 级联清理。
        // session_id 即 thread_id（server.rs:474 注入）；两者任一为空则跳过（幂等）。
        let session_id = state
            .get("session_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if let Err(e) = self
            .store
            .link_pipeline_session(pipeline_id, session_id, &tenant_id)
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
                        run_id: self.run_id.clone(),
                        branch_id: self.branch_id.clone(),
                        seq_in_branch: 0,
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
    async fn persist_run_end(&self, final_state: &serde_json::Value) {
        // 收尾 checkpoint：run 结束时无条件落一份最终态快照（最终态是重建的最佳基线）。
        // 这样重建时能直接从本 run 最终 state 接着跑，不必回放整轮 traces。
        // checkpoint_id 用 step_no 锚点，同 step 重放幂等。
        let pipeline_id = final_state
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if !pipeline_id.is_empty() {
            let step_no = self.total_step_no.load(Ordering::SeqCst);
            let tenant_id = self.default_tenant.tenant_id.clone();
            if let Err(e) = self
                .store
                .save_checkpoint(pipeline_id, &tenant_id, step_no, final_state)
                .await
            {
                warn!(pipeline_id = %pipeline_id, error = %e, "收尾 save_checkpoint 失败（继续执行）");
                self.metrics.inc_persist_failure();
            }
        }

        // update_run_status 要求 current_branch 和 current_seq 同时 Some 或同时 None。
        // 标记完成只需更新 status + ended_at，用 (None, None) 不更新 branch/seq。
        if let Err(e) = self
            .store
            .update_run_status(
                &self.run_id,
                agentos_core::types::RunStatus::Completed,
                None,
                None,
            )
            .await
        {
            warn!(run_id = %self.run_id, error = %e, "update_run_status(Completed) 失败");
            self.metrics.inc_persist_failure();
        }
    }

    /// merge 插件 state_updates 进 state（纯内存合并）。
    ///
    /// messages **只接受 op 声明**（`{_ops:[set/insert]}`）→ "一次 apply" 到内存、
    /// 表、实录三落点。全量数组形式零兼容：收到即 warn 丢弃——改队列
    /// 必须走 ops，声明式契约由所有插件（llm_core/tool_core/context_window_guard）履行。
    async fn merge_and_project(
        &self,
        state: &mut serde_json::Value,
        updates: &HashMap<String, serde_json::Value>,
    ) {
        for (k, v) in updates {
            if k == "messages" {
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
                    match apply_messages_op_update(
                        state,
                        self.store.as_ref(),
                        &tenant_id,
                        &ops_owned,
                    )
                    .await
                    {
                        Ok(ledger) => {
                            // ops 即轨迹：指纹实录累积到 per-step 缓冲，step 末尾落 traces
                            if !ledger.is_empty() {
                                self.ops_ledger.lock().extend(ledger);
                            }
                        }
                        Err(e) => {
                            warn!(error = %e, "apply_messages_op_update 失败（继续）");
                            self.metrics.inc_persist_failure();
                        }
                    }
                } else {
                    warn!("messages 更新未携带 _ops（全量数组已退役，零兼容），该更新被忽略");
                }
                continue;
            }
            set_key(state, k, v.clone());
        }
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
    async fn count_step_and_maybe_checkpoint(
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
}

// ── 路由处理 ──────────────────────────────────────────────────

/// 应用转移分支：按 YAML 顺序匹配第一个 `when` 为真的分支，执行其 `then`。
///
/// 匹配后立即 `break`（priority 由 YAML 顺序体现）。返回命中的 `RouteNext`
/// （克隆），供调用方（循环体转移决策 / step 级跳转）使用。
/// `when` 已在加载期编译为 AST（G10）：None = 恒真短路，其余只求值零解析。
fn apply_routes(routes: &[CompiledRoute], state: &mut serde_json::Value) -> Option<RouteNext> {
    for route in routes {
        let matched = match &route.when {
            None => true,
            Some(cond) => eval_expr(cond, state),
        };
        if matched {
            // set 字段
            for (k, v) in &route.set {
                set_key(state, k, v.clone());
            }
            match &route.next {
                RouteNext::Loop => { /* 继续，外层 while 会循环 */ }
                RouteNext::End => {
                    set_key(state, "ended", serde_json::Value::Bool(true));
                }
                RouteNext::Wait => {
                    set_key(state, "suspended", serde_json::Value::Bool(true));
                }
                // Step 真跳转由 execute_steps 消费返回值完成（G10 新 DSL "回头"语义）；
                // 此处不写 state.next_step。
                RouteNext::Step(_id) => {}
                RouteNext::Phase(id) => {
                    // 转移到指定循环体：记到 state.next_phase（run() 在循环体
                    // 结束时消费；step 级路由设置后在本循环体结束时生效）
                    set_key(state, "next_phase", serde_json::Value::String(id.clone()));
                }
            }
            return Some(route.next.clone());
        }
    }
    None
}

// ── state 操作工具 ─────────────────────────────────────────────

/// 确保 state 是 Object（非 Object 时替换为空 Object）。
fn ensure_object(state: &mut serde_json::Value) {
    if !state.is_object() {
        *state = serde_json::Value::Object(serde_json::Map::new());
    }
}

/// state 是否含指定 key。
fn key_present(state: &serde_json::Value, key: &str) -> bool {
    state
        .as_object()
        .map(|o| o.contains_key(key))
        .unwrap_or(false)
}

/// 设置 state[key] = value（state 必须是 Object，否则忽略）。
fn set_key(state: &mut serde_json::Value, key: &str, value: serde_json::Value) {
    if let Some(obj) = state.as_object_mut() {
        obj.insert(key.to_string(), value);
    }
}

/// 读取 state[key] 的布尔值（缺失/非 bool 返回 false）。
fn truthy_flag(state: &serde_json::Value, key: &str) -> bool {
    state
        .as_object()
        .and_then(|o| o.get(key))
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
}

/// 计算 step 执行前后的 state diff：返回 after 中相对 before 变更的顶层 key 及其新值。
/// - 新增的 key：纳入 diff
/// - 值变化的 key：纳入 diff（after 的值）
/// - 值相同的 key：跳过
///
/// 非顶层（深层）变更按整体替换（不递归细粒度 diff），对齐 step 级快照语义。
/// messages **不参与 diff**——它走 ops 实录（ops_ledger，插件声明、指纹降级），
/// 全量数组 diff 推断不适用（零兼容）。
fn state_diff(before: &serde_json::Value, after: &serde_json::Value) -> serde_json::Value {
    let before_obj = before.as_object();
    let after_obj = match after.as_object() {
        Some(o) => o,
        None => return serde_json::Value::Object(Default::default()),
    };
    let mut diff = serde_json::Map::new();
    for (k, v_after) in after_obj {
        if k == "messages" {
            continue; // 轨迹由 ops_ledger 实录提供，diff 不推断
        }
        let changed = match before_obj.and_then(|b| b.get(k)) {
            Some(v_before) => v_before != v_after,
            None => true, // 新增 key
        };
        if changed {
            diff.insert(k.clone(), v_after.clone());
        }
    }
    serde_json::Value::Object(diff)
}

/// 把槽位 op（`set`/`insert`，按**稳定 seq** 寻址）应用到**内存稠密数组** `state["messages"]`。
///
/// 与表侧 `SqliteStore::apply_messages_ops_to_table`（稀疏、留 gap）是**同一组 op 的两个落点**：
/// 引擎收到插件 emit 的 op 后，"一次 apply" 同时更新内存 state 与 DB。详见
/// `docs/message_persistence_design.md`。
///
/// 约定：内存数组稠密（删除会紧凑），每个元素自带稳定 `seq` 字段（≠ 数组下标）。
/// - `set(seq, msg)`：msg 为对象 → 找到该 seq 则替换内容（保留 seq=modify）；找不到则按 seq
///   升序插入（append 或填回某 seq）。
/// - `set(seq, null)`：删除该 seq 的元素（数组紧凑，**幸存元素 seq 不变**）。
/// - `insert(at, msg)`：`seq>=at` 的元素 `seq+1`（后段顺延），新元素占 `at`。
pub fn apply_slot_ops_to_array(arr: &mut Vec<serde_json::Value>, ops: &[serde_json::Value]) {
    fn seq_of(m: &serde_json::Value) -> i64 {
        m.get("seq").and_then(|v| v.as_i64()).unwrap_or(i64::MIN)
    }
    fn set_seq(msg: &mut serde_json::Value, seq: i64) {
        if let Some(o) = msg.as_object_mut() {
            o.insert("seq".into(), serde_json::json!(seq));
        }
    }

    for op in ops {
        let kind = op.get("op").and_then(|v| v.as_str()).unwrap_or("");
        match kind {
            "set" => {
                let Some(seq) = op.get("seq").and_then(|v| v.as_i64()) else {
                    continue;
                };
                match op.get("msg") {
                    Some(msg) if msg.is_object() => {
                        let mut new_msg = msg.clone();
                        set_seq(&mut new_msg, seq);
                        if let Some(pos) = arr.iter().position(|m| seq_of(m) == seq) {
                            arr[pos] = new_msg; // modify：同 seq 替换内容
                        } else {
                            // 新 seq：按升序插入（append 或填回特定 seq）
                            let pos = arr
                                .iter()
                                .position(|m| seq_of(m) > seq)
                                .unwrap_or(arr.len());
                            arr.insert(pos, new_msg);
                        }
                    }
                    _ => {
                        // delete：移除该 seq 元素（数组紧凑，幸存元素 seq 不变）
                        if let Some(pos) = arr.iter().position(|m| seq_of(m) == seq) {
                            arr.remove(pos);
                        }
                    }
                }
            }
            "insert" => {
                let Some(at) = op.get("at").and_then(|v| v.as_i64()) else {
                    continue;
                };
                let Some(msg) = op.get("msg") else {
                    continue;
                };
                // 后段顺延：seq>=at 的元素 seq+1
                for m in arr.iter_mut() {
                    let s = seq_of(m);
                    if s >= at {
                        set_seq(m, s + 1);
                    }
                }
                let mut new_msg = msg.clone();
                set_seq(&mut new_msg, at);
                let pos = arr.iter().position(|m| seq_of(m) > at).unwrap_or(arr.len());
                arr.insert(pos, new_msg);
            }
            _ => {}
        }
    }
}

/// "一次 apply"：把插件 emit 的 messages op **同时**应用到内存 state 与 DB 表。
///
/// 这是新模型（op-based）的接线核心：引擎收到插件 `state_updates["messages"]={_ops:[...]}`
/// 后调用本函数——**同一组 op** 既更新 `state["messages"]`（`apply_slot_ops_to_array`，
/// 稠密、元素带 seq），又落 `message_slots` 表（`apply_messages_ops_to_table`，稀疏、留 gap）。
/// 无 mirror、无 diff。详见 `docs/message_persistence_design.md`。
///
/// 返回**指纹降级实录**（ops 即轨迹）：`[{op, seq, message_id}]`，msg 全文替换为
/// `compute_message_id` 指纹（delete 为 null）——调用方（executor）把它落进 step 轨迹，
/// 轨迹因此是"实际运行的实录"而非事后 diff 推断。
///
/// - `pipeline_id` 从 `state["pipeline_id"]` 读，为空则只更内存、跳过落表（首轮未注入兜底）。
/// - 内存侧无 `messages` 数组时自动 seed 一个空数组。
pub async fn apply_messages_op_update(
    state: &mut serde_json::Value,
    store: &dyn agentos_core::traits::StorageBackend,
    tenant_id: &str,
    ops: &[serde_json::Value],
) -> Result<Vec<serde_json::Value>, agentos_core::types::StorageError> {
    if ops.is_empty() {
        return Ok(vec![]);
    }

    // 1. 内存：把 op 应用到 state["messages"]（稠密数组，元素带 seq）
    if !state.is_object() {
        *state = serde_json::Value::Object(Default::default());
    }
    let need_seed = !matches!(state.get("messages"), Some(v) if v.is_array());
    if need_seed {
        state
            .as_object_mut()
            .expect("ensured object above")
            .insert("messages".into(), serde_json::Value::Array(vec![]));
    }

    // 解析无 seq 的 set（= append）：引擎按 state 现有 max seq 分配递增 seq。
    // 这样 append 类插件（llm_core/tool_core/user push）无需感知 seq——seq 是引擎分配的稳定槽位。
    // 显式带 seq 的 op（modify/fill-gap/insert）照原样透传，并用其 seq 推进 max。
    let mut max_seq = state
        .get("messages")
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|m| m.get("seq").and_then(|s| s.as_i64()))
                .max()
                .unwrap_or(-1)
        })
        .unwrap_or(-1);
    // A1 注入判定基准：resolve 前的旧 max（新槽位 = append 的判据）。
    let entry_max_seq = max_seq;
    let resolved: Vec<serde_json::Value> = ops
        .iter()
        .map(|op| {
            let kind = op.get("op").and_then(|v| v.as_str()).unwrap_or("");
            let explicit = op.get("seq").and_then(|v| v.as_i64());
            match (kind, explicit) {
                ("set", None) => {
                    max_seq += 1;
                    let mut o = op.clone();
                    if let Some(obj) = o.as_object_mut() {
                        obj.insert("seq".into(), serde_json::json!(max_seq));
                    }
                    o
                }
                (_, Some(s)) => {
                    if s > max_seq {
                        max_seq = s;
                    }
                    op.clone()
                }
                _ => op.clone(),
            }
        })
        .collect();

    if let Some(a) = state.get_mut("messages").and_then(|v| v.as_array_mut()) {
        apply_slot_ops_to_array(a, &resolved);
    }

    // 2. 表：同一组 op 落 message_slots（pipeline_id 为空则跳过）
    // 提取为 owned String，避免对 state 的不可变借用跨越后续可变借用（E0502）。
    let pipeline_id = state
        .get("pipeline_id")
        .and_then(|v| v.as_str())
        .map(str::to_string)
        .unwrap_or_default();
    if !pipeline_id.is_empty() {
        // A1：本轮首个 assistant 追加 op 携带内核 message_id（`_message_id` 内部
        // 字段挂 op 上），落表时优先用它作 record_id——流式占位（a_<uuid>）与
        // DB 重载记录 id 对齐，前端去重不再依赖 role::seq 指纹兜底。
        let table_ops = inject_run_message_id(state, &resolved, entry_max_seq);
        store
            .apply_messages_ops_to_table(&pipeline_id, tenant_id, &table_ops)
            .await?;
    }

    // 3. 实录：msg → 指纹降级（compute_message_id 规范化排除 seq，带不带 seq 同指纹）
    Ok(resolved.iter().filter_map(op_ledger_entry).collect())
}

/// A1：把内核 message_id 注入"本轮首个 assistant 追加 op"。
///
/// 注入以 `_message_id` 内部字段挂在 **op** 上而非消息体上：消息是不可变值
/// （blob 内容寻址、LLM 上下文、指纹实录都不应携带它），仅表侧 record_id 消费。
///
/// 命中条件（缺一不可，防止误注入历史消息）：
/// - `set` op 且 msg.role == "assistant" 且 msg 未自带 id；
/// - 新槽位追加（op.seq > resolve 前旧 max）——context_window_guard 等对旧槽位的
///   modify op 带显式旧 seq，天然排除；
/// - 每 run 仅一次：`state["_assistant_id_assigned"]` 置位后，多轮迭代的后续
///   assistant 追加（各自独立消息）不再注入，保证 id 与前端流式占位一一对应。
fn inject_run_message_id(
    state: &mut serde_json::Value,
    resolved: &[serde_json::Value],
    entry_max_seq: i64,
) -> Vec<serde_json::Value> {
    let Some(mid) = state
        .get("message_id")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
    else {
        return resolved.to_vec();
    };
    if state
        .get("_assistant_id_assigned")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
    {
        return resolved.to_vec();
    }
    let mut out: Vec<serde_json::Value> = resolved.to_vec();
    for op in out.iter_mut() {
        if op.get("op").and_then(|v| v.as_str()) != Some("set") {
            continue;
        }
        let is_new_slot = op
            .get("seq")
            .and_then(|v| v.as_i64())
            .map(|s| s > entry_max_seq)
            .unwrap_or(false);
        if !is_new_slot {
            continue;
        }
        let Some(msg) = op.get("msg") else { continue };
        if !msg.is_object() {
            continue;
        }
        if msg.get("role").and_then(|v| v.as_str()) != Some("assistant") {
            continue;
        }
        if msg
            .get("id")
            .and_then(|v| v.as_str())
            .is_some_and(|s| !s.is_empty())
        {
            continue;
        }
        if let Some(obj) = op.as_object_mut() {
            obj.insert("_message_id".to_string(), serde_json::json!(mid));
        }
        if let Some(obj) = state.as_object_mut() {
            obj.insert(
                "_assistant_id_assigned".to_string(),
                serde_json::json!(true),
            );
        }
        break;
    }
    out
}

/// 把单个已解析 op 降级为轨迹实录条目：`{op, seq, message_id, blob_id}`。
///
/// - `set`：msg 为对象 → message_id = 内容指纹 + blob_id = 全文 blob 定位
///   （回退重建按 blob_id 直查 blobs 取全文，指纹仅审计核对）；msg 为 null/缺省
///   （delete）→ 两者皆 null
/// - `insert`：`{op, at, message_id, blob_id}`
/// - 未知 op：跳过（前向兼容）
///
/// blob_id 与表侧写路径（`write_slot_to_table_locked` 的 `ensure_blob_locked`）同源：
/// 都是 `compute_blob_id(serde_json::to_string(msg))`——同一消息必得同一 blob。
pub(crate) fn op_ledger_entry(op: &serde_json::Value) -> Option<serde_json::Value> {
    let kind = op.get("op").and_then(|v| v.as_str()).unwrap_or("");
    let ids = |op: &serde_json::Value| {
        op.get("msg").filter(|m| m.is_object()).map(|m| {
            let blob_src = serde_json::to_string(m).unwrap_or_default();
            (
                agentos_core::ids::compute_message_id(m),
                agentos_core::ids::compute_blob_id(blob_src.as_bytes()),
            )
        })
    };
    match kind {
        "set" => {
            let seq = op.get("seq").and_then(|v| v.as_i64())?;
            let ids = ids(op);
            Some(serde_json::json!({
                "op": "set",
                "seq": seq,
                "message_id": ids.as_ref().map(|(m, _)| m.clone()),
                "blob_id": ids.as_ref().map(|(_, b)| b.clone()),
            }))
        }
        "insert" => {
            let at = op.get("at").and_then(|v| v.as_i64())?;
            let ids = ids(op);
            Some(serde_json::json!({
                "op": "insert",
                "at": at,
                "message_id": ids.as_ref().map(|(m, _)| m.clone()),
                "blob_id": ids.as_ref().map(|(_, b)| b.clone()),
            }))
        }
        _ => None,
    }
}

// ═════════════════════════════════════════════════════════════════
// 单元测试
// ═════════════════════════════════════════════════════════════════

#[cfg(test)]
#[path = "tests/pipeline_loop_tests.rs"]
mod tests;
