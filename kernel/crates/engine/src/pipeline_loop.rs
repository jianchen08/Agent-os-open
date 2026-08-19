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

use agentos_config::ConfigCenter;

use agentos_core::traits::{PluginInvoker, StorageBackend};
use agentos_core::types::{
    ContentLoader, EngineError, PipelineConfig, PluginContext, PluginError, PluginResult,
    RouteNext, StepLibrary, TenantContext,
};

use crate::compiler::{
    compile_pipeline, CompiledBody, CompiledItem, CompiledPipeline, CompiledRoute, CompiledStep,
};
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
    /// 统一配置中心（统一配置加载方案 TDD-7）。
    ///
    /// 注入后 loop 内每轮迭代调 `load_agent_into_state` 重读 agent yaml，
    /// 实现"改 agent 配置调整正在跑的任务"（per-iteration 热加载）。
    /// None = 不做 per-iteration 重读（per-run 加载仍由 process_via_engine 负责）。
    config_center: Option<Arc<ConfigCenter>>,
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
            config_center: None,
            pipeline_end_hooks: Vec::new(),
        }
    }

    /// 注入统一配置中心，启用 per-iteration agent 配置热加载（TDD-7）。
    ///
    /// 注入后 loop 内每轮迭代开头重读 agent yaml——
    /// 改 config/agents/<id>.yaml，正在跑的任务下一轮迭代立即用新配置。
    pub fn with_config_center(mut self, cc: Arc<ConfigCenter>) -> Self {
        self.config_center = Some(cc);
        self
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

    /// 执行管道。
    ///
    /// `initial_state` 是初始状态（含 `message` / `agent_id` 等）。
    /// 返回最终 state。
    ///
    /// 流程（多循环体模型，[来源: 任务 §run 逻辑]）：
    /// 1. state 默认设 `"ended" = false`（如未设）
    /// 2. 按 `config.loop_bodies` 顺序执行每个循环体：
    ///    - 每个体按自身 `loop_config` 单次或循环执行 steps（循环受 `max_iterations`
    ///      安全阀约束，>0 时生效；-1=无限）；
    ///    - `"ended"` 只结束当前循环体的循环，之后仍顺序推进（exit 体照常执行）；
    ///    - `"suspended"` 终止整个管道（等待恢复，不推进后续体）；
    ///    - `run_on_error` 的循环体在管道已 ended / 出错时仍执行（收尾语义）；
    ///    - 转移：step 级路由设置的 `state.next_phase` > 循环体 `exit_routes` 命中
    ///      （`RouteNext::Phase`）> 默认顺序进入下一循环体。
    /// 3. 最后一个循环体结束 = run 结束。
    pub async fn run(
        &self,
        config: &PipelineConfig,
        step_library: &StepLibrary,
        initial_state: serde_json::Value,
    ) -> Result<serde_json::Value, EngineError> {
        // G10 兼容路径：现场编译后执行（测试 / 旧调用方）。编译失败 = 加载期
        // 校验失败（when 语法错误 / 未知引用 / 引用环），如实报错不静默——
        // 与"非法 when 静默 false"的旧行为不同，这是 G10 的既定语义升级。
        let compiled = compile_pipeline(config, step_library, &self.plugin_ids).map_err(|e| {
            EngineError::Other {
                message: format!("管道编译失败: {e}"),
            }
        })?;
        self.run_compiled(&compiled, initial_state).await
    }

    /// 执行已编译管道（G10 生产路径：运行时零解析、零三级命中重算）。
    ///
    /// 语义与 [`PipelineExecutor::run`] 完全一致（`run` 内部即编译后转此）；
    /// 差异仅在编译时机——生产路径在启动期 / 热重载时编译一次，`Arc` 原子换入，
    /// 在途 run 持旧计划跑完（快照语义），此处直接消费 [`CompiledPipeline`]。
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
        // G10：循环模式 = loop_config.enabled 或 while_cond 任一开启（编译期已归一）
        let looping = body.looping;
        let max_iters = body.max_iterations;
        if looping {
            loop {
                if truthy_flag(state, "suspended") {
                    break;
                }
                if !ignore_ended && truthy_flag(state, "ended") {
                    break;
                }
                // G10 新 DSL：while 循环继续条件（同一 eval_condition 求值器，
                // 已编译 AST 零解析）；假则退出循环（正常推进后续循环体）。
                if let Some(cond) = &body.while_cond {
                    if !eval_expr(cond, state) {
                        debug!(body = %body.id, "while 条件为假，退出循环");
                        break;
                    }
                }
                iteration += 1;
                if max_iters > 0 && iteration > max_iters {
                    break;
                }
                // 统一配置加载方案 TDD-7：每轮迭代开头重读 agent 配置（per-iteration 热加载）。
                // 改 config/agents/<id>.yaml 后，正在跑的任务下一轮迭代立即用新配置——
                // 实现"边跑边调"（如纠正走偏的 agent / 补漏工具 / 调 max_iterations）。
                // config_center 未注入时跳过（per-run 加载仍由 process_via_engine 负责）。
                if let Some(cc) = &self.config_center {
                    // 先 clone agent_id，避免 state 同时被不可变借用（get）和可变借用（load）
                    let agent_id_opt = state
                        .get("agent_id")
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string());
                    if let Some(agent_id) = agent_id_opt {
                        agentos_config::load_agent_into_state(cc, state, &agent_id);
                    }
                }
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
            0,
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
    /// 表、实录三落点。全量数组形式已退役（零兼容）：收到即 warn 丢弃——改队列
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
                // 此处不再写 state.next_step（旧"记号"语义退役）
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
/// 旧的全量数组 diff 推断（messages_diff_ops）已退役（零兼容）。
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
mod tests {
    use super::*;
    use agentos_core::types::{LoopBody, PipelineStep, Route, StepItem};
    use async_trait::async_trait;
    use serde_json::json;
    use std::collections::HashMap;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    use agentos_core::traits::StorageBackend;
    use agentos_core::types::{
        Branch, CheckpointConfig, LoopConfig, MessageRecord, RunRecord, RunStatus,
        ToolExecutionResult, TraceEntry,
    };

    // ── 测试基础设施 ──────────────────────────────────────────

    /// 可编程的 MockInvoker：按 plugin_id 返回预设的 PluginResult。
    /// 同时统计每个插件被调用的次数 + 捕获每次收到 ctx.config（inputs 通道测试用）。
    struct MockInvoker {
        results: Mutex<HashMap<String, PluginResult>>,
        calls: Mutex<HashMap<String, usize>>,
        configs: Mutex<Vec<(String, serde_json::Value)>>,
    }

    impl MockInvoker {
        fn new() -> Self {
            Self {
                results: Mutex::new(HashMap::new()),
                calls: Mutex::new(HashMap::new()),
                configs: Mutex::new(Vec::new()),
            }
        }

        fn set_result(&self, plugin_id: &str, result: PluginResult) {
            self.results
                .lock()
                .unwrap()
                .insert(plugin_id.to_string(), result);
        }

        fn call_count(&self, plugin_id: &str) -> usize {
            *self.calls.lock().unwrap().get(plugin_id).unwrap_or(&0)
        }

        /// 某插件每次 invoke 收到的 ctx.config（按调用顺序；无调用 = 空 Vec）。
        fn captured_configs(&self, plugin_id: &str) -> Vec<serde_json::Value> {
            self.configs
                .lock()
                .unwrap()
                .iter()
                .filter(|(p, _)| p == plugin_id)
                .map(|(_, c)| c.clone())
                .collect()
        }
    }

    #[async_trait]
    impl PluginInvoker for MockInvoker {
        async fn invoke_pipeline_plugin(
            &self,
            plugin_id: &str,
            ctx: &PluginContext,
        ) -> Result<PluginResult, PluginError> {
            // 计数
            *self
                .calls
                .lock()
                .unwrap()
                .entry(plugin_id.to_string())
                .or_insert(0) += 1;
            // 捕获每插件收到的 config（per-plugin inputs 经此通道）
            self.configs
                .lock()
                .unwrap()
                .push((plugin_id.to_string(), ctx.config.clone()));
            // 返回预设结果（缺失则返回空成功）
            let result = self
                .results
                .lock()
                .unwrap()
                .get(plugin_id)
                .cloned()
                .unwrap_or_default();
            Ok(result)
        }

        async fn invoke_tool(
            &self,
            _plugin_id: &str,
            _tool_name: &str,
            _inputs: &serde_json::Value,
        ) -> Result<ToolExecutionResult, PluginError> {
            Ok(ToolExecutionResult::success(serde_json::Value::Null))
        }

        async fn send_lifecycle_hook(
            &self,
            _plugin_id: &str,
            _hook: agentos_core::traits::LifecycleHook,
            _context: &agentos_core::traits::HookContext,
        ) -> Result<(), PluginError> {
            Ok(())
        }
    }

    /// 空 StorageBackend，仅用于构造 ContentLoader。
    /// 记录收到的 `save_checkpoint` step_no（checkpoint 计步测试读取；其余测试不关心，
    /// 默认 trait 实现为 no-op，这里显式 override 以便观察按步计数触发时机）。
    struct NullStorage {
        checkpoints: Mutex<Vec<i64>>,
        traces: Mutex<usize>,
    }

    impl Default for NullStorage {
        fn default() -> Self {
            Self {
                checkpoints: Mutex::new(Vec::new()),
                traces: Mutex::new(0),
            }
        }
    }

    impl NullStorage {
        /// 每次 save_checkpoint 收到的 step_no（升序）。
        fn saved_step_nos(&self) -> Vec<i64> {
            self.checkpoints.lock().unwrap().clone()
        }

        fn save_count(&self) -> usize {
            self.checkpoints.lock().unwrap().len()
        }

        /// 收到的 append_trace 次数（项级 when 跳过 / 无产出 step 不落 trace 测试用）。
        fn trace_count(&self) -> usize {
            *self.traces.lock().unwrap()
        }
    }

    #[async_trait]
    impl StorageBackend for NullStorage {
        async fn get_run(
            &self,
            _run_id: &str,
        ) -> Result<RunRecord, agentos_core::types::StorageError> {
            Err(agentos_core::types::StorageError::NotFound("null".into()))
        }
        async fn get_messages_by_pipeline(
            &self,
            _pipeline_id: &str,
            _opts: agentos_core::traits::MessageQueryOpts,
        ) -> Result<Vec<MessageRecord>, agentos_core::types::StorageError> {
            Ok(vec![])
        }
        async fn get_blob(
            &self,
            _blob_id: &str,
        ) -> Result<Vec<u8>, agentos_core::types::StorageError> {
            Ok(vec![])
        }
        async fn append_trace(
            &self,
            _entry: TraceEntry,
        ) -> Result<(), agentos_core::types::StorageError> {
            *self.traces.lock().unwrap() += 1;
            Ok(())
        }
        async fn save_checkpoint(
            &self,
            _pipeline_id: &str,
            _tenant_id: &str,
            step_no: i64,
            _state: &serde_json::Value,
        ) -> Result<(), agentos_core::types::StorageError> {
            self.checkpoints.lock().unwrap().push(step_no);
            Ok(())
        }
        async fn create_branch(
            &self,
            _branch: Branch,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn update_run_status(
            &self,
            _run_id: &str,
            _status: RunStatus,
            _branch: Option<&str>,
            _seq: Option<u32>,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn create_run(
            &self,
            _run_id: &str,
            _config_hash: &str,
            _tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn store_blob(
            &self,
            _data: &[u8],
            _mime_type: &str,
        ) -> Result<String, agentos_core::types::StorageError> {
            Ok("null".to_string())
        }
        async fn create_session(
            &self,
            _session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn get_session(
            &self,
            _thread_id: &str,
        ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            Ok(None)
        }
        async fn list_sessions(
            &self,
            _filter: agentos_core::traits::SessionListFilter,
        ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            Ok(vec![])
        }
        async fn update_session(
            &self,
            _session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn delete_session(
            &self,
            _thread_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn link_pipeline_session(
            &self,
            _pipeline_id: &str,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            Ok(vec![])
        }
        async fn get_step_traces_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError>
        {
            Ok(vec![])
        }

        // ── users（0.5.0 最小持久化）：NullStorage 不实现，返回空
        async fn create_user(
            &self,
            _user: &agentos_core::types::UserRecord,
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
        async fn get_user_by_username(
            &self,
            _username: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            Ok(None)
        }
        async fn list_users(
            &self,
        ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            Ok(Vec::new())
        }
        async fn update_last_login(
            &self,
            _user_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn delete_user(
            &self,
            _user_id: &str,
        ) -> Result<bool, agentos_core::types::StorageError> {
            Ok(false)
        }
    }

    /// 测试夹具：构造一个 PipelineExecutor + MockInvoker（可拿引用设置结果）。
    struct Fixture {
        executor: PipelineExecutor,
        invoker: Arc<MockInvoker>,
        store: Arc<NullStorage>,
    }

    impl Fixture {
        fn build(plugin_ids: &[&str]) -> Self {
            let invoker = Arc::new(MockInvoker::new());
            let store = Arc::new(NullStorage::default());
            let executor = PipelineExecutor::new(
                invoker.clone() as Arc<dyn PluginInvoker>,
                PathBuf::from("."),
                TenantContext::new("tenant_test", "session_test"),
                plugin_ids.iter().map(|s| s.to_string()),
                store.clone() as Arc<dyn StorageBackend>,
                "run_test",
                "main",
            );
            Self {
                executor,
                invoker,
                store,
            }
        }

        async fn run(
            &self,
            config: &PipelineConfig,
            library: &StepLibrary,
            initial: serde_json::Value,
        ) -> serde_json::Value {
            self.executor
                .run(config, library, initial)
                .await
                .expect("run should succeed")
        }
    }

    /// 构造简单的 atomic step（引用一个插件名）。
    fn atomic_step(id: &str, plugin: &str) -> PipelineStep {
        PipelineStep {
            id: id.to_string(),
            steps: vec![plugin.into()],
            when: None,
            context: HashMap::new(),
            routes: vec![],
            loop_config: None,
        }
    }

    /// state_updates 辅助构造。
    fn updates(pairs: &[(&str, serde_json::Value)]) -> HashMap<String, serde_json::Value> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.clone()))
            .collect()
    }

    // ── G9 step 级 when 门：invoke 前求值，假则零调用 ──

    fn gated_body(steps: Vec<StepItem>) -> PipelineConfig {
        PipelineConfig {
            name: "gate".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),
                steps: vec![PipelineStep {
                    id: "body".into(),
                    when: None,
                    steps,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                }],
                loop_config: None,
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
        }
    }

    #[tokio::test]
    async fn when_gate_false_skips_item_zero_calls() {
        let fixture = Fixture::build(&["a", "b"]);
        fixture.invoker.set_result(
            "a",
            PluginResult {
                state_updates: updates(&[("a_val", json!(1))]),
                ..Default::default()
            },
        );
        let config = gated_body(vec![
            StepItem::Bare("a".into()),
            StepItem::Gated {
                name: "b".into(),
                when: Some("False".into()),
                inputs: HashMap::new(),
            },
        ]);
        let final_state = fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        assert_eq!(fixture.invoker.call_count("a"), 1);
        assert_eq!(
            fixture.invoker.call_count("b"),
            0,
            "when=False 的项必须零调用"
        );
        assert_eq!(final_state["a_val"], json!(1));
        assert!(final_state.get("b_val").is_none());
    }

    #[tokio::test]
    async fn when_gate_true_runs_item() {
        let fixture = Fixture::build(&["a"]);
        let config = gated_body(vec![StepItem::Gated {
            name: "a".into(),
            when: Some("True".into()),
            inputs: HashMap::new(),
        }]);
        fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        assert_eq!(fixture.invoker.call_count("a"), 1, "when=True 正常执行");
    }

    #[tokio::test]
    async fn invalid_when_expression_fails_compilation() {
        // G10 语义升级：when 语法错误在加载期编译时暴露（不再静默 false 跳过）。
        // 兼容路径 run() 现场编译 → 返回 Err，调用方如实报错。
        let fixture = Fixture::build(&["a"]);
        let config = gated_body(vec![StepItem::Gated {
            name: "a".into(),
            when: Some("this is ((( invalid".into()),
            inputs: HashMap::new(),
        }]);
        let err = fixture
            .executor
            .run(&config, &StepLibrary::default(), json!({}))
            .await
            .unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("编译失败"), "err: {msg}");
        assert!(msg.contains("when"), "err: {msg}");
    }

    #[tokio::test]
    async fn group_when_false_skips_whole_step() {
        let fixture = Fixture::build(&["a", "b"]);
        let mut config = gated_body(vec![StepItem::Bare("a".into()), StepItem::Bare("b".into())]);
        config.loop_bodies[0].steps[0].when = Some("False".into());
        fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        assert_eq!(
            fixture.invoker.call_count("a"),
            0,
            "组级 when=False 组内零调用"
        );
        assert_eq!(fixture.invoker.call_count("b"), 0);
    }

    #[tokio::test]
    async fn gate_reads_state_updated_by_earlier_item() {
        // 门在到达时对 state 求值：前一项把 go 置 True 后，后一项的门放行
        let fixture = Fixture::build(&["a", "b"]);
        fixture.invoker.set_result(
            "a",
            PluginResult {
                state_updates: updates(&[("go", json!(true))]),
                ..Default::default()
            },
        );
        let config = gated_body(vec![
            StepItem::Bare("a".into()),
            StepItem::Gated {
                name: "b".into(),
                when: Some("go == True".into()),
                inputs: HashMap::new(),
            },
        ]);
        fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        assert_eq!(fixture.invoker.call_count("a"), 1);
        assert_eq!(
            fixture.invoker.call_count("b"),
            1,
            "前项更新 state 后门应放行"
        );
    }

    // ── per-plugin inputs（config 通道，不进 state / 不落 trace）────────

    #[tokio::test]
    async fn step_item_inputs_reach_plugin_via_config_without_state_or_trace() {
        // step 项声明 inputs → 插件经 config["inputs"] 收到；不 merge 进 state、
        // 不产生 step diff（插件无其它 state_updates → 0 trace）。
        let fixture = Fixture::build(&["a"]);
        let config = gated_body(vec![StepItem::Gated {
            name: "a".into(),
            when: None,
            inputs: HashMap::from([("mode".into(), json!("strict")), ("limit".into(), json!(5))]),
        }]);
        let final_state = fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        assert_eq!(
            fixture.invoker.captured_configs("a"),
            vec![json!({ "inputs": { "mode": "strict", "limit": 5 } })],
            "插件应经 config.inputs 收到 step 项声明的输入"
        );
        // 不污染 state：无 inputs / mode / limit 顶层键
        assert!(final_state.get("inputs").is_none());
        assert!(final_state.get("mode").is_none());
        assert!(final_state.get("limit").is_none());
        // 插件无产出 + inputs 不进 diff → 0 trace
        assert_eq!(fixture.store.trace_count(), 0, "inputs 不得产生轨迹");
    }

    // ── checkpoint 按「配置 step」计数（组级 when 跳过的 step 不计）─────

    #[tokio::test]
    async fn checkpoint_counts_configured_steps_group_skipped_excluded() {
        // 单次 body，4 个配置 step（s2 组级 when=False 跳过）；interval_steps=2 →
        // 按步：a(1) → c(2) 恰好触发一次 save(step_no=2)；s2 不计步。
        let fixture = Fixture::build(&["a", "b", "c", "d"]);
        let mut config = PipelineConfig {
            name: "ckpt".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),
                steps: vec![
                    atomic_step("s1", "a"),
                    atomic_step("s2", "b"),
                    atomic_step("s3", "c"),
                    atomic_step("s4", "d"),
                ],
                loop_config: None,
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: CheckpointConfig {
                enabled: true,
                interval_steps: 2,
            },
        };
        // 组级 when=False：s2（→b）整个跳过，不计步
        config.loop_bodies[0].steps[1].when = Some("False".into());
        fixture
            .run(
                &config,
                &StepLibrary::default(),
                json!({ "pipeline_id": "p1" }),
            )
            .await;
        // 执行序列：a(1) → [s2 跳过] → c(2) → d(3)。interval=2 → 第 2 步触发一次
        // 中段存档（step_no=2）；run 结束时 persist_run_end 再落最终态（step_no=3）。
        assert_eq!(
            fixture.store.saved_step_nos(),
            vec![2, 3],
            "按步计：第 2 个实际执行的配置 step 触发中段存档；组级 when 跳过的 s2 不计；末尾为 run 结束兜底存档"
        );
        assert_eq!(fixture.store.save_count(), 2, "中段一次 + run 结尾兜底一次");
    }

    #[tokio::test]
    async fn checkpoint_counts_steps_across_loop_rounds_not_rounds() {
        // 每轮 3 个配置 step，循环 2 轮 = 6 步；interval_steps=4 → 按步在第 4 个
        // step 触发一次存档（旧按轮：每 4 轮=12 步才触发）。counting invoker
        // 在第 6 次调用后 set ended 终止：r1 a/b/c + r2 a/b/c。
        let invoker = Arc::new(CountingInvoker {
            counter: Arc::new(AtomicUsize::new(0)),
            stop_after: 6,
            set_suspended_after: 0,
        });
        let store = Arc::new(NullStorage::default());
        let executor = PipelineExecutor::new(
            invoker.clone() as Arc<dyn PluginInvoker>,
            PathBuf::from("."),
            TenantContext::new("t", "s"),
            ["a", "b", "c"].iter().map(|s| s.to_string()),
            store.clone() as Arc<dyn StorageBackend>,
            "r",
            "b",
        );
        let config = PipelineConfig {
            name: "ckpt_loop".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),
                steps: vec![
                    atomic_step("s1", "a"),
                    atomic_step("s2", "b"),
                    atomic_step("s3", "c"),
                ],
                loop_config: Some(LoopConfig {
                    enabled: true,
                    max_iterations: -1,
                }),
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: CheckpointConfig {
                enabled: true,
                interval_steps: 4,
            },
        };
        executor
            .run(
                &config,
                &StepLibrary::default(),
                json!({ "pipeline_id": "p1" }),
            )
            .await
            .expect("run ok");
        assert_eq!(
            store.saved_step_nos(),
            vec![4, 6],
            "按步计跨轮：第 4 个执行 step 触发中段存档（旧按轮则此时远未到阈值）；末尾 6 = run 结束兜底存档"
        );
    }

    // ── 项级 when 跳过：零调用 + 整 step 无产出则不落 trace ────────────

    #[tokio::test]
    async fn when_gate_skipped_item_leaves_no_trace_and_no_call() {
        // a 有 state_updates 产出，但 when=False 跳过 → a 零调用；step 无 context
        // 变更 + 全部项被跳过 = 空 diff → persist_step_trace 早退不落 trace。
        let fixture = Fixture::build(&["a"]);
        fixture.invoker.set_result(
            "a",
            PluginResult {
                state_updates: updates(&[("a_val", json!(1))]),
                ..Default::default()
            },
        );
        let config = gated_body(vec![StepItem::Gated {
            name: "a".into(),
            when: Some("False".into()),
            inputs: HashMap::new(),
        }]);
        let final_state = fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        assert_eq!(fixture.invoker.call_count("a"), 0, "when=False 零调用");
        assert!(final_state.get("a_val").is_none());
        assert_eq!(
            fixture.store.trace_count(),
            0,
            "整 step 被 when 门架空 → 不落 trace"
        );
    }

    /// 计数式 invoker：每次调用计数 +1，可选在第 N 次后 set `ended` 或 `suspended`。
    /// 用于验证循环 / 挂起逻辑。
    struct CountingInvoker {
        counter: Arc<AtomicUsize>,
        /// 第 N 次调用后 set ended=true（0=永不）。stop_after=3 表示第 3 次起 set ended。
        stop_after: usize,
        /// 第 N 次调用后 set suspended=true（0=永不）。优先级低于 stop_after。
        set_suspended_after: usize,
    }

    #[async_trait]
    impl PluginInvoker for CountingInvoker {
        async fn invoke_pipeline_plugin(
            &self,
            _plugin_id: &str,
            _ctx: &PluginContext,
        ) -> Result<PluginResult, PluginError> {
            let n = self.counter.fetch_add(1, Ordering::SeqCst) + 1;
            let mut updates = HashMap::new();
            updates.insert("round".into(), json!(n));
            if self.stop_after > 0 && n >= self.stop_after {
                updates.insert("ended".into(), json!(true));
            } else if self.set_suspended_after > 0 && n >= self.set_suspended_after {
                updates.insert("suspended".into(), json!(true));
            }
            Ok(PluginResult {
                state_updates: updates,
                ..Default::default()
            })
        }
        async fn invoke_tool(
            &self,
            _p: &str,
            _t: &str,
            _i: &serde_json::Value,
        ) -> Result<ToolExecutionResult, PluginError> {
            Ok(ToolExecutionResult::success(serde_json::Value::Null))
        }
        async fn send_lifecycle_hook(
            &self,
            _p: &str,
            _h: agentos_core::traits::LifecycleHook,
            _c: &agentos_core::traits::HookContext,
        ) -> Result<(), PluginError> {
            Ok(())
        }
    }

    /// 用给定 invoker + plugin_ids 构造一个 PipelineExecutor（NullStorage 后端）。
    fn make_executor(invoker: Arc<dyn PluginInvoker>, plugin_ids: &[&str]) -> PipelineExecutor {
        let store: Arc<dyn StorageBackend> = Arc::new(NullStorage::default());
        PipelineExecutor::new(
            invoker,
            PathBuf::from("."),
            TenantContext::new("t", "s"),
            plugin_ids.iter().map(|s| s.to_string()),
            store,
            "r",
            "b",
        )
    }

    // ── 测试用例 ──────────────────────────────────────────────

    #[tokio::test]
    async fn test_single_step_atomic() {
        // 一个 step 引用原子插件；验证插件被调用 + state_updates 被 merge
        let fixture = Fixture::build(&["echo"]);
        fixture.invoker.set_result(
            "echo",
            PluginResult {
                state_updates: updates(&[("reply", json!("hello"))]),
                ..Default::default()
            },
        );
        let config = PipelineConfig {
            name: "single".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),

                steps: vec![atomic_step("s1", "echo")],

                loop_config: None,

                while_cond: None,
                exit_routes: vec![],

                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let library = StepLibrary::default();
        let final_state = fixture.run(&config, &library, json!({})).await;
        assert_eq!(fixture.invoker.call_count("echo"), 1);
        assert_eq!(final_state["reply"], json!("hello"));
        // ended 默认 false
        assert_eq!(final_state["ended"], json!(false));
    }

    #[tokio::test]
    async fn test_composite_step() {
        // 一个组合 step 引用两个子 step（命中规则①：当前管道 step id），验证递归执行。
        //
        // 注意 spec 语义：execute_steps 会遍历 config.steps 中【所有】step，
        // 而 find_step 也在 config.steps 中查。如果 child_a/child_b 同时在 config.steps 里，
        // 它们会被顶层 execute_steps 跑一次，又被 parent 的递归引用跑一次（共 2 次）。
        // 为了精确验证"递归执行"（而不是重复触发），这里只把 parent 放进 config.steps，
        // 子 step 放进公共 step 库——它们只能通过 parent 的引用被命中（命中规则②）。
        let fixture = Fixture::build(&["a", "b"]);
        fixture.invoker.set_result(
            "a",
            PluginResult {
                state_updates: updates(&[("a_val", json!(1))]),
                ..Default::default()
            },
        );
        fixture.invoker.set_result(
            "b",
            PluginResult {
                state_updates: updates(&[("b_val", json!(2))]),
                ..Default::default()
            },
        );
        let config = PipelineConfig {
            name: "composite".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),

                steps: vec![PipelineStep {
                    id: "parent".into(),
                    steps: vec!["child_a".into(), "child_b".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                }],

                loop_config: None,

                while_cond: None,
                exit_routes: vec![],

                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let mut library = StepLibrary::default();
        library
            .steps
            .insert("child_a".to_string(), atomic_step("child_a", "a"));
        library
            .steps
            .insert("child_b".to_string(), atomic_step("child_b", "b"));
        let final_state = fixture.run(&config, &library, json!({})).await;
        assert_eq!(fixture.invoker.call_count("a"), 1);
        assert_eq!(fixture.invoker.call_count("b"), 1);
        assert_eq!(final_state["a_val"], json!(1));
        assert_eq!(final_state["b_val"], json!(2));
    }

    #[tokio::test]
    async fn test_composite_step_double_trigger_semantics() {
        // 补充验证 spec 语义：如果子 step 同时在 config.steps 里，会被执行 2 次
        // （顶层 execute_steps 一次 + parent 递归一次）。这验证"重复触发"是 spec 的
        // 既定行为，而非 bug。
        let fixture = Fixture::build(&["a"]);
        fixture.invoker.set_result(
            "a",
            PluginResult {
                state_updates: updates(&[("a_val", json!(1))]),
                ..Default::default()
            },
        );
        let config = PipelineConfig {
            name: "double".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),

                steps: vec![
                    PipelineStep {
                        id: "parent".into(),
                        steps: vec!["child".into()],
                        when: None,
                        context: HashMap::new(),
                        routes: vec![],
                        loop_config: None,
                    },
                    atomic_step("child", "a"),
                ],

                loop_config: None,

                while_cond: None,
                exit_routes: vec![],

                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let _ = fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        // 顶层执行 parent（递归触发 child 1 次）+ 顶层执行 child（1 次）= 2 次
        assert_eq!(fixture.invoker.call_count("a"), 2);
    }

    #[tokio::test]
    async fn test_step_library() {
        // step 引用公共 step 库的 id（命中规则②）
        let fixture = Fixture::build(&["lib_plugin"]);
        fixture.invoker.set_result(
            "lib_plugin",
            PluginResult {
                state_updates: updates(&[("lib_out", json!("from_library"))]),
                ..Default::default()
            },
        );
        let config = PipelineConfig {
            name: "with_lib".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),

                steps: vec![atomic_step("caller", "shared_step")],

                loop_config: None,

                while_cond: None,
                exit_routes: vec![],

                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let mut library = StepLibrary::default();
        library.steps.insert(
            "shared_step".to_string(),
            atomic_step("shared_step", "lib_plugin"),
        );
        let final_state = fixture.run(&config, &library, json!({})).await;
        assert_eq!(fixture.invoker.call_count("lib_plugin"), 1);
        assert_eq!(final_state["lib_out"], json!("from_library"));
    }

    #[tokio::test]
    async fn test_loop() {
        // loop.enabled=true，插件每次递增计数器；第 3 次 set ended=true 后停止
        let counter = Arc::new(AtomicUsize::new(0));
        let executor = make_executor(
            Arc::new(CountingInvoker {
                counter: counter.clone(),
                stop_after: 3,
                set_suspended_after: 0, // 不挂起
            }) as Arc<dyn PluginInvoker>,
            &["counter_plugin"],
        );
        let config = PipelineConfig {
            name: "loop_test".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),
                steps: vec![atomic_step("body", "counter_plugin")],
                loop_config: Some(agentos_core::types::LoopConfig {
                    enabled: true,
                    max_iterations: -1, // 无限，靠 ended 退出
                }),
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let state = executor
            .run(&config, &StepLibrary::default(), json!({}))
            .await
            .unwrap();
        // 验证循环到第 3 次因 ended 停止
        assert_eq!(counter.load(Ordering::SeqCst), 3);
        assert_eq!(state["round"], json!(3));
        assert_eq!(state["ended"], json!(true));
    }

    #[tokio::test]
    async fn test_loop_max_iterations_safety() {
        // max_iterations=2 安全阀生效；插件从不设 ended，应跑满 2 轮就停
        let counter = Arc::new(AtomicUsize::new(0));
        let executor = make_executor(
            Arc::new(CountingInvoker {
                counter: counter.clone(),
                stop_after: 0, // 永不 set ended
                set_suspended_after: 0,
            }) as Arc<dyn PluginInvoker>,
            &["p"],
        );
        let config = PipelineConfig {
            name: "loop_cap".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),
                steps: vec![atomic_step("body", "p")],
                loop_config: Some(agentos_core::types::LoopConfig {
                    enabled: true,
                    max_iterations: 2,
                }),
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let _ = executor
            .run(&config, &StepLibrary::default(), json!({}))
            .await
            .unwrap();
        assert_eq!(counter.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn test_routes() {
        // routes when 条件匹配后 set 字段 + ended
        let fixture = Fixture::build(&[]);
        let config = PipelineConfig::single_body(
            "routes",
            LoopConfig::default(),
            vec![PipelineStep {
                id: "router".into(),
                steps: vec![], // 不调任何插件
                when: None,
                context: HashMap::new(),
                routes: vec![Route {
                    when: "core_type == 'tool_execute'".into(),
                    then: agentos_core::types::RouteAction {
                        next: RouteNext::End,
                        set: updates(&[("handled", json!(true))]),
                    },
                }],
                loop_config: None,
            }],
        );
        let library = StepLibrary::default();
        let state = fixture
            .run(&config, &library, json!({ "core_type": "tool_execute" }))
            .await;
        assert_eq!(state["handled"], json!(true));
        assert_eq!(state["ended"], json!(true));
    }

    #[tokio::test]
    async fn test_routes_wait() {
        // Wait → suspended=true
        let fixture = Fixture::build(&[]);
        let config = PipelineConfig {
            name: "wait".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),

                steps: vec![PipelineStep {
                    id: "w".into(),
                    steps: vec![],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![Route {
                        when: "True".into(),
                        then: agentos_core::types::RouteAction {
                            next: RouteNext::Wait,
                            set: HashMap::new(),
                        },
                    }],
                    loop_config: None,
                }],

                loop_config: None,

                while_cond: None,
                exit_routes: vec![],

                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let state = fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        assert_eq!(state["suspended"], json!(true));
    }

    #[tokio::test]
    async fn test_unknown_reference_fails_compilation() {
        // G10 语义升级：引用不存在的 step/插件在加载期编译时报错（不静默 error log）。
        let fixture = Fixture::build(&[]);
        let config = PipelineConfig {
            name: "miss".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),

                steps: vec![
                    atomic_step("s1", "ghost_step"), // 既不是 step 也不是已知插件
                    atomic_step("s2", "ghost_plugin"),
                ],

                loop_config: None,

                while_cond: None,
                exit_routes: vec![],

                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let err = fixture
            .executor
            .run(&config, &StepLibrary::default(), json!({}))
            .await
            .unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("ghost_step"), "err: {msg}");
    }

    #[tokio::test]
    async fn test_context_injection() {
        // step.context 的 {{state.xxx}} 被渲染注入到 state
        let fixture = Fixture::build(&["reader"]);
        // 插件读 ctx.state 里的 injected 字段，写回 state 确认注入生效
        fixture.invoker.set_result(
            "reader",
            PluginResult {
                state_updates: updates(&[("seen", json!("{{state.injected}}"))]),
                ..Default::default()
            },
        );
        // 注意：上面结果也会被渲染，但 state_updates merge 时不渲染。
        // 我们改为：context 注入后，直接用 state.injected 验证（不依赖插件）。
        // 这里再写一个 context 注入测试。
        let _ = fixture;
        let fixture2 = Fixture::build(&[]);
        let mut context = HashMap::new();
        context.insert("injected".to_string(), json!("agent={{state.agent_id}}"));
        let config = PipelineConfig::single_body(
            "ctx",
            LoopConfig::default(),
            vec![PipelineStep {
                id: "ctx_step".into(),
                steps: vec![],
                when: None,
                context,
                routes: vec![Route {
                    when: "injected == 'agent=A1'".into(),
                    then: agentos_core::types::RouteAction {
                        next: RouteNext::End,
                        set: updates(&[("matched", json!(true))]),
                    },
                }],
                loop_config: None,
            }],
        );
        let state = fixture2
            .run(
                &config,
                &StepLibrary::default(),
                json!({ "agent_id": "A1" }),
            )
            .await;
        // injected 模板被渲染为 "agent=A1"
        assert_eq!(state["injected"], json!("agent=A1"));
        // 路由条件命中（证明注入确实发生在路由求值之前）
        assert_eq!(state["matched"], json!(true));
        assert_eq!(state["ended"], json!(true));
    }

    #[tokio::test]
    async fn test_skip_remaining() {
        // 插件返回 skip_remaining=true → 跳过同一 step.steps 中后续项
        let fixture = Fixture::build(&["first", "second"]);
        fixture.invoker.set_result(
            "first",
            PluginResult {
                state_updates: updates(&[("first_done", json!(true))]),
                skip_remaining: true,
                ..Default::default()
            },
        );
        fixture.invoker.set_result(
            "second",
            PluginResult {
                state_updates: updates(&[("second_done", json!(true))]),
                ..Default::default()
            },
        );
        let config = PipelineConfig {
            name: "skip".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),

                steps: vec![PipelineStep {
                    id: "both".into(),
                    steps: vec!["first".into(), "second".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                }],

                loop_config: None,

                while_cond: None,
                exit_routes: vec![],

                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let state = fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        assert_eq!(state["first_done"], json!(true));
        // second 不应被调用
        assert_eq!(fixture.invoker.call_count("second"), 0);
        assert!(state.get("second_done").is_none());
    }

    #[tokio::test]
    async fn test_plugin_error_continues() {
        // 插件返回 error → warn + 继续后续 step（不 panic）
        let fixture = Fixture::build(&["bad", "good"]);
        fixture.invoker.set_result(
            "bad",
            PluginResult {
                error: Some(PluginError {
                    message: "boom".into(),
                    code: None,
                    source: None,
                }),
                ..Default::default()
            },
        );
        fixture.invoker.set_result(
            "good",
            PluginResult {
                state_updates: updates(&[("ok", json!(true))]),
                ..Default::default()
            },
        );
        let config = PipelineConfig {
            name: "err".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),

                steps: vec![PipelineStep {
                    id: "seq".into(),
                    steps: vec!["bad".into(), "good".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                }],

                loop_config: None,

                while_cond: None,
                exit_routes: vec![],

                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let state = fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        // good 仍被调用
        assert_eq!(fixture.invoker.call_count("good"), 1);
        assert_eq!(state["ok"], json!(true));
    }

    #[tokio::test]
    async fn test_suspended_stops_loop() {
        // 管道级 loop，插件 set suspended=true → 循环停止（不进入下一轮）
        let counter = Arc::new(AtomicUsize::new(0));
        let executor = make_executor(
            Arc::new(CountingInvoker {
                counter: counter.clone(),
                stop_after: 0,
                set_suspended_after: 2,
            }) as Arc<dyn PluginInvoker>,
            &["p"],
        );
        let config = PipelineConfig {
            name: "suspend_loop".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),
                steps: vec![atomic_step("body", "p")],
                loop_config: Some(agentos_core::types::LoopConfig {
                    enabled: true,
                    max_iterations: 10,
                }),
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let state = executor
            .run(&config, &StepLibrary::default(), json!({}))
            .await
            .unwrap();
        // 跑了 2 轮就停
        assert_eq!(counter.load(Ordering::SeqCst), 2);
        assert_eq!(state["suspended"], json!(true));
        // ended 未被设置
        assert_eq!(state["ended"], json!(false));
    }

    #[tokio::test]
    async fn test_dynamic_plugin_name() {
        // step.steps 里含 {{state.core_plugin}} 模板 → 渲染后命中插件
        let fixture = Fixture::build(&["real_core"]);
        fixture.invoker.set_result(
            "real_core",
            PluginResult {
                state_updates: updates(&[("executed", json!(true))]),
                ..Default::default()
            },
        );
        let config = PipelineConfig {
            name: "dyn".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),

                steps: vec![PipelineStep {
                    id: "dyn_step".into(),
                    steps: vec!["{{state.core_plugin}}".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                }],

                loop_config: None,

                while_cond: None,
                exit_routes: vec![],

                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let state = fixture
            .run(
                &config,
                &StepLibrary::default(),
                json!({ "core_plugin": "real_core" }),
            )
            .await;
        assert_eq!(fixture.invoker.call_count("real_core"), 1);
        assert_eq!(state["executed"], json!(true));
    }

    #[tokio::test]
    async fn test_non_object_initial_state_becomes_object() {
        // 非对象 initial_state 应被规范化为空对象，不 panic
        let fixture = Fixture::build(&[]);
        let config = PipelineConfig {
            name: "noop".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),

                steps: vec![],

                loop_config: None,

                while_cond: None,
                exit_routes: vec![],

                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let state = fixture
            .run(&config, &StepLibrary::default(), serde_json::Value::Null)
            .await;
        assert!(state.is_object());
        assert_eq!(state["ended"], json!(false));
    }

    #[tokio::test]
    async fn test_route_step_jumps_to_target_step() {
        // G10 语义升级：RouteNext::Step(id) 真跳转——j 命中后跳到 t 执行，
        // 不再写 state.next_step 记号。跳转目标须在本循环体（编译期校验）。
        let fixture = Fixture::build(&["a"]);
        let config = PipelineConfig {
            name: "jump".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),

                steps: vec![
                    PipelineStep {
                        id: "j".into(),
                        steps: vec![],
                        when: None,
                        context: HashMap::new(),
                        routes: vec![Route {
                            when: "True".into(),
                            then: agentos_core::types::RouteAction {
                                next: RouteNext::Step("t".into()),
                                set: HashMap::new(),
                            },
                        }],
                        loop_config: None,
                    },
                    PipelineStep {
                        id: "t".into(),
                        steps: vec!["a".into()],
                        when: None,
                        context: HashMap::new(),
                        routes: vec![],
                        loop_config: None,
                    },
                ],

                loop_config: None,

                while_cond: None,
                exit_routes: vec![],

                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let state = fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        assert_eq!(
            fixture.invoker.call_count("a"),
            1,
            "j 跳转到 t 后 t 的插件被执行"
        );
        assert!(
            state.get("next_step").is_none(),
            "Step 真跳转不再写 next_step 记号"
        );
    }

    #[tokio::test]
    async fn test_step_route_self_jump_guard_errors() {
        // 恒跳回自身的 step 路由 → 跳转护栏截断为 Err（不无限执行）。
        // 上限 = steps.len()×4（本例 1 步 → max(4,16)=16 次跳转）：
        // 第 17 次跳转超限返回 Err，插件恰好被调用 17 次（每次跳转前执行一次）。
        let counter = Arc::new(AtomicUsize::new(0));
        let executor = make_executor(
            Arc::new(CountingInvoker {
                counter: counter.clone(),
                stop_after: 0,
                set_suspended_after: 0,
            }) as Arc<dyn PluginInvoker>,
            &["p"],
        );
        let mut jumping = atomic_step("looper", "p");
        jumping.routes = vec![Route {
            when: "True".into(),
            then: agentos_core::types::RouteAction {
                next: RouteNext::Step("looper".into()),
                set: HashMap::new(),
            },
        }];
        let config = PipelineConfig {
            name: "self_jump".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),
                steps: vec![jumping],
                loop_config: None,
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let result = executor
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        let err = result.expect_err("恒自跳路由应被护栏截断为 Err");
        let msg = err.to_string();
        assert!(
            msg.contains("step 级跳转次数超限") && msg.contains("死循环"),
            "错误信息应说明疑似 step 路由死循环：{msg}"
        );
        assert_eq!(
            counter.load(Ordering::SeqCst),
            17,
            "1 步管道上限 16 次跳转，第 17 次截断（每次跳转前插件执行一次）"
        );
    }

    #[tokio::test]
    async fn test_step_level_loop() {
        // step 自带 loop_config（非管道级）：循环执行 step.steps
        let counter = Arc::new(AtomicUsize::new(0));
        let executor = make_executor(
            Arc::new(CountingInvoker {
                counter: counter.clone(),
                stop_after: 3,
                set_suspended_after: 0,
            }) as Arc<dyn PluginInvoker>,
            &["p"],
        );
        let config = PipelineConfig {
            name: "step_loop".into(),
            loop_bodies: vec![LoopBody {
                // 管道级不循环
                id: "main".into(),
                steps: vec![PipelineStep {
                    id: "looper".into(),
                    steps: vec!["p".into()],
                    when: None,
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: Some(agentos_core::types::LoopConfig {
                        enabled: true,
                        max_iterations: -1,
                    }),
                }],
                loop_config: None,
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };
        let state = executor
            .run(&config, &StepLibrary::default(), json!({}))
            .await
            .unwrap();
        // step 自带循环跑了 3 轮（第 3 次 set ended）
        assert_eq!(counter.load(Ordering::SeqCst), 3);
        assert_eq!(state["ended"], json!(true));
    }

    // ════════════════════════════════════════════════════════════════
    // TDD-7: per-iteration agent 配置热加载测试
    // 设计依据：docs/working/重要设计/统一配置加载方案.md TDD-7
    // ════════════════════════════════════════════════════════════════

    #[tokio::test]
    async fn test_config_center_enables_per_iteration_agent_load() {
        // 契约：config_center 注入后，loop 内每轮调 load_agent_into_state，
        // agent yaml 的字段被注入 state（即使 initial_state 没有这些字段）。
        let temp = tempfile::tempdir().unwrap();
        let agents_dir = temp.path().join("agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        std::fs::write(
            agents_dir.join("reload_test.yaml"),
            "system_prompt: 注入的提示词\ncustom: hello\n",
        )
        .unwrap();

        let cc = Arc::new(ConfigCenter::new(temp.path().to_path_buf()));

        let fixture = Fixture::build(&["noop"]);
        fixture.invoker.set_result(
            "noop",
            PluginResult {
                state_updates: updates(&[("ended", json!(true))]),
                ..Default::default()
            },
        );

        let executor = make_executor(fixture.invoker.clone(), &["noop"]).with_config_center(cc);

        let config = PipelineConfig {
            name: "reload_test".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),
                steps: vec![atomic_step("s1", "noop")],
                loop_config: Some(agentos_core::types::LoopConfig {
                    enabled: true,
                    max_iterations: 5,
                }),
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };

        // initial_state 只有 agent_id，没有 system_prompt/custom
        let initial = json!({"agent_id": "reload_test"});

        let final_state = executor
            .run(&config, &StepLibrary::default(), initial)
            .await
            .unwrap();

        // 验证 load_agent_into_state 被调用：agent yaml 字段注入了 state
        assert_eq!(
            final_state["system_prompt"], "注入的提示词",
            "config_center 注入后，loop 应调 load_agent_into_state"
        );
        assert_eq!(final_state["custom"], "hello");
    }

    #[tokio::test]
    async fn test_no_config_center_skips_agent_load() {
        // 契约：config_center 未注入（None）时，loop 内不调 load_agent_into_state，
        // state 不会凭空出现 agent 配置字段（per-run 加载仍由 process_via_engine 负责）。
        let fixture = Fixture::build(&["noop"]);
        fixture.invoker.set_result(
            "noop",
            PluginResult {
                state_updates: updates(&[("ended", json!(true))]),
                ..Default::default()
            },
        );

        // 不调 with_config_center（config_center = None）
        let executor = make_executor(fixture.invoker.clone(), &["noop"]);

        let config = PipelineConfig {
            name: "no_cc".into(),
            loop_bodies: vec![LoopBody {
                id: "main".into(),
                steps: vec![atomic_step("s1", "noop")],
                loop_config: Some(agentos_core::types::LoopConfig {
                    enabled: true,
                    max_iterations: 5,
                }),
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
        };

        let initial = json!({"agent_id": "ghost_agent"});

        let final_state = executor
            .run(&config, &StepLibrary::default(), initial)
            .await
            .unwrap();

        // 没 config_center → 不调 load_agent_into_state → state 不会有 system_prompt
        assert!(
            final_state.get("system_prompt").is_none(),
            "无 config_center 时不应注入 agent 配置"
        );
    }

    // ── inject_run_message_id 直接单元测试（A1/E0502 修复区域）──

    #[test]
    fn test_inject_run_message_id_no_message_id_untouched() {
        // state 无 message_id（或为空）→ ops 原样返回，标志不置位。
        let mut state = json!({"messages": []});
        let ops = vec![json!({"op": "set", "msg": {"role": "assistant", "content": "x"}})];
        let out = inject_run_message_id(&mut state, &ops, -1);
        assert_eq!(out, ops, "无 message_id 时 op 应原样返回");
        assert!(state.get("_assistant_id_assigned").is_none());
    }

    #[test]
    fn test_inject_run_message_id_flag_already_set_untouched() {
        // _assistant_id_assigned=true → 不再注入（每 run 仅一次）。
        let mut state = json!({"message_id": "m_1", "_assistant_id_assigned": true});
        let ops = vec![json!({"op": "set", "msg": {"role": "assistant", "content": "x"}})];
        let out = inject_run_message_id(&mut state, &ops, -1);
        assert_eq!(out, ops);
    }

    #[test]
    fn test_inject_run_message_id_skips_insert_ops() {
        // 非 set 的 op（insert）跳过。
        let mut state = json!({"message_id": "m_2"});
        let ops =
            vec![json!({"op": "insert", "at": 0, "msg": {"role": "assistant", "content": "x"}})];
        let out = inject_run_message_id(&mut state, &ops, -1);
        assert_eq!(out, ops);
        assert!(state.get("_assistant_id_assigned").is_none());
    }

    #[test]
    fn test_inject_run_message_id_skips_old_slot_and_non_object_msg() {
        // 旧槽位（seq <= entry_max_seq）与 msg 非对象 → 均跳过。
        let mut state = json!({"message_id": "m_3"});
        let ops = vec![
            json!({"op": "set", "seq": 0, "msg": {"role": "assistant", "content": "old"}}),
            json!({"op": "set", "seq": 1, "msg": "not-an-object"}),
            json!({"op": "set", "seq": 2, "msg": null}),
        ];
        let out = inject_run_message_id(&mut state, &ops, 5);
        assert_eq!(out, ops);
        assert!(state.get("_assistant_id_assigned").is_none());
    }

    #[test]
    fn test_inject_run_message_id_skips_msg_with_own_id() {
        // assistant 消息自带 id → 不注入（有权威 id 的不用占位）。
        let mut state = json!({"message_id": "m_4"});
        let ops = vec![json!({
            "op": "set",
            "seq": 7,
            "msg": {"role": "assistant", "content": "x", "id": "own_123"}
        })];
        let out = inject_run_message_id(&mut state, &ops, 1);
        assert_eq!(out, ops);
        assert!(state.get("_assistant_id_assigned").is_none());
    }

    #[test]
    fn test_inject_run_message_id_only_first_assistant_append() {
        // 命中：首个新槽位 assistant 追加 op 挂 _message_id + 置位；同批后续不再注入。
        // 注：注入前引擎已把无 seq 的 set 解析为递增 seq（见 apply_messages_op_update），
        // 故此处传带 seq 的 resolved op。
        let mut state = json!({"message_id": "a_run_9"});
        let ops = vec![
            json!({"op": "set", "seq": 1, "msg": {"role": "user", "content": "q"}}),
            json!({"op": "set", "seq": 2, "msg": {"role": "assistant", "content": "第一轮"}}),
            json!({"op": "set", "seq": 3, "msg": {"role": "assistant", "content": "第二轮"}}),
        ];
        let out = inject_run_message_id(&mut state, &ops, 0);
        assert_eq!(out[0].get("_message_id"), None, "user 不注入");
        assert_eq!(out[1]["_message_id"], "a_run_9", "首个 assistant 注入");
        assert_eq!(out[2].get("_message_id"), None, "同批后续不注入");
        assert_eq!(state["_assistant_id_assigned"], true);
        // 注入只挂 op 上，不改消息体
        assert!(out[1]["msg"].get("id").is_none());
    }

    // ── apply_slot_ops_to_array 边界分支 ──

    #[test]
    fn test_apply_slot_ops_insert_shifts_subsequent_seqs() {
        let mut arr = vec![
            json!({"seq": 0, "role": "user"}),
            json!({"seq": 1, "role": "assistant"}),
        ];
        apply_slot_ops_to_array(
            &mut arr,
            &[json!({
                "op": "insert", "at": 0, "msg": {"role": "system"}
            })],
        );
        let seqs: Vec<i64> = arr.iter().map(|m| m["seq"].as_i64().unwrap()).collect();
        assert_eq!(seqs, vec![0, 1, 2], "insert at 0 后段顺延");
        assert_eq!(arr[0]["role"], "system");
    }

    #[test]
    fn test_apply_slot_ops_delete_missing_seq_noop() {
        let mut arr = vec![json!({"seq": 0, "role": "user"})];
        apply_slot_ops_to_array(&mut arr, &[json!({"op": "set", "seq": 5, "msg": null})]);
        assert_eq!(arr.len(), 1, "删除不存在的 seq 应 no-op");
    }

    #[test]
    fn test_apply_slot_ops_ignores_unknown_and_seqless_ops() {
        let mut arr = vec![json!({"seq": 0, "role": "user"})];
        apply_slot_ops_to_array(
            &mut arr,
            &[
                json!({"op": "mystery", "seq": 3, "msg": {"role": "x"}}),
                json!({"op": "set", "msg": {"role": "y"}}),
                json!({"op": "insert", "msg": {"role": "z"}}),
            ],
        );
        assert_eq!(arr.len(), 1, "未知 op / 缺 seq / 缺 at 全部忽略");
    }

    #[test]
    fn test_apply_slot_ops_modify_replaces_same_seq() {
        let mut arr = vec![json!({"seq": 0, "role": "user", "content": "old"})];
        apply_slot_ops_to_array(
            &mut arr,
            &[json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "new"}})],
        );
        assert_eq!(arr.len(), 1);
        assert_eq!(arr[0]["content"], "new");
        assert_eq!(arr[0]["seq"], 0, "modify 保留原 seq");
    }

    // ── op_ledger_entry 实录降级分支 ──

    #[test]
    fn test_op_ledger_entry_insert_and_unknown() {
        // insert 实录：{op, at, message_id, blob_id}
        let insert = op_ledger_entry(&json!({
            "op": "insert", "at": 2, "msg": {"role": "assistant", "content": "hi"}
        }));
        let ins = insert.expect("insert 应产生实录");
        assert_eq!(ins["op"], "insert");
        assert_eq!(ins["at"], 2);
        assert!(ins["message_id"].as_str().unwrap().starts_with("mc_"));
        assert!(ins["blob_id"].is_string());

        // 未知 op → 跳过（前向兼容）
        assert!(op_ledger_entry(&json!({"op": "mystery", "seq": 1})).is_none());
    }

    #[test]
    fn test_op_ledger_entry_set_without_msg_ids_null() {
        // set 但 msg 缺失（delete）→ message_id/blob_id 为 null。
        let e =
            op_ledger_entry(&json!({"op": "set", "seq": 3, "msg": null})).expect("set 应产生实录");
        assert_eq!(e["op"], "set");
        assert_eq!(e["seq"], 3);
        assert!(e["message_id"].is_null());
        assert!(e["blob_id"].is_null());
    }
}
